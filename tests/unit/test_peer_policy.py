"""What a native call reaching another session is judged by, end to end.

Three layers, because the failure this stops is a disagreement between them:
the kernel decides from spellings alone, the dispatcher folds those spellings
off disk, and the roster is written by an entirely different process. A test
of the kernel alone would pass while the fold read a format nobody writes,
which is exactly how a redirect ends up reaching nobody — so the fold here is
read against a roster the real writer produced.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

import sh

from lup.channels.models import utc_now
from lup.coordination.bare import store
from lup.coordination.identity import mint_member_id
from lup.coordination.meeting import coordination_root
from lup.coordination.policy import peer_policy, wake_socket_refusal
from lup.coordination.repository import RepositoryPeers
from lup.coordination.roster import Delivery
from lup.policy.kernel.peers import (
    decide_peer_listing,
    decide_peer_send,
    peer_listing_context,
)
from lup.policy.peer_policy import erase_peer_policy
from lup.types import JsonObject
from tests.unit.repos import initialized_repo

DISPATCHER = Path(".claude/plugins/lup/hooks/scripts/policy.py")

DECLARED = erase_peer_policy(peer_policy())
"""The row this repository actually compiles, rather than a fixture beside it.

Written against the declaration so these cases pin the behaviour and not the
prose: rewording the redirect moves this with it, while a literal copy would
pin the wording and go quiet about the decision.
"""


def joined_repository(work: Path, hooks: Path) -> RepositoryPeers:
    """A throwaway repository with a roster, written by the real writer."""
    initialized_repo(work, hooks)
    return RepositoryPeers(work)


def folded_addresses(work: Path) -> list[str]:
    """Every spelling the compiled hook's own fold reaches a live member at."""
    return store.addresses(coordination_root(work))


def decide(payload: object) -> JsonObject:
    """Run the generated dispatcher over one hook payload."""
    return json.loads(
        str(sh.Command("python3")("-I", "-S", str(DISPATCHER), _in=json.dumps(payload)))
    )


def send_payload(target: str, cwd: Path) -> JsonObject:
    """One native send, in the shape the runtime was measured to deliver.

    Both aliases are carried because the live payload carries both, and the
    check reads every string rather than a named field — a fixture naming one
    of them would pass over a reading that only ever saw the other.
    """
    return {
        "tool_name": "SendMessage",
        "tool_input": {
            "to": target,
            "recipient": target,
            "message": "a follow-up",
            "content": "a follow-up",
            "type": "message",
        },
        "cwd": str(cwd),
    }


def test_a_send_to_a_peer_on_the_roster_is_denied_and_redirected(
    tmp_path: Path,
) -> None:
    """The whole point: the durable surface is named, not merely refused."""
    peers = joined_repository(tmp_path / "work", tmp_path / "hooks")
    peers.join(mint_member_id(), tmp_path / "work", cli_name="feat-touches")
    decision = decide(send_payload("feat-touches", tmp_path / "work"))
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "deny"
    reason = str(specific["permissionDecisionReason"])
    assert reason.startswith("refused: `feat-touches` — ")
    assert "coordination_send" in reason


def test_a_send_to_a_target_the_roster_never_heard_of_is_left_alone(
    tmp_path: Path,
) -> None:
    """Subagent continuation rides this call, and must go on riding it."""
    joined_repository(tmp_path / "work", tmp_path / "hooks")
    decision = decide(send_payload("a1e3f28c0aeb857ea", tmp_path / "work"))
    assert decision == {}


def test_a_listing_carries_the_repository_roster_without_deciding_anything(
    tmp_path: Path,
) -> None:
    """Nothing is permitted or refused; the second population rides alongside."""
    peers = joined_repository(tmp_path / "work", tmp_path / "hooks")
    peers.join(mint_member_id(), tmp_path / "work", cli_name="feat-touches")
    decision = decide(
        {"tool_name": "ListAgents", "tool_input": {}, "cwd": str(tmp_path / "work")}
    )
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert "permissionDecision" not in specific
    attached = str(specific["additionalContext"])
    assert "feat-touches" in attached
    assert "user" in attached


def test_a_listing_outside_any_roster_carries_nothing(tmp_path: Path) -> None:
    """An attachment stating an empty roster is paid for and teaches nothing."""
    initialized_repo(tmp_path / "work", tmp_path / "hooks")
    decision = decide(
        {"tool_name": "ListAgents", "tool_input": {}, "cwd": str(tmp_path / "work")}
    )
    assert decision == {}


def test_the_fold_reaches_a_member_by_id_by_label_and_by_name(
    tmp_path: Path,
) -> None:
    """Whichever spelling a sender last read has to be the one that is caught."""
    work = tmp_path / "work"
    peers = joined_repository(work, tmp_path / "hooks")
    member = mint_member_id()
    peers.join(member, work, cli_name="feat-touches")
    found = folded_addresses(work)
    assert member in found
    assert f"session:{member}" in found
    assert "feat-touches" in found
    assert "user" in found


