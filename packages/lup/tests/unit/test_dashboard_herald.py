"""The dashboard makes a parked review visible when no page shows it.

It runs on the host and follows every relay whether or not a tab is open, so
it is what tells the operator a review parked: a desktop notice naming what
waits and where, once per review, and — where no tab follows the page — the
page itself, reopened at most once per quiet period and never where the
person turned that off. It publishes what it counts for every session to
read without its capability, which is what a session's status line shows.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sh

from lup.coordination.bare import store
from lup.coordination.bare.changes import changes
from lup.coordination.identity import mint_member_id
from lup.coordination.mail import ActorMail
from lup.coordination.peers import USER_ADDRESS, user_peer
from lup.coordination.repository import RepositoryPeers
from lup.devtools.dashboard.companion import DashboardRegistry
from lup.devtools.dashboard.pulse import (
    DashboardPulse,
    PulseFile,
    StatusInput,
    status_line,
)
from lup.devtools.dashboard.service import DesktopNotice, Herald
from lup.devtools.review.app import relay
from lup.launch.companions import lent_directory
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion
from lup.providers.user_config import UserConfigFile
from tests.unit.reviews import bound

URL = "http://127.0.0.1:8766"
CAPABILITY = "the-capability"


class Desk:
    """The operator's desktop, recording what the dashboard did to it."""

    def __init__(self) -> None:
        self.notices: list[DesktopNotice] = []
        self.opened: list[str] = []

    def notify(self, notice: DesktopNotice) -> bool:
        self.notices.append(notice)
        return True

    def reopen(self, url: str) -> bool:
        self.opened.append(url)
        return True


def repository(root: Path) -> Path:
    sh.Command("git")("init", "-q", "-b", "main", str(root))
    return root.resolve()


def parked(
    root: Path, question_id: str, member: str = "", session: str = "native-session"
) -> PersistentQuestion:
    operation = Operation(
        id=f"operation-{question_id}",
        session=session,
        requester="asking-session",
        tool="Bash",
        payload={"command": f"touch {question_id}"},
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
                member=member,
            )
        )
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return repository(tmp_path / "project")


@pytest.fixture
def registry(tmp_path: Path, root: Path) -> Iterator[DashboardRegistry]:
    """A dashboard's registry, with one launch in ``root`` holding it."""
    held = DashboardRegistry(directory=tmp_path / "dashboard")
    with held.registered(root):
        yield held


@pytest.fixture
def desk() -> Desk:
    return Desk()


@pytest.fixture
def config(tmp_path: Path) -> UserConfigFile:
    return UserConfigFile(tmp_path / "config")


def herald(
    registry: DashboardRegistry,
    desk: Desk,
    config: UserConfigFile,
    tabs: int = 0,
    quiet: timedelta = timedelta(minutes=10),
    crowd: int = 3,
) -> Herald:
    return Herald(
        registry.directory,
        registry,
        URL,
        CAPABILITY,
        tabs=lambda: tabs,
        config=config,
        notify=desk.notify,
        reopen=desk.reopen,
        quiet=quiet,
        crowd=crowd,
    )


