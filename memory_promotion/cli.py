from __future__ import annotations

import argparse
import os
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


def _discover_project_sessions(project_path: str, source_platforms: list[str]) -> dict[str, list[Path]]:
    paths = extract.PathConfig()
    discovered_sessions: dict[str, dict[str, list[Path]]] = {}
    if "claude" in source_platforms:
        discovered_sessions["claude"] = extract.discover_claude_project_sessions(paths, project_path)
    if "codex" in source_platforms:
        discovered_sessions["codex"] = extract.discover_codex_project_sessions(paths, project_path)
    return extract.project_session_index(discovered_sessions).get(project_path, {})


def run_capture(args: argparse.Namespace) -> int:
    project_path = str(Path(args.project).resolve())
    project_sessions = _discover_project_sessions(project_path, args.source_platforms)
    if not project_sessions:
        print(f"No eligible sessions found for {project_path}.")
        return 0

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
        return 0
    if args.dry_run:
        print(f"  Would send ~{result.prompt_length} chars to LLM via {backend_label}")
        extract.preview_user_messages(new_messages)
        return 0
    if result.parse_error:
        print(f"  Skipping capture — could not parse candidate JSON: {result.parse_error}")
        return 1

    print(f"  Captured raw events: {result.raw_event_count}")
    print(f"  Searchable facts: {result.searchable_fact_count}")
    print(f"  Promoted memory items: {result.promoted_count}")
    return 0


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
        help="Rewrite the curated MEMORY.md from promoted facts using a lightweight LLM pass",
    )
    rewrite.add_argument("--project", default=".", help="Project path to process. Default: current directory.")
    rewrite.add_argument("--output-dir", type=Path, default=Path("./output"))
    rewrite.add_argument("--llm-backend", choices=extract.SUPPORTED_LLM_BACKENDS, default="codex-cli")
    rewrite.add_argument("--llm-model")
    rewrite.add_argument("--dry-run", action="store_true")

    prepare = subparsers.add_parser(
        "prepare-context",
        help="Build the pre-turn context block from promoted memory plus relevant searchable recall",
    )
    prepare.add_argument("--project", default=".", help="Project path to process. Default: current directory.")
    prepare.add_argument("--output-dir", type=Path, default=Path("./output"))
    prepare.add_argument("--prompt", required=True, help="Current user prompt used for lexical recall.")
    prepare.add_argument("--limit", type=int, default=5, help="Maximum recall bullets. Default: 5.")
    prepare.add_argument("--show-stats", action="store_true")

    args = parser.parse_args(argv)
    if args.command in {"capture", "ingest-and-filter"}:
        args.source_platforms = _parse_platforms(args.source_platforms)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "capture":
        return run_capture(args)
    if args.command == "ingest-and-filter":
        return run_ingest_and_filter(args)
    if args.command == "flush-pending":
        return run_flush_pending(args)
    if args.command == "rewrite-memory":
        return run_rewrite_memory(args)
    if args.command == "prepare-context":
        return run_prepare_context(args)
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
