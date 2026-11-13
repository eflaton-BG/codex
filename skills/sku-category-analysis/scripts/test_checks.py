"""Offline tests; no login, provider calls, service changes, or secret output."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("checks_under_test", Path(__file__).with_name("checks.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sku-readonly-checks-test-"))
        self.now = datetime(2026, 11, 12, 3, 30, tzinfo=timezone.utc)
        config = self.root / ".aws/config"
        config.parent.mkdir()
        config.write_text("[default]\nlogin_session = example-session\n")
        directory = self.root / ".aws/login/cache"
        directory.mkdir(parents=True)
        self.cache = directory / (hashlib.sha256(b"example-session").hexdigest() + ".json")
        self.cache.write_text(json.dumps({
            "accessToken": {
                "accessKeyId": "do-not-print-key", "secretAccessKey": "do-not-print-secret",
                "sessionToken": "do-not-print-token", "expiresAt": "2026-11-12T03:44:41Z",
            },
            "refreshToken": "do-not-print-refresh-token",
        }))
        self.identity = {
            "account": "Stride", "model": "gpt-6.1-terra", "base_url": c.TRUSTED_ENDPOINT,
            "credential_source": "dotenv", "credential_file": str(c.HOME / ".codex/.env"),
            "credential_profile": "BG_AI_GATEWAY_STRIDE_KEY",
            "aws_account": "account", "aws_arn": "principal",
        }
        meta = self.root / ".job"
        meta.mkdir()
        (meta / "identity.json").write_text(json.dumps(self.identity))

    def test_access_credential_expiry_is_not_login_session_expiry(self):
        result = c.aws_lifetime(self.root, {}, self.now)
        self.assertEqual(result["seconds_remaining"], 881)
        self.assertIsNone(result["login_session_expires_at"])
        self.assertTrue(result["refresh_material_present"])
        self.assertNotIn("do-not-print", json.dumps(result))

    def test_expired_and_five_minute_warnings(self):
        for at, phrase in (
            (datetime(2026, 11, 12, 3, 42, tzinfo=timezone.utc), "five minutes"),
            (datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc), "expired"),
        ):
            with self.subTest(at=at):
                self.assertIn(phrase, c.aws_lifetime(self.root, {}, at)["warning"])

    def test_environment_credentials_override_login_cache_metadata(self):
        result = c.aws_lifetime(self.root, {"AWS_ACCESS_KEY_ID": "hidden"}, self.now)
        self.assertEqual(result["provider"], "environment")
        self.assertIsNone(result["access_credentials_expire_at"])
        self.assertNotIn("hidden", json.dumps(result))

    def test_missing_login_cache_and_unconfigured_provider_are_explicit(self):
        # Fixtures are retained. Replace configuration rather than deleting it.
        (self.root / ".aws/config").write_text("[default]\nlogin_session = other-session\n")
        self.assertIn("No cached", c.aws_lifetime(self.root, {}, self.now)["warning"])
        (self.root / ".aws/config").write_text("[default]\nregion = us-east-1\n")
        self.assertEqual(c.aws_lifetime(self.root, {}, self.now)["provider"], "unknown")

    def test_job_endpoint_and_account_are_validated_before_network(self):
        for field, bad in (
            ("base_url", "https://untrusted.example/v1"),
            ("credential_file", "/tmp/other.env"),
            ("account", "Other"),
            ("credential_profile", "unsafe;command"),
        ):
            with self.subTest(field=field):
                (self.root / ".job/identity.json").write_text(json.dumps(dict(self.identity, **{field: bad})))
                with patch.object(c, "aws_identity") as aws, patch.object(c, "model_availability") as model:
                    with self.assertRaises(ValueError):
                        c.preflight(self.root, "Stride")
                aws.assert_not_called()
                model.assert_not_called()

    def test_preflight_verifies_exact_identity_and_model(self):
        with patch.object(c, "aws_lifetime", return_value={"provider": "login"}):
            with patch.object(c, "aws_identity", return_value={"account": "account", "arn": "principal"}):
                with patch.object(c, "model_availability", return_value=True) as model:
                    result = c.preflight(self.root, "Stride")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["s3_image_reads"], 0)
        model.assert_called_once_with(self.identity)

    def test_aws_identity_mismatch_prevents_gateway_request(self):
        with patch.object(c, "aws_lifetime", return_value={}):
            with patch.object(c, "aws_identity", return_value={"account": "wrong", "arn": "principal"}):
                with patch.object(c, "model_availability") as model:
                    with self.assertRaises(ValueError):
                        c.preflight(self.root, "Stride")
        model.assert_not_called()

    def test_preflight_scopes_do_not_make_unrequested_provider_calls(self):
        with patch.object(c, "aws_lifetime", return_value={}):
            with patch.object(c, "aws_identity") as aws:
                with patch.object(c, "model_availability", return_value=True):
                    c.preflight(self.root, "Stride", "model")
        aws.assert_not_called()

    def test_status_does_not_echo_unrelated_sensitive_fields(self):
        (self.root / ".job/state.json").write_text(json.dumps({
            "status": "stopped_error", "completed": 3, "private_token": "do-not-print",
        }))
        result = c.job_status(self.root)
        self.assertEqual(result["state"]["completed"], 3)
        self.assertNotIn("do-not-print", json.dumps(result))

    def test_failure_reason_is_from_checkpoint_not_generic_terminal_error(self):
        records = self.root / ".job/records"
        records.mkdir()
        record = {
            "index": 21718, "status": "failed", "error_type": "ClientError",
            "error_diagnostic": {"chain": [{"type": "ClientError", "message": "ExpiredToken"}]},
        }
        (records / "image-21718.json").write_text(json.dumps(record))
        (self.root / ".job/run.log").write_text(
            json.dumps({"event": "failed", "index": 21718}) + "\nTraceback...\n"
        )
        result = c.diagnose(self.root)
        self.assertEqual(result["failures"][0]["reason"], "AWS credentials expired or require refresh")
        self.assertEqual(result["failures"][0]["stage"], "before_model_request")

    def test_service_inspection_has_fixed_executable_and_read_only_subcommand(self):
        run = Mock(return_value=SimpleNamespace(
            returncode=0, stdout="ActiveState=failed\nMainPID=0\n", stderr="",
        ))
        result = c.service_status("codex-test.service", run)
        self.assertEqual(result["ActiveState"], "failed")
        self.assertEqual(run.call_args.args[0][:3], ["/usr/bin/systemctl", "--user", "show"])
        with self.assertRaises(ValueError):
            c.service_status("codex-foo.service;arbitrary", run)
        self.assertEqual(run.call_count, 1)

    def test_redaction_hides_image_data_bearer_and_query_credentials(self):
        result = c.sanitize("Bearer secret-value data:image/png;base64,abc https://test/?token=hidden")
        for secret in ("secret-value", "base64,abc", "token=hidden"):
            self.assertNotIn(secret, result)


if __name__ == "__main__":
    unittest.main()
