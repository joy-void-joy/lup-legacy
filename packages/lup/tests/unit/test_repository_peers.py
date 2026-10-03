"""The population nobody assembled: every session working in one repository.

Written against the failures that make a repository roster useless without
being visibly broken — a name that stops resolving the moment its session
renames, two sessions in one worktree printed under one address so a message
to the other lands on oneself, a description that still says what somebody
was doing an hour ago, a listing that is a month of history, mail queued for
a session that left, and a store that comes into being because a process
mentioned it.
"""

from datetime import timedelta
from pathlib import Path

import pytest

import json

from lup.channels.models import utc_now
from lup.coordination.bare import store
from lup.coordination.bare.changes import MOVED_LINE, changes
from lup.coordination.identity import (
    MEMBER_ENV,
    NAME_ENV,
    NameTakenError,
    derived_cli_name,
    mint_member_id,
    session_member_id,
)
from lup.coordination.peer_tools import create_peer_tools
from lup.coordination.peers import USER_ADDRESS
from lup.coordination.repository import (
    PeerDepartedError,
    RepositoryPeers,
    Retention,
    launched_member,
)
from lup.coordination.roster import Delivery
from lup.coordination.meeting import coordination_root
from lup.tools.mcp import LupMcpTool, ToolResponse, response_text


def written(transcript: Path, roots: int) -> None:
    """A transcript with this many conversations in it, as a rewind leaves one.

    A rewind appends a root to the same file — a turn parented by nothing —
    which is the one sign the runtime leaves that the conversation this prompt
    belongs to is not the one before it.
    """
    transcript.write_text(
        "".join(
            json.dumps({"type": "user", "uuid": f"u{index}", "parentUuid": None}) + "\n"
            for index in range(roots)
        ),
        encoding="utf-8",
    )


def joined(
    root: Path, name: str, worktree: str = "tree"
) -> tuple[RepositoryPeers, str]:
    """One repository's peers, with a session already on the roster."""
    peers = RepositoryPeers(root)
    member = mint_member_id()
    peers.join(member, root / worktree, cli_name=name)
    return peers, member


def test_the_store_is_shared_by_every_worktree_of_one_repository(
    tmp_path: Path,
) -> None:
    """Derived from the shared git directory, so no worktree owns the roster."""
    (tmp_path / ".git").mkdir()

    assert coordination_root(tmp_path) == tmp_path / ".git" / "lup" / "coordination"


def test_a_store_rooted_outside_any_repository_stays_under_that_root(
    tmp_path: Path,
) -> None:
    """The path answers for itself, so nothing reaches the checkout it ran in.

    A root in no repository has no shared git directory to derive from, and
    the recoverable answer is the root itself rather than whatever `git` would
    say about the process's working directory. Anything else and a caller
    handed a throwaway path — a test, a probe, a tool pointed somewhere — would
    silently join the real roster and be read by every session on it.
    """
    outside = tmp_path / "nowhere"
    outside.mkdir()

    root = coordination_root(outside)

    assert root.is_relative_to(outside)
    RepositoryPeers(outside).join(mint_member_id(), outside, cli_name="alone")
    assert [view.address for view in RepositoryPeers(outside).listing()] == ["alone"]


def test_mentioning_the_roster_does_not_create_it(tmp_path: Path) -> None:
    """Opening a cohort writes, so a session assembling its tools must not."""
    RepositoryPeers(tmp_path)

    assert not coordination_root(tmp_path).exists()


def test_a_session_is_reached_by_its_name_and_by_its_id(tmp_path: Path) -> None:
    """One resolution, so a spelling one surface accepts the next does not reject."""
    peers, member = joined(tmp_path, "reviewer")

    by_name = peers.address("reviewer")
    by_id = peers.address(member)

    assert by_name is not None
    assert by_name == by_id


def test_a_name_written_down_before_a_rename_still_reaches_its_session(
    tmp_path: Path,
) -> None:
    """There is no error a sender could be shown: the name was right when read."""
    peers, member = joined(tmp_path, "reviewer")

    peers.rename(member, "merger")

    assert peers.address("reviewer") == peers.address("merger")
    assert peers.called(member) == "merger"


