You are analyzing NEW conversation excerpts from Codex and Claude Code for a single repository.
Your job is to extract accepted memory events for the local memory-promotion pipeline.

## Repository scope
- Scope: {scope_name}
- Project path: {project_path}

## Existing promoted memory
{existing_memory}

## Requirements
- Return JSON only. No markdown fences. No prose.
- Output shape:
  {{
    "candidates": [
      {{
        "text": "single memory line",
        "category": "language|documentation_style|communication|workflow|tooling|project_context|explicit_request|other",
        "durability": "durable|tentative",
        "signal_type": "explicit|implicit|project_constraint",
        "evidence_ids": ["message-id-1", "message-id-2"]
      }}
    ]
  }}
- Accept only stable user preferences, workflow habits, tooling choices, communication patterns, and stable project constraints.
- Reject one-off bug details, temporary TODOs, branch-specific state, tool noise, and weak speculation.
- Prefer Codex-grounded repo behavior when evidence is ambiguous.
- Use only evidence_ids that appear in the excerpts below.
- Keep each candidate short and directly useful in future coding sessions.
- If nothing should enter raw memory, return {{"candidates": []}}.

## Behavioral Signals
- Look beyond explicit "remember this" requests.
- Also extract stable preferences from user corrections, repeated rejections followed by a replacement, and consistent choices repeated across turns.
- When the user edits or rejects formatting, wording, or document structure, infer the stable rule behind that correction.
- For these inferred patterns, use `signal_type: "implicit"` and cite the evidence_ids that show the pattern.

## Category Guide
- `documentation_style`: formatting rules, markdown structure, diagram style, colors, headings, bullets
- `communication`: tone, response style, verbosity, language choice in replies
- `workflow`: verification habits, review flow, planning or implementation preferences
- `tooling`: tool choice, permissions, runtime or environment preferences
- `project_context`: stable repo constraints and structural facts relevant to contributors
- `language`: preferred natural language when it is distinct from overall communication style
- `explicit_request`: literal memory-intent requests from the user
- `other`: durable items that do not fit the categories above

## signal_type rules:
- `explicit`: ONLY when the user uses literal memory-intent language such as "remember", "always", "never", "from now on", "记住", or "以后都"
- `project_constraint`: stable structural facts about the repo or workflow that any contributor should know
- `implicit`: all other durable preferences inferred from repeated behavior or accepted instructions

## New conversation excerpts
{conversations}
