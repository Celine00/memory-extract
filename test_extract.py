import json
import tempfile
import unittest
from pathlib import Path

import extract


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

            messages = extract.load_codex_session_messages(session_path, "/tmp/project")

        self.assertEqual(
            [(message.platform, message.role, message.content) for message in messages],
            [
                ("codex", "user", "Implement the fix"),
                ("codex", "assistant", "Implemented it"),
            ],
        )


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
            Path("/tmp/codex/Users-demo-repo/MEMORY.md"),
        )
        self.assertEqual(
            extract.resolve_target_path("codex", global_scope, config),
            Path("/tmp/codex/MEMORY.md"),
        )
        self.assertIsNone(extract.resolve_target_path("claude", global_scope, config))


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


if __name__ == "__main__":
    unittest.main()