def test_a_name_a_live_session_answers_to_cannot_be_taken(tmp_path: Path) -> None:
    """One address for two peers is the collision the roster exists to rule out."""
    peers, first = joined(tmp_path, "reviewer")
    second = mint_member_id()
    peers.join(second, tmp_path / "other", cli_name="worker")

    with pytest.raises(NameTakenError) as refused:
        peers.rename(second, "reviewer")

    assert refused.value.holder_id == first
    assert peers.called(second) == "worker"
    with pytest.raises(NameTakenError):
        peers.join(mint_member_id(), tmp_path / "third", cli_name="reviewer")


def test_a_name_a_session_renamed_away_from_reaches_whoever_took_it(
    tmp_path: Path,
) -> None:
    """A name is a handle, and handles get reused after they are released."""
    peers, first = joined(tmp_path, "reviewer")
    second = mint_member_id()
    peers.join(second, tmp_path / "other", cli_name="worker")
    peers.rename(first, "merger")

    peers.rename(second, "reviewer")

    found = peers.address("reviewer")
    assert found is not None and found.id == second
    assert found.id != first


def test_a_name_reaches_the_live_session_ahead_of_one_that_left(
    tmp_path: Path,
) -> None:
    """The session somebody typing a name means is the one still here."""
    peers, first = joined(tmp_path, "reviewer")
    peers.rename(first, "merger")
    second = mint_member_id()
    peers.join(second, tmp_path / "other", cli_name="reviewer")

    peers.leave(second, summary="landed")

    found = peers.address("reviewer")
    assert found is not None and found.id == first


def test_two_sessions_in_one_worktree_are_numbered_apart(tmp_path: Path) -> None:
    """The address a listing prints for either reaches that one and not the other."""
    peers = RepositoryPeers(tmp_path)
    first, second, third = mint_member_id(), mint_member_id(), mint_member_id()
    worktree = tmp_path / "dev"

    peers.join(first, worktree)
    peers.join(second, worktree)
    peers.join(third, worktree)

    assert [peers.called(one) for one in (first, second, third)] == [
        "dev",
        "dev-2",
        "dev-3",
    ]
    assert {view.address for view in peers.listing()} == {"dev", "dev-2", "dev-3"}
    found = peers.address("dev-2")
    assert found is not None and found.id == second


def test_a_numbered_name_is_kept_across_rejoins_and_freed_by_a_departure(
    tmp_path: Path,
) -> None:
    """Numbered against the live sessions, so a departure gives the name back."""
    peers = RepositoryPeers(tmp_path)
    first, second, third = mint_member_id(), mint_member_id(), mint_member_id()
    worktree = tmp_path / "dev"
    peers.join(first, worktree)
    peers.join(second, worktree)

    peers.join(second, worktree)
    peers.leave(first)
    peers.join(third, worktree)

    assert peers.called(second) == "dev-2"
    assert peers.called(third) == "dev"


def test_a_launched_session_joins_under_the_name_its_launcher_minted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime's chrome and the roster show one name, because one process chose it."""
    monkeypatch.setenv(NAME_ENV, "dev-2")
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()

    peers.join(member, tmp_path / "dev")

    assert peers.called(member) == "dev-2"


def test_a_launcher_mints_a_name_no_live_session_answers_to(tmp_path: Path) -> None:
    """Numbered against the roster it reads, and reading it creates nothing."""
    worktree = tmp_path / "dev"
    worktree.mkdir()

    alone = launched_member(worktree)
    assert alone.cli_name == "dev"
    assert not coordination_root(worktree).exists()

    RepositoryPeers(worktree).join(mint_member_id(), worktree)
    beside = launched_member(worktree)
    assert beside.cli_name == "dev-2"
    assert beside.member_id != alone.member_id
    assert beside.environment() == {MEMBER_ENV: beside.member_id, NAME_ENV: "dev-2"}


