"""What every workflow command tree reads about the repository it runs in.

Its own module because it is read by more than one of them. The commands lup
ships are grouped by the module that owns them — the quality gate and the
scans under ``dev``, the branch and worktree loop under ``git``, a resolver run
under ``resolve`` — and each of those factories is wired over this same
declaration, so leaving it beside any one of them would make the other two
import that one for a type.
"""

from pathlib import Path

from pydantic import BaseModel

import lup.devtools.dev.check as check
import lup.devtools.dev.git_guards as git_guards_mod
import lup.devtools.dev.reach as reach
from lup.devtools.dev.release import ReleaseSpec
from lup.devtools.dev.scaffold import ScaffoldSource
from lup.devtools.dev.subprojects import SubProjects
from lup.devtools.project import DevProject
from lup.harness.models import HookSet, Plugin


class DevDeclarations(BaseModel, frozen=True):
    """Everything the workflow trees read about the repository they run in.

    Read when a command runs rather than when the CLI is composed: each of
    these resolves against the working directory, and a CLI is imported long
    before anyone knows which repository it will be pointed at.
    """

    project: DevProject
    hooks: HookSet
    plugin: Plugin
    test_roots: list[check.TestRoot]
    sub_projects: SubProjects = SubProjects()
    """The uv projects nested in this repository, each with its own environment.

    Their suites are among ``test_roots`` already, since the policy derives
    the test role from that one list; what is read here is the environments
    themselves — synced in a fresh worktree, and before the gate's Pyright
    reads their packages. None is the answer for a repository that is one
    project."""

    git_guards: list[git_guards_mod.GitGuard] = git_guards_mod.DECLARED_GUARDS
    """Which checks this repository installs as git hooks.

    A default rather than a fixture: the pair lup arms is what most projects
    want, and one that guards a third moment — or runs its gate under another
    name — says so here instead of forking the module that writes them."""

    spread: reach.Spread | None = None
    """Which of this repository's trees reach a project built on it, and how.

    Absent in almost every project, and that is the honest answer rather than
    an omission: a repository nobody builds on carries nobody's copy of
    anything, so there is no scaffold whose cost could be asked about. A
    repository that does ship one declares it here, and `dev reach` measures
    what each mechanism carried."""

    scaffold: ScaffoldSource | None = None
    """Where this project's copied half came from, in a project that adopted one.

    The mirror image of ``spread``: that one says this repository is somebody's
    upstream, this one says somebody is ours. Absent is the honest answer for a
    repository that wrote its own modules, and for the scaffold itself — which
    is the origin of every copy and so has nothing to merge from."""

    release: ReleaseSpec = ReleaseSpec()
    """Which files a release moves, and what its tag is called.

    A default rather than a fixture, because most repositories publish
    themselves out of their own root and that is what it describes. One
    keeping its distribution in a subdirectory — as this repository does,
    under ``packages/lup`` — names that manifest, and a release then moves
    the version somebody installs rather than whichever one a search
    happened to reach first."""

    def restored_workspaces(self) -> list[Path]:
        """The toolchain workspaces a fresh worktree restores beside `uv sync`.

        Derived from the suites rather than declared again: the suite that
        runs in a workspace is the one that knows it has to be restored, and
        a second list naming the same directories would drift from the first.
        """
        return [
            workspace
            for root in self.test_roots
            for workspace in root.restored_workspaces()
        ]
