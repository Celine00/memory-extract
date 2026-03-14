# Memory Extract

从 Claude Code 和 Codex 的本地对话记录中提取稳定的用户偏好，生成统一的 `MEMORY.md`，再按 scope 写回目标平台。

## 原理

```
Claude sessions   ~/.claude/projects/{project}/*.jsonl
Codex sessions    ~/.codex/sessions/YYYY/MM/DD/*.jsonl
Claude history    ~/.claude/history.jsonl          (global 补充信号)
        │
        ▼
   extract.py
     ├─ 解析多平台 session
     ├─ 清洗 scaffolding / tool noise
     ├─ 归一化成 canonical messages
     ├─ 按 scope 聚合 (project / global)
     └─ 生成一份 canonical MEMORY.md
        │
        ├─ 写回 Claude
        │    project: ~/.claude/projects/{project}/memory/MEMORY.md
        │    global:  仅在显式配置路径时写回
        │
        └─ 写回 Codex
             project: ~/.codex/memories/{project_slug}/MEMORY.md
             global:  ~/.codex/memories/MEMORY.md
```

## 当前行为

- 支持两种 memory mode:
  - `canonical`（默认）: 直接生成一份 `MEMORY.md`，可按平台写回
  - `layered`: 仅试点 `project` scope，写 repo-local 的 `raw -> searchable -> promoted MEMORY.md`
- 默认同时读取 `claude` 和 `codex` 两边的会话日志。
- 默认同时写回 `claude` 和 `codex`。
- 同一个 scope 只生成一份 canonical memory，再同步写到多个 target。
- 支持两种 scope:
  - `project`: 每个项目一份 memory
  - `global`: 跨项目共享的一份 memory
- `--output-dir` 不再替代原生写回，而是额外导出一份 canonical 副本，方便检查。
- `extract.py` 继续负责历史批处理和 canonical；持续增量的 layered runtime 现在在 `memory_promotion/` 包里。
- `layered` 现在支持两种执行方式：
  - `capture`：原有的一次性全流程
  - `ingest-and-filter -> flush-pending -> rewrite-memory`：低频 LLM 的 staged 流程
- prompt 模板现在集中放在 repo 根目录的 `prompts/`，调整提取文案时优先改这里。

## 数据清洗

### Claude
- 只保留 `type: "user"` 和 `type: "assistant"`。
- 丢弃 `progress`、`system`、snapshot 等非对话消息。
- 过滤本地命令 scaffolding、中断提示和纯 slash command 噪声，例如 `/mcp`、`<local-command-...>`。

### Codex
- 只保留 `response_item` 里的真实 `message` 项。
- 只保留 `role: user` 和 `role: assistant`。
- 丢弃 `event_msg`、`function_call`、`function_call_output`、reasoning/commentary 镜像。
- 自动去掉首轮注入的 `AGENTS.md instructions`、`<environment_context>`、permissions block。
- 遇到 `[SYSTEM] ... [USER]` 包装时，只保留真实 `[USER]` 内容。

## 提取策略

### Project memory
- 聚合同一项目下来自 Claude 和 Codex 的消息。
- 允许保留稳定的项目上下文、关键路径、项目专用工作流。

### Global memory
- 聚合所有项目的消息。
- Claude 的 `~/.claude/history.jsonl` 会作为额外的全局补充信号。
- 只保留跨项目稳定偏好，不应该把项目专属事实写进去。

## 使用

