"""The front-door rule: nothing inside the library imports from the package root.

The root re-exports the library's public names for its users and resolves the
agents and the launch vocabulary lazily. These pin both spellings that read it
from inside, the module each diagnostic sends a reader to, the scope that
leaves the front door's users alone, and the live library at zero.
"""

import importlib
from pathlib import Path

import pytest

from lup import LAZY_EXPORTS
from lup.devtools.dev.boundaries import library_sources
from lup.execution.shell import git
from lup.harness.codescan.boundaries import (
    FRONT_DOOR_PATH,
    RuleId,
    front_door_exports,
    front_door_findings,
)
from lup.harness.codescan.common import PythonSource, module_name
from lup.harness.codescan.project import AuditedProject, RuleFinding

LIBRARY_MODULE = "packages/lup/src/lup/sessions/client.py"


def source(text: str, path: str = LIBRARY_MODULE) -> PythonSource:
    return PythonSource(path=Path(path), module=module_name(Path(path)), text=text)


ROOT = source(
    "from typing import TYPE_CHECKING\n"
    "from lup.sessions.surface import Agent\n"
    "if TYPE_CHECKING:\n"
    "    from lup.launch.declaration import InnerSandbox\n"
    "    from lup.providers.claude import Claude\n",
    FRONT_DOOR_PATH,
)
"""A root re-exporting one name eagerly, and one agent and one launch field it
resolves lazily."""


def audit(text: str, path: str = LIBRARY_MODULE) -> list[RuleFinding]:
    return front_door_findings(AuditedProject(sources=[ROOT, source(text, path)]))


@pytest.mark.parametrize(
    ("text", "path", "line", "remedy"),
    [
        (
            "from lup import Agent\n",
            LIBRARY_MODULE,
            1,
            "from lup.sessions.surface import Agent",
        ),
        (
            "from lup import Claude\n",
            LIBRARY_MODULE,
            1,
            "from lup.providers.claude import Claude",
        ),
        (
            "from lup import InnerSandbox\n",
            LIBRARY_MODULE,
            1,
            "from lup.launch.declaration import InnerSandbox",
        ),
        (
            "import lup\n\nagent = lup.Agent\n",
            LIBRARY_MODULE,
            3,
            "from lup.sessions.surface import Agent",
        ),
        (
            "import lup.sessions\n\nagent = lup.Claude\n",
            LIBRARY_MODULE,
            3,
            "from lup.providers.claude import Claude",
        ),
        (
            "import lup as front\n\nagent = front.Agent\n",
            LIBRARY_MODULE,
            3,
            "from lup.sessions.surface import Agent",
        ),
        (
            "from .. import Agent\n",
            LIBRARY_MODULE,
            1,
            "from lup.sessions.surface import Agent",
        ),
        (
            "from .. import Agent\n",
            "packages/lup/src/lup/sessions/__init__.py",
            1,
            "from lup.sessions.surface import Agent",
        ),
        (
            "from . import Claude\n",
            "packages/lup/src/lup/types.py",
            1,
            "from lup.providers.claude import Claude",
        ),
    ],
    ids=[
        "from-import",
        "lazy-agent",
        "lazy-launch-field",
        "attribute",
        "attribute-through-a-submodule-import",
        "aliased-root",
        "relative-climb",
        "relative-climb-from-a-package",
        "relative-from-a-top-level-module",
    ],
)
def test_a_read_of_the_root_is_sent_to_the_module_defining_the_name(
    text: str, path: str, line: int, remedy: str
) -> None:
    findings = audit(text, path)

    assert [(item.kind, item.line, item.rule_id) for item in findings] == [
        ("missing", line, RuleId.FRONT_DOOR)
    ]
    assert remedy in findings[0].message


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("from lup.sessions.surface import Agent\n", LIBRARY_MODULE),
        ("import lup\n\nroot = lup.__file__\n", LIBRARY_MODULE),
        (
            "import lup.sessions.surface as surface\n\nagent = surface.Agent\n",
            LIBRARY_MODULE,
        ),
        ("from . import events\n", LIBRARY_MODULE),
        ("from .events import TurnId\n", LIBRARY_MODULE),
        ("from . import events\n", "packages/lup/src/lup/sessions/__init__.py"),
    ],
    ids=[
        "defining-module",
        "package-location",
        "aliased-submodule",
        "sibling-module",
        "relative-module",
        "package-own-module",
    ],
)
def test_a_name_taken_where_it_is_defined_is_cleared(text: str, path: str) -> None:
    assert audit(text, path) == []


