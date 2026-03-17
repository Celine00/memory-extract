from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import prompts as prompt_templates

from .models import (
    CATEGORY_PRIORITY,
    CATEGORY_TITLES,
    CANDIDATE_CATEGORIES,
    CaptureResult,
    CandidateMemory,
    EvidenceRef,
    EXTRACTION_VERSION,
    FACT_STATUSES,
    DEFAULT_IDLE_FLUSH_MINUTES,
    DEFAULT_MAX_PENDING_MINUTES,
    MAX_PENDING_WINDOWS_PER_FLUSH,
    LAYERED_SCOPE_ONLY,
    LOG_RECORD_PREFIX,
    MAX_RECALL_ITEMS,
    MEMORY_LINE_LIMIT,
    PENDING_WINDOW_STATUSES,
    PROMOTION_STATES,
    SIGNAL_TYPES,
    SOFT_MEMORY_LINE_TARGET,
    LayeredPaths,
    MemoryEvent,
    PendingMessage,
    PendingWindow,
    PromotedMemoryItem,
    RecallItem,
    ScopeState,
    SearchableFact,
    SessionCheckpoint,
    build_candidate_hash,
    build_event_id,
    build_fact_id,
    build_role_window_hash,
    candidate_text_key,
    dedupe_evidence_refs,
    format_timestamp,
    infer_signal_type,
    normalize_candidate_category,
    normalize_candidate_text,
    normalize_durability,
    normalize_scope,
    normalize_signal_type,
    parse_timestamp,
    slugify_project_path,
    tokenize_text,
    STATE_VERSION,
)

MAX_MESSAGES_PER_SCOPE = 200
DOCUMENTATION_STYLE_KEYWORDS = {
    "bold",
    "heading",
    "mermaid",
    "emoji",
    "horizontal",
    "numbering",
    "diagram",
    "color",
    "font",
    "format",
    "markdown",
    "table",
    "bullet",
    "indent",
    "---",
    "**",
}

LAYERED_CANDIDATE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "category": {"type": "string", "enum": list(CANDIDATE_CATEGORIES)},
                    "durability": {"type": "string", "enum": ["durable", "tentative"]},
                    "signal_type": {"type": "string", "enum": list(SIGNAL_TYPES)},
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["text", "category", "durability", "signal_type", "evidence_ids"],
            },
        }
    },
    "required": ["candidates"],
}

LAYERED_EXTRACTION_PROMPT = prompt_templates.load_prompt("layered_capture")


def _message_attr(message: object, name: str, default: Any = "") -> Any:
    return getattr(message, name, default)


def format_layered_conversations(messages: Sequence[object]) -> str:
    selected = list(messages)[-MAX_MESSAGES_PER_SCOPE:]
    lines: list[str] = []
    for message in selected:
        lines.append(f"id: {_message_attr(message, 'message_id')}")
        lines.append(f"platform: {_message_attr(message, 'platform')}")
        session_file = _message_attr(message, "session_file")
        if session_file:
            lines.append(f"session_file: {session_file}")
        jsonl_line = _message_attr(message, "jsonl_line")
        if jsonl_line:
            lines.append(f"jsonl_line: {jsonl_line}")
        timestamp = _message_attr(message, "timestamp")
        if timestamp:
            lines.append(f"timestamp: {timestamp}")
        lines.append(f"role: {_message_attr(message, 'role')}")
        lines.append("content:")
        lines.append(str(_message_attr(message, "content")))
        lines.append("")
    return "\n".join(lines)


def build_layered_prompt(
    *,
    project_path: str,
    existing_memory: str,
    messages: Sequence[object],
) -> str:
    return prompt_templates.render_prompt(
        "layered_capture",
        scope_name=LAYERED_SCOPE_ONLY,
        project_path=project_path or "(unknown project)",
        existing_memory=existing_memory,
        conversations=format_layered_conversations(messages),
    )


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


def build_evidence_ref(message: object) -> EvidenceRef | None:
    session_file = _message_attr(message, "session_file")
    jsonl_line = _message_attr(message, "jsonl_line")
    if not session_file or not jsonl_line:
        return None
    return EvidenceRef(
        platform=str(_message_attr(message, "platform")),
        session_file=str(session_file),
        jsonl_line=int(jsonl_line),
        timestamp=str(_message_attr(message, "timestamp")),
    )


def parse_candidate_response(
    raw: str,
    messages: Sequence[object],
    *,
    default_scope: str,
    now: datetime | None = None,
) -> list[CandidateMemory]:
    payload = extract_json_payload(raw)
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("candidates", [])
    else:
        raise ValueError("Candidate payload must be a JSON array or object.")

    if not isinstance(entries, list):
        raise ValueError("Candidate payload must contain a list of candidates.")

    message_lookup = {
        str(_message_attr(message, "message_id")): message
        for message in messages
        if _message_attr(message, "message_id")
    }
    now_iso = (now or datetime.now()).isoformat()
    candidates: list[CandidateMemory] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        text = normalize_candidate_text(str(entry.get("text", "")))
        if not text:
            continue

        category = normalize_candidate_category(entry.get("category"))
        durability = normalize_durability(entry.get("durability"))
        scope = normalize_scope(entry.get("scope"), default_scope)
        signal_type = normalize_signal_type(entry.get("signal_type"), category)

        evidence_refs: list[EvidenceRef] = []
        raw_ids = entry.get("evidence_ids", [])
        if isinstance(raw_ids, list):
            for evidence_id in raw_ids:
                message = message_lookup.get(str(evidence_id).strip())
                if not message:
                    continue
                ref = build_evidence_ref(message)
                if ref:
                    evidence_refs.append(ref)

        raw_evidence = entry.get("evidence", [])
        if isinstance(raw_evidence, list):
            for evidence in raw_evidence:
                if not isinstance(evidence, dict):
                    continue
                evidence_id = str(evidence.get("message_id") or evidence.get("id") or "").strip()
                if evidence_id:
                    message = message_lookup.get(evidence_id)
                    if message:
                        ref = build_evidence_ref(message)
                        if ref:
                            evidence_refs.append(ref)
                        continue
                session_file = str(evidence.get("session_file") or "").strip()
                platform = str(evidence.get("platform") or "").strip()
                line_value = evidence.get("jsonl_line")
                if not session_file or not platform:
                    continue
                try:
                    jsonl_line = int(line_value)
                except (TypeError, ValueError):
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
            continue

        evidence_timestamps = [ref.timestamp for ref in deduped if ref.timestamp]
        observed_at = max(evidence_timestamps, default=now_iso)
        candidates.append(
            CandidateMemory(
                text=text,
                category=category,
                durability=durability,
                scope=scope,
                signal_type=signal_type,
                evidence=deduped,
                observed_at=observed_at,
                candidate_hash=build_candidate_hash(text, category, scope, durability, signal_type),
            )
        )

    return candidates


