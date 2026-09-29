"""The roster's changes registered under each runtime's prompt event, and run.

One rendering, two plugins. What is asserted per runtime is that the event is
registered with the guard, that the entry beside the shipped package cannot
refuse, and — by running the rendered guard over a store the typed writers
produced, from inside a repository, the way the runtime would — that its
stdout is the envelope both vendors document: the pointer on the first
prompt, nothing on a quiet one, and a line for what changed after that.

The package itself is asserted to be there whatever the project declared,
because the compiled dispatcher imports it at its top level: a plugin
carrying the dispatcher and not the package is a permission hook that raises
before it decides anything, which refuses every call in the session.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
import sh

from lup.coordination.bare.changes import envelope, pointer
from lup.coordination.bare.store import member_of, session_actor
from lup.coordination.identity import MEMBER_ENV, mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.harness.generate import NativeHarnessComposition
from lup.harness.models import Artifact
from lup.policy.dispatcher import STORE_PACKAGE
from lup.providers.claude.harness import CLAUDE_EXIT_EVENT, CLAUDE_PROMPT_EVENT
from lup.providers.codex.harness import CODEX_EXIT_EVENT, CODEX_PROMPT_EVENT
from lup.providers.roster_prompt import (
    ARRIVAL_ENTRY,
    ARRIVAL_SCRIPT,
    CHANGES_ENTRY,
    DEPARTURE_ENTRY,
    DEPARTURE_SCRIPT,
    GUARD_SCRIPT,
    departure_hook,
    prompt_hook,
    store_modules,
    wake_hook,
)
from lup_template.harness.catalog import portable_harness
from lup_template.harness.composition import claude_target, codex_target

RUNTIMES = pytest.mark.parametrize(
    ("target", "tree", "event"),
    [
        pytest.param(claude_target, ".claude", CLAUDE_PROMPT_EVENT, id="claude"),
        pytest.param(codex_target, ".codex", CODEX_PROMPT_EVENT, id="codex"),
    ],
)

ENDINGS = pytest.mark.parametrize(
    ("target", "tree", "event"),
    [
        pytest.param(claude_target, ".claude", CLAUDE_EXIT_EVENT, id="claude"),
        pytest.param(codex_target, ".codex", CODEX_EXIT_EVENT, id="codex"),
    ],
)


def rendered(tree: str) -> Path:
    """The plugin root one runtime's tree renders the hook under."""
    return Path(f"{tree}/plugins/lup")


def shipped(
    target: Callable[[Path], NativeHarnessComposition],
) -> dict[Path, Artifact]:
    """Every artifact one runtime's plugin carries, by the path it carries it at."""
    return {
        artifact.path: artifact
        for artifact in target(Path.cwd()).recipe.desired.artifacts
    }


def laid_out(
    artifacts: dict[Path, Artifact], plugin: Path, root: Path, guard: str, entry: str
) -> Path:
    """One guard, its entry and the shipped package, as a plugin lays them out.

    The package travels whole rather than by the modules this hook happens to
    reach: the entry imports one of them and that one imports the fold, so a
    copy of two files would test a layout no plugin has.
    """
    carried = [
        (
            Path("hooks") / "scripts" / guard,
            artifacts[plugin / "hooks" / "scripts" / guard],
        ),
        (
            Path("hooks") / "runtime" / entry,
            artifacts[plugin / "hooks" / "runtime" / entry],
        ),
        *[
            (Path("hooks") / "runtime" / module.path, module)
            for module in store_modules()
        ],
    ]
    for relative, artifact in carried:
        landed = root / relative
        landed.parent.mkdir(parents=True, exist_ok=True)
        landed.write_text(artifact.content)
    return root / "hooks" / "scripts" / guard


@RUNTIMES
def test_the_prompt_event_registers_the_fold_and_refuses_nothing(
    target: Callable[[Path], NativeHarnessComposition], tree: str, event: str
) -> None:
    """Under its own event, with no matcher, and with no `exit 2` beside it."""
    artifacts = shipped(target)
    plugin = rendered(tree)
    hooks = json.loads(artifacts[plugin / "hooks" / "hooks.json"].content)["hooks"]

    # One group per fold registered under the event, and the roster's is the
    # one naming the roster's guard: the carriers' fold answers at the same
    # moment and neither stands in for the other.
    [group] = [
        group
        for group in hooks[event]
        if any(GUARD_SCRIPT in entry["command"] for entry in group["hooks"])
    ]
    [entry] = group["hooks"]

    assert "matcher" not in group
    assert GUARD_SCRIPT in entry["command"]
    assert "exit 2" not in entry["command"]
    assert artifacts[plugin / "hooks" / "scripts" / GUARD_SCRIPT].executable
    assert plugin / "hooks" / "runtime" / CHANGES_ENTRY in artifacts


