import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import status


class StatusTests(unittest.TestCase):
    def test_reports_deduplicated_completed_day_coverage_and_stored_error(self):
        """Read local progress without counting partial days or hiding errors."""
        job = Path(tempfile.mkdtemp(prefix="frontier-status-test-"))
        root = job / "manifest"
        work = root / ".fallback-work"
        for day in ("day1", "day2", "partial"):
            (work / day).mkdir(parents=True)
        (root / "unique-skus.summary.json").write_text('{"unique_skus": 3}')
        for day, skus in (("day1", ["a", "b"]), ("day2", ["b"])):
            (work / day / "complete.json").write_text(json.dumps({
                "candidates": [{"SkuId": sku} for sku in skus],
            }))
        (work / "partial" / "sync-example.json").write_text("{}")
        (work / "last-error.json").write_text('{"error_type": "RuntimeError"}')
        output = io.StringIO()
        with redirect_stdout(output):
            status.status(job)
        result = json.loads(output.getvalue())
        self.assertEqual(result["completed_days"], 2)
        self.assertEqual(result["candidate_skus"], 2)
        self.assertEqual(result["inventory_skus"], 3)
        self.assertEqual(result["validated_log_slices"], 1)
        self.assertEqual(result["fallback_error"], {"error_type": "RuntimeError"})
        self.assertEqual(result["scope"], "preparation_only")


if __name__ == "__main__":
    unittest.main()
