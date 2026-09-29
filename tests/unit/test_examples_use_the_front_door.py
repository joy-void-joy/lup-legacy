"""Every shipped example takes its agent and its launch fields from the root.

The corpus that teaches the library is the first thing a reader copies, so what
it reaches for is what they learn to reach for. A root exporting no agent could
teach nothing else: every example would open a session by importing an adapter
directly, which is the one tier `seam-boundary` fails the build over everywhere
else in the library, and the front door would be the defect.

This fails on the import line rather than on the day somebody tries the
example, and it is deliberately about the names the root resolves on first
access -- the agents and the fields a launch adds to one -- rather than about
library imports in general: an example whose whole subject is policy
legitimately reaches into `lup.policy`. What none of them may do is reach past
the root for `Claude` or `InnerSandbox`, because a reader who has to know
`lup.providers.claude` or `lup.launch.declaration` exists to declare an agent
has already been failed.

And every field a launch adds to an agent has an example of its own, taking
its agent from the root, so the corpus demonstrates each capability
`harness claude|codex` has as the declaration a program writes.
"""

import ast
from pathlib import Path

import pytest
from pydantic import BaseModel

from lup import LAZY_EXPORTS

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# The agents the root exports, which every example running a turn between them
# has to demonstrate; the adapters are where they live, not where an example
# is supposed to find them.
AGENTS = {"Claude", "Codex"}


def example_sources() -> list[Path]:
    found = sorted(path for path in EXAMPLES.glob("*.py") if path.name != "__init__.py")
    assert found, "no examples found to check"
    return found


