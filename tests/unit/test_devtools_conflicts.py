"""Behavior tests for `lup-devtools git conflict` during a real rebase.

Builds a throwaway git repo with a genuine rebase conflict and pins that
the conflict commands use REBASE_HEAD (MERGE_HEAD and CHERRY_PICK_HEAD do
not exist during a rebase): status must list both sides' commits and audit
must diff the theirs side without reporting a partial result.

A second repository conflicts the manifest itself, which is the case the
workflow exists for and the one `uv run` cannot survive: the whole conflict
workflow has to run there through the launcher the documentation names.
"""

import io
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import sh

from lup.providers.harness import claude_prompt_renderer
from lup.types import JsonObject
from lup.workspace import paths
from lup.devtools.dev import conflicts
from lup.harness.content.skills.merge import SKILL as MERGE_SKILL
from tests.unit.repos import commit_file, git_in, initialized_repo


@pytest.fixture
def rebase_conflict_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    git = initialized_repo(repo, tmp_path / "no-hooks")

    def commit_conflicting(content: str, message: str) -> None:
        commit_file(git, repo, "file.txt", content, message)

    commit_conflicting("base\n", "chore: base")
    git("checkout", "-b", "feature")
    commit_conflicting("feature\n", "feat: feature change")
    git("checkout", "main")
    commit_conflicting("main\n", "fix: main change")
    git("checkout", "feature")
    try:
        git("rebase", "main")
    except sh.ErrorReturnCode:
        pass

    return repo


