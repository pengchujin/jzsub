#!/usr/bin/env python3
"""Deterministic, source-locked bilingual subtitle preparation and rendering.

The source SRT is archived byte-for-byte.  Source cue text is parsed into an
immutable ledger and every rendered segment references whole ledger cues.  A
translation file can provide only an id, the locked source hash, and Chinese
text; it cannot provide an editable copy of the source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from typing import Any, Iterable, Iterator, Sequence


SCHEMA_VERSION = 1
PIPELINE_VERSION = "1.1"
TRANSLATION_CONTRACT_VERSION = 3
ARCHIVE_NAME = "source.original.srt"
MANIFEST_NAME = "subtitle-manifest.json"
VALIDATION_NAME = "validation.json"
TRANSLATION_INPUT_DIR = "translation-input"
TRANSLATION_OUTPUT_DIR = "translation-output"
DEFAULT_FONT = "MiSans"
DEFAULT_FONT_WEIGHT = 700
TRANSLATION_ENGINE = "active_codex_default_gpt"
TRANSLATION_BATCH_SIZE = 80
TRANSLATION_CONTEXT_SEGMENTS = 2
ASS_WORD_JOINER = "\u2060"
# Both languages get the same pixel budget: 68 half-width columns at font 42
# and 62 columns (31 CJK characters) at font 46 are each about 1430 of the
# 1760 available PlayRes pixels on 16:9 video.
SOURCE_WRAP_COLUMNS = 68
CHINESE_WRAP_COLUMNS = 62
SOURCE_FONT_SIZE = 42
CHINESE_FONT_SIZE = 46
ASS_PLAY_RES_Y = 1080
ASS_MARGIN_X = 80
ASS_BOTTOM_MARGIN = 50
DEFAULT_VIDEO_SIZE = (1920, 1080)


class PipelineError(RuntimeError):
    """A user-facing validation or pipeline failure."""


class NoDialogueError(PipelineError):
    """The source subtitle contains only non-dialogue annotations."""


_TIME_RE = re.compile(
    r"^(?P<h>\d+):(?P<m>[0-5]\d):(?P<s>[0-5]\d)[,.](?P<ms>\d{1,3})$"
)
_TIMING_RE = re.compile(
    r"^\s*(?P<start>\d+:[0-5]\d:[0-5]\d[,.]\d{1,3})"
    r"\s*-->\s*"
    r"(?P<end>\d+:[0-5]\d:[0-5]\d[,.]\d{1,3})"
    r"(?P<settings>.*)$"
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(f"invalid JSON in {path}: {exc}") from exc


def _parse_timestamp(value: str) -> int:
    match = _TIME_RE.fullmatch(value)
    if not match:
        raise PipelineError(f"invalid SRT timestamp: {value!r}")
    milliseconds = int(match.group("ms").ljust(3, "0"))
    return (
        int(match.group("h")) * 3_600_000
        + int(match.group("m")) * 60_000
        + int(match.group("s")) * 1_000
        + milliseconds
    )


def parse_srt_bytes(raw: bytes) -> list[dict[str, Any]]:
    """Parse UTF-8 SRT while preserving every cue text code point.

    SRT record separators and CRLF/LF encoding are structural.  The original
    bytes are independently archived and SHA-256 locked.
    """

    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PipelineError("source SRT must be UTF-8 or UTF-8 with BOM") from exc

    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
    body = normalized.strip("\n")
    if not body:
        raise PipelineError("source SRT is empty")

    blocks = re.split(r"\n[ \t]*\n+", body)
    parsed: list[dict[str, Any]] = []
    previous_start = -1
    for position, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        timing_index: int | None = None
        timing_match: re.Match[str] | None = None
        for candidate in range(min(2, len(lines))):
            match = _TIMING_RE.fullmatch(lines[candidate])
            if match:
                timing_index = candidate
                timing_match = match
                break
        if timing_index is None or timing_match is None:
            raise PipelineError(f"SRT cue {position} has no valid timing line")

        original_index = lines[0] if timing_index == 1 else None
        text = "\n".join(lines[timing_index + 1 :])
        if text == "":
            raise PipelineError(f"SRT cue {position} has empty source text")
        start_ms = _parse_timestamp(timing_match.group("start"))
        end_ms = _parse_timestamp(timing_match.group("end"))
        if end_ms <= start_ms:
            raise PipelineError(f"SRT cue {position} has non-positive duration")
        if start_ms < previous_start:
            raise PipelineError("source SRT cue start times are not monotonic")
        previous_start = start_ms
        parsed.append(
            {
                "position": position,
                "original_index": original_index,
                "timing_line": lines[timing_index],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "settings": timing_match.group("settings"),
                "text": text,
            }
        )
    return parsed


# Corner brackets 「」『』 are Japanese quotation marks around real dialogue,
# so they are deliberately not treated as annotation brackets.
_ANNOTATION_BRACKETS = re.compile(r"[\[(【（〔][^\[\]()【】（）〔〕]*[\])】）〕]")
_MUSIC_NOTES = re.compile(r"[♪♫♬♩🎵🎶]+")


def is_non_dialogue_annotation(text: str) -> bool:
    """True when an entire cue is a sound description such as [Music].

    YouTube captions interleave cues like ``[Music]``, ``[Applause]``,
    ``【音乐】``, ``（拍手）`` or bare music notes with the dialogue. Whole
    annotation cues carry nothing to translate or display. Cues that mix an
    annotation with dialogue are kept untouched, because displayed source text
    must never be edited.
    """

    remainder = _MUSIC_NOTES.sub(" ", _ANNOTATION_BRACKETS.sub(" ", text))
    if remainder == text:
        return False
    return not any(character.isalnum() for character in remainder)


def _build_cue_ledger(parsed: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    for raw_cue in parsed:
        if is_non_dialogue_annotation(raw_cue["text"]):
            continue
        payload = {
            "position": raw_cue["position"],
            "original_index": raw_cue["original_index"],
            "timing_line": raw_cue["timing_line"],
            "start_ms": raw_cue["start_ms"],
            "end_ms": raw_cue["end_ms"],
            "settings": raw_cue["settings"],
            "text": raw_cue["text"],
        }
        cue_sha256 = _sha256_json(payload)
        cues.append(
            {
                **payload,
                "id": f"cue-{raw_cue['position']:06d}-{cue_sha256[:12]}",
                "text_sha256": _sha256_bytes(raw_cue["text"].encode("utf-8")),
                "cue_sha256": cue_sha256,
            }
        )
    return cues


def _cluster_spans(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield practical extended grapheme clusters using only the stdlib."""

    index = 0
    length = len(text)
    while index < length:
        start = index
        index += 1
        while index < length:
            codepoint = ord(text[index])
            if (
                unicodedata.combining(text[index])
                or 0xFE00 <= codepoint <= 0xFE0F
                or 0xE0100 <= codepoint <= 0xE01EF
                or 0x1F3FB <= codepoint <= 0x1F3FF
            ):
                index += 1
                continue
            if text[index] == "\u200d" and index + 1 < length:
                index += 2
                continue
            break
        yield start, index, text[start:index]


