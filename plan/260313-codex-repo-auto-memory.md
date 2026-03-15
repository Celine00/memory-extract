# Codex Repo Auto Memory Tasks

## Summary

- Goal: use Codex `notify` as the per-turn trigger for layered memory ingestion.
- First rollout scope:
  - `/Users/celinezou/Celine00/memory-extract`
  - `/Users/celinezou/Skyscanner/pqs-pipeline-spark-jobs`
- Observation-first approach:
  - keep output repo-local
  - do not write into native Claude/Codex memory paths
  - make the trigger cheap on every turn
  - move expensive flush work to a periodic scheduler

## Current State

- Codex already supports `notify` via `config.toml`.
- The local machine already uses a `notify` script.
- `memory-extract` already has the staged layered flow:
  - `ingest-and-filter`
  - `flush-pending`
  - `rewrite-memory`
- `notify` is suitable for cheap post-turn ingest.
- `notify` alone is not sufficient for idle-time flush because no hook fires after the user stops typing.

## Decision

- Repo-local Codex wiring:
  - each observed repo gets a local `.codex/config.toml`
  - each observed repo gets a local `.codex/notify.sh`
- Post-turn behavior:
  - `notify` runs repo-local ingest only
  - ingest should be fire-and-forget and should not block the user-facing notification
- Periodic behavior:
  - a LaunchAgent runs every 5 minutes
  - each tick runs `flush-pending` for the observed repos
  - flush keeps existing `25m idle` and `90m max pending` thresholds

## Repo Output Paths

- `memory-extract`
  - output stays at `./output`
- `pqs-pipeline-spark-jobs`
  - output goes to `./.codex/memory-extract-output`

## Planned Changes

1. Add tracked helper scripts in `memory-extract` for:
   - repo-local notify dispatch
   - direct ingest/flush wrappers
   - LaunchAgent install/status/uninstall
2. Add repo-local Codex config and notify entrypoints:
   - `memory-extract/.codex/config.toml`
   - `memory-extract/.codex/notify.sh`
   - `pqs-pipeline-spark-jobs/.codex/config.toml`
   - `pqs-pipeline-spark-jobs/.codex/notify.sh`
3. Add repo-local wrappers in `pqs-pipeline-spark-jobs` that call back into `memory-extract`.
4. Install a user LaunchAgent for periodic flush after code changes are in place.

## Guardrails

- Keep Codex hook work cheap and idempotent.
- Keep expensive LLM work in scheduled flush only.
- Keep repo outputs local and uncommitted by default.
- Do not change canonical writeback behavior.
- Do not require Claude-specific hook integration for this first rollout.

## Verification

- Trigger a Codex turn in each repo and confirm pending ingest state advances.
- Dry-run periodic flush for both repos.
- Check repo-local outputs:
  - pending queue
  - searchable facts
  - curated `MEMORY.md`
- Confirm LaunchAgent status reflects the installed schedule.
