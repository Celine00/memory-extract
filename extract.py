#!/usr/bin/env python3
"""
Extract durable user preferences from Claude Code and Codex session logs
and generate canonical MEMORY.md files for project/global scopes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

MAX_CONTENT_LEN = 800
MAX_MESSAGES_PER_SCOPE = 200
MEMORY_LINE_LIMIT = 180
MIN_MESSAGES_PER_SCOPE = 3
SUPPORTED_PLATFORMS = ("claude", "codex")
SUPPORTED_SCOPES = ("project", "global")
CLAUDE_KEEP_TYPES = {"user", "assistant"}

EXTRACTION_PROMPT = """\
You are analyzing conversation logs from Claude Code and Codex, two AI coding assistants.
Your job is to extract **durable user preferences, habits, and patterns** that would be useful
for future sessions with this user.

## Scope
- Memory scope: {scope_name}
- Scope behavior: {scope_description}
- Project path: {project_path}

## What to extract:
- **Language preference**: Chinese, English, or mixed
- **Communication style**: terse vs detailed, code-first vs explanation-first
- **Tech stack & tools**: package managers, frameworks, languages, editors, CLI tools
- **Workflow patterns**: planning habits, testing habits, review habits, iteration style
- **Naming & style**: commit style, code style, documentation preferences
- **Project context**: only if this is project memory and the context is stable/useful
- **Explicit requests**: things the user clearly asked to be remembered

## What NOT to extract:
- One-off task details or temporary bug fixes
- Tool call logs, environment boilerplate, agent scaffolding, or system instructions
- Sensitive data (tokens, API keys, secrets, personal info beyond name/path context)
- Anything speculative or weakly implied
- Project-specific facts in global memory unless they clearly generalize

## Output format:
Write a concise MEMORY.md in markdown. Use bullet points grouped by category.
Keep it under 150 lines. Be specific and actionable.
Start with the highest-value preferences first.
If the logs are too short or too generic, say so briefly.

## Existing memory (preserve and extend, don't duplicate):
{existing_memory}

## Conversation excerpts:
{conversations}
"""


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


@dataclass(frozen=True)
class WriterConfig:
    claude_project_memory_template: str
    claude_global_memory_path: str | None
    codex_project_memory_template: str
    codex_global_memory_path: str | None


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
    if not project_path:
        return "global"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", project_path.strip("/"))
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "root"


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


def load_claude_session_messages(jsonl_path: Path, project_path: str) -> list[NormalizedMessage]:
    messages: list[NormalizedMessage] = []
    try:
        with jsonl_path.open("r") as handle:
            for line in handle:
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

                content = sanitize(truncate_content(content))
                messages.append(
                    NormalizedMessage(
                        platform="claude",
                        project_path=project_path,
                        role=msg_type,
                        content=content,
                        timestamp=entry.get("timestamp", ""),
                    )
                )
    except (OSError, PermissionError) as exc:
        print(f"  Warning: cannot read {jsonl_path}: {exc}", file=sys.stderr)

    return messages


def load_claude_history_messages(paths: PathConfig) -> list[NormalizedMessage]:
    history_path = paths.claude_history_path
    if not history_path.exists():
        return []

    messages: list[NormalizedMessage] = []
    try:
        with history_path.open("r") as handle:
            for line in handle:
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
                    NormalizedMessage(
                        platform="claude",
                        project_path=None,
                        role="user",
                        content=sanitize(truncate_content(display)),
                        timestamp=str(entry.get("timestamp", "")),
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


def load_codex_session_messages(jsonl_path: Path, project_path: str) -> list[NormalizedMessage]:
    messages: list[NormalizedMessage] = []
    try:
        with jsonl_path.open("r") as handle:
            for line in handle:
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

                content = sanitize(truncate_content(content))
                messages.append(
                    NormalizedMessage(
                        platform="codex",
                        project_path=project_path,
                        role=role,
                        content=content,
                        timestamp=entry.get("timestamp", ""),
                    )
                )
    except (OSError, PermissionError) as exc:
        print(f"  Warning: cannot read {jsonl_path}: {exc}", file=sys.stderr)

    return messages


def sort_messages(messages: Iterable[NormalizedMessage]) -> list[NormalizedMessage]:
    def sort_key(message: NormalizedMessage) -> tuple[str, str, str]:
        parsed = parse_timestamp(message.timestamp)
        sortable_timestamp = parsed.isoformat() if parsed else message.timestamp
        return sortable_timestamp, message.platform, message.role

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
                    loaded.extend(load_claude_session_messages(session_file, project_path))
                elif platform == "codex":
                    loaded.extend(load_codex_session_messages(session_file, project_path))
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

    return EXTRACTION_PROMPT.format(
        scope_name=scope_key.scope,
        scope_description=scope_description,
        project_path=project_path,
        existing_memory=existing_memory,
        conversations=format_conversations(messages),
    )


def call_llm(prompt: str, dry_run: bool = False) -> str:
    if dry_run:
        return "(dry run — would call LLM here)"

    try:
        import anthropic
    except ImportError:
        print("Error: anthropic SDK not installed. Run: pip install anthropic", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract canonical user memory from Claude Code and Codex sessions"
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
        help="Also export canonical MEMORY.md files here for inspection.",
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
        f"for scope={args.scope} ..."
    )

    if args.list_projects:
        print_discovered_projects(discovered_sessions)
        return

    project_messages = collect_project_messages(discovered_sessions)
    global_messages = collect_global_messages(project_messages, args.source_platforms, paths)
    scope_messages = build_scope_messages(args.scope, project_messages, global_messages)

    if not scope_messages:
        print("No eligible messages found for the requested scope/source combination.")
        return

    print(f"Found {len(scope_messages)} scope target(s) to process.\n")

    for scope_key, messages in scope_messages.items():
        print(f"Processing {scope_key.scope}: {scope_key.display_name}")
        print(f"  Messages (user+assistant): {len(messages)}")

        if len(messages) < MIN_MESSAGES_PER_SCOPE:
            print("  Skipping — too few messages to extract patterns.\n")
            continue

        target_paths = []
        for platform in args.target_platforms:
            resolved = resolve_target_path(platform, scope_key, writer_config)
            target_paths.append((platform, resolved))

        existing_memory = load_existing_memory(scope_key, args.target_platforms, writer_config)
        prompt = build_prompt(scope_key, existing_memory, messages)

        if args.dry_run:
            print(f"  Would send ~{len(prompt)} chars to LLM")
            for platform, path in target_paths:
                if path:
                    print(f"  Would write {platform}: {path}")
                else:
                    print(f"  Skip {platform}: no configured target path for {scope_key.scope} scope")
            if args.output_dir:
                print(f"  Would export canonical copy: {build_export_path(args.output_dir, scope_key)}")
            user_messages = [msg for msg in messages if msg.role == "user"][:3]
            if user_messages:
                print("  First 3 user messages:")
                for message in user_messages:
                    preview = message.content[:100].replace("\n", " ")
                    print(f"    - [{message.platform}] {preview}...")
            print()
            continue

        print("  Calling Claude API for extraction...")
        memory_content = call_llm(prompt)
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

    print("Done!")


if __name__ == "__main__":
    main()