def _cluster_width(cluster: str) -> int:
    if cluster == "\t":
        return 4
    if cluster == "\n":
        return 0
    widths: list[int] = []
    for character in cluster:
        if character == "\u200d" or unicodedata.combining(character):
            continue
        if unicodedata.category(character) in {"Cf", "Mn", "Me"}:
            continue
        widths.append(2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1)
    return max(widths, default=0) if "\u200d" in cluster else sum(widths)


def _display_width(text: str) -> int:
    return sum(_cluster_width(cluster) for _, _, cluster in _cluster_spans(text))


def _wrap_single_line_exact(text: str, max_columns: int) -> list[str]:
    if not text:
        return [""]
    clusters = list(_cluster_spans(text))
    pieces: list[str] = []
    start_cluster = 0
    while start_cluster < len(clusters):
        width = 0
        index = start_cluster
        last_space_boundary: int | None = None
        while index < len(clusters):
            cluster = clusters[index][2]
            next_width = _cluster_width(cluster)
            if index > start_cluster and width + next_width > max_columns:
                break
            width += next_width
            index += 1
            if cluster.isspace():
                last_space_boundary = index
            if width > max_columns and index == start_cluster + 1:
                break
        if index >= len(clusters):
            pieces.append(text[clusters[start_cluster][0] :])
            break
        cut_cluster = (
            last_space_boundary
            if last_space_boundary is not None and last_space_boundary > start_cluster
            else index
        )
        if cut_cluster <= start_cluster:
            cut_cluster = start_cluster + 1
        cut_codepoint = clusters[cut_cluster - 1][1]
        pieces.append(text[clusters[start_cluster][0] : cut_codepoint])
        start_cluster = cut_cluster
    return pieces


def wrap_layout_chunks(text: str, max_columns: int) -> list[str]:
    """Return exact source chunks separated only by renderer-added line breaks.

    ``''.join(result)`` is guaranteed to equal ``text`` exactly.  Renderers use
    ``'\n'.join(result)``; therefore wrapping never replaces or removes a source
    character.
    """

    if max_columns < 1:
        raise ValueError("max_columns must be positive")
    chunks = [""]
    cursor = 0
    while cursor <= len(text):
        newline = text.find("\n", cursor)
        if newline < 0:
            content = text[cursor:]
            delimiter = ""
            at_end = True
        else:
            content = text[cursor:newline]
            delimiter = "\n"
            at_end = False
        pieces = _wrap_single_line_exact(content, max_columns)
        chunks[-1] += pieces[0]
        chunks.extend(pieces[1:])
        chunks[-1] += delimiter
        if at_end:
            break
        cursor = newline + 1
    if "".join(chunks) != text:
        raise AssertionError("layout wrapping changed source text")
    return chunks


def _render_wrapped(text: str, max_columns: int) -> str:
    return "\n".join(wrap_layout_chunks(text, max_columns))


def normalize_chinese_caption(text: str) -> str:
    """Apply the house style without changing the immutable source subtitle.

    Full-width Chinese commas and periods become a single space inside a cue
    and disappear at its edges. ASCII punctuation remains available for model
    numbers, URLs, code, and foreign names.
    """

    return re.sub(r"\s*[，。]+\s*", " ", text).strip()


_SENTENCE_END = re.compile(r"[.!?。！？…][\"'”’)）\]】》]*\s*$")


