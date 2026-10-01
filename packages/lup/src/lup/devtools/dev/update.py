"""Every carrier moved to one upstream commit, by one command.

Three mechanisms carry an upstream scaffold into a project: a pin for the
library, a regeneration for the native trees, a hand-port for the copied
half. Moved separately, they come apart — the pin lands a library whose
callers are still last month's, and nothing says so until something breaks.
So the update is one command, and what it moves them all to is one commit.

The commit is read back rather than chosen twice. `uv lock` resolves the pin
and writes the commit it got; that commit is what the scaffold is compiled at
and what the migrations are read between, so the three carriers agree by
construction instead of by being run in the right order on the same afternoon.

Where they do not agree — an update interrupted by a conflict, a lock moved by
hand, a scaffold branch nobody merged — :class:`CarrierDrift` says so in one
line, and the same line is what the gate and the prompt-time fold report.
"""

from collections.abc import Callable
from pathlib import Path

import sh
import typer
from pydantic import BaseModel

import lup.devtools.dev.library as library
import lup.devtools.dev.migrations as migrations
import lup.devtools.dev.scaffold as scaffold
import lup.devtools.dev.scaffold_fit as scaffold_fit
from lup.formats.banner import REGENERATE_COMMAND
from lup.devtools.sync import ensure_local, find_project
from lup.devtools.utils import decode_stderr, refuse, short_sha, uv
from lup.execution.shell import git
from lup.policy.kernel.diagnostic import devtools, step


class CarrierDrift(BaseModel, frozen=True):
    """Where each carrier stands, for a reader deciding whether to update.

    Three facts, none of them stored: the commit the lock resolved, the commit
    the last merged scaffold was compiled at, and how many migrations lie
    between them. A project whose carriers agree is the quiet case and says
    nothing at all.
    """

    library: str
    """The commit the library pin resolved, empty where nothing pins a commit."""

    scaffold: str
    """The commit the copied half was last merged at, empty before adoption."""

    pending: int = 0
    """Declared migrations between the two, which no merge applies on its own."""

    def settled(self) -> bool:
        """Whether every carrier stands at one commit with nothing owed.

        A carrier that says nothing settles nothing either way: a project
        resolving a released library pins no commit, and one that has not
        adopted a scaffold branch has merged none — neither is drift, and
        reporting them as drift would leave a row nobody can act on.
        """
        agreed = not self.library or not self.scaffold or self.library == self.scaffold
        return agreed and not self.pending

    def spelled(self) -> str:
        """The one line a gate row and a prompt-time fold both report."""
        return (
            f"library at {short_sha(self.library) or 'no pinned commit'}, "
            f"scaffold merged at {short_sha(self.scaffold) or 'nothing yet'}, "
            f"{self.pending} migration(s) pending"
        )


def drift(
    root: Path,
    source: scaffold.ScaffoldSource,
    distribution: str = library.DISTRIBUTION,
) -> CarrierDrift:
    """Read where this project's carriers stand, from git and the lock alone."""
    return CarrierDrift(
        library=scaffold.pinned_commit(root, distribution),
        scaffold=scaffold.merged_at(root, source.branch),
    )


def upstream_checkout(project: str, report: Callable[[str], None]) -> Path:
    """The local clone of upstream, materialized and fetched if need be.

    The registration is the one `sync` already keeps: a project that tracks
    its upstream for review is the same project that compiles its scaffold
    from it, and a second way of naming the same repository is a second thing
    to get wrong.
    """
    return ensure_local(find_project(project), report).checkout


def resolved_pin(
    root: Path,
    distribution: str,
    commit: str,
    report: Callable[[str], None],
    project: str = library.REGISTRATION,
) -> str:
    """Move the pin, and hand back the commit the lock resolved it to.

    A named commit is pinned as a revision; an unnamed one re-resolves whatever
    ref the project already declared, which is what following a branch means.
    Either way the answer is read out of `uv.lock` rather than assumed, because
    the lock is what the environment is built from and a commit this decided
    for itself would be a fourth carrier to keep in step.
    """
    if commit:
        library.set_mode(
            root,
            library.LibraryMode.GIT,
            git=library.GitSource(
                url=library.repository_url(root, project=project),
                ref_kind="rev",
                ref=commit,
            ),
        )
    else:
        source = library.read_git_source(root)
        if source is not None:
            source.require_available_branch()
    report(f"Resolving {distribution}...")
    uv("lock", "--upgrade-package", distribution, _cwd=str(root))
    return scaffold.pinned_commit(root, distribution)


