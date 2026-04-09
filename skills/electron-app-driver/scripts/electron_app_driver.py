from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
INVOCATION_ROOT = Path.cwd().resolve()
NODE_RUNNER = Path(__file__).with_name("electron_runner.mjs")
DEFAULT_OUT_ROOT = INVOCATION_ROOT / ".workspace" / "playwright" / "electron"
DEFAULT_PLACEHOLDERS = (
    "{repo_root}",
    "{workspace_root}",
    "{manifest_dir}",
    "{app_dir}",
    "{runtime_dir}",
    "{out_dir}",
)
DISPLAY_LOCK_DIR = Path("/tmp")
DISPLAY_SOCKET_DIR = Path("/tmp/.X11-unix")
DEFAULT_DISPLAY_RANGE = range(90, 110)


class DriverError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone local Electron app driver."
    )
    parser.add_argument(
        "--out-dir", default="", help="Override artifact output directory."
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Extra environment variable assignment KEY=VALUE.",
    )
    parser.add_argument("--timeout-sec", type=int, default=300)
    parser.add_argument(
        "--xvfb-display",
        default="",
        help="Virtual display number or name, for example 99 or :99. Defaults to the first free display in :90-:109.",
    )
    parser.add_argument(
        "--xvfb-size",
        default="1440x960",
        help="Virtual display size as WIDTHxHEIGHT. Default: 1440x960.",
    )
    parser.add_argument(
        "--xvfb-color-depth",
        type=int,
        default=24,
        help="Xvfb color depth. Default: 24.",
    )
    parser.add_argument(
        "--xvfb-extra-arg",
        action="append",
        default=[],
        help="Extra raw Xvfb arg. Repeat to pass multiple flags.",
    )
    parser.add_argument(
        "--xvfb-startup-timeout-sec",
        type=float,
        default=8.0,
        help="How long to wait for the virtual display to come up. Default: 8s.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run", help="Run a named scenario from the manifest scenario directory."
    )
    run.add_argument("--manifest", required=True)
    run.add_argument(
        "--scenario", required=True, help="Scenario name or explicit JSON path."
    )

    step = subparsers.add_parser("step", help="Run an explicit actions file.")
    step.add_argument("--manifest", required=True)
    step.add_argument("--actions", required=True)

    screenshot = subparsers.add_parser(
        "screenshot", help="Take a screenshot with no extra actions."
    )
    screenshot.add_argument("--manifest", required=True)
    screenshot.add_argument("--name", default="")
    screenshot.add_argument("--wait-ms", type=int, default=1200)

    status = subparsers.add_parser(
        "status", help="Inspect manifest resolution and Xvfb launch prerequisites."
    )
    status.add_argument("--manifest", required=True)

    return parser.parse_args()


def _resolve_input_path(path_like: str, *, base_dir: Path | None = None) -> Path:
    raw = Path(path_like).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return ((base_dir or INVOCATION_ROOT) / raw).resolve()


