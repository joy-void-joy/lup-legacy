# lup: ignore[import-re, re-call]
# The marker head this reads is `lup.harness.codescan.markers` syntax, matched the way
# that module matches the rest of it; no format with a parser of its own is
# involved on either side.
"""The wake conditions a deferral states, in spellings the checker resolves.

A `# lup: defer[<condition>]:` note parks work behind something other than
this note can check. A condition written as prose is rendered into the notes
listing and read by whoever happens to look — which is the half of a gate
that does not work. A deferral is dormant precisely because it is correct to
be dormant now, so nobody has any reason to read it until the moment it stops
being correct, and that is the one moment nothing announced.

So a condition written in one of the spellings below is *resolved* rather
than printed. Each names a fact about this checkout that has a definite
answer offline — a branch being in play, a path ceasing to exist — and
`dev check` asks it on every run. While the answer is no the note stays where
it was, listed among the other deferrals and gating nothing. When the answer
turns yes the check fails, once, at the moment the condition the author named
actually came true.

Where the note *lives* is the other half, and the one that decides whether
any of this arrives in time. A note about a branch is written by somebody
standing somewhere else — usually the integration branch — and the person it
concerns is on the branch it names, in a checkout that has no copy of it and
will not until they merge. Reading only the working tree therefore delivers
every such warning exactly one step after the step it was written to change.
So the integration branch is read too, for the notes naming the branch in
hand, and those wake on that branch rather than on whoever merges later.

What this cannot do is reach a checkout that does not carry this code. A
branch without it runs the check it has, so its warning still arrives with
the merge that brings the tooling — no worse for these gates, and no better.
Every branch carrying it is reached on its first run.

Prose keeps working and keeps meaning what it meant. A condition this module
does not recognise is not an error and not a typo to be corrected: "until the
v2 API ships" is a real gate that this checkout simply cannot see, and the
listing carrying it to a reader is the whole of what it ever did. What
changes is only that a condition which *can* be resolved does not settle
for being read.

The spellings are a union rather than a table of branches, so naming another
kind of fact is adding a member that answers for itself. Each takes the
`<keyword>:<argument>` shape, which is what keeps prose and gate
distinguishable without either having to be quoted or escaped: a keyword is
one word, so prose that happens to contain a colon names no keyword this
union declares and falls through to being prose.
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import sh
from pydantic import BaseModel

from lup.harness.codescan.markers import (
    MarkerComment,
    NoteKind,
    find_feedback,
    scan_mode_for,
)
from lup.devtools.dev.branches import get_integration_branch, is_ancestor
from lup.devtools.dev.comments import FoundComment
from lup.devtools.dev.records import read_record
from lup.execution.shell import git
from lup.workspace.paths import project_root

# A resolvable condition is `<keyword>:<argument>`, and the keyword is a
# single word — which is what separates it from prose that merely contains a
# colon. Matched rather than split because the shape is structured, and a
# pattern stating the whole of it at once cannot half-match the way a split
# followed by a length check can. The argument runs to the end, newlines
# included, because a continuation line joins into a note's text before this
# ever sees it.
GATE_RE = re.compile(r"^(?P<keyword>[\w-]+)\s*:\s*(?P<argument>.*)$", re.DOTALL)


def current_branch() -> str:
    """The branch this checkout is standing on, empty on a detached head.

    Empty rather than an error, because a detached head is a legitimate place
    to run a check from and no gate that names a branch can match one. It
    falls through to the questions that do not need it.
    """
    return git.out("branch", "--show-current").strip()


def readable_ref(branch: str) -> str:
    """The ref this checkout reads that branch through, local first, or empty.

    A clone holds the branch it checked out and nothing else, and a pull
    request's checkout stands on no branch at all, so a name that is a local
    ref in a working checkout is only a remote-tracking one there. Both
    answers are the same branch and the ref that answered is returned, because
    every caller goes on to hand it to git.

    It is asked of the branch under question *and* of the branch that question
    is settled against. Asked of one alone, a subject resolved through
    ``origin/`` is compared with a bare integration name that resolves to
    nothing, so `git merge-base --is-ancestor` fails on its second argument
    and the landing verdict comes back false — reported as
    "origin/main has not reached main", which is one ref declining to have
    reached itself.
    """
    for ref in (branch, f"origin/{branch}"):
        try:
            git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except sh.ErrorReturnCode:
            continue
        return ref
    return ""


class GateVerdict(BaseModel, frozen=True):
    """Whether a condition has come true, and what the tree said when asked.

    ``evidence`` is what the checkout answered rather than a restatement of
    the gate, because a woken deferral is read by somebody who did not write
    it and is deciding whether to act now. "has reached dev" tells them the
    thing they would otherwise go and check by hand; "the gate fired" sends
    them to do it.
    """

    fired: bool
    evidence: str


class Gate(BaseModel, frozen=True):
    """One deferral's condition, spelled so this checkout can resolve it.

    ``keyword`` is the word before the colon, and belongs to the class rather
    than to an instance: it is what the spelling *is*, so a member owns it the
    way it owns its own name. ``argument`` is everything after, unparsed,
    because what a keyword takes is that keyword's business.
    """

    keyword: ClassVar[str]
    argument: str

    def asked(self) -> GateVerdict:
        """Whether this checkout says the condition has come true."""
        raise NotImplementedError

    def spelling(self) -> str:
        """This gate written the way it appears inside the bracket."""
        return f"{self.keyword}:{self.argument}"


class BranchInPlay(Gate, frozen=True):
    """`branch:<name>` — fires wherever that branch has become somebody's problem.

    Three moments, and the first is the one that matters. **Standing on the
    branch fires it**, because a note about a branch is written for whoever is
    working on that branch, and they are the last people to see it: the note
    lives wherever it was written — usually the integration branch — and their
    checkout will not carry it until they merge. Waking only on arrival would
    reach them one step after the step it was meant to change, which is not a
    warning, it is a post-mortem.

    Landing fires it too, for the run after nobody acted.

    A branch this checkout cannot see does **not** fire, and that is the ruling
    that keeps the whole gate usable. Absence looks identical either way: a
    branch pruned after landing and a branch that was simply never fetched are
    the same missing ref, and the second is the ordinary case rather than the
    rare one — a CI job clones the ref under test and nothing else, so every
    feature branch in the repository is missing there. Firing on absence
    therefore turns every build red for conditions none of which came true,
    which is precisely the branch a reader learns to ignore. So absence stays
    dormant and says it could not see.

    Which left one branch silent forever: landed, deleted in the same sweep,
    and asked about afterwards. The answer is not to read absence harder but
    to have written the answer down while it was still readable — `git delete`
    judges containment off the ref it is about to remove, and records the
    verdict beside the branch. So a missing ref is asked of that record, and
    fires only where something in this clone once watched the branch land. A
    CI clone that never fetched the branch never deleted it either and holds
    no such record, so it stays dormant for the reason it always did.
    """

    keyword: ClassVar[str] = "branch"

    def visible(self) -> str:
        """The ref this checkout can read the named branch through, or empty.

        The remote-tracking ref counts and is answered with, because a full
        clone that has never checked a branch out still knows what it is and
        where it sits against the integration branch — which is the whole of
        what the landing question needs. Returning which ref answered rather
        than that one did keeps the caller from having to guess again.
        """
        return readable_ref(self.argument)

    def recorded(self) -> GateVerdict:
        """The verdict for a ref this checkout cannot read, from what lup kept.

        Asked of the same checkout the ref questions are asked of, because a
        branch's record is shared by every worktree of the repository the
        deletion happened in — and a clone where it did not happen holds
        nothing, which is the dormant answer the ruling above requires.
        """
        landed = read_record(self.argument).landed_in
        if landed:
            return GateVerdict(
                fired=True,
                evidence=(
                    f"{self.argument} is gone, and reached {landed} before it "
                    "was deleted"
                ),
            )
        return GateVerdict(
            fired=False,
            evidence=(
                f"{self.argument} is not in this checkout, which says "
                "nothing about whether it landed"
            ),
        )

    def asked(self) -> GateVerdict:
        if not self.argument:
            return GateVerdict(fired=True, evidence="names no branch")
        if current_branch() == self.argument:
            return GateVerdict(
                fired=True,
                evidence=(
                    f"you are on {self.argument} — this note was written about "
                    "this branch, from wherever it was left"
                ),
            )
        integration = get_integration_branch()
        readable = self.visible()
        if not readable:
            return self.recorded()
        landed_in = readable_ref(integration)
        if not landed_in:
            return GateVerdict(
                fired=False,
                evidence=(
                    f"{readable} cannot be judged here: this checkout reads "
                    f"{integration} through no ref, so whether anything has "
                    "landed is a question it cannot answer"
                ),
            )
        if is_ancestor(readable, landed_in):
            return GateVerdict(
                fired=True, evidence=f"{readable} has reached {landed_in}"
            )
        return GateVerdict(
            fired=False, evidence=f"{readable} has not reached {landed_in}"
        )


class PathGone(Gate, frozen=True):
    """`gone:<path>` — fires once that path no longer exists.

    Resolved against the project root rather than the working directory, so
    the gate says the same thing from wherever the check was run.
    """

    keyword: ClassVar[str] = "gone"

    def asked(self) -> GateVerdict:
        if not self.argument:
            return GateVerdict(fired=True, evidence="names no path")
        if (project_root() / Path(self.argument)).exists():
            return GateVerdict(fired=False, evidence=f"{self.argument} still exists")
        return GateVerdict(fired=True, evidence=f"{self.argument} is gone")


DECLARED_GATES: list[type[Gate]] = [BranchInPlay, PathGone]
"""Every spelling a bracketed deferral can be resolved in.