def test_a_listing_says_what_each_session_is_doing_now(tmp_path: Path) -> None:
    """A task is what a member arrived for; a description is where it has got to."""
    peers, member = joined(tmp_path, "reviewer")

    peers.describe(member, "reading the merge for dropped code")

    [view] = peers.listing()
    assert view.doing == "reading the merge for dropped code"
    assert view.address == "reviewer"


def test_a_listing_says_what_each_session_is_holding_not_only_what_it_claims(
    tmp_path: Path,
) -> None:
    """The row is read to decide whether it is safe to write, and a description
    cannot answer that: it is only as fresh as the last time somebody wrote it.
    """
    peers, member = joined(tmp_path, "reviewer")

    peers.describe(member, "reading the merge")
    (tmp_path / "packages" / "lup").mkdir(parents=True)
    peers.lock(member, tmp_path / "packages" / "lup")

    [view] = peers.listing()
    assert view.doing == "reading the merge"
    assert view.holding == [f"under {tmp_path / 'packages' / 'lup'}"]
    assert not view.contested


def test_a_session_holding_nothing_says_so_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """Empty is the honest state, and is what a session that has not written
    yet must report — a reader treats it as "go ahead", so inventing a claim
    here would be worse than saying nothing.
    """
    peers, _member = joined(tmp_path, "reviewer")

    [view] = peers.listing()

    assert view.holding == []
    assert view.contested == []


def test_a_claim_nothing_could_attribute_appears_on_both_sessions_rows(
    tmp_path: Path,
) -> None:
    """Contested is the half a reader acts on: two sessions are in one place and
    neither of them knows it.
    """
    peers, first = joined(tmp_path, "first")
    second = mint_member_id()
    peers.join(second, tmp_path / "other", cli_name="second")
    disputed = tmp_path / "src" / "shared.py"
    disputed.parent.mkdir(parents=True, exist_ok=True)
    disputed.write_text("shared = True\n", encoding="utf-8")

    # Nothing records the contest. Each session writes down what it left the
    # path in, on its own file, and the two meeting there is the whole of it —
    # which is what a single record with a guessed author could never be.
    peers.touched(first, disputed)
    peers.touched(second, disputed)

    rows = {view.address: view for view in peers.listing()}
    assert rows["first"].contested == [f"at {disputed}"]
    assert rows["second"].contested == [f"at {disputed}"]


def test_a_session_that_never_described_itself_falls_back_to_its_task(
    tmp_path: Path,
) -> None:
    """Silence about the work is not silence about the purpose."""
    peers, _member = joined(tmp_path, "reviewer", worktree="feature")

    [view] = peers.listing()

    assert view.doing.endswith("feature")


def test_a_departure_since_the_reader_joined_is_listed_and_says_so(
    tmp_path: Path,
) -> None:
    """Whether the peer you wrote to is still there is what a listing answers."""
    peers, reviewer = joined(tmp_path, "reviewer")
    reader = mint_member_id()
    peers.join(reader, tmp_path / "other", cli_name="reader")
    arrived = peers.row(reader)
    assert arrived is not None and arrived.arrived is not None

    peers.leave(reviewer, summary="landed it")

    rows = {view.address: view for view in peers.listing(since=arrived.arrived)}
    assert set(rows) == {"reader", "reviewer"}
    assert not rows["reviewer"].member.running
    assert rows["reviewer"].member.summary == "landed it"
    assert [view.address for view in peers.listing()] == ["reader"]


def test_a_departure_before_the_reader_joined_is_history(tmp_path: Path) -> None:
    """What left before the reader came is nothing the reader could have written to."""
    peers, reviewer = joined(tmp_path, "reviewer")
    peers.leave(reviewer, summary="landed it")
    reader = mint_member_id()
    peers.join(reader, tmp_path / "other", cli_name="reader")
    arrived = peers.row(reader)
    assert arrived is not None

    assert [view.address for view in peers.listing(since=arrived.arrived)] == ["reader"]


