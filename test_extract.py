import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import extract
import prompts as prompt_templates
from memory_promotion import cli as promotion_cli
from memory_promotion import pipeline as promotion_pipeline


class CodexParsingTests(unittest.TestCase):
    def test_clean_codex_user_content_strips_scaffolding(self):
        raw = """# AGENTS.md instructions for /tmp/project

<INSTRUCTIONS>
ignore this
</INSTRUCTIONS>
<environment_context>
  <cwd>/tmp/project</cwd>
</environment_context>
[SYSTEM]
You are a planner.

[USER]
Implement the fix
"""
        self.assertEqual(extract.clean_codex_user_content(raw), "Implement the fix")

    def test_load_codex_session_messages_keeps_only_real_user_and_assistant_messages(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            session_path = Path(tmp_dir) / "session.jsonl"
            entries = [
                {"type": "session_meta", "payload": {"cwd": "/tmp/project"}},
                {
                    "timestamp": "2026-03-09T10:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "# AGENTS.md instructions for /tmp/project\n\n"
                                "<INSTRUCTIONS>\nignore this\n</INSTRUCTIONS>\n"
                                "<environment_context>\n  <cwd>/tmp/project</cwd>\n</environment_context>\n"
                                "[SYSTEM]\nplanner\n\n[USER]\nImplement the fix",
                            }
                        ],
                    },
                },
                {
                    "timestamp": "2026-03-09T10:00:05Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "duplicate commentary"},
                },
                {
                    "timestamp": "2026-03-09T10:00:06Z",
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "exec_command", "arguments": "{}"},
                },
                {
                    "timestamp": "2026-03-09T10:00:10Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Implemented it"}],
                    },
                },
            ]
            session_path.write_text("\n".join(json.dumps(entry) for entry in entries))

            result = extract.load_codex_session_messages(session_path, "/tmp/project")

        self.assertEqual(
            [(message.platform, message.role, message.content) for message in result.messages],
            [
                ("codex", "user", "Implement the fix"),
                ("codex", "assistant", "Implemented it"),
            ],
        )
        self.assertEqual(result.total_jsonl_lines, 5)


class WriterConfigTests(unittest.TestCase):
    def test_resolve_target_path_uses_scope_specific_templates(self):
        config = extract.WriterConfig(
            claude_project_memory_template="/tmp/claude/{claude_encoded_project}/memory/MEMORY.md",
            claude_global_memory_path=None,
            codex_project_memory_template="/tmp/codex/{project_slug}/MEMORY.md",
            codex_global_memory_path="/tmp/codex/MEMORY.md",
        )

        project_scope = extract.ScopeKey(scope="project", project_path="/Users/demo/repo")
        global_scope = extract.ScopeKey(scope="global")

        self.assertEqual(
            extract.resolve_target_path("claude", project_scope, config),
            Path("/tmp/claude/-Users-demo-repo/memory/MEMORY.md"),
        )
        self.assertEqual(
            extract.resolve_target_path("codex", project_scope, config),
            Path("/tmp/codex/demo-repo/MEMORY.md"),
        )
        self.assertEqual(
            extract.resolve_target_path("codex", global_scope, config),
            Path("/tmp/codex/MEMORY.md"),
        )
        self.assertIsNone(extract.resolve_target_path("claude", global_scope, config))

    def test_slugify_project_path_uses_last_two_segments(self):
        self.assertEqual(
            extract.slugify_project_path("/Users/celinezou/Celine00/memory-extract"),
            "Celine00-memory-extract",
        )
        self.assertEqual(
            extract.slugify_project_path("/Users/demo/repo"),
            "demo-repo",
        )


class ClaudeDiscoveryTests(unittest.TestCase):
    def test_discover_claude_project_sessions_prefers_history_project_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            project_dir = home / ".claude" / "projects" / "-Users-demo-repo-name"
            project_dir.mkdir(parents=True)
            session_path = project_dir / "session-123.jsonl"
            session_path.write_text("{}\n")

            history_path = home / ".claude" / "history.jsonl"
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history_path.write_text(
                json.dumps(
                    {
                        "project": "/Users/demo/repo-name",
                        "sessionId": "session-123",
                        "timestamp": 1,
                        "display": "hello",
                    }
                )
            )

            discovered = extract.discover_claude_project_sessions(
                extract.PathConfig(home=home),
                project_filter=None,
            )

        self.assertEqual(discovered, {"/Users/demo/repo-name": [session_path]})


