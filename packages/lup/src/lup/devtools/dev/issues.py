"""Reading a project's tracker issues, and answering them where they are read.

The library stays free of any forge: `lup.resolver` knows an issue only as a
number, a URL and some text. Reaching a tracker is workflow tooling's job, so
it lives here beside the pull-request queries that already use `gh`, and a
project on a different tracker supplies its own reader rather than waiting for
the library to learn an API it has no business knowing.
"""

import json
import logging
import shlex
from collections.abc import Iterator
from typing import Literal
from html import escape

import sh
from pydantic import BaseModel, Field

from lup.devtools.project import Tracker
from lup.devtools.utils import (
    decode_stderr,
    gh,
    names_same_repository,
    repository_slug,
)
from lup.resolver.models import IssueEvidence

logger = logging.getLogger(__name__)

EXCLUDED_LABEL = "resolver-skip"
"""Which label withholds an issue from intake.

Opt-out rather than opt-in: opt-in means remembering to label, and what goes
unlabelled goes unfixed. The name is this project's choice and no more, so a
caller passes its own.
"""

# lup: ignore[constant-declaration] — the fields the models below parse, spelled
# as `gh issue list --json` names them
ISSUE_FIELDS = "number,url,title,body,labels"


type TrackerVerb = Literal["comment", "close", "reopen"]
"""The operations `dev tracker` performs, closed by what restores each one.

A comment is answered, a close is reopened, a reopen is closed. Everything
else a forge offers -- merging, publishing, editing a repository's settings,
deleting a branch on the way out -- is left to `gh`, where the permission
policy asks about it in front of somebody. The list is short on purpose: this
command runs inside an allowed devtools invocation, so nothing downstream of
it can ask, and a verb added here is a verb nobody is consulted about.
"""

# lup: ignore[constant-declaration] — gh's own words, quoted to be recognized
DISABLED_ISSUES = "has disabled issues"
"""What gh says when the repository accepts no issues at all.

Matched on the phrase rather than an exit code, which is the same for every
refusal gh makes. A phrase gh rewords stops being recognized and the failure
reads as it did before this, which is the safe direction for a message that
only ever adds advice.
"""


class TrackerRoutes(BaseModel, frozen=True):
    """Every repository this checkout's tooling may name, and the way past it.

    `origin` answers for the first, and it answers alone: a fork whose origin
    is the fork reports to the fork, which is where work done in a fork
    belongs. What makes the upstream reachable is that somebody declared it,
    not that a remote happens to carry a conventional name.
    """

    own: str = ""
    declared: list[Tracker] = []

    def owning(self, component: str) -> str:
        """The declared tracker whose components claim this one, if any."""
        return next(
            (entry.repository for entry in self.declared if entry.claims(component)),
            "",
        )

    def chosen(self, route: list[str], named: str = "", component: str = "") -> str:
        """Where this operation lands, refusing a repository nobody declared.

        Unnamed, the owning component decides and this checkout answers for
        whatever no tracker claims -- which is the routing a report needs: a
        defect in a dependency this tree cannot edit belongs to whoever can
        edit it, and everything else belongs here. Where a report belongs is
        not where it may be filed unasked, which :meth:`claimed_elsewhere`
        answers for a new one.

        Named, the repository has to be this one or a declared tracker.
        Returned in the spelling that was declared rather than the one that
        was typed, so a URL and a bare pair reach gh identically.
        """
        if not named:
            return self.owning(component) or self.own
        reachable = [self.own, *(entry.repository for entry in self.declared)]
        found = next(
            (
                candidate
                for candidate in reachable
                if candidate and names_same_repository(named, candidate)
            ),
            "",
        )
        if not found:
            raise RuntimeError(self.refusal(named, route))
        return found

    def refusal(self, named: str, route: list[str]) -> str:
        """Why that repository is out of reach here, and what does reach it.

        The route matters more than the refusal. What this declines is
        reaching another repository *unsupervised* -- it runs inside an
        allowed devtools call, where nothing further can ask. `gh` reaches it
        under the permission policy, which does ask, so the way out is not
        blocked and the answer says so.

        Spelled with the arguments already given rather than described,
        because a refusal reading "use gh instead" leaves somebody rebuilding
        an invocation they had written correctly the first time.
        """
        listed = [f"  {entry.repository} — {entry.what}" for entry in self.declared]
        return "\n".join(
            [
                f"{named} is neither this checkout's repository"
                f" ({self.own or 'which origin does not name'}) nor a tracker"
                " this project declares.",
                *(
                    ["Declared trackers:", *listed]
                    if listed
                    else ["This project declares no other tracker."]
                ),
                "To reach it deliberately, run gh directly — the permission"
                " policy asks about that:",
                f"  {shlex.join(['gh', *route, '--repo', named])}",
            ]
        )

    def claimed_elsewhere(self, component: str, invocation: list[str]) -> str:
        """Why a new report routed past this checkout is not filed, and what files it.

        The permission policy reads the words and routing reads the
        component, so an unnamed report reads as one filed on this checkout's
        own repository, the form the policy lets through unasked. Filed where
        a declared tracker claims its component, it would reach that
        project's watchers with nobody asked. It stops instead, and prints
        the same invocation naming the tracker -- the form the policy asks
        about -- spelled with the arguments given, as :meth:`refusal` spells
        its gh line.

        Empty where the component stays here, a tracker naming this
        checkout's own repository in another spelling included.
        """
        tracker = self.owning(component)
        if not tracker or names_same_repository(tracker, self.own):
            return ""
        return "\n".join(
            [
                f"{component} belongs to {tracker}, not to this checkout's"
                f" repository ({self.own or 'which origin does not name'}).",
                "A report filed there reaches another project's watchers, so it"
                " is not filed unasked. Naming the tracker files it, and the"
                " permission policy asks about that:",
                f"  {shlex.join([*invocation, '--repo', tracker])}",
            ]
        )


