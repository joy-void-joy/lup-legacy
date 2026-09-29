"""Publish native installs without letting native cache pruning reach live homes."""

import json
import shutil
from pathlib import Path
import pytest
import sh
import tomlkit
from lup.providers.codex.harness_runtime import (
    CodexPluginInstaller,
    PluginCacheConfig,
    cachebusted_plugin_version,
    digest_directory,
    plugin_content_digest,
)

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None, reason="native Codex CLI required"
)


@pytest.fixture
def publication(tmp_path: Path) -> tuple[Path, CodexPluginInstaller]:
    source = tmp_path / "plugin"
    manifest = source / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "lup", "version": "1.0.0"}))
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(
        json.dumps(
            {
                "name": "lup-isolated-test",
                "plugins": [
                    {
                        "name": "lup",
                        "source": {"source": "local", "path": "./plugin"},
                        "policy": {
                            "installation": "AVAILABLE",
                            "authentication": "ON_INSTALL",
                        },
                    }
                ],
            }
        )
    )
    config = PluginCacheConfig(
        codex_home=tmp_path / "home", marketplace="lup-isolated-test"
    )
    config.codex_home.mkdir()
    return (source, CodexPluginInstaller(config))


def test_native_updates_preserve_live_revisions_and_unrelated_configuration(
    tmp_path: Path, publication: tuple[Path, CodexPluginInstaller]
) -> None:
    source, installer = publication
    config = installer.config
    settings = config.codex_home / "config.toml"
    settings.write_text(
        '# Keep this comment.\nmodel = "fixture"\n[plugins."other@elsewhere"]\nenabled = true\n'
    )
    candidates: dict[str, str] = {}
    for content in ("Earlier source.\n", "Later source.\n"):
        (source / "revision.txt").write_text(content)
        digest = plugin_content_digest(source)
        assert digest is not None
        candidates[digest] = content
    high, low = sorted(candidates, reverse=True)
    (source / "revision.txt").write_text(candidates[high])
    first = installer.ensure(source, tmp_path)
    original = (first.installed_root / ".codex-plugin" / "plugin.json").read_bytes()
    (source / "revision.txt").write_text(candidates[low])
    legacy = config.cache_root() / cachebusted_plugin_version(source, low)
    shutil.copytree(source, legacy)
    legacy_manifest = legacy / ".codex-plugin/plugin.json"
    legacy_manifest.write_text(json.dumps({"name": "lup", "version": legacy.name}))
    retained_digest = digest_directory(legacy, Path.read_bytes)
    second = installer.ensure(source, tmp_path)
    assert installer.ensure(source, tmp_path).installed_root == second.installed_root
    assert first.source_digest > second.source_digest
    assert second.package_version == "1.0.0"
    assert second.ready
    assert second.installed_root != legacy
    assert digest_directory(legacy, Path.read_bytes) == retained_digest
    assert first.installed_root != second.installed_root
    assert (
        first.installed_root / ".codex-plugin" / "plugin.json"
    ).read_bytes() == original
    assert (first.installed_root / "revision.txt").read_text() == candidates[high]
    document = tomlkit.parse(settings.read_text())
    assert document["model"] == "fixture"
    assert document["plugins"]["other@elsewhere"]["enabled"] is True
    assert "# Keep this comment." in settings.read_text()

    def selected_version() -> list[str]:
        listed = sh.Command("codex")(
            "plugin",
            "list",
            "--json",
            _cwd=str(tmp_path),
            _env=installer.plugin_environment(),
        )
        selected = json.loads(str(listed))["installed"]
        return [
            row["version"]
            for row in selected
            if row["pluginId"] == "lup@lup-isolated-test"
        ]

    assert selected_version() == [second.installed_root.name]
    (source / "revision.txt").write_text(candidates[high])
    restored = installer.ensure(source, tmp_path)
    assert selected_version() == [restored.installed_root.name]
    assert restored.installed_root not in {first.installed_root, second.installed_root}
    assert installer.ensure(source, tmp_path).installed_root == restored.installed_root
    assert (second.installed_root / "revision.txt").read_text() == candidates[low]
    assert (
        json.loads((source / ".codex-plugin/plugin.json").read_text())["version"]
        == "1.0.0"
    )
    assert (
        first.installed_root / ".codex-plugin" / "plugin.json"
    ).read_bytes() == original


@pytest.mark.parametrize("version", ["local", "zzz-external"])
def test_native_unorderable_winning_revision_is_preserved_and_refused(
    tmp_path: Path, publication: tuple[Path, CodexPluginInstaller], version: str
) -> None:
    source, installer = publication
    first = installer.ensure(source, tmp_path)
    retained = installer.config.cache_root() / version
    shutil.copytree(first.installed_root, retained)
    manifest = retained / ".codex-plugin/plugin.json"
    document = json.loads(manifest.read_text())
    document["version"] = version
    manifest.write_text(json.dumps(document))
    original = manifest.read_bytes()
    settings = installer.config.codex_home / "config.toml"
    original_settings = settings.read_bytes()
    listed = sh.Command("codex")(
        "plugin",
        "list",
        "--json",
        _cwd=str(tmp_path),
        _env=installer.plugin_environment(),
    )
    selected = json.loads(str(listed))["installed"]
    assert [
        row["version"] for row in selected if row["pluginId"] == "lup@lup-isolated-test"
    ] == [version]
    (source / "replacement.txt").write_text("A source revision requiring activation.")
    with pytest.raises(RuntimeError, match="clean Codex home"):
        installer.ensure(source, tmp_path)
    assert manifest.read_bytes() == original
    assert first.installed_root.is_dir()
    assert settings.read_bytes() == original_settings
