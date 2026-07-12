from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_delivery.py"
SPEC = importlib.util.spec_from_file_location("verify_delivery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


class VerifyDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "video.intermediate.mkv"
        self.source.write_bytes(b"video")
        self.subtitle = self.root / "video.source-srt.en.srt"
        self.subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        self.manifest = self.root / "download-manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "status": "downloaded",
                    "output_directory": str(self.root),
                    "artifacts": {
                        "intermediate": {"path": self.source.name},
                        "subtitle": {
                            "source_srt": {"path": self.subtitle.name},
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_subtitled_job_with_only_translation_inputs_is_incomplete(self) -> None:
        inputs = self.root / "subtitles" / "translation-input"
        inputs.mkdir(parents=True)
        batch = inputs / "batch-0001.json"
        batch.write_text("{}", encoding="utf-8")
        outputs = self.root / "subtitles" / "translation-output"
        outputs.mkdir()
        (self.root / "subtitles" / "subtitle-manifest.json").write_text(
            json.dumps(
                {
                    "translation_batches": [{"path": str(batch)}],
                    "translation_output_dir": str(outputs),
                }
            ),
            encoding="utf-8",
        )

        result = delivery.assess_delivery(self.manifest)

        self.assertFalse(result["complete"])
        self.assertEqual(result["stage"], "translation_required")
        self.assertIn("batch-0001.json", result["missing"])

    def test_video_only_job_requires_the_video_file_on_disk(self) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "status": "downloaded",
                    "output_directory": str(self.root),
                    "artifacts": {
                        "intermediate": {"path": self.source.name},
                        "subtitle": None,
                    },
                }
            ),
            encoding="utf-8",
        )

        result = delivery.assess_delivery(self.manifest)
        self.assertTrue(result["complete"])
        self.assertEqual(result["stage"], "video_only_complete")

        self.source.unlink()
        with self.assertRaisesRegex(delivery.DeliveryError, "video artifact"):
            delivery.assess_delivery(self.manifest)


if __name__ == "__main__":
    unittest.main()
