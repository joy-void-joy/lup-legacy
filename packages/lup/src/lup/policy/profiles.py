"""Turning one project's harness declaration into a boundary a launch measures.

:mod:`lup.policy.boundary` says what a profile promises and what a launch
observed; this is what joins the two to the declaration a project already
wrote. Without it ``ExecutionBoundary`` was a type nothing constructed, which
made ``contained=True`` a claim no launch had ever checked -- precisely the
failure the type exists to name.

Two halves, and keeping them apart is the point. :func:`compile_boundary` reads
a declaration and answers what this profile *promises*; :func:`measured` reads
what the probes came back with and answers what the launch *delivered*. A
single function doing both could only ever return the promise, because a
declaration is always internally consistent -- it is the measurement that can
disagree with it, and it can only disagree if it arrives separately.

The ``harness`` to ``policy`` edge this stands on is one the library sanctions
rather than tolerates: the declaration is what a policy is *for*. It lives here
rather than under ``policy/kernel/`` because the kernel may import nothing but
its own decision vocabulary, and a boundary is exactly the kind of fact the
kernel is handed rather than reads.
"""

from pathlib import Path

from lup.harness.models import HookSet
from lup.harness.requirements import Finding
from lup.policy.boundary import (
    BoundaryCapability,
    BoundaryPreflight,
    CapabilityEvidence,
    ExecutionBoundary,
)
from lup.workspace.checkout_state import CheckoutState


def depended_on(hooks: HookSet, contained: bool) -> list[BoundaryCapability]:
    """The capabilities a profile of this posture actually depends on.

    One answer, asked in both places that need it, because the compiler and
    the measurement disagreeing about the roster is a capability with a
    requirement and no evidence -- or evidence for one nothing required.
    """
    return [
        entry for entry in hooks.boundary_capabilities if entry.depended_on(contained)
    ]


def compile_boundary(
    hooks: HookSet,
    contained: bool,
    writable: list[Path] = [],
    managed_roots: list[Path] = [CheckoutState(root=Path()).directory()],
    name: str = "",
) -> ExecutionBoundary:
    """What this profile promises, read off the declaration it already has.

    Every field here exists somewhere in the project's own harness
    declaration, and compiling rather than restating it is what stops the two
    drifting: a path added to the sandbox grant or a tree given the scratch
    role reaches the boundary by being declared, not by somebody remembering
    there was a second list.

    Which is why the ambient policy and the capability roster are taken from
    the hook set rather than passed: they are declarations like the rest, and
    a caller free to supply its own would be a second place they could be
    said — with the launcher's copy quietly overruling the one every
    dispatcher was compiled against.

    ``writable`` is the launch's lease and arrives as plain paths rather than
    as a :class:`~lup.sandbox.rail.Lease`, so this module stays clear of the
    container machinery it would otherwise have to import to name one. It is
    the *measured* half of the writable set: absolute, and mounted at the same
    path on both sides of the boundary, which is what makes it usable as a
    judgement. The declared sandbox grants join it spelled as they were
    written, because those answer for an uncontained session whose home is the
    launcher's own.

    ``precious_roots`` stays empty on purpose. Precious is what anything
    unnamed already is, and listing the roots that are already the default
    would make the omission of one read as a decision.
    """
    sandbox = hooks.sandbox
    return ExecutionBoundary(
        name=name or ("contained" if contained else "ambient"),
        contained=contained,
        writable_roots=[
            *writable,
            *(Path(grant) for grant in (sandbox.writable_paths if sandbox else [])),
        ],
        disposable_roots=[
            entry.root for entry in hooks.path_roles if entry.role == "scratch"
        ],
        managed_roots=managed_roots,
        network_destinations=[
            *(str(scope.origin) for scope in hooks.allowed_fetch),
            *(sandbox.extra_domains if sandbox else []),
        ],
        unjudged_ambient=hooks.unjudged_ambient,
        capabilities=[entry.requirement for entry in depended_on(hooks, contained)],
    )


def measured(
    boundary: ExecutionBoundary,
    capabilities: list[BoundaryCapability],
    findings: list[Finding],
) -> BoundaryPreflight:
    """What the launch observed, one piece of evidence per declared capability.

    Every capability the profile declares gets a row, including the ones that
    failed and the ones nothing probed. That is the whole discipline: a
    preflight holding evidence only for what worked reads identically to one
    where nothing was asked, and the second is the state this layer was built
    to make impossible to be in by accident.

    The detail is carried through from the probe's own words rather than
    summarised, because the diagnostic a person reads has to name what was
    tried. "The sandbox is broken" sends somebody to read configuration; the
    exercise's own failure sends them to the thing that failed.
    """
    return BoundaryPreflight(
        boundary=boundary,
        evidence=[evidence(entry, findings) for entry in capabilities],
    )


def evidence(entry: BoundaryCapability, findings: list[Finding]) -> CapabilityEvidence:
    """One capability's row, from the finding that answered for it.

    A declared probe with no finding is undelivered rather than absent from
    the report, and says which roster failed to carry it. That case is a
    wiring mistake -- a capability naming a manifest handle nothing declares,
    or a host-side probe expected from an image-side roster -- and it has to
    be visible as a failed measurement rather than as a capability quietly
    dropping out of the preflight.
    """
    found = [item for item in findings if item.requirement.capability == entry.probe]
    if not entry.probe:
        return CapabilityEvidence(
            capability=entry.requirement.capability,
            delivered=False,
            detail=entry.absent(),
        )
    if not found:
        return CapabilityEvidence(
            capability=entry.requirement.capability,
            delivered=False,
            detail=(
                f"no roster exercised {entry.probe!r}, so this capability was "
                "declared and never asked about"
            ),
        )
    answered = found[0]
    return CapabilityEvidence(
        capability=entry.requirement.capability,
        delivered=answered.working,
        detail=answered.detail,
    )
