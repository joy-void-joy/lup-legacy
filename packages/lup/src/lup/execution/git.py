"""What a git repository answers, each question asked one way.

The same dozen questions — where is the top, where is the shared directory,
which branch is this, does that ref name a commit, is one commit in another's
history — are asked across the library, and asked separately each one comes
back with its own failure answer (a raise here, ``""`` there, ``None`` or
``False`` elsewhere) and its own path form (relative where git printed it
so, absolute where somebody remembered ``--path-format``). A
:class:`Repository` asks each of them once, with one failure contract per
question and every location absolute.

It runs git through a :class:`~lup.execution.process.ProcessLauncher`, the
same seam the resolver and the network probes run through, so a test hands
it a scripted launcher and a sandboxed caller hands it a checked one. The
local launcher it defaults to captures through pipes, which is what the
shared ``git`` command's ``--no-pager`` and ``color.ui=never`` exist to
arrange on a terminal.

Two copies stay outside it by construction, because they run on a bare
interpreter that cannot import this library: the policy host's
``git_answers`` and the drift fold's ``asked``. A test pins each to the
answers given here.
"""

from pathlib import Path
from typing import Self

from pydantic import BaseModel

from lup.execution.process import (
    ExitStatus,
    LaunchRequest,
    LocalProcessLauncher,
    ProcessLauncher,
)
from lup.types import EnvVars


class GitError(RuntimeError):
    """A question git declined to answer, in git's own words."""

    def __init__(self, arguments: list[str], status: ExitStatus) -> None:
        self.arguments = arguments
        self.status = status
        super().__init__(
            f"`git {' '.join(arguments)}` exited {status.code}: {status.stderr.strip()}"
        )


class Worktree(BaseModel, frozen=True):
    """One checkout as `git worktree list --porcelain -z` describes it."""

    path: Path
    """Absolute, exactly as git records it — a space in it included."""

    head: str = ""
    """The commit checked out, empty for a bare repository."""

    branch: str = ""
    """The short name of the branch checked out, empty when detached or bare."""

    bare: bool = False
    detached: bool = False

    locked: bool = False
    lock_reason: str = ""
    """Whatever the locker passed, empty when they passed nothing."""

    prunable: bool = False
    prune_reason: str = ""

    @classmethod
    def listed(cls, fields: list[str]) -> Self:
        """The worktree one porcelain record names, one field per entry.

        A label this does not know is passed over, since git adds them.
        """
        path = Path()
        head = branch = lock_reason = prune_reason = ""
        bare = detached = locked = prunable = False
        for field in fields:
            match field.split(maxsplit=1):
                case ["worktree", named]:
                    path = Path(named)
                case ["HEAD", commit]:
                    head = commit
                case ["branch", ref]:
                    branch = ref.removeprefix("refs/heads/")
                case ["bare"]:
                    bare = True
                case ["detached"]:
                    detached = True
                case ["locked", *reason]:
                    locked, lock_reason = True, " ".join(reason)
                case ["prunable", *reason]:
                    prunable, prune_reason = True, " ".join(reason)
        return cls(
            path=path,
            head=head,
            branch=branch,
            bare=bare,
            detached=detached,
            locked=locked,
            lock_reason=lock_reason,
            prunable=prunable,
            prune_reason=prune_reason,
        )