def build_layered_paths(output_dir: Path, project_path: str, now: datetime | None = None) -> LayeredPaths:
    date_stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    project_slug = slugify_project_path(project_path)
    project_dir = output_dir / "project" / project_slug
    memory_dir = project_dir / "memory"
    pending_dir = memory_dir / "pending"
    searchable_dir = memory_dir / "searchable"
    archive_dir = searchable_dir / "archive"
    audit_dir = memory_dir / "audit"
    raw_dir = memory_dir / "raw"
    return LayeredPaths(
        project_dir=project_dir,
        curated_memory_path=project_dir / "MEMORY.md",
        deterministic_memory_path=project_dir / "MEMORY.deterministic.md",
        memory_dir=memory_dir,
        raw_dir=raw_dir,
        raw_daily_path=raw_dir / f"{date_stamp}.jsonl",
        pending_dir=pending_dir,
        pending_path=pending_dir / "queue.jsonl",
        searchable_dir=searchable_dir,
        facts_path=searchable_dir / "facts.jsonl",
        archive_dir=archive_dir,
        archive_daily_path=archive_dir / f"{date_stamp}.jsonl",
        audit_dir=audit_dir,
        audit_daily_path=audit_dir / f"{date_stamp}.md",
        state_path=output_dir / ".state" / f"{project_slug}.json",
        ingest_state_path=output_dir / ".state" / f"{project_slug}.ingest.json",
        flush_state_path=output_dir / ".state" / f"{project_slug}.flush.json",
    )


def load_layered_existing_memory(curated_path: Path) -> str:
    if not curated_path.exists():
        return "(no existing memory)"
    try:
        content = curated_path.read_text().strip()
    except OSError:
        return "(no existing memory)"
    return content or "(no existing memory)"


def load_scope_state(path: Path, project_path: str) -> ScopeState:
    if not path.exists():
        return ScopeState(version=STATE_VERSION, project_path=project_path)

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ScopeState(version=STATE_VERSION, project_path=project_path)

    sessions_payload = payload.get("sessions", {})
    sessions: dict[str, SessionCheckpoint] = {}
    if isinstance(sessions_payload, dict):
        for session_path, checkpoint in sessions_payload.items():
            if not isinstance(checkpoint, dict):
                continue
            try:
                sessions[session_path] = SessionCheckpoint(
                    size=int(checkpoint.get("size", 0)),
                    mtime_ms=float(checkpoint.get("mtime_ms", 0)),
                    last_jsonl_line=int(checkpoint.get("last_jsonl_line", 0)),
                )
            except (TypeError, ValueError):
                continue

    return ScopeState(
        version=int(payload.get("version", STATE_VERSION)),
        project_path=str(payload.get("project_path") or project_path),
        last_run_at=str(payload.get("last_run_at", "")),
        sessions=sessions,
        seen_message_ids=set(map(str, payload.get("seen_message_ids", []))),
        seen_event_ids=set(map(str, payload.get("seen_event_ids", []))),
        seen_candidate_hashes=set(map(str, payload.get("seen_candidate_hashes", []))),
    )


def save_scope_state(path: Path, state: ScopeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "project_path": state.project_path,
        "last_run_at": state.last_run_at,
        "sessions": {
            session_path: {
                "size": checkpoint.size,
                "mtime_ms": checkpoint.mtime_ms,
                "last_jsonl_line": checkpoint.last_jsonl_line,
            }
            for session_path, checkpoint in sorted(state.sessions.items())
        },
        "seen_message_ids": sorted(state.seen_message_ids),
        "seen_event_ids": sorted(state.seen_event_ids),
        "seen_candidate_hashes": sorted(state.seen_candidate_hashes),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def enforce_memory_limit(content: str, line_limit: int = MEMORY_LINE_LIMIT) -> str:
    lines = content.splitlines()
    if len(lines) <= line_limit:
        return content
    trimmed = lines[:line_limit]
    trimmed.append("<!-- truncated to stay within the line limit -->")
    return "\n".join(trimmed)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


PENDING_REASON_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "explicit_request",
        (
            r"\b(always|prefer|default|please|must|should|avoid|never|keep|only|use|update|sync)\b",
            r"\b(don't|do not|won't|cannot|can't)\b",
            r"(默认|尽量|不要|别|统一|以后|总是|必须|应该|保持|继续|记住|优先|只用|同步更新)",
        ),
    ),
    (
        "communication",
        (
            r"\b(concise|brief|detailed|markdown|bullet|bullets|nested|tone|reply|summary|plan|review)\b",
            r"(简洁|详细|中文|英文|列表|分点|嵌套|语气|回复|总结|计划|review)",
        ),
    ),
    (
        "project_constraint",
        (
            r"\b(this repo|this repository|this project|canonical|layered|readme|agents\.md|hook|skill|pytest|unittest|memory)\b",
            r"(这个仓库|这个项目|不要改|保持.*行为|测试|文档|记忆|hook|skill)",
        ),
    ),
)

ASSISTANT_CONFIRMATION_PATTERNS: tuple[str, ...] = (
    r"\b(understood|i will|i'll|will keep|will use|will follow|keep this|continue to)\b",
    r"(会按|会保持|后续会|明白了|收到|我会按|我会保持)",
)


def _pending_message_sort_key(message: PendingMessage) -> tuple[str, int, str]:
    return (message.session_file, message.jsonl_line, message.message_id)


def _pending_message_from_obj(message: object) -> PendingMessage | None:
    message_id = str(_message_attr(message, "message_id")).strip()
    session_file = str(_message_attr(message, "session_file")).strip()
    role = str(_message_attr(message, "role")).strip()
    platform = str(_message_attr(message, "platform")).strip()
    try:
        jsonl_line = int(_message_attr(message, "jsonl_line", 0) or 0)
    except (TypeError, ValueError):
        jsonl_line = 0
    if not message_id or not session_file or not role or jsonl_line <= 0:
        return None
    return PendingMessage(
        message_id=message_id,
        role=role,
        content=str(_message_attr(message, "content")),
        timestamp=str(_message_attr(message, "timestamp")),
        session_file=session_file,
        jsonl_line=jsonl_line,
        platform=platform,
    )


def _pending_message_to_record(message: PendingMessage) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "role": message.role,
        "content": message.content,
        "timestamp": message.timestamp,
        "session_file": message.session_file,
        "jsonl_line": message.jsonl_line,
        "platform": message.platform,
    }


def _pending_message_from_record(record: dict[str, object]) -> PendingMessage | None:
    try:
        message_id = str(record.get("message_id") or "").strip()
        session_file = str(record.get("session_file") or "").strip()
        role = str(record.get("role") or "").strip()
        platform = str(record.get("platform") or "").strip()
        jsonl_line = int(record.get("jsonl_line") or 0)
    except (TypeError, ValueError):
        return None
    if not message_id or not session_file or not role or jsonl_line <= 0:
        return None
    return PendingMessage(
        message_id=message_id,
        role=role,
        content=str(record.get("content") or ""),
        timestamp=str(record.get("timestamp") or ""),
        session_file=session_file,
        jsonl_line=jsonl_line,
        platform=platform,
    )


def _pending_window_to_record(window: PendingWindow) -> dict[str, object]:
    return {
        "window_id": window.window_id,
        "project_path": window.project_path,
        "platform": window.platform,
        "session_file": window.session_file,
        "first_timestamp": window.first_timestamp,
        "last_timestamp": window.last_timestamp,
        "message_ids": list(window.message_ids),
        "messages": [_pending_message_to_record(message) for message in window.messages],
        "jsonl_line_range": list(window.jsonl_line_range),
        "reason_codes": list(window.reason_codes),
        "excerpt": window.excerpt,
        "status": window.status,
        "queued_at": window.queued_at,
    }


