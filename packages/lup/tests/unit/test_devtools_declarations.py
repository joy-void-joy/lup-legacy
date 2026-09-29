"""What the library's roster asks of a project, and only when it has to.

The agent prompt is read by one sub-app, `feedback`, which a project gets by
taking the feedback-loop module and which is off by default. Required on the
declaration, it made every adopter without an agent of its own invent an
`AgentPrompt` for a command it would never serve. These pin that the prompt
is asked for exactly where it is read: absent is fine until `feedback` is
served, and serving it without one is refused in words naming both answers.
"""

import json
from unittest import mock

import pytest
from typer.testing import CliRunner

from lup.devtools.dev.declarations import DevDeclarations
from lup.devtools.feedback.models import AgentPrompt
from lup.devtools.harness.composition import NativeTargets
from lup.devtools.roster import LIBRARY_ROSTER, DevtoolsDeclarations

FEEDBACK = next(entry for entry in LIBRARY_ROSTER if entry.spec.name == "feedback")
"""The one entry reading the prompt, built for real rather than stubbed."""


def unread() -> DevDeclarations:
    """A dev declaration nothing under test reaches for."""
    raise AssertionError("the feedback sub-app reads no dev declaration")


def declarations(prompt: AgentPrompt | None = None) -> DevtoolsDeclarations:
    """The least a project declares, with or without an agent prompt."""
    return DevtoolsDeclarations(
        dev=unread,
        targets=NativeTargets(builders={}),
        repository_writers=[],
        prompt=None if prompt is None else lambda: prompt,
    )


def test_a_project_that_does_not_serve_feedback_declares_no_prompt() -> None:
    """Declining the sub-app leaves nothing to read a prompt, and nothing asks."""
    with mock.patch("lup.devtools.roster.LIBRARY_ROSTER", [FEEDBACK]):
        served = declarations().roster(["feedback"])

    assert served == []


def test_serving_feedback_without_a_prompt_is_refused_by_what_to_do() -> None:
    """Refused where the roster is built, not when prompt-health first runs."""
    with (
        mock.patch("lup.devtools.roster.LIBRARY_ROSTER", [FEEDBACK]),
        pytest.raises(ValueError, match="pass `prompt=`") as refused,
    ):
        declarations().roster()

    assert "feedback-loop" in str(refused.value)


def test_a_declared_prompt_is_what_the_health_report_weighs() -> None:
    prompt = AgentPrompt(sections=["You are an agent."], rendered="You are an agent.")

    app = FEEDBACK.build(declarations(prompt))
    result = CliRunner().invoke(app, ["prompt-health", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["rendered_characters"] == len(prompt.rendered)
