# Chrome authentication without cookie export

Use this reference only when anonymous extraction cannot access the requested quality, captions, or video.

## Supported boundary

Keep authentication headless by default. Let `yt-dlp` read the selected local Chrome profile directly; do not initialize the Chrome connection plugin or open the video page merely to obtain cookies. Do not inspect browser cookies, local storage, profiles, passwords, or session stores.

Pass the existing login state to `yt-dlp` locally:

```text
--cookies-from-browser BROWSER[+KEYRING][:PROFILE][::CONTAINER]
```

Chrome examples:

```text
chrome
chrome:Default
chrome:Profile 1
chrome:/absolute/path/to/a/profile
```

Use the most recently accessed Chrome profile with `chrome`. If extraction proves that the wrong account/profile was selected, ask the user for the visible Chrome profile name and retry with `chrome:<profile>`; do not enumerate or inspect profile contents.

## Procedure

1. For public content, run `fetch_video.py --browser-cookies auto`. It probes anonymously and retries with `chrome` only when the probe reports an authentication, anti-bot, or HTTP 401/403 failure.
2. For Bilibili member quality or content already known to require login, run `fetch_video.py --browser-cookies chrome` immediately. This is still headless and does not open a page.
3. Keep cookies inside `yt-dlp`; never combine `--cookies-from-browser` with `--cookies`, never create `cookies.txt`, and never print cookie values.
4. Load the Chrome control skill and open the supplied page only after direct cookie reading fails because Chrome is signed out, the wrong profile was selected, or the user must complete an interactive login/CAPTCHA. Keep that page only as a login handoff, then retry the same local profile.

## Platform cautions

- Prefer anonymous YouTube downloads when possible. The yt-dlp project warns that using an account can trigger temporary or permanent account restrictions; use account cookies only for content that actually requires them.
- Bilibili may require `SESSDATA` login state for member formats or CC subtitles. Let `yt-dlp` read it from the selected profile; do not extract it yourself.
- A normal Chrome profile read is not the same as yt-dlp's separate stable-incognito-cookie export procedure. Do not claim otherwise.
- Chrome login cookies do not solve YouTube PO Token enforcement. Follow the current official PO Token provider guidance only when the extractor explicitly reports that requirement.

## Common failures

- **Chrome connection unavailable:** This does not block silent `--cookies-from-browser`. Require the plugin only for an interactive login handoff.
- **Cookie database locked or decryption failed:** Close only the necessary Chrome profile if the user agrees, or retry after Chrome releases the database. Do not copy the database.
- **Wrong profile:** Ask for the user's profile name and pass it explicitly.
- **Fresh cookies still fail:** Re-probe anonymously, update yt-dlp, and check current extractor/EJS/PO Token guidance. Do not export cookies to debug.