def _pending_window_from_record(record: dict[str, object]) -> PendingWindow | None:
    try:
        window_id = str(record.get("window_id") or "").strip()
        project_path = str(record.get("project_path") or "").strip()
        platform = str(record.get("platform") or "").strip()
        session_file = str(record.get("session_file") or "").strip()
        first_timestamp = str(record.get("first_timestamp") or "")
        last_timestamp = str(record.get("last_timestamp") or "")
        raw_range = record.get("jsonl_line_range") or []
        if not isinstance(raw_range, list) or len(raw_range) != 2:
            return None
        jsonl_line_range = (int(raw_range[0]), int(raw_range[1]))
        status = str(record.get("status") or "queued")
    except (TypeError, ValueError):
        return None
    if status not in PENDING_WINDOW_STATUSES:
        status = "queued"
    raw_messages = record.get("messages") or []
    if not isinstance(raw_messages, list):
        return None
    messages = tuple(
        sorted(
            [message for entry in raw_messages if isinstance(entry, dict) for message in [_pending_message_from_record(entry)] if message],
            key=_pending_message_sort_key,
        )
    )
    if not window_id or not project_path or not session_file or not messages:
        return None
    message_ids = tuple(map(str, record.get("message_ids") or [message.message_id for message in messages]))
    reason_codes = tuple(sorted({str(reason).strip() for reason in record.get("reason_codes") or [] if str(reason).strip()}))
    return PendingWindow(
        window_id=window_id,
        project_path=project_path,
        platform=platform or messages[0].platform,
        session_file=session_file,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        message_ids=message_ids,
        messages=messages,
        jsonl_line_range=jsonl_line_range,
        reason_codes=reason_codes,
        excerpt=str(record.get("excerpt") or ""),
        status=status,
        queued_at=str(record.get("queued_at") or ""),
    )


def load_pending_windows(path: Path) -> list[PendingWindow]:
    if not path.exists():
        return []
    windows: list[PendingWindow] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        window = _pending_window_from_record(payload)
        if window:
            windows.append(window)
    return windows


def save_pending_windows(path: Path, windows: Sequence[PendingWindow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        windows,
        key=lambda item: (
            _best_timestamp(item.first_timestamp)[0],
            item.session_file,
            item.jsonl_line_range,
            item.window_id,
        ),
    )
    lines = [json.dumps(_pending_window_to_record(window), ensure_ascii=False, sort_keys=True) for window in ordered]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _build_pending_excerpt(messages: Sequence[PendingMessage], limit: int = 1200) -> str:
    parts = [f"{message.role}: {normalize_candidate_text(message.content)}" for message in messages if normalize_candidate_text(message.content)]
    excerpt = "\n".join(parts).strip()
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 1].rstrip() + "…"


def _reason_codes_for_message(message: PendingMessage) -> set[str]:
    text = message.content.strip()
    if not text:
        return set()
    lowered = text.lower()
    reasons: set[str] = set()
    patterns = PENDING_REASON_PATTERNS if message.role == "user" else tuple()
    for reason_code, regexes in patterns:
        if any(re.search(regex, text, re.IGNORECASE) for regex in regexes):
            reasons.add(reason_code)
    if message.role == "assistant" and any(re.search(regex, lowered, re.IGNORECASE) for regex in ASSISTANT_CONFIRMATION_PATTERNS):
        reasons.add("assistant_confirmation")
    return reasons


def _build_pending_window_id(
    *,
    project_path: str,
    session_file: str,
    jsonl_line_range: tuple[int, int],
    message_ids: Sequence[str],
) -> str:
    digest = hashlib.sha256()
    digest.update(project_path.encode("utf-8"))
    digest.update(b"||")
    digest.update(session_file.encode("utf-8"))
    digest.update(b"||")
    digest.update(f"{jsonl_line_range[0]}:{jsonl_line_range[1]}".encode("utf-8"))
    digest.update(b"||")
    digest.update("||".join(message_ids).encode("utf-8"))
    return digest.hexdigest()[:20]


def collect_pending_windows(
    project_path: str,
    messages: Sequence[object],
    *,
    now: datetime | None = None,
) -> list[PendingWindow]:
    pending_messages = [message for item in messages if (message := _pending_message_from_obj(item))]
    if not pending_messages:
        return []

    grouped: dict[str, list[PendingMessage]] = {}
    for message in pending_messages:
        grouped.setdefault(message.session_file, []).append(message)

    queued_at = (now or datetime.now()).isoformat()
    windows: list[PendingWindow] = []
    for session_file, session_messages in grouped.items():
        ordered = sorted(session_messages, key=_pending_message_sort_key)
        candidate_ranges: list[tuple[int, int, set[str]]] = []
        for index, message in enumerate(ordered):
            reasons = _reason_codes_for_message(message)
            if not reasons:
                continue
            start = max(0, index - 1)
            end = min(len(ordered) - 1, index + 1)
            candidate_ranges.append((start, end, reasons))
        if not candidate_ranges:
            continue

        merged: list[tuple[int, int, set[str]]] = []
        for start, end, reasons in candidate_ranges:
            if merged and start <= merged[-1][1] + 1:
                prev_start, prev_end, prev_reasons = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end), prev_reasons | reasons)
            else:
                merged.append((start, end, set(reasons)))

        for start, end, reasons in merged:
            window_messages = tuple(ordered[start : end + 1])
            message_ids = tuple(message.message_id for message in window_messages)
            jsonl_line_range = (window_messages[0].jsonl_line, window_messages[-1].jsonl_line)
            timestamps = [message.timestamp for message in window_messages if message.timestamp]
            windows.append(
                PendingWindow(
                    window_id=_build_pending_window_id(
                        project_path=project_path,
                        session_file=session_file,
                        jsonl_line_range=jsonl_line_range,
                        message_ids=message_ids,
                    ),
                    project_path=project_path,
                    platform=window_messages[0].platform,
                    session_file=session_file,
                    first_timestamp=min(timestamps, key=_best_timestamp) if timestamps else "",
                    last_timestamp=max(timestamps, key=_best_timestamp) if timestamps else "",
                    message_ids=message_ids,
                    messages=window_messages,
                    jsonl_line_range=jsonl_line_range,
                    reason_codes=tuple(sorted(reasons)),
                    excerpt=_build_pending_excerpt(window_messages),
                    status="queued",
                    queued_at=queued_at,
                )
            )
    return windows


def pending_windows_ready(
    windows: Sequence[PendingWindow],
    *,
    now: datetime | None = None,
    idle_minutes: int = DEFAULT_IDLE_FLUSH_MINUTES,
    max_pending_minutes: int = DEFAULT_MAX_PENDING_MINUTES,
) -> tuple[bool, str | None]:
    queued = [window for window in windows if window.status == "queued"]
    if not queued:
        return False, None
    current_epoch = (now or datetime.now()).timestamp()
    latest_ts = max(
        (parsed.timestamp() for window in queued if window.last_timestamp for parsed in [parse_timestamp(window.last_timestamp)] if parsed),
        default=None,
    )
    earliest_ts = min(
        (parsed.timestamp() for window in queued if window.first_timestamp for parsed in [parse_timestamp(window.first_timestamp)] if parsed),
        default=None,
    )
    if latest_ts is not None:
        idle_age = (current_epoch - latest_ts) / 60
        if idle_age >= idle_minutes:
            return True, f"idle>{idle_minutes}m"
    if earliest_ts is not None:
        oldest_age = (current_epoch - earliest_ts) / 60
        if oldest_age >= max_pending_minutes:
            return True, f"oldest>{max_pending_minutes}m"
    return False, None


