"""Behavior tests for `lup-devtools dev library`: how a project obtains lup.

The mode is a property of pyproject.toml, so every assertion here reads the
rewritten file back rather than trusting the change log. What must hold is
that un-vendoring removes every path that stops resolving once the package is
gone, that vendoring restores exactly those, and that the round trip is the
identity — a project can move between modes without accumulating drift. What
the project declared for its own reasons is not part of any of that, so the
fixture carries one such pyright environment and every rewrite leaves it whole.
"""

import tomllib
import json
from pathlib import Path

import pytest
import typer
from pydantic import ValidationError

import lup.devtools.dev.library as library
from lup.execution.shell import git
from lup.types import JsonValue

VENDORED_PYPROJECT = """\
[project]
name = "demo"
dependencies = ["fastapi>=0.139.0", "lup-agents[claude,codex,docker]", "typer>=0.21.1"]

[tool.uv]
exclude-newer = "3 days"

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
lup-agents = { workspace = true }

[tool.pytest.ini_options]
pythonpath = ["src", "packages/lup/src"]

[tool.pyright]
include = ["src", "packages/lup/src", "tests", "packages/lup/tests"]

[[tool.pyright.executionEnvironments]]
root = "packages/lup/src/lup/providers/claude/assets"
extraPaths = [".claude/plugins/lup/hooks/runtime", "packages/lup/src/lup/policy/assets"]

[[tool.pyright.executionEnvironments]]
root = ".claude/plugins/lup/hooks/scripts"
extraPaths = [".claude/plugins/lup/hooks/runtime"]

[[tool.pyright.executionEnvironments]]
root = "packages/lup/src/lup/providers/codex/assets"
extraPaths = [".codex/plugins/lup/hooks/runtime", "packages/lup/src/lup/policy/assets"]

[[tool.pyright.executionEnvironments]]
root = ".codex/plugins/lup/hooks/scripts"
extraPaths = [".codex/plugins/lup/hooks/runtime"]

[[tool.pyright.executionEnvironments]]
root = "src/demo/search/backends"
reportMissingImports = "none"
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(VENDORED_PYPROJECT, encoding="utf-8")
    (tmp_path / library.VENDORED_ROOT).mkdir(parents=True)
    (tmp_path / library.VENDORED_ROOT / "pyproject.toml").write_text(
        '[project]\nname = "lup-agents"\n', encoding="utf-8"
    )
    return tmp_path


def at(root: Path, *keys: str) -> JsonValue:
    """Walk a key path through pyproject.toml, or None where it stops."""
    with (root / "pyproject.toml").open("rb") as handle:
        value: JsonValue = tomllib.load(handle)
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def strings(root: Path, *keys: str) -> list[str]:
    """Read a list-of-strings setting, or an empty list where it is absent."""
    value = at(root, *keys)
    return [str(item) for item in value] if isinstance(value, list) else []


def environment_roots(root: Path) -> list[str]:
    """Name the root of every declared pyright execution environment."""
    match at(root, "tool", "pyright", "executionEnvironments"):
        case [*environments]:
            return [
                str(entry["root"])
                for entry in environments
                if isinstance(entry, dict) and "root" in entry
            ]
        case _:
            return []


def test_a_vendored_project_reads_as_local(project: Path) -> None:
    assert library.read_mode(project) is library.LibraryMode.LOCAL


def test_publishing_drops_every_path_that_stops_resolving(project: Path) -> None:
    library.set_mode(project, library.LibraryMode.PUBLISHED, version="0.3.0")

    assert library.read_mode(project) is library.LibraryMode.PUBLISHED
    assert "lup-agents[claude,codex,docker]>=0.3.0" in strings(
        project, "project", "dependencies"
    )
    assert at(project, "tool", "uv", "workspace") is None
    assert strings(project, "tool", "pytest", "ini_options", "pythonpath") == ["src"]
    assert library.VENDORED_SRC not in strings(project, "tool", "pyright", "include")
    assert not [
        root for root in environment_roots(project) if library.VENDORED_ROOT in root
    ]


def test_publishing_keeps_the_generated_tree_environments(project: Path) -> None:
    library.set_mode(project, library.LibraryMode.PUBLISHED, version="0.3.0")

    assert environment_roots(project) == [
        ".claude/plugins/lup/hooks/scripts",
        ".codex/plugins/lup/hooks/scripts",
        "src/demo/search/backends",
    ]


def test_an_environment_the_project_owns_survives_whole(project: Path) -> None:
    """`extraPaths` is optional, and the rest of the table is not lup's to touch."""
    library.set_mode(
        project,
        library.LibraryMode.GIT,
        git=library.GitSource(url="https://github.com/upstream/framework", ref="dev"),
    )

    environments = at(project, "tool", "pyright", "executionEnvironments")
    assert isinstance(environments, list)
    assert {"root": "src/demo/search/backends", "reportMissingImports": "none"} in (
        environments
    )


