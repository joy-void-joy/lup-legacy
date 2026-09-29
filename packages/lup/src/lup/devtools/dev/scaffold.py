"""Upstream's copied half at one commit, compiled so an update is a git merge.

A project built on a scaffold receives it through three mechanisms. The
library arrives by a dependency pin, the native trees by a regeneration, and
the copied half — the modules initialization stamped out and the project has
owned ever since — only when somebody reads upstream's commits and retypes
them. The third is the one that fails: a commit changing the library *and* its
caller lands its library half by the pin and leaves the call site behind, so
the project meets it as a breakage rather than as work anybody chose to do.

The copied half is a pure function of upstream, though, and that is what this
module is about. ``scaffold(commit, package)`` is upstream's copied trees at
one commit with initialization's rename applied and whatever the project
declined subtracted — so it is compiled rather than remembered, onto a branch
of the project's own repository. The branch is rooted once at the commit the
project was stamped from and grows one commit per update, which gives git both
sides of the history it was missing: the merge base is the commit this project
last took, the two diffs are upstream's changes and the project's own, and
bringing the copied half up to date is ``git merge``.

Nothing here reads or writes the checkout. A scaffold commit is built through
an index of its own and recorded with ``commit-tree``, so a session with
uncommitted work keeps it and a branch nobody has checked out still advances;
the merge the caller asks for is the one operation that touches the tree.
"""

import os
import tarfile
import tomllib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import sh
from pydantic import BaseModel

from lup.devtools.dev.library import DISTRIBUTION
from lup.devtools.utils import short_sha
from lup.execution.shell import git
from lup.harness.passages import PASSAGE_SUFFIX, prose_beside


class ScaffoldRoot(BaseModel, frozen=True):
    """One tree upstream ships by copy, and where the adopter keeps it."""

    upstream: str
    """Its path in upstream's own repository."""

    adopter: str
    """Where it lands here, with ``{package}`` standing for this project's package."""

    def resolved(self, package: str) -> str:
        """This root's path in a project whose package is ``package``."""
        return self.adopter.format(package=package)


SCAFFOLD_ROOTS = [
    ScaffoldRoot(upstream="src/lup_template", adopter="src/{package}"),
    ScaffoldRoot(upstream="tests", adopter="tests"),
]
"""Which of upstream's trees are copied rather than imported.

The boundary, and it is narrower than "everything initialization touched":
`docs/` and the root files are an adopting domain's own from the first day,
about its subject rather than upstream's, and a merge carrying them would hand
every project upstream's README. A fork shipping a different layout passes its
own roots rather than editing these.
"""


class ScaffoldFile(BaseModel, frozen=True):
    """One file of one upstream root, under both of the spellings it has.

    A decline is declared in upstream's spelling and lands in the adopter's,
    and the file itself is read and written under a third — the compiled tree
    under a destination directory. Carrying the root beside the relative path
    keeps all three derivable from the one fact, so nothing here has to
    reassemble a path from a string somebody else assembled.
    """

    root: ScaffoldRoot
    relative: PurePosixPath
    """Its path under that root, which every spelling is built from."""

    def upstream(self) -> PurePosixPath:
        """Where it sits in upstream's own repository, as a decline spells it."""
        return PurePosixPath(self.root.upstream) / self.relative

    def adopter(self, package: str) -> PurePosixPath:
        """Where it lands in a project whose package is ``package``."""
        return PurePosixPath(self.root.resolved(package)) / self.relative

    def beside(self, name: str) -> "ScaffoldFile":
        """The file of this same root in this same directory, under ``name``."""
        return self.model_copy(update={"relative": self.relative.parent / name})


