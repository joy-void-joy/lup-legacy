"""What every console door says about a run that is not there.

Silence and "no such run" were indistinguishable, and one of them is wrong.
A session invoked from a sibling worktree — which has no `.lup` at all —
read an empty listing as a real answer about the run it meant, and reported
it. That happened to be true once; nothing in the output supported it.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from lup.devtools.supervisor import doors
from lup.devtools.utils import Refusal
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
    """`actors` answered "nothing recorded yet" and exited zero for any id.

    It read the journal before anything checked the run was there, so a
    missing directory yielded no actors and that read as a real answer.
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
