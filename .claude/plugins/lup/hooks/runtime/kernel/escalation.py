"""What the agent asked for when it wrote a marker on a call.

Escalation is a *request*, never self-authority. It asks one of two things:
"put this to a human, because the rule that refused it does not know what I
know" and "run this outside the containment boundary, because inside cannot
answer it". They compose — a refused command that also has to reach the host
is both — and they promote a verdict along different axes, so the marker
names which it means.

The accepted spellings are ``# lup: escalate[decision]:``,
``# lup: escalate[sandbox]:``, and ``# lup: escalate[decision,sandbox]:``,
each followed by a nonempty reason. A marker naming no kind is refused with
those spellings rather than read as one of them: guessed as a decision, it
would leave the agent stuck inside the boundary never learning the sandbox
half exists, and guessed as either it grants an axis nobody named.

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

The bracketed clause is optional so a marker naming no kind is still read as
a marker, and refused by name, rather than run as a command whose first line
happens to be a comment. One pattern rather than two keeps the leading-comment
rules — whitespace, the colon, the case-blindness — in one place.
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

# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
MISSING_KIND = "escalation names no kind"
"""What a marker naming no kind is refused with.

Refused rather than read as a decision, because the kind is the request: a
marker that does not name one asks for nothing settlement can act on, and
the sandbox half — the one an agent stuck inside the boundary needs — is
learned from this refusal or not at all.
"""


# lup: ignore[library-default] — refusal wording, declared with its verdict
KIND_RECOVERY = (
    step("put it to a reviewer with a first line `# lup: escalate[decision]: <why>`"),
    step("or run it on the host with a first line `# lup: escalate[sandbox]: <why>`"),
)
"""The ways past a marker that named no kind: the two kinds it could name."""


class EscalationRequest:
    """One parsed marker: which axes it asks to move, and the reason given.

    ``raw`` is exactly the text the agent wrote and ``normalized`` is the
    canonical spelling of what it meant, both retained because the audit has
    to be able to say which axes a marker asked for without reconstructing
    that from the effect it produced.
    """

    kinds: tuple[EscalationKind, ...]
    reason: str
    raw: str
    normalized: str

    def __init__(
        self,
        kinds: tuple[EscalationKind, ...],
        reason: str,
        raw: str = "",
    ) -> None:
        self.kinds = kinds
        self.reason = reason
        self.raw = raw
        named = ",".join(kind for kind in ESCALATION_KINDS if kind in kinds)
        self.normalized = f"# lup: escalate[{named}]: {reason}" if kinds else ""

    def asks(self, kind: EscalationKind) -> bool:
        """Whether this request names one axis."""
        return kind in self.kinds


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
    recovery: tuple[Step, ...]

    def __init__(
        self,
        request: EscalationRequest | None,
        refusal: str,
        remainder: str,
        recovery: tuple[Step, ...] = (),
    ) -> None:
        self.request = request
        self.refusal = refusal
        self.remainder = remainder
        self.recovery = recovery


def read_escalation(text: str) -> MarkerReading:
    """Read a leading escalation marker off one call's text.

    ``remainder`` is the call with the marker line removed, which is what
    classification judges: the marker is a request about the call and not
    part of it, and leaving it in would make an unclassifiable comment out of
    every escalated command.

    An absent marker leaves the text whole and asks for nothing. A malformed
    one — no reason, no kind, or a kind this vocabulary does not carry — is a
    refusal stated here rather than a request passed on, because settlement
    can only promote a verdict and has nowhere to put "the request itself was
    wrong".
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
        return MarkerReading(None, MISSING_KIND, remainder, KIND_RECOVERY)
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
