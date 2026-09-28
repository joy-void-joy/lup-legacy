"""Portable hooks preserve their declared coverage and approval posture."""

from pathlib import Path

import pytest

from lup.policy.hooks import LupHookInput, LupHookMatcher, LupHookOutput, LupHooksConfig
from lup.providers.codex.hooks import APPROVAL_METHODS
from lup.providers.codex import Codex
from lup.providers.codex.runtime import CodexSessionOpener
from lup.providers.codex.selection import codex_config
from lup.providers.selection import SessionRequest
from lup.sessions.errors import UnsupportedCapability
from lup.types import EnvVars
from typing import Never


@pytest.mark.parametrize("matcher", [None, "*", "Bash", "item/.*"])
async def test_direct_opener_rejects_unenforceable_hooks_before_native_setup(
    tmp_path: Path,
    matcher: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def native_setup(_declared: int | None, _environment: EnvVars) -> Never:
        pytest.fail("native setup must not run for unsupported hook coverage")

    monkeypatch.setattr(
        "lup.providers.codex.runtime.allowance_environment", native_setup
    )
    hooks = LupHooksConfig(pre_tool_use=[LupHookMatcher(matcher=matcher, hook=observe)])
    config = Codex(cwd=tmp_path, hooks=hooks, approval_policy="never")
    with pytest.raises(UnsupportedCapability, match="explicit native approval scope"):
        async with CodexSessionOpener(config).open_session():
            pytest.fail("unsupported session must not open")


@pytest.mark.parametrize("approval_policy", [None, "never"])
async def test_direct_pre_hooks_require_an_explicit_asking_policy(
    tmp_path: Path,
    approval_policy: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def native_setup(_declared: int | None, _environment: EnvVars) -> Never:
        pytest.fail("native setup must not run when callbacks cannot be reached")

    monkeypatch.setattr(
        "lup.providers.codex.runtime.allowance_environment", native_setup
    )
    hooks = LupHooksConfig(
        pre_tool_use=[LupHookMatcher(matcher=APPROVAL_METHODS[0], hook=observe)]
    )
    config = Codex.model_validate(
        {"cwd": tmp_path, "hooks": hooks, "approval_policy": approval_policy}
    )
    with pytest.raises(UnsupportedCapability, match="explicit asking"):
        async with CodexSessionOpener(config).open_session():
            pytest.fail("unreachable callbacks must not open")


@pytest.mark.parametrize("approval", [False, True])
async def test_supported_direct_hooks_reach_setup_without_changing_approvals(
    tmp_path: Path,
    approval: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def native_setup(_declared: int | None, _environment: EnvVars) -> Never:
        raise RuntimeError("reached native setup after hook validation")

    monkeypatch.setattr(
        "lup.providers.codex.runtime.allowance_environment", native_setup
    )
    observer = LupHookMatcher(hook=observe)
    hooks = LupHooksConfig(post_tool_use=[observer], stop=[observer])
    hooks.pre_tool_use = (
        [LupHookMatcher(matcher=APPROVAL_METHODS[0], hook=observe)]
        if approval
        else [LupHookMatcher(hook=observe, tag="mailbox")]
    )
    config = Codex(
        cwd=tmp_path, hooks=hooks, approval_policy="on-request" if approval else "never"
    )
    with pytest.raises(
        RuntimeError, match="reached native setup after hook validation"
    ):
        async with CodexSessionOpener(config).open_session():
            pytest.fail("the native setup seam is replaced")
    assert config.approval_policy == ("on-request" if approval else "never")


async def observe(_event: LupHookInput) -> LupHookOutput:
    return LupHookOutput()


def test_lifecycle_observers_do_not_request_native_approvals(tmp_path: Path) -> None:
    observer = LupHookMatcher(hook=observe)
    hooks = LupHooksConfig(post_tool_use=[observer], stop=[observer])
    config = codex_config(SessionRequest(cwd=tmp_path, hooks=hooks))
    assert config.hooks is hooks
    assert config.approval_policy == "never"


@pytest.mark.parametrize("matcher", [None, "", "*"])
def test_mailbox_delivery_does_not_authorize_tools(
    tmp_path: Path, matcher: str | None
) -> None:
    hooks = LupHooksConfig(
        pre_tool_use=[LupHookMatcher(matcher=matcher, hook=observe, tag="mailbox")]
    )
    config = codex_config(SessionRequest(cwd=tmp_path, hooks=hooks))
    assert config.hooks is hooks
    assert config.approval_policy == "never"


@pytest.mark.parametrize("matcher", [*APPROVAL_METHODS, "|".join(APPROVAL_METHODS)])
def test_exact_native_scope_enables_approval_callbacks(
    tmp_path: Path, matcher: str
) -> None:
    hooks = LupHooksConfig(pre_tool_use=[LupHookMatcher(matcher=matcher, hook=observe)])
    config = codex_config(SessionRequest(cwd=tmp_path, hooks=hooks))
    assert config.hooks is hooks
    assert config.approval_policy == "on-request"


@pytest.mark.parametrize(
    "matcher", [None, "*", "Bash", "item/.*", f"prefix{APPROVAL_METHODS[0]}"]
)
def test_universal_or_ambiguous_pre_tool_coverage_is_rejected(
    tmp_path: Path, matcher: str | None
) -> None:
    hooks = LupHooksConfig(pre_tool_use=[LupHookMatcher(matcher=matcher, hook=observe)])
    with pytest.raises(UnsupportedCapability, match="explicit native approval scope"):
        codex_config(SessionRequest(cwd=tmp_path, hooks=hooks))
