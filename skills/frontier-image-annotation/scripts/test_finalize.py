import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import finalize


class FinalizationTests(unittest.TestCase):
    def test_requires_read_and_write_approval_before_any_work(self):
        with self.assertRaises(PermissionError):
            finalize.finalize(SimpleNamespace(approved_write=True, approved_s3_read=False))

    def test_ready_mapping_never_sleeps(self):
        root = Path(tempfile.mkdtemp(prefix="frontier-finalize-test-"))
        expected = {"date_from": "2026-09-01", "date_to": "2026-09-30"}
        (root / "fallback-mapping.summary.json").write_text(json.dumps(expected))
        with patch.object(finalize.time, "sleep") as sleep:
            self.assertEqual(finalize.wait_for_mapping(root, True, 60), expected)
        sleep.assert_not_called()

    def test_missing_mapping_without_wait_fails_immediately(self):
        root = Path(tempfile.mkdtemp(prefix="frontier-finalize-test-"))
        with patch.object(finalize.time, "sleep") as sleep:
            with self.assertRaises(FileNotFoundError):
                finalize.wait_for_mapping(root, False, 60)
        sleep.assert_not_called()

    def test_producer_error_stops_without_sleep_or_network(self):
        root = Path(tempfile.mkdtemp(prefix="frontier-finalize-test-"))
        (root / ".fallback-work").mkdir()
        (root / ".fallback-work/last-error.json").write_text('{"error_type":"test"}')
        with patch.object(finalize.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                finalize.wait_for_mapping(root, True, 60)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