@pytest.mark.parametrize(
    "path",
    [
        "src/app/agent.py",
        "examples/quickstart.py",
        "packages/lup/tests/unit/test_sessions.py",
        FRONT_DOOR_PATH,
    ],
)
def test_the_front_doors_users_and_the_root_itself_are_not_judged(path: str) -> None:
    held = source("from lup import Claude\nimport lup\nagent = lup.Agent\n", path)

    assert front_door_findings(AuditedProject(sources=[ROOT, held])) == []


def test_a_name_the_root_does_not_export_is_refused_all_the_same() -> None:
    [finding] = audit("from lup import sessions\n")

    assert "import sessions from the module that defines it" in finding.message


def test_without_the_root_only_the_from_import_can_be_judged() -> None:
    """The attribute form is read against what the root exports, so it needs one."""
    held = source("from lup import Claude\nimport lup\nagent = lup.Agent\n")

    findings = front_door_findings(AuditedProject(sources=[held]))

    assert [(item.line, item.kind) for item in findings] == [(1, "missing")]
    assert "import Claude from the module that defines it" in findings[0].message


def test_the_rule_is_strong_and_refuses_every_directive() -> None:
    findings = audit("from lup import Agent  # lup: ignore[front-door] — a reason\n")

    assert sorted(item.kind for item in findings) == ["missing", "spurious"]


def live_library(monkeypatch: pytest.MonkeyPatch) -> list[PythonSource]:
    """The library as the tree holds it, read from the checkout's root.

    `library_sources` lists paths relative to the working directory, and this
    suite runs from `packages/lup`, where no path starts with the library root:
    unanchored, the scan read nothing and the zero-findings test passed on it.
    """
    monkeypatch.chdir(git.out("rev-parse", "--show-toplevel").strip())
    held = [source(item.text, item.rel) for item in library_sources()]
    assert held, "no library source was read, so a scan over it proves nothing"
    return held


def test_every_name_the_live_root_exports_is_where_a_diagnostic_sends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = live_library(monkeypatch)
    [root] = [held for held in library if held.path == Path(FRONT_DOOR_PATH)]
    exports = front_door_exports(root.text)

    assert {"Claude", "Codex"} <= exports.keys()
    assert [
        f"{module} does not define {name}"
        for name, module in exports.items()
        if not hasattr(importlib.import_module(module), name)
    ] == []


def test_the_live_library_reads_nothing_through_its_front_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = live_library(monkeypatch)
    findings = front_door_findings(AuditedProject(sources=library))

    assert [f"{item.path}:{item.line} {item.message}" for item in findings] == []


def test_the_live_root_names_each_lazy_export_where_its_table_resolves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table the module hook reads and the block a checker reads are one list.

    Spelled twice because neither can be derived from the other: the
    ``TYPE_CHECKING`` block never runs, and a checker cannot read a table. A
    row the block lacks is a name no checker sees and no diagnostic can send a
    reader to; a block naming another module types a name as a class the hook
    never hands back.
    """
    library = live_library(monkeypatch)
    [root] = [held for held in library if held.path == Path(FRONT_DOOR_PATH)]
    exports = front_door_exports(root.text)

    assert {
        name: module for name, module in exports.items() if name in LAZY_EXPORTS
    } == LAZY_EXPORTS