def owed_since(
    merged: str, repository: Path, report: Callable[[str], None], root: Path = Path()
) -> list[str]:
    """What this update asks of the project beyond what the merge already did.

    Read from the library that just landed, because that is where a migration
    is declared: whatever it holds that the commit this project came from did
    not is what somebody has to act on. Ancestry is asked of upstream's own
    clone, the only checkout that has both commits.

    A project with nothing merged yet is told nothing: every declaration would
    be pending for it, which is true of a project that has taken none of them
    and useless to read.
    """
    if not merged:
        return []
    pending = migrations.RenderedMigrations.model_validate_json(
        uv.out(
            "run",
            "--no-sync",
            "lup-devtools",
            "dev",
            "migrate",
            "pending",
            merged,
            "--repository",
            str(repository.resolve()),
            "--json",
            _cwd=str(root),
        )
    )
    if not pending.count:
        return []
    return [f"{pending.count} migration(s) pending:", *pending.lines]


def regenerated(root: Path, report: Callable[[str], None]) -> None:
    """Regenerate every native tree, under the library the update just installed.

    In a subprocess rather than in this process, and that is not a style
    choice: a native tree is compiled from the library's own declarations, and
    the library imported here is the one that was installed when the command
    started — the very version this update exists to replace. Regenerating
    in-process would write the old library's trees and report them as current.

    A subprocess that refuses says why in the words generation itself refused
    with, and those are relayed whole rather than folded into an exception
    about an exit status: every carrier has moved by the time this runs, so
    what is left to act on is one declaration, and the reader needs to be
    told which.
    """
    report("Regenerating the native trees...")
    try:
        uv("run", "lup-devtools", "harness", "generate", "all", _cwd=str(root))
    except sh.ErrorReturnCode as refusal:
        report("The native trees were not regenerated. Generation refused:")
        for line in decode_stderr(refusal).splitlines():
            report(f"  {line}")
        report(
            "Every other carrier has moved and the merge has landed, so what "
            "is left is the declaration named above. Fix it, then run "
            f"`{REGENERATE_COMMAND}`."
        )
        raise typer.Exit(1) from refusal


def settled(
    root: Path,
    repository: Path,
    source: scaffold.ScaffoldSource,
    package: str,
    commit: str,
    already: str,
    report: Callable[[str], None],
) -> scaffold.MergeOutcome | None:
    """Merge ``scaffold(commit)`` as the declaration standing now compiles it.

    The compile is a pure function of the upstream commit *and* this project's
    own declaration of what it takes, so both halves have to be read here
    rather than the commit alone. Where neither moved, the branch does not
    either and there is nothing to merge; where the declaration moved — which
    is what resolving a scaffold conflict does — the same commit compiles a
    wider or narrower tree, and merging it is how the difference arrives.

    Nothing is regenerated after a conflicted merge: the trees are compiled
    from declarations the merge has not finished writing, and a regeneration
    over half a merge produces an artifact matching neither side.
    """
    standing = scaffold.branch_head(root, source.branch)
    head = scaffold.advanced(root, repository, source, package, commit)
    if head == standing and already == commit:
        report(f"The copied half is already merged at {short_sha(commit)}.")
        regenerated(root, report)
        return None
    outcome = scaffold.merged(root, source)
    report(f"Copied half: {outcome.spelled()}.")
    for line in owed_since(already, repository, report, root):
        report(line)
    for path in outcome.conflicted:
        report(f"  conflicted  {path}")
    if not outcome.complete():
        report(
            "Resolve those, `git add` them, and run `dev update` again: that "
            "pass concludes the merge and compiles the copied half against "
            "the declaration your resolution wrote, so an upstream path this "
            "project stops declining arrives in the same pass. Nothing is "
            "regenerated before then — the native trees are compiled from "
            "declarations the merge has not finished writing."
        )
        return outcome
    regenerated(root, report)
    return outcome


def resumed(
    root: Path,
    repository: Path,
    source: scaffold.ScaffoldSource,
    package: str,
    standing: str,
    report: Callable[[str], None],
) -> scaffold.MergeOutcome | None:
    """Finish the scaffold merge left standing here, at the commit it carries.

    Resolving a scaffold conflict is where a project decides what of upstream
    it wants, so it is also where :attr:`ScaffoldSource.declined` changes —
    and the scaffold commit under the standing merge was compiled from the
    declaration that resolution has just replaced. A path it stopped
    declining is in neither side of that merge, which is why the resolution
    cannot be the end of the pass: the merge is concluded here and the copied
    half compiled again against what the resolution wrote, so the two agree
    without a commit nobody is in a position to make in between.

    The pin is not moved. This pass finishes the one an earlier pass started,
    so its commit is the one the standing merge already carries, read back off
    that scaffold commit's own trailer rather than resolved a second time.
    """
    if standing != scaffold.branch_head(root, source.branch):
        report(
            f"A merge of {short_sha(standing)} is standing in this checkout "
            f"and it is not {source.branch}'s. Finish or abort it before "
            "updating: git holds one merge at a time, and the copied half "
            "arrives as one."
        )
        return None
    held = scaffold.unresolved(root)
    if held:
        report(f"The merge of {source.branch} is still unresolved:")
        for path in held:
            report(f"  conflicted  {path}")
        report(
            "Resolve those and `git add` them, then run `dev update` again. "
            "Nothing is compiled while the index holds a conflict, because "
            "what the resolution writes is what the copied half is compiled "
            "against."
        )
        return scaffold.MergeOutcome(
            plan=scaffold.planned(root, source.branch), conflicted=held
        )
    taken = scaffold.compiled_at(root, standing)
    report(f"Concluding the merge of {source.branch} at {short_sha(taken)}...")
    scaffold.concluded(root)
    return settled(root, repository, source, package, taken, taken, report)