class ScaffoldSource(BaseModel, frozen=True):
    """Where this project's copied half comes from, and what of it it took.

    Declared rather than discovered. Which tree a project copied, under what
    name, and which of its modules it declined are facts about an adoption
    that happened once and that no later reading of either repository
    recovers — a file absent here is one the project deleted or one upstream
    never had, and the two want opposite things from an update.
    """

    project: str = "lup"
    """The tracked registration the scaffold is compiled from.

    A name rather than a URL: `sync.json` already says where each tracked
    repository is and how this machine reaches it, and a second spelling here
    is how a project ends up merging one clone and pinning another.
    """

    package: str = "lup_template"
    """Upstream's own package name — what initialization renamed, in its text."""

    branch: str = "lup-scaffold"
    """The branch upstream's copied half is compiled onto, in this repository."""

    roots: list[ScaffoldRoot] = SCAFFOLD_ROOTS

    declined: list[str] = []
    """Upstream paths this project did not take, spelled as upstream spells them.

    Subtracted at compile time rather than deleted after the merge, which is
    the difference between a project that never had a file and one that keeps
    deleting it: a declined path is absent from every scaffold commit, so it
    is absent from the merge base too and git has nothing to report about it.
    A directory declines everything beneath it.

    Coherent as a selection, not only as a list: a path whose only reader is
    another path the project took is refused by :func:`halved_passages` where
    the scaffold is compiled, because the declaration is where such a pair can
    still be edited.
    """

    def declines(self, file: ScaffoldFile) -> bool:
        """Whether this project declined one file of one upstream root."""
        spelled = file.upstream()
        return any(spelled.is_relative_to(PurePosixPath(one)) for one in self.declined)


class CompiledScaffold(BaseModel, frozen=True):
    """One materialized scaffold: the commit it is of, and what it holds."""

    commit: str
    """The upstream commit compiled, in full, so a trailer can carry it."""

    files: list[str]
    """Every path written, spelled as this project spells it."""


def extracted(repository: Path, treeish: str, destination: Path) -> None:
    """Write one tree of one commit into a directory, without a checkout.

    Through ``git archive`` rather than blob by blob, so a file git holds and
    nothing here understands — an image, a fixture that is not text — arrives
    byte for byte instead of through a decoding this would have to guess at.
    """
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "scaffold.tar"
    git(
        "-C",
        str(repository),
        "archive",
        "--format=tar",
        "--output",
        str(archive),
        treeish,
    )
    with tarfile.open(archive) as held:
        held.extractall(destination, filter="data")
    archive.unlink()


def renamed_in_place(path: Path, upstream_package: str, package: str) -> bool:
    """Rewrite one file's mentions of upstream's package name as this one's.

    Initialization's rename, as it reaches a file inside the copied trees:
    every spelling of the package name, in imports and in the dotted anchors
    that name modules from strings. Text surgery on purpose — a rename *is*
    text surgery — and the pairing it produces is what the merge is measured
    on, since a file this leaves byte-identical to the project's own copy is
    one the merge never has to consider.

    A file that is not text is left exactly as it arrived and says so by
    answering False, because a rename that could not read a file has not made
    one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    # lup: ignore[string-replace] — the rename this compiles is text surgery
    # over another repository's tree, and the spelling is the whole subject
    renamed = text.replace(upstream_package, package)
    if renamed != text:
        path.write_text(renamed, encoding="utf-8")
    return True


class HalvedPassage(BaseModel, frozen=True):
    """A pair of files one declaration is written across, with one half declined.

    A content module and the Markdown beside it are two files of one thing:
    the module says what the document is and names the passage it places, and
    the prose holds the words that passage is made of. Whichever half is
    missing, what is left has no reader — the module reads a file that is not
    there, the prose sits where nothing looks for it.
    """

    module: PurePosixPath
    """The Python half, spelled as upstream spells it."""

    prose: PurePosixPath
    """The Markdown half beside it, holding that module's words."""

    prose_declined: bool
    """Which half the decline list covers: the prose, or the module reading it."""

    def spelled(self) -> str:
        """The one line this pair contributes to the refusal."""
        match self.prose_declined:
            case True:
                return (
                    f"declined {self.prose}, took {self.module} — "
                    f"the module whose words those are"
                )
            case False:
                return (
                    f"declined {self.module}, took {self.prose} — "
                    f"the words that module is the only reader of"
                )


def halved_passages(
    source: ScaffoldSource, held: list[ScaffoldFile]
) -> list[HalvedPassage]:
    """Every pair this selection takes one half of and declines the other.

    Walked from the Python half, because that is the direction the naming rule
    runs: a module file names the prose beside it, while a Markdown file
    upstream does not ship has no half to be coherent with. Both incoherences
    fall out of the one walk — the pair is reported whenever the decline list
    covers one of the two and not the other, whichever one that is.
    """
    shipped = {file.upstream() for file in held}
    return [
        HalvedPassage(
            module=file.upstream(),
            prose=prose.upstream(),
            prose_declined=source.declines(prose),
        )
        for file in held
        if file.relative.suffix == ".py"
        for prose in [file.beside(prose_beside(file.relative.name))]
        if prose.upstream() in shipped
        if source.declines(file) != source.declines(prose)
    ]