def test_a_name_a_member_has_renamed_away_from_still_reaches_it(
    tmp_path: Path,
) -> None:
    """A sender typing the older name typed what was correct when they read it."""
    work = tmp_path / "work"
    peers = joined_repository(work, tmp_path / "hooks")
    member = mint_member_id()
    peers.join(member, work, cli_name="feat-touches")
    peers.rename(member, "feat-locks")
    found = folded_addresses(work)
    assert "feat-touches" in found
    assert "feat-locks" in found


def test_a_name_reclaimed_while_a_member_was_quiet_still_reaches_it(
    tmp_path: Path,
) -> None:
    """The live member is called this, whoever else holds the newest claim on it.

    The sequence is ordinary and the state it leaves is not: a session goes
    quiet long enough to read as gone, a newcomer in the same worktree takes
    the name it derives from that worktree, the newcomer finishes, and the
    first speaks again. It is live, every listing prints that name for it, and
    the newest claim on the string belongs to somebody who has left -- so a
    reading that asked only who a name reaches now found nobody live and let
    the send through with no record. Measured on this repository's own roster,
    where one of five live members was in exactly that state.
    """
    work = tmp_path / "work"
    peers = joined_repository(work, tmp_path / "hooks")
    quiet = mint_member_id()
    peers.join(quiet, work, cli_name="feat-touches")
    peers.leave(quiet, summary="stepped away")
    newcomer = mint_member_id()
    peers.join(newcomer, work, cli_name="feat-touches")
    peers.leave(newcomer, summary="landed")
    peers.join(quiet, work, cli_name="feat-touches")

    found = folded_addresses(work)

    assert store.called(coordination_root(work))[quiet] == "feat-touches"
    assert "feat-touches" in found


def test_a_member_that_has_left_is_no_longer_reached(tmp_path: Path) -> None:
    """Redirecting to a departed peer trades one call reaching nobody for another."""
    work = tmp_path / "work"
    peers = joined_repository(work, tmp_path / "hooks")
    member = mint_member_id()
    peers.join(member, work, cli_name="feat-touches")
    peers.leave(member, summary="landed")
    found = folded_addresses(work)
    assert member not in found
    assert "feat-touches" not in found


def test_the_listing_says_what_carries_a_message_to_each_member(
    tmp_path: Path,
) -> None:
    """A roster naming members without saying what reaches them is a guess."""
    work = tmp_path / "work"
    peers = joined_repository(work, tmp_path / "hooks")
    member = mint_member_id()
    peers.join(member, work, cli_name="feat-touches", delivery=Delivery.HOOK)
    peers.describe(member, "rewriting the touch ledger")
    listing = store.listing_lines(coordination_root(work))
    row = next(line for line in listing if line.startswith("feat-touches"))
    assert "rewriting the touch ledger" in row
    assert Delivery.HOOK in row
    assert work.name in row


def test_a_member_whose_pulse_stopped_is_no_longer_reached_until_it_beats(
    tmp_path: Path,
) -> None:
    """A killed session wrote no departure; its silence is read the same way."""
    work = tmp_path / "work"
    peers = joined_repository(work, tmp_path / "hooks")
    member = mint_member_id()
    peers.join(member, work, cli_name="feat-touches")
    long_ago = (
        utc_now() - timedelta(seconds=store.STALE_AFTER_SECONDS + 1)
    ).timestamp()
    os.utime(
        store.member_path(peers.root, store.session_actor(member)), (long_ago, long_ago)
    )

    assert "feat-touches" not in folded_addresses(work)
    assert "user" in folded_addresses(work)

    store.beat(peers.root, store.session_actor(member))

    assert "feat-touches" in folded_addresses(work)


def test_an_escalated_send_becomes_the_question_the_sender_asked_for() -> None:
    """A deliberate use is stopped once and reviewed, never walled off."""
    assert DECLARED is not None
    decision = decide_peer_send(
        ["# lup: escalate: the peer is mid-turn and its hook is off", "feat-touches"],
        ["feat-touches"],
        DECLARED,
    )
    assert decision.effect == "ask"
    assert "the peer is mid-turn" in decision.reason


def test_a_project_declaring_no_roster_has_every_peer_call_left_alone() -> None:
    """Declining coordination must not price a listing at an approval."""
    assert decide_peer_send(["anybody"], [], None).effect == "defer"
    assert decide_peer_listing(None).effect == "defer"
    assert peer_listing_context(["feat-touches — mailbox"], None) == ""


def test_an_empty_roster_attaches_nothing() -> None:
    """An attachment nobody can act on is still paid for on every call."""
    assert DECLARED is not None
    assert peer_listing_context([], DECLARED) == ""


def test_the_wake_socket_directory_is_withheld_where_the_image_binds_it() -> None:
    """The refusal follows the declaration, and an empty one refuses nothing."""
    refused = wake_socket_refusal("/run/sessions")
    assert [paths.paths for paths in refused] == [["/run/sessions/**"]]
    assert any(
        "coordination_send" in through["says"] for through in refused[0].recovery
    )
    assert wake_socket_refusal("") == []
