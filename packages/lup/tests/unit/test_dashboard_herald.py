"""The dashboard makes a parked review visible when no page shows it.

It runs on the host and follows every relay whether or not a tab is open, so
it is what tells the operator a review parked: a desktop notice naming what
waits and where, once per review, and — where no tab follows the page — the
page itself, reopened at most once per quiet period and never where the
person turned that off. It publishes what it counts for every session to
read without its capability, which is what a session's status line shows.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sh

from lup.devtools.dashboard.companion import DashboardRegistry
from lup.devtools.dashboard.pulse import DashboardPulse, PulseFile, status_line
from lup.devtools.dashboard.service import DesktopNotice, Herald
from lup.devtools.review.app import relay
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


def parked(root: Path, question_id: str) -> PersistentQuestion:
    operation = Operation(
        id=f"operation-{question_id}",
        session="native-session",
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
    pulse = PulseFile.of(registry.directory)

    watching.look()
    counted = pulse.read()
    relay(root).answer(question.id, "operator", True)
    watching.look()

    assert counted is not None
    assert counted.url == URL and counted.pending == 1
    assert counted.sessions == 1 and counted.tabs == 2
    assert counted.repositories == [str(root / ".git")]
    assert status_line(pulse.path) == URL
    watching.retired()
    assert pulse.read() is None and status_line(pulse.path) == ""


def test_the_status_line_says_what_waits_and_where(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    pulse = DashboardPulse(url=URL, pid=1, pending=3, beat=now)
    kept = PulseFile.of(tmp_path)
    kept.path.parent.mkdir(parents=True)
    kept.path.write_text(pulse.model_dump_json())

    assert status_line(kept.path, now) == f"3 reviews pending · {URL}"
    assert pulse.model_copy(update={"pending": 1}).line() == f"1 review pending · {URL}"
    assert status_line(kept.path, now + timedelta(minutes=5)) == ""
