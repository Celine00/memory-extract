# History Backfill Capture Design

## Background

The current layered `capture` flow finds all new transcript messages for a project, but the extraction prompt only includes the last 200 messages. This works for ongoing incremental use. It fails on first run for large repos.

Validation on 2026-03-16 showed the problem clearly:

- Skyscanner native memory had 37 bullets across 5 sections
- Extracted layered memory had 3 bullets across 2 sections
- The new prompt did recover documentation-style rules
- Most older communication, tooling, and workflow facts never entered the prompt

The bottleneck is not dedup first. The bottleneck is history coverage.

## Use Case

We need first-run `capture` to build useful repo-local memory for large projects without:

- sending the entire transcript history in one prompt
- changing the existing incremental capture behavior for already-processed sessions
- mixing staged pending-window logic into the direct `capture` path

After historical backfill is complete, the command should behave like today: process only newly appended messages.

## Proposal

Add bounded history backfill to `capture`.

### Main idea

For each session, track two positions:

- incremental checkpoint: the latest JSONL line known to the collector
- backfill cursor: the highest historical line already sent through layered extraction

On first run, `capture` should not hand all unseen history to the prompt and let prompt rendering cut it to the last 200 messages. Instead, it should extract one bounded historical window at a time, in chronological order, and persist progress in state.

When all sessions for a project have completed historical backfill, `capture` should fall back to the existing incremental behavior.

### Recommended behavior

Use a simple rule:

1. Collect session messages as today
2. If any session still has unfinished backfill, select the next historical window from the oldest unfinished session
3. Build the prompt from that window only
4. Extract events, refresh facts, and advance that session's backfill cursor
5. Repeat on the next `capture` run
6. Once all sessions are backfilled, process only truly new appended messages

Keep one extraction window per `capture` call for the first implementation. It is slower to finish a full project, but much safer for prompt size, runtime, and debugging.

## State Changes

Extend layered capture state with per-session backfill progress.

Recommended new shape:

```json
{
  "sessions": {
    "/path/to/session.jsonl": {
      "size": 12345,
      "mtime_ms": 1234567890.0,
      "last_jsonl_line": 500,
      "backfill_cursor_line": 120,
      "backfill_complete": false
    }
  }
}
```

Rules:

- bump state version when these fields are introduced
- `last_jsonl_line` keeps its current meaning for change detection
- `backfill_cursor_line` is the last historical line already extracted for this session
- `backfill_complete=true` means old history for this session is fully consumed
- old state files must load cleanly, defaulting missing backfill fields to zero / false
- state saving must preserve compatibility for both direct `capture` and `capture-all`

## Windowing Rules

Use session-local windows, not project-global slices.

Recommended first version:

- order sessions by mtime, oldest first during backfill
- within a session, process messages in chronological order
- cap each window to the same prompt budget used today, roughly 200 normalized messages
- keep user and assistant turns together inside the selected slice

This preserves conversation coherence and avoids mixing unrelated sessions into one prompt.

Implementation note:

- do not try to derive backfill windows from the current `new_messages` list alone
- current incremental collection advances session checkpoints to the end of file
- backfill therefore needs its own helper that reopens a session file and selects the next historical slice from `backfill_cursor_line`

## CLI And Output

No new command is needed in the first version.

`capture` and `capture-all` should automatically backfill until each project is complete.

`capture-all --skip-if-recent` needs one special rule:

- if a project still has unfinished backfill, do not skip it only because `last_run_at` is recent
- otherwise large projects can stall forever after one historical window per run

Optional small output improvement:

- report whether the run processed a `backfill` window or `incremental` messages
- report backfill progress like `session 2/9, line 120/480`

No change is needed for output directory structure or searchable fact format.

## Error Handling

- If a session file shrinks or is rewritten, keep the current incremental rescan protection
- If historical backfill was incomplete and the session changed, recompute the next window from the file contents instead of trusting stale offsets blindly
- If prompt parsing fails, do not advance the backfill cursor
- Only mark processed window messages as seen; untouched historical windows must remain eligible for future backfill

## Non-Goals

- semantic dedup
- changing promotion thresholds
- reusing staged pending-window flush for direct capture
- write-back to native memory paths

## Testing

Add tests for:

- old state loading with missing backfill fields
- first `capture` selecting only the first historical window
- repeated `capture` continuing from the previous backfill cursor
- completed backfill falling back to appended-message incremental capture
- large first-run histories no longer being truncated to the last 200 messages only

## Key Message

The next quality step is not smarter prompt wording. It is making sure older durable history actually reaches the prompt. A bounded per-session backfill cursor inside `capture` is the smallest change that fixes that problem without rewriting the whole layered pipeline.