class Repository:
    """The repository enclosing ``root``, asked through ``runner``.

    Two kinds of answer, and a question has exactly one of them. Where a
    question has a natural "no" — a ref naming nothing, a merge not in
    progress, a remote not configured, a commit not in another's history —
    a refusal is that "no" (``None`` or ``False``), because every caller
    reads it that way. Where it has none — the top, the branch, a count, the
    worktrees — a refusal raises :class:`GitError` carrying git's words.
    """

    def __init__(
        self,
        root: Path,
        runner: ProcessLauncher = LocalProcessLauncher(),
        environment: EnvVars | None = None,
    ) -> None:
        self.root = root
        self.runner = runner
        self.environment = environment or {}
        """Laid over the inherited environment for every question: an index
        of its own (``GIT_INDEX_FILE``), or what keeps a credential prompt
        from waiting on a terminal."""

    def run(self, *arguments: str, stream: bool = False) -> ExitStatus:
        """Launch git here with ``arguments``, whatever it answers.

        ``stream`` shows its output as it arrives, for a command somebody
        waits through — a fetch, a push — as well as capturing it.
        """
        return self.runner.launch(
            LaunchRequest(
                arguments=["git", *arguments],
                cwd=self.root,
                environment=self.environment,
                stream=stream,
            )
        )

    def answer(self, *arguments: str) -> str:
        """What git printed, less its trailing newline, or :class:`GitError`."""
        status = self.run(*arguments)
        if status.code != 0:
            raise GitError(list(arguments), status)
        return status.stdout.removesuffix("\n")

    def located(self, *flags: str) -> list[Path]:
        """The location ``rev-parse`` names for each flag, in order, absolute.

        Asked in one process, for a caller that needs several at once.
        """
        listed = self.answer("rev-parse", "--path-format=absolute", *flags)
        return [Path(line) for line in listed.splitlines()]

    def top(self) -> Path:
        """The top of the working tree ``root`` is in."""
        [top] = self.located("--show-toplevel")
        return top

    def git_dir(self) -> Path:
        """This checkout's own administrative directory."""
        [own] = self.located("--git-dir")
        return own

    def common_dir(self) -> Path:
        """The administrative directory every worktree of the repository shares."""
        [common] = self.located("--git-common-dir")
        return common

    def admin_dirs(self) -> list[Path]:
        """This checkout's directory and the shared one, once each where they agree.

        A linked worktree has two, and a question about git's administrative
        files has to read both: `config` and its lock live in the shared one,
        `config.worktree` in the checkout's own.
        """
        return list(dict.fromkeys(self.located("--git-dir", "--git-common-dir")))

    def branch(self) -> str:
        """The branch checked out, empty on a detached head."""
        return self.answer("branch", "--show-current")

    def resolves(self, ref: str) -> str | None:
        """The commit ``ref`` names here, or ``None`` where it names none."""
        status = self.run("rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
        named = status.stdout.strip()
        return named if status.code == 0 and named else None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether git confirms ``ancestor`` is in ``descendant``'s history.

        A name that resolves to nothing is not an ancestor of anything, so a
        refusal reads as ``False`` like the "no" git gives with exit 1.
        """
        return self.run("merge-base", "--is-ancestor", ancestor, descendant).code == 0

    def count(self, *revisions: str) -> int:
        """How many commits ``rev-list`` selects with ``revisions``."""
        return int(self.answer("rev-list", "--count", *revisions))

    def conflicted(self) -> list[Path]:
        """Every path the index holds unmerged, relative to the top."""
        listed = self.answer("diff", "--name-only", "--diff-filter=U")
        return [Path(line) for line in listed.splitlines() if line]

    def merging(self) -> str | None:
        """The commit a merge in progress is bringing in, ``None`` when settled."""
        status = self.run("rev-parse", "-q", "--verify", "MERGE_HEAD")
        named = status.stdout.strip()
        return named if status.code == 0 and named else None

    def remote_url(self, name: str) -> str | None:
        """Where remote ``name`` fetches from, ``None`` where it is not configured."""
        status = self.run("remote", "get-url", name)
        url = status.stdout.strip()
        return url if status.code == 0 and url else None

    def worktrees(self) -> list[Worktree]:
        """Every checkout of this repository, the main one first.

        Read NUL-separated, the one form git prints unquoted: a path with a
        space, or a lock reason carrying a quote, comes back whole.
        """
        listed = self.answer("worktree", "list", "--porcelain", "-z")
        records: list[list[str]] = [[]]
        for field in listed.split("\0"):  # lup: ignore[string-split] — NUL records
            if field:
                records[-1].append(field)
            else:
                records.append([])
        return [Worktree.listed(fields) for fields in records if fields]
