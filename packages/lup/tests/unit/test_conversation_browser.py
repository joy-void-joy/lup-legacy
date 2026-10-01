"""Persistent conversation browser and profile lifecycle."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from playwright import async_api as playwright_api
from typer.testing import CliRunner

from lup.providers.codex.login import CODEX_LOGIN
from lup.devtools.conversation import app as conversation_app
from lup.devtools.conversation import browser
from lup.devtools.conversation import chatgpt
from lup.devtools.conversation import selection
from lup.providers.profile_tree import profile_directory
from lup.providers.user_config import UserConfigFile


@pytest.mark.asyncio
async def test_browser_context_enables_chromium_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = AsyncMock()
    chromium = Mock(
        launch_persistent_context=AsyncMock(return_value=context),
    )
    playwright = Mock(chromium=chromium)

    @asynccontextmanager
    async def started() -> AsyncIterator[Mock]:
        yield playwright

    monkeypatch.setattr(playwright_api, "async_playwright", started)
    monkeypatch.setattr(browser, "browser_executable", lambda: "/usr/bin/chromium")

    async with browser.browser_context(tmp_path, headless=True):
        pass

    chromium.launch_persistent_context.assert_awaited_once_with(
        str(tmp_path),
        headless=True,
        args=[browser.AUTOMATION_FLAG],
        executable_path="/usr/bin/chromium",
        chromium_sandbox=True,
    )


@pytest.mark.asyncio
async def test_a_chromium_that_never_closes_does_not_strand_the_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def never_closing() -> None:
        """A Chromium that accepts the close and never reports having gone."""
        await asyncio.Event().wait()

    context = AsyncMock()
    context.close = never_closing
    chromium = Mock(launch_persistent_context=AsyncMock(return_value=context))
    playwright = Mock(chromium=chromium)

    @asynccontextmanager
    async def started() -> AsyncIterator[Mock]:
        yield playwright

    monkeypatch.setattr(playwright_api, "async_playwright", started)
    monkeypatch.setattr(browser, "browser_executable", lambda: None)

    with caplog.at_level("WARNING", logger=browser.logger.name):
        async with browser.browser_context(tmp_path, headless=True, close_seconds=0.01):
            pass

    assert "did not close" in caplog.text


@pytest.mark.asyncio
async def test_login_finishes_when_the_browser_window_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    page = AsyncMock()
    context = AsyncMock()
    context.pages = [page]

    @asynccontextmanager
    async def opened(_directory: Path, *, headless: bool) -> AsyncIterator[AsyncMock]:
        assert not headless
        yield context

    monkeypatch.setattr(browser, "browser_context", opened)

    await browser.login(
        tmp_path / "chatgpt-web",
        "https://chatgpt.com/c/conversation-1",
        "ChatGPT",
    )

    page.goto.assert_awaited_once_with(
        "https://chatgpt.com/c/conversation-1",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    page.wait_for_event.assert_awaited_once_with("close", timeout=0)
    assert capsys.readouterr().out == (
        "Sign in to ChatGPT in the browser window, then close the window to continue.\n"
    )


def test_a_named_codex_profile_keeps_chatgpt_web_state_beside_its_home(
    tmp_path: Path,
) -> None:
    profiles = profile_directory(CODEX_LOGIN, UserConfigFile(tmp_path / "lup"))
    profile = profiles.add("work", scope="global")

    directory = profiles.state_dir("work", "chatgpt-web")

    assert profile.config_dir == tmp_path / "lup" / "profiles" / "work" / "codex-home"
    assert directory == tmp_path / "lup" / "profiles" / "work" / "chatgpt-web"


def test_the_active_profile_supplies_browser_state_when_none_is_named(
    tmp_path: Path,
) -> None:
    profiles = profile_directory(CODEX_LOGIN, UserConfigFile(tmp_path / "lup"))
    profiles.add("work", scope="global")

    assert profiles.state_dir(None, "chatgpt-web") == (
        tmp_path / "lup" / "profiles" / "work" / "chatgpt-web"
    )


def test_chatgpt_command_reuses_the_active_codex_profile_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles = profile_directory(CODEX_LOGIN, UserConfigFile(tmp_path / "lup"))
    profiles.add("work", scope="global")
    opened: list[Path] = []

    async def retain(
        requests: Sequence[selection.RetentionRequest],
        _root: Path,
        state_directory: Path,
        output: Path,
    ) -> list[selection.RetentionAttempt]:
        opened.append(state_directory)
        destination = output / "chatgpt" / "conversation-1"
        destination.mkdir(parents=True)
        return [
            selection.RetentionAttempt(request=requests[0], destination=destination)
        ]

    monkeypatch.setattr(conversation_app, "project_root", lambda: tmp_path)
    monkeypatch.setattr(conversation_app, "retain_chatgpt", retain)
    monkeypatch.setattr(
        conversation_app,
        "checkpoint_delivery",
        lambda _root, _destinations, *, provider: None,
    )

    result = CliRunner().invoke(
        conversation_app.create_conversation_app(profiles),
        ["chatgpt", "https://chatgpt.com/c/conversation-1"],
    )

    assert result.exit_code == 0
    assert opened == [tmp_path / "lup" / "profiles" / "work" / "chatgpt-web"]


def recording_browser(monkeypatch: pytest.MonkeyPatch, opened: list[Path]) -> None:
    """Record each persistent state a retention opens, launching no browser."""

    @asynccontextmanager
    async def opener(directory: Path, *, headless: bool) -> AsyncIterator[Mock]:
        opened.append(directory)
        yield Mock(request=Mock())

    monkeypatch.setattr(conversation_app, "browser_context", opener)


def one_conversation() -> list[selection.RetentionRequest]:
    """The single request every persistent-state test asks to retain."""
    return [selection.RetentionRequest(url="https://chatgpt.com/c/conversation-1")]


@pytest.mark.asyncio
async def test_download_reads_the_selected_state_without_opening_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / ".lup" / "conversations" / "chatgpt-web"
    selected.mkdir(parents=True)
    opened: list[Path] = []
    recording_browser(monkeypatch, opened)

    async def download(
        _reference: chatgpt.ConversationReference,
        *,
        root: Path,
        api: object,
        output: Path,
        artifact: str = "",
    ) -> Path:
        return output / "chatgpt" / "conversation-1"

    interactive_login = AsyncMock()
    monkeypatch.setattr(chatgpt, "download_chatgpt", download)
    monkeypatch.setattr(conversation_app, "login", interactive_login)
    state = conversation_app.browser_directory(tmp_path, "chatgpt", None, None)

    attempts = await conversation_app.retain_chatgpt(
        one_conversation(), tmp_path, state, tmp_path / "retained"
    )

    assert opened == [selected]
    assert [attempt.destination for attempt in attempts] == [
        tmp_path / "retained" / "chatgpt" / "conversation-1"
    ]
    interactive_login.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_refuses_with_the_explicit_setup_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_browser(monkeypatch, [])

    async def missing(
        _reference: chatgpt.ConversationReference,
        *,
        root: Path,
        api: object,
        output: Path,
        artifact: str = "",
    ) -> Path:
        raise chatgpt.ChatGPTAuthenticationRequired("missing")

    interactive_login = AsyncMock()
    monkeypatch.setattr(chatgpt, "download_chatgpt", missing)
    monkeypatch.setattr(conversation_app, "login", interactive_login)
    state = conversation_app.browser_directory(tmp_path, "chatgpt", None, None)

    attempts = await conversation_app.retain_chatgpt(
        one_conversation(), tmp_path, state, tmp_path / "retained"
    )

    assert attempts[0].destination is None
    assert "uv run lup-devtools conversation setup chatgpt" in attempts[0].error
    interactive_login.assert_not_awaited()


@pytest.mark.parametrize(
    ("provider", "url", "label"),
    [
        ("chatgpt", "https://chatgpt.com/", "ChatGPT"),
        ("claude", "https://claude.ai/", "Claude"),
    ],
)
def test_only_conversation_setup_opens_an_interactive_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    url: str,
    label: str,
) -> None:
    interactive_login = AsyncMock()
    monkeypatch.setattr(conversation_app, "project_root", lambda: tmp_path)
    monkeypatch.setattr(conversation_app, "login", interactive_login)

    result = CliRunner().invoke(
        conversation_app.create_conversation_app(), ["setup", provider]
    )

    assert result.exit_code == 0
    interactive_login.assert_awaited_once_with(
        tmp_path / ".lup" / "conversations" / f"{provider}-web",
        url,
        label,
    )
