# 🤖 RSS AI Daily Summarizer

这是一个基于 **GitHub Actions + OpenRouter (Gemini)** 的自动化 RSS 摘要生成器。它能够监控指定的 RSS 订阅源，提取新文章，通过 AI 生成精简的中文摘要，并重新封装成一个新的 RSS 订阅源供阅读器（如 Reeder, NetNewsWire）使用。

## ✨ 核心特性

*   **增量更新 (Incremental Processing)**: 使用 `processed.txt` 记录已处理的条目，避免重复调用 AI 和信息爆炸。
*   **AI 智能摘要**: 调用 OpenRouter 接口（推荐使用 `google/gemini-2.0-flash-001`），将多条新闻合并为一份精炼的中文简报。
*   **工程化准则**:
    *   **极简主义**: 仅使用 Python 标准库，无需安装 `requests` 或 `feedparser`。
    *   **Token 优化**: 自动剥离 HTML 标签，仅发送纯文本以节省输入费用。
    *   **Git-as-DB**: 利用 GitHub Actions 运行并自动将状态回传仓库，实现“零服务器”维护。
*   **标准兼容**: 生成符合 RSS 2.0 规范的 XML 文件，可直接通过 GitHub Raw 链接订阅。

## 📂 项目结构

```text
.
├── summarize.py           # 核心处理脚本
├── processed.txt          # 状态记录文件（已处理的链接）
├── summary_feed.xml       # 生成的摘要 RSS 文件
├── example-rss-feed       # 示例源文件（可替换为远程 URL）
└── .github/workflows/
    └── rss-summary.yml    # GitHub Actions 定时任务配置
```

## 🚀 快速开始

### 1. 配置 GitHub Secrets
在你的 GitHub 仓库中，进入 `Settings -> Secrets and variables -> Actions`，点击 `New repository secret` 添加以下机密：
*   **`OPENROUTER_API_KEY`**: 你的 OpenRouter API 密钥（必填）。
*   **`RSS_SOURCE`**: 你想要监控的远程 RSS URL（例如 `https://9to5mac.com/feed/`）。如果不配置，默认使用示例源。
*   **`AI_MODEL`**: 指定使用的 AI 模型（例如 `google/gemini-2.0-flash-001`）。如果不配置，默认使用该模型。

### 2. 订阅你的摘要
一旦 GitHub Actions 运行成功（**每天北京时间 07:35 运行**，或在 Actions 页面手动触发），你可以在以下地址找到你的摘要源：
`https://raw.githubusercontent.com/{你的用户名}/{仓库名}/main/summary_feed.xml`

## 🛠 技术细节

*   **唯一标识**: 每个摘要条目的 link 使用 `https://github.com/liusonwood/rss_ai_summarise#{运行时间戳}`，确保阅读器能识别为新内容。
*   **熔断机制**: 每次运行最多处理 28 条新文章（MAX_ITEMS），防止订阅源突发大规模更新导致的高额 API 费用。
*   **内容清洗**: 发送给 LLM 前会截断过长内容，确保在 context window 内获得最高质量的摘要。

## ⚖️ License
MIT
