"""Prompts module for cognition-project.

This module renders the brief for Devin sessions and defines the structured output schema.
"""

from typing import Dict, Any, Optional

from cognition.core import config
from cognition.verification import scanner


def render_brief(task: Dict[str, Any], attempt_no: int, prior_verdict: Optional[str] = None) -> str:
    """Render the brief for a Devin session.
    
    The issue says what is wrong; the brief must add what proves it fixed:
    
    - The exit criterion as an executable command — the exact pinned Bandit invocation
    - Blast radius as prohibitions — allowed paths, ≤ 60 changed lines, no suppression 
      comments as the fix, no dependency-file changes
    - The named test subset to run before opening the PR
    - The branch and PR contract — branch cognition-project/<fp>, body containing 
      Fixes #<n> and the footer <!-- cognition-project:fp=<fp> -->
    - On attempt 2, the verifier's verbatim rejection
    - The escalation contract — don't guess; emit needs_human with one specific question 
      and stop
    
    Args:
        task: Task dictionary from the database.
        attempt_no: Attempt number (1-indexed).
        prior_verdict: Verbatim rejection from prior attempt (if attempt_no > 1).
        
    Returns:
        The rendered brief as a string.
    """
    fp = task["fp"]
    issue_number = task["issue_number"]
    payload = task.get("payload_json", {})
    
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)
    
    rule_id = payload.get("test_id", "unknown")
    file_path = payload.get("file_path", "unknown")
    code = payload.get("code", "")
    issue_text = payload.get("issue_text", "")
    
    # Build the brief
    brief = f"""# Fix this security finding

## Issue #{issue_number}: {rule_id} in {file_path}

### What's wrong
{issue_text}

### Code to fix
```python
{code}
```

## Your task

Fix this finding while following these constraints exactly.

## Exit criterion
You must run this command to verify your fix:
```bash
{config.BANDIT_CMD}
```

The finding must be absent from the output, and no new findings should appear.

## Blast radius constraints

Your fix MUST respect these limits:

1. **Allowed paths only**: You may only modify files under these paths:
   {chr(10).join(f'   - {p}' for p in config.PATH_ALLOWLIST)}

2. **Size limit**: Your diff must be ≤ {config.MAX_DIFF_LOC} lines (added + removed)

3. **No suppression comments**: Do NOT add `# nosec`, `# noqa`, or `# type: ignore` as the fix. Fix the underlying problem instead.

4. **No dependency changes**: Do NOT modify any dependency files (requirements.txt, setup.py, pyproject.toml, package.json, etc.)

## Testing

Before opening a PR, run this test subset:
```bash
python -m pytest {config.TEST_MAPPING.get(rule_id, config.TEST_MAPPING.get("default", "tests/unit_tests"))} -v
```

All tests must pass.

## Branch and PR contract

1. Create a branch named exactly: `{scanner.branch_name(fp)}`
2. Open a PR with a body containing:
   - `Fixes #{issue_number}`
   - This footer: `<!-- cognition-project:fp={fp} -->`

The footer is REQUIRED for the verification system to match your PR to this task.

"""

    # Add prior verdict if this is a retry
    if attempt_no > 1 and prior_verdict:
        brief += f"""## Prior rejection (attempt {attempt_no - 1})

The verifier rejected your previous attempt with this reason:

```
{prior_verdict}
```

Use this feedback to improve your fix. Do NOT create a new session — this is a retry in the same context.

"""

    # Add escalation contract
    brief += """## Escalation contract

If you genuinely cannot determine the correct fix:
1. Emit `needs_human: true` in your structured output
2. Include one specific question in the `question` field
3. Stop and wait for human guidance

Do NOT guess or make assumptions that could introduce new vulnerabilities.

## Structured output

Provide your summary in the structured output with these fields:
- `fixed`: true if you believe the finding is fixed
- `scanner_clean`: true if you ran the scanner and it passed
- `needs_human`: true if you need human help
- `question`: your specific question (if needs_human is true)
- `summary`: a brief description of what you did
"""

    return brief


def structured_output_schema() -> Dict[str, Any]:
    """Return the structured output schema for Devin sessions.
    
    This is a self-contained Draft-7 JSON schema with the following fields:
    - fixed: bool
    - scanner_clean: bool
    - needs_human: bool
    - question: string
    - summary: string
    
    Returns:
        Draft-7 JSON schema as a dictionary.
    """
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["fixed", "scanner_clean", "needs_human", "question", "summary"],
        "properties": {
            "fixed": {
                "type": "boolean",
                "description": "Whether the agent believes the finding is fixed"
            },
            "scanner_clean": {
                "type": "boolean",
                "description": "Whether the agent ran the scanner and it passed"
            },
            "needs_human": {
                "type": "boolean",
                "description": "Whether the agent needs human intervention"
            },
            "question": {
                "type": "string",
                "description": "A specific question if needs_human is true"
            },
            "summary": {
                "type": "string",
                "description": "A brief description of what the agent did"
            }
        },
        "additionalProperties": False
    }
