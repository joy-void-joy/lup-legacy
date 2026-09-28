"""A directory that says what it is, and the person who is always in it.

Written against the two failures that make a cohort unreachable without being
visibly broken: a stranger that cannot tell a cohort directory from any other
directory, and an address for the human that resolves in the process that
minted it and nowhere else.
"""

from pathlib import Path

from lup.coordination.cohort import ActorCohort
from lup.coordination.manifest import (
    CohortManifest,
    cohorts_under,
    manifest_path,
    publish_manifest,
    read_manifest,
)
from lup.coordination.peers import USER_ADDRESS, USER_TASK, user_peer
from lup.coordination.roster import Delivery


def test_a_cohort_says_what_it_is_where_it_sits(tmp_path: Path) -> None:
    """Opening a cohort is what makes its directory findable, not a convention."""
    cohort = ActorCohort(tmp_path / "run-1", description="checking the ledger")

    found = read_manifest(cohort.root)

    assert found is not None
    assert found.run_id == "run-1"
    assert found.description == "checking the ledger"


def test_re_opening_a_cohort_keeps_when_it_was_assembled(tmp_path: Path) -> None:
    """A run resumed after a park is the same population, dated from its start."""
    first = publish_manifest(tmp_path, "run-1", "the original purpose")

    again = publish_manifest(tmp_path, "run-1", "something a later caller passed")

    assert again == first, "first writer wins, so a re-attach restamps nothing"


def test_a_directory_that_says_nothing_is_not_refused(tmp_path: Path) -> None:
    """A cohort written before anything published a manifest still folds."""
    assert read_manifest(tmp_path) is None


def test_an_unreadable_manifest_answers_like_an_absent_one(tmp_path: Path) -> None:
    """The claim is about the directory; the streams beside it read either way."""
    manifest_path(tmp_path).write_text("{not json", encoding="utf-8")

    assert read_manifest(tmp_path) is None


def test_a_stranger_finds_every_cohort_beneath_a_tree(tmp_path: Path) -> None:
    """What a peer that created nothing reaches for, newest first."""
    older = publish_manifest(tmp_path / "runs" / "old", "old")
    newer = CohortManifest(
        run_id="new",
        opened_at=older.opened_at.replace(year=older.opened_at.year + 1),
    )
    root = tmp_path / "elsewhere" / "new"
    root.mkdir(parents=True)
    manifest_path(root).write_text(newer.model_dump_json(), encoding="utf-8")

    found = cohorts_under(tmp_path)

    assert [entry.manifest.run_id for entry in found] == ["new", "old"]
    assert [entry.root for entry in found] == [root, tmp_path / "runs" / "old"]


def test_the_person_is_a_member_the_same_verbs_reach(tmp_path: Path) -> None:
    """On the roster and resolved by the one fold, without being an agent."""
    cohort = ActorCohort(tmp_path)

    [member] = cohort.roster.live()

    assert member.task == USER_TASK
    assert cohort.reaching(USER_ADDRESS) == user_peer()
    assert member.delivery is Delivery.MAILBOX, "a person reads when they look"
    assert cohort.live() == [], "nobody started them, so no listing of spawns has them"


def test_a_process_that_opened_nothing_reaches_the_person(tmp_path: Path) -> None:
    """The failure this refuses: an address that resolves only where it was minted.

    Supplying the human's address to a console as a constructor argument
    leaves a caller attaching to a directory some other process wrote free to
    spell it differently — or not at all — and read one mailbox while the run
    writes another. The directory answers instead.
    """
    ActorCohort(tmp_path).tell_user("the environment is broken")

    attached = ActorCohort(tmp_path)

    assert [message.text for message in attached.heard().messages] == [
        "the environment is broken"
    ]


def test_a_reader_does_not_give_the_person_a_second_arrival(tmp_path: Path) -> None:
    """Every process opening a view announces them, and one of them is enough."""
    for _ in range(3):
        ActorCohort(tmp_path)

    assert [member.kind for member in ActorCohort(tmp_path).roster.live()] == ["user"]


def test_a_sender_is_told_what_carries_its_message(tmp_path: Path) -> None:
    """Accepted is not delivered, and the modes differ in whether anything wakes."""
    cohort = ActorCohort(tmp_path)
    spawned = cohort.actor("worker", "one")
    cohort.spawn(spawned, "do the thing")

    assert cohort.delivery(spawned) is Delivery.INBOX
    assert cohort.delivery(cohort.user) is Delivery.MAILBOX
    assert cohort.delivery(cohort.actor("worker", "never-recorded")) is Delivery.MAILBOX
