"""Offline reusable-runner coverage; no network, Slack posts, or paid requests."""
from contextlib import contextmanager
from io import BytesIO, StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2 as httpx
import openai
from PIL import Image
import pytest
import yaml
from botocore.exceptions import ClientError

import job_control
import job_monitor as monitor
import job_runner as runner
import job_runtime as rt


@pytest.fixture
def prepared(tmp_path):
    """Prepare a tiny, non-Pittston local job with explicitly chosen account/config."""
    images = tmp_path / "photos"
    images.mkdir()
    for name in ("same-stem.png", "second.png", "third.png"):
        Image.new("RGB", (10, 8), "red").save(images / name)
    config = tmp_path / "categories.yaml"
    config.write_text(yaml.safe_dump({
        "name": "Other site", "model": "model-for-test",
        "task": "Assign a packaging category", "general_rules": ["Use only visible evidence"],
        "labels": [{"name": "Box", "definition": "A rigid carton"},
                   {"name": "Bag", "definition": "A flexible bag"}],
        "output": {"field": "category"},
    }))
    key = tmp_path / "keys.env"
    key.write_text("TEST_ACCOUNT_KEY='offline-test-key'\n")
    key.chmod(0o600)
    args = SimpleNamespace(
        job_dir=tmp_path / "arbitrary-job", images=images, manifest=None, config=config,
        account="A different account", credential_source="dotenv",
        credential_profile="TEST_ACCOUNT_KEY", credential_file=key,
        base_url="https://gateway.example.test/v1", aws_profile=None, aws_account=None, aws_arn=None,
        workers=2, max_output_tokens=37, crop=[2, 1, 8, 7], image_detail="high",
        api_attempts=3, s3_attempts=3, reserve_gib=0,
    )
    result = runner.prepare(args)
    return args, result, runner.Job(args.job_dir)


@pytest.fixture
def response():
    return SimpleNamespace(id="response-for-test", model="model-for-test", status="completed",
                           output_text='{"category":"Box"}', usage={"total_tokens": 17},
                           error=None, incomplete_details=None)


@pytest.fixture
def client(response):
    return SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response)))


def test_prepare_uses_arbitrary_account_model_count_and_crop(prepared):
    """Job settings derive from arguments/config/inventory, not the October dataset."""
    args, result, job = prepared
    assert result["count"] == 3
    assert result["account"] == "A different account"
    assert result["model"] == "model-for-test"
    assert job.identity["crop_box"] == [2, 1, 8, 7]
    assert job.identity["max_output_tokens"] == 37
    assert job.identity["aws"] is None
    assert not (args.job_dir / ".job/monitor.json").exists()


def test_manifest_deduplicates_images_but_retains_skus(tmp_path):
    """Duplicate S3 image rows are one inference input with all SKU associations."""
    manifest = tmp_path / "input.csv"
    manifest.write_text('s3_uri,s3_size_bytes,s3_etag,SkuId\n'
                        's3://other-bucket/different-month/p.png,100,etag,sku-a\n'
                        's3://other-bucket/different-month/p.png,100,etag,sku-b\n')
    items = runner.inputs_from(manifest=manifest)
    assert len(items) == 1
    assert items[0]["bucket"] == "other-bucket"
    assert items[0]["skus"] == ["sku-a", "sku-b"]


def test_same_image_basename_in_different_directories_is_supported(tmp_path):
    """Distinct source paths do not collide merely because their basenames match."""
    for directory in ("one", "two"):
        target = tmp_path / directory
        target.mkdir()
        Image.new("RGB", (3, 3)).save(target / "same.png")
    items = runner.inputs_from(images=tmp_path)
    assert [item["index"] for item in items] == [1, 2]
    assert len({item["uri"] for item in items}) == 2


def test_s3_requires_verified_size_and_etag(tmp_path):
    """An unverified S3 path cannot be turned into an approved streaming job."""
    manifest = tmp_path / "bad.csv"
    manifest.write_text("s3_uri,SkuId\ns3://bucket/image.png,sku\n")
    with pytest.raises(ValueError, match="size and pinned ETag"):
        runner.inputs_from(manifest=manifest)


