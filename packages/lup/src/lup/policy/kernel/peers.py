"""What a native call reaching another session is judged by.

A runtime that can address another session offers a second way to reach one,
and the two acts are not the same. A message on the coordination stream is a
durable record every worktree folds and every later session can read; a native
send is a call whose text exists only inside whichever process received it. So
where both would reach the same member, this stops the one leaving no record
and names the one that does — and where the native call reaches somebody the
roster has never heard of, it says nothing at all.

That asymmetry is the whole design. A subagent this session started is on no
repository roster, so continuing one passes through untouched; a listing
speaking for a wider population than this repository is left to speak for it,
with this repository's own roster attached rather than substituted. Refusing
the listing was considered and is wrong for a measurable reason: most of what
it returns is sessions in other repositories, which a repository-scoped roster
is structurally incapable of holding, so the redirect would answer a question
the reader did not ask.
"""

from .decision import KernelDecision, file_review_row
from .effects import STRENGTH
from .rows import PeerPolicyRow
from .tools import TOOL_ESCALATE_HINT, escalated_reason


def undeclared_roster(
    reason: str = "no roster is declared for this repository",
) -> KernelDecision:
    """The answer to a peer call where nothing coordinates: leave it alone.

    ``defer`` rather than a verdict, because a project whose sessions do not
    coordinate has said nothing about this call and the runtime's own
    permissions are the whole of what should decide it. An ``ask`` here would
    price a listing at an approval for every project that declined the module.
    """
    return KernelDecision("defer", reason)


def addressed_peer(values: list[str], addresses: list[str]) -> str:
    """Which live member this call names, or nothing where it names none.

    Matched against every string the call carries rather than against a named
    field, for the reason a refusal is: which field a runtime spells a
    recipient in is that runtime's business, and a check knowing one spelling
    would fail open on the rest. Whole values only — a message *mentioning* a
    peer is not a message addressed to one.
    """
    return next((value for value in values if value in addresses), "")


def decide_peer_send(
    values: list[str], addresses: list[str], row: PeerPolicyRow | None
) -> KernelDecision:
    """Judge one native send against who this repository's roster holds.

    A hit denies and says where the same peer is reached durably. A miss
    defers: the target is a subagent this session started, a teammate, or a
    session in another repository, and every one of those is a use the native
    call is the only way to make.
    """
    if row is None:
        return undeclared_roster()
    named = addressed_peer(values, addresses)
    if not named:
        return KernelDecision("defer", "no member of this repository's roster")
    why = escalated_reason(values)
    if why:
        return KernelDecision(
            "ask",
            f"escalated ({why}): {row['send_reason']}",
            recovery=row["send_recovery"],
        )
    return KernelDecision(
        "deny",
        f"{named}: {row['send_reason']}",
        recovery=f"{row['send_recovery']} {TOOL_ESCALATE_HINT}",
    )


def decide_peer_listing(row: PeerPolicyRow | None) -> KernelDecision:
    """Judge one native listing, which is to say leave it alone.

    Always a deferral. The call answers a question this repository's roster
    cannot, so there is nothing here to permit and nothing to refuse — what
    the roster has to offer rides alongside as context rather than as a
    verdict, and a verdict is the one thing that could take the answer away.
    """
    if row is None:
        return undeclared_roster()
    return KernelDecision("defer", "a wider population than this repository's roster")


def peer_listing_context(listing: list[str], row: PeerPolicyRow | None) -> str:
    """This repository's roster, framed so a reader can tell it from the wider one.

    Empty where nothing has joined, because an attachment saying a roster is
    empty teaches a reader nothing they could act on and is paid for on every
    listing call. Empty likewise where no roster is declared at all.
    """
    if row is None or not listing:
        return ""
    return "\n".join([row["listing_note"], *[f"  {line}" for line in listing]])


def decide_foreign_claim(
    path: str, holders: list[str], row: PeerPolicyRow | None
) -> KernelDecision | None:
    """Judge a write to a path a live session other than this one is holding.

    An approval question rather than a refusal, because the answer is
    genuinely the operator's: two sessions editing one file is sometimes
    exactly right, and a policy that decided otherwise would refuse ordinary
    parallel work. What it must not be is silent — the failure this exists for
    is finding out at merge time.

    Every holder is named rather than the first. More than one is a change
    nothing could attribute, and a reader deciding whom to ask has to see that
    it is a question rather than an answer.

    ``None`` where nothing is held, which is not the same as an allow: this
    family has no positive authority to grant, and returning one would let a
    quiet path weaken a verdict the edit gates reached on their own.
    """
    if row is None or not holders:
        return None
    return KernelDecision(
        "ask",
        f"{path} is held by {', '.join(holders)} — {row['claim_reason']}",
        recovery=row["claim_recovery"],
    )


def settled_with_claim(
    verdict: KernelDecision, claim: KernelDecision | None
) -> KernelDecision:
    """One edit's own verdict and the claim over its path, settled together.

    The stronger effect decides and both reasons survive, because the two
    answer different questions — whether this content may be written, and
    whether somebody else is already in this file — and an approver shown only
    one of them is deciding on half of it.

    ``findings`` is the channel composition already travels, so the reasons
    are joined once at the seam every renderer funnels through rather than
    concatenated here. A tie keeps the edit's own verdict as the carrier,
    which is right: it is the verdict about the act, and the claim is context
    for whoever answers it.

    The per-file evidence the edit's owner bound is restated as the settled
    verdict, whichever part carries it: a claim that decides changes what the
    file's row says was decided, never which images were judged.
    """
    if claim is None:
        return verdict
    parts = (verdict, claim)
    settled = max(parts, key=lambda part: STRENGTH.index(part.effect)).revised(
        findings=parts
    )
    return settled.revised(
        file_reviews=tuple(
            file_review_row(
                settled,
                row["path"],
                row["before_sha256"],
                row["after_sha256"],
                row["after"],
            )
            for row in verdict.file_reviews
        )
    )
