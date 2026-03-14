from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


def _prompt_path(name: str) -> Path:
    return PROMPTS_DIR / f"{name}.md"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    path = _prompt_path(name)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to load prompt template '{name}' from {path}") from exc


def render_prompt(name: str, **kwargs: str) -> str:
    try:
        return load_prompt(name).format(**kwargs)
    except KeyError as exc:
        raise RuntimeError(f"Missing prompt template variable '{exc.args[0]}' for '{name}'") from exc

