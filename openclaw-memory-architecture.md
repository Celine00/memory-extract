# OpenClaw Memory Architecture

基于 [openclaw/openclaw](https://github.com/openclaw/openclaw) 源码的完整记忆系统架构文档。
最后更新：2026-03-09

---

## 总体架构

OpenClaw 采用三层记忆架构 + 可选 QMD 后端，按「始终注入 → 按需加载 → 按需召回」分层：

```
┌───────────────────────────────────────────────────────────┐
│                     新 LLM Session                         │
│                                                            │
│  Layer 1 — Workspace 文件（始终注入 System Prompt）          │
│    ├─ IDENTITY.md   (AI 人设)                              │
│    ├─ USER.md       (用户画像/偏好)                         │
│    ├─ MEMORY.md     (精选长期记忆, ≤200 行)                 │
│    └─ 时区/工具/技能 等动态片段                              │
│                                                            │
│  Layer 2 — Session 对话记录（JSONL, 可裁剪/压缩）            │
│    └─ ~/.openclaw/agents/{agentId}/sessions/*.jsonl        │
│                                                            │
│  Layer 3 — 语义记忆搜索（按需召回）                          │
│    ├─ Builtin: SQLite + Vector + BM25 混合检索              │
│    └─ QMD:    外部后端, 3 种搜索模式, collection 管理        │
└───────────────────────────────────────────────────────────┘
```

---

## Layer 1：Workspace 文件

位置：`~/.openclaw/workspace/`

| 文件 | 作用 | 加载时机 |
|------|------|----------|
| `IDENTITY.md` | AI 的人设/性格定义 | 每次 session 启动 |
| `USER.md` | 用户个人资料、偏好、时区 | 每次 session 启动 |
| `MEMORY.md` | 精选的长期记忆（仅 direct chat） | 每次 session 启动 |
| `BOOTSTRAP.md` | 一次性 onboarding 笔记 | 首次加载后缓存 |
| `SOUL.md` | 扩展人格定义（可选） | 每次 session 启动 |
| `TOOLS.md` | 工具文档 | 每次 session 启动 |
| `AGENTS.md` | 子 agent 定义 | 每次 session 启动 |
| `HEARTBEAT.md` | 定时任务逻辑 | 每次 session 启动 |
| `memory/YYYY-MM-DD.md` | 每日追加的记忆日志 | 始终被索引 |

**Bootstrap 加载**：上限 200KB，使用 inode+size+mtime 缓存做变更检测（`bootstrap-cache.ts`）。
**MEMORY.md 截断**：仅前 200 行注入 system prompt，内容必须高信噪比。

关键源码：
- `src/agents/workspace.ts` — workspace 目录管理 + guarded file reading
- `src/agents/bootstrap-files.ts` — bootstrap 文件解析管线
- `src/agents/bootstrap-cache.ts` — inode+size+mtime 缓存

---

## Layer 2：Session 对话记录

位置：`~/.openclaw/agents/{agentId}/sessions/{sessionKey}.jsonl`

每条 JSONL 消息包含：
- `role` (user/assistant)
- `content` (文本/content blocks)
- `thinking` (如果开启)
- `tool_calls` (工具调用记录)
- `usage` (token 统计)

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

Session JSONL 会被规范化为可搜索的文本：
- 过滤有 role 的消息对象（user/assistant）
- 提取 content（string 或 text[] 格式）
- 折叠空白、去换行、脱敏
- 输出格式：`"User: <text>\nAssistant: <text>"`

### 历史回顾工具

`sessions_history`（`src/agents/tools/sessions-history-tool.ts`）：
- 查询 gateway 的 `chat.history` 方法
- 80KB 硬上限
- 脱敏处理（redact credentials, truncate large messages）

---

## Layer 3：语义记忆搜索

OpenClaw 支持两种搜索后端，可通过配置切换。

### Builtin 后端（默认）

每个 agent 一个 SQLite 数据库：`~/.openclaw/memory/{agentId}.sqlite`

#### Schema（`memory-schema.ts`）

| 表 | 用途 |
|---|---|
| `meta` | 索引元数据 |
| `files` | 已索引文件列表 |
| `chunks` | 文本分块（400 tokens/chunk, 80 tokens overlap） |
| `embedding_cache` | 向量缓存 |

#### 混合搜索策略

```
Query
  │
  ├─ Query Expansion（多语言关键词提取）
  │    → "原始查询 OR keyword1 OR keyword2 ..."
  │
  ├─ Vector Search (权重 0.7)
  │    语义相似度，能匹配同义表达
  │
  └─ BM25 / FTS5 (权重 0.3)
       关键词精确匹配
       FTS candidate multiplier: 4x
  │
  ▼
Weighted Merge（自动归一化至 sum=1）
  │
  ├─ MMR Re-ranking（可选, λ=0.7）
  │    Jaccard similarity, 迭代选择最大化多样性
  │
  └─ Temporal Decay（可选, 半衰期 30 天）
       score × exp(-λ × ageInDays)
       Evergreen 文件不衰减（MEMORY.md, memory.md, 非日期文件）
  │
  ▼
Top-K Results（默认 6 条, minScore ≥ 0.35）
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

### QMD 后端（可选）

QMD（Query, Memory, Docs）是一个外部搜索后端，通过 mcporter daemon 提供服务。

#### 配置（`backend-config.ts`）

```typescript
memory.backend = "qmd"  // 默认 "builtin"
```

#### 三种搜索模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `search` | 快速轻量搜索 | CPU-bound 系统（默认） |
| `vsearch` | 向量搜索 | GPU/高性能环境 |
| `query` | 深度查询 + expansion + reranking | 精确召回需求 |

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

- 默认仅 direct/1-on-1 对话
- 支持 session 导出到目录 + 保留策略

### 搜索管理器（`search-manager.ts`）

统一管理 Builtin 和 QMD 后端的入口：

```
search(query) → QMD primary → fallback Builtin
                      ↓ (failure)
              evict cache → switch to fallback
              status.from = "qmd", reason = error_message
```

接口：
```typescript
search(query, { maxResults?, minScore?, sessionKey? })
readFile({ relPath, from?, lines? })
status()
sync({ reason?, force?, progress? })
probeEmbeddingAvailability()
probeVectorAvailability()
```

### Agent 可用的记忆工具

- `memory_search` — 对 `MEMORY.md` + `memory/**/*.md` 做语义搜索
- `memory_get` — 读取指定文件的特定行范围

---

## Embedding 提供商

自动选择，按优先级 local → remote。Ollama 需显式 opt-in，不参与自动选择。

| 提供商 | 默认模型 | 类型 | Max Tokens |
|--------|---------|------|-----------|
| Local (node-llama-cpp) | `embeddinggemma-300m-qat-Q8_0.gguf` | 本地 GGUF | — |
| OpenAI | `text-embedding-3-small` | 远程 | 8,192 |
| Gemini | `gemini-embedding-001` | 远程 | 2,048 |
| Voyage | `voyage-4-large` | 远程 | ~32k |
| Mistral | `mistral-embed` | 远程 | — |
| Ollama | `nomic-embed-text` (127.0.0.1:11434) | 本地 | — |

**模型名前缀规范化**：
- OpenAI: `openai/` 前缀自动剥离
- Voyage: `voyage/` 前缀自动剥离
- Mistral: `mistral/` 前缀自动剥离
- Gemini: `models/`、`gemini/`、`google/` 前缀自动剥离
- Ollama: `ollama/` 前缀自动剥离
- Local: 支持 `hf:` (HuggingFace) 和 `https:` (直接 URL) 前缀

---

## 多语言 Query Expansion

源码：`src/memory/query-expansion.ts`

### 支持的语言（7 种）

| 语言 | 分词策略 |
|------|----------|
| English (EN) | 空白+标点分割 |
| Spanish (ES) | 空白+标点分割 |
| Portuguese (PT) | 空白+标点分割 |
| Arabic (AR) | 空白+标点分割 |
| Chinese (ZH) | 单字 unigram + 双字 bigram |
| Korean (KO) | 韩文单词 + 去助词词干 (15 种助词, longest-match-first) |
| Japanese (JA) | 混合脚本提取 (汉字/假名/ASCII 分别处理) |

### 工作流程

```
用户查询
  ↓
本地关键词提取
  ├─ 按语言选择分词器
  ├─ 过滤停用词（每种语言独立停用词表）
  ├─ 过滤 1-2 字母英文词、纯数字、纯标点
  └─ 跨语言去重
  ↓
可选 LLM 扩展（如果可用）
  ├─ 语义同义词/相关词
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
| `CONTEXT_WINDOW_HARD_MIN_TOKENS` | 16,000 | 低于此值阻断 |
| `CONTEXT_WINDOW_WARN_BELOW_TOKENS` | 32,000 | 低于此值警告 |

### Context Window 解析优先级

1. Models config context override
2. Model advertised context window
3. Agent config `contextTokens` cap
4. Provider default

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

### 触发条件（三种）

| 触发器 | 条件 | 说明 |
|--------|------|------|
| Token-based | 剩余 ≤ 4,000 tokens (softThreshold) | 主触发器 |
| Byte-based | transcript > 2MB | Force flush 备份机制 |
| Compaction-based | 每个 compaction cycle 仅触发一次 | 防重复 |

### 关键行为

1. **隐藏 agentic turn**：系统插入一个隐藏对话轮次，让 AI 主动决定持久化哪些信息
2. **Append-only**：只能追加到现有 memory 文件，不可覆盖（Issue #6877）
3. **禁止时间戳变体**：禁止创建 `YYYY-MM-DD-HHMM.md` 等变体文件，防止碎片化（Issue #34919）
4. **日期替换**：使用用户时区的实际日期替换 prompt 模板中的日期
5. **静默选项**：AI 可返回 SILENT_REPLY_TOKEN 表示"无需保存"
6. **目标文件**：仅写入 `memory/YYYY-MM-DD.md`

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
       "请把重要信息 APPEND 到 memory/YYYY-MM-DD.md"
       "禁止覆盖，禁止变体文件名"
       │
       ▼
     Context Pruning（可选，opt-in）
       删除旧消息释放空间
       │
       ▼
     Session Compaction
       压缩磁盘上的 transcript
```

---

## 新 Session 恢复流程

```
 1. 收到新消息
      │
 2. 加载 SessionEntry（从 sessions.json）
      │
 3. 加载 Workspace 文件
    ├─ IDENTITY.md, USER.md, MEMORY.md（有缓存）
    └─ 变更检测：inode + size + mtime
      │
 4. 构建 System Prompt（src/agents/system-prompt.ts）
    ├─ 身份信息（owner numbers, authorized senders）
    ├─ Memory Recall 指令（memory_search 可用）
    ├─ 当前时间（用户时区）
    ├─ 工具列表
    ├─ 技能（动态加载的 SKILL.md）
    ├─ 消息路由规则
    └─ 运行时信息（host, OS, arch, node version）
      │
 5. 加载历史 transcript（JSONL）
    └─ 可选：context pruning 裁剪旧轮次
      │
 6. 调用 LLM
      │
 7. Agent 可按需调用 memory_search 补充上下文
      │
 8. 追加新轮次到 session JSONL
      │
 9. 更新 session 元数据（totalTokens, compactionCount）
      │
10. 如果接近 compaction → 触发 memory flush
```

---

## 关键源码文件索引

| 组件 | 文件 |
|------|------|
| **Memory 核心** | |
| Memory 管理器 | `src/memory/manager.ts` |
| Memory schema | `src/memory/memory-schema.ts` |
| Memory 搜索管理器 | `src/memory/search-manager.ts` |
| Memory 类型定义 | `src/memory/types.ts` |
| **搜索后端** | |
| QMD 管理器 | `src/memory/qmd-manager.ts` |
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
| Local embedding | `src/memory/embeddings-local.ts` |
| **Agent 集成** | |
| Memory 搜索配置 | `src/agents/memory-search.ts` |
| Memory 工具 | `src/agents/tools/memory-tool.ts` |
| Memory 引用 | `src/agents/tools/memory-tool.citations.ts` |
| Session 历史工具 | `src/agents/tools/sessions-history-tool.ts` |
| Session 文件处理 | `src/memory/session-files.ts` |
| **Prompt & Context** | |
| System prompt 构建 | `src/agents/system-prompt.ts` |
| 自动回复 prompt | `src/auto-reply/reply/commands-system-prompt.ts` |
| Context window guard | `src/agents/context-window-guard.ts` |
| Context pruning | `src/agents/pi-extensions/context-pruning/` |
| Memory flush | `src/auto-reply/reply/memory-flush.ts` |
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
| Context Guard | HARD_MIN_TOKENS | 16,000 |
| Context Guard | WARN_BELOW_TOKENS | 32,000 |
| Memory Flush | softThreshold | 4,000 tokens |
| Memory Flush | force flush | 2MB transcript |
| Search | maxResults | 6 |
| Search | minScore | 0.35 |
| Search | chunkTokens / overlap | 400 / 80 |
| Hybrid | vector weight | 0.7 |
| Hybrid | text weight | 0.3 |
| Hybrid | candidate multiplier | 4 |
| MMR | lambda | 0.7 |
| Temporal Decay | half-life | 30 days |
| QMD | search timeout | 4,000ms |
| QMD | update interval | 5 min |
| QMD | embed interval | 60 min |
| QMD | debounce | 15,000ms |
| QMD | maxSnippetChars | 700 |
| QMD | maxInjectedChars | 4,000 |

---

## 对 memory-extract 项目的启示

1. **主动记忆 vs 被动提取**：OpenClaw 让 AI 在 context 快满时主动写记忆（append-only, 禁止变体文件），而不是事后从冷数据提取。`extract.py` 是事后提取，质量上天然有差距。

2. **分层存储**：Workspace 文件（始终注入）+ JSONL（按需加载）+ 混合搜索（按需召回），三层各有分工。目前 memory-extract 只做了最外层（生成 MEMORY.md）。

3. **双后端架构**：Builtin（SQLite + vector + BM25）适合本地部署，QMD 适合高级检索需求（3 种搜索模式 + collection 管理）。后续可考虑实现类似的分层搜索。

4. **混合搜索调参**：70% vector + 30% BM25 + 可选 MMR (λ=0.7) + 可选时间衰减 (30 天半衰期)。这套参数经过生产验证，可作为 RAG 基线。

5. **多语言 Query Expansion**：支持 7 种语言的本地分词 + 可选 LLM 扩展。CJK 语言各有专用分词策略（中文 unigram+bigram, 韩文去助词, 日文混合脚本）。

6. **200 行限制**：MEMORY.md 被截断到 200 行注入 system prompt，内容必须精简。这也是 extract.py 设置 180 行上限的原因。

7. **缓存和增量**：Bootstrap 文件用 inode+size+mtime 做变更检测，Memory 索引增量更新，QMD 用 debounce+interval 控制更新频率。
