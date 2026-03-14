# OpenClaw Memory Architecture

Complete memory-system architecture notes based on the [openclaw/openclaw](https://github.com/openclaw/openclaw) codebase.

Codebase sync date: 2026-03-13
Bilingual split updated: 2026-03-13

---

## Overall Architecture

OpenClaw uses a three-layer memory architecture with an optional QMD backend. The layers are organized as "always injected -> load on demand -> recall on demand":

```
┌───────────────────────────────────────────────────────────┐
│                      New LLM Session                       │
│                                                           │
│  Layer 1 — Workspace Files (always in system prompt)      │
│    ├─ IDENTITY.md   (AI identity / persona)               │
│    ├─ USER.md       (user profile / preferences)          │
│    ├─ MEMORY.md     (curated long-term memory, <=200 lines)│
│    └─ timezone / tools / skills and other dynamic blocks  │
│                                                           │
│  Layer 2 — Session Transcript (JSONL, prunable/compact)   │
│    └─ ~/.openclaw/agents/{agentId}/sessions/*.jsonl       │
│                                                           │
│  Layer 3 — Semantic Memory Search (recall on demand)      │
│    ├─ Builtin: SQLite index running as hybrid /           │
│    │           vector-only / fts-only                     │
│    └─ QMD:    External backend, 3 search modes,           │
│               collections + scope                         │
└───────────────────────────────────────────────────────────┘
```

Additional notes:
- Layer 2 transcripts always exist, but they are only indexed into Layer 3 when `memorySearch.experimental.sessionMemory=true` and `sources` includes `sessions`.
- Compaction is now part of the memory lifecycle: after compaction, OpenClaw emits a transcript update and can run a targeted session-memory reindex.

---

## Layer 1: Workspace Files

Location: `~/.openclaw/workspace/`

| File | Purpose | Load timing |
|------|---------|-------------|
| `IDENTITY.md` | AI identity and persona definition | Every session start |
| `USER.md` | User profile, preferences, timezone | Every session start |
| `MEMORY.md` | Curated long-term memory, direct chat only | Every session start |
| `BOOTSTRAP.md` | One-time onboarding notes | Cached after first load |
| `SOUL.md` | Extended persona definition, optional | Every session start |
| `TOOLS.md` | Tool documentation | Every session start |
| `AGENTS.md` | Sub-agent definitions | Every session start |
| `HEARTBEAT.md` | Scheduled task logic | Every session start |
| `memory/YYYY-MM-DD.md` | Daily append-only memory logs | Always indexed |

**Bootstrap loading**: workspace guarded reads are capped at 2MB in `workspace.ts`. Prompt injection then applies a second budget and truncation pass at `20,000 chars/file` and `150,000 chars total` in `bootstrap.ts` and `bootstrap-budget.ts`. The cache still uses `inode + size + mtime` change detection in `bootstrap-cache.ts`.

**`MEMORY.md` truncation**: only the first 200 lines are injected into the system prompt, so the file must stay high-signal.

Key source files:
- `src/agents/workspace.ts` - workspace directory management and guarded file reading
- `src/agents/bootstrap-files.ts` - bootstrap file parsing pipeline
- `src/agents/bootstrap-cache.ts` - `inode + size + mtime` cache

---

## Layer 2: Session Transcript

Location: `~/.openclaw/agents/{agentId}/sessions/{sessionKey}.jsonl`

Each JSONL message can include:
- `role` (`user` / `assistant`)
- `content` (text or content blocks)
- `thinking` (when enabled)
- `tool_calls` (tool invocation records)
- `usage` (token accounting)

### Session Metadata

Stored in `~/.openclaw/sessions.json`:

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

### Session File Processing (`session-files.ts`)

Session JSONL is normalized into searchable text:
- keep only message objects with a `role` (`user` / `assistant`)
- extract `content` from either string or `text[]` formats
- collapse whitespace, strip newlines, and redact sensitive text
- emit the searchable form as `"User: <text>\nAssistant: <text>"`

**When transcripts enter semantic search**
- Default `sources = ["memory"]`, so transcripts remain a Layer 2 history source only.
- Transcripts are indexed only when `memorySearch.experimental.sessionMemory = true` and `sources` explicitly includes `sessions`.

### History Review Tool

`sessions_history` in `src/agents/tools/sessions-history-tool.ts`:
- calls the gateway `chat.history` method
- enforces an 80KB hard limit
- redacts credentials and truncates large messages

---

## Layer 3: Semantic Memory Search

OpenClaw supports two search backends and can switch between them by configuration.

### Builtin Backend (default)

Each agent gets one SQLite database: `~/.openclaw/memory/{agentId}.sqlite`

The builtin backend now has 3 real operating modes:

| Mode | Condition | Behavior |
|------|-----------|----------|
| `hybrid` | embedding provider available and FTS available | vector + BM25 hybrid retrieval |
| `vector-only` | embedding provider available, FTS unavailable | vector search only |
| `fts-only` | no embedding provider available | pure FTS search without embeddings |

#### Schema (`memory-schema.ts`)

| Table | Purpose |
|---|---|
| `meta` | Index metadata |
| `files` | Indexed file list |
| `chunks` | Text chunks (`400 tokens/chunk`, `80 tokens overlap`) |
| `embedding_cache` | Vector cache |
| `chunks_vec` | `sqlite-vec` virtual table |
| `chunks_fts` | FTS5 text index |

`meta.memory_index_meta_v1` persists the active index contract:
- `provider` / `model` / `providerKey`
- `sources`
- `scopeHash`
- `chunkTokens` / `chunkOverlap`
- `vectorDims`

#### Hybrid Search Strategy

```
Query
  │
  ├─ Query Expansion (multilingual keyword extraction)
  │    → "original query OR keyword1 OR keyword2 ..."
  │
  ├─ Vector Search (weight 0.7)
  │    Semantic similarity, good for paraphrases
  │
  └─ BM25 / FTS5 (weight 0.3)
       Exact keyword matching
       FTS candidate multiplier: 4x
  │
  ▼
Weighted Merge (auto-normalized to sum=1)
  │
  ├─ MMR Re-ranking (optional, λ=0.7)
  │    Jaccard similarity with iterative diversity selection
  │
  └─ Temporal Decay (optional, half-life 30 days)
       score × exp(-λ × ageInDays)
       Evergreen files do not decay (`MEMORY.md`, `memory.md`, non-dated files)
  │
  ▼
Top-K Results (default 6, minScore ≥ 0.35)
  → injected into LLM context
```

#### Search Parameters

| Parameter | Default |
|------|--------|
| `maxResults` | 6 |
| `minScore` | 0.35 |
| `chunkTokens` | 400 |
| `chunkOverlap` | 80 |
| `vectorWeight` | 0.7 |
| `textWeight` | 0.3 |
| `candidateMultiplier` | 4 |
| `mmrLambda` | 0.7 (opt-in) |
| `temporalDecayHalfLife` | 30 days (opt-in) |

#### Incremental Sync and Recovery

Builtin indexing is orchestrated by `manager-sync-ops.ts`:

| Trigger | Default | Meaning |
|---------|---------|---------|
| `sync.onSessionStart` | `true` | warm on session start |
| `sync.onSearch` | `true` | background catch-up before/around search when dirty |
| `sync.watch` | `true` | watch `MEMORY.md` / `memory/**/*.md` |
| `sync.watchDebounceMs` | `1500ms` | filesystem debounce |
| `sync.sessions.deltaBytes` | `100000` | reindex transcript after enough appended bytes |
| `sync.sessions.deltaMessages` | `50` | reindex transcript after enough appended JSONL lines |
| `sync.sessions.postCompactionForce` | `true` | force targeted session refresh after compaction |

Implementation details:
- memory file watch uses `chokidar` and ignores `.git`, `node_modules`, `venv`, and similar directories.
- session transcript updates flow through `emitSessionTranscriptUpdate()` and can target specific `sessionFiles` without pruning unrelated session rows.
- full rebuild prefers safe reindexing via a temporary DB and atomic swap; unsafe reindexing is test-only.
- readonly SQLite handles trigger one reopen-and-retry recovery path.

### QMD Backend (optional)

QMD (Query, Memory, Docs) is an external search backend served through the `mcporter` daemon.

#### Configuration (`backend-config.ts`)

```typescript
memory.backend = "qmd"  // default is "builtin"
```

#### Three Search Modes

| Mode | Description | Best for |
|------|-------------|----------|
| `search` | Fast lightweight search | CPU-bound systems, default |
| `vsearch` | Vector search | GPU or high-performance environments |
| `query` | Deep query with expansion and reranking | Precision recall needs |

The default mode is now `search`; `query` is no longer the default because it is too slow on CPU-only systems for interactive use.

#### Collection Management

| Property | Default |
|------|--------|
| Collection kinds | `memory`, `custom`, `sessions` |
| Memory collections | `MEMORY.md`, `memory.md`, `memory/**/*.md` |
| Update interval | 5 minutes |
| Debounce | 15,000ms |
| Embedding interval | 60 minutes |
| Max results | 6 |
| Max snippet chars | 700 |
| Max injected chars | 4,000 |

#### Timeout Settings

| Operation | Timeout |
|------|------|
| Search | 4,000ms |
| Command | 30,000ms |
| Update | 120,000ms |
| Embed | 120,000ms |

#### Session Scope

- default scope is direct or 1-on-1 chat only
- supports session export to directory plus retention rules

The default scope is effectively:

```yaml
default: deny
rules:
  - action: allow
    match:
      chatType: direct
```

#### Session Export

When `memory.qmd.sessions.enabled = true`:
- QMD renders `~/.openclaw/agents/{agentId}/sessions/*.jsonl` into `.md` files before indexing them as a collection.
- the default export directory lives under the agent state dir at `qmd/sessions/`, unless `exportDir` is configured.
- `retentionDays` prunes old exported session markdown files from that directory.

#### Platform and Query Details

- Windows CLI launch now goes through `resolveWindowsSpawnProgram()` / `materializeWindowsSpawnProgram()` and no longer forces `.cmd/.bat` shims.
- Han-script queries in `search` mode are normalized for BM25: single-character Han tokens are dropped and the keyword list is capped at 12 items.
- searches wait up to `500ms` for a pending QMD update before reading, which reduces mid-update jitter.

### Search Manager (`search-manager.ts`)

Unified entry point for both the builtin and QMD backends:

```
memory.backend = "builtin"
  → MemoryIndexManager

memory.backend = "qmd"
  → QmdMemoryManager
  → FallbackMemoryManager wrapper
       ↓ primary.search() throws
       close primary
       evict QMD cache entry
       lazily create builtin fallback
```

API:

```typescript
search(query, { maxResults?, minScore?, sessionKey? })
readFile({ relPath, from?, lines? })
status()
sync({ reason?, force?, sessionFiles?, progress? })
probeEmbeddingAvailability()
probeVectorAvailability()
```

Key behavior:
- QMD managers are cached by `agentId + ResolvedQmdConfig`; builtin managers are cached by `agentId + workspaceDir + resolved settings`.
- After a QMD failure, the current wrapper switches to builtin and evicts the failed QMD cache entry, so the next fresh request can try QMD again.
- `status()` carries fallback metadata (`from: "qmd"` + failure reason) so doctor/status output can explain the real runtime path.

### Memory Tools Available to the Agent

- `memory_search` - semantic search over `MEMORY.md` and `memory/**/*.md`
- `memory_get` - read a specific file and line range

---

## Embedding Providers

Providers are auto-selected in priority order `local -> remote`. Ollama is opt-in only and is not part of automatic selection.

| Provider | Default model | Type | Max Tokens |
|--------|---------|------|-----------|
| Local (`node-llama-cpp`) | `embeddinggemma-300m-qat-Q8_0.gguf` | Local GGUF | - |
| OpenAI | `text-embedding-3-small` | Remote | 8,192 |
| Gemini | `gemini-embedding-001` | Remote | 2,048 |
| Voyage | `voyage-4-large` | Remote | ~32k |
| Mistral | `mistral-embed` | Remote | - |
| Ollama | `nomic-embed-text` (`127.0.0.1:11434`) | Local | - |

**Model prefix normalization**
- OpenAI: strip the `openai/` prefix automatically
- Voyage: strip the `voyage/` prefix automatically
- Mistral: strip the `mistral/` prefix automatically
- Gemini: strip `models/`, `gemini/`, and `google/` prefixes automatically
- Ollama: strip the `ollama/` prefix automatically
- Local: supports `hf:` (HuggingFace) and `https:` (direct URL) prefixes

---

## Multilingual Query Expansion

Source: `src/memory/query-expansion.ts`

### Supported Languages (7)

| Language | Tokenization strategy |
|------|----------|
| English (EN) | split on whitespace and punctuation |
| Spanish (ES) | split on whitespace and punctuation |
| Portuguese (PT) | split on whitespace and punctuation |
| Arabic (AR) | split on whitespace and punctuation |
| Chinese (ZH) | unigram plus bigram characters |
| Korean (KO) | Korean words plus particle stripping stemmer, 15 particles, longest-match-first |
| Japanese (JA) | mixed-script extraction, handling kanji, kana, and ASCII separately |

### Flow

```
User query
  ↓
Local keyword extraction
  ├─ choose tokenizer by language
  ├─ remove stop words (language-specific stop lists)
  ├─ remove 1-2 letter English words, pure numbers, pure punctuation
  └─ dedupe across languages
  ↓
Optional LLM expansion (when available)
  ├─ semantic synonyms and related terms
  └─ graceful fallback to local extraction on failure
  ↓
Output: { original, keywords, expanded }
expanded = "original query OR keyword1 OR keyword2 ..."
```

---

## Context Window Guard

Source: `src/agents/context-window-guard.ts`

### Hard Limits

| Parameter | Value | Meaning |
|------|-----|------|
| `CONTEXT_WINDOW_HARD_MIN_TOKENS` | 16,000 | block below this value |
| `CONTEXT_WINDOW_WARN_BELOW_TOKENS` | 32,000 | warn below this value |

### Context Window Resolution Priority

1. models config context override
2. model-advertised context window
3. agent config `contextTokens` cap
4. provider default

### Guard Output

```typescript
{
  tokens: number;           // resolved context window
  source: "model" | "modelsConfig" | "agentContextTokens" | "default";
  shouldWarn: boolean;      // tokens < 32,000
  shouldBlock: boolean;     // tokens < 16,000
}
```

---

## Memory Flush

Source: `src/auto-reply/reply/memory-flush.ts`

### Triggers (3)

| Trigger | Condition | Notes |
|--------|------|------|
| Token-based | `freshTotalTokens >= contextWindow - reserveTokensFloor(20,000) - softThreshold(4,000)` | primary trigger, prefers live tokenCount |
| Byte-based | transcript > 2MB | force-flush backup |
| Compaction-based | only once per compaction cycle | prevents duplicate flushes |

### Key Behavior

1. **Hidden agentic turn**: the system inserts a hidden turn so the AI can decide what should be persisted.
2. **Append-only**: memory can only be appended to existing files and cannot overwrite them (`Issue #6877`).
3. **Read-only bootstrap files**: `MEMORY.md`, `SOUL.md`, `TOOLS.md`, `AGENTS.md`, and similar bootstrap/reference files are treated as read-only during the flush turn.
4. **No timestamp variants**: filenames like `YYYY-MM-DD-HHMM.md` are forbidden to avoid fragmentation (`Issue #34919`).
5. **Date substitution**: `YYYY-MM-DD` is resolved in the user's timezone before the turn runs.
6. **Silent option**: the AI can return `SILENT_REPLY_TOKEN`; prompt/systemPrompt text gets the token hint auto-added if missing.
7. **Target file**: only `memory/YYYY-MM-DD.md` may be written.

### Full Flow

```
After each turn
  │
  ▼
Check token usage / transcript size
  │
  ├─ below threshold → continue normally
  │
  └─ threshold reached
       │
       ▼
     Memory Flush (hidden agent turn)
       "Please APPEND important information to memory/YYYY-MM-DD.md"
       "Do not overwrite bootstrap files"
       "Do not create variant filenames"
       │
       ▼
     Context Pruning (optional, opt-in)
       remove older messages to free space
       │
       ▼
     Session Compaction
       compress transcript on disk
       │
       ▼
     Post-compaction side effects
       emitSessionTranscriptUpdate(sessionFile)
       + optional postIndexSync(off | async | await)
```

---

## New Session Recovery Flow

```
 1. Receive a new message
      │
 2. Load SessionEntry (from sessions.json)
      │
 3. Load workspace files
    ├─ IDENTITY.md, USER.md, MEMORY.md (with cache)
    └─ change detection: inode + size + mtime
      │
 4. Build system prompt (`src/agents/system-prompt.ts`)
    ├─ identity info (owner numbers, authorized senders)
    ├─ memory recall instructions (`memory_search` available)
    ├─ current time (user timezone)
    ├─ tool list
    ├─ skills (dynamically loaded `SKILL.md`)
    ├─ message routing rules
    └─ runtime info (host, OS, arch, node version)
      │
 5. Load historical transcript (JSONL)
    └─ optional context pruning trims old turns
      │
 6. Call the LLM
      │
 7. The agent can call `memory_search` on demand for more context
      │
 8. Append the new turn to session JSONL
      │
 9. Update session metadata (`totalTokens`, `compactionCount`)
      │
10. ContextEngine `afterTurn()` or fallback ingest takes over the end-of-turn lifecycle
      │
11. If close to the compaction threshold, run the hidden memory-flush turn first
      │
12. Execute session compaction
      │
13. Trigger post-compaction side effects
    ├─ `emitSessionTranscriptUpdate(sessionFile)`
    └─ if `sources` includes `sessions` and `postCompactionForce=true`,
       run a targeted session reindex via `postIndexSync = off | async | await`
```

---

## Key Source File Index

| Component | File |
|------|------|
| **Memory core** | |
| Memory manager | `src/memory/manager.ts` |
| Memory sync ops | `src/memory/manager-sync-ops.ts` |
| Memory schema | `src/memory/memory-schema.ts` |
| Memory search manager | `src/memory/search-manager.ts` |
| Memory type definitions | `src/memory/types.ts` |
| **Search backends** | |
| QMD manager | `src/memory/qmd-manager.ts` |
| QMD process spawning | `src/memory/qmd-process.ts` |
| QMD backend config | `src/memory/backend-config.ts` |
| Hybrid search merge | `src/memory/hybrid.ts` |
| MMR reranking | `src/memory/mmr.ts` |
| Temporal decay | `src/memory/temporal-decay.ts` |
| Query expansion | `src/memory/query-expansion.ts` |
| **Embedding** | |
| OpenAI embedding | `src/memory/embeddings-openai.ts` |
| Gemini embedding | `src/memory/embeddings-gemini.ts` |
| Voyage embedding | `src/memory/embeddings-voyage.ts` |
| Mistral embedding | `src/memory/embeddings-mistral.ts` |
| Ollama embedding | `src/memory/embeddings-ollama.ts` |
| Local embedding | `src/memory/node-llama.ts` |
| **Agent integration** | |
| Memory search config | `src/agents/memory-search.ts` |
| Memory tools | `src/agents/tools/memory-tool.ts` |
| Memory citations | `src/agents/tools/memory-tool.citations.ts` |
| Session history tool | `src/agents/tools/sessions-history-tool.ts` |
| Session file processing | `src/memory/session-files.ts` |
| **Prompt and context** | |
| System prompt builder | `src/agents/system-prompt.ts` |
| Auto-reply prompt | `src/auto-reply/reply/commands-system-prompt.ts` |
| Context window guard | `src/agents/context-window-guard.ts` |
| Context pruning | `src/agents/pi-extensions/context-pruning/` |
| Memory flush | `src/auto-reply/reply/memory-flush.ts` |
| Compaction runner | `src/agents/pi-embedded-runner/compact.ts` |
| Turn afterTurn / compaction orchestration | `src/agents/pi-embedded-runner/run/attempt.ts` |
| **Workspace** | |
| Workspace manager | `src/agents/workspace.ts` |
| Workspace directories | `src/agents/workspace-dirs.ts` |
| Bootstrap files | `src/agents/bootstrap-files.ts` |
| Bootstrap cache | `src/agents/bootstrap-cache.ts` |
| Bootstrap budget | `src/agents/bootstrap-budget.ts` |
| **Session** | |
| Session management | `src/config/sessions/` |
| Session transcript | `src/config/sessions/transcript.ts` |
| Session reply | `src/auto-reply/reply/session.ts` |
| Session history | `src/auto-reply/reply/history.ts` |

---

## Key Constants Summary

| Component | Constant | Value |
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

## Implications for the `memory-extract` Project

1. **Active memory vs. passive extraction**: OpenClaw lets the AI write memory proactively when context is getting full, with append-only writes and no filename variants. `extract.py` works after the fact on cold logs, so it starts at a quality disadvantage.

2. **Layered storage**: workspace files for always-on injection, JSONL transcripts for load-on-demand, and hybrid search for recall-on-demand. Each layer has a distinct job. `memory-extract` currently covers only the outermost layer by generating `MEMORY.md`.

3. **Dual-backend architecture**: the builtin path (`SQLite + vector + BM25`) fits local deployment, while QMD fits more advanced retrieval needs with three search modes and collection management. A similar layered search path is worth considering later.

4. **Hybrid retrieval tuning**: 70 percent vector plus 30 percent BM25, with optional MMR (`λ = 0.7`) and optional temporal decay (30-day half-life). These production-tuned defaults are a solid RAG baseline.

5. **Multilingual query expansion**: local tokenization for seven languages plus optional LLM expansion. CJK languages each have dedicated strategies, including Chinese unigram plus bigram, Korean particle stripping, and Japanese mixed-script extraction.

6. **The 200-line limit**: only the first 200 lines of `MEMORY.md` reach the system prompt, so the file must stay tight. This is also why `extract.py` uses a 180-line cap.

7. **Caching and incremental updates**: bootstrap files use `inode + size + mtime`; builtin session memory now supports `deltaBytes` / `deltaMessages` incremental refresh and targeted post-compaction transcript rebuilds; QMD uses debounce plus fixed update/embed intervals.

8. **Compaction is now part of the memory path**: current OpenClaw treats pre-compaction memory flush, post-compaction transcript signaling, and optional post-index sync as one lifecycle chain rather than isolated patches.