def _smart_group_indices(cues: Sequence[dict[str, Any]]) -> list[list[int]]:
    """Group whole adjacent cues into sentence-aligned display segments.

    A group always closes at end-of-sentence punctuation, so one caption never
    spans two sentences and a sentence is split only when it exceeds the
    duration or width budget. Cues are never split or rewritten.
    """

    groups: list[list[int]] = []
    current: list[int] = []
    for index, cue in enumerate(cues):
        if current:
            first = cues[current[0]]
            previous = cues[current[-1]]
            gap = cue["start_ms"] - previous["end_ms"]
            combined_span = cue["end_ms"] - first["start_ms"]
            combined_text = " ".join(
                cues[item]["text"].replace("\n", " ") for item in [*current, index]
            )
            joins_sentence = (
                not _SENTENCE_END.search(previous["text"])
                and -1_000 <= gap <= 750
                and combined_span <= 7_000
                and _display_width(combined_text) <= 84
            )
            if not joins_sentence:
                groups.append(current)
                current = []
        current.append(index)
    if current:
        groups.append(current)
    return groups


def _segment_payload(cues: Sequence[dict[str, Any]], cue_indices: Sequence[int]) -> dict[str, Any]:
    selected = [cues[index] for index in cue_indices]
    return {
        "cue_ids": [cue["id"] for cue in selected],
        "cue_sha256": [cue["cue_sha256"] for cue in selected],
        "start_ms": selected[0]["start_ms"],
        "end_ms": max(cue["end_ms"] for cue in selected),
    }


def _build_segments(
    cues: Sequence[dict[str, Any]], segment_mode: str
) -> list[dict[str, Any]]:
    if segment_mode == "preserve":
        groups = [[index] for index in range(len(cues))]
    elif segment_mode == "smart":
        groups = _smart_group_indices(cues)
    else:
        raise PipelineError(f"unsupported segment mode: {segment_mode}")

    segments: list[dict[str, Any]] = []
    for position, group in enumerate(groups, start=1):
        payload = _segment_payload(cues, group)
        source_sha256 = _sha256_json(payload)
        segments.append(
            {
                "position": position,
                "id": f"seg-{position:06d}-{source_sha256[:12]}",
                "start_ms": payload["start_ms"],
                "end_ms": payload["end_ms"],
                "cue_ids": payload["cue_ids"],
                "source_sha256": source_sha256,
            }
        )
    if segment_mode == "smart":
        for current, following in zip(segments, segments[1:]):
            if current["end_ms"] > following["start_ms"]:
                current["end_ms"] = max(
                    current["start_ms"] + 10, following["start_ms"]
                )
    return segments


def _cue_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {cue["id"]: cue for cue in manifest["cues"]}


def _segment_source_text(
    segment: dict[str, Any], cue_by_id: dict[str, dict[str, Any]]
) -> str:
    return "\n".join(cue_by_id[cue_id]["text"] for cue_id in segment["cue_ids"])


def _translation_item(
    segment: dict[str, Any], cue_by_id: dict[str, dict[str, Any]]
) -> dict[str, str]:
    return {
        "id": segment["id"],
        "source_sha256": segment["source_sha256"],
        "source": _segment_source_text(segment, cue_by_id),
    }


def _compact_translation_item(
    segment: dict[str, Any], cue_by_id: dict[str, dict[str, Any]]
) -> dict[str, str]:
    return {
        "id": segment["id"],
        "source": _segment_source_text(segment, cue_by_id),
    }


