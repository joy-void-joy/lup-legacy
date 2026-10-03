"""A uv project nested in this repository, declared once and checked by construction.

A repository can hold a project of its own beside the root one: a directory
with its own `pyproject.toml`, its own lockfile, its own environment, often on
another Python. Nothing the gates read discovers one. Pyright resolves every
import against the root's interpreter, so each of the nested project's
third-party imports is unresolvable; the gate runs no suite it was not told
about; and the policy judges the nested tests as production source. Answering
each by hand-editing the configuration it lives in spells one fact in four
places, which then drift apart.

So the fact is declared once, as a :class:`SubProject` in the project's
catalog, and compiled into each place a gate reads it:

- a Pyright execution environment rooted at the sub-project, on its own
  Python, searching its sources and its environment's packages — and a second
  one rooted at its scratch directory, so a script run with
  ``uv run --project <root>`` is checked where it runs;
- the sub-project's root in the tree Pyright checks, and Ruff's target
  version for both roots;
- a test root running its suite in its own environment, whose collected files
  carry the test role;
- scratch roles for every environment directory, wherever it sits, so the
  gate — which hands Pyright an exclusion list of its own — never analyses a
  sub-project's installed packages as source.

The first three live in ``pyproject.toml``, which the project also writes by
hand, so :func:`write_sub_projects` touches only what it placed and records
the roots it placed beside them: dropping a declaration then takes its entries
with it rather than leaving them looking like decisions somebody made.
"""

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import tomlkit
import tomlkit.items
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field, field_validator

from lup.devtools.dev.check import TestRoot
from lup.formats.toml import edited_manifest
from lup.harness.models import HookPathRole
from lup.types import StringMap
from lup.workspace.paths import project_root


class ExecutionEnvironment(BaseModel, frozen=True):
    """One Pyright execution environment, spelled as the manifest's table spells it."""

    root: str
    python_version: str = Field(alias="pythonVersion")
    extra_paths: list[str] = Field(alias="extraPaths")


class SubProject(BaseModel, frozen=True):
    """One uv project nested in this repository, with its own environment."""

    root: Path = Field(
        description=(
            "The directory holding its `pyproject.toml`, relative to the "
            "repository top — what its execution environment is rooted at, "
            "what its suite runs from, and what `uv run --project` names"
        )
    )
    python: str = Field(
        description=(
            "The Python its environment runs, `3.13`. Pyright checks its code "
            "against this version and reads its packages from the "
            "environment's `lib/python3.13/site-packages`, so pin the same one "
            "in the sub-project (`uv python pin 3.13`), or uv may build the "
            "environment on another and that path names nothing"
        )
    )
    sources: list[Path] = Field(
        default=[Path("src")],
        description=(
            "Where its import roots sit, relative to its root: `src` for a "
            "src layout, the root itself (`Path()`) for a flat one"
        ),
    )
    scratch: Path | None = Field(
        default=None,
        description=(
            "Where scratch scripts that run in its environment sit, relative "
            "to the repository top; `tmp/<root>` when unsaid. A script there "
            "is type-checked on its Python against its packages, the way "
            "`uv run --project <root>` runs it"
        ),
    )
    suite: bool = Field(
        default=True,
        description="Whether it has a pytest suite the gate runs from its root",
    )
    parallel: bool | None = Field(
        default=None,
        description=(
            "Whether its suite spreads over processes; unsaid, its own "
            "environment is asked whether it holds pytest-xdist"
        ),
    )

    @field_validator("root", "scratch")
    @classmethod
    def from_the_top(cls, value: Path | None) -> Path | None:
        """A sub-project is named from the repository top, like every root a gate reads.

        An absolute path would compile one machine's layout into a committed
        manifest, where every other checkout reads it as naming nothing.
        """
        if value is not None and value.is_absolute():
            raise ValueError(
                f"{value} is absolute; name a sub-project's directories "
                "relative to the repository top"
            )
        return value

    @field_validator("python")
    @classmethod
    def a_release(cls, value: str) -> str:
        """A Python release, read as `packaging` reads one, down to its minor."""
        try:
            parsed = Version(value)
        except InvalidVersion as error:
            raise ValueError(
                f"{value!r} is not a Python version, like '3.13'"
            ) from error
        return f"{parsed.major}.{parsed.minor}"

    def scratch_root(self) -> Path:
        """Where its scratch scripts sit: declared, or `tmp/<root>`."""
        return self.scratch or Path("tmp") / self.root

    def environment_roots(self) -> list[Path]:
        """Every root checked in its environment: itself and its scratch."""
        return [self.root, self.scratch_root()]

    def test_root(self) -> TestRoot:
        """Its suite, run from its own root in its own environment."""
        return TestRoot(
            name=f"pytest ({self.root.as_posix()})",
            directory=self.root,
            parallel=self.parallel,
        )

    def execution_environments(
        self, environments: Sequence[str]
    ) -> list[ExecutionEnvironment]:
        """The Pyright execution environments it compiles to, one per checked root.

        Pyright gives an execution environment no interpreter of its own, so
        its packages are named as the environment's `site-packages` — once
        per directory name a machine builds the environment under, because
        `UV_PROJECT_ENVIRONMENT` moves it and a committed manifest cannot know
        which this machine used. Pyright passes by a path nobody synced.
        """
        searched = [
            *(self.root / source for source in self.sources),
            *(
                self.root / name / "lib" / f"python{self.python}" / "site-packages"
                for name in environments
            ),
        ]
        return [
            ExecutionEnvironment(
                root=root.as_posix(),
                pythonVersion=self.python,
                extraPaths=[path.as_posix() for path in searched],
            )
            for root in self.environment_roots()
        ]

    def target_versions(self) -> StringMap:
        """Ruff's per-file target version, keyed by Ruff's glob for each checked root."""
        release = Version(self.python)
        return {
            f"{root.as_posix()}/**": f"py{release.major}{release.minor}"
            for root in self.environment_roots()
        }


