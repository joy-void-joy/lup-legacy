"""One configuration-home volume per repository and runtime, and the move into them.

An unsplit shared volume holds both CLIs' files, so a split that guessed would
hand one runtime the other's login or history, and a split that ran twice
could overwrite what a session wrote since. These pin the ownership table,
which unsplit volumes a repository answers for, the move itself against an
engine kept in memory — its idempotence, its refusal to touch a volume an
open session holds, and what it says about entries nobody declares.

Every session running on a volume shares the login it was handed, so these
also pin the handoff: another account's login under running sessions is
refused with their count and the ways through, handed anyway when a launch
asks to move them, withheld by a probe, and handed as always where nothing
runs, nothing was recorded, or the account is the one the volume holds.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import sh

import lup.launch.config_volume as config_volume
import lup.launch.container as container
from lup.launch.config_volume import (
    HandedLogin,
    LaunchedAccount,
    LaunchedAccounts,
    LoginOwner,
    SessionsMove,
    VolumeLogin,
    VolumeLogins,
    kept_for_superseded,
    settle_handoff,
    sweep_superseded,
    swept_superseded_notice,
    HomeHelper,
    HomeSplit,
    RuntimeVolume,
    UnsplitVolumes,
    split_config_volumes,
)
from lup.launch.refusal import LaunchRefused
from lup.harness.image import Image, Podman
from lup.harness.requirements import Manifest
from lup.providers.login import ProviderLogin
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN
from lup.launch.superseded import SupersededFile
from lup.providers.user_config import UserConfigFile

LOGINS = [CLAUDE_LOGIN, CODEX_LOGIN]


class MemoryEngine:
    """A container engine whose volumes are dictionaries of top-level entries."""

    def __init__(self, volumes: dict[str, dict[str, str]]) -> None:
        self.volumes = volumes
        self.attached: dict[str, list[str]] = {}
        self.running: dict[str, list[str]] = {}
        self.refuse_removal: list[str] = []
        self.fail_copies = 0
        self.calls: list[list[str]] = []

    def __call__(self, *arguments: str) -> str:
        words = list(arguments)
        self.calls.append(words)
        match words:
            case ["volume", "ls", *_]:
                return "\n".join(self.volumes)
            case ["ps", "-a", "--filter", filtered, *_]:
                return "\n".join(
                    self.attached.get(filtered.removeprefix("volume="), [])
                )
            case ["ps", "--filter", filtered, "--format", "{{.Names}}"]:
                return "\n".join(self.running.get(filtered.removeprefix("volume="), []))
            case ["volume", "rm", name]:
                if name in self.refuse_removal:
                    raise sh.ErrorReturnCode_1(" ".join(words), b"", b"in use")
                self.volumes.pop(name)
                return ""
            case ["run", *_]:
                return self.run(words)
        raise AssertionError(f"unexpected engine call {words}")

    def run(self, words: list[str]) -> str:
        program = words[words.index("--entrypoint") + 1]
        mounts = [words[index + 1] for index, word in enumerate(words) if word == "-v"]
        source = next(mount.split(":")[0] for mount in mounts if mount.endswith(":ro"))
        match program:
            case "ls":
                return "\n".join(self.volumes.get(source, {}))
            case "cp":
                if self.fail_copies:
                    self.fail_copies -= 1
                    raise sh.ErrorReturnCode_1("cp", b"", b"no space")
                target = next(m.split(":")[0] for m in mounts if not m.endswith(":ro"))
                held = self.volumes.setdefault(target, {})
                for word in words:
                    if word.startswith(f"{config_volume.SPLIT_SOURCE}/"):
                        entry = word.removeprefix(f"{config_volume.SPLIT_SOURCE}/")
                        held.setdefault(entry, self.volumes[source][entry])
                return ""
        raise AssertionError(f"unexpected helper program {program}")


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A checkout whose repository is called `lup`, with one other worktree."""
    layout = Mock()
    layout.name.return_value = "lup"
    monkeypatch.setattr(config_volume, "repository_layout", Mock(return_value=layout))
    monkeypatch.setattr(
        config_volume, "sibling_worktrees", lambda root: [tmp_path / "feat-x"]
    )
    root = tmp_path / "dev"
    root.mkdir()
    return root


def test_each_entry_goes_to_the_runtimes_that_declare_it() -> None:
    split = HomeSplit.of(
        [
            ".claude.json",
            ".claude.json.tmp.340696.9544bf52c7c9",
            "auth.json",
            "state_5.sqlite-wal",
            "history.jsonl",
            "mystery",
        ],
        LOGINS,
    )

    assert split.owned == {
        "claude": [".claude.json", "history.jsonl", "mystery"],
        "codex": ["auth.json", "state_5.sqlite-wal", "history.jsonl", "mystery"],
    }
    assert split.unknown == ["mystery"]
    assert split.debris == [".claude.json.tmp.340696.9544bf52c7c9"]


