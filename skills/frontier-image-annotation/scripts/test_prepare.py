import unittest
import csv
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prepare


class PreparationTests(unittest.TestCase):
    def test_september_window(self):
        start, end = prepare.bounds("2026-09-01", "2026-09-30")
        self.assertEqual(prepare.stamp(start), "2026-09-01T04:00:00Z")
        self.assertEqual(prepare.stamp(end), "2026-10-01T04:00:00Z")

    def test_dst_window(self):
        start, end = prepare.bounds("2026-11-01", "2026-11-01")
        self.assertEqual((end - start).total_seconds(), 25 * 3600)

    def test_reversed_dates(self):
        with self.assertRaises(ValueError):
            prepare.bounds("2026-09-30", "2026-09-01")

    def test_paths(self):
        self.assertEqual(
            prepare.image_uri("2026-09-01/abc.png", "s3://bucket/res1"),
            "s3://bucket/res1/2026-09-01/abc.png",
        )
        for path in ("/tmp/image.png", "../image.png", "s3://other/image.png"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                prepare.image_uri(path, "s3://bucket/res1")
        self.assertIsNone(prepare.image_uri("", "s3://bucket/res1"))

    def test_prefer_unshared_and_preserve_missing(self):
        rows = [
            {"SkuId": "a", "s3_uri": "shared", "prediction_timestamp": "1"},
            {"SkuId": "b", "s3_uri": "shared", "prediction_timestamp": "1"},
            {"SkuId": "a", "s3_uri": "unique", "prediction_timestamp": "2"},
        ]
        selected, missing = prepare.select_candidates({"a", "b", "c"}, rows)
        self.assertEqual(selected[0]["s3_uri"], "unique")
        self.assertFalse(selected[0]["shared_image"])
        self.assertTrue(selected[1]["shared_image"])
        self.assertEqual(missing[0]["SkuId"], "c")

    def test_no_inventory_expansion(self):
        with self.assertRaises(ValueError):
            prepare.select_candidates({"a"}, [{"SkuId": "outside", "s3_uri": "x"}])

    def test_partial_search_rejected(self):
        for payload in ({"timed_out": True}, {"_shards": {"failed": 1}}, {"error": {}}):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                prepare.validate_search(payload)

    def test_s3_validation_excludes_empty_and_deduplicates_bytes(self):
        # Retain the fixture directory: workspace cleanup needs user approval.
        root = Path(tempfile.mkdtemp(prefix="frontier-preparation-test-"))
        (root / ".native-work").mkdir()
        args = SimpleNamespace(date_from="2026-09-01", date_to="2026-09-30", approved_s3_read=True)
        summary = {
            "date_from": args.date_from, "date_to": args.date_to,
            "source": "SkuRobotEligibilityChange", "unique_skus": 3,
        }
        prepare.save(root / "unique-skus.summary.json", summary)
        prepare.write_csv(root / "unique-skus.csv", ["SkuId"],
                          [{"SkuId": sku} for sku in ("a", "b", "c")])
        prepare.save(root / "native-mapping.summary.json", summary)
        prepare.write_csv(root / "native-one-image-per-sku.csv", ["SkuId", "s3_uri"], [
            {"SkuId": "a", "s3_uri": "s3://bucket/good.png"},
            {"SkuId": "b", "s3_uri": "s3://bucket/good.png"},
            {"SkuId": "c", "s3_uri": "s3://bucket/empty.png"},
        ])
        calls = []

        class FakeError(Exception):
            pass

        class FakeClient:
            def get_caller_identity(self):
                return {"Account": "test-account", "Arn": "test-arn"}

            def head_object(self, Bucket, Key):
                calls.append((Bucket, Key))
                return {"ContentLength": 10 if Key == "good.png" else 0, "ETag": "test-etag"}

        fake = SimpleNamespace(Session=lambda: SimpleNamespace(client=lambda service: FakeClient()))
        with patch.dict("sys.modules", {
            "boto3": fake,
            "botocore.exceptions": SimpleNamespace(ClientError=FakeError),
        }), patch.object(prepare, "emit"):
            prepare.validate_s3(args, root)
        result = json.loads((root / "s3-validation.summary.json").read_text())
        self.assertEqual(result["validated_skus"], 2)
        self.assertEqual(result["rejected_skus"], 1)
        self.assertEqual(result["s3_total_bytes"], 10)
        self.assertEqual(len(calls), 2)
        self.assertEqual((root / "s3-paths.txt").read_text(), "s3://bucket/good.png\n")
        cache = json.loads((root / ".native-work/s3-head-cache.json").read_text())
        self.assertTrue(cache["s3://bucket/good.png"]["checked_at"])
        with (root / "one-image-per-sku.csv").open() as source:
            self.assertEqual({row["SkuId"] for row in csv.DictReader(source)}, {"a", "b"})

    def test_s3_auth_error_does_not_publish_success(self):
        root = Path(tempfile.mkdtemp(prefix="frontier-preparation-test-"))
        args = SimpleNamespace(
            date_from="2026-09-01", date_to="2026-09-30",
            approved_s3_read=True, mapping_source="fallback",
        )
        summary = {
            "date_from": args.date_from, "date_to": args.date_to,
            "source": "SkuRobotEligibilityChange", "unique_skus": 1, "mapped_skus": 1,
        }
        prepare.save(root / "unique-skus.summary.json", summary)
        prepare.write_csv(root / "unique-skus.csv", ["SkuId"], [{"SkuId": "a"}])
        prepare.save(root / "fallback-mapping.summary.json", summary)
        prepare.write_csv(root / "fallback-one-image-per-sku.csv", ["SkuId", "s3_uri"], [
            {"SkuId": "a", "s3_uri": "s3://bucket/image.png"},
        ])

        class AccessDenied(Exception):
            response = {"Error": {"Code": "AccessDenied"}}

        class FakeClient:
            def get_caller_identity(self):
                return {"Account": "test-account", "Arn": "test-arn"}

            def head_object(self, **kwargs):
                raise AccessDenied()

        fake = SimpleNamespace(Session=lambda: SimpleNamespace(client=lambda service: FakeClient()))
        with patch.dict("sys.modules", {
            "boto3": fake, "botocore.exceptions": SimpleNamespace(ClientError=AccessDenied),
        }), patch.object(prepare, "emit"):
            with self.assertRaises(AccessDenied):
                prepare.validate_s3(args, root)
        self.assertFalse((root / "s3-validation.summary.json").exists())
        self.assertFalse((root / "one-image-per-sku.csv").exists())

    def test_s3_read_requires_approval(self):
        with self.assertRaises(PermissionError):
            prepare.validate_s3(SimpleNamespace(approved_s3_read=False), Path("/unused"))


if __name__ == "__main__":
    unittest.main()
