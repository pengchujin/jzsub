# TODO

## Whisper 转写:无字幕视频生成字幕(未处理)

**背景**:agent 模型(GPT/Claude)都不能直接"听"音频生成带时间轴的字幕;Codex/Claude 的语音输入只是 UI 层听写。可行路径是编排本地带时间戳的 ASR:`ffmpeg 抽音轨 → Whisper → SRT → 现有字幕管线`。`subtitle_pipeline.py prepare` 接受任意 SRT,翻译/渲染/烧录零改动。

**方案**:

- [ ] 新增 `transcribe_audio.py`(或 `--transcribe` 开关):ffmpeg 抽音轨 → whisper-cpp / mlx-whisper / faster-whisper(按可用性探测)→ SRT
- [ ] **必须显式请求才启用**,保持 SKILL.md 现有防幻觉默认("Do not invent captions. Offer Whisper only when separately requested.")
- [ ] manifest 中转写字幕标 `kind: "asr"`(区别于 manual/automatic),进管线用 `smart` 分段
- [ ] 交付报告明确标注"字幕来自本地 Whisper 转写,非平台字幕"
- [ ] 依赖检查:whisper 实现探测与安装提示;不调用云端转写 API(与 Invariant 4/5 一致)

## Windows 登录态下载支持(未处理)

**背景**:Windows 上 Chrome 127+ 启用 App-Bound Encryption 后,yt-dlp 无法再直读 Chrome cookie 数据库;macOS 不受影响(Keychain 路线仍有效)。

**方案**(成本最低、不动隐私模型):

- [ ] 把 `fetch_video.py` 中 `auto` 模式静默回退硬编码的 `"chrome"` 改为可配置(如 `--auto-cookie-browser firefox`,默认仍为 chrome)
- [ ] `chrome-auth.md` / SKILL.md 补充一句:Windows + Chrome 127+ 请改用 Firefox 登录后以 `--browser-cookies firefox` 下载
- [ ] 自测补充:回退浏览器可配置且不接受控制字符

**明确不做**(会破坏 "agent 永不接触 cookie 值" 的设计支柱):

- CDP 远程调试口抓 cookie(`Network.getAllCookies`)
- 导出 cookies.txt(Invariant 5 禁止;YouTube 会 rotate 导出 cookie)
- 手动传单条 `SESSDATA` 经 agent 转交
