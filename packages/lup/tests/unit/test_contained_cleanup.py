"""What lup clears away on its own, and what `harness clean` lists and removes.

Every image a declaration edit leaves, every environment a deleted worktree
leaves and every proxy a stopped session leaves is gigabytes nobody asked to
keep; every one of them removed while something still pointed at it is a
rebuild, a lost history, or a diagnosis gone. These pin what counts as
finished for each kind, the sweeps a launch runs, the removal a worktree's
own removal carries, and the dry run.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import sh

import lup.devtools.harness.clean as clean
import lup.launch.container as contained
from lup.devtools.dev.worktree import said_environment_removed
from lup.launch.config_volume import HomeHelper
from lup.launch.superseded import SupersededFile, SupersededRecord
from lup.launch.environments import (
    claim_of,
    claimed,
    environment_directory,
    recorded_environments,
    remove_worktree_environment,
    sweep_environments,
)
from lup.harness.image import Image, Podman
from lup.harness.requirements import Manifest
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN
from lup.sandbox.rail import Lease


def test_an_environment_is_finished_once_its_claimed_checkout_is_gone(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "environments"
    kept, gone = tmp_path / "tree" / "dev", tmp_path / "tree" / "feat"
    kept.mkdir(parents=True)
    claimed(kept, cache)
    claimed(gone, cache)

    swept = sweep_environments([], cache)

    assert [held.root for held in swept] == [gone]
    assert environment_directory(kept, cache).is_dir()
    assert not environment_directory(gone, cache).exists()
    assert not claim_of(environment_directory(gone, cache)).exists()


def test_an_unclaimed_environment_is_finished_only_where_its_digest_proves_it(
    tmp_path: Path,
) -> None:
    """Made before claims existed: matched by name under a worktree's parent."""
    cache = tmp_path / "environments"
    tree = tmp_path / "tree"
    (tree / "dev").mkdir(parents=True)
    environment_directory(tree / "old-feature", cache).mkdir(parents=True)
    stranger = cache / "old-feature-000000000000"
    stranger.mkdir()

    found = {
        held.directory: held for held in recorded_environments([tree / "dev"], cache)
    }

    assert found[environment_directory(tree / "old-feature", cache)].finished()
    assert found[stranger].root is None and not found[stranger].finished()


def test_removing_a_worktree_removes_its_environment_and_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    worktree = tmp_path / "tree" / "feat"
    held = claimed(worktree)

    said_environment_removed(worktree)

    assert not held.exists()
    assert str(held) in capsys.readouterr().out
    assert remove_worktree_environment(worktree) is None


NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)


def kept(tmp_path: Path) -> clean.Kept:
    """A record holding one volume another repository's split superseded yesterday,
    and one an old session still holds."""
    record = SupersededFile(tmp_path / "state")
    if not record.path().is_file():
        record.save(
            SupersededRecord().with_superseded(
                ["lup-cfg-gone-repo", "lup-cfg-held"],
                ["lup-claude-gone-repo"],
                NOW - timedelta(days=1),
            )
        )
    return clean.Kept(record, timedelta(days=14), NOW)


class Engine:
    """An engine answering from fixed listings, recording what it was told to do."""

    def __init__(self) -> None:
        self.images = [
            ("id-1", "localhost/lup-agent:dev", "2.1 GB"),
            ("id-1", "localhost/lup-agent:0123456789ab", "2.1 GB"),
            ("id-2", "localhost/lup-agent:ba9876543210", "1.9 GB"),
        ]
        self.volumes = [
            "lup-claude-lup",
            "lup-cfg-other",
            "lup-cfg-gone-repo",
            "lup-cfg-held",
            "lup-sandbox-ws-1",
            "lup-uv",
            "someone-elses",
        ]
        self.proxies = {"lup-egress-lup": "running", "lup-egress-old": "exited"}
        self.attached = {"lup-cfg-held": "an-old-session"}
        self.done: list[list[str]] = []

    def __call__(self, *arguments: str) -> str:
        words = list(arguments)
        match words:
            case ["images", "--format", template] if '"tag"' in template:
                return "\n".join(
                    json.dumps({"tag": tag, "size": size})
                    for _, tag, size in self.images
                )
            case ["images", *_]:
                return "\n".join(f"{ident} {tag}" for ident, tag, _ in self.images)
            case ["volume", "ls", *_]:
                return "\n".join(self.volumes)
            case ["ps", "-a", "--filter", held, "--format", _] if held.startswith(
                "volume="
            ):
                return self.attached.get(held.removeprefix("volume="), "")
            case ["ps", "-a", "--filter", _, "--filter", "status=exited", *_]:
                return "\n".join(n for n, s in self.proxies.items() if s == "exited")
            case ["ps", "-a", "--filter", _, "--format", _]:
                return "\n".join(self.proxies)
            case ["run", *rest]:
                return "\n".join(
                    f"4\t{word}" for word in rest if word.startswith("/lup-sized/")
                )
        self.done.append(words)
        return ""


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Engine:
    held = Engine()
    monkeypatch.setattr(sh, "Command", lambda binary: held)
    monkeypatch.setattr(clean, "sibling_worktrees", lambda root: [])
    monkeypatch.setattr("lup.launch.config_volume.sibling_worktrees", lambda root: [])
    layout = Mock()
    layout.name.return_value = "lup"
    monkeypatch.setattr(
        "lup.launch.config_volume.repository_layout",
        Mock(return_value=layout),
    )
    monkeypatch.setattr(contained, "repository_layout", Mock(return_value=layout))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return held