class ClaudeCleaningTests(unittest.TestCase):
    def test_clean_claude_content_drops_local_command_scaffolding(self):
        self.assertEqual(extract.clean_claude_content("<local-command-stdout>pwd</local-command-stdout>"), "")
        self.assertEqual(extract.clean_claude_content("/mcp"), "")
        self.assertEqual(extract.clean_claude_content("[Request interrupted by user for tool use]"), "")
        self.assertEqual(extract.clean_claude_content("Explain the repo"), "Explain the repo")


class ProjectFilterTests(unittest.TestCase):
    def test_matches_project_filter_requires_exact_path(self):
        self.assertTrue(
            extract.matches_project_filter(
                "/Users/celinezou/Celine00/memory-extract",
                "/Users/celinezou/Celine00/memory-extract",
            )
        )
        self.assertFalse(
            extract.matches_project_filter(
                "/Users/celinezou/Celine00",
                "/Users/celinezou/Celine00/memory-extract",
            )
        )


class LlmBackendTests(unittest.TestCase):
    @patch.dict("extract.os.environ", {}, clear=True)
    @patch("extract.command_available")
    @patch("extract.anthropic_sdk_available", return_value=False)
    def test_resolve_llm_backend_auto_prefers_claude_cli_without_api_key(
        self,
        _mock_sdk,
        mock_command_available,
    ):
        mock_command_available.side_effect = lambda name: name == "claude"

        self.assertEqual(extract.resolve_llm_backend("auto"), "claude-cli")

    @patch.dict("extract.os.environ", {}, clear=True)
    @patch("extract.command_available")
    @patch("extract.anthropic_sdk_available", return_value=False)
    def test_resolve_llm_backend_auto_falls_back_to_codex_cli(
        self,
        _mock_sdk,
        mock_command_available,
    ):
        mock_command_available.side_effect = lambda name: name == "codex"

        self.assertEqual(extract.resolve_llm_backend("auto"), "codex-cli")

    def test_build_claude_cli_command_supports_model_and_schema(self):
        command = extract.build_claude_cli_command(
            model="claude-sonnet-4-6",
            output_schema=extract.LAYERED_CANDIDATE_JSON_SCHEMA,
        )

        self.assertIn("--print", command)
        self.assertIn("--tools", command)
        self.assertIn("", command)
        self.assertIn("--model", command)
        self.assertIn("claude-sonnet-4-6", command)
        self.assertIn("--json-schema", command)

    @patch("extract.subprocess.run")
    def test_run_codex_cli_reads_output_file(self, mock_run):
        def fake_run(command, input, text, capture_output, cwd, check):
            self.assertIn("codex", command[0])
            self.assertIn("exec", command)
            self.assertIn("--output-last-message", command)
            self.assertIn("--output-schema", command)
            self.assertEqual(input, "prompt body")
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text('{"candidates":[]}')
            return SimpleNamespace(stdout="", stderr="")

        mock_run.side_effect = fake_run

        response = extract.run_codex_cli(
            "prompt body",
            model="gpt-5",
            output_schema=extract.LAYERED_CANDIDATE_JSON_SCHEMA,
            cwd="/tmp/project",
        )

        self.assertEqual(response, '{"candidates":[]}')

    @patch("extract.run_claude_cli", return_value="hello")
    @patch("extract.resolve_llm_backend", return_value="claude-cli")
    def test_call_llm_routes_to_claude_cli(self, _mock_backend, mock_claude):
        response = extract.call_llm("hello", backend="auto", model="sonnet")

        self.assertEqual(response, "hello")
        mock_claude.assert_called_once()


