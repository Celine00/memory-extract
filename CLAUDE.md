# memory-extract

Multi-platform memory extraction from Claude Code and Codex session logs.

For detailed repo guidance, file index, workflows, and editing expectations, see [AGENTS.md](./AGENTS.md).

## Quick Reference

- **Canonical mode**: `python3 extract.py --scope project --project .`
- **Layered mode**: `python3 -m memory_promotion.cli capture --project . --output-dir ./output`
- **Quick pilot**: `./scripts/run-layered-pilot`
- **Tests**: `python3 -m unittest -q`

## Key Conventions

- Python-first repo; use `pyenv` virtualenv named `memory-extract`.
- Layered mode defaults to scanning both `codex` and `claude` sessions (`--source-platforms codex,claude`).
- LLM backends: `auto`, `anthropic-api`, `claude-cli`, `codex-cli`. API key only needed for `anthropic-api`.
- Keep `canonical` behavior stable; treat `layered` as a repo-local pilot.
- If you change layered output shape or semantics, update both `README.md` and `AGENTS.md` together.

<claude-mem-context>
# Recent Activity

### Mar 8, 2026

| ID | Time | T | Title | Read |
|----|------|---|-------|------|
| #791 | 10:31 PM | ✅ | Anthropic SDK Added to Memory Extraction Project | ~156 |
| #788 | 10:30 PM | ✅ | Added Type Hints to Session Extraction Tool | ~199 |
| #786 | " | ✅ | Memory Extraction Implementation Started | ~252 |
</claude-mem-context>