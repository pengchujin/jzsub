# JZSub

一个面向 Codex 的视频下载与双语字幕 Skill。支持 YouTube、Bilibili 及其他 yt-dlp 平台，在用户有权访问内容的前提下下载最高可用画质、封面和原语言字幕，并生成中英/中外文对照字幕及硬字幕 MP4。

## 功能

- 使用 `bv*+ba/b` 下载最高可用质量，保留最高质量中间文件
- 尽可能无损封装 MP4，避免重复有损转码
- 下载并转换封面为 JPEG
- 原字幕按字节锁定，翻译过程不能修改原文
- 由当前 Codex 会话的默认 GPT 直接翻译，不启动 Ollama、MLX、llama.cpp 等本地模型
- 生成原文、简体中文、双语 SRT 和带样式 ASS
- 默认使用 MiSans Bold，原文在上、中文在下；背景由 libass 按实际字形、字号和换行精确测量，不再用字符数估算
- 自动消除滚动式自动字幕的显示时间重叠
- 使用 libass 将双语字幕一次性烧录为 H.264/AAC MP4
- macOS 默认 FFmpeg 缺少 libass 时自动选择 Homebrew `ffmpeg-full`
- 内置交付门槛：有源字幕时，翻译、渲染或烧录未完成都会返回未完成状态，不能把仅下载视频误报为成功
- 下载到源字幕后自动准备 GPT 翻译批次，并以 `bilingual_required`/退出码 3 强制流程继续；退出码 3 是非终态，不是下载失败
- 静默认证：匿名失败后可直接读取 Chrome 登录态，不导出 Cookie、不打开视频页
- 平台没有合适字幕时直接交付视频、MP4、封面和清单

## 安装

```bash
git clone https://github.com/pengchujin/download-bilingual-video-skill.git
mkdir -p ~/.codex/skills
cp -R download-bilingual-video-skill/skills/jzsub ~/.codex/skills/
```

然后在 Codex 中使用：

```text
$jzsub https://www.youtube.com/watch?v=VIDEO_ID
```

## 依赖

- Python 3.10+
- 当前版本的 `yt-dlp`
- 带 libass `subtitles` 滤镜的 FFmpeg 与 ffprobe
- Deno 2.3+（YouTube 完整提取能力）
- Chrome（仅在目标内容需要现有登录态时）
- MiSans Bold（字幕默认字体）

字幕翻译不需要安装本地大模型、模型运行时或额外翻译服务；由执行 Skill 的当前 Codex 默认 GPT 直接完成。只有用户明确指定其他翻译引擎时才会改用其他方案。

macOS + Homebrew 示例：

```bash
brew install yt-dlp ffmpeg-full deno
export PATH="/opt/homebrew/opt/ffmpeg-full/bin:$PATH"
```

MiSans 字体不包含在本仓库中。请从[小米 HyperOS 官方页面](https://hyperos.mi.com/font/zh/download/)下载并按页面许可安装 `MiSans-Bold.ttf`。本 Skill 生成的 ASS 会注明使用 MiSans。

## 静默 Chrome 认证

公开内容默认先匿名探测，仅在登录、反机器人或 HTTP 401/403 错误时静默读取 Chrome：

```bash
python3 skills/jzsub/scripts/fetch_video.py \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  --output-dir ~/Downloads/video-job \
  --browser-cookies auto
```

Bilibili 会员画质等已知需要登录的内容可直接使用：

```bash
python3 skills/jzsub/scripts/fetch_video.py \
  "https://www.bilibili.com/video/BV_ID" \
  --output-dir ~/Downloads/video-job \
  --browser-cookies chrome
```

该流程不会创建 `cookies.txt`，也不会把 Cookie 值写入日志或清单。只有需要用户重新登录或处理验证码时，才应打开浏览器进行交接。

## 验证

```bash
python3 skills/jzsub/scripts/fetch_video.py --self-test
python3 -m unittest discover \
  -s skills/jzsub/tests \
  -v
python3 skills/jzsub/scripts/verify_delivery.py \
  /path/to/video-job/download-manifest.json
```

## 安全与版权

- 只下载用户有权访问和保存的内容
- 不绕过 DRM、付费墙、验证码或平台安全限制
- 不包含、导出或提交浏览器 Cookie
- 不包含 MiSans 字体文件；字体受其官方许可约束

## License

本仓库中的 Skill 说明、脚本与测试采用 [MIT License](LICENSE)。第三方软件、平台内容和 MiSans 字体分别受其各自条款约束。
