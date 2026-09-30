"""A second launch into a repository's volume does not undo a session still running there.

Every launch seeds the volume with the person's settings, and a volume is
shared by every session opened in its repository. A seed that replaced the
files would erase whatever an earlier session, still open, had changed since
its own launch — before that session could carry it back. These pin the
three-way merge against the last launch's seed, for both runtimes: a
session's own change survives another launch and returns when that session
closes, a change only the person made still arrives, and where both changed
the same setting the person's wins and is named.
"""

import fcntl
import json
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import pytest

from lup.launch.config_volume import (
    HomeFile,
    HomeHelper,
    HomeSeedPlaces,
    settle_home_seed,
)
from lup.harness.assets.home_seed import (
    ABSENT,
    LOCK,
    Absent,
    Seeded,
    apply,
    read_tree,
    three_way,
)
from lup.harness.assets.trust_seed import record_trust
from lup.harness.image import Podman
from lup.providers.claude.config_home import ClaudeConfigHome
from lup.providers.claude.home_seed import ClaudeHomeReturn, ClaudeHomeSeed
from lup.providers.codex.home import seeded_codex_settings
from lup.providers.user_config import UserConfig, UserConfigFile
from lup.types import JsonObject


def side(value: Seeded) -> Seeded:
    """A settings object holding ``theme`` as that value, or lacking it."""
    return {} if isinstance(value, Absent) else {"theme": value}


@pytest.mark.parametrize(
    ("base", "ours", "theirs", "merged", "conflicts"),
    [
        ("dark", "light", "dark", "light", []),
        ("dark", "dark", "ansi", "ansi", []),
        ("dark", "light", "ansi", "ansi", ["theme"]),
        ("dark", "light", "light", "light", []),
        ("dark", ABSENT, "dark", ABSENT, []),
        (ABSENT, "light", ABSENT, "light", []),
    ],
    ids=[
        "session-only",
        "source-only",
        "both-changed",
        "both-agree",
        "session-removed",
        "session-added",
    ],
)
def test_each_setting_merges_three_ways(
    base: Seeded, ours: Seeded, theirs: Seeded, merged: Seeded, conflicts: list[str]
) -> None:
    result = three_way(side(base), side(ours), side(theirs))

    assert result.value == side(merged)
    assert result.conflicts == conflicts


class Volume:
    """A config volume as a directory, read the way the launcher's helper reads one."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir()

    def read(self, helper: HomeHelper, volume: str, names: list[str]) -> list[HomeFile]:
        return [
            HomeFile(name=held.name, content=(held.text or "").encode())
            for held in read_tree(self.directory)
            if held.name in names or Path(held.name).parts[0] in names
        ]

    def settings(self) -> JsonObject:
        return json.loads((self.directory / "settings.json").read_text())

    def session_sets(self, key: str, value: str) -> None:
        settings = self.settings()
        settings[key] = value
        (self.directory / "settings.json").write_text(json.dumps(settings))


class Launch:
    """One contained Claude launch: its seed settled, applied, and its session's return."""

    def __init__(
        self,
        tmp_path: Path,
        name: str,
        volume: Volume,
        account: ClaudeConfigHome,
        personal: UserConfig,
    ) -> None:
        seed = ClaudeHomeSeed.compose(account, personal)
        self.places = HomeSeedPlaces(
            seed=seed.write(tmp_path / name / "seed"),
            applied=tmp_path / name / "applied",
        )
        helper = HomeHelper(
            engine=Podman(), tag="lup-agent:x", uid=1, gid=1, config_home="/cfg"
        )
        self.said = settle_home_seed(helper, "lup-claude-x", self.places)
        apply(self.places.seed, volume.directory)
        self.volume = volume

    def closes(self, personal: UserConfig) -> ClaudeHomeReturn:
        document = self.volume.directory / ".claude.json"
        return ClaudeHomeReturn.between(
            ClaudeHomeSeed.applied(self.places.applied),
            self.volume.settings(),
            json.loads(document.read_text()) if document.is_file() else {},
            None,
            personal,
        )


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Volume:
    held = Volume(tmp_path / "volume")
    monkeypatch.setattr(
        HomeHelper, "read", lambda self, v, names: held.read(self, v, names)
    )
    return held


