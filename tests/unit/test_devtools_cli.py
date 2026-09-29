# lup: ignore[string-replace, tuple-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""Smoke and behavior tests for the lup-devtools CLI.

The smoke test walks the root typer app's full command tree (sub-apps and
nested groups) and invokes ``--help`` on every command. This catches
import-time crashes (e.g. a module-level ``sh.Command`` for a missing
binary) and option wiring errors, entirely offline.

The pr tests pin output behavior with a stubbed gh: ``pr merge`` must
print its MergeResult even when the tree-dir lookup raises ``typer.Exit``.
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
import sh
import typer
from typer.testing import CliRunner

from lup.devtools.dev import policy_explain, pr
import lup.devtools.harness.app as harness_app
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.home import CodexWorktreeHomeStore
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.login import ProviderLogin
from lup.workspace.paths import project_root
from lup_template.harness.catalog import declared_hook_set
from lup_template.devtools.main import app
from lup.devtools.sync import load_json

# Typer renders usage errors through Rich, which styles option tokens whenever
# it believes it is writing to a terminal. That splits a flag name from the
# prose beside it with escape codes, so output assertions only hold when the
# console is plain: FORCE_COLOR is cleared and a dumb terminal is declared.
PLAIN_CONSOLE = {"FORCE_COLOR": None, "NO_COLOR": "1", "TERM": "dumb"}

runner = CliRunner(env=PLAIN_CONSOLE)


def iter_command_paths(
    current: typer.Typer, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, ...]]:
    """Yield the CLI path of every group and command, depth-first."""
    yield prefix
    for command in current.registered_commands:
        if command.name:
            name = command.name
        elif command.callback:
            name = command.callback.__name__.lower().replace("_", "-")
        else:
            continue
        yield (*prefix, name)
    for group in current.registered_groups:
        sub_app = group.typer_instance
        if sub_app is None or not isinstance(group.name, str):
            continue
        yield from iter_command_paths(sub_app, (*prefix, group.name))


COMMAND_PATHS = sorted(iter_command_paths(app))


def test_command_tree_is_walked() -> None:
    flattened = {" ".join(path) for path in COMMAND_PATHS}
    assert "trace list" in flattened
    assert "git pr merge" in flattened
    assert "git worktree create" in flattened


@pytest.mark.parametrize("launch_only", [False, True])
@pytest.mark.parametrize(
    "target, login", [("claude", CLAUDE_LOGIN), ("codex", CODEX_LOGIN)]
)
def test_container_requirements_respect_launch_only(
    monkeypatch: pytest.MonkeyPatch,
    launch_only: bool,
    target: str,
    login: ProviderLogin,
    tmp_path: Path,
) -> None:
    checks = Mock(return_value=[])
    monkeypatch.setattr(harness_app, "report_inside_requirements", checks)
    monkeypatch.setenv(login.config_home_env, str(tmp_path))
    arguments = ["harness", "requirements", target, "--inside"]

    if launch_only:
        arguments.append("--launch-only")
    result = runner.invoke(app, arguments)

    assert result.exit_code == 0, result.output
    checks.assert_called_once()
    assert checks.call_args.kwargs["setting_up"] == (not launch_only)
    assert checks.call_args.args[3] == tmp_path
    assert checks.call_args.args[4] == login

    assert "No container requirements selected." in result.output


def test_all_container_requirements_select_each_runtimes_default_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = Mock(return_value=[])
    monkeypatch.setattr(harness_app, "report_inside_requirements", checks)
    monkeypatch.delenv(CLAUDE_LOGIN.config_home_env, raising=False)
    monkeypatch.delenv(CODEX_LOGIN.config_home_env, raising=False)

    result = runner.invoke(
        app, ["harness", "requirements", "all", "--inside", "--launch-only"]
    )

    assert result.exit_code == 0, result.output
    assert [(call.args[3], call.args[4]) for call in checks.call_args_list] == [
        (Path.home() / ".claude", CLAUDE_LOGIN),
        (CodexWorktreeHomeStore().home_for(project_root()), CODEX_LOGIN),
    ]


