"""What the agent asked for when it wrote a marker on a call.

Escalation is a *request*, never self-authority. Two different things were
sharing one spelling: "put this to a human, because the rule that refused it
does not know what I know" and "run this outside the containment boundary,
because inside cannot answer it". They compose — a refused command that also
has to reach the host is both — and they promote a verdict along different
axes, so the marker names which it means.

The accepted spellings are ``# lup: escalate[decision]:``,
``# lup: escalate[sandbox]:``, and ``# lup: escalate[decision,sandbox]:``,
each followed by a nonempty reason. The bare ``# lup: escalate:`` that
predates the distinction stays a working alias for decision escalation and
says so, because the alternative is a session whose every marker stops
working at once for having been written before the vocabulary grew.

A reason is mandatory in every spelling. A marker with none would be the
agent authorising itself: the whole content of the request is what it says
to whoever answers it, and a request that says nothing asks them to approve
a rule id.
"""

# lup: ignore[import-re] — this module's subject is a comment grammar
# this repository defines; there is no parser for our own marker syntax
import re
from typing import Literal

from .diagnostic import Step, step

type EscalationKind = Literal["decision", "sandbox"]
"""Which axis a marker asks to move.

``decision`` asks for a reviewer over a verdict a rule reached without one:
an overrideable deny becomes a question, a deferral becomes a question, an
existing question carries the reason with it. ``sandbox`` asks for the
operation to run on the launcher's host, which is a placement rather than a
permission, and which is therefore always reviewed however the effect reads.
"""

# lup: ignore[library-default] — the vocabulary of the marker grammar
# itself, fixed by what settlement can act on rather than by any
# adopter's toolchain: a third kind would be a settlement row, not a
# word an adopter passes
ESCALATION_KINDS: tuple[EscalationKind, ...] = ("decision", "sandbox")
"""Every kind a marker may name, in the order the canonical spelling lists them."""

# lup: ignore[re-call] — the marker's own grammar, which nothing else parses
ESCALATE_RE = re.compile(
    r"^\s*#[ \t]*lup[ \t]*:[ \t]*escalate\b"
    r"(?:[ \t]*\[(?P<kinds>[^\]\n]*)\])?"
    r"[ \t]*:?[ \t]*(?P<why>[^\n]*)(?:\n|$)",
    re.IGNORECASE,
)
"""The one marker grammar, whether or not it names its kinds.

Optional rather than two patterns because the two spellings differ by a
bracketed clause and nothing else, and a second pattern is a second place for
the leading-comment rules — whitespace, the colon, the case-blindness — to
drift.
"""

# lup: ignore[constant-declaration] — refusal wording, declared with the
# verdict that returns it; a caller passing different words would be stating
# a different refusal
MISSING_REASON = "escalation requires a stated reason"
"""What a marker naming no reason is refused with.

Refused before classification, so it never reaches settlement: an escalation
that states nothing is the agent approving its own call, which is the one
thing the marker cannot be.
"""

# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
UNKNOWN_KIND = (
    "escalation kind {kind!r} is not one of "
    "'decision' (put this to a reviewer) or 'sandbox' (run it on the host)"
)
"""What a marker naming a kind this vocabulary does not carry is refused with.

Named rather than ignored, because a typo silently read as the bare alias
would grant decision escalation to a request that asked for the host, and the
agent would spend a turn discovering the call still ran inside.
"""

# lup: ignore[constant-declaration] — migration wording, declared with the alias it annotates
LEGACY_NOTICE = (
    step(
        "write the kind out: `# lup: escalate:` alone is read as"
        " `# lup: escalate[decision]:`, and `# lup: escalate[sandbox]:` asks for"
        " the host"
    ),
)
"""What the bare spelling adds to the verdict it produces.

The alias keeps working and says it is an alias. Dropped silently it would
read as the whole vocabulary, and the sandbox half — the one an agent stuck
inside the boundary actually needs — would stay undiscovered.
"""


class EscalationRequest:
    """One parsed marker: which axes it asks to move, and the reason given.

    ``raw`` is exactly the text the agent wrote and ``normalized`` is the
    canonical spelling of what it meant, both retained because the audit has
    to be able to say a legacy marker was read as decision escalation without
    reconstructing that from the effect it produced.
    """

    kinds: tuple[EscalationKind, ...]
    reason: str
    raw: str
    normalized: str
    legacy: bool

    def __init__(
        self,
        kinds: tuple[EscalationKind, ...],
        reason: str,
        raw: str = "",
        legacy: bool = False,
    ) -> None:
        self.kinds = kinds
        self.reason = reason
        self.raw = raw
        self.legacy = legacy
        named = ",".join(kind for kind in ESCALATION_KINDS if kind in kinds)
        self.normalized = f"# lup: escalate[{named}]: {reason}" if kinds else ""

    def asks(self, kind: EscalationKind) -> bool:
        """Whether this request names one axis."""
        return kind in self.kinds

    def notice(self) -> tuple[Step, ...]:
        """What this request tells the agent about its own spelling, if anything."""
        return LEGACY_NOTICE if self.legacy else ()


class MarkerReading:
    """What reading a call's leading line found, including finding nothing.

    Three outcomes rather than an optional request: absent, a valid request,
    and a marker that is structurally invalid. The third is not the first —
    a call whose marker names an unknown kind has said something, and reading
    it as unmarked would run it under a verdict its author did not ask for.
    """

    request: EscalationRequest | None
    refusal: str
    remainder: str

    def __init__(
        self, request: EscalationRequest | None, refusal: str, remainder: str
    ) -> None:
        self.request = request
        self.refusal = refusal
        self.remainder = remainder


def read_escalation(text: str) -> MarkerReading:
    """Read a leading escalation marker off one call's text.

    ``remainder`` is the call with the marker line removed, which is what
    classification judges: the marker is a request about the call and not
    part of it, and leaving it in would make an unclassifiable comment out of
    every escalated command.

    An absent marker leaves the text whole and asks for nothing. A malformed
    one — no reason, or a kind this vocabulary does not carry — is a refusal
    stated here rather than a request passed on, because settlement can only
    promote a verdict and has nowhere to put "the request itself was wrong".
    """
    marker = ESCALATE_RE.match(text)
    if marker is None:
        return MarkerReading(None, "", text)
    remainder = text[marker.end() :]
    reason = marker.group("why").strip()
    if not reason:
        return MarkerReading(None, MISSING_REASON, remainder)
    named = marker.group("kinds")
    if named is None:
        return MarkerReading(
            EscalationRequest(("decision",), reason, marker.group(0), legacy=True),
            "",
            remainder,
        )
    # lup: ignore[string-split] — the marker's own comma-separated kind list,
    # a grammar this repository defines and nothing else parses
    named_kinds = [word.strip().lower() for word in named.split(",")]
    unknown = next((word for word in named_kinds if word not in ESCALATION_KINDS), None)
    if unknown is not None or not any(named_kinds):
        return MarkerReading(None, UNKNOWN_KIND.format(kind=unknown or ""), remainder)
    # Read in the canonical order rather than the order they were written, so
    # two markers naming the same pair produce the same normalized spelling and
    # an audit comparing them is comparing requests rather than typing.
    kinds: tuple[EscalationKind, ...] = tuple(
        kind for kind in ESCALATION_KINDS if kind in named_kinds
    )
    return MarkerReading(
        EscalationRequest(kinds, reason, marker.group(0)), "", remainder
    )
