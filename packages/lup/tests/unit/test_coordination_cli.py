"""The console's sweep: what a person runs for a roster no server is up on.

A roster where every session that ever joined reads as running is one only a
person's command can put right, because the servers that would sweep it are
gone.
"""

import os
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.channels.models import utc_now
from lup.coordination.bare.store import departed_path, member_path, session_actor
from lup.coordination.identity import mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.devtools.coordination import app as coordination_app


@pytest.mark.parametrize("explicit", ["", "chosen-session"])
def test_join_preserves_launcher_identity_unless_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: str
) -> None:
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    monkeypatch.setenv("LUP_COORDINATION_MEMBER", "launched-session")
    arguments = ["join", *(["--id", explicit] if explicit else [])]

    result = CliRunner().invoke(coordination_app.create_coordination_app(), arguments)

    chosen = explicit or "launched-session"
    assert result.exit_code == 0, result.output
    assert result.output == f"{chosen}\n"
    assert RepositoryPeers(tmp_path).row(chosen) is not None
    assert RepositoryPeers(tmp_path).live_ids() == [chosen]


def test_join_without_launcher_mints_a_stable_printed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    monkeypatch.delenv("LUP_COORDINATION_MEMBER", raising=False)
    monkeypatch.setattr(coordination_app, "mint_member_id", lambda: "minted-session")

    result = CliRunner().invoke(coordination_app.create_coordination_app(), ["join"])

    assert result.exit_code == 0, result.output
    assert result.output == "minted-session\n"
    assert RepositoryPeers(tmp_path).row("minted-session") is not None


def long_gone(root: Path, name: str) -> tuple[RepositoryPeers, str]:
    """One repository whose only session spoke an hour ago and never beat.

    The member file put back in time, which is the whole of what a stopped
    session looks like: its modification time is the pulse, so there is
    nothing to append and no stamp to leave behind.
    """
    peers = RepositoryPeers(root)
    member = mint_member_id()
    peers.join(member, root / "tree", cli_name=name)
    when = (utc_now() - timedelta(hours=1)).timestamp()
    os.utime(member_path(peers.root, session_actor(member)), (when, when))
    return peers, member


def test_a_dry_run_names_the_lapsed_and_retires_nobody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    peers, member = long_gone(tmp_path, "stale")

    result = CliRunner().invoke(
        coordination_app.create_coordination_app(), ["sweep", "-n"]
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"stale — session:{member}#1 — unheard since ")
    assert result.output.endswith("would retire 1 session(s)\n")
    # Nothing moved, which is the whole of what a dry run promises: the file
    # is where a live member's file sits, and the pulse says what it says
    # whether or not anybody has swept.
    assert member_path(peers.root, session_actor(member)).is_file()
    assert not departed_path(peers.root, session_actor(member)).exists()


def test_a_sweep_retires_the_lapsed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    peers, member = long_gone(tmp_path, "stale")
    app = coordination_app.create_coordination_app()

    first = CliRunner().invoke(app, ["sweep"])
    second = CliRunner().invoke(app, ["sweep"])

    assert first.exit_code == 0, first.output
    assert first.output.endswith("retired 1 session(s)\n")
    assert second.output == "retired 0 session(s)\n"
    [recorded] = [one for one in peers.cohort.live() if one.actor.id == member]
    assert not recorded.running
    assert recorded.error.startswith("unheard since ")


def test_the_person_reads_their_own_mailbox_and_takes_it_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--id user` is the person's mailbox, headed with who wrote and the post to answer."""
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()
    peers.join(member, tmp_path / "tree", cli_name="builder")
    peers.send("user", "the store half is done", sender=member)
    [sent] = peers.waiting("user").messages
    app = coordination_app.create_coordination_app()

    peeked = CliRunner().invoke(app, ["mailbox", "--id", "user"])
    taken = CliRunner().invoke(app, ["mailbox", "--id", "user", "--take"])

    heading = f"[message from {member} by agent · post {sent.post}]"
    assert peeked.exit_code == 0, peeked.output
    assert peeked.output == f"{heading} the store half is done\n"
    assert taken.output == peeked.output
    assert peers.waiting("user").messages == []


def test_the_person_answers_from_the_console_into_the_post_s_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--as user` signs it so the answer comes back to the person's mailbox."""
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()
    peers.join(member, tmp_path / "tree", cli_name="builder")
    peers.send("user", "rebase or merge?", sender=member)
    [asked] = peers.waiting("user").messages

    answered = CliRunner().invoke(
        coordination_app.create_coordination_app(),
        ["send", "merge", "--to", "builder", "--as", "user", "--reply-to", asked.post],
    )

    assert answered.exit_code == 0, answered.output
    [reply] = peers.waiting(member).messages
    assert f"queued for session:{member}#1 as post {reply.post}, " in answered.output
    assert (reply.sender, reply.in_reply_to, reply.thread) == (
        "user",
        asked.post,
        asked.thread,
    )
    assert reply.heading().startswith("[message from user by console")


def test_a_send_signed_by_nobody_here_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordination_app, "project_root", lambda: tmp_path)
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()
    peers.join(member, tmp_path / "tree", cli_name="builder")

    refused = CliRunner().invoke(
        coordination_app.create_coordination_app(),
        ["send", "hi", "--to", "builder", "--as", "somebody"],
    )

    assert refused.exit_code != 0
    assert "`--as somebody` — signs as nobody here: it takes `user`" in refused.output
    assert peers.waiting(member).messages == []
