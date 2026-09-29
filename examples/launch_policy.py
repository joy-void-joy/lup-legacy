"""Judge every call a launched session makes by one declared policy.

``policy=`` is the semantic policy the plugin's dispatcher enforces and the
boundary a launch measures and records; named beside a harness plugin it
must be that harness's own. A session opened in this process is judged by
the same declaration, compiled into in-process hooks instead.

    uv run -m examples.launch_policy
"""

from pathlib import Path

from lup import Claude
from lup.harness.models import HookSandbox, HookSet


def main() -> None:
    policy = HookSet(
        id="hooks.example",
        policy_ids=["shell", "edit", "fetch"],
        sandbox=HookSandbox(excluded_commands=["git *"], writable_paths=["/tmp"]),
    )
    agent = Claude(plugin=Path(".claude/plugins/lup"), policy=policy)
    print(agent.command())


if __name__ == "__main__":
    main()