def test_un_vendoring_drops_the_tests_root_along_with_the_source_one(
    project: Path,
) -> None:
    library.set_mode(project, library.LibraryMode.PUBLISHED, version="0.3.0")

    # Both roots live inside the package that is about to be deleted, so a
    # pyright include naming either would point at nothing.
    assert strings(project, "tool", "pyright", "include") == ["src", "tests"]


def test_a_git_project_reads_as_git_and_names_where_it_resolves(project: Path) -> None:
    source = library.GitSource(
        url="https://github.com/upstream/framework", ref_kind="branch", ref="dev"
    )

    library.set_mode(project, library.LibraryMode.GIT, git=source)

    assert library.read_mode(project) is library.LibraryMode.GIT
    assert library.read_git_source(project) == source
    # The distribution sits inside the repository, not at its root.
    assert (
        at(project, "tool", "uv", "sources", "lup-agents", "subdirectory")
        == "packages/lup"
    )
    # A source override supplies the version, so no bound is restated.
    assert "lup-agents[claude,codex,docker]" in strings(
        project, "project", "dependencies"
    )


def test_git_un_vendors_exactly_as_publishing_does(project: Path) -> None:
    source = library.GitSource(url="https://github.com/upstream/framework", ref="dev")
    library.set_mode(project, library.LibraryMode.GIT, git=source)

    assert at(project, "tool", "uv", "workspace") is None
    assert strings(project, "tool", "pytest", "ini_options", "pythonpath") == ["src"]
    assert environment_roots(project) == [
        ".claude/plugins/lup/hooks/scripts",
        ".codex/plugins/lup/hooks/scripts",
        "src/demo/search/backends",
    ]


def test_each_kind_of_ref_is_written_under_its_own_key(project: Path) -> None:
    for kind in ("branch", "tag", "rev"):
        source = library.GitSource(
            url="https://github.com/upstream/framework", ref_kind=kind, ref="something"
        )
        library.set_mode(project, library.LibraryMode.GIT, git=source)

        assert at(project, "tool", "uv", "sources", "lup-agents", kind) == "something"
        assert library.read_git_source(project) == source


def test_a_command_line_may_name_one_ref_and_no_more() -> None:
    assert library.git_source("u", branch="dev").ref == "dev"
    assert library.git_source("u", tag="v1").ref_kind == "tag"
    # No ref named at all falls back to the repository's default branch.
    assert library.git_source("u").ref == "main"

    with pytest.raises(typer.BadParameter, match="--branch and --tag"):
        library.git_source("u", branch="dev", tag="v1")


def test_git_mode_needs_somewhere_to_resolve_from(project: Path) -> None:
    with pytest.raises(ValueError, match="repository and ref"):
        library.set_mode(project, library.LibraryMode.GIT)


def test_moving_between_modes_and_back_settles_where_it_started(project: Path) -> None:
    """Table order in the file may shift; nothing may be lost, gained, or reordered."""
    before = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))

    library.set_mode(project, library.LibraryMode.PUBLISHED, version="0.3.0")
    library.set_mode(project, library.LibraryMode.LOCAL)

    assert tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8")) == (
        before
    )


def test_restating_a_settled_mode_changes_nothing(project: Path) -> None:
    assert library.set_mode(project, library.LibraryMode.LOCAL) == []


def test_vendoring_without_a_package_present_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(VENDORED_PYPROJECT, encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="no library to vendor"):
        library.set_mode(tmp_path, library.LibraryMode.LOCAL)


def test_a_dry_run_reports_without_writing(project: Path) -> None:
    before = (project / "pyproject.toml").read_text(encoding="utf-8")

    changes = library.set_mode(
        project, library.LibraryMode.PUBLISHED, version="0.3.0", dry_run=True
    )

    assert changes
    assert (project / "pyproject.toml").read_text(encoding="utf-8") == before


