"""Canonical declaration for the fb-analyze skill."""

import lup.harness.models as models

SKILL = models.Skill(
    id="skill.fb-analyze",
    name="fb-analyze",
    description="Aggregate tool health, capability gaps, and reasoning patterns across sessions",
    tools=[
        "Bash(uv run lup-devtools:*)",
        "Read",
        "Grep",
        "Glob",
        "Agent",
        "AskUserQuestion",
    ],
    prompt=models.PromptDocument(
        source=__name__,
        parts=[
            models.Passage(
                module=__name__,
                values={
                    "delegate": models.Delegate(
                        subagent_type="lup:version-explorer",
                        name="version_comparison",
                        prompt="Compare vX.Y.Z and vA.B.C",
                    )
                },
            ),
        ],
    ),
)