@pytest.fixture
def account(tmp_path: Path) -> ClaudeConfigHome:
    directory = tmp_path / "account"
    directory.mkdir()
    (directory / "settings.json").write_text(
        json.dumps({"theme": "dark", "verbose": False})
    )
    return ClaudeConfigHome(directory=directory, document=tmp_path / "doc.json")


def test_a_second_launch_keeps_a_running_sessions_theme_and_it_returns_at_its_close(
    tmp_path: Path, volume: Volume, account: ClaudeConfigHome
) -> None:
    personal = UserConfig()
    first = Launch(tmp_path, "first", volume, account, personal)
    volume.session_sets("theme", "light")

    second = Launch(tmp_path, "second", volume, account, personal)

    assert volume.settings()["theme"] == "light"
    assert second.said == []
    config = UserConfigFile(tmp_path / "lup")
    first_back = first.closes(personal)
    first_back.apply(account, config)
    assert first_back.portable == {("theme", "claude"): "light"}
    assert config.load().theme.claude == "light"
    assert second.closes(personal).carried() == []


def test_where_both_changed_a_setting_the_persons_wins_and_is_named(
    tmp_path: Path, volume: Volume, account: ClaudeConfigHome
) -> None:
    Launch(tmp_path, "first", volume, account, UserConfig())
    volume.session_sets("theme", "light")
    volume.session_sets("verbose", "session-only")
    changed = UserConfig.model_validate({"theme": {"claude": "dark-ansi"}})

    second = Launch(tmp_path, "second", volume, account, changed)

    assert volume.settings()["theme"] == "dark-ansi"
    assert volume.settings()["verbose"] == "session-only"
    assert len(second.said) == 1
    assert "settings.json theme" in second.said[0].text


def test_a_setting_only_the_person_changed_still_arrives(
    tmp_path: Path, volume: Volume, account: ClaudeConfigHome
) -> None:
    Launch(tmp_path, "first", volume, account, UserConfig())
    (account.directory / "settings.json").write_text(
        json.dumps({"theme": "dark", "verbose": True})
    )

    second = Launch(tmp_path, "second", volume, account, UserConfig())

    assert volume.settings()["verbose"] is True
    assert second.said == []


def test_a_contained_codex_home_keeps_a_running_sessions_theme(tmp_path: Path) -> None:
    installed: JsonObject = {"tui": {"theme": "zenburn", "animations": True}}
    running = '[tui]\ntheme = "dracula"\nanimations = true\n\n[projects."/x"]\ntrust_level = "trusted"\n'

    kept = seeded_codex_settings(running, json.dumps(installed), installed)
    overridden = seeded_codex_settings(
        running,
        json.dumps(installed),
        {"tui": {"theme": "monokai", "animations": True}},
    )
    first = seeded_codex_settings(running, None, installed)

    assert kept.settings == {"tui": {"theme": "dracula", "animations": True}}
    assert kept.conflicts == []
    assert overridden.settings["tui"] == {"theme": "monokai", "animations": True}
    assert overridden.conflicts == ["tui.theme"]
    assert first.settings == installed and first.conflicts == []


