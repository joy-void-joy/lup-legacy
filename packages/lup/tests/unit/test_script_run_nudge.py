"""When a scratch script has stopped being the one-off the ladder allowed.

The rung exists because computing something once does not earn a command. The
nudge exists because nothing in that argument survives repetition, and the
agent repeating it has no memory of having done so before.

What is pinned here is the cadence, because both ways of getting it wrong are
silent. Nudging every run makes it noise on a command that worked, read once
and skipped forever. Nudging once makes it invisible to every session that was
mid-thought when it arrived.
"""

from pathlib import Path

from lup.policy.assets.host import script_run_nudge
from lup.policy.kernel.lex import python_script_targets
from lup.policy.kernel.words import INTERPRETERS


def runs(root: Path, script: str, times: int, after: int, every: int) -> list[str]:
    """Run one script that many times, collecting the nudges it earned."""
    return [
        nudge
        for _ in range(times)
        if (nudge := script_run_nudge([script], root, after, every))
    ]


def test_a_one_off_is_left_alone(tmp_path: Path) -> None:
    """The case the rung exists for must cost nothing to use."""
    assert runs(tmp_path, "tmp/probe.py", 4, after=5, every=10) == []


def test_the_nudge_arrives_when_the_script_stops_being_one(tmp_path: Path) -> None:
    """Said at the threshold, and it names the count so the claim is checkable."""
    earned = runs(tmp_path, "tmp/probe.py", 5, after=5, every=10)

    assert len(earned) == 1
    assert "tmp/probe.py (5x)" in earned[0]
    assert "lup-devtools" in earned[0]


def test_it_recurs_rather_than_firing_once(tmp_path: Path) -> None:
    """A session that was mid-thought at the first one has to hear it again."""
    earned = runs(tmp_path, "tmp/probe.py", 25, after=5, every=10)

    assert len(earned) == 3
    assert "(5x)" in earned[0]
    assert "(15x)" in earned[1]
    assert "(25x)" in earned[2]


def test_two_scripts_are_counted_apart(tmp_path: Path) -> None:
    """One script's repetition says nothing about another's."""
    for _ in range(5):
        script_run_nudge(["tmp/a.py"], tmp_path, after=5, every=10)
    quiet = script_run_nudge(["tmp/b.py"], tmp_path, after=5, every=10)

    assert quiet == ""


def test_an_unwritable_ledger_costs_the_command_nothing(tmp_path: Path) -> None:
    """A counter is not worth failing a command over.

    A read-only checkout is an ordinary place to be running, and a nudge that
    raised there would turn advice into an outage.
    """
    assert script_run_nudge(["tmp/probe.py"], tmp_path / "nowhere" / "deep") != "!"


def test_a_corrupt_ledger_starts_over_rather_than_raising(tmp_path: Path) -> None:
    """Whatever else is in that file, it is not worth a traceback."""
    (tmp_path / ".lup").mkdir()
    (tmp_path / ".lup" / "script-runs.json").write_text("not json at all")

    assert script_run_nudge(["tmp/probe.py"], tmp_path, after=1, every=10) != ""


def test_the_script_is_read_off_the_command_the_way_it_was_typed() -> None:
    """The counter is only as good as what names the file it counts."""
    named = python_script_targets(
        "uv run pytest | uv run python tmp/one.py", INTERPRETERS
    )

    assert named == ["tmp/one.py"]


def test_inline_code_names_no_script_to_count() -> None:
    """`-c` is refused anyway, and there would be no file to promote."""
    assert python_script_targets("uv run python -c 'x'", INTERPRETERS) == []
