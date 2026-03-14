# AGENTS.md

Repo guidance for agents working in `/Users/celinezou/Celine00/memory-extract`.

## What This Repo Does

- `extract.py` extracts durable user preferences from local Claude Code and Codex session logs.
- The repo currently supports two modes:
  - `canonical`: generate one final `MEMORY.md` per scope and optionally write it back to platform paths.
  - `layered`: project-only pilot that supports:
    - manual `capture`
    - staged low-cost flow: `ingest-and-filter -> flush-pending -> rewrite-memory`
- LLM backends:
  - `anthropic-api`
  - `claude-cli`
  - `codex-cli`
  - `auto` (prefer API when available, otherwise CLI fallback)
- Auth split:
  - `ANTHROPIC_API_KEY` is only for `anthropic-api`
  - `claude-cli` uses local Claude CLI auth/session
  - `codex-cli` uses local Codex CLI auth/session

## Recommended Workflows

### Canonical mode

Use when the goal is to produce platform-facing memory files directly.

```bash
python3 extract.py --scope project --project /abs/project/path
python3 extract.py --scope global
python3 extract.py --scope project --project /abs/project/path --llm-backend claude-cli
python3 extract.py --scope project --project /abs/project/path --llm-backend codex-cli
```

### Layered mode

Use when experimenting on a single repo without touching Claude/Codex native memory paths.

```bash
# Manual all-in-one capture
python3 -m memory_promotion.cli capture \
  --project /abs/project/path \
  --output-dir ./output \
  --llm-backend codex-cli

# Low-cost staged flow
python3 -m memory_promotion.cli ingest-and-filter \
  --project /abs/project/path \
  --output-dir ./output

python3 -m memory_promotion.cli flush-pending \
  --project /abs/project/path \
  --output-dir ./output \
  --llm-backend codex-cli
```

Quick wrapper for the current repo:

```bash
./scripts/run-layered-pilot
./scripts/run-layered-pilot --dry-run
./scripts/run-layered-ingest
./scripts/run-layered-flush
./scripts/run-observed-repo-flushes
LLM_BACKEND=claude-cli ./scripts/run-layered-pilot
LLM_BACKEND=codex-cli ./scripts/run-layered-pilot
```

Expected output shape:

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

Layered mode constraints:
- requires `--output-dir`
- supports only `--scope project`
- keeps append-only raw JSONL logs
- keeps append-only pending queue + raw JSONL logs
- rebuilds curated `MEMORY.md` locally from promoted searchable facts
- can optionally rewrite final `MEMORY.md` from promoted facts using a lightweight LLM pass
- `project_slug` 默认取项目路径最后两个目录名，例如 `/Users/celinezou/Celine00/memory-extract` -> `Celine00-memory-extract`

## Important Files

- `extract.py`: canonical CLI, parsers, and compatibility entrypoint for layered mode
- `memory_promotion/`: Codex-first layered runtime, raw/searchable/promote/recall pipeline
- `test_extract.py`: unit tests for parsing, path resolution, layered incremental behavior
- `README.md`: user-facing usage and mode selection
- `openclaw-memory-architecture.md`: bilingual index for the OpenClaw memory architecture reference
- `openclaw-memory-architecture-en.md`: English architecture reference and upstream design notes
- `openclaw-memory-architecture-zh.md`: Chinese architecture reference and upstream design notes
- `scripts/claude-safe`: repo-local Claude wrapper; do not remove unless README is updated too
- `scripts/run-layered-pilot`: convenience wrapper for the project-scoped layered pilot
- `scripts/run-layered-ingest`: convenience wrapper for the low-cost ingest stage
- `scripts/run-layered-flush`: convenience wrapper for the low-cost flush stage
- `scripts/run-observed-repo-flushes`: flush staged memory for the currently observed repos in one pass
- `scripts/launchd_memory_manager.py`: install/status/uninstall for the local LaunchAgent that polls staged flushes
- `.codex/notify.sh`: repo-local Codex notify hook that triggers cheap ingest on turn completion

## Editing Expectations

- Preserve current `canonical` behavior unless the task explicitly changes it.
- Treat `layered` as a repo-local pilot, not a platform integration.
- If you change layered output shape or semantics, update both `README.md` and this file in the same change.
- Keep evidence-bearing daily logs append-only.
- Do not silently make `layered` write to native Claude/Codex memory paths.
- When changing the pilot workflow, keep the wrapper script aligned with the documented default command.
- Keep backend behavior explicit in docs: API key is only for `anthropic-api`, not for `claude-cli` or `codex-cli`.
- Keep Codex hook work cheap: `notify` should only trigger ingest, while periodic flush stays in LaunchAgent polling.

## Layered Output Review

After a layered run, check:

- `project/{project_slug}/memory/raw/YYYY-MM-DD.jsonl` for newly appended `MemoryEvent` records and evidence quality
- `project/{project_slug}/memory/pending/queue.jsonl` for queued or flushed candidate windows
- `project/{project_slug}/memory/searchable/facts.jsonl` for consolidation quality and promotion state
- `project/{project_slug}/memory/audit/YYYY-MM-DD.md` for readable daily review output
- `project/{project_slug}/MEMORY.md` for readability, dedupe quality, and durable-only promoted content
- `.state/{project_slug}.ingest.json` for transcript checkpoint advancement
- `.state/{project_slug}.flush.json` for event/candidate dedupe state
- `.state/{project_slug}.json` for backward-compatible manual `capture`

Healthy output should be incremental, evidence-backed, and free of obvious one-off bug noise.

## Verification

Run these after code changes:

```bash
python3 -m unittest -q
python3 -m py_compile extract.py test_extract.py memory_promotion/*.py
```

Useful spot checks:

```bash
python3 extract.py --help
python3 -m memory_promotion.cli --help
python3 extract.py --list-projects --source-platforms all
python3 extract.py --dry-run --scope both
python3 extract.py --memory-mode layered --scope project --project /abs/project/path --output-dir ./output --dry-run
python3 -m memory_promotion.cli ingest-and-filter --project /abs/project/path --output-dir ./output --dry-run
python3 -m memory_promotion.cli flush-pending --project /abs/project/path --output-dir ./output --dry-run
```
