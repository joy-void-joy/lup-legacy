"""What the gate's pytest step does with the test roots a project declares.

The gate runs one pytest per declared root, and everything it decides before
that run is decided from a declaration rather than from this repository: which
flags the environment can answer for, whether the directory a root names is
even there, and which suite installs a path somebody named. The first two were
read from what the template happens to hold, so both were green here and broken
for every project that installed the library.
"""

import os
import shutil
import sys
from pathlib import Path

import pytest
import sh
import typer

import lup.devtools.dev.check as check


def test_the_worker_flag_is_offered_where_the_suite_declares_it_takes_it() -> None:
    root = check.TestRoot(name="pytest", directory=Path(), parallel=True)

    assert root.spread(4) == ["-n", "4", "--dist", "worksteal"]


def test_no_worker_flag_where_the_suite_declares_it_runs_serially() -> None:
    assert (
        check.TestRoot(name="pytest", directory=Path(), parallel=False).spread(4) == []
    )


def test_one_worker_spells_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    # The count is the off switch: a worker count no higher than one is the
    # same run without the interpreter boot, so nothing else has to say it —
    # and nothing is asked of the suite's environment to find that out.
    monkeypatch.setattr(check, "uv", refuse_probe)
    root = check.TestRoot(name="pytest", directory=Path())

    assert root.spread(1) == []
    assert root.spread(0) == []


def refuse_probe(*_: object, **__: object) -> None:
    raise AssertionError("the suite's environment was asked about a serial run")


