"""Unified pre-flight checks: ruff, pyright, pytest."""

import json
import os
import tomllib
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from tempfile import gettempdir
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter

import sh
import typer
from pydantic import BaseModel

from lup.execution.git import Repository
from lup.providers.settings_schema import unclassified_settings
from lup.providers.harness import (
    claude_prompt_renderer,
    codex_prompt_renderer,
    guidance_artifacts,
)
from lup.harness.codescan.markers import find_feedback
from lup.harness.coverage import coverage_gaps
from lup.harness.dependencies import reaches
from lup.harness.modules import unloaded_guidance
from lup.harness.notice import Notice
from lup.harness.models import (
    GUIDANCE_BUDGET,
    GuidanceBudget,
    GuidanceSection,
    HookPathRole,
    HookSet,
    document_byte_size,
)
from lup.devtools.hooks.classify import stopped_everyday
from lup.policy.assets.host import project_environment
from lup.policy.everyday import SESSION_SHAPES
from lup.workspace.paths import is_template_scaffold, project_root

from lup.devtools.dev.admission import Admission, admitted
from lup.devtools.launcher import project_python
from lup.devtools.dev.antipatterns import scan_antipatterns, scanned_files
from lup.devtools.project import DevProject
from lup.devtools.dev.boundaries import scan_application_placement
from lup.devtools.dev.branches import (
    detect_base_branch,
    get_integration_branch,
    unlanded_siblings,
)
from lup.devtools.dev.git_guards import GitGuard, compiled_guards, read_hooks
from lup.devtools.dev.worktree import OWNERSHIP_MERGE_DRIVER, MergeDriver
from lup.devtools.dev.cites import sweep_cites
from lup.devtools.dev.collection import PytestCollection
from lup.devtools.dev.comments import FoundComment, scan_tracked
from lup.devtools.dev.conflicts import conflict_blocks, staged_paths, staged_text
from lup.devtools.dev.tracked import tracked_files
from lup.devtools.dev.commands import CommandSurface
from lup.devtools.dev.documented import generated_files, unresolved
from lup.ledger.models import LedgerNode
from lup.ledger.store import LedgerLayout
from lup.devtools.dev.environment import foreign_installs
from lup.devtools.dev.gates import sweep_all
from lup.devtools.dev.migrations import (
    MigrationRecord,
    gate_base,
    undeclared_breaks,
)
from lup.devtools.dev.records import branches_awaiting_adoption, record_location
from lup.devtools.dev.reach import Spread
from lup.devtools.dev.scaffold import ScaffoldSource
from lup.devtools.dev.update import drift as carrier_drift
from lup.devtools.harness.drift import (
    RepositoryWriter,
    inspect_drift,
    report_stale,
    roster_gaps,
)
from lup.harness.generate import NativeHarnessComposition
from lup.devtools.utils import decode_stderr, uv
from lup.diagnostics import refuse
from lup.policy.kernel.diagnostic import devtools, step, way
from lup.execution.shell import git
from lup.web.build import BUN, restore_dependencies

# The suite waits on git subprocesses and hook scripts far more than it
# computes — its system time runs to roughly twice its user time — so it
# parallelizes well, and goes on doing so past the point a worker's own
# interpreter boot would be expected to cancel the return. Measured on a
# 32-core host, the root suite alone ran in 673s serial, 102s under 8 workers,
# 97s under 16, and 88s under 24: more workers is still winning at this cap.
#
# Capped regardless, because the gate is not one suite running alone. It puts
# both test roots and pyright on the host at once, so a count that saturates
# the machine in isolation buys its own suite's time out of the two phases
# beside it — and the gate costs whichever of the three finishes last, not the
# one that was tuned. The cap is what keeps one suite from starving the
# others. It is bounded by the core count too, because a number that suits
# this host oversubscribes a laptop.
TEST_WORKERS = min(16, os.process_cpu_count() or 8)


class CheckReport(BaseModel):
    """One check's verdict, and the lines it wants printed where it belongs.

    A check running beside its neighbours finishes whenever it finishes, so a
    gate echoing as it went would report in a different order every run. Each
    check hands its lines back instead, and the gate prints them in the order
    it declares them rather than the order they arrived.
    """

    name: str
    passed: bool = True
    lines: list[str]
    counted: bool = True
    """Whether the summary tallies this row. An advisory one reports and never
    gates: a note asking somebody for something is worth reading, not worth
    refusing a branch over."""

    elapsed: float = 0.0
    """Seconds this check spent, zero where nothing timed it.

    Carried on the row rather than printed as it goes, for the reason the
    lines are: checks finish out of order, so a timing echoed on completion
    would interleave differently every run and could not be read down the
    page. Zero is the honest reading for a row nobody timed — an in-process
    sweep costs what the gate's own wall time already accounts for, and a
    fabricated duration there would invite the reader to optimise a number
    that measures nothing."""


def ran(name: str, command: Callable[[], object], ok: str = "ok") -> CheckReport:
    """One external tool's verdict, carrying what it printed when it failed.

    A tool that never started is a verdict too. `sh` prepares the child before
    exec — the working directory among it — and what fails there arrives as a
    fork exception rather than an exit status, so it is a sibling of the class
    a caller catches rather than a kind of it. Escaping here takes the whole
    gate down: the checks that had already passed go unreported, and a
    condition of the environment reads as a crash in the checker. So the two
    are reported the same way and told apart by what the row says — as is a
    tool the check ran on its way to its own, the restore before `bun test`,
    which refuses in words naming the command and carrying its output.
    """
    started = perf_counter()
    try:
        command()
    except RuntimeError as error:
        return CheckReport(
            name=name,
            passed=False,
            lines=[f"{name}: FAIL", *str(error).splitlines()],
            elapsed=perf_counter() - started,
        )
    except sh.ErrorReturnCode as error:
        # Both streams, because the tools disagree about where a verdict
        # goes: pytest and the type checker write theirs to stdout, bun's
        # test runner to stderr.
        printed = [
            stream
            for stream in (
                error.stdout.decode().rstrip(),
                decode_stderr(error).rstrip(),
            )
            if stream
        ]
        return CheckReport(
            name=name,
            passed=False,
            lines=[f"{name}: FAIL", *printed],
            elapsed=perf_counter() - started,
        )
    except sh.ForkException as error:
        return CheckReport(
            name=name,
            passed=False,
            lines=[
                f"{name}: FAIL (never started)",
                *(f"  {line}" for line in str(error).strip().splitlines()),
            ],
            elapsed=perf_counter() - started,
        )
    return CheckReport(
        name=name, lines=[f"{name}: {ok}"], elapsed=perf_counter() - started
    )


def spent(reports: list[CheckReport], started: float) -> str:
    """What the gate cost, and which rows to attack to make it cost less.

    Appended to the tally rather than printed as a block of its own, because
    it is read in the same glance: a gate that passed in nine seconds and one
    that passed in nine minutes ask for different next moves, and a reader who
    has to hold a stopwatch to tell them apart will not.

    Every timed row is named, ranked. The gate runs its tools at once, so its
    wall time is the slowest of them rather than their sum — which is exactly
    why the ranking is the useful half: it says which single row the wall time
    is waiting on, and what would be waiting on next if that row got faster.
    An untimed row is left out because it has nothing to say, not to keep the
    line short.
    """
    ranked = sorted(
        (report for report in reports if report.elapsed),
        key=lambda report: report.elapsed,
        reverse=True,
    )
    costs = ", ".join(f"{report.name} {report.elapsed:.0f}s" for report in ranked)
    return f" in {perf_counter() - started:.0f}s" + (f" — {costs}" if costs else "")


def non_code_roots(project: DevProject) -> list[str]:
    """Retained data and disposable scratch that no code check should read."""
    return [
        row["root"] for row in project.path_roles if row["role"] in ("data", "scratch")
    ]


def option_arguments(option: str, values: list[str]) -> list[str]:
    """Repeat one CLI option once for every value it carries."""
    return [argument for value in values for argument in (option, value)]


def ruff_format_check(
    fix: bool, excluded_roots: list[str], scope: list[str] | None = None
) -> CheckReport:
    """Whether every file is formatted — or, with *fix*, formatting them.

    An empty *scope* is the whole tree rather than nothing, which is what the
    dot has always meant here. A caller narrowing to a list it computed hands
    that list; a caller narrowing to nothing wants nothing checked and says so
    by not narrowing at all.
    """
    return ran(
        "ruff format",
        lambda: uv(
            "run",
            "ruff",
            "format",
            *([] if fix else ["--check"]),
            *option_arguments("--exclude", excluded_roots),
            *(scope or ["."]),
        ),
        "applied" if fix else "ok",
    )