```bash
# 默认：读取 claude+codex，生成项目级 memory，并写回两个平台
python3 extract.py

# 只处理一个项目
python3 extract.py --scope project --project /Users/celinezou/Celine00/memory-extract

# 生成全局 memory
python3 extract.py --scope global

# 一次同时生成 project + global
python3 extract.py --scope both

# 只读 Codex，会写回 Codex 和 Claude
python3 extract.py --source-platforms codex --target-platforms codex,claude

# 干跑：只看会处理哪些 scope、会写到哪里
python3 extract.py --dry-run --scope both

# 额外导出 canonical 副本到本地目录，便于检查
python3 extract.py --output-dir ./output

# layered runtime：Codex + Claude，本地三层记忆
python3 -m memory_promotion.cli capture \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --llm-backend codex-cli

# staged ingest：只做脚本增量扫描和高召回预筛，不调 LLM
python3 -m memory_promotion.cli ingest-and-filter \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output

# staged flush：满足静默窗口或强制窗口后，才批量调一次 LLM
python3 -m memory_promotion.cli flush-pending \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --llm-backend codex-cli

# 仅重写最终 MEMORY.md
python3 -m memory_promotion.cli rewrite-memory \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --llm-backend codex-cli

# layered runtime：只扫 Codex
python3 -m memory_promotion.cli capture \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --source-platforms codex

# layered runtime：只扫 Claude
python3 -m memory_promotion.cli capture \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --source-platforms claude

# compatibility 入口：旧命令仍可用
python3 extract.py \
  --memory-mode layered \
  --scope project \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --llm-backend codex-cli

# pre-turn context：promoted memory + relevant recall
python3 -m memory_promotion.cli prepare-context \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --prompt "Please refactor the layered pipeline"

# 列出选定 source 下发现的项目
python3 extract.py --list-projects --source-platforms all
```

## 现在能怎么玩

### 1. 先看机器上有哪些项目有可提取的会话

```bash
python3 extract.py --list-projects --source-platforms all
```

适合先摸底。输出会按项目列出 `claude` / `codex` 各自发现了多少 session。

### 2. 用 canonical 模式直接生成或更新平台侧 `MEMORY.md`

```bash
# 项目级，直接写回 Claude/Codex 默认 memory 路径
python3 extract.py --scope project --project /Users/celinezou/Celine00/memory-extract

# 全局 memory
python3 extract.py --scope global
```

适合你已经接受“每次重扫后重写一份 `MEMORY.md`”的场景。

特点：
- 会聚合同一 scope 下的历史消息
- 调一次选定的 LLM backend，直接返回最终 `MEMORY.md`
- `project` 和 `global` 都支持
- 可以继续用 `--target-platforms` / memory path template 覆盖写回位置

### 3. 用 layered 模式做 repo-local 试点

```bash
python3 -m memory_promotion.cli capture \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --llm-backend codex-cli

# 只扫 Codex 会话
python3 -m memory_promotion.cli capture \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --source-platforms codex

# 只扫 Claude 会话
python3 -m memory_promotion.cli capture \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --source-platforms claude
```

适合你现在这个”先在一个 repo 里试试看”的需求。

特点：
- 默认同时扫描 Codex 和 Claude 会话（`--source-platforms codex,claude`）
- 可以用 `--source-platforms codex` 或 `--source-platforms claude` 限制只扫一边
- 只支持 `project` scope
- 不写回 Claude/Codex 原生 memory 路径
- 只写 `--output-dir` 下的 repo-local 试点产物
- 会保存增量 state，后续重跑默认只处理新增会话行
- 会把 accepted candidates 写进 `raw/YYYY-MM-DD.jsonl`
- 会把 consolidated facts 写进 `searchable/facts.jsonl`
- 会从 promoted facts 确定性重建 `MEMORY.md`
- 会生成 `audit/YYYY-MM-DD.md` 方便人工检查
- `extract.py --memory-mode layered ...` 仍然保留，但现在只是兼容入口

### 4. 用 staged flow 降低 LLM capture 频率

```bash
# 先把新 transcript 内容入队到 pending queue
python3 -m memory_promotion.cli ingest-and-filter \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output

# 再按静默窗口或强制窗口低频 flush
python3 -m memory_promotion.cli flush-pending \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --llm-backend codex-cli
```

特点：
- `ingest-and-filter` 只做脚本扫描、清洗、预筛、入队，不调 LLM
- `flush-pending` 只处理 `pending/queue.jsonl` 里的窗口
- 默认阈值是 `25` 分钟静默或 `90` 分钟强制 flush
- `rewrite-memory` 只基于 promoted facts 轻量改写 `MEMORY.md`
- `capture` 仍可继续使用，适合手动全量重跑

