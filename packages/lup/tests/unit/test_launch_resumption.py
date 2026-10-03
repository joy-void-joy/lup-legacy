"""Reopening an earlier session, in each runtime's own spelling.

The policy a session enforces is compiled into a plugin tree its runtime
loads at startup, so widening that policy takes effect only on a new
process — and a new process started from nothing loses the conversation
that established what the widening was for. Reopening is what closes that
loop, which is why the request is one declaration and only the words differ.
"""

from pathlib import Path

import pytest

from lup.diagnostics import Refusal
from lup.harness.models import Resumption
from lup.launch.declaration import Latest, Pick, Reopen
from lup.sessions.events import SessionId
from tests.unit.harness_launch import checkout, composition, profiles, stub_host
from lup.providers.claude.launch import claude_resume_arguments
from lup.providers.codex.launch import codex_resume_arguments


def test_a_launch_that_reopens_nothing_adds_no_words() -> None:
    """The default is the ordinary launch, on both runtimes."""
    plain = Resumption()

    assert plain.wanted() is False
    assert claude_resume_arguments(plain) == []
    assert codex_resume_arguments(plain) == []


@pytest.mark.parametrize(
    ("resume", "claude", "codex"),
    [
        (Resumption(latest=True), ["--continue"], ["resume", "--last"]),
        (Resumption(pick=True), ["--resume"], ["resume"]),
        (Resumption(session="abc123"), ["--resume", "abc123"], ["resume", "abc123"]),
    ],
)
def test_each_runtime_spells_the_same_request_its_own_way(
    resume: Resumption, claude: list[str], codex: list[str]
) -> None:
    """One request, two vocabularies — a flag on one, a subcommand on the other.

    The shapes are genuinely different rather than differently named: a
    subcommand has to lead the vector where a flag does not, which is the
    whole reason the words are built per runtime and the request is not.
    """
    assert resume.wanted() is True
    assert claude_resume_arguments(resume) == claude
    assert codex_resume_arguments(resume) == codex


def test_naming_two_sessions_at_once_is_refused_rather_than_ranked() -> None:
    """A launch reopens one session, and picking for the operator would guess."""
    both = Resumption(latest=True, session="abc123")

    complaint = both.contradicted()

    assert complaint is not None
    assert "--continue" in complaint
    assert "--session" in complaint


def test_one_named_session_is_not_a_contradiction() -> None:
    for resume in (
        Resumption(),
        Resumption(latest=True),
        Resumption(pick=True),
        Resumption(session="abc123"),
    ):
        assert resume.contradicted() is None, resume


def test_a_contradicted_request_never_reaches_a_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused as the declaration is made, on both launchers.

    Before any step of the workflow around the session, because a launch
    that cannot happen should not first rewrite the tree it was going to open.
    """
    from lup.devtools.harness import launch

    root = checkout(tmp_path)
    caught = stub_host(monkeypatch, root)
    contradicted = launch.LaunchArguments(
        resume=Resumption(pick=True, session="abc123")
    )

    with pytest.raises(Refusal):
        launch.launch_claude(
            composition(root, "claude"), contradicted, profiles(), False
        )
    with pytest.raises(Refusal):
        launch.launch_codex(
            composition(root, "codex"), contradicted, None, False, False
        )
    assert caught.events == []


@pytest.mark.parametrize(
    ("resume", "reopening"),
    [
        (Resumption(), None),
        (Resumption(latest=True), Latest()),
        (Resumption(pick=True), Pick()),
        (Resumption(session="abc"), Reopen(session=SessionId(value="abc"))),
    ],
)
def test_each_reopening_flag_is_the_declarations_resume(
    resume: Resumption, reopening: Latest | Pick | Reopen | None
) -> None:
    """``--continue``, ``--resume`` and ``--session`` are one field of the declaration."""
    from lup.devtools.harness import launch

    assert launch.LaunchArguments(resume=resume).reopening() == reopening


def test_a_relaxed_launch_says_what_it_retired_and_what_it_did_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The launch is the only moment a relaxation is legible.

    The tree it compiles carries no rules, so nothing downstream can report
    their absence: a session opened under it meets no rule and cannot tell
    that from a repository with none. Two consequences ride along because
    both bite later and neither announces itself — the sweep still holds the
    repository to every rule, and the committed tree has just been rewritten.
    """
    from lup.harness.codescan.common import RuleSelection
    from lup.devtools.harness.launch import announce_relaxed_rules
    from lup.harness.models import HookSet, Plugin

    plugin = Plugin(
        id="plugin.lup",
        name="lup",
        description="a plugin",
        version="0.0.0",
        marketplace="lup",
        skills=[],
        agents=[],
        hooks=HookSet(
            id="hooks.lup",
            policy_ids=["edit"],
            rules=RuleSelection(retired=["dict-get", "own-model-dispatch"]),
        ),
    )

    announce_relaxed_rules(False, plugin)
    assert capsys.readouterr() == ("", "")

    announce_relaxed_rules(True, plugin)
    out, err = capsys.readouterr()
    assert "retired for this session: 2 rules" in out
    assert "still holds this repository" in err
    assert "`uv run lup-devtools dev check --antipatterns`" in err
    assert "before committing: `uv run lup-devtools harness generate all`" in err
    assert "`uv run lup-devtools dev seams --retire-all`" in err


def test_every_rule_retired_names_them_rather_than_standing_for_them() -> None:
    """The selection is subtractive, so "all of them" is spelled as all of them.

    A rule the library adds later is then one this selection has visibly not
    answered for, rather than one a flag silently swallowed.
    """
    from lup.harness.codescan.registry import all_rules, every_rule_retired

    retired = every_rule_retired()

    assert len(retired.retired) == len(all_rules())
    assert retired.retired
    assert not any(retired.keeps(rule.id) for rule in all_rules())
