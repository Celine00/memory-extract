from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import extract

from .models import (
    DEFAULT_IDLE_FLUSH_MINUTES,
    DEFAULT_MAX_PENDING_MINUTES,
    LAYERED_SCOPE_ONLY,
)
from .pipeline import (
    build_layered_paths,
    build_prepare_context,
    load_scope_state,
    process_capture,
    process_ingest,
    process_pending_flush,
    rewrite_memory_file,
)


def _parse_platforms(raw: str) -> list[str]:
    return extract.parse_platforms(raw)


def _resolve_backend_label(backend: str, dry_run: bool) -> str:
    if dry_run:
        return backend
    return extract.resolve_llm_backend(backend)


@dataclass(frozen=True)
class CaptureProjectResult:
    project_path: str
    exit_code: int
    had_sessions: bool
    new_message_count: int = 0
    raw_event_count: int = 0
    searchable_fact_count: int = 0
    promoted_count: int = 0
    no_new_messages: bool = False
    parse_error: str = ""


def _discover_project_sessions(project_path: str, source_platforms: list[str]) -> dict[str, list[Path]]:
    paths = extract.PathConfig()
    discovered_sessions: dict[str, dict[str, list[Path]]] = {}
    if "claude" in source_platforms:
        discovered_sessions["claude"] = extract.discover_claude_project_sessions(paths, project_path)
    if "codex" in source_platforms:
        discovered_sessions["codex"] = extract.discover_codex_project_sessions(paths, project_path)
    return extract.project_session_index(discovered_sessions).get(project_path, {})


def _normalize_existing_project_path(project_path: str) -> str | None:
    raw_path = project_path.strip()
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.exists():
        return None
    return str(candidate.resolve())


def discover_all_project_paths(
    source_platforms: list[str],
    *,
    paths: extract.PathConfig | None = None,
) -> list[str]:
    paths = paths or extract.PathConfig()
    discovered: set[str] = set()

    if "claude" in source_platforms:
        history_index = extract.load_claude_history_index(paths)
        projects_dir = paths.claude_projects_dir
        if projects_dir.exists():
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue

                session_files = sorted(project_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime)
                resolved_path = None
                for session_file in session_files:
                    mapped_project = history_index.get(session_file.stem) or extract.resolve_claude_project_path(
                        project_dir.name
                    )
                    resolved_path = _normalize_existing_project_path(mapped_project)
                    if resolved_path:
                        break

                if not resolved_path:
                    inferred_project = extract.resolve_claude_project_path(project_dir.name)
                    resolved_path = _normalize_existing_project_path(inferred_project)

                if resolved_path:
                    discovered.add(resolved_path)

    if "codex" in source_platforms:
        for project_path in extract.discover_codex_project_sessions(paths, None):
            normalized = _normalize_existing_project_path(project_path)
            if normalized:
                discovered.add(normalized)

    return sorted(discovered)


