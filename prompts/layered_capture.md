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
        "category": "language|communication|workflow|tooling|project_context|explicit_request|other",
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

## New conversation excerpts
{conversations}