class StartingHome:
    """A config home both start programs amend, with the seeds each reads.

    The trust program's seed file, and a settings seed that owns
    ``settings.json`` whole and ``autoUpdates`` of ``.claude.json``.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "config"
        self.home.mkdir()
        self.document = self.home / ".claude.json"
        self.trust_seed = tmp_path / "trust-seed.json"
        self.trust_seed.write_text(json.dumps({"projects": {}}), encoding="utf-8")
        self.seed = tmp_path / "seed"
        (self.seed / "replace").mkdir(parents=True)
        (self.seed / "merge").mkdir()
        (self.seed / "managed").write_text("settings.json\n", encoding="utf-8")
        (self.seed / "replace" / "settings.json").write_text(
            json.dumps({"theme": "dark"}), encoding="utf-8"
        )
        (self.seed / "merge" / ".claude.json").write_text(
            json.dumps({"autoUpdates": False}), encoding="utf-8"
        )

    def trusts(self, checkout: str) -> bool:
        return record_trust(self.document, self.trust_seed, [checkout])

    def seeds(self) -> list[str]:
        return apply(self.seed, self.home)

    def held(self) -> list[tuple[int, int]]:
        """Each file the runtime reads, by the inode and the time it was written."""
        return [
            (held.st_ino, held.st_mtime_ns)
            for held in (
                (self.home / name).stat() for name in (".claude.json", "settings.json")
            )
        ]


@pytest.fixture
def starting(tmp_path: Path) -> StartingHome:
    return StartingHome(tmp_path)


def test_a_start_whose_seed_changes_nothing_writes_nothing(
    starting: StartingHome,
) -> None:
    """Nearly every start, and the one that would race a running session's save.

    The session saves ``.claude.json`` holding no lock of ours, so a start
    rewriting it unchanged would drop whatever the session saved between that
    start's read and its rename.
    """
    starting.seeds()
    saved = json.loads(starting.document.read_text(encoding="utf-8"))
    starting.document.write_text(json.dumps({**saved, "numStartups": 3}))
    before = starting.held()

    assert starting.seeds() == []

    assert starting.held() == before


def test_both_start_programs_wait_on_the_one_lock(starting: StartingHome) -> None:
    """The trust program names its lock itself; the settings seed must name the same."""
    # The pool opened first so it is left last: a failed assertion closes the
    # lock before the pool waits on the programs held behind it.
    with (
        ThreadPoolExecutor(max_workers=2) as pool,
        (starting.home / LOCK).open("a") as lock,
    ):
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = [pool.submit(starting.trusts, "/w"), pool.submit(starting.seeds)]
        waited = wait(started, timeout=0.5)
        assert waited.done == set()
        assert not starting.document.exists()
        fcntl.flock(lock, fcntl.LOCK_UN)
        assert [start.result(timeout=10) for start in started] == [True, []]


def test_containers_starting_at_once_neither_tear_nor_drop_the_trust_document(
    starting: StartingHome,
) -> None:
    """Every container on a home records trust and applies the seed, several at once.

    Both programs read ``.claude.json``, merge into it and write it back. A
    staging name they shared was measured publishing a document whose front
    was NUL bytes, and a merge interleaved with the other's drops what that
    one added -- so each holds the lock the other takes, and stages through a
    file of its own. A runtime starting meanwhile reads whichever whole
    document was last renamed into place, never one being written.
    """
    cached: JsonObject = {
        f"/cached/{number}": {"lastCost": number} for number in range(2000)
    }
    starting.document.write_text(json.dumps({"projects": cached}), encoding="utf-8")
    checkouts = [f"/w/tree/{number}" for number in range(16)]
    finished = threading.Event()

    def torn_reads() -> list[str]:
        torn: list[str] = []
        for _ in iter(finished.is_set, True):
            try:
                json.loads(starting.document.read_text(encoding="utf-8"))
            except ValueError as error:
                torn.append(str(error))
        return torn

    with ThreadPoolExecutor(max_workers=2 * len(checkouts) + 1) as pool:
        reading = pool.submit(torn_reads)
        started = [
            *(pool.submit(starting.trusts, checkout) for checkout in checkouts),
            *(pool.submit(starting.seeds) for _ in checkouts),
        ]
        try:
            for start in started:
                start.result()
        finally:
            finished.set()

    assert reading.result() == []
    content = json.loads(starting.document.read_text(encoding="utf-8"))
    assert content["autoUpdates"] is False
    assert len(content["projects"]) == len(cached) + len(checkouts)
    assert all(
        content["projects"][checkout]["hasTrustDialogAccepted"] is True
        for checkout in checkouts
    )
    assert sorted(path.name for path in starting.home.iterdir()) == [
        ".claude.json",
        ".lup-seed",
        ".lup-trust.lock",
        "settings.json",
    ]
