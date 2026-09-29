"""Local web surfaces: the boundaries a page served on this machine keeps.

What a page served on this machine does to stay local-only — the loopback bind
refusal and the `Host` check that DNS rebinding would otherwise walk past —
and the one sequence that stands such a page up, with the bundles and view
schemas it serves. One subject: a local HTTP surface a browser reaches. The
two user-facing pages, `devtools/dashboard` and `devtools/supervisor`, sit
*on* this; it does not belong beside them.
"""
