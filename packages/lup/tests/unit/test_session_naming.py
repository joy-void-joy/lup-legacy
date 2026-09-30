"""A session named for its work, decided in the half both runtimes ship.

Everything a runtime's naming hook does besides asking a model is decided in
:mod:`lup.coordination.bare.naming`: whether a prompt asks, what an answer may
be, how a name is settled against the live sessions' names, and when the
runtime is given the roster's name. Each is asserted here over a store the
typed writers produced, so the half a bare interpreter runs agrees with the
library by construction rather than by a pin.
"""

import json
import time
from datetime import timedelta
from pathlib import Path

import pytest

from lup.channels.models import utc_now
from lup.coordination.bare import naming
from lup.coordination.bare.naming import Arrival, Naming, Titling
from lup.coordination.bare.store import (
    Member,
    current_name,
    member_of,
    session_actor,
    stamped,
)
from lup.coordination.identity import NameTakenError, mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath


def declared(attempts: int = 3, deadline_seconds: float = 20.0) -> Naming:
    """The naming declaration as a hook reads it, with what a test varies."""
    return Naming(
        instruction="Name the work.",
        attempts=attempts,
        deadline_seconds=deadline_seconds,
        longest=48,
        arguments=["--model", "opus", "--effort", "low", "--tools", ""],
    )


def joined(
    peers: RepositoryPeers, worktree: Path, name: str, wake: WakePath = WakePath()
) -> str:
    """One session on the roster under *name*, and the id it answers to."""
    member = mint_member_id()
    peers.join(member, worktree, cli_name=name, wake=wake)
    return member


def arrival(worktree: Path) -> Arrival:
    """A prompt from the root session working in *worktree*."""
    return Arrival(
        session_id="root-session",
        cwd=str(worktree),
        prompt="Rename sessions after their work",
    )


def strangers(worktree: Path) -> list[Arrival]:
    """Prompts carrying the member's id that are not its root session's own."""
    return [
        Arrival(session_id="root-session", cwd=str(worktree), agent_id="child"),
        Arrival(session_id="root-session", cwd=str(worktree), agent_type="reviewer"),
        Arrival(session_id="another-session", cwd=str(worktree)),
        Arrival(session_id="root-session", cwd="/somewhere/else"),
    ]


def owned(peers: RepositoryPeers, member: str, worktree: Path) -> Member:
    """The member as its own root session's prompt finds it."""
    found = naming.owning(peers.root, member, arrival(worktree))
    assert found is not None
    return found


