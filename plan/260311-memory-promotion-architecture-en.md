# Memory Promotion Architecture

Last updated: 2026-03-11

## Background

The current repo already has a good start for layered memory:

- It can extract durable candidates from Claude Code and Codex logs.
- It can keep evidence-bearing daily logs.
- It can rebuild a curated `MEMORY.md`.

What is still missing is a clear middle layer.

Right now the pipeline is close to:

`transcript -> candidate log -> curated MEMORY.md`

That is not enough for long-running use.

- If too much goes into `MEMORY.md`, the file becomes noisy.
- If too little goes into `MEMORY.md`, future sessions lose useful detail.
- If there is no searchable layer, long-tail context has nowhere to live.

The fix is to make the three layers explicit:

`raw -> searchable -> MEMORY.md`

This doc defines that architecture for v1.

## User Case

The target workflow is:

1. After each turn, a hook watches the new transcript window.
2. The system extracts candidate memories from that window.
3. All accepted candidates are stored locally with evidence.
4. Stable facts accumulate in a searchable local store.
5. Only the highest-value facts are promoted into `MEMORY.md`.
6. Before the next prompt, the system injects:
   - `MEMORY.md` as the always-on memory
   - a small recall block from the searchable layer when relevant

This keeps `MEMORY.md` short and stable, while still letting the system remember much more.

## Proposal

### 1. Three-tier memory model

| Tier | Purpose | Source of truth | Read pattern |
|------|---------|-----------------|-------------|
| `raw` | Append-only evidence log of extracted observations | JSONL | Audit and reprocessing |
| `searchable` | Consolidated working memory for recall | JSONL | Query-time retrieval |
| `MEMORY.md` | Small always-inject memory | Markdown | Inject every session |

Rules:

- `raw` is broad but still filtered. It stores durable candidates, not full transcripts.
- `searchable` is the main working memory.
- `MEMORY.md` is the promoted summary, not the database.

### 2. Scope and defaults for v1

v1 decisions are fixed:

- Scope: `project` only
- Capture: `post-turn`
- Recall: `pre-turn`
- Searchable storage: `JSONL first`
- Search backend: lexical/local first, no vector dependency in v1
- No mem0 dependency in v1

This keeps the first version small and easy to validate.

### 3. Storage layout

Project-local layered output should evolve to:

```text
output/
  .state/{project_slug}.json
  project/{project_slug}/
    MEMORY.md
    memory/
      raw/
        YYYY-MM-DD.jsonl
      searchable/
        facts.jsonl
        archive/
          YYYY-MM-DD.jsonl
      audit/
        YYYY-MM-DD.md
```

Meaning:

- `raw/YYYY-MM-DD.jsonl`
  Append-only extracted memory events.
- `searchable/facts.jsonl`
  Current consolidated facts for this project.
- `searchable/archive/YYYY-MM-DD.jsonl`
  Optional append-only change ledger for merges, contradictions, demotions, and promotions.
- `audit/YYYY-MM-DD.md`
  Human-readable daily review output rendered from machine records.
- `MEMORY.md`
  Deterministically rebuilt from promoted facts only.

Markdown stays for human review. JSONL is the machine contract.

### 4. Data model

#### 4.1 Raw event

`MemoryEvent` is the unit written by the post-turn capture step.

Required fields:

- `event_id`
- `project_path`
- `session_file`
- `jsonl_line_range`
- `observed_at`
- `role_window_hash`
- `candidate_text`
- `normalized_text`
- `category`
- `durability`
  Values: `durable`, `tentative`
- `signal_type`
  Values: `explicit`, `implicit`, `project_constraint`
- `evidence`
- `source_platform`
- `turn_id`
- `extraction_version`

Rules:

- `MemoryEvent` is append-only.
- It must always carry enough evidence to trace back to the transcript.
- It should never be rewritten in place.

#### 4.2 Searchable fact

`SearchableFact` is the consolidated working memory layer.

Required fields:

- `fact_id`
- `project_path`
- `canonical_text`
- `display_text`
- `category`
- `status`
  Values: `active`, `tentative`, `contradicted`, `demoted`, `archived`
- `support_count`
- `distinct_turn_count`
- `distinct_session_count`
- `first_observed_at`
- `last_observed_at`
- `explicit_signal`
- `project_constraint_signal`
- `source_event_ids`
- `token_index`
- `promotion_state`
  Values: `never`, `candidate`, `promoted`, `demoted`

Rules:

- Only one active consolidated fact should exist for one canonical meaning.
- Contradictions do not delete history. They change status.
- `token_index` is a local lexical index payload. It is not a vector embedding in v1.

#### 4.3 Promoted memory item

`PromotedMemoryItem` is not stored separately as a long-lived primary record.
It is a deterministic view derived from `SearchableFact`.

Required fields:

- `fact_id`
- `display_text`
- `category`
- `promotion_reason`
- `rank`

### 5. End-to-end flow

```text
new transcript window
  -> candidate extraction
  -> write MemoryEvent to raw JSONL
  -> consolidate into SearchableFact
  -> recompute promotion set
  -> rebuild MEMORY.md
  -> on next prompt, inject MEMORY.md + small recall block
```

### 6. Post-turn capture rules

The post-turn hook should process only new transcript content since the last checkpoint.

Capture pipeline:

