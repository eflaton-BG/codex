#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 120
USER_AGENT = "bga-connections/1.5"


def fail(message, code=1):
    print(message, file=sys.stderr)
    raise SystemExit(code)

def api_key():
    value = os.environ.get("BG_AI_GATEWAY_API_KEY", "").strip()
    if not value:
        fail("BG_AI_GATEWAY_API_KEY is required. Run the BG AI Gateway installer or export it before using bga-connections.")
    return value

def base_url():
    value = os.environ.get("BG_AI_GATEWAY_BASE_URL", "").strip()
    if not value:
        fail("BG_AI_GATEWAY_BASE_URL is required. Run the BG AI Gateway installer before using bga-connections.")
    return value.rstrip("/")


def timeout_seconds():
    raw = os.environ.get("BG_AI_GATEWAY_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        fail("BG_AI_GATEWAY_TIMEOUT_SECONDS must be a positive number.")
    if value <= 0:
        fail("BG_AI_GATEWAY_TIMEOUT_SECONDS must be a positive number.")
    return value


def ssl_context():
    if os.environ.get("BG_AI_GATEWAY_INSECURE", "").strip().lower() in {"1", "true", "yes"}:
        return ssl._create_unverified_context()
    return None

def request(path, method="GET", payload=None, extra_headers=None):
    data = None
    headers = {"Authorization": "Bearer " + api_key(), "User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url() + path, data=data, method=method, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=timeout_seconds(), context=ssl_context())
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
        fail(body or f"Request failed with HTTP {err.code}", err.code if err.code < 256 else 1)
    except (urllib.error.URLError, TimeoutError) as err:
        fail(f"Unable to reach BG AI Gateway at {base_url()}: {err}")


def read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as err:
        fail(f"Unable to read {path}: {err}")


def read_binary_file_base64(path):
    try:
        with open(path, "rb") as handle:
            return base64.b64encode(handle.read()).decode("ascii")
    except OSError as err:
        fail(f"Unable to read {path}: {err}")


def read_multipart_file(path):
    try:
        value = json.loads(read_text_file(path))
    except json.JSONDecodeError as err:
        fail(f"Invalid multipart JSON in {path}: {err}")
    if not isinstance(value, list):
        fail("--multipart-json must contain a JSON array of multipart parts.")
    return value


def read_json_file(path):
    try:
        return json.loads(read_text_file(path))
    except json.JSONDecodeError as err:
        fail(f"Invalid JSON in {path}: {err}")


def split_named_value(value, option):
    if "=" not in value:
        fail(f"{option} values must be name=value")
    name, item_value = value.split("=", 1)
    if not name.strip():
        fail(f"{option} names cannot be empty")
    return name.strip(), item_value


def multipart_parts(args):
    parts = read_multipart_file(args.multipart_json) if args.multipart_json else []
    for item in args.multipart_text or []:
        name, value = split_named_value(item, "--multipart-text")
        parts.append({"name": name, "bodyText": value})
    for item in args.multipart_file or []:
        name, path = split_named_value(item, "--multipart-file")
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        parts.append({
            "name": name,
            "filename": os.path.basename(path),
            "contentType": content_type,
            "bodyBase64": read_binary_file_base64(path),
            "isBase64": True,
        })
    return parts

def print_json_response(res):
    body = res.read().decode("utf-8", "replace")
    if not body:
        print("{}")
        return
    try:
        print(json.dumps(json.loads(body), indent=2, sort_keys=True))
    except json.JSONDecodeError:
        print(body)

def call_payload(args):
    query = {}
    for item in args.query or []:
        if "=" not in item:
            fail("--query values must be key=value")
        key, value = item.split("=", 1)
        query[key] = value
    headers = {}
    for item in args.header or []:
        if ":" not in item:
            fail("--header values must be Name: value")
        key, value = item.split(":", 1)
        headers[key.strip()] = value.strip()
    payload = {"method": args.method.upper()}
    if args.path:
        payload["path"] = args.path
    if args.url:
        payload["url"] = args.url
    if query:
        payload["query"] = query
    if headers:
        payload["headers"] = headers
    if args.body_text is not None:
        payload["bodyText"] = args.body_text
    if args.body_file is not None:
        payload["bodyText"] = read_text_file(args.body_file)
    if args.body_base64 is not None:
        payload["bodyBase64"] = args.body_base64
        payload["isBase64"] = True
    if args.body_base64_file is not None:
        payload["bodyBase64"] = read_binary_file_base64(args.body_base64_file)
        payload["isBase64"] = True
    parts = multipart_parts(args)
    if parts:
        if "bodyText" in payload or "bodyBase64" in payload:
            fail("Body options cannot be combined with multipart options.")
        payload["multipart"] = parts
    return payload


def require_request_target(args):
    if bool(args.path) == bool(args.url):
        fail("Specify exactly one of --path or --url.")


def download_to_output(request_path, payload, output, overwrite):
    output = os.path.abspath(output)
    if os.path.exists(output) and not overwrite:
        fail(f"Output file exists: {output}. Pass --overwrite to replace it.")
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with request(request_path, "POST", payload) as res:
        provider_status = int(res.headers.get("X-BG-Agent-Provider-Status", res.status))
        if provider_status < 200 or provider_status >= 300:
            fail(res.read().decode("utf-8", "replace") or f"Provider download failed with HTTP {provider_status}")
        digest = hashlib.sha256()
        bytes_written = 0
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=os.path.dirname(output) or ".",
                prefix=os.path.basename(output) + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp = handle.name
                while True:
                    chunk = res.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    bytes_written += len(chunk)
            os.replace(tmp, output)
            tmp = ""
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        print(json.dumps({
            "bytesWritten": bytes_written,
            "output": output,
            "sha256": digest.hexdigest(),
            "status": provider_status,
        }, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(prog="bga-connections")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    permissions = sub.add_parser("permissions")
    permissions.add_argument("connection_id")
    for name in ("call", "download"):
        cmd = sub.add_parser(name)
        cmd.add_argument("connection_id")
        cmd.add_argument("--method", default="GET")
        cmd.add_argument("--path")
        cmd.add_argument("--url")
        cmd.add_argument("--query", action="append")
        cmd.add_argument("--header", action="append")
        body = cmd.add_mutually_exclusive_group()
        body.add_argument("--body-text")
        body.add_argument("--body-file")
        body.add_argument("--body-base64")
        body.add_argument("--body-base64-file")
        cmd.add_argument("--multipart-json")
        cmd.add_argument("--multipart-text", action="append", metavar="NAME=VALUE")
        cmd.add_argument("--multipart-file", action="append", metavar="NAME=PATH")
        if name == "download":
            cmd.add_argument("--output", required=True)
            cmd.add_argument("--overwrite", action="store_true")
    slack_file_download = sub.add_parser("slack-file-download", help="Download a Slack-hosted file as the saved Slack user; not a BG Agents app operation")
    slack_file_download.add_argument("connection_id")
    slack_file_download.add_argument("file_id")
    slack_file_download.add_argument("--output", required=True)
    slack_file_download.add_argument("--overwrite", action="store_true")
    request_endpoint = sub.add_parser("request-endpoint")
    request_endpoint.add_argument("--provider", required=True)
    request_endpoint.add_argument("--method", required=True)
    request_endpoint.add_argument("--path")
    request_endpoint.add_argument("--url")
    request_endpoint.add_argument("--reason", required=True)
    request_endpoint.add_argument("--tool-context")
    slack = sub.add_parser("send-slack-message", help="Send a message as the BG Agents Slack app; saved-connection calls act as the user")
    slack.add_argument("--channel", required=True)
    slack.add_argument("--section", action="append", required=True)
    slack.add_argument("--thread-ts")
    github = sub.add_parser("post-github-review")
    github.add_argument("--owner", required=True)
    github.add_argument("--repo", required=True)
    github.add_argument("--pull-number", required=True, type=int)
    github.add_argument("--head-sha", required=True)
    github.add_argument("--body", required=True)
    github.add_argument("--explicit-user-request", action="store_true")
    sub.add_parser("datasets-list")
    dataset_query = sub.add_parser("datasets-query")
    dataset_query_source = dataset_query.add_mutually_exclusive_group(required=True)
    dataset_query_source.add_argument("--sql")
    dataset_query_source.add_argument("--sql-file")
    sub.add_parser("datasets-admin-list")
    for name in ("datasets-admin-create", "datasets-admin-update", "datasets-admin-add-column", "datasets-admin-create-index", "datasets-admin-set-grant"):
        command = sub.add_parser(name)
        if name != "datasets-admin-create":
            command.add_argument("dataset_id")
        command.add_argument("--json-file", required=True)
    dataset_delete = sub.add_parser("datasets-admin-delete")
    dataset_delete.add_argument("dataset_id")
    dataset_delete.add_argument("--confirmation", required=True)
    dataset_rename = sub.add_parser("datasets-admin-rename-column")
    dataset_rename.add_argument("dataset_id")
    dataset_rename.add_argument("column_name")
    dataset_rename.add_argument("--name", required=True)
    dataset_delete_index = sub.add_parser("datasets-admin-delete-index")
    dataset_delete_index.add_argument("dataset_id")
    dataset_delete_index.add_argument("index_name")
    dataset_grants = sub.add_parser("datasets-admin-list-grants")
    dataset_grants.add_argument("dataset_id")
    args = parser.parse_args()

    if args.command == "list":
        print_json_response(request("/bga/v1/connections"))
    elif args.command == "permissions":
        print_json_response(request(f"/bga/v1/connections/{urllib.parse.quote(args.connection_id)}/permissions"))
    elif args.command == "call":
        require_request_target(args)
        print_json_response(request(f"/bga/v1/connections/{urllib.parse.quote(args.connection_id)}/call", "POST", call_payload(args)))
    elif args.command == "download":
        require_request_target(args)
        download_to_output(f"/bga/v1/connections/{urllib.parse.quote(args.connection_id)}/download", call_payload(args), args.output, args.overwrite)
    elif args.command == "slack-file-download":
        workspace_path = args.output.replace("\\", "/")
        if os.path.isabs(workspace_path):
            fail("--output must be a workspace-relative path for slack-file-download.")
        download_to_output(
            f"/bga/v1/connections/{urllib.parse.quote(args.connection_id)}/slack-files/download",
            {"fileId": args.file_id, "workspacePath": workspace_path, "overwrite": args.overwrite},
            args.output,
            args.overwrite,
        )
    elif args.command == "request-endpoint":
        require_request_target(args)
        payload = {"provider": args.provider, "method": args.method, "reason": args.reason}
        if args.path:
            payload["path"] = args.path
        if args.url:
            payload["url"] = args.url
        if args.tool_context:
            payload["toolContext"] = args.tool_context
        print_json_response(request("/bga/v1/endpoint-requests", "POST", payload))
    elif args.command == "send-slack-message":
        payload = {"channel": args.channel, "sections": args.section}
        if args.thread_ts:
            payload["thread_ts"] = args.thread_ts
        print_json_response(request("/bga/v1/platform/slack/messages", "POST", payload))
    elif args.command == "post-github-review":
        if not args.explicit_user_request:
            fail("--explicit-user-request is required. Ask the user to explicitly request this COMMENT review before posting.")
        print_json_response(request("/bga/v1/platform/github/reviews", "POST", {"owner": args.owner, "repo": args.repo, "pullNumber": args.pull_number, "headSha": args.head_sha, "body": args.body}, {"X-BGA-Explicit-User-Request": "true"}))
    elif args.command == "datasets-list":
        print_json_response(request("/bga/v1/datasets"))
    elif args.command == "datasets-query":
        sql = args.sql if args.sql is not None else read_text_file(args.sql_file)
        print_json_response(request("/bga/v1/datasets/query", "POST", {"sql": sql}))
    elif args.command == "datasets-admin-list":
        print_json_response(request("/bga/v1/datasets/admin"))
    elif args.command == "datasets-admin-create":
        print_json_response(request("/bga/v1/datasets/admin", "POST", read_json_file(args.json_file)))
    elif args.command == "datasets-admin-update":
        dataset_id = urllib.parse.quote(args.dataset_id, safe="")
        print_json_response(request(f"/bga/v1/datasets/admin/{dataset_id}", "PATCH", read_json_file(args.json_file)))
    elif args.command == "datasets-admin-delete":
        dataset_id = urllib.parse.quote(args.dataset_id, safe="")
        print_json_response(request(f"/bga/v1/datasets/admin/{dataset_id}", "DELETE", {"confirmation": args.confirmation}))
    elif args.command == "datasets-admin-add-column":
        dataset_id = urllib.parse.quote(args.dataset_id, safe="")
        print_json_response(request(f"/bga/v1/datasets/admin/{dataset_id}/columns", "POST", read_json_file(args.json_file)))
    elif args.command == "datasets-admin-rename-column":
        dataset_id = urllib.parse.quote(args.dataset_id, safe="")
        column_name = urllib.parse.quote(args.column_name, safe="")
        print_json_response(request(f"/bga/v1/datasets/admin/{dataset_id}/columns/{column_name}", "PATCH", {"name": args.name}))
    elif args.command == "datasets-admin-create-index":
        dataset_id = urllib.parse.quote(args.dataset_id, safe="")
        print_json_response(request(f"/bga/v1/datasets/admin/{dataset_id}/indexes", "POST", read_json_file(args.json_file)))
    elif args.command == "datasets-admin-delete-index":
        dataset_id = urllib.parse.quote(args.dataset_id, safe="")
        index_name = urllib.parse.quote(args.index_name, safe="")
        print_json_response(request(f"/bga/v1/datasets/admin/{dataset_id}/indexes/{index_name}", "DELETE"))
    elif args.command == "datasets-admin-list-grants":
        dataset_id = urllib.parse.quote(args.dataset_id, safe="")
        print_json_response(request(f"/bga/v1/datasets/admin/{dataset_id}/grants"))
    elif args.command == "datasets-admin-set-grant":
        dataset_id = urllib.parse.quote(args.dataset_id, safe="")
        print_json_response(request(f"/bga/v1/datasets/admin/{dataset_id}/grants", "PUT", read_json_file(args.json_file)))

if __name__ == "__main__":
    main()