@ENDINGS
def test_the_ending_event_registers_the_departure_and_refuses_nothing(
    target: Callable[[Path], NativeHarnessComposition], tree: str, event: str
) -> None:
    """Under its own event, for every reason, and with no `exit 2` beside it."""
    artifacts = shipped(target)
    plugin = rendered(tree)
    hooks = json.loads(artifacts[plugin / "hooks" / "hooks.json"].content)["hooks"]

    [group] = hooks[event]
    [entry] = group["hooks"]

    assert "matcher" not in group
    assert DEPARTURE_SCRIPT in entry["command"]
    assert "exit 2" not in entry["command"]
    assert artifacts[plugin / "hooks" / "scripts" / DEPARTURE_SCRIPT].executable
    assert plugin / "hooks" / "runtime" / DEPARTURE_ENTRY in artifacts


def test_a_project_without_a_roster_registers_nothing_and_carries_nothing() -> None:
    """The declaration is the peer policy, so declining it declines this too."""
    undeclared = portable_harness().declared_hooks.model_copy(
        update={"peer_policy": None}
    )

    quiet = prompt_hook(Path("plugin"), "PLUGIN_ROOT", undeclared, "UserPromptSubmit")
    silent = departure_hook(
        Path("plugin"), "PLUGIN_ROOT", undeclared, "SessionEnd", "SubagentStop"
    )

    assert quiet.registered == {}
    assert quiet.artifacts == []
    assert silent.registered == {}
    assert silent.artifacts == []


