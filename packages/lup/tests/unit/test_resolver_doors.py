"""What every console door says about a run that is not there.

Silence and "no such run" must be distinguishable, because one of them is
wrong. A session invoked from a sibling worktree — which has no `.lup` at all
— would read an empty listing as a real answer about the run it meant, and
report it, with nothing in the output to support it.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from lup.devtools.supervisor import doors
from lup.diagnostics import Refusal
from lup.policy.kernel.diagnostic import rendered


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose `.lup/resolve` holds nothing at all."""
    monkeypatch.setattr(doors, "resolve_state_root", lambda: tmp_path / ".lup/resolve")
    return tmp_path


@pytest.mark.parametrize(
    "door",
    [
        pytest.param(lambda: doors.list_questions(run_id="ghost"), id="questions"),
        pytest.param(lambda: doors.list_actors(run_id="ghost"), id="actors"),
        pytest.param(lambda: doors.park_run(run_id="ghost", reason="x"), id="park"),
        pytest.param(lambda: doors.drain_run(run_id="ghost", reason="x"), id="drain"),
        pytest.param(
            lambda: doors.answer_questions(pairs=["q=1"], run_id="ghost"), id="answer"
        ),
        pytest.param(
            lambda: doors.say_to_actor(text="hi", run_id="ghost", to=""), id="say"
        ),
        pytest.param(
            lambda: doors.redirect_actor(text="stop", run_id="ghost", to=""),
            id="redirect",
        ),
    ],
)
def test_a_door_refuses_a_run_that_does_not_exist(
    door: Callable[[], None], elsewhere: Path
) -> None:
    """No door answers "nothing recorded yet" and exits zero for an unknown id.

    Reading the journal before anything checks the run is there turns a
    missing directory into no actors, which reads as a real answer.
    """
    with pytest.raises(Refusal) as refused:
        door()

    assert refused.value.said["what"] == "ghost"
    assert "names no resolver run" in refused.value.said["why"]


def test_the_refusal_names_where_it_looked(elsewhere: Path) -> None:
    """Which is the whole diagnosis when the cause is the wrong directory."""
    with pytest.raises(Refusal) as refused:
        doors.list_actors(run_id="ghost")

    assert str(elsewhere / ".lup/resolve") in rendered(refused.value.said)