def coupling_refusal(halved: list[HalvedPassage]) -> str:
    """What a project reads when its selection halves a declaration.

    The pairing is explained once and each pair named once, because a reader
    meeting nineteen of these needs the whole list and the rule behind it, not
    the rule nineteen times.
    """
    return (
        f"{len(halved)} declined path(s) leave half of a declaration behind. "
        f"A content module and the `{PASSAGE_SUFFIX}` file beside it are one "
        f"declaration written across two files: the module says what the "
        f"document is and names the passage it places, the Markdown holds the "
        f"words, and generation reads the second through the first. Neither "
        f"half has a reader without the other, so `declined` takes both or "
        f"neither.\n"
        + "".join(f"  {pair.spelled()}\n" for pair in halved)
        + "Add the other half of each pair to `declined`, or take both halves."
    )


def compiled(
    repository: Path,
    commit: str,
    source: ScaffoldSource,
    package: str,
    destination: Path,
) -> CompiledScaffold:
    """Materialize ``scaffold(commit, package)`` under ``destination``.

    The pure function this module exists for: upstream's copied trees at one
    commit, renamed, with the declined paths subtracted. Two calls with the
    same arguments write the same bytes, which is what lets the result be
    recorded as a tree and compared as one.

    The whole tree is read before anything is subtracted, because this is the
    one place holding both halves of the question a decline asks: what
    upstream ships at this commit, and what the project said it took. A
    selection that halves a declaration is refused here rather than compiled,
    since every later reader of it — the merge, the generation — meets it as a
    file that is simply not there, hundreds of lines from the declaration that
    said so.
    """

    def extracted_files() -> Iterator[ScaffoldFile]:
        """Every file of every root, each root walked as soon as it arrives.

        Walked root by root rather than after the last extraction, so a root
        landing under another one is still read as its own tree: the archive
        it arrives in is written into the directory it fills.
        """
        for root in source.roots:
            holding = destination / root.resolved(package)
            extracted(repository, f"{commit}:{root.upstream}", holding)
            for path in sorted(holding.rglob("*")):
                if path.is_file():
                    yield ScaffoldFile(
                        root=root,
                        relative=PurePosixPath(path.relative_to(holding).as_posix()),
                    )

    held = list(extracted_files())
    halved = halved_passages(source, held)
    if halved:
        raise ValueError(coupling_refusal(halved))
    for declined in [file for file in held if source.declines(file)]:
        (destination / declined.adopter(package)).unlink()
    taken = [file for file in held if not source.declines(file)]
    for file in taken:
        renamed_in_place(destination / file.adopter(package), source.package, package)
    return CompiledScaffold(
        commit=commit,
        files=sorted(file.adopter(package).as_posix() for file in taken),
    )


# lup: ignore[constant-declaration] — an identity this repository defines: the
# word it writes into its own commits and reads back out of them
SCAFFOLD_TRAILER = "Lup-Scaffold-Commit"
"""The trailer carrying which upstream commit a scaffold commit was compiled at.

A trailer rather than a file in the tree, and rather than a record kept beside
the branch: git parses trailers itself, the scaffold tree is upstream's bytes
and nothing of ours belongs in it, and what is inside the commit travels with
it through a clone, a fetch, and a merge.
"""


def git_directory(root: Path) -> str:
    """Where git keeps this checkout's administrative files."""
    return git.out("-C", str(root), "rev-parse", "--absolute-git-dir")


def written_tree(root: Path, contents: Path) -> str:
    """Record a directory as a git tree object, through an index of its own.

    The checkout's index is where a session's staged work sits, so writing
    through it would stage a whole scaffold over somebody's half-made commit.
    A temporary index file and an explicit work tree keep the two apart: this
    writes objects into the repository and touches nothing checked out.
    """
    with TemporaryDirectory() as holding:
        environment = {
            # lup: ignore[os-environ] — the process environment is inherited,
            # not read: git needs its own PATH and configuration home, and the
            # three names below are what redirect it away from the checkout
            **os.environ,
            "GIT_INDEX_FILE": str(Path(holding) / "index"),
            "GIT_DIR": git_directory(root),
            "GIT_WORK_TREE": str(contents),
        }
        git("add", "--all", "--force", "--", ".", _cwd=str(contents), _env=environment)
        return git.out("write-tree", _cwd=str(contents), _env=environment)