def test_a_repository_answers_for_its_shared_digest_and_worktree_volumes(
    repository: Path,
) -> None:
    found = UnsplitVolumes.found(
        repository,
        [
            "lup-cfg-lup",
            "lup-cfg-lup-codex-abc",
            "lup-cfg-dev",
            "lup-cfg-feat-x",
            "lup-cfg-other",
            "lup-claude-lup",
        ],
        LOGINS,
        ["dev", "feat-x"],
    )

    assert found.shared == "lup-cfg-lup"
    assert found.scoped == {"claude": [], "codex": ["lup-cfg-lup-codex-abc"]}
    assert found.branches == ["lup-cfg-dev", "lup-cfg-feat-x"]
    assert "lup-cfg-other" not in found.every()


def test_the_helper_runs_as_the_session_without_its_entrypoint_or_network() -> None:
    helper = HomeHelper(
        engine=Podman(), tag="lup-agent:dev", uid=1000, gid=1001, config_home="/cfg"
    )

    argv = helper.argv("ls", ["v:/lup-split-from:ro"], ["-A", "/lup-split-from"])

    assert argv[:5] == ["podman", "run", "--rm", "--network", "none"]
    assert "--userns=keep-id" in argv and "1000:1001" in argv
    assert argv[argv.index("--entrypoint") + 1] == "ls"
    assert argv[-3:] == ["lup-agent:dev", "-A", "/lup-split-from"]


NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
KEPT = timedelta(days=14)


@pytest.fixture
def record(tmp_path: Path) -> SupersededFile:
    """Where this test's superseded volumes are recorded."""
    return SupersededFile(tmp_path / "state")


def moved(
    root: Path,
    engine: MemoryEngine,
    monkeypatch: pytest.MonkeyPatch,
    record: SupersededFile,
    now: datetime = NOW,
) -> str:
    """Run one split against the engine in memory, answering what it said."""
    monkeypatch.setattr(sh, "Command", lambda binary: engine)
    helper = HomeHelper(
        engine=Podman(), tag="lup-agent:dev", uid=1000, gid=1000, config_home="/cfg"
    )
    said = split_config_volumes(
        root,
        helper,
        [
            RuntimeVolume(login=CLAUDE_LOGIN, volume="lup-claude-lup"),
            RuntimeVolume(login=CODEX_LOGIN, volume="lup-codex-lup"),
        ],
        record,
        KEPT,
        now,
    )
    return "\n".join(notice.text for notice in said)


def test_the_split_copies_each_runtimes_files_and_keeps_the_unsplit_volumes(
    repository: Path, monkeypatch: pytest.MonkeyPatch, record: SupersededFile
) -> None:
    engine = MemoryEngine(
        {
            "lup-cfg-lup": {
                ".claude.json": "claude document",
                ".claude.json.tmp.1.a": "half a write",
                "auth.json": "codex login (old)",
                "history.jsonl": "both",
                "mystery": "?",
            },
            "lup-cfg-lup-codex-abc": {
                "auth.json": "codex login",
                ".claude.json": "entrypoint seed",
            },
            "lup-cfg-feat-x": {".claude.json": "per worktree"},
        }
    )

    said = moved(repository, engine, monkeypatch, record)

    assert engine.volumes["lup-claude-lup"] == {
        ".claude.json": "claude document",
        "history.jsonl": "both",
        "mystery": "?",
    }
    assert engine.volumes["lup-codex-lup"] == {
        "auth.json": "codex login (old)",
        "history.jsonl": "both",
        "mystery": "?",
        ".claude.json": "entrypoint seed",
    }
    assert {"lup-cfg-lup", "lup-cfg-lup-codex-abc", "lup-cfg-feat-x"} <= set(
        engine.volumes
    )
    assert record.load().names() == [
        "lup-cfg-lup",
        "lup-cfg-lup-codex-abc",
        "lup-cfg-feat-x",
    ]
    assert "now lives in lup-claude-lup, lup-codex-lup" in said
    assert "until 2026-10-09" in said
    assert "no runtime declares mystery" in said
    assert ".claude.json.tmp.1.a" in said


