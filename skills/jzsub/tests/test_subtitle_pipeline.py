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
                    "zh_cn": f"中文 {index}",
                }
                for index, segment in enumerate(manifest["segments"], start=1)
            ]
        (directory / filename).write_text(
            json.dumps({"translations": records}, ensure_ascii=False),
            encoding="utf-8",
        )
        return directory

    def test_compact_batches_omit_model_visible_hashes(self) -> None:
        manifest_path, manifest = self.prepare_fixture(
            srt([("00:00:00,000", "00:00:01,000", "Hello world")])
        )
        self.assertEqual(manifest["translation_contract_version"], 3)
        batch = json.loads(
            Path(manifest["translation_batches"][0]["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(set(batch["items"][0]), {"id", "source"})
        self.assertNotIn("source_sha256", json.dumps(batch))
        self.assertEqual(batch["output_fields"], ["id", "zh_cn"])

        translations_dir = self.write_translations(manifest)
        pipeline.render(manifest_path, translations_dir, self.root / "output")

    def test_complete_subtitle_is_one_unified_translation_document(self) -> None:
        cues = [
            (f"00:00:{index:02d},000", f"00:00:{index:02d},900", f"Line {index}")
            for index in range(25)
        ]
        manifest_path, manifest = self.prepare_fixture(srt(cues))

        first = pipeline.next_translation_batch(manifest_path)
        self.assertFalse(first["done"])
        self.assertEqual(first["remaining"], 1)
        self.assertEqual(len(first["batch"]["items"]), 25)
        self.assertEqual(first["batch"]["context"], {"before": [], "after": []})
        self.assertNotIn("segments", first)
        self.assertNotIn("cues", first)
        self.assertNotIn("source_sha256", json.dumps(first))

        output = Path(first["output_path"])
        output.write_text(
            json.dumps(
                {"translations": [
                    {"id": item["id"], "zh_cn": "中文"}
                    for item in first["batch"]["items"]
                ]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        complete = pipeline.next_translation_batch(manifest_path)
        self.assertTrue(complete["done"])
        self.assertEqual(complete["remaining"], 0)

    def test_ass_uses_fixed_vertical_anchors_for_source_and_chinese(self) -> None:
        manifest_path, manifest = self.prepare_fixture(
            srt([
                ("00:00:00,000", "00:00:02,000", "Short"),
                (
                    "00:00:02,100",
                    "00:00:05,000",
                    "A much longer source caption that wraps onto another display line while preserving its exact text",
                ),
            ])
        )
        translations_dir = self.write_translations(
            manifest,
            records=[
                {"id": manifest["segments"][0]["id"], "zh_cn": "短句"},
                {"id": manifest["segments"][1]["id"], "zh_cn": "这是一条会换行的较长中文字幕 用来验证位置固定"},
            ],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        rendered = output_dir.joinpath("bilingual.ass").read_text(encoding="utf-8")

        source_events = [line for line in rendered.splitlines() if ",Original," in line]
        chinese_events = [line for line in rendered.splitlines() if ",Chinese," in line]
        self.assertEqual(len(source_events), 2)
        self.assertEqual(len(chinese_events), 2)
        self.assertTrue(all(r"{\an2\pos(960,875)}" in line for line in source_events))
        self.assertTrue(all(r"{\an2\pos(960,1010)}" in line for line in chinese_events))

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
        self.assertEqual(batch["output_fields"], ["id", "zh_cn"])
        self.assertNotIn("source", batch["output_fields"])

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

    def test_chinese_house_style_and_measured_background(self) -> None:
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
        self.assertIn("Style: BackgroundOriginal,MiSans,42", rendered)
        self.assertIn("Style: BackgroundChinese,MiSans,46", rendered)
        self.assertIn(",3,8,0,2,80,80,70,1", rendered)
        self.assertIn("Dialogue: 0,", rendered)
        self.assertNotIn(r"{\an7\p1}", rendered)
        self.assertIn("Dialogue: 1,", rendered)
        pipeline.validate(manifest_path, translations_dir, output_dir)

    def test_background_uses_identical_text_layout_for_libass_measurement(self) -> None:
        source = "日本語の字幅は Latin text と同じではありません"
        manifest_path, manifest = self.prepare_fixture(
            srt([("00:00:00,000", "00:00:03,000", source)]),
            source_language="ja",
        )
        segment = manifest["segments"][0]
        translations_dir = self.write_translations(
            manifest,
            records=[
                {
                    "id": segment["id"],
                    "source_sha256": segment["source_sha256"],
                    "zh_cn": "日文字形宽度与拉丁文字不同",
                }
            ],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        rendered = output_dir.joinpath("bilingual.ass").read_text(encoding="utf-8")

        self.assertIn("Style: BackgroundOriginal,MiSans,42", rendered)
        self.assertIn("Style: BackgroundChinese,MiSans,46", rendered)
        self.assertIn(",3,8,0,2,80,80,70,1", rendered)
        self.assertNotIn(r"{\an7\p1}", rendered)
        self.assertIn(
            r"BackgroundOriginal,,0,0,0,,{\an2\pos(960,875)}" + source,
            rendered,
        )
        self.assertIn(
            r"BackgroundChinese,,0,0,0,,{\an2\pos(960,1010)}日文字形宽度与拉丁文字不同",
            rendered,
        )

    def test_smart_mode_groups_only_whole_adjacent_cues(self) -> None:
        raw = srt(
            [
                ("00:00:00,000", "00:00:00,900", "Hello"),
                ("00:00:01,000", "00:00:02,000", "world."),
                ("00:00:04,000", "00:00:05,000", "Separate."),
            ]
        )
        manifest_path, manifest = self.prepare_fixture(raw, segment_mode="smart")
        self.assertEqual(len(manifest["segments"]), 3)
        self.assertEqual(len(manifest["render_segments"]), 2)
        self.assertEqual(
            manifest["render_segments"][0]["cue_ids"],
            [manifest["cues"][0]["id"], manifest["cues"][1]["id"]],
        )
        self.assertEqual(manifest["cues"][0]["text"], "Hello")
        self.assertEqual(manifest["cues"][1]["text"], "world.")
        covered = [cue_id for segment in manifest["render_segments"] for cue_id in segment["cue_ids"]]
        self.assertEqual(covered, [cue["id"] for cue in manifest["cues"]])

        translations_dir = self.write_translations(manifest)
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        rendered = output_dir.joinpath("source.srt").read_text(encoding="utf-8")
        self.assertIn("Hello\nworld.", rendered)
        chinese = output_dir.joinpath("zh-CN.srt").read_text(encoding="utf-8")
        self.assertIn("中文 1 中文 2", chinese)
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
        self.assertEqual(manifest["render_segments"][0]["end_ms"], 2000)
        self.assertEqual(manifest["render_segments"][1]["end_ms"], 5000)
        for current, following in zip(manifest["render_segments"], manifest["render_segments"][1:]):
            self.assertLessEqual(current["end_ms"], following["start_ms"])


if __name__ == "__main__":
    unittest.main()
