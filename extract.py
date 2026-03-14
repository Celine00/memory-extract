#!/usr/bin/env python3
"""
Extract durable user preferences from Claude Code and Codex session logs.

Modes:
- canonical: current behavior, rewrite one MEMORY.md per scope
- layered: write raw/searchable/audit outputs locally and rebuild promoted MEMORY.md
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from memory_promotion import models as promotion_models
from memory_promotion import pipeline as promotion_pipeline
import prompts as prompt_templates

MAX_CONTENT_LEN = 800
MAX_MESSAGES_PER_SCOPE = 200
MEMORY_LINE_LIMIT = promotion_models.MEMORY_LINE_LIMIT
SUPPORTED_PLATFORMS = ("claude", "codex")
SUPPORTED_SCOPES = ("project", "global")
SUPPORTED_MEMORY_MODES = ("canonical", "layered")
SUPPORTED_LLM_BACKENDS = ("auto", "anthropic-api", "claude-cli", "codex-cli")
CLAUDE_KEEP_TYPES = {"user", "assistant"}
LAYERED_SCOPE_ONLY = promotion_models.LAYERED_SCOPE_ONLY
STATE_VERSION = promotion_models.STATE_VERSION
LOG_RECORD_PREFIX = promotion_models.LOG_RECORD_PREFIX
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
CANDIDATE_CATEGORIES = promotion_models.CANDIDATE_CATEGORIES
CATEGORY_TITLES = promotion_models.CATEGORY_TITLES

LAYERED_CANDIDATE_JSON_SCHEMA = promotion_pipeline.LAYERED_CANDIDATE_JSON_SCHEMA

EXTRACTION_PROMPT = prompt_templates.load_prompt("canonical_memory")

LAYERED_EXTRACTION_PROMPT = promotion_pipeline.LAYERED_EXTRACTION_PROMPT


@dataclass(frozen=True)
class PathConfig:
    home: Path = Path.home()

    @property
    def claude_dir(self) -> Path:
        return self.home / ".claude"

    @property
    def claude_projects_dir(self) -> Path:
        return self.claude_dir / "projects"

    @property
    def claude_history_path(self) -> Path:
        return self.claude_dir / "history.jsonl"

    @property
    def codex_dir(self) -> Path:
        return self.home / ".codex"

    @property
    def codex_sessions_dir(self) -> Path:
        return self.codex_dir / "sessions"

    @property
    def codex_memories_dir(self) -> Path:
        return self.codex_dir / "memories"


@dataclass(frozen=True)
class ScopeKey:
    scope: str
    project_path: str | None = None

    @property
    def display_name(self) -> str:
        if self.scope == "global":
            return "global"
        return self.project_path or "(unknown project)"


@dataclass(frozen=True)
class NormalizedMessage:
    platform: str
    project_path: str | None
    role: str
    content: str
    timestamp: str = ""
    session_file: str | None = None
    jsonl_line: int | None = None
    message_id: str = ""


@dataclass(frozen=True)
class SessionLoadResult:
    messages: list[NormalizedMessage]
    total_jsonl_lines: int


@dataclass(frozen=True)
class WriterConfig:
    claude_project_memory_template: str
    claude_global_memory_path: str | None
    codex_project_memory_template: str
    codex_global_memory_path: str | None


@dataclass(frozen=True)
class EvidenceRef:
    platform: str
    session_file: str
    jsonl_line: int
    timestamp: str = ""


@dataclass(frozen=True)
class CandidateMemory:
    text: str
    category: str
    durability: str
    scope: str
    signal_type: str
    evidence: tuple[EvidenceRef, ...]
    observed_at: str
    candidate_hash: str


@dataclass(frozen=True)
class SessionCheckpoint:
    size: int
    mtime_ms: float
    last_jsonl_line: int


@dataclass
class ScopeState:
    version: int
    project_path: str
    last_run_at: str = ""
    sessions: dict[str, SessionCheckpoint] = field(default_factory=dict)
    seen_message_ids: set[str] = field(default_factory=set)
    seen_event_ids: set[str] = field(default_factory=set)
    seen_candidate_hashes: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class LayeredPaths:
    project_dir: Path
    curated_memory_path: Path
    daily_log_path: Path
    memory_dir: Path
    state_path: Path


def parse_platforms(raw: str) -> list[str]:
    value = raw.strip().lower()
    if value == "all":
        return list(SUPPORTED_PLATFORMS)

    platforms = []
    for item in value.split(","):
        platform = item.strip()
        if not platform:
            continue
        if platform not in SUPPORTED_PLATFORMS:
            raise argparse.ArgumentTypeError(
                f"Unsupported platform: {platform}. Use one of: {', '.join(SUPPORTED_PLATFORMS)}, all"
            )
        if platform not in platforms:
            platforms.append(platform)

    if not platforms:
        raise argparse.ArgumentTypeError("At least one platform must be selected.")
    return platforms


def sanitize(text: str) -> str:
    """Basic sanitization to remove potential secrets."""
    text = re.sub(r"(sk-[a-zA-Z0-9]{20,})", "[REDACTED_KEY]", text)
    text = re.sub(r"(ghp_[a-zA-Z0-9]{36,})", "[REDACTED_TOKEN]", text)
    text = re.sub(r"(xoxb-[a-zA-Z0-9-]+)", "[REDACTED_TOKEN]", text)
    text = re.sub(r"(Bearer\s+[a-zA-Z0-9._-]{20,})", "Bearer [REDACTED]", text)
    return text


def truncate_content(text: str) -> str:
    if len(text) <= MAX_CONTENT_LEN:
        return text
    return text[:MAX_CONTENT_LEN] + "\n... [truncated]"


def resolve_claude_project_path(encoded_name: str) -> str:
    return encoded_name.replace("-", "/")


def encode_claude_project_path(project_path: str) -> str:
    return project_path.replace("/", "-")


def slugify_project_path(project_path: str | None) -> str:
    return promotion_models.slugify_project_path(project_path)


def stable_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def make_message_id(
    platform: str,
    session_file: str | None,
    jsonl_line: int | None,
    role: str,
    timestamp: str,
    content: str,
) -> str:
    digest = hashlib.sha256()
    parts = [
        platform,
        session_file or "",
        str(jsonl_line or 0),
        role,
        timestamp,
        content,
    ]
    digest.update("||".join(parts).encode("utf-8"))
    return digest.hexdigest()[:16]


def build_message(
    *,
    platform: str,
    project_path: str | None,
    role: str,
    content: str,
    timestamp: str = "",
    session_file: str | None = None,
    jsonl_line: int | None = None,
) -> NormalizedMessage:
    message_id = make_message_id(platform, session_file, jsonl_line, role, timestamp, content)
    return NormalizedMessage(
        platform=platform,
        project_path=project_path,
        role=role,
        content=content,
        timestamp=timestamp,
        session_file=session_file,
        jsonl_line=jsonl_line,
        message_id=message_id,
    )


def matches_project_filter(project_path: str, project_filter: str | None) -> bool:
    if not project_filter:
        return True
    return project_path.rstrip("/") == project_filter.rstrip("/")


def parse_jsonl_line(line: str) -> dict | None:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def extract_claude_content(entry: dict, msg_type: str) -> str:
    message = entry.get("message", {})
    if not isinstance(message, dict):
        return ""

    raw = message.get("content", "")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def clean_claude_content(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""

    if cleaned.startswith(
        (
            "<local-command-",
            "<bash-input>",
            "<bash-stdout>",
            "<bash-stderr>",
            "<command-name>",
            "<command-message>",
        )
    ):
        return ""

    if cleaned.startswith("[Request interrupted by user"):
        return ""

    if re.fullmatch(r"/[A-Za-z][\w-]*", cleaned):
        return ""

    return cleaned


def load_claude_session_messages(
    jsonl_path: Path,
    project_path: str,
    min_jsonl_line: int = 0,
) -> SessionLoadResult:
    messages: list[NormalizedMessage] = []
    total_jsonl_lines = 0
    session_file = stable_path(jsonl_path)
    try:
        with jsonl_path.open("r") as handle:
            for total_jsonl_lines, line in enumerate(handle, start=1):
                if total_jsonl_lines <= min_jsonl_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                entry = parse_jsonl_line(line)
                if not entry:
                    continue

                msg_type = entry.get("type")
                if msg_type not in CLAUDE_KEEP_TYPES:
                    continue

                content = clean_claude_content(extract_claude_content(entry, msg_type))
                if not content:
                    continue

                messages.append(
                    build_message(
                        platform="claude",
                        project_path=project_path,
                        role=msg_type,
                        content=sanitize(truncate_content(content)),
                        timestamp=str(entry.get("timestamp", "")),
                        session_file=session_file,
                        jsonl_line=total_jsonl_lines,
                    )
                )
    except (OSError, PermissionError) as exc:
        print(f"  Warning: cannot read {jsonl_path}: {exc}", file=sys.stderr)

    return SessionLoadResult(messages=messages, total_jsonl_lines=total_jsonl_lines)


def load_claude_history_messages(paths: PathConfig) -> list[NormalizedMessage]:
    history_path = paths.claude_history_path
    if not history_path.exists():
        return []

    messages: list[NormalizedMessage] = []
    session_file = stable_path(history_path)
    try:
        with history_path.open("r") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                entry = parse_jsonl_line(line)
                if not entry:
                    continue
                display = clean_claude_content(str(entry.get("display", "")))
                if not display:
                    continue

                messages.append(
                    build_message(
                        platform="claude",
                        project_path=None,
                        role="user",
                        content=sanitize(truncate_content(display)),
                        timestamp=str(entry.get("timestamp", "")),
                        session_file=session_file,
                        jsonl_line=line_no,
                    )
                )
    except (OSError, PermissionError) as exc:
        print(f"  Warning: cannot read {history_path}: {exc}", file=sys.stderr)

    return messages[-MAX_MESSAGES_PER_SCOPE:]


def load_claude_history_index(paths: PathConfig) -> dict[str, str]:
    history_path = paths.claude_history_path
    if not history_path.exists():
        return {}

    session_to_project: dict[str, str] = {}
    try:
        with history_path.open("r") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                entry = parse_jsonl_line(line)
                if not entry:
                    continue
                session_id = str(entry.get("sessionId", "")).strip()
                project_path = str(entry.get("project", "")).strip()
                if session_id and project_path:
                    session_to_project[session_id] = project_path
    except (OSError, PermissionError) as exc:
        print(f"  Warning: cannot read {history_path}: {exc}", file=sys.stderr)

    return session_to_project


def discover_claude_project_sessions(
    paths: PathConfig, project_filter: str | None
) -> dict[str, list[Path]]:
    discovered: dict[str, list[Path]] = {}
    projects_dir = paths.claude_projects_dir
    if not projects_dir.exists():
        return discovered

    history_index = load_claude_history_index(paths)

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        session_files = sorted(project_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime)
        if not session_files:
            continue

        for session_file in session_files:
            session_id = session_file.stem
            project_path = history_index.get(session_id) or resolve_claude_project_path(project_dir.name)
            if not matches_project_filter(project_path, project_filter):
                continue
            discovered.setdefault(project_path, []).append(session_file)

    return discovered


def extract_codex_content(blocks: object, role: str) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""

    wanted_types = {"input_text"} if role == "user" else {"output_text"}
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in wanted_types:
            parts.append(block.get("text", ""))
    return "\n".join(part for part in parts if part)


def clean_codex_user_content(text: str) -> str:
    cleaned = text.strip()
    for pattern in (
        r"(?s)^# AGENTS\.md instructions for .*?</INSTRUCTIONS>\s*",
        r"(?s)^<environment_context>.*?</environment_context>\s*",
        r"(?s)^<permissions instructions>.*?</permissions instructions>\s*",
    ):
        while True:
            updated = re.sub(pattern, "", cleaned)
            if updated == cleaned:
                break
            cleaned = updated.strip()

    if cleaned.startswith("[SYSTEM]") and "[USER]" in cleaned:
        _, user_part = cleaned.split("[USER]", 1)
        cleaned = user_part.strip()

    return cleaned


def load_codex_session_meta(jsonl_path: Path) -> dict:
    try:
        with jsonl_path.open("r") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                entry = parse_jsonl_line(line)
                if entry and entry.get("type") == "session_meta":
                    payload = entry.get("payload", {})
                    if isinstance(payload, dict):
                        return payload
                    break
    except (OSError, PermissionError) as exc:
        print(f"  Warning: cannot read {jsonl_path}: {exc}", file=sys.stderr)
    return {}


def discover_codex_project_sessions(
    paths: PathConfig, project_filter: str | None
) -> dict[str, list[Path]]:
    discovered: dict[str, list[Path]] = {}
    sessions_dir = paths.codex_sessions_dir
    if not sessions_dir.exists():
        return discovered

    for session_file in sorted(sessions_dir.rglob("*.jsonl")):
        meta = load_codex_session_meta(session_file)
        project_path = str(meta.get("cwd", "")).strip()
        if not project_path:
            continue
        if not matches_project_filter(project_path, project_filter):
            continue
        discovered.setdefault(project_path, []).append(session_file)

    return discovered


def load_codex_session_messages(
    jsonl_path: Path,
    project_path: str,
    min_jsonl_line: int = 0,
) -> SessionLoadResult:
    messages: list[NormalizedMessage] = []
    total_jsonl_lines = 0
    session_file = stable_path(jsonl_path)
    try:
        with jsonl_path.open("r") as handle:
            for total_jsonl_lines, line in enumerate(handle, start=1):
                if total_jsonl_lines <= min_jsonl_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                entry = parse_jsonl_line(line)
                if not entry or entry.get("type") != "response_item":
                    continue

                payload = entry.get("payload", {})
                if not isinstance(payload, dict) or payload.get("type") != "message":
                    continue

                role = payload.get("role")
                if role not in {"user", "assistant"}:
                    continue

                content = extract_codex_content(payload.get("content"), role).strip()
                if role == "user":
                    content = clean_codex_user_content(content)
                if not content:
                    continue

                messages.append(
                    build_message(
                        platform="codex",
                        project_path=project_path,
                        role=role,
                        content=sanitize(truncate_content(content)),
                        timestamp=str(entry.get("timestamp", "")),
                        session_file=session_file,
                        jsonl_line=total_jsonl_lines,
                    )
                )
    except (OSError, PermissionError) as exc:
        print(f"  Warning: cannot read {jsonl_path}: {exc}", file=sys.stderr)

    return SessionLoadResult(messages=messages, total_jsonl_lines=total_jsonl_lines)


def sort_messages(messages: Iterable[NormalizedMessage]) -> list[NormalizedMessage]:
    def sort_key(message: NormalizedMessage) -> tuple[str, str, int, str, str]:
        parsed = parse_timestamp(message.timestamp)
        sortable_timestamp = parsed.isoformat() if parsed else message.timestamp
        return (
            sortable_timestamp,
            message.session_file or "",
            message.jsonl_line or 0,
            message.platform,
            message.role,
        )

    return sorted(messages, key=sort_key)


def collect_project_messages(
    discovered_sessions: dict[str, dict[str, list[Path]]]
) -> dict[str, list[NormalizedMessage]]:
    project_messages: dict[str, list[NormalizedMessage]] = {}
    for platform, projects in discovered_sessions.items():
        for project_path, session_files in projects.items():
            loaded: list[NormalizedMessage] = []
            for session_file in session_files:
                if platform == "claude":
                    loaded.extend(load_claude_session_messages(session_file, project_path).messages)
                elif platform == "codex":
                    loaded.extend(load_codex_session_messages(session_file, project_path).messages)
            if loaded:
                project_messages.setdefault(project_path, []).extend(loaded)

    return {key: sort_messages(value) for key, value in project_messages.items()}


def collect_global_messages(
    project_messages: dict[str, list[NormalizedMessage]],
    source_platforms: list[str],
    paths: PathConfig,
) -> list[NormalizedMessage]:
    flattened: list[NormalizedMessage] = []
    for messages in project_messages.values():
        flattened.extend(messages)

    if "claude" in source_platforms:
        flattened.extend(load_claude_history_messages(paths))

    return sort_messages(flattened)


def format_timestamp(raw: str) -> str:
    if not raw:
        return ""
    parsed = parse_timestamp(raw)
    if not parsed:
        return raw
    return parsed.strftime("%Y-%m-%d %H:%M")


def parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def format_conversations(messages: list[NormalizedMessage]) -> str:
    selected = messages[-MAX_MESSAGES_PER_SCOPE:]
    lines = []
    for message in selected:
        role = "User" if message.role == "user" else "Assistant"
        timestamp = format_timestamp(message.timestamp)
        platform = message.platform.title()
        project_suffix = ""
        if message.project_path:
            project_suffix = f" [{message.project_path}]"
        lines.append(f"**[{platform} {role}]** ({timestamp}){project_suffix}")
        lines.append(message.content)
        lines.append("")
    return "\n".join(lines)


def format_layered_conversations(messages: list[NormalizedMessage]) -> str:
    selected = messages[-MAX_MESSAGES_PER_SCOPE:]
    lines = []
    for message in selected:
        lines.append(f"id: {message.message_id}")
        lines.append(f"platform: {message.platform}")
        if message.session_file:
            lines.append(f"session_file: {message.session_file}")
        if message.jsonl_line:
            lines.append(f"jsonl_line: {message.jsonl_line}")
        if message.timestamp:
            lines.append(f"timestamp: {message.timestamp}")
        lines.append(f"role: {message.role}")
        lines.append("content:")
        lines.append(message.content)
        lines.append("")
    return "\n".join(lines)


def build_scope_messages(
    requested_scope: str,
    project_messages: dict[str, list[NormalizedMessage]],
    global_messages: list[NormalizedMessage],
) -> dict[ScopeKey, list[NormalizedMessage]]:
    grouped: dict[ScopeKey, list[NormalizedMessage]] = {}
    if requested_scope in {"project", "both"}:
        for project_path, messages in sorted(project_messages.items()):
            grouped[ScopeKey(scope="project", project_path=project_path)] = messages
    if requested_scope in {"global", "both"} and global_messages:
        grouped[ScopeKey(scope="global")] = global_messages
    return grouped


def build_writer_config(args: argparse.Namespace, paths: PathConfig) -> WriterConfig:
    claude_project_template = args.claude_project_memory_template or str(
        paths.claude_projects_dir / "{claude_encoded_project}" / "memory" / "MEMORY.md"
    )
    codex_project_template = args.codex_project_memory_template or str(
        paths.codex_memories_dir / "{project_slug}" / "MEMORY.md"
    )
    codex_global_path = args.codex_global_memory_path or str(paths.codex_memories_dir / "MEMORY.md")
    return WriterConfig(
        claude_project_memory_template=claude_project_template,
        claude_global_memory_path=args.claude_global_memory_path,
        codex_project_memory_template=codex_project_template,
        codex_global_memory_path=codex_global_path,
    )


def resolve_target_path(
    platform: str,
    scope_key: ScopeKey,
    writer_config: WriterConfig,
) -> Path | None:
    fields = {
        "project_path": scope_key.project_path or "",
        "project_slug": slugify_project_path(scope_key.project_path),
        "claude_encoded_project": encode_claude_project_path(scope_key.project_path or ""),
    }

    if platform == "claude":
        if scope_key.scope == "project":
            template = writer_config.claude_project_memory_template
            return Path(template.format(**fields)).expanduser()
        if writer_config.claude_global_memory_path:
            return Path(writer_config.claude_global_memory_path.format(**fields)).expanduser()
        return None

    if platform == "codex":
        if scope_key.scope == "project":
            template = writer_config.codex_project_memory_template
            return Path(template.format(**fields)).expanduser()
        if writer_config.codex_global_memory_path:
            return Path(writer_config.codex_global_memory_path.format(**fields)).expanduser()
        return None

    raise ValueError(f"Unsupported target platform: {platform}")


def build_export_path(output_dir: Path, scope_key: ScopeKey) -> Path:
    if scope_key.scope == "global":
        return output_dir / "global" / "MEMORY.md"
    return output_dir / "project" / slugify_project_path(scope_key.project_path) / "MEMORY.md"


def build_layered_paths(output_dir: Path, scope_key: ScopeKey, now: datetime | None = None) -> LayeredPaths:
    if scope_key.scope != LAYERED_SCOPE_ONLY:
        raise ValueError("Layered mode only supports project scope.")
    return promotion_pipeline.build_layered_paths(output_dir, scope_key.project_path or "", now=now)


def load_existing_memory(
    scope_key: ScopeKey,
    target_platforms: list[str],
    writer_config: WriterConfig,
) -> str:
    sections = []
    seen = set()
    for platform in target_platforms:
        path = resolve_target_path(platform, scope_key, writer_config)
        if not path or not path.exists():
            continue
        try:
            content = path.read_text()
        except OSError:
            continue
        normalized = content.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        sections.append(f"### {platform.title()} existing memory ({path})\n{normalized}")

    if not sections:
        return "(no existing memory)"
    return "\n\n".join(sections)


def load_layered_existing_memory(curated_path: Path) -> str:
    return promotion_pipeline.load_layered_existing_memory(curated_path)


def enforce_memory_limit(content: str) -> str:
    lines = content.splitlines()
    if len(lines) <= MEMORY_LINE_LIMIT:
        return content
    trimmed = lines[:MEMORY_LINE_LIMIT]
    trimmed.append("<!-- truncated to stay within the 200-line limit -->")
    return "\n".join(trimmed)


def write_memory(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(enforce_memory_limit(content))


def build_prompt(
    scope_key: ScopeKey,
    existing_memory: str,
    messages: list[NormalizedMessage],
) -> str:
    if scope_key.scope == "global":
        scope_description = (
            "Global memory shared across projects. Keep only cross-project preferences and habits."
        )
        project_path = "(global scope)"
    else:
        scope_description = (
            "Project memory for one repository/workspace. Stable project-specific context is allowed."
        )
        project_path = scope_key.project_path or "(unknown project)"

    return prompt_templates.render_prompt(
        "canonical_memory",
        scope_name=scope_key.scope,
        scope_description=scope_description,
        project_path=project_path,
        existing_memory=existing_memory,
        conversations=format_conversations(messages),
    )


def build_layered_prompt(
    scope_key: ScopeKey,
    existing_memory: str,
    messages: list[NormalizedMessage],
) -> str:
    return promotion_pipeline.build_layered_prompt(
        project_path=scope_key.project_path or "(unknown project)",
        existing_memory=existing_memory,
        messages=messages,
    )


def anthropic_sdk_available() -> bool:
    return importlib.util.find_spec("anthropic") is not None


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


def resolve_llm_backend(requested_backend: str) -> str:
    if requested_backend == "auto":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key and anthropic_sdk_available():
            return "anthropic-api"
        if command_available("claude"):
            return "claude-cli"
        if command_available("codex"):
            return "codex-cli"
        if api_key:
            return "anthropic-api"
        raise RuntimeError(
            "No usable LLM backend found. Set ANTHROPIC_API_KEY for anthropic-api, "
            "or install/login to claude or codex CLI."
        )

    if requested_backend == "anthropic-api":
        return requested_backend
    if requested_backend == "claude-cli":
        if not command_available("claude"):
            raise RuntimeError("Requested --llm-backend claude-cli, but `claude` is not on PATH.")
        return requested_backend
    if requested_backend == "codex-cli":
        if not command_available("codex"):
            raise RuntimeError("Requested --llm-backend codex-cli, but `codex` is not on PATH.")
        return requested_backend
    raise RuntimeError(f"Unsupported llm backend: {requested_backend}")


def build_claude_cli_command(
    *,
    model: str | None = None,
    output_schema: dict | None = None,
) -> list[str]:
    command = [
        "claude",
        "--print",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--permission-mode",
        "plan",
        "--disable-slash-commands",
        "--tools",
        "",
    ]
    if model:
        command.extend(["--model", model])
    if output_schema is not None:
        command.extend(["--json-schema", json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))])
    return command


def run_subprocess_or_raise(
    command: list[str],
    *,
    prompt: str,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=cwd,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or str(exc)
        raise RuntimeError(detail) from exc


def run_claude_cli(
    prompt: str,
    *,
    model: str | None = None,
    output_schema: dict | None = None,
    cwd: str | None = None,
) -> str:
    result = run_subprocess_or_raise(
        build_claude_cli_command(model=model, output_schema=output_schema),
        prompt=prompt,
        cwd=cwd,
    )
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("claude CLI returned empty output")
    return output


def build_codex_cli_command(
    *,
    cwd: str | None = None,
    model: str | None = None,
    output_schema_path: str | None = None,
    output_path: str,
) -> list[str]:
    command = [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--output-last-message",
        output_path,
    ]
    if cwd:
        command.extend(["-C", cwd])
    if model:
        command.extend(["--model", model])
    if output_schema_path:
        command.extend(["--output-schema", output_schema_path])
    command.append("-")
    return command


def run_codex_cli(
    prompt: str,
    *,
    model: str | None = None,
    output_schema: dict | None = None,
    cwd: str | None = None,
) -> str:
    with tempfile.TemporaryDirectory(prefix="memory-extract-codex-") as temp_dir:
        temp_path = Path(temp_dir)
        output_path = temp_path / "last-message.txt"
        schema_path: Path | None = None
        if output_schema is not None:
            schema_path = temp_path / "schema.json"
            schema_path.write_text(json.dumps(output_schema, ensure_ascii=False, indent=2))

        command = build_codex_cli_command(
            cwd=cwd,
            model=model,
            output_schema_path=str(schema_path) if schema_path else None,
            output_path=str(output_path),
        )
        run_subprocess_or_raise(command, prompt=prompt, cwd=cwd)
        try:
            output = output_path.read_text().strip()
        except OSError as exc:
            raise RuntimeError(f"codex CLI did not write output file: {output_path}") from exc
        if not output:
            raise RuntimeError("codex CLI returned empty output")
        return output


def call_llm(
    prompt: str,
    dry_run: bool = False,
    *,
    backend: str = "auto",
    model: str | None = None,
    output_schema: dict | None = None,
    cwd: str | None = None,
) -> str:
    if dry_run:
        return "(dry run — would call LLM here)"

    try:
        resolved_backend = resolve_llm_backend(backend)
        if resolved_backend == "anthropic-api":
            try:
                import anthropic
            except ImportError:
                print(
                    "Error: anthropic SDK not installed. Run: python3 -m pip install anthropic",
                    file=sys.stderr,
                )
                sys.exit(1)

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                print(
                    "Error: ANTHROPIC_API_KEY environment variable not set. "
                    "Run: export ANTHROPIC_API_KEY=your_key_here",
                    file=sys.stderr,
                )
                sys.exit(1)

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=model or DEFAULT_ANTHROPIC_MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        if resolved_backend == "claude-cli":
            return run_claude_cli(prompt, model=model, output_schema=output_schema, cwd=cwd)

        if resolved_backend == "codex-cli":
            return run_codex_cli(prompt, model=model, output_schema=output_schema, cwd=cwd)

        raise RuntimeError(f"Unhandled llm backend: {resolved_backend}")
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def print_discovered_projects(discovered_sessions: dict[str, dict[str, list[Path]]]):
    merged: dict[str, dict[str, int]] = {}
    for platform, projects in discovered_sessions.items():
        for project_path, session_files in projects.items():
            merged.setdefault(project_path, {})[platform] = len(session_files)

    if not merged:
        print("No projects with session data found.")
        return

    print(f"\nFound {len(merged)} projects:\n")
    for project_path in sorted(merged):
        platform_counts = merged[project_path]
        present = ", ".join(sorted(platform_counts))
        details = ", ".join(
            f"{platform}: {count} session(s)" for platform, count in sorted(platform_counts.items())
        )
        print(f"  {project_path}")
        print(f"    Platforms: {present}")
        print(f"    {details}")


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def extract_json_payload(raw: str) -> object:
    stripped = strip_code_fence(raw)
    for candidate in (stripped, raw.strip()):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    search_space = stripped or raw
    for opener, closer in (("{", "}"), ("[", "]")):
        start = search_space.find(opener)
        end = search_space.rfind(closer)
        if start == -1 or end == -1 or end <= start:
            continue
        snippet = search_space[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            continue

    raise ValueError("LLM response did not contain valid JSON.")


def normalize_candidate_text(text: str) -> str:
    normalized = text.strip()
    normalized = re.sub(r"^[*-]\s+", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def candidate_text_key(text: str) -> str:
    return normalize_candidate_text(text).casefold()


def normalize_candidate_category(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value in CANDIDATE_CATEGORIES:
        return value
    return "other"


def normalize_durability(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value == "durable":
        return "durable"
    return "tentative"


def normalize_scope(raw: object, default_scope: str) -> str:
    value = str(raw or "").strip().lower()
    if value in SUPPORTED_SCOPES:
        return value
    return default_scope


def build_candidate_hash(text: str, category: str, scope: str, durability: str) -> str:
    digest = hashlib.sha256()
    digest.update(
        "||".join(
            [
                candidate_text_key(text),
                category,
                scope,
                durability,
            ]
        ).encode("utf-8")
    )
    return digest.hexdigest()[:16]


def build_evidence_ref(message: NormalizedMessage) -> EvidenceRef | None:
    if not message.session_file or not message.jsonl_line:
        return None
    return EvidenceRef(
        platform=message.platform,
        session_file=message.session_file,
        jsonl_line=message.jsonl_line,
        timestamp=message.timestamp,
    )


def dedupe_evidence_refs(evidence: Iterable[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    unique: list[EvidenceRef] = []
    seen = set()
    for item in evidence:
        key = (item.platform, item.session_file, item.jsonl_line, item.timestamp)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= 3:
            break
    return tuple(unique)


def parse_candidate_response(
    raw: str,
    messages: list[NormalizedMessage],
    *,
    default_scope: str,
    now: datetime | None = None,
) -> list[CandidateMemory]:
    return promotion_pipeline.parse_candidate_response(
        raw,
        messages,
        default_scope=default_scope,
        now=now,
    )


def load_scope_state(path: Path, project_path: str) -> ScopeState:
    return promotion_pipeline.load_scope_state(path, project_path)


def save_scope_state(path: Path, state: ScopeState) -> None:
    promotion_pipeline.save_scope_state(path, state)


def project_session_index(
    discovered_sessions: dict[str, dict[str, list[Path]]]
) -> dict[str, dict[str, list[Path]]]:
    merged: dict[str, dict[str, list[Path]]] = {}
    for platform, projects in discovered_sessions.items():
        for project_path, session_files in projects.items():
            merged.setdefault(project_path, {})[platform] = session_files
    return merged


def collect_incremental_scope_messages(
    scope_key: ScopeKey,
    project_sessions: dict[str, list[Path]],
    state: ScopeState,
) -> tuple[list[NormalizedMessage], dict[str, SessionCheckpoint]]:
    if scope_key.scope != LAYERED_SCOPE_ONLY:
        raise ValueError("Incremental collection only supports project scope.")

    new_messages: list[NormalizedMessage] = []
    updated_sessions: dict[str, SessionCheckpoint] = {}
    for platform, session_files in sorted(project_sessions.items()):
        for session_file in sorted(session_files, key=lambda item: item.stat().st_mtime):
            try:
                stat = session_file.stat()
            except OSError:
                continue

            session_path = stable_path(session_file)
            previous = state.sessions.get(session_path)
            min_jsonl_line = 0
            if previous:
                unchanged = stat.st_size == previous.size and stat.st_mtime == previous.mtime_ms
                rewritten_same_size = stat.st_size == previous.size and stat.st_mtime != previous.mtime_ms
                shrunk = stat.st_size < previous.size or stat.st_mtime < previous.mtime_ms
                if unchanged:
                    updated_sessions[session_path] = previous
                    continue
                if not shrunk and not rewritten_same_size:
                    min_jsonl_line = previous.last_jsonl_line

            if platform == "claude":
                result = load_claude_session_messages(session_file, scope_key.project_path or "", min_jsonl_line)
            elif platform == "codex":
                result = load_codex_session_messages(session_file, scope_key.project_path or "", min_jsonl_line)
            else:
                continue

            if previous and min_jsonl_line > 0 and result.total_jsonl_lines < previous.last_jsonl_line:
                if platform == "claude":
                    result = load_claude_session_messages(session_file, scope_key.project_path or "", 0)
                else:
                    result = load_codex_session_messages(session_file, scope_key.project_path or "", 0)

            updated_sessions[session_path] = SessionCheckpoint(
                size=stat.st_size,
                mtime_ms=stat.st_mtime,
                last_jsonl_line=result.total_jsonl_lines,
            )
            new_messages.extend(
                message for message in result.messages if message.message_id not in state.seen_message_ids
            )

    return sort_messages(new_messages), updated_sessions


def render_log_entry(candidate: CandidateMemory) -> str:
    record = {
        "candidate_hash": candidate.candidate_hash,
        "text": candidate.text,
        "category": candidate.category,
        "durability": candidate.durability,
        "scope": candidate.scope,
        "observed_at": candidate.observed_at,
        "evidence": [
            {
                "platform": ref.platform,
                "session_file": ref.session_file,
                "jsonl_line": ref.jsonl_line,
                "timestamp": ref.timestamp,
            }
            for ref in candidate.evidence
        ],
    }
    lines = [f"{LOG_RECORD_PREFIX} {json.dumps(record, ensure_ascii=False, sort_keys=True)} -->"]
    lines.append(f"- [{candidate.category}] {candidate.text}")
    lines.append("  Evidence:")
    for ref in candidate.evidence:
        label = f"{ref.platform} {ref.session_file}:{ref.jsonl_line}"
        if ref.timestamp:
            label = f"{label} @ {format_timestamp(ref.timestamp)}"
        lines.append(f"  - {label}")
    lines.append("")
    return "\n".join(lines)


def append_candidates_to_daily_log(path: Path, candidates: list[CandidateMemory]) -> None:
    if not candidates:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "\n" if path.exists() and path.stat().st_size > 0 else ""
    with path.open("a") as handle:
        handle.write(prefix)
        handle.write("\n".join(render_log_entry(candidate) for candidate in candidates))


def candidate_from_log_record(record: dict) -> CandidateMemory | None:
    try:
        text = normalize_candidate_text(str(record.get("text", "")))
        category = normalize_candidate_category(record.get("category"))
        durability = normalize_durability(record.get("durability"))
        scope = normalize_scope(record.get("scope"), "project")
        observed_at = str(record.get("observed_at", ""))
    except Exception:
        return None

    if not text:
        return None

    evidence_payload = record.get("evidence", [])
    evidence_refs: list[EvidenceRef] = []
    if isinstance(evidence_payload, list):
        for evidence in evidence_payload:
            if not isinstance(evidence, dict):
                continue
            session_file = str(evidence.get("session_file", "")).strip()
            platform = str(evidence.get("platform", "")).strip()
            if not session_file or not platform:
                continue
            try:
                jsonl_line = int(evidence.get("jsonl_line", 0))
            except (TypeError, ValueError):
                continue
            if jsonl_line <= 0:
                continue
            evidence_refs.append(
                EvidenceRef(
                    platform=platform,
                    session_file=session_file,
                    jsonl_line=jsonl_line,
                    timestamp=str(evidence.get("timestamp", "")),
                )
            )

    deduped = dedupe_evidence_refs(evidence_refs)
    if not deduped:
        return None

    candidate_hash = str(record.get("candidate_hash") or "").strip()
    signal_type = promotion_models.infer_signal_type(category)
    if not candidate_hash:
        candidate_hash = build_candidate_hash(text, category, scope, durability)

    return CandidateMemory(
        text=text,
        category=category,
        durability=durability,
        scope=scope,
        signal_type=signal_type,
        evidence=deduped,
        observed_at=observed_at,
        candidate_hash=candidate_hash,
    )


def load_logged_candidates(memory_dir: Path) -> list[CandidateMemory]:
    if not memory_dir.exists():
        return []

    candidates: list[CandidateMemory] = []
    for log_path in sorted(memory_dir.glob("*.md")):
        try:
            content = log_path.read_text()
        except OSError:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line.startswith(LOG_RECORD_PREFIX) or not line.endswith("-->"):
                continue
            raw_json = line[len(LOG_RECORD_PREFIX) : -3].strip()
            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
            candidate = candidate_from_log_record(payload)
            if candidate:
                candidates.append(candidate)
    return candidates


def candidate_sort_key(candidate: CandidateMemory) -> tuple[float, str]:
    parsed = parse_timestamp(candidate.observed_at)
    timestamp = parsed.timestamp() if parsed else 0.0
    return (timestamp, candidate.text.casefold())


def compile_curated_memory(candidates: list[CandidateMemory]) -> str:
    latest_by_text: dict[str, CandidateMemory] = {}
    for candidate in candidates:
        if candidate.scope != "project" or candidate.durability != "durable":
            continue
        key = candidate_text_key(candidate.text)
        existing = latest_by_text.get(key)
        if existing is None or candidate_sort_key(candidate) > candidate_sort_key(existing):
            latest_by_text[key] = candidate

    grouped: dict[str, list[CandidateMemory]] = {category: [] for category in CANDIDATE_CATEGORIES}
    for candidate in latest_by_text.values():
        grouped.setdefault(candidate.category, []).append(candidate)

    lines = ["# Project Memory", ""]
    has_entries = False
    for category in CANDIDATE_CATEGORIES:
        entries = grouped.get(category, [])
        if not entries:
            continue
        has_entries = True
        lines.append(f"## {CATEGORY_TITLES[category]}")
        for candidate in sorted(entries, key=candidate_sort_key, reverse=True):
            lines.append(f"- {candidate.text}")
        lines.append("")

    if not has_entries:
        lines.extend(["_No durable project memory extracted yet._", ""])

    return enforce_memory_limit("\n".join(lines).strip())


def preview_user_messages(messages: list[NormalizedMessage]) -> None:
    user_messages = [msg for msg in messages if msg.role == "user"][:3]
    if not user_messages:
        return
    print("  First 3 user messages:")
    for message in user_messages:
        preview = message.content[:100].replace("\n", " ")
        print(f"    - [{message.platform}] {preview}...")


def resolve_backend_label(args: argparse.Namespace) -> str:
    if args.dry_run:
        return args.llm_backend
    return resolve_llm_backend(args.llm_backend)


def process_canonical_scope(
    scope_key: ScopeKey,
    messages: list[NormalizedMessage],
    args: argparse.Namespace,
    writer_config: WriterConfig,
) -> None:
    print(f"Processing {scope_key.scope}: {scope_key.display_name}")
    print(f"  Messages (user+assistant): {len(messages)}")

    if len(messages) < 3:
        print("  Skipping — too few messages to extract patterns.\n")
        return

    backend_label = resolve_backend_label(args)
    target_paths = []
    for platform in args.target_platforms:
        resolved = resolve_target_path(platform, scope_key, writer_config)
        target_paths.append((platform, resolved))

    existing_memory = load_existing_memory(scope_key, args.target_platforms, writer_config)
    prompt = build_prompt(scope_key, existing_memory, messages)

    if args.dry_run:
        print(f"  Would send ~{len(prompt)} chars to LLM via {backend_label}")
        for platform, path in target_paths:
            if path:
                print(f"  Would write {platform}: {path}")
            else:
                print(f"  Skip {platform}: no configured target path for {scope_key.scope} scope")
        if args.output_dir:
            print(f"  Would export canonical copy: {build_export_path(args.output_dir, scope_key)}")
        preview_user_messages(messages)
        print()
        return

    print(f"  Calling {backend_label} for extraction...")
    memory_content = call_llm(
        prompt,
        backend=args.llm_backend,
        model=args.llm_model,
        cwd=os.getcwd(),
    )
    memory_content = enforce_memory_limit(memory_content)

    for platform, path in target_paths:
        if not path:
            print(f"  Skipped {platform}: no configured target path for {scope_key.scope} scope")
            continue
        write_memory(path, memory_content)
        print(f"  Written {platform}: {path}")

    if args.output_dir:
        export_path = build_export_path(args.output_dir, scope_key)
        write_memory(export_path, memory_content)
        print(f"  Exported canonical copy: {export_path}")

    print()


def process_layered_scope(
    scope_key: ScopeKey,
    project_sessions: dict[str, list[Path]],
    args: argparse.Namespace,
    *,
    now: datetime | None = None,
) -> None:
    layered_paths = build_layered_paths(args.output_dir, scope_key, now=now)
    state = load_scope_state(layered_paths.state_path, scope_key.project_path or "")
    new_messages, updated_sessions = collect_incremental_scope_messages(scope_key, project_sessions, state)
    backend_label = resolve_backend_label(args)

    print(f"Processing layered {scope_key.scope}: {scope_key.display_name}")
    print(f"  New messages since last checkpoint: {len(new_messages)}")
    print(f"  Raw log: {layered_paths.raw_daily_path}")
    print(f"  Searchable facts: {layered_paths.facts_path}")
    print(f"  Audit: {layered_paths.audit_daily_path}")
    print(f"  Curated memory: {layered_paths.curated_memory_path}")
    print(f"  State: {layered_paths.state_path}")

    if new_messages and not args.dry_run:
        print(f"  Calling {backend_label} for layered extraction...")

    result = promotion_pipeline.process_capture(
        project_path=scope_key.project_path or "",
        output_dir=args.output_dir,
        state=state,
        new_messages=new_messages,
        updated_sessions=updated_sessions,
        llm_call=call_llm,
        llm_backend=args.llm_backend,
        llm_model=args.llm_model,
        dry_run=args.dry_run,
        now=now,
        cwd=os.getcwd(),
    )

    if result.no_new_messages:
        print("  No new messages to process.\n")
        return

    if args.dry_run:
        print(f"  Would send ~{result.prompt_length} chars to LLM via {backend_label}")
        preview_user_messages(new_messages)
        print()
        return

    if result.parse_error:
        print(f"  Skipping scope — could not parse candidate JSON: {result.parse_error}\n")
        return

    print(f"  Captured raw events: {result.raw_event_count}")
    print(f"  Rebuilt searchable facts: {result.searchable_fact_count}")
    print(f"  Promoted memory items: {result.promoted_count}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract user memory from Claude Code and Codex sessions"
    )
    parser.add_argument(
        "--project",
        help="Only process this project path (e.g. /Users/you/myproject). Valid only with --scope project.",
    )
    parser.add_argument(
        "--scope",
        choices=("project", "global", "both"),
        default="project",
        help="Which memory scope(s) to generate.",
    )
    parser.add_argument(
        "--memory-mode",
        choices=SUPPORTED_MEMORY_MODES,
        default="canonical",
        help="canonical rewrites one MEMORY.md per scope; layered writes repo-local raw/searchable/audit data and rebuilds a promoted project MEMORY.md.",
    )
    parser.add_argument(
        "--llm-backend",
        choices=SUPPORTED_LLM_BACKENDS,
        default="auto",
        help="LLM backend for extraction: anthropic-api, claude-cli, codex-cli, or auto.",
    )
    parser.add_argument(
        "--llm-model",
        help="Optional model override for the selected LLM backend.",
    )
    parser.add_argument(
        "--source-platforms",
        default="all",
        help="Comma-separated platforms to read from: claude,codex or all.",
    )
    parser.add_argument(
        "--target-platforms",
        default="all",
        help="Comma-separated platforms to write to: claude,codex or all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write files or call LLM; show what would happen.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Export canonical outputs here. Required for layered mode.",
    )
    parser.add_argument(
        "--list-projects",
        action="store_true",
        help="List discovered projects from the selected source platforms and exit.",
    )
    parser.add_argument(
        "--claude-project-memory-template",
        help="Override Claude project memory template. Default: ~/.claude/projects/{claude_encoded_project}/memory/MEMORY.md",
    )
    parser.add_argument(
        "--claude-global-memory-path",
        help="Optional Claude global memory path. Unset by default because no native global path is hardcoded.",
    )
    parser.add_argument(
        "--codex-project-memory-template",
        help="Override Codex project memory template. Default: ~/.codex/memories/{project_slug}/MEMORY.md",
    )
    parser.add_argument(
        "--codex-global-memory-path",
        help="Override Codex global memory path. Default: ~/.codex/memories/MEMORY.md",
    )
    args = parser.parse_args(argv)
    args.source_platforms = parse_platforms(args.source_platforms)
    args.target_platforms = parse_platforms(args.target_platforms)
    if args.project and args.scope != "project":
        parser.error("--project can only be used with --scope project")
    if args.memory_mode == "layered" and not args.list_projects:
        if args.scope != LAYERED_SCOPE_ONLY:
            parser.error("--memory-mode layered only supports --scope project")
        if not args.output_dir:
            parser.error("--memory-mode layered requires --output-dir")
    return args


def main(argv: list[str] | None = None):
    args = parse_args(argv)
    paths = PathConfig()
    writer_config = build_writer_config(args, paths)

    discovered_sessions: dict[str, dict[str, list[Path]]] = {}
    if "claude" in args.source_platforms:
        discovered_sessions["claude"] = discover_claude_project_sessions(paths, args.project)
    if "codex" in args.source_platforms:
        discovered_sessions["codex"] = discover_codex_project_sessions(paths, args.project)

    print(
        f"Scanning sources: {', '.join(args.source_platforms)} "
        f"for scope={args.scope}, mode={args.memory_mode} ..."
    )

    if args.list_projects:
        print_discovered_projects(discovered_sessions)
        return

    if args.memory_mode == "layered":
        projects = project_session_index(discovered_sessions)
        if not projects:
            print("No eligible projects found for layered mode.")
            return

        print(f"Found {len(projects)} project target(s) to process.\n")
        for project_path, sessions in sorted(projects.items()):
            process_layered_scope(ScopeKey(scope="project", project_path=project_path), sessions, args)
        print("Done!")
        return

    project_messages = collect_project_messages(discovered_sessions)
    global_messages = collect_global_messages(project_messages, args.source_platforms, paths)
    scope_messages = build_scope_messages(args.scope, project_messages, global_messages)

    if not scope_messages:
        print("No eligible messages found for the requested scope/source combination.")
        return

    print(f"Found {len(scope_messages)} scope target(s) to process.\n")
    for scope_key, messages in scope_messages.items():
        process_canonical_scope(scope_key, messages, args, writer_config)

    print("Done!")


if __name__ == "__main__":
    main()
