# History Backfill Capture Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make layered `capture` backfill old session history in bounded windows so first-run extraction for large repos can recover older durable preferences instead of only seeing the last 200 messages.

**Architecture:** Extend layered capture state with per-session backfill progress, add a session-local history window selector in the transcript-loading layer, and teach `capture` / `capture-all` to prioritize unfinished backfill before normal appended-message incremental capture. Keep staged ingest/flush behavior unchanged and reuse the existing consolidate/rewrite pipeline once a bounded message slice has been selected.

**Tech Stack:** Python 3, `unittest`, existing layered pipeline in `extract.py` and `memory_promotion/*`

---

## Chunk 1: File Map

### Task 1: Lock file responsibilities before coding

**Files:**
- Modify: `/Users/celinezou/Celine00/memory-extract/memory_promotion/models.py`
- Modify: `/Users/celinezou/Celine00/memory-extract/memory_promotion/pipeline.py`
- Modify: `/Users/celinezou/Celine00/memory-extract/extract.py`
- Modify: `/Users/celinezou/Celine00/memory-extract/memory_promotion/cli.py`
- Modify: `/Users/celinezou/Celine00/memory-extract/test_extract.py`
- Modify: `/Users/celinezou/Celine00/memory-extract/README.md`
- Modify: `/Users/celinezou/Celine00/memory-extract/AGENTS.md`

- [ ] **Step 1: Re-read the current state and capture boundaries**

Run: `sed -n '150,220p' memory_promotion/models.py`
Run: `sed -n '330,410p' memory_promotion/pipeline.py`
Run: `sed -n '1200,1265p' extract.py`
Run: `sed -n '120,220p' memory_promotion/cli.py`

Expected: confirm that `SessionCheckpoint` only stores `size`, `mtime_ms`, and `last_jsonl_line`, and that `capture` currently always calls `collect_incremental_scope_messages(...)` before `process_capture(...)`.

- [ ] **Step 2: Freeze the intended file ownership**

Use this split during implementation:

```python
file_roles = {
    "memory_promotion/models.py": "state dataclasses and state-version constants",
    "memory_promotion/pipeline.py": "state load/save helpers and capture result metadata",
    "extract.py": "session history window selection and backfill-aware message collection",
    "memory_promotion/cli.py": "capture orchestration and capture-all skip logic",
    "test_extract.py": "state compatibility, backfill progression, fallback, and batch-skip tests",
    "README.md": "user-facing capture behavior update",
    "AGENTS.md": "repo guidance stays aligned with layered semantics",
}
```

- [ ] **Step 3: Record the required non-goals**

Do not implement:

```python
non_goals = [
    "semantic dedup",
    "promotion threshold tuning",
    "staged flush reuse inside direct capture",
    "native memory write-back",
]
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-03-16-history-backfill-capture.md
git commit -m "docs: add history backfill capture plan"
```

## Chunk 2: State And Window Selection

### Task 2: Add backfill state fields with backward compatibility

**Files:**
- Modify: `/Users/celinezou/Celine00/memory-extract/memory_promotion/models.py`
- Modify: `/Users/celinezou/Celine00/memory-extract/memory_promotion/pipeline.py`
- Test: `/Users/celinezou/Celine00/memory-extract/test_extract.py`

- [ ] **Step 1: Write the failing state-compatibility tests**

Add tests that prove:

```python
def test_load_scope_state_defaults_missing_backfill_fields():
    ...

def test_save_scope_state_persists_backfill_fields():
    ...
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `python3 -m unittest -q test_extract.LayeredModeTests.test_load_scope_state_defaults_missing_backfill_fields test_extract.LayeredModeTests.test_save_scope_state_persists_backfill_fields`

Expected: FAIL because `SessionCheckpoint` and state serialization do not yet expose backfill fields.

- [ ] **Step 3: Implement the minimal state change**

Update:

```python
@dataclass(frozen=True)
class SessionCheckpoint:
    size: int
    mtime_ms: float
    last_jsonl_line: int
    backfill_cursor_line: int = 0
    backfill_complete: bool = False
