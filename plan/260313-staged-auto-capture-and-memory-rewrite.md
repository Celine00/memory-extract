# 降频版自动采集：脚本预筛 + LLM 批量压缩 + MEMORY 润色

## Summary

- 把当前每次直接 `capture` 的流程拆成两阶段：
  - 便宜的脚本 ingest + 预筛
  - 低频的 LLM flush + 压缩
- `searchable` 和 `audit` 继续走脚本，保证确定性和低成本。
- `MEMORY.md` 改成基于 promoted facts 的可选 LLM 润色层。
- 批处理窗口默认采用：
  - `25 分钟静默` 后 flush
  - `90 分钟强制 flush` 作为长 session 上限
  - `launchd 每 5 分钟轮询` 作为自动触发器

## Why

- 当前昂贵的步骤只有 LLM 候选抽取。
- transcript 增量扫描、去重、事实聚合、promotion、本地审计都已经是脚本逻辑。
- 预筛先抓“可能有 durable 价值”的窗口，再批量送入 LLM，能显著降低调用频率。
- 最终 `MEMORY.md` 面向阅读体验，适合做一次轻量 LLM 改写；`facts` 和 `audit` 更适合保持结构化、可回放、可审计。

## Key Changes

- 新增 `ingest-and-filter`：
  - 扫描 Claude/Codex transcript 的新增消息
  - 运行高召回规则
  - 把命中的上下文窗口写入 `pending` 队列
  - 更新 ingest checkpoint
- 新增 `flush-pending`：
  - 检查 `pending` 队列是否满足静默窗口或强制 flush 条件
  - 只把候选窗口送入 LLM 抽取 candidate memory
  - 继续复用现有 event、facts、promotion 流程
- 新增 `rewrite-memory`：
  - promoted facts 变化时，基于 facts 调一次轻量 LLM
  - 生成更自然的 `MEMORY.md`
  - LLM 不可用时回退到确定性规则版
- 自动化入口：
  - Claude hook 和 Codex notify 只做 `ingest-and-filter`
  - `launchd` 定时器负责 `flush-pending`

## Data Shape

- 新增 `pending/{project_slug}.jsonl`
- 新增 `.state/{project_slug}.ingest.json`
- 新增 `.state/{project_slug}.flush.json`
- `pending` 记录至少包含：
  - `window_id`
  - `project_path`
  - `platform`
  - `session_file`
  - `message_ids`
  - `first_timestamp`
  - `last_timestamp`
  - `reason_codes`
  - `excerpt`
  - `status`

## Test Plan

- 普通 coding turn 不进入 `pending`
- 显式偏好或项目约束会进入 `pending`
- 同一波对话的相近窗口会合并或去重
- 25 分钟静默后会触发 LLM flush
- 长 session 超过 90 分钟会强制 flush
- `facts.jsonl`、archive、audit 继续确定性更新
- promoted facts 不变时跳过 `rewrite-memory`
- LLM 不可用时 `MEMORY.md` 回退到规则版

## Assumptions

- source of truth 仍然是 transcript 和 `facts.jsonl`
- `MEMORY.md` 是从 promoted facts 派生出的展示层
- 预筛阶段优先高召回，宁可多抓一些候选，不追求极低误报
- v1 先支持本机 repo-local 自动采集，不做团队级服务化
