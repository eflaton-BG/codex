import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from unittest.mock import patch

import fallback


class FallbackTests(unittest.TestCase):
    def test_retry_replaces_unvalidated_pending_response(self):
        """A failed response must not prevent a subsequent validated retry."""
        work = Path(tempfile.mkdtemp(prefix="frontier-fallback-test-"))
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        exporter = SimpleNamespace(SYNC_LOGGER="sync", SAVE_LOGGER="save")
        attempts = []

        def fake_cli(*arguments):
            path = Path(arguments[arguments.index("--output") + 1])
            attempts.append(path)
            if len(attempts) == 1:
                path.write_text("gateway failure")
                raise RuntimeError("502")
            if path.exists() and "--overwrite" not in arguments:
                raise RuntimeError("Output file exists")
            path.write_text(json.dumps({
                "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
            }))
            return {"status": 200}

        with patch.object(fallback.prepare, "cli", side_effect=fake_cli):
            rows = fallback.read_log_slice(
                "uuid", work, start, start + timedelta(minutes=30), "sync", exporter,
            )
        self.assertEqual(rows, [])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(list(work.glob("*.json"))), 1)

    def test_stopped_worker_does_not_query(self):
        stop = Event()
        stop.set()
        with patch.object(fallback.prepare, "cli") as provider:
            with self.assertRaisesRegex(RuntimeError, "Stopped"):
                fallback.read_log_slice(None, None, None, None, None, None, stop)
        provider.assert_not_called()

    def test_oversized_slice_splits_without_losing_evidence(self):
        work = Path(tempfile.mkdtemp(prefix="frontier-fallback-test-"))
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = start + timedelta(hours=2)
        calls = []
        exporter = SimpleNamespace(
            SYNC_LOGGER="sync", SAVE_LOGGER="save",
            iso_z=fallback.prepare.stamp,
            parse_datetime=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
        )

        def fake_cli(*arguments):
            body = json.loads(arguments[arguments.index("--body-text") + 1])
            calls.append(body)
            window = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
            first = exporter.parse_datetime(window["gte"])
            last = exporter.parse_datetime(window["lt"])
            too_large = last - first > timedelta(hours=1)
            hits = [] if too_large else [{"_source": {
                "@timestamp": window["gte"], "tote_id": "tote",
                "latest_image_timestamp": window["gte"],
            }}]
            payload = {
                "timed_out": False, "_shards": {"failed": 0},
                "hits": {"total": {"value": 10001 if too_large else 1, "relation": "eq"},
                         "hits": hits},
            }
            path = Path(arguments[arguments.index("--output") + 1])
            path.write_text(json.dumps(payload))
            return {"status": 200}

        with patch.object(fallback.prepare, "cli", side_effect=fake_cli):
            rows = fallback.read_log_slice("uuid", work, start, end, "sync", exporter)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(calls), 3)
        self.assertNotEqual(rows[0]["sync_timestamp"], rows[1]["sync_timestamp"])
        # A complete child slice resumes without making a provider request.
        with patch.object(fallback.prepare, "cli", side_effect=AssertionError("unexpected provider call")):
            cached = fallback.read_log_slice(
                "uuid", work, start, start + timedelta(hours=1), "sync", exporter,
            )
        self.assertEqual(cached, rows[:1])

    def test_partial_tiny_slice_fails_closed(self):
        work = Path(tempfile.mkdtemp(prefix="frontier-fallback-test-"))
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        exporter = SimpleNamespace(SYNC_LOGGER="sync", SAVE_LOGGER="save")

        def fake_cli(*arguments):
            path = Path(arguments[arguments.index("--output") + 1])
            path.write_text(json.dumps({
                "timed_out": True, "_shards": {"failed": 0},
                "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
            }))
            return {"status": 200}

        with patch.object(fallback.prepare, "cli", side_effect=fake_cli):
            with self.assertRaises(RuntimeError):
                fallback.read_log_slice("uuid", work, start, start + timedelta(seconds=1), "sync", exporter)
        self.assertEqual(list(work.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