def ruff_lint_check(
    fix: bool, excluded_roots: list[str], scope: list[str] | None = None
) -> CheckReport:
    """Whether the lint rules hold — or, with *fix*, applying what they can."""
    return ran(
        "ruff check",
        lambda: uv(
            "run",
            "ruff",
            "check",
            *option_arguments("--exclude", excluded_roots),
            *(scope or ["."]),
            *(["--fix"] if fix else []),
        ),
    )


def pyright_base_configuration(root: Path) -> Path | None:
    """The configuration Pyright would discover from the repository root."""
    json_config = root / "pyrightconfig.json"
    if json_config.is_file():
        return json_config
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    with pyproject.open("rb") as stream:
        settings = tomllib.load(stream)
    match settings:
        case {"tool": {"pyright": _}}:
            return pyproject
        case _:
            return None


# lup: ignore[constant-declaration] — an identity this repository defines: the
# name lup's own scratch file answers to, which the writer and the sweep must
# spell alike for one to find the other
PYRIGHT_SCRATCH = ".lup-pyright-"
"""What this gate's generated Pyright configuration is named at the root.

A prefix rather than a fixed name: concurrent checks each hold their own, and
two runs sharing one path would have the second rewrite what the first handed
Pyright mid-analysis.
"""


def sweep_pyright_scratch(root: Path, older_than: timedelta) -> list[Path]:
    """Remove generated Pyright configurations nothing is still running against.

    The configuration has to sit at the project root — Pyright resolves a
    base's relative ``include`` and each ``executionEnvironments`` root against
    the file that declares them, so one written elsewhere analyses a different
    tree. Measured rather than assumed: extending this repository's own base
    from a temporary directory analysed 1092 files and reported 60 missing
    imports that are not missing.

    Living at the root means a run that is killed rather than returned from
    leaves its file behind — the `finally` that unlinks it never executes —
    and they accumulate as untracked junk that every later `git status` and
    every drift check reports. This checkout held three, the oldest eleven
    days.

    Swept by age because the alternative is worse. Several sessions check this
    repository at once, and a sweep of *every* such file would delete a
    configuration another session's Pyright is reading. An age no analysis
    reaches separates the two without asking who owns what.
    """
    now = datetime.now(UTC)
    stale = [
        path
        for path in root.glob(f"{PYRIGHT_SCRATCH}*.json")
        if now - datetime.fromtimestamp(path.stat().st_mtime, UTC) > older_than
    ]
    for path in stale:
        path.unlink(missing_ok=True)
    return stale


def pyright_check(
    excluded_roots: list[str],
    scope: list[str] | None = None,
    abandoned_after: timedelta = timedelta(hours=1),
    environments: Sequence[Path] = (),
) -> CheckReport:
    """Whether the code-bearing workspace type-checks.

    Narrowed by naming files beside the project, which is exact rather than a
    guess: Pyright resolves each named file's imports itself, so a scope is a
    statement about which files are *reported on* and not about which code was
    understood. That is what separates narrowing this from narrowing a test
    run — there is no dependency graph to reconstruct and therefore none to
    reconstruct wrongly.

    Handed no ``--threads``, so it checks on one core. Measured over lup and
    an adopter on a shared 32-core host, ``--threads 8`` mostly cut the wall
    time by a third to three quarters, for two to five times the CPU and three
    times the memory: each thread is a forked process holding its own program,
    half a gigabyte to a gigabyte of it. In the full gate that buys nothing —
    Pyright finishes well before the suites beside it, and the gate costs its
    slowest row — while the cores it would take come out of those suites. It
    is also why a gate run without its suites holds no slot; a threaded
    Pyright would be something the slots have to divide.

    ``abandoned_after`` is how long a generated configuration may go untouched
    before this treats it as a killed run's leavings. An hour because the
    longest analysis here is minutes and the shortest session is not, so the
    gap is wide enough that no live run is ever inside it — and overridable
    because it is a judgement about how long is long, which a slower tree
    would make differently.

    ``environments`` are the declared sub-projects, each synced before
    Pyright starts: their execution environments name the packages on disk,
    and the sub-project's own suite, which would build that environment
    too, runs beside Pyright rather than before it — so a fresh clone's
    gate would report every third-party import resolved or not by which
    finished first. Inexact, the way `uv run` syncs, so nothing a worktree's
    own sync installed is taken away; a sync that fails is this row's
    verdict, in uv's words.
    """
    root = project_root()
    sweep_pyright_scratch(root, abandoned_after)
    base = pyright_base_configuration(root)
    interpreter = project_python(root)
    python_arguments = (
        ["--pythonpath", str(interpreter)] if interpreter is not None else []
    )
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".lup-pyright-",
        suffix=".json",
        dir=root,
        delete=False,
    ) as stream:
        configuration = Path(stream.name)
        json.dump(
            {
                **({"extends": str(base)} if base is not None else {"include": ["."]}),
                "exclude": excluded_roots,
            },
            stream,
        )

    def checked() -> None:
        """Every sub-project's environment readied, then the one Pyright run."""
        for environment in environments:
            uv("sync", "--inexact", _cwd=str(root / environment))
        uv(
            "run",
            "pyright",
            "--project",
            str(configuration),
            *python_arguments,
            *(scope or []),
        )

    try:
        return ran("pyright", checked)
    finally:
        configuration.unlink(missing_ok=True)


def importable(directory: Path, module: str) -> bool:
    """Whether the environment `uv run` builds for *directory* can import *module*.

    Asked of that environment's own interpreter, from that directory, because
    that is where a suite runs: `uv run pytest` from the suite's root, which
    for a nested project is a different environment from the one running the
    gate, on a different Python. A uv that cannot build the environment
    answers no; the suite's own run then fails in uv's words, which say more
    than this could.
    """
    program = (
        "import importlib.util, sys; "
        f"sys.exit(importlib.util.find_spec({module!r}) is None)"
    )
    try:
        uv("run", "python", "-c", program, _cwd=str(directory))
    except (sh.ErrorReturnCode, sh.ForkException):
        return False
    return True


def ignored_arguments(excluded_roots: list[str]) -> list[str]:
    """The globs that keep collection out of retained data and scratch."""
    return option_arguments(
        "--ignore-glob",
        [pattern for root in excluded_roots for pattern in (root, f"{root}/**")],
    )


