You are rewriting a repo-local MEMORY.md from already curated promoted facts.
Do not add new facts. Do not speculate. Do not remove a fact unless it is clearly duplicated by another line.

## Project
- Path: {project_path}

## Deterministic memory draft
{deterministic_memory}

## Promoted facts
{promoted_facts}

## Requirements
- Return markdown only.
- Preserve the facts and intent from the deterministic draft.
- Improve wording, dedupe near-identical lines, and keep the result concise.
- Merge bullets that express the same preference in different words.
- Combine closely related documentation-style rules when the merged bullet stays clear.
- Preserve every distinct preference; do not drop facts just to shorten the file.
- Keep the sectioned MEMORY.md shape. Use short bullet lines.
- Do not invent categories that are not already implied by the draft.
- Keep the output under 100 lines when possible and under 150 lines at most.