def test_conflicting_duplicate_source_metadata_is_rejected(tmp_path):
    """Conflicting manifest rows never silently pick a size/ETag."""
    manifest = tmp_path / "bad.csv"
    manifest.write_text("s3_uri,size_bytes,etag\ns3://bucket/p.png,100,one\ns3://bucket/p.png,101,two\n")
    with pytest.raises(ValueError, match="disagree"):
        runner.inputs_from(manifest=manifest)


def test_prepare_never_adopts_existing_job(prepared):
    """Preparation does not replace an existing plan or its work."""
    args, _, _ = prepared
    with pytest.raises(ValueError, match="new/empty"):
        runner.prepare(args)


def test_changed_config_blocks_resume(prepared):
    """A changed prompt/config cannot reuse a previous approval."""
    args, _, _ = prepared
    with (args.job_dir / "product_annotation.yaml").open("a") as stream:
        stream.write("\n# changed\n")
    with pytest.raises(ValueError, match="Pinned job artifact changed"):
        runner.Job(args.job_dir)


def test_legacy_job_is_not_adopted_or_executed(tmp_path):
    """The active legacy run is deliberately outside the new runner's mutation path."""
    (tmp_path / ".job").mkdir()
    (tmp_path / ".job/identity.json").write_text('{"model":"some-model","account":"someone"}')
    with pytest.raises(ValueError, match="keep its existing exact runner"):
        runner.load_job(tmp_path)


def test_request_uses_job_model_schema_and_cap(prepared, client):
    """Inference uses only the job's selected model and category schema."""
    _, _, job = prepared
    record = job.process(client, job.items[0])
    sent = client.responses.create.call_args.kwargs
    assert sent["model"] == "model-for-test"
    assert sent["max_output_tokens"] == 37
    assert sent["text"]["format"]["schema"]["properties"]["category"]["enum"] == ["Box", "Bag"]
    assert record["status"] == "success"
    with Image.open(job.path("crops", 1)) as crop:
        assert crop.size == (6, 6)


def test_completed_image_never_calls_model_again(prepared, client):
    """An unchanged success is validated and skipped rather than re-billed."""
    _, _, job = prepared
    first = job.process(client, job.items[0])
    assert job.process(client, job.items[0]) == first
    assert client.responses.create.call_count == 1


def test_changed_successful_artifact_blocks_resume(prepared, client):
    """Corrupted output is surfaced, not silently regenerated with inference."""
    _, _, job = prepared
    job.process(client, job.items[0])
    job.path("labels", 1).write_text("Bag\n")
    with pytest.raises(ValueError, match="Successful output changed"):
        job.audit()
    assert client.responses.create.call_count == 1


