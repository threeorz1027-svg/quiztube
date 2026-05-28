# QuizTube——将B站视频转换为高质量的康奈尔笔记和复习quiz

QuizTube 是一个面向学习场景的 AI 视频笔记与复习工具。  
你只需要粘贴 B 站视频链接，系统会自动完成字幕/转写、结构化康奈尔笔记生成，并进一步产出可循环复习的 Quiz 题目，帮助你把“看过”变成“记住”。

## 项目简介

QuizTube 聚焦三个核心目标：

- 将视频内容快速沉淀为可编辑、可保存的康奈尔笔记
- 将知识点自动转化为复习题，支持间隔重复训练
- 提供本地化可配置能力（LLM / ASR API 配置），降低使用门槛

## 主要能力

- **B 站链接一键生成笔记**：优先官方字幕，必要时 ASR 兜底
- **康奈尔笔记编辑器**：线索区 / 笔记区 / 总结区固定布局，支持富文本编辑
- **Quiz 复习系统**：自动生成题目、会话答题、错题解释与复习状态追踪
- **合集分P支持**：可选择多个分P合并生成单篇笔记
- **后台任务中心**：生成进度可视化与任务状态反馈
- **API 配置页面**：可在页面中配置 Chat / ASR 的 Base URL、Model、Key

## 技术栈

- **Backend**: FastAPI (Python)
- **Frontend**: HTML + Tailwind CSS + Vanilla JavaScript
- **AI**: OpenAI Compatible API（支持 MiMo / Ark / OpenAI 等）
- **媒体处理**: yt-dlp + ffmpeg

## 适用人群

- 需要高频复习技术视频/课程的学习者
- 想把视频内容沉淀成结构化知识卡片的创作者
- 希望快速搭建“视频转笔记 + 测验复习”流程的开发者

## 本地部署攻略

### 1) 环境准备

- 操作系统：macOS / Linux（Windows 建议使用 WSL）
- Python：`3.9+`（推荐 `3.10+`）
- 必备工具：
  - `ffmpeg`（音频切片）
  - `yt-dlp`（视频/字幕抓取）

macOS 可用 Homebrew 安装：

```bash
brew install ffmpeg yt-dlp
```

### 2) 拉取项目并安装依赖

```bash
git clone https://github.com/threeorz1027-svg/quiztube.git
cd quiztube
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3) 启动项目

```bash
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

浏览器打开：

- `http://127.0.0.1:8000`

### 4) 配置 API（推荐在页面里配置）

项目内置了 **API配置** 页面：

1. 打开左侧导航 `API配置`
2. 填写 Chat/ASR 的 `Base URL`、`Model`、`API Key`
3. 点击保存后立即生效

> 配置优先级：页面本地配置 > 系统环境变量

### 5) 可选：环境变量方式配置

如果你更习惯命令行，也可在启动前手动设置：

```bash
export MIMO_API_KEY="your_key"
export MIMO_BASE_URL="https://api.xiaomimimo.com/v1"
export MIMO_CHAT_MODEL="mimo-v2-flash"
export MIMO_TRANSCRIBE_MODEL="mimo-v2-omni"
```

### 6) 常见问题排查

- **页面打不开 / Connection refused**
  - 检查服务是否启动：`uvicorn ...`
  - 检查端口是否被占用（默认 `8000`）
- **ASR 报 Invalid API Key**
  - 到 `API配置` 页面确认 Key 是否正确保存
  - 避免多个无效环境变量覆盖
- **提示缺少 yt-dlp / ffmpeg**
  - 重新安装工具并确认 PATH 生效
- **B站视频抓不到字幕**
  - 可在配置里保持 ASR 兜底开启
