"""Which upstream commit a project's copied half actually corresponds to.

Adoption roots a branch once, at one commit, and records it as the merge base
for every later update — so the whole mechanism rests on that one argument.
Two wrong answers are easy to give and both look like success. Rooting at the
commit the pin already resolves to leaves the merge base and the merge target
the same commit, so the first update reports three zeros and every upstream
change the project never hand-ported stays untaken with nothing to say so.
Rooting far behind the copy re-offers changes the project applied by hand
long ago, in a layout it has since left.

Neither is a judgement anybody has to make blind. ``scaffold(commit)`` is a
pure function of upstream and the checkout is right there, so a candidate can
be *measured*: compile it, and count how many of its files this checkout
carries and how many it carries byte for byte. A copy stamped from one commit
and edited since reads highest at that commit and lower on either side of it,
which makes the answer a peak in a measurement rather than a preference — and
a base the measurement argues against is refused with the reading that
argues, rather than accepted in silence.
"""

from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

import sh
import typer
from pydantic import BaseModel

from lup.execution.git import Repository
import lup.devtools.dev.library as library
import lup.devtools.dev.scaffold as scaffold
from lup.devtools.utils import format_table, short_sha
from lup.execution.shell import git

CANDIDATE_DEPTH = 40
"""How many commits one round of a search measures.

The resolution rather than the range, and it buys both: a round spreads this
many measurements over whatever range it was given and the next spreads them
over the interval around the one that read highest, so a whole history is
covered by a handful of rounds. Measured on this repository, one compile of a
three-hundred-file copied half costs under half a second, and its sixteen
hundred candidates answer in seventy-eight measurements and fourteen seconds
— where measuring every one of them would take a quarter of an hour.
"""

FIT_MARGIN = Fraction(1, 10)
"""How much better a candidate must read before it contradicts the base given.

A tenth of the compiled half, in byte-identical files. Below that, two
neighbouring commits differing by one file would each call the other wrong;
above it, a base whose copy is a layout and a year behind stops being
plausible — the adoption that motivated this read 1 of 10 identical at the
base it was told to use.
"""


class ScaffoldFit(BaseModel, frozen=True):
    """How much of one compiled scaffold this checkout already holds.

    The whole reading, and it is three counts rather than a verdict: what the
    scaffold at that commit holds, how much of it the checkout has at all,
    and how much of it is there byte for byte. The last is the one that
    identifies a base, since a file the compile leaves identical to the
    project's own copy is a file that commit's merge would not have to
    consider.
    """

    commit: str
    """The upstream commit measured, in full."""

    compiled: int
    """How many files ``scaffold(commit)`` holds for this project."""

    carried: int
    """How many of them exist in the checkout, under any contents."""

    identical: int
    """How many of them the checkout holds byte for byte."""

    subject: str = ""
    """What that commit says it did, for a reader recognizing it."""

    def share(self) -> Fraction:
        """The identical files as a share of the compiled half.

        Exact, and comparable across candidates that compile different
        numbers of files — which neighbouring commits do, one of them being
        the commit that added a module.
        """
        if not self.compiled:
            return Fraction(0)
        return Fraction(self.identical, self.compiled)

    def spelled(self) -> str:
        """The reading, in the words a refusal and a report line both use."""
        return (
            f"{self.identical} of {self.compiled} compiled file(s) identical, "
            f"{self.carried} carried"
        )

    def reported(self) -> str:
        """The reading as one line, naming the commit it is of."""
        return (
            f"scaffold({short_sha(self.commit)}) against this checkout: "
            f"{self.spelled()}."
        )

    def row(self) -> list[str]:
        """The reading as a table row, for a search printing every candidate."""
        return [
            short_sha(self.commit),
            f"{self.identical}/{self.compiled}",
            f"{self.carried}/{self.compiled}",
            self.subject,
        ]