def test_a_console_is_told_the_recent_departures_and_not_the_old(
    tmp_path: Path,
) -> None:
    """No arrival of its own, so the retention window stands in for one."""
    peers, reviewer = joined(tmp_path, "reviewer")
    peers.leave(reviewer, summary="landed it")

    assert [view.address for view in peers.recent()] == ["reviewer"]

    peers.retention = Retention(departed_seconds=0.0)
    assert peers.recent(now=utc_now() + timedelta(seconds=1)) == []


def test_a_message_to_nobody_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """Each surface renders "nobody answers to that" in its own words."""
    peers, _member = joined(tmp_path, "reviewer")

    assert peers.send("nobody", "hello") is None


def test_a_message_to_a_session_that_left_is_refused_with_its_departure(
    tmp_path: Path,
) -> None:
    """Mail waiting for nobody tells the sender it was delivered."""
    peers, member = joined(tmp_path, "reviewer")
    peers.leave(member, summary="landed it")

    with pytest.raises(PeerDepartedError) as refused:
        peers.send("reviewer", "the base moved under you")

    assert "reviewer left at" in str(refused.value)
    assert "landed it" in str(refused.value)
    assert peers.waiting(member).messages == []


def test_mail_reaches_a_peer_and_is_taken_once(tmp_path: Path) -> None:
    """The durable record: it waits in the file until somebody reads it."""
    peers, member = joined(tmp_path, "reviewer")

    peers.send("reviewer", "the base moved under you")

    assert [item.text for item in peers.waiting(member).messages] == [
        "the base moved under you"
    ]
    assert len(peers.take(member).messages) == 1
    assert peers.waiting(member).messages == []


def test_a_repository_peer_reads_when_it_looks(tmp_path: Path) -> None:
    """Nothing wakes a session nobody is holding, and the roster says so."""
    peers, member = joined(tmp_path, "reviewer")

    [view] = peers.listing()

    assert view.member.delivery is Delivery.WAITING
    assert view.member.worktree.endswith("tree")
    assert view.member.actor.id == member


def test_a_session_with_no_name_is_called_after_its_worktree(tmp_path: Path) -> None:
    """The one fact a session has before it has done anything."""
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()

    peers.join(member, tmp_path / "feat-coordination")

    assert peers.called(member) == "feat-coordination"
    assert derived_cli_name(tmp_path / "feat-coordination") == "feat-coordination"


def test_a_launcher_that_minted_an_id_outranks_the_runtime_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launcher set it where an agent's own shell call cannot reach."""
    monkeypatch.setenv(MEMBER_ENV, "proven")

    assert session_member_id("runtime-session") == "proven"


def test_a_session_nobody_launched_answers_to_its_own_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full peer that cannot prove who started it is still a peer."""
    monkeypatch.delenv(MEMBER_ENV, raising=False)

    assert session_member_id("runtime-session") == "runtime-session"
    assert session_member_id() == ""


def test_every_name_a_session_answered_to_stays_on_its_own_file(
    tmp_path: Path,
) -> None:
    """Kept rather than replaced, which is what lets an old one go on resolving.

    On the member rather than in a record beside it: the two were only ever
    read together, and a name is the member changing rather than an event
    about it.
    """
    peers, member = joined(tmp_path, "first")

    peers.rename(member, "second")

    [found] = [one for one in store.members(peers.root) if one.get("id") == member]
    assert [named.get("cli_name") for named in store.names_of(found)] == [
        "first",
        "second",
    ]
    assert peers.answering("first") == member
    assert peers.called(member) == "second"


async def test_a_session_using_its_tools_is_on_the_roster(tmp_path: Path) -> None:
    """A verb puts its session on the roster before it does anything else.

    Nothing else could: a description names a member the fold has never seen
    and is dropped, and a peer looking for who is working here reads a roster
    this session is absent from. Joining on every call rather than once is
    what lets a tool server answer without knowing whether it is the first.
    """
    peers = RepositoryPeers(tmp_path)
    tools = {
        tool.name: tool
        for tool in create_peer_tools(peers, "abc123", tmp_path / "feat-thing")
    }

    await tools["coordination_describe"].handler({"description": "rewriting the guard"})

    [listed] = peers.listing()
    assert listed.member.actor.id == "abc123"
    assert listed.doing == "rewriting the guard"
    assert listed.member.delivery == Delivery.HOOK


async def test_joining_twice_leaves_one_member(tmp_path: Path) -> None:
    """Idempotent, because every verb calls it and a session takes many."""
    peers = RepositoryPeers(tmp_path)
    tools = {
        tool.name: tool
        for tool in create_peer_tools(peers, "abc123", tmp_path / "feat-thing")
    }

    await tools["coordination_describe"].handler({"description": "rewriting"})
    await tools["coordination_peers"].handler({})
    await tools["coordination_peers"].handler({})

    assert len(peers.listing()) == 1


def verbs(peers: RepositoryPeers, member: str, worktree: Path) -> dict[str, LupMcpTool]:
    """One session's coordination verbs, by name."""
    return {tool.name: tool for tool in create_peer_tools(peers, member, worktree)}