@ENDINGS
def test_the_rendered_departure_ends_the_row_of_the_session_that_sent_it(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """The guard run as the runtime runs it at an ending, over a real store.

    No launcher-proven id in the environment, so the writer falls back to the
    id the runtime hands the hook — the same fallback the prompt fold takes,
    which is what makes the row that arrived the row that leaves.
    """
    guard = laid_out(
        shipped(target),
        rendered(tree),
        tmp_path / "plugin",
        DEPARTURE_SCRIPT,
        DEPARTURE_ENTRY,
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    sh.git("init", "-q", str(repository))
    peers = RepositoryPeers(repository)
    peers.join("abc123", repository, cli_name="mine")
    other = mint_member_id()
    peers.join(other, repository, cli_name="other")
    ending = {"session_id": "abc123", "cwd": str(repository), "hook_event_name": event}

    ended = str(
        sh.sh(
            str(guard),
            _in=json.dumps(ending),
            _cwd=str(repository),
            _env={**os.environ, MEMBER_ENV: ""},
        )
    )

    assert ended == ""
    standing = {member.actor.id: member.running for member in peers.cohort.live()}
    assert standing["abc123"] is False
    assert standing[other] is True


@RUNTIMES
def test_the_rendered_guard_prints_the_changes_as_additional_context(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """The measurement: the guard run as the runtime runs it, over a real store.

    No launcher-proven id in the environment, so the reader falls back to the
    id the runtime hands the hook — the bare session's own case.
    """
    guard = laid_out(
        shipped(target),
        rendered(tree),
        tmp_path / "plugin",
        GUARD_SCRIPT,
        CHANGES_ENTRY,
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    sh.git("init", "-q", str(repository))
    peers = RepositoryPeers(repository)
    peers.join(mint_member_id(), repository, cli_name="reviewer")
    prompt = {"session_id": "abc123", "cwd": str(repository), "hook_event_name": event}
    environment = {**os.environ, MEMBER_ENV: ""}

    def submitted() -> str:
        return str(
            sh.sh(
                str(guard),
                _in=json.dumps(prompt),
                _cwd=str(repository),
                _env=environment,
            )
        )

    first = submitted()
    quiet = submitted()
    peers.join(mint_member_id(), repository, cli_name="third")
    arrived = submitted()

    assert json.loads(first) == envelope(event, [pointer(1)])
    assert quiet == ""
    assert json.loads(arrived) == envelope(
        event, [f"third arrived — repository — working in {repository}"]
    )


@RUNTIMES
def test_the_shipped_package_travels_whatever_the_project_declared(
    target: Callable[[Path], NativeHarnessComposition], tree: str, event: str
) -> None:
    """The dispatcher imports the package, so carrying it cannot be conditional.

    A plugin holding the compiled dispatcher and not this package is a
    permission hook that raises before it decides anything, which refuses
    every call in the session rather than one of them. There is nothing for a
    declaration to switch off either: what places these files takes no hook
    set, so a project declining the roster still carries the fold its
    dispatcher stands on.
    """
    artifacts = shipped(target)
    plugin = rendered(tree)
    carried = [module.path for module in store_modules()]

    assert Path(STORE_PACKAGE) / "store.py" in carried
    assert all(plugin / "hooks" / "runtime" / path in artifacts for path in carried)


@RUNTIMES
def test_the_entry_reaches_the_package_under_an_isolated_interpreter(
    target: Callable[[Path], NativeHarnessComposition],
    tree: str,
    event: str,
    tmp_path: Path,
) -> None:
    """The entry names its own search path, so no interpreter flag can take it away.

    Run with ``-I -S``, which is every way the environment could have been
    supplying the answer: no site packages, no ``PYTHONPATH``, and no script
    directory prepended. The dispatcher beside it is already measured this
    way, and this hook fails open — so an entry that depended on the
    interpreter's own path would not break loudly, it would stop answering.
    """
    laid_out(
        shipped(target),
        rendered(tree),
        tmp_path / "plugin",
        GUARD_SCRIPT,
        CHANGES_ENTRY,
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    sh.git("init", "-q", str(repository))
    peers = RepositoryPeers(repository)
    peers.join(mint_member_id(), repository, cli_name="reviewer")
    prompt = {"session_id": "abc123", "cwd": str(repository), "hook_event_name": event}

    isolated = str(
        sh.Command("python3")(
            "-I",
            "-S",
            str(tmp_path / "plugin" / "hooks" / "runtime" / CHANGES_ENTRY),
            str(peers.root),
            "",
            event,
            _in=json.dumps(prompt),
            _cwd=str(repository),
        )
    )

    assert json.loads(isolated) == envelope(event, [pointer(1)])


@pytest.mark.parametrize("event", ["SessionStart", CODEX_PROMPT_EVENT])
def test_codex_native_arrival_binds_the_existing_launcher_member(
    tmp_path: Path, event: str
) -> None:
    artifacts = shipped(codex_target)
    plugin = rendered(".codex")
    hooks = json.loads(artifacts[plugin / "hooks" / "hooks.json"].content)["hooks"]
    assert any(
        ARRIVAL_SCRIPT in hook["command"]
        for group in hooks[event]
        for hook in group["hooks"]
    )
    guard = laid_out(
        artifacts, plugin, tmp_path / "plugin", ARRIVAL_SCRIPT, ARRIVAL_ENTRY
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    sh.git("init", "-q", str(repository))
    peers = RepositoryPeers(repository)
    member = mint_member_id()
    peers.join(member, repository, cli_name="root")
    payload = {
        "session_id": "native-thread",
        "cwd": str(repository),
        "hook_event_name": event,
    }
    result = str(
        sh.sh(
            str(guard),
            _in=json.dumps(payload),
            _cwd=str(repository),
            _env={**os.environ, MEMBER_ENV: member},
        )
    )
    assert result == ""
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert found.get("wake", {}).get("handle") == "native-thread"
    assert found.get("wake", {}).get("runtime") == "codex"


def test_codex_arrival_entry_runs_without_site_packages(tmp_path: Path) -> None:
    laid_out(
        shipped(codex_target),
        rendered(".codex"),
        tmp_path / "plugin",
        ARRIVAL_SCRIPT,
        ARRIVAL_ENTRY,
    )
    peers = RepositoryPeers(tmp_path)
    member = mint_member_id()
    peers.join(member, tmp_path, cli_name="root")
    result = str(
        sh.Command("python3")(
            "-I",
            "-S",
            str(tmp_path / "plugin" / "hooks" / "runtime" / ARRIVAL_ENTRY),
            str(peers.root),
            member,
            "codex",
            "/native-home",
            "SessionStart",
            CODEX_PROMPT_EVENT,
            _in=json.dumps(
                {
                    "session_id": "native-thread",
                    "cwd": str(tmp_path),
                    "hook_event_name": "SessionStart",
                }
            ),
        )
    )
    assert result == ""
    found = member_of(peers.root, session_actor(member))
    assert found is not None
    assert found.get("wake", {}).get("handle") == "native-thread"


def test_arrival_guard_carries_only_its_declared_events(tmp_path: Path) -> None:
    plugin = Path("plugin")
    event = "Root 'Ready'"
    hook = wake_hook(
        plugin,
        "PLUGIN_ROOT",
        portable_harness().declared_hooks,
        "codex",
        (event,),
        "NATIVE_HOME",
    )
    guard = laid_out(
        {artifact.path: artifact for artifact in hook.artifacts},
        plugin,
        tmp_path / "plugin",
        ARRIVAL_SCRIPT,
        ARRIVAL_ENTRY,
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    sh.git("init", "-q", str(repository))
    peers = RepositoryPeers(repository)
    member = mint_member_id()
    peers.join(member, repository, cli_name="root")
    environment = {**os.environ, MEMBER_ENV: member, "NATIVE_HOME": "/target home"}
    for actual, expected in [("SessionStart", ""), (event, "native-thread")]:
        result = str(
            sh.sh(
                str(guard),
                _in=json.dumps(
                    {
                        "session_id": "native-thread",
                        "cwd": str(repository),
                        "hook_event_name": actual,
                    }
                ),
                _cwd=str(repository),
                _env=environment,
            )
        )
        assert result == ""
        found = member_of(peers.root, session_actor(member))
        assert found is not None
        assert found.get("wake", {}).get("handle", "") == expected
    assert found.get("wake", {}).get("home") == "/target home"