```

Also:

```python
STATE_VERSION = 3
```

And make `load_scope_state(...)` default missing values to `0` / `False` while `save_scope_state(...)` always writes the new fields.

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `python3 -m unittest -q test_extract.LayeredModeTests.test_load_scope_state_defaults_missing_backfill_fields test_extract.LayeredModeTests.test_save_scope_state_persists_backfill_fields`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add memory_promotion/models.py memory_promotion/pipeline.py test_extract.py
git commit -m "feat: add layered capture backfill state"
```

### Task 3: Add a session-local history window selector

**Files:**
- Modify: `/Users/celinezou/Celine00/memory-extract/extract.py`
- Test: `/Users/celinezou/Celine00/memory-extract/test_extract.py`

- [ ] **Step 1: Write the failing history-window tests**

Add tests covering:

```python
def test_collect_backfill_scope_messages_selects_oldest_session_window():
    ...

def test_collect_backfill_scope_messages_continues_from_backfill_cursor():
    ...

def test_collect_backfill_scope_messages_marks_session_complete_after_last_window():
    ...
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `python3 -m unittest -q test_extract.LayeredModeTests.test_collect_backfill_scope_messages_selects_oldest_session_window test_extract.LayeredModeTests.test_collect_backfill_scope_messages_continues_from_backfill_cursor test_extract.LayeredModeTests.test_collect_backfill_scope_messages_marks_session_complete_after_last_window`

Expected: FAIL because no backfill-specific collector exists yet.

- [ ] **Step 3: Implement the minimal backfill collector**

Add a helper in `extract.py` that:

```python
def collect_backfill_scope_messages(
    scope_key: ScopeKey,
    project_sessions: dict[str, list[Path]],
    state: ScopeState,
    *,
    max_messages: int = promotion_pipeline.MAX_MESSAGES_PER_SCOPE,
) -> tuple[list[NormalizedMessage], dict[str, SessionCheckpoint], bool]:
    ...
```

Implementation requirements:

```python
rules = [
    "reopen each session from disk instead of relying on new_messages",
    "pick the oldest unfinished session first",
    "slice one chronological window of up to max_messages",
    "advance only that session's backfill_cursor_line when the window is accepted",
    "set backfill_complete=True only after the session history is exhausted",
]
```

Also add a lightweight helper that refreshes session metadata without consuming history:

```python
def collect_scope_session_checkpoints(
    scope_key: ScopeKey,
    project_sessions: dict[str, list[Path]],
    state: ScopeState,
) -> dict[str, SessionCheckpoint]:
    ...
```

Use it to initialize checkpoint rows for newly discovered sessions before orchestration decides whether backfill is still pending.

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `python3 -m unittest -q test_extract.LayeredModeTests.test_collect_backfill_scope_messages_selects_oldest_session_window test_extract.LayeredModeTests.test_collect_backfill_scope_messages_continues_from_backfill_cursor test_extract.LayeredModeTests.test_collect_backfill_scope_messages_marks_session_complete_after_last_window`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add extract.py test_extract.py
git commit -m "feat: add session-local history backfill windows"
```

## Chunk 3: Capture Orchestration

### Task 4: Route `capture` through backfill before incremental mode

**Files:**
- Modify: `/Users/celinezou/Celine00/memory-extract/memory_promotion/cli.py`
- Modify: `/Users/celinezou/Celine00/memory-extract/memory_promotion/pipeline.py`
- Modify: `/Users/celinezou/Celine00/memory-extract/extract.py`
- Test: `/Users/celinezou/Celine00/memory-extract/test_extract.py`

- [ ] **Step 1: Write the failing orchestration tests**

Add tests for:

```python
def test_process_capture_prefers_backfill_window_before_incremental_messages():
    ...

def test_process_capture_uses_incremental_messages_after_backfill_complete():
    ...

def test_backfill_parse_error_does_not_advance_backfill_cursor():
    ...
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `python3 -m unittest -q test_extract.LayeredModeTests.test_process_capture_prefers_backfill_window_before_incremental_messages test_extract.LayeredModeTests.test_process_capture_uses_incremental_messages_after_backfill_complete test_extract.LayeredModeTests.test_backfill_parse_error_does_not_advance_backfill_cursor`

Expected: FAIL because capture orchestration does not yet distinguish backfill from incremental mode.

- [ ] **Step 3: Implement the minimal orchestration change**

Change `_capture_project(...)` so it chooses message input in this order:

```python
updated_sessions = collect_scope_session_checkpoints(...)
if project_has_pending_backfill(state, updated_sessions):
    selected_messages, updated_sessions = collect_backfill_scope_messages(...)
    capture_mode = "backfill"