def test_an_unrenamed_template_refuses_to_un_vendor(project: Path) -> None:
    (project / "src" / "lup_template").mkdir(parents=True)

    with pytest.raises(typer.BadParameter, match="template itself"):
        library.guard_leaving_local(project, force=False)

    library.guard_leaving_local(project, force=True)


def test_a_git_source_requires_a_nonempty_repository() -> None:
    for fields in ({}, {"url": ""}):
        with pytest.raises(ValidationError):
            library.GitSource.model_validate(fields)


def test_an_explicit_source_overrides_the_pin_and_registry(project: Path) -> None:
    source = library.GitSource(url="https://forge.example/pinned/framework")
    library.set_mode(project, library.LibraryMode.GIT, git=source)
    (project / "sync.json").write_text(
        '{"projects":[{"name":"lup","url":"https://forge.example/registered/framework"}]}'
    )
    assert library.repository_url(
        project, "https://forge.example/chosen/framework"
    ) == ("https://forge.example/chosen/framework")
    assert library.repository_url(project) == source.url


def test_the_named_local_registration_overrides_its_shared_entry(project: Path) -> None:
    (project / "sync.json").write_text(
        '{"projects":[{"name":"custom","url":"https://forge.example/shared/framework"}]}'
    )
    (project / "sync.json.local").write_text(
        '{"projects":[{"name":"custom","url":"https://forge.example/local/framework"}]}'
    )
    assert library.repository_url(project, project="custom") == (
        "https://forge.example/local/framework"
    )
    assert library.configured_repository(project) == ""


def test_a_registered_checkout_supplies_its_own_origin(project: Path) -> None:
    upstream = project / "upstream"
    upstream.mkdir()
    git("init", "--quiet", str(upstream))
    git(
        "-C",
        str(upstream),
        "remote",
        "add",
        "origin",
        "git@forge.example:maintainers/framework.git",
    )
    (project / "sync.json.local").write_text(
        '{"projects":[{"name":"lup","path":"upstream"}]}'
    )
    assert (
        library.repository_url(project) == "git@forge.example:maintainers/framework.git"
    )
    assert library.library_trackers(project)[0].repository == (
        "forge.example/maintainers/framework"
    )


def test_the_consuming_origin_never_becomes_the_dependency_source(
    project: Path,
) -> None:
    git("init", "--quiet", str(project))
    git("-C", str(project), "remote", "add", "origin", "https://forge.example/acme/app")
    with pytest.raises(typer.BadParameter, match="Pass --url"):
        library.repository_url(project)
    assert library.library_trackers(project) == []


@pytest.mark.parametrize(
    ("url", "repository"),
    [
        ("https://forge.example/team/framework.git", "forge.example/team/framework"),
        (
            "https://forge.example:8443/team/framework.git",
            "forge.example:8443/team/framework",
        ),
        (
            "ssh://git@forge.example:2222/team/framework.git",
            "forge.example/team/framework",
        ),
        ("ssh://git@forge.example/team/framework.git", "forge.example/team/framework"),
        (
            "https://user:secret@forge.example/team/framework.git",
            "forge.example/team/framework",
        ),
        ("file:///local/framework", ""),
        ("/local/framework", ""),
    ],
)
def test_tracker_identity_keeps_the_forge_without_credentials_or_local_paths(
    project: Path, url: str, repository: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library, "resolved_host", lambda alias: alias)
    (project / "sync.json.local").write_text(
        json.dumps({"projects": [{"name": "lup", "url": url}]})
    )
    trackers = library.library_trackers(project)
    assert [tracker.repository for tracker in trackers] == (
        [repository] if repository else []
    )
    if trackers:
        assert trackers[0].claims("lup.resolver")


@pytest.mark.parametrize(
    "url",
    ["git@work-forge:team/framework.git", "ssh://git@work-forge/team/framework.git"],
)
def test_tracker_routing_resolves_ssh_aliases_without_changing_the_git_source(
    project: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    (project / "sync.json.local").write_text(
        json.dumps({"projects": [{"name": "lup", "url": url}]})
    )
    monkeypatch.setattr(library, "resolved_host", lambda _alias: "forge.example")
    assert library.repository_url(project) == url
    assert (
        library.library_trackers(project)[0].repository
        == "forge.example/team/framework"
    )