def issue_arguments(verb: TrackerVerb, number: int, note: str = "") -> list[str]:
    """The gh words one compensable operation is spelled with, less its repository.

    Built apart from the call that runs them because a refusal has to print
    the same invocation it declined to make: the way past this command is to
    run gh directly, and a route rebuilt by hand beside the one performed is
    a route that goes stale the first time either moves.

    A note is the whole point of a comment and optional on the other two,
    where it is the sentence that explains a state change to whoever was
    watching the issue. gh spells both the same way, so the only difference
    is which of them may be empty.
    """
    match verb:
        case "comment":
            return ["issue", "comment", str(number), "--body", note]
        case "close" | "reopen":
            carried = ["--comment", note] if note else []
            return ["issue", verb, str(number), *carried]


def act_on_issue(
    verb: TrackerVerb, number: int, repository: str, note: str = ""
) -> str:
    """Perform one compensable operation on an issue, and say where it landed."""
    return gh.out(*issue_arguments(verb, number, note), "--repo", repository).strip()


def disabled_issues_advice(failure: str, routes: TrackerRoutes) -> str:
    """What to add when the repository a report was routed to takes no issues.

    Without it a friction report is lost outright, and recovering it means
    running the reporter from another checkout and explaining by hand where
    the evidence came from — while a route to a tracker that takes it is
    declared all along, because this project declares trackers.

    Named rather than taken. Re-filing somebody's report on another project's
    tracker unasked moves it out of sight of everyone watching the first one,
    and a command that did that quietly would be deciding whose problem this
    is.

    Empty for every other failure, which leaves the message gh gave exactly
    as it was.
    """
    if DISABLED_ISSUES not in failure or not routes.declared:
        return ""
    return "\n".join(
        [
            "That repository accepts no issues. This project declares these"
            " trackers, and `--repo` names one:",
            *(f"  {entry.repository} — {entry.what}" for entry in routes.declared),
        ]
    )


class FrictionReport(BaseModel, frozen=True, extra="forbid"):
    """Observed workflow friction in the shape the improvement loop consumes."""

    summary: str = Field(min_length=1, description="Concise issue title")
    component: str = Field(min_length=1, description="Component that owns the fix")
    command: str = Field(min_length=1, description="Exact command that was run")
    error: str = Field(min_length=1, description="Exact error that was observed")
    state: str = Field(
        min_length=1, description="State the failed operation left behind"
    )
    recovery_cost: str = Field(min_length=1, description="Work required to recover")

    def body(self) -> str:
        """Render the evidence without letting its contents alter Markdown."""
        sections: list[str] = [
            f"## Owning component\n\n<pre><code>{escape(self.component)}</code></pre>",
            f"## Exact command\n\n<pre><code>{escape(self.command)}</code></pre>",
            f"## Exact error\n\n<pre><code>{escape(self.error)}</code></pre>",
            f"## State left behind\n\n<pre><code>{escape(self.state)}</code></pre>",
            f"## Recovery cost\n\n<pre><code>{escape(self.recovery_cost)}</code></pre>",
        ]
        return "\n\n".join(sections)

    def file(self, repository: str = "", issue: int | None = None) -> str:
        """File or correct this report against the checkout's explicit repository."""
        slug = repository or repository_slug()
        if not slug:
            raise RuntimeError(
                "cannot file friction: origin names no GitHub repository"
            )
        operation = ["create"] if issue is None else ["edit", str(issue)]
        arguments = ["issue", *operation, "--repo", slug]
        arguments.extend(["--title", self.summary, "--body", self.body()])
        return gh.out(*arguments).strip()


