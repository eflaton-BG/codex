"""Offline authentication recovery tests: no live calls, real waits, or billable work."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from botocore.exceptions import ClientError
import pytest

spec = importlib.util.spec_from_file_location("auth_recovery_tests", Path(__file__).with_name("resume_auth.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
spec = importlib.util.spec_from_file_location("auth_watch_tests", Path(__file__).with_name("slack_watch_auth_v3.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


@pytest.fixture
def expired():
    return ClientError({"Error": {"Code": "ExpiredToken", "Message": "Expired AWS credentials"}}, "GetObject")


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    m.r.STOP.clear()
    monkeypatch.setattr(m.a, "event", Mock())
    return m.AwsRecovery(tmp_path)


def test_expired_s3_recovers_with_new_session_before_returning_crop(recovery, expired, monkeypatch):
    """An expired read is retried only after a new AWS session passes verification."""
    read = Mock(side_effect=[expired, ("crop", {"source_sha256": "sha"})])
    probe = Mock()
    monkeypatch.setattr(recovery, "client", Mock(return_value="fresh-client"))
    monkeypatch.setattr(recovery, "probe", probe)
    monkeypatch.setattr(m.a, "ORIGINAL_READ", read)
    assert recovery.read(None, {"index": 21718}) == ("crop", {"source_sha256": "sha"})
    assert recovery.generation == 1
    assert recovery.status["status"] == "ready"
    assert read.call_count == 2
    probe.assert_called_once()


def test_failed_probe_waits_for_cache_change_without_repeated_network_calls(
    recovery, expired, monkeypatch
):
    """An unchanged cache triggers no further STS calls while the job is paused."""
    probe = Mock(side_effect=[expired, None])
    fingerprint = ["old"]
    observed = []

    def wait(_seconds):
        observed.append((probe.call_count, recovery.status["status"]))
        if len(observed) == 2:
            fingerprint[0] = "new"
        return False

    monkeypatch.setattr(recovery, "probe", probe)
    monkeypatch.setattr(m, "cache_fingerprint", lambda: fingerprint[0])
    monkeypatch.setattr(m.r.STOP, "wait", wait)
    recovery.recover(expired, 21718, 0)
    assert observed == [(1, "paused_auth"), (1, "paused_auth")]
    assert probe.call_count == 2
    assert recovery.generation == 1


def test_shutdown_interrupts_auth_pause(recovery, expired, monkeypatch):
    """A shutdown request exits the pause instead of probing or submitting model work."""
    monkeypatch.setattr(recovery, "probe", Mock(side_effect=expired))
    monkeypatch.setattr(m.r.STOP, "wait", lambda _seconds: True)
    with pytest.raises(RuntimeError, match="Stopped while paused"):
        recovery.recover(expired, 21718, 0)
    assert recovery.ready.is_set()


def test_identity_mismatch_is_never_recovered(recovery, expired, monkeypatch):
    """A fresh session for a different identity cannot resume the run."""
    monkeypatch.setattr(recovery, "probe", Mock(side_effect=ValueError("identity mismatch")))
    with pytest.raises(ValueError, match="identity mismatch"):
        recovery.recover(expired, 21718, 0)
    assert recovery.generation == 0


def test_other_workers_reuse_verified_recovery(recovery, expired, monkeypatch):
    """A worker with an older generation does not start a second refresh sequence."""
    probe = Mock()
    monkeypatch.setattr(recovery, "probe", probe)
    recovery.generation = 1
    recovery.recover(expired, 21718, 0)
    probe.assert_not_called()


def test_auth_read_recovery_is_bounded(recovery, expired, monkeypatch):
    """A persistently rejected S3 token is not read indefinitely."""
    monkeypatch.setattr(recovery, "client", Mock())
    monkeypatch.setattr(recovery, "probe", Mock())
    read = Mock(side_effect=expired)
    monkeypatch.setattr(m.a, "ORIGINAL_READ", read)
    with pytest.raises(ClientError):
        recovery.read(None, {"index": 21718})
    assert read.call_count == 3
    assert recovery.generation == 2


def test_permission_denial_is_not_misdiagnosed_as_expiry(recovery, monkeypatch):
    """An S3 AccessDenied error stops rather than waiting for a new login."""
    denied = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    monkeypatch.setattr(recovery, "client", Mock())
    monkeypatch.setattr(recovery, "probe", Mock())
    monkeypatch.setattr(m.a, "ORIGINAL_READ", Mock(side_effect=denied))
    with pytest.raises(ClientError):
        recovery.read(None, {"index": 21718})
    recovery.probe.assert_not_called()


def test_model_submission_is_gated_before_existing_api_retry_wrapper(monkeypatch):
    """Pausing AWS cannot bypass the gate into billable inference."""
    gate = Mock(side_effect=RuntimeError("paused"))
    api = Mock()
    monkeypatch.setattr(m, "AWS", SimpleNamespace(gate=gate))
    monkeypatch.setattr(m, "ORIGINAL_API", api)
    with pytest.raises(RuntimeError, match="paused"):
        m.model_with_auth_gate("annotation", "client", "item", "record", "image")
    api.assert_not_called()


def test_recovery_refuses_any_prior_model_or_crop_fields():
    """Only the reviewed failure before a crop or model request may be reset."""
    record = {"index": 21718, "status": "failed", "error_type": "ClientError",
              "error_diagnostic": {"chain": [{"message": "S3 ExpiredToken"}]}}
    assert m.safe_pre_model_failure(record)
    for field in ("request_started_at", "response_id", "crop_file", "api_total_attempts"):
        assert not m.safe_pre_model_failure(dict(record, **{field: "anything"}))


def test_paused_status_is_durable_without_mutating_model_retry_limits(tmp_path, monkeypatch):
    """Heartbeats expose the actionable pause and keep the two-retry API policy."""
    monkeypatch.setattr(m, "META", tmp_path / ".job")
    monkeypatch.setattr(m.a, "ROOT", tmp_path)
    monkeypatch.setattr(m.a, "AUTHORIZATION", {"plan_id": "recovery"})
    monkeypatch.setattr(m, "AUTHORIZATION", {"aws_startup_lifetime": {}})
    monkeypatch.setattr(m, "AWS", SimpleNamespace(
        status={"status": "paused_auth", "reason": "ExpiredToken", "action": "Run aws login"}))
    path = tmp_path / ".job/state.json"
    m.write_with_status(path, {"status": "running"})
    state = json.loads(path.read_text())
    assert state["status"] == "paused_auth"
    assert state["pause_reason"] == "ExpiredToken"
    assert state["api_application_max_retries"] == 2
    assert state["api_sdk_max_retries"] == 0


def test_slack_explains_aws_pause_and_distinguishes_expiries(monkeypatch):
    """Slack states required action without claiming access expiry is hard session expiry."""
    monkeypatch.setattr(w.checks, "aws_lifetime", lambda: {
        "access_credentials_expire_local": "access-expiry",
        "refresh_material_present": True, "login_session_expires_at": None,
    })
    text = w.details({"status": "paused_auth", "pause_reason": "AWS ExpiredToken",
                      "required_action": "Run aws login yourself"})
    assert "AWS ExpiredToken" in text
    assert "Run aws login yourself" in text
    assert "This is not the hard login-session expiry" in text
    assert "not exposed by the cache" in text


def test_terminal_slack_explains_checkpoint_cause_and_action(monkeypatch):
    """Terminal messages identify the failed image and stage instead of only stopped_error."""
    monkeypatch.setattr(w.checks, "aws_lifetime", lambda: {})
    monkeypatch.setattr(w.checks, "diagnose", lambda _: {"failures": [{
        "index": 21718, "stage": "before_model_request", "reason": "AWS expired",
        "messages": ["ExpiredToken"], "action": "Run aws login yourself",
    }]})
    text = w.details({"status": "stopped_error"})
    assert "Image 21718 | before_model_request | AWS expired: ExpiredToken" in text
    assert "Action: Run aws login yourself" in text