如果你只是想在当前 repo 快速跑一遍 layered 试点，可以直接用：

```bash
./scripts/run-layered-pilot
./scripts/run-layered-ingest
./scripts/run-layered-flush

# 常见变体
./scripts/run-layered-pilot --dry-run
LLM_BACKEND=claude-cli ./scripts/run-layered-pilot
LLM_BACKEND=codex-cli ./scripts/run-layered-pilot
OUTPUT_DIR=./pilot-output ./scripts/run-layered-pilot
PROJECT_PATH=/Users/celinezou/Celine00/other-repo ./scripts/run-layered-pilot
```

这个 wrapper 默认等价于：

```bash
python3 -m memory_promotion.cli capture \
  --project "$PWD" \
  --output-dir ./output \
  --llm-backend codex-cli
```

新的 staged wrappers 分别等价于：

```bash
python3 -m memory_promotion.cli ingest-and-filter \
  --project "$PWD" \
  --output-dir ./output

python3 -m memory_promotion.cli flush-pending \
  --project "$PWD" \
  --output-dir ./output \
  --llm-backend codex-cli
```

如果你想先观察 Codex hook 驱动的自动化效果，这个 repo 现在也带了本地试点入口：

```bash
# 查看或安装每 5 分钟轮询的 flush LaunchAgent
python3 scripts/launchd_memory_manager.py status
python3 scripts/launchd_memory_manager.py install

# 手动跑一次两个观察 repo 的 flush
./scripts/run-observed-repo-flushes
```

当前自动化试点的行为是：

- repo-local `.codex/notify.sh` 在 `agent-turn-complete` 后触发 `ingest-and-filter`
- LaunchAgent 每 5 分钟轮询一次 `flush-pending`
- 当前观察的 repo 有两个：
  - `memory-extract` 自己，输出仍在 `./output`
  - `pqs-pipeline-spark-jobs`，输出在 `./.codex/memory-extract-output`

如果你想单独构建 pre-turn 注入块，可以直接用：

```bash
python3 -m memory_promotion.cli prepare-context \
  --project "$PWD" \
  --output-dir ./output \
  --prompt "current user prompt here"
```

### 4. 先 dry-run，再决定是否真写

```bash
# canonical dry-run
python3 extract.py --dry-run --scope both

# layered dry-run
python3 -m memory_promotion.cli capture \
  --project /Users/celinezou/Celine00/memory-extract \
  --output-dir ./output \
  --dry-run
```

dry-run 会告诉你会处理哪些 scope、会发多大的 prompt、会写到哪里。

## 什么时候用哪个 mode

| 场景 | 推荐 mode | 原因 |
|------|-----------|------|
| 想直接产出平台会读的 `MEMORY.md` | `canonical` | 输出最直接，兼容现有路径和 CLI 参数 |
| 想在单 repo 内验证“raw -> searchable -> promoted MEMORY” | `layered` | 不污染平台原生路径，便于反复试验 |
| 想要 `global` memory | `canonical` | `layered` 当前不支持 |
| 想研究每天追加了什么 memory event / fact promotion | `layered` | 有 raw JSONL、searchable facts 和 audit |

## LLM backend 怎么选

现在支持四种取值：

```bash
--llm-backend auto
--llm-backend anthropic-api
--llm-backend claude-cli
--llm-backend codex-cli
```

行为如下：
- `auto`：默认值。优先使用 `anthropic-api`；如果当前 shell 没有可用的 `ANTHROPIC_API_KEY` 或没装 `anthropic` SDK，就回退到本机可用的 `claude` CLI，再不行回退到 `codex` CLI。
- `anthropic-api`：直接用 Python `anthropic` SDK 调 Anthropic API。
- `claude-cli`：调用本机 `claude --print` 做非交互抽取，不要求 `ANTHROPIC_API_KEY`。
- `codex-cli`：调用本机 `codex exec` 做非交互抽取，不要求 `ANTHROPIC_API_KEY`。

对新的 layered runtime，推荐默认用 `codex-cli` 作为 LLM backend。`claude-cli` 仍然可选。layered 模式现在默认同时扫描 `codex` 和 `claude` 两边的会话日志（`--source-platforms codex,claude`）。

