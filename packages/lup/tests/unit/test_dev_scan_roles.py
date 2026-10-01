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
    serial = check.TestRoot(name="pytest", directory=tmp_path, parallel=False)
    serial.checked(2, excluded)

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


class Listed:
    """A file inventory Git would hand the sweep, of whatever paths a test names."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths

    def lines(self, *_args: str, **_options: str) -> list[str]:
        return self.paths

    def __call__(self, *args: str, **_options: str) -> str:
        separator = "\0" if "-z" in args else "\n"
        return separator.join([*self.paths, ""])


def test_the_sweep_reads_each_file_for_the_rules_reaching_its_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test is read for its prose, a page for its prose, a generated page not at all.

    A rule names the roles it reaches, and the prose rule reaches a test as
    well as production. A page generation wrote is judged at the passage it
    was rendered from, so it is not read twice — and its rule reference,
    which quotes every example a rule refuses, is not read as prose.
    """
    files = {
        "src/app.py": "value = 1\n",
        "tests/test_app.py": '"""The app answers."""\n',
        "guide.md": "The app answers.\n",
        "docs/page.md": (
            "<!-- Generated from app.page by `make page` — edit the source, "
            "not this file. See docs/harness.md. -->\n\n# Page\n"
        ),
        "notes/jobs/generated.py": "payload = 'large'\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tracked, "git", Listed(list(files)))
    project = DevProject(
        package="app",
        path_roles=[
            PathRoleRow(root="tests", role="test"),
            PathRoleRow(root="notes", role="data"),
        ],
    )

    scanned = {
        item.rel: [rule.id for rule in item.patterns]
        for item in antipatterns.scanned_files(project)
    }

    assert sorted(scanned) == ["guide.md", "src/app.py", "tests/test_app.py"]
    assert scanned["tests/test_app.py"] == ["historical-voice"]
    assert scanned["guide.md"] == ["historical-voice"]
    assert "historical-voice" in scanned["src/app.py"]
    assert len(scanned["src/app.py"]) > 1
