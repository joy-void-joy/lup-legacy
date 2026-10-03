"""A neutral module spelling a runtime's own tree as a path is refused.

Each adapter states its tree, and the library reads it off the adapter
everywhere else. The native-spelling rule keeps it so: a path naming
`.claude` or `.codex` in a neutral module is a spelling only that runtime's
adapter may hold. The names come from the runtimes, through the application
roots, so the rule itself names no runtime.
"""

from pathlib import Path

import pytest

from lup.harness.codescan.boundaries import audit_path_boundaries
from lup.harness.codescan.common import ApplicationRoots
from lup.providers.harness import every_runtime
from lup_template.harness.catalog import application_roots

TREES = ApplicationRoots(runtime_trees=[".claude/", ".codex/"])
"""An application naming both runtimes' trees, as the catalog's does."""

NEUTRAL = Path("packages/lup/src/lup/sessions/example.py")
ADAPTER = Path("packages/lup/src/lup/providers/claude/example.py")


def spelling_findings(path: Path, code: str) -> list[str]:
    """What the native-spelling rule reports about one line written at ``path``."""
    return [
        finding.message
        for finding in audit_path_boundaries(path, f"{code}\n", TREES)
        if finding.kind == "missing" and finding.rule_id == "native-spelling"
    ]


def test_the_rule_is_given_every_runtimes_tree() -> None:
    assert application_roots().runtime_trees == [
        runtime.tree("tree_root") for runtime in every_runtime()
    ]


@pytest.mark.parametrize(
    "code",
    [
        'plugin = root / ".claude" / "plugins"',
        'settings = Path(".codex/config.local.toml")',
        'source = "./.claude/plugins"',
        'rule = "Read(./.claude/settings.json.local*)"',
        'target = f"{root}/.codex/plugins"',
    ],
)
def test_a_neutral_module_spelling_a_runtime_tree_is_refused(code: str) -> None:
    assert spelling_findings(NEUTRAL, code)
    assert spelling_findings(ADAPTER, code) == []


@pytest.mark.parametrize(
    "code",
    [
        'reason = "edit .claude/settings.json by hand and the next run undoes it"',
        'config = Path.home() / ".claude.json"',
        'url = "https://code.claude.com/docs"',
        'settings = root / CLAUDE.tree("project_settings")',
    ],
)
def test_prose_another_file_and_an_adapters_spelling_are_not_a_tree_path(
    code: str,
) -> None:
    assert spelling_findings(NEUTRAL, code) == []


def test_an_application_naming_no_runtime_tree_refuses_none() -> None:
    findings = audit_path_boundaries(
        NEUTRAL, 'plugin = root / ".claude" / "plugins"\n', ApplicationRoots()
    )

    assert [finding for finding in findings if finding.kind == "missing"] == []