def refusal(response: ToolResponse) -> str:
    """What a verb said when it refused, failing the test where it did not refuse."""
    assert response.get("is_error"), response_text(response)
    return response_text(response)


async def test_the_verbs_are_refused_until_the_session_has_described_itself(
    tmp_path: Path,
) -> None:
    """A row saying only where a session is answers its readers wrongly."""
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, "abc123", tmp_path / "feat-thing")

    assert "coordination_describe" in refusal(
        await tools["coordination_peers"].handler({})
    )
    assert "coordination_describe" in refusal(
        await tools["coordination_send"].handler({"address": "user", "text": "hi"})
    )

    await tools["coordination_describe"].handler({"description": "rewriting"})

    await tools["coordination_peers"].handler({})
    assert [view.doing for view in peers.listing()] == ["rewriting"]


async def test_a_rewind_unsays_the_description_and_the_verbs_ask_again(
    tmp_path: Path,
) -> None:
    """What a discarded conversation said it was on is not what this one is on.

    The rewind is noticed where the conversation is visible — the prompt fold,
    which reads the transcript this prompt belongs to against the one this
    session's row records — and what it leaves is this row with nothing said
    about itself. The verbs then ask again, because they read the same field
    every peer does.
    """
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, "abc123", tmp_path / "feat-thing")
    peers.join("abc123", tmp_path / "feat-thing")
    await tools["coordination_describe"].handler({"description": "the old plan"})
    transcript = tmp_path / "transcript.jsonl"
    written(transcript, roots=1)
    changes(peers.root, "abc123", tmp_path / "feat-thing", str(transcript))

    written(transcript, roots=2)
    told = changes(peers.root, "abc123", tmp_path / "feat-thing", str(transcript))

    assert told and told[0] == MOVED_LINE
    assert "coordination_describe" in refusal(
        await tools["coordination_peers"].handler({})
    )


async def test_a_send_reports_what_is_queued_for_its_recipient(
    tmp_path: Path,
) -> None:
    """The recipient's queue, this message included, and never the sender's own."""
    peers, _reviewer = joined(tmp_path, "reviewer")
    tools = verbs(peers, "abc123", tmp_path / "dev")
    await tools["coordination_describe"].handler({"description": "rewriting"})
    peers.send("dev", "waiting for the sender, not the recipient")

    first = await tools["coordination_send"].handler(
        {"address": "reviewer", "text": "one"}
    )
    second = await tools["coordination_send"].handler(
        {"address": "reviewer", "text": "two"}
    )

    assert [
        each["outstanding"] for each in json.loads(response_text(first))["reached"]
    ] == [1]
    assert [
        each["outstanding"] for each in json.loads(response_text(second))["reached"]
    ] == [2]


