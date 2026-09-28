"""Canonical declaration for the profile skill."""

import lup.harness.models as models

SKILL = models.Skill(
    id="skill.profile",
    name="profile",
    description="Read, select, and switch the account a session runs as",
    arguments=[
        models.Argument(
            name="selection",
            description="A verb and a profile name, or nothing to be shown the roster",
            required=False,
        ),
    ],
    machine_hint=models.ProfileHint(alone=["list"], naming=["use", "switch"]),
    tools=[
        "Bash(uv run lup-devtools:*)",
        "Read",
    ],
    prompt=models.PromptDocument(
        source=__name__,
        parts=[
            models.Passage(
                module=__name__,
                values={
                    "arguments": models.ArgumentsRef(),
                    "ask": models.AskUser(
                        question="which profile to act on, and whether to select it for the next launch or switch this session onto it now",
                    ),
                },
            ),
        ],
    ),
)
