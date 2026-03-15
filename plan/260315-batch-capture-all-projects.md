# Batch Capture All Projects

Date: 2026-03-15
Status: Implemented

## Goal

Add `capture-all` command to scan all Claude/Codex projects in one batch run, scheduled twice daily via launchd. This is a stepping stone toward eventual per-session hook-based capture.

## Current State

- `cli.py` supports single-project `capture --project /path`
- `launchd_memory_manager.py` has LaunchAgent install/uninstall/status
- 26 Claude project directories in `~/.claude/projects/`
- Incremental state checkpoint already prevents reprocessing seen sessions

## Changes

### 1. `memory_promotion/cli.py` -- add `capture-all` subcommand

New function `run_capture_all(args)`:

```
capture-all
  --output-dir ./output
  --llm-backend codex-cli
  --llm-model (optional)
  --source-platforms codex,claude
  --max-projects N          # 0 = unlimited, default 0
  --skip-if-recent HOURS    # skip projects whose state was updated within N hours, default 0 (disabled)
  --dry-run
```

Logic:
1. Discover all project paths from `~/.claude/projects/` directory names (reverse the `-` encoding to `/`)
2. Optionally also scan `~/.codex/` if codex in source-platforms
3. Filter: skip projects with no session files; optionally skip recently processed (check state file mtime vs `--skip-if-recent`)
4. For each project, call existing `run_capture()` with a synthesized `argparse.Namespace`
5. Wrap each project in try/except -- log error, continue to next
6. Print summary at end:

```
Batch complete: 26 projects discovered, 18 processed, 3 skipped (recent), 5 skipped (no sessions)
  Total new messages: 142
  Total raw events captured: 87
  Errors: 0
```

Project discovery helper (new function `discover_all_project_paths`):
- Read `~/.claude/projects/` directory listing
- Convert directory name like `-Users-celinezou-Celine00-memory-extract` to `/Users/celinezou/Celine00/memory-extract`
- Filter to paths that actually exist on disk
- Return sorted list of absolute path strings

### 2. `scripts/run-batch-capture` -- wrapper script

```sh
#!/bin/sh
set -eu
OUTPUT_DIR="${OUTPUT_DIR:-./output}"
LLM_BACKEND="${LLM_BACKEND:-codex-cli}"
SKIP_RECENT="${SKIP_RECENT:-6}"

exec python3 -m memory_promotion.cli capture-all \
  --output-dir "$OUTPUT_DIR" \
  --llm-backend "$LLM_BACKEND" \
  --skip-if-recent "$SKIP_RECENT" \
  "$@"
```

### 3. `scripts/launchd_memory_manager.py` -- support batch mode

- Add `--runner-script` option to `install` subcommand, default remains `run-observed-repo-flushes`
- Change `LAUNCH_AGENT_LABEL` default to support a second label for batch: `com.memoryextract.batch-capture`
- Add a convenience `install-batch` subcommand that defaults to:
  - runner = `scripts/run-batch-capture`
  - interval = 43200 (12 hours)
  - label = `com.memoryextract.batch-capture`

Usage:
```sh
python3 scripts/launchd_memory_manager.py install-batch
python3 scripts/launchd_memory_manager.py status --label com.memoryextract.batch-capture
python3 scripts/launchd_memory_manager.py uninstall --label com.memoryextract.batch-capture
```

### 4. Tests -- `test_extract.py`

Add tests for:
- `discover_all_project_paths()` -- mock `~/.claude/projects/` listing, verify path conversion
- `run_capture_all()` -- mock session discovery, verify it iterates projects and handles errors gracefully
- `--skip-if-recent` filtering logic

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Serial not parallel | Serial | LLM calls are the bottleneck; parallel adds complexity for no gain |
| Error isolation | try/except per project, log + continue | Batch must not fail entirely on one bad project |
| No hook yet | Correct | CPU cost too high per-session; memory quality needs improvement first |
| Reuse `run_capture` | Yes | Single project logic stays in one place; `capture-all` is orchestration only |
| launchd not cron | launchd | Already have the infrastructure; native macOS; survives sleep/wake |
| Default 12h interval | Yes | User requirement: twice daily is sufficient |
| `--skip-if-recent` default 6h in wrapper | Yes | Prevents double-processing if manually triggered close to scheduled run |

## Not In Scope

- Parallel processing
- Hook-based per-session trigger (future, after memory quality improves)
- Changes to single-project `capture` behavior
- Write-back to native `~/.claude/projects/*/memory/`

## File Checklist

- [x] `memory_promotion/cli.py` -- add `capture-all`, `discover_all_project_paths`
- [x] `scripts/run-batch-capture` -- new wrapper, chmod +x
- [x] `scripts/launchd_memory_manager.py` -- add `install-batch` convenience
- [x] `test_extract.py` -- add batch discovery and capture-all tests
- [x] `README.md` -- document `capture-all` and batch setup
- [x] `AGENTS.md` -- update file index