def _write_translation_batches(
    work_dir: Path,
    source_language: str,
    cues: Sequence[dict[str, Any]],
    segments: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    input_dir = work_dir / TRANSLATION_INPUT_DIR
    input_dir.mkdir(parents=True, exist_ok=True)
    for stale in input_dir.glob("batch-*.json"):
        stale.unlink()

    cue_by_id = {cue["id"]: cue for cue in cues}
    batches: list[dict[str, Any]] = []
    # Bounded ordered batches keep one model pass small enough to answer
    # completely; read-only neighbor context preserves terminology, pronouns,
    # and sentence flow across batch edges.
    for batch_number, start in enumerate(
        range(0, len(segments), TRANSLATION_BATCH_SIZE), start=1
    ):
        selected = list(segments[start : start + TRANSLATION_BATCH_SIZE])
        end = start + len(selected)
        before = list(segments[max(0, start - TRANSLATION_CONTEXT_SEGMENTS) : start])
        after = list(segments[end : end + TRANSLATION_CONTEXT_SEGMENTS])
        payload = {
            "translation_contract_version": TRANSLATION_CONTRACT_VERSION,
            "source_language": source_language,
            "target_language": "zh-CN",
            "context": {
                "before": [_compact_translation_item(item, cue_by_id) for item in before],
                "after": [_compact_translation_item(item, cue_by_id) for item in after],
            },
            "items": [_compact_translation_item(item, cue_by_id) for item in selected],
            "output_fields": ["id", "zh_cn"],
        }
        path = (input_dir / f"batch-{batch_number:04d}.json").resolve()
        encoded = _canonical_json_bytes(payload) + b"\n"
        _atomic_write(path, encoded)
        batches.append(
            {
                "path": str(path),
                "sha256": _sha256_bytes(encoded),
                "segment_ids": [segment["id"] for segment in selected],
            }
        )
    return batches


def _validated_video_size(video_size: tuple[int, int] | None) -> tuple[int, int]:
    if video_size is None:
        return DEFAULT_VIDEO_SIZE
    try:
        width, height = int(video_size[0]), int(video_size[1])
    except (TypeError, ValueError, IndexError) as exc:
        raise PipelineError("video size must be two positive integers") from exc
    if width <= 0 or height <= 0:
        raise PipelineError("video size must be two positive integers")
    return width, height


def prepare(
    source_srt: Path,
    work_dir: Path,
    source_language: str,
    segment_mode: str = "preserve",
    video_size: tuple[int, int] | None = None,
) -> Path:
    source_srt = source_srt.expanduser().resolve()
    work_dir = work_dir.expanduser().resolve()
    width, height = _validated_video_size(video_size)
    if not source_language.strip():
        raise PipelineError("--source-language cannot be empty")
    try:
        raw = source_srt.read_bytes()
    except FileNotFoundError as exc:
        raise PipelineError(f"source SRT not found: {source_srt}") from exc

    parsed = parse_srt_bytes(raw)
    cues = _build_cue_ledger(parsed)
    if not cues:
        raise NoDialogueError(
            "source SRT contains only non-dialogue annotations such as [Music]; "
            "there is nothing to translate"
        )
    # Translation units retain every original cue. Display segmentation is
    # derived separately and is applied only after the complete translation.
    segments = _build_segments(cues, "preserve")
    render_segments = _build_segments(cues, segment_mode)
    work_dir.mkdir(parents=True, exist_ok=True)
    archive = (work_dir / ARCHIVE_NAME).resolve()
    if archive.exists():
        if archive.read_bytes() != raw:
            raise PipelineError(
                "write-once source archive already exists with different bytes"
            )
    elif archive != source_srt:
        _atomic_write(archive, raw)
    else:
        raise PipelineError("source archive unexpectedly disappeared while preparing")
    if archive.read_bytes() != raw:
        raise PipelineError("source archive is not byte-for-byte identical")

    batches = _write_translation_batches(
        work_dir, source_language.strip(), cues, segments
    )
    translation_output_dir = (work_dir / TRANSLATION_OUTPUT_DIR).resolve()
    translation_output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "translation_contract_version": TRANSLATION_CONTRACT_VERSION,
        "source_language": source_language.strip(),
        "target_language": "zh-CN",
        "segment_mode": segment_mode,
        "video_size": {"width": width, "height": height},
        "source": {
            "original_path": str(source_srt),
            "archive_path": str(archive),
            "sha256": _sha256_bytes(raw),
            "size_bytes": len(raw),
            "encoding": "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8",
        },
        "cues": cues,
        "source_ledger_sha256": _sha256_json(cues),
        "segments": segments,
        "segment_ledger_sha256": _sha256_json(segments),
        "render_segments": render_segments,
        "render_segment_ledger_sha256": _sha256_json(render_segments),
        "translation_batches": batches,
        "translation_output_dir": str(translation_output_dir),
    }
    manifest_path = (work_dir / MANIFEST_NAME).resolve()
    _atomic_write(manifest_path, _json_bytes(manifest))
    validate_manifest(manifest_path)
    return manifest_path


