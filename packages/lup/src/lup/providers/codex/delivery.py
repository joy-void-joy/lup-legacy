"""What the Codex adapter is built to deliver, and how each belief is known.

The counterpart to the Claude record, kept at this boundary for the same
reason: the mechanisms are named in this runtime's own words, and those words
belong here.

Two of these rows say a mechanism is absent, which is the point of recording
them. A guarantee omitted reads as delivered; a guarantee stated as absent
with its fallback is a design somebody made — and for the placement row it is
the design a launch preflight enforces, by refusing to start a profile that
requires containment this runtime cannot hold.
"""

from lup.policy.delivery import DeliveryFact


# lup: ignore[constant-declaration] — the same observation, for the other
# runtime and on the same grounds
CODEX_DELIVERY: list[DeliveryFact] = [
    DeliveryFact(
        guarantee="ask_survives_auto_mode",
        provider="codex",
        mechanism=(
            "the generated PreToolUse hook parks every ask in the review queue"
            " and refuses the call while it waits; only an answer the operator"
            " recorded in the host's state releases it, once"
        ),
        standing="measured",
        fallback=(
            "generated-dispatcher fixtures verify the receipt gate; native"
            " permission requests and execution never supply approval authority"
        ),
    ),
    DeliveryFact(
        guarantee="exact_call_resumes",
        provider="codex",
        mechanism=(
            "an operator answer releases the call once: `review wait` writes the"
            " after-documents or runs the command it recorded, or one retry with"
            " the same session, directory, payload, policy reason and edited"
            " file preimages spends it"
        ),
        standing="measured",
        fallback=(
            "correlation drift is a refusal rather than a reuse, which the"
            " generated hook's own suite exercises"
        ),
    ),
    DeliveryFact(
        guarantee="defer_is_transparent",
        provider="codex",
        mechanism="the hook exits zero with no output, leaving the native flow",
        standing="measured",
        fallback="none needed: emitting nothing is the absence of a mechanism",
    ),
    DeliveryFact(
        guarantee="inside_placement_enforced",
        provider="codex",
        mechanism="",
        standing="absent",
        fallback=(
            "this runtime's verdicts place no call, so a placement renders as"
            " the plain effect and containment is the session's own posture —"
            " which is why a profile requiring inside_placement here fails its"
            " launch preflight rather than running unconfined"
        ),
    ),
    DeliveryFact(
        guarantee="outside_placement_carried",
        provider="codex",
        mechanism=(
            "the model requests placement on its own call and a compiled prefix"
            " rule approves exactly the boundary's declared exclusions"
        ),
        standing="measured",
        fallback=(
            "a request the declaration does not cover is refused with the"
            " placement policy reached and what to remove"
        ),
    ),
    DeliveryFact(
        guarantee="rejection_receipt",
        provider="codex",
        mechanism="an explicit rejection is recorded on the host by the review queue",
        standing="measured",
        fallback="absence of native execution supplies no answer",
    ),
    DeliveryFact(
        guarantee="hook_failure_is_closed",
        provider="codex",
        mechanism=(
            "every exception shape reaches one refusal that names what went"
            " wrong, because naming the exceptions is what let an unreadable"
            " file escape as a traceback exit"
        ),
        standing="measured",
        fallback="the reason carries whatever went wrong; an interrupt passes through",
    ),
]
"""What this repository believes about the Codex adapter, and on what basis."""
