"""Normal resolver entry refuses integration drift with a failing exit status."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from typer.testing import CliRunner

import lup.devtools.harness.resolve as entry
import lup.providers.claude.config_home as claude_home
import lup.providers.codex.home as codex_home
import lup.providers.codex.install as codex_install
from lup.harness.contracts import SkillInvocationRenderer
from lup.harness.models import ResolveSpec, SkillInvocation
from lup.execution.process import LocalProcessLauncher
from lup.resolver.core import ResolverCore, resolver_config_digest
from lup.resolver.models import (
    AnswerBatch,
    IntegrationRecord,
    ResolvePhase,
    ResolverConfig,
    ResolveState,
    ReviewerContext,
    SourceSnapshot,
    VerificationCommand,
    WorkerContext,
    WritableRootLease,
)
from lup.resolver.run import ResolverInvariantError
from lup.sessions.surface import Agent
from lup_template.devtools.main import app
from tests.unit.repos import commit_file, git_in, initialized_repo


@pytest.mark.parametrize("adapter", ["claude", "codex"])
def test_normal_resolver_cli_refuses_integration_head_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    workspace = tmp_path / "source"
    hooks = tmp_path / "hooks"
    git = initialized_repo(workspace, hooks)
    commit_file(git, workspace, "README.md", "base\n", "base")
    base = str(git("rev-parse", "HEAD")).strip()
    lease = WritableRootLease(
        concern_id="integration", root=tmp_path / "integration", branch="review"
    )
    git("worktree", "add", "-b", lease.branch, str(lease.root), base)
    integration_git = git_in(lease.root, hooks)
    integration_git("commit", "--allow-empty", "-m", "unrecorded integration head")
    drifted = str(integration_git("rev-parse", "HEAD")).strip()
    assert drifted != base
    config = ResolverConfig(
        state_root=workspace / ".lup" / "resolve",
        workspace=workspace,
        worktree_root=tmp_path,
        run_id="recover",
        integration_branch=lease.branch,
        verification_commands=[
            VerificationCommand(name="diff", arguments=["git", "diff", "--check"])
        ],
    )
    spec = ResolveSpec(
        id="resolve",
        worker_identity="resolver-worker",
        worker_skill=SkillInvocation(plugin="lup", skill="worker"),
        review_skill=SkillInvocation(plugin="lup", skill="review"),
        merge_skill=SkillInvocation(plugin="lup", skill="merge"),
    )

    def unused_actor(_context: WorkerContext | ReviewerContext) -> Agent:
        raise AssertionError(
            "integration drift must be refused before opening an actor"
        )

    core = ResolverCore(
        config,
        spec,
        unused_actor,
        unused_actor,
        create_autospec(SkillInvocationRenderer, instance=True),
        LocalProcessLauncher(),
    )
    core.repository.save(
        ResolveState(
            config_digest=resolver_config_digest(config),
            config=config,
            run_id=config.run_id,
            phase=ResolvePhase.FAILED,
            resume_from=ResolvePhase.VERIFICATION,
            source=SourceSnapshot(branch="main", commit=base),
            spec=spec,
            concerns=[],
            progress=[],
            answers=AnswerBatch(run_id=config.run_id, answers=[]),
            leases=[lease],
            integration=IntegrationRecord(
                branch=lease.branch, worktree=lease.root, commit=base, concerns=[]
            ),
        )
    )
    monkeypatch.setattr(entry, "project_root", lambda: workspace)
    monkeypatch.setattr(entry, "ResolverCore", lambda *_, **__: core)
    monkeypatch.setattr(entry, "check_remote_auth", lambda: True)
    monkeypatch.setattr(entry, "report_a_blocked_registration", lambda _: None)
    monkeypatch.setattr(entry, "engine_absence", lambda: None)
    monkeypatch.setattr(
        claude_home,
        "selected_config_home",
        lambda _: SimpleNamespace(
            configuration_fault=lambda: None, shell_fault=lambda: None
        ),
    )
    monkeypatch.setattr(
        codex_home,
        "select_codex_home",
        lambda *_, **__: SimpleNamespace(path=tmp_path / "codex-home", isolated=True),
    )
    monkeypatch.setattr(codex_install, "install_codex_plugin", lambda *_, **__: None)

    result = CliRunner().invoke(
        app, ["resolve", "--adapter", adapter, "--run-id", config.run_id]
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, ResolverInvariantError)
    assert "persisted commit changed for integration" in str(result.exception)
    assert "recover-integration" in str(result.exception)
    assert str(integration_git("rev-parse", "HEAD")).strip() == drifted
    recorded = core.repository.load().integration
    assert recorded is not None
    assert recorded.commit == base