def _capture_project(
    args: argparse.Namespace,
    *,
    project_path: str | None = None,
    project_sessions: dict[str, list[Path]] | None = None,
) -> CaptureProjectResult:
    project_path = str(Path(project_path or args.project).resolve())
    if project_sessions is None:
        project_sessions = _discover_project_sessions(project_path, args.source_platforms)
    if not project_sessions:
        print(f"No eligible sessions found for {project_path}.")
        return CaptureProjectResult(project_path=project_path, exit_code=0, had_sessions=False)

    scope_key = extract.ScopeKey(scope=LAYERED_SCOPE_ONLY, project_path=project_path)
    paths = build_layered_paths(args.output_dir, project_path)
    state = load_scope_state(paths.state_path, project_path)
    new_messages, updated_sessions = extract.collect_incremental_scope_messages(
        scope_key,
        project_sessions,
        state,
    )
    backend_label = _resolve_backend_label(args.llm_backend, args.dry_run)

    print(f"Processing layered capture: {project_path}")
    print(f"  New messages since last checkpoint: {len(new_messages)}")
    print(f"  Raw log: {paths.raw_daily_path}")
    print(f"  Searchable facts: {paths.facts_path}")
    print(f"  Audit: {paths.audit_daily_path}")
    print(f"  Curated memory: {paths.curated_memory_path}")
    print(f"  State: {paths.state_path}")

    result = process_capture(
        project_path=project_path,
        output_dir=args.output_dir,
        state=state,
        new_messages=new_messages,
        updated_sessions=updated_sessions,
        llm_call=extract.call_llm,
        llm_backend=args.llm_backend,
        llm_model=args.llm_model,
        dry_run=args.dry_run,
        cwd=os.getcwd(),
    )

    if result.no_new_messages:
        print("  No new messages to process.")
        return CaptureProjectResult(
            project_path=project_path,
            exit_code=0,
            had_sessions=True,
            new_message_count=len(new_messages),
            no_new_messages=True,
        )
    if args.dry_run:
        print(f"  Would send ~{result.prompt_length} chars to LLM via {backend_label}")
        extract.preview_user_messages(new_messages)
        return CaptureProjectResult(
            project_path=project_path,
            exit_code=0,
            had_sessions=True,
            new_message_count=len(new_messages),
        )
    if result.parse_error:
        print(f"  Skipping capture — could not parse candidate JSON: {result.parse_error}")
        return CaptureProjectResult(
            project_path=project_path,
            exit_code=1,
            had_sessions=True,
            new_message_count=len(new_messages),
            parse_error=result.parse_error,
        )

    print(f"  Captured raw events: {result.raw_event_count}")
    print(f"  Searchable facts: {result.searchable_fact_count}")
    print(f"  Promoted memory items: {result.promoted_count}")
    return CaptureProjectResult(
        project_path=project_path,
        exit_code=0,
        had_sessions=True,
        new_message_count=len(new_messages),
        raw_event_count=result.raw_event_count,
        searchable_fact_count=result.searchable_fact_count,
        promoted_count=result.promoted_count,
    )


def run_capture(args: argparse.Namespace) -> int:
    return _capture_project(args).exit_code


def _should_skip_recent(project_path: str, output_dir: Path, skip_if_recent_hours: int) -> bool:
    if skip_if_recent_hours <= 0:
        return False
    state_path = build_layered_paths(output_dir, project_path).state_path
    if not state_path.exists():
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=skip_if_recent_hours)
    updated_at = datetime.fromtimestamp(state_path.stat().st_mtime, tz=timezone.utc)
    return updated_at >= cutoff


def run_capture_all(args: argparse.Namespace) -> int:
    discovered_projects = discover_all_project_paths(args.source_platforms)
    if not discovered_projects:
        print("No eligible projects discovered for batch capture.")
        return 0

    selected_projects = discovered_projects
    if args.max_projects > 0:
        selected_projects = discovered_projects[: args.max_projects]

    processed_count = 0
    skipped_recent_count = 0
    skipped_no_sessions_count = 0
    error_count = 0
    total_new_messages = 0
    total_raw_events = 0

    print(f"Batch capture discovering {len(discovered_projects)} project(s).")
    if args.max_projects > 0:
        print(f"  Limiting run to {len(selected_projects)} project(s) via --max-projects {args.max_projects}.")

    for index, project_path in enumerate(selected_projects, start=1):
        print("")
        print(f"[{index}/{len(selected_projects)}] {project_path}")
        if _should_skip_recent(project_path, args.output_dir, args.skip_if_recent):
            print(f"  Skipping recent project; state updated within {args.skip_if_recent} hour(s).")
            skipped_recent_count += 1
            continue

        project_sessions = _discover_project_sessions(project_path, args.source_platforms)
        if not project_sessions:
            print("  Skipping project with no eligible sessions.")
            skipped_no_sessions_count += 1
            continue

        project_args = argparse.Namespace(
            project=project_path,
            output_dir=args.output_dir,
            llm_backend=args.llm_backend,
            llm_model=args.llm_model,
            source_platforms=args.source_platforms,
            dry_run=args.dry_run,
        )

        try:
            result = _capture_project(
                project_args,
                project_path=project_path,
                project_sessions=project_sessions,
            )
        except Exception as exc:  # pragma: no cover - defensive batch isolation
            print(f"  Error: {exc}")
            error_count += 1
            continue

        processed_count += 1
        total_new_messages += result.new_message_count
        total_raw_events += result.raw_event_count
        if result.exit_code != 0:
            error_count += 1

    print("")
    print(
        "Batch complete: "
        f"{len(discovered_projects)} projects discovered, "
        f"{processed_count} processed, "
        f"{skipped_recent_count} skipped (recent), "
        f"{skipped_no_sessions_count} skipped (no sessions)"
    )
    if args.max_projects > 0 and len(selected_projects) < len(discovered_projects):
        print(f"  Selected this run: {len(selected_projects)}")
    print(f"  Total new messages: {total_new_messages}")
    print(f"  Total raw events captured: {total_raw_events}")
    print(f"  Errors: {error_count}")
    return 1 if error_count else 0