A list of the members rather than a keyword-to-class mapping written out
beside them, because the keyword is already on the class and a second copy of
it here is the pair that drifts.
"""


def parse_gate(
    condition: str | None, declared: list[type[Gate]] = DECLARED_GATES
) -> Gate | None:
    """The gate a condition spelled, or ``None`` where it stated prose.

    ``None`` covers three things that are all the same thing to a caller: a
    deferral that stated no condition at all, one whose condition names no
    keyword this union declares, and one whose condition is prose that happens
    to contain a colon. Each is a note this checkout cannot resolve and must
    therefore carry to a reader, which is what the listing already does.
    """
    if condition is None:
        return None
    stated = GATE_RE.match(condition)
    if stated is None:
        return None
    for gate in declared:
        if gate.keyword == stated.group("keyword"):
            return gate(argument=stated.group("argument").strip())
    return None


def inbound_notes(
    branch: str,
    integration: str,
    find: Callable[[str, str], list[MarkerComment]] = find_feedback,
) -> list[FoundComment]:
    """Deferrals on *integration* whose gate names *branch*.

    This is the half that makes a branch gate reach anybody in time. A note is
    written where its author stood — on the integration branch, or on whatever
    landed into it — and the person it concerns is standing somewhere else, on
    the branch it names, in a checkout that does not carry it and will not
    until they merge. Scanning only the working tree therefore delivers the
    warning exactly one step after the step it was written to change.

    So the integration branch is read as well, and only for notes naming the
    branch in hand. Naming is what keeps this quiet: a check that surfaced
    every deferral on the integration branch would put the same wall of text
    in front of every branch in the repository, which is the listing nobody
    reads with extra steps.

    Two git calls plus one per file that actually holds a marker, rather than
    a second full scan of the tree: the grep names the candidates and only
    those are read back and parsed. A blob that has stopped being valid UTF-8
    or has vanished between the two calls is skipped, the way the working-tree
    scan skips what it cannot decode.

    A note the working tree has already answered is not delivered. Resolving
    a woken deferral is rewriting it as `solved:` with its words unchanged,
    and the branch it names does that in its own tree while the integration
    copy keeps the deferral until the branch lands — so reading the copy alone
    kept the one branch that had done what the note asked failing, with
    nothing it could do about it but land.
    """

    def answered(rel: str) -> list[str]:
        """The words of every note the working tree's copy of *rel* claims solved."""
        try:
            here = (project_root() / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        return [
            note.text
            for note in find(here, scan_mode_for(Path(rel)))
            if note.kind == NoteKind.solved
        ]

    if not branch:
        return []
    aimed_here = f"{BranchInPlay.keyword}:{branch}"
    try:
        candidates = git.lines(
            # From the root, not from wherever this was invoked. `git grep`
            # scopes to the working directory, so a check run from a nested
            # package — which the library's own suite is — would miss every
            # note outside it and report the quiet, passing answer. Asking
            # from the root also keeps the paths it names root-relative, which
            # is what `git show <ref>:<path>` wants back.
            "-C",
            str(project_root()),
            "grep",
            "--name-only",
            "--fixed-strings",
            f"defer[{aimed_here}]",
            integration,
        )
    except sh.ErrorReturnCode:
        return []
    found: list[FoundComment] = []  # lup: ignore[empty-collection] — scan fold
    for line in candidates:
        # `git grep <ref>` prefixes each hit with `<ref>:`, which is the ref
        # this was asked for and so is dropped rather than parsed back out.
        rel = line.removeprefix(f"{integration}:")
        try:
            text = git.out("-C", str(project_root()), "show", f"{integration}:{rel}")
        except sh.ErrorReturnCode:
            continue
        lines = text.splitlines()
        claimed = answered(rel)
        for comment in find(text, scan_mode_for(Path(rel))):
            # The grep names files, not notes, so a file that carries one note
            # aimed here carries all its others into this loop too. Keeping
            # them would let a deferral about some other branch fire on this
            # one purely for sharing a file, which is the noise the naming
            # rule exists to prevent.
            if comment.condition != aimed_here or comment.text in claimed:
                continue
            context = "\n".join(lines[comment.read_start - 1 : comment.read_end])
            found.append(
                FoundComment(file=rel, context=context, **comment.model_dump())
            )
    return found


class WokenDeferral(BaseModel, frozen=True):
    """A parked note whose stated condition this checkout says has come true."""

    file: str
    start_line: int
    end_line: int
    gate: str
    evidence: str
    text: str
    origin: str = ""
    """The ref this note was read from, empty for the working tree.

    Carried because "where the note is" and "where you are" have just stopped
    being the same place, and a reader sent to a file that does not exist in
    their checkout has been given a worse than useless instruction.
    """

    def reported(self) -> str:
        """This deferral as the failing check prints it.

        The note's own words come last and whole. What woke it is a line
        somebody skims; what it asked for is the thing they have to act on,
        and a deferral trimmed to fit a report is one whose instruction has to
        be gone and found in the file the report was supposed to save the trip
        to.
        """
        where = f"{self.origin}:" if self.origin else ""
        return (
            f"  {where}{self.file}:{self.start_line}-{self.end_line}  "
            f"[{self.gate}] {self.evidence}\n    {self.text}"
        )


class GateSweep(BaseModel, frozen=True):
    """Every resolvable gate this tree carries, and which of them fired.

    ``asked`` counts the gates resolved rather than the notes scanned, because
    that is the number saying whether this check is doing anything: a tree
    whose deferrals are all prose asks nothing, and a line reporting "ok"
    over no gates at all would read as a guarantee it never made.
    """

    asked: int
    woken: list[WokenDeferral]

    def lines(self) -> list[str]:
        """The check's header and one entry per woken deferral."""
        if not self.woken:
            return [f"woken deferrals: ok, {self.asked} gate(s) asked"]
        header = (
            f"woken deferrals: FAIL ({len(self.woken)} of {self.asked} gate(s) fired)"
        )
        return [header, *(deferral.reported() for deferral in self.woken)]


def sweep_gates(
    found: list[FoundComment],
    declared: list[type[Gate]] = DECLARED_GATES,
    origin: str = "",
) -> GateSweep:
    """Resolve every bracketed deferral in *found* that names a declared gate.

    Notes that stated prose are not counted as asked. They were never a
    question this checkout could put, so folding them into the total would
    report coverage the sweep does not have.

    *origin* is the ref these notes were read from, and travels onto whatever
    wakes so the report can say where to go and read it.
    """
    asked = 0
    woken: list[WokenDeferral] = []  # lup: ignore[empty-collection] — scan fold
    for comment in found:
        gate = parse_gate(comment.condition, declared)
        if gate is None:
            continue
        asked += 1
        verdict = gate.asked()
        if not verdict.fired:
            continue
        woken.append(
            WokenDeferral(
                file=comment.file,
                start_line=comment.start_line,
                end_line=comment.end_line,
                gate=gate.spelling(),
                evidence=verdict.evidence,
                text=comment.text,
                origin=origin,
            )
        )
    return GateSweep(asked=asked, woken=woken)


def sweep_all(
    found: list[FoundComment], declared: list[type[Gate]] = DECLARED_GATES
) -> GateSweep:
    """Every gate this checkout can put: its own tree's, and the ones aimed at it.

    The two halves answer different failures and neither covers the other. The
    working tree's gates catch a condition that came true under a note you are
    carrying; the integration branch's catch a note somebody left about the
    branch you are standing on, which your checkout has no copy of. A sweep
    that ran only the first is the one that reaches its reader a merge too
    late.
    """
    integration = get_integration_branch()
    here = sweep_gates(found, declared)
    inbound = sweep_gates(
        inbound_notes(current_branch(), integration), declared, origin=integration
    )
    return GateSweep(
        asked=here.asked + inbound.asked, woken=[*here.woken, *inbound.woken]
    )
