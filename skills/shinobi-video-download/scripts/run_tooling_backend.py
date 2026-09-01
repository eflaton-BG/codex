#!/usr/bin/env python3

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class BackendUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Backend:
    kind: str
    namespace: str
    pod: str
    work_dir: str
    deployment: str | None = None


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Build Shinobi evidence near the DVR service using tooling/console, "
            "with an explicitly requested temporary Deployment also supported."
        )
    )
    parser.add_argument(
        "--execution",
        choices=("auto", "deployment", "console"),
        default="auto",
    )
    parser.add_argument("--context", required=True)
    parser.add_argument("--station", required=True)
    parser.add_argument("--start-local", required=True)
    parser.add_argument("--end-local", required=True)
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--camera", action="append", dest="cameras", required=True)
    parser.add_argument("--layout", choices=("hstack", "separate"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--query-padding-minutes", type=int, default=6)
    parser.add_argument("--download-workers", type=int, default=4)
    parser.add_argument(
        "--copy-retries",
        type=int,
        default=-1,
        help="kubectl cp retries; defaults to infinite.",
    )
    parser.add_argument("--stack-height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--local-port", type=int, default=18089)
    parser.add_argument("--kubectl", default="/usr/local/bin/kubectl")
    parser.add_argument("--tooling-namespace", default="tooling")
    parser.add_argument("--shinobi-namespace", default="dvr")
    parser.add_argument("--shinobi-pod", default="shinobi-0")
    parser.add_argument("--shinobi-secret", default="shinobi-secrets")
    return parser.parse_args()


def load_download_module():
    path = Path(__file__).resolve().parent / "download_originals.py"
    spec = importlib.util.spec_from_file_location("shinobi_download_originals", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_kubectl(
    args,
    kubectl_args,
    *,
    input_data=None,
    text=True,
    check=True,
):
    command_args = list(kubectl_args)
    try:
        remote_boundary = command_args.index("--")
    except ValueError:
        command_args.extend(["--context", args.context])
    else:
        command_args[remote_boundary:remote_boundary] = [
            "--context",
            args.context,
        ]
    command = [
        args.kubectl,
        *command_args,
    ]
    result = subprocess.run(
        command,
        input=input_data,
        text=text,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail.splitlines()[-1] if detail else "kubectl failed")
    return result


def can_i(args, verb, resource):
    result = run_kubectl(
        args,
        [
            "auth",
            "can-i",
            verb,
            resource,
            "-n",
            args.tooling_namespace,
        ],
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "yes"


def list_tooling_pods(args):
    result = run_kubectl(
        args,
        [
            "get",
            "pods",
            "-n",
            args.tooling_namespace,
            "-o",
            "json",
        ],
    )
    return json.loads(result.stdout).get("items", [])


def find_console(args):
    candidates = []
    for pod in list_tooling_pods(args):
        metadata = pod.get("metadata", {})
        status = pod.get("status", {})
        name = metadata.get("name", "")
        labels = metadata.get("labels", {})
        if status.get("phase") != "Running":
            continue
        if not (
            name == "console"
            or name.startswith("console-")
            or labels.get("app") == "console"
            or labels.get("app.kubernetes.io/name") == "console"
        ):
            continue
        containers = pod.get("spec", {}).get("containers", [])
        statuses = status.get("containerStatuses", [])
        if not containers or (statuses and not all(item.get("ready") for item in statuses)):
            continue
        candidates.append((name, containers[0]["image"]))
    if not candidates:
        raise BackendUnavailable(
            f"No ready console pod exists in namespace {args.tooling_namespace}"
        )
    return sorted(candidates)[0]


def cleanup_deployment(args, name):
    result = run_kubectl(
        args,
        [
            "delete",
            "deployment",
            name,
            "-n",
            args.tooling_namespace,
            "--wait=true",
            "--timeout=90s",
        ],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Could not delete deployment {name}: "
            f"{detail.splitlines()[-1] if detail else 'kubectl failed'}"
        )


def create_deployment_backend(args, run_id, image):
    if not can_i(args, "create", "deployments.apps") or not can_i(
        args, "delete", "deployments.apps"
    ):
        raise BackendUnavailable(
            "RBAC does not allow creating and deleting Deployments in tooling"
        )

    name = f"codex-shinobi-{run_id}"
    labels = {"app": "codex-shinobi-worker", "codex-shinobi-run": run_id}
    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": args.tooling_namespace,
            "labels": labels,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"codex-shinobi-run": run_id}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": "worker",
                            "image": image,
                            "command": [
                                "/bin/sh",
                                "-c",
                                "trap 'exit 0' TERM INT; "
                                "while true; do /bin/sleep 3600; done",
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "2", "memory": "2Gi"},
                            },
                        }
                    ]
                },
            },
        },
    }
    created = False
    try:
        run_kubectl(
            args,
            ["create", "-f", "-"],
            input_data=json.dumps(manifest),
        )
        created = True
        run_kubectl(
            args,
            [
                "rollout",
                "status",
                f"deployment/{name}",
                "-n",
                args.tooling_namespace,
                "--timeout=90s",
            ],
        )
        pods = list_tooling_pods(args)
        matches = [
            pod
            for pod in pods
            if pod.get("metadata", {})
            .get("labels", {})
            .get("codex-shinobi-run")
            == run_id
            and pod.get("status", {}).get("phase") == "Running"
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected one running pod for Deployment {name}, found {len(matches)}"
            )
        return Backend(
            kind="deployment",
            namespace=args.tooling_namespace,
            pod=matches[0]["metadata"]["name"],
            work_dir=f"/tmp/codex-shinobi-{run_id}",
            deployment=name,
        )
    except Exception as exc:
        cleanup_error = None
        if created:
            try:
                cleanup_deployment(args, name)
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
        message = f"Could not provision temporary Deployment: {exc}"
        if cleanup_error:
            message += f"; cleanup also failed: {cleanup_error}"
        raise BackendUnavailable(message) from exc


