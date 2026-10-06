"""Development checks stop at roots that do not carry live code."""

import json
from pathlib import Path

import pytest

from lup.devtools.dev import antipatterns, boundaries, check, tracked
from lup.devtools.project import DevProject
from lup.policy.kernel.rows import PathRoleRow


class GitListing:
    """The file inventory both scans receive from Git."""

    def lines(self, *_args: str, **_options: str) -> list[str]:
        return ["src/app.py", "notes/jobs/generated.py"]

    def __call__(self, *args: str, **_options: str) -> str:
        separator = "\0" if "-z" in args else "\n"
        return separator.join(["src/app.py", "notes/jobs/generated.py", ""])


@pytest.fixture
def data_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DevProject:
    (tmp_path / "src").mkdir()
    (tmp_path / "notes/jobs").mkdir(parents=True)
    (tmp_path / "src/app.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "notes/jobs/generated.py").write_text(
        "payload = 'large'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    listing = GitListing()
    monkeypatch.setattr(tracked, "git", listing)
    return DevProject(
        package="app",
        path_roles=[
            PathRoleRow(root="tests", role="test"),
            PathRoleRow(root="notes", role="data"),
            PathRoleRow(root="**/tmp", role="scratch"),
        ],
    )


def test_antipatterns_skip_data_roots(data_project: DevProject) -> None:
    assert [item.rel for item in antipatterns.scanned_files(data_project)] == [
        "src/app.py"
    ]


def test_boundaries_skip_data_roots(data_project: DevProject) -> None:
    assert [item.rel for item in boundaries.tracked_python_sources(data_project)] == [
        "src/app.py"
    ]


def test_every_external_check_skips_data_and_scratch(
    data_project: DevProject, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    pyright_configuration: dict[str, object] = {}

    def capture(*arguments: str, **_options: str) -> None:
        calls.append(arguments)
        if arguments[1:3] == ("pyright", "--project"):
            pyright_configuration.update(
                json.loads(Path(arguments[3]).read_text(encoding="utf-8"))
            )

    monkeypatch.setattr(check, "uv", capture)
    monkeypatch.setattr(check, "project_root", lambda: tmp_path)
    excluded = check.non_code_roots(data_project)

    # About data roots, not the environment: the whole configuration is
    # asserted, so the session's own redirect is held out of it.
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    check.ruff_format_check(False, excluded)
    check.ruff_lint_check(False, excluded)
    check.pyright_check(excluded)
    check.TestRoot(name="pytest", directory=tmp_path).checked(2, excluded)

    assert excluded == ["notes", "**/tmp"]
    assert all("tests" not in call for call in calls)
    assert all("notes" in call and "**/tmp" in call for call in (calls[0], calls[1]))
    assert pyright_configuration == {
        "include": ["."],
        "exclude": ["notes", "**/tmp"],
    }
    assert calls[3][-8:] == (
        "--ignore-glob",
        "notes",
        "--ignore-glob",
        "notes/**",
        "--ignore-glob",
        "**/tmp",
        "--ignore-glob",
        "**/tmp/**",
    )


def test_a_narrowed_ruff_run_forces_the_exclusions(
    data_project: DevProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file named on ruff's command line is checked unless exclusions are forced.

    The changed-file scope names files by path, so without `--force-exclude` a
    data root's script that changed would be linted by the narrowed run and
    skipped by the whole-tree one, and the two runs would disagree about it.
    """
    calls: list[tuple[str, ...]] = []

    def capture(*arguments: str, **_options: str) -> None:
        calls.append(arguments)

    monkeypatch.setattr(check, "uv", capture)
    excluded = check.non_code_roots(data_project)
    scope = ["notes/study.py", "src/app.py"]

    check.ruff_format_check(False, excluded, scope)
    check.ruff_lint_check(False, excluded, scope)
    check.ruff_format_check(False, excluded)
    check.ruff_lint_check(False, excluded)

    narrowed, whole = calls[:2], calls[2:]
    assert all("--force-exclude" in call for call in narrowed)
    assert all("notes/study.py" in call for call in narrowed)
    assert all("--force-exclude" not in call for call in whole)