def run_ingest_and_filter(args: argparse.Namespace) -> int:
    project_path = str(Path(args.project).resolve())
    project_sessions = _discover_project_sessions(project_path, args.source_platforms)
    if not project_sessions:
        print(f"No eligible sessions found for {project_path}.")
        return 0

    scope_key = extract.ScopeKey(scope=LAYERED_SCOPE_ONLY, project_path=project_path)
    paths = build_layered_paths(args.output_dir, project_path)
    state = load_scope_state(paths.ingest_state_path, project_path)
    new_messages, updated_sessions = extract.collect_incremental_scope_messages(
        scope_key,
        project_sessions,
        state,
    )
    print(f"Processing layered ingest: {project_path}")
    print(f"  New messages since last ingest: {len(new_messages)}")
    print(f"  Pending queue: {paths.pending_path}")
    print(f"  Ingest state: {paths.ingest_state_path}")
    if args.dry_run:
        extract.preview_user_messages(new_messages)
        return 0

    result = process_ingest(
        project_path=project_path,
        output_dir=args.output_dir,
        state=state,
        new_messages=new_messages,
        updated_sessions=updated_sessions,
    )
    print(f"  Added pending windows: {result['pending_window_count']}")
    print(f"  Queued windows: {result['queued_window_count']}")
    return 0


def run_flush_pending(args: argparse.Namespace) -> int:
    project_path = str(Path(args.project).resolve())
    paths = build_layered_paths(args.output_dir, project_path)
    state = load_scope_state(paths.flush_state_path, project_path)
    backend_label = _resolve_backend_label(args.llm_backend, args.dry_run)
    print(f"Processing layered flush: {project_path}")
    print(f"  Pending queue: {paths.pending_path}")
    print(f"  Flush state: {paths.flush_state_path}")
    result = process_pending_flush(
        project_path=project_path,
        output_dir=args.output_dir,
        state=state,
        llm_call=extract.call_llm,
        llm_backend=args.llm_backend,
        llm_model=args.llm_model,
        dry_run=args.dry_run,
        cwd=os.getcwd(),
        idle_minutes=args.idle_minutes,
        max_pending_minutes=args.max_pending_minutes,
    )
    if result["no_pending"]:
        print("  No pending windows to flush.")
        return 0
    print(f"  Selected windows: {result['selected_window_count']}")
    print(f"  Pending messages: {result['pending_message_count']}")
    if not result["ready_to_flush"]:
        print(f"  Not ready to flush yet. Waiting for idle/max window. queued_reason={result['ready_reason']}")
        return 0
    if args.dry_run:
        print(f"  Would send ~{result['prompt_length']} chars to LLM via {backend_label}")
        return 0
    if result["parse_error"]:
        print(f"  Skipping flush — could not parse candidate JSON: {result['parse_error']}")
        return 1
    print(f"  Captured raw events: {result['raw_event_count']}")
    print(f"  Searchable facts: {result['searchable_fact_count']}")
    print(f"  Promoted memory items: {result['promoted_count']}")
    print(f"  MEMORY rewritten: {result['rewrite_applied']}")
    return 0


