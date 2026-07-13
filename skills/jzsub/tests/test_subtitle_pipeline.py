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
        video_size: tuple[int, int] | None = None,
    ) -> tuple[Path, dict]:
        source = self.root / "downloaded.srt"
        source.write_bytes(raw)
        manifest_path = pipeline.prepare(
            source, self.root / "work", source_language, segment_mode, video_size
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
                    "translation": f"中文 {index}",
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
        self.assertEqual(manifest["translation_contract_version"], 4)
        batch = json.loads(
            Path(manifest["translation_batches"][0]["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(set(batch["items"][0]), {"id", "source"})
        self.assertNotIn("source_sha256", json.dumps(batch))
        self.assertEqual(batch["output_fields"], ["id", "translation"])

        translations_dir = self.write_translations(manifest)
        pipeline.render(manifest_path, translations_dir, self.root / "output")

    def test_short_subtitles_fit_one_translation_batch(self) -> None:
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
                    {"id": item["id"], "translation": "中文"}
                    for item in first["batch"]["items"]
                ]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        complete = pipeline.next_translation_batch(manifest_path)
        self.assertTrue(complete["done"])
        self.assertEqual(complete["remaining"], 0)

    def test_long_subtitles_split_into_bounded_context_linked_batches(self) -> None:
        total = pipeline.TRANSLATION_BATCH_SIZE * 2 + 5
        cues = [
            (
                f"{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d},000",
                f"{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d},900",
                f"Line {index}",
            )
            for index in range(total)
        ]
        manifest_path, manifest = self.prepare_fixture(srt(cues))

        batches = manifest["translation_batches"]
        self.assertEqual(len(batches), 3)
        segment_ids = [segment["id"] for segment in manifest["segments"]]
        batched = [batch_id for batch in batches for batch_id in batch["segment_ids"]]
        self.assertEqual(batched, segment_ids)

        second = json.loads(Path(batches[1]["path"]).read_text(encoding="utf-8"))
        size = pipeline.TRANSLATION_BATCH_SIZE
        context_span = pipeline.TRANSLATION_CONTEXT_SEGMENTS
        self.assertEqual(len(second["items"]), size)
        self.assertEqual(
            [item["id"] for item in second["context"]["before"]],
            segment_ids[size - context_span : size],
        )
        self.assertEqual(
            [item["id"] for item in second["context"]["after"]],
            segment_ids[2 * size : 2 * size + context_span],
        )

        remaining = len(batches)
        while True:
            pending = pipeline.next_translation_batch(manifest_path)
            if pending["done"]:
                break
            self.assertEqual(pending["remaining"], remaining)
            Path(pending["output_path"]).write_text(
                json.dumps(
                    {"translations": [
                        {"id": item["id"], "translation": "中文"}
                        for item in pending["batch"]["items"]
                    ]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            remaining -= 1
        self.assertEqual(remaining, 0)
        pipeline.load_translations(manifest, self.root / "work" / "translation-output")

    def test_ass_stacks_source_above_chinese_at_the_bottom(self) -> None:
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
                {"id": manifest["segments"][0]["id"], "translation": "短句"},
                {"id": manifest["segments"][1]["id"], "translation": "这是一条会换行的较长中文字幕 用来验证位置固定"},
            ],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        rendered = output_dir.joinpath("bilingual.ass").read_text(encoding="utf-8")

        text_events = [line for line in rendered.splitlines() if ",Bilingual," in line]
        box_events = [line for line in rendered.splitlines() if ",BilingualBox," in line]
        self.assertEqual(len(text_events), 2)
        self.assertEqual(len(box_events), 2)
        # One bottom-anchored stack: source at fs42 above Chinese at fs46.
        for line in text_events + box_events:
            self.assertIn(r"{\an2\pos(960,1030)\fs42}", line)
        for line in text_events:
            self.assertIn(r"\N{\fs46\1c&H00FFFF&}", line)
        for line in box_events:
            self.assertIn(r"\N{\fs46}", line)
            self.assertNotIn(r"\1c", line)

    def test_portrait_video_gets_matching_playres_and_narrower_wrapping(self) -> None:
        manifest_path, manifest = self.prepare_fixture(
            srt([
                (
                    "00:00:00,000",
                    "00:00:03,000",
                    "A long landscape-width caption that must wrap much earlier on a portrait video",
                )
            ]),
            video_size=(1080, 1920),
        )
        self.assertEqual(manifest["video_size"], {"width": 1080, "height": 1920})
        translations_dir = self.write_translations(
            manifest,
            records=[
                {
                    "id": manifest["segments"][0]["id"],
                    "translation": "竖屏视频中的中文字幕必须按较窄的宽度换行",
                }
            ],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        rendered = output_dir.joinpath("bilingual.ass").read_text(encoding="utf-8")

        layout = pipeline._ass_layout(manifest)
        self.assertEqual(layout["play_res_x"], round(1080 * 1080 / 1920))
        self.assertEqual(layout["source_font_size"], 36)
        self.assertEqual(layout["target_font_size"], 40)
        self.assertEqual(layout["bottom_margin"], 120)
        self.assertEqual(layout["position_y"], 960)
        self.assertLess(layout["target_columns"], pipeline.TARGET_WRAP_COLUMNS)
        self.assertLess(layout["source_columns"], pipeline.SOURCE_WRAP_COLUMNS)
        self.assertIn(f"PlayResX: {layout['play_res_x']}", rendered)
        self.assertIn("PlayResY: 1080", rendered)
        self.assertIn(r"{\an2\pos(304,960)\fs36}", rendered)
        self.assertIn(r"\N{\fs40\1c&H00FFFF&}", rendered)
        chinese_srt = output_dir.joinpath("zh-CN.srt").read_text(encoding="utf-8")
        chinese_lines = [line for line in chinese_srt.splitlines()[2:] if line]
        self.assertGreater(len(chinese_lines), 1)

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
        self.assertEqual(batch["output_fields"], ["id", "translation"])
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
                    "translation": translation,
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
                    "translation": "一",
                }
            ],
        )
        with self.assertRaisesRegex(pipeline.PipelineError, "missing translations"):
            pipeline.load_translations(manifest, translations_dir)

        self.clear_translations()
        valid_first = {
            "id": first["id"],
            "source_sha256": first["source_sha256"],
            "translation": "一",
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
                "translation": "二",
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
            "translation": "中文",
        }
        translations_dir = self.write_translations(manifest, records=[forbidden])
        with self.assertRaisesRegex(pipeline.PipelineError, "forbidden/missing fields"):
            pipeline.load_translations(manifest, translations_dir)

        self.clear_translations()
        controlled = {
            "id": segment["id"],
            "source_sha256": segment["source_sha256"],
            "translation": "中\n文",
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
                    "translation": "你好，世界。",
                }
            ],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)

        self.assertEqual(pipeline.normalize_target_caption("你好，世界。", "zh-CN"), "你好 世界")
        self.assertEqual(pipeline.normalize_target_caption("版本 5.6，发布。", "zh-CN"), "版本 5.6 发布")
        chinese_srt = output_dir.joinpath("zh-CN.srt").read_text(encoding="utf-8")
        self.assertIn("你好 世界", chinese_srt)
        self.assertNotIn("，", chinese_srt)
        self.assertNotIn("。", chinese_srt)
        source_srt = output_dir.joinpath("source.srt").read_text(encoding="utf-8")
        source_lines = source_srt.splitlines()[2:]
        self.assertGreater(max(map(len, source_lines)), 42)
        self.assertLessEqual(max(map(len, source_lines)), pipeline.SOURCE_WRAP_COLUMNS)
        rendered = output_dir.joinpath("bilingual.ass").read_text(encoding="utf-8")
        self.assertIn("Style: Bilingual,MiSans,46", rendered)
        self.assertIn("Style: BilingualBox,MiSans,46", rendered)
        self.assertIn(",4,8,0,2,80,80,50,1", rendered)
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
                    "translation": "日文字形宽度与拉丁文字不同",
                }
            ],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        rendered = output_dir.joinpath("bilingual.ass").read_text(encoding="utf-8")

        self.assertIn("Style: BilingualBox,MiSans,46", rendered)
        self.assertIn(",4,8,0,2,80,80,50,1", rendered)
        self.assertNotIn(r"{\an7\p1}", rendered)
        self.assertIn(
            r"BilingualBox,,0,0,0,,{\an2\pos(960,1030)\fs42}"
            + source
            + r"\N{\fs46}日文字形宽度与拉丁文字不同",
            rendered,
        )
        self.assertIn(
            r"Bilingual,,0,0,0,,{\an2\pos(960,1030)\fs42}"
            + source
            + r"\N{\fs46\1c&H00FFFF&}日文字形宽度与拉丁文字不同",
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

    def test_chinese_lines_use_the_full_shared_width_budget(self) -> None:
        manifest_path, manifest = self.prepare_fixture(
            srt([("00:00:00,000", "00:00:03,000", "A test line.")])
        )
        one_line = "这条二十五个汉字长度的中文字幕不应被折成两行显示"
        self.assertEqual(len(one_line), 24)
        translations_dir = self.write_translations(
            manifest,
            records=[{"id": manifest["segments"][0]["id"], "translation": one_line}],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        chinese_srt = output_dir.joinpath("zh-CN.srt").read_text(encoding="utf-8")
        chinese_lines = [line for line in chinese_srt.splitlines()[2:] if line]
        self.assertEqual(chinese_lines, [one_line])

    def test_japanese_target_keeps_punctuation_and_names_outputs(self) -> None:
        self.assertEqual(
            pipeline.normalize_target_caption("こんにちは、世界。", "ja"),
            "こんにちは、世界。",
        )
        source = self.root / "downloaded.srt"
        source.write_bytes(srt([("00:00:00,000", "00:00:02,000", "Hello world.")]))
        manifest_path = pipeline.prepare(
            source, self.root / "work", "en", "preserve", None, "ja"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["target_language"], "ja")
        batch = json.loads(
            Path(manifest["translation_batches"][0]["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(batch["target_language"], "ja")

        translations_dir = self.write_translations(
            manifest,
            records=[
                {"id": manifest["segments"][0]["id"], "translation": "こんにちは、世界。"}
            ],
        )
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        ja_srt = output_dir.joinpath("ja.srt").read_text(encoding="utf-8")
        self.assertIn("こんにちは、世界。", ja_srt)
        self.assertFalse(output_dir.joinpath("zh-CN.srt").exists())
        report = json.loads(output_dir.joinpath("validation.json").read_text())
        self.assertEqual(report["target_language"], "ja")
        pipeline.validate(manifest_path, translations_dir, output_dir)

    def test_rejects_invalid_target_language_tag(self) -> None:
        source = self.root / "downloaded.srt"
        source.write_bytes(srt([("00:00:00,000", "00:00:01,000", "Hi")]))
        with self.assertRaisesRegex(pipeline.PipelineError, "--target-language"):
            pipeline.prepare(source, self.root / "work", "en", "preserve", None, "bad lang!!")

    def test_sound_annotation_cues_are_excluded_from_translation(self) -> None:
        self.assertTrue(pipeline.is_non_dialogue_annotation("[Music]"))
        self.assertTrue(pipeline.is_non_dialogue_annotation("[Applause] [Laughter]"))
        self.assertTrue(pipeline.is_non_dialogue_annotation("【音乐】"))
        self.assertTrue(pipeline.is_non_dialogue_annotation("（拍手）"))
        self.assertTrue(pipeline.is_non_dialogue_annotation("♪♪"))
        self.assertTrue(pipeline.is_non_dialogue_annotation("♪ [upbeat music] ♪"))
        self.assertFalse(pipeline.is_non_dialogue_annotation("[Applause] Thank you"))
        self.assertFalse(pipeline.is_non_dialogue_annotation("Hello (world)"))
        self.assertFalse(pipeline.is_non_dialogue_annotation("「こんにちは」"))
        self.assertFalse(pipeline.is_non_dialogue_annotation("Plain dialogue."))

        raw = srt(
            [
                ("00:00:00,000", "00:00:01,000", "[Music]"),
                ("00:00:01,100", "00:00:02,000", "Real dialogue starts."),
                ("00:00:02,100", "00:00:03,000", "♪"),
                ("00:00:03,100", "00:00:04,000", "[Applause] Thanks everyone."),
            ]
        )
        manifest_path, manifest = self.prepare_fixture(raw, segment_mode="smart")
        texts = [cue["text"] for cue in manifest["cues"]]
        self.assertEqual(texts, ["Real dialogue starts.", "[Applause] Thanks everyone."])
        self.assertEqual(len(manifest["segments"]), 2)

        translations_dir = self.write_translations(manifest)
        output_dir = self.root / "output"
        pipeline.render(manifest_path, translations_dir, output_dir)
        rendered = output_dir.joinpath("source.srt").read_text(encoding="utf-8")
        self.assertNotIn("[Music]", rendered)
        self.assertNotIn("♪", rendered)
        pipeline.validate(manifest_path, translations_dir, output_dir)

    def test_annotation_only_subtitles_raise_no_dialogue(self) -> None:
        raw = srt(
            [
                ("00:00:00,000", "00:00:01,000", "[Music]"),
                ("00:00:01,100", "00:00:02,000", "【背景音乐】"),
            ]
        )
        source = self.root / "downloaded.srt"
        source.write_bytes(raw)
        with self.assertRaises(pipeline.NoDialogueError):
            pipeline.prepare(source, self.root / "work", "en")

    def test_smart_mode_closes_groups_exactly_at_sentence_boundaries(self) -> None:
        raw = srt(
            [
                ("00:00:00,000", "00:00:01,000", "This is the first"),
                ("00:00:01,100", "00:00:02,000", "half of a sentence."),
                ("00:00:02,100", "00:00:03,000", "Next sentence starts"),
                ("00:00:03,100", "00:00:04,000", "and keeps going"),
            ]
        )
        _, manifest = self.prepare_fixture(raw, segment_mode="smart")

        cues = manifest["cues"]
        groups = [segment["cue_ids"] for segment in manifest["render_segments"]]
        self.assertEqual(
            groups,
            [
                [cues[0]["id"], cues[1]["id"]],
                [cues[2]["id"], cues[3]["id"]],
            ],
        )

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
