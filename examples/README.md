# Runtime composition examples

These modules are executable compositions, not abbreviated fragments. Run
them from the repository root with `uv`; provider-backed examples use your
existing CLI credentials and make a real model call.

```bash
uv run -m examples.one_shot
uv run -m examples.wrapper_stack
uv run -m examples.background_agent
uv run -m examples.profile_transform
uv run -m examples.model_route
```

`monitored_run` is the one that makes no model call at all: a three-stage
pipeline — a shell step, a fan-out sized by what it found, and a reduce —
written so the run can be resumed a part at a time and watched while it goes.

```bash
uv run -m examples.monitored_run plan
uv run -m examples.monitored_run run
uv run lup-devtools run monitor tmp/runs/monitored-run --events
```

Run it twice and the second run does nothing; edit `measure` and only it and
`report` recompute. `docs/runs.md` carries why.

`profile_transform` expects a Claude configuration home at `~/.claude-work`.
Change that path to a profile you own. `compatible_endpoint` expects an
Anthropic-compatible service on `http://localhost:4000`:

```bash
uv run -m examples.compatible_endpoint
```

The policy pair wires the semantic policies into a session's hooks, so the
call a policy denies is a call the session refuses — the fetch path and the
tool-call path of the same declared origin table:

```bash
uv run -m examples.semantic_policy
uv run -m examples.semantic_policy_shell
```

Both make a real model call. Their enforcement is checked without one by
`tests/unit/test_policy_examples.py`, which drives each example's own
session configuration through the hooks the SDK would invoke.

## Launching, one field at a time

Each `launch_*` module declares one field a launch adds to an agent, on
Claude Code and Codex where both take it, and prints the command the
declaration compiles to: what `launch()` runs in the foreground, the
terminal handed over, after checking the host and measuring the boundary the
way a launch does.

```bash
uv run -m examples.launch_plugin
uv run -m examples.launch_policy
uv run -m examples.launch_tool_servers
uv run -m examples.launch_inner_sandbox
uv run -m examples.launch_outer_container
uv run -m examples.launch_identity
uv run -m examples.launch_profile
uv run -m examples.launch_home
uv run -m examples.launch_recording
uv run -m examples.launch_resume
uv run -m examples.launch_recursion
uv run -m examples.launch_mounts
uv run -m examples.launch_devices
uv run -m examples.launch_sudo
uv run -m examples.launch_nested_repositories
uv run -m examples.launch_companions
```

`launch_profile` expects a profile named `work` among yours
(`uv run lup-devtools harness profile list`), `launch_outer_container`,
`launch_devices` and `launch_nested_repositories` a Docker or Podman engine,
and `launch_sudo` a rootless one.

Each composition declares its agent at the application boundary. The
one-shot `ask`, the declared layers, the background scheduler, and the router
depend only on narrow runtime contracts once the agent is declared.