def test_an_undeclared_suite_asks_its_own_environment_where_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suite runs under `uv run` from its own directory, so that is where to ask.

    Asked in the gate's interpreter, a nested project's suite would be handed
    `-n` by a gate whose own environment holds pytest-xdist, and the nested
    pytest, which does not, refuses the flag before collecting a test.
    """
    asked: list[object] = []  # lup: ignore[empty-collection] — call record

    def answered(*arguments: str, **options: object) -> None:
        asked.append((arguments[:2], options["_cwd"]))

    monkeypatch.setattr(check, "uv", answered)

    spread = check.TestRoot(name="pytest (studio)", directory=tmp_path).spread(4)

    assert spread == ["-n", "4", "--dist", "worksteal"]
    assert asked == [(("run", "python"), str(tmp_path))]


def test_a_suite_whose_environment_lacks_xdist_runs_serially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An adopter installs the library's dependencies, which carry no
    # pytest-xdist, and pytest rejects `-n` before collecting anything. A
    # pytest row passing `-n` would then read FAIL on every branch whatever
    # the suite did, which is indistinguishable from a real regression.
    def absent(*arguments: str, **_: object) -> None:
        raise sh.ErrorReturnCode_1(" ".join(arguments), b"", b"")

    monkeypatch.setattr(check, "uv", absent)

    assert check.TestRoot(name="pytest", directory=tmp_path).spread(4) == []


def test_the_probe_reads_the_real_environment_of_the_suite(tmp_path: Path) -> None:
    """Measured against uv itself: the library's suite has xdist, a bare project not.

    The bare project depends on nothing and asks for the interpreter running
    this test, so `uv run` builds its environment without reaching an index.
    """
    library = Path(__file__).resolve().parents[2]
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "pyproject.toml").write_text(
        '[project]\nname = "bare"\nversion = "0"\n'
        f'requires-python = ">={sys.version_info.major}.{sys.version_info.minor}"\n',
        encoding="utf-8",
    )

    assert check.TestRoot(name="pytest (lup)", directory=library).spread(2)
    assert check.TestRoot(name="pytest (bare)", directory=bare).spread(2) == []


def test_a_root_whose_directory_is_missing_reports_instead_of_raising(
    tmp_path: Path,
) -> None:
    # The template declares a root at `packages/lup`, which an adopter
    # inherits and does not hold. `sh` changes directory in the forked child,
    # so the failure arrives as a fork exception rather than an exit status:
    # left to escape the gate, it hands the operator a traceback where a
    # verdict belongs, with the checks that had already passed never reported.
    root = check.TestRoot(name="pytest (lup)", directory=tmp_path / "packages/lup")

    report = root.checked(4, [])

    assert not report.passed
    assert report.name == "pytest (lup)"
    assert str(root.directory) in report.lines[0]


def test_a_missing_root_names_the_declaration_rather_than_the_checker(
    tmp_path: Path,
) -> None:
    # "A declared root does not exist" and "its suite failed" send the reader
    # to different places, so the row says which of the two it is.
    report = check.TestRoot(name="pytest (lup)", directory=tmp_path / "gone").absent()

    assert "test_roots" in " ".join(report.lines)


def test_a_root_that_names_a_file_is_no_root(tmp_path: Path) -> None:
    # Not a directory is not a directory: the fork would fail the same way.
    named = tmp_path / "tests"
    named.write_text("", encoding="utf-8")

    assert not check.TestRoot(name="pytest", directory=named).checked(4, []).passed


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root enters a directory whatever its mode says"
)
def test_a_root_that_cannot_be_entered_is_reported_rather_than_forked_out(
    tmp_path: Path,
) -> None:
    # The directory is there, so no declaration is wrong and no guard reading
    # one would see this. `sh` enters it in the forked child, and a failure
    # there is a fork exception rather than an exit status — the class the
    # missing-directory guard answers one instance of. A gate that crashes on a
    # condition of the environment reports nothing at all, including the
    # checks that had already passed.
    barred = tmp_path / "barred"
    barred.mkdir()
    barred.chmod(0o000)
    try:
        report = check.TestRoot(name="pytest (lup)", directory=barred).checked(4, [])
    finally:
        barred.chmod(0o700)

    assert not report.passed
    assert report.lines[0] == "pytest (lup): FAIL (never started)"
    assert any("Permission denied" in line for line in report.lines)


def declared_roots(workspace: Path) -> list[check.TestRoot]:
    """The two-suite shape the template declares, rooted in a temporary tree."""
    return [
        check.TestRoot(name="pytest", directory=workspace),
        check.TestRoot(name="pytest (lup)", directory=workspace / "packages/lup"),
    ]


def test_paths_from_two_suites_become_two_invocations(tmp_path: Path) -> None:
    # Named in one pytest run, the two suites both claim `tests.conftest` and
    # collection dies before a test runs — so the narrowing move a caller
    # reaches for, run the suites I touched, is served one suite at a time.
    grouped = check.group_by_root(
        declared_roots(tmp_path),
        [
            str(tmp_path / "packages/lup/tests/unit/test_profiles.py"),
            str(tmp_path / "tests/unit/test_harness_compilation.py"),
        ],
    )

    assert [group.root.name for group in grouped] == ["pytest", "pytest (lup)"]
    assert [group.paths for group in grouped] == [
        ["tests/unit/test_harness_compilation.py"],
        ["tests/unit/test_profiles.py"],
    ]


def test_a_nested_suite_claims_what_sits_under_it(tmp_path: Path) -> None:
    # The workspace root contains the package root, so read in declaration
    # order it would claim every path and run the library's tests from a
    # directory where `src` is the application's.
    grouped = check.group_by_root(
        declared_roots(tmp_path), [str(tmp_path / "packages/lup/tests/unit")]
    )

    assert [group.root.name for group in grouped] == ["pytest (lup)"]
    assert grouped[0].paths == ["tests/unit"]


def test_naming_nothing_asks_every_suite_for_all_of_itself(tmp_path: Path) -> None:
    grouped = check.group_by_root(declared_roots(tmp_path), [])

    assert [group.paths for group in grouped] == [[], []]


def test_a_path_under_no_declared_suite_is_refused(tmp_path: Path) -> None:
    # Silently dropping it would report a green run over tests nobody ran.
    with pytest.raises(typer.BadParameter):
        check.group_by_root(declared_roots(tmp_path / "workspace"), [str(tmp_path)])


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun is not installed here")
def test_a_bun_root_runs_the_workspace_tests_and_carries_their_report(
    tmp_path: Path,
) -> None:
    """The frontend's tests are a suite of the gate: green where they pass,
    and a failure carries bun's own report, which it writes to stderr."""
    workspace = tmp_path / "web"
    # Dependencies present and no lockfile to be behind: nothing to restore.
    (workspace / "node_modules").mkdir(parents=True)
    (workspace / "passing.test.ts").write_text(
        'import { expect, test } from "bun:test";\n'
        'test("holds", () => { expect(1 + 1).toBe(2); });\n',
        encoding="utf-8",
    )
    root = check.BunTestRoot(name="bun test", directory=workspace)

    assert root.checked(4, []).passed

    (workspace / "failing.test.ts").write_text(
        'import { expect, test } from "bun:test";\n'
        'test("breaks", () => { expect(1 + 1).toBe(3); });\n',
        encoding="utf-8",
    )
    report = root.checked(4, [])

    assert not report.passed
    assert report.lines[0] == "bun test: FAIL"
    assert any("breaks" in line for line in report.lines)


