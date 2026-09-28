"""Mail reaching a session through the plugin, where no live inbox exists.

The in-process hook closes over an `ActorInbox`, so it reaches only sessions
lup opened itself. This reader is what a person's own session has instead: a
directory of files and a script that reads them through the store package
shipped beside it.

The **emitted** copy is what runs here, loaded from the plugin's runtime
directory exactly as the guard execs it, because the reader resolves the store
package as a sibling and there is nowhere else that is true. That is the same
arrangement the compiled dispatcher is exercised under, and it is why this
sits in the template's suite rather than the library's: it reads a generated
tree, which only a repository that has generated one has.

What there is to pin is the split. The reader owns Claude Code's envelope and
nothing else — which member's mail, and what it means to have arrived, are the
shipped package's, so a rename there moves this without anything having to be
restated. What is asserted is the behaviour that envelope carries: mail
arrives once, a redirect stops the call it arrived before, and a store that is
not there stops nothing.
"""

import json
from pathlib import Path
from types import ModuleType

from lup.channels.models import Door
from lup.coordination.bare.mail import new_message, post
from lup.coordination.identity import member_ref
from lup.coordination.mail import ActorMail
from lup.types import JsonObject
from tests.unit.bundled import bundled

RUNTIME = Path(".claude/plugins/lup/hooks/runtime/coordination_delivery.py")
"""Where the plugin carries the reader, and the only place its imports resolve."""


def bundled_delivery() -> ModuleType:
    """The emitted reader, imported the way the guard runs it.

    From the generated tree rather than from `lup.providers.claude.assets`,
    because the module names its own directory as a search path and imports
    the store package as a sibling: in the workspace there is no such sibling,
    and in the plugin there is.
    """
    return bundled("bundled_coordination_delivery", RUNTIME)


def handed(root: Path, member_id: str) -> JsonObject | None:
    """What the emitted reader hands one member, taking it out of their inbox."""
    return bundled_delivery().deliver(root, member_id)


def queued(root: Path, member_id: str, text: str, redirect: bool = False) -> None:
    """Put one message in a member's inbox, the way a sender leaves it.

    Under the conversation key the typed sender writes to, spelled by the ref
    rather than assembled here — a directory only the test could name would
    pin nothing about where mail actually lands.
    """
    post(
        root,
        member_ref(member_id).conversation(),
        new_message(
            sender="somebody",
            to=member_id,
            body=text,
            door=str(Door.AGENT),
            redirect=redirect,
        ),
    )


def spoken(answer: JsonObject | None) -> JsonObject:
    """The envelope one delivery carries, failing where it delivered nothing."""
    assert answer is not None
    envelope = answer["hookSpecificOutput"]
    assert isinstance(envelope, dict)
    return envelope


def carried(answer: JsonObject | None) -> str:
    """What one delivery puts in front of the session, however it was framed."""
    envelope = spoken(answer)
    return str(
        envelope.get("additionalContext")
        or envelope.get("permissionDecisionReason")
        or ""
    )


def test_the_reader_and_the_typed_writer_meet_in_one_inbox(tmp_path: Path) -> None:
    """The import a verbatim copy cannot have, had: both reach one directory.

    The typed sender and the bare reader agree because they call the same
    shipped module, so there is no pair of spellings left to keep in step by
    hand — which is what a test used to stand in for.
    """
    ActorMail(tmp_path).send(member_ref("abc123"), "from the typed half")

    assert "from the typed half" in carried(handed(tmp_path, "abc123"))


def test_mail_is_delivered_once_and_leaves_the_inbox(tmp_path: Path) -> None:
    """The ordinary path: what is addressed here arrives, and arrives once.

    Consumed by deleting the files handed over, so there is no position to
    commit and a second call finds an empty directory rather than a cursor it
    has to trust.
    """
    queued(tmp_path, "abc123", "first")
    queued(tmp_path, "abc123", "second")

    delivered = carried(handed(tmp_path, "abc123"))

    assert "first" in delivered
    assert "second" in delivered
    assert handed(tmp_path, "abc123") is None


def test_another_member_s_mail_is_left_where_it_is(tmp_path: Path) -> None:
    """One inbox per member, so there is nothing to address-match and get wrong."""
    queued(tmp_path, "abc124", "theirs")

    assert handed(tmp_path, "abc123") is None
    assert "theirs" in carried(handed(tmp_path, "abc124"))


def test_a_redirect_stops_the_call_it_arrived_before(tmp_path: Path) -> None:
    """Telling and stopping are different acts and get different verdicts."""
    queued(tmp_path, "abc123", "wrong branch", redirect=True)

    answer = handed(tmp_path, "abc123")

    assert answer is not None
    assert spoken(answer).get("permissionDecision") == "deny"
    assert "wrong branch" in carried(answer)


def test_a_message_still_being_written_is_left_for_the_next_call(
    tmp_path: Path,
) -> None:
    """A sender writes to a neighbour and renames, so a partial file has no name.

    What a reader can still meet is a file some other tool left in the
    directory. It reads as absent rather than as a failure, because this runs
    before every tool call and one unreadable file must not cost the session
    the messages around it.
    """
    queued(tmp_path, "abc123", "whole")
    inbox = tmp_path / "inbox" / member_ref("abc123").conversation()
    (inbox / "half.json").write_text('{"id": "half", "text": "half', encoding="utf-8")

    delivered = carried(handed(tmp_path, "abc123"))

    assert "whole" in delivered
    assert "half" not in delivered


def test_a_store_that_is_not_there_delivers_nothing_and_raises_nothing(
    tmp_path: Path,
) -> None:
    """The failure that must not stop a call, since this runs before all of them."""
    assert handed(tmp_path / "absent", "abc123") is None


def test_a_standing_notice_is_not_mail_and_is_not_consumed(tmp_path: Path) -> None:
    """Delivery hands over what it deletes, so it must not reach what it cannot.

    A notice is read at the head of a turn, restated for as long as it holds.
    Handing one over here would consume a fact that has not stopped being
    true — and would do it before every tool call rather than once a turn.
    """
    ActorMail(tmp_path).notify("the base moved under all of you")

    assert handed(tmp_path, "abc123") is None
    assert [notice.text for notice in ActorMail(tmp_path).standing()] == [
        "the base moved under all of you"
    ]


def test_the_envelope_names_the_event_the_runtime_fires_it_under(
    tmp_path: Path,
) -> None:
    """Read from the nested output, which is the one way to be wrong silently."""
    queued(tmp_path, "abc123", "anything")

    answer = handed(tmp_path, "abc123")

    assert answer is not None
    assert spoken(answer).get(bundled_delivery().EVENT_FIELD) == (
        bundled_delivery().EVENT_NAME
    )
    assert json.loads(json.dumps(answer)) == answer
