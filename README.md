# 懒人读书

> **为什么做这个项目**
>
> AI 时代，资讯信息量成倍增长，书籍更是买得多、读完的少。
> "懒人读书"的思路是：借助 LLM 把一本书**蒸馏**成浓缩的精华笔记，
> 快速抓住每章的核心论点和关键证据；哪里没看懂、哪里想再确认，
> 一键**跳回原书的具体页**去细读——先快览全局，再按需精读，
> 用更少的时间读更多有价值的书。

给定书名 + 作者，自动从 [Z-Library](https://z-lib.gd)（经
[zlib CLI](https://github.com/heartleo/zlib)）搜索并下载原文（EPUB / PDF），
再用 LLM 蒸馏成仿 [HowToLiveBetter](https://github.com/eternity4719/HowToLiveBetter)
风格的结构化读书笔记，输出到 `notes/<书名>/`（Markdown 章节笔记 + 单文件交互式网页 + 原文副本）。

> ⚠️ **法律风险提示**：Z-Library / Anna's Archive 属于盗版书库。本项目仅定位为**个人学习用途**的研究工具，
> 请勿用于公开分发、商业用途或大规模批量下载。下载行为受当地法律约束，请自行评估风险。

## 功能

- **搜索 + 下载**：封装 zlib CLI，按书名 + 作者搜书，多本候选时打印列表让你选序号；
  默认偏好 EPUB，`--format` 可指定偏好格式，没有偏好格式时用其唯一可用格式并提示。
- **文本提取**：EPUB 按 toc/spine 切章节；PDF 优先用大纲（`get_toc`）切章节，
  无大纲退化为按页数切块；同时按页拆分文本（PDF 专有），为逐条论据标注页码做准备。
- **LLM 蒸馏**（可插拔后端）：
  - **章节级**：每章抽出若干条论据，固定字段——标签、核心论点、关键证据（含原文直接引用）、
    反例或争议、适用边界、一句话总结；
  - **书级合并**：生成全书概览 + 按主题分组的导览 + 核心反复强调点。
- **论据 → 具体页码**（PDF 专有）：蒸馏时对逐页原文做 LLM 标注，把每条论据依据的页定位到
  PDF 物理页（±1-2 页精度）；EPUB 无页码概念，退化到章节起始页。
- **交互式 HTML**：蒸馏完自动生成单文件网页（数据内联，双击即可打开，无需服务器），
  仿 HowToLiveBetter 检索页——左侧章节目录、条目卡片可展开、顶部搜索、
  "只看有原文引用的" 筛选、全部展开/收起、明暗模式；每张卡片底部有
  **📄 原文 · 第 N 页** 按钮，点击全屏内嵌打开原文 PDF 并翻到该论据的具体页。

## 安装

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

## 配置

**最简单的方式：复制 `config.example` 为 `book-config`，填入真实值。**
`pipeline` 启动时自动读取项目根目录的 `book-config`（`KEY=VALUE` 每行一个，`#` 为注释）。
已设置的环境变量优先于 `book-config`，所以临时换模型直接 `--model` 覆盖即可，不用改文件。
`book-config` 已被 `.gitignore` 忽略（含真实 key，不会提交）。

蒸馏后端可插拔，支持 **Anthropic 官方 API** 和任意 **OpenAI 兼容接口**
（OpenAI / DeepSeek / Moonshot / 智谱 GLM / Ollama / vLLM 等，只要暴露 `/chat/completions`）。

OpenAI 兼容接口示例：
```
LLM_PROVIDER=openai
OPENAI_API_KEY=你的key
OPENAI_BASE_URL=接口地址（如 https://api.deepseek.com/v1）
DISTILL_MODEL=模型名
```

Anthropic 官方 API：
```
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

可用的键：
- `LLM_PROVIDER`：`anthropic`（默认）或 `openai`
- `DISTILL_MODEL`：模型名，缺省时按 provider 用各自默认值（Anthropic=`claude-sonnet-4-5`，OpenAI=`gpt-4o-mini`）
- `ZLIB_BIN`：zlib 可执行文件的完整路径（zlib 不在 PATH 里时填，如 `C:\Users\you\...\zlib.exe`）

## 前置：安装并登录 zlib CLI

在线搜索和下载走 [zlib CLI](https://github.com/heartleo/zlib)（Z-Library 客户端），需要一次性配置
（都在真实终端里做，登录是交互式的）：

```bash
# 1. 安装
winget install --id heartleo.zlib --exact

# 2. 找一个可达的 Z-Library 域名（结果会记到 ~/.config/zlib/.env）
zlib doctor

# 3. 登录（需要 Z-Library 账号；也可把浏览器登录后的 Cookie 导入 CLI）
zlib login
```

登录状态由 zlib 自己保存在 `~/.config/zlib/`，本项目不接触任何凭证。
zlib 需要代理时，把 `ZLIB_PROXY` 写进 `~/.config/zlib/.env`。

如果书已经下载好（或手动放进 `raw/`），可以跳过联网，用 `--skip-download` 离线蒸馏（见用法）。

## 用法

```bash
# 完整流程：搜索 -> 下载 -> 提取 -> 蒸馏（LLM 后端由 book-config 决定）
python -m src.pipeline --book "Deep Work" --author "Cal Newport"

# 临时换模型（覆盖 book-config 里的 DISTILL_MODEL）
python -m src.pipeline --book "Deep Work" --model 另一个模型名

# 指定偏好格式（EPUB 优先，PDF 兜底）
python -m src.pipeline --book "深度工作" --format pdf

# 离线蒸馏（文件已下载/已手动放进 raw/，跳过联网，只调 LLM）
python -m src.pipeline --book "书名" --author "作者" --skip-download --raw-file raw/<文件>.epub
```

单独重新生成网页（md 改过之后）：
```bash
python -m src.render "notes/<书名>"
```

流程中如果搜到多本候选书，会打印列表让你选序号。

## 输出格式

每本书一个 `notes/<书名>/` 目录：

- `README.md`：书级概览（2-4 句）+ 全书反复强调的核心 + 按主题分组的导览 + 章节目录链接
- `NN-<章节名>.md`：每章若干条目，固定字段：
  - 标签（核心观点 / 数据事实 / 方法建议 / 案例故事 / 背景知识 等 + 强度）
  - 核心论点
  - 关键证据（可含原文直接引用）
  - 反例或争议
  - 适用边界
  - 一句话总结
  - `原文页码：第 N 页`（**PDF 专有**：每条论据在原文里的具体页码，由 LLM 逐页标注得出，
    精确到 ±1-2 页；EPUB 无此字段，退化到章节起始页）
  - 末尾 `<!-- 来源: <章节名>, 第X页起 -->` 注释，便于人工回查原文
- `<书名>.html`：**单文件交互式网页**（蒸馏完自动生成，也可用 `src.render` 单独重跑），
  样式见上节"功能"。
- `<书名>.pdf` / `<书名>.epub`：原文文件副本（蒸馏完自动复制进书目录），让笔记文件夹
  自包含、"跳转到原文"按钮能定位到它。原文是 PDF 时按钮可精确翻到页；EPUB 只能到章节。