def pending_messages_for_flush(windows: Sequence[PendingWindow]) -> list[PendingMessage]:
    unique: dict[str, PendingMessage] = {}
    for window in selected_pending_windows(windows):
        for message in window.messages:
            unique.setdefault(message.message_id, message)
    return sorted(unique.values(), key=_pending_message_sort_key)


def selected_pending_windows(windows: Sequence[PendingWindow]) -> list[PendingWindow]:
    return [window for window in windows if window.status == "queued"][:MAX_PENDING_WINDOWS_PER_FLUSH]


def mark_pending_windows_flushed(
    windows: Sequence[PendingWindow],
    flushed_ids: set[str],
) -> list[PendingWindow]:
    updated: list[PendingWindow] = []
    for window in windows:
        if window.window_id in flushed_ids and window.status == "queued":
            updated.append(
                PendingWindow(
                    window_id=window.window_id,
                    project_path=window.project_path,
                    platform=window.platform,
                    session_file=window.session_file,
                    first_timestamp=window.first_timestamp,
                    last_timestamp=window.last_timestamp,
                    message_ids=window.message_ids,
                    messages=window.messages,
                    jsonl_line_range=window.jsonl_line_range,
                    reason_codes=window.reason_codes,
                    excerpt=window.excerpt,
                    status="flushed",
                    queued_at=window.queued_at,
                )
            )
        else:
            updated.append(window)
    return updated


def _event_to_record(event: MemoryEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "project_path": event.project_path,
        "session_file": event.session_file,
        "jsonl_line_range": list(event.jsonl_line_range),
        "observed_at": event.observed_at,
        "role_window_hash": event.role_window_hash,
        "candidate_text": event.candidate_text,
        "normalized_text": event.normalized_text,
        "category": event.category,
        "durability": event.durability,
        "signal_type": event.signal_type,
        "evidence": [
            {
                "platform": ref.platform,
                "session_file": ref.session_file,
                "jsonl_line": ref.jsonl_line,
                "timestamp": ref.timestamp,
            }
            for ref in event.evidence
        ],
        "source_platform": event.source_platform,
        "turn_id": event.turn_id,
        "extraction_version": event.extraction_version,
    }


def _event_from_record(record: dict[str, object]) -> MemoryEvent | None:
    try:
        event_id = str(record.get("event_id") or "").strip()
        project_path = str(record.get("project_path") or "").strip()
        session_file = str(record.get("session_file") or "").strip()
        raw_line_range = record.get("jsonl_line_range") or []
        if not event_id or not project_path or not session_file or not isinstance(raw_line_range, list):
            return None
        if len(raw_line_range) != 2:
            return None
        jsonl_line_range = (int(raw_line_range[0]), int(raw_line_range[1]))
        evidence_payload = record.get("evidence") or []
        evidence: list[EvidenceRef] = []
        if isinstance(evidence_payload, list):
            for entry in evidence_payload:
                if not isinstance(entry, dict):
                    continue
                evidence.append(
                    EvidenceRef(
                        platform=str(entry.get("platform") or ""),
                        session_file=str(entry.get("session_file") or ""),
                        jsonl_line=int(entry.get("jsonl_line") or 0),
                        timestamp=str(entry.get("timestamp") or ""),
                    )
                )
        deduped = dedupe_evidence_refs([ref for ref in evidence if ref.session_file and ref.jsonl_line > 0])
        if not deduped:
            return None
        return MemoryEvent(
            event_id=event_id,
            project_path=project_path,
            session_file=session_file,
            jsonl_line_range=jsonl_line_range,
            observed_at=str(record.get("observed_at") or ""),
            role_window_hash=str(record.get("role_window_hash") or ""),
            candidate_text=normalize_candidate_text(str(record.get("candidate_text") or "")),
            normalized_text=normalize_candidate_text(str(record.get("normalized_text") or record.get("candidate_text") or "")),
            category=normalize_candidate_category(record.get("category")),
            durability=normalize_durability(record.get("durability")),
            signal_type=normalize_signal_type(record.get("signal_type"), normalize_candidate_category(record.get("category"))),
            evidence=deduped,
            source_platform=str(record.get("source_platform") or deduped[0].platform),
            turn_id=str(record.get("turn_id") or ""),
            extraction_version=str(record.get("extraction_version") or EXTRACTION_VERSION),
        )
    except (TypeError, ValueError):
        return None


