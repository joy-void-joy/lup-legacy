"""The carrier fold registered under each runtime's prompt event, and run.

One rendering, two plugins. What is asserted per runtime is that the event
carries this guard beside the roster's own fold rather than in place of it,
that the entry cannot refuse, and — by running the rendered guard inside a
repository whose library pin and scaffold branch have parted, the way the
runtime would — that its stdout is the envelope both vendors document.

The fold is shipped verbatim and imports nothing of lup's, so the spelling it
shares with the writer is pinned here instead: the trailer it reads is the one
a scaffold commit carries.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
import sh

import lup.devtools.dev.drift_fold as fold
import lup.devtools.dev.scaffold as scaffold
from lup.harness.generate import NativeHarnessComposition
from lup.harness.models import CarrierPins, HookSet
from lup.providers.claude.harness import CLAUDE_PROMPT_EVENT
from lup.providers.codex.harness import CODEX_PROMPT_EVENT
from lup.providers.drift_prompt import GUARD_SCRIPT, RUNTIME_MODULE, drift_hook
from lup_template.harness.catalog import portable_harness
from lup_template.harness.composition import claude_target, codex_target

RUNTIMES = pytest.mark.parametrize(
    ("target", "tree", "event"),
    [
        pytest.param(claude_target, ".claude", CLAUDE_PROMPT_EVENT, id="claude"),
        pytest.param(codex_target, ".codex", CODEX_PROMPT_EVENT, id="codex"),
    ],
)


def rendered(tree: str) -> Path:
    """The plugin root one runtime's tree renders the hook under."""
    return Path(f"{tree}/plugins/lup")


def declared(branch: str) -> HookSet:
    """A hook set whose carriers name one branch, for a project that took one."""
    return portable_harness().declared_hooks.model_copy(
        update={"carriers": CarrierPins(branch=branch, distribution="lup-agents")}
    )


def shipped(tmp_path: Path, branch: str) -> Path:
    """The guard as a plugin ships it, beside the runtime it hands over to."""
    hook = drift_hook(
        Path("plugin"), "PLUGIN_ROOT", declared(branch), "UserPromptSubmit"
    )
    for artifact in hook.artifacts:
        path = tmp_path / artifact.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(artifact.content, encoding="utf-8")
    return tmp_path / "plugin" / "hooks" / "scripts" / GUARD_SCRIPT


def repository(root: Path) -> Path:
    """A git repository that can commit, wherever the caller wants one."""
    root.mkdir(parents=True, exist_ok=True)
    sh.git("init", "-q", "-b", "main", str(root))
    sh.git("-C", str(root), "config", "user.email", "t@example.test")
    sh.git("-C", str(root), "config", "user.name", "t")
    return root


def committed(root: Path, message: str) -> str:
    """Everything in the tree, as one commit, and the commit it became."""
    sh.git("-C", str(root), "add", "-A")
    sh.git("-C", str(root), "commit", "-q", "-m", message)
    return str(sh.git("-C", str(root), "rev-parse", "HEAD")).strip()


def test_the_fold_reads_the_trailer_the_scaffold_writes() -> None:
    """The one spelling the two halves share, pinned where an import would be."""
    assert fold.SCAFFOLD_TRAILER == scaffold.SCAFFOLD_TRAILER


@RUNTIMES
def test_the_prompt_event_carries_the_fold_and_refuses_nothing(
    target: Callable[[Path], NativeHarnessComposition], tree: str, event: str
) -> None:
    hook = drift_hook(rendered(tree), "PLUGIN_ROOT", declared("lup-scaffold"), event)

    assert [artifact.path.name for artifact in hook.artifacts] == [
        GUARD_SCRIPT,
        RUNTIME_MODULE,
    ]
    match hook.registered[event]:
        case [{"hooks": [{"command": str(command)}]}]:
            # Never `|| exit 2`: a fold that failed is a session that was not
            # told, and refusing the prompt over it would stop the work the
            # line exists to inform.
            assert command.endswith("|| exit 0")
            assert GUARD_SCRIPT in command
        case other:
            pytest.fail(f"{tree}: {other!r} registers no guard")


@RUNTIMES
def test_the_generated_tree_carries_both_folds_under_one_event(
    target: Callable[[Path], NativeHarnessComposition], tree: str, event: str
) -> None:
    """Merged registrations would keep one of the two, and quietly."""
    artifacts = {
        artifact.path: artifact
        for artifact in target(Path.cwd()).recipe.desired.artifacts
    }
    hooks = json.loads(artifacts[rendered(tree) / "hooks" / "hooks.json"].content)

    commands = [
        entry["command"] for group in hooks["hooks"][event] for entry in group["hooks"]
    ]

    assert [command for command in commands if GUARD_SCRIPT in command]
    assert [command for command in commands if "coordination_changes.sh" in command]


def test_a_project_that_took_no_copied_half_registers_nothing() -> None:
    """Including the scaffold itself, which is the origin of every copy."""
    source = portable_harness().declared_hooks.model_copy(update={"carriers": None})

    hook = drift_hook(Path(".claude/plugins/lup"), "PLUGIN_ROOT", source, "Prompt")

    assert hook.registered == {}
    assert hook.artifacts == []


def test_the_rendered_guard_says_nothing_where_no_scaffold_was_merged(
    tmp_path: Path,
) -> None:
    """One `git rev-parse` is the whole cost to a project with no upstream."""
    project = repository(tmp_path / "project")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    committed(project, "the project")

    spoken = sh.sh(
        str(shipped(tmp_path, "lup-scaffold")),
        _in="{}",
        _cwd=str(project),
        _env={**os.environ},
    )

    assert str(spoken) == ""


def test_the_rendered_guard_names_both_commits_when_they_have_parted(
    tmp_path: Path,
) -> None:
    """The line a session is handed, over a branch built the way an update builds it."""
    source = scaffold.ScaffoldSource(project="upstream")
    upstream = repository(tmp_path / "upstream")
    for root in source.roots:
        directory = upstream / root.upstream
        directory.mkdir(parents=True)
        (directory / "fixture.py").write_text("x = 1\n", encoding="utf-8")
    base = committed(upstream, "the scaffold")

    project = repository(tmp_path / "project")
    (project / "uv.lock").write_text(
        '[[package]]\nname = "lup-agents"\nversion = "0.2.0"\n'
        'source = { git = "https://example.test/lup?branch=dev#'
        f'{"a" * 40}" }}\n',
        encoding="utf-8",
    )
    committed(project, "the project")
    scaffold.adopt(project, upstream, source, "demo", base)

    spoken = str(
        sh.sh(
            str(shipped(tmp_path, source.branch)),
            _in="{}",
            _cwd=str(project),
            _env={**os.environ},
        )
    )

    pushed = json.loads(spoken)["hookSpecificOutput"]
    assert pushed["hookEventName"] == "UserPromptSubmit"
    assert base in pushed["additionalContext"]
    assert "a" * 40 in pushed["additionalContext"]
