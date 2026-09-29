"""How much Codex asks before it acts, and who answers it, as a declaration states it.

Codex's own words for it are its ``approval_policy`` and, under
``on-request``, its ``approvals_reviewer``. A launched session asks the person
at its terminal, so a declaration may say it asks; a session opened in this
process asks this program, which answers only through declared hooks, so that
is refused where one opens.
"""

import json
from pathlib import Path

import pytest

from lup.providers.codex import Codex
from lup.providers.codex.app_server import CodexAppServer
from lup.providers.codex.launch import codex_arguments
from lup.providers.codex.runtime import CodexConversationState
from lup.types import CustomModel


def test_a_launched_codex_may_ask_the_person_at_its_terminal() -> None:
    agent = Codex(
        model=CustomModel(id="gpt"),
        approval_policy="on-request",
        approvals_reviewer="auto_review",
    )

    words = codex_arguments(agent, [], [])

    assert ["--config", 'approval_policy="on-request"'] == words[
        words.index('approval_policy="on-request"') - 1 : words.index(
            'approval_policy="on-request"'
        )
        + 1
    ]
    assert 'approvals_reviewer="auto_review"' in words


async def test_a_codex_opened_here_refuses_approvals_nothing_would_answer(
    tmp_path: Path,
) -> None:
    """An asking policy with no hooks stalls the turn on its first command."""
    agent = Codex(
        model=CustomModel(id="gpt"), cwd=tmp_path, approval_policy="on-request"
    )

    with pytest.raises(ValueError, match="supply hooks to answer them"):
        async with agent.open():
            pass


def test_a_codex_opened_here_hands_its_reviewer_to_the_thread(tmp_path: Path) -> None:
    agent = Codex(
        model=CustomModel(id="gpt"), cwd=tmp_path, approvals_reviewer="auto_review"
    )

    parameters = CodexConversationState(
        agent, CodexAppServer(Path("codex")), None
    ).thread_parameters()

    assert json.loads(json.dumps(parameters["config"]))["approvals_reviewer"] == (
        "auto_review"
    )
