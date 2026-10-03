"""A post goes to everyone in its discussion, and every copy says which discussion it is.

A thread is the posts that answer each other, back to the one it began with;
everyone who wrote in it or was written to is in it, the person included.
Posting into one leaves one copy per participant but the sender, all sharing
one post id, each headed by the discussion so its reader can answer them all.
"""

from pathlib import Path

import pytest

from lup.channels.models import Door
from lup.coordination.bare import mail as bare_mail
from lup.coordination.identity import mint_member_id
from lup.coordination.mail import Posting
from lup.coordination.peers import USER_ADDRESS, user_peer
from lup.coordination.repository import RepositoryPeers


def joined(peers: RepositoryPeers, name: str, root: Path) -> str:
    member = mint_member_id()
    peers.join(member, root / name, cli_name=name)
    return member


def test_every_message_names_its_post_and_begins_its_own_thread(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    reader = joined(peers, "reader", tmp_path)

    peers.send(reader, "the base moved", sender=USER_ADDRESS)

    [message] = peers.waiting(reader).messages
    assert message.post
    assert message.thread == message.post
    assert message.title == ""


def test_a_reply_goes_into_the_thread_of_the_post_it_answers(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    research = joined(peers, "research", tmp_path)
    summary = joined(peers, "summary", tmp_path)
    peers.send(summary, "Which sources are in?", sender=research)
    [asked] = peers.take(summary).messages

    peers.send(research, "Three of them.", sender=summary, in_reply_to=asked.post)

    [answer] = peers.waiting(research).messages
    assert answer.in_reply_to == asked.post
    assert answer.thread == asked.post
    assert answer.post != asked.post


def test_a_post_into_a_discussion_reaches_everyone_in_it_but_its_sender(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    research = joined(peers, "research", tmp_path)
    summary = joined(peers, "summary", tmp_path)
    peers.send(summary, "Which sources are in?\nAsking before I cite.", sender=research)
    [asked] = peers.take(summary).messages
    peers.send(
        USER_ADDRESS, "Two are paywalled.", sender=summary, in_reply_to=asked.post
    )

    posted = peers.post_into(
        asked.thread, "Cite the open ones only.", sender=USER_ADDRESS, door=Door.PAGE
    )

    assert {member.id for member in posted.reached} == {research, summary}
    assert posted.refused == []
    copies = [
        *peers.waiting(research).messages,
        *peers.waiting(summary).messages,
    ]
    assert {copy.post for copy in copies} == {posted.post}
    assert {copy.thread for copy in copies} == {asked.thread}
    assert {copy.title for copy in copies} == {"Which sources are in?"}
    [to_research] = peers.waiting(research).messages
    assert to_research.participants == ["summary", USER_ADDRESS]
    assert to_research.in_reply_to != ""
    handed = bare_mail.spoken(bare_mail.waiting(peers.root, f"session-{research}"))
    assert handed == (
        f"[discussion «Which sources are in?» · with summary, user · thread "
        f"{asked.thread}] [message from user by page · post {posted.post}] "
        "Cite the open ones only."
    )


def test_a_post_answers_the_latest_post_unless_it_names_one(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    research = joined(peers, "research", tmp_path)
    summary = joined(peers, "summary", tmp_path)
    peers.send(summary, "first", sender=research)
    [first] = peers.take(summary).messages
    peers.send(research, "second", sender=summary, in_reply_to=first.post)
    [second] = peers.take(research).messages

    latest = peers.post_into(first.thread, "third", sender=USER_ADDRESS)
    named = peers.post_into(
        first.thread, "fourth", sender=USER_ADDRESS, in_reply_to=first.post
    )

    by_post = {
        message.post: message.in_reply_to
        for message in peers.waiting(research).messages
    }
    assert by_post[latest.post] == second.post
    assert by_post[named.post] == first.post


def test_a_named_member_joins_the_discussion_from_then_on(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    research = joined(peers, "research", tmp_path)
    summary = joined(peers, "summary", tmp_path)
    review = joined(peers, "review", tmp_path)
    peers.send(summary, "Which sources are in?", sender=research)
    [asked] = peers.take(summary).messages

    peers.post_into(
        asked.thread, "Bring review in.", sender=USER_ADDRESS, joining=("review",)
    )
    later = peers.post_into(asked.thread, "And now all three.", sender=research)

    assert {member.id for member in later.reached} == {summary, review, USER_ADDRESS}
    assert [message.text for message in peers.take(USER_ADDRESS).messages] == [
        "And now all three."
    ]


def test_a_participant_that_left_is_said_and_the_rest_reached(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    research = joined(peers, "research", tmp_path)
    summary = joined(peers, "summary", tmp_path)
    peers.send(summary, "Which sources are in?", sender=research)
    [asked] = peers.take(summary).messages
    peers.leave(research, summary="done")

    posted = peers.post_into(asked.thread, "Anyone?", sender=USER_ADDRESS)

    assert [member.id for member in posted.reached] == [summary]
    assert [refusal.address for refusal in posted.refused] == ["research"]


def test_a_thread_no_post_began_is_refused(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    joined(peers, "research", tmp_path)

    with pytest.raises(LookupError, match="began a thread"):
        peers.post_into("0123456789ab", "hello?", sender=USER_ADDRESS)


def test_a_discussion_reads_back_to_its_first_post_and_no_further(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    research = joined(peers, "research", tmp_path)
    summary = joined(peers, "summary", tmp_path)
    peers.send(summary, "an older exchange", sender=research)
    peers.cohort.mail.send(
        user_peer(), "the start", sender=research, posting=Posting(post="feedface0001")
    )
    peers.send(
        summary, "the start", sender=research, posting=Posting(post="feedface0001")
    )
    peers.send(research, "a reply", sender=summary, in_reply_to="feedface0001")

    found = peers.cohort.mail.discussion("feedface0001")

    assert found is not None
    assert found.title == "the start"
    assert found.participants == [research, USER_ADDRESS, summary]
