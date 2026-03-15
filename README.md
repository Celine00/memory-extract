# Memory Extract

从 Claude Code 和 Codex 的本地对话记录中提取稳定的用户偏好，生成统一的 `MEMORY.md`。

## 快速开始

```bash
# 查看有哪些可扫描的项目
python3 extract.py --list-projects --source-platforms all

# 单项目提取（layered 模式，推荐）
python3 -m memory_promotion.cli capture --project . --output-dir ./output

# 批量扫描所有项目
python3 -m memory_promotion.cli capture-all --output-dir ./output

# dry-run 先看不写
python3 -m memory_promotion.cli capture --project . --output-dir ./output --dry-run
python3 -m memory_promotion.cli capture-all --output-dir ./output --dry-run
```

快捷脚本（等价于上面的命令，支持环境变量覆盖）：

```bash
./scripts/run-layered-pilot           # capture 当前项目
./scripts/run-batch-capture           # capture-all 所有项目
./scripts/run-layered-ingest          # 只入队，不调 LLM
./scripts/run-layered-flush           # 按静默窗口 flush LLM
```

## 两种模式

| | Canonical | Layered（推荐） |
|---|-----------|----------------|
| 入口 | `python3 extract.py` | `python3 -m memory_promotion.cli` |
| Scope | project / global / both | project only |
| 写回位置 | Claude/Codex 原生 memory 路径 | 仅写 `--output-dir`，不碰原生路径 |
| 增量处理 | 否，每次全量 | 是，基于 session checkpoint |
| 批量扫描 | 否 | `capture-all` |
| 定时调度 | 否 | LaunchAgent 支持 |
| 适用场景 | 直接产出平台 MEMORY.md | 本地试验、审查提取质量 |

## Layered 命令一览

### capture — 单项目一次性全流程

```bash
python3 -m memory_promotion.cli capture \
  --project /path/to/project \
  --output-dir ./output \
  --llm-backend codex-cli         # 默认值
```

### capture-all — 批量扫描所有项目

自动发现 `~/.claude/projects/` 和 `~/.codex/` 下的所有项目，逐个增量处理。

```bash
python3 -m memory_promotion.cli capture-all \
  --output-dir ./output \
  --skip-if-recent 6              # 跳过最近 6 小时内处理过的项目
  --max-projects 10               # 限制本次最多处理 10 个，0=不限
```

### staged flow — 低频 LLM 调用

适合自动化场景，把"入队"和"LLM 提取"解耦：

```bash
# 1. 脚本入队（不调 LLM）
python3 -m memory_promotion.cli ingest-and-filter --project . --output-dir ./output

# 2. 满足静默窗口后 flush（调 LLM）
python3 -m memory_promotion.cli flush-pending --project . --output-dir ./output

# 3. 仅重写 MEMORY.md
python3 -m memory_promotion.cli rewrite-memory --project . --output-dir ./output
```

### prepare-context — 构建 pre-turn 注入块

```bash
python3 -m memory_promotion.cli prepare-context \
  --project . --output-dir ./output \
  --prompt "current user prompt here"
```

## Canonical 命令

```bash
python3 extract.py --scope project --project /path/to/project
python3 extract.py --scope global
python3 extract.py --scope both
python3 extract.py --dry-run --scope both
```

## 定时调度（macOS LaunchAgent）

```bash
# 安装每 12 小时跑一次的 batch capture
python3 scripts/launchd_memory_manager.py install-batch

# 安装每 5 分钟轮询一次的 staged flush
python3 scripts/launchd_memory_manager.py install

# 查看状态
python3 scripts/launchd_memory_manager.py status
python3 scripts/launchd_memory_manager.py status --label com.memoryextract.batch-capture

# 卸载
python3 scripts/launchd_memory_manager.py uninstall --label com.memoryextract.batch-capture
```

## LLM Backend

| Backend | 认证方式 | 说明 |
|---------|---------|------|
| `codex-cli`（默认） | Codex CLI 登录态 | 调用 `codex exec` |
| `claude-cli` | Claude CLI 登录态 | 调用 `claude --print` |
| `anthropic-api` | `ANTHROPIC_API_KEY` | Python SDK 直调 API |
| `auto` | 按优先级回退 | api → claude → codex |

用 `--llm-backend` 指定，用 `--llm-model` 覆盖默认模型。

## 输出结构（Layered）

```text
output/
  .state/{project_slug}.json              # capture checkpoint
  .state/{project_slug}.ingest.json       # ingest checkpoint
  .state/{project_slug}.flush.json        # flush 去重状态
  project/{project_slug}/
    MEMORY.md                             # 最终 curated memory
    MEMORY.deterministic.md               # 规则版 fallback
    memory/
      pending/queue.jsonl                 # 候选窗口队列
      raw/YYYY-MM-DD.jsonl               # append-only 原始事件
      searchable/facts.jsonl              # consolidated 事实
      searchable/archive/YYYY-MM-DD.jsonl # 事实变更记录
      audit/YYYY-MM-DD.md                # 人类可读日报
```

审查顺序建议：`raw/` → `searchable/facts.jsonl` → `audit/` → `MEMORY.md`

## 环境

- Python 3.9+
- `pyenv` virtualenv 名称：`memory-extract`

```bash
# 如果用 anthropic-api backend
pyenv activate memory-extract
pip install anthropic
export ANTHROPIC_API_KEY=your_key

# 如果用 claude-cli 或 codex-cli，无需 API key
```

## 数据清洗

- **Claude**: 保留 `type: "user"` 和 `type: "assistant"`，丢弃 progress/system/snapshot 等非对话消息，过滤 scaffolding 噪声。
- **Codex**: 保留 `role: user` 和 `role: assistant`，丢弃 function_call/reasoning 等，去掉首轮注入的 instructions。

## 测试

```bash
python3 -m unittest -q
```