def test_a_path_under_the_bun_workspace_is_owned_by_the_bun_root(
    tmp_path: Path,
) -> None:
    roots = [
        check.TestRoot(name="pytest (lup)", directory=tmp_path / "packages/lup"),
        check.BunTestRoot(name="bun test", directory=tmp_path / "packages/lup/web"),
    ]
    named = tmp_path / "packages/lup/web/src/explorer/narrow.test.ts"

    assert check.owning_index(roots, named) == 1
    assert check.owning_index(roots, tmp_path / "packages/lup/tests/test_x.py") == 0


def test_a_bun_root_restores_its_workspace_before_the_tests_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `uv run pytest` gets from uv, the bun row does itself: the
    lockfile is restored first, then the tests run against it."""
    workspace = tmp_path / "web"
    workspace.mkdir()
    steps: list[str] = []  # lup: ignore[empty-collection] — order record

    def restore(directory: Path) -> bool:
        assert directory == workspace
        steps.append("restore")
        return True

    def bun(*args: str, **_options: object) -> None:
        steps.append(args[0])

    monkeypatch.setattr(check, "restore_dependencies", restore)
    monkeypatch.setattr(check, "BUN", bun)

    assert check.BunTestRoot(name="bun test", directory=workspace).checked(4, [])

    assert steps == ["restore", "test"]


def test_a_restore_that_fails_is_the_bun_rows_verdict_naming_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "web"
    workspace.mkdir()

    def refuse(directory: Path) -> bool:
        raise RuntimeError(
            f"`bun install --frozen-lockfile` in {directory} failed:\n"
            "error: lockfile had changes, but lockfile is frozen"
        )

    monkeypatch.setattr(check, "restore_dependencies", refuse)

    report = check.BunTestRoot(name="bun test", directory=workspace).checked(4, [])

    assert not report.passed
    assert report.lines[0] == "bun test: FAIL"
    assert any("bun install --frozen-lockfile" in line for line in report.lines)
    assert any("lockfile is frozen" in line for line in report.lines)


def test_only_the_bun_root_names_a_workspace_to_restore(tmp_path: Path) -> None:
    pytest_root = check.TestRoot(name="pytest", directory=tmp_path)
    bun_root = check.BunTestRoot(name="bun test", directory=tmp_path / "web")

    assert pytest_root.restored_workspaces() == []
    assert bun_root.restored_workspaces() == [tmp_path / "web"]


def test_the_bun_root_collects_tests_beside_their_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files bun runs are the files the policy gives the test role.

    Bun collects four stems over four extensions from the workspace down,
    so the role names each as a file pattern under the workspace.
    """
    monkeypatch.chdir(tmp_path)
    bun_root = check.BunTestRoot(name="bun test", directory=Path("web"))

    assert len(bun_root.collected()) == 16
    assert Path("web/**/*.test.tsx") in bun_root.collected()
    assert Path("web/**/*_spec.js") in bun_root.collected()

    roles = check.collected_test_roles([bun_root])
    assert [role.root for role in roles] == bun_root.collected()
    assert {role.role for role in roles} == {"test"}


def suite_configured(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (
            "pyproject.toml",
            '[tool.pytest.ini_options]\ntestpaths = ["tests", "checks"]\n',
        ),
        ("pyproject.toml", '[tool.pytest]\ntestpaths = ["tests", "checks"]\n'),
        ("pytest.toml", '[pytest]\ntestpaths = ["tests", "checks"]\n'),
        ("pytest.ini", "[pytest]\ntestpaths = tests checks\n"),
        ("tox.ini", "[pytest]\ntestpaths =\n    tests\n    checks\n"),
        ("setup.cfg", "[tool:pytest]\ntestpaths = tests checks\n"),
    ],
)
def test_a_pytest_root_collects_the_testpaths_its_configuration_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, text: str
) -> None:
    """Every file pytest reads its configuration from, in the spelling each takes.

    A pytest root that derived nothing left adlib's nested `studio/tests`
    judged as production: every test longer than three lines went to the
    operator as a size question.
    """
    monkeypatch.chdir(tmp_path)
    suite_configured(tmp_path / "studio", name, text)

    root = check.TestRoot(name="pytest (studio)", directory=Path("studio"))

    assert root.collected() == [Path("studio/tests"), Path("studio/checks")]