class IssueLabel(BaseModel):
    """One label as `gh issue list --json labels` returns it."""

    name: str = ""


class IssueRow(BaseModel):
    """One issue as `gh issue list --json` returns it (aliases are gh's names)."""

    number: int
    url: str
    title: str
    body: str = ""
    labels: list[IssueLabel] = []

    def excluded_by(self, label: str) -> bool:
        return any(applied.name == label for applied in self.labels)

    def evidence(self) -> IssueEvidence:
        return IssueEvidence(
            number=self.number, url=self.url, title=self.title, body=self.body
        )


class OpenIssues(BaseModel, frozen=True):
    """What a tracker answered, and whether it answered at all.

    The two are separate because the list cannot tell them apart: a
    repository with nothing open and a tracker that refused the credential
    both read as no issues, and a caller acting on the second as though it
    were the first plans from an emptiness nobody established: with an
    expired token, a bare list prints "0 open issue(s)" and exits zero,
    which is what a clean tracker looks like.
    """

    reached: bool
    issues: list[IssueEvidence] = Field(default=[])
    why: str = ""
    """What the tracker said when it refused, for a reader who has to fix it."""


def read_open_issues(
    excluded: str = EXCLUDED_LABEL, limit: int = 200, repository: str = ""
) -> OpenIssues:
    """Every open issue a run should weigh, oldest first, and whether they are all.

    Oldest first because that is the order they were found in, and a planner
    reading them in that order sees a later issue's context already
    established by the earlier one it followed from.
    """
    slug = repository or repository_slug()
    arguments = ["issue", "list", "--state", "open", "--limit", str(limit)]
    if slug:
        arguments.extend(["-R", slug])
    try:
        rows = json.loads(gh.out(*arguments, "--json", ISSUE_FIELDS))
    except (sh.ErrorReturnCode, json.JSONDecodeError) as error:
        logger.warning("could not read open issues: %s", error)
        why = decode_stderr(error) if isinstance(error, sh.ErrorReturnCode) else ""
        return OpenIssues(reached=False, why=why.strip() or str(error))
    issues = [IssueRow.model_validate(row) for row in rows]
    kept = [issue for issue in issues if not issue.excluded_by(excluded)]
    return OpenIssues(
        reached=True,
        issues=[issue.evidence() for issue in sorted(kept, key=lambda row: row.number)],
    )


def fetch_open_issues(
    excluded: str = EXCLUDED_LABEL, limit: int = 200, repository: str = ""
) -> list[IssueEvidence]:
    """The issues alone, for a caller that proceeds either way.

    A tracker that cannot be reached yields nothing here. Intake still has the
    tree's own notes, and a run that plans without the issues is better than a
    run that will not start — but a caller whose whole answer *is* the
    tracker's reads :func:`read_open_issues` instead, and says which it got.
    """
    return read_open_issues(excluded, limit, repository).issues


def comment_on_issues(
    issues: list[IssueEvidence], body: str, repository: str = ""
) -> list[int]:
    """Say where an issue was answered, on the issue. Never close it.

    A run's reviewer passing is not a human having read the code, so closing
    on the run's own judgement claims more than it knows. The comment is the
    honest half: it reaches whoever is watching the issue, and leaves the
    decision to close with them.
    """
    slug = repository or repository_slug()

    def commented() -> Iterator[int]:
        for issue in issues:
            arguments = ["issue", "comment", str(issue.number), "--body", body]
            if slug:
                arguments.extend(["-R", slug])
            try:
                gh.out(*arguments)
            except sh.ErrorReturnCode as error:
                logger.warning(
                    "could not comment on %s: %s",
                    issue.reference(),
                    decode_stderr(error),
                )
                continue
            yield issue.number

    return list(commented())
