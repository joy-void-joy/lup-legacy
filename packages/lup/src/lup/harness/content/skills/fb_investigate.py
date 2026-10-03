"""Canonical declaration for the fb-investigate skill."""

import lup.harness.models as models
from lup.harness.content.application import ApplicationLayout


def skill(layout: ApplicationLayout) -> models.Skill:
    """Classify session errors against this project's own tool sources."""
    return models.Skill(
        id="skill.fb-investigate",
        name="fb-investigate",
        description="Deep trace reading and error classification for selected sessions",
        tools=[
            "Bash(uv run lup-devtools:*)",
            "Read",
            "Grep",
            "Glob",
            "Agent",
            "AskUserQuestion",
        ],
        argument_hint="<session_id1> [session_id2 ...]",
        prompt=models.PromptDocument(
            source=__name__,
            parts=[
                models.Passage(
                    module=__name__,
                    values={
                        "delegate": models.Delegate(
                            subagent_type="lup:trace-explorer",
                            name="trace_patterns",
                            prompt="Analyze traces for sessions <ids>; report tool failures, capability gaps, reasoning quality",
                        ),
                        "delegate_2": models.Delegate(
                            subagent_type="lup:trace-explorer",
                            name="session_<session_id>",
                            prompt="Investigate session <session_id> following the per-session steps below. Report: tool call inventory, errors with quoted output, workflow assessment, outcome classification, counterfactuals.",
                        ),
                        "ask": models.AskUser(
                            question="whether to proceed to analysis, dig deeper on particular sessions, or skip ahead to implementation"
                        ),
                        "agent_tools_directory": models.code(
                            layout.directory("agent", "tools")
                        ),
                    },
                ),
            ],
        ),
    )