def test_a_second_split_finds_nothing_left_to_copy(
    repository: Path, monkeypatch: pytest.MonkeyPatch, record: SupersededFile
) -> None:
    engine = MemoryEngine({"lup-cfg-lup": {"auth.json": "codex login"}})
    moved(repository, engine, monkeypatch, record)
    engine.calls.clear()

    assert moved(repository, engine, monkeypatch, record) == ""
    assert [call for call in engine.calls if call[0] == "run"] == []


def test_an_interrupted_split_finishes_without_overwriting_what_came_since(
    repository: Path, monkeypatch: pytest.MonkeyPatch, record: SupersededFile
) -> None:
    engine = MemoryEngine(
        {
            "lup-cfg-lup": {".claude.json": "old", "projects": "transcripts"},
            "lup-claude-lup": {".claude.json": "written since"},
        }
    )
    engine.fail_copies = 1

    first = moved(repository, engine, monkeypatch, record)
    assert "the next launch tries again" in first
    assert record.load().names() == []
    moved(repository, engine, monkeypatch, record)

    assert engine.volumes["lup-claude-lup"] == {
        ".claude.json": "written since",
        "projects": "transcripts",
    }
    assert record.load().names() == ["lup-cfg-lup"]


def test_a_volume_an_open_session_holds_postpones_the_whole_split(
    repository: Path, monkeypatch: pytest.MonkeyPatch, record: SupersededFile
) -> None:
    engine = MemoryEngine({"lup-cfg-lup": {"auth.json": "codex login"}})
    engine.attached = {"lup-cfg-lup": ["lup-dev-session"]}

    said = moved(repository, engine, monkeypatch, record)

    assert "waits for the first launch after it closes" in said
    assert "lup-dev-session" in said
    assert list(engine.volumes) == ["lup-cfg-lup"]
    assert record.load().names() == []


def superseded_after_a_split(
    repository: Path, monkeypatch: pytest.MonkeyPatch, record: SupersededFile
) -> MemoryEngine:
    """An engine whose unsplit shared volume a split superseded at :data:`NOW`."""
    engine = MemoryEngine({"lup-cfg-lup": {"auth.json": "codex login"}})
    moved(repository, engine, monkeypatch, record)
    return engine


def test_a_superseded_volume_stays_until_its_days_have_passed(
    repository: Path, monkeypatch: pytest.MonkeyPatch, record: SupersededFile
) -> None:
    engine = superseded_after_a_split(repository, monkeypatch, record)

    early = sweep_superseded(Podman(), record, KEPT, NOW + timedelta(days=13))
    due = sweep_superseded(Podman(), record, KEPT, NOW + timedelta(days=14))

    assert early == []
    assert due == ["lup-cfg-lup"]
    assert "lup-cfg-lup" not in engine.volumes
    assert record.load().names() == []
    assert "lup-cfg-lup" in swept_superseded_notice(due, KEPT)[0].text


def test_clean_yes_removes_a_superseded_volume_before_its_days(
    repository: Path, monkeypatch: pytest.MonkeyPatch, record: SupersededFile
) -> None:
    engine = superseded_after_a_split(repository, monkeypatch, record)

    assert sweep_superseded(Podman(), record, KEPT, NOW, early=True) == ["lup-cfg-lup"]
    assert "lup-cfg-lup" not in engine.volumes


def test_a_superseded_volume_a_container_holds_is_never_removed(
    repository: Path, monkeypatch: pytest.MonkeyPatch, record: SupersededFile
) -> None:
    engine = superseded_after_a_split(repository, monkeypatch, record)
    engine.attached = {"lup-cfg-lup": ["an-old-session"]}

    late = NOW + timedelta(days=90)
    assert sweep_superseded(Podman(), record, KEPT, late) == []
    assert sweep_superseded(Podman(), record, KEPT, late, early=True) == []
    assert "lup-cfg-lup" in engine.volumes
    assert record.load().names() == ["lup-cfg-lup"]


def test_the_days_a_superseded_volume_is_kept_are_the_persons_to_set(
    tmp_path: Path,
) -> None:
    config = UserConfigFile(tmp_path / "lup")
    assert kept_for_superseded(config) == timedelta(days=14)

    config.record({("cleanup", "superseded_volumes_after_days"): 3})

    assert kept_for_superseded(config) == timedelta(days=3)


VOLUME = "lup-claude-lup"


def account(tmp_path: Path, name: str) -> LoginOwner:
    """A profile's Claude home, signed in with a login of its own."""
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    CLAUDE_LOGIN.credentials_path(home).write_text(
        json.dumps({"claudeAiOauth": {"refreshToken": name}}), encoding="utf-8"
    )
    return LoginOwner(home=home, profile=name)


