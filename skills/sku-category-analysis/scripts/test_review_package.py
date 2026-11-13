import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image


class ReviewPackageTests(unittest.TestCase):
    def test_shared_image_counts_once_for_review_twice_for_sku_chart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "images"
            labels = root / "labels"
            manifest = root / "manifest"
            for path in (images, labels, manifest):
                path.mkdir()
            Image.new("RGB", (8, 8)).save(images / "one.png")
            Image.new("RGB", (8, 8)).save(labels / "one_crop.png")
            (labels / "one.txt").write_text("Books\n")
            with (manifest / "one-image-per-sku.csv").open("w") as stream:
                writer = csv.DictWriter(stream, fieldnames=["SkuId", "s3_uri"])
                writer.writeheader()
                writer.writerows([{"SkuId": sku, "s3_uri": "s3://fixture/one.png"} for sku in ("a", "b")])
            command = [
                sys.executable, str(Path(__file__).with_name("build_review_package.py")),
                "--job-dir", str(root), "--labels-dir", str(labels), "--month", "Test Month",
                "--sample-size", "1", "--seed", "7",
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((root / "annotation-review-1/sampling-summary.json").read_text())
            self.assertEqual(summary["sample_size"], 1)
            self.assertEqual(summary["population_image_counts"], {"Books": 1})
            self.assertEqual(summary["population_sku_counts"], {"Books": 2})
            with Image.open(root / "Product Category Distribution Test Month.png") as image:
                image.verify()
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("Refusing to overwrite", second.stderr)


if __name__ == "__main__":
    unittest.main()
