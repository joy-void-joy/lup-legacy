"""A contained Codex session runs its hooks from a revision it cannot rewrite.

Codex runs a plugin's hooks from the revision installed in its home, and a
contained session's home is the per-repository volume it writes. So the
launch writes that revision again on the host, from the plugin's source,
outside the checkout, and mounts it read-only over the plugin's cache in the
home: the session then runs the hooks it was launched with, and whatever
another revision the volume holds is out of its sight.
"""

import json
import shutil
from pathlib import Path, PurePosixPath

import pytest
import sh
from typer.testing import CliRunner

import lup.providers.codex.install as installation
from lup.harness.image import Image
from lup.providers.codex.harness_runtime import plugin_content_digest, revision_snapshot
from lup.providers.codex.install import PreparedPlugin
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.marketplace import CodexMarketplace
from lup.providers.codex.session import held_revision


def plugin(root: Path) -> Path:
    """A plugin source whose hooks run a script, as a generated one does."""
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "lup", "version": "0.1.0"}), encoding="utf-8"
    )
    (root / "hooks").mkdir()
    (root / "hooks" / "policy.py").write_text("print('judged')\n", encoding="utf-8")
    return root


def revision_of(source: Path) -> str:
    """The name an automatic installation gives this source's content."""
    return f"0.1.0+codex.{plugin_content_digest(source)}"


def test_the_preparation_reports_the_revision_it_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the preparation knows the name the home's own cache made it take."""
    installed = tmp_path / "home" / "plugins" / "cache" / "lup" / "lup" / "0.1.0"
    monkeypatch.setattr(
        installation,
        "install_codex_plugin",
        lambda *_arguments: PreparedPlugin(installed_root=installed),
    )

    said = CliRunner().invoke(
        installation.app,
        ["--root", str(tmp_path), "--home", str(tmp_path / "home"), "--report"],
    )

    assert said.exit_code == 0, said.output
    assert PreparedPlugin.model_validate_json(said.stdout).installed_root == installed
    assert CODEX_LOGIN.home_preparation is not None
    assert (
        CODEX_LOGIN.home_preparation.command(tmp_path, Path("/cfg"), report=True)[-1]
        == "--report"
    )


def test_a_snapshot_is_the_source_under_the_revisions_own_name(tmp_path: Path) -> None:
    source = plugin(tmp_path / "source")
    revision = revision_of(source)

    held = revision_snapshot(source, revision, tmp_path / "revisions")

    assert [entry.name for entry in held.iterdir()] == [revision]
    assert plugin_content_digest(held / revision) == plugin_content_digest(source)
    manifest = json.loads(
        (held / revision / ".codex-plugin" / "plugin.json").read_text()
    )
    assert manifest["version"] == revision
    assert revision_snapshot(source, revision, tmp_path / "revisions") == held


def test_a_revision_named_for_other_content_is_refused(tmp_path: Path) -> None:
    """The source moved between the preparation and the snapshot, or the report lied."""
    source = plugin(tmp_path / "source")

    with pytest.raises(ValueError, match="does not name"):
        revision_snapshot(source, "0.1.0+codex.deadbeef", tmp_path / "revisions")


def test_the_hold_covers_the_plugins_whole_cache_in_the_home(tmp_path: Path) -> None:
    source = plugin(tmp_path / "source")
    declared = CodexMarketplace(name="lup", plugin="lup", source=source)
    cache = PurePosixPath("/cfg/plugins/cache/lup/lup")
    prepared = PreparedPlugin(installed_root=Path(cache / revision_of(source)))

    held = held_revision(prepared, declared, "/cfg", tmp_path / "revisions")

    assert list(held.values()) == [cache.as_posix()]
    assert held_revision(PreparedPlugin(), declared, "/cfg", tmp_path) == {}
    elsewhere = PreparedPlugin(
        installed_root=Path("/cfg/elsewhere") / revision_of(source)
    )
    with pytest.raises(ValueError, match="outside"):
        held_revision(elsewhere, declared, "/cfg", tmp_path / "revisions")


def test_the_hold_is_mounted_after_the_volume_it_nests_in() -> None:
    """Parent before child, as every other mount a session argv carries."""
    image = Image()
    opening = image.session_arguments(
        tag="lup-agent:x",
        checkout=Path("/checkout"),
        uid=1000,
        gid=1000,
        writable={},
        read_only={},
        state_volume="lup-codex-x",
        config_home_env="CODEX_HOME",
    )
    snapshot = Path("/cache/revisions/abc")

    held = image.home_mounts(
        opening, "lup-codex-x", {snapshot: "/cfg/plugins/cache/lup/lup"}
    )

    volume = held.index("lup-codex-x:/cfg")
    assert held[volume + 1 : volume + 3] == [
        "-v",
        f"{snapshot}:/cfg/plugins/cache/lup/lup:ro",
    ]
    assert image.home_mounts(opening, "lup-codex-x", {}) == opening


def test_a_session_cannot_rewrite_the_hooks_it_runs(tmp_path: Path) -> None:
    """Measured with real mounts in a user namespace: the held cache refuses a write."""
    if shutil.which("unshare") is None:
        pytest.skip("unshare is not on PATH")
    source = plugin(tmp_path / "source")
    revision = revision_of(source)
    cache = tmp_path / "home" / "plugins" / "cache" / "lup" / "lup"
    shutil.copytree(source, cache / revision)
    (cache / "9.9.9").mkdir()
    held = revision_snapshot(source, revision, tmp_path / "revisions")
    hook = cache / revision / "hooks" / "policy.py"
    script = (
        f"mount --bind '{held}' '{cache}' && mount -o remount,bind,ro '{cache}' && "
        f"ls '{cache}' && (echo planted > '{hook}' || echo refused)"
    )
    try:
        said = str(
            sh.Command("unshare")(
                "--user",
                "--map-current-user",
                "--keep-caps",
                "--mount",
                "--",
                "sh",
                "-c",
                script,
            )
        )
    except sh.ErrorReturnCode as refused:
        pytest.skip(f"this host refuses an unprivileged mount namespace: {refused}")

    assert said.split() == [revision, "refused"]
    assert hook.read_text(encoding="utf-8") == "print('judged')\n"