def offered(tmp_path: Path, name: str, moving: SessionsMove = "refuse") -> HandedLogin:
    """That profile's login, offered to the volume a launch opens on."""
    owner = account(tmp_path, name)
    return HandedLogin(
        credential=CLAUDE_LOGIN.credentials_path(owner.home),
        owner=owner,
        moving=moving,
    )


@pytest.fixture
def logins(tmp_path: Path) -> VolumeLogins:
    """Where this test's volume records are kept."""
    return VolumeLogins(tmp_path / "volume-logins")


def held_by(
    tmp_path: Path, logins: VolumeLogins, name: str, volume: str = VOLUME
) -> None:
    """Record ``name``'s login as the one the volume was last handed."""
    logins.record(
        VolumeLogin(
            volume=volume,
            runtime="claude",
            owner=account(tmp_path, name),
            handed_at=NOW,
        )
    )


def handed_over(
    handed: HandedLogin,
    logins: VolumeLogins,
    engine: MemoryEngine,
    monkeypatch: pytest.MonkeyPatch,
    login: ProviderLogin = CLAUDE_LOGIN,
    volume: str = VOLUME,
) -> config_volume.Handoff:
    """Settle one start's handoff against the engine in memory."""
    monkeypatch.setattr(sh, "Command", lambda binary: engine)
    return settle_handoff(handed, login, volume, Podman(), logins, NOW)


