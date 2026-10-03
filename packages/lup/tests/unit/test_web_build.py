"""The frontend toolchain: a schema the types compile from, bundles owned like
every generated tree, and a page served from what was built.

The end-to-end build runs only where bun and the workspace's dependencies are
present, which is every machine that changes frontend code and the CI runner;
the rest is exercised over a bundle written by hand.
"""

import json
import os
import shutil
import threading
import tomllib
from pathlib import Path

import pytest
import sh
from fastapi.testclient import TestClient

from lup.devtools.surfaces import EXPLORER, LIBRARY_SURFACES
from lup.execution.shell import git
from lup.harness.ownership import OWNERSHIP_FILENAME, load_manifest
from lup.web.build import (
    Surface,
    dependencies_behind,
    restore_dependencies,
    source_digest,
    write_web_bundles,
)
from lup.web.schema import view_schema, write_view_schema
from lup.web.serve import bundle_app, whole_bundle

PACKAGE = Path(__file__).resolve().parents[2]
"""The library's own package, whose bun workspace sits beside its source."""

WORKSPACE = PACKAGE / "web"

HANDMADE = [Surface(name="explorer", models=[])]
"""One surface by name, for a test that builds nothing real."""


def test_the_view_schema_declares_every_surface_model(tmp_path: Path) -> None:
    written = write_view_schema(Path("schema/views.json"), LIBRARY_SURFACES, tmp_path)

    schema = json.loads(written.read_text(encoding="utf-8"))
    assert {"GraphView", "NodeDetail", "WizardView", "StepReply"} <= set(
        schema["$defs"]
    )
    assert schema["$defs"]["NodeView"]["additionalProperties"] is False
    assert "slug" in schema["$defs"]["NodeView"]["required"]
    assert (
        write_view_schema(
            Path("schema/views.json"), LIBRARY_SURFACES, tmp_path, check=True
        )
        == written
    )
    written.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="stale"):
        write_view_schema(
            Path("schema/views.json"), LIBRARY_SURFACES, tmp_path, check=True
        )
    assert view_schema(LIBRARY_SURFACES) == view_schema(LIBRARY_SURFACES)


def test_every_built_bundle_is_named_by_the_package_data() -> None:
    """The wheel carries what the package data names, and a built file it does
    not name is the failure the surfaces replaced: served from a checkout,
    missing from the wheel, every request to `/` an error."""
    manifest = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    globs: list[str] = manifest["tool"]["setuptools"]["package-data"]["lup.web"]
    home = PACKAGE / "src" / "lup" / "web"
    shipped = {path for glob in globs for path in home.glob(glob) if path.is_file()}
    built = {path for path in (home / "bundles").rglob("*") if path.is_file()}

    assert built and built <= shipped
    assert {surface.name for surface in LIBRARY_SURFACES} <= {
        path.parent.name for path in built if path.name == "index.html"
    }


def handmade(root: Path, surface: str = "explorer") -> Path:
    """A bundle the way Vite lays one out, without Vite."""
    home = root / "bundles" / surface
    (home / "assets").mkdir(parents=True)
    (home / "index.html").write_text(
        '<!doctype html><script type="module" src="./assets/app.js"></script>\n',
        encoding="utf-8",
    )
    (home / "assets" / "app.js").write_text(
        "console.log('explorer');\n", encoding="utf-8"
    )
    return root / "bundles"


def test_bundle_app_serves_the_page_and_its_assets_by_name(tmp_path: Path) -> None:
    bundles = handmade(tmp_path)
    # The app answers only for the Host it was built for, which is the
    # loopback guard doing its job; the client has to speak that Host.
    client = TestClient(
        bundle_app("Explorer", "http://127.0.0.1:1", "explorer", bundles),
        base_url="http://127.0.0.1:1",
    )

    page = client.get("/")
    script = client.get("/assets/app.js")

    assert page.status_code == 200 and "assets/app.js" in page.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "explorer" in script.text
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/assets/..%2Findex.html").status_code == 404


def rebuilt(bundles: Path, surface: str = "explorer") -> None:
    """The bundle rebuilt as a new build lands: a page naming new assets, the old ones gone."""
    home = bundles / surface
    (home / "assets" / "app.js").unlink()
    (home / "assets" / "next.js").write_text("console.log('next');\n", encoding="utf-8")
    (home / "index.html").write_text(
        '<!doctype html><script type="module" src="./assets/next.js"></script>\n',
        encoding="utf-8",
    )


