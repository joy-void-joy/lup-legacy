"""The launcher asks the credential owner rather than trusting local timestamps."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import sh

import lup.launch.session as launch_session
import lup.providers.codex.session as codex_session
from lup.launch.declaration import LaunchSandbox
from lup.launch.refusal import LaunchRefused
from lup.harness.clipboard import ClipboardBridge, ClipboardTransport
from lup.harness.egress import SessionEgress
from lup.sandbox.models import NetworkMode
from lup.providers.codex.account import CodexAccountState
from lup.providers.codex.app_server import AppServerError, RpcError
from lup.providers.codex.login import CODEX_LOGIN


def account(present: bool, required: bool = True) -> CodexAccountState:
    return CodexAccountState.model_validate(
        {
            "account": {"type": "chatgpt"} if present else None,
            "requiresOpenaiAuth": required,
        }
    )


@pytest.mark.parametrize("state", [account(True), account(False, required=False)])
def test_native_account_readiness_does_not_need_a_local_auth_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: CodexAccountState
) -> None:
    read = AsyncMock(return_value=state)
    confirm = Mock(side_effect=AssertionError("ready accounts need no sign-in"))
    monkeypatch.setattr(codex_session, "read_account", read)

    codex_session.codex_login_preflight(
        tmp_path, {"PATH": "/fixture/bin"}, consent=confirm
    )

    read.assert_awaited_once_with(
        Path("codex"),
        {"PATH": "/fixture/bin", "CODEX_HOME": str(tmp_path)},
        refresh_token=True,
        arguments=[],
    )


def test_login_is_followed_by_native_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = AsyncMock(side_effect=[account(False), account(True)])
    login = Mock()
    monkeypatch.setattr(codex_session, "read_account", read)
    monkeypatch.setattr(sh, "Command", lambda _name: login)

    codex_session.codex_login_preflight(tmp_path, {}, consent=lambda _question: True)

    assert read.await_count == 2
    login.assert_called_once_with("login", _fg=True, _env={"CODEX_HOME": str(tmp_path)})


def test_failed_native_verification_after_login_does_not_open_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        codex_session, "read_account", AsyncMock(return_value=account(False))
    )
    monkeypatch.setattr(sh, "Command", lambda _name: Mock())

    with pytest.raises(LaunchRefused, match="authentication"):
        codex_session.codex_login_preflight(
            tmp_path, {}, consent=lambda _question: True
        )


@pytest.mark.parametrize(
    "failure",
    [
        AppServerError(RpcError(code=-32603, message="secret-token-value")),
        TimeoutError("secret-token-value"),
        RuntimeError("secret-token-value"),
    ],
)
def test_failed_account_check_is_not_reported_as_ready_or_leaked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    monkeypatch.setattr(codex_session, "read_account", AsyncMock(side_effect=failure))

    codex_session.codex_login_preflight(tmp_path, {}, consent=lambda _question: False)

    output = capsys.readouterr().out
    assert "not verified" in output
    assert str(tmp_path) in output
    assert "secret-token-value" not in output


@pytest.mark.parametrize("headless", [False, True])
def test_contained_authentication_selects_the_requested_login_flow(
    monkeypatch: pytest.MonkeyPatch,
    headless: bool,
) -> None:
    read = AsyncMock(side_effect=[account(False), account(True)])
    login = Mock()
    command = Mock(return_value=login)
    monkeypatch.setattr(codex_session, "read_account", read)
    monkeypatch.setattr(sh, "Command", command)

    codex_session.codex_login_preflight(
        Path("/cfg"),
        {},
        ["podman", "run", "-i", "image", "codex"],
        headless=headless,
        consent=lambda _question: True,
    )

    read.assert_awaited_with(
        Path("podman"),
        {"CODEX_HOME": "/cfg"},
        refresh_token=True,
        arguments=["run", "-i", "image", "codex"],
    )
    command.assert_called_once_with("podman")
    login.assert_called_once_with(
        "run",
        "-i",
        "image",
        "codex",
        "login",
        *(["--device-auth"] if headless else []),
        _fg=True,
        _env={"CODEX_HOME": "/cfg"},
    )


@pytest.mark.parametrize("sandbox", list(LaunchSandbox))
@pytest.mark.parametrize("transport", ["commands", "x11"])
@pytest.mark.parametrize(
    ("network", "headless"),
    [("host", False), ("filtered", True), ("bridge", True), ("none", True)],
)
def test_session_authentication_uses_the_same_execution_boundary(
    sandbox: LaunchSandbox,
    transport: ClipboardTransport,
    network: NetworkMode,
    headless: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = Mock()
    composition.recipe.source.image.config_home = "/cfg"
    composition.recipe.source.image.forge.sourced.return_value = ""
    composition.recipe.source.image.clipboard = ClipboardBridge()
    composition.recipe.source.image.egress = SessionEgress(mode=network)
    composition.clipboard_transport = transport
    plugin = Mock(hooks=None)
    authenticate = Mock()
    monkeypatch.setattr(launch_session, "settle_boundary", Mock())
    monkeypatch.setattr(launch_session, "say_opening", Mock())
    monkeypatch.setattr(launch_session, "verify_inside", Mock(return_value=[]))
    monkeypatch.setattr(
        launch_session,
        "contained_argv",
        Mock(return_value=["podman", "run", "-it", "image"]),
    )

    argv = launch_session.session_argv(
        "codex",
        ["resume", "session"],
        tmp_path,
        composition.recipe.source.image,
        composition.recipe.source.requirements,
        plugin.hooks,
        tmp_path,
        CODEX_LOGIN,
        sandbox,
        {},
        authenticate=authenticate,
        clipboard=composition.clipboard_transport,
    )

    if sandbox.contained():
        authenticate.assert_called_once_with(
            ["podman", "run", "-i", "image", "codex"], Path("/cfg"), headless
        )
        assert argv == [
            "podman",
            "run",
            "-it",
            "image",
            *ClipboardBridge().wrap(["codex", "resume", "session"], transport),
        ]
    else:
        authenticate.assert_called_once_with(["codex"], tmp_path, False)
        assert argv == ["codex", "resume", "session"]
