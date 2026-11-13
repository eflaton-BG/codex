"""Shared category-job primitives. No job Python code is imported."""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore import exceptions as aws_errors
from PIL import Image, ImageDraw, ImageFont
import openai

import checks

SDK_CONFIG = Config(connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 1})
_SECRETS = set()


def register_secret(value):
    """Keep selected secret values in process memory only, for defensive error redaction."""
    if value:
        _SECRETS.add(value)


def sanitized(value):
    text = str(value)
    for secret in tuple(_SECRETS):
        text = text.replace(secret, "[redacted]")
    return checks.sanitize(text)


def stamp():
    return datetime.now().astimezone().isoformat()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    with pending.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def error_info(error):
    chain, seen = [], set()
    current = error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append({"type": type(current).__name__, "message": sanitized(current)})
        current = current.__cause__ or current.__context__
    code = None
    if isinstance(error, aws_errors.ClientError):
        code = error.response.get("Error", {}).get("Code")
    return {"error_type": type(error).__name__, "http_status": getattr(error, "status_code", None),
            "aws_code": code, "chain": chain}


def auth_error(error):
    return (
        isinstance(error, aws_errors.ClientError)
        and error_info(error)["aws_code"] in {
            "ExpiredToken", "ExpiredTokenException", "RequestExpired", "InvalidToken",
        }
    ) or type(error).__name__ in {
        "LoginRefreshRequired", "TokenRetrievalError", "NoCredentialsError",
        "CredentialRetrievalError", "UnauthorizedSSOTokenError",
    }


def transient_api(error):
    return isinstance(error, openai.APIConnectionError) or (
        isinstance(error, openai.APIStatusError)
        and (error.status_code in (408, 429) or 500 <= error.status_code <= 599)
    )


def transient_s3(error):
    if isinstance(error, (
        aws_errors.ConnectionClosedError, aws_errors.EndpointConnectionError,
        aws_errors.ConnectTimeoutError, aws_errors.ReadTimeoutError,
        aws_errors.ResponseStreamingError, aws_errors.IncompleteReadError,
    )):
        return True
    if isinstance(error, aws_errors.ClientError):
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return error_info(error)["aws_code"] in {
            "SlowDown", "RequestTimeout", "InternalError", "ServiceUnavailable",
        } or status in (429, 500, 502, 503, 504)
    return False


def https_endpoint(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Endpoint must be explicit HTTPS without credentials, query, or fragment")
    return value.rstrip("/")


def lifetime(identity):
    if not identity.get("aws"):
        return None
    environ = dict(os.environ, AWS_PROFILE=identity["aws"]["profile"])
    # An explicitly selected boto3 profile does not use inherited access keys.
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        environ.pop(name, None)
    return checks.aws_lifetime(environ=environ)


def cache_fingerprint():
    paths = [Path.home() / ".aws/config", Path.home() / ".aws/credentials"]
    directory = Path(os.environ.get("AWS_LOGIN_CACHE_DIRECTORY", Path.home() / ".aws/login/cache"))
    paths.extend(directory.glob("*.json"))
    values = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        values.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(values))