def test_a_page_keeps_its_assets_while_its_bundle_is_rebuilt_beneath_it(
    tmp_path: Path,
) -> None:
    """The page and every asset it names come from one build for as long as it serves."""
    bundles = handmade(tmp_path)
    serving = TestClient(
        bundle_app("Explorer", "http://127.0.0.1:1", "explorer", bundles),
        base_url="http://127.0.0.1:1",
    )

    rebuilt(bundles)
    page = serving.get("/")
    named = serving.get("/assets/app.js")
    other = serving.get("/assets/next.js")
    fresh = TestClient(
        bundle_app("Explorer", "http://127.0.0.1:1", "explorer", bundles),
        base_url="http://127.0.0.1:1",
    )

    assert "assets/app.js" in page.text
    assert named.status_code == 200 and "explorer" in named.text
    assert other.status_code == 404
    assert other.headers["content-type"].startswith("text/plain")
    assert "reload the page" in other.text
    assert "assets/next.js" in fresh.get("/").text
    assert fresh.get("/assets/next.js").status_code == 200


def test_a_bundle_caught_mid_rebuild_is_read_again_until_whole(
    tmp_path: Path,
) -> None:
    bundles = handmade(tmp_path)
    home = bundles / "explorer"
    (home / "index.html").write_text(
        '<!doctype html><script type="module" src="./assets/late.js"></script>\n'
        '<link rel="stylesheet" href="./assets/app.css">\n',
        encoding="utf-8",
    )
    (home / "assets" / "app.css").write_text("body {}\n", encoding="utf-8")
    landing = threading.Timer(
        0.3,
        (home / "assets" / "late.js").write_text,
        args=("console.log('late');\n",),
    )
    landing.start()

    bundle = whole_bundle(home, attempts=50, pause=0.05)
    landing.join()

    assert {asset.name for asset in bundle.assets} == {"app.js", "app.css", "late.js"}
    (home / "index.html").write_text(
        '<!doctype html><script src="./assets/never.js"></script>\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="never.js"):
        whole_bundle(home, attempts=2, pause=0.01)


def test_a_missing_bundle_is_refused_naming_the_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="harness generate all"):
        bundle_app("Explorer", "http://127.0.0.1:1", "explorer", tmp_path / "none")


def repository(root: Path) -> Path:
    """``root`` as a checkout of its own, which is what tells a source from scratch."""
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", str(root))
    return root


def test_source_digest_moves_with_the_sources_and_not_with_generated_types(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    (tmp_path / "src" / "explorer").mkdir(parents=True)
    (tmp_path / "src" / "generated").mkdir()
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "src" / "explorer" / "App.tsx").write_text("one", encoding="utf-8")
    before = source_digest(tmp_path)

    (tmp_path / "src" / "generated" / "views.d.ts").write_text("t", encoding="utf-8")
    assert source_digest(tmp_path) == before
    (tmp_path / "src" / "explorer" / "App.tsx").write_text("two", encoding="utf-8")
    assert source_digest(tmp_path) != before


def test_source_digest_reads_what_a_clone_holds_and_nothing_git_ignores(
    tmp_path: Path,
) -> None:
    """A tool's state dropped under `src/` is no source, or the proof would
    hold only in the checkout holding it and every other checkout rebuild."""
    repository(tmp_path)
    (tmp_path / ".gitignore").write_text(".lup/\n", encoding="utf-8")
    (tmp_path / "src" / "dashboard").mkdir(parents=True)
    (tmp_path / "src" / "dashboard" / "App.tsx").write_text("one", encoding="utf-8")
    before = source_digest(tmp_path)

    state = tmp_path / "src" / "dashboard" / ".lup" / "script-runs.json"
    state.parent.mkdir()
    state.write_text("{}", encoding="utf-8")
    assert source_digest(tmp_path) == before
    (tmp_path / "src" / "dashboard" / "Fresh.tsx").write_text("new", encoding="utf-8")
    assert source_digest(tmp_path) != before


