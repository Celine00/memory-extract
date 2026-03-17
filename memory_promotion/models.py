from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SUPPORTED_SCOPES = ("project", "global")
LAYERED_SCOPE_ONLY = "project"
STATE_VERSION = 2
EXTRACTION_VERSION = "memory-promotion-v1"
LOG_RECORD_PREFIX = "<!-- memory-extract:"
MEMORY_LINE_LIMIT = 180
SOFT_MEMORY_LINE_TARGET = 100
MAX_RECALL_ITEMS = 5
DEFAULT_IDLE_FLUSH_MINUTES = 25
DEFAULT_MAX_PENDING_MINUTES = 90
DEFAULT_FLUSH_POLL_MINUTES = 5
MAX_PENDING_WINDOWS_PER_FLUSH = 32
PENDING_WINDOW_STATUSES = ("queued", "flushed", "dropped")

CANDIDATE_CATEGORIES = (
    "language",
    "documentation_style",
    "communication",
    "workflow",
    "tooling",
    "project_context",
    "explicit_request",
    "other",
)

CATEGORY_TITLES = {
    "language": "Language Preference",
    "documentation_style": "Documentation Style",
    "communication": "Communication Style",
    "workflow": "Workflow Patterns",
    "tooling": "Tools And Stack",
    "project_context": "Project Context",
    "explicit_request": "Explicit Requests",
    "other": "Other Durable Notes",
}

SIGNAL_TYPES = ("explicit", "implicit", "project_constraint")
FACT_STATUSES = ("active", "tentative", "contradicted", "demoted", "archived")
PROMOTION_STATES = ("never", "candidate", "promoted", "demoted")

CATEGORY_PRIORITY = {
    "explicit_request": 0,
    "documentation_style": 1,
    "communication": 2,
    "workflow": 3,
    "tooling": 4,
    "project_context": 5,
    "language": 6,
    "other": 7,
}


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
class MemoryEvent:
    event_id: str
    project_path: str
    session_file: str
    jsonl_line_range: tuple[int, int]
    observed_at: str
    role_window_hash: str
    candidate_text: str
    normalized_text: str
    category: str
    durability: str
    signal_type: str
    evidence: tuple[EvidenceRef, ...]
    source_platform: str
    turn_id: str
    extraction_version: str = EXTRACTION_VERSION


@dataclass(frozen=True)
class SearchableFact:
    fact_id: str
    project_path: str
    canonical_text: str
    display_text: str
    category: str
    status: str
    support_count: int
    distinct_turn_count: int
    distinct_session_count: int
    first_observed_at: str
    last_observed_at: str
    explicit_signal: bool
    project_constraint_signal: bool
    source_event_ids: tuple[str, ...]
    token_index: tuple[str, ...]
    promotion_state: str


@dataclass(frozen=True)
class PromotedMemoryItem:
    fact_id: str
    display_text: str
    category: str
    promotion_reason: str
    rank: int


@dataclass(frozen=True)
class RecallItem:
    fact_id: str
    display_text: str
    score: float


@dataclass(frozen=True)
class PendingMessage:
    message_id: str
    role: str
    content: str
    timestamp: str
    session_file: str
    jsonl_line: int
    platform: str


@dataclass(frozen=True)
class PendingWindow:
    window_id: str
    project_path: str
    platform: str
    session_file: str
    first_timestamp: str
    last_timestamp: str
    message_ids: tuple[str, ...]
    messages: tuple[PendingMessage, ...]
    jsonl_line_range: tuple[int, int]
    reason_codes: tuple[str, ...]
    excerpt: str
    status: str = "queued"
    queued_at: str = ""


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
    deterministic_memory_path: Path
    memory_dir: Path
    raw_dir: Path
    raw_daily_path: Path
    pending_dir: Path
    pending_path: Path
    searchable_dir: Path
    facts_path: Path
    archive_dir: Path
    archive_daily_path: Path
    audit_dir: Path
    audit_daily_path: Path
    state_path: Path
    ingest_state_path: Path
    flush_state_path: Path


@dataclass(frozen=True)
class CaptureResult:
    prompt_length: int
    paths: LayeredPaths
    new_message_count: int
    raw_event_count: int = 0
    searchable_fact_count: int = 0
    promoted_count: int = 0
    recall_count: int = 0
    no_new_messages: bool = False
    parse_error: str | None = None


def slugify_project_path(project_path: str | None) -> str:
    if not project_path:
        return "global"
    parts = [part for part in Path(project_path).parts if part not in {"/", ""}]
    tail = parts[-2:] if len(parts) >= 2 else parts[-1:]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(tail))
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "root"


def parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def format_timestamp(raw: str) -> str:
    parsed = parse_timestamp(raw)
    if not parsed:
        return raw
    return parsed.strftime("%Y-%m-%d %H:%M")


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


def infer_signal_type(category: str) -> str:
    if category == "explicit_request":
        return "explicit"
    if category == "project_context":
        return "project_constraint"
    return "implicit"


def normalize_signal_type(raw: object, category: str) -> str:
    value = str(raw or "").strip().lower()
    if value in SIGNAL_TYPES:
        return value
    return infer_signal_type(category)


def build_candidate_hash(
    text: str,
    category: str,
    scope: str,
    durability: str,
    signal_type: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(
        "||".join(
            [
                candidate_text_key(text),
                category,
                scope,
                durability,
                signal_type,
            ]
        ).encode("utf-8")
    )
    return digest.hexdigest()[:16]


def dedupe_evidence_refs(evidence: list[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    unique: list[EvidenceRef] = []
    seen = set()
    for item in evidence:
        key = (item.platform, item.session_file, item.jsonl_line, item.timestamp)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= 5:
            break
    return tuple(unique)


def build_role_window_hash(candidate_text: str, evidence: tuple[EvidenceRef, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(candidate_text_key(candidate_text).encode("utf-8"))
    for ref in evidence:
        digest.update(
            f"{ref.platform}|{ref.session_file}|{ref.jsonl_line}|{ref.timestamp}".encode("utf-8")
        )
    return digest.hexdigest()[:16]


def build_event_id(
    project_path: str,
    candidate_hash: str,
    evidence: tuple[EvidenceRef, ...],
    role_window_hash: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(project_path.encode("utf-8"))
    digest.update(candidate_hash.encode("utf-8"))
    digest.update(role_window_hash.encode("utf-8"))
    for ref in evidence:
        digest.update(f"{ref.session_file}:{ref.jsonl_line}".encode("utf-8"))
    return digest.hexdigest()[:24]


def build_fact_id(project_path: str, canonical_text: str, category: str) -> str:
    digest = hashlib.sha256()
    digest.update(project_path.encode("utf-8"))
    digest.update(candidate_text_key(canonical_text).encode("utf-8"))
    digest.update(category.encode("utf-8"))
    return digest.hexdigest()[:24]


def tokenize_text(text: str) -> tuple[str, ...]:
    return tuple(sorted({token for token in re.findall(r"[A-Za-z0-9_./-]+", text.casefold()) if token}))