def console_backend(args, run_id, console):
    pod, _ = console
    return Backend(
        kind="console",
        namespace=args.tooling_namespace,
        pod=pod,
        work_dir=f"/tmp/codex-shinobi-{run_id}",
    )


def select_backend(args, run_id):
    console = find_console(args)
    if args.execution == "deployment":
        return create_deployment_backend(args, run_id, console[1])
    return console_backend(args, run_id, console)


def exec_in_backend(
    args,
    backend,
    remote_command,
    *,
    input_data=None,
    text=True,
    check=True,
):
    return run_kubectl(
        args,
        [
            "exec",
            "-i",
            backend.pod,
            "-n",
            backend.namespace,
            "--",
            *remote_command,
        ],
        input_data=input_data,
        text=text,
        check=check,
    )


def upload_worker(args, backend):
    worker_source = (
        Path(__file__).resolve().parent / "shinobi_tooling_worker.py"
    ).read_bytes()
    exec_in_backend(
        args,
        backend,
        ["/bin/mkdir", "-p", backend.work_dir],
    )
    worker_path = f"{backend.work_dir}/worker.py"
    exec_in_backend(
        args,
        backend,
        [
            "/usr/bin/python3",
            "-c",
            (
                "import pathlib,sys; "
                "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
            ),
            worker_path,
        ],
        input_data=worker_source,
        text=False,
    )
    return worker_path


def authenticate(args):
    module = load_download_module()

    class AuthArgs:
        kubectl = args.kubectl
        context = args.context
        namespace = args.shinobi_namespace
        pod = args.shinobi_pod
        secret = args.shinobi_secret
        local_port = args.local_port
        remote_port = 8080

    auth_args = AuthArgs()
    secret_data = module.load_secret(auth_args)
    port_forward = module.start_port_forward(auth_args)
    try:
        token, group_key = module.authenticate(
            f"http://127.0.0.1:{args.local_port}",
            secret_data,
        )
    except Exception:
        port_forward.terminate()
        port_forward.wait(timeout=10)
        raise
    return token, group_key, port_forward


def build_request(args, backend, token, group_key):
    return {
        "base_url": (
            f"http://shinobi.{args.shinobi_namespace}.svc.cluster.local"
        ),
        "auth_token": token,
        "group_key": group_key,
        "station": args.station,
        "start_local": args.start_local,
        "end_local": args.end_local,
        "start_utc": args.start_utc,
        "end_utc": args.end_utc,
        "timezone": args.timezone,
        "cameras": args.cameras,
        "layout": args.layout,
        "query_padding_minutes": args.query_padding_minutes,
        "download_workers": args.download_workers,
        "stack_height": args.stack_height,
        "fps": args.fps,
        "work_dir": backend.work_dir,
    }