def recorded(root: Path, tree: str, message: str, parents: list[str]) -> str:
    """Commit one tree onto no branch, and hand back what was committed."""
    lineage = [word for parent in parents for word in ("-p", parent)]
    return git.out("-C", str(root), "commit-tree", tree, *lineage, "-m", message)


def scaffold_message(
    source: ScaffoldSource, compiled_scaffold: CompiledScaffold
) -> str:
    """What one scaffold commit says it is, for a reader of the branch's log."""
    return (
        f"scaffold({short_sha(compiled_scaffold.commit)})\n\n"
        f"{source.project}'s copied half at {compiled_scaffold.commit}, "
        f"{len(compiled_scaffold.files)} file(s), renamed to this project's "
        f"package.\n\n"
        f"Compiled, never edited: what this branch holds is upstream's, and a "
        f"change made here would reach the project once and be gone at the "
        f"next update.\n\n"
        f"{SCAFFOLD_TRAILER}: {compiled_scaffold.commit}\n"
    )


def branch_head(root: Path, branch: str) -> str:
    """The commit a branch stands at, or nothing where there is no such branch."""
    return git.out(
        "-C",
        str(root),
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        _ok_code=[0, 1],
    )


def compiled_at(root: Path, commit: str) -> str:
    """The upstream commit one scaffold commit was compiled at, by its trailer."""
    if not commit:
        return ""
    return git.out(
        "-C",
        str(root),
        "log",
        "-1",
        f"--format=%(trailers:key={SCAFFOLD_TRAILER},valueonly)",
        commit,
    )


def merged_at(root: Path, branch: str) -> str:
    """The upstream commit this checkout's copied half last merged.

    Read from git's own merge base rather than from a record this would have
    to keep: the last scaffold commit that is an ancestor of HEAD *is* the
    last one merged, and its trailer says which upstream commit it holds. A
    project that has merged none has no merge base, and the answer is nothing.
    """
    if not branch_head(root, branch):
        return ""
    base = git.out(
        "-C", str(root), "merge-base", "HEAD", f"refs/heads/{branch}", _ok_code=[0, 1]
    )
    return compiled_at(root, base)


def tree_at(root: Path, commit: str) -> str:
    """The tree one commit holds, so two commits' contents can be compared.

    What a branch decision is made on rather than the commit id: two compiles
    of one scaffold record identical trees and differing commits, because a
    commit carries its parent and the time it was written.
    """
    return git.out("-C", str(root), "rev-parse", f"{commit}^{{tree}}")


class RecordedScaffold(BaseModel, frozen=True):
    """One compiled scaffold written into the repository, on no branch yet."""

    tree: str
    """The tree object ``write-tree`` recorded, in full."""

    built: CompiledScaffold
    """What was compiled into it, for the message a commit of it carries."""


def recorded_scaffold(
    root: Path,
    repository: Path,
    source: ScaffoldSource,
    package: str,
    commit: str,
) -> RecordedScaffold:
    """Compile ``scaffold(commit)`` and record it as a tree of this repository.

    Recorded before any branch moves, because whether the branch should move
    at all is answered by comparing this tree with the one already there.
    """
    with TemporaryDirectory() as holding:
        contents = Path(holding) / "scaffold"
        built = compiled(repository, commit, source, package, contents)
        return RecordedScaffold(tree=written_tree(root, contents), built=built)