def test_changed_local_source_is_not_sent(prepared, client):
    """Local input hashes are checked at use, not just preparation."""
    _, _, job = prepared
    Path(job.items[0]["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="Local source image changed"):
        job.process(client, job.items[0])
    client.responses.create.assert_not_called()


def test_api_retries_are_three_total_with_no_new_crop(prepared, client, response, monkeypatch):
    """Application retries are bounded and reuse the same saved crop."""
    _, _, job = prepared
    error = openai.APIConnectionError(request=httpx.Request("POST", "https://gateway.test/responses"))
    client.responses.create.side_effect = [error, error, response]
    backoff = Mock(return_value=False)
    monkeypatch.setattr(job.stop, "wait", backoff)
    record = job.process(client, job.items[0])
    assert record["status"] == "success"
    assert client.responses.create.call_count == 3
    assert [call.args[0] for call in backoff.call_args_list] == [1, 3]
    assert len(list((job.root / ".job/attempts/api/00001").glob("attempt-*.json"))) == 3


def test_exhausted_api_budget_cannot_be_reset_by_recovery(prepared, client, monkeypatch):
    """Recovery never makes a fresh budget for an image whose request may be billable."""
    args, result, job = prepared
    client.responses.create.side_effect = openai.APIConnectionError(
        request=httpx.Request("POST", "https://gateway.test/responses"))
    monkeypatch.setattr(job.stop, "wait", lambda _: False)
    with pytest.raises(openai.APIConnectionError):
        job.process(client, job.items[0])
    recovery = SimpleNamespace(job_dir=args.job_dir, account=args.account, plan_id=result["plan_id"],
                               index=[1], finish_response=False, reason="test", approved_write=True)
    with pytest.raises(ValueError, match="model request may have occurred"):
        runner.recover(recovery)
    assert client.responses.create.call_count == 3


@pytest.mark.parametrize("failure", ["incomplete", "wrong-model", "bad-label"])
def test_invalid_response_is_persisted_and_not_retried(prepared, client, response, failure):
    """A completed transport is not retried for invalid semantic output."""
    _, _, job = prepared
    if failure == "incomplete":
        response.status = "incomplete"
        response.incomplete_details = {"reason": "max_output_tokens"}
    elif failure == "wrong-model":
        response.model = "substitute"
    else:
        response.output_text = '{"category":"invented"}'
    with pytest.raises(ValueError):
        job.process(client, job.items[0])
    record = runner.read(job.cp(1))
    assert record["response"]["id"] == response.id
    assert client.responses.create.call_count == 1


def test_response_received_finishes_locally_without_inference(prepared, client, response):
    """A valid saved response can complete outputs without a second API call."""
    _, _, job = prepared
    record = job.process(client, job.items[0])
    record["status"] = "response_received"
    rt.atomic(job.cp(1), record)
    assert job.process(client, job.items[0])["status"] == "success"
    assert client.responses.create.call_count == 1


def test_pre_model_recovery_archives_failure_and_preserves_success(prepared, client):
    """Recovery is parameterized by selected index rather than a hardcoded failed image."""
    args, result, job = prepared
    job.process(client, job.items[0])
    preserved = job.cp(1).read_bytes()
    item = job.items[2]
    rt.atomic(job.cp(3), {"item": item, "plan_id": job.plan, "status": "failed",
                         "error": {"aws_code": "ExpiredToken"}})
    attempt = job.root / ".job/attempts/s3/00003/attempt-1.json"
    rt.atomic(attempt, {"status": "failed"})
    recovery = SimpleNamespace(job_dir=args.job_dir, account=args.account, plan_id=result["plan_id"],
                               index=[3], finish_response=False, reason="Fresh credentials verified")
    assert runner.recover(recovery)["model_calls"] == 0
    record = runner.read(job.cp(3))
    assert record["status"] == "new"
    assert job.cp(1).read_bytes() == preserved
    assert (job.root / record["recovery_archive"] / "image-00003.json").exists()
    assert not attempt.exists()


def test_recovery_batch_validates_every_target_before_mutating(prepared):
    """One unsafe target prevents partial mutation of an entire recovery batch."""
    args, result, job = prepared
    for index in (1, 2):
        record = {"item": job.items[index - 1], "plan_id": job.plan, "status": "failed"}
        if index == 2:
            record["request_started_at"] = rt.stamp()
        rt.atomic(job.cp(index), record)
    before = job.cp(1).read_bytes()
    recovery = SimpleNamespace(job_dir=args.job_dir, account=args.account, plan_id=result["plan_id"],
                               index=[1, 2], finish_response=False, reason="test")
    with pytest.raises(ValueError, match="model request may have occurred"):
        runner.recover(recovery)
    assert job.cp(1).read_bytes() == before


def test_s3_read_closes_body_and_pins_etag(monkeypatch):
    """Whole-object verification uses IfMatch and always closes the stream body."""
    identity = {"aws": {"profile": "another", "account": "123", "arn": "principal"},
                "policy": {"auth_recoveries_per_read": 2}}
    gate = rt.AwsGate(identity, runner.threading.Event(), lambda _: None)
    body = BytesIO(b"png")
    client = Mock()
    client.get_object.return_value = {"ETag": '"etag"', "ContentLength": 3, "Body": body}
    monkeypatch.setattr(gate, "client", lambda: client)
    assert gate.read({"index": 9, "bucket": "other", "key": "x.png",
                      "source_bytes": 3, "source_etag": '"etag"'}) == b"png"
    assert body.closed
    assert client.get_object.call_args.kwargs["IfMatch"] == '"etag"'


def test_short_s3_body_is_a_retryable_transport_failure(monkeypatch):
    """A truncated but non-raising stream read gets whole-object transport recovery."""
    identity = {"aws": {"profile": "another", "account": "123", "arn": "principal"},
                "policy": {"auth_recoveries_per_read": 2}}
    gate = rt.AwsGate(identity, runner.threading.Event(), lambda _: None)
    body = BytesIO(b"x")
    client = Mock()
    client.get_object.return_value = {"ETag": '"etag"', "ContentLength": 3, "Body": body}
    monkeypatch.setattr(gate, "client", lambda: client)
    with pytest.raises(Exception) as caught:
        gate.read({"index": 9, "bucket": "other", "key": "x.png",
                   "source_bytes": 3, "source_etag": '"etag"'})
    assert rt.transient_s3(caught.value)
    assert body.closed


def test_success_validation_failure_does_not_overwrite_checkpoint(prepared, client):
    """A damaged saved output cannot cause mutation of its successful provenance."""
    _, _, job = prepared
    job.process(client, job.items[0])
    original = job.cp(1).read_bytes()
    job.path("labels", 1).write_text("damaged\n")
    with pytest.raises(ValueError):
        job.process(client, job.items[0])
    assert job.cp(1).read_bytes() == original
    assert client.responses.create.call_count == 1


def test_auth_recovery_waits_for_changed_cache_not_blind_probes(monkeypatch):
    """No repeated STS calls occur against unchanged failed credential material."""
    identity = {"aws": {"profile": "another", "account": "123", "arn": "principal"},
                "policy": {"auth_pause_seconds": 86400, "auth_max_probes": 10}}
    statuses, cache = [], ["old"]
    gate = rt.AwsGate(identity, runner.threading.Event(), statuses.append)
    expired = ClientError({"Error": {"Code": "ExpiredToken"}}, "GetObject")
    probe = Mock(side_effect=[expired, {"account": "123", "arn": "principal"}])
    monkeypatch.setattr(gate, "probe", probe)
    monkeypatch.setattr(rt, "cache_fingerprint", lambda: cache[0])
    polls = []

    def wait(_):
        polls.append(probe.call_count)
        if len(polls) == 2:
            cache[0] = "new"
        return False

    monkeypatch.setattr(gate.stop, "wait", wait)
    gate.recover(expired, 9, 0)
    assert polls == [1, 1]
    assert statuses[0]["status"] == "paused_auth"
    assert statuses[-1]["status"] == "ready"
    assert gate.generation == 1


def test_auth_identity_mismatch_is_not_accepted(monkeypatch):
    """Fresh credentials must still identify the exact configured principal."""
    identity = {"aws": {"profile": "another", "account": "123", "arn": "principal"},
                "policy": {"auth_pause_seconds": 86400, "auth_max_probes": 10}}
    gate = rt.AwsGate(identity, runner.threading.Event(), lambda _: None)
    monkeypatch.setattr(gate, "probe", Mock(side_effect=ValueError("wrong identity")))
    with pytest.raises(ValueError, match="wrong identity"):
        gate.recover(RuntimeError("expired"), 4, 0)
    assert gate.generation == 0


def test_unit_names_are_job_specific_not_month_specific(tmp_path):
    """Service names are deterministic and isolated between jobs/plans."""
    one = job_control.units(tmp_path / "site-one", "plan-one")
    two = job_control.units(tmp_path / "site-two", "plan-one")
    assert one != two
    assert one == job_control.units(tmp_path / "site-one", "plan-one")
    assert "october" not in one["run"]


def test_s3_execution_requires_its_own_approval(prepared, monkeypatch):
    """Billable approval alone does not authorize S3 image reads."""
    args, result, job = prepared
    identity = dict(job.identity, aws={"profile": "p", "account": "a", "arn": "r"})
    monkeypatch.setattr(runner, "load_job", lambda _: (identity, job.items, result["plan_id"]))
    monkeypatch.setattr(runner.sys, "argv", [
        "job_runner.py", "run", "--job-dir", str(args.job_dir), "--account", args.account,
        "--plan-id", result["plan_id"], "--approved-billable-run",
    ])
    with pytest.raises(ValueError, match="S3 image-read approval"):
        runner.main()


def test_start_uses_installed_wrapper_and_never_auto_enables_slack(prepared, monkeypatch):
    """An unconfigured/unapproved monitor is never started with inference."""
    args, result, job = prepared
    launch = Mock()
    monkeypatch.setattr(job_control, "launch", launch)
    monkeypatch.setattr(job_control.checks, "service_status", lambda _: {"ActiveState": "inactive"})
    start_args = SimpleNamespace(job_dir=args.job_dir, account=args.account, approved_thread_updates=False)
    output = job_control.start(start_args, job.identity, result["plan_id"])
    assert not output["monitor_started"]
    launch.assert_called_once()
    assert launch.call_args.args[2][0] == "run"


def test_model_preflight_rejects_account_substitution_before_network(prepared, monkeypatch):
    """Changing selected account on a resume is rejected before credentials/network use."""
    args, _, _ = prepared
    network = Mock()
    monkeypatch.setattr(runner, "model_client", network)
    with pytest.raises(ValueError, match="Account differs"):
        runner.preflight(args.job_dir, "substitute")
    network.assert_not_called()


def test_agent_secrets_key_survives_preflight_for_subsequent_run_client(monkeypatch):
    """Clearing inherited routing does not discard the explicitly injected profile key."""
    monkeypatch.setenv("OPENAI_API_KEY", "injected-test-value")
    monkeypatch.setenv("_SKU_JOB_INJECTED", "1")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "unrelated-project")
    created = Mock()
    monkeypatch.setattr(runner, "OpenAI", created)
    identity = {"credential_source": "agent-secrets", "base_url": "https://gateway.example.test"}
    runner.model_client(identity)
    runner.model_client(identity)
    assert created.call_count == 2
    assert created.call_args.kwargs["max_retries"] == 0
    assert created.call_args.kwargs["api_key"] == "injected-test-value"
    assert "OPENAI_PROJECT_ID" not in runner.os.environ


def test_slack_destinations_are_per_job_and_never_broadcast(prepared, monkeypatch):
    """Posting uses the explicit job thread/channel, not a fixed personal destination."""
    args, _, _ = prepared
    captured = []

    @contextmanager
    def request(path, method, payload):
        captured.append((path, json.loads(payload["bodyText"])))
        yield StringIO(json.dumps({"status": 200, "bodyText": json.dumps({
            "ok": True, "channel": "COTHER", "ts": "receipt",
            "message": {"thread_ts": "123.456"},
        })}))

    config = {"channel": "COTHER", "thread_ts": "123.456"}
    assert monitor.post(args.job_dir, SimpleNamespace(request=request), "new-uuid", config,
                        "ZekesCodexFriend | test", {"completed": 2, "status": "running"})
    assert captured[0][1]["channel"] == "COTHER"
    assert captured[0][1]["thread_ts"] == "123.456"
    assert captured[0][1]["reply_broadcast"] is False


def test_slack_ambiguous_send_is_journaled_not_immediately_replayed(prepared):
    """An uncertain post retains an audit record and does not resend in the same call."""
    args, _, _ = prepared
    request = Mock(side_effect=TimeoutError("gateway timeout"))
    assert not monitor.post(args.job_dir, SimpleNamespace(request=request), "uuid",
                            {"channel": "COTHER", "thread_ts": "123.456"}, "text",
                            {"completed": 2, "status": "running"})
    request.assert_called_once()
    journal = next((args.job_dir / ".job/slack").glob("send-*.json"))
    assert runner.read(journal)["status"] == "unconfirmed"


def test_slack_mentions_real_failure_and_expiry_semantics(prepared, monkeypatch):
    """Status text identifies image/stage/action and distinguishes the two expiry types."""
    args, _, job = prepared
    monkeypatch.setattr(monitor.rt, "lifetime", lambda _: {
        "access_credentials_expire_local": "access-expiry", "refresh_material_present": True})
    rt.atomic(job.cp(2), {"item": job.items[1], "status": "failed", "error": {"aws_code": "ExpiredToken"}})
    state = {"status": "stopped_error", "total": 3, "completed": 1, "updated_at": rt.stamp(),
             "failure": {"error_type": "ClientError", "aws_code": "ExpiredToken"}}
    rt.atomic(args.job_dir / ".job/state.json", state)
    text = monitor.message(args.job_dir, job.identity, {"label": "Other site / other period"}, state)
    assert "Image 2 | before_model_request" in text
    assert "ExpiredToken" in text
    assert "Action:" in text
    assert "This is not the hard login-session expiry" in text
    assert "unknown/not exposed" in text


def test_complete_job_exports_all_images_and_skips_rerun_api(prepared, response, monkeypatch):
    """A full offline run resumes without another model call and exports every input."""
    args, _, job = prepared
    sequence = iter(range(100))

    def respond(**_kwargs):
        return SimpleNamespace(**(vars(response) | {"id": f"response-{next(sequence)}"}))

    client = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=respond)))

    @contextmanager
    def open_client(_identity):
        yield client

    monkeypatch.setattr(runner, "model_client", open_client)
    monkeypatch.setattr(runner, "preflight", lambda *a, **k: {"status": "ok"})
    assert job.run() == 0
    assert runner.read(args.job_dir / ".job/state.json")["status"] == "complete"
    assert len((args.job_dir / "classification-results.csv").read_text().splitlines()) == 4
    assert runner.Job(args.job_dir).run() == 0
    assert client.responses.create.call_count == 3


