# OpenClaw Memory Architecture 调研笔记

基于对 [openclaw/openclaw](https://github.com/openclaw/openclaw) 源码的阅读，整理其跨 session 记忆系统的实现方式。

---

## 总体架构：三层记忆

```
┌─────────────────────────────────────────────────────┐
│                    新 LLM Session                     │
│                                                       │
│  System Prompt 注入:                                  │
│    ├─ IDENTITY.md  (AI 人设)                          │
│    ├─ USER.md      (用户画像/偏好)                    │
│    ├─ MEMORY.md    (精选长期记忆)                     │
│    └─ 时区/工具/技能 等动态片段                       │
│                                                       │
│  对话历史:                                            │
│    └─ 从 JSONL transcript 加载（可裁剪）               │
│                                                       │
│  按需召回:                                            │
│    └─ Agent 调用 memory_search 工具                   │
│        → SQLite + 向量搜索 + BM25 混合检索            │
└─────────────────────────────────────────────────────┘
```

### 第一层：Workspace 文件（始终注入）

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

关键源码：
- `src/agents/workspace.ts` — workspace 目录管理
- `src/agents/bootstrap-files.ts` — bootstrap 文件加载（上限 200KB）
- `src/agents/bootstrap-cache.ts` — 基于 inode+size+mtime 的缓存

### 第二层：Session 对话记录（JSONL）

位置：`~/.openclaw/agents/{agentId}/sessions/{sessionKey}.jsonl`

每条消息包含：
- `role` (user/assistant)
- `content` (文本/content blocks)
- `thinking` (如果开启了 thinking)
- `tool_calls` (工具调用记录)
- `usage` (token 统计)

Session 元数据存储在 `~/.openclaw/sessions.json`：
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

历史回顾工具：`sessions_history`（`src/agents/tools/sessions-history-tool.ts`）
- 查询 gateway 的 `chat.history` 方法
- 有 80KB 硬上限
- 会做脱敏（redact credentials, truncate large messages）

### 第三层：语义记忆搜索（RAG）

存储：每个 agent 一个 SQLite 数据库 `~/.openclaw/memory/{agentId}.sqlite`

核心模块：`src/memory/`

#### Schema（`memory-schema.ts`）

| 表 | 用途 |
|---|---|
| `meta` | 索引元数据 |
| `files` | 已索引文件列表 |
| `chunks` | 文本分块（~400 tokens/chunk, 80 tokens overlap） |
| `embedding_cache` | 向量缓存 |

#### Embedding 提供商（自动选择，按优先级）

1. **本地**：Ollama 或 node-llama-cpp（GGUF 模型）
2. **OpenAI**：`text-embedding-3-small`（默认远程）
3. **Gemini**：`gemini-embedding-001`
4. **Voyage**：`voyage-4-large`
5. **Mistral**：`mistral-embed`
6. **Ollama**：作为 fallback

#### 混合搜索策略

```
Query
  │
  ├─ Vector Search (70% 权重)
  │    语义相似度，能匹配同义表达
  │
  └─ BM25 / FTS5 (30% 权重)
       关键词精确匹配
  │
  ▼
Weighted Merge
  │
  ├─ MMR Re-ranking (可选)
  │    减少重复片段，增加多样性
  │
  └─ Temporal Decay (可选)
       新记忆权重更高，30 天半衰期
  │
  ▼
Top-K Results → 注入 LLM Context
```

Agent 可用的工具：
- `memory_search` — 对 `MEMORY.md` + `memory/**/*.md` 做语义搜索
- `memory_get` — 读取指定文件的特定行范围

---

## 关键机制：Context Window Guard

源码：`src/agents/context-window-guard.ts` + `src/agents/pi-extensions/context-pruning/`

### 流程

```
每轮对话后
  │
  ▼
检查 token 用量
  │
  ├─ 剩余 > softThreshold → 正常继续
  │
  └─ 剩余 ≤ softThreshold (~4K)
       │
       ▼
     Memory Flush（隐藏 agent turn）
       "请把重要信息写入 memory/YYYY-MM-DD.md"
       │
       ▼
     Context Pruning（可选，opt-in）
       删除旧消息释放空间
       │
       ▼
     Session Compaction
       压缩磁盘上的 transcript
```

### 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `contextWindow` | 从 model registry 读取 | 模型的 context 上限 |
| `agents.defaults.contextTokens` | - | 可手动设置上限 |
| `reserveTokensFloor` | 20K | 预留给回复的 token |
| `softThreshold` | ~4K | 触发 memory flush 的阈值 |

### Memory Flush（`src/auto-reply/reply/memory-flush.ts`）

这是最巧妙的部分：在 context 快满时，系统插入一个**隐藏的 agentic turn**，让 AI 自己决定把什么写入持久化记忆。相比事后从冷数据提取，AI 在对话中更清楚什么信息是重要的。

---

## 新 Session 的完整恢复流程

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
   ├─ Memory Recall 指令（告诉 AI 可以用 memory_search）
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
| Memory 管理器 | `src/memory/manager.ts` |
| Memory schema | `src/memory/memory-schema.ts` |
| Embedding 适配 | `src/memory/embeddings-*.ts` |
| Memory 工具 | `src/agents/tools/memory-tool.ts` |
| Memory 引用 | `src/agents/tools/memory-tool.citations.ts` |
| Session 历史工具 | `src/agents/tools/sessions-history-tool.ts` |
| Session transcript | `src/config/sessions/transcript.ts` |
| System prompt 构建 | `src/agents/system-prompt.ts` |
| 自动回复 prompt | `src/auto-reply/reply/commands-system-prompt.ts` |
| Context window guard | `src/agents/context-window-guard.ts` |
| Context pruning | `src/agents/pi-extensions/context-pruning/` |
| Memory flush | `src/auto-reply/reply/memory-flush.ts` |
| Workspace 管理 | `src/agents/workspace.ts` |
| Workspace 目录 | `src/agents/workspace-dirs.ts` |
| Bootstrap 文件 | `src/agents/bootstrap-files.ts` |
| Bootstrap 缓存 | `src/agents/bootstrap-cache.ts` |
| Session 管理 | `src/config/sessions/` |
| Session 回复 | `src/auto-reply/reply/session.ts` |

---

## 对 memory-extract 项目的启示

OpenClaw 的记忆系统有几个值得借鉴的设计：

1. **主动记忆 vs 被动提取**：OpenClaw 让 AI 在 context 快满时主动写记忆，而不是事后从冷数据提取。我们的 `extract.py` 是事后提取，质量上天然有差距。

2. **分层存储**：Workspace 文件（始终注入）+ JSONL（按需加载）+ 向量搜索（按需召回），三层各有分工。我们目前只做了最外层（生成 MEMORY.md）。

3. **混合搜索**：70% vector + 30% BM25 + 时间衰减 + MMR 去重，这套调参对召回质量很关键。如果后续 memory-extract 要做 RAG，可以参考这个比例。

4. **缓存和增量**：Bootstrap 文件用 inode+size+mtime 做变更检测，避免每次都重新加载。Memory 索引也是增量更新。

5. **200 行限制**：MEMORY.md 被截断到 200 行，所以内容必须精简、高信噪比。这也是我们 extract.py 设置 180 行上限的原因。
