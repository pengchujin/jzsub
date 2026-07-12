#!/usr/bin/env python3
"""Fetch one video's best streams, cover, and original-language subtitles.

This script deliberately shells out to the yt-dlp executable instead of importing
yt-dlp as a Python package. Browser cookies are passed directly to
``--cookies-from-browser`` and are never exported or written to the manifest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, NamedTuple, Sequence


FORMAT_SELECTOR = "bv*+ba/b"
MANIFEST_NAME = "download-manifest.json"
YOUTUBE_SKIP_TRANSLATIONS = "youtube:skip=translated_subs"
AUTO_BROWSER_COOKIES = "auto"
_CHINESE_CODES = {"zh", "zho", "chi", "cmn", "yue", "wuu"}
_NON_SUBTITLE_CODES = {"live_chat", "live-chat", "danmaku"}
_UNSAFE_FILENAME = re.compile(r"[<>:\"/\\|?*%\x00-\x1f\x7f]")
_CREDENTIAL_REMAINDER = re.compile(
    r"(?i)\b(?P<key>proxy-authorization|set-cookie|authorization|cookie|password|sessdata|token)\b"
    r"(?P<separator>\s*[:=]\s*|\s+)"
)
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class FetchError(RuntimeError):
    """Expected, user-actionable fetch failure."""


class SubtitleSelectionError(FetchError):
    """No suitable original-language subtitle matched the requested policy."""


class SubtitleChoice(NamedTuple):
    language: str
    kind: str
    original_format: str
    available_formats: tuple[str, ...]


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    encoded = encoded[:max_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def safe_stem(title: Any, video_id: Any, max_bytes: int = 180) -> str:
    """Return a bounded cross-platform filename stem without losing Unicode."""

    clean_title = unicodedata.normalize("NFKC", str(title or "untitled"))
    clean_title = _UNSAFE_FILENAME.sub("_", clean_title)
    clean_title = re.sub(r"\s+", " ", clean_title)
    clean_title = re.sub(r"_+", "_", clean_title).strip(" ._") or "untitled"
    if clean_title.upper() in _WINDOWS_RESERVED:
        clean_title = f"_{clean_title}"

    clean_id = unicodedata.normalize("NFKC", str(video_id or "unknown"))
    clean_id = _UNSAFE_FILENAME.sub("_", clean_id)
    clean_id = re.sub(r"\s+", "_", clean_id).strip(" ._") or "unknown"
    clean_id = _truncate_utf8(clean_id, max(1, min(64, max_bytes // 2))).rstrip(" ._") or "unknown"
    suffix = f" [{clean_id}]"
    budget = max(1, max_bytes - len(suffix.encode("utf-8")))
    clean_title = _truncate_utf8(clean_title, budget).rstrip(" ._") or "untitled"
    return f"{clean_title}{suffix}"


def validate_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise FetchError("URL must be an absolute http:// or https:// video link")
    if any(ord(char) < 32 for char in url):
        raise FetchError("URL contains control characters")
    return url


def display_url(url: str) -> str:
    """Return a diagnostic URL that cannot reveal query tokens or fragments."""

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "<redacted-url>"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "<redacted-url>"
    return f"{parsed.scheme}://{parsed.netloc}/…"


def canonical_public_url(info: dict[str, Any], fallback: str) -> str:
    """Keep only public locator query fields in the manifest."""

    candidate = str(info.get("webpage_url") or info.get("original_url") or fallback)
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return display_url(fallback)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return display_url(fallback)
    public_keys = {"v", "p", "bvid", "aid", "ep_id", "season_id"}
    public_query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() in public_keys
    ]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(public_query), "")
    )


def sanitize_diagnostic(text: str, secrets: Sequence[str] = ()) -> str:
    sanitized = text or ""
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        sanitized = sanitized.replace(secret, "<redacted>")
    sanitized = re.sub(
        r"https?://[^\s'\"<>]+",
        lambda match: display_url(match.group(0)),
        sanitized,
    )
    redacted_lines: list[str] = []
    for line in sanitized.splitlines():
        match = _CREDENTIAL_REMAINDER.search(line)
        if match:
            separator = match.group("separator")
            if ":" in separator:
                normalized_separator = ": "
            elif "=" in separator:
                normalized_separator = "="
            else:
                normalized_separator = " "
            line = (
                line[: match.start("key")]
                + match.group("key")
                + normalized_separator
                + "<redacted>"
            )
        redacted_lines.append(line)
    return "\n".join(redacted_lines).strip()


def _validate_browser_spec(browser_cookies: str | None) -> None:
    if browser_cookies is None:
        return
    if not browser_cookies.strip() or any(ord(char) < 32 for char in browser_cookies):
        raise FetchError("--browser-cookies must be a non-empty yt-dlp browser/profile spec")


def _looks_like_authentication_failure(error: BaseException | str) -> bool:
    text = str(error).lower()
    markers = (
        "sign in",
        "log in",
        "login required",
        "authentication required",
        "not a bot",
        "cookies-from-browser",
        "members-only",
        "member-only",
        "premium-only",
        "http error 401",
        "http error 403",
        "forbidden",
    )
    return any(marker in text for marker in markers)


def ytdlp_common_args(
    browser_cookies: str | None,
    allow_remote_ejs: bool,
    executable: str = "yt-dlp",
) -> list[str]:
    """Build deterministic yt-dlp arguments without exporting browser cookies."""

    _validate_browser_spec(browser_cookies)
    if browser_cookies == AUTO_BROWSER_COOKIES:
        raise FetchError("the auto browser-cookie mode must be resolved before invoking yt-dlp")
    args = [
        executable,
        "--ignore-config",
        "--no-playlist",
        "--no-write-playlist-metafiles",
        "--no-progress",
        "--extractor-args",
        YOUTUBE_SKIP_TRANSLATIONS,
    ]
    if allow_remote_ejs:
        args.extend(["--remote-components", "ejs:npm"])
    if browser_cookies:
        # Pass the user's browser/profile expression byte-for-byte. Never export it.
        args.extend(["--cookies-from-browser", browser_cookies])
    return args


def _run(
    args: Sequence[str],
    purpose: str,
    *,
    secrets: Sequence[str] = (),
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise FetchError(f"Required executable not found while {purpose}: {args[0]}") from exc
    except OSError as exc:
        raise FetchError(f"Could not start {args[0]} while {purpose}: {exc}") from exc

    if check and result.returncode:
        details = sanitize_diagnostic(result.stderr, secrets)[-4000:]
        if not details:
            details = f"{args[0]} exited with status {result.returncode}"
        raise FetchError(f"Failed while {purpose}: {details}")
    return result


def _parse_single_json(stdout: str, purpose: str) -> dict[str, Any]:
    candidates = [stdout.strip(), *(line.strip() for line in reversed(stdout.splitlines()))]
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise FetchError(f"{purpose} did not return valid JSON")


def probe_video(
    url: str,
    browser_cookies: str | None,
    allow_remote_ejs: bool,
    executable: str,
) -> dict[str, Any]:
    command = ytdlp_common_args(browser_cookies, allow_remote_ejs, executable)
    command.extend(["--dump-single-json", "--skip-download", url])
    result = _run(command, "probing the video", secrets=(url, browser_cookies or ""))
    info = _parse_single_json(result.stdout, "yt-dlp probe")
    if info.get("_type") in {"playlist", "multi_video"} or isinstance(info.get("entries"), list):
        raise FetchError("Expected one video, but the supplied URL resolved to a playlist")
    if not info.get("id"):
        raise FetchError("yt-dlp probe returned no video id")
    return info


def _normalized_language(language: str) -> str:
    return language.strip().lower().replace("_", "-")


def _language_base(language: str) -> str:
    normalized = _normalized_language(language)
    if normalized.endswith("-orig"):
        normalized = normalized[:-5]
    return normalized.split("-", 1)[0]


def _excluded_language(language: str) -> bool:
    normalized = _normalized_language(language)
    segments = set(normalized.split("-"))
    return (
        normalized in _NON_SUBTITLE_CODES
        or _language_base(normalized) in _CHINESE_CODES
        or bool(segments & _CHINESE_CODES)
    )


def _track_formats(tracks: Any) -> tuple[str, ...]:
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not isinstance(tracks, list):
        return ()
    formats: list[str] = []
    for track in tracks:
        if isinstance(track, dict) and track.get("ext"):
            ext = str(track["ext"]).lower()
            if ext not in formats:
                formats.append(ext)
    return tuple(formats)


def _youtube_track_is_translated(track: Any) -> bool:
    if not isinstance(track, dict):
        return False
    url = str(track.get("url") or "")
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    except ValueError:
        query = {}
    if query.get("tlang"):
        return True
    name = str(track.get("name") or track.get("label") or "").lower()
    source = str(track.get("source") or "").lower()
    return bool(re.search(r"\bfrom\b", name)) or source in {"translation", "translated"}


def _candidate_rows(info: dict[str, Any]) -> list[dict[str, Any]]:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    is_youtube = "youtube" in extractor
    rows: list[dict[str, Any]] = []
    for kind, key in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        mapping = info.get(key)
        if not isinstance(mapping, dict):
            continue
        for position, (language, tracks) in enumerate(mapping.items()):
            language = str(language)
            if _excluded_language(language):
                continue
            normalized_tracks = tracks if isinstance(tracks, list) else [tracks]
            if is_youtube and any(_youtube_track_is_translated(track) for track in normalized_tracks):
                continue
            formats = _track_formats(normalized_tracks)
            if not formats:
                continue
            rows.append(
                {
                    "language": language,
                    "kind": kind,
                    "formats": formats,
                    "position": position,
                }
            )

    # YouTube exposes generated translations alongside the true ASR track. When an
    # explicit *-orig track exists, non-orig automatic rows are redundant at best
    # and translated at worst.
    if is_youtube and any(
        row["kind"] == "automatic" and _normalized_language(row["language"]).endswith("-orig")
        for row in rows
    ):
        rows = [
            row
            for row in rows
            if row["kind"] != "automatic"
            or _normalized_language(row["language"]).endswith("-orig")
        ]
    return rows


def _preferred_original_format(formats: Sequence[str]) -> str:
    for preferred in ("srt", "vtt", "ttml", "ass", "srv3", "srv2", "srv1", "json3"):
        if preferred in formats:
            return preferred
    return formats[0] if formats else "best"


def select_source_subtitle(
    info: dict[str, Any], source_lang: str | None = None
) -> SubtitleChoice | None:
    """Choose one non-Chinese, non-translated original-language subtitle."""

    rows = _candidate_rows(info)
    if source_lang:
        if _excluded_language(source_lang):
            raise SubtitleSelectionError("--source-lang must name a non-Chinese subtitle track")
        requested = _normalized_language(source_lang)
        requested_base = _language_base(requested)
        matches = [
            row
            for row in rows
            if _normalized_language(row["language"]) == requested
            or _language_base(row["language"]) == requested_base
        ]
        if not matches:
            available = ", ".join(row["language"] for row in rows) or "none"
            raise SubtitleSelectionError(
                f"Requested source subtitle {source_lang!r} is unavailable; candidates: {available}"
            )
        matches.sort(
            key=lambda row: (
                _normalized_language(row["language"]) != requested,
                row["kind"] != "manual",
                not _normalized_language(row["language"]).endswith("-orig"),
                row["position"],
            )
        )
        selected = matches[0]
    else:
        if not rows:
            return None

        declared_language = str(
            info.get("original_language") or info.get("language") or ""
        ).strip()
        if _language_base(declared_language) in {"und", "mul", "unknown"}:
            declared_language = ""
        if declared_language and _excluded_language(declared_language):
            # A Chinese-source video does not need a Chinese translation. Other
            # advertised tracks may be translations, so do not guess one.
            return None

        selected = None
        if declared_language:
            declared_normalized = _normalized_language(declared_language)
            declared_base = _language_base(declared_normalized)
            declared_matches = [
                row for row in rows if _language_base(row["language"]) == declared_base
            ]
            if declared_matches:
                declared_matches.sort(
                    key=lambda row: (
                        _normalized_language(row["language"]) != declared_normalized,
                        row["kind"] != "manual",
                        not _normalized_language(row["language"]).endswith("-orig"),
                        row["position"],
                    )
                )
                selected = declared_matches[0]
            else:
                raise SubtitleSelectionError(
                    "The platform declares original language "
                    f"{declared_language!r}, but no matching subtitle track is available; "
                    "specify --source-lang to override"
                )

        if selected is None:
            orig_rows = [
                row
                for row in rows
                if _normalized_language(row["language"]).endswith("-orig")
            ]
            orig_bases = {_language_base(row["language"]) for row in orig_rows}
            if len(orig_bases) == 1:
                unique_base = next(iter(orig_bases))
                matching_orig = [
                    row for row in orig_rows if _language_base(row["language"]) == unique_base
                ]
                matching_orig.sort(
                    key=lambda row: (row["kind"] != "manual", row["position"])
                )
                selected = matching_orig[0]
            elif len(orig_bases) > 1:
                choices = ", ".join(sorted(orig_bases))
                raise SubtitleSelectionError(
                    "Multiple plausible original subtitle languages remain "
                    f"({choices}); specify --source-lang"
                )

        if selected is None:
            remaining_bases = {_language_base(row["language"]) for row in rows}
            if len(remaining_bases) == 1:
                unique_base = next(iter(remaining_bases))
                same_base = [
                    row for row in rows if _language_base(row["language"]) == unique_base
                ]
                same_base.sort(
                    key=lambda row: (
                        row["kind"] != "manual",
                        not _normalized_language(row["language"]).endswith("-orig"),
                        row["position"],
                    )
                )
                selected = same_base[0]
            elif len(remaining_bases) > 1:
                choices = ", ".join(sorted(remaining_bases))
                raise SubtitleSelectionError(
                    "Multiple plausible subtitle languages remain "
                    f"({choices}); specify --source-lang"
                )
            else:
                return None

    formats = tuple(selected["formats"])
    return SubtitleChoice(
        language=selected["language"],
        kind=selected["kind"],
        original_format=_preferred_original_format(formats),
        available_formats=formats,
    )


def available_subtitle_summary(info: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "language": row["language"],
            "kind": row["kind"],
            "formats": list(row["formats"]),
        }
        for row in _candidate_rows(info)
    ]


def _download_video_and_cover(
    *,
    url: str,
    output_dir: Path,
    base: str,
    browser_cookies: str | None,
    allow_remote_ejs: bool,
    executable: str,
) -> subprocess.CompletedProcess[str]:
    command = ytdlp_common_args(browser_cookies, allow_remote_ejs, executable)
    command.extend(
        [
            "-P",
            str(output_dir),
            "-f",
            FORMAT_SELECTOR,
            "--merge-output-format",
            "mkv",
            "--remux-video",
            "mkv",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--no-overwrites",
            "--no-post-overwrites",
            "-o",
            f"{base}.intermediate.%(ext)s",
            "-o",
            f"thumbnail:{base}.cover.%(ext)s",
            url,
        ]
    )
    return _run(
        command,
        "downloading the highest-quality video and cover",
        secrets=(url, browser_cookies or ""),
    )


def _subtitle_language_label(language: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", language).strip("._-") or "unknown"


def _download_original_subtitle(
    *,
    url: str,
    output_dir: Path,
    base: str,
    choice: SubtitleChoice,
    browser_cookies: str | None,
    allow_remote_ejs: bool,
    executable: str,
) -> subprocess.CompletedProcess[str]:
    command = ytdlp_common_args(browser_cookies, allow_remote_ejs, executable)
    write_flag = "--write-subs" if choice.kind == "manual" else "--write-auto-subs"
    language_label = _subtitle_language_label(choice.language)
    command.extend(
        [
            "-P",
            str(output_dir),
            "--skip-download",
            write_flag,
            "--sub-langs",
            re.escape(choice.language),
            "--sub-format",
            choice.original_format,
            "--no-overwrites",
            "--no-post-overwrites",
            "-o",
            f"subtitle:{base}.source-original.{language_label}.%(ext)s",
        ]
    )
    command.append(url)
    return _run(
        command,
        "preserving the original subtitle track",
        secrets=(url, browser_cookies or ""),
    )


def _artifact(output_dir: Path, prefix: str, required_suffix: str | None = None) -> Path | None:
    candidates = []
    for path in output_dir.iterdir():
        if not path.is_file() or not path.name.startswith(prefix):
            continue
        if path.name.endswith((".part", ".ytdl", ".temp")) or ".partial." in path.name:
            continue
        if required_suffix and path.suffix.lower() != required_suffix.lower():
            continue
        candidates.append(path)
    return max(candidates, key=lambda item: item.stat().st_mtime_ns) if candidates else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _derive_source_srt(
    original: Path,
    target: Path,
    *,
    ffmpeg: str,
    replace_existing: bool,
) -> tuple[Path, str, str]:
    """Derive SRT from one immutable downloaded subtitle, then atomically publish it."""

    if target.exists() and not replace_existing:
        raise FetchError(f"Derived subtitle already exists: {target}; pass --resume to regenerate it")
    parent_hash = _sha256(original)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=".srt", dir=target.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        if original.suffix.lower() == ".srt":
            shutil.copyfile(original, temp_path)
            method = "byte-copy"
        else:
            temp_path.unlink(missing_ok=True)
            result = _run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-i",
                    str(original),
                    "-map",
                    "0:0",
                    "-c:s",
                    "srt",
                    "-f",
                    "srt",
                    str(temp_path),
                ],
                "converting the preserved subtitle to SRT",
                secrets=(str(original), str(target), str(temp_path)),
                check=False,
            )
            if result.returncode:
                details = sanitize_diagnostic(
                    result.stderr, (str(original), str(target), str(temp_path))
                )[-1200:]
                raise FetchError(
                    "Failed while converting the preserved subtitle to SRT: "
                    + (details or f"ffmpeg exited with status {result.returncode}")
                )
            method = "ffmpeg"

        if _sha256(original) != parent_hash:
            raise FetchError("The preserved original subtitle changed during SRT derivation")
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
    return target, method, parent_hash


def _file_record(path: Path | None, output_dir: Path, *, checksum: bool = False) -> dict[str, Any] | None:
    if path is None:
        return None
    record: dict[str, Any] = {
        "path": path.relative_to(output_dir).as_posix(),
        "size_bytes": path.stat().st_size,
        "extension": path.suffix.lower().lstrip("."),
    }
    if checksum:
        record["sha256"] = _sha256(path)
    return record


def _ffprobe(path: Path, executable: str) -> dict[str, Any]:
    result = _run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            (
                "format=format_name,duration,size,bit_rate:"
                "stream=index,codec_type,codec_name,profile,level,bit_rate,"
                "width,height,avg_frame_rate,r_frame_rate,pix_fmt,"
                "color_transfer,color_space,color_primaries,channels,sample_rate"
            ),
            "-of",
            "json",
            str(path),
        ],
        "inspecting the downloaded media",
        secrets=(str(path),),
    )
    return _parse_single_json(result.stdout, "ffprobe")


def _atomic_ffmpeg_output(
    input_path: Path,
    output_path: Path,
    ffmpeg_args: Sequence[str],
    ffmpeg: str,
    *,
    replace_existing: bool = False,
) -> tuple[Path | None, str | None]:
    if output_path.exists() and not replace_existing:
        return output_path, None
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}.", suffix=output_path.suffix, dir=output_path.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    temp_path.unlink(missing_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        *ffmpeg_args,
        "-f",
        "mp4",
        str(temp_path),
    ]
    result = _run(
        command,
        "creating an MP4",
        secrets=(str(input_path), str(output_path), str(temp_path)),
        check=False,
    )
    if result.returncode:
        temp_path.unlink(missing_ok=True)
        details = sanitize_diagnostic(result.stderr, (str(input_path), str(temp_path)))[-1200:]
        return None, details or f"ffmpeg exited with status {result.returncode}"
    os.replace(temp_path, output_path)
    return output_path, None


def _try_lossless_mp4(
    intermediate: Path,
    output_path: Path,
    ffmpeg: str,
    *,
    replace_existing: bool = False,
) -> tuple[Path | None, str | None]:
    return _atomic_ffmpeg_output(
        intermediate,
        output_path,
        ["-c", "copy", "-movflags", "+faststart"],
        ffmpeg,
        replace_existing=replace_existing,
    )


def _create_fallback_mp4(
    intermediate: Path,
    output_path: Path,
    ffmpeg: str,
    *,
    replace_existing: bool = False,
) -> tuple[Path | None, str | None]:
    return _atomic_ffmpeg_output(
        intermediate,
        output_path,
        [
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-movflags",
            "+faststart",
        ],
        ffmpeg,
        replace_existing=replace_existing,
    )


def _warning_lines(results: Sequence[subprocess.CompletedProcess[str]], secrets: Sequence[str]) -> list[str]:
    warnings: list[str] = []
    for result in results:
        for line in result.stderr.splitlines():
            if "warning" not in line.lower():
                continue
            clean = sanitize_diagnostic(line, secrets)
            if clean and clean not in warnings:
                warnings.append(clean[:1000])
    return warnings


def _manifest_base(
    *,
    info: dict[str, Any],
    url: str,
    output_dir: Path,
    browser_cookies: str | None,
    allow_remote_ejs: bool,
    choice: SubtitleChoice | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {
            "url": canonical_public_url(info, url),
            "extractor": info.get("extractor_key") or info.get("extractor"),
            "id": str(info.get("id")),
            "title": str(info.get("title") or "untitled"),
            "duration_seconds": info.get("duration"),
            "declared_language": info.get("language") or info.get("original_language"),
        },
        "output_directory": str(output_dir),
        "authentication": {
            "browser_cookies_used": bool(browser_cookies),
            "cookie_export_created": False,
        },
        "remote_components": {
            "ejs_allowed": allow_remote_ejs,
            "ejs_source": "npm" if allow_remote_ejs else None,
        },
        "selection": {
            "playlist_allowed": False,
            "format": FORMAT_SELECTOR,
            "intermediate_container": "mkv",
            "subtitle": choice._asdict() if choice else None,
            "subtitle_candidates": available_subtitle_summary(info),
        },
        "warnings": [],
    }


def _write_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    destination = output_dir / MANIFEST_NAME
    fd, temp_name = tempfile.mkstemp(prefix=".download-manifest.", suffix=".json", dir=output_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)
        raise
    return destination


def _load_subtitle_pipeline() -> Any:
    path = Path(__file__).resolve().with_name("subtitle_pipeline.py")
    spec = importlib.util.spec_from_file_location("download_video_subtitle_pipeline", path)
    if spec is None or spec.loader is None:
        raise FetchError(f"Could not load subtitle pipeline: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _advance_bilingual_stage(download_manifest: Path) -> int:
    """Prepare captions immediately and make a subtitled fetch non-terminal."""
    download_manifest = download_manifest.expanduser().resolve()
    try:
        manifest = json.loads(download_manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise FetchError(f"Could not read download manifest: {download_manifest}") from exc
    if not isinstance(manifest, dict):
        raise FetchError("Download manifest root must be an object")
    output_value = manifest.get("output_directory")
    output_dir = (
        Path(output_value).expanduser().resolve()
        if isinstance(output_value, str)
        else download_manifest.parent
    )
    artifacts = manifest.get("artifacts")
    subtitle = artifacts.get("subtitle") if isinstance(artifacts, dict) else None
    source_record = subtitle.get("source_srt") if isinstance(subtitle, dict) else None
    source_value = source_record.get("path") if isinstance(source_record, dict) else None
    if not isinstance(source_value, str):
        manifest["status"] = "video_only_complete"
        execution = manifest.setdefault("execution", {})
        if not isinstance(execution, dict):
            execution = manifest["execution"] = {}
        execution.update({"complete": True, "next_stage": None})
        _write_manifest(output_dir, manifest)
        print(
            json.dumps(
                {"complete": True, "status": "video_only_complete"},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    source_srt = Path(source_value)
    if not source_srt.is_absolute():
        source_srt = output_dir / source_srt
    language = subtitle.get("language")
    kind = subtitle.get("kind")
    if not isinstance(language, str) or not language:
        raise FetchError("Downloaded subtitle has no language tag")
    pipeline = _load_subtitle_pipeline()
    try:
        subtitle_manifest = pipeline.prepare(
            source_srt,
            output_dir / "subtitles",
            language,
            "smart" if kind == "automatic" else "preserve",
        )
        subtitle_data = json.loads(subtitle_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, pipeline.PipelineError) as exc:
        raise FetchError(f"Could not prepare source subtitles: {exc}") from exc
    batches = subtitle_data.get("translation_batches")
    batch_paths = [
        batch.get("path")
        for batch in batches
        if isinstance(batch, dict) and isinstance(batch.get("path"), str)
    ] if isinstance(batches, list) else []
    if not batch_paths:
        raise FetchError("Subtitle preparation produced no translation batches")

    manifest["status"] = "bilingual_required"
    execution = manifest.setdefault("execution", {})
    if not isinstance(execution, dict):
        execution = manifest["execution"] = {}
    execution.update(
        {
            "complete": False,
            "next_stage": "translation_required",
            "subtitle_manifest": str(subtitle_manifest),
            "translation_batch_count": len(batch_paths),
        }
    )
    _write_manifest(output_dir, manifest)
    print(
        json.dumps(
            {
                "complete": False,
                "status": "bilingual_required",
                "next_stage": "translation_required",
                "subtitle_manifest": str(subtitle_manifest),
                "translation_batch_count": len(batch_paths),
                "instruction": (
                    "Run subtitle_pipeline.py next-batch for one compact GPT translation "
                    "batch at a time, then render, burn, and verify."
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 3


def _require_executable(name: str, install_hint: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FetchError(f"Required executable {name!r} was not found in PATH. {install_hint}")
    return path


def _prepare_output_dir(output_dir: Path, *, resume: bool) -> Path:
    if output_dir.exists() and not output_dir.is_dir():
        raise FetchError(f"Output path is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not resume and any(output_dir.iterdir()):
        raise FetchError(
            f"Output directory is not empty: {output_dir}. "
            "Choose a new directory or pass --resume explicitly."
        )
    return output_dir


def _dry_run_plan(args: argparse.Namespace) -> dict[str, Any]:
    cookie_mode = (
        "anonymous-then-silent-chrome"
        if args.browser_cookies == AUTO_BROWSER_COOKIES
        else ("browser-direct" if args.browser_cookies else "anonymous")
    )
    return {
        "status": "dry-run",
        "network_accessed": False,
        "files_written": False,
        "source": display_url(args.url),
        "output_directory": str(Path(args.output_dir).expanduser()),
        "browser_cookies_configured": bool(args.browser_cookies),
        "browser_cookie_mode": cookie_mode,
        "browser_cookie_value_logged": False,
        "allow_remote_ejs": args.allow_remote_ejs,
        "resume": args.resume,
        "probe_only": args.probe_only,
        "format": FORMAT_SELECTOR,
        "intermediate_container": "mkv",
        "lossless_mp4_attempted": not args.probe_only,
        "lossy_mp4_fallback_requested": args.mp4_fallback,
    }


def execute(args: argparse.Namespace) -> Path | None:
    url = validate_url(args.url)
    _validate_browser_spec(args.browser_cookies)
    if args.dry_run:
        print(json.dumps(_dry_run_plan(args), ensure_ascii=False, indent=2, sort_keys=True))
        return None

    yt_dlp = _require_executable(
        "yt-dlp",
        "Install the current official yt-dlp release and ensure the binary is executable.",
    )
    if args.allow_remote_ejs:
        _require_executable(
            "deno",
            "--allow-remote-ejs uses the official ejs:npm path and therefore requires Deno.",
        )
    output_dir = Path(args.output_dir).expanduser().resolve()
    _prepare_output_dir(output_dir, resume=args.resume)

    requested_browser_cookies = args.browser_cookies
    auto_cookie_fallback = requested_browser_cookies == AUTO_BROWSER_COOKIES
    effective_browser_cookies = None if auto_cookie_fallback else requested_browser_cookies
    authentication_mode = "browser-direct" if effective_browser_cookies else "anonymous"

    print(f"Probing one video: {display_url(url)}", file=sys.stderr)
    try:
        info = probe_video(
            url, effective_browser_cookies, args.allow_remote_ejs, yt_dlp
        )
    except FetchError as exc:
        if not auto_cookie_fallback or not _looks_like_authentication_failure(exc):
            raise
        effective_browser_cookies = "chrome"
        authentication_mode = "anonymous-then-silent-chrome"
        print(
            "Anonymous access requires authentication; retrying silently with Chrome cookies…",
            file=sys.stderr,
        )
        info = probe_video(
            url, effective_browser_cookies, args.allow_remote_ejs, yt_dlp
        )
    choice = select_source_subtitle(info, args.source_lang)
    manifest = _manifest_base(
        info=info,
        url=url,
        output_dir=output_dir,
        browser_cookies=effective_browser_cookies,
        allow_remote_ejs=args.allow_remote_ejs,
        choice=choice,
    )
    manifest["execution"] = {"resume": bool(args.resume)}
    manifest["authentication"]["mode"] = authentication_mode
    if choice is None:
        manifest["warnings"].append(
            "No suitable non-Chinese original subtitle was advertised by the platform"
        )

    if args.probe_only:
        manifest["status"] = "probed"
        destination = _write_manifest(output_dir, manifest)
        print(f"Probe manifest: {destination}", file=sys.stderr)
        return destination

    ffmpeg = _require_executable(
        "ffmpeg",
        "Install FFmpeg; it is required for stream merging, thumbnail conversion, and MP4 output.",
    )
    ffprobe = _require_executable(
        "ffprobe",
        "Install FFmpeg with ffprobe; it is required to verify downloaded media.",
    )
    if "youtube" in str(info.get("extractor_key") or info.get("extractor") or "").lower():
        if not shutil.which("deno"):
            manifest["warnings"].append(
                "Deno was not found; current yt-dlp may expose fewer YouTube formats without a supported JS runtime"
            )

    base = safe_stem(info.get("title"), info.get("id"))
    completed: list[subprocess.CompletedProcess[str]] = []
    print("Downloading highest-quality streams and cover…", file=sys.stderr)
    completed.append(
        _download_video_and_cover(
            url=url,
            output_dir=output_dir,
            base=base,
            browser_cookies=effective_browser_cookies,
            allow_remote_ejs=args.allow_remote_ejs,
            executable=yt_dlp,
        )
    )

    language_label = _subtitle_language_label(choice.language) if choice else None
    original_prefix = (
        f"{base}.source-original.{language_label}." if language_label else None
    )
    original_subtitle = (
        _artifact(output_dir, original_prefix) if original_prefix else None
    )
    if choice and original_subtitle is None:
        print(f"Preserving original subtitle track ({choice.language})…", file=sys.stderr)
        completed.append(
            _download_original_subtitle(
                url=url,
                output_dir=output_dir,
                base=base,
                choice=choice,
                browser_cookies=effective_browser_cookies,
                allow_remote_ejs=args.allow_remote_ejs,
                executable=yt_dlp,
            )
        )
        original_subtitle = _artifact(output_dir, original_prefix)

    intermediate = _artifact(output_dir, f"{base}.intermediate.")
    if intermediate is None:
        raise FetchError("yt-dlp completed but no intermediate video file was found")
    cover = _artifact(output_dir, f"{base}.cover.", ".jpg")
    if choice and original_subtitle is None:
        raise FetchError("The selected original subtitle track was not written to disk")
    source_srt: Path | None = None
    subtitle_conversion_method: str | None = None
    subtitle_parent_hash: str | None = None
    if choice and original_subtitle:
        source_srt, subtitle_conversion_method, subtitle_parent_hash = _derive_source_srt(
            original_subtitle,
            output_dir / f"{base}.source-srt.{language_label}.srt",
            ffmpeg=ffmpeg,
            replace_existing=args.resume,
        )
    if cover is None:
        manifest["warnings"].append("The platform did not yield a JPG cover")

    media_probe = _ffprobe(intermediate, ffprobe)
    if not any(
        stream.get("codec_type") == "video"
        for stream in media_probe.get("streams", [])
        if isinstance(stream, dict)
    ):
        raise FetchError("The intermediate failed verification: no video stream was found")
    master_path = output_dir / f"{base}.master.mp4"
    print("Trying lossless MP4 remux…", file=sys.stderr)
    master, remux_error = _try_lossless_mp4(
        intermediate,
        master_path,
        ffmpeg,
        replace_existing=args.resume,
    )
    if remux_error:
        manifest["warnings"].append(f"Lossless MP4 remux unavailable: {remux_error}")

    fallback: Path | None = None
    fallback_error: str | None = None
    if master is None and args.mp4_fallback:
        print("Lossless remux was unavailable; creating requested high-quality MP4 fallback…", file=sys.stderr)
        fallback, fallback_error = _create_fallback_mp4(
            intermediate,
            output_dir / f"{base}.fallback.mp4",
            ffmpeg,
            replace_existing=args.resume,
        )
        if fallback_error:
            raise FetchError(f"Requested MP4 fallback failed: {fallback_error}")

    original_subtitle_record = _file_record(
        original_subtitle, output_dir, checksum=True
    )
    source_srt_record = _file_record(source_srt, output_dir, checksum=True)
    if choice and original_subtitle_record and source_srt_record:
        if original_subtitle_record.get("sha256") != subtitle_parent_hash:
            raise FetchError("Original subtitle hash no longer matches the SRT parent hash")
        original_subtitle_record["content_role"] = "immutable-parent"
        source_srt_record["conversion_method"] = subtitle_conversion_method
        source_srt_record["derived_from"] = {
            "path": original_subtitle_record["path"],
            "sha256": subtitle_parent_hash,
        }

    manifest["status"] = "downloaded"
    manifest["artifacts"] = {
        "intermediate": _file_record(intermediate, output_dir),
        "media_streams": media_probe.get("streams", []),
        "lossless_mp4_master": _file_record(master, output_dir),
        "lossy_mp4_fallback": {
            "requested": bool(args.mp4_fallback),
            "created": _file_record(fallback, output_dir),
            "reason_not_created": (
                "lossless_master_available"
                if args.mp4_fallback and master is not None
                else (fallback_error if args.mp4_fallback and fallback is None else None)
            ),
            "video_encoding": "libx264 preset=slow crf=18" if fallback else None,
            "audio_encoding": "aac 256k" if fallback else None,
        },
        "cover": _file_record(cover, output_dir, checksum=True),
        "subtitle": {
            "language": choice.language if choice else None,
            "kind": choice.kind if choice else None,
            "original": original_subtitle_record,
            "source_srt": source_srt_record,
            "original_is_never_modified_by_this_script": True,
        }
        if choice
        else None,
    }
    manifest["warnings"].extend(
        item
        for item in _warning_lines(completed, (url, effective_browser_cookies or ""))
        if item not in manifest["warnings"]
    )
    destination = _write_manifest(output_dir, manifest)
    print(f"Download manifest: {destination}", file=sys.stderr)
    return destination


def run_self_tests() -> bool:
    class FetchVideoTests(unittest.TestCase):
        def test_safe_stem_blocks_traversal_and_keeps_id(self) -> None:
            self.assertEqual(safe_stem("../bad/name", "id"), "bad_name [id]")

        def test_safe_stem_is_utf8_bounded(self) -> None:
            stem = safe_stem("中文" * 100, "BV1", max_bytes=48)
            self.assertLessEqual(len(stem.encode("utf-8")), 48)
            self.assertTrue(stem.endswith(" [BV1]"))

        def test_safe_stem_bounds_an_untrusted_long_id(self) -> None:
            stem = safe_stem("title", "x" * 500, max_bytes=48)
            self.assertLessEqual(len(stem.encode("utf-8")), 48)

        def test_cookie_profile_is_passed_directly_without_export(self) -> None:
            common = ytdlp_common_args("chrome:Profile 1", True)
            index = common.index("--cookies-from-browser")
            self.assertEqual(common[index + 1], "chrome:Profile 1")
            self.assertNotIn("--cookies", common)
            self.assertIn("ejs:npm", common)
            self.assertNotIn("ejs:github", common)

        def test_auto_cookie_mode_is_resolved_before_ytdlp(self) -> None:
            with self.assertRaisesRegex(FetchError, "must be resolved"):
                ytdlp_common_args(AUTO_BROWSER_COOKIES, False)

        def test_only_authentication_failures_trigger_cookie_fallback(self) -> None:
            self.assertTrue(
                _looks_like_authentication_failure(
                    "Sign in to confirm you're not a bot; use --cookies-from-browser"
                )
            )
            self.assertTrue(_looks_like_authentication_failure("HTTP Error 403: Forbidden"))
            self.assertFalse(_looks_like_authentication_failure("Temporary DNS failure"))

        def test_no_subtitle_is_a_valid_video_only_selection(self) -> None:
            info = {"extractor_key": "BiliBili", "subtitles": {}, "automatic_captions": {}}
            self.assertIsNone(select_source_subtitle(info))
            self.assertEqual(available_subtitle_summary(info), [])

        def test_subtitle_download_is_nonterminal_and_prepares_translation_batches(self) -> None:
            advance = globals().get("_advance_bilingual_stage")
            self.assertTrue(callable(advance), "_advance_bilingual_stage is required")
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "video.source-srt.ja.srt"
                source.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
                    encoding="utf-8",
                )
                manifest = {
                    "status": "downloaded",
                    "output_directory": str(root),
                    "execution": {},
                    "artifacts": {
                        "subtitle": {
                            "language": "ja",
                            "kind": "automatic",
                            "source_srt": {"path": source.name},
                        }
                    },
                }
                manifest_path = _write_manifest(root, manifest)

                exit_code = advance(manifest_path)

                updated = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(exit_code, 3)
                self.assertEqual(updated["status"], "bilingual_required")
                self.assertFalse(updated["execution"]["complete"])
                self.assertEqual(updated["execution"]["next_stage"], "translation_required")
                self.assertTrue((root / "subtitles" / "subtitle-manifest.json").is_file())
                self.assertTrue(
                    list((root / "subtitles" / "translation-input").glob("batch-*.json"))
                )

        def test_youtube_prefers_orig_and_excludes_translations(self) -> None:
            choice = select_source_subtitle(
                {
                    "extractor_key": "Youtube",
                    "subtitles": {},
                    "automatic_captions": {
                        "zh-Hans": [{"ext": "vtt"}],
                        "en": [
                            {
                                "ext": "vtt",
                                "url": "https://example.invalid/caption?tlang=en",
                            }
                        ],
                        "en-orig": [
                            {"ext": "json3"},
                            {"ext": "vtt", "url": "https://example.invalid/caption"},
                        ],
                        "live_chat": [{"ext": "json"}],
                    },
                }
            )
            self.assertIsNotNone(choice)
            self.assertEqual(choice.language, "en-orig")
            self.assertEqual(choice.original_format, "vtt")

        def test_youtube_excludes_named_translated_track_without_tlang(self) -> None:
            info = {
                "extractor_key": "Youtube",
                "subtitles": {},
                "automatic_captions": {
                    "es": [{"ext": "vtt", "name": "Spanish from English"}],
                    "en": [{"ext": "vtt", "name": "English"}],
                },
            }
            self.assertEqual(
                [row["language"] for row in available_subtitle_summary(info)],
                ["en"],
            )

        def test_bilibili_style_ai_zh_language_is_excluded(self) -> None:
            info = {
                "extractor_key": "BiliBili",
                "subtitles": {
                    "ai-zh": [{"ext": "srt"}],
                    "en": [{"ext": "srt"}],
                },
            }
            self.assertEqual(
                [row["language"] for row in available_subtitle_summary(info)],
                ["en"],
            )

        def test_unique_orig_base_wins_over_manual_other_language(self) -> None:
            choice = select_source_subtitle(
                {
                    "extractor_key": "Generic",
                    "subtitles": {"fr": [{"ext": "srt"}]},
                    "automatic_captions": {"en-orig": [{"ext": "vtt"}]},
                }
            )
            self.assertEqual(choice.kind, "automatic")
            self.assertEqual(choice.language, "en-orig")

        def test_declared_original_language_wins_over_manual_translation(self) -> None:
            choice = select_source_subtitle(
                {
                    "extractor_key": "Generic",
                    "language": "ja",
                    "subtitles": {"en": [{"ext": "srt"}]},
                    "automatic_captions": {"ja": [{"ext": "vtt"}]},
                }
            )
            self.assertEqual(choice.language, "ja")

        def test_declared_language_without_matching_track_requires_override(self) -> None:
            info = {
                "extractor_key": "Generic",
                "language": "ja",
                "subtitles": {"en": [{"ext": "srt"}]},
            }
            with self.assertRaisesRegex(
                SubtitleSelectionError, "no matching subtitle track"
            ):
                select_source_subtitle(info)
            self.assertEqual(select_source_subtitle(info, "en").language, "en")

        def test_ambiguous_language_bases_require_override(self) -> None:
            with self.assertRaisesRegex(SubtitleSelectionError, "--source-lang"):
                select_source_subtitle(
                    {
                        "extractor_key": "Generic",
                        "subtitles": {
                            "fr": [{"ext": "srt"}],
                            "de": [{"ext": "srt"}],
                        },
                    }
                )

        def test_declared_chinese_source_does_not_select_translation(self) -> None:
            choice = select_source_subtitle(
                {
                    "extractor_key": "Generic",
                    "original_language": "zh-CN",
                    "subtitles": {"en": [{"ext": "srt"}]},
                }
            )
            self.assertIsNone(choice)

        def test_sole_remaining_language_base_is_selected(self) -> None:
            choice = select_source_subtitle(
                {
                    "extractor_key": "Generic",
                    "subtitles": {"en": [{"ext": "srt"}]},
                    "automatic_captions": {"en-US": [{"ext": "vtt"}]},
                }
            )
            self.assertEqual(choice.language, "en")
            self.assertEqual(choice.kind, "manual")

        def test_source_override_matches_orig_variant(self) -> None:
            choice = select_source_subtitle(
                {
                    "extractor_key": "Youtube",
                    "subtitles": {},
                    "automatic_captions": {"ja-orig": [{"ext": "vtt"}]},
                },
                "ja",
            )
            self.assertEqual(choice.language, "ja-orig")

        def test_diagnostics_redact_cookie_profile_and_url_query(self) -> None:
            clean = sanitize_diagnostic(
                "failed https://video.test/watch?v=x&token=secret chrome:Private Profile",
                ("chrome:Private Profile",),
            )
            self.assertNotIn("secret", clean)
            self.assertNotIn("Private Profile", clean)
            self.assertIn("https://video.test/…", clean)

        def test_diagnostics_redact_entire_credential_line_remainder(self) -> None:
            diagnostic = "\n".join(
                [
                    "Cookie: a=1; b=2; Path=/private",
                    "Set-Cookie: SESSDATA=abc; Secure; HttpOnly",
                    "Authorization: Bearer top secret value",
                    "Proxy-Authorization=Basic cHJveHk= trailing",
                    "Password = swordfish; next=leak",
                    "Token: abc; refresh=def",
                    "SESSDATA=xyz; bili_jct=still-secret",
                    "safe line remains",
                ]
            )
            clean = sanitize_diagnostic(diagnostic)
            for secret in (
                "a=1",
                "b=2",
                "/private",
                "abc",
                "Bearer",
                "top secret",
                "cHJveHk",
                "trailing",
                "swordfish",
                "next=leak",
                "refresh=def",
                "xyz",
                "bili_jct",
            ):
                self.assertNotIn(secret, clean)
            self.assertIn("safe line remains", clean)
            self.assertEqual(clean.count("<redacted>"), 7)

        def test_nonempty_output_requires_resume(self) -> None:
            prepare = globals().get("_prepare_output_dir")
            self.assertTrue(callable(prepare), "_prepare_output_dir is required")
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                (output / "existing.txt").write_text("existing", encoding="utf-8")
                with self.assertRaisesRegex(FetchError, "--resume"):
                    prepare(output, resume=False)
                self.assertEqual(prepare(output, resume=True), output)

        def test_resume_flag_is_in_parser_and_dry_run_plan(self) -> None:
            parser = build_parser()
            self.assertIn("resume", {action.dest for action in parser._actions})
            parsed = parser.parse_args(
                ["--resume", "--dry-run", "https://example.invalid/video"]
            )
            self.assertTrue(parsed.resume)
            self.assertTrue(_dry_run_plan(parsed)["resume"])

        def test_srt_source_is_derived_by_byte_copy_with_parent_hash(self) -> None:
            derive = globals().get("_derive_source_srt")
            self.assertTrue(callable(derive), "_derive_source_srt is required")
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original = root / "source-original.en.srt"
                target = root / "source-srt.en.srt"
                payload = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
                original.write_bytes(payload)
                expected_hash = _sha256(original)
                result, method, parent_hash = derive(
                    original, target, ffmpeg="unused", replace_existing=False
                )
                self.assertEqual(result, target)
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(method, "byte-copy")
                self.assertEqual(parent_hash, expected_hash)
                self.assertEqual(_sha256(original), expected_hash)

        def test_resume_mp4_does_not_trust_existing_output(self) -> None:
            self.assertIn(
                "replace_existing",
                inspect.signature(_atomic_ffmpeg_output).parameters,
            )
            false_executable = shutil.which("false")
            if false_executable is None:
                self.skipTest("false executable is unavailable")
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "input.mkv"
                output = root / "master.mp4"
                source.write_bytes(b"source")
                output.write_bytes(b"stale-master")
                result, error = _atomic_ffmpeg_output(
                    source,
                    output,
                    [],
                    false_executable,
                    replace_existing=True,
                )
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assertEqual(output.read_bytes(), b"stale-master")

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FetchVideoTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download one video's highest-quality streams and cover JPG, plus an original-language subtitle when available."
        )
    )
    parser.add_argument("url", nargs="?", help="A single http(s) video URL")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Artifact directory (default: current directory)",
    )
    parser.add_argument(
        "--browser-cookies",
        metavar="BROWSER[:PROFILE]",
        help=(
            "Pass this exact value to yt-dlp --cookies-from-browser, e.g. chrome, "
            "chrome:Default, or 'chrome:Profile 1'. Use 'auto' to try anonymously and "
            "silently retry Chrome only for authentication failures. No cookies.txt is created."
        ),
    )
    parser.add_argument(
        "--source-lang",
        help="Override original subtitle language selection, e.g. en, en-orig, ja, or ko",
    )
    parser.add_argument(
        "--allow-remote-ejs",
        action="store_true",
        help="Allow yt-dlp to fetch the EJS component from npm via Deno when required",
    )
    parser.add_argument(
        "--mp4-fallback",
        "--recode-mp4",
        dest="mp4_fallback",
        action="store_true",
        help="If lossless MP4 remux fails, explicitly allow a CRF 18 H.264/AAC fallback",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Allow an existing non-empty output directory; verify the intermediate and "
            "regenerate derived MP4/SRT artifacts"
        ),
    )
    light_group = parser.add_mutually_exclusive_group()
    light_group.add_argument(
        "--probe-only",
        action="store_true",
        help="Probe metadata/subtitles and write only download-manifest.json",
    )
    light_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print a redacted plan without network access or filesystem writes",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run lightweight stdlib tests without network access",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return 0 if run_self_tests() else 1
    if not args.url:
        parser.error("a video URL is required unless --self-test is used")
    try:
        result = execute(args)
        if result is not None and not args.probe_only:
            return _advance_bilingual_stage(result)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
