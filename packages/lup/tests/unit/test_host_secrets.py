"""Secrets kept on the host alone: one file per project under the person's lup config.

A checkout's ``.env.local`` is inside the checkout, which every contained
session mounts and every session reads — the right place for what the
application runs on, the wrong one for a key only a host companion should
hold. Such a key lives in ``$XDG_CONFIG_HOME/lup/secrets/<project>.env``,
owner-only from the moment it exists, never mounted into a container, taken
out of what a launched session inherits, and handed only to the companions
that name it. Written from inside a container it is refused, naming the host.
"""

import json
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lup.launch.companions import (
    CompanionLaunch,
    CompanionPlace,
    CompanionProcess,
    Contribution,
    SharedProcess,
    held_companions,
)
import lup.devtools.harness.launch as launch
import lup.providers.claude.launch as claude_launch
from lup.launch.declaration import LaunchSandbox
from lup.launch.refusal import LaunchRefused
from lup.launch.secrets import HostOnlyRefused, HostSecrets, withheld_secrets
from tests.unit.harness_launch import checkout, composition, profiles, stub_host


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LUP_CONTAINED", raising=False)
    return tmp_path / "xdg" / "lup"


def project(tmp_path: Path, name: str = "adlib") -> Path:
    root = tmp_path / "checkout"
    root.mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    return root


def test_a_project_s_secrets_live_under_the_person_s_lup_config(
    tmp_path: Path, config: Path
) -> None:
    store = HostSecrets.for_checkout(project(tmp_path))

    assert store.path() == config / "secrets" / "adlib.env"


def test_the_store_is_its_owner_s_alone_and_keeps_what_it_is_not_told_to_change(
    tmp_path: Path, config: Path
) -> None:
    store = HostSecrets.for_checkout(project(tmp_path))

    store.write({"GEMINI_API_KEY": "first"})
    store.write({"OTHER_KEY": "second"})
    store.write({"GEMINI_API_KEY": "third"})

    assert store.read() == {"GEMINI_API_KEY": "third", "OTHER_KEY": "second"}
    assert stat.S_IMODE(store.path().stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path().parent.stat().st_mode) == 0o700
    del config


def test_a_cleared_key_is_gone_and_a_missing_one_is_no_change(
    tmp_path: Path, config: Path
) -> None:
    store = HostSecrets.for_checkout(project(tmp_path))
    store.write({"KEEP": "1", "DROP": "2"})

    store.clear(["DROP", "NEVER_THERE"])

    assert store.read() == {"KEEP": "1"}
    del config


def test_writing_inside_a_container_is_refused_naming_the_host(
    tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = HostSecrets.for_checkout(project(tmp_path))
    monkeypatch.setenv("LUP_CONTAINED", "1")

    with pytest.raises(HostOnlyRefused, match="host"):
        store.write({"GEMINI_API_KEY": "leaked"})

    assert not store.path().exists()
    del config


def test_a_launched_session_inherits_no_host_only_secret(
    tmp_path: Path, config: Path
) -> None:
    root = project(tmp_path)
    HostSecrets.for_checkout(root).write({"GEMINI_API_KEY": "secret"})

    kept = withheld_secrets({"GEMINI_API_KEY": "exported", "PATH": "/bin"}, root)

    assert kept == {"PATH": "/bin"}
    del config


class Printer(SharedProcess, frozen=True):
    """A companion that writes the environment it was started with, then serves a port."""

    def process(self, place: CompanionPlace, root: Path) -> CompanionProcess:
        script = (
            "import json, os, socket, sys\n"
            f"open({str(place.state / 'seen.json')!r}, 'w').write("
            "json.dumps(dict(os.environ)))\n"
            "server = socket.socket(); server.bind(('127.0.0.1', int(sys.argv[1])))\n"
            "server.listen(); server.accept()\n"
        )
        return CompanionProcess(
            argv=[sys.executable, "-c", script, str(place.ports["web"])], cwd=root
        )

    def contribution(self, place: CompanionPlace, root: Path) -> Contribution:
        del place, root
        return Contribution()


@contextmanager
def printed(tmp_path: Path, companion: Printer) -> Iterator[dict[str, str]]:
    root = project(tmp_path)
    launch = CompanionLaunch(root=root, runtime="claude", environment={"PATH": "/bin"})
    with held_companions([companion], launch):
        seen = companion.slot(root).directory / "seen.json"
        yield json.loads(seen.read_text())


def test_a_companion_is_handed_only_the_secrets_it_names(
    tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = project(tmp_path)
    HostSecrets.for_checkout(root).write({"GEMINI_API_KEY": "secret", "OTHER": "x"})
    companion = Printer(
        name="printer", ports={"web": 38641}, secrets=["GEMINI_API_KEY"]
    )

    with printed(tmp_path, companion) as environment:
        assert environment["GEMINI_API_KEY"] == "secret"
        assert "OTHER" not in environment
    del config


def test_a_companion_naming_a_secret_the_store_lacks_is_refused(
    tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    companion = Printer(name="printer", ports={"web": 38642}, secrets=["ABSENT_KEY"])

    with pytest.raises(LaunchRefused, match="ABSENT_KEY"):
        with printed(tmp_path, companion):
            pass
    del config


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_launch_hands_its_session_none_of_the_store_s_keys(
    tmp_path: Path,
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
) -> None:
    root = checkout(tmp_path)
    caught = stub_host(monkeypatch, root)
    monkeypatch.setattr(claude_launch, "settle_claude_theme", lambda *_a, **_k: None)
    HostSecrets.for_checkout(root).write({"GEMINI_API_KEY": "secret"})
    monkeypatch.setenv("GEMINI_API_KEY", "exported-by-the-shell")
    request = launch.LaunchRequest(sandbox=LaunchSandbox.INNER)

    if runtime == "claude":
        launch.launch_claude(composition(root, "claude"), request, profiles(), False)
    else:
        launch.launch_codex(composition(root, "codex"), request, None, False, False)

    assert "GEMINI_API_KEY" not in caught.env
    del config
