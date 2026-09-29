"""A nested uv project, declared once, and checked by every gate by construction.

The fixture is the shape adlib met: a root project on Python 3.14 holding
`studio/`, its own uv project on 3.13 with its own environment, its own suite,
and scratch scripts run with `uv run --project studio`. Before the
declaration existed, every studio import failed Pyright until somebody
hand-edited `[tool.pyright]`, the gate ran no studio test, the rule scan named
studio's modules `studio.src.studio.*`, and the studio's tests were judged as
production source.

Each test reads what the declaration compiled to — the rewritten
`pyproject.toml`, the real Pyright and Ruff run over the fixture, the suite's
collected files — rather than what a writer said it did.
"""

import json
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
import sh

import lup.devtools.dev.check as check
from lup.devtools.dev.subprojects import SubProject, SubProjects, write_sub_projects
from lup.devtools.dev.worktree import SyncedSubProject
from lup.harness.codescan.common import module_name
from lup.types import JsonValue

ROOT_HEAD = """\
[project]
name = "adlibish"
version = "0.1.0"
requires-python = ">=3.14"

[tool.lup]
agent_version = "0.1.0"

[tool.pyright]
include = ["src", "tests"]
pythonVersion = "3.14"

[[tool.pyright.executionEnvironments]]
root = "scripts"
extraPaths = ["scripts/vendor"]
"""

RUFF_TABLE = """
[tool.ruff]
target-version = "py314"
"""

ROOT_PYPROJECT = ROOT_HEAD + RUFF_TABLE

STUDIO_BY_HAND = """
[[tool.pyright.executionEnvironments]]
root = "studio"
extraPaths = ["studio/.venv/lib/python3.13/site-packages"]
"""
"""The environment adlib wrote by hand before a declaration could compile it."""

STUDIO_PYPROJECT = """\
[project]
name = "studio"
version = "0.1.0"
requires-python = ">=3.13,<3.14"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""

TEMPLATE_STRING = 'name = "studio"\ngreeting = t"hello {name}"\n'
"""Python 3.14 syntax, which a checker told the file runs on 3.13 refuses."""

STUDIO = SubProject(root=Path("studio"), python="3.13")
DECLARED = SubProjects(projects=[STUDIO], environments=[".venv", ".venv-contained"])

SEARCHED = [
    "studio/src",
    "studio/.venv/lib/python3.13/site-packages",
    "studio/.venv-contained/lib/python3.13/site-packages",
]
COMPILED = [
    {"root": "studio", "pythonVersion": "3.13", "extraPaths": SEARCHED},
    {"root": "tmp/studio", "pythonVersion": "3.13", "extraPaths": SEARCHED},
    {"root": "scripts", "extraPaths": ["scripts/vendor"]},
]
"""What `DECLARED` compiles to beside the project's own environment."""