1. Load the new transcript window.
2. Extract candidate memories.
3. Filter obvious noise.
4. Write accepted candidates to `raw`.
5. Consolidate raw events into searchable facts.
6. Recompute promotion and rebuild `MEMORY.md` if needed.

Accept into `raw` when the candidate is one of:

- explicit user preference
- stable workflow habit
- stable tooling preference
- stable communication preference
- stable project rule or project constraint

Reject from `raw` when the candidate is one of:

- one-off bug detail
- temporary TODO
- transient branch or file state
- repeated scaffolding or tool noise
- weak speculation

Important rule:

- The extractor may write to `raw`.
- The extractor must not write directly to `MEMORY.md`.

### 7. Consolidation rules for searchable facts

Consolidation turns multiple raw events into one fact.

Merge when:

- `normalized_text` matches
- category is compatible
- there is no explicit contradiction

Do not merge when:

- the newer event negates the older one
- the older fact is broad and the newer fact is clearly more specific but not equivalent

Status rules:

- Start as `tentative` when support is weak
- Move to `active` when the fact is stable enough to recall
- Move to `contradicted` when newer evidence conflicts
- Move to `demoted` when it should no longer stay in `MEMORY.md`
- Move to `archived` only when it is obsolete and no longer useful even for recall

v1 default thresholds:

- `explicit` signal:
  one accepted event can create an `active` fact
- `implicit` signal:
  require `support_count >= 2`
- `project_constraint` signal:
  one strong event can create an `active` fact if it clearly describes a stable repo rule

### 8. Promotion rules for MEMORY.md

`MEMORY.md` is for always-worth-injecting memory only.

Promote when one of these is true:

- the fact is an explicit durable instruction from the user
- the fact is a stable project constraint
- the fact is an implicit preference with `support_count >= 2`

Do not auto-promote in v1 when:

- category is `other`
- status is not `active`
- the fact is clearly task-local
- the fact only appeared once and was not explicit

Category priority for promotion:

1. `explicit_request`
2. `communication`
3. `workflow`
4. `tooling`
5. `project_context`
6. `language`
7. `other`

Budget policy:

- Hard cap stays under the existing 180-line limit.
- Soft target for v1 is `60-100` lines.
- Stronger facts should push weaker facts out.

Demotion policy:

- contradicted fact: demote immediately
- stale project-context fact: demote before user preferences
- weak low-support fact under budget pressure: demote first

### 9. Recall rules for the searchable layer

Pre-turn recall should search `SearchableFact`, not `raw`.

Recall flow:

1. Build a short query from:
   - the current user prompt
   - optionally the most recent turn summary
2. Search active searchable facts using lexical matching over:
   - `canonical_text`
   - `display_text`
   - `token_index`
3. Rank by:
   - text match quality
   - support count
   - recency
   - promotion state
4. Remove anything already covered by `MEMORY.md`
5. Inject a small bounded recall block

Recall budget for v1:

- top `3-5` facts
- each fact should be one short bullet
- total recall block should be small enough to fit comfortably beside `MEMORY.md`

### 10. Logical modules

These are logical modules. They do not need to be separate files on day one.

| Module | Responsibility |
|--------|----------------|
| `capture` | Hook entrypoints, transcript windowing, checkpoint read/write |
| `extract` | LLM-backed candidate extraction from new turns |
| `raw_store` | Append-only raw JSONL writing and loading |
| `consolidate` | Merge events into facts, handle contradiction and support counting |
| `search` | Local lexical retrieval over searchable facts |
| `promote` | Promotion scoring, demotion, per-category quotas |
| `compile_memory` | Deterministic `MEMORY.md` rebuild |
| `inject` | Build pre-turn context from `MEMORY.md` and recall block |
| `audit` | Render human-readable daily review Markdown |
| `state` | Idempotency, replay protection, incremental checkpoints |

### 11. Relationship with current repo

This architecture should evolve the current layered mode, not replace it.

Keep:

- project-only pilot shape
- append-only evidence idea
- deterministic curated memory rebuild
- checkpoint-based incremental processing

Change:

- make `raw` machine-readable JSONL, not Markdown-only
- add a real `searchable` layer between candidate extraction and `MEMORY.md`
- separate “searchable recall” from “always inject memory”

### 12. Non-goals for v1

Out of scope:

- global memory
- vector search
- cross-project dedupe
- mem0 integration
- automatic contradiction resolution by LLM after consolidation
- heavy knowledge graph features

## Support Needed

Implementation work after this doc should happen in this order:

1. Introduce `MemoryEvent` JSONL writing.
2. Introduce `SearchableFact` consolidation.
3. Rebuild `MEMORY.md` from promoted facts only.
4. Add pre-turn recall block from searchable facts.
5. Add audit Markdown rendering.

Recommended validation questions during implementation:

- Does `MEMORY.md` stay short after many turns?
- Can a useful fact exist in searchable without polluting `MEMORY.md`?
- Can every promoted line be traced back to raw evidence?
- Does rerunning the same window keep the state idempotent?

## Key Message

This repo should not try to make `MEMORY.md` carry all memory.

Its job is to manage three layers cleanly:

- `raw` keeps evidence
- `searchable` keeps working memory
- `MEMORY.md` keeps only the small set of always-worth-injecting facts

That is the simplest architecture that supports:

- continuous memory growth
- bounded native memory
- explainable promotion and demotion
- future backend upgrades without losing the current product direction
