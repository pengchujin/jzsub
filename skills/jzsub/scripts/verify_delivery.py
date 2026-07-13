#!/usr/bin/env python3
"""Fail closed until a downloaded video job reaches its required deliverable."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence


class DeliveryError(RuntimeError):
    """A malformed or unreadable delivery job."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeliveryError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DeliveryError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DeliveryError(f"manifest root must be an object: {path}")
    return value


def _artifact_path(job_dir: Path, value: Any) -> Path | None:
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        return None
    path = Path(value["path"])
    return path if path.is_absolute() else job_dir / path


def _existing_video_artifact(job_dir: Path, artifacts: dict[str, Any]) -> Path | None:
    records = [artifacts.get("lossless_mp4_master"), artifacts.get("intermediate")]
    fallback = artifacts.get("lossy_mp4_fallback")
    if isinstance(fallback, dict):
        records.append(fallback.get("created"))
    for record in records:
        path = _artifact_path(job_dir, record)
        if path is not None and path.is_file() and path.stat().st_size:
            return path
    return None


DELIVERABLES = ("full", "video", "subs", "bilingual-subs")


def assess_delivery(download_manifest: Path) -> dict[str, Any]:
    download_manifest = download_manifest.expanduser().resolve()
    download = _read_json(download_manifest)
    configured_dir = download.get("output_directory")
    job_dir = (
        Path(configured_dir).expanduser().resolve()
        if isinstance(configured_dir, str)
        else download_manifest.parent
    )
    deliverable = download.get("deliverable")
    if deliverable not in DELIVERABLES:
        deliverable = "full"
    artifacts = download.get("artifacts")
    if not isinstance(artifacts, dict):
        raise DeliveryError("download manifest has no artifacts object")
    if deliverable in ("full", "video") and _existing_video_artifact(job_dir, artifacts) is None:
        raise DeliveryError("no declared video artifact exists on disk")

    def complete(stage: str, **extra: Any) -> dict[str, Any]:
        return {
            "complete": True,
            "stage": stage,
            "deliverable": deliverable,
            "job_dir": str(job_dir),
            "missing": [],
            **extra,
        }

    subtitle_record = artifacts.get("subtitle")
    subtitle = (
        _artifact_path(job_dir, subtitle_record.get("source_srt"))
        if isinstance(subtitle_record, dict)
        else None
    )
    if subtitle is not None and not subtitle.is_file():
        raise DeliveryError(f"declared source subtitle is missing: {subtitle}")
    if deliverable in ("subs", "bilingual-subs") and subtitle is None:
        raise DeliveryError(
            "a subtitle delivery was requested, but the manifest declares no source subtitle"
        )
    has_dialogue = (
        subtitle is not None
        and isinstance(subtitle_record, dict)
        and subtitle_record.get("dialogue") is not False
    )

    if deliverable == "video":
        return complete("video_complete")
    if deliverable == "subs":
        return complete("subs_complete")
    if not has_dialogue:
        # full falls back to plain video; bilingual-subs still delivered the
        # source subtitle files even though nothing was translatable.
        stage = "video_only_complete" if deliverable == "full" else "subs_complete"
        return complete(stage)

    subtitle_dir = job_dir / "subtitles"
    subtitle_manifest_path = subtitle_dir / "subtitle-manifest.json"
    if not subtitle_manifest_path.is_file():
        return {
            "complete": False,
            "stage": "subtitle_prepare_required",
            "job_dir": str(job_dir),
            "missing": [str(subtitle_manifest_path)],
        }

    subtitle_manifest = _read_json(subtitle_manifest_path)
    batches = subtitle_manifest.get("translation_batches")
    if not isinstance(batches, list) or not batches:
        raise DeliveryError("subtitle manifest has no translation batches")
    output_dir_value = subtitle_manifest.get("translation_output_dir")
    translation_output_dir = (
        Path(output_dir_value)
        if isinstance(output_dir_value, str)
        else subtitle_dir / "translation-output"
    )
    missing_batches: list[str] = []
    for batch in batches:
        if not isinstance(batch, dict) or not isinstance(batch.get("path"), str):
            raise DeliveryError("subtitle manifest has an invalid translation batch")
        name = Path(batch["path"]).name
        if not (translation_output_dir / name).is_file():
            missing_batches.append(name)
    if missing_batches:
        return {
            "complete": False,
            "stage": "translation_required",
            "job_dir": str(job_dir),
            "missing": missing_batches,
        }

    rendered_dir = subtitle_dir / "rendered"
    required_rendered = [rendered_dir / "bilingual.ass", rendered_dir / "validation.json"]
    missing_rendered = [str(path) for path in required_rendered if not path.is_file()]
    if missing_rendered:
        return {
            "complete": False,
            "stage": "render_required",
            "job_dir": str(job_dir),
            "missing": missing_rendered,
        }
    if deliverable == "bilingual-subs":
        return complete("bilingual_subs_complete", rendered_dir=str(rendered_dir))

    burned = sorted(
        path for path in job_dir.glob("*.bilingual.mp4") if path.is_file() and path.stat().st_size
    )
    if not burned:
        return {
            "complete": False,
            "stage": "burn_required",
            "job_dir": str(job_dir),
            "missing": ["*.bilingual.mp4"],
        }
    return complete("bilingual_complete", burned_video=str(burned[-1]))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check whether a video job is video-only complete or bilingual complete."
    )
    parser.add_argument("download_manifest", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = assess_delivery(args.download_manifest)
    except (DeliveryError, OSError) as exc:
        print(f"delivery verification error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