else:
    selected_messages, updated_sessions = collect_incremental_scope_messages(...)
    capture_mode = "incremental"
```

And update `process_capture(...)` / `CaptureResult` only as needed to report the chosen mode and selected message count without changing consolidation behavior.

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `python3 -m unittest -q test_extract.LayeredModeTests.test_process_capture_prefers_backfill_window_before_incremental_messages test_extract.LayeredModeTests.test_process_capture_uses_incremental_messages_after_backfill_complete test_extract.LayeredModeTests.test_backfill_parse_error_does_not_advance_backfill_cursor`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add memory_promotion/cli.py memory_promotion/pipeline.py extract.py test_extract.py
git commit -m "feat: route capture through historical backfill"
```

### Task 5: Fix `capture-all --skip-if-recent` for unfinished backfill

**Files:**
- Modify: `/Users/celinezou/Celine00/memory-extract/memory_promotion/cli.py`
- Test: `/Users/celinezou/Celine00/memory-extract/test_extract.py`

- [ ] **Step 1: Write the failing batch-skip tests**

Add:

```python
def test_capture_all_does_not_skip_recent_project_with_unfinished_backfill():
    ...

def test_capture_all_still_skips_recent_project_after_backfill_complete():
    ...
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `python3 -m unittest -q test_extract.BatchCaptureTests.test_capture_all_does_not_skip_recent_project_with_unfinished_backfill test_extract.BatchCaptureTests.test_capture_all_still_skips_recent_project_after_backfill_complete`

Expected: FAIL because `_should_skip_recent(...)` only checks state-file mtime today.

- [ ] **Step 3: Implement the minimal skip override**

Teach `_should_skip_recent(...)` to inspect state and return `False` when:

```python
unfinished_backfill = any(not checkpoint.backfill_complete for checkpoint in state.sessions.values())
```

Only honor the recent-skip cutoff when all sessions are backfill-complete.

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `python3 -m unittest -q test_extract.BatchCaptureTests.test_capture_all_does_not_skip_recent_project_with_unfinished_backfill test_extract.BatchCaptureTests.test_capture_all_still_skips_recent_project_after_backfill_complete`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add memory_promotion/cli.py test_extract.py
git commit -m "fix: keep capture-all backfill progressing"
```

## Chunk 4: Docs And Full Verification

### Task 6: Update docs and verify end-to-end behavior

**Files:**
- Modify: `/Users/celinezou/Celine00/memory-extract/README.md`
- Modify: `/Users/celinezou/Celine00/memory-extract/AGENTS.md`
- Modify: `/Users/celinezou/Celine00/memory-extract/test_extract.py`

- [ ] **Step 1: Update user-facing behavior notes**

Document that first-run `capture` may require multiple invocations for large projects because history backfill is processed in bounded windows before normal incremental mode resumes.

- [ ] **Step 2: Run the focused regression tests**

Run: `python3 -m unittest -q test_extract.LayeredModeTests test_extract.BatchCaptureTests`

Expected: PASS

- [ ] **Step 3: Run the full repo verification**

Run: `python3 -m unittest -q`
Run: `python3 -m py_compile extract.py test_extract.py memory_promotion/*.py`

Expected:
- all unit tests pass
- py_compile exits 0

- [ ] **Step 4: Do one manual dry-run sanity check**

Run: `python3 -m memory_promotion.cli capture --project /Users/celinezou/Skyscanner/pqs-pipeline-spark-jobs --output-dir ./output/validation-backfill --llm-backend codex-cli --dry-run`

Expected: the CLI reports a bounded backfill window instead of effectively relying on the last 200 messages from a project-wide unseen-message pool.

- [ ] **Step 5: Commit**

```bash
git add README.md AGENTS.md test_extract.py
git commit -m "docs: describe layered history backfill capture"
```
