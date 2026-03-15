# AGENTS.md

Repo guidance for agents working in `/Users/celinezou/Celine00/memory-extract`.

## What This Repo Does

从 Claude Code 和 Codex 的本地对话记录中提取稳定的用户偏好，生成统一的 `MEMORY.md`。

两种模式：
- **canonical** (`extract.py`): 一次性全量提取，直接写回 Claude/Codex 原生 memory 路径
- **layered** (`memory_promotion/`): 增量提取，写 repo-local output，支持 batch 和定时调度

LLM backends: `codex-cli`（默认）| `claude-cli` | `anthropic-api` | `auto`

## Important Files

| File | Purpose |
|------|---------|
| `extract.py` | Canonical CLI, parsers, session discovery, compatibility entrypoint |
| `memory_promotion/` | Layered runtime: raw/searchable/promote/recall pipeline |
| `memory_promotion/cli.py` | Layered CLI: capture, capture-all, ingest, flush, rewrite, prepare-context |
| `memory_promotion/pipeline.py` | Layered pipeline logic |
| `memory_promotion/models.py` | Data models |
| `test_extract.py` | Unit tests |
| `prompts/` | Prompt templates for LLM extraction |
| `scripts/run-layered-pilot` | Wrapper: single project capture |
| `scripts/run-batch-capture` | Wrapper: batch capture-all |
| `scripts/run-layered-ingest` | Wrapper: staged ingest |
| `scripts/run-layered-flush` | Wrapper: staged flush |
| `scripts/run-observed-repo-flushes` | Flush staged memory for observed repos |
| `scripts/launchd_memory_manager.py` | LaunchAgent install/status/uninstall |
| `scripts/claude-safe` | Repo-local Claude wrapper (disables broken skills) |
| `.codex/notify.sh` | Codex hook: triggers ingest on turn completion |

## Layered Output Structure

```text
output/
  .state/{project_slug}.json              # capture checkpoint
  .state/{project_slug}.ingest.json       # ingest checkpoint
  .state/{project_slug}.flush.json        # flush dedup state
  project/{project_slug}/
    MEMORY.md                             # curated memory (LLM-rewritten)
    MEMORY.deterministic.md               # deterministic fallback
    memory/
      pending/queue.jsonl                 # candidate window queue
      raw/YYYY-MM-DD.jsonl               # append-only MemoryEvent
      searchable/facts.jsonl              # consolidated SearchableFact
      searchable/archive/YYYY-MM-DD.jsonl # fact change ledger
      audit/YYYY-MM-DD.md                # human-readable daily review
```

`project_slug`: 项目路径最后两个目录名，如 `/Users/celinezou/Celine00/memory-extract` → `Celine00-memory-extract`

## Editing Expectations

- Preserve `canonical` behavior unless the task explicitly changes it.
- Treat `layered` as repo-local pilot; do not write to native Claude/Codex memory paths.
- If you change layered output shape or semantics, update `README.md` and this file together.
- Keep evidence-bearing daily logs append-only.
- Keep backend behavior explicit: API key is only for `anthropic-api`.
- Keep Codex hook cheap: `notify` only triggers ingest; periodic flush stays in LaunchAgent.
- Keep wrapper scripts aligned with documented default commands.

## Verification

```bash
python3 -m unittest -q
python3 -m py_compile extract.py test_extract.py memory_promotion/*.py
```

Spot checks:

```bash
python3 extract.py --list-projects --source-platforms all
python3 -m memory_promotion.cli capture-all --output-dir ./output --dry-run
python3 -m memory_promotion.cli capture --project . --output-dir ./output --dry-run
```