def validate_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise PipelineError("subtitle manifest root must be an object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise PipelineError("unsupported subtitle manifest schema version")
    if manifest.get("pipeline_version") != PIPELINE_VERSION:
        raise PipelineError("unsupported subtitle pipeline version")

    source = manifest.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("archive_path"), str):
        raise PipelineError("manifest source archive is missing")
    archive = Path(source["archive_path"]).expanduser()
    try:
        raw = archive.read_bytes()
    except FileNotFoundError as exc:
        raise PipelineError(f"locked source archive not found: {archive}") from exc
    if _sha256_bytes(raw) != source.get("sha256"):
        raise PipelineError("locked source archive SHA-256 mismatch")
    if len(raw) != source.get("size_bytes"):
        raise PipelineError("locked source archive size mismatch")

    expected_cues = _build_cue_ledger(parse_srt_bytes(raw))
    if manifest.get("cues") != expected_cues:
        raise PipelineError("manifest cue ledger differs from locked source SRT")
    if manifest.get("source_ledger_sha256") != _sha256_json(expected_cues):
        raise PipelineError("source cue ledger SHA-256 mismatch")

    segment_mode = manifest.get("segment_mode")
    contract_version = manifest.get("translation_contract_version", 1)
    expected_segments = _build_segments(
        expected_cues, "preserve" if contract_version >= 3 else segment_mode
    )
    if manifest.get("segments") != expected_segments:
        raise PipelineError("segment ledger/provenance differs from locked source cues")
    if manifest.get("segment_ledger_sha256") != _sha256_json(expected_segments):
        raise PipelineError("segment ledger SHA-256 mismatch")

    if contract_version >= 3:
        expected_render_segments = _build_segments(expected_cues, segment_mode)
        if manifest.get("render_segments") != expected_render_segments:
            raise PipelineError("render segment ledger differs from locked source cues")
        if manifest.get("render_segment_ledger_sha256") != _sha256_json(expected_render_segments):
            raise PipelineError("render segment ledger SHA-256 mismatch")

    covered = [cue_id for segment in expected_segments for cue_id in segment["cue_ids"]]
    expected_order = [cue["id"] for cue in expected_cues]
    if covered != expected_order:
        raise PipelineError("segments do not cover source cues exactly once and in order")

    if contract_version not in {1, 2, TRANSLATION_CONTRACT_VERSION}:
        raise PipelineError("unsupported translation contract version")
    batches = manifest.get("translation_batches")
    if not isinstance(batches, list) or not batches:
        raise PipelineError("manifest translation batches are missing")
    cue_by_id = {cue["id"]: cue for cue in expected_cues}
    segment_by_id = {segment["id"]: segment for segment in expected_segments}
    segment_position = {
        segment["id"]: index for index, segment in enumerate(expected_segments)
    }
    batched_ids: list[str] = []
    for batch in batches:
        if not isinstance(batch, dict) or not isinstance(batch.get("path"), str):
            raise PipelineError("invalid translation batch record")
        path = Path(batch["path"])
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise PipelineError(f"translation input batch not found: {path}") from exc
        if _sha256_bytes(data) != batch.get("sha256"):
            raise PipelineError(f"translation input batch SHA-256 mismatch: {path}")
        payload = _read_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise PipelineError(f"invalid translation input batch: {path}")
        if payload.get("source_language") != manifest.get("source_language") or payload.get("target_language") != "zh-CN":
            raise PipelineError(f"translation input contract mismatch: {path}")
        if contract_version == 1:
            expected_contract = {
                "engine": TRANSLATION_ENGINE,
                "external_translation_service_allowed": False,
                "local_inference_allowed": False,
            }
            if payload.get("task") != "translate_subtitles_to_simplified_chinese" or payload.get("execution_contract") != expected_contract:
                raise PipelineError(f"translation input contract mismatch: {path}")
        elif payload.get("translation_contract_version") != contract_version or payload.get("output_fields") != ["id", "zh_cn"]:
            raise PipelineError(f"translation input contract mismatch: {path}")
        item_ids = [item.get("id") for item in payload["items"] if isinstance(item, dict)]
        if item_ids != batch.get("segment_ids"):
            raise PipelineError(f"translation batch segment IDs mismatch: {path}")
        if any(segment_id not in segment_by_id for segment_id in item_ids):
            raise PipelineError(f"translation batch has an unknown segment ID: {path}")
        item_builder = _translation_item if contract_version == 1 else _compact_translation_item
        expected_items = [item_builder(segment_by_id[segment_id], cue_by_id) for segment_id in item_ids]
        if payload["items"] != expected_items:
            raise PipelineError(f"translation batch source text/hash was altered: {path}")
        positions = [segment_position[segment_id] for segment_id in item_ids]
        if positions != list(range(positions[0], positions[0] + len(positions))):
            raise PipelineError(f"translation batch segment order is not contiguous: {path}")
        context = payload.get("read_only_context" if contract_version == 1 else "context")
        if not isinstance(context, dict) or set(context) != {"before", "after"}:
            raise PipelineError(f"translation read-only context is invalid: {path}")
        first_position = positions[0]
        after_position = positions[-1] + 1
        expected_before = [
            item_builder(segment, cue_by_id)
            for segment in expected_segments[
                max(0, first_position - TRANSLATION_CONTEXT_SEGMENTS) : first_position
            ]
        ]
        expected_after = [
            item_builder(segment, cue_by_id)
            for segment in expected_segments[
                after_position : after_position + TRANSLATION_CONTEXT_SEGMENTS
            ]
        ]
        if context["before"] != expected_before or context["after"] != expected_after:
            raise PipelineError(f"translation read-only context was altered: {path}")
        batched_ids.extend(item_ids)
    if batched_ids != [segment["id"] for segment in expected_segments]:
        raise PipelineError("translation batches do not cover segments exactly once")
    return manifest


def _translation_records_from_root(root: Any, path: Path) -> list[dict[str, Any]]:
    if isinstance(root, list):
        records = root
    elif isinstance(root, dict) and set(root) == {"translations"}:
        records = root["translations"]
    elif isinstance(root, dict) and set(root) in (
        {"id", "zh_cn"},
        {"id", "source_sha256", "zh_cn"},
    ):
        records = [root]
    else:
        raise PipelineError(
            f"{path} must be a translation list, a translations object, or one strict item"
        )
    if not isinstance(records, list):
        raise PipelineError(f"translations in {path} must be an array")
    output: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise PipelineError(f"translation {index} in {path} must be an object")
        allowed_sets = ({"id", "zh_cn"}, {"id", "source_sha256", "zh_cn"})
        if set(record) not in allowed_sets:
            allowed = {"id", "zh_cn"}
            extra = sorted(set(record) - {"id", "source_sha256", "zh_cn"})
            missing = sorted(allowed - set(record))
            raise PipelineError(
                f"translation {index} in {path} has forbidden/missing fields "
                f"(extra={extra}, missing={missing})"
            )
        if not all(isinstance(value, str) for value in record.values()):
            raise PipelineError(f"translation {index} in {path} fields must be strings")
        zh_cn = record["zh_cn"]
        if not zh_cn.strip():
            raise PipelineError(f"translation {record['id']} in {path} is empty")
        if any(unicodedata.category(character) == "Cc" for character in zh_cn):
            raise PipelineError(
                f"translation {record['id']} in {path} contains a control character"
            )
        output.append(record)
    return output


