"""Launching a declared agent: the vocabulary both compilations of one declaration read.

An agent declared as :class:`~lup.providers.claude.Claude` or
:class:`~lup.providers.codex.Codex` is compiled twice from the same fields:
into SDK options when a program opens a session in process, and into the
``(argv, env, cwd)`` an interactive CLI runs when a person launches one. What
the two compilations share without being either provider's lives here, one
module per concern.

- :mod:`lup.launch.declaration` — the fields a launch adds to a declaration
  (the sandbox and its mounts, the coordination identity, the recording, the
  session a launch reopens), the command a launch compiles to, and the
  lifecycle steps a repository runs around it.
- :mod:`lup.launch.companions` — what a session wants running on the host
  beside it, held around every session a declaration opens, and the leases
  one process shared by several sessions is kept alive by.
- :mod:`lup.launch.compilation` — the fields every runtime compiles alike
  for either output: the recursive-agent allowance, the declared policy as
  in-process hooks, the environment a launched CLI inherits.
- :mod:`lup.launch.session` — composing a launched session: the gates it
  clears, the boundary it is measured behind, the argv inside the container
  or on the host, and the transcript kept of it.
- :mod:`lup.launch.container`, :mod:`lup.launch.config_volume`,
  :mod:`lup.launch.environments` — the verified container, the
  configuration home a contained session keeps, and the environments it
  syncs; :mod:`lup.launch.preflight`, :mod:`lup.launch.pointer_trust` and
  :mod:`lup.launch.superseded` — what a launch records and refuses on the
  way in.
- :mod:`lup.launch.boundary` — vouching for the inner sandbox, the same way
  on every runtime; :mod:`lup.launch.foreground` — running the CLI in the
  foreground with the terminal inherited, between the steps; and
  :mod:`lup.launch.refusal` — the one refusal a launch raises.

Each provider's own spelling of the same fields sits beside its adapter, in
``lup.providers.<runtime>.launch``.
"""