async def test_an_answer_with_a_thread_reaches_everyone_in_the_discussion(
    tmp_path: Path,
) -> None:
    """What the person posted into a discussion an agent answers into it, so all of it reads whole."""
    peers, reviewer = joined(tmp_path, "reviewer")
    tools = verbs(peers, "abc123", tmp_path / "dev")
    await tools["coordination_describe"].handler({"description": "rewriting"})
    peers.send("abc123", "Is the relay done?", sender=reviewer)
    [asked] = peers.take("abc123").messages
    told = peers.post_into(asked.thread, "Both of you: report.", sender=USER_ADDRESS)

    answered = json.loads(
        response_text(
            await tools["coordination_send"].handler(
                {"thread": asked.thread, "text": "Done, merging now."}
            )
        )
    )

    assert answered["thread"] == asked.thread
    assert {each["address"] for each in answered["reached"]} == {
        f"session:{reviewer}#1",
        "user:user#1",
    }
    [to_reviewer] = [
        message
        for message in peers.waiting(reviewer).messages
        if message.text == "Done, merging now."
    ]
    assert to_reviewer.in_reply_to == told.post
    assert to_reviewer.post == answered["post"]


async def test_a_reply_names_its_post_and_the_mailbox_shows_the_thread(
    tmp_path: Path,
) -> None:
    peers, reviewer = joined(tmp_path, "reviewer")
    tools = verbs(peers, "abc123", tmp_path / "dev")
    await tools["coordination_describe"].handler({"description": "rewriting"})
    peers.send("abc123", "Is the relay done?", sender=reviewer)
    [asked] = peers.waiting("abc123").messages
    peers.post_into(asked.thread, "Both of you: report.", sender=USER_ADDRESS)

    read = json.loads(response_text(await tools["coordination_mailbox"].handler({})))

    assert read["messages"][0] == (
        f"[message from {reviewer} by agent · post {asked.post}] Is the relay done?"
    )
    assert read["messages"][1].startswith(
        f"[discussion «Is the relay done?» · with reviewer, user · thread {asked.thread}]"
    )

    replied = json.loads(
        response_text(
            await tools["coordination_send"].handler(
                {"address": "reviewer", "in_reply_to": asked.post, "text": "Yes."}
            )
        )
    )
    assert replied["thread"] == asked.thread
    [answer] = [
        message
        for message in peers.waiting(reviewer).messages
        if message.text == "Yes."
    ]
    assert (answer.in_reply_to, answer.thread) == (asked.post, asked.thread)


async def test_a_send_with_neither_address_nor_thread_is_refused(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, "abc123", tmp_path / "dev")
    await tools["coordination_describe"].handler({"description": "rewriting"})

    assert "`address`" in refusal(
        await tools["coordination_send"].handler({"text": "hi"})
    )


async def test_the_peers_listing_ends_with_the_person_and_what_they_hold(
    tmp_path: Path,
) -> None:
    """The person is a full peer: what they say they are on, and what they lock."""
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, "abc123", tmp_path / "dev")
    await tools["coordination_describe"].handler({"description": "rewriting"})
    held = tmp_path / "held"
    held.mkdir()
    peers.describe(USER_ADDRESS, "watching the relay land")
    peers.lock(USER_ADDRESS, held)

    listed = json.loads(response_text(await tools["coordination_peers"].handler({})))

    person = listed["peers"][-1]
    assert person["address"] == "user"
    assert person["doing"] == "watching the relay land"
    assert person["holding"] == [f"under {held}"]
    assert [claim.path for claim in peers.holding(held / "inner.py")] == [str(held)]


def test_the_person_s_hold_outlives_every_session_and_is_not_one(
    tmp_path: Path,
) -> None:
    """What they lock stands with nobody working, and nobody working is what ends a watch."""
    peers, member = joined(tmp_path, "builder")
    held = tmp_path / "held"
    held.mkdir()
    peers.lock(USER_ADDRESS, held)

    peers.leave(member, summary="landed it")

    assert peers.live_ids() == []
    assert [claim.path for claim in peers.held()] == [str(held)]
    assert [claim.path for claim in peers.holding(held / "inner.py")] == [str(held)]


