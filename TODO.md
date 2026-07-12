# TODO

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
