# Current yt-dlp platform notes

Use this reference for preflight and failure recovery. Recheck the official documentation before changing a workaround because extractor behavior, YouTube clients, PO Tokens, and runtime requirements change frequently.

## Quality and MP4

- Use `-f "bv*+ba/b"` for the highest available video plus audio selection.
- Use FFmpeg to merge separate streams.
- Treat `--merge-output-format` as a merge-container preference only.
- Treat `--remux-video mp4` as lossless container conversion; it fails when MP4 cannot hold the selected codecs.
- Treat `--recode-video mp4` as a lossy fallback. Avoid it before a later subtitle burn, which would cause two video encodes.
- Do not use yt-dlp's `-t mp4` preset for the maximum-quality source: it sorts toward H.264/AAC compatibility.

Official references:

- https://github.com/yt-dlp/yt-dlp/blob/master/README.md#format-selection
- https://github.com/yt-dlp/yt-dlp/blob/master/README.md#post-processing-options

## Burn-in dependency

Require an FFmpeg build with the `subtitles` filter backed by libass. Check it before a long download:

```bash
ffmpeg -hide_banner -filters
```

If `subtitles` is absent, keep the successfully downloaded video, cover, and validated SRT/ASS artifacts, but do not claim that the burned MP4 is complete. Install or select a libass-enabled FFmpeg build through the user's existing package-management method, then rerun only `burn_subtitles.py`. The burn step must also verify the sibling `validation.json` checksum for `bilingual.ass`.

## Subtitles and covers

- Use `--write-subs` for manual captions and `--write-auto-subs` for platform-provided automatic captions.
- Use `--sub-format "srt/best" --convert-subs srt` for the locked translation source.
- Use `--write-thumbnail --convert-thumbnails jpg` for a separate cover file.
- Use the YouTube extractor option `skip=translated_subs` so a platform-translated track is not mistaken for the original.
- Exclude `live_chat` and Bilibili `danmaku` from translation candidates.
- Probe the real subtitle list and honor an explicit `--source-lang`; never assume every foreign video is English.

Official references:

- https://github.com/yt-dlp/yt-dlp/blob/master/README.md#subtitle-options
- https://github.com/yt-dlp/yt-dlp/blob/master/README.md#thumbnail-options
- https://github.com/yt-dlp/yt-dlp/blob/master/README.md#extractor-arguments

## YouTube JavaScript and EJS

Current full YouTube support requires a supported external JavaScript runtime and yt-dlp EJS challenge scripts. Prefer Deno 2.3 or newer; Deno is enabled by default. Node 22 or newer requires `--js-runtimes node`.

The official standalone yt-dlp executables bundle EJS. Third-party packages such as Homebrew may or may not bundle it. When the installed package lacks EJS, prefer fixing that installation. If the user authorizes remote solver components and Deno is available, use the official `--remote-components ejs:npm` path rather than downloading arbitrary scripts.

Official reference: https://github.com/yt-dlp/yt-dlp/wiki/EJS

## YouTube cookies and PO Tokens

- Use account cookies only for content that needs authentication. The yt-dlp project warns that account use can result in temporary or permanent restrictions.
- Default extractor clients may omit some formats or subtitles as YouTube expands PO Token enforcement.
- Start with yt-dlp defaults. On an explicit PO Token failure, follow the current official provider-plugin guidance; do not cache or hand-extract a guessed token.
- Cookies and PO Tokens solve different requirements.

Official references:

- https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube
- https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide

## Bilibili

yt-dlp includes Bilibili video, series, favorites, space, watch-later, course, BiliIntl, and live extractors. Real availability can still break when the site changes.

- Use Chrome login state for member-only formats and login-only CC subtitles.
- Treat `danmaku` XML as comments, not source dialogue captions.
- Let the extractor convert Bilibili CC JSON to SRT.
- Verify actual formats and subtitles with the probe result; a listed extractor does not guarantee every URL currently works.

Official references:

- https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
- https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py