async def test_a_send_to_ones_own_address_is_refused(tmp_path: Path) -> None:
    """The name resolved, to the sender, which is the one peer it cannot mean."""
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, "abc123", tmp_path / "dev")
    await tools["coordination_describe"].handler({"description": "rewriting"})

    assert "own address" in refusal(
        await tools["coordination_send"].handler({"address": "dev", "text": "hi"})
    )


async def test_a_send_to_a_session_that_left_is_a_refusal_naming_its_departure(
    tmp_path: Path,
) -> None:
    peers, gone = joined(tmp_path, "reviewer")
    peers.leave(gone, summary="landed it")
    tools = verbs(peers, "abc123", tmp_path / "dev")
    await tools["coordination_describe"].handler({"description": "rewriting"})

    assert "landed it" in refusal(
        await tools["coordination_send"].handler({"address": "reviewer", "text": "hi"})
    )


async def test_the_rename_verb_refuses_a_name_a_live_session_answers_to(
    tmp_path: Path,
) -> None:
    peers, _other = joined(tmp_path, "reviewer")
    tools = verbs(peers, "abc123", tmp_path / "dev")

    assert "reviewer" in refusal(
        await tools["coordination_rename"].handler({"name": "reviewer"})
    )

    await tools["coordination_rename"].handler({"name": "merger"})
    assert peers.called("abc123") == "merger"
    found = peers.address("merger")
    assert found is not None and found.id == "abc123"


async def test_a_lock_over_a_path_that_does_not_exist_is_refused(
    tmp_path: Path,
) -> None:
    """A lock covers what is there to write."""
    peers = RepositoryPeers(tmp_path)
    tools = verbs(peers, "abc123", tmp_path / "dev")
    await tools["coordination_describe"].handler({"description": "rewriting"})

    assert "does not exist" in refusal(
        await tools["coordination_lock"].handler({"path": str(tmp_path / "gone")})
    )
    assert peers.held() == []


async def test_releasing_a_prefix_this_session_does_not_hold_is_refused(
    tmp_path: Path,
) -> None:
    """Named rather than silent, because the asker is about to act on the answer."""
    peers, holder = joined(tmp_path, "reviewer")
    held = tmp_path / "src"
    held.mkdir()
    peers.lock(holder, held)
    tools = verbs(peers, "abc123", tmp_path / "dev")
    await tools["coordination_describe"].handler({"description": "rewriting"})

    assert holder in refusal(
        await tools["coordination_release"].handler({"path": str(held)})
    )
    assert peers.holding(held / "a.py")


def test_a_claim_over_a_path_that_has_gone_ends_at_the_read(
    tmp_path: Path,
) -> None:
    """A worktree removed from under a live session takes its paths with it.

    Nothing is swept and nothing is written. The claim carries the path's
    modification time, so a reader asks the filesystem: gone is vacant, and a
    file written since is somebody else's state rather than this session's.
    That is what kept a claim over a deleted worktree standing for a day —
    there was a record and nothing to retire it with.
    """
    peers, member = joined(tmp_path, "reviewer")
    tree = tmp_path / "tree"
    tree.mkdir()
    changed = tree / "a.py"
    changed.write_text("value = 1\n", encoding="utf-8")
    peers.touched(member, changed)
    assert [claim.path for claim in peers.held()] == [str(changed)]

    changed.unlink()

    assert peers.held() == []

    changed.write_text("value = 2\n", encoding="utf-8")
    assert peers.held() == []


def test_a_row_names_the_transcript_its_last_prompt_came_from(tmp_path: Path) -> None:
    """What a session is doing now is in its runtime's transcript, so the row says which.

    The prompt fold records the transcript each prompt belongs to; a reader
    following what the session does reads it off the row rather than guessing
    where a runtime keeps it.
    """
    peers, member = joined(tmp_path, "reviewer")
    transcript = tmp_path / "transcript.jsonl"
    written(transcript, roots=1)
    assert [row.transcript for row in peers.present()] == [""]

    changes(peers.root, member, tmp_path / "tree", str(transcript))

    assert [row.transcript for row in peers.present()] == [str(transcript)]
