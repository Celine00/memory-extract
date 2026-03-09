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

- 默认同时读取 `claude` 和 `codex` 两边的会话日志。
- 默认同时写回 `claude` 和 `codex`。
- 同一个 scope 只生成一份 canonical memory，再同步写到多个 target。
- 支持两种 scope:
  - `project`: 每个项目一份 memory
  - `global`: 跨项目共享的一份 memory
- `--output-dir` 不再替代原生写回，而是额外导出一份 canonical 副本，方便检查。

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

# 列出选定 source 下发现的项目
python3 extract.py --list-projects --source-platforms all
```

## 路径覆盖

如果你的本地目录结构和默认值不同，可以覆盖写回路径：

```bash
python3 extract.py \
  --codex-global-memory-path ~/.codex/memories/MEMORY.md \
  --codex-project-memory-template ~/.codex/memories/{project_slug}/MEMORY.md \
  --claude-global-memory-path ~/.claude/MEMORY.md
```

可用模板变量：
- `{project_slug}`: 适合文件名/目录名的项目 slug
- `{claude_encoded_project}`: Claude 风格的项目编码名
- `{project_path}`: 原始项目路径

## 环境要求

- Python 3.9+
- `anthropic` SDK (`pip install anthropic`)
- `ANTHROPIC_API_KEY` 环境变量

## 注意事项

- 对话记录可能包含敏感信息，脚本会做基础脱敏，但不能替代人工检查。
- Claude 项目级写回路径是已验证的默认路径。
- Claude 全局 memory 没有被脚本硬编码，只有显式配置路径时才会写回。
- Codex 默认写回 `~/.codex/memories/`，如果你的本机读取规则不同，请用覆盖参数修正。
- `MEMORY.md` 会被截断到 180 行以内，给平台的 200 行限制留出余量。