def imported_names(tree: ast.Module) -> list[tuple[str, str]]:
    """Every `from <module> import <name>` in the file, as pairs."""
    return [
        (node.module or "", alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]


@pytest.mark.parametrize("path", example_sources(), ids=lambda p: p.name)
def test_a_lazy_export_is_taken_from_the_package_root(path: Path) -> None:
    imported = imported_names(ast.parse(path.read_text(encoding="utf-8")))
    reached = [
        (module, name)
        for module, name in imported
        if name in LAZY_EXPORTS and module != "lup"
    ]

    assert not reached, (
        f"{path.name} takes {reached[0][1]} from {reached[0][0]!r}. It is "
        f"exported from the package root — `from lup import {reached[0][1]}` — "
        "and an example that reaches past it teaches a reader to do the same."
    )


@pytest.mark.parametrize("path", example_sources(), ids=lambda p: p.name)
def test_no_example_opens_a_session_through_an_adapter(path: Path) -> None:
    """The narrower thing that is always wrong: naming an opener.

    A `ClaudeSessionOpener` or `CodexSessionOpener` is the engine an agent
    composes. An example holding
    one has not configured an agent differently — it has stepped inside the
    composition root, where the contract it depends on is not a public one.
    """
    imported = imported_names(ast.parse(path.read_text(encoding="utf-8")))
    openers = [
        (module, name) for module, name in imported if name.endswith("SessionOpener")
    ]

    assert not openers, f"{path.name} imports {openers[0][1]!r}, an internal engine"


def imported_modules(tree: ast.Module) -> list[str]:
    """Every module the file imports, whichever statement spells it."""
    modules: list[str] = []
    for node in ast.walk(tree):
        match node:
            case ast.ImportFrom(module=str(module)):
                modules.append(module)
            case ast.Import(names=aliases):
                modules.extend(alias.name for alias in aliases)
            case _:
                pass
    return modules


@pytest.mark.parametrize("path", example_sources(), ids=lambda p: p.name)
def test_an_example_imports_the_library_not_this_application(path: Path) -> None:
    """An example runs wherever lup is installed, so it reads nothing under `src/`.

    What it demonstrates is the library; a value it needs from a project — a
    hook set, a vocabulary — it declares itself. Reaching into this
    repository's application package makes the example one only this checkout
    can run.
    """
    applications = [
        package.name
        for package in (EXAMPLES.parent / "src").iterdir()
        if (package / "__init__.py").is_file()
    ]
    reached = [
        module
        for module in imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        if module.split(".")[0] in applications
    ]

    assert not reached, (
        f"{path.name} imports {reached[0]!r} from this repository's application; "
        "declare what the example needs in the example itself"
    )


def test_every_example_that_runs_a_turn_names_the_root() -> None:
    """Stated over the corpus, so the property cannot decay one file at a time.

    A per-file check passes vacuously for a corpus that has stopped using the
    front door entirely — the failure this whole test exists for. At least one
    example must import each agent from `lup`, or there is nothing being
    demonstrated.
    """
    reached = {
        name
        for path in example_sources()
        for module, name in imported_names(ast.parse(path.read_text(encoding="utf-8")))
        if module == "lup" and name in AGENTS
    }

    assert reached == AGENTS, (
        f"the examples demonstrate {sorted(reached)} from the package root; "
        f"every agent the root exports needs one — missing "
        f"{sorted(AGENTS - reached)}"
    )


class FieldExample(BaseModel, frozen=True):
    """Where one field a launch adds to an agent is demonstrated, and how it is spelled."""

    example: str
    """The example module's file name."""

    call: str
    """The constructor the field is declared in."""

    keyword: str | None = None
    """The keyword naming the field in that call, or none where the call is the field."""


# The capabilities `harness claude|codex` has, each a declaration field both
# compilations honour — decision 16 of the DX overhaul — and the host companions
# the launch declaration gained beside them.
LAUNCH_FIELDS = {
    "plugin": FieldExample(example="launch_plugin.py", call="Claude", keyword="plugin"),
    "policy hooks": FieldExample(
        example="launch_policy.py", call="Claude", keyword="policy"
    ),
    "MCP": FieldExample(
        example="launch_tool_servers.py", call="ClaudeTools", keyword="mcp"
    ),
    "inner sandbox": FieldExample(
        example="launch_inner_sandbox.py", call="InnerSandbox"
    ),
    "outer container": FieldExample(
        example="launch_outer_container.py", call="OuterContainer"
    ),
    "coordination identity and wake socket": FieldExample(
        example="launch_identity.py", call="Member", keyword="wake_sockets"
    ),
    "profile": FieldExample(
        example="launch_profile.py", call="Claude", keyword="profile"
    ),
    "config home": FieldExample(example="launch_home.py", call="Codex", keyword="home"),
    "transcript and ledger recording": FieldExample(
        example="launch_recording.py", call="Recording", keyword="ledger"
    ),
    "resume": FieldExample(example="launch_resume.py", call="Claude", keyword="resume"),
    "recursion allowance": FieldExample(
        example="launch_recursion.py", call="Claude", keyword="max_recursive_agent"
    ),
    "mounts": FieldExample(example="launch_mounts.py", call="Mount"),
    "devices": FieldExample(
        example="launch_devices.py", call="OuterContainer", keyword="devices"
    ),
    "host companions": FieldExample(
        example="launch_companions.py", call="Claude", keyword="companions"
    ),
    "container network": FieldExample(
        example="launch_network.py", call="OuterContainer", keyword="network"
    ),
    "container memory": FieldExample(
        example="launch_memory.py", call="OuterContainer", keyword="memory"
    ),
    "how much the runtime asks": FieldExample(
        example="launch_asking.py", call="Codex", keyword="approvals_reviewer"
    ),
    "a kind of session's own guidance": FieldExample(
        example="launch_guidance.py", call="OuterContainer", keyword="guidance"
    ),
    "a companion's host-only secrets": FieldExample(
        example="launch_companion_secrets.py", call="Preview", keyword="secrets"
    ),
    "named host services": FieldExample(
        example="launch_host_service.py", call="HostService"
    ),
    "held generated trees": FieldExample(
        example="launch_held_trees.py", call="OuterContainer", keyword="hold_generated"
    ),
}


def calls(tree: ast.Module) -> list[ast.Call]:
    """Every call the example makes, by the name it calls."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def called(node: ast.Call) -> str:
    match node.func:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
        case _:
            return ""


@pytest.mark.parametrize("field", list(LAUNCH_FIELDS), ids=str)
def test_every_launch_field_has_an_example_declaring_it(field: str) -> None:
    """One example per field, so each is demonstrated where a reader copies from.

    Checked by pyright as the rest of the corpus is — the examples are in the
    workspace's include — so an example declaring a field wrongly fails the
    build rather than the reader.
    """
    wanted = LAUNCH_FIELDS[field]
    path = EXAMPLES / wanted.example
    assert path.is_file(), f"no example for {field}: {wanted.example}"
    declared = [
        node
        for node in calls(ast.parse(path.read_text(encoding="utf-8")))
        if called(node) == wanted.call
        and (
            wanted.keyword is None
            or any(keyword.arg == wanted.keyword for keyword in node.keywords)
        )
    ]

    assert declared, f"{wanted.example} declares no {wanted.call}" + (
        f"({wanted.keyword}=...)" if wanted.keyword else "()"
    )


@pytest.mark.parametrize(
    "path", [EXAMPLES / wanted.example for wanted in LAUNCH_FIELDS.values()], ids=str
)
def test_a_launch_example_takes_its_agent_from_the_root(path: Path) -> None:
    imported = imported_names(ast.parse(path.read_text(encoding="utf-8")))

    assert any(module == "lup" and name in AGENTS for module, name in imported)
