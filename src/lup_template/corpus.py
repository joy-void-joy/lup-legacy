"""This repository's corpus: claims, what backs them, and what retired them.

A corpus is a body of claims somebody is prepared to be held to, together with
the evidence behind each and the corrections that have since retired some.
Every type here answers ``standing`` over its neighbourhood and stores none of
it, which is the invariant the whole design rests on: a claim cannot outrun its
artifact and cannot keep a label its evidence stopped supporting, because
nothing ever wrote the label down.

**These types are the template's, not the library's, by decision.** Nothing in
the library consumes them; the ledger's charter is to declare no epistemics,
and the order in which a claim's standing is read below *is* an epistemics a
project may disagree with; and every adopter edits them — the grades, the
priorities, what a question is. A type everybody subclasses to narrow is a
scaffold file with a compatibility burden attached, so it is a scaffold file.
What the library keeps is mechanism: standing read on request, a reader that
follows a chain of premises, `prepared`, slugs, `moved_since`, the cite check.

**Evidence rots against files.** A piece of evidence carries the digests of
every file it was checked against, so it stops standing the moment one of them
changes — read from the working tree when somebody asks, which is why the
neighbourhood carries a root.

**A claim refuses to hide contradiction**, reports a premise that fell as its
own regression, and is graded in this repository's six words. They are the
words senders in a mathematics corpus write on their own claims unprompted, an
explicit negative among them:
*"[M] No loop was found at small size. I have not run this search; the
statement is that no such search exists in this project's record."* Six is not
a recommendation; an adopting project replaces `GRADES` and nothing else.

**Corrections supersede partially.** What survives and what changes are facts
about the pair, so they live on the ``supersedes`` edge; a boolean would have
thrown the survivors away.
"""

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator

from lup.ledger.files import digest_of
from lup.ledger.models import LedgerEdge, LedgerNode, Standing, Surroundings


class Grade(BaseModel, frozen=True):
    """One word a claim may carry, and what a reader should make of it."""

    name: str
    meaning: str


# lup: ignore[constant-declaration] — a vocabulary this repository defines
# for itself, replaced whole by an adopting project rather than tuned
GRADES = [
    Grade(name="literature", meaning="stated in a cited source, not checked here"),
    Grade(name="measured", meaning="observed by running something in this record"),
    Grade(name="argued", meaning="reasoned in prose, with no check behind it"),
    Grade(name="conjecture", meaning="believed, and offered as a target"),
    Grade(name="lean", meaning="kernel-checked, with the certificate attached"),
    Grade(name="mine", meaning="the sender's own, unchecked by anybody else"),
]


class Scoped(BaseModel, frozen=True):
    """One file a piece of evidence was checked against, as it was then."""

    path: str = Field(min_length=1)
    """Relative to the working tree, so the same record reads in every checkout."""

    digest: str


class Validation(BaseModel, frozen=True):
    """What one piece of evidence was checked as, over which files, by what.

    The schema says which checker's word this is, so a certificate from a
    kernel and a hash from a script are not the same kind of thing. The subject
    digest pins what was checked. The scope pins what it was checked *against*
    — and that is the part that rots, because a proof over three files is a
    proof over those three files as they were.
    """

    schema_id: str = Field(min_length=1)
    subject_digest: str = Field(min_length=1)
    scope: list[Scoped] = []

    def drifted(self, root: Path) -> list[str]:
        """Every scoped path whose content is no longer what was checked."""
        return [
            scoped.path
            for scoped in self.scope
            if digest_of(root / scoped.path) != scoped.digest
        ]


def superseded_by(around: Surroundings) -> Standing | None:
    """The reading a correction imposes on this node, or nothing where none points here.

    One reading for every kind a correction can retire — a claim, a piece of
    evidence, a source — so the corpus reads ``superseded`` the same way
    wherever a ``supersedes`` edge lands, and a retired node is unsound
    whatever else it could have said about itself: evidence a correction
    retired must not go on counting as support.
    """
    superseding = [
        edge.source for edge in around.incoming if edge.kind == "corpus:supersedes"
    ]
    if not superseding:
        return None
    return Standing(
        label="superseded",
        reason=f"corrected by {', '.join(superseding)}",
        sound=False,
    )