@pytest.mark.skipif(
    shutil.which("bun") is None or not (WORKSPACE / "node_modules").is_dir(),
    reason="the frontend toolchain is not installed here",
)
def test_write_web_bundles_builds_owns_and_verifies(tmp_path: Path) -> None:
    """One build lands the tree with proof; a second changes nothing; a hand
    edit is stale; a source edit is stale."""
    bundles = Path("bundles")

    landed = write_web_bundles(WORKSPACE, bundles, [EXPLORER], tmp_path)

    assert (landed / "explorer" / "index.html").is_file()
    manifest = load_manifest(landed / OWNERSHIP_FILENAME)
    assert manifest is not None and manifest.target_requirements == ["bun"]
    assert all(item.path.parts[0] == "bundles" for item in manifest.files)
    assert (
        write_web_bundles(WORKSPACE, bundles, [EXPLORER], tmp_path, check=True)
        == landed
    )
    (landed / "explorer" / "index.html").write_text("edited\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="behind"):
        write_web_bundles(WORKSPACE, bundles, [EXPLORER], tmp_path, check=True)


def recorded_restores(
    monkeypatch: pytest.MonkeyPatch, refusing: str = ""
) -> list[tuple[str, ...]]:
    """Bun as the restore sees it: each install recorded, `node_modules` laid
    down the way a real one lays it — or refused with ``refusing`` as bun's
    own stderr."""
    calls: list[tuple[str, ...]] = []  # lup: ignore[empty-collection] — call record

    def bun(*args: str, _cwd: str) -> None:
        calls.append(args)
        if refusing:
            raise sh.ErrorReturnCode_1(
                "bun install --frozen-lockfile", b"", refusing.encode("utf-8")
            )
        (Path(_cwd) / "node_modules").mkdir(exist_ok=True)

    monkeypatch.setattr("lup.web.build.BUN", bun)
    return calls


def test_missing_dependencies_are_restored_and_current_ones_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "web"
    workspace.mkdir()
    (workspace / "bun.lock").write_text("{}\n", encoding="utf-8")
    restores = recorded_restores(monkeypatch)

    assert dependencies_behind(workspace)
    assert restore_dependencies(workspace)
    assert restores == [("install", "--frozen-lockfile")]
    assert not dependencies_behind(workspace)
    assert not restore_dependencies(workspace)
    assert len(restores) == 1


def test_dependencies_older_than_the_lockfile_are_restored_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lockfile that moved after the install — a checkout, a merge — is what
    a copied `node_modules` is behind; the restore dates it current again."""
    workspace = tmp_path / "web"
    installed = workspace / "node_modules"
    installed.mkdir(parents=True)
    lockfile = workspace / "bun.lock"
    lockfile.write_text("{}\n", encoding="utf-8")
    earlier = lockfile.stat().st_mtime - 60
    os.utime(installed, (earlier, earlier))
    restores = recorded_restores(monkeypatch)

    assert dependencies_behind(workspace)
    assert restore_dependencies(workspace)
    assert not dependencies_behind(workspace)
    assert not restore_dependencies(workspace)
    assert len(restores) == 1


def test_a_workspace_without_a_lockfile_is_behind_only_while_bare(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "web"
    workspace.mkdir()

    assert dependencies_behind(workspace)
    (workspace / "node_modules").mkdir()
    assert not dependencies_behind(workspace)


def test_a_failed_restore_names_the_command_and_carries_buns_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "web"
    workspace.mkdir()
    recorded_restores(
        monkeypatch, refusing="error: lockfile had changes, but lockfile is frozen\n"
    )

    with pytest.raises(RuntimeError, match="bun install --frozen-lockfile") as failed:
        restore_dependencies(workspace)

    assert str(workspace) in str(failed.value)
    assert "lockfile is frozen" in str(failed.value)


def test_the_build_restores_a_workspace_without_dependencies_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = repository(tmp_path) / "web"
    (workspace / "src" / "explorer").mkdir(parents=True)
    (workspace / "package.json").write_text("{}\n", encoding="utf-8")
    restores = recorded_restores(monkeypatch)

    def built(home: Path, surface: str, out: Path) -> list[Path]:
        assert (home / "node_modules").is_dir()
        out.mkdir(parents=True, exist_ok=True)
        page = out / "index.html"
        page.write_text(f"<!doctype html>{surface}\n", encoding="utf-8")
        return [page]

    monkeypatch.setattr("lup.web.build.built_files", built)

    landed = write_web_bundles(Path("web"), Path("bundles"), HANDMADE, tmp_path)

    assert restores == [("install", "--frozen-lockfile")]
    assert (landed / "explorer" / "index.html").is_file()


def test_the_build_runs_where_the_proof_no_longer_holds_and_nowhere_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holding proof is current in either mode; a check never builds."""
    workspace = repository(tmp_path) / "web"
    (workspace / "node_modules").mkdir(parents=True)
    (workspace / "src" / "explorer").mkdir(parents=True)
    (workspace / "package.json").write_text("{}\n", encoding="utf-8")
    source = workspace / "src" / "explorer" / "App.tsx"
    source.write_text("one", encoding="utf-8")
    builds: list[str] = []

    def built(home: Path, surface: str, out: Path) -> list[Path]:
        builds.append(surface)
        out.mkdir(parents=True, exist_ok=True)
        page = out / "index.html"
        page.write_text(f"<!doctype html>{surface}\n", encoding="utf-8")
        return [page]

    monkeypatch.setattr("lup.web.build.built_files", built)
    arguments = (Path("web"), Path("bundles"), HANDMADE, tmp_path)

    landed = write_web_bundles(*arguments)
    write_web_bundles(*arguments, check=True)
    write_web_bundles(*arguments)
    assert builds == ["explorer"]

    source.write_text("two", encoding="utf-8")
    with pytest.raises(RuntimeError, match="behind"):
        write_web_bundles(*arguments, check=True)
    assert builds == ["explorer"]
    write_web_bundles(*arguments)
    assert builds == ["explorer", "explorer"]

    (landed / "explorer" / "index.html").write_text("edited\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="behind"):
        write_web_bundles(*arguments, check=True)
    assert builds == ["explorer", "explorer"]