如果你想强制指定 backend，可以这样跑：

```bash
python3 extract.py --scope project --project /abs/project/path --llm-backend claude-cli
python3 extract.py --scope project --project /abs/project/path --llm-backend codex-cli
python3 extract.py --scope project --project /abs/project/path --llm-backend anthropic-api
```

`--llm-model` 也支持，但只有你明确想覆盖 CLI 或 API 默认模型时才需要传。

## 跑完 layered 之后怎么看

建议按这个顺序看：

1. 先看 `project/{project_slug}/memory/raw/YYYY-MM-DD.jsonl`
   这里是 append-only `MemoryEvent`，最适合检查“这次新增了什么 accepted candidate”。
2. 再看 `project/{project_slug}/memory/searchable/facts.jsonl`
   这里是 consolidated working memory，最适合检查 searchable 层有没有把事实合并对。
3. 再看 `project/{project_slug}/memory/audit/YYYY-MM-DD.md`
   这里是给人看的日报，最适合快速 review promotion 质量。
4. 最后看 `project/{project_slug}/MEMORY.md`、`project/{project_slug}/MEMORY.deterministic.md`、`.state/{project_slug}.ingest.json`
   这里确认最终展示层、规则版 fallback 和 transcript checkpoint 是否都在正常推进。

一个健康的 layered 输出通常应该满足：
- 同一段 transcript window 重跑后不会重复追加 raw event。
- raw event 都带 evidence，且 evidence 能回指到具体 session 文件和 JSONL 行。
- searchable facts 会合并重复事实，但不会把所有事实都塞进 `MEMORY.md`。
- `MEMORY.md` 里主要是 always-worth-injecting 的稳定偏好和约束。
- 新增一两条会话后重跑，只会追加新的 raw event，并增量更新 searchable/audit。

## Repo-local Claude wrapper

如果你在这个 repo 里运行 `claude` 时遇到类似下面的全局 skill 报错：

```text
/Users/celinezou/.claude/skills/.../SKILL.md: missing YAML frontmatter delimited by ---
```

可以改用：

```bash
./scripts/claude-safe
```

这个 wrapper 会在当前 repo 内用 `--disable-slash-commands` 启动 Claude，避免加载 `~/.claude/skills` 里的坏 skill；不会修改你的全局 Claude skill 文件。

## 路径覆盖

`--memory-mode layered` 不使用 `--target-platforms` 或平台 memory template；
它只写 `--output-dir` 下的试点目录结构：

```text
output/
  .state/{project_slug}.json
  .state/{project_slug}.ingest.json
  .state/{project_slug}.flush.json
  project/{project_slug}/MEMORY.md
  project/{project_slug}/MEMORY.deterministic.md
  project/{project_slug}/memory/pending/queue.jsonl
  project/{project_slug}/memory/raw/YYYY-MM-DD.jsonl
  project/{project_slug}/memory/searchable/facts.jsonl
  project/{project_slug}/memory/searchable/archive/YYYY-MM-DD.jsonl
  project/{project_slug}/memory/audit/YYYY-MM-DD.md
```

其中：
- `.state/{project_slug}.json`：原有手动 `capture` 的兼容 state
- `.state/{project_slug}.ingest.json`：脚本 ingest checkpoint，记录每个 session 处理到哪一行、哪些 message 已看过
- `.state/{project_slug}.flush.json`：LLM flush 去重状态，记录哪些 event/candidate 已处理
- `memory/pending/queue.jsonl`：脚本预筛后的候选窗口队列
- `memory/raw/YYYY-MM-DD.jsonl`：append-only `MemoryEvent`
- `memory/searchable/facts.jsonl`：consolidated `SearchableFact`
- `memory/searchable/archive/YYYY-MM-DD.jsonl`：事实变化 ledger
- `memory/audit/YYYY-MM-DD.md`：人类可读的 daily review
- `MEMORY.deterministic.md`：只由 promoted facts 确定性重建的 fallback 版本
- `MEMORY.md`：基于 promoted facts 的最终展示层，可选地经一次轻量 LLM 改写