def test_s3_job_uses_manifest_count_and_separate_s3_api_journals(prepared, client, monkeypatch):
    """Streaming is a full supported job mode, not only a generic manifest parser."""
    args, _, _ = prepared
    args.job_dir = args.job_dir.parent / "s3-job"
    args.images = None
    args.manifest = args.job_dir.parent / "s3.csv"
    raw = BytesIO()
    Image.new("RGB", (10, 8), "blue").save(raw, format="PNG")
    png = raw.getvalue()
    args.manifest.write_text(
        f"s3_uri,s3_size_bytes,s3_etag,SkuId\n"
        f"s3://a-different-site/archive/p.png,{len(png)},etag-one,sku-one\n"
        f"s3://a-different-site/archive/p.png,{len(png)},etag-one,sku-two\n"
    )
    args.aws_profile, args.aws_account, args.aws_arn = "chosen-profile", "123456789012", "chosen-principal"
    result = runner.prepare(args)
    job = runner.Job(args.job_dir)
    monkeypatch.setattr(job.aws, "read", lambda _item: png)
    record = job.process(client, job.items[0])
    assert result["count"] == 1
    assert job.identity["sku_count"] == 2
    assert job.identity["aws"]["profile"] == "chosen-profile"
    assert record["status"] == "success"
    assert (job.root / ".job/attempts/s3/00001/attempt-1.json").exists()
    assert (job.root / ".job/attempts/api/00001/attempt-1.json").exists()
    assert job.statistics["api_requests"] == job.statistics["s3_requests"] == 1