class FitSurvey(BaseModel, frozen=True):
    """Every candidate a search measured, newest first, and what it drew from."""

    tip: str
    """The commit the candidates were reachable from."""

    reachable: int
    """How many commits changed the copied half in the range, measured or not."""

    readings: list[ScaffoldFit]
    """What each measured candidate read, newest first."""

    def best(self) -> ScaffoldFit | None:
        """The candidate no other reads higher than, or nothing where none was.

        Ties go to the newest, the readings being newest first: commits that
        read alike compile the bytes this checkout holds alike, and rooting
        at the later one leaves the merge less to re-offer.
        """
        return max(self.readings, key=ScaffoldFit.share, default=None)

    def lines(self) -> list[str]:
        """The whole reading, for somebody deciding which commit to root at.

        Every candidate measured rather than the peak alone: a field with one
        clear peak and a field where six commits read alike are different
        answers, and only the table tells them apart.
        """
        peak = self.best()
        if peak is None:
            return [
                f"No commit reachable from {short_sha(self.tip)} that changed "
                f"the copied half could be measured, of {self.reachable} there."
            ]
        return [
            f"{len(self.readings)} of the {self.reachable} commit(s) that "
            f"changed the copied half before {short_sha(self.tip)} measured, "
            f"newest first:",
            self.table(),
            f"The peak is {short_sha(peak.commit)} ({peak.spelled()}): "
            f"`dev scaffold adopt --base {peak.commit}`.",
        ]

    def table(self) -> str:
        """Every reading, with the peak marked, for somebody choosing a base."""
        peak = self.best()
        return format_table(
            ["", "commit", "identical", "carried", "subject"],
            [
                ["*" if peak and one.commit == peak.commit else "", *one.row()]
                for one in self.readings
            ],
            aligns=["left", "left", "right", "right", "left"],
        )


def resolved(repository: Path, revision: str) -> str:
    """One revision of upstream as the commit it names, refused where it names none.

    Resolved in upstream's own clone, that being the repository the argument
    is about: a short sha, a tag or a branch all become the full commit the
    compile measures and the adoption trailer records.
    """
    found = Repository(repository).resolves(revision)
    if found is None:
        raise typer.BadParameter(
            f"{revision} names no commit in this project's upstream clone "
            f"({repository}). `dev scaffold fit` prints the candidates it "
            f"measured, newest first, with the commit each one names."
        )
    return found


def subject_of(repository: Path, commit: str) -> str:
    """What one upstream commit says it did, in its own summary line."""
    return git.out("-C", str(repository), "log", "-1", "--format=%s", commit)


def measured(
    root: Path,
    repository: Path,
    source: scaffold.ScaffoldSource,
    package: str,
    commit: str,
) -> ScaffoldFit:
    """Count how much of ``scaffold(commit)`` this checkout already holds.

    Compiled into a directory of its own and compared byte for byte, so the
    reading is about the bytes rather than about a timestamp or a path: a
    project that took this commit's copied half and edited some of it reads
    high here, and one that took a different commit's reads lower.
    """
    with TemporaryDirectory() as holding:
        contents = Path(holding) / "scaffold"
        built = scaffold.compiled(repository, commit, source, package, contents)
        carried = [path for path in built.files if (root / path).is_file()]
        identical = [
            path
            for path in carried
            if (root / path).read_bytes() == (contents / path).read_bytes()
        ]
        return ScaffoldFit(
            commit=commit,
            compiled=len(built.files),
            carried=len(carried),
            identical=len(identical),
            subject=subject_of(repository, commit),
        )


def candidates(
    repository: Path, source: scaffold.ScaffoldSource, tip: str
) -> list[str]:
    """Every commit worth measuring, newest first from ``tip``.

    Only the ones that changed the copied half, because the scaffold is a
    function of those trees alone: a commit touching nothing under them
    compiles the same bytes as its parent, so measuring it would report a
    second peak at a commit that carried nothing into anybody's copy. That
    is the range, whole — this repository's own history holds sixteen
    hundred of them and the walk costs a quarter of a second, while
    measuring them all would cost a quarter of an hour.
    """
    return git.lines(
        "-C",
        str(repository),
        "log",
        "--format=%H",
        tip,
        "--",
        *[each.upstream for each in source.roots],
    )


