# memory-extract

Multi-platform memory extraction from Claude Code and Codex session logs.

For detailed file index, output structure, and editing expectations, see [AGENTS.md](./AGENTS.md).

## Quick Reference

```bash
# 单项目提取
python3 -m memory_promotion.cli capture --project . --output-dir ./output

# 全量批量扫描
python3 -m memory_promotion.cli capture-all --output-dir ./output

# 快捷脚本
./scripts/run-layered-pilot          # 当前项目
./scripts/run-batch-capture           # 所有项目

# 测试
python3 -m unittest -q
```

## Key Conventions

- Python-first repo; use `pyenv` virtualenv named `memory-extract`.
- Layered mode defaults to scanning both `codex` and `claude` sessions (`--source-platforms codex,claude`).
- LLM backends: `auto`, `anthropic-api`, `claude-cli`, `codex-cli`. API key only needed for `anthropic-api`.
- Keep `canonical` behavior stable; treat `layered` as the active development path.
- If you change layered output shape or semantics, update both `README.md` and `AGENTS.md` together.