def advanced(
    root: Path,
    repository: Path,
    source: ScaffoldSource,
    package: str,
    commit: str,
) -> str:
    """Put ``scaffold(commit)`` on the scaffold branch, and hand back its commit.

    The branch's root commit where there is no branch yet, and one more commit
    on it where there is. Either way the scaffold tree replaces the branch's
    whole tree, because a file upstream deleted has to leave the scaffold or
    the merge would go on reintroducing it.

    A compile landing exactly what the branch already holds — the same tree,
    of the same upstream commit — records nothing and hands back the head
    standing there. What the scaffold is depends on the project's declaration
    as much as on the commit, so the same commit is compiled again whenever
    that declaration moves, which is what resolving a scaffold conflict does;
    and where neither moved, a second commit of identical bytes would advance
    the branch without advancing what it holds, leaving a merge for git to
    report with nothing in it. Both halves are read, because the trailer is
    part of what a scaffold commit carries: an upstream commit that changed
    nothing in the copied trees compiles the same tree under a new name, and
    the branch has to take that name or every later reading of what this
    project merged is a commit behind.
    """
    holding = recorded_scaffold(root, repository, source, package, commit)
    head = branch_head(root, source.branch)
    if (
        head
        and holding.tree == tree_at(root, head)
        and compiled_at(root, head) == commit
    ):
        return head
    parents = [head] if head else []
    recorded_commit = recorded(
        root, holding.tree, scaffold_message(source, holding.built), parents
    )
    git(
        "-C",
        str(root),
        "update-ref",
        f"refs/heads/{source.branch}",
        recorded_commit,
        *parents,
    )
    return recorded_commit


def adopt(
    root: Path,
    repository: Path,
    source: ScaffoldSource,
    package: str,
    base: str,
) -> str:
    """Root the scaffold branch at the commit this project was stamped from.

    Two steps, once per project. The branch is rooted at ``scaffold(base)``,
    the tree the project started from, and a merge recording that root as an
    ancestor — ``-s ours``, so none of upstream's bytes are applied now — is
    what makes git's merge base the adoption commit from then on. Without it
    the two histories are unrelated and every update would offer the whole
    scaffold as new.

    Both or neither: a merge that refuses -- over a staged change, or a hook
    saying no -- puts the branch back where it stood. A branch rooted and
    never recorded is the one state adoption cannot recover from, since it is
    what every later adoption reads as having happened already.
    """
    standing = branch_head(root, source.branch)
    recorded_commit = advanced(root, repository, source, package, base)
    try:
        git(
            "-C",
            str(root),
            "merge",
            "--strategy=ours",
            "--allow-unrelated-histories",
            "--no-edit",
            "-m",
            f"adopt {source.branch} at {short_sha(base)}\n\n"
            f"Records which commit of {source.project} this project's copied half "
            f"was stamped from, so an update is a merge against that base rather "
            f"than against nothing.\n\n{SCAFFOLD_TRAILER}: {base}\n",
            recorded_commit,
        )
    except sh.ErrorReturnCode:
        branch = f"refs/heads/{source.branch}"
        if standing:
            git("-C", str(root), "update-ref", branch, standing, recorded_commit)
        else:
            git("-C", str(root), "update-ref", "-d", branch, recorded_commit)
        raise
    return recorded_commit


class MergePlan(BaseModel, frozen=True):
    """What a merge of the scaffold branch has in front of it, before it runs."""

    base: str
    """The scaffold commit both sides descend from."""

    upstream_only: list[str]
    """Changed or added upstream alone: taken whole, and nobody reads a diff.

    A module upstream added is here rather than in a class of its own, because
    the adopter's side of it is the same either way: nothing of theirs is at
    stake, and the merge lands it without asking.
    """

    both_sides: list[str]
    """Changed on both: git reconciles what it can and reports the rest."""


class MergeOutcome(BaseModel, frozen=True):
    """What the merge did, in the terms an update report is written in."""

    plan: MergePlan
    conflicted: list[str]

    def fast_forwarded(self) -> list[str]:
        """Files the merge took whole, because only upstream changed them."""
        return self.plan.upstream_only

    def merged_clean(self) -> list[str]:
        """Files changed on both sides that git reconciled without help."""
        return [path for path in self.plan.both_sides if path not in self.conflicted]

    def complete(self) -> bool:
        """Whether the merge finished, or left conflicts for somebody to resolve."""
        return not self.conflicted

    def spelled(self) -> str:
        """The three counts an update reports, whichever way it went."""
        return (
            f"{len(self.fast_forwarded())} fast-forwarded, "
            f"{len(self.merged_clean())} merged clean, "
            f"{len(self.conflicted)} conflicted"
        )