class TestRoot(BaseModel):
    """One independently installed test suite the project asks the gate to run."""

    name: str
    directory: Path
    parallel: bool | None = None
    """Whether this suite spreads over processes, where the project says.

    Unsaid, the suite's own environment is asked whether it holds
    pytest-xdist — the gate's environment is the wrong one to ask, since a
    nested project installs its own. A suite whose answer the project already
    knows says it here and spares every run the question."""

    def restored_workspaces(self) -> list[Path]:
        """The toolchain workspaces this suite restores from a lockfile before it runs.

        What a fresh worktree readies beside the environment `uv sync`
        builds. A pytest suite runs against that environment and restores
        nothing of its own, which is the answer for every suite but the
        bun workspace's.
        """
        return []

    def spelled(self) -> Path | None:
        """This suite's directory as the repository top spells it, None outside it.

        A root is named relative to the working directory the gate runs from,
        which is the top, or — as the template's first root is — as that
        directory itself. Either way a role pattern is read from the top, and
        a generated tree may carry no machine's absolute path, so both
        spellings become the one relative path. A suite outside the checkout
        holds nothing the policy judges.
        """
        located = self.directory.resolve()
        top = Path.cwd().resolve()
        return located.relative_to(top) if located.is_relative_to(top) else None

    def collected(self) -> list[Path]:
        """The files this suite collects as tests, as patterns from the repository top.

        What the policy reads to give those files the test role, so the suite
        the gate runs and the role table cannot name different files. A
        pytest suite collects by the ``testpaths`` its own configuration
        declares, read the way pytest reads it
        (:class:`~lup.devtools.dev.collection.PytestCollection`), so a
        nested project's tests are tests the moment its suite is declared.
        """
        spelled = self.spelled()
        if spelled is None:
            return []
        return PytestCollection.read(self.directory).patterns(spelled)

    def spread(self, workers: int) -> list[str]:
        """The flag that spreads this suite over processes, where it can take one.

        `-n` belongs to pytest-xdist, and a project building on this library
        has no reason to hold it: a package declares what it needs to run, and
        a dependency group installs for the project that writes it rather than
        for anyone depending on that project. Declaring the plugin would
        therefore either reach this library's own developers alone or push
        test parallelism into every adopter's runtime install, so the flag is
        offered where this suite's environment can import it and dropped where
        it cannot. Pytest rejects an unrecognized argument before collecting
        anything, and a gate that failed on that would be reporting on its own
        speed rather than on the suite.

        Fewer than two workers spells serial, so the count descends into
        running the same tests behind a single interpreter rather than needing
        a second way of saying nothing — and asks the environment nothing.

        Scheduled by work stealing rather than xdist's default, because a
        suite costs its busiest worker. The default hands each worker its
        share up front, and a share holding a module of git-driving tests left
        one worker running for a minute after the rest were idle — measured,
        the library suite's busiest worker at 1.7 to 2.8 times the median,
        where stealing held it to 1.1 to 1.3, and the template suite's from
        1.2 to 1.05.
        """
        if workers < 2:
            return []
        match self.parallel:
            case None:
                parallel = importable(self.directory, "xdist")
            case declared:
                parallel = declared
        return ["-n", str(workers), "--dist", "worksteal"] if parallel else []

    def absent(self) -> CheckReport:
        """The verdict a root naming a directory this checkout lacks earns.

        A different fact from a suite that failed, and the reader's next move
        differs: the declaration is wrong, or the tree it named is somewhere
        else. It is answered before the run rather than caught after it,
        because `sh` changes directory in the forked child — where the failure
        arrives as a fork exception rather than an exit status, escapes the
        handler that reads exit statuses, and takes the whole gate down with a
        traceback while the checks that had already passed go unreported.
        """
        return CheckReport(
            name=self.name,
            passed=False,
            lines=[
                f"{self.name}: FAIL (no directory at {self.directory})",
                f"  the '{self.name}' test root names a path this checkout does "
                "not hold — drop it from the project's declared `test_roots`, "
                "or point it at the suite it meant",
            ],
        )

    def basetemp(self) -> Path:
        """The temporary root this suite holds alone, named for where it runs.

        The gate runs the roots at once, and pytest picks its own by scanning
        `pytest-of-<user>` for the next free `pytest-N` — a read and a create
        with a gap between them, so two runners starting together take the
        same number and hand two suites one tree. What that looks like from
        inside is a fixture meeting a path some other suite's test made:
        `destination path 'origin.git' already exists`, raised by a clone that
        runs once.

        Derived from the directory rather than from the name, so two checkouts
        of this repository running their gates at the same time stay apart
        too — the name is the same in both and the path is not.
        """
        stamp = sha256(str(self.directory.resolve()).encode("utf-8")).hexdigest()
        return Path(gettempdir()) / f"lup-pytest-{stamp[:16]}"

    def run(
        self,
        paths: list[str],
        workers: int,
        excluded_roots: list[str],
        foreground: bool = False,
        integration: bool = False,
    ) -> None:
        """Run this suite over these paths, or the whole of it for none.

        From its own directory, because the library ships to an index without
        the application beside it: its suite runs where `src` is all it can
        see, and a library test reaching for a template fixture passes at the
        root and fails there, which is the only place that difference shows.
        ``foreground`` hands the runner's own output to the terminal as it
        arrives, for a caller who named one file and wants its report whole.
        ``integration`` lifts the marker expression the suite's configuration
        deselects by, last so it overrides the one ``addopts`` carries: an
        integration test named outright otherwise runs nothing.
        """
        uv(
            "run",
            "pytest",
            *paths,
            f"--basetemp={self.basetemp()}",
            *self.spread(workers),
            *ignored_arguments(excluded_roots),
            *(["-m", ""] if integration else []),
            _cwd=str(self.directory),
            _fg=foreground,
        )

    def checked(self, workers: int, excluded_roots: list[str]) -> CheckReport:
        """Whether this suite passes, run whole from its own root."""
        if not self.directory.is_dir():
            return self.absent()
        return ran(self.name, lambda: self.run([], workers, excluded_roots))


class BunTestRoot(TestRoot):
    """A bun workspace's own tests, run by `bun test` from the workspace.

    The frontend's pure functions — how a page narrows and searches a graph it
    holds — are held to the server's semantics here, beside the pytest
    suites, so a gate that is green ran them. The workspace's dependencies
    are restored first where they are behind its lockfile, as `uv run`
    restores the environment before pytest; a restore that fails is the
    row's verdict, naming the command with bun's output. Where bun is
    missing this fails the way the bundle build does: a repository that
    builds surfaces has the toolchain as a dependency of its gate.
    """

    shapes: tuple[str, ...] = ("*.test", "*_test", "*.spec", "*_spec")
    """The stems bun collects as tests, each over every one of `extensions`.

    Bun's own vocabulary, read from the workspace down and skipping
    `node_modules`; a workspace that collects otherwise says so here.
    """
    extensions: tuple[str, ...] = ("js", "jsx", "ts", "tsx")

    def collected(self) -> list[Path]:
        spelled = self.spelled()
        if spelled is None:
            return []
        names = (f"{shape}.{ext}" for shape in self.shapes for ext in self.extensions)
        return [spelled / "**" / name for name in names]

    def restored_workspaces(self) -> list[Path]:
        return [self.directory]

    def run(
        self,
        paths: list[str],
        workers: int,
        excluded_roots: list[str],
        foreground: bool = False,
        integration: bool = False,
    ) -> None:
        # Workers, the ignored roots and the marker are pytest's vocabulary;
        # bun runs the workspace's tests in its own way and reads none of them.
        del workers, excluded_roots, integration
        restore_dependencies(self.directory)
        BUN("test", *paths, _cwd=str(self.directory), _fg=foreground)


def collected_test_roles(test_roots: list[TestRoot]) -> list[HookPathRole]:
    """The test role, declared for every file the gate's suites collect.

    Derived from the suites rather than spelled beside them: a test file the
    gate runs is judged as a test by the policy — written whole without a
    question, held still by an acceptance guard — because the one
    declaration answers both. A second table naming the same files would
    drift from the first, and a file the gate ran that the policy budgeted
    as source is the disagreement this exists to rule out.
    """
    patterns = [pattern for root in test_roots for pattern in root.collected()]
    return [HookPathRole(root=pattern, role="test") for pattern in patterns]


class RootSelection(BaseModel):
    """One suite's share of what a caller named, spelled from inside that suite.

    Empty *paths* asks for everything the suite declares, which is what the
    root's own `testpaths` already says — so a caller who named nothing gets
    each suite whole rather than a selection of none.
    """

    root: TestRoot
    paths: list[str]


def owning_index(test_roots: list[TestRoot], selection: Path) -> int | None:
    """Which declared suite holds a named path, deepest root winning.

    A workspace root contains the package roots under it, so the outermost
    suite would claim every path if the roots were read in the order they
    were declared. The deepest one containing the path is the suite that
    installs it.
    """
    named = selection.resolve()
    deepest = sorted(
        range(len(test_roots)),
        key=lambda index: len(test_roots[index].directory.resolve().parts),
        reverse=True,
    )
    return next(
        (
            index
            for index in deepest
            if named.is_relative_to(test_roots[index].directory.resolve())
        ),
        None,
    )


def absent_selections(selections: list[str]) -> list[str]:
    """The named tests nothing on disk answers, spelled as they were named.

    A node id names its test after the file holding it, so the file is what
    is looked for. Handed a name nothing answers, pytest collected nothing,
    and the run reported "no tests ran" and a failed suite without saying
    which name was wrong.
    """
    return [
        selection
        for selection in selections
        # lup: ignore[string-split] — pytest's node-id separator, which pytest
        # exposes no parser for
        if not Path(selection.partition("::")[0]).exists()
    ]


def group_by_root(
    test_roots: list[TestRoot], selections: list[str]
) -> list[RootSelection]:
    """Split what a caller named into one invocation per suite that owns part.

    Two independently installed suites each own a top-level `tests` package,
    and one interpreter has one meaning for that name: a run naming a path
    from each has both suites claiming `tests.conftest`, and collection dies
    before a test runs. No import mode settles it, because the collision is in
    what the suites are rather than in how pytest finds them — the isolation
    that makes them separate installs is the same isolation that stops them
    sharing an interpreter. So the narrowing move a caller reaches for — run
    the suites I touched — is served by one run per suite instead.
    """
    owned: list[list[str]] = [[] for _ in test_roots]
    for selection in selections:
        index = owning_index(test_roots, Path(selection))
        if index is None:
            declared = ", ".join(str(root.directory) for root in test_roots)
            refuse(
                f"sits under no declared test root: {declared}",
                what=selection,
                code=2,
            )
        installed = test_roots[index].directory.resolve()
        owned[index].append(str(Path(selection).resolve().relative_to(installed)))
    return [
        RootSelection(root=root, paths=paths)
        for root, paths in zip(test_roots, owned, strict=True)
        if paths or not selections
    ]


