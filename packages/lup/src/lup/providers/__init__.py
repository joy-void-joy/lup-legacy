"""Native implementations of Lup's independently composed capabilities.

Provider configuration, wire formats, event decoding, harness rendering, and
session construction stay in the named adapter package. Portable callers use
the narrow contracts and semantic models in :mod:`lup.sessions`,
:mod:`lup.harness`, and :mod:`lup.policy`.

Parity between the two adapters
===============================

Parity here means each adapter answers every portable question its runtime
can answer, and says why where it cannot. It never means matching module for
module: the two runtimes are reached differently — one through an SDK, one
through a JSON-RPC process — so a module with no counterpart is usually that
difference showing rather than something missing.

What follows is a module-by-module reading of both packages in both
directions. Each unmatched module says which it is, so a later reader can
tell a decided asymmetry from an unnoticed one.

Matched, and answering the same questions
-----------------------------------------

``harness.py`` — both spell one :class:`~lup.harness.contracts.NativeSpellings`,
which is closed by construction: a method added there cannot be left
unanswered by either. One method is answered by declining (see below).
Codex additionally compiles native prefix rules and a project config file,
because it reads shell allows and tool servers from files Claude has no
equivalent of; Claude renders a separate MCP artifact for the same reason in
reverse.

``native.py`` — both decode their runtime's own tool payloads into the shared
policy vocabulary and render a decision back. Claude sees an edit preimage
and Codex an opaque patch, which is why one has separate edit and write
operations and the other one file-change operation; Codex's renderer also
carries ``supports_ask`` because approval at its hook boundary is claimed
only where it is evidenced.

``config.py``, ``login.py``, ``runtime.py``, ``assets/policy_dispatcher.py``
— matched, class for class.

``hooks.py`` — matched in purpose, not in shape. Codex adds an approval
responder because its transport elicits approvals over the wire that the
Claude SDK resolves internally.

``harness_runtime.py`` — Codex adds a content-addressed plugin installer,
because its trust model requires an installed, verified plugin where Claude
trusts a directory it is handed.

``selection.py`` — both render a portable request into their own session
configuration and refuse nothing silently: Codex names the three fields it
has no spelling for and raises rather than dropping them. Each splits
rendering from building, so an application can stack a ``ConfigTransform``
onto what a request asked for before a session exists — which is what the
transforms in each ``config.py`` are for, and what the Codex side has no
entry point to without that split.

``usage/`` — both read an account's metered windows and its daily tokens into
the report in :mod:`lup.observability.usage`, which owns the display, the pacing bars, and
the ``--json`` snapshot. Codex publishes both readings over its own
app-server as Claude does, so the display is neutral and each adapter holds
only what its account actually reports. What differs is that one
account splits its tokens by model and the other does not, which is why one
draws a legend and the other has none to draw.

The Codex method names are read off the shipped binary rather than off the
published schema, which is what keeps the daily read from being spelled
wrongly: the response type is ``GetAccountTokenUsageResponse`` and the
notification beside it is ``thread/tokenUsage/updated``, so the method looks
like it should match, and it does not. A wrong method name here is invisible
— the runtime answers with an error, and an error on the daily read renders
as an account with no history — so the binary is the authority, and
``codescan``'s sanctioned-spelling table is the second place a rename has to
reach.

Unmatched, and deliberately so
------------------------------

``codex/app_server.py``, ``codex/patch.py`` — the JSON-RPC transport and the
patch-envelope decoder. Both exist because the Codex runtime is a process
speaking a wire protocol; the Claude runtime is a library, and a counterpart
would have nothing to do.

``claude/config_home.py`` — the configuration document, where it sits under
each setting of the configuration-home variable, and the workspace trust
recorded inside it. Unmatched because the two runtimes disagree about what a
configuration document is for: Claude keeps per-project trust in one JSON
document whose location changes with that variable, and a session derived
from the wrong one starts trusting nothing. Codex records installed state
and hook trust in the TOML its home already holds, which ``codex/home.py``
seeds and sanitizes — so the concern is answered there rather than absent,
and a Codex counterpart to this module would have no second document to
reconcile.

``codex/home.py`` has no Claude counterpart, and that is not a gap. Both
runtimes keep accounts the same way — a directory per name under the
person's lup config home, a home per runtime inside it (``profile_tree.py``)
— but a Codex launch opens a per-worktree home derived from the selected
account's, because its plugin is installed per home; Claude takes its plugin
by flag and opens the account's home as it stands. Codex keeps one rotating
credential the runtime refreshes in place, so the per-worktree homes converge
on the account home rather than copying it and diverging, which would strand
every stale copy.

Declined rather than absent
---------------------------

One portable idea has no Codex spelling, and says so through an
:class:`~lup.harness.contracts.Unsupported` carrying the reason: handing a
document whole to a tool, which nothing in its roster does. It is a declared
answer rather than a missing method, so prose that asks for it gets nothing
rather than an approximation, and an audit gets the reason.
"""
