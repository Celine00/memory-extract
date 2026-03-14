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