def run_selected(
    test_roots: list[TestRoot],
    selections: list[str],
    excluded_roots: list[str],
    workers: int = TEST_WORKERS,
    integration: bool = False,
) -> None:
    """Run each named path in the suite that installs it, reporting per suite.

    Output reaches the terminal as it arrives rather than being carried back
    the way the gate carries it: a caller who named one file wants pytest's
    own failure report, and the gate's ordered summary exists for a run whose
    checks finish out of order.

    Admitted the way the gate is, holding one of the clone's slots across
    every suite it runs and spreading each over its share of *workers*. This
    is the gate's suite at the gate's width, and the command several agents
    run at once while their changes are moving; unadmitted, each of them
    opens a full-width suite beside whatever gate holds a slot, and the
    division the gate makes is undone by the runs it cannot see. The notices
    are said as they arise rather than after, because this output streams:
    a run queued behind four others says so before it waits, not after.
    The selection is read before a slot is asked for, so a path under no
    suite, or one nothing on disk answers, is refused at once rather than
    after a wait. ``integration`` runs what the suites deselect by marker.
    """
    absent = absent_selections(selections)
    if absent:
        refuse("nothing on disk answers it", what=", ".join(absent), code=2)
    groups = group_by_root(test_roots, selections)
    failed: list[str] = []
    with admitted(project_root(), workers, announce=Notice.say) as admission:
        for group in groups:
            if not group.root.directory.is_dir():
                for line in group.root.absent().lines:
                    typer.echo(line)
                failed.append(group.root.name)
                continue
            typer.echo(f"\n{group.root.name}  ({group.root.directory})")
            try:
                group.root.run(
                    group.paths,
                    admission.workers,
                    excluded_roots,
                    foreground=True,
                    integration=integration,
                )
            except sh.ErrorReturnCode:
                failed.append(group.root.name)
            except sh.ForkException as error:
                typer.echo(f"{group.root.name}: never started\n{str(error).strip()}")
                failed.append(group.root.name)
    if failed:
        typer.echo(f"\nFailed: {', '.join(failed)}")
        raise typer.Exit(1)


def inline_notes_lines(found: list[FoundComment], scaffold: bool = False) -> list[str]:
    """The inline-notes header and detail lines.

    Advisory rather than gating: a note is a standing request to somebody, and
    the tree is expected to carry open ones for as long as the work they name
    is open. Failing on them would make every branch red for a condition its
    author chose deliberately, so this reports and the reader decides. Their
    `deferred` lines render after the unresolved ones, carrying the gate a
    bracketed deferral stated, so what is still being asked reads first.

    Customization markers read two ways, and *scaffold* says which. In the
    scaffold itself they are inventory — counted, never listed, because a
    permanent wall of text would sit in front of the notes somebody is
    actually owed, and `dev todos` exists to walk them. In a repository that
    adopted the template they are decisions nobody has made yet, so they list
    like any other note. Advisory either way: a domain that means to leave one
    standing writes `# lup: defer:`, and that is the sentence it should have
    to write rather than a red branch it learns to ignore.
    """
    unresolved = [comment for comment in found if comment.kind in ("note", "solved")]
    deferred = [comment for comment in found if comment.kind == "defer"]
    customization = [comment for comment in found if comment.kind == "template"]
    counts = f"{len(unresolved)} unresolved"
    if deferred:
        counts += f", {len(deferred)} deferred"
    if customization:
        counts += f", {len(customization)} customization"
    lines = [f"inline notes: {counts} (advisory)"]
    lines.extend(
        f"  {comment.file}:{comment.start_line}-{comment.end_line}"
        for comment in unresolved
    )
    lines.extend(
        f"  {comment.deferral_label()} "
        f"{comment.file}:{comment.start_line}-{comment.end_line}"
        for comment in deferred
    )
    lines.extend(
        f"  customization {comment.file}:{comment.start_line}-{comment.end_line}"
        for comment in ([] if scaffold else customization)
    )
    return lines


def guidance_bytes(compositions: list[NativeHarnessComposition]) -> int:
    """The heaviest always-loaded document any runtime tree renders, in bytes.

    Read off the compiled artifacts rather than re-rendered from the parts,
    because ``reject_oversized_guidance`` refuses on ``artifact.content`` —
    banner included — and a row measuring anything else is not measuring the
    gate it reports for. Re-rendering read 260 bytes light, which is a window
    where the row says ok about a tree generation would refuse.
    """
    sizes = [
        document_byte_size(artifact.content)
        for composition in compositions
        for artifact in guidance_artifacts(composition.recipe.desired)
    ]
    if not sizes:
        raise ValueError(
            "no target renders an always-loaded guidance artifact, so its "
            "budget cannot be weighed — a runtime tree lost its "
            "'harness.guidance' artifact, or no targets were resolved."
        )
    return max(sizes)


def budget_reports(
    used: int, scaffold: bool, declined: list[GuidanceSection] | None = None
) -> list[CheckReport]:
    """Every verdict the guidance's weight earns, given what this repository is.

    The runtime ceiling always; the scaffold's share of it only while this
    repository is still the template, because only then is the document one
    somebody else inherits and only then is there a reservation to keep. The
    all-on row whenever there is prose this tree declines, because that is the
    only condition under which the number differs from the one above it.
    """
    return [
        guidance_budget_report(used),
        *([scaffold_budget_report(used)] if scaffold else []),
        *([roster_budget_report(used, declined)] if declined else []),
    ]


def roster_budget_report(
    used: int,
    declined: list[GuidanceSection],
    budget: GuidanceBudget = GUIDANCE_BUDGET,
) -> CheckReport:
    """Whether a project taking every module could load every module's prose.

    The row the two above it cannot stand in for. Both weigh the document this
    tree renders, and this tree is the lightest interesting composition of its
    own roster — a scaffold takes every module and loads the prose of only the
    ones it offers. So a module off by default can grow its section without
    either row moving, and the project that turns it on finds a document the
    runtime truncates.

    Measured as the tree's own weight plus the sections it declines, rather
    than by recomposing: the compiled artifact carries a banner the parts do
    not, and a recomposed measurement reads light by exactly that much.
    """
    return budget_report(
        "roster budget",
        used + sum(document_byte_size(section.text) for section in declined),
        budget.ceiling,
        f"every module's prose loaded, {len(declined)} section(s) this tree declines",
    )


def budget_report(name: str, used: int, ceiling: int, note: str) -> CheckReport:
    """One budget's verdict, in the sentence every budget row answers in.

    Both rows weigh the same document against a different ceiling, so the
    sentence is written once: what a reader learns to expect from one row
    holds for the other, and neither can drift into reporting a different set
    of facts than its neighbour. What differs is the note each appends, which
    is the only part its own ceiling makes it the authority on.
    """
    free = ceiling - used
    state = "ok" if free >= 0 else f"FAIL (over by {-free})"
    return CheckReport(
        name=name,
        passed=free >= 0,
        lines=[f"{name}: {state} — {used}/{ceiling} bytes, {note}"],
    )


def guidance_budget_report(
    used: int, budget: GuidanceBudget = GUIDANCE_BUDGET
) -> CheckReport:
    """Whether a session will load the whole document or a truncated one."""
    return budget_report(
        "guidance budget",
        used,
        budget.ceiling,
        f"{budget.ceiling - used} free",
    )


def scaffold_budget_report(
    used: int, budget: GuidanceBudget = GUIDANCE_BUDGET
) -> CheckReport:
    """Whether a scaffold has left its adopter room inside the runtime ceiling.

    A separate verdict from the budget row beside it, because the two ask
    different questions of the same number. That one asks whether a runtime
    will truncate this tree, which is true of every project. This one asks
    whether a repository still shipping as a template is spending guidance
    budget on itself that the domain adopting it will need for its own
    architecture and conventions — and there is no domain yet to notice.

    Gating rather than advisory: a reservation nobody has to honour is spent
    by the first section that wants the room, which is how the headroom
    disappeared before anyone declared one.

    A passing row states the room left, because the number a session needs
    before it writes is how much it may spend, and the reservation — which
    never moves — cannot tell it that. The failing row states the overage
    instead: a negative amount of room is what the overage already says.
    """
    free = budget.scaffold_ceiling - used
    reserved = f"{budget.template_headroom} reserved for the adopting domain"
    note = f"{free} free, {reserved}" if free >= 0 else reserved
    return budget_report("scaffold budget", used, budget.scaffold_ceiling, note)


def branch_record_reports(pending: list[str]) -> list[CheckReport]:
    """What lup's branch bookkeeping earns while it still sits in two places.

    Advisory rather than gating. Every read falls back to the shared config
    per field, so a clone that never adopts its records answers exactly as
    one that did: there is no defect here to refuse a branch over. The
    command that finishes it writes the shared git directory, which is the
    host's, so a gating row would be red in every worktree until somebody
    stood somewhere no session reaches — and a gate whose resting colour is
    red is a gate a reader stops reading.

    Nothing at all once no branch is left, rather than a permanent ok, for
    the same reason: this is one move with an end, and a row that can only
    say ok from then on is a line everybody learns to skip. The gate prints
    the lines a check hands back, so handing back no check is how a row
    leaves — the shape the borrowed-environment and unlanded-sibling rows
    already use.
    """
    if not pending:
        return []
    destination = record_location(pending[0]).parent
    return [
        CheckReport(
            name="branch records",
            counted=False,
            lines=[
                f"branch records: {len(pending)} branch(es) still recorded in "
                "the shared git config (advisory)",
                "  every read falls back to those keys, so nothing is broken",
                "  "
                + way(
                    step(
                        "move them, once per clone",
                        devtools("git", "worktree", "adopt-records"),
                    )
                ),
                f"  it writes the shared git directory's `{destination}/`, "
                "so it runs on the host",
            ],
        )
    ]


