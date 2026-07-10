from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "subtitle_pipeline.py"
SPEC = importlib.util.spec_from_file_location("subtitle_pipeline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)


def srt(cues: list[tuple[str, str, str]], newline: str = "\n") -> bytes:
    blocks = []
    for index, (start, end, text) in enumerate(cues, start=1):
        blocks.append(f"{index}{newline}{start} --> {end}{newline}{text}")
    return (newline + newline).join(blocks).encode("utf-8") + newline.encode("ascii")


class SubtitlePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare_fixture(
        self,
        raw: bytes,
        *,
        segment_mode: str = "preserve",
        source_language: str = "en",
    ) -> tuple[Path, dict]:
        source = self.root / "downloaded.srt"
        source.write_bytes(raw)
        manifest_path = pipeline.prepare(
            source, self.root / "work", source_language, segment_mode
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return manifest_path, manifest

    def write_translations(
        self,
        manifest: dict,
        *,
        records: list[dict] | None = None,
        filename: str = "translations.json",
    ) -> Path:
        directory = self.root / "translations"
        directory.mkdir(exist_ok=True)
        if records is None:
            records = [
                {
                    "id": segment["id"],
                    "source_sha256": segment["source_sha256"],
                    "zh_cn": f"中文 {index}",
                }
                for index, segment in enumerate(manifest["segments"], start=1)
            ]
        (directory / filename).write_text(
            json.dumps({"translations": records}, ensure_ascii=False),
            encoding="utf-8",
        )
        return directory

    def clear_translations(self) -> None:
        directory = self.root / "translations"
        if directory.exists():
            for path in directory.glob("*.json"):
                path.unlink()

    def test_raw_archive_and_source_text_are_exactly_preserved(self) -> None:
        raw = b"\xef\xbb\xbf" + srt(
            [
                ("00:00:00,100", "00:00:01,500", "  Café & co.  "),
                ("00:00:01,700", "00:00:03,000", "Line one\r\nLine two"),
            ],
            newline="\r\n",
        )
        manifest_path, manifest = self.prepare_fixture(raw)

        archive = Path(manifest["source"]["archive_path"])
        self.assertEqual(archive.read_bytes(), raw)
        self.assertTrue((self.root / "work" / "translation-output").is_dir())
        self.assertEqual(manifest["cues"][0]["text"], "  Café & co.  ")
        self.assertEqual(manifest["cues"][1]["text"], "Line one\nLine two")

        original_path = Path(manifest["source"]["original_path"])
        original_path.write_bytes(
            srt([("00:00:00,000", "00:00:01,000", "Different source")])
        )
        with self.assertRaisesRegex(pipeline.PipelineError, "write-once source archive"):
            pipeline.prepare(original_path, self.root / "work", "en")
        self.assertEqual(archive.read_bytes(), raw)

        batch_path = Path(manifest["translation_batches"][0]["path"])
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        self.assertEqual(batch["items"][0]["source"], "  Café & co.  ")
        self.assertNotIn("source", batch["required_output"]["item_fields"])
        self.assertEqual(
            batch["execution_contract"],
            {
                "engine": "active_codex_default_gpt",
                "external_translation_service_allowed": False,
                "local_inference_allowed": False,
            },
        )

        translations_dir = self.write_translations(manifest)
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        pipeline.validate(manifest_path, translations_dir, output_dir)

        source_output = output_dir.joinpath("source.srt").read_text(encoding="utf-8")
        self.assertIn("  Café & co.  ", source_output)
        self.assertIn("Line one\nLine two", source_output)
        report = json.loads(output_dir.joinpath("validation.json").read_text())
        self.assertTrue(report["structurally_valid"])
        self.assertEqual(report["validation_scope"], "structural_source_integrity")
        self.assertEqual(report["font"], "MiSans")
        self.assertEqual(report["font_weight"], 700)
        self.assertFalse(report["translation_quality_reviewed"])
        self.assertTrue(report["invariants"]["raw_source_sha256_locked"])

    def test_ass_malicious_text_is_losslessly_escaped(self) -> None:
        malicious = r"Literal {\pos(10,20)} \N \n \h } { 中文"
        raw = srt([("00:00:00,000", "00:00:02,000", malicious)])
        manifest_path, manifest = self.prepare_fixture(raw)
        segment = manifest["segments"][0]
        translation = r"中文 {\move(0,0,9,9)} \N"
        translations_dir = self.write_translations(
            manifest,
            records=[
                {
                    "id": segment["id"],
                    "source_sha256": segment["source_sha256"],
                    "zh_cn": translation,
                }
            ],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)

        self.assertEqual(
            pipeline.ass_unescape_for_validation(pipeline.ass_escape(malicious)), malicious
        )
        self.assertEqual(
            pipeline.ass_unescape_for_validation(pipeline.ass_escape(translation)), translation
        )
        rendered = output_dir.joinpath("bilingual.ass").read_text(encoding="utf-8")
        self.assertNotIn(r"{\pos(10,20)}", rendered)
        self.assertNotIn(r"{\move(0,0,9,9)}", rendered)
        pipeline.validate(manifest_path, translations_dir, output_dir)

    def test_ass_ffmpeg_guards_round_trip_all_reserved_sequences(self) -> None:
        word_joiner = pipeline.ASS_WORD_JOINER
        cases = [
            r"{\pos(1,2)}\N",
            r"literal \N, \n, and \h",
            "opening { and closing } braces",
            f"existing{word_joiner}word-joiner",
            "\\" + word_joiner + "N",
            "line one\nline two",
            "Unicode 中文 👩\u200d🚀 e\u0301",
        ]
        for source in cases:
            with self.subTest(source=source):
                encoded = pipeline.ass_escape(source)
                self.assertEqual(pipeline.ass_unescape_for_validation(encoded), source)

        self.assertEqual(pipeline.ass_escape("\\N"), "\\" + word_joiner + "N")
        self.assertEqual(pipeline.ass_escape("\\n"), "\\" + word_joiner + "n")
        self.assertEqual(pipeline.ass_escape("\\h"), "\\" + word_joiner + "h")
        self.assertEqual(pipeline.ass_escape("{"), r"\{{}")
        self.assertEqual(pipeline.ass_escape("\n"), r"\N")
        with self.assertRaisesRegex(pipeline.PipelineError, "unguarded backslash"):
            pipeline.ass_unescape_for_validation(r"\h")

    def test_translation_contract_rejects_missing_duplicate_and_hash_mismatch(self) -> None:
        raw = srt(
            [
                ("00:00:00,000", "00:00:01,000", "One"),
                ("00:00:01,100", "00:00:02,000", "Two"),
            ]
        )
        _, manifest = self.prepare_fixture(raw)
        first, second = manifest["segments"]

        translations_dir = self.write_translations(
            manifest,
            records=[
                {
                    "id": first["id"],
                    "source_sha256": first["source_sha256"],
                    "zh_cn": "一",
                }
            ],
        )
        with self.assertRaisesRegex(pipeline.PipelineError, "missing translations"):
            pipeline.load_translations(manifest, translations_dir)

        self.clear_translations()
        valid_first = {
            "id": first["id"],
            "source_sha256": first["source_sha256"],
            "zh_cn": "一",
        }
        self.write_translations(manifest, records=[valid_first], filename="a.json")
        self.write_translations(manifest, records=[valid_first], filename="b.json")
        with self.assertRaisesRegex(pipeline.PipelineError, "duplicate translation ID"):
            pipeline.load_translations(manifest, translations_dir)

        self.clear_translations()
        records = [
            {**valid_first, "source_sha256": "0" * 64},
            {
                "id": second["id"],
                "source_sha256": second["source_sha256"],
                "zh_cn": "二",
            },
        ]
        self.write_translations(manifest, records=records)
        with self.assertRaisesRegex(pipeline.PipelineError, "source SHA-256 mismatch"):
            pipeline.load_translations(manifest, translations_dir)

    def test_translation_contract_rejects_editable_source_and_controls(self) -> None:
        manifest_path, manifest = self.prepare_fixture(
            srt([("00:00:00,000", "00:00:01,000", "Source")])
        )
        del manifest_path
        segment = manifest["segments"][0]
        forbidden = {
            "id": segment["id"],
            "source_sha256": segment["source_sha256"],
            "source": "rewritten",
            "zh_cn": "中文",
        }
        translations_dir = self.write_translations(manifest, records=[forbidden])
        with self.assertRaisesRegex(pipeline.PipelineError, "forbidden/missing fields"):
            pipeline.load_translations(manifest, translations_dir)

        self.clear_translations()
        controlled = {
            "id": segment["id"],
            "source_sha256": segment["source_sha256"],
            "zh_cn": "中\n文",
        }
        self.write_translations(manifest, records=[controlled])
        with self.assertRaisesRegex(pipeline.PipelineError, "control character"):
            pipeline.load_translations(manifest, translations_dir)

    def test_unicode_layout_inserts_breaks_without_changing_source(self) -> None:
        text = "👩\u200d🚀e\u0301 العربية 中文🙂 and-more-text"
        chunks = pipeline.wrap_layout_chunks(text, 6)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(any("👩\u200d🚀" in chunk for chunk in chunks))

        manifest_path, manifest = self.prepare_fixture(
            srt([("00:00:00,000", "00:00:03,000", text)]),
            source_language="ar",
        )
        translations_dir = self.write_translations(manifest)
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir, "Noto Sans")
        pipeline.validate(manifest_path, translations_dir, output_dir, "Noto Sans")

    def test_chinese_house_style_and_rounded_background(self) -> None:
        source = "This English subtitle is intentionally longer than forty-two columns but should remain on one display line"
        manifest_path, manifest = self.prepare_fixture(
            srt([("00:00:00,000", "00:00:03,000", source)])
        )
        segment = manifest["segments"][0]
        translations_dir = self.write_translations(
            manifest,
            records=[
                {
                    "id": segment["id"],
                    "source_sha256": segment["source_sha256"],
                    "zh_cn": "你好，世界。",
                }
            ],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)

        self.assertEqual(pipeline.normalize_chinese_caption("你好，世界。"), "你好 世界")
        self.assertEqual(pipeline.normalize_chinese_caption("版本 5.6，发布。"), "版本 5.6 发布")
        chinese_srt = output_dir.joinpath("zh-CN.srt").read_text(encoding="utf-8")
        self.assertIn("你好 世界", chinese_srt)
        self.assertNotIn("，", chinese_srt)
        self.assertNotIn("。", chinese_srt)
        source_srt = output_dir.joinpath("source.srt").read_text(encoding="utf-8")
        source_lines = source_srt.splitlines()[2:]
        self.assertGreater(max(map(len, source_lines)), 42)
        self.assertLessEqual(max(map(len, source_lines)), pipeline.SOURCE_WRAP_COLUMNS)
        rendered = output_dir.joinpath("bilingual.ass").read_text(encoding="utf-8")
        self.assertIn("Style: Original,MiSans,42", rendered)
        self.assertIn("Style: Chinese,MiSans,46", rendered)
        self.assertIn("Style: Background", rendered)
        self.assertIn("Dialogue: 0,", rendered)
        self.assertIn(r"{\an7\p1}m ", rendered)
        self.assertIn("Dialogue: 1,", rendered)
        pipeline.validate(manifest_path, translations_dir, output_dir)

    def test_background_tightly_tracks_caption_dimensions(self) -> None:
        short = pipeline._ass_background_bounds(["Hi"], ["你好"])
        medium = pipeline._ass_background_bounds(
            ["stream that I've ever seen, but then"],
            ["思绪倾泻 但随后却能得到"],
        )

        short_width = short[2] - short[0]
        medium_width = medium[2] - medium[0]
        self.assertEqual(short_width, pipeline.BACKGROUND_MIN_WIDTH)
        self.assertLess(medium_width, 760)
        self.assertGreater(medium_width, short_width)
        self.assertEqual(medium[3] - medium[1], 121)

    def test_smart_mode_groups_only_whole_adjacent_cues(self) -> None:
        raw = srt(
            [
                ("00:00:00,000", "00:00:00,900", "Hello"),
                ("00:00:01,000", "00:00:02,000", "world."),
                ("00:00:04,000", "00:00:05,000", "Separate."),
            ]
        )
        manifest_path, manifest = self.prepare_fixture(raw, segment_mode="smart")
        self.assertEqual(len(manifest["segments"]), 2)
        self.assertEqual(
            manifest["segments"][0]["cue_ids"],
            [manifest["cues"][0]["id"], manifest["cues"][1]["id"]],
        )
        self.assertEqual(manifest["cues"][0]["text"], "Hello")
        self.assertEqual(manifest["cues"][1]["text"], "world.")
        covered = [
            cue_id for segment in manifest["segments"] for cue_id in segment["cue_ids"]
        ]
        self.assertEqual(covered, [cue["id"] for cue in manifest["cues"]])

        translations_dir = self.write_translations(manifest)
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        rendered = output_dir.joinpath("source.srt").read_text(encoding="utf-8")
        self.assertIn("Hello\nworld.", rendered)
        pipeline.validate(manifest_path, translations_dir, output_dir)

    def test_smart_mode_clamps_rolling_caption_overlap(self) -> None:
        raw = srt(
            [
                ("00:00:00,000", "00:00:04,000", "First sentence."),
                ("00:00:02,000", "00:00:06,000", "Second sentence."),
                ("00:00:05,000", "00:00:07,000", "Third sentence."),
            ]
        )
        _, manifest = self.prepare_fixture(raw, segment_mode="smart")

        self.assertEqual(len(manifest["segments"]), 3)
        self.assertEqual(manifest["cues"][0]["end_ms"], 4000)
        self.assertEqual(manifest["segments"][0]["end_ms"], 2000)
        self.assertEqual(manifest["segments"][1]["end_ms"], 5000)
        for current, following in zip(manifest["segments"], manifest["segments"][1:]):
            self.assertLessEqual(current["end_ms"], following["start_ms"])


if __name__ == "__main__":
    unittest.main()
