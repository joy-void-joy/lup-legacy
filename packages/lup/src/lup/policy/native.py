"""The wire seam a native adapter implements in front of the semantic core.

:class:`NativeEventDecoder` turns one provider's raw hook payload into the
semantic events of :mod:`lup.policy.models`. Implementations live in
``lup.providers.<provider>.native``; the verdict goes back out through the
adapter's own hooks, from :func:`~lup.policy.enforcement.policy_hook_output`.
Nothing here decides — the kernel does.
"""

from abc import ABC, abstractmethod

from lup.policy.models import SemanticEvent


class NativeEventDecoder[N](ABC):
    """Decode one native boundary into Lup semantic events."""

    @abstractmethod
    def decode(self, event: N) -> SemanticEvent:
        """Decode or return conservative typed evidence."""
