# OpenClaw Memory Architecture

基于 [openclaw/openclaw](https://github.com/openclaw/openclaw) 源码的完整记忆系统架构文档。

内容同步日期：2026-03-13
双语拆分更新：2026-03-13

---

## 总体架构

OpenClaw 采用三层记忆架构加可选 QMD 后端，按「始终注入 -> 按需加载 -> 按需召回」分层：

```
┌───────────────────────────────────────────────────────────┐
│                     新 LLM Session                         │
│                                                            │
│  Layer 1 — Workspace 文件（始终注入 System Prompt）          │
│    ├─ IDENTITY.md   (AI 人设)                              │
│    ├─ USER.md       (用户画像/偏好)                         │
│    ├─ MEMORY.md     (精选长期记忆, <=200 行)                │
│    └─ 时区 / 工具 / 技能等动态片段                           │
│                                                            │
│  Layer 2 — Session 对话记录（JSONL, 可裁剪 / compact）       │
│    └─ ~/.openclaw/agents/{agentId}/sessions/*.jsonl        │
│                                                            │
│  Layer 3 — 语义记忆搜索（按需召回）                          │
│    ├─ Builtin: SQLite 索引，可运行在 hybrid / vector-only   │
│    │           / fts-only 三种形态                          │
│    └─ QMD:    外部后端, 3 种搜索模式, collection + scope     │
└───────────────────────────────────────────────────────────┘
```

补充说明：
- Layer 2 的 transcript 始终存在，但只有在 `memorySearch.experimental.sessionMemory=true` 且 `sources` 包含 `sessions` 时，才会被 Layer 3 主动索引。
- compaction 现在不只是“压缩历史”，还会触发统一的 post-compaction side effects：广播 transcript 更新，并按配置做 session memory 定向重建。

---

## Layer 1：Workspace 文件

位置：`~/.openclaw/workspace/`

| 文件 | 作用 | 加载时机 |
|------|------|----------|
| `IDENTITY.md` | AI 的身份与人格定义 | 每次 session 启动 |
| `USER.md` | 用户资料、偏好、时区 | 每次 session 启动 |
| `MEMORY.md` | 精选长期记忆，仅 direct chat 使用 | 每次 session 启动 |
| `BOOTSTRAP.md` | 一次性 onboarding 笔记 | 首次加载后缓存 |
| `SOUL.md` | 扩展人格定义，可选 | 每次 session 启动 |
| `TOOLS.md` | 工具文档 | 每次 session 启动 |
| `AGENTS.md` | 子 agent 定义 | 每次 session 启动 |
| `HEARTBEAT.md` | 定时任务逻辑 | 每次 session 启动 |
| `memory/YYYY-MM-DD.md` | 每日追加的记忆日志 | 始终被索引 |

**Bootstrap 加载**：`workspace.ts` 中的 workspace guarded read 上限为 2MB。注入 prompt 时，`bootstrap.ts` 和 `bootstrap-budget.ts` 还会再按 `20,000 chars/file` 和 `150,000 chars total` 做一轮 budget 和截断。缓存继续使用 `inode + size + mtime` 做变更检测，见 `bootstrap-cache.ts`。

**`MEMORY.md` 截断**：只有前 200 行会注入 system prompt，因此内容必须保持高信噪比。

关键源码：
- `src/agents/workspace.ts` - workspace 目录管理与 guarded file reading
- `src/agents/bootstrap-files.ts` - bootstrap 文件解析管线
- `src/agents/bootstrap-cache.ts` - `inode + size + mtime` 缓存

---

## Layer 2：Session 对话记录

位置：`~/.openclaw/agents/{agentId}/sessions/{sessionKey}.jsonl`

每条 JSONL 消息可能包含：
- `role`（`user` / `assistant`）
- `content`（文本或 content blocks）
- `thinking`（开启时）
- `tool_calls`（工具调用记录）
- `usage`（token 统计）

### Session 元数据

存储在 `~/.openclaw/sessions.json`：

```json
{
  "sessionKey": "string",
  "sessionId": "string",
  "channel": "string",
  "chatType": "direct | group | channel",
  "totalTokens": 12345,
  "totalTokensFresh": 10000,
  "compactionCount": 2,
  "memoryFlushCompactionCount": 1,
  "thinkingLevel": "low | high | xhigh"
}
```

### Session 文件处理（`session-files.ts`）

Session JSONL 会被规范化为可搜索文本：
- 只保留带 `role` 的消息对象（`user` / `assistant`）
- 从字符串或 `text[]` 结构中提取 `content`
- 折叠空白、去换行、脱敏
- 输出格式为 `"User: <text>\nAssistant: <text>"`

**是否进入语义索引**
- 默认 `sources = ["memory"]`，所以 transcript 只作为 Layer 2 历史，不会自动进入 Layer 3。
- 只有当 `memorySearch.experimental.sessionMemory = true` 且 `sources` 显式包含 `sessions` 时，Builtin / QMD 才会把 JSONL 产物索引为可检索 session memory。

### 历史回顾工具

`src/agents/tools/sessions-history-tool.ts` 中的 `sessions_history`：
- 调用 gateway 的 `chat.history` 方法
- 有 80KB 硬上限
- 会脱敏凭证并截断超大消息

---

## Layer 3：语义记忆搜索

OpenClaw 支持两种搜索后端，并且可以通过配置切换。

### Builtin 后端（默认）

每个 agent 对应一个 SQLite 数据库：`~/.openclaw/memory/{agentId}.sqlite`

Builtin 后端现在有 3 种实际运行形态：

| 形态 | 条件 | 行为 |
|------|------|------|
| `hybrid` | embedding provider 可用，且 FTS 可用 | 向量检索 + BM25 混合检索 |
| `vector-only` | embedding provider 可用，但 FTS 不可用 | 只做向量检索 |
| `fts-only` | 没有可用 embedding provider | 不做向量嵌入，退化为纯 FTS 搜索 |

#### Schema（`memory-schema.ts`）

| 表 | 用途 |
|---|---|
| `meta` | 索引元数据 |
| `files` | 已索引文件列表 |
| `chunks` | 文本分块（`400 tokens/chunk`，`80 tokens overlap`） |
| `embedding_cache` | 向量缓存 |
| `chunks_vec` | `sqlite-vec` 向量表 |
| `chunks_fts` | FTS5 文本索引表 |

`meta.memory_index_meta_v1` 会持久化当前索引参数，包括：
- `provider` / `model` / `providerKey`
- `sources`
- `scopeHash`
- `chunkTokens` / `chunkOverlap`
- `vectorDims`

#### 混合搜索策略

```
Query
  │
  ├─ Query Expansion（多语言关键词提取）
  │    → "原始查询 OR keyword1 OR keyword2 ..."
  │
  ├─ Vector Search（权重 0.7）
  │    语义相似度，适合匹配同义表达
  │
  └─ BM25 / FTS5（权重 0.3）
       关键词精确匹配
       FTS candidate multiplier: 4x
  │
  ▼
Weighted Merge（自动归一化到 sum=1）
  │
  ├─ MMR Re-ranking（可选，λ=0.7）
  │    用 Jaccard similarity 做迭代式多样性选择
  │
  └─ Temporal Decay（可选，半衰期 30 天）
       score × exp(-λ × ageInDays)
       Evergreen 文件不衰减（`MEMORY.md`、`memory.md`、非日期文件）
  │
  ▼
Top-K Results（默认 6 条，`minScore >= 0.35`）
  → 注入 LLM Context
```

#### 搜索参数

| 参数 | 默认值 |
|------|--------|
| `maxResults` | 6 |
| `minScore` | 0.35 |
| `chunkTokens` | 400 |
| `chunkOverlap` | 80 |
| `vectorWeight` | 0.7 |
| `textWeight` | 0.3 |
| `candidateMultiplier` | 4 |
| `mmrLambda` | 0.7（opt-in） |
| `temporalDecayHalfLife` | 30 天（opt-in） |

#### 增量同步与恢复

Builtin 索引的增量同步由 `manager-sync-ops.ts` 统一管理：

| 触发器 | 默认值 | 说明 |
|--------|--------|------|
| `sync.onSessionStart` | `true` | session 启动时预热 |
| `sync.onSearch` | `true` | 搜索前发现 dirty 时后台补同步 |
| `sync.watch` | `true` | watch `MEMORY.md` / `memory/**/*.md` |
| `sync.watchDebounceMs` | `1500ms` | 文件系统变更去抖 |
| `sync.sessions.deltaBytes` | `100000` | transcript 追加到这一阈值后重建 |
| `sync.sessions.deltaMessages` | `50` | transcript 追加到这一阈值后重建 |
| `sync.sessions.postCompactionForce` | `true` | compaction 后强制定向刷新该 session |

实现细节：
- memory 文件 watch 使用 `chokidar`，会忽略 `.git`、`node_modules`、`venv` 等目录。
- session transcript 通过 `emitSessionTranscriptUpdate()` 进入 debounce 队列，支持只重建指定 `sessionFiles`，不会误删无关 session 行。
- 全量重建优先走“临时库 + 原子替换”的 safe reindex；测试环境才会走 unsafe reindex。
- 如果 SQLite 连接进入 readonly/`SQLITE_READONLY` 状态，manager 会重开连接并重试一次。

### QMD 后端（可选）

QMD（Query, Memory, Docs）是一个外部搜索后端，通过 `mcporter` daemon 提供服务。

#### 配置（`backend-config.ts`）

```typescript
memory.backend = "qmd"  // 默认是 "builtin"
```

#### 三种搜索模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `search` | 快速轻量搜索 | CPU 受限系统，默认 |
| `vsearch` | 向量搜索 | GPU 或高性能环境 |
| `query` | 深度查询，带 expansion 和 reranking | 需要高精度召回的场景 |

默认模式已经改为 `search`，原因是 `query` 在 CPU-only 环境上太慢，不再适合作为交互式默认值。

#### Collection 管理

| 属性 | 默认值 |
|------|--------|
| Collection kinds | `memory`, `custom`, `sessions` |
| Memory collections | `MEMORY.md`, `memory.md`, `memory/**/*.md` |
| Update interval | 5 分钟 |
| Debounce | 15,000ms |
| Embedding interval | 60 分钟 |
| Max results | 6 |
| Max snippet chars | 700 |
| Max injected chars | 4,000 |

#### 超时设置

| 操作 | 超时 |
|------|------|
| Search | 4,000ms |
| Command | 30,000ms |
| Update | 120,000ms |
| Embed | 120,000ms |

#### Session Scope

- 默认只覆盖 direct 或 1-on-1 对话
- 支持把 session 导出到目录并配置保留策略

默认 scope 等价于：

```yaml
default: deny
rules:
  - action: allow
    match:
      chatType: direct
```

#### Session export

当 `memory.qmd.sessions.enabled = true` 时：
- QMD 会把 `~/.openclaw/agents/{agentId}/sessions/*.jsonl` 渲染为独立 `.md` 文件后再建 collection。
- 默认导出目录在 agent state 下的 `qmd/sessions/`，也可以显式配置 `exportDir`。
- `retentionDays` 生效时，过旧 transcript 不会再保留在导出目录中。

#### 平台与查询细节

- Windows CLI 启动现在走 `resolveWindowsSpawnProgram()` / `materializeWindowsSpawnProgram()`，不再强制 `.cmd/.bat` shim。
- `search` 模式遇到汉字查询时，会先做 BM25 关键词规整：丢弃单字 Han token，最多保留 12 个关键词，避免单字把 BM25 信号冲掉。
- 搜索前如果 QMD 正在做 update，读取路径会最多额外等待 `500ms`，避免“刚更新一半就查”的抖动。

### 搜索管理器（`search-manager.ts`）

Builtin 和 QMD 后端的统一入口：

```
memory.backend = "builtin"
  → MemoryIndexManager

memory.backend = "qmd"
  → QmdMemoryManager
  → FallbackMemoryManager wrapper
       ↓ primary.search() throw
       close primary
       evict QMD cache entry
       lazily create builtin fallback
```

接口：

```typescript
search(query, { maxResults?, minScore?, sessionKey? })
readFile({ relPath, from?, lines? })
status()
sync({ reason?, force?, sessionFiles?, progress? })
probeEmbeddingAvailability()
probeVectorAvailability()
```

关键行为：
- QMD manager 按 `agentId + ResolvedQmdConfig` 缓存；Builtin manager 按 `agentId + workspaceDir + resolved settings` 缓存。
- QMD 失败后，当前 wrapper 会切到 Builtin；同时把失败的 QMD cache entry 驱逐，所以下一次新请求仍然会重新尝试 fresh QMD manager。
- `status()` 不只是健康检查；fallback 后会把 `from: "qmd"` 和失败原因挂到状态里，便于 doctor/status 输出解释真实运行路径。

### Agent 可用的记忆工具

- `memory_search` - 对 `MEMORY.md` 和 `memory/**/*.md` 做语义搜索
- `memory_get` - 读取指定文件和特定行范围

---

## Embedding 提供商

自动选择顺序为 `local -> remote`。Ollama 必须显式 opt-in，不参与自动选择。

| 提供商 | 默认模型 | 类型 | Max Tokens |
|--------|---------|------|-----------|
| Local（`node-llama-cpp`） | `embeddinggemma-300m-qat-Q8_0.gguf` | 本地 GGUF | - |
| OpenAI | `text-embedding-3-small` | 远程 | 8,192 |
| Gemini | `gemini-embedding-001` | 远程 | 2,048 |
| Voyage | `voyage-4-large` | 远程 | ~32k |
| Mistral | `mistral-embed` | 远程 | - |
| Ollama | `nomic-embed-text`（`127.0.0.1:11434`） | 本地 | - |

**模型名前缀规范化**
- OpenAI：自动剥离 `openai/` 前缀
- Voyage：自动剥离 `voyage/` 前缀
- Mistral：自动剥离 `mistral/` 前缀
- Gemini：自动剥离 `models/`、`gemini/`、`google/` 前缀
- Ollama：自动剥离 `ollama/` 前缀
- Local：支持 `hf:`（HuggingFace）和 `https:`（直接 URL）前缀

---

## 多语言 Query Expansion

源码：`src/memory/query-expansion.ts`

### 支持的语言（7 种）

| 语言 | 分词策略 |
|------|----------|
| English（EN） | 按空白和标点切分 |
| Spanish（ES） | 按空白和标点切分 |
| Portuguese（PT） | 按空白和标点切分 |
| Arabic（AR） | 按空白和标点切分 |
| Chinese（ZH） | 单字 unigram 加双字 bigram |
| Korean（KO） | 韩文单词加去助词词干，15 种助词，longest-match-first |
| Japanese（JA） | 混合脚本提取，汉字、假名、ASCII 分开处理 |

### 工作流程

```
用户查询
  ↓
本地关键词提取
  ├─ 按语言选择分词器
  ├─ 过滤停用词（各语言独立停用词表）
  ├─ 过滤 1 到 2 个字母的英文词、纯数字、纯标点
  └─ 跨语言去重
  ↓
可选 LLM 扩展（如果可用）
  ├─ 语义同义词和相关词
  └─ 失败时 graceful fallback 到本地提取
  ↓
输出：{ original, keywords, expanded }
expanded = "原始查询 OR keyword1 OR keyword2 ..."
```

---

## Context Window Guard

源码：`src/agents/context-window-guard.ts`

### 硬限制

| 参数 | 值 | 说明 |
|------|-----|------|
| `CONTEXT_WINDOW_HARD_MIN_TOKENS` | 16,000 | 低于此值直接阻断 |
| `CONTEXT_WINDOW_WARN_BELOW_TOKENS` | 32,000 | 低于此值发出警告 |

### Context Window 解析优先级

1. models config context override
2. model advertised context window
3. agent config `contextTokens` cap
4. provider default

### Guard 输出

```typescript
{
  tokens: number;           // 解析后的 context window
  source: "model" | "modelsConfig" | "agentContextTokens" | "default";
  shouldWarn: boolean;      // tokens < 32,000
  shouldBlock: boolean;     // tokens < 16,000
}
```

---

## Memory Flush

源码：`src/auto-reply/reply/memory-flush.ts`

### 触发条件（3 种）

| 触发器 | 条件 | 说明 |
|--------|------|------|
| Token-based | `freshTotalTokens >= contextWindow - reserveTokensFloor(20,000) - softThreshold(4,000)` | 主触发器，优先使用实时 tokenCount |
| Byte-based | `transcript > 2MB` | force flush 兜底 |
| Compaction-based | 每个 compaction cycle 只触发一次 | 防止重复 flush |

### 关键行为

1. **隐藏 agentic turn**：系统插入一个隐藏轮次，让 AI 决定哪些信息应该持久化。
2. **Append-only**：记忆只能追加到现有文件，不能覆盖（`Issue #6877`）。
3. **只读保护**：`MEMORY.md`、`SOUL.md`、`TOOLS.md`、`AGENTS.md` 等 bootstrap/reference 文件在 flush turn 中被视为只读，禁止覆盖。
4. **禁止时间戳变体**：禁止创建 `YYYY-MM-DD-HHMM.md` 一类文件名，避免碎片化（`Issue #34919`）。
5. **日期替换**：目标路径里的 `YYYY-MM-DD` 会按用户时区解析成真实日期。
6. **静默选项**：AI 可以返回 `SILENT_REPLY_TOKEN` 表示“无需保存”；prompt / systemPrompt 若未包含该 token，会被自动补上。
7. **目标文件**：只写入 `memory/YYYY-MM-DD.md`。

### 完整流程

```
每轮对话后
  │
  ▼
检查 token 用量 / transcript 大小
  │
  ├─ 未达阈值 → 正常继续
  │
  └─ 达到阈值
       │
       ▼
     Memory Flush（隐藏 agent turn）
       "Please APPEND important information to memory/YYYY-MM-DD.md"
       "Do not overwrite bootstrap files"
       "Do not create variant filenames"
       │
       ▼
     Context Pruning（可选，opt-in）
       删除旧消息释放空间
       │
       ▼
     Session Compaction
       压缩磁盘上的 transcript
       │
       ▼
     Post-compaction side effects
       emitSessionTranscriptUpdate(sessionFile)
       + optional postIndexSync(off | async | await)
```

---

## 新 Session 恢复流程

```
 1. 收到新消息
      │
 2. 加载 SessionEntry（来自 sessions.json）
      │
 3. 加载 workspace 文件
    ├─ IDENTITY.md、USER.md、MEMORY.md（有缓存）
    └─ 变更检测：inode + size + mtime
      │
 4. 构建 system prompt（`src/agents/system-prompt.ts`）
    ├─ 身份信息（owner numbers、authorized senders）
    ├─ memory recall 指令（`memory_search` 可用）
    ├─ 当前时间（用户时区）
    ├─ 工具列表
    ├─ 技能（动态加载的 `SKILL.md`）
    ├─ 消息路由规则
    └─ 运行时信息（host、OS、arch、node version）
      │
 5. 加载历史 transcript（JSONL）
    └─ 可选的 context pruning 会裁剪旧轮次
      │
 6. 调用 LLM
      │
 7. Agent 可按需调用 `memory_search` 补充上下文
      │
 8. 把新轮次追加到 session JSONL
      │
 9. 更新 session 元数据（`totalTokens`、`compactionCount`）
      │
10. ContextEngine `afterTurn()` 或 fallback ingest 流程接手 turn 结束后的生命周期
      │
11. 如果接近 compaction 阈值，则先执行隐藏 memory flush turn
      │
12. 执行 session compaction
      │
13. 触发 post-compaction side effects
    ├─ `emitSessionTranscriptUpdate(sessionFile)`
    └─ 若 `sources` 包含 `sessions` 且 `postCompactionForce=true`
       则按 `postIndexSync = off | async | await` 做定向 session reindex
```

---

## 关键源码文件索引

| 组件 | 文件 |
|------|------|
| **Memory 核心** | |
| Memory 管理器 | `src/memory/manager.ts` |
| Memory 增量同步 | `src/memory/manager-sync-ops.ts` |
| Memory schema | `src/memory/memory-schema.ts` |
| Memory 搜索管理器 | `src/memory/search-manager.ts` |
| Memory 类型定义 | `src/memory/types.ts` |
| **搜索后端** | |
| QMD 管理器 | `src/memory/qmd-manager.ts` |
| QMD 进程启动 | `src/memory/qmd-process.ts` |
| QMD 后端配置 | `src/memory/backend-config.ts` |
| 混合搜索合并 | `src/memory/hybrid.ts` |
| MMR 重排序 | `src/memory/mmr.ts` |
| 时间衰减 | `src/memory/temporal-decay.ts` |
| Query Expansion | `src/memory/query-expansion.ts` |
| **Embedding** | |
| OpenAI embedding | `src/memory/embeddings-openai.ts` |
| Gemini embedding | `src/memory/embeddings-gemini.ts` |
| Voyage embedding | `src/memory/embeddings-voyage.ts` |
| Mistral embedding | `src/memory/embeddings-mistral.ts` |
| Ollama embedding | `src/memory/embeddings-ollama.ts` |
| Local embedding | `src/memory/node-llama.ts` |
| **Agent 集成** | |
| Memory 搜索配置 | `src/agents/memory-search.ts` |
| Memory 工具 | `src/agents/tools/memory-tool.ts` |
| Memory 引用 | `src/agents/tools/memory-tool.citations.ts` |
| Session 历史工具 | `src/agents/tools/sessions-history-tool.ts` |
| Session 文件处理 | `src/memory/session-files.ts` |
| **Prompt 与 Context** | |
| System prompt 构建 | `src/agents/system-prompt.ts` |
| 自动回复 prompt | `src/auto-reply/reply/commands-system-prompt.ts` |
| Context window guard | `src/agents/context-window-guard.ts` |
| Context pruning | `src/agents/pi-extensions/context-pruning/` |
| Memory flush | `src/auto-reply/reply/memory-flush.ts` |
| Compaction runner | `src/agents/pi-embedded-runner/compact.ts` |
| Turn afterTurn / compaction orchestration | `src/agents/pi-embedded-runner/run/attempt.ts` |
| **Workspace** | |
| Workspace 管理 | `src/agents/workspace.ts` |
| Workspace 目录 | `src/agents/workspace-dirs.ts` |
| Bootstrap 文件 | `src/agents/bootstrap-files.ts` |
| Bootstrap 缓存 | `src/agents/bootstrap-cache.ts` |
| Bootstrap budget | `src/agents/bootstrap-budget.ts` |
| **Session** | |
| Session 管理 | `src/config/sessions/` |
| Session transcript | `src/config/sessions/transcript.ts` |
| Session 回复 | `src/auto-reply/reply/session.ts` |
| Session 历史 | `src/auto-reply/reply/history.ts` |

---

## 关键常量汇总

| 组件 | 常量 | 值 |
|------|------|-----|
| Context Guard | `HARD_MIN_TOKENS` | 16,000 |
| Context Guard | `WARN_BELOW_TOKENS` | 32,000 |
| Memory Flush | `softThresholdTokens` | 4,000 |
| Memory Flush | `reserveTokensFloor` | 20,000 |
| Memory Flush | force flush | 2MB transcript |
| Search | `maxResults` | 6 |
| Search | `minScore` | 0.35 |
| Search | `chunkTokens / overlap` | 400 / 80 |
| Search Sync | `watchDebounceMs` | 1,500ms |
| Search Sync | `session deltaBytes / deltaMessages` | 100,000 / 50 |
| Search Sync | `postCompactionForce` | `true` |
| Hybrid | vector weight | 0.7 |
| Hybrid | text weight | 0.3 |
| Hybrid | candidate multiplier | 4 |
| MMR | lambda | 0.7 |
| Temporal Decay | half-life | 30 days |
| Compaction | `postIndexSync` default | `async` |
| QMD | search timeout | 4,000ms |
| QMD | default search mode | `search` |
| QMD | update interval | 5 min |
| QMD | embed interval | 60 min |
| QMD | debounce | 15,000ms |
| QMD | maxSnippetChars | 700 |
| QMD | maxInjectedChars | 4,000 |

---

## 对 `memory-extract` 项目的启示

1. **主动记忆 vs 被动提取**：OpenClaw 会在 context 快满时让 AI 主动写记忆，而且要求 append-only 并禁止文件名变体。`extract.py` 是事后从冷日志里提取，因此天然有质量差距。

2. **分层存储**：workspace 文件负责始终注入，JSONL transcript 负责按需加载，混合搜索负责按需召回。三层职责明确。当前 `memory-extract` 只覆盖了最外层，也就是生成 `MEMORY.md`。

3. **双后端架构**：Builtin 路径（`SQLite + vector + BM25`）适合本地部署，QMD 更适合高级检索需求，提供三种搜索模式和 collection 管理。后续可以考虑类似的分层搜索路径。

4. **混合检索调参**：70% vector 加 30% BM25，再叠加可选的 MMR（`λ = 0.7`）和可选的时间衰减（30 天半衰期）。这套生产参数可以作为 RAG 基线。

5. **多语言 Query Expansion**：支持 7 种语言的本地分词加可选的 LLM 扩展。CJK 语言各有专门策略，包括中文 unigram 加 bigram、韩文去助词、日文混合脚本提取。

6. **200 行限制**：只有 `MEMORY.md` 的前 200 行会进入 system prompt，所以这个文件必须很精简。这也是 `extract.py` 把上限设为 180 行的原因。

7. **缓存与增量更新**：bootstrap 文件用 `inode + size + mtime` 做变更检测；Builtin session memory 现在支持 `deltaBytes` / `deltaMessages` 增量刷新，并在 compaction 后做定向 transcript 重建；QMD 则用 debounce + fixed interval 控制 update/embed。

8. **Compaction 已经进入 memory 主路径**：最新实现把 compaction 前 memory flush、compaction 后 transcript update、以及可选的 post-index sync 串成一条完整生命周期链，而不是三个彼此独立的补丁点。
