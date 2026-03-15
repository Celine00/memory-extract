#!/usr/bin/env python3
"""Manage the user LaunchAgent for periodic Codex memory flushing."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

LAUNCH_AGENT_LABEL = "com.memoryextract.codex-auto-memory"
DEFAULT_BATCH_LABEL = "com.memoryextract.batch-capture"
DEFAULT_INTERVAL_SECONDS = 300
BATCH_INTERVAL_SECONDS = 43200
LAUNCH_AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()
LOG_DIR = Path("~/Library/Logs/memory-extract").expanduser()
PASSTHROUGH_ENV_VARS = (
    "AIHEZU_OAI_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "HOME",
    "USER",
    "SHELL",
)


def launch_agent_plist_path(label: str = LAUNCH_AGENT_LABEL) -> Path:
    return LAUNCH_AGENTS_DIR / f"{label}.plist"


def build_program_arguments(runner_script: Path) -> list[str]:
    return ["/bin/bash", str(runner_script)]


def build_launch_agent_plist(
    *,
    label: str,
    program_arguments: list[str],
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
    interval_seconds: int,
    environment_variables: dict[str, str],
) -> dict[str, Any]:
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StartInterval": interval_seconds,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "EnvironmentVariables": environment_variables,
    }


def write_launch_agent_plist(plist_path: Path, plist_content: dict[str, Any]) -> None:
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as plist_file:
        plistlib.dump(plist_content, plist_file, sort_keys=True)


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(command: list[str], *, allow_failure: bool = False) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode == 0 or allow_failure:
        return
    stderr = (result.stderr or "").strip() or "<empty>"
    raise RuntimeError(f"launchctl command failed: {' '.join(command)} stderr={stderr}")


def _selected_environment_variables() -> dict[str, str]:
    selected: dict[str, str] = {}
    for key in PASSTHROUGH_ENV_VARS:
        value = os.environ.get(key)
        if value:
            selected[key] = value
    return selected


def _resolve_runner_script(repo_root: Path, runner_script: str) -> Path:
    candidate = Path(runner_script).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Runner script does not exist: {resolved}")
    return resolved


def _install_environment_variables(args: argparse.Namespace) -> dict[str, str]:
    environment_variables = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "LLM_BACKEND": args.llm_backend,
    }
    if getattr(args, "idle_minutes", None) is not None:
        environment_variables["IDLE_MINUTES"] = str(args.idle_minutes)
    if getattr(args, "max_pending_minutes", None) is not None:
        environment_variables["MAX_PENDING_MINUTES"] = str(args.max_pending_minutes)
    if getattr(args, "output_dir", None):
        environment_variables["OUTPUT_DIR"] = str(args.output_dir)
    if getattr(args, "skip_if_recent", None) is not None:
        environment_variables["SKIP_RECENT"] = str(args.skip_if_recent)
    environment_variables.update(_selected_environment_variables())
    if args.llm_model:
        environment_variables["LLM_MODEL"] = args.llm_model
    return environment_variables


def install_launch_agent(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    runner_script = _resolve_runner_script(repo_root, args.runner_script)
    log_stem = args.label.replace("/", "_")
    stdout_path = LOG_DIR / f"{log_stem}.stdout.log"
    stderr_path = LOG_DIR / f"{log_stem}.stderr.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path.touch(exist_ok=True)
    stderr_path.touch(exist_ok=True)

    environment_variables = _install_environment_variables(args)

    plist_path = launch_agent_plist_path(args.label)
    plist_content = build_launch_agent_plist(
        label=args.label,
        program_arguments=build_program_arguments(runner_script),
        working_directory=repo_root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        interval_seconds=args.interval_seconds,
        environment_variables=environment_variables,
    )
    write_launch_agent_plist(plist_path, plist_content)

    domain = _launchctl_domain()
    _run_launchctl(["launchctl", "bootout", domain, str(plist_path)], allow_failure=True)
    _run_launchctl(["launchctl", "bootstrap", domain, str(plist_path)])
    _run_launchctl(["launchctl", "enable", f"{domain}/{args.label}"], allow_failure=True)
    print(f"Installed LaunchAgent: {plist_path}")
    return 0


def uninstall_launch_agent(args: argparse.Namespace) -> int:
    plist_path = launch_agent_plist_path(args.label)
    domain = _launchctl_domain()
    _run_launchctl(["launchctl", "bootout", domain, str(plist_path)], allow_failure=True)
    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed LaunchAgent: {plist_path}")
    else:
        print(f"LaunchAgent was not installed: {plist_path}")
    return 0


def status_launch_agent(args: argparse.Namespace) -> int:
    plist_path = launch_agent_plist_path(args.label)
    if not plist_path.exists():
        print(f"not installed: {plist_path}")
        return 0

    with plist_path.open("rb") as plist_file:
        plist_content = plistlib.load(plist_file)

    environment_variables = plist_content.get("EnvironmentVariables", {})
    program_arguments = plist_content.get("ProgramArguments", [])
    print(f"installed: {plist_path}")
    print(f"label: {plist_content.get('Label')}")
    print(f"interval_seconds: {plist_content.get('StartInterval')}")
    print(f"run_at_load: {plist_content.get('RunAtLoad', False)}")
    print(f"runner: {' '.join(program_arguments) if program_arguments else '<missing>'}")
    print(f"llm_backend: {environment_variables.get('LLM_BACKEND', 'codex-cli')}")
    print(f"idle_minutes: {environment_variables.get('IDLE_MINUTES', '25')}")
    print(f"max_pending_minutes: {environment_variables.get('MAX_PENDING_MINUTES', '90')}")
    if environment_variables.get("OUTPUT_DIR"):
        print(f"output_dir: {environment_variables['OUTPUT_DIR']}")
    if environment_variables.get("SKIP_RECENT"):
        print(f"skip_recent_hours: {environment_variables['SKIP_RECENT']}")
    print(
        "aihezu_oai_key: "
        + ("configured" if environment_variables.get("AIHEZU_OAI_KEY") else "missing")
    )
    print(f"stdout: {plist_content.get('StandardOutPath')}")
    print(f"stderr: {plist_content.get('StandardErrorPath')}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the Codex auto-memory LaunchAgent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="Install or update the LaunchAgent.")
    install_parser.add_argument("--label", default=LAUNCH_AGENT_LABEL)
    install_parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    install_parser.add_argument("--llm-backend", default="codex-cli")
    install_parser.add_argument("--llm-model", default=None)
    install_parser.add_argument("--idle-minutes", type=int, default=25)
    install_parser.add_argument("--max-pending-minutes", type=int, default=90)
    install_parser.add_argument("--runner-script", default="scripts/run-observed-repo-flushes")
    install_parser.set_defaults(handler=install_launch_agent)

    install_batch_parser = subparsers.add_parser(
        "install-batch",
        help="Install or update the twice-daily batch capture LaunchAgent.",
    )
    install_batch_parser.add_argument("--label", default=DEFAULT_BATCH_LABEL)
    install_batch_parser.add_argument("--interval-seconds", type=int, default=BATCH_INTERVAL_SECONDS)
    install_batch_parser.add_argument("--llm-backend", default="codex-cli")
    install_batch_parser.add_argument("--llm-model", default=None)
    install_batch_parser.add_argument("--runner-script", default="scripts/run-batch-capture")
    install_batch_parser.add_argument("--output-dir", default="./output")
    install_batch_parser.add_argument("--skip-if-recent", type=int, default=6)
    install_batch_parser.set_defaults(
        handler=install_launch_agent,
        idle_minutes=None,
        max_pending_minutes=None,
    )

    uninstall_parser = subparsers.add_parser("uninstall", help="Unload and remove the LaunchAgent.")
    uninstall_parser.add_argument("--label", default=LAUNCH_AGENT_LABEL)
    uninstall_parser.set_defaults(handler=uninstall_launch_agent)

    status_parser = subparsers.add_parser("status", help="Show installed LaunchAgent details.")
    status_parser.add_argument("--label", default=LAUNCH_AGENT_LABEL)
    status_parser.set_defaults(handler=status_launch_agent)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
