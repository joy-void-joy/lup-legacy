"""The package root stays a deliberately small runtime front door.

Small, but not only nouns. A root exporting carrier types and nothing that made
one would reach no agent at all, and every non-trivial example would have to
open a session by importing an adapter — the tier
`seam-boundary` fails the build over everywhere else in the library. The
agents below are what prevents that, asserted here so the root cannot quietly
become a vocabulary.

The fields a launch adds to an agent's declaration are taken from the same
door, and resolved the same way: each is the class its declaration module
defines, and none of the machinery behind them loads until one is named.
"""

import subprocess
import sys

import lup
import lup.launch.companions as companions
import lup.launch.declaration as declaration

AGENTS = {"Claude", "Codex"}

LAUNCH_VOCABULARY = {
    "InnerSandbox",
    "Latest",
    "Member",
    "Mount",
    "NoSandbox",
    "OuterContainer",
    "Pick",
    "Recording",
    "Reopen",
}

COMPANION_VOCABULARY = {
    "CompanionLaunch",
    "CompanionPlace",
    "CompanionProcess",
    "CompanionScope",
    "Contribution",
    "HostCompanion",
    "SharedProcess",
}


def in_a_fresh_interpreter(source: str) -> str:
    """Run one program in its own interpreter and hand back what it printed.

    Import-time properties can only be asserted where nothing has imported
    anything yet. In-process, `sys.modules` carries whatever every earlier test
    in the session pulled in — this suite loads the Claude SDK elsewhere — so
    an in-process version of the assertions below would be measuring the test
    run rather than the package.
    """
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_every_export_resolves() -> None:
    missing = [name for name in lup.__all__ if getattr(lup, name, None) is None]
    assert missing == []


def test_root_exports_only_portable_runtime_conveniences() -> None:
    assert set(lup.__all__) == {  # lup: ignore[set-shape] — exact export comparison
        "Agent",
        "Claude",
        "Codex",
        # What a launch keeps running on the host beside a session.
        "CompanionLaunch",
        "CompanionPlace",
        "CompanionProcess",
        "CompanionScope",
        "Contribution",
        "Conversation",
        # The one way to name a model no catalog lists, which every model
        # argument a root agent takes accepts.
        "CustomModel",
        "HostCompanion",
        "HostService",
        # The fields a launch adds to an agent's declaration.
        "InnerSandbox",
        "Latest",
        "Member",
        "Mount",
        "NoSandbox",
        "OuterContainer",
        "Pick",
        "Recording",
        "Reopen",
        "SessionId",
        "SessionSummary",
        "SharedProcess",
        "Turn",
        "TurnId",
        "TurnInput",
        "TurnMessage",
        "TurnResult",
    }


def test_the_root_exports_an_agent_for_every_provider() -> None:
    """The property the export list above is only one spelling of.

    A reader who has found `import lup` must be able to get an agent from it.
    Stated separately because the set comparison passes for any set — including
    one that has lost every agent and kept the nouns, which is exactly the
    vocabulary this front door must not shrink to.
    """
    assert AGENTS <= set(lup.__all__)
    assert all(callable(getattr(lup, name)) for name in AGENTS)


def test_importing_lup_loads_no_provider_sdk_and_stays_small() -> None:
    """The promise the module docstring makes, measured where it is meaningful.

    Eagerly re-exporting the agents pulls several hundred modules — an ASGI
    server and a CLI framework among them — on behalf of a caller who may
    have wanted a type annotation. Deferring them is what makes
    the root cheap enough to be the thing everybody imports.
    """
    reported = in_a_fresh_interpreter(
        "import sys, lup; print(len(sys.modules), 'claude_agent_sdk' in sys.modules)"
    )
    count, sdk_loaded = reported.split()

    assert sdk_loaded == "False"
    assert int(count) < 400, f"`import lup` pulled {count} modules"


def test_naming_an_agent_loads_its_adapter_but_no_provider_sdk() -> None:
    """Reaching an agent imports its adapter, which is unavoidable.

    The vendor's own SDK is not, and is loaded by opening a session instead —
    so `import lup` works, and keeps working, on a machine with only one
    provider's SDK installed, or neither.
    """
    reported = in_a_fresh_interpreter(
        "import sys; "
        "from lup import Claude, Codex; "
        "print(Claude.__module__, Codex.__module__, "
        "'claude_agent_sdk' in sys.modules)"
    )

    assert reported == "lup.providers.claude lup.providers.codex False"


def test_each_launch_field_is_the_class_its_declaration_module_defines() -> None:
    """The root hands back the declaration's own class, not a copy or a stand-in.

    Identity rather than equality of names: a field taken from the root and one
    taken from `lup.launch.declaration` have to be the same class, or a
    declaration built from one would fail an `isinstance` against the other.
    """
    assert LAUNCH_VOCABULARY <= set(lup.__all__)
    assert [
        name
        for name in sorted(LAUNCH_VOCABULARY)
        if getattr(lup, name) is not getattr(declaration, name)
    ] == []
    assert COMPANION_VOCABULARY <= set(lup.__all__)
    assert [
        name
        for name in sorted(COMPANION_VOCABULARY)
        if getattr(lup, name) is not getattr(companions, name)
    ] == []


def test_importing_lup_loads_no_launch_machinery() -> None:
    """The launch vocabulary resolves lazily for the same reason the agents do.

    Its declaration module stands beside the harness, policy and sandbox
    machinery a launch composes, several hundred modules deep; a caller who
    imported `lup` for a type annotation pays for none of it.
    """
    reported = in_a_fresh_interpreter(
        "import sys, lup; print('lup.launch' in sys.modules)"
    )

    assert reported == "False"


def test_naming_a_launch_field_loads_its_declaration_but_no_provider_sdk() -> None:
    """A launch field is provider-neutral, so naming one reaches no SDK either."""
    reported = in_a_fresh_interpreter(
        "import sys; "
        "from lup import InnerSandbox, Reopen; "
        "print(InnerSandbox.__module__, Reopen.__module__, "
        "'claude_agent_sdk' in sys.modules)"
    )

    assert reported == "lup.launch.declaration lup.launch.declaration False"
