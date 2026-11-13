import argparse
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from PIL import Image

import annotation_job as job


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.output = self.root / "labels"
        self.output.mkdir()
        Image.new("RGB", (8, 8)).save(self.images / "one.png")
        self.config = self.root / "product_annotation.yaml"
        self.config.write_text("model: test\nlabels:\n  - name: Good\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_credentials_require_named_literal_owner_only_assignment(self):
        keyfile = self.root / ".env"
        keyfile.write_text("OTHER=wrong\nexport SELECTED='fixture-key'\n")
        keyfile.chmod(0o600)
        self.assertEqual(job.dotenv_key(keyfile, "SELECTED"), "fixture-key")
        keyfile.chmod(0o644)
        with self.assertRaises(ValueError):
            job.dotenv_key(keyfile, "SELECTED")

    def test_credentials_reject_missing_duplicate_and_shell_expansion(self):
        keyfile = self.root / ".env"
        for text in ("OTHER=key", "SELECTED=a\nSELECTED=b", "SELECTED='$(echo secret)'"):
            keyfile.write_text(text)
            keyfile.chmod(0o600)
            with self.subTest(text=text), self.assertRaises(ValueError):
                job.dotenv_key(keyfile, "SELECTED")

    def test_duplicate_stems_are_rejected(self):
        (self.images / "sub").mkdir()
        Image.new("RGB", (8, 8)).save(self.images / "sub/one.png")
        with self.assertRaises(ValueError):
            job.inventory(self.images)

    def test_validation_catches_missing_invalid_and_extra_outputs(self):
        report = job.validate_outputs(self.images, self.output, ["Good"])
        self.assertFalse(job.complete(report))
        (self.output / "one.txt").write_text("Wrong")
        (self.output / "one_crop.png").write_bytes(b"")
        (self.output / "extra.txt").write_text("Good")
        report = job.validate_outputs(self.images, self.output, ["Good"])
        self.assertEqual(report["invalid_labels"], ["one"])
        self.assertEqual(report["empty_crops"], ["one"])
        self.assertEqual(report["extra_labels"], ["extra"])

    def test_validation_accepts_complete_outputs(self):
        (self.output / "one.txt").write_text("Good\n")
        Image.new("RGB", (8, 8)).save(self.output / "one_crop.png")
        self.assertTrue(job.complete(job.validate_outputs(self.images, self.output, ["Good"])))

    def fake_annotation(self, response):
        def save_label(label, path):
            path.write_text(label)
        return SimpleNamespace(
            encode_image_to_data_url=lambda _: ("encoded", Image.new("RGB", (8, 8))),
            annotate_image=lambda *_: response,
            label_from_response=lambda r: r.output_text,
            save_annotated_crop=lambda image, label, path: image.save(path),
            save_label=save_label,
        )

    def test_empty_response_property_cannot_crash_diagnostics(self):
        class BrokenResponse:
            @property
            def output_text(self):
                raise TypeError("sensitive upstream error")
        result = job.process_one(self.fake_annotation(BrokenResponse()), None,
                                 self.images / "one.png", self.output)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "TypeError")
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertFalse((self.output / "one.txt").exists())

    def test_success_writes_crop_before_completion_marker(self):
        result = job.process_one(self.fake_annotation(SimpleNamespace(output_text="Good")),
                                 None, self.images / "one.png", self.output)
        self.assertTrue(result["success"])
        self.assertTrue(job.complete(job.validate_outputs(self.images, self.output, ["Good"])))

    def source_job(self):
        source = self.root / "sample"
        (source / "images").mkdir(parents=True)
        (source / "product_annotation.yaml").write_bytes(self.config.read_bytes())
        (source / "images/one.png").write_bytes((self.images / "one.png").read_bytes())
        labels = source / "images_labels_test"
        labels.mkdir()
        (labels / "one.txt").write_text("Good\n")
        Image.new("RGB", (8, 8)).save(labels / "one_crop.png")
        return source

    def test_seed_verifies_hashes_and_preserves_existing(self):
        source = self.source_job()
        rows = job.seed_results(job.inventory(self.images), self.output, self.config, [source], ["Good"])
        self.assertEqual(rows[0]["stem"], "one")
        (self.output / "one.txt").write_text("Different\n")
        with self.assertRaises(ValueError):
            job.seed_results(job.inventory(self.images), self.output, self.config, [source], ["Good"])
        self.assertEqual((self.output / "one.txt").read_text(), "Different\n")

    def test_seed_rejects_config_and_image_changes(self):
        source = self.source_job()
        (source / "product_annotation.yaml").write_text("different")
        with self.assertRaises(ValueError):
            job.seed_results(job.inventory(self.images), self.output, self.config, [source], ["Good"])
        (source / "product_annotation.yaml").write_bytes(self.config.read_bytes())
        Image.new("RGB", (9, 9)).save(source / "images/one.png")
        with self.assertRaises(ValueError):
            job.seed_results(job.inventory(self.images), self.output, self.config, [source], ["Good"])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_active_lock_prevents_run_before_credentials_or_api(self):
        state = self.root / "images_labels_test/.job"
        state.mkdir(parents=True)
        args = argparse.Namespace(images=self.images, config=self.config)
        with (state / "run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                job.run_job(args)

    def test_no_default_profile_even_with_inherited_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-selected"}):
            with patch("sys.argv", ["annotation_job.py", "--images", str(self.images),
                                    "--config", str(self.config), "--approved-billable-run"]):
                with self.assertRaises(SystemExit):
                    job.main()

    def job_args(self):
        return argparse.Namespace(
            images=self.images, config=self.config, repo=self.root,
            credential_source="agent-secrets", credential_profile="chosen",
            credential_file=None, base_url="https://fixture.invalid/v1",
            reuse_from=[], workers=1, api_max_retries=0, progress_seconds=60,
            stop_after_failures=2,
        )

    def run_fake(self, args, annotation):
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _: None))
        with patch.object(job.runner, "discover", return_value=self.config), \
             patch.object(job.runner, "stage_annotation_script", return_value=self.config), \
             patch.object(job.importlib.util, "spec_from_file_location", return_value=spec), \
             patch.object(job.importlib.util, "module_from_spec", return_value=annotation), \
             patch("openai.OpenAI", return_value=Mock()) as client, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-only"}), \
             contextlib.redirect_stdout(io.StringIO()):
            result = job.run_job(args)
            return result, client

    def test_run_resume_skips_success_and_rejects_account_change(self):
        annotation = self.fake_annotation(SimpleNamespace(output_text="Good"))
        annotation.annotate_image = Mock(return_value=SimpleNamespace(output_text="Good"))
        args = self.job_args()
        result, _ = self.run_fake(args, annotation)
        self.assertEqual(result, 0)
        self.assertEqual(annotation.annotate_image.call_count, 1)
        result, _ = self.run_fake(args, annotation)
        self.assertEqual(result, 0)
        self.assertEqual(annotation.annotate_image.call_count, 1)
        state_dir = self.root / "images_labels_test/.job"
        self.assertEqual(json.loads((state_dir / "state.json").read_text())["status"], "complete")
        self.assertTrue(job.complete(json.loads((state_dir / "validation.json").read_text())))
        args.credential_profile = "other-account"
        with self.assertRaises(ValueError):
            self.run_fake(args, annotation)
        self.assertEqual(annotation.annotate_image.call_count, 1)

    def test_failure_limit_stops_new_work_and_journals_each_failure(self):
        for name in ("two", "three", "four"):
            Image.new("RGB", (8, 8)).save(self.images / f"{name}.png")
        annotation = self.fake_annotation(None)
        annotation.annotate_image = Mock(side_effect=RuntimeError("fixture failure"))
        result, _ = self.run_fake(self.job_args(), annotation)
        self.assertEqual(result, 1)
        self.assertEqual(annotation.annotate_image.call_count, 2)
        state_dir = self.root / "images_labels_test/.job"
        events = [json.loads(line) for p in state_dir.glob("results-*.jsonl")
                  for line in p.read_text().splitlines()]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(not e["success"] for e in events))
        self.assertEqual(json.loads((state_dir / "state.json").read_text())["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