def run_rewrite_memory(args: argparse.Namespace) -> int:
    project_path = str(Path(args.project).resolve())
    backend_label = _resolve_backend_label(args.llm_backend, args.dry_run)
    print(f"Rewriting layered memory: {project_path}")
    result = rewrite_memory_file(
        project_path=project_path,
        output_dir=args.output_dir,
        llm_call=extract.call_llm,
        llm_backend=args.llm_backend,
        llm_model=args.llm_model,
        dry_run=args.dry_run,
        cwd=os.getcwd(),
    )
    if args.dry_run:
        print(f"  Would rewrite MEMORY via {backend_label}")
        return 0
    print(f"  Promoted memory items: {result['promoted_count']}")
    print(f"  MEMORY rewritten: {result['rewrite_applied']}")
    return 0


def run_prepare_context(args: argparse.Namespace) -> int:
    project_path = str(Path(args.project).resolve())
    context, recall_items = build_prepare_context(
        project_path=project_path,
        output_dir=args.output_dir,
        query=args.prompt,
        limit=args.limit,
    )
    print(context)
    if args.show_stats:
        print("")
        print(f"[recall_count={len(recall_items)}]")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local memory promotion runtime (Codex + Claude)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="Incrementally capture new transcript content")
    capture.add_argument("--project", default=".", help="Project path to process. Default: current directory.")
    capture.add_argument("--output-dir", type=Path, default=Path("./output"))
    capture.add_argument("--llm-backend", choices=extract.SUPPORTED_LLM_BACKENDS, default="codex-cli")
    capture.add_argument("--llm-model")
    capture.add_argument("--source-platforms", default="codex,claude")
    capture.add_argument("--dry-run", action="store_true")

    capture_all = subparsers.add_parser(
        "capture-all",
        help="Batch capture all discovered Claude/Codex projects",
    )
    capture_all.add_argument("--output-dir", type=Path, default=Path("./output"))
    capture_all.add_argument("--llm-backend", choices=extract.SUPPORTED_LLM_BACKENDS, default="codex-cli")
    capture_all.add_argument("--llm-model")
    capture_all.add_argument("--source-platforms", default="codex,claude")
    capture_all.add_argument(
        "--max-projects",
        type=int,
        default=0,
        help="Maximum number of discovered projects to process. 0 = unlimited.",
    )
    capture_all.add_argument(
        "--skip-if-recent",
        type=int,
        default=0,
        help="Skip projects whose capture state was updated within N hours. 0 = disabled.",
    )
    capture_all.add_argument("--dry-run", action="store_true")

    ingest = subparsers.add_parser(
        "ingest-and-filter",
        help="Incrementally ingest new transcript content and queue high-recall pending windows",
    )
    ingest.add_argument("--project", default=".", help="Project path to process. Default: current directory.")
    ingest.add_argument("--output-dir", type=Path, default=Path("./output"))
    ingest.add_argument("--source-platforms", default="codex,claude")
    ingest.add_argument("--dry-run", action="store_true")

    flush = subparsers.add_parser(
        "flush-pending",
        help="Flush queued pending windows through the LLM when idle/max thresholds are met",
    )
    flush.add_argument("--project", default=".", help="Project path to process. Default: current directory.")
    flush.add_argument("--output-dir", type=Path, default=Path("./output"))
    flush.add_argument("--llm-backend", choices=extract.SUPPORTED_LLM_BACKENDS, default="codex-cli")
    flush.add_argument("--llm-model")
    flush.add_argument("--idle-minutes", type=int, default=DEFAULT_IDLE_FLUSH_MINUTES)
    flush.add_argument("--max-pending-minutes", type=int, default=DEFAULT_MAX_PENDING_MINUTES)
    flush.add_argument("--dry-run", action="store_true")

    rewrite = subparsers.add_parser(
        "rewrite-memory",
        help="Rewrite the curated MEMORY.md from promoted facts using a lightweight LLM pass"
[…]