def planned(root: Path, branch: str) -> MergePlan:
    """Which files each side changed since the base, read before the merge.

    Before, because the merge moves HEAD and the question is about where HEAD
    was. The split is the one an update's report is written in: what arrived
    for free, and what somebody may have to look at.
    """
    base = git.out("-C", str(root), "merge-base", "HEAD", f"refs/heads/{branch}")
    ours = {*git.lines("-C", str(root), "diff", "--name-only", base, "HEAD")}
    theirs = {
        *git.lines("-C", str(root), "diff", "--name-only", base, f"refs/heads/{branch}")
    }
    return MergePlan(
        base=base,
        upstream_only=sorted(theirs - ours),
        both_sides=sorted(theirs & ours),
    )


def unresolved(root: Path) -> list[str]:
    """Every path this checkout's merge still holds unmerged in its index.

    The index rather than the working tree, because that is what git answers
    a commit with: a file edited into shape and not added back is resolved to
    its reader and unmerged to git, and it is git that refuses the commit.
    """
    return git.lines("-C", str(root), "diff", "--name-only", "--diff-filter=U")


def merging(root: Path) -> str:
    """The commit a merge standing in this checkout is bringing in, or nothing.

    Git writes ``MERGE_HEAD`` when a merge stops for a resolution and removes
    it when the merge commit lands, so this is what says a merge is still
    open — and whose commit it is open on, which is how an update tells its
    own interrupted pass from a merge somebody else started.

    A directory that is no repository answers the same as one holding no
    merge, which is git's 128 beside its 1: nothing is standing there either
    way, and this is asked before anything has established that the root is a
    checkout at all.
    """
    return git.out(
        "-C",
        str(root),
        "rev-parse",
        "--verify",
        "--quiet",
        "MERGE_HEAD",
        _ok_code=[0, 1, 128],
    )


def concluded(root: Path) -> str:
    """Commit the merge standing in this checkout, and hand back the commit.

    Git's own prepared message, so the commit reads as the merge it is. And
    ``--no-verify``, which is not a way around a gate: a commit concluding a
    merge is mid-transaction by exactly the definition the drift guard cannot
    be satisfied at, since the generated trees are compiled from declarations
    the resolution has just rewritten and the regeneration that settles them
    comes after. Git draws the same line — a merge it completes on its own
    runs ``pre-merge-commit`` rather than ``pre-commit`` — so the guard sees
    only the merges somebody had to resolve by hand, and the commit after
    this one is read by it as always.
    """
    git("-C", str(root), "commit", "--no-verify", "--no-edit")
    return git.out("-C", str(root), "rev-parse", "HEAD")


def merged(root: Path, source: ScaffoldSource) -> MergeOutcome:
    """Merge the scaffold branch into this checkout, and say what happened.

    The one operation here that touches the working tree, and it is an
    ordinary merge: what it leaves conflicted is resolved the way every other
    conflict in this repository is, and concluded by the update that started
    it rather than by hand.
    """
    # lup: defer: unmeasured -- whether git's rename detection carries an
    # adopter's moved copies of scaffold files through this merge, rather than
    # reading each as a delete plus an add; the divergence probe that sized
    # this merge paired files by path only. An adopter that moved its copied
    # half around (nori did) is the case to try it on
    plan = planned(root, source.branch)
    git(
        "-C",
        str(root),
        "merge",
        "--no-edit",
        f"refs/heads/{source.branch}",
        _ok_code=[0, 1],
    )
    return MergeOutcome(plan=plan, conflicted=unresolved(root))


def locked_git_source(root: Path, distribution: str) -> str:
    """The git URL uv resolved one distribution from, as the lock records it.

    `uv.lock` is where the *resolved* commit is written down — a project
    pinned to a branch names no commit in its manifest, and the lock names the
    one it actually got. Read through `tomllib` rather than matched, because
    what is wanted is one field of one table and the file is a document.
    """
    lock = root / "uv.lock"
    if not lock.is_file():
        return ""
    with lock.open("rb") as handle:
        document = tomllib.load(handle)
    match document:
        case {"package": [*packages]}:
            held = packages
        case _:
            return ""
    for package in held:
        match package:
            case {"name": str(name), "source": {"git": str(url)}} if (
                name == distribution
            ):
                return url
    return ""


def pinned_commit(root: Path, distribution: str = DISTRIBUTION) -> str:
    """Which commit of the library this project currently resolves.

    The fragment of the lock's git URL, which is where uv writes the commit it
    resolved — so a project pinned to a branch answers with the commit that
    branch stood at when it last locked, which is the fact an update needs.
    """
    return urlsplit(locked_git_source(root, distribution)).fragment
