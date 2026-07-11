from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "burn_subtitles.py"
SPEC = importlib.util.spec_from_file_location("burn_subtitles", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
burn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(burn)


class BurnSubtitleValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.subtitle = self.root / "bilingual.ass"
        self.subtitle.write_bytes(b"[Script Info]\nTitle: test\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def report(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "structurally_valid": True,
            "validation_scope": "structural_source_integrity",
            "segment_count": 2,
            "translation_count": 2,
            "outputs": {
                "bilingual.ass": hashlib.sha256(self.subtitle.read_bytes()).hexdigest()
            },
        }
        value.update(overrides)
        return value

    def write_report(self, value: dict[str, object] | None = None) -> Path:
        path = self.root / "validation.json"
        path.write_text(
            json.dumps(value if value is not None else self.report()),
            encoding="utf-8",
        )
        return path

    def test_accepts_matching_structural_validation_report(self) -> None:
        report = self.write_report()

        validated = burn._validate_validation_report(self.subtitle, report)

        self.assertEqual(validated, report.resolve())

    def test_rejects_stale_ass_checksum(self) -> None:
        report = self.write_report()
        self.subtitle.write_bytes(self.subtitle.read_bytes() + b"stale")

        with self.assertRaisesRegex(burn.BurnError, "bilingual.ass.*SHA-256"):
            burn._validate_validation_report(self.subtitle, report)

    def test_rejects_missing_structural_report(self) -> None:
        missing = self.root / "missing-validation.json"

        with self.assertRaisesRegex(burn.BurnError, "validation report.*does not exist"):
            burn._validate_validation_report(self.subtitle, missing)

    def test_rejects_report_without_structural_approval(self) -> None:
        report = self.write_report(self.report(structurally_valid=False))

        with self.assertRaisesRegex(burn.BurnError, "structurally_valid=true"):
            burn._validate_validation_report(self.subtitle, report)

    def test_rejects_wrong_validation_scope(self) -> None:
        report = self.write_report(self.report(validation_scope="translation_review"))

        with self.assertRaisesRegex(burn.BurnError, "structural_source_integrity"):
            burn._validate_validation_report(self.subtitle, report)

    def test_cli_accepts_validation_report_override(self) -> None:
        args = burn._parser().parse_args(
            [
                "input.mp4",
                "bilingual.ass",
                "output.mp4",
                "--validation-report",
                "reviewed.json",
            ]
        )

        self.assertEqual(args.validation_report, Path("reviewed.json"))

    def test_selects_libass_capable_ffmpeg_full_when_path_build_lacks_it(self) -> None:
        default = self.root / "bin" / "ffmpeg"
        full = self.root / "opt" / "ffmpeg-full" / "bin" / "ffmpeg"
        default.parent.mkdir(parents=True)
        full.parent.mkdir(parents=True)
        default.write_text("", encoding="utf-8")
        full.write_text("", encoding="utf-8")

        with mock.patch.object(
            burn,
            "_ffmpeg_has_subtitles_filter",
            side_effect=lambda path: Path(path) == full,
        ):
            selected = burn._select_libass_ffmpeg(str(default), candidates=[full])

        self.assertEqual(selected, str(full))

    def test_progress_bar_is_compact_and_human_readable(self) -> None:
        line = burn._format_progress(50, 71.5, 143.0, "0.68x")

        self.assertEqual(
            line,
            "烧录 [██████████░░░░░░░░░░]  50%  01:11 / 02:23  0.68x",
        )
        self.assertLess(len(line), 80)

    def test_encode_command_uses_machine_readable_quiet_progress(self) -> None:
        command, _ = burn._encode_command(
            "ffmpeg",
            self.root / "input.mkv",
            self.subtitle,
            self.root / "output.mp4",
            {"index": 0},
            [],
            force=False,
            crf=18,
            preset="slow",
            encoder="libx264",
        )

        self.assertIn("-nostats", command)
        self.assertEqual(command[command.index("-loglevel") + 1], "error")
        self.assertEqual(command[command.index("-progress") + 1], "pipe:1")

    def test_rejects_output_duration_mismatch(self) -> None:
        input_video = {
            "codec_type": "video",
            "width": 320,
            "height": 180,
            "avg_frame_rate": "24/1",
        }
        output_probe = {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "8.9"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 320,
                    "height": 180,
                    "avg_frame_rate": "24/1",
                    "duration": "8.9",
                }
            ],
        }

        with mock.patch.object(burn, "_probe", return_value=output_probe):
            with self.assertRaisesRegex(burn.BurnError, "duration changed"):
                burn._verify_output(
                    "ffprobe",
                    self.root / "output.mp4",
                    input_video,
                    False,
                    input_duration=10.0,
                )


if __name__ == "__main__":
    unittest.main()