def _parse_env_assignments(entries: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise DriverError(f"--env must be KEY=VALUE, got: {entry}")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise DriverError(f"--env key must not be empty: {entry}")
        env[key] = value
    return env


def _normalize_display(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise DriverError("xvfb display value must not be empty")
    return raw if raw.startswith(":") else f":{raw}"


def _is_display_taken(display: str) -> bool:
    display_number = display.removeprefix(":")
    socket_path = DISPLAY_SOCKET_DIR / f"X{display_number}"
    lock_path = DISPLAY_LOCK_DIR / f".X{display_number}-lock"
    return socket_path.exists() or lock_path.exists()


def _find_free_display(requested_display: str) -> str:
    if requested_display:
        display = _normalize_display(requested_display)
        if _is_display_taken(display):
            raise DriverError(f"requested Xvfb display is already in use: {display}")
        return display
    for display_number in DEFAULT_DISPLAY_RANGE:
        candidate = f":{display_number}"
        if not _is_display_taken(candidate):
            return candidate
    raise DriverError("unable to find a free Xvfb display in :90-:109")


def _parse_screen_size(value: str) -> tuple[int, int]:
    width_raw, sep, height_raw = value.lower().partition("x")
    if not sep:
        raise DriverError(f"invalid --xvfb-size value: {value}")
    try:
        width = int(width_raw)
        height = int(height_raw)
    except ValueError as error:
        raise DriverError(f"invalid --xvfb-size value: {value}") from error
    if width <= 0 or height <= 0:
        raise DriverError(f"xvfb size must be positive, got: {value}")
    return width, height


def _render_template(value: str, mapping: dict[str, str]) -> str:
    rendered = value
    for token in DEFAULT_PLACEHOLDERS:
        rendered = rendered.replace(token, mapping[token[1:-1]])
    return rendered


def _resolve_manifest_value(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _render_template(value, mapping)
    if isinstance(value, list):
        return [_resolve_manifest_value(item, mapping) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_manifest_value(item, mapping) for key, item in value.items()
        }
    return value


def load_manifest(manifest_path: Path, out_dir_override: str = "") -> dict[str, Any]:
    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_raw, dict):
        raise DriverError("manifest must be a JSON object")

    app_dir_value = manifest_raw.get("app_dir")
    electron_executable_value = manifest_raw.get("electron_executable")
    electron_args_value = manifest_raw.get("electron_args")

    if not isinstance(app_dir_value, str) or not app_dir_value.strip():
        raise DriverError("manifest app_dir is required")
    if (
        not isinstance(electron_executable_value, str)
        or not electron_executable_value.strip()
    ):
        raise DriverError("manifest electron_executable is required")
    if not isinstance(electron_args_value, list) or not all(
        isinstance(item, str) for item in electron_args_value
    ):
        raise DriverError("manifest electron_args must be a list of strings")

    manifest_dir = manifest_path.parent.resolve()
    placeholder_seed = {
        "repo_root": str(INVOCATION_ROOT),
        "workspace_root": str(INVOCATION_ROOT),
        "manifest_dir": str(manifest_dir),
        "app_dir": "",
        "out_dir": "",
        "runtime_dir": "",
    }
    app_dir = _resolve_input_path(_render_template(app_dir_value, placeholder_seed))
    out_dir = (
        _resolve_input_path(out_dir_override)
        if out_dir_override
        else (
            _resolve_input_path(
                str(
                    _render_template(
                        str(manifest_raw.get("default_out_dir", "")),
                        {
                            **placeholder_seed,
                            "app_dir": str(app_dir),
                        },
                    )
                )
            )
            if isinstance(manifest_raw.get("default_out_dir"), str)
            and manifest_raw.get("default_out_dir")
            else (DEFAULT_OUT_ROOT / manifest_path.stem).resolve()
        )
    )
    runtime_dir = (out_dir / "runtime").resolve()

    mapping = {
        "repo_root": str(INVOCATION_ROOT),
        "workspace_root": str(INVOCATION_ROOT),
        "manifest_dir": str(manifest_dir),
        "app_dir": str(app_dir),
        "out_dir": str(out_dir),
        "runtime_dir": str(runtime_dir),
    }

    resolved = _resolve_manifest_value(manifest_raw, mapping)
    resolved["manifest_path"] = str(manifest_path)
    resolved["manifest_dir"] = str(manifest_dir)
    resolved["app_dir"] = str(app_dir)
    resolved["out_dir"] = str(out_dir)
    resolved["runtime_dir"] = str(runtime_dir)
    resolved["cwd"] = str(Path(str(resolved.get("cwd", app_dir))).resolve())
    resolved["electron_executable"] = str(
        Path(str(resolved["electron_executable"])).resolve()
    )
    resolved["electron_args"] = [str(item) for item in resolved["electron_args"]]
    resolved["env"] = dict(resolved.get("env", {}))
    resolved["initial_wait_ms"] = int(resolved.get("initial_wait_ms", 1200))
    resolved["startup_timeout_ms"] = int(resolved.get("startup_timeout_ms", 120000))
    resolved["step_timeout_ms"] = int(resolved.get("step_timeout_ms", 15000))
    resolved["window_index"] = int(resolved.get("window_index", 0))
    resolved["scenario_dir"] = str(
        Path(
            str(resolved.get("scenario_dir", Path(manifest_dir) / "scenarios"))
        ).resolve()
    )
    return resolved


def resolve_actions_path(manifest: dict[str, Any], scenario: str) -> Path:
    candidate = Path(scenario).expanduser()
    if candidate.exists():
        return candidate.resolve()
    scenario_dir = Path(str(manifest["scenario_dir"]))
    name = scenario if scenario.endswith(".json") else f"{scenario}.json"
    candidate = scenario_dir / name
    if candidate.exists():
        return candidate.resolve()
    raise DriverError(f"scenario not found: {scenario}")


def build_launch_env(
    manifest: dict[str, Any], explicit_env: dict[str, str]
) -> dict[str, str]:
    runtime_dir = Path(str(manifest["runtime_dir"]))
    runtime_defaults = {
        "XDG_CONFIG_HOME": str(runtime_dir / "xdg-config"),
        "XDG_DATA_HOME": str(runtime_dir / "xdg-data"),
        "XDG_CACHE_HOME": str(runtime_dir / "xdg-cache"),
        "TMPDIR": str(runtime_dir / "tmp"),
        "HOME": str(runtime_dir / "home"),
        "CODEX_ELECTRON_FORCE_PRIMARY_DISPLAY": "1",
    }
    env = dict(runtime_defaults)
    env.update(
        {str(key): str(value) for key, value in dict(manifest.get("env", {})).items()}
    )
    env.update(explicit_env)
    return env


def _build_xvfb_command(args: argparse.Namespace, display: str) -> list[str]:
    width, height = _parse_screen_size(args.xvfb_size)
    xvfb_path = shutil.which("Xvfb")
    if not xvfb_path:
        raise DriverError("Xvfb is not installed or is not on PATH")
    return [
        xvfb_path,
        display,
        "-screen",
        "0",
        f"{width}x{height}x{args.xvfb_color_depth}",
        "-nolisten",
        "tcp",
        "-ac",
        "-noreset",
        *args.xvfb_extra_arg,
    ]


def _wait_for_xvfb_ready(
    display: str, proc: subprocess.Popen[str], timeout_sec: float
) -> None:
    xdpyinfo_path = shutil.which("xdpyinfo")
    if not xdpyinfo_path:
        raise DriverError("xdpyinfo is required to verify Xvfb startup")

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = ""
            if proc.stderr is not None:
                stderr = proc.stderr.read().strip()
            details = f": {stderr}" if stderr else ""
            raise DriverError(f"Xvfb exited before becoming ready{details}")
        result = subprocess.run(
            [xdpyinfo_path, "-display", display],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.1)
    raise DriverError(f"timed out waiting for Xvfb display {display} to become ready")


class XvfbSession:
    def __init__(self, args: argparse.Namespace):
        self._args = args
        self.display = ""
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "XvfbSession":
        self.display = _find_free_display(self._args.xvfb_display)
        command = _build_xvfb_command(self._args, self.display)
        self._proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for_xvfb_ready(
            self.display, self._proc, self._args.xvfb_startup_timeout_sec
        )
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)


def ensure_manifest_prereqs(manifest: dict[str, Any]) -> None:
    app_dir = Path(str(manifest["app_dir"]))
    electron_executable = Path(str(manifest["electron_executable"]))
    if not app_dir.is_dir():
        raise DriverError(f"app_dir does not exist: {app_dir}")
    if not electron_executable.is_file():
        raise DriverError(f"electron executable does not exist: {electron_executable}")
    for arg in manifest["electron_args"]:
        if arg.endswith(".js"):
            path_candidate = Path(arg)
            if path_candidate.is_absolute() and not path_candidate.exists():
                raise DriverError(f"launch arg path does not exist: {path_candidate}")
    if not shutil.which("Xvfb"):
        raise DriverError("Xvfb is not installed or is not on PATH")
    if not shutil.which("xdpyinfo"):
        raise DriverError("xdpyinfo is required to verify Xvfb startup")


def _run_local(command: list[str], env: dict[str, str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        command, cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **env}
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, combined


def _run_actions(
    manifest: dict[str, Any],
    actions_path: Path,
    *,
    explicit_env: dict[str, str],
    args: argparse.Namespace,
) -> int:
    ensure_manifest_prereqs(manifest)
    out_dir = Path(str(manifest["out_dir"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = Path(str(manifest["runtime_dir"]))
    runtime_dir.mkdir(parents=True, exist_ok=True)
    for child in ("xdg-config", "xdg-data", "xdg-cache", "tmp", "home"):
        (runtime_dir / child).mkdir(parents=True, exist_ok=True)

    with XvfbSession(args) as xvfb:
        env = build_launch_env(manifest, {**explicit_env, "DISPLAY": xvfb.display})
        cmd = [
            "node",
            str(NODE_RUNNER),
            "--electron-executable",
            str(manifest["electron_executable"]),
            "--actions",
            str(actions_path),
            "--out-dir",
            str(out_dir),
            "--cwd",
            str(manifest["cwd"]),
            "--env-json",
            json.dumps(env),
            "--startup-timeout-ms",
            str(manifest["startup_timeout_ms"]),
            "--step-timeout-ms",
            str(manifest["step_timeout_ms"]),
            "--initial-wait-ms",
            str(manifest["initial_wait_ms"]),
            "--window-index",
            str(manifest["window_index"]),
        ]
        for arg in manifest["electron_args"]:
            cmd.extend(["--electron-arg", arg])

        code, output = _run_local(cmd, env={}, cwd=INVOCATION_ROOT)
        print(output, end="")
        return code


def _screenshot_actions_file(out_dir: Path, name: str, wait_ms: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{name}.png" if name else "screenshot.png"
    screenshot_path = out_dir / filename
    payload = {
        "actions": [
            {"type": "wait_for_timeout", "ms": wait_ms},
            {"type": "screenshot", "path": str(screenshot_path), "full_page": False},
        ]
    }
    temp = NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    with temp:
        json.dump(payload, temp)
    return Path(temp.name).resolve()


def cmd_run(args: argparse.Namespace) -> int:
    manifest = load_manifest(
        _resolve_input_path(args.manifest), out_dir_override=args.out_dir
    )
    actions_path = resolve_actions_path(manifest, args.scenario)
    return _run_actions(
        manifest, actions_path, explicit_env=_parse_env_assignments(args.env), args=args
    )


def cmd_step(args: argparse.Namespace) -> int:
    manifest = load_manifest(
        _resolve_input_path(args.manifest), out_dir_override=args.out_dir
    )
    actions_path = _resolve_input_path(args.actions)
    return _run_actions(
        manifest, actions_path, explicit_env=_parse_env_assignments(args.env), args=args
    )


def cmd_screenshot(args: argparse.Namespace) -> int:
    manifest = load_manifest(
        _resolve_input_path(args.manifest), out_dir_override=args.out_dir
    )
    out_dir = Path(str(manifest["out_dir"]))
    actions_path = _screenshot_actions_file(out_dir, args.name, args.wait_ms)
    try:
        return _run_actions(
            manifest,
            actions_path,
            explicit_env=_parse_env_assignments(args.env),
            args=args,
        )
    finally:
        actions_path.unlink(missing_ok=True)


def cmd_status(args: argparse.Namespace) -> int:
    manifest = load_manifest(
        _resolve_input_path(args.manifest), out_dir_override=args.out_dir
    )
    explicit_env = _parse_env_assignments(args.env)
    target_display = (
        _normalize_display(args.xvfb_display) if args.xvfb_display else "<auto>"
    )
    width, height = _parse_screen_size(args.xvfb_size)
    status = {
        "display": None,
        "manifest": manifest,
        "launch_env": build_launch_env(
            manifest, {**explicit_env, "DISPLAY": target_display}
        ),
        "playwright_runner": str(NODE_RUNNER),
        "skill_root": str(SKILL_ROOT),
        "workspace_root": str(INVOCATION_ROOT),
        "xvfb": {
            "binary": shutil.which("Xvfb"),
            "xdpyinfo_binary": shutil.which("xdpyinfo"),
            "target_display": target_display,
            "screen": {
                "width": width,
                "height": height,
                "depth": args.xvfb_color_depth,
            },
            "extra_args": args.xvfb_extra_arg,
        },
    }
    print(json.dumps(status, indent=2, sort_keys=True))
    ensure_manifest_prereqs(manifest)
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "run":
        return cmd_run(args)
    if args.command == "step":
        return cmd_step(args)
    if args.command == "screenshot":
        return cmd_screenshot(args)
    if args.command == "status":
        return cmd_status(args)
    raise DriverError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
