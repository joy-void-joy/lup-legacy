"""A project that declined the resolver module declares no resolver spec."""

import lup_template.harness.catalog as catalog
import lup_template.harness.content.catalog as content


def test_a_declined_resolver_leaves_the_harness_without_a_spec() -> None:
    """The spec's three invocations name skills a declined module does not
    declare, so a harness carrying one would refuse to validate."""
    declined = content.composition(declined=["resolver"])

    assert catalog.portable_harness(composed=declined).resolver is None


def test_a_taken_resolver_still_declares_its_spec() -> None:
    spec = catalog.portable_harness().resolver
    assert spec is not None
    assert spec.worker_skill.skill == "implementer"