def migration_reports(
    project: DevProject,
    spread: Spread | None,
    base: str | None,
    record: MigrationRecord = MigrationRecord(),
) -> list[CheckReport]:
    """What this checkout owes the projects built on it, judged from ``base``.

    Asked only where one of those exists, because what makes a vanished name
    a broken import is somebody holding it. Gating rather than advisory — the
    commit that takes a capability is the one place that knows why, and a
    break landing without that leaves an adopter an unresolvable import and
    nothing to read. The whole gate and the narrowed one both ask it, each
    from its own base: removing a public name is a mistake a change makes
    in one file, and a narrowed run that skipped this row let two of them
    through to the whole gate.

    No base at all is said rather than exited: a checkout with nothing to
    read from is a fact about the clone, and a report that names it is what
    lets somebody fetch one.
    """
    match (spread, base):
        case (None, _):
            return []
        case (_, None):
            return [
                CheckReport(
                    name="declared migrations",
                    counted=False,
                    lines=[
                        "declared migrations: skipped — no base to judge from "
                        "(advisory)",
                        "  fetch the integration branch or `main` so a merge base "
                        "exists",
                    ],
                )
            ]
        case (_, str(judged)):
            owed = undeclared_breaks(project, judged, record)
            return [
                CheckReport(
                    name="declared migrations",
                    passed=not owed,
                    lines=[
                        f"declared migrations: FAIL ({len(owed)} gone with nothing "
                        "to read)",
                        *(f"  {capability.spelled()}" for capability in owed),
                        f"  {record.instruction(project_root())}",
                    ]
                    if owed
                    else ["declared migrations: ok"],
                )
            ]


def changed_paths(since: str) -> list[str]:
    """Every tracked path this tree changed since a ref, as posix strings.

    A ref git cannot resolve refuses the run rather than answering nothing.
    The two readings are indistinguishable once the exit status is dropped —
    an empty answer is exactly what a tree that changed nothing gives — and
    the scope this builds decides which files the blocking gates read. A
    mistyped ref would scope them to none of them and report ok.
    """
    try:
        named = git.lines("diff", "--name-only", since, _ok_code=[0])
    except sh.ErrorReturnCode as error:
        refuse(
            f"does not name a commit in this tree: {decode_stderr(error)}",
            what=f"--since {since}",
            code=2,
        )
    return [line for line in named if line]


def named_gate_base(named: str, option: str = "--base") -> str:
    """The commit a caller's own ref names, for judging what this branch did.

    The merge base rather than the tip. What a branch took away is judged from
    where it started, and a base that has moved on since carries changes this
    branch never made — read against the tip they come back as capabilities
    this branch removed, which is how naming `dev` directly reported 504 gone
    on a branch that had removed none. What a branch changed is the same
    question asked of files, and ``option`` is the flag the ref came through,
    for the refusal to name.

    A ref nothing resolves refuses the run. Answering nothing instead would be
    indistinguishable from a branch that removed nothing, which is the reading
    a mistyped ref most wants to be mistaken for.
    """
    try:
        found = git.out("merge-base", named, "HEAD", _ok_code=[0])
    except sh.ErrorReturnCode as error:
        refuse(
            "shares no history with this checkout, so there is nothing to judge a"
            f" change from: {decode_stderr(error)}",
            what=f"{option} {named}",
            code=2,
        )
    return found


class ChangeBase(BaseModel, frozen=True):
    """The commit a narrowed check reads this tree's changes from, and why that one."""

    commit: str
    reached: str
    """How it was found, as the report says it: which base, by what evidence."""


def change_base(named: str | None, integration: str) -> ChangeBase:
    """Where this branch's own changes start: a merge base, never a tip.

    A base's tip that moved on since the branch was cut carries changes the
    branch never made, and a diff against it reads them back as the branch's:
    108 files on a feature branch whose author touched a handful, among which
    nobody could find their own failure. So a named ref is taken as the merge
    base with it, and with none named, as the merge base with the base this
    branch records — the one `worktree create` wrote, or else the cut git
    logged, or else the nearest branch by topology, said as such.

    On the integration branch itself, or a checkout on no branch with none
    beside it, there is no base to leave from, and what changed is the work
    not yet committed.
    """
    if named is not None:
        return ChangeBase(
            commit=named_gate_base(named, "--since"),
            reached=f"the merge base with {named}",
        )
    current = Repository(Path.cwd()).branch()
    siblings = [
        branch
        for branch in git.lines("branch", "--format=%(refname:short)")
        if branch != current
    ]
    if not current or current == integration or not siblings:
        return ChangeBase(
            commit="HEAD",
            reached=f"HEAD, the work {current or 'here'} has not committed",
        )
    found = detect_base_branch(current)
    match found.source:
        case "recorded":
            evidence = f"the base {current} records"
        case "created":
            evidence = f"the branch git logged {current} as cut from"
        case "guessed":
            evidence = f"the nearest branch to {current}, guessed from topology"
    return ChangeBase(
        commit=found.merge_base,
        reached=f"the merge base with {found.name}, {evidence}",
    )


class ChangedScope(BaseModel, frozen=True):
    """What this tree changed since a ref, split by whether a scoped check reads it."""

    checked: list[str]
    """The Python files, which Ruff and Pyright answer about exactly."""

    unread: list[str]
    """Every other changed file, which no scoped check here reads."""


def changed_scope(since: str) -> ChangedScope:
    """Every file this tree changed since a ref, untracked ones included.

    Untracked is the half `git diff` does not report and an iterating check
    cannot afford to miss: a module written five minutes ago is exactly what
    its author is asking about, and a scope that silently left it out would
    answer "clean" about the one file in the tree nobody has read yet.

    Deleted paths are dropped, because a scope naming them hands a checker a
    file it cannot open and turns a narrowed run into an error about its own
    argument list. What is not Python is kept rather than dropped, so the run
    can say it went unread instead of implying it was checked.
    """
    named = {
        *changed_paths(since),
        *git.lines("ls-files", "--others", "--exclude-standard"),
    }
    present = sorted(path for path in named if Path(path).is_file())
    return ChangedScope(
        checked=[path for path in present if path.endswith(".py")],
        unread=[path for path in present if not path.endswith(".py")],
    )


def antipattern_report(
    project: DevProject, scope: Sequence[str] | None = None
) -> CheckReport:
    """The anti-pattern sweep's row: every missing or spurious marker, or ok.

    One row for the whole gate and for `--changed` alike, so a file landed by
    `cp` or by a merge -- neither of which an edit gate reads -- meets in the
    loop the same rules the gate refuses it by. ``scope`` narrows it to the
    files named, and ``None`` is the whole repository.
    """
    scan = scan_antipatterns(project, scope)
    blocking = [f for f in scan.findings if f.kind != "untyped"]
    refined = f", {len(scan.refuted)} refuted" if scan.refuted else ""
    advisory = len(scan.findings) - len(blocking)
    tail = f" ({advisory} untyped, advisory{refined})" if advisory else refined
    return CheckReport(
        name="antipatterns",
        passed=not blocking,
        lines=[
            f"antipatterns: FAIL ({len(blocking)} finding(s){refined})",
            *(
                f"  {f.file}:{f.line} [{f.kind} {f.rule_id or '(bare)'}] {f.message}"
                for f in blocking
            ),
        ]
        if blocking
        else [f"antipatterns: ok{tail}"],
    )


def conflict_marker_report(
    paths: list[str], read: Callable[[str], str | None]
) -> CheckReport:
    """Every file and line a merge's conflict block opens on, or ok.

    Gating: a block is a resolution nobody finished, whatever language the
    file is in, and nothing else this gate runs reads markdown or a page for
    one — a merge committed its markers into a passage and the page generated
    from it, and the whole gate passed. *read* answers a path's text, or
    ``None`` for one that does not read as text.
    """
    found = [
        f"  {path}:{line}"
        for path in paths
        for text in [read(path)]
        if text is not None
        for line in conflict_blocks(text)
    ]
    return CheckReport(
        name="conflict markers",
        passed=not found,
        lines=[
            f"conflict markers: FAIL ({len(found)} block(s) a merge left behind)",
            *found,
            "  resolve each, or excuse a fixture holding one on purpose with a "
            "`lup: ignore[conflict-marker]` line heading its paragraph",
        ]
        if found
        else ["conflict markers: ok"],
    )