class Evidence(LedgerNode, frozen=True):
    """Bytes that bear on a claim, with the validation that says what they are.

    The bytes ride as the node's attachments under their own digest, so two
    pieces of evidence with the same bytes are one blob. The two kinds below
    share the shape and differ in one word, because a reader ranks a checker's
    output and a person's differently and should be able to.
    """

    validation: Validation

    def prepared(self, root: Path) -> Self:
        """The scope pinned to the tree as it is now, for any path recorded bare.

        A caller names the files evidence was checked against; the digests are
        this type's to take, at the one moment they are true. A scoped path
        that already carries a digest is left alone, so a record replayed from
        another machine keeps what it was checked against there.
        """
        pinned = [
            scoped
            if scoped.digest
            else Scoped(path=scoped.path, digest=digest_of(root / scoped.path))
            for scoped in self.validation.scope
        ]
        return self.model_copy(
            update={"validation": self.validation.model_copy(update={"scope": pinned})}
        )

    def standing(self, around: Surroundings) -> Standing:
        """Superseded, then fresh while every scoped file is as it was, stale the moment one is not.

        Superseded first, in the order a claim reads, because a correction
        outranks whatever the digests say. Unchecked where there is no tree to
        read, which is true on a snapshot read elsewhere, and is reported as
        sound: a reader that cannot check has no grounds to say the evidence
        went away.
        """
        if (retired := superseded_by(around)) is not None:
            return retired
        if around.root is None:
            return Standing(label="unchecked", reason="no working tree to read")
        gone = self.validation.drifted(around.root)
        if gone:
            return Standing(
                label="stale",
                reason=f"{', '.join(gone)} changed since it was checked",
                sound=False,
            )
        return Standing(label="fresh")


class Artifact(Evidence, frozen=True):
    """Evidence a person or a script produced: a log, a table, a measurement."""

    kind: Literal["corpus:artifact"] = "corpus:artifact"


class Certificate(Evidence, frozen=True):
    """Evidence a checker produced, whose schema names the checker."""

    kind: Literal["corpus:certificate"] = "corpus:certificate"


class Source(LedgerNode, frozen=True):
    """External bytes a claim rests on, kept under their own digest.

    What a claim was read *from* — a paper, a dataset, a page — recorded so it
    cannot move or change: a claim citing it cites those bytes and not whatever
    the origin serves next year.
    """

    kind: Literal["corpus:source"] = "corpus:source"
    origin: str = Field(min_length=1)

    def standing(self, around: Surroundings) -> Standing:
        """Superseded where a correction points here; recorded otherwise.

        A source is its own bytes under their digest and rots against
        nothing, so the one thing that retires it is somebody saying it was
        the wrong thing to have read from — read in the order a claim reads,
        so a claim it supported stops counting it.
        """
        retired = superseded_by(around)
        return retired if retired is not None else Standing(label="recorded")


class Correction(LedgerNode, frozen=True):
    """Something recorded earlier was wrong, and here is what to know now.

    Append-only like everything else: the wrong node stays, readable, with
    this pointing at it through ``supersedes``, which carries what survives
    and what changes.
    """

    kind: Literal["corpus:correction"] = "corpus:correction"
    where: str = ""
    """Where the mistake was noticed, for a reader retracing it.

    Optional, because corpus-first the wrong thing is the node the edge points
    at, and that is exact where prose is not.
    """

    wrong: str = Field(min_length=1)
    lesson: str = ""


class Question(LedgerNode, frozen=True):
    """Something still to find out, open until a claim that answers it stands.

    What "we still want to investigate" is as a node: the todolist of
    knowledge. Open by default, answered the moment a sound claim points at it
    through ``answers``, and closed only by somebody saying so — a question
    nobody answered is still a question, and silence must not read as done.
    """

    kind: Literal["corpus:question"] = "corpus:question"

    closed: str = ""
    """Why nobody should work on this any more, empty while it is live."""

    def finished(self) -> bool:
        return bool(self.closed)

    def completed(self) -> LedgerNode:
        """This question closed, with the one word `done` leaves room for."""
        return self.model_copy(update={"closed": self.closed or "closed"})

    def standing(self, around: Surroundings) -> Standing:
        """Closed, answered by a claim that stands, or open — in that order.

        Answered is read from the answering claims' own standing, as deep as
        the reader can see, so a question answered by a claim whose premise
        was later refuted goes back to open without anybody amending anything.
        """
        if self.closed:
            return Standing(label="closed", reason=self.closed)
        candidates = [
            edge.source for edge in around.incoming if edge.kind == "corpus:answers"
        ]
        standing_answers = [
            source
            for source in candidates
            if (reading := around.standing_at(source)) is not None and reading.sound
        ]
        if standing_answers:
            return Standing(
                label="answered", reason=f"by {', '.join(standing_answers)}"
            )
        if candidates:
            return Standing(
                label="open",
                reason=f"{len(candidates)} candidate answer(s), none standing",
            )
        return Standing(label="open", reason="no answer recorded")


