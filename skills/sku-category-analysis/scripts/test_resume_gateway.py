"""Offline regression tests for explicit rejected-request recovery."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import openai
import pytest

import resume_gateway as g


@pytest.fixture
def rejected():
    record = {
        "index": 25296, "status": "failed", "account": "Stride",
        "requested_model": g.r.MODEL, "error_type": "AuthenticationError",
        "http_status": 401, "api_total_attempts": 1,
        "api_attempt_journal": ".job/recovery/api-attempts/25296/attempt-1.json",
        "error_diagnostic": {"chain": [{"message": "invalid Gateway key"}]},
        "crop_file": "crop.png", "crop_sha256": "crop-hash",
        "request_started_at": "old-request", "uri": "s3://bucket/image.png",
    }
    attempt = {"index": 25296, "attempt": 1, "status": "failed",
               "error": {"type": "AuthenticationError", "http_status": 401},
               "crop_sha256": "crop-hash", "model": g.r.MODEL, "account": "Stride",
               "retry_scheduled": False, "outcome_may_be_billable": True}
    return record, attempt


def test_definite_gateway_rejection_is_reviewable(rejected):
    """Both the checkpoint and its journal must confirm the same auth rejection."""
    g.rejected_request(*rejected)


@pytest.mark.parametrize("fields", [
    {"http_status": 500}, {"error_type": "APIConnectionError"},
    {"account": "Personal"}, {"requested_model": "other"},
    {"response_id": "response"}, {"api_total_attempts": 2},
    {"status": "api_in_flight"},
])
def test_uncertain_or_different_request_is_refused(rejected, fields):
    """Recovery cannot turn unrelated failures into authorized auth retries."""
    record, attempt = rejected
    with pytest.raises(ValueError):
        g.rejected_request(record | fields, attempt)


@pytest.mark.parametrize("fields", [
    {"index": 1}, {"attempt": 2}, {"status": "requesting"},
    {"response_id": "response"}, {"retry_scheduled": True},
    {"crop_sha256": "changed"}, {"account": "Personal"},
    {"error": {"type": "AuthenticationError", "http_status": 403}},
])
def test_mismatched_attempt_journal_is_refused(rejected, fields):
    """The old attempt cannot be missing, ambiguous, or from a different image."""
    record, attempt = rejected
    with pytest.raises(ValueError):
        g.rejected_request(record, attempt | fields)


def test_recovered_checkpoint_retains_crop_budget_and_failure_provenance(rejected, tmp_path, monkeypatch):
    """A reviewed recovery reuses its crop and does not restore a fresh API budget."""
    monkeypatch.setattr(g, "ROOT", tmp_path)
    original, _ = rejected
    authorization = {"plan_id": "recovery", "identity": {"failed_sha256": "failure-hash"}}
    value = g.recovered_checkpoint(original, authorization, tmp_path / "archive.json")
    assert value["status"] == "crop_saved"
    assert value["crop_file"] == original["crop_file"]
    assert value["crop_sha256"] == original["crop_sha256"]
    assert value["api_prior_attempts"] == value["api_total_attempts"] == 1
    assert value["original_failure_archive"] == "archive.json"
    assert value["original_failure_sha256"] == "failure-hash"
    assert "request_started_at" not in value and "error_diagnostic" not in value
    assert original["status"] == "failed"


def test_only_two_additional_attempts_are_possible_and_first_journal_is_untouched(tmp_path, monkeypatch):
    """Recovery starts at attempt 2 and retains the existing attempt-1 bytes."""
    a = g.a
    monkeypatch.setattr(a, "ROOT", tmp_path)
    monkeypatch.setattr(a, "API_RECOVERY", tmp_path / "recovery")
    monkeypatch.setattr(a, "AUTHORIZATION", {"plan_id": "reviewed"})
    monkeypatch.setattr(a, "event", Mock())
    monkeypatch.setattr(a.r, "write_json", a.ORIGINAL_WRITE)
    monkeypatch.setattr(a.r, "checkpoint", lambda _: tmp_path / "checkpoint.json")
    monkeypatch.setattr(a.r.STOP, "wait", lambda _: False)
    a.r.STOP.clear()
    first = tmp_path / "recovery/api-attempts/25296/attempt-1.json"
    first.parent.mkdir(parents=True)
    first.write_text('{"original":"authentication rejection"}')
    before = first.read_bytes()
    error = openai.APIConnectionError(request=httpx2.Request("POST", "https://example.test"))
    annotation = SimpleNamespace(annotate_image=Mock(side_effect=error))
    record = {"api_prior_attempts": 1, "crop_sha256": "crop-hash"}
    with pytest.raises(openai.APIConnectionError):
        a.bounded_api(annotation, Mock(), {"index": 25296}, record, "image")
    assert annotation.annotate_image.call_count == 2
    assert record["api_total_attempts"] == 3
    assert first.read_bytes() == before
    assert sorted(path.name for path in first.parent.glob("*.json")) == [
        "attempt-1.json", "attempt-2.json", "attempt-3.json",
    ]
    assert json.loads((first.parent / "attempt-3.json").read_text())["retry_scheduled"] is False