def strided(window: list[str], depth: int) -> list[str]:
    """At most ``depth`` of these commits, evenly spread, both ends kept.

    The ends because they bound what the round can conclude: a peak at the
    newest sample says the answer may be newer than the range, and one at the
    oldest says the copy may predate it.
    """
    if len(window) <= depth:
        return window
    stride = -(-len(window) // depth)
    spread = window[::stride]
    return spread if window[-1] in spread else [*spread, window[-1]]


def neighbourhood(window: list[str], spread: list[str], peak: str) -> list[str]:
    """The stretch of ``window`` between the samples either side of ``peak``.

    Where the next round looks: a strided round locates the peak to within
    one stride, so the commit itself is between the sample before it and the
    sample after — and those two bound a stretch the next round measures at
    its own resolution.
    """
    at = spread.index(peak)
    start = window.index(spread[at - 1]) if at else 0
    end = window.index(spread[at + 1]) if at + 1 < len(spread) else len(window) - 1
    return window[start : end + 1]


def surveyed(
    root: Path,
    repository: Path,
    source: scaffold.ScaffoldSource,
    package: str,
    tip: str,
    depth: int = CANDIDATE_DEPTH,
) -> FitSurvey:
    """Measure the range at descending resolution, closing on the peak.

    A dense walk back from the tip is the wrong shape for this question. A
    project's copy is behind upstream by however many changes nobody retyped,
    which on this repository's own history is hundreds of commits — forty of
    them cover two days, and an adoption can stand a year behind.
    So a round spreads its measurements over the whole range, and the next
    round measures the interval around whichever sample read highest, until a
    round is measuring every commit it was given.

    What that assumes is that the reading changes slowly from one commit to
    the next, and it does: neighbouring commits differ in one or two files of
    a copied half holding hundreds, where a base a month wrong differs in
    dozens. A stride cannot step over the peak without the samples on either
    side of it reading high.
    """
    reachable = candidates(repository, source, tip)
    # One reading per commit, kept across the rounds: a narrowing round
    # re-measures nothing an earlier round already compiled.
    taken: dict[str, ScaffoldFit] = {}

    def reading(commit: str) -> ScaffoldFit | None:
        """This commit's reading, measured once however often it is asked for."""
        if commit not in taken:
            try:
                taken[commit] = measured(root, repository, source, package, commit)
            except sh.ErrorReturnCode:
                # A commit predating one of the copied roots has no such tree
                # to archive, so there is no scaffold at it for anybody to
                # have been stamped from.
                return None
        return taken[commit]

    def closed_on(window: list[str]) -> None:
        """Measure this window at the resolution ``depth`` allows, then narrow."""
        spread = strided(window, depth)
        readings = [one for one in map(reading, spread) if one]
        if not readings or len(spread) >= len(window):
            return
        peak = max(readings, key=ScaffoldFit.share)
        narrowed = neighbourhood(window, spread, peak.commit)
        if len(narrowed) < len(window):
            closed_on(narrowed)

    if reachable:
        closed_on(reachable)
    return FitSurvey(
        tip=tip,
        reachable=len(reachable),
        readings=[taken[commit] for commit in reachable if commit in taken],
    )


def searched_tip(
    root: Path, repository: Path, distribution: str = library.DISTRIBUTION
) -> str:
    """Where a search starts: the commit the library pin resolves to.

    The pin rather than upstream's head, and not only to bound the walk. The
    copied half is compiled at whatever the pin resolves to, so a base later
    than the pin would ask the first merge to run backwards — an answer past
    the pin is one nobody could act on. A project pinning no commit has no
    such anchor, and the clone's own head is what is left.
    """
    return scaffold.pinned_commit(root, distribution) or resolved(repository, "HEAD")


def contradicting(
    named: ScaffoldFit, survey: FitSurvey, margin: Fraction = FIT_MARGIN
) -> ScaffoldFit | None:
    """The candidate that reads far enough above this one to argue against it."""
    best = survey.best()
    if best is None or best.commit == named.commit:
        return None
    return best if best.share() - named.share() >= margin else None


def settled_report(named: ScaffoldFit, survey: FitSurvey, margin: Fraction) -> str:
    """What a search that did not contradict the base has to say about it.

    A base nothing reads above is the answer the measurement has, which is
    not the same as a good answer: a project whose copy matches no upstream
    commit closely gets the peak of a poor field, and is told so here rather
    than left to read it out of the counts.
    """
    if not survey.readings:
        return (
            f"No commit reachable from {short_sha(survey.tip)} that changed "
            f"the copied half could be measured, so this base stands on the "
            f"reading above alone."
        )
    poorly = (
        " The field is poor, though: this checkout matches no upstream "
        "commit closely, so expect the first merge to report conflicts."
        if named.share() < margin
        else ""
    )
    return (
        f"None of the {len(survey.readings)} candidate(s) measured back to "
        f"{short_sha(survey.readings[-1].commit)} reads better, so the "
        f"measurement points here.{poorly}"
    )


def restated(named: ScaffoldFit) -> str:
    """What a caller types to root at a base the measurement argues against.

    The reading itself, so the escape cannot be supplied by habit: the count
    is a fact about this base against this checkout, and somebody who has it
    to hand has read what it says.
    """
    return f"--accept-fit {named.identical}"


def pin_refusal(named: ScaffoldFit, better: ScaffoldFit | None) -> str:
    """Why rooting at the commit the pin already resolves to carries nothing."""

    def pointer() -> str:
        """Where the measurement says this project's copy actually sits."""
        if better:
            return (
                f"{short_sha(better.commit)} ({better.subject}) reads "
                f"{better.spelled()} and is the likelier base."
            )
        if named.share() == 1:
            return (
                "Nothing contradicts it, though: this checkout holds that "
                "scaffold byte for byte, so the copy really is at the pin "
                "and an update carrying nothing would be the truth."
            )
        return (
            "No commit measured reads better, though, so `dev scaffold fit` "
            "is worth reading before deciding."
        )

    return (
        f"--base {short_sha(named.commit)} is the commit the library pin "
        f"already resolves to, so the merge base and the merge target would "
        f"be one commit: the first `dev update` would merge nothing, report "
        f"`0 fast-forwarded, 0 merged clean, 0 conflicted`, and say the "
        f"copied half is already merged at that commit. That reads like "
        f"success, and wherever the copy is behind the pin it is the "
        f"opposite: every upstream change to the copied half nobody "
        f"hand-ported stays untaken, permanently, with nothing to say so. "
        f"Root the branch at the commit this "
        f"project's copy was last carried up to whole, and let `dev update` "
        f"carry the range from there to the pin. {pointer()} Root here anyway "
        f"by restating that reading: `{restated(named)}`."
    )


def fit_refusal(named: ScaffoldFit, better: ScaffoldFit) -> str:
    """Why a base the measurement reads poorly at is probably the wrong one."""
    return (
        f"--base {short_sha(named.commit)} fits this checkout poorly: "
        f"{named.spelled()}, where {short_sha(better.commit)} "
        f"({better.subject}) reads {better.spelled()}. A copy stamped from "
        f"one commit and edited since reads highest at that commit, so the "
        f"branch belongs at the peak rather than here — rooting behind it "
        f"re-offers changes this project already applied by hand, and the "
        f"first merge reports them as conflicts. `dev scaffold fit` prints "
        f"every candidate measured. Root here anyway by restating this "
        f"reading: `{restated(named)}`."
    )


def checked_base(
    root: Path,
    repository: Path,
    source: scaffold.ScaffoldSource,
    package: str,
    base: str,
    accept_fit: int | None,
    report: Callable[[str], None],
    distribution: str = library.DISTRIBUTION,
    depth: int = CANDIDATE_DEPTH,
    margin: Fraction = FIT_MARGIN,
) -> str:
    """The commit to root the branch at, refused where the measurement says no.

    Measured before anything is written, and answerable: a project whose copy
    matches no upstream commit closely still has to adopt something, so every
    refusal here names the reading that produced it and the reading is what
    overrules it. What cannot happen is a base accepted in silence that the
    checkout itself says is wrong.
    """
    commit = resolved(repository, base)
    named = measured(root, repository, source, package, commit)
    report(named.reported())
    if accept_fit is not None:
        if accept_fit != named.identical:
            raise typer.BadParameter(
                f"--accept-fit {accept_fit} is not what this base reads: "
                f"{named.spelled()}. The escape restates the measurement, so "
                f"`{restated(named)}` is what roots the branch here."
            )
        report(f"Rooting there on that reading, restated as {restated(named)}.")
        return commit
    pinned = scaffold.pinned_commit(root, distribution)
    # A base whose whole compiled half is already here byte for byte is one
    # no candidate could read a margin above, so nothing is searched for it.
    # The pin is searched whatever it reads, its refusal being the one that
    # has to name where the copy really sits.
    survey = (
        surveyed(
            root,
            repository,
            source,
            package,
            searched_tip(root, repository, distribution),
            depth,
        )
        if named.share() + margin <= 1 or commit == pinned
        else None
    )
    better = contradicting(named, survey, margin) if survey is not None else None
    if commit == pinned:
        raise typer.BadParameter(pin_refusal(named, better))
    if better:
        raise typer.BadParameter(fit_refusal(named, better))
    if survey is not None:
        report(settled_report(named, survey, margin))
    return commit
