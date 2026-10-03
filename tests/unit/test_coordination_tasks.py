# tests claim: a task's standing is read from its neighbourhood so a blocker
# that finished stops blocking with nobody amending the edge; a delegation to
# nobody parks rather than refuses; and the person's rendering is ordered by
# what each row costs its reader rather than by when it was written.
"""Tasks, delegation, and the rendering a person reads them in."""

import json
import socket
from pathlib import Path
from threading import Thread

import pytest

from lup.coordination.refs import ActorRef
from lup.coordination.rendering import USER_HOLDER, render, user_tasks
from lup.coordination.tasks import Blocks, Task
from lup.coordination.wake import WakePath, Woken
from lup.providers.wake import wake
from lup.ledger.journal import LedgerStore
from lup.ledger.models import LedgerNode

NODE_CLASSES: list[type[LedgerNode]] = [Task]


def opened(root: Path, who: str = "alpha") -> LedgerStore:
    return LedgerStore(root, ActorRef(kind="session", id=who))


def test_a_task_needs_only_a_title_so_delegating_stays_one_line(
    tmp_path: Path,
) -> None:
    """The cheapest thing in this vocabulary, deliberately.

    A gate that asked for more would make delegating expensive enough to skip,
    which costs the record everything it was for.
    """
    store = opened(tmp_path)
    task = store.record(Task, "re-audit the bound")
    assert store.standing(task, NODE_CLASSES).label == "open"


def test_standing_reads_in_the_order_a_reader_acts(tmp_path: Path) -> None:
    """Done, then blocked, then waiting, then held, then open.

    Blocked outranks held because a task somebody holds and cannot start is
    not progress, and showing it as held sends a reader to the wrong person.
    """
    store = opened(tmp_path)
    open_task = store.record(Task, "unheld")
    held = store.record(Task, "held", holder="beta")
    waiting = store.record(Task, "waiting", holder="beta", needs="judgement")
    assert store.standing(open_task, NODE_CLASSES).label == "open"
    assert store.standing(held, NODE_CLASSES).label == "held"
    # Waiting outranks held: the holder cannot move it either.
    assert store.standing(waiting, NODE_CLASSES).label == "waiting"

    blocker = store.record(Task, "first", holder="alpha")
    store.relate(Blocks, blocker, held)
    assert store.standing(held, NODE_CLASSES).label == "blocked"


def test_a_finished_blocker_stops_blocking_with_nobody_amending_the_edge(
    tmp_path: Path,
) -> None:
    """The question is asked of the blocker rather than remembered on the edge.

    An edge carrying the blocker's state would be a stored status by another
    name — something a person has to go back and correct, which is exactly
    what went wrong in both worked repositories.
    """
    store = opened(tmp_path)
    blocker = store.record(Task, "land the gate", holder="alpha")
    blocked = store.record(Task, "build on it", holder="beta")
    store.relate(Blocks, blocker, blocked)
    assert store.standing(blocked, NODE_CLASSES).label == "blocked"

    store.amend(blocker.model_copy(update={"done": True}))
    assert store.standing(blocked, NODE_CLASSES).label == "held"


def test_needs_and_blocks_are_different_facts(tmp_path: Path) -> None:
    """One says what class of input is awaited; the other is a dependency."""
    store = opened(tmp_path)
    waiting = store.record(Task, "approve it", holder="user", needs="judgement")
    dependent = store.record(Task, "then do it", holder="alpha")
    store.relate(Blocks, waiting, dependent)

    assert store.standing(waiting, NODE_CLASSES).label == "waiting"
    assert store.standing(dependent, NODE_CLASSES).label == "blocked"


def test_the_person_s_list_is_ordered_by_what_each_row_costs(
    tmp_path: Path,
) -> None:
    """Mechanical rows first, judgements last, whatever order they were written.

    Nobody keeps "most urgent first" true in a derived document, and grouping
    by what a row needs gives a reader the ordering they actually wanted.
    """
    store = opened(tmp_path)
    store.record(Task, "weigh the trade-off", holder=USER_HOLDER, needs="judgement")
    store.record(Task, "paste this command", holder=USER_HOLDER, needs="command")
    store.record(Task, "make an account", holder=USER_HOLDER, needs="account")

    assert [task.needs for task in user_tasks(store)] == [
        "command",
        "account",
        "judgement",
    ]
    document = render(store)
    assert document.index("Commands to run") < document.index("Judgements to make")


def test_the_rendering_shows_only_what_is_left(tmp_path: Path) -> None:
    """A to-do list is what remains; what was done stays in the log."""
    store = opened(tmp_path)
    done = store.record(Task, "already handled", holder=USER_HOLDER, needs="command")
    store.record(Task, "still waiting", holder=USER_HOLDER, needs="command")
    store.amend(done.model_copy(update={"done": True}))

    assert [task.title for task in user_tasks(store)] == ["still waiting"]
    assert "already handled" not in render(store)


def test_an_empty_list_is_still_a_document(tmp_path: Path) -> None:
    """A file that vanished would leave a reader unsure which happened."""
    store = opened(tmp_path)
    assert "Nothing is waiting on you." in render(store)


def test_a_member_with_no_wake_path_is_told_so_rather_than_nudged() -> None:
    """Mail is always the record; a wake sits on top and may be absent."""
    answered = wake(WakePath(), "anything")
    assert answered == Woken(
        reached=False,
        reason=(
            "this member declared no wake path, so the mail waits until it next looks"
        ),
    )


@pytest.mark.usefixtures("unix_socket")
def test_a_claude_peer_is_woken_by_a_frame_written_to_its_wake_socket(
    tmp_path: Path,
) -> None:
    """Claude's path is a socket every session binds, not a tool a caller holds.

    A session takes a turn on a frame written there, so the library finishes
    the wake itself rather than handing it back with instructions.
    """
    address = tmp_path / "peer.sock"
    delivered: list[bytes] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(address))
        listener.listen(1)

        def take_one_frame() -> None:
            connection, _ = listener.accept()
            with connection:
                delivered.append(connection.recv(4096))

        waiting = Thread(target=take_one_frame)
        waiting.start()
        answered = wake(WakePath(runtime="claude", handle=str(address)), "look")
        waiting.join(timeout=5)

    assert answered == Woken(reached=True)
    assert json.loads(delivered[0]) == {
        "type": "user",
        "message": {"role": "user", "content": "look"},
    }


@pytest.mark.usefixtures("unix_socket")
def test_a_claude_peer_whose_wake_socket_has_gone_leaves_the_mail_waiting(
    tmp_path: Path,
) -> None:
    """A wake that fails costs latency and never a message, so it never raises."""
    answered = wake(
        WakePath(runtime="claude", handle=str(tmp_path / "vanished.sock")), "look"
    )
    assert not answered.reached
    assert "nothing is listening" in answered.reason