def append_memory_events(path: Path, events: Sequence[MemoryEvent]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for event in events:
            handle.write(json.dumps(_event_to_record(event), ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def load_raw_memory_events(raw_dir: Path) -> list[MemoryEvent]:
    if not raw_dir.exists():
        return []
    events: list[MemoryEvent] = []
    for raw_path in sorted(raw_dir.glob("*.jsonl")):
        try:
            lines = raw_path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event = _event_from_record(payload)
            if event:
                events.append(event)
    return events


def _legacy_candidate_from_record(record: dict[str, object]) -> CandidateMemory | None:
    text = normalize_candidate_text(str(record.get("text") or ""))
    if not text:
        return None
    category = normalize_candidate_category(record.get("category"))
    durability = normalize_durability(record.get("durability"))
    scope = normalize_scope(record.get("scope"), LAYERED_SCOPE_ONLY)
    observed_at = str(record.get("observed_at") or "")
    signal_type = normalize_signal_type(record.get("signal_type"), category)
    evidence_payload = record.get("evidence") or []
    evidence_refs: list[EvidenceRef] = []
    if isinstance(evidence_payload, list):
        for evidence in evidence_payload:
            if not isinstance(evidence, dict):
                continue
            try:
                ref = EvidenceRef(
                    platform=str(evidence.get("platform") or ""),
                    session_file=str(evidence.get("session_file") or ""),
                    jsonl_line=int(evidence.get("jsonl_line") or 0),
                    timestamp=str(evidence.get("timestamp") or ""),
                )
            except (TypeError, ValueError):
                continue
            if ref.session_file and ref.jsonl_line > 0:
                evidence_refs.append(ref)

    deduped = dedupe_evidence_refs(evidence_refs)
    if not deduped:
        return None
    candidate_hash = str(record.get("candidate_hash") or "").strip()
    if not candidate_hash:
        candidate_hash = build_candidate_hash(text, category, scope, durability, signal_type)
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


def legacy_candidates_to_events(memory_dir: Path, project_path: str) -> list[MemoryEvent]:
    if not memory_dir.exists():
        return []

    events: list[MemoryEvent] = []
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
            if not isinstance(payload, dict):
                continue
            candidate = _legacy_candidate_from_record(payload)
            if candidate:
                event = candidate_to_event(project_path, candidate)
                if event:
                    events.append(event)
    return events


def candidate_to_event(project_path: str, candidate: CandidateMemory) -> MemoryEvent | None:
    if candidate.scope != LAYERED_SCOPE_ONLY or not candidate.evidence:
        return None
    sorted_evidence = sorted(candidate.evidence, key=lambda ref: (ref.session_file, ref.jsonl_line))
    primary = sorted_evidence[0]
    line_range = (min(ref.jsonl_line for ref in sorted_evidence), max(ref.jsonl_line for ref in sorted_evidence))
    role_window_hash = build_role_window_hash(candidate.text, candidate.evidence)
    return MemoryEvent(
        event_id=build_event_id(project_path, candidate.candidate_hash, candidate.evidence, role_window_hash),
        project_path=project_path,
        session_file=primary.session_file,
        jsonl_line_range=line_range,
        observed_at=candidate.observed_at,
        role_window_hash=role_window_hash,
        candidate_text=candidate.text,
        normalized_text=normalize_candidate_text(candidate.text),
        category=candidate.category,
        durability=candidate.durability,
        signal_type=candidate.signal_type,
        evidence=candidate.evidence,
        source_platform=primary.platform,
        turn_id=f"{primary.session_file}:{line_range[1]}",
        extraction_version=EXTRACTION_VERSION,
    )


def load_all_memory_events(paths: LayeredPaths, project_path: str) -> list[MemoryEvent]:
    event_map: dict[str, MemoryEvent] = {}
    for event in load_raw_memory_events(paths.raw_dir):
        event_map[event.event_id] = event
    for event in legacy_candidates_to_events(paths.memory_dir, project_path):
        event_map.setdefault(event.event_id, event)
    return sorted(event_map.values(), key=lambda item: (item.observed_at, item.event_id))


def _fact_to_record(fact: SearchableFact) -> dict[str, object]:
    return {
        "fact_id": fact.fact_id,
        "project_path": fact.project_path,
        "canonical_text": fact.canonical_text,
        "display_text": fact.display_text,
        "category": fact.category,
        "status": fact.status,
        "support_count": fact.support_count,
        "distinct_turn_count": fact.distinct_turn_count,
        "distinct_session_count": fact.distinct_session_count,
        "first_observed_at": fact.first_observed_at,
        "last_observed_at": fact.last_observed_at,
        "explicit_signal": fact.explicit_signal,
        "project_constraint_signal": fact.project_constraint_signal,
        "source_event_ids": list(fact.source_event_ids),
        "token_index": list(fact.token_index),
        "promotion_state": fact.promotion_state,
    }


def _fact_from_record(record: dict[str, object]) -> SearchableFact | None:
    try:
        status = str(record.get("status") or "tentative")
        if status not in FACT_STATUSES:
            status = "tentative"
        promotion_state = str(record.get("promotion_state") or "never")
        if promotion_state not in PROMOTION_STATES:
            promotion_state = "never"
        source_event_ids = tuple(map(str, record.get("source_event_ids") or []))
        token_index = tuple(map(str, record.get("token_index") or []))
        return SearchableFact(
            fact_id=str(record.get("fact_id") or ""),
            project_path=str(record.get("project_path") or ""),
            canonical_text=normalize_candidate_text(str(record.get("canonical_text") or "")),
            display_text=normalize_candidate_text(str(record.get("display_text") or "")),
            category=normalize_candidate_category(record.get("category")),
            status=status,
            support_count=int(record.get("support_count") or 0),
            distinct_turn_count=int(record.get("distinct_turn_count") or 0),
            distinct_session_count=int(record.get("distinct_session_count") or 0),
            first_observed_at=str(record.get("first_observed_at") or ""),
            last_observed_at=str(record.get("last_observed_at") or ""),
            explicit_signal=bool(record.get("explicit_signal")),
            project_constraint_signal=bool(record.get("project_constraint_signal")),
            source_event_ids=source_event_ids,
            token_index=token_index,
            promotion_state=promotion_state,
        )
    except (TypeError, ValueError):
        return None


def load_searchable_facts(path: Path) -> list[SearchableFact]:
    if not path.exists():
        return []
    facts: list[SearchableFact] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        fact = _fact_from_record(payload)
        if fact and fact.fact_id:
            facts.append(fact)
    return facts


def save_searchable_facts(path: Path, facts: Sequence[SearchableFact]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(_fact_to_record(fact), ensure_ascii=False, sort_keys=True) for fact in facts]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _best_timestamp(value: str) -> tuple[float, str]:
    parsed = parse_timestamp(value)
    if not parsed:
        return (0.0, value)
    return (parsed.timestamp(), value)


def _status_for_events(events: Sequence[MemoryEvent]) -> str:
    if any(event.signal_type in {"explicit", "project_constraint"} for event in events):
        return "active"
    if len(events) >= 2:
        return "active"
    return "tentative"


def maybe_reclassify_category(text: str, category: str) -> str:
    if category != "communication":
        return category
    lower = text.lower()
    if any(keyword in lower for keyword in DOCUMENTATION_STYLE_KEYWORDS):
        return "documentation_style"
    return category


def _event_category(event: MemoryEvent) -> str:
    return maybe_reclassify_category(event.candidate_text or event.normalized_text, event.category)


def _best_category(events: Sequence[MemoryEvent]) -> str:
    categories = {_event_category(event) for event in events}
    return min(categories, key=lambda category: CATEGORY_PRIORITY.get(category, 99))


def consolidate_facts(
    project_path: str,
    events: Sequence[MemoryEvent],
    previous_facts: Sequence[SearchableFact] | None = None,
) -> list[SearchableFact]:
    grouped: dict[str, list[MemoryEvent]] = {}
    for event in events:
        grouped.setdefault(candidate_text_key(event.normalized_text), []).append(event)

    previous_by_fact_id = {fact.fact_id: fact for fact in previous_facts or []}
    facts: list[SearchableFact] = []
    for group_key, group_events in grouped.items():
        ordered = sorted(group_events, key=lambda item: (_best_timestamp(item.observed_at), item.event_id))
        latest = ordered[-1]
        category = _best_category(ordered)
        fact_id = build_fact_id(project_path, group_key, category)
        previous = previous_by_fact_id.get(fact_id)
        first_observed_at = min((event.observed_at for event in ordered), key=_best_timestamp)
        last_observed_at = max((event.observed_at for event in ordered), key=_best_timestamp)
        facts.append(
            SearchableFact(
                fact_id=fact_id,
                project_path=project_path,
                canonical_text=group_key,
                display_text=latest.candidate_text,
                category=category,
                status=_status_for_events(ordered),
                support_count=len(ordered),
                distinct_turn_count=len({event.turn_id for event in ordered if event.turn_id}),
                distinct_session_count=len({event.session_file for event in ordered if event.session_file}),
                first_observed_at=first_observed_at,
                last_observed_at=last_observed_at,
                explicit_signal=any(event.signal_type == "explicit" for event in ordered),
                project_constraint_signal=any(
                    event.signal_type == "project_constraint" for event in ordered
                ),
                source_event_ids=tuple(sorted({event.event_id for event in ordered})),
                token_index=tokenize_text(f"{group_key} {latest.candidate_text}"),
                promotion_state=previous.promotion_state if previous else "never",
            )
        )
    return sorted(
        facts,
        key=lambda fact: (
            CATEGORY_PRIORITY.get(fact.category, 99),
            -_best_timestamp(fact.last_observed_at)[0],
            fact.display_text.casefold(),
        ),
    )


def _promotion_reason(fact: SearchableFact) -> str:
    if fact.explicit_signal:
        return "explicit durable instruction"
    if fact.project_constraint_signal:
        return "stable project constraint"
    return "repeated implicit preference"


def _promotion_sort_key(fact: SearchableFact) -> tuple[int, int, int, float, str]:
    return (
        1 if fact.explicit_signal else 0,
        1 if fact.project_constraint_signal else 0,
        min(fact.support_count, 9),
        _best_timestamp(fact.last_observed_at)[0],
        -CATEGORY_PRIORITY.get(fact.category, 99),
    )


def _is_promotable(fact: SearchableFact) -> bool:
    if fact.category == "other":
        return False
    if fact.status != "active":
        return False
    if fact.explicit_signal or fact.project_constraint_signal:
        return True
    return fact.support_count >= 2


def _render_memory_from_facts(facts: Sequence[SearchableFact]) -> str:
    grouped: dict[str, list[SearchableFact]] = {category: [] for category in CANDIDATE_CATEGORIES}
    for fact in facts:
        grouped.setdefault(fact.category, []).append(fact)

    lines = ["# Project Memory", ""]
    has_entries = False
    for category in CANDIDATE_CATEGORIES:
        entries = grouped.get(category, [])
        if not entries:
            continue
        has_entries = True
        lines.append(f"## {CATEGORY_TITLES[category]}")
        for fact in sorted(entries, key=lambda item: (_best_timestamp(item.last_observed_at)[0], item.display_text.casefold()), reverse=True):
            lines.append(f"- {fact.display_text}")
        lines.append("")

    if not has_entries:
        lines.extend(["_No durable project memory extracted yet._", ""])
    return "\n".join(lines).strip()


def apply_promotion(facts: Sequence[SearchableFact]) -> tuple[list[SearchableFact], list[PromotedMemoryItem]]:
    promotable = sorted(
        [fact for fact in facts if _is_promotable(fact)],
        key=_promotion_sort_key,
        reverse=True,
    )
    selected = list(promotable)
    while selected:
        rendered = _render_memory_from_facts(selected)
        line_count = len(rendered.splitlines())
        if line_count <= SOFT_MEMORY_LINE_TARGET and line_count <= MEMORY_LINE_LIMIT:
            break
        selected.pop()

    selected_ids = {fact.fact_id for fact in selected}
    updated_facts: list[SearchableFact] = []
    promoted_items: list[PromotedMemoryItem] = []
    rank = 1
    for fact in facts:
        if fact.fact_id in selected_ids:
            promotion_state = "promoted"
            promoted_items.append(
                PromotedMemoryItem(
                    fact_id=fact.fact_id,
                    display_text=fact.display_text,
                    category=fact.category,
                    promotion_reason=_promotion_reason(fact),
                    rank=rank,
                )
            )
            rank += 1
        elif _is_promotable(fact):
            promotion_state = "candidate"
        elif fact.promotion_state in {"candidate", "promoted", "demoted"}:
            promotion_state = "demoted"
        else:
            promotion_state = "never"
        updated_facts.append(
            SearchableFact(
                fact_id=fact.fact_id,
                project_path=fact.project_path,
                canonical_text=fact.canonical_text,
                display_text=fact.display_text,
                category=fact.category,
                status=fact.status,
                support_count=fact.support_count,
                distinct_turn_count=fact.distinct_turn_count,
                distinct_session_count=fact.distinct_session_count,
                first_observed_at=fact.first_observed_at,
                last_observed_at=fact.last_observed_at,
                explicit_signal=fact.explicit_signal,
                project_constraint_signal=fact.project_constraint_signal,
                source_event_ids=fact.source_event_ids,
                token_index=fact.token_index,
                promotion_state=promotion_state,
            )
        )
    return updated_facts, promoted_items


def compile_curated_memory(facts: Sequence[SearchableFact]) -> str:
    promoted = [fact for fact in facts if fact.promotion_state == "promoted"]
    return enforce_memory_limit(_render_memory_from_facts(promoted))


def format_promoted_facts_for_rewrite(facts: Sequence[SearchableFact]) -> str:
    promoted = [fact for fact in facts if fact.promotion_state == "promoted"]
    if not promoted:
        return "- (no promoted facts)"
    lines: list[str] = []
    for fact in promoted:
        lines.append(
            f"- [{fact.category}] {fact.display_text} "
            f"(support={fact.support_count}, explicit={str(fact.explicit_signal).lower()}, "
            f"project_constraint={str(fact.project_constraint_signal).lower()})"
        )
    return "\n".join(lines)


def build_memory_rewrite_prompt(
    *,
    project_path: str,
    deterministic_memory: str,
    facts: Sequence[SearchableFact],
) -> str:
    return prompt_templates.render_prompt(
        "rewrite_memory",
        project_path=project_path or "(unknown project)",
        deterministic_memory=deterministic_memory,
        promoted_facts=format_promoted_facts_for_rewrite(facts),
    )


def rewrite_curated_memory(
    *,
    project_path: str,
    facts: Sequence[SearchableFact],
    deterministic_memory: str,
    llm_call: Callable[..., str] | None,
    llm_backend: str,
    llm_model: str | None,
    dry_run: bool,
    cwd: str | None = None,
) -> tuple[str, bool]:
    promoted = [fact for fact in facts if fact.promotion_state == "promoted"]
    if not promoted or not llm_call or dry_run:
        return deterministic_memory, False
    prompt = build_memory_rewrite_prompt(
        project_path=project_path,
        deterministic_memory=deterministic_memory,
        facts=facts,
    )
    rewritten = llm_call(
        prompt,
        dry_run=False,
        backend=llm_backend,
        model=llm_model,
        cwd=cwd,
    ).strip()
    if not rewritten:
        return deterministic_memory, False
    return enforce_memory_limit(strip_code_fence(rewritten)), True


def refresh_searchable_and_memory(
    *,
    project_path: str,
    paths: LayeredPaths,
    now: datetime | None = None,
    llm_call: Callable[..., str] | None = None,
    llm_backend: str = "auto",
    llm_model: str | None = None,
    dry_run: bool = False,
    cwd: str | None = None,
    rewrite_memory: bool = False,
) -> tuple[list[SearchableFact], list[PromotedMemoryItem], bool]:
    previous_facts = load_searchable_facts(paths.facts_path)
    all_events = load_all_memory_events(paths, project_path)
    facts = consolidate_facts(project_path, all_events, previous_facts)
    facts, promoted_items = apply_promotion(facts)
    save_searchable_facts(paths.facts_path, facts)
    archive_records = _facts_archive_records(
        previous_facts,
        facts,
        changed_at=(now or datetime.now()).isoformat(),
    )
    append_archive_records(paths.archive_daily_path, archive_records)
    deterministic_memory = compile_curated_memory(facts)
    previous_deterministic = load_layered_existing_memory(paths.deterministic_memory_path)
    write_text(paths.deterministic_memory_path, deterministic_memory)
    memory_content = deterministic_memory
    rewrite_applied = False
    if rewrite_memory and deterministic_memory != previous_deterministic:
        memory_content, rewrite_applied = rewrite_curated_memory(
            project_path=project_path,
            facts=facts,
            deterministic_memory=deterministic_memory,
            llm_call=llm_call,
            llm_backend=llm_backend,
            llm_model=llm_model,
            dry_run=dry_run,
            cwd=cwd,
        )
    write_text(paths.curated_memory_path, memory_content)
    daily_events = load_daily_events(paths.raw_daily_path)
    write_text(
        paths.audit_daily_path,
        render_audit_markdown(
            project_path=project_path,
            daily_events=daily_events,
            facts=facts,
            promoted_items=promoted_items,
        ),
    )
    return facts, promoted_items, rewrite_applied


def _facts_archive_records(
    previous_facts: Sequence[SearchableFact],
    current_facts: Sequence[SearchableFact],
    *,
    changed_at: str,
) -> list[dict[str, object]]:
    previous_map = {fact.fact_id: _fact_to_record(fact) for fact in previous_facts}
    current_map = {fact.fact_id: _fact_to_record(fact) for fact in current_facts}
    records: list[dict[str, object]] = []
    for fact_id, record in current_map.items():
        previous = previous_map.get(fact_id)
        if previous == record:
            continue
        records.append(
            {
                "changed_at": changed_at,
                "change_type": "upsert" if previous is None else "refresh",
                "fact": record,
            }
        )
    for fact_id in previous_map.keys() - current_map.keys():
        records.append({"changed_at": changed_at, "change_type": "removed", "fact_id": fact_id})
    return records


def append_archive_records(path: Path, records: Sequence[dict[str, object]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def load_daily_events(path: Path) -> list[MemoryEvent]:
    if not path.exists():
        return []
    events: list[MemoryEvent] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event = _event_from_record(payload)
        if event:
            events.append(event)
    return events


def render_audit_markdown(
    *,
    project_path: str,
    daily_events: Sequence[MemoryEvent],
    facts: Sequence[SearchableFact],
    promoted_items: Sequence[PromotedMemoryItem],
) -> str:
    lines = ["# Memory Promotion Audit", ""]
    lines.append(f"- Project: `{project_path}`")
    lines.append(f"- Raw events today: {len(daily_events)}")
    lines.append(f"- Searchable facts: {len(facts)}")
    lines.append(f"- Promoted items: {len(promoted_items)}")
    lines.append("")

    lines.append("## New Raw Events")
    if not daily_events:
        lines.append("- No accepted memory events today.")
    else:
        for event in sorted(daily_events, key=lambda item: (_best_timestamp(item.observed_at)[0], item.event_id), reverse=True):
            lines.append(
                f"- [{event.category} | {event.signal_type} | {event.durability}] {event.candidate_text}"
            )
            for evidence in event.evidence:
                label = f"{evidence.platform} {evidence.session_file}:{evidence.jsonl_line}"
                if evidence.timestamp:
                    label = f"{label} @ {format_timestamp(evidence.timestamp)}"
                lines.append(f"  - {label}")
    lines.append("")

    lines.append("## Active Searchable Facts")
    active_facts = [fact for fact in facts if fact.status == "active"]
    if not active_facts:
        lines.append("- No active facts yet.")
    else:
        for fact in active_facts[:20]:
            lines.append(
                f"- [{fact.category} | support={fact.support_count} | {fact.promotion_state}] {fact.display_text}"
            )
    lines.append("")

    lines.append("## Promoted Memory")
    if not promoted_items:
        lines.append("- Nothing promoted into `MEMORY.md` yet.")
    else:
        for item in promoted_items:
            lines.append(f"- [{item.category}] {item.display_text}")
    lines.append("")
    return "\n".join(lines)


def recall_facts(
    facts: Sequence[SearchableFact],
    query: str,
    *,
    limit: int = MAX_RECALL_ITEMS,
) -> list[RecallItem]:
    query_tokens = set(tokenize_text(query))
    if not query_tokens:
        return []

    ranked: list[RecallItem] = []
    for fact in facts:
        if fact.status != "active" or fact.promotion_state == "promoted":
            continue
        fact_tokens = set(fact.token_index)
        overlap = query_tokens & fact_tokens
        canonical = fact.canonical_text.casefold()
        query_lower = query.casefold()
        lexical_score = float(len(overlap) * 10)
        if canonical and canonical in query_lower:
            lexical_score += 6.0
        elif any(token in canonical for token in query_tokens):
            lexical_score += 2.0
        if lexical_score <= 0:
            continue
        lexical_score += min(fact.support_count, 5)
        lexical_score += 0.5 if fact.promotion_state == "candidate" else 0.0
        lexical_score += _best_timestamp(fact.last_observed_at)[0] / 1_000_000_000_000.0
        ranked.append(RecallItem(fact_id=fact.fact_id, display_text=fact.display_text, score=lexical_score))

    return sorted(ranked, key=lambda item: (item.score, item.display_text.casefold()), reverse=True)[:limit]


def build_prepare_context(
    *,
    project_path: str,
    output_dir: Path,
    query: str,
    now: datetime | None = None,
    limit: int = MAX_RECALL_ITEMS,
) -> tuple[str, list[RecallItem]]:
    paths = build_layered_paths(output_dir, project_path, now=now)
    memory_content = load_layered_existing_memory(paths.curated_memory_path)
    facts = load_searchable_facts(paths.facts_path)
    recall_items = recall_facts(facts, query, limit=limit)

    lines: list[str] = []
    if memory_content and memory_content != "(no existing memory)":
        lines.append(memory_content.strip())
    else:
        lines.append("# Project Memory")
        lines.append("")
        lines.append("_No durable project memory extracted yet._")

    if recall_items:
        lines.append("")
        lines.append("## Relevant Recall")
        for item in recall_items:
            lines.append(f"- {item.display_text}")
    return "\n".join(lines).strip(), recall_items


def process_ingest(
    *,
    project_path: str,
    output_dir: Path,
    state: ScopeState,
    new_messages: Sequence[object],
    updated_sessions: dict[str, SessionCheckpoint],
    now: datetime | None = None,
) -> dict[str, object]:
    paths = build_layered_paths(output_dir, project_path, now=now)
    existing_windows = load_pending_windows(paths.pending_path)
    existing_ids = {window.window_id for window in existing_windows}
    new_windows = collect_pending_windows(project_path, new_messages, now=now)
    appended_windows = [window for window in new_windows if window.window_id not in existing_ids]
    if appended_windows:
        save_pending_windows(paths.pending_path, [*existing_windows, *appended_windows])

    state.sessions = updated_sessions
    state.seen_message_ids.update(
        str(_message_attr(message, "message_id")) for message in new_messages if _message_attr(message, "message_id")
    )
    state.last_run_at = (now or datetime.now()).isoformat()
    save_scope_state(paths.ingest_state_path, state)
    return {
        "paths": paths,
        "new_message_count": len(new_messages),
        "pending_window_count": len(appended_windows),
        "queued_window_count": len([window for window in load_pending_windows(paths.pending_path) if window.status == "queued"]),
    }


def process_pending_flush(
    *,
    project_path: str,
    output_dir: Path,
    state: ScopeState,
    llm_call: Callable[..., str],
    llm_backend: str,
    llm_model: str | None,
    dry_run: bool = False,
    now: datetime | None = None,
    cwd: str | None = None,
    idle_minutes: int = DEFAULT_IDLE_FLUSH_MINUTES,
    max_pending_minutes: int = DEFAULT_MAX_PENDING_MINUTES,
) -> dict[str, object]:
    paths = build_layered_paths(output_dir, project_path, now=now)
    windows = load_pending_windows(paths.pending_path)
    selected_windows = selected_pending_windows(windows)
    ready, ready_reason = pending_windows_ready(
        selected_windows,
        now=now,
        idle_minutes=idle_minutes,
        max_pending_minutes=max_pending_minutes,
    )
    pending_messages = pending_messages_for_flush(selected_windows)
    prompt = build_layered_prompt(
        project_path=project_path,
        existing_memory=load_layered_existing_memory(paths.curated_memory_path),
        messages=pending_messages,
    )
    result = {
        "paths": paths,
        "queued_window_count": len([window for window in windows if window.status == "queued"]),
        "selected_window_count": len(selected_windows),
        "pending_message_count": len(pending_messages),
        "prompt_length": len(prompt),
        "ready_to_flush": ready,
        "ready_reason": ready_reason,
        "raw_event_count": 0,
        "searchable_fact_count": len(load_searchable_facts(paths.facts_path)),
        "promoted_count": len([fact for fact in load_searchable_facts(paths.facts_path) if fact.promotion_state == "promoted"]),
        "rewrite_applied": False,
        "parse_error": None,
        "no_pending": not selected_windows,
    }
    if not selected_windows or not pending_messages or dry_run or not ready:
        return result

    raw_response = llm_call(
        prompt,
        backend=llm_backend,
        model=llm_model,
        output_schema=LAYERED_CANDIDATE_JSON_SCHEMA,
        cwd=cwd,
    )
    try:
        candidates = parse_candidate_response(
            raw_response,
            pending_messages,
            default_scope=LAYERED_SCOPE_ONLY,
            now=now,
        )
    except ValueError as exc:
        result["parse_error"] = str(exc)
        return result

    new_events: list[MemoryEvent] = []
    for candidate in candidates:
        event = candidate_to_event(project_path, candidate)
        if not event or event.event_id in state.seen_event_ids:
            continue
        new_events.append(event)

    append_memory_events(paths.raw_daily_path, new_events)
    facts, promoted_items, rewrite_applied = refresh_searchable_and_memory(
        project_path=project_path,
        paths=paths,
        now=now,
        llm_call=llm_call,
        llm_backend=llm_backend,
        llm_model=llm_model,
        dry_run=dry_run,
        cwd=cwd,
        rewrite_memory=True,
    )

    state.seen_event_ids.update(event.event_id for event in new_events)
    state.seen_candidate_hashes.update(candidate.candidate_hash for candidate in candidates)
    state.last_run_at = (now or datetime.now()).isoformat()
    save_scope_state(paths.flush_state_path, state)

    updated_windows = mark_pending_windows_flushed(windows, {window.window_id for window in selected_windows})
    save_pending_windows(paths.pending_path, updated_windows)

    result.update(
        {
            "raw_event_count": len(new_events),
            "searchable_fact_count": len(facts),
            "promoted_count": len(promoted_items),
            "rewrite_applied": rewrite_applied,
        }
    )
    return result


def rewrite_memory_file(
    *,
    project_path: str,
    output_dir: Path,
    llm_call: Callable[..., str],
    llm_backend: str,
    llm_model: str | None,
    dry_run: bool = False,
    now: datetime | None = None,
    cwd: str | None = None,
) -> dict[str, object]:
    paths = build_layered_paths(output_dir, project_path, now=now)
    facts = load_searchable_facts(paths.facts_path)
    deterministic_memory = compile_curated_memory(facts)
    write_text(paths.deterministic_memory_path, deterministic_memory)
    if dry_run:
        return {"paths": paths, "promoted_count": len([fact for fact in facts if fact.promotion_state == "promoted"]), "rewrite_applied": False}
    memory_content, rewrite_applied = rewrite_curated_memory(
        project_path=project_path,
        facts=facts,
        deterministic_memory=deterministic_memory,
        llm_call=llm_call,
        llm_backend=llm_backend,
        llm_model=llm_model,
        dry_run=dry_run,
        cwd=cwd,
    )
    write_text(paths.curated_memory_path, memory_content)
    return {
        "paths": paths,
        "promoted_count": len([fact for fact in facts if fact.promotion_state == "promoted"]),
        "rewrite_applied": rewrite_applied,
    }


def process_capture(
    *,
    project_path: str,
    output_dir: Path,
    state: ScopeState,
    new_messages: Sequence[object],
    updated_sessions: dict[str, SessionCheckpoint],
    llm_call: Callable[..., str],
    llm_backend: str,
    llm_model: str | None,
    dry_run: bool = False,
    now: datetime | None = None,
    cwd: str | None = None,
) -> CaptureResult:
    paths = build_layered_paths(output_dir, project_path, now=now)
    prompt = build_layered_prompt(
        project_path=project_path,
        existing_memory=load_layered_existing_memory(paths.curated_memory_path),
        messages=new_messages,
    )
    result = CaptureResult(
        prompt_length=len(prompt),
        paths=paths,
        new_message_count=len(new_messages),
        no_new_messages=not new_messages,
    )
    if not new_messages or dry_run:
        return result

    raw_response = llm_call(
        prompt,
        backend=llm_backend,
        model=llm_model,
        output_schema=LAYERED_CANDIDATE_JSON_SCHEMA,
        cwd=cwd,
    )
    try:
        candidates = parse_candidate_response(
            raw_response,
            new_messages,
            default_scope=LAYERED_SCOPE_ONLY,
            now=now,
        )
    except ValueError as exc:
        return CaptureResult(
            prompt_length=len(prompt),
            paths=paths,
            new_message_count=len(new_messages),
            parse_error=str(exc),
        )

    new_events: list[MemoryEvent] = []
    for candidate in candidates:
        event = candidate_to_event(project_path, candidate)
        if not event or event.event_id in state.seen_event_ids:
            continue
        new_events.append(event)

    append_memory_events(paths.raw_daily_path, new_events)
    facts, promoted_items, _ = refresh_searchable_and_memory(
        project_path=project_path,
        paths=paths,
        now=now,
        llm_call=llm_call,
        llm_backend=llm_backend,
        llm_model=llm_model,
        dry_run=dry_run,
        cwd=cwd,
        rewrite_memory=True,
    )

    state.sessions = updated_sessions
    state.seen_message_ids.update(
        str(_message_attr(message, "message_id")) for message in new_messages if _message_attr(message, "message_id")
    )
    state.seen_event_ids.update(event.event_id for event in new_events)
    state.seen_candidate_hashes.update(candidate.candidate_hash for candidate in candidates)
    state.last_run_at = (now or datetime.now()).isoformat()
    save_scope_state(paths.state_path, state)

    return CaptureResult(
        prompt_length=len(prompt),
        paths=paths,
        new_message_count=len(new_messages),
        raw_event_count=len(new_events),
        searchable_fact_count=len(facts),
        promoted_count=len(promoted_items),
        recall_count=0,
        no_new_messages=False,
        parse_error=None,
    )