def test_a_session_answering_only_to_the_name_it_joined_with_is_due(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    found = owned(peers, member, tmp_path)

    assert naming.due(found, naming.looked(peers.root, member, found), declared(), None)


def test_a_renamed_session_is_never_named_over(tmp_path: Path) -> None:
    """A second name is somebody's choice, whoever made it."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    peers.rename(member, "chosen")
    found = owned(peers, member, tmp_path)

    assert not naming.due(
        found, naming.looked(peers.root, member, found), declared(), None
    )


@pytest.mark.parametrize(
    ("shown", "asks"),
    [
        (None, True),
        ("", True),
        ("worktree", True),
        ("worktree-2", True),
        ("somebody-chose-this", False),
    ],
)
def test_a_title_somebody_set_in_the_chrome_stops_the_ask(
    tmp_path: Path, shown: str | None, asks: bool
) -> None:
    """Nothing reported, nothing set, or a default of the worktree's leaves the ask owed."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, worktree, "worktree")
    found = owned(peers, member, worktree)

    assert (
        naming.due(found, naming.looked(peers.root, member, found), declared(), shown)
        is asks
    )


def test_asking_stops_once_the_declared_attempts_are_spent(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    found = owned(peers, member, tmp_path)
    titling = naming.looked(peers.root, member, found)
    for _ in range(2):
        naming.asking(peers.root, member, titling)
        titling = naming.concluded(peers.root, member)

    assert naming.due(found, titling, declared(attempts=3), None)
    assert not naming.due(found, titling, declared(attempts=2), None)


def test_an_ask_under_way_is_not_started_again_until_its_deadline_passes(
    tmp_path: Path,
) -> None:
    """A slow ask settles the name either way; one that died stops counting."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    found = owned(peers, member, tmp_path)
    started = utc_now()
    titling = Titling(
        attempts=1, asked=stamped(started), pushed="dev", shown="dev", resumed=""
    )

    assert not naming.due(
        found, titling, declared(), None, now=started + timedelta(seconds=5)
    )
    assert naming.due(
        found, titling, declared(), None, now=started + timedelta(seconds=21)
    )


def test_a_name_a_live_session_answers_to_is_numbered_past(tmp_path: Path) -> None:
    """The typed rename refuses the name; the hook, which nobody chose for, numbers it."""
    peers = RepositoryPeers(tmp_path)
    joined(peers, tmp_path, "auth-refactor")
    member = joined(peers, tmp_path, "dev")

    with pytest.raises(NameTakenError):
        peers.rename(member, "auth-refactor")
    taken = naming.settled(peers.root, member, "auth-refactor")

    assert taken == "auth-refactor-2"
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert current_name(found) == "auth-refactor-2"
    assert peers.address("dev") == peers.address("auth-refactor-2")


def test_a_rename_that_landed_first_is_kept(tmp_path: Path) -> None:
    """Settled against the member as it is under the lock, not as the hook read it."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    peers.rename(member, "chosen")

    assert naming.settled(peers.root, member, "session-naming") == ""
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert current_name(found) == "chosen"


def test_only_the_members_own_root_session_is_named(tmp_path: Path) -> None:
    """A child carrying its launcher's member id names nothing, as it binds nothing."""
    peers = RepositoryPeers(tmp_path)
    member = joined(
        peers,
        tmp_path,
        "dev",
        wake=WakePath(runtime="claude", handle="/inbox", session="root-session"),
    )

    assert naming.owning(peers.root, member, arrival(tmp_path)) is not None
    assert [
        naming.owning(peers.root, member, stranger) for stranger in strangers(tmp_path)
    ] == [None, None, None, None]


def test_a_session_not_yet_on_the_roster_is_named_later(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    joined(peers, tmp_path, "dev")

    assert naming.owning(peers.root, mint_member_id(), arrival(tmp_path)) is None


def test_the_runtime_is_given_a_roster_rename_once(tmp_path: Path) -> None:
    """The first look takes the launch's name as given; a rename is carried once."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    titling = naming.looked(peers.root, member, owned(peers, member, tmp_path))

    assert naming.pending(owned(peers, member, tmp_path), titling) == ""
    peers.rename(member, "chosen")
    assert naming.pending(owned(peers, member, tmp_path), titling) == "chosen"
    titling = naming.shown_as(peers.root, member, "chosen", "chosen")
    assert naming.pending(owned(peers, member, tmp_path), titling) == ""
    assert naming.recalled(peers.root, member) == titling


def test_a_title_somebody_set_in_the_chrome_is_taken_up_by_the_roster(
    tmp_path: Path,
) -> None:
    """A `/rename`, or a reopened conversation's own title, is the newer choice.

    Taken up over whatever the roster was called, since it was chosen where
    the session is looked at, in the shape a roster name has; the chrome
    keeps it as it was set, and nothing is handed back to it. Once: the next
    look finds the chrome showing what was recorded.
    """
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    peers.rename(member, "chosen-by-a-peer")
    found = owned(peers, member, tmp_path)
    titling = naming.looked(peers.root, member, found)

    assert naming.adoptable(found, titling, "") == ""
    assert naming.adoptable(found, titling, "chosen-by-a-peer") == ""
    assert naming.adoptable(found, titling, "My Own Title") == "My Own Title"
    assert naming.adopted(peers.root, member, "My Own Title", 48) == "my-own-title"
    titling = naming.looked(peers.root, member, owned(peers, member, tmp_path))
    assert titling["shown"] == "My Own Title"
    assert (
        naming.adoptable(owned(peers, member, tmp_path), titling, "My Own Title") == ""
    )
    assert naming.pending(owned(peers, member, tmp_path), titling) == ""


def test_a_title_taken_up_is_numbered_past_a_live_session_that_has_it(
    tmp_path: Path,
) -> None:
    peers = RepositoryPeers(tmp_path)
    joined(peers, tmp_path, "taken")
    member = joined(peers, tmp_path, "dev")

    assert naming.adopted(peers.root, member, "Taken", 48) == "taken-2"
    titling = naming.looked(peers.root, member, owned(peers, member, tmp_path))
    assert (titling["pushed"], titling["shown"]) == ("taken-2", "Taken")


def test_a_resume_heard_before_the_session_joined_waits_for_its_first_look(
    tmp_path: Path,
) -> None:
    """A runtime reports a resume as the session starts, which can be before
    its tool server has put it on the roster; the first look begins around it.
    """
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()
    naming.resuming(peers.root, member, "thread-9")
    peers.join(member, tmp_path, cli_name="dev")

    titling = naming.looked(peers.root, member, owned(peers, member, tmp_path))

    assert titling["resumed"] == "thread-9"
    assert titling["pushed"] == "dev"
    assert naming.concluded(peers.root, member)["resumed"] == ""


@pytest.mark.parametrize(
    ("reply", "name"),
    [
        ('{"name": "auth-refactor"}', "auth-refactor"),
        ('{"name": "session-naming-hook-2"}', "session-naming-hook-2"),
        ('{"name": null}', ""),
        ('{"name": "Auth Refactor"}', ""),
        ('{"name": "auth--refactor"}', ""),
        ('{"name": "-auth"}', ""),
        ('{"name": "refactoré"}', ""),
        (json.dumps({"name": "-".join(["word"] * 12)}), ""),
        ('{"title": "auth-refactor"}', ""),
        ("[]", ""),
        ("auth-refactor", ""),
    ],
)
def test_an_answer_names_only_in_the_shape_a_name_takes(reply: str, name: str) -> None:
    """What a person types to reach the session: worktree-shaped, and not too long."""
    assert naming.answered(reply, 48) == name


def test_the_answer_schema_admits_a_name_or_nothing() -> None:
    schema = json.loads(naming.answer_schema())

    assert schema["required"] == ["name"]
    assert schema["properties"]["name"]["type"] == ["string", "null"]
    assert schema["additionalProperties"] is False


def test_a_malformed_declaration_names_nothing(tmp_path: Path) -> None:
    """Checked field by field, so a hook never asks under a deadline it misread."""
    written = tmp_path / "session_naming.json"
    written.write_text(json.dumps({**declared(), "attempts": "3"}))
    assert naming.settings(written) is None
    written.write_text(json.dumps({**declared(), "arguments": "--tools"}))
    assert naming.settings(written) is None

    written.write_text(json.dumps(declared()))
    assert naming.settings(written) == declared()
    assert naming.settings(tmp_path / "absent.json") is None


def test_the_compiled_declaration_is_read_beside_the_hooks_manifest(
    tmp_path: Path,
) -> None:
    """Outside the runtime directory, whose every file a policy snapshot hashes."""
    host = tmp_path / "hooks" / "runtime" / "session_naming.py"
    host.parent.mkdir(parents=True)
    (tmp_path / "hooks" / "session_naming.json").write_text(json.dumps(declared()))

    assert naming.compiled_for(host) == declared()


def test_the_prompt_reaches_the_model_quoted_as_the_thing_to_name() -> None:
    """Between the markers the declared instruction points at, whole."""
    prompt = "Refactor the roster\n\nand keep every line of this"

    assert naming.request_for(prompt) == f"<request>\n{prompt}\n</request>"


def test_an_ask_that_overruns_its_deadline_is_killed_with_everything_it_started(
    tmp_path: Path,
) -> None:
    """A wrapper killed alone leaves its real binary running; the session goes whole."""
    survivor = tmp_path / "survived"
    started = utc_now()

    assert (
        naming.ran(
            ["sh", "-c", f"(sleep 2; touch {survivor}) & wait"],
            "",
            0.5,
        )
        is None
    )
    assert (utc_now() - started).total_seconds() < 2
    for _ in range(30):
        if survivor.exists():
            break
        time.sleep(0.1)
    assert not survivor.exists()


def test_an_ask_that_answers_in_time_is_read_whole() -> None:
    assert naming.ran(["cat"], '{"name": "auth-refactor"}', 5.0) == (
        '{"name": "auth-refactor"}'
    )
    assert naming.ran(["no-such-program-anywhere"], "", 5.0) is None


@pytest.mark.parametrize(
    ("title", "form"),
    [
        ("Renamed By Hand", "renamed-by-hand"),
        (
            "Fix expired MCP authentication token",
            "fix-expired-mcp-authentication-token",
        ),
        ("⚠ MCP client for `codex_apps` failed", "mcp-client-for-codex-apps-failed"),
        ("already-shaped", "already-shaped"),
        ("café au lait", "caf-au-lait"),
        (" ".join(["word"] * 12), "-".join(["word"] * 9)),
        ("x" * 60, "x" * 48),
        ("⚠ — …", ""),
    ],
)
def test_a_title_takes_the_roster_s_shape_only_on_the_roster(
    title: str, form: str
) -> None:
    """Lowercased, runs of anything else one hyphen, whole words while they fit."""
    assert naming.roster_form(title, 48) == form
    assert form == "" or naming.fitting(form, 48)


@pytest.mark.parametrize(
    ("title", "default"),
    [
        ("worktree", True),
        ("worktree-2", True),
        ("worktree-12", True),
        ("worktree-two", False),
        ("worktree-", False),
        ("other", False),
    ],
)
def test_a_default_is_the_worktree_s_name_or_that_one_numbered(
    tmp_path: Path, title: str, default: bool
) -> None:
    """What a launch shows and a resume keeps where nobody renamed it."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, worktree, "worktree")

    assert naming.defaulted(owned(peers, member, worktree), title) is default


def prompt_from(worktree: Path, prompt: str) -> Arrival:
    """The root session's prompt, carrying *prompt*."""
    return Arrival(session_id="root-session", cwd=str(worktree), prompt=prompt)


def test_a_prompt_due_a_name_starts_an_ask_and_holds_nothing(tmp_path: Path) -> None:
    """The step is recorded before the runtime's half starts the ask, so the next
    prompt finds it under way rather than starting another."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    arrived = prompt_from(tmp_path, "Name sessions after their work")

    step = naming.prompted(peers.root, member, arrived, declared(), "dev")
    again = naming.prompted(peers.root, member, arrived, declared(), "dev")

    assert step == naming.Step(move="ask", value="")
    assert again == naming.NOTHING
    recalled = naming.recalled(peers.root, member)
    assert recalled is not None
    assert recalled["attempts"] == 1
    assert recalled["asked"]


def test_a_prompt_that_says_nothing_asks_nothing(tmp_path: Path) -> None:
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")

    step = naming.prompted(
        peers.root, member, prompt_from(tmp_path, "  "), declared(), "dev"
    )

    assert step == naming.NOTHING


def test_the_name_an_ask_settled_is_handed_on_at_the_next_prompt(
    tmp_path: Path,
) -> None:
    """Where only the hook's answer sets a title, the ask's name waits a prompt."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    arrived = prompt_from(tmp_path, "Name sessions after their work")
    naming.prompted(peers.root, member, arrived, declared(), "dev")
    naming.settled(peers.root, member, "session-naming")
    naming.concluded(peers.root, member)

    step = naming.prompted(peers.root, member, arrived, declared(), "dev")
    after = naming.prompted(peers.root, member, arrived, declared(), "session-naming")

    assert step == naming.Step(move="push", value="session-naming")
    assert after == naming.NOTHING


def test_a_rename_in_the_chrome_wins_over_the_name_an_ask_settled(
    tmp_path: Path,
) -> None:
    """`/rename` is somebody's choice: the roster takes it up, and the ask's name
    is never handed to the chrome over it."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    arrived = prompt_from(tmp_path, "Name sessions after their work")
    naming.prompted(peers.root, member, arrived, declared(), "dev")
    naming.settled(peers.root, member, "session-naming")
    naming.concluded(peers.root, member)

    step = naming.prompted(peers.root, member, arrived, declared(), "My Own Title")
    after = naming.prompted(peers.root, member, arrived, declared(), "My Own Title")

    assert step == naming.NOTHING
    assert after == naming.NOTHING
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert current_name(found) == "my-own-title"


def test_a_rename_in_the_chrome_while_the_ask_runs_keeps_the_ask_off_the_roster(
    tmp_path: Path,
) -> None:
    """Taken up first, so the ask finishing later finds a chosen name and writes none."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    arrived = prompt_from(tmp_path, "Name sessions after their work")
    naming.prompted(peers.root, member, arrived, declared(), "dev")

    naming.prompted(peers.root, member, arrived, declared(), "Mine")

    assert naming.settled(peers.root, member, "session-naming") == ""
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert current_name(found) == "mine"


def test_a_resumed_conversation_s_title_is_taken_up_at_its_first_prompt(
    tmp_path: Path,
) -> None:
    """The launch leaves a reopened conversation its title, and the roster follows it."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")

    step = naming.prompted(
        peers.root,
        member,
        prompt_from(tmp_path, "carry on"),
        declared(),
        "fix-login-redirect",
    )

    assert step == naming.NOTHING
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert current_name(found) == "fix-login-redirect"


def test_a_resumed_session_without_a_title_is_asked_for_one(tmp_path: Path) -> None:
    """A reopened conversation still called after its worktree was never named."""
    worktree = tmp_path / "dev"
    worktree.mkdir()
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, worktree, "dev")

    step = naming.prompted(
        peers.root, member, prompt_from(worktree, "Fix the login"), declared(), "dev-3"
    )

    assert step == naming.Step(move="ask", value="")


def test_a_resume_waiting_reads_the_reopened_session_s_own_name(
    tmp_path: Path,
) -> None:
    """Where the runtime reports no title, the resume recorded at its start is read."""
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, tmp_path, "dev")
    naming.resuming(peers.root, member, "thread-9")
    arrived = prompt_from(tmp_path, "carry on")

    step = naming.prompted(peers.root, member, arrived, declared(), None)

    assert step == naming.Step(move="adopt", value="thread-9")
    assert naming.reopened(peers.root, member, "Fix expired MCP token", 48)
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert current_name(found) == "fix-expired-mcp-token"
    assert naming.concluded(peers.root, member)["resumed"] == ""
    assert naming.prompted(peers.root, member, arrived, declared(), None) == (
        naming.NOTHING
    )


def test_a_reopened_session_that_kept_no_name_is_left_to_be_asked(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "dev"
    worktree.mkdir()
    peers = RepositoryPeers(tmp_path)
    member = joined(peers, worktree, "dev")

    assert not naming.reopened(peers.root, member, "", 48)
    assert not naming.reopened(peers.root, member, "dev-2", 48)
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert current_name(found) == "dev"