def written(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A root project on 3.14 with a nested uv project on 3.13 beside it.

    The nested environment holds one third-party package the root's does not,
    `torchish`, so an import of it resolves exactly where the studio's
    environment is the one being read. Its site-packages also hold a module
    that fails type checking, which no gate should ever report: an
    environment is a build product, not source.
    """
    written(tmp_path / "pyproject.toml", ROOT_PYPROJECT)
    written(tmp_path / "src/adlibish/__init__.py", "")
    written(tmp_path / "studio/pyproject.toml", STUDIO_PYPROJECT)
    written(tmp_path / "studio/.python-version", "3.13\n")
    written(tmp_path / "studio/src/studio/__init__.py", "")
    written(tmp_path / "studio/src/studio/eyes/__init__.py", "")
    written(tmp_path / "studio/src/studio/eyes/grade.py", "CPU_USED: int = 4\n")
    written(tmp_path / "studio/tests/test_grade.py", "def test_grade() -> None: ...\n")
    site = tmp_path / "studio/.venv/lib/python3.13/site-packages"
    written(site / "torchish/__init__.py", "def zeros() -> int: ...\n")
    written(site / "broken/__init__.py", 'nothing: int = "wrong"\n')
    written(tmp_path / "studio/.venv/pyvenv.cfg", "home = /usr/bin\n")
    return tmp_path


def manifest(root: Path) -> dict[str, JsonValue]:
    with (root / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def pyright_settings(root: Path) -> dict[str, JsonValue]:
    match manifest(root):
        case {"tool": {"pyright": dict(settings)}}:
            return settings
        case _:
            raise AssertionError("no [tool.pyright]")


def test_a_sub_project_compiles_into_its_own_pyright_environment(
    repository: Path,
) -> None:
    """Its own Python, its own sources, and its own environment's packages.

    Pyright gives an execution environment no interpreter of its own, so the
    environment's site-packages are named directly — once per name a machine
    builds it under, because `UV_PROJECT_ENVIRONMENT` moves it and a committed
    file cannot know which one this machine used. A name nobody synced is a
    path Pyright skips.
    """
    write_sub_projects(DECLARED, repository)

    assert pyright_settings(repository)["executionEnvironments"] == COMPILED


def test_the_sub_project_joins_the_checked_tree_and_its_scratch_does_not(
    repository: Path,
) -> None:
    write_sub_projects(DECLARED, repository)

    assert pyright_settings(repository)["include"] == ["src", "tests", "studio"]


def test_ruff_reads_the_sub_project_at_its_own_python(repository: Path) -> None:
    write_sub_projects(DECLARED, repository)

    match manifest(repository):
        case {"tool": {"ruff": {"per-file-target-version": dict(versions)}}}:
            assert versions == {"studio/**": "py313", "tmp/studio/**": "py313"}
        case _:
            raise AssertionError("no per-file target version written")


def installed(name: str) -> Path:
    """A checker installed beside this interpreter, as the gate runs it."""
    located = shutil.which(name, path=str(Path(sys.executable).parent))
    if located is None:
        pytest.skip(f"{name} is not installed beside {sys.executable}")
    return Path(located)


def pyright_report(root: Path, *files: str) -> dict[str, list[str]]:
    """What the real Pyright says about each named file, read from its JSON."""
    finished = sh.Command(str(installed("pyright")))(
        "--outputjson", *files, _cwd=str(root), _ok_code=[0, 1]
    )
    reported: dict[str, list[str]] = {name: [] for name in files}
    for item in json.loads(str(finished))["generalDiagnostics"]:
        named = Path(item["file"]).relative_to(root.resolve()).as_posix()
        reported[named].append(item["message"])
    return reported


def test_pyright_resolves_the_sub_project_in_its_own_environment_and_nowhere_else(
    repository: Path,
) -> None:
    """The per-edit check runs this Pyright from the checkout root over one file.

    What resolves under `studio/` is exactly what its environment and its
    sources hold; the same import from the root project's tree is still
    unresolvable, because the root does not have the package.
    """
    importing = "import torchish\nfrom studio.eyes.grade import CPU_USED\n"
    written(repository / "studio/src/studio/run.py", importing)
    written(repository / "src/adlibish/run.py", importing)
    write_sub_projects(DECLARED, repository)

    reported = pyright_report(
        repository, "studio/src/studio/run.py", "src/adlibish/run.py"
    )

    assert reported["studio/src/studio/run.py"] == []
    assert len(reported["src/adlibish/run.py"]) == 2


def test_a_scratch_script_for_the_sub_project_is_checked_in_its_environment(
    repository: Path,
) -> None:
    """adlib's GPU check under `tmp/`, run with `uv run --project studio`.

    Checked against the root it reported seven blocking errors for a script
    that ran fine. Under `tmp/studio/` it is checked where it runs.
    """
    script = "import torchish\nfrom studio.eyes.grade import CPU_USED\n"
    written(repository / "tmp/studio/gpu_check.py", script)
    write_sub_projects(DECLARED, repository)

    assert pyright_report(repository, "tmp/studio/gpu_check.py") == {
        "tmp/studio/gpu_check.py": []
    }


def test_the_sub_project_is_checked_against_its_own_python(repository: Path) -> None:
    written(repository / "studio/src/studio/greet.py", TEMPLATE_STRING)
    written(repository / "src/adlibish/greet.py", TEMPLATE_STRING)
    write_sub_projects(DECLARED, repository)

    reported = pyright_report(
        repository, "studio/src/studio/greet.py", "src/adlibish/greet.py"
    )

    assert any("3.14" in message for message in reported["studio/src/studio/greet.py"])
    assert reported["src/adlibish/greet.py"] == []


def test_ruff_refuses_newer_syntax_only_in_the_sub_project(repository: Path) -> None:
    written(repository / "studio/src/studio/greet.py", TEMPLATE_STRING)
    written(repository / "src/adlibish/greet.py", TEMPLATE_STRING)
    write_sub_projects(DECLARED, repository)

    finished = sh.Command(str(installed("ruff")))(
        "check",
        "--output-format",
        "json",
        "studio/src/studio/greet.py",
        "src/adlibish/greet.py",
        _cwd=str(repository),
        _ok_code=[0, 1],
    )
    flagged = {
        Path(item["filename"]).relative_to(repository.resolve()).as_posix()
        for item in json.loads(str(finished))
    }

    assert flagged == {"studio/src/studio/greet.py"}


def test_the_gate_never_reads_the_sub_project_environment(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pyright walks an explicitly excluded tree's hidden directories too.

    The gate hands Pyright its own `exclude`, which switches off Pyright's
    default of skipping dot-directories — so the studio's environment, now
    inside the checked tree, would be analysed package by package. Its
    directory names are declared scratch wherever they sit, and the gate
    excludes scratch.
    """
    written(repository / "studio/src/studio/wrong.py", 'count: int = "four"\n')
    write_sub_projects(DECLARED, repository)
    pyright = installed("pyright")

    def direct(*arguments: str, **_: object) -> None:
        sh.Command(str(pyright))(*arguments[2:], _cwd=str(repository))

    monkeypatch.setattr(check, "uv", direct)
    monkeypatch.setattr(check, "project_root", lambda: repository)
    scratch = [role.root.as_posix() for role in DECLARED.environment_roles()]

    report = check.pyright_check(scratch)

    printed = "\n".join(report.lines)
    assert not report.passed
    assert "studio/src/studio/wrong.py" in printed
    assert "site-packages" not in printed


def test_every_environment_name_is_scratch_wherever_it_sits() -> None:
    assert [
        (role.root.as_posix(), role.role) for role in DECLARED.environment_roles()
    ] == [("**/.venv", "scratch"), ("**/.venv-contained", "scratch")]


def test_the_sub_project_suite_collects_by_its_own_testpaths(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """adlib named `HookPathRole("studio/tests", "test")` by hand to get this.

    Declared as a test root, the studio's tests stayed production: every
    test longer than three lines went to the operator as a size question.
    The role is read off the suite's own configuration, so the files the
    gate runs and the files the policy judges as tests are one answer.
    """
    monkeypatch.chdir(repository)

    [suite] = DECLARED.test_roots()
    roles = check.collected_test_roles([suite])

    assert suite.name == "pytest (studio)"
    assert suite.directory == Path("studio")
    assert [(role.root, role.role) for role in roles] == [
        (Path("studio/tests"), "test")
    ]


def test_a_sub_project_without_a_suite_declares_no_test_root() -> None:
    quiet = SubProjects(
        projects=[SubProject(root=Path("tools"), python="3.12", suite=False)]
    )

    assert quiet.test_roots() == []


def test_the_sub_project_suite_carries_its_declared_parallelism() -> None:
    serial = SubProject(root=Path("studio"), python="3.13", parallel=False)

    [suite] = SubProjects(projects=[serial]).test_roots()

    assert suite.spread(8) == []


def test_nested_modules_are_named_from_the_segment_after_src() -> None:
    """The name an import of the module spells, which every lookup is keyed by.

    Named `studio.src.studio.eyes.grade`, a constant another studio module
    uses as a parameter default was never cleared by `constant-declaration`:
    the importer's `studio.eyes.grade` found no module by that name.
    """
    assert module_name(Path("studio/src/studio/eyes/grade.py")) == "studio.eyes.grade"
    assert module_name(Path("studio/src/studio/__init__.py")) == "studio"
    assert module_name(Path("studio/tests/test_grade.py")) == "studio.tests.test_grade"


def test_the_writer_leaves_a_project_declaring_none_untouched(
    repository: Path,
) -> None:
    before = (repository / "pyproject.toml").read_bytes()

    write_sub_projects(SubProjects(), repository)
    write_sub_projects(SubProjects(), repository, check=True)

    assert (repository / "pyproject.toml").read_bytes() == before


def test_a_regeneration_changes_nothing_further(repository: Path) -> None:
    write_sub_projects(DECLARED, repository)
    once = (repository / "pyproject.toml").read_bytes()

    write_sub_projects(DECLARED, repository)
    write_sub_projects(DECLARED, repository, check=True)

    assert (repository / "pyproject.toml").read_bytes() == once


def test_a_manifest_behind_its_declaration_is_stale(repository: Path) -> None:
    with pytest.raises(RuntimeError, match="pyproject.toml"):
        write_sub_projects(DECLARED, repository, check=True)


def test_dropping_a_sub_project_takes_everything_it_compiled_with_it(
    repository: Path,
) -> None:
    """The project's own entries are what is left, exactly as it wrote them.

    What generation placed is recorded beside it, so dropping the declaration
    removes the entries rather than leaving them to look like decisions
    somebody made by hand.
    """
    before = manifest(repository)
    write_sub_projects(DECLARED, repository)

    write_sub_projects(SubProjects(), repository)

    assert manifest(repository) == before


def test_a_hand_written_environment_at_the_declared_root_is_replaced(
    repository: Path,
) -> None:
    """adlib's own `studio` environment becomes the compiled one, not a second one."""
    written(repository / "pyproject.toml", ROOT_HEAD + STUDIO_BY_HAND + RUFF_TABLE)

    write_sub_projects(DECLARED, repository)

    assert pyright_settings(repository)["executionEnvironments"] == COMPILED


def test_a_sub_project_is_named_from_the_repository_top() -> None:
    with pytest.raises(ValueError, match="relative"):
        SubProject(root=Path("/somewhere/studio"), python="3.13")


def test_the_gate_syncs_each_sub_project_before_pyright_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pyright reads a sub-project's packages off disk, so they must be there first.

    The sub-project's suite would build its environment too, but beside
    Pyright rather than before it — a fresh clone's gate would then report
    every third-party import unresolved or not, depending on which finished
    first. Inexact, as `uv run` syncs, so the gate never uninstalls what a
    worktree's own sync put there.
    """
    handed: list[tuple[tuple[str, ...], object]] = []

    def recorded(*arguments: str, **options: object) -> None:
        handed.append((arguments[:2], options.get("_cwd")))

    monkeypatch.setattr(check, "uv", recorded)
    monkeypatch.setattr(check, "project_root", lambda: tmp_path)

    check.pyright_check([], environments=[Path("studio")])

    assert handed == [
        (("sync", "--inexact"), str(tmp_path / "studio")),
        (("run", "pyright"), None),
    ]


def test_a_fresh_worktree_syncs_each_sub_project_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-edit check reads the sub-project's environment from its first edit.

    A sub-project the new tree does not hold — cut from a branch older than
    the declaration — has nothing to sync, which is the step done.
    """
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    step = SyncedSubProject(worktree=tmp_path, project=Path("studio"))
    assert step.satisfied()

    written(tmp_path / "studio/pyproject.toml", STUDIO_PYPROJECT)
    assert not step.satisfied()
    assert not step.required()

    (tmp_path / "studio/.venv").mkdir()
    assert step.satisfied()
