"""One configuration-home volume per repository and runtime, and the move into them.

An unsplit shared volume holds both CLIs' files, so a split that guessed would
hand one runtime the other's login or history, and a split that ran twice
could overwrite what a session wrote since. These pin the ownership table,
which unsplit volumes a repository answers for, the move itself against an
engine kept in memory — its idempotence, its refusal to touch a volume an
open session holds, and what it says about entries nobody declares.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import sh

import lup.launch.config_volume as config_volume
from lup.launch.config_volume import (
    kept_for_superseded,
    sweep_superseded,
    swept_superseded_notice,
    HomeHelper,
    HomeSplit,
    RuntimeVolume,
    UnsplitVolumes,
    split_config_volumes,
)
from lup.harness.image import Podman
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