class PromptTemplateTests(unittest.TestCase):
    def tearDown(self):
        prompt_templates.load_prompt.cache_clear()

    def test_build_prompt_renders_external_template(self):
        prompt = extract.build_prompt(
            extract.ScopeKey(scope="project", project_path="/tmp/project"),
            "# Existing memory",
            [
                extract.NormalizedMessage(
                    platform="codex",
                    project_path="/tmp/project",
                    role="user",
                    content="Implement tests",
                )
            ],
        )

        self.assertIn("Memory scope: project", prompt)
        self.assertIn("Project path: /tmp/project", prompt)
        self.assertIn("# Existing memory", prompt)
        self.assertIn("Implement tests", prompt)

    def test_build_layered_prompt_renders_external_template(self):
        prompt = promotion_pipeline.build_layered_prompt(
            project_path="/tmp/project",
            existing_memory="# Existing memory",
            messages=[
                extract.NormalizedMessage(
                    platform="claude",
                    project_path="/tmp/project",
                    role="assistant",
                    content="Use python3 -m unittest -q",
                    message_id="msg-1",
                )
            ],
        )

        self.assertIn("Scope: project", prompt)
        self.assertIn("Project path: /tmp/project", prompt)
        self.assertIn("Return JSON only. No markdown fences. No prose.", prompt)
        self.assertIn("Use python3 -m unittest -q", prompt)

    def test_load_prompt_raises_clear_error_for_missing_template(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_dir = Path(tmp_dir)
            missing_path = missing_dir / "missing-template.md"
            with patch.object(prompt_templates, "PROMPTS_DIR", missing_dir):
                prompt_templates.load_prompt.cache_clear()
                with self.assertRaises(RuntimeError) as context:
                    prompt_templates.load_prompt("missing-template")

        message = str(context.exception)
        self.assertIn("missing-template", message)
        self.assertIn(str(missing_path), message)


class LayeredModeTests(unittest.TestCase):
    def write_codex_session(self, session_path: Path, messages: list[tuple[str, str]]) -> None:
        entries = [{"type": "session_meta", "payload": {"cwd": "/tmp/project"}}]
        for index, (role, text) in enumerate(messages):
            block_type = "input_text" if role == "user" else "output_text"
            entries.append(
                {
                    "timestamp": f"2026-03-09T10:00:{index:02d}Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": [{"type": block_type, "text": text}],
                    },
                }
            )
        session_path.write_text("\n".join(json.dumps(entry) for entry in entries))

    def test_parse_args_requires_output_dir_for_layered_mode(self):
        with self.assertRaises(SystemExit):
            extract.parse_args(["--memory-mode", "layered"])

    def test_parse_args_rejects_non_project_scope_for_layered_mode(self):
        with self.assertRaises(SystemExit):
            extract.parse_args(["--memory-mode", "layered", "--scope", "global", "--output-dir", "/tmp/out"])

    def test_parse_candidate_response_maps_evidence_ids(self):
        message = extract.build_message(
            platform="codex",
            project_path="/tmp/project",
            role="user",
            content="Keep answers concise.",
            timestamp="2026-03-09T10:00:00Z",
            session_file="/tmp/project/session.jsonl",
            jsonl_line=2,
        )
        raw = json.dumps(
            {
                "candidates": [
                    {
                        "text": "User prefers concise repo updates.",
                        "category": "communication",
                        "durability": "durable",
                        "scope": "project",
                        "evidence_ids": [message.message_id],
                    }
                ]
            }
        )

        candidates = extract.parse_candidate_response(raw, [message], default_scope="project")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "communication")
        self.assertEqual(candidates[0].signal_type, "implicit")
        self.assertEqual(candidates[0].evidence[0].jsonl_line, 2)
        self.assertEqual(candidates[0].evidence[0].platform, "codex")

    def test_collect_incremental_scope_messages_rescans_on_shrink_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            session_path = Path(tmp_dir) / "session.jsonl"
            self.write_codex_session(
                session_path,
                [
                    ("user", "Keep answers concise."),
                    ("assistant", "Noted."),
                    ("user", "Use unittest for verification."),
                ],
            )
            full_result = extract.load_codex_session_messages(session_path, "/tmp/project")
            original_stat = session_path.stat()
            state = extract.ScopeState(
                version=extract.STATE_VERSION,
                project_path="/tmp/project",
                sessions={
                    extract.stable_path(session_path): extract.SessionCheckpoint(
                        size=original_stat.st_size,
                        mtime_ms=original_stat.st_mtime,
                        last_jsonl_line=full_result.total_jsonl_lines,
                    )
                },
                seen_message_ids={message.message_id for message in full_result.messages},
            )

            self.write_codex_session(session_path, [("user", "Keep answers concise.")])
            scope_key = extract.ScopeKey(scope="project", project_path="/tmp/project")
            messages, updated_sessions = extract.collect_incremental_scope_messages(
                scope_key,
                {"codex": [session_path]},
                state,
            )

        self.assertEqual(messages, [])
        checkpoint = updated_sessions[extract.stable_path(session_path)]
        self.assertLess(checkpoint.size, original_stat.st_size)

    def test_process_layered_scope_is_incremental_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "out"
            session_path = root / "session.jsonl"
            now = datetime(2026, 3, 9, 12, 0, 0)
            scope_key = extract.ScopeKey(scope="project", project_path="/tmp/project")
            args = SimpleNamespace(
                output_dir=output_dir,
                dry_run=False,
                llm_backend="auto",
                llm_model=None,
            )

            self.write_codex_session(
                session_path,
                [
                    ("user", "Keep repo updates concise."),
                    ("assistant", "Will do."),
                ],
            )
            first_messages = extract.load_codex_session_messages(session_path, "/tmp/project").messages
            first_user_id = next(message.message_id for message in first_messages if message.role == "user")

            call_count = {"value": 0}
            responses: list[str] = [
                json.dumps(
                    {
                        "candidates": [
                            {
                                "text": "User prefers concise repo updates.",
                                "category": "communication",
                                "durability": "durable",
                                "signal_type": "explicit",
                                "evidence_ids": [first_user_id],
                            }
                        ]
                    }
                ),
            ]

            original_call_llm = extract.call_llm
            try:
                def fake_call_llm(prompt: str, dry_run: bool = False, **kwargs) -> str:
                    del prompt, dry_run, kwargs
                    call_count["value"] += 1
                    return responses.pop(0)

                extract.call_llm = fake_call_llm

                extract.process_layered_scope(scope_key, {"codex": [session_path]}, args, now=now)
                layered_paths = extract.build_layered_paths(output_dir, scope_key, now=now)
                first_raw = layered_paths.raw_daily_path.read_text()
                first_memory = layered_paths.curated_memory_path.read_text()
                first_facts = layered_paths.facts_path.read_text()

                extract.process_layered_scope(scope_key, {"codex": [session_path]}, args, now=now)
                self.assertEqual(call_count["value"], 1)
                self.assertEqual(layered_paths.raw_daily_path.read_text(), first_raw)
                self.assertEqual(layered_paths.curated_memory_path.read_text(), first_memory)
                self.assertEqual(layered_paths.facts_path.read_text(), first_facts)

                self.write_codex_session(
                    session_path,
                    [
                        ("user", "Keep repo updates concise."),
                        ("assistant", "Will do."),
                        ("user", "Verify changes with unittest."),
                    ],
                )
                second_messages = extract.load_codex_session_messages(session_path, "/tmp/project").messages
                second_user_id = second_messages[-1].message_id
                responses.append(
                    json.dumps(
                        {
                            "candidates": [
                                {
                                    "text": "Use unittest for verification in this repo.",
                                    "category": "workflow",
                                    "durability": "durable",
                                    "signal_type": "explicit",
                                    "evidence_ids": [second_user_id],
                                }
                            ]
                        }
                    )
                )

                extract.process_layered_scope(scope_key, {"codex": [session_path]}, args, now=now)
            finally:
                extract.call_llm = original_call_llm

            layered_paths = extract.build_layered_paths(output_dir, scope_key, now=now)
            final_raw = layered_paths.raw_daily_path.read_text()
            final_facts = layered_paths.facts_path.read_text()
            final_audit = layered_paths.audit_daily_path.read_text()
            final_memory = layered_paths.curated_memory_path.read_text()
            state = json.loads(layered_paths.state_path.read_text())

        self.assertEqual(call_count["value"], 2)
        self.assertEqual(len(final_raw.splitlines()), 2)
        self.assertIn("User prefers concise repo updates.", final_facts)
        self.assertIn("Use unittest for verification in this repo.", final_facts)
        self.assertIn("## Promoted Memory", final_audit)
        self.assertIn("User prefers concise repo updates.", final_memory)
        self.assertIn("Use unittest for verification in this repo.", final_memory)
        self.assertEqual(len(state["seen_event_ids"]), 2)
        self.assertEqual(len(state["seen_candidate_hashes"]), 2)

    def test_searchable_fact_can_exist_without_being_promoted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "out"
            session_path = root / "session.jsonl"
            now = datetime(2026, 3, 9, 12, 0, 0)
            scope_key = extract.ScopeKey(scope="project", project_path="/tmp/project")
            args = SimpleNamespace(
                output_dir=output_dir,
                dry_run=False,
                llm_backend="auto",
                llm_model=None,
            )

            self.write_codex_session(
                session_path,
                [
                    ("user", "Remember the repo uses bootstrap aliases."),
                    ("assistant", "Noted."),
                ],
            )
            messages = extract.load_codex_session_messages(session_path, "/tmp/project").messages
            user_id = next(message.message_id for message in messages if message.role == "user")

            original_call_llm = extract.call_llm
            try:
                extract.call_llm = lambda *args, **kwargs: json.dumps(
                    {
                        "candidates": [
                            {
                                "text": "The repo uses bootstrap aliases.",
                                "category": "other",
                                "durability": "durable",
                                "signal_type": "explicit",
                                "evidence_ids": [user_id],
                            }
                        ]
                    }
                )
                extract.process_layered_scope(scope_key, {"codex": [session_path]}, args, now=now)
            finally:
                extract.call_llm = original_call_llm

            layered_paths = extract.build_layered_paths(output_dir, scope_key, now=now)
            facts = [json.loads(line) for line in layered_paths.facts_path.read_text().splitlines()]
            memory_content = layered_paths.curated_memory_path.read_text()
            context, recall_items = promotion_pipeline.build_prepare_context(
                project_path="/tmp/project",
                output_dir=output_dir,
                query="bootstrap aliases",
                now=now,
            )

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["status"], "active")
        self.assertEqual(facts[0]["promotion_state"], "never")
        self.assertNotIn("The repo uses bootstrap aliases.", memory_content)
        self.assertEqual(len(recall_items), 1)
        self.assertIn("## Relevant Recall", context)
        self.assertIn("The repo uses bootstrap aliases.", context)

    def test_legacy_markdown_log_is_loaded_into_new_searchable_layer(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "out"
            session_path = root / "session.jsonl"
            now = datetime(2026, 3, 9, 12, 0, 0)
            scope_key = extract.ScopeKey(scope="project", project_path="/tmp/project")
            layered_paths = extract.build_layered_paths(output_dir, scope_key, now=now)
            layered_paths.memory_dir.mkdir(parents=True, exist_ok=True)
            legacy_record = {
                "candidate_hash": "legacy123",
                "text": "Use pytest for legacy verification.",
                "category": "workflow",
                "durability": "durable",
                "scope": "project",
                "signal_type": "explicit",
                "observed_at": "2026-03-08T10:00:00Z",
                "evidence": [
                    {
                        "platform": "codex",
                        "session_file": "/tmp/project/legacy.jsonl",
                        "jsonl_line": 4,
                        "timestamp": "2026-03-08T10:00:00Z",
                    }
                ],
            }
            legacy_log = layered_paths.memory_dir / "2026-03-08.md"
            legacy_log.write_text(
                f"{extract.LOG_RECORD_PREFIX} {json.dumps(legacy_record, sort_keys=True)} -->\n"
            )
            args = SimpleNamespace(
                output_dir=output_dir,
                dry_run=False,
                llm_backend="auto",
                llm_model=None,
            )

            self.write_codex_session(
                session_path,
                [
                    ("user", "Keep repo updates concise."),
                    ("assistant", "Will do."),
                ],
            )
            messages = extract.load_codex_session_messages(session_path, "/tmp/project").messages
            user_id = next(message.message_id for message in messages if message.role == "user")

            original_call_llm = extract.call_llm
            try:
                extract.call_llm = lambda *args, **kwargs: json.dumps(
                    {
                        "candidates": [
                            {
                                "text": "User prefers concise repo updates.",
                                "category": "communication",
                                "durability": "durable",
                                "signal_type": "explicit",
                                "evidence_ids": [user_id],
                            }
                        ]
                    }
                )
                extract.process_layered_scope(scope_key, {"codex": [session_path]}, args, now=now)
            finally:
                extract.call_llm = original_call_llm

            facts = layered_paths.facts_path.read_text()

        self.assertIn("Use pytest for legacy verification.", facts)
        self.assertIn("User prefers concise repo updates.", facts)


class LayeredClaudeIntegrationTests(unittest.TestCase):
    """Verify layered mode works with Claude-format session files."""

    def write_claude_session(self, session_path: Path, project_path: str, messages: list[tuple[str, str]]) -> None:
        entries = []
        for index, (role, text) in enumerate(messages):
            entries.append(json.dumps({
                "parentMessageId": f"msg-{index}",
                "type": role,
                "message": {"content": text},
                "timestamp": f"2026-03-12T10:00:{index:02d}Z",
            }))
        session_path.write_text("\n".join(entries))

    def test_layered_capture_discovers_claude_sessions(self):
        """End-to-end: layered capture picks up Claude-format session files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            project_path = "/Users/demo/my-project"

            # Set up Claude project directory and session file
            encoded = "-Users-demo-my-project"
            project_dir = home / ".claude" / "projects" / encoded
            project_dir.mkdir(parents=True)
            session_path = project_dir / "session-abc.jsonl"
            self.write_claude_session(
                session_path,
                project_path,
                [
                    ("user", "Always use pytest for tests."),
                    ("assistant", "Understood, I will use pytest."),
                ],
            )

            # Create history.jsonl to map session to project
            history_path = home / ".claude" / "history.jsonl"
            history_path.write_text(json.dumps({
                "project": project_path,
                "sessionId": "session-abc",
                "timestamp": 1,
                "display": "test session",
            }))

            paths = extract.PathConfig(home=home)
            discovered = extract.discover_claude_project_sessions(paths, project_filter=project_path)

            self.assertIn(project_path, discovered)
            self.assertEqual(len(discovered[project_path]), 1)

            # Load and verify messages are normalized correctly
            result = extract.load_claude_session_messages(
                discovered[project_path][0], project_path
            )
            platforms = [m.platform for m in result.messages]
            self.assertTrue(all(p == "claude" for p in platforms))
            roles = [m.role for m in result.messages]
            self.assertEqual(roles, ["user", "assistant"])

    def test_default_source_platforms_includes_both(self):
        """The CLI default for --source-platforms should include codex and claude."""
        from memory_promotion.cli import parse_args

        args = parse_args(["capture", "--dry-run"])
        self.assertIn("codex", args.source_platforms)
        self.assertIn("claude", args.source_platforms)

    def test_ingest_default_source_platforms_includes_both(self):
        """The ingest CLI default for --source-platforms should include codex and claude."""
        from memory_promotion.cli import parse_args

        args = parse_args(["ingest-and-filter", "--dry-run"])
        self.assertIn("codex", args.source_platforms)
        self.assertIn("claude", args.source_platforms)

    def test_capture_all_default_source_platforms_includes_both(self):
        """The batch capture CLI default for --source-platforms should include codex and claude."""
        from memory_promotion.cli import parse_args

        args = parse_args(["capture-all", "--dry-run"])
        self.assertIn("codex", args.source_platforms)
        self.assertIn("claude", args.source_platforms)


class BatchCaptureTests(unittest.TestCase):
    def test_discover_all_project_paths_merges_claude_and_codex_sources(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            claude_project = (home / "work" / "repo-one").resolve()
            codex_project = (home / "work" / "repo-two").resolve()
            claude_project.mkdir(parents=True)
            codex_project.mkdir(parents=True)

            claude_encoded = extract.encode_claude_project_path(str(claude_project))
            claude_project_dir = home / ".claude" / "projects" / claude_encoded
            claude_project_dir.mkdir(parents=True)
            (claude_project_dir / "session-claude.jsonl").write_text("{}\n")
            (home / ".claude" / "history.jsonl").write_text(
                json.dumps(
                    {
                        "project": str(claude_project),
                        "sessionId": "session-claude",
                        "timestamp": 1,
                    }
                )
            )

            codex_session_dir = home / ".codex" / "sessions" / "2026" / "03" / "15"
            codex_session_dir.mkdir(parents=True)
            (codex_session_dir / "session-codex.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"cwd": str(codex_project)}}),
                        json.dumps(
                            {
                                "type": "response_item",
                                "timestamp": "2026-03-15T10:00:00Z",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "Remember pytest."}],
                                },
                            }
                        ),
                    ]
                )
            )
            (codex_session_dir / "session-missing.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"cwd": str(home / "missing-project")}})
            )

            discovered = promotion_cli.discover_all_project_paths(
                ["claude", "codex"],
                paths=extract.PathConfig(home=home),
            )

            self.assertEqual(discovered, [str(claude_project), str(codex_project)])

    def test_should_skip_recent_checks_capture_state_mtime(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_path = str((Path(tmp_dir) / "repo").resolve())
            Path(project_path).mkdir()
            output_dir = Path(tmp_dir) / "output"
            paths = promotion_pipeline.build_layered_paths(output_dir, project_path)
            paths.state_path.parent.mkdir(parents=True, exist_ok=True)
            paths.state_path.write_text("{}")

            self.assertTrue(promotion_cli._should_skip_recent(project_path, output_dir, 6))

            old_timestamp = datetime.fromisoformat("2026-03-14T00:00:00+00:00").timestamp()
            os.utime(paths.state_path, (old_timestamp, old_timestamp))
            self.assertFalse(promotion_cli._should_skip_recent(project_path, output_dir, 1))

    def test_run_capture_all_skips_recent_no_sessions_and_continues_on_errors(self):
        args = SimpleNamespace(
            output_dir=Path("/tmp/output"),
            llm_backend="codex-cli",
            llm_model=None,
            source_platforms=["codex", "claude"],
            max_projects=0,
            skip_if_recent=6,
            dry_run=False,
        )
        discovered_projects = [
            "/tmp/recent-project",
            "/tmp/no-sessions-project",
            "/tmp/parse-error-project",
            "/tmp/exception-project",
            "/tmp/success-project",
        ]
        project_sessions = {
            "/tmp/parse-error-project": {"codex": [Path("/tmp/parse-error.jsonl")]},
            "/tmp/exception-project": {"codex": [Path("/tmp/exception.jsonl")]},
            "/tmp/success-project": {"codex": [Path("/tmp/success.jsonl")]},
        }

        def fake_skip_recent(project_path: str, output_dir: Path, skip_if_recent_hours: int) -> bool:
            self.assertEqual(output_dir, args.output_dir)
            self.assertEqual(skip_if_recent_hours, 6)
            return project_path == "/tmp/recent-project"

        def fake_capture(project_args, *, project_path=None, project_sessions=None):
            self.assertEqual(project_args.source_platforms, args.source_platforms)
            if project_path == "/tmp/parse-error-project":
                return promotion_cli.CaptureProjectResult(
                    project_path=project_path,
                    exit_code=1,
                    had_sessions=True,
                    new_message_count=3,
                )
            if project_path == "/tmp/exception-project":
                raise RuntimeError("boom")
            if project_path == "/tmp/success-project":
                return promotion_cli.CaptureProjectResult(
                    project_path=project_path,
                    exit_code=0,
                    had_sessions=True,
                    new_message_count=5,
                    raw_event_count=2,
                )
            raise AssertionError(f"Unexpected project_path: {project_path}")

        with (
            patch("memory_promotion.cli.discover_all_project_paths", return_value=discovered_projects),
            patch("memory_promotion.cli._should_skip_recent", side_effect=fake_skip_recent),
            patch(
                "memory_promotion.cli._discover_project_sessions",
                side_effect=lambda project_path, source_platforms: project_sessions.get(project_path, {}),
            ),
            patch("memory_promotion.cli._capture_project", side_effect=fake_capture),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = promotion_cli.run_capture_all(args)

        self.assertEqual(result, 1)
        output = stdout.getvalue()
        self.assertIn("5 projects discovered, 2 processed, 1 skipped (recent), 1 skipped (no sessions)", output)
        self.assertIn("Total new messages: 8", output)
        self.assertIn("Total raw events captured: 2", output)
        self.assertIn("Errors: 2", output)


class PendingQueueTests(unittest.TestCase):
    def test_collect_pending_windows_uses_context_window(self):
        messages = [
            SimpleNamespace(
                message_id="m1",
                platform="codex",
                session_file="/tmp/session.jsonl",
                jsonl_line=1,
                timestamp="2026-03-13T10:00:00Z",
                role="user",
                content="Please keep answers concise and update README if behavior changes.",
            ),
            SimpleNamespace(
                message_id="m2",
                platform="codex",
                session_file="/tmp/session.jsonl",
                jsonl_line=2,
                timestamp="2026-03-13T10:00:05Z",
                role="assistant",
                content="Understood, I will keep answers concise and update README as needed.",
            ),
            SimpleNamespace(
                message_id="m3",
                platform="codex",
                session_file="/tmp/session.jsonl",
                jsonl_line=3,
                timestamp="2026-03-13T10:00:10Z",
                role="user",
                content="Implement the parser refactor.",
            ),
        ]

        windows = promotion_pipeline.collect_pending_windows("/tmp/project", messages)

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].message_ids, ("m1", "m2", "m3"))
        self.assertIn("explicit_request", windows[0].reason_codes)

    def test_process_pending_flush_builds_events_and_rewrites_memory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            project_path = "/tmp/project"
            paths = promotion_pipeline.build_layered_paths(output_dir, project_path)
            pending_messages = [
                SimpleNamespace(
                    message_id="m1",
                    platform="codex",
                    session_file="/tmp/session.jsonl",
                    jsonl_line=1,
                    timestamp="2026-03-13T10:00:00Z",
                    role="user",
                    content="Please keep answers concise.",
                ),
                SimpleNamespace(
                    message_id="m2",
                    platform="codex",
                    session_file="/tmp/session.jsonl",
                    jsonl_line=2,
                    timestamp="2026-03-13T10:00:05Z",
                    role="assistant",
                    content="Understood, I will keep answers concise.",
                ),
            ]
            windows = promotion_pipeline.collect_pending_windows(project_path, pending_messages)
            promotion_pipeline.save_pending_windows(paths.pending_path, windows)

            llm_calls: list[dict[str, object]] = []

            def fake_llm(prompt, **kwargs):
                llm_calls.append({"prompt": prompt, **kwargs})
                if kwargs.get("output_schema"):
                    return json.dumps(
                        {
                            "candidates": [
                                {
                                    "text": "Keep answers concise.",
                                    "category": "communication",
                                    "durability": "durable",
                                    "signal_type": "explicit",
                                    "evidence_ids": ["m1", "m2"],
                                }
                            ]
                        }
                    )
                return "# Project Memory\n\n## Communication Style\n- Keep answers concise.\n"

            state = extract.ScopeState(version=extract.STATE_VERSION, project_path=project_path)
            result = promotion_pipeline.process_pending_flush(
                project_path=project_path,
                output_dir=output_dir,
                state=state,
                llm_call=fake_llm,
                llm_backend="codex-cli",
                llm_model=None,
                now=datetime.fromisoformat("2026-03-13T11:00:00+00:00"),
                cwd=tmp_dir,
            )

            self.assertTrue(result["ready_to_flush"])
            self.assertEqual(result["raw_event_count"], 1)
            self.assertEqual(len(llm_calls), 2)
            self.assertTrue(paths.curated_memory_path.exists())
            self.assertTrue(paths.deterministic_memory_path.exists())
            facts = promotion_pipeline.load_searchable_facts(paths.facts_path)
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].promotion_state, "promoted")
            queued = [window for window in promotion_pipeline.load_pending_windows(paths.pending_path) if window.status == "queued"]
            self.assertEqual(queued, [])


if __name__ == "__main__":
    unittest.main()
