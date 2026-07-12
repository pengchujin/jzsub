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


def assess_delivery(download_manifest: Path) -> dict[str, Any]:
    download_manifest = download_manifest.expanduser().resolve()
    download = _read_json(download_manifest)
    configured_dir = download.get("output_directory")
    job_dir = (
        Path(configured_dir).expanduser().resolve()
        if isinstance(configured_dir, str)
        else download_manifest.parent
    )
    artifacts = download.get("artifacts")
    if not isinstance(artifacts, dict):
        raise DeliveryError("download manifest has no artifacts object")
    if _existing_video_artifact(job_dir, artifacts) is None:
        raise DeliveryError("no declared video artifact exists on disk")

    subtitle_record = artifacts.get("subtitle")
    subtitle = None
    if isinstance(subtitle_record, dict) and subtitle_record.get("dialogue") is not False:
        subtitle = _artifact_path(job_dir, subtitle_record.get("source_srt"))
    if subtitle is None:
        return {
            "complete": True,
            "stage": "video_only_complete",
            "job_dir": str(job_dir),
            "missing": [],
        }
    if not subtitle.is_file():
        raise DeliveryError(f"declared source subtitle is missing: {subtitle}")

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
    return {
        "complete": True,
        "stage": "bilingual_complete",
        "job_dir": str(job_dir),
        "burned_video": str(burned[-1]),
        "missing": [],
    }


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