class SubProjects(BaseModel, frozen=True):
    """Every uv project nested in this repository, and where environments are built."""

    projects: list[SubProject] = []
    environments: list[str] = Field(
        default=[".venv"],
        description=(
            "The directory names a project's environment is built under, "
            "relative to its root: uv's default, and whatever else a machine "
            "running this project sets `UV_PROJECT_ENVIRONMENT` to — a "
            "container keeping its own environment beside the host's names "
            "that one too. Each is scratch wherever it sits, the root "
            "project's included"
        ),
    )

    def test_roots(self) -> list[TestRoot]:
        """The suites the gate runs, one per sub-project that has one."""
        return [project.test_root() for project in self.projects if project.suite]

    def environment_roles(self) -> list[HookPathRole]:
        """Every environment directory is scratch, at any depth.

        A build product, rebuilt by a sync, so destroying one costs the
        command rather than any information. And the declaration the gate
        excludes from Pyright: it hands Pyright an exclusion list of its own,
        which switches off Pyright's default of skipping dot-directories, so
        a sub-project's environment inside the checked tree would otherwise
        be analysed package by package.
        """
        return [
            HookPathRole(root=Path("**") / name, role="scratch")
            for name in self.environments
        ]

    def synced(self) -> list[Path]:
        """The roots whose environments a gate or a fresh worktree readies."""
        return [project.root for project in self.projects]

    def placed_roots(self) -> list[str]:
        """Every root a declaration places in the manifest, in declaration order."""
        return [
            root.as_posix()
            for project in self.projects
            for root in project.environment_roots()
        ]


def normalized(root: str) -> str:
    """One root however the manifest spells it: `./studio/` is `studio`."""
    return PurePosixPath(root).as_posix()


def recorded_roots(document: tomlkit.TOMLDocument) -> list[str]:
    """The roots an earlier generation placed, from its record in ``[tool.lup]``."""
    match document.unwrap():
        case {"tool": {"lup": {"sub-project-roots": list(roots)}}}:
            return [normalized(str(root)) for root in roots]
        case _:
            return []


def placed_environments(
    tool: tomlkit.items.Table, declared: SubProjects, owned: list[str]
) -> list[str]:
    """Pyright's execution environments: the compiled ones first, then the project's.

    First, because Pyright takes the first environment whose root holds the
    file, and a project's own environment rooted above a sub-project would
    otherwise answer for it. The project's own move as the tables it wrote.
    """
    generated = [
        entry.model_dump(by_alias=True)
        for project in declared.projects
        for entry in project.execution_environments(declared.environments)
    ]
    pyright = tool["pyright"] if "pyright" in tool else tomlkit.table()
    key = "executionEnvironments"
    current = list(pyright[key]) if key in pyright else []
    kept = [entry for entry in current if normalized(str(entry["root"])) not in owned]
    if [entry.unwrap() for entry in current] == [
        *generated,
        *(entry.unwrap() for entry in kept),
    ]:
        return []
    environments = tomlkit.aot()
    for entry in generated:
        table = tomlkit.table()
        table.update(entry)
        environments.append(table)
    for entry in kept:
        environments.append(entry)
    if environments:
        pyright[key] = environments
    else:
        del pyright[key]
    if "pyright" not in tool:
        tool["pyright"] = pyright
    return [f"pyright environments: {len(current)} -> {len(environments)}"]