def run_worker(args, backend, worker_path, request):
    result = exec_in_backend(
        args,
        backend,
        ["/usr/bin/python3", worker_path],
        input_data=json.dumps(request),
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip()
        try:
            payload = json.loads(result.stdout)
            message = payload.get("message") or message
        except json.JSONDecodeError:
            pass
        message = message.replace(request["auth_token"], "<redacted>")
        raise RuntimeError(message or "tooling worker failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("tooling worker did not return valid JSON") from exc
    if payload.get("status") != "ok":
        raise RuntimeError(payload.get("message", "tooling worker failed"))
    return payload


def safe_export_filename(value):
    path = Path(value)
    if path.name != value or value in {"", ".", ".."}:
        raise RuntimeError(f"Unsafe export filename returned by worker: {value!r}")
    return value


def artifact_command(args, backend, remote_command):
    return [
        args.kubectl,
        "exec",
        "-i",
        backend.pod,
        "-n",
        backend.namespace,
        "--context",
        args.context,
        "--",
        *remote_command,
    ]


def chunked_stream_export_file(
    args,
    backend,
    filename,
    destination,
    expected_size,
):
    partial = destination.with_suffix(destination.suffix + ".part")
    chunk_size = 1024 * 1024
    with partial.open("wb") as stream:
        for offset in range(0, expected_size, chunk_size):
            expected_chunk_size = min(chunk_size, expected_size - offset)
            command = artifact_command(
                args,
                backend,
                [
                    "/bin/dd",
                    f"if={backend.work_dir}/export/{filename}",
                    "iflag=skip_bytes,count_bytes",
                    f"skip={offset}",
                    f"count={expected_chunk_size}",
                    "status=none",
                ],
            )
            last_error = ""
            for attempt in range(1, 6):
                result = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                )
                if (
                    result.returncode == 0
                    and len(result.stdout) == expected_chunk_size
                ):
                    stream.write(result.stdout)
                    break
                detail = result.stderr.decode(errors="replace").strip()
                last_error = detail or (
                    f"expected {expected_chunk_size} bytes, "
                    f"received {len(result.stdout)}"
                )
                if attempt < 5:
                    time.sleep(0.25)
            else:
                raise RuntimeError(
                    f"Could not stream chunk at byte {offset} for {filename}: "
                    f"{last_error.splitlines()[-1]}"
                )
    os.replace(partial, destination)


def stream_export_file(
    args,
    backend,
    filename,
    *,
    expected_size=None,
    expected_sha256=None,
):
    filename = safe_export_filename(filename)
    destination = args.output_dir / filename
    partial = destination.with_suffix(destination.suffix + ".part")
    command = artifact_command(
        args,
        backend,
        ["/bin/cat", f"{backend.work_dir}/export/{filename}"],
    )
    with partial.open("wb") as stream:
        process = subprocess.Popen(
            command,
            stdout=stream,
            stderr=subprocess.PIPE,
        )
        _, stderr = process.communicate()
    use_chunked_fallback = False
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        if expected_size is None:
            raise RuntimeError(
                f"Could not stream tooling artifact {filename}: "
                f"{detail.splitlines()[-1] if detail else 'kubectl exec failed'}"
            )
        use_chunked_fallback = True
    else:
        os.replace(partial, destination)
        if expected_size is not None and destination.stat().st_size != expected_size:
            use_chunked_fallback = True
        elif (
            expected_sha256
            and sha256_file(destination) != expected_sha256
        ):
            if expected_size is None:
                raise RuntimeError(
                    f"SHA-256 mismatch for streamed artifact: {filename}"
                )
            use_chunked_fallback = True

    if use_chunked_fallback:
        chunked_stream_export_file(
            args,
            backend,
            filename,
            destination,
            expected_size,
        )

    if expected_size is not None and destination.stat().st_size != expected_size:
        raise RuntimeError(f"Size mismatch for streamed artifact: {filename}")
    if expected_sha256 and sha256_file(destination) != expected_sha256:
        raise RuntimeError(f"SHA-256 mismatch for streamed artifact: {filename}")


def copy_export(args, backend, worker_result):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    copy_result = run_kubectl(
        args,
        [
            "cp",
            f"{backend.namespace}/{backend.pod}:{backend.work_dir}/export/.",
            str(args.output_dir),
            "-n",
            backend.namespace,
            f"--retries={args.copy_retries}",
        ],
        check=False,
    )
    if copy_result.returncode == 0:
        return

    filenames = [
        (
            worker_result["manifest"],
            worker_result.get("manifest_bytes"),
            worker_result["manifest_sha256"],
        ),
        ("result.json", None, None),
        *(
            (item["filename"], int(item["bytes"]), item["sha256"])
            for item in worker_result["videos"]
        ),
        *(
            (item["filename"], item.get("bytes"), item["sha256"])
            for item in worker_result["qa_frames"]
        ),
    ]
    unique = {}
    for filename, size, digest in filenames:
        unique.setdefault(filename, (size, digest))
    for filename, (size, digest) in unique.items():
        stream_export_file(
            args,
            backend,
            filename,
            expected_size=int(size) if size is not None else None,
            expected_sha256=digest,
        )


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_and_expand_result(args, backend):
    result_path = args.output_dir / "result.json"
    if not result_path.is_file():
        raise RuntimeError("Copied artifacts do not contain result.json")
    result = json.loads(result_path.read_text(encoding="utf-8"))

    def expanded(item):
        path = args.output_dir / item["filename"]
        if not path.is_file():
            raise RuntimeError(f"Copied artifact is missing: {path.name}")
        actual = sha256_file(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"SHA-256 mismatch for copied artifact: {path.name}")
        return {"path": str(path.resolve()), **item}

    videos = [expanded(item) for item in result["videos"]]
    qa_frames = [expanded(item) for item in result["qa_frames"]]
    manifest_path = args.output_dir / result["manifest"]
    if not manifest_path.is_file():
        raise RuntimeError("Copied artifacts do not contain manifest.json")
    if sha256_file(manifest_path) != result["manifest_sha256"]:
        raise RuntimeError("SHA-256 mismatch for copied artifact: manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    primary_name = result["primary_video"]["filename"]
    primary = next(
        item for item in videos if Path(item["path"]).name == primary_name
    )
    return {
        "status": "ok",
        "execution": backend.kind,
        "station": args.station,
        "context": args.context,
        "timezone": args.timezone,
        "requested_start_local": manifest["requested_start_local"],
        "requested_end_local": manifest["requested_end_local"],
        "requested_start_utc": manifest["requested_start_utc"],
        "requested_end_utc": manifest["requested_end_utc"],
        "cameras": manifest["cameras"],
        "originals": len(manifest["originals"]),
        "manifest": str(manifest_path.resolve()),
        "primary_video": primary,
        "videos": videos,
        "qa_frames": qa_frames,
    }


def cleanup_backend(args, backend):
    if backend.deployment:
        cleanup_deployment(args, backend.deployment)
        return
    last_detail = ""
    for attempt in range(1, 4):
        result = exec_in_backend(
            args,
            backend,
            [
                "/usr/bin/python3",
                "-c",
                (
                    "import pathlib,shutil,sys; "
                    "p=pathlib.Path(sys.argv[1]); "
                    "shutil.rmtree(p,ignore_errors=True); "
                    "raise SystemExit(p.exists())"
                ),
                backend.work_dir,
            ],
            check=False,
        )
        if result.returncode == 0:
            return
        detail = result.stderr.strip() or result.stdout.strip()
        last_detail = detail.splitlines()[-1] if detail else "kubectl exec failed"
        if attempt < 3:
            time.sleep(0.25)
    raise RuntimeError(
        f"Could not remove console work directory {backend.work_dir}: {last_detail}"
    )


def main():
    args = parse_args()
    if args.download_workers < 1:
        raise SystemExit("--download-workers must be at least 1")
    args.output_dir = args.output_dir.expanduser().resolve()
    run_id = uuid.uuid4().hex[:12]
    backend = None
    port_forward = None
    output = None
    exit_code = 0
    started = time.monotonic()
    timings = {}
    try:
        backend = select_backend(args, run_id)
        token, group_key, port_forward = authenticate(args)
        worker_path = upload_worker(args, backend)
        timings["backend_setup_and_auth"] = round(time.monotonic() - started, 3)
        request = build_request(args, backend, token, group_key)
        processing_started = time.monotonic()
        worker_result = run_worker(args, backend, worker_path, request)
        timings["remote_processing"] = round(
            time.monotonic() - processing_started,
            3,
        )
        transfer_started = time.monotonic()
        copy_export(args, backend, worker_result)
        output = verify_and_expand_result(args, backend)
        timings["artifact_transfer_and_verify"] = round(
            time.monotonic() - transfer_started,
            3,
        )
    except BackendUnavailable as exc:
        output = {
            "status": "unavailable",
            "stage": "tooling-backend",
            "message": str(exc),
        }
        exit_code = 2
    except Exception as exc:
        output = {
            "status": "error",
            "stage": "tooling-backend",
            "message": str(exc),
        }
        exit_code = 1
    finally:
        cleanup_started = time.monotonic()
        if port_forward is not None:
            port_forward.terminate()
            try:
                port_forward.wait(timeout=10)
            except subprocess.TimeoutExpired:
                port_forward.kill()
                port_forward.wait(timeout=10)
        if backend is not None:
            try:
                cleanup_backend(args, backend)
            except Exception as cleanup_exc:
                output = {
                    "status": "error",
                    "stage": "tooling-cleanup",
                    "message": str(cleanup_exc),
                }
                exit_code = 1
        timings["cleanup"] = round(time.monotonic() - cleanup_started, 3)

    timings["total"] = round(time.monotonic() - started, 3)
    output["timing_seconds"] = timings
    print(json.dumps(output, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