def test_the_first_configuration_pytest_would_read_is_the_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `pytest.ini` answers even where it says nothing, and the table in the
    # manifest beside it is then not pytest's configuration at all.
    monkeypatch.chdir(tmp_path)
    suite_configured(tmp_path, "pytest.ini", "[pytest]\n")
    suite_configured(
        tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )

    assert check.TestRoot(name="pytest", directory=Path()).collected() == [
        Path("**/test_*.py"),
        Path("**/*_test.py"),
        Path("**/conftest.py"),
    ]


def test_a_suite_without_testpaths_collects_by_its_file_patterns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    suite_configured(
        tmp_path / "tools",
        "pyproject.toml",
        '[tool.pytest.ini_options]\npython_files = "check_*.py"\n',
    )

    assert check.TestRoot(name="pytest", directory=Path("tools")).collected() == [
        Path("tools/**/check_*.py"),
        Path("tools/**/conftest.py"),
    ]


def test_a_root_spelled_from_the_working_directory_is_named_from_the_top(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The template spells its first root `Path.cwd()`; the policy reads patterns
    from the repository top, and a generated tree may hold no machine's path."""
    monkeypatch.chdir(tmp_path)
    suite_configured(
        tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )

    assert check.TestRoot(name="pytest", directory=tmp_path).collected() == [
        Path("tests")
    ]


def test_a_suite_outside_the_checkout_gives_nothing_a_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    suite_configured(
        tmp_path / "elsewhere", "pytest.ini", "[pytest]\ntestpaths = tests\n"
    )

    elsewhere = check.TestRoot(name="pytest", directory=tmp_path / "elsewhere")

    assert elsewhere.collected() == []


@pytest.mark.parametrize("name", ["pyproject.toml", "pytest.ini", "setup.cfg"])
def test_a_configuration_a_merge_holds_open_configures_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The role table compiles whenever the toolchain starts, conflicts or not.

    The CLI reads it on import, and resolving a conflicted manifest is done
    through that CLI; a decode error escaping here stopped `lup-devtools`
    starting at all, in the one checkout that needed it to.
    """
    monkeypatch.chdir(tmp_path)
    suite_configured(
        tmp_path,
        name,
        '[tool.pytest.ini_options]\n<<<<<<< ours\ntestpaths = ["tests"]\n'
        '=======\ntestpaths = ["checks"]\n>>>>>>> theirs\n',
    )

    assert check.TestRoot(name="pytest", directory=Path()).collected() == [
        Path("**/test_*.py"),
        Path("**/*_test.py"),
        Path("**/conftest.py"),
    ]


def test_a_named_path_nothing_answers_is_refused_by_name(tmp_path: Path) -> None:
    # Handed to pytest it collects nothing, and the run reports "no tests ran"
    # and a failed suite without saying which of the names is wrong.
    (tmp_path / "tests/unit").mkdir(parents=True)
    kept = tmp_path / "tests/unit/test_kept.py"
    kept.write_text("def test_kept() -> None:\n    pass\n", encoding="utf-8")
    gone = tmp_path / "tests/unit/test_gone.py"

    with pytest.raises(typer.BadParameter, match="test_gone.py"):
        check.run_selected(declared_roots(tmp_path), [str(kept), str(gone)], [])


def test_a_node_id_is_found_by_the_file_it_names(tmp_path: Path) -> None:
    named = tmp_path / "test_kept.py"
    named.write_text("def test_kept() -> None:\n    pass\n", encoding="utf-8")

    assert check.absent_selections([f"{named}::test_kept"]) == []
    assert check.absent_selections([f"{tmp_path / 'gone.py'}::test_kept"]) == [
        f"{tmp_path / 'gone.py'}::test_kept"
    ]


def test_integration_lifts_the_marker_the_configuration_deselects_by(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The suites' own configuration deselects `integration`, so an
    # integration test named outright ran nothing; the flag is the way to run it.
    ran: list[tuple[str, ...]] = []
    monkeypatch.setattr(check, "uv", lambda *words, **_options: ran.append(words))
    root = check.TestRoot(name="pytest", directory=tmp_path)

    root.run(["tests/integration"], 1, [], integration=True)
    root.run(["tests/unit"], 1, [])

    assert ran[0][-2:] == ("-m", "")
    assert "-m" not in ran[1]