@pytest.fixture(scope="module")
def cli_main() -> Callable[..., object]:
    """The whole command tree as click runs it, built once for the help sweep.

    `CliRunner.invoke` builds the tree from the typer app again on every call,
    a fifth of a second each over every path below, and building it is not
    what the sweep asks about: whether each path's help renders is.
    """
    return typer.main.get_command(app).main


@pytest.mark.parametrize(
    "path", COMMAND_PATHS, ids=[" ".join(p) or "(root)" for p in COMMAND_PATHS]
)
def test_help_succeeds_for_every_command(
    path: tuple[str, ...],
    cli_main: Callable[..., object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        cli_main([*path, "--help"], prog_name="lup-devtools")
    assert exited.value.code == 0, capsys.readouterr().out


READONLY_COMMANDS: list[list[str]] = [
    ["version"],
    ["version", "changelog"],
    ["trace", "list"],
    ["feedback", "status"],
    ["setup", "status"],
    ["setup", "profile", "list"],
    ["sync", "status"],
]


@pytest.mark.parametrize("args", READONLY_COMMANDS, ids=lambda args: " ".join(args))
def test_readonly_command_exits_cleanly(args: list[str]) -> None:
    """--help only proves wiring; a runtime crash in a callback (the version
    sub-app once crashed on every real invocation) needs a real run."""
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output


class FakeGh:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.reads: list[tuple[str, ...]] = []
        self.cleaned: list[str] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        return ""

    def out(self, *args: str) -> str:
        """Reads are kept apart from actions, so ``calls`` stays the merge's own."""
        self.reads.append(args)
        return '{"headRefName": "feature"}'


@pytest.fixture
def merge_stubs(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(pr, "gh", fake)
    monkeypatch.setattr(pr, "get_integration_branch", lambda: "dev")
    monkeypatch.setattr(pr, "parse_worktrees", dict)
    # Stubbed rather than let through: the real one deletes whatever branch the
    # stubbed head ref names, and these tests run inside a live checkout.
    monkeypatch.setattr(pr, "cleanup_merged_branch", fake.cleaned.append)
    return fake


def test_merge_prints_json_result_when_no_worktree_holds_the_integration_branch(
    merge_stubs: FakeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pr.merge(42, dry_run=False, as_json=True)

    out = capsys.readouterr().out
    assert '"pr_number": 42' in out
    assert '"merged": true' in out
    assert '"pulled": false' in out
    assert merge_stubs.calls[0][:2] == ("pr", "merge")


def test_merge_prints_text_result_when_no_worktree_holds_the_integration_branch(
    merge_stubs: FakeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pr.merge(7, dry_run=False, as_json=False)

    out = capsys.readouterr().out
    assert "merged: True" in out
    assert "integration_branch: dev" in out


def test_merge_defaults_to_a_merge_commit(merge_stubs: FakeGh) -> None:
    """The branch's own commits stay reachable unless a caller says otherwise."""
    pr.merge(42, dry_run=False)

    assert "--merge" in merge_stubs.calls[0]
    assert "--squash" not in merge_stubs.calls[0]


def test_merge_takes_the_method_it_is_given(merge_stubs: FakeGh) -> None:
    pr.merge(42, dry_run=False, method=pr.MergeMethod.squash)

    assert "--squash" in merge_stubs.calls[0]
    assert "--merge" not in merge_stubs.calls[0]


def test_merge_hands_further_flags_to_gh_untouched(merge_stubs: FakeGh) -> None:
    """What this signature does not name still has a way through."""
    pr.merge(42, dry_run=False, gh_args=("--admin",))

    assert "--admin" in merge_stubs.calls[0]


def test_merge_clears_the_branch_itself_rather_than_asking_gh_to(
    merge_stubs: FakeGh,
) -> None:
    """``--delete-branch`` runs a plain ``git branch -d``, blind to worktrees.

    Doing it here instead reaches the deletion path that removes the checkout
    first and archives the branch's traces, so a merge in a tree of worktrees
    finishes rather than leaving both behind.
    """
    pr.merge(42, dry_run=False)

    assert "--delete-branch" not in merge_stubs.calls[0]
    assert merge_stubs.cleaned == ["feature"]


class GhExitOne(sh.ErrorReturnCode):
    """A concrete non-zero exit; the sh base leaves ``exit_code`` to subclasses."""

    exit_code = 1


class FailingGh:
    """gh exiting non-zero, over a PR left in *state*."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        raise GhExitOne(
            "gh pr merge", b"", b"failed to delete local branch: used by worktree"
        )

    def out(self, *args: str) -> str:
        self.calls.append(args)
        return f'{{"state": "{self.state}"}}'


def test_a_cleanup_that_failed_is_not_a_merge_that_failed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-zero gh over a PR GitHub calls merged is a leftover, not a failure.

    Reading the state back is what separates them, and the difference is the
    whole of what the caller does next: a merge that did not happen is retried,
    where a landed PR reported as unlanded gets merged again, or hand-landed
    work that is already in.
    """
    monkeypatch.setattr(pr, "gh", FailingGh("MERGED"))
    monkeypatch.setattr(pr, "get_integration_branch", lambda: "dev")
    monkeypatch.setattr(pr, "parse_worktrees", dict)

    pr.merge(42, dry_run=False)

    assert "cleanup did not finish" in capsys.readouterr().err


def test_a_merge_that_did_not_happen_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr, "gh", FailingGh("OPEN"))
    monkeypatch.setattr(pr, "get_integration_branch", lambda: "dev")
    monkeypatch.setattr(pr, "parse_worktrees", dict)

    with pytest.raises(typer.Exit):
        pr.merge(42, dry_run=False)


def test_annotated_registry_raises_a_typed_recovery_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sync.json"
    assert load_json(path) == {"projects": []}

    path.write_text('{"projects": []}\n# a trailing annotation\n', encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="not valid JSON"):
        load_json(path)


def test_ending_a_run_needs_no_adapter_but_driving_one_still_does() -> None:
    """An abort takes no turn, so the flag that picks a runtime is not its own.

    Ending a run reads recorded state and frees worktrees; nothing renders a
    skill invocation, so demanding the adapter refused the one operation a
    run in trouble most needs.
    """
    ended = runner.invoke(
        app, ["resolve", "--abort", "reason", "--run-id", "absent-run"]
    )
    assert "--adapter is required" not in ended.output
    assert "no resolver run 'absent-run' to abort" in ended.output

    driven = runner.invoke(app, ["resolve", "--run-id", "absent-run"])
    assert "--adapter is required" in driven.output


def effect_of(arguments: list[str], environment: dict[str, str | None]) -> str:
    """What `dev policy` reports for one invocation under one environment.

    The exit code carries the verdict rather than the run's health — nothing
    allowed exits non-zero so a caller can gate on it — so the effect is read
    from the payload and the status says nothing here.
    """
    result = runner.invoke(
        app, ["dev", "policy", "--json", *arguments], env=environment
    )
    assert result.stdout, result.output
    readings = json.loads(result.stdout)[0]["readings"]
    assert len(readings) == 1, f"expected one placement, got {readings}"
    return str(readings[0]["effect"])


UNLAUNCHED: dict[str, str | None] = {
    "LUP_BOUNDARY_NONCE": None,
    "LUP_BOUNDARY_ROOT": None,
    "LUP_SANDBOX_ACTIVE": None,
}
"""A process no launch measured: no ledger named, and no sandbox armed."""


@pytest.mark.parametrize(
    ("sandbox", "effect"), [(None, "ask"), ("1", "allow")], ids=["host", "inner"]
)
def test_dev_policy_answers_for_this_session_by_default(
    sandbox: str | None, effect: str
) -> None:
    """The reader is about to spend a turn in this session, so its answer leads.

    Read as its dispatcher reads it: the runtime's sandbox from the launcher's
    variable, and the rest from the ledger the launch named -- none here.
    """
    result = runner.invoke(
        app,
        ["dev", "policy", "--json", "frobnicate"],
        env={**UNLAUNCHED, "LUP_SANDBOX_ACTIVE": sandbox},
    )
    readings = json.loads(result.stdout)[0]["readings"]
    assert [(reading["placement"], reading["effect"]) for reading in readings] == [
        ("session", effect)
    ]


def test_the_unbounded_reading_ignores_the_container_around_this_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row that answers for no boundary, asked from behind one.

    Not hypothetical: `dev policy` is read inside a contained session, which is
    where the guidance sends an agent before it spends a turn, and a reading
    that took containment from the ledger the launch wrote reported the
    bounded answer under the heading of the unbounded one. The placement is
    what decides both walls, so a launch measured as a container changes
    none of the three readings.
    """
    ledger = tmp_path / ".lup" / "preflight" / "launch.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"contained": ["yes"], "delivered": ["inside_placement"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.delenv("LUP_BOUNDARY_ROOT", raising=False)

    verdict = policy_explain.verdict_for(
        "frobnicate",
        "shell",
        autonomous=False,
        cwd=tmp_path,
        hooks=declared_hook_set(),
    )

    assert [reading.effect for reading in verdict.readings] == ["ask", "allow", "allow"]


@pytest.mark.parametrize(
    ("measured", "effect"),
    [
        ({"contained": ["yes"], "delivered": ["inside_placement"]}, "allow"),
        ({"contained": ["yes"]}, "ask"),
        ({"unjudged_ambient": ["defer"]}, "defer"),
        ({}, "ask"),
    ],
    ids=["contained", "container-placing-nothing", "deferring", "measured-nothing"],
)
def test_the_session_reading_takes_its_posture_from_the_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    measured: dict[str, list[str]],
    effect: str,
    launch_record_held: None,
) -> None:
    """What the launch measured is what the dispatcher answers by, and so this.

    A container settles unjudged work only where it measured work placed
    inside, and an uncontained profile that hands such work to the runtime
    says so in the ledger rather than in any declaration read here.
    """
    ledger = tmp_path / ".lup" / "preflight" / "launch.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps(measured), encoding="utf-8")
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.delenv("LUP_BOUNDARY_ROOT", raising=False)
    monkeypatch.delenv("LUP_SANDBOX_ACTIVE", raising=False)

    verdict = policy_explain.verdict_for(
        "frobnicate",
        "shell",
        autonomous=False,
        cwd=tmp_path,
        hooks=declared_hook_set(),
        placements=[policy_explain.session_placement(tmp_path)],
    )

    assert [(reading.placement, reading.effect) for reading in verdict.readings] == [
        ("session", effect)
    ]


def test_the_session_reading_takes_the_host_executor_from_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The channel carrying outside work is measured, not assumed either way."""
    ledger = tmp_path / ".lup" / "preflight" / "launch.json"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.delenv("LUP_BOUNDARY_ROOT", raising=False)

    ledger.write_text(json.dumps({"delivered": ["host_executor"]}), encoding="utf-8")
    assert policy_explain.session_placement(tmp_path).host_executor
    ledger.write_text(json.dumps({"blocked": ["host_executor"]}), encoding="utf-8")
    assert not policy_explain.session_placement(tmp_path).host_executor


@pytest.mark.parametrize(
    ("placement", "effect"),
    [("none", "ask"), ("inner", "allow"), ("outer", "allow")],
)
def test_each_placement_stays_askable_for_explicitly(
    placement: str, effect: str
) -> None:
    """The flag narrows to one placement, and the environment still cannot.

    Kept because a caller scripting against this wants one row rather than
    three, whatever the session running it measured about itself.
    """
    assert (
        effect_of(["--placement", placement, "frobnicate"], {"LUP_SANDBOX_ACTIVE": "1"})
        == effect
    )


def test_a_placement_nobody_launches_is_refused_with_the_ones_that_exist() -> None:
    """A misspelled placement names the three, rather than answering for none."""
    result = runner.invoke(
        app, ["dev", "policy", "--placement", "sandboxed", "frobnicate"]
    )

    assert result.exit_code != 0
    assert "none, inner, outer" in result.output
