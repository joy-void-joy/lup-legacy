"""A review whose requester is gone expires, and says why.

Its answer could release nothing: a native retry comes only from the session
that asked. So it expires at once where the roster saw that session leave, and
after a grace where the roster never knew it — never while it still runs.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from lup.devtools.review.app import (
    RequesterPresence,
    Sighting,
    expire_orphaned,
    relay,
)
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion
from tests.unit.reviews import bound


def parked(
    root: Path,
    question_id: str,
    requester: str,
    created: datetime | None = None,
    member: str = "",
) -> PersistentQuestion:
    operation = Operation(
        id=f"operation-{question_id}",
        session=f"native-{requester}",
        requester=requester,
        tool="Bash",
        payload={"command": "touch must-not-run"},
        cwd=root,
        worktree=root,
    )
    return relay(root).record(
        bound(
            PersistentQuestion(
                id=question_id,
                operation=operation,
                fingerprint="",
                reason="The operator reviews this command.",
                eligible=["operator"],
                resumption="native_retry",
                created=created or datetime.now(UTC),
                member=member,
            )
        )
    )


def test_only_reviews_nobody_can_retry_expire(tmp_path: Path) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=2)
    parked(tmp_path, "running", "live-session", long_ago)
    parked(tmp_path, "departed", "ended-session")
    parked(tmp_path, "unknown-recent", "never-joined")
    parked(tmp_path, "unknown-old", "never-joined-long-ago", long_ago)
    presence = RequesterPresence(
        sessions={
            "live-session": Sighting(name="builder", running=True),
            "ended-session": Sighting(name="reviewer", running=False),
        }
    )

    expired = expire_orphaned(tmp_path, presence)

    assert sorted(entry.id for entry in expired) == ["departed", "unknown-old"]
    states = {entry.id: entry.state for entry in relay(tmp_path).questions()}
    assert states == {
        "running": "pending",
        "departed": "expired",
        "unknown-recent": "pending",
        "unknown-old": "expired",
    }
    [reason] = {entry.outcome for entry in expired}
    assert "requester ended" in reason


def test_a_resumed_session_s_review_stays_while_its_member_runs(tmp_path: Path) -> None:
    """Resumed, a session keeps its roster member and hands commands the id it began with.

    That id's row has ended, and the session that asked still runs.
    """
    parked(tmp_path, "resumed", "first-runtime-id")
    parked(tmp_path, "by-member", "first-runtime-id", member="member-id")
    presence = RequesterPresence(
        sessions={
            "first-runtime-id": Sighting(name="lead", running=False),
            "member-id": Sighting(name="lead", running=True),
        }
    )

    expired = expire_orphaned(tmp_path, presence)

    assert [each.id for each in expired] == ["resumed"]
    kept = relay(tmp_path).find("by-member")
    assert kept is not None and kept.state == "pending"


def test_an_answered_review_is_never_expired_by_the_sweep(tmp_path: Path) -> None:
    entry = parked(tmp_path, "answered", "ended-session")
    relay(tmp_path).answer(entry.id, "operator", True)

    swept = relay(tmp_path).expire([entry.id], "requester ended")

    assert swept == []
    found = relay(tmp_path).find(entry.id)
    assert found is not None and found.state == "approved"


def test_a_session_is_named_as_the_roster_names_it(tmp_path: Path) -> None:
    by_member = parked(tmp_path, "one", "member-id")
    unnamed = parked(tmp_path, "two", "nobody-knows")
    presence = RequesterPresence(
        sessions={"native-member-id": Sighting(name="builder", running=True)}
    )

    assert presence.called(by_member) == "builder"
    assert presence.called(unnamed) == "nobody-knows"