def worktree_text(path: str) -> str | None:
    """A file's text as the working tree holds it, or ``None``."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def run_conflict_markers(staged: bool, index: Path | None = None) -> None:
    """Refuse what holds a conflict block: the tracked tree, or the next commit.

    *staged* reads what the index holds for the paths the next commit adds or
    changes, which is what the commit hook asks about — the working tree can
    already hold the resolution of a block the index still carries. *index*
    is the index the commit is made from where git made one for it -- `git
    commit -a`, `git commit <path>` -- as the hook hands it over; relative, it
    is spelled from the checkout's top, where git runs its hooks.
    """
    root = project_root()
    committed = None if index is None else root / index
    report = (
        conflict_marker_report(
            staged_paths(root, committed), partial(staged_text, root, committed)
        )
        if staged
        else conflict_marker_report(tracked_files(), worktree_text)
    )
    for line in report.lines:
        typer.echo(line)
    if not report.passed:
        raise typer.Exit(1)


def owned_comments(
    found: list[FoundComment], scope: list[str] | None
) -> list[FoundComment]:
    """Which unresolved notes this check is answerable for.

    A resolver worker's own notes are already cleared from its worktree
    before it starts, so every note it can still see belongs to a sibling
    concern it has no lease on. Reporting the whole tree would tell it about
    work it cannot touch; reporting what it changed says the only thing it
    can act on, which is whether it left a note in its own code.
    """
    if scope is None:
        return found
    owned = dict.fromkeys(scope)
    return [item for item in found if str(item.file) in owned]


def scan_reports(
    project: DevProject,
    scope: list[str] | None,
    compositions: list[NativeHarnessComposition],
    repository_writers: list[RepositoryWriter],
    git_guards: list[GitGuard],
    hooks_declaration: HookSet,
    node_classes: list[type[LedgerNode]] | None = None,
    ledger: LedgerLayout = LedgerLayout(),
    command_surface: Callable[[], CommandSurface] | None = None,
    scaffold_source: ScaffoldSource | None = None,
    spread: Spread | None = None,
    migration_base: str | None = None,
) -> list[CheckReport]:
    """Every check the gate answers itself, in the order it reports them.

    ``migration_base`` is the commit a caller named to judge removed
    capabilities from, in place of the one detection reaches on its own.
    """

    def reported() -> Iterator[CheckReport]:
        # advisory — a note asks somebody for something, and a tree is expected
        # to carry open ones; worth reading, not worth refusing over
        # gating — see conflict_marker_report
        yield conflict_marker_report(
            tracked_files() if scope is None else scope, worktree_text
        )

        found = owned_comments(scan_tracked(find_feedback), scope)
        scaffold = is_template_scaffold(project_root())
        yield CheckReport(
            name="inline notes",
            counted=False,
            lines=inline_notes_lines(found, scaffold)
            if found
            else ["inline notes: none"],
        )

        # gating — a deferral that stated a condition this checkout can resolve
        # is a question with an answer, and the answer turning yes is the one
        # moment the note was written for. Advisory is right for what somebody
        # still has to judge; this is the part nobody has to. Read from the
        # integration branch as well as from here, because a note about this
        # branch was written where its author stood and this checkout has no
        # copy of it.
        sweep = sweep_all(found)
        yield CheckReport(
            name="woken deferrals",
            passed=not sweep.woken,
            lines=sweep.lines(),
        )

        # Scoped where a scope was given, so the sweep reads the files this
        # tree is answerable for rather than reading every file and setting
        # most of the findings aside. A lease holds one concern's changes and
        # its gate answers "is this change good?" — a whole-repository read
        # made every lease's verdict depend on state no worker controls, and
        # cost the whole repository's resolve to reach it.
        yield antipattern_report(project, scope)

        # A document naming a node is held to what the node says now, so prose
        # cannot go on citing a corrected figure. Counted only where there is
        # a cite to hold: a repository with none has nothing this can fail.
        cited = sweep_cites(node_classes or [], ledger)
        yield CheckReport(
            name="cites",
            counted=bool(cited.checked or cited.failing),
            passed=cited.passed(),
            lines=cited.lines(),
        )

        # gating — a settings key nobody decided about stays inside a
        # contained session, which is safe and silently wrong for a
        # preference, so a key the CLI gained is a question the gate asks
        # rather than a default it keeps.
        undecided = unclassified_settings()
        yield CheckReport(
            name="settings flow",
            passed=not undecided,
            lines=[
                f"settings flow: FAIL ({len(undecided)} key(s) no flow decides)",
                *(f"  {key}" for key in undecided),
                "  Decide each in lup.providers.claude.preferences.",
            ]
            if undecided
            else ["settings flow: ok"],
        )

        portable = scan_application_placement(project)
        yield CheckReport(
            name="application placement",
            lines=[
                f"application placement: {len(portable)} portable module(s)",
                *(f"  {module.file}" for module in portable),
            ]
            if portable
            else ["application placement: ok"],
        )

        # advisory — a retirement is a decision, and a decision nobody meets
        # again becomes permanent by default while its roster grows
        retired = [
            f"{roster}: {name}"
            for roster, names in (
                ("sub-app", project.subapps.retired),
                ("module", project.modules.declined()),
                ("rule", project.rules.retired),
            )
            for name in names
        ]
        if retired:
            yield CheckReport(
                name="retired from lup",
                counted=False,
                lines=[
                    f"retired from lup: {len(retired)} (advisory)",
                    *(f"  {entry}" for entry in retired),
                ],
            )

        # Asked here because the guards cannot report their own absence: a
        # hooks directory git no longer finds silences every one of them at
        # once, and every other row goes on passing exactly as before. The
        # other direction is just as quiet — git runs whatever is at the path
        # whether or not anything still declares that moment — so a checkout
        # armed by an older declaration names what it still pays for.
        hooks = read_hooks(git_guards, project_root())
        unarmed = hooks.unarmed()
        yield CheckReport(
            name="git guards",
            passed=hooks.reachable,
            lines=[
                *(
                    [
                        f"git guards: FAIL (no hooks directory at {hooks.directory})",
                        "  git runs no hook from this checkout — every guard is off",
                        "  check `git config --show-origin --get core.hooksPath`",
                    ]
                    if not hooks.reachable
                    else [
                        f"git guards: ok ({len(hooks.guards) - len(unarmed)}/"
                        f"{len(hooks.guards)} armed)",
                        *(f"  {state.describe()}" for state in unarmed),
                    ]
                ),
                *(f"  {state.describe()}" for state in hooks.orphaned),
            ],
        )

        # The hooks run what the manifest compiles, which is read without
        # loading this declaration: were the two to differ, a hook would run a
        # guard nobody declares here, or stand down past one somebody does.
        held = compiled_guards(project_root())
        match held:
            case None:
                compiled = [
                    "compiled git guards: none (advisory) — each hook loads the "
                    "application to read the declaration"
                ]
            case list() if held == git_guards:
                compiled = ["compiled git guards: ok"]
            case _:
                compiled = [
                    "compiled git guards: FAIL (the manifest's [tool.lup] git-guards "
                    "is not the declaration the dev tree reads)",
                    "  compile the dev tree's own declaration with `write_git_guards`, "
                    "then run `uv run lup-devtools harness generate all`",
                ]
        yield CheckReport(
            name="compiled git guards",
            counted=held is not None,
            passed=held is None or held == git_guards,
            lines=compiled,
        )

        # Asked here for the reason the guards above are: git resolves a driver
        # name from config alone, which no repository can ship, so a checkout
        # that never ran `worktree create` reads the declaration, finds nothing
        # registered, and text-merges the generated trees without a word. What
        # that costs is the compiled dispatcher: conflict markers in a script
        # the runtime executes leave a boundary refusing every call in the
        # session, the merge abort included.
        registered = MergeDriver().satisfied()
        yield CheckReport(
            name="merge driver",
            passed=registered,
            lines=[
                f"merge driver: ok ({OWNERSHIP_MERGE_DRIVER})"
                if registered
                else f"merge driver: FAIL ({OWNERSHIP_MERGE_DRIVER} is unregistered)",
                *(
                    []
                    if registered
                    else [
                        "  the generated trees text-merge and can conflict",
                        "  "
                        + way(step("register it", devtools("git", "merge-driver"))),
                    ]
                ),
            ],
        )

        # The other declaration about a checkout only git can answer for: the
        # committed half of the log merges losslessly only where its journal
        # is declared `merge=union`, an attribute of the checkout rather than
        # of the code that reads it. The local half has nothing to ask.
        troubles = ledger.problems(project_root())
        yield CheckReport(
            name="ledger placement",
            passed=not troubles,
            lines=[
                f"ledger placement: {'FAIL' if troubles else 'ok'} ({ledger.describe()})",
                *(f"  {trouble}" for trouble in troubles),
            ],
        )

        # Beside the guards for the reason they are here: this is the other
        # thing about a clone that no file in the tree can settle, and it is
        # the only place the unfinished half of a move says so. The keys it
        # counts answer every read, so nothing else has any reason to speak.
        yield from branch_record_reports(branches_awaiting_adoption())

        # The one measurement of the shell vocabulary that reads the direction
        # a tightening shows up in. The recorded asks say which commands a
        # question was raised about, and the rule census says what each row
        # earns; neither would notice a de-escalation that stopped firing, so
        # a change putting a question in front of `git status` passes both.
        stopped = stopped_everyday(hooks_declaration)
        declared_count = sum(
            len(family.commands) for family in hooks_declaration.everyday_commands
        )
        swept = declared_count * len(SESSION_SHAPES)
        yield CheckReport(
            name="everyday commands",
            passed=not stopped,
            lines=[
                f"everyday commands: FAIL ({len(stopped)} stopped of {swept})",
                *(
                    line
                    for item in stopped
                    for line in (
                        f"  [{item.effect}] {item.command}",
                        f"    {item.what}, {item.shape} — {item.reason}",
                    )
                ),
            ]
            if stopped
            else [
                f"everyday commands: ok, {declared_count} allowed in "
                f"{len(SESSION_SHAPES)} session shapes"
            ],
        )

        # The other direction on the same subject. The sweep above asks whether
        # a command this project runs is still allowed; this asks whether a
        # command it *tells a reader to run* exists at all. Twenty-two did not,
        # including the one in the hooks workflow's own step 7, and each was
        # written beside the command it named — which is why neither the author
        # nor any reviewer caught it and a session typing it did. A declined
        # module's tree may still be named in its own hand-written files.
        written = (
            unresolved(
                command_surface().admits,
                project.subapps.retired,
                generated_files(project_root()),
            )
            if command_surface
            else []
        )
        yield CheckReport(
            name="documented commands",
            passed=not written,
            lines=[
                f"documented commands: FAIL ({len(written)} naming no command)",
                *(f"  {mention.named()}" for mention in written),
            ]
            if written
            else [
                "documented commands: ok"
                if command_surface
                else "documented commands: skipped, no CLI declared to walk"
            ],
        )

        # The same reading the commit hook and the pipeline refuse on, asked
        # here rather than recomposed, so a tree cannot be stale at one gate
        # and current at another. The summary names both halves because the
        # verdict reads both: a stale repository artifact with every tree
        # current is a failure whose tree count is zero, and a line saying so
        # tells a reader nothing at all — while `report_stale` writes the
        # detail to stderr, where a two-hundred-second run buries it.
        drift = inspect_drift(compositions, repository_writers)
        if not drift.clean:
            report_stale(drift)
        yield CheckReport(
            name="harness drift",
            passed=drift.clean,
            lines=drift.summary,
        )

        # The same question one carrier further out: that row asks whether the
        # trees match the declarations in this checkout, and this asks whether
        # the three carriers that brought the checkout here stand at one
        # upstream commit. Advisory, because both commits are ones this project
        # moved deliberately, and because the row is read most often during an
        # update a conflict interrupted — refusing then would bury the
        # conflicts the gate was run to read.
        carriers = (
            carrier_drift(project_root(), scaffold_source)
            if scaffold_source is not None and not is_template_scaffold(project_root())
            else None
        )
        if carriers is not None and not carriers.settled():
            yield CheckReport(
                name="carrier drift",
                counted=False,
                lines=[
                    f"carrier drift: {carriers.spelled()} (advisory)",
                    "  `dev update` moves every carrier to one upstream commit",
                ],
            )

        # The other direction on the same subject: that row is what this
        # checkout owes its upstream, and this is what it owes the projects
        # built on it. A caller's own `--base` stands in for detection here
        # and nowhere else: this is the one check that judges against a base
        # at all, so an override that reached further would be claiming to
        # scope checks it has nothing to do with.
        yield from migration_reports(
            project,
            spread,
            (migration_base or gate_base(get_integration_branch()))
            if spread is not None
            else None,
        )

        # Beside parity because both ask whether the roster arrived whole, one
        # turn further out: parity reads a declaration against the trees, and
        # this reads the checkout against the modules that were meant to
        # declare it. A subject nobody claims reaches neither of the others —
        # it renders into every tree, identically, forever.
        unclaimed = coverage_gaps(project_root(), project.coverage)
        yield CheckReport(
            name="module coverage",
            passed=not unclaimed,
            lines=[
                f"module coverage: FAIL ({len(unclaimed)} unclaimed)",
                *(f"  {gap.describe()}" for gap in unclaimed),
            ]
            if unclaimed
            else [
                "module coverage: ok, "
                f"{len(project.coverage.modules)} module(s) claim everything declared"
            ],
        )

        # The roster's other promise, read off the same modules: coverage asks
        # whether every declaration has one owner, and this whether what each
        # module names is something it stands on. A reach nobody declared is
        # met by whoever declines an unrelated module, as a generation that
        # refuses a skill they kept — so it is refused here instead, where the
        # module that reached is the one named.
        unmet = reaches(
            [
                project.coverage.selection.resolved(module)
                for module in project.coverage.modules
            ],
            [claude_prompt_renderer(), codex_prompt_renderer()],
            project.coverage.context,
            project.coverage.beside,
        )
        yield CheckReport(
            name="module independence",
            passed=not unmet,
            lines=[
                f"module independence: FAIL ({len(unmet)} undeclared)",
                *(f"  {reach.describe()}" for reach in unmet),
            ]
            if unmet
            else [
                "module independence: ok, "
                f"{len(project.coverage.modules)} module(s) name only what they "
                "stand on"
            ],
        )

        # Beside drift because a tree can be perfectly current against a source
        # that renders one target a skill short, and drift reads every tree as
        # clean while the two rosters have parted.
        gaps = roster_gaps(compositions)
        yield CheckReport(
            name="roster parity",
            passed=not gaps,
            lines=[
                f"roster parity: FAIL ({len(gaps)} gap(s))",
                *(f"  {gap.describe()}" for gap in gaps),
            ]
            if gaps
            else ["roster parity: ok"],
        )

        yield from budget_reports(
            guidance_bytes(compositions),
            scaffold,
            unloaded_guidance(project.coverage.modules, project.modules),
        )

        # advisory — the environment is the operator's arrangement rather than
        # this branch's, so a borrowed one is worth reading and not worth
        # refusing a merge over. It is reported at all because nothing else
        # would ever say it: a sync into a shared environment succeeds, and
        # the project it uninstalled finds out somewhere nobody touched.
        environment = project_environment(project_root())
        borrowed = (
            foreign_installs(project_root(), environment)
            if environment.is_dir()
            else []
        )
        if borrowed:
            yield CheckReport(
                name="borrowed environment",
                counted=False,
                lines=[
                    f"borrowed environment: {len(borrowed)} other project(s) "
                    "(advisory)",
                    f"  {environment}",
                    *(f"  holds {owner}" for owner in borrowed),
                    "  `dev env sync` refuses to write over them, "
                    "`dev env status` says why",
                ],
            )

        # advisory — reports another tree's state, so it never gates this one
        unlanded = unlanded_siblings()
        if unlanded:
            yield CheckReport(
                name="unlanded siblings",
                counted=False,
                lines=[
                    f"unlanded siblings: {len(unlanded)} (advisory)",
                    *(f"  {branch.name}  {branch.standing()}" for branch in unlanded),
                ],
            )

    return list(reported())


def run_checks(
    fix: bool,
    no_test: bool,
    project: DevProject,
    test_roots: list[TestRoot],
    compositions: list[NativeHarnessComposition],
    repository_writers: list[RepositoryWriter],
    git_guards: list[GitGuard],
    hooks_declaration: HookSet,
    command_surface: Callable[[], CommandSurface] | None = None,
    scope: list[str] | None = None,
    test_workers: int = TEST_WORKERS,
    node_classes: list[type[LedgerNode]] | None = None,
    ledger: LedgerLayout = LedgerLayout(),
    scaffold_source: ScaffoldSource | None = None,
    spread: Spread | None = None,
    migration_base: str | None = None,
    release_tag_prefix: str = "v",
    environments: Sequence[Path] = (),
) -> None:
    """Run ruff format, ruff check, pyright, pytest, and this gate's own sweeps.

    Read-only by default (reports issues without modifying files).
    Pass *fix* to auto-fix formatting and lint issues. ``scope`` narrows the
    note and anti-pattern gates to paths this tree is answerable for,
    ``migration_base`` names the commit removed capabilities are judged from,
    and ``environments`` are the sub-projects Pyright reads through their own.

    A gate with no suite to run says so in a row of its own, where the suites
    would have reported. A tally counting only what ran reads the same
    whether the tests passed or nobody declared any, and a project whose code
    all sits in a nested one met exactly that: every check passed while the
    nested suite, run by nobody, had been failing. Advisory, because declaring
    none is a choice the gate reports rather than refuses.
    """
    started = perf_counter()
    excluded_roots = non_code_roots(project)
    suites = [] if no_test else test_roots
    # One slot for the whole run, taken before any tool starts: the tools
    # start together, each suite reads its width as it launches, and a
    # run's share is settled when it opens rather than revised as others
    # come and go. What the share divides is the suites' workers alone —
    # Pyright is handed no `--threads`, so it checks on a single core
    # however many runs are on the machine — and a run opening no suite
    # takes no slot, since a slot it held would narrow every suite opening
    # beside it for the whole of that suite's run and divide nothing.
    held = (
        admitted(project_root(), test_workers)
        if suites
        else nullcontext(Admission(workers=test_workers))
    )
    with held as admission:
        tools: list[Callable[[], CheckReport]] = [
            partial(ruff_format_check, fix, excluded_roots),
            partial(ruff_lint_check, fix, excluded_roots),
            partial(pyright_check, excluded_roots, environments=environments),
            *(
                partial(root.checked, admission.workers, excluded_roots)
                for root in suites
            ),
        ]
        sweeps = partial(
            scan_reports,
            project,
            scope,
            compositions,
            repository_writers,
            git_guards,
            hooks_declaration,
            node_classes=node_classes or [],
            ledger=ledger,
            command_surface=command_surface,
            scaffold_source=scaffold_source,
            spread=spread,
            migration_base=migration_base,
        )

        if fix:
            # `--fix` rewrites the tree, so its tools go one at a time: a
            # formatter moving lines under a checker reading them answers about a
            # file that is no longer there.
            tooled = [tool() for tool in tools]
            changed = git.lines("diff", "--name-only", _ok_code=[0])
            if changed:
                lint = next(report for report in tooled if report.name == "ruff check")
                lint.lines.extend(
                    [
                        f"  auto-fixed {len(changed)} file(s)",
                        *(f"    {f}" for f in changed),
                    ]
                )
            scanned = sweeps()
        else:
            # Read-only, no check reads what another writes, so the gate costs its
            # slowest rather than the sum of all of them. Each tool waits on a
            # process of its own; the sweeps hold this thread while they do.
            with ThreadPoolExecutor(max_workers=len(tools)) as pool:
                running = [pool.submit(tool) for tool in tools]
                scanned = sweeps()
                tooled = [job.result() for job in running]

    undeclared = (
        []
        if no_test or test_roots
        else [
            CheckReport(
                name="tests", lines=["tests: no suites declared"], counted=False
            )
        ]
    )
    reports = [*tooled, *undeclared, *scanned]
    # Ahead of the reports, because it says what the numbers below were
    # measured under: a run that took its share of a busy machine is not a
    # run that was slow, and a reader given the timings alone reads it as one.
    for notice in admission.said:
        typer.echo(notice.text)
    for report in reports:
        for line in report.lines:
            typer.echo(line)

    counted = [report for report in reports if report.counted]
    passed = sum(1 for report in counted if report.passed)
    typer.echo(f"\n{passed}/{len(counted)} checks passed{spent(reports, started)}")

    failed = [report.name for report in counted if not report.passed]
    if failed:
        typer.echo(f"Failed: {', '.join(failed)}")
        raise typer.Exit(1)


def unrun_lines(scope: ChangedScope, test_roots: list[TestRoot]) -> list[str]:
    """What a narrowed run leaves to the whole gate, named rather than implied.

    A run that reports only what it checked reads as a verdict on the change,
    and a change of Markdown and CSS alone checked nothing at all. So the
    changed files no scoped check reads are listed, and the gates not run
    are named: the suites by the names the gate runs them under, and the
    sweeps as the one kind they are — each reads the whole tree, which is
    why no scope narrows them.
    """
    suites = ", ".join(root.name for root in test_roots) or "none declared"
    return [
        *(
            [
                f"Unread: {len(scope.unread)} changed file(s) no scoped check reads:",
                *(f"  {path}" for path in scope.unread),
            ]
            if scope.unread
            else []
        ),
        f"Not run: the test suites ({suites}) and the whole-tree sweeps — "
        "notes, harness drift, documented commands and the rest.",
        "  `uv run lup-devtools dev test <path>` runs the tests named, and "
        "`uv run lup-devtools dev check` all of it: what a commit has to pass.",
    ]


def run_changed(
    project: DevProject,
    base: ChangeBase,
    test_roots: list[TestRoot],
    spread: Spread | None = None,
    fix: bool = False,
    environments: Sequence[Path] = (),
    record: MigrationRecord = MigrationRecord(),
) -> None:
    """Check the files this tree changed, and say plainly what went unchecked.

    The narrow half of the gate, for the loop a change is still moving in.
    Ruff and Pyright are the two checks a scope narrows *exactly*: each reads
    the files it is handed and answers about those, so a narrowed run says the
    same thing about them a whole run would. Pyright resolves each file's
    imports itself, which is why naming files is a statement about what is
    reported rather than about what was understood. ``base`` is where the
    change starts, which :func:`change_base` answers.

    **The declared-migrations row runs too**, from the same base, wherever
    ``spread`` says projects are built on this one (:func:`migration_reports`).
    It is scoped by nature — what this branch took away since it left its
    base — and a public name removed is a one-file mistake, which the loop is
    the place to catch rather than the whole gate. It runs whether or not any
    Python file survives, because deleting a module is the removal it exists
    to see. It parses every declaration in ``record``, so a changed one it
    read is not listed as unread.

    **The anti-pattern rules run too**, over the changed files the sweep reads
    (:func:`antipattern_report`). They read files wherever they came from, so
    a file landed by `cp` or taken whole in a merge -- neither of which the
    edit gate reads on the way in -- is refused here, in the loop, rather than
    first at the gate a commit passes.

    **Tests are not narrowed, and not run.** Which tests reach a change is a
    question about the import graph, and this repository reaches modules
    through `importlib` in places no static reading sees — so a narrowed suite
    could report green while skipping the one test the change breaks. A gate
    that is trusted and wrong costs more than a gate that is slow, so this one
    declines the question and says so on every run, naming the suites and
    sweeps it left (:func:`unrun_lines`). `dev test` runs the files a person
    names; `dev check` stays the bar a commit passes.

    **No gate slot is held**, where `dev check` over its suites and `dev test`
    each hold one. This opens no suite, and its tools over a handful of files
    finish in seconds. Admitted, it would queue the loop's quick half behind
    runs of minutes, and every run opening beside it would take a narrower
    share for the whole of its own length — a share is fixed when a run opens
    — to make room for a check long since finished.
    """
    started = perf_counter()
    scope = changed_scope(base.commit)
    excluded_roots = non_code_roots(project)
    typer.echo(f"Changes since {base.reached} ({base.commit}).\n")
    tools: list[Callable[[], CheckReport]] = [
        *(
            [
                partial(ruff_format_check, fix, excluded_roots, scope.checked),
                partial(ruff_lint_check, fix, excluded_roots, scope.checked),
                partial(
                    pyright_check,
                    excluded_roots,
                    scope.checked,
                    environments=environments,
                ),
            ]
            if scope.checked
            else []
        ),
        partial(antipattern_report, project, [*scope.checked, *scope.unread]),
    ]
    with ThreadPoolExecutor(max_workers=len(tools) + 1) as pool:
        running = [pool.submit(tool) for tool in tools]
        migrated = migration_reports(project, spread, base.commit, record)
        marked = conflict_marker_report([*scope.checked, *scope.unread], worktree_text)
        reports = [*(job.result() for job in running), *migrated, marked]
    ruled = [item.rel for item in scanned_files(project, scope.unread)]
    unread = [
        path
        for path in scope.unread
        if path not in ruled and not (migrated and record.holds(Path(path)))
    ]
    scope = scope.model_copy(update={"unread": unread})

    if not scope.checked:
        typer.echo("No Python file changed, so neither ruff nor pyright ran.")
    for report in reports:
        for line in report.lines:
            typer.echo(line)

    counted = [report for report in reports if report.counted]
    passed = sum(1 for report in counted if report.passed)
    if counted:
        typer.echo(
            f"\n{passed}/{len(counted)} checks passed over {len(scope.checked)} "
            f"changed Python file(s){spent(reports, started)}"
        )
    for line in unrun_lines(scope, test_roots):
        typer.echo(line)

    failed = [report.name for report in counted if not report.passed]
    if failed:
        typer.echo(f"Failed: {', '.join(failed)}")
        raise typer.Exit(1)