def test_detects_rebase_state(
    rebase_conflict_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(rebase_conflict_repo)
    assert conflicts.detect_conflict_state() == "rebase"


def test_status_uses_rebase_head_and_lists_both_sides(
    rebase_conflict_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(rebase_conflict_repo)

    conflicts.conflict_status(as_json=True)

    data = json.loads(capsys.readouterr().out)
    assert data["operation"] == "rebase"
    assert data["theirs_ref"] == "REBASE_HEAD"
    assert data["conflicted_files"] == ["file.txt"]
    assert any("feature change" in line for line in data["theirs_commits"])
    assert any("main change" in line for line in data["ours_commits"])


def test_audit_diffs_theirs_side_during_rebase(
    rebase_conflict_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(rebase_conflict_repo)

    conflicts.conflict_audit(["file.txt"], as_json=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["files"][0]["partial"] is False
    assert "partial" not in captured.err


MANIFEST = """[project]
name = "scratch"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [{dependency}]

[tool.lup]
agent_version = "0.1.0"
"""
"""A manifest that declares a lup project, because that is the case at stake.

Every lup project's root is found by reading this table, so a manifest that
carries it and does not parse is the one that takes path resolution — and with
it the whole toolchain — down. A scratch manifest without ``[tool.lup]`` fails
to declare a root for a reason that has nothing to do with the conflict.
"""


@pytest.fixture
def conflicted_manifest_repo(tmp_path: Path) -> Path:
    """A repository stopped mid-merge whose own manifest is what conflicts."""
    repo = tmp_path / "manifest-repo"
    git = initialized_repo(repo, tmp_path / "no-hooks")

    def commit_manifest(dependency: str, message: str) -> None:
        content = MANIFEST.format(dependency=dependency)
        commit_file(git, repo, conflicts.MANIFEST, content, message)

    commit_manifest("", "chore: manifest")
    git("checkout", "-b", "feature")
    commit_manifest('"httpx"', "feat: reach the network")
    git("checkout", "main")
    commit_manifest('"trio"', "feat: run concurrently")
    with pytest.raises(sh.ErrorReturnCode):
        git("merge", "feature")

    return repo


@pytest.fixture
def documented_launcher(
    conflicted_manifest_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> sh.Command:
    """The console script reached exactly as the conflict workflow spells it.

    Nothing could sync an environment into the scratch repository from a
    manifest that does not parse, so the path the documentation names points
    at the console script this suite is running under — the same artifact a
    real worktree holds from before the merge that broke its manifest.

    `UV_PROJECT_ENVIRONMENT` is taken away because this fixture builds the
    layout it would contradict. The variable is a fact about the machine and
    stays set for the rest of the suite — the session image uses it to put the
    environment outside the mounted checkout — but here it names a directory
    that is real, is not under this scratch repository, and is therefore
    reached by a bare name. `launcher_invocation` would then be right about
    this container and wrong about the repository it was asked about, and the
    notice would name a spelling that is not the one built two lines below.
    """
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    launcher = conflicted_manifest_repo / conflicts.DOCUMENTED_LAUNCHER
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(Path(sys.executable).parent / "lup-devtools")
    return sh.Command(str(launcher)).bake(
        _cwd=str(conflicted_manifest_repo),
        _truncate_exc=False,
    )


def test_root_resolution_answers_the_project_whose_manifest_conflicts(
    conflicted_manifest_repo: Path,
) -> None:
    """The load-bearing step, which no subprocess in this file can exercise.

    ``devtools/setup.py`` resolves the project root at import, so this runs
    before Typer dispatches and decides whether any command starts at all.
    A launcher spawned from here is rescued whatever the answer, because the
    library it imports lives inside *this* checkout — a healthy lup project
    whose manifest the fallback walk finds. A real worktree has no such
    second project: the library is vendored under the same root, and the
    conflicted manifest is the only one the walk ever reaches. That
    asymmetry is what hid this, so the resolution is asserted directly.
    """
    assert paths.declared_project_root(conflicted_manifest_repo) == (
        conflicted_manifest_repo
    )
    assert paths.read_agent_version(conflicted_manifest_repo) == "0.0.0"
    assert paths.read_project_name(conflicted_manifest_repo) == "lup"


def test_a_conflicted_subpackage_does_not_shadow_the_root_above_it(
    conflicted_manifest_repo: Path,
) -> None:
    """An unreadable manifest is the last answer, not the nearest one.

    Reading a conflicted manifest as a root is a concession to repair it,
    and a manifest that does declare the project is a better answer wherever
    it sits — otherwise a merge that conflicts one subpackage would relocate
    the root of everything under it.
    """
    subpackage = conflicted_manifest_repo / "packages" / "inner"
    subpackage.mkdir(parents=True)
    (subpackage / conflicts.MANIFEST).write_text("<<<<<<< ours\n", encoding="utf-8")
    (conflicted_manifest_repo / conflicts.MANIFEST).write_text(
        MANIFEST.format(dependency=""), encoding="utf-8"
    )

    assert paths.declared_project_root(subpackage) == conflicted_manifest_repo


def test_uv_cannot_start_once_the_manifest_is_what_conflicts(
    conflicted_manifest_repo: Path,
) -> None:
    """The premise: the spelling every other command uses withdraws here."""
    with pytest.raises(sh.ErrorReturnCode):
        sh.Command("uv")(
            "run",
            "lup-devtools",
            "git",
            "conflict",
            "status",
            _cwd=str(conflicted_manifest_repo),
        )


def test_whole_conflict_workflow_runs_against_a_conflicted_manifest(
    conflicted_manifest_repo: Path,
    documented_launcher: sh.Command,
) -> None:
    """Every command the merge skill needs before the conflict is settled."""

    def report(*words: str) -> JsonObject:
        return json.loads(str(documented_launcher("git", "conflict", *words)))

    status = report("status", "--json")
    assert status["operation"] == "merge"
    assert status["theirs_ref"] == "MERGE_HEAD"
    assert status["conflicted_files"] == [conflicts.MANIFEST]

    assert report("list", "--json")["files"] == [
        {
            "path": conflicts.MANIFEST,
            "conflict_count": 1,
            "scope": "in-scope",
            "branch_touched": True,
        }
    ]
    assert report("audit", conflicts.MANIFEST, "--json")["files"] == [
        {
            "path": conflicts.MANIFEST,
            "ours_removals": [],
            "theirs_removals": [],
            "warning": False,
            "partial": False,
        }
    ]

    remaining = io.StringIO()
    with pytest.raises(sh.ErrorReturnCode):
        documented_launcher("git", "conflict", "complete", "--dry-run", _err=remaining)
    assert conflicts.MANIFEST in remaining.getvalue()

    manifest = conflicted_manifest_repo / conflicts.MANIFEST
    manifest.write_text(MANIFEST.format(dependency='"httpx", "trio"'), encoding="utf-8")
    git_in(conflicted_manifest_repo, conflicted_manifest_repo.parent / "no-hooks")(
        "add", conflicts.MANIFEST
    )

    completion = str(documented_launcher("git", "conflict", "complete", "--dry-run"))
    assert "git commit --no-edit" in completion


def test_conflict_workflow_does_not_import_the_project_application(
    conflicted_manifest_repo: Path,
    documented_launcher: sh.Command,
    tmp_path: Path,
) -> None:
    """Repair starts while a module imported by the ordinary CLI is conflicted."""
    shadow = tmp_path / "shadow"
    application = shadow / "lup_template" / "devtools" / "main.py"
    application.parent.mkdir(parents=True)
    (application.parent.parent / "__init__.py").touch()
    (application.parent / "__init__.py").touch()
    application.write_text(
        "<<<<<<< ours\napp = None\n=======\napp = False\n>>>>>>> theirs\n",
        encoding="utf-8",
    )

    output = documented_launcher(
        "git",
        "conflict",
        "status",
        "--json",
        _env={**os.environ, "PYTHONPATH": str(shadow)},
    )

    report = json.loads(str(output))
    assert report["operation"] == "merge"
    assert report["conflicted_files"] == [conflicts.MANIFEST]


def test_conflict_repair_survives_the_reported_kernel_module_conflict(
    documented_launcher: sh.Command, tmp_path: Path
) -> None:
    shadow = tmp_path / "shadow"
    library = Path(conflicts.__file__).parents[2]
    shutil.copytree(
        library, shadow / "lup", ignore=shutil.ignore_patterns("__pycache__")
    )
    broken = shadow / "lup/policy/kernel/edit.py"
    broken.write_text("<<<<<<< HEAD\n=======\n>>>>>>> feature\n")
    environment = {**os.environ, "PYTHONPATH": str(shadow)}
    status = json.loads(
        str(
            documented_launcher("git", "conflict", "status", "--json", _env=environment)
        )
    )
    assert status["operation"] == "merge"
    audit = json.loads(
        str(
            documented_launcher(
                "git",
                "conflict",
                "audit",
                conflicts.MANIFEST,
                "--json",
                _env=environment,
            )
        )
    )
    assert audit["files"][0]["path"] == conflicts.MANIFEST


def test_a_started_command_resolves_the_conflicted_project_as_its_root(
    documented_launcher: sh.Command,
) -> None:
    """The started process reads the scratch manifest, not this checkout's.

    Root resolution has a second answer — the project enclosing the library's
    own installation, which here is this repository and is sound. Pinning the
    version the launcher reports is what separates "the conflicted manifest
    answered" from "the walk fell through to a project that happens to
    parse": an unreadable manifest declares no version, so the fallback
    ``0.0.0`` is only reachable by way of the scratch repository.
    """
    reported = str(documented_launcher("version"))

    assert "Agent version: 0.0.0" in reported


def test_a_started_command_names_the_launcher_to_reach_it_by(
    documented_launcher: sh.Command,
) -> None:
    """A worker never has to read a `uv` parse error to find the fallback."""
    diagnostics = io.StringIO()
    documented_launcher("git", "conflict", "status", "--json", _err=diagnostics)

    notice = diagnostics.getvalue()
    assert conflicts.MANIFEST in notice
    assert (
        conflicts.invocation(
            conflicts.DOCUMENTED_LAUNCHER, "git", "conflict", "status", "--json"
        )
        in notice
    )


@pytest.mark.parametrize(
    "command",
    [
        ["git", "conflict", "status", "--json"],
        ["git", "conflict", "audit", "<conflicted-files>", "--json"],
        ["git", "conflict", "complete"],
    ],
)
def test_merge_skill_documents_the_launcher_the_commands_declare(
    command: list[str],
) -> None:
    """The workflow a worker follows names the entry point that starts.

    The skill's prompt is prose, and it has to stay prose: an f-string there
    un-masks the whole document to the anti-pattern scanner, which then reads
    the merge guidance as code. So what holds the documentation to the
    declaration is this — and what holds the declaration to the classifier is
    the shell fixture suite, which judges these same words.
    """
    prompt = claude_prompt_renderer().render(MERGE_SKILL.prompt)

    assert conflicts.invocation(conflicts.DOCUMENTED_LAUNCHER, *command) in prompt


class TestTheirsRef:
    def test_rebase_uses_rebase_head(self) -> None:
        from lup.devtools.dev.conflicts import theirs_ref_for

        assert theirs_ref_for("rebase") == "REBASE_HEAD"

    def test_merge_uses_merge_head(self) -> None:
        from lup.devtools.dev.conflicts import theirs_ref_for

        assert theirs_ref_for("merge") == "MERGE_HEAD"

    def test_cherry_pick_uses_cherry_pick_head(self) -> None:
        from lup.devtools.dev.conflicts import theirs_ref_for

        assert theirs_ref_for("cherry-pick") == "CHERRY_PICK_HEAD"