def placed_includes(
    tool: tomlkit.items.Table, declared: SubProjects, owned: list[str]
) -> list[str]:
    """Each sub-project in the tree Pyright checks, where the project lists one.

    A project listing no ``include`` has Pyright check everything already.
    Scratch is not added: the gate checks what a commit carries. A root the
    list already names stays where it is.
    """
    if "pyright" not in tool or "include" not in tool["pyright"]:
        return []
    include = tool["pyright"]["include"]
    checked = [project.root.as_posix() for project in declared.projects]
    current = [normalized(str(path)) for path in include]
    dropped = [
        index
        for index, path in enumerate(current)
        if path in owned and path not in checked
    ]
    missing = [root for root in checked if root not in current]
    if not dropped and not missing:
        return []
    for index in reversed(dropped):
        del include[index]
    for root in missing:
        include.append(root)
    return [f"pyright include: -{len(dropped)} +{len(missing)}"]


def placed_target_versions(
    tool: tomlkit.items.Table, declared: SubProjects, owned: list[str]
) -> list[str]:
    """Ruff's target version for every root checked in a sub-project's environment."""
    generated = {
        pattern: version
        for project in declared.projects
        for pattern, version in project.target_versions().items()
    }
    patterns = {f"{root}/**" for root in owned}
    key = "per-file-target-version"
    ruff = tool["ruff"] if "ruff" in tool else tomlkit.table()
    versions = ruff[key] if key in ruff else tomlkit.table()
    current = {str(pattern): str(version) for pattern, version in versions.items()}
    kept = {
        pattern: version
        for pattern, version in current.items()
        if pattern not in patterns
    }
    if current == {**kept, **generated}:
        return []
    for pattern in current:
        if pattern in patterns and pattern not in generated:
            del versions[pattern]
    for pattern, version in generated.items():
        versions[pattern] = version
    if versions and key not in ruff:
        ruff[key] = versions
    if not versions and key in ruff:
        del ruff[key]
    if ruff and "ruff" not in tool:
        tool["ruff"] = ruff
    return [f"ruff target versions: {len(current)} -> {len(versions)}"]


def recorded(tool: tomlkit.items.Table, placed: list[str]) -> list[str]:
    """The record of what this placed, so a later generation can take it back."""
    lup = tool["lup"]
    key = "sub-project-roots"
    current = [normalized(str(root)) for root in lup[key]] if key in lup else []
    if current == placed:
        return []
    if placed:
        lup[key] = placed
    else:
        del lup[key]
    return [f"sub-project roots: {current} -> {placed}"]


def apply_sub_projects(
    document: tomlkit.TOMLDocument, declared: SubProjects
) -> list[str]:
    """Bring a manifest's compiled sub-project entries in line with the declaration.

    Owned is what the declaration places and what the record says an earlier
    generation placed, so an entry for a sub-project the catalog dropped goes
    with it. A manifest that owns nothing is left as it was read, byte for
    byte.
    """
    placed = declared.placed_roots()
    owned = [*recorded_roots(document), *placed]
    if not owned:
        return []
    tool = document["tool"]
    return [
        *placed_environments(tool, declared, owned),
        *placed_includes(tool, declared, owned),
        *placed_target_versions(tool, declared, owned),
        *recorded(tool, placed),
    ]


def write_sub_projects(
    declared: SubProjects, root: Path | None = None, *, check: bool = False
) -> Path:
    """Write or verify what the declared sub-projects compile to in ``pyproject.toml``.

    A repository writer: ``harness generate all`` writes it, and the drift
    check reads it with *check*, which reports the manifest stale rather than
    writing it.
    """
    manifest = (root or project_root()) / "pyproject.toml"
    changes = edited_manifest(
        manifest,
        lambda document: apply_sub_projects(document, declared),
        write=not check,
    )
    if changes and check:
        raise RuntimeError(
            f"{manifest} is behind the declared sub-projects: {'; '.join(changes)}"
        )
    return manifest