def test_a_volume_nothing_was_recorded_for_takes_the_login_and_records_it(
    tmp_path: Path, logins: VolumeLogins, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing recorded there is nothing to compare, so it hands as always."""
    engine = MemoryEngine({})
    engine.running = {VOLUME: ["a-session"]}
    handed = offered(tmp_path, "work")

    handoff = handed_over(handed, logins, engine, monkeypatch)

    assert handoff.credential == handed.credential
    assert handoff.notices == []
    assert handoff.record is not None
    assert handoff.record.owner == handed.owner
    assert handoff.record.fingerprint == handed.fingerprint(CLAUDE_LOGIN) != ""
    assert logins.held(VOLUME) is None, "recorded only once the argv stands"


def test_another_accounts_login_under_running_sessions_is_refused_with_their_count(
    tmp_path: Path, logins: VolumeLogins, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_by(tmp_path, logins, "personal")
    engine = MemoryEngine({})
    engine.running = {VOLUME: ["first", "second"]}

    with pytest.raises(LaunchRefused) as refused:
        handed_over(offered(tmp_path, "work"), logins, engine, monkeypatch)

    said = str(refused.value)
    assert said.startswith(
        "refused: `work` — 2 running contained claude sessions of this "
        "repository use personal's login"
    )
    assert "moves each onto work's at its next request" in said
    assert "pass `--move-sessions`" in said
    assert "`uv run lup-devtools harness profile switch work --runtime claude`" in said
    assert "pass `--profile personal`" in said
    engine.running = {VOLUME: ["only"]}
    with pytest.raises(LaunchRefused, match="1 running contained claude session of"):
        handed_over(offered(tmp_path, "work"), logins, engine, monkeypatch)


def test_a_runtime_keeping_its_login_is_told_its_sessions_keep_theirs(
    tmp_path: Path, logins: VolumeLogins, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex reloads only its own account, so nothing moves until a relaunch."""
    held_by(tmp_path, logins, "personal", volume="lup-codex-lup")
    engine = MemoryEngine({})
    engine.running = {"lup-codex-lup": ["one"]}

    with pytest.raises(LaunchRefused) as refused:
        handed_over(
            offered(tmp_path, "work"),
            logins,
            engine,
            monkeypatch,
            login=CODEX_LOGIN,
            volume="lup-codex-lup",
        )

    said = str(refused.value)
    assert "1 running contained codex session of this repository" in said
    assert "each keeps personal's until it is opened again, and opens on work's" in said
    assert "--runtime codex" in said


def test_move_sessions_hands_the_login_anyway_and_says_whom_it_moved(
    tmp_path: Path, logins: VolumeLogins, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_by(tmp_path, logins, "personal")
    engine = MemoryEngine({})
    engine.running = {VOLUME: ["first", "second"]}
    handed = offered(tmp_path, "work", moving="move")

    handoff = handed_over(handed, logins, engine, monkeypatch)

    assert handoff.credential == handed.credential
    assert handoff.record is not None and handoff.record.owner.profile == "work"
    [moved] = handoff.notices
    assert "now holds work's, in place of personal's" in moved.text
    assert "2 running session(s)" in moved.text
    assert "each takes it at its next request" in moved.text


def test_another_accounts_login_with_nothing_running_is_handed_without_asking(
    tmp_path: Path, logins: VolumeLogins, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_by(tmp_path, logins, "personal")
    handed = offered(tmp_path, "work")

    handoff = handed_over(handed, logins, MemoryEngine({}), monkeypatch)

    assert handoff.credential == handed.credential
    assert handoff.record is not None and handoff.notices == []


def test_the_account_the_volume_holds_is_handed_whatever_runs(
    tmp_path: Path, logins: VolumeLogins, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renewed login of the same account moves nobody, so nothing is asked."""
    held_by(tmp_path, logins, "work")
    engine = MemoryEngine({})
    engine.running = {VOLUME: ["a-session"]}
    handed = offered(tmp_path, "work")
    renamed = handed.model_copy(
        update={
            "owner": LoginOwner(home=handed.owner.home / ".." / "work", profile=None)
        }
    )

    for same in (handed, renamed):
        handoff = handed_over(same, logins, engine, monkeypatch)
        assert handoff.credential == handed.credential
        assert handoff.notices == []
    assert engine.calls == [], "the engine is not asked about the same account"


def test_a_probe_keeps_the_login_running_sessions_use(
    tmp_path: Path, logins: VolumeLogins, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_by(tmp_path, logins, "personal")
    engine = MemoryEngine({})
    engine.running = {VOLUME: ["a-session"]}

    handoff = handed_over(
        offered(tmp_path, "work", moving="keep"), logins, engine, monkeypatch
    )

    assert handoff.credential is None and handoff.record is None
    [kept] = handoff.notices
    assert "keeps personal's" in kept.text and "rather than handing work's" in kept.text


def test_what_a_volume_was_handed_reads_back_as_it_was_recorded(
    tmp_path: Path, logins: VolumeLogins
) -> None:
    held_by(tmp_path, logins, "work")

    held = logins.held(VOLUME)

    assert held is not None
    assert held.owner == account(tmp_path, "work")
    assert held.owner.named() == "work"
    assert LoginOwner(home=tmp_path / "x").named() == f"the account at {tmp_path / 'x'}"
    assert logins.held("lup-claude-other") is None


def launched(
    tmp_path: Path, member: str, runtime: str, contained: bool
) -> LaunchedAccount:
    """A launch of one runtime on ``personal``, contained or on the host."""
    return LaunchedAccount(
        member=member,
        runtime=runtime,
        owner=account(tmp_path, "personal"),
        checkout=tmp_path / "dev",
        contained=contained,
        at=NOW,
    )


def test_a_session_draws_on_what_its_volume_holds_only_where_it_rereads_its_login(
    tmp_path: Path, logins: VolumeLogins, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contained Claude session follows its volume; Codex and host sessions do not."""
    layout = Mock()
    layout.name.return_value = "lup"
    monkeypatch.setattr(container, "repository_layout", Mock(return_value=layout))
    accounts = LaunchedAccounts(tmp_path / "launched")
    for recorded in (
        launched(tmp_path, "inside", "claude", contained=True),
        launched(tmp_path, "codex", "codex", contained=True),
        launched(tmp_path, "host", "claude", contained=False),
    ):
        accounts.record(recorded)
    held_by(tmp_path, logins, "work")
    held_by(tmp_path, logins, "work", volume="lup-codex-lup")

    def drawn(member: str) -> str | None:
        owner = container.drawn_account(member, accounts, logins)
        return owner.profile if owner is not None else None

    assert [drawn(member) for member in ("inside", "codex", "host", "gone")] == [
        "work",
        "personal",
        "personal",
        None,
    ]
    assert sorted(each.member for each in accounts.every()) == [
        "codex",
        "host",
        "inside",
    ]
    accounts.forget("codex")
    assert accounts.launched("codex") is None


def test_a_launch_that_would_move_running_sessions_is_refused_before_anything_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused while nothing it started needs undoing: no lease, no image, no proxy."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    layout = Mock()
    layout.name.return_value = "lup"
    monkeypatch.setattr(container, "repository_layout", Mock(return_value=layout))
    built = Mock(side_effect=AssertionError("the launch went past its handoff"))
    monkeypatch.setattr(container, "judged_roots", built)
    monkeypatch.setattr(
        config_volume, "running_containers", lambda volume, engine: ["a", "b"]
    )
    held_by(tmp_path, VolumeLogins(), "personal")

    with pytest.raises(LaunchRefused, match="2 running contained claude sessions"):
        container.contained_argv(
            Image(),
            Manifest(),
            tmp_path,
            None,
            offered(tmp_path, "work"),
            CLAUDE_LOGIN,
            engine=Podman(),
        )
    built.assert_not_called()
