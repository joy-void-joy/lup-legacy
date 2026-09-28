"""Canonical declaration for the release skill."""

import lup.harness.models as models

SKILL = models.Skill(
    id="skill.release",
    name="release",
    description=(
        "Cut a release or a candidate of one: settle the level, close the "
        "changelog, tag it — or promote the candidate that held"
    ),
    arguments=[
        models.Argument(
            name="arguments",
            description="Optional arguments supplied with the skill invocation",
            required=False,
        ),
    ],
    tools=[
        "Bash(uv run lup-devtools:*, git:*, gh:*)",
        "Read",
        "Edit",
        "AskUserQuestion",
    ],
    argument_hint="[patch|minor|major] [--pre]",
    prompt=models.PromptDocument(
        source=__name__,
        parts=[
            models.Passage(
                module=__name__,
                values={
                    "arguments": models.ArgumentsRef(),
                    "ask": models.AskUser(question="which level this release is"),
                    "ask_moved": models.AskUser(
                        question=(
                            "whether to cut another candidate or release what "
                            "the integration branch holds now"
                        )
                    ),
                },
            ),
        ],
    ),
)