class Claim(LedgerNode, frozen=True):
    """One statement somebody is prepared to be held to, graded in our words.

    What this type answers is not how good the claim is — that is the grade,
    written by whoever recorded it — but whether what backs it is still there,
    and whether what it rests on still stands.
    """

    kind: Literal["corpus:claim"] = "corpus:claim"
    grade: str = ""

    @field_validator("grade")
    @classmethod
    def graded_in_our_words(cls, spelling: str) -> str:
        """Refuse a word outside `GRADES`; an empty grade is honest."""
        if spelling and spelling not in {grade.name for grade in GRADES}:
            names = ", ".join(grade.name for grade in GRADES)
            raise ValueError(f"{spelling!r} is not one of {names}")
        return spelling

    def standing(self, around: Surroundings) -> Standing:
        """Where this claim stands, read from what points at it right now.

        Superseded first, because a correction outranks every piece of
        evidence. Then a premise that fell, as deep as the reader can see —
        which is how one refutation reaches every claim built on it without
        anybody amending any of them. Then the evidence, each piece asked
        whether it itself still stands — so only evidence that is sound is
        counted, and a piece a correction retired or a file change withered
        is not — and the answer refuses to hide a contradiction.
        """
        if (retired := superseded_by(around)) is not None:
            return retired

        premises = [
            edge.target for edge in around.outgoing if edge.kind == "corpus:rests_on"
        ]
        regressed = [
            (premise, reading)
            for premise in premises
            if (reading := around.standing_at(premise)) is not None
            and not reading.sound
        ]
        if regressed:
            premise, reading = regressed[0]
            return Standing(
                label="premise regressed",
                reason=f"rests on {premise}, which is {reading.label}: {reading.reason}",
                sound=False,
            )

        def evidence(kind: str) -> list[LedgerNode]:
            return [
                node
                for edge in around.incoming
                if edge.kind == kind and (node := around.at(edge.source)) is not None
            ]

        def still_standing(nodes: list[LedgerNode]) -> list[LedgerNode]:
            return [
                node
                for node in nodes
                if (reading := around.standing_at(node.id)) is not None
                and reading.sound
            ]

        support = evidence("corpus:supports")
        against = evidence("corpus:refutes")
        live_support = still_standing(support)
        live_against = still_standing(against)
        verified = any(edge.kind == "corpus:verifies" for edge in around.incoming)

        if live_against and live_support:
            return Standing(
                label="contradicted",
                reason=(
                    f"{len(live_against)} counterexample(s) and "
                    f"{len(live_support)} supporting artifact(s) both stand"
                ),
                sound=False,
            )
        if live_against:
            return Standing(
                label="refuted",
                reason=f"{len(live_against)} counterexample(s) stand",
                sound=False,
            )
        if support and not live_support:
            withered = [
                reading.reason
                for node in support
                if (reading := around.standing_at(node.id)) is not None
            ]
            return Standing(
                label="stale",
                reason="; ".join(reason for reason in withered if reason),
                sound=False,
            )
        if live_support:
            return Standing(
                label="verified" if verified else "supported",
                reason=f"{len(live_support)} artifact(s) stand",
            )
        return Standing(label="unsupported", reason="no evidence attached")


class Supports(LedgerEdge, frozen=True):
    """This evidence is for that claim."""

    kind: Literal["corpus:supports"] = "corpus:supports"


class Refutes(LedgerEdge, frozen=True):
    """This evidence is a counterexample to that claim."""

    kind: Literal["corpus:refutes"] = "corpus:refutes"


class Verifies(LedgerEdge, frozen=True):
    """Somebody other than the author checked that claim and stands by it.

    The one rule kept about who may say what, and it is a property of the
    relation: only the edge sees both ends, so only the edge can refuse an
    author verifying their own work.
    """

    kind: Literal["corpus:verifies"] = "corpus:verifies"

    def refusal(self, source: LedgerNode, target: LedgerNode) -> str:
        del source
        if target.author.id == self.author.id:
            return "a verification of your own work is not one"
        return ""


class Supersedes(LedgerEdge, frozen=True):
    """This correction retires that node — partially, and it says which parts.

    What changes may not be empty: a correction that changes nothing is a
    note. What survives may be, and often is not, which is the whole reason
    this is not a boolean.
    """

    kind: Literal["corpus:supersedes"] = "corpus:supersedes"
    changes: list[str] = Field(min_length=1)
    survives: list[str] = []


class RestsOn(LedgerEdge, frozen=True):
    """This claim depends on that one: if the premise falls, so does this.

    Drawn from the dependant to the premise, because that is the direction
    somebody records it in, and read the other way by the reader following a
    refutation outward to everything built on it.
    """

    kind: Literal["corpus:rests_on"] = "corpus:rests_on"

    def refusal(self, source: LedgerNode, target: LedgerNode) -> str:
        if source.id == target.id:
            return "a claim cannot rest on itself"
        return ""


class Answers(LedgerEdge, frozen=True):
    """This claim answers that question, for as long as the claim stands."""

    kind: Literal["corpus:answers"] = "corpus:answers"


class About(LedgerEdge, frozen=True):
    """This node is about that file: a claim about `src/parser.py`, a question about a module.

    Descriptive rather than load-bearing: the file's own standing says whether
    it moved since it was recorded, and a reader following the edge sees that
    beside the claim. A claim that must fall with a file rests on evidence
    scoped to it instead.
    """

    kind: Literal["corpus:about"] = "corpus:about"