def test_a_parked_review_is_told_once_naming_what_waits_and_where(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    parked(root, "q-1")
    watching = herald(registry, desk, config)

    watching.look()
    watching.look()
    herald(registry, desk, config).look()

    [notice] = desk.notices
    assert notice.summary == "A review waits for you in project"
    assert "Run: touch q-1" in notice.body
    assert "asked by asking-session in project" in notice.body
    assert URL in notice.body
    assert CAPABILITY not in notice.body


def test_a_review_answered_before_anyone_looked_is_not_told(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    question = parked(root, "q-1")
    relay(root).answer(question.id, "operator", True)

    herald(registry, desk, config).look()

    assert desk.notices == [] and desk.opened == []


def test_many_parked_at_once_are_told_in_one_notice(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    for each in ("q-1", "q-2", "q-3"):
        parked(root, each)

    herald(registry, desk, config, crowd=2).look()

    [notice] = desk.notices
    assert notice.summary == "3 reviews wait for you in project"
    assert URL in notice.body


def test_with_no_tab_open_the_page_reopens_once_per_quiet_period(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    watching = herald(registry, desk, config, quiet=timedelta(minutes=10))
    start = datetime.now(UTC)

    parked(root, "q-1")
    watching.look(start)
    parked(root, "q-2")
    watching.look(start + timedelta(minutes=5))
    parked(root, "q-3")
    herald(registry, desk, config, quiet=timedelta(minutes=10)).look(
        start + timedelta(minutes=11)
    )

    assert desk.opened == [f"{URL}/#token={CAPABILITY}"] * 2
    assert len(desk.notices) == 3


def test_a_tab_already_open_leaves_the_page_alone(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    parked(root, "q-1")

    herald(registry, desk, config, tabs=1).look()

    assert desk.opened == []
    assert len(desk.notices) == 1


def test_a_person_who_turned_reopening_off_is_not_reopened_on(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    config.record({("dashboard", "reopen"): False})
    parked(root, "q-1")

    herald(registry, desk, config).look()

    assert desk.opened == []
    assert len(desk.notices) == 1


def test_the_pulse_counts_what_waits_and_says_where(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    question = parked(root, "q-1")
    watching = herald(registry, desk, config, tabs=2)
    pulse = PulseFile.of(lent_directory(registry.directory))

    watching.look()
    counted = pulse.read()
    relay(root).answer(question.id, "operator", True)
    watching.look()

    assert counted is not None
    assert counted.url == URL and counted.pending == 1
    assert counted.sessions == 1 and counted.tabs == 2
    assert counted.repositories == [str(root / ".git")]
    assert counted.address == URL
    assert status_line(pulse.path).plain() == f"● {URL}"
    watching.retired()
    assert pulse.read() is None
    assert status_line(pulse.path).plain() == "○ dashboard down · dashboard restart"


def test_the_pulse_names_the_first_declared_origin_the_operator_opens_the_page_at(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    """The page keeps its capability per origin; the line links there, never with the capability."""
    pulse = PulseFile.of(lent_directory(registry.directory))
    watching = herald(registry, desk, config)
    config.record(
        {("dashboard", "origins"): ["https://their.proxy.name", "https://other.name"]}
    )

    watching.look()

    assert published(pulse).address == "https://their.proxy.name"
    assert published(pulse).url == URL
    assert status_line(pulse.path).plain() == "● https://their.proxy.name"
    assert CAPABILITY not in pulse.path.read_text(encoding="utf-8")


def joined(root: Path, name: str, conversation: str = "") -> str:
    """One session on the repository's roster, launched in ``root``, writing ``conversation``'s transcript."""
    peers = RepositoryPeers(root)
    member = mint_member_id()
    peers.join(member, root, cli_name=name)
    if conversation:
        transcript = root.parent / "home" / f"{conversation}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()
        changes(peers.root, member, root, str(transcript))
    return member


def called(transcript: Path, call: str, at: datetime) -> None:
    """The transcript records a call that nothing has answered yet, made at ``at``."""
    transcript.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "assistant",
        "uuid": call,
        "timestamp": at.isoformat(),
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call, "name": "Bash", "input": {}}],
        },
    }
    with transcript.open("a", encoding="utf-8") as written:
        written.write(json.dumps(record) + "\n")


def published(pulse: PulseFile) -> DashboardPulse:
    counted = pulse.read()
    assert counted is not None
    return counted


def test_the_pulse_lists_each_session_with_the_reviews_it_parked(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    lead = joined(root, "lead", "conversation-1")
    other = joined(root, "other")
    parked(root, "41cb73e1a2b3c4d5", member=lead)
    parked(root, "q-2", member=other)
    parked(root, "q-3")
    pulse = PulseFile.of(lent_directory(registry.directory))

    herald(registry, desk, config).look()

    members = {member.name: member for member in published(pulse).members}
    assert members["lead"].reviews == ["41cb73e1a2b3c4d5"]
    assert members["lead"].runtime == ["conversation-1"]
    assert members["lead"].worktree == str(root)
    assert members["lead"].project == "project"
    assert members["other"].reviews == ["q-2"]
    asking = StatusInput(session_id="conversation-1")
    assert status_line(pulse.path, asking).plain() == (
        f"project · lead │ ?3 reviews (1 here: 41cb73e1) │ ● {URL}"
    )


def test_a_review_a_subagent_parked_is_its_sessions(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    lead = joined(root, "lead", "conversation-1")
    peers = RepositoryPeers(root)
    store.joined_subagent(
        peers.root, lead, store.Caller(agent_id="a1", agent_type="general-purpose")
    )
    parked(root, "q-1", session="a1")
    parked(root, "q-2", session="conversation-1")
    pulse = PulseFile.of(lent_directory(registry.directory))

    herald(registry, desk, config).look()

    [member] = published(pulse).members
    assert member.name == "lead" and member.reviews == ["q-1", "q-2"]
    asking = StatusInput(session_id="conversation-1")
    assert status_line(pulse.path, asking).plain() == (
        f"project · lead │ ?2 reviews (2 here) │ ● {URL}"
    )


def test_a_call_silent_ten_minutes_is_quiet_unless_a_subagent_of_its_runs(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    lead = joined(root, "lead", "conversation-1")
    transcript = root.parent / "home" / "conversation-1.jsonl"
    began = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    called(transcript, "t1", began)
    pulse = PulseFile.of(lent_directory(registry.directory))
    watching = herald(registry, desk, config)

    watching.look(began + timedelta(minutes=9))
    early = published(pulse).quiet
    watching.look(began + timedelta(minutes=11))
    quiet = published(pulse).quiet
    line = status_line(pulse.path, now=began + timedelta(minutes=11)).plain()
    peers = RepositoryPeers(root)
    store.joined_subagent(
        peers.root, lead, store.Caller(agent_id="a1", agent_type="general-purpose")
    )
    watching.look(began + timedelta(minutes=12))
    waiting = published(pulse).quiet
    called(transcript.with_suffix("") / "subagents" / "agent-a1.jsonl", "s1", began)
    watching.look(began + timedelta(minutes=13))
    stuck = published(pulse).quiet

    assert (early, quiet) == (0, 1)
    assert line == f"⚠ 1 quiet │ ● {URL}"
    assert waiting == 0, "a session whose subagent runs waits on it"
    assert stuck == 1, "the subagent is the one gone quiet, not its session"


def test_a_path_two_sessions_hold_is_held_twice(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    peers = RepositoryPeers(root)
    lead = joined(root, "lead")
    other = joined(root, "other")
    child = store.joined_subagent(
        peers.root, lead, store.Caller(agent_id="a1", agent_type="general-purpose")
    )
    assert child is not None
    shared = root / "shared.py"
    kept = root / "kept.py"
    for claimed in (shared, kept):
        claimed.write_text("value = 1\n", encoding="utf-8")
    peers.touched(lead, shared, kept)
    peers.touched(other, shared)
    peers.touched(store.actor_id(child), kept)
    pulse = PulseFile.of(lent_directory(registry.directory))

    herald(registry, desk, config).look()

    assert published(pulse).contested == 1, "a session and its subagent hold as one"


def test_messages_agents_sent_the_operator_are_counted_while_they_wait(
    registry: DashboardRegistry, desk: Desk, config: UserConfigFile, root: Path
) -> None:
    peers = RepositoryPeers(root)
    lead = joined(root, "lead")
    peers.send(USER_ADDRESS, "the build is green", sender=lead)
    peers.send(USER_ADDRESS, "and the docs are regenerated", sender=lead)
    pulse = PulseFile.of(lent_directory(registry.directory))
    watching = herald(registry, desk, config)

    watching.look()
    waiting = published(pulse).unread
    mail = ActorMail(peers.root)
    mail.delivered(user_peer(), mail.waiting(user_peer()))
    watching.look()

    assert waiting == 2
    assert published(pulse).unread == 0