def test_the_dry_run_lists_everything_and_marks_what_nothing_points_at(
    tmp_path: Path, engine: Engine
) -> None:
    root = tmp_path / "dev"
    root.mkdir()
    claimed(tmp_path / "gone")

    helper = HomeHelper(
        engine=Podman(), tag="lup-agent:dev", uid=1, gid=1, config_home="/cfg"
    )
    held = clean.inventory(
        root, Image(), Podman(), [CLAUDE_LOGIN, CODEX_LOGIN], helper, kept(tmp_path)
    )
    listed = "\n".join(clean.listing(held, Podman()))

    finished = sorted(item.name for item in held if item.finished)
    assert finished == sorted(
        [
            "lup-agent:ba9876543210",
            "lup-sandbox-ws-1",
            str(environment_directory(tmp_path / "gone")),
            "lup-egress-old",
            "lup-cfg-gone-repo",
        ]
    )
    assert "lup-agent:0123456789ab" not in finished
    assert "a repository's config home" in listed
    assert "lup-cfg-gone-repo  — superseded by lup-claude-gone-repo" in listed
    assert "a launch removes it from 2026-10-08" in listed
    assert "4.0 KB  lup-cfg-gone-repo" in listed
    assert "held by an-old-session" in listed
    assert "another repository's old config home" in listed
    assert "a cache this project declares" in listed
    assert "not lup's to judge" not in listed
    assert engine.done == []


def test_yes_removes_only_what_is_finished(tmp_path: Path, engine: Engine) -> None:
    root = tmp_path / "dev"
    root.mkdir()
    gone = claimed(tmp_path / "gone")
    kept_root = tmp_path / "kept"
    kept_root.mkdir()
    alive = claimed(kept_root)
    logins = [CLAUDE_LOGIN, CODEX_LOGIN]
    held = clean.inventory(root, Image(), Podman(), logins, None, kept(tmp_path))

    said = clean.cleaned(root, held, Podman(), logins, None, kept(tmp_path))

    assert ["rmi", "lup-agent:ba9876543210"] in engine.done
    assert ["volume", "rm", "lup-sandbox-ws-1"] in engine.done
    assert ["rm", "lup-egress-old"] in engine.done
    assert ["volume", "rm", "lup-cfg-gone-repo"] in engine.done
    assert ["volume", "rm", "lup-cfg-held"] not in engine.done
    assert not any("lup-claude-lup" in words for words in engine.done)
    assert not gone.exists() and alive.exists()
    assert "Removed 5" in said[-1].text


def test_a_launch_sweeps_other_projects_stopped_proxies_but_not_its_own(
    engine: Engine,
) -> None:
    engine.proxies = {"lup-egress-lup": "exited", "lup-egress-old": "exited"}

    assert contained.sweep_containers(Podman(), keep="lup-egress-lup") == [
        "lup-egress-old"
    ]
    assert engine.done == [["rm", "lup-egress-old"]]


def test_a_build_prunes_what_it_superseded_and_a_reuse_prunes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Opened(Exception):
        """Raised where the launch would start its egress, past everything tested."""

    retired: list[list[str]] = []
    monkeypatch.setattr(
        contained, "judged_roots", lambda *a, **k: Mock(notices=[], refusal=None)
    )
    monkeypatch.setattr(contained, "prepared_across", lambda *a, **k: [])
    monkeypatch.setattr(contained, "fleet_lease", lambda *a, **k: Lease())
    monkeypatch.setattr(contained, "store_exposure", lambda lease: None)
    monkeypatch.setattr(
        contained,
        "resolved_agent_clis",
        lambda image: Mock(image=image, said=[]),
    )
    monkeypatch.setattr(contained, "build_image", lambda *a, **k: None)
    monkeypatch.setattr(contained, "name_for_checkout", lambda *a, **k: None)
    monkeypatch.setattr(contained, "split_config_volumes", lambda *a, **k: [])
    monkeypatch.setattr(contained, "sweep_environments", lambda worktrees: [])
    monkeypatch.setattr(contained, "sweep_containers", lambda engine, keep: [])
    monkeypatch.setattr(contained, "sibling_worktrees", lambda root: [])
    layout = Mock()
    layout.name.return_value = "lup"
    monkeypatch.setattr(contained, "repository_layout", Mock(return_value=layout))
    monkeypatch.setattr(
        contained, "superseded_images", lambda engine, keep: ["lup-agent:old"]
    )
    monkeypatch.setattr(
        contained,
        "retire_images",
        lambda tags, engine: retired.append(tags) or tags,
    )
    monkeypatch.setattr(contained, "start_egress", Mock(side_effect=Opened("egress")))

    for built in (True, False):
        monkeypatch.setattr(contained, "image_matches", lambda *a, **k: not built)
        with pytest.raises(Opened):
            contained.contained_argv(
                Image(), Manifest(), tmp_path, None, None, CLAUDE_LOGIN, engine=Podman()
            )

    assert retired == [["lup-agent:old"]]