def load_translations(
    manifest: dict[str, Any], translations_dir: Path
) -> dict[str, str]:
    translations_dir = translations_dir.expanduser().resolve()
    if not translations_dir.is_dir():
        raise PipelineError(f"translations directory not found: {translations_dir}")
    files = sorted(translations_dir.glob("*.json"))
    if not files:
        raise PipelineError(f"no translation JSON files found in {translations_dir}")

    expected = {segment["id"]: segment for segment in manifest["segments"]}
    collected: dict[str, str] = {}
    for path in files:
        for record in _translation_records_from_root(_read_json(path), path):
            segment_id = record["id"]
            if segment_id in collected:
                raise PipelineError(f"duplicate translation ID: {segment_id}")
            if segment_id not in expected:
                raise PipelineError(f"extra translation ID: {segment_id}")
            if "source_sha256" in record and record["source_sha256"] != expected[segment_id]["source_sha256"]:
                raise PipelineError(f"source SHA-256 mismatch for translation {segment_id}")
            collected[segment_id] = record["zh_cn"]

    missing = [segment_id for segment_id in expected if segment_id not in collected]
    if missing:
        raise PipelineError(f"missing translations: {', '.join(missing)}")
    return collected


def next_translation_batch(manifest_path: Path) -> dict[str, Any]:
    """Return one pending compact batch without exposing the full manifest."""

    manifest = validate_manifest(manifest_path)
    output_dir = Path(manifest["translation_output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    batches = manifest["translation_batches"]
    pending: list[tuple[dict[str, Any], Path]] = []
    for batch in batches:
        input_path = Path(batch["path"])
        output_path = output_dir / input_path.name
        if not output_path.exists():
            pending.append((batch, output_path))
            continue
        records = _translation_records_from_root(_read_json(output_path), output_path)
        if [record["id"] for record in records] != batch["segment_ids"]:
            raise PipelineError(f"translation output IDs mismatch: {output_path}")
    if not pending:
        return {"done": True, "remaining": 0, "translations_dir": str(output_dir)}
    batch, output_path = pending[0]
    input_path = Path(batch["path"])
    return {
        "done": False,
        "remaining": len(pending),
        "input_path": str(input_path),
        "output_path": str(output_path),
        "batch": _read_json(input_path),
    }


def _srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _ass_timestamp(milliseconds: int, *, end: bool = False) -> str:
    centiseconds = (milliseconds + 9) // 10 if end else milliseconds // 10
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def ass_escape(text: str) -> str:
    """Losslessly encode untrusted visible text for an ASS Dialogue field."""

    # Mirror FFmpeg's official ff_ass_bprint_text_event strategy: guard every
    # literal backslash with U+2060 WORD JOINER so sequences such as \N, \n and
    # \h cannot become ASS commands; encode an opening brace as ``\{{}`` so it
    # cannot begin an override block; and reserve ``\N`` for layout newlines.
    output: list[str] = []
    for character in text:
        if character == "\\":
            output.append("\\" + ASS_WORD_JOINER)
        elif character == "{":
            output.append(r"\{{}")
        elif character == "\n":
            output.append(r"\N")
        else:
            output.append(character)
    return "".join(output)


def ass_unescape_for_validation(encoded: str) -> str:
    """Strict inverse of :func:`ass_escape` for source-integrity validation."""

    output: list[str] = []
    index = 0
    while index < len(encoded):
        if encoded.startswith("\\" + ASS_WORD_JOINER, index):
            output.append("\\")
            index += 2
            continue
        if encoded.startswith(r"\{{}", index):
            output.append("{")
            index += 4
            continue
        if encoded.startswith(r"\N", index):
            output.append("\n")
            index += 2
            continue
        if encoded[index] == "\\":
            raise PipelineError("ASS text contains an unguarded backslash")
        output.append(encoded[index])
        index += 1
    return "".join(output)


def _render_srt(entries: Iterable[tuple[int, int, str]]) -> str:
    blocks: list[str] = []
    for index, (start_ms, end_ms, text) in enumerate(entries, start=1):
        blocks.append(
            f"{index}\n{_srt_timestamp(start_ms)} --> {_srt_timestamp(end_ms)}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _validate_font(font: str) -> str:
    if not font.strip():
        raise PipelineError("font name cannot be empty")
    if any(character in font for character in "\r\n,"):
        raise PipelineError("font name contains an unsafe ASS header character")
    return font.strip()


def _render_segments(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return manifest.get("render_segments", manifest["segments"])


def _display_translation(
    manifest: dict[str, Any], segment: dict[str, Any], translations: dict[str, str]
) -> str:
    if "render_segments" not in manifest:
        return translations[segment["id"]]
    translation_by_cue: dict[str, str] = {}
    for unit in manifest["segments"]:
        if len(unit["cue_ids"]) != 1:
            raise PipelineError("post-translation segmentation requires one cue per unit")
        translation_by_cue[unit["cue_ids"][0]] = translations[unit["id"]]
    return " ".join(translation_by_cue[cue_id].strip() for cue_id in segment["cue_ids"])


def _manifest_video_size(manifest: dict[str, Any]) -> tuple[int, int]:
    size = manifest.get("video_size")
    if size is None:
        return DEFAULT_VIDEO_SIZE
    if not isinstance(size, dict):
        raise PipelineError("manifest video_size must be an object")
    return _validated_video_size((size.get("width"), size.get("height")))


def _ass_layout(manifest: dict[str, Any]) -> dict[str, int]:
    """Derive PlayRes and wrap widths from the video aspect ratio.

    PlayResY is fixed and PlayResX follows the display aspect, so libass scales
    fonts and positions isotropically on portrait and landscape video alike.
    Wrap widths shrink with the available horizontal space and are capped at
    the 16:9 house-style limits.
    """

    width, height = _manifest_video_size(manifest)
    play_res_y = ASS_PLAY_RES_Y
    play_res_x = max(320, round(play_res_y * width / height))
    available = max(160, play_res_x - 2 * ASS_MARGIN_X)
    return {
        "play_res_x": play_res_x,
        "play_res_y": play_res_y,
        "source_columns": max(12, min(SOURCE_WRAP_COLUMNS, 2 * available // SOURCE_FONT_SIZE)),
        "chinese_columns": max(8, min(CHINESE_WRAP_COLUMNS, 2 * available // CHINESE_FONT_SIZE)),
        "position_x": play_res_x // 2,
        "position_y": play_res_y - ASS_BOTTOM_MARGIN,
    }


def _render_ass(
    manifest: dict[str, Any], translations: dict[str, str], font: str
) -> str:
    font = _validate_font(font)
    cue_by_id = _cue_map(manifest)
    layout = _ass_layout(manifest)
    header = f"""[Script Info]
; Generated by subtitle_pipeline.py from a SHA-256 locked source ledger.
; Typeface: MiSans Bold by Xiaomi. https://hyperos.mi.com/font/zh/download/
ScriptType: v4.00+
PlayResX: {layout['play_res_x']}
PlayResY: {layout['play_res_y']}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Bilingual,{font},{CHINESE_FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,2,1,2,{ASS_MARGIN_X},{ASS_MARGIN_X},{ASS_BOTTOM_MARGIN},1
Style: BilingualBox,{font},{CHINESE_FONT_SIZE},&HFF000000,&HFF000000,&H78000000,&H78000000,-1,0,0,0,100,100,0,0,4,8,0,2,{ASS_MARGIN_X},{ASS_MARGIN_X},{ASS_BOTTOM_MARGIN},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    # One bottom-anchored event stacks source directly above Chinese, so the
    # pair hugs the bottom margin and the two languages can never overlap.
    # The box layer uses libass BorderStyle=4: one translucent panel behind
    # the whole event. Per-line BorderStyle=3 boxes would overlap between
    # adjacent lines and double-paint the translucent color into dark bands.
    anchor = (
        rf"{{\an2\pos({layout['position_x']},{layout['position_y']})"
        rf"\fs{SOURCE_FONT_SIZE}}}"
    )
    dialogue: list[str] = []
    for segment in _render_segments(manifest):
        source_exact = _segment_source_text(segment, cue_by_id)
        chinese_exact = normalize_chinese_caption(
            _display_translation(manifest, segment, translations)
        )
        source = "\n".join(wrap_layout_chunks(source_exact, layout["source_columns"]))
        chinese = "\n".join(wrap_layout_chunks(chinese_exact, layout["chinese_columns"]))
        escaped_source = ass_escape(source)
        escaped_chinese = ass_escape(chinese)
        if ass_unescape_for_validation(escaped_source) != source:
            raise PipelineError(f"ASS source escape round-trip failed for {segment['id']}")
        if ass_unescape_for_validation(escaped_chinese) != chinese:
            raise PipelineError(f"ASS translation escape round-trip failed for {segment['id']}")
        start = _ass_timestamp(segment["start_ms"])
        end_ms = max(segment["end_ms"], segment["start_ms"] + 10)
        end = _ass_timestamp(end_ms, end=True)
        box_text = (
            f"{anchor}{escaped_source}"
            rf"\N{{\fs{CHINESE_FONT_SIZE}}}{escaped_chinese}"
        )
        text = (
            f"{anchor}{escaped_source}"
            rf"\N{{\fs{CHINESE_FONT_SIZE}\1c&H00FFFF&}}{escaped_chinese}"
        )
        dialogue.append(f"Dialogue: 0,{start},{end},BilingualBox,,0,0,0,,{box_text}")
        dialogue.append(f"Dialogue: 1,{start},{end},Bilingual,,0,0,0,,{text}")
    return header + "\n".join(dialogue) + "\n"


def _expected_outputs(
    manifest: dict[str, Any], translations: dict[str, str], font: str
) -> dict[str, bytes]:
    cue_by_id = _cue_map(manifest)
    layout = _ass_layout(manifest)
    source_entries: list[tuple[int, int, str]] = []
    chinese_entries: list[tuple[int, int, str]] = []
    bilingual_entries: list[tuple[int, int, str]] = []
    for segment in _render_segments(manifest):
        source_exact = _segment_source_text(segment, cue_by_id)
        source_chunks = wrap_layout_chunks(source_exact, layout["source_columns"])
        if "".join(source_chunks) != source_exact:
            raise PipelineError(f"source wrapping changed {segment['id']}")
        source_layout = "\n".join(source_chunks)
        chinese_exact = normalize_chinese_caption(
            _display_translation(manifest, segment, translations)
        )
        chinese_chunks = wrap_layout_chunks(chinese_exact, layout["chinese_columns"])
        if "".join(chinese_chunks) != chinese_exact:
            raise PipelineError(f"Chinese wrapping changed {segment['id']}")
        chinese_layout = "\n".join(chinese_chunks)
        timing = (segment["start_ms"], segment["end_ms"])
        source_entries.append((*timing, source_layout))
        chinese_entries.append((*timing, chinese_layout))
        bilingual_entries.append((*timing, f"{source_layout}\n{chinese_layout}"))
    return {
        "source.srt": _render_srt(source_entries).encode("utf-8"),
        "zh-CN.srt": _render_srt(chinese_entries).encode("utf-8"),
        "bilingual.srt": _render_srt(bilingual_entries).encode("utf-8"),
        "bilingual.ass": _render_ass(manifest, translations, font).encode("utf-8"),
    }


def _check_outputs(
    output_dir: Path,
    expected: dict[str, bytes],
    manifest_path: Path,
) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for name, expected_bytes in expected.items():
        path = output_dir / name
        try:
            actual = path.read_bytes()
        except FileNotFoundError as exc:
            raise PipelineError(f"rendered subtitle is missing: {path}") from exc
        if actual != expected_bytes:
            raise PipelineError(f"rendered subtitle differs from locked expectation: {path}")
        checksums[name] = _sha256_bytes(actual)

    rendered_manifest = output_dir / MANIFEST_NAME
    try:
        copied = rendered_manifest.read_bytes()
    except FileNotFoundError as exc:
        raise PipelineError(f"rendered manifest is missing: {rendered_manifest}") from exc
    original = manifest_path.read_bytes()
    if copied != original:
        raise PipelineError("rendered manifest differs from locked input manifest")
    checksums[MANIFEST_NAME] = _sha256_bytes(copied)
    return checksums


def _validation_report(
    manifest: dict[str, Any], checksums: dict[str, str], font: str
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "structurally_valid": True,
        "validation_scope": "structural_source_integrity",
        "translation_quality_reviewed": False,
        "source_sha256": manifest["source"]["sha256"],
        "source_ledger_sha256": manifest["source_ledger_sha256"],
        "segment_ledger_sha256": manifest["segment_ledger_sha256"],
        "segment_count": len(manifest["segments"]),
        "translation_count": len(manifest["segments"]),
        "font": _validate_font(font),
        "font_weight": DEFAULT_FONT_WEIGHT,
        "outputs": checksums,
        "invariants": {
            "raw_source_sha256_locked": True,
            "source_cues_exact_and_ordered": True,
            "segment_provenance_exact_and_ordered": True,
            "translations_complete_and_hash_matched": True,
            "ass_escape_round_trip": True,
            "rendered_outputs_match_expectation": True,
        },
    }


def render(
    manifest_path: Path,
    translations_dir: Path,
    output_dir: Path,
    font: str = DEFAULT_FONT,
) -> Path:
    manifest_path = manifest_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    manifest = validate_manifest(manifest_path)
    translations = load_translations(manifest, translations_dir)
    expected = _expected_outputs(manifest, translations, font)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, data in expected.items():
        _atomic_write(output_dir / name, data)
    destination_manifest = output_dir / MANIFEST_NAME
    if destination_manifest.resolve() != manifest_path:
        _atomic_write(destination_manifest, manifest_path.read_bytes())
    checksums = _check_outputs(output_dir, expected, manifest_path)
    report = _validation_report(manifest, checksums, font)
    report_path = output_dir / VALIDATION_NAME
    _atomic_write(report_path, _json_bytes(report))
    return report_path


def validate(
    manifest_path: Path,
    translations_dir: Path,
    output_dir: Path,
    font: str = DEFAULT_FONT,
) -> Path:
    manifest_path = manifest_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    manifest = validate_manifest(manifest_path)
    translations = load_translations(manifest, translations_dir)
    expected = _expected_outputs(manifest, translations, font)
    checksums = _check_outputs(output_dir, expected, manifest_path)
    report = _validation_report(manifest, checksums, font)
    report_path = output_dir / VALIDATION_NAME
    _atomic_write(report_path, _json_bytes(report))
    return report_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare, render, and validate source-locked bilingual subtitles."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare_parser = commands.add_parser("prepare", help="lock and segment a source SRT")
    prepare_parser.add_argument("source_srt", type=Path)
    prepare_parser.add_argument("--work-dir", type=Path, required=True)
    prepare_parser.add_argument("--source-language", required=True)
    prepare_parser.add_argument(
        "--segment-mode", choices=("preserve", "smart"), default="preserve"
    )
    prepare_parser.add_argument(
        "--video-size",
        metavar="WIDTHxHEIGHT",
        help="video display size used for caption layout (default: 1920x1080)",
    )

    next_parser = commands.add_parser(
        "next-batch", help="print only the next pending compact translation batch"
    )
    next_parser.add_argument("--manifest", type=Path, required=True)

    for name, help_text in (
        ("render", "render bilingual subtitle artifacts"),
        ("validate", "validate existing bilingual subtitle artifacts"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--translations-dir", type=Path, required=True)
        command.add_argument("--output-dir", type=Path, required=True)
        command.add_argument(
            "--font",
            default=DEFAULT_FONT,
            help="ASS font family (default: MiSans; subtitle styles use weight 700/Bold)",
        )
    return parser


def _parse_video_size(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", value)
    if not match:
        raise PipelineError("--video-size must look like 1920x1080")
    return int(match.group(1)), int(match.group(2))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(
                args.source_srt,
                args.work_dir,
                args.source_language,
                args.segment_mode,
                _parse_video_size(args.video_size),
            )
            payload = {"ok": True, "manifest": str(result)}
        elif args.command == "next-batch":
            payload = {"ok": True, **next_translation_batch(args.manifest)}
        elif args.command == "render":
            result = render(
                args.manifest, args.translations_dir, args.output_dir, args.font
            )
            payload = {"ok": True, "validation": str(result)}
        else:
            result = validate(
                args.manifest, args.translations_dir, args.output_dir, args.font
            )
            payload = {"ok": True, "validation": str(result)}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (PipelineError, OSError, UnicodeError) as exc:
        print(f"subtitle pipeline error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