class AwsGate:
    def __init__(self, identity, stop, publish):
        self.identity, self.stop, self.publish = identity, stop, publish
        self.aws = identity["aws"]
        self.policy = identity["policy"]
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.ready.set()
        self.generation = 0
        self.local = threading.local()
        self.clients = []
        self.clients_lock = threading.Lock()

    def session(self):
        return boto3.Session(profile_name=self.aws["profile"])

    def probe(self):
        with closing(self.session().client("sts", config=SDK_CONFIG)) as client:
            who = client.get_caller_identity()
        if who["Account"] != self.aws["account"] or who["Arn"] != self.aws["arn"]:
            raise ValueError("AWS identity differs from the job's pinned account/principal")
        return {"account": who["Account"], "arn": who["Arn"]}

    def gate(self):
        while not self.ready.wait(1):
            if self.stop.is_set():
                raise RuntimeError("Stopped while paused for AWS authentication")
        if self.stop.is_set():
            raise RuntimeError("Stop requested before new work")

    def client(self):
        if getattr(self.local, "generation", None) != self.generation:
            old = getattr(self.local, "client", None)
            if old is not None:
                old.close()
            self.local.client = self.session().client("s3", config=SDK_CONFIG)
            self.local.generation = self.generation
            with self.clients_lock:
                self.clients.append(self.local.client)
        return self.local.client

    def recover(self, error, index, generation):
        with self.lock:
            if generation != self.generation:
                return
            self.ready.clear()
            start, probes, previous = time.monotonic(), 0, None
            try:
                self.publish({"status": "paused_auth", "index": index, "reason": error_info(error),
                              "action": "Trying one fresh native AWS session; if unsuccessful, run "
                                        "aws login yourself. No model request is replayed."})
                while (probes < self.policy["auth_max_probes"]
                       and time.monotonic() - start < self.policy["auth_pause_seconds"]):
                    if self.stop.is_set():
                        raise RuntimeError("Stopped while paused for AWS authentication")
                    current = cache_fingerprint()
                    if probes and current == previous:
                        if self.stop.wait(30):
                            raise RuntimeError("Stopped while paused for AWS authentication")
                        continue
                    probes += 1
                    try:
                        self.probe()
                    except ValueError:
                        raise
                    except Exception as check_error:
                        previous = cache_fingerprint()
                        self.publish({"status": "paused_auth", "index": index, "probes": probes,
                                      "reason": error_info(check_error),
                                      "action": "Run aws login yourself. Waiting for changed cache "
                                                "and verification of the same AWS identity."})
                    else:
                        self.generation += 1
                        self.publish({"status": "ready", "probes": probes,
                                      "reason": "Fresh AWS identity verified"})
                        return
                raise RuntimeError("AWS pause reached its time or changed-cache probe limit")
            finally:
                self.ready.set()

    def read(self, item):
        for attempt in range(self.policy["auth_recoveries_per_read"] + 1):
            self.gate()
            generation = self.generation
            try:
                response = self.client().get_object(
                    Bucket=item["bucket"], Key=item["key"], IfMatch=item["source_etag"])
                try:
                    if response["ETag"] != item["source_etag"]:
                        raise ValueError("S3 ETag changed")
                    if response["ContentLength"] != item["source_bytes"]:
                        raise ValueError("S3 source size changed")
                    raw = response["Body"].read(item["source_bytes"] + 1)
                    if len(raw) != item["source_bytes"]:
                        raise aws_errors.IncompleteReadError(
                            actual_bytes=len(raw), expected_bytes=item["source_bytes"])
                    return raw
                finally:
                    response["Body"].close()
            except Exception as error:
                if not auth_error(error) or attempt == self.policy["auth_recoveries_per_read"]:
                    raise
                self.recover(error, item["index"], generation)
        raise AssertionError("Unreachable")

    def close(self):
        for client in self.clients:
            client.close()


class CategoryModel:
    """Same YAML category/prompt contract as the existing BG annotation tool."""
    def __init__(self, config, identity):
        self.config, self.identity = config, identity
        self.labels = [label["name"] for label in config["labels"]]
        self.field = config["output"]["field"]
        self.prompt = (
            "You are an annotator labeling products in a carton.\n\n"
            f"Task:\n{config['task']}\n\nRules:\n"
            + "\n".join(f"- {rule}" for rule in config["general_rules"])
            + "\n\nLabels and definitions:\n"
            + "\n\n".join(f"- {label['name']}: {label['definition']}" for label in config["labels"])
            + f'\n\nOutput:\n{{"{self.field}": "label"}}'
        )

    def request(self, client, crop_path):
        self.schema = {
            "type": "json_schema", "name": "product_image_label", "strict": True,
            "schema": {"type": "object", "properties": {
                self.field: {"type": "string", "enum": self.labels}},
                "required": [self.field], "additionalProperties": False},
        }
        image = "data:image/png;base64," + base64.b64encode(crop_path.read_bytes()).decode()
        return client.responses.create(
            model=self.identity["model"],
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": self.prompt}]},
                {"role": "user", "content": [{"type": "input_image", "image_url": image,
                                             "detail": self.identity["image_detail"]}]},
            ],
            text={"format": self.schema},
            max_output_tokens=self.identity["max_output_tokens"],
        )

    def label(self, response):
        if response["status"] != "completed":
            raise ValueError("Incomplete model response; inspect saved details before another request")
        if response["model"] != self.identity["model"]:
            raise ValueError("Unexpected response model")
        label = json.loads(response["output_text"])[self.field]
        if label not in self.labels:
            raise ValueError("Unexpected category label")
        return label


def response_data(response):
    result = {}
    for name in ("id", "model", "status", "usage", "incomplete_details", "error"):
        value = getattr(response, name, None)
        result[name] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    try:
        result["output_text"] = response.output_text
    except Exception as error:
        result["output_text"] = None
        result["output_text_error"] = error_info(error)
    return result


def crop_image(raw, box):
    with Image.open(BytesIO(raw)) as source:
        if box and (box[2] > source.width or box[3] > source.height):
            raise ValueError("Configured crop lies outside source image")
        return (source.crop(tuple(box)) if box else source.copy()).convert("RGB")


def save_png(image, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".png.pending")
    image.save(pending, format="PNG")
    pending.replace(path)


def annotated_crop(crop_path, label, target):
    with Image.open(crop_path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), label, font=font)
    draw.rectangle((0, 0, image.width, box[3] - box[1] + 24), fill="black")
    draw.text((12, 12), label, font=font, fill="white")
    save_png(image, target)
