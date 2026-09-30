"""Semantic permission policy that must decide identically in two homes.

The central concern: one canonical policy produces the same verdicts inside
this library (validated pydantic surfaces) and inside generated native plugin
dispatchers that run without lup installed. Every module placement follows
from that split:

- :mod:`lup.policy.kernel` — the hermetic decision core (shell, fetch, edit,
  anti-pattern scanning) over primitive rows, written against a pinned
  stdlib allowlist so it can be copied verbatim into generated runtimes.
- :mod:`lup.policy.models` — the typed semantic events and the
  allow/ask/deny ``Decision`` vocabulary.
- :mod:`lup.policy.contracts` — the ``DecisionPolicy`` seam.
- :mod:`lup.policy.native` — the decode seam a native adapter implements at
  its wire boundary.
- :mod:`lup.policy.rules` — validated pydantic policies that erase their
  inputs into kernel rows and delegate every verdict to the kernel.
- :mod:`lup.policy.chain` — the conservative answer for a call no semantic
  family classifies.
- :mod:`lup.policy.bundle` — assembly for generation: reads the kernel
  source verbatim and renders application-owned data rows as generated
  files; the adapters' hook renderers consume it.
- :mod:`lup.policy.dispatcher` — compilation for generation: composes each
  generated script from :mod:`lup.policy.assets.host`, the half no runtime
  spells differently, plus the adapter-owned half its declaration proves.

The kernel imports nothing outside its allowlist and no other module here;
``rules`` and ``chain`` build the library layer on kernel plus models; only
``bundle`` and ``dispatcher`` are about generating files rather than
deciding, and the host half decides nothing — it resolves what the kernel
deliberately cannot see.
"""