def test_configured_key_is_redacted_even_in_unusual_sdk_error_text(prepared, monkeypatch):
    """Known credential values are removed even if an SDK message uses unexpected wording."""
    _, _, job = prepared
    monkeypatch.setattr(runner, "OpenAI", Mock())
    runner.model_client(job.identity)
    diagnostic = rt.error_info(RuntimeError("Incorrect API key provided: offline-test-key; contact support"))
    assert "offline-test-key" not in json.dumps(diagnostic)


def test_connection_uuid_is_rediscovered_and_permission_checked():
    """Slack's current connected UUID is used only after its permission is checked."""
    seen = []
    selected_id = "11111111-2222-3333-4444-555555555555"

    @contextmanager
    def request(path):
        seen.append(path)
        if path.endswith("/permissions"):
            body = {"products": [{"enabled": True, "categories": [{
                "enabled": True, "endpoints": [{"enabled": True, "method": "POST",
                                              "path": "/chat.postMessage"}]}]}]}
        else:
            body = [{"connectionRef": "slack:default", "connectionId": selected_id,
                     "status": "connected", "callable": True}]
        yield StringIO(json.dumps(body))

    assert monitor.connection(SimpleNamespace(request=request)) == selected_id
    assert seen == ["/bga/v1/connections", f"/bga/v1/connections/{selected_id}/permissions"]