layered 模式的几个固定约束：
- 必须带 `--output-dir`
- 只支持 `--scope project`
- 候选抽取可以走 `anthropic-api`、`claude-cli` 或 `codex-cli`
- 提取器只负责写 raw candidates，不会直接写最终 `MEMORY.md`
- 最终 `MEMORY.md` 是从 searchable facts 的 promoted 子集本地重建出来的

如果你的本地目录结构和默认值不同，可以覆盖写回路径：

```bash
python3 extract.py \
  --codex-global-memory-path ~/.codex/memories/MEMORY.md \
  --codex-project-memory-template ~/.codex/memories/{project_slug}/MEMORY.md \
  --claude-global-memory-path ~/.claude/MEMORY.md
```

可用模板变量：
- `{project_slug}`: 适合文件名/目录名的项目 slug
  现在默认取项目路径最后两个目录名，例如 `/Users/celinezou/Celine00/memory-extract` -> `Celine00-memory-extract`
- `{claude_encoded_project}`: Claude 风格的项目编码名
- `{project_path}`: 原始项目路径

## 环境要求

- Python 3.9+
- 至少满足下面三种中的一种：
  - 安装 `anthropic` SDK，并设置 `ANTHROPIC_API_KEY`
  - 本机可直接运行 `claude`
  - 本机可直接运行 `codex`

### pyenv

这个 repo 的 `.python-version` 当前指向 `memory-extract`，如果你用 `pyenv`，先激活这个环境再装依赖：

```bash
pyenv activate memory-extract
python3 -m pip install anthropic
export ANTHROPIC_API_KEY=your_key_here
```

如果你准备走 `claude-cli` 或 `codex-cli` backend，这一步不一定要做；只有 `anthropic-api` backend 才要求装 `anthropic` SDK 并设置 `ANTHROPIC_API_KEY`。

可以先确认变量已经进了当前 shell：

```bash
printenv ANTHROPIC_API_KEY
```

如果你不走 `pyenv`，至少需要把 `anthropic` SDK 装到当前 `python3` 对应的环境里：

```bash
python3 -m pip install anthropic
export ANTHROPIC_API_KEY=your_key_here
```

注意：
- `export ANTHROPIC_API_KEY=...` 只对当前 shell 生效；开一个新终端需要重新设置，除非你把它放进自己的 shell 配置。
- 不要把真实 API key 写进 repo 文件、脚本或提交记录里。

### 是否一定要自己设 `ANTHROPIC_API_KEY`

不一定。

只有在你显式使用 `--llm-backend anthropic-api`，或者 `--llm-backend auto` 最终解析到 `anthropic-api` 时，才需要 `ANTHROPIC_API_KEY`。

如果你想直接复用本机 CLI 登录态，可以改用：

```bash
python3 extract.py --llm-backend claude-cli ...
python3 extract.py --llm-backend codex-cli ...
```

这两种模式不要求你自己设 `ANTHROPIC_API_KEY`。

### 设定 key 代表什么

设定 `ANTHROPIC_API_KEY` 代表这个脚本直接调用 Anthropic API。

这不等于在使用 Claude Code CLI。两者是分开的：
- `ANTHROPIC_API_KEY`：给 `extract.py` 里的 Python SDK 用
- `claude` CLI 登录态：给 Claude Code CLI 用
- `codex` CLI 登录态：给 Codex CLI 用

## 注意事项

- 对话记录可能包含敏感信息，脚本会做基础脱敏，但不能替代人工检查。
- Claude 项目级写回路径是已验证的默认路径。
- Claude 全局 memory 没有被脚本硬编码，只有显式配置路径时才会写回。
- Codex 默认写回 `~/.codex/memories/`，如果你的本机读取规则不同，请用覆盖参数修正。
- `MEMORY.md` 会被截断到 180 行以内，给平台的 200 行限制留出余量。
- layered 模式是 repo-local 试点，不负责让 Claude/Codex 自动读取 `./output/...` 下的 memory；它的目标是先验证增量记忆流程和产物质量。
