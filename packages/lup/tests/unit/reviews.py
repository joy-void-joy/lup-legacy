"""Parking a review as a native hook parks it, for the suites that build one by hand."""

from lup.policy.relay import PersistentQuestion


def bound(question: PersistentQuestion) -> PersistentQuestion:
    """The same question under the fingerprint a native hook binds its record to."""
    return question.model_copy(update={"fingerprint": question.native_fingerprint()})