def updated(
    root: Path,
    source: scaffold.ScaffoldSource,
    package: str,
    commit: str,
    distribution: str,
    report: Callable[[str], None],
) -> scaffold.MergeOutcome | None:
    """Move every carrier to one upstream commit, and say what it cost.

    Nothing is answered before the pin moves, because the pin is what decides
    the commit — except where a merge of the copied half is already standing
    in the checkout. Then this pass is the second half of an earlier one: the
    commit was decided there, and re-resolving the pin now would move the
    carriers out from under a merge that is only part-way applied.

    Nor before a scaffold branch stands. The merge an update is has two
    sides and a base, and a project that never rooted the branch has no base:
    compiling one now would root it at the pin, so the merge would find no
    ancestor at all -- and the branch left standing would refuse the adoption
    that answers it.
    """
    standing = scaffold.merging(root)
    if standing:
        return resumed(
            root,
            upstream_checkout(source.project, report),
            source,
            package,
            standing,
            report,
        )
    if not scaffold.branch_head(root, source.branch):
        report(
            f"This project has not rooted {source.branch}, so there is no commit "
            "its copied half was last carried up to, and nothing to merge from. "
            "Root it once, at the commit the project was stamped from: "
            "`dev scaffold adopt --base <commit>` -- `dev scaffold fit` measures "
            "the candidates where that commit is not known. Nothing has moved."
        )
        raise typer.Exit(1)
    resolved = resolved_pin(root, distribution, commit, report, source.project)
    if not resolved:
        report(
            f"{distribution} resolves to no commit, so the copied half has "
            "nothing to be compiled at. Pin the library at a repository "
            f"(`dev library git --branch <branch>`) to update both halves."
        )
        return None
    # Only once the pin answers, because materializing the upstream clone for
    # a pin that resolves to nothing reports the clone's failure over the
    # pin's. Reading for a standing merge above it costs nothing: that read is
    # local and moves no carrier.
    repository = upstream_checkout(source.project, report)
    report(f"Library at {short_sha(resolved)}; syncing the environment...")
    uv("sync", _cwd=str(root))
    already = scaffold.merged_at(root, source.branch)
    return settled(root, repository, source, package, resolved, already, report)


def adopted(
    root: Path,
    source: scaffold.ScaffoldSource,
    package: str,
    base: str,
    report: Callable[[str], None],
    accept_fit: int | None = None,
) -> str:
    """Root this project's scaffold branch, once, at the commit it was stamped from.

    Refused where the branch already stands, because rooting it twice is how a
    project ends up with two unrelated histories of the same tree and a merge
    base older than either. Refused too where the checkout's own copy says
    the base is wrong: the argument decides every later merge, and
    :func:`scaffold_fit.checked_base` measures it rather than trusting it.

    And refused over a staged change, before anything is fetched or measured:
    adoption is recorded as a merge, and git refuses a merge over a staged
    change -- which a checkout that was just renamed always holds, the rename
    being staged moves.
    """
    standing = scaffold.branch_head(root, source.branch)
    if standing:
        refuse(
            f"already stands at {short_sha(standing)}, compiled at "
            f"{short_sha(scaffold.compiled_at(root, standing))}, and adoption "
            "happens once",
            what=source.branch,
            steps=[
                step(
                    "take every later upstream change as an update",
                    devtools("dev", "update"),
                )
            ],
            code=2,
        )
    staged = git.lines("-C", str(root), "diff", "--cached", "--name-only")
    if staged:
        refuse(
            f"{len(staged)} path(s) are staged in this checkout, and adoption is "
            "recorded as a merge, which git refuses over a staged change",
            steps=[step("commit them first")],
            code=2,
        )
    repository = upstream_checkout(source.project, report)
    commit = scaffold_fit.checked_base(
        root, repository, source, package, base, accept_fit, report
    )
    rooted = scaffold.adopt(root, repository, source, package, commit)
    report(
        f"{source.branch} rooted at {short_sha(commit)} ({short_sha(rooted)}), "
        f"recorded as this project's merge base for every later update."
    )
    return rooted
