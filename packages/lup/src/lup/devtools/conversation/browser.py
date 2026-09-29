"""Persistent browser sessions for authenticated conversation providers."""

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from pydantic import BaseModel

from lup.devtools.conversation.errors import ConversationBrowserError

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — Chromium's own flag
AUTOMATION_FLAG = "--disable-blink-features=AutomationControlled"


class BrowserCookie(BaseModel, frozen=True, extra="ignore"):
    """The fields needed to replay one browser cookie."""

    name: str = ""
    value: str = ""


def require_playwright() -> None:
    """Refuse with the installation path instead of an import traceback."""
    if find_spec("playwright.async_api") is None:
        raise ConversationBrowserError(
            "Conversation retention needs the `lup-agents[conversation]` extra. "
            "Install it, then run `uv run playwright install chromium`."
        )


def browser_executable() -> str | None:
    """A system Chromium executable, falling back to Playwright's build."""
    return next(
        (
            executable
            for name in ("chromium", "chromium-browser", "google-chrome")
            if (executable := shutil.which(name)) is not None
        ),
        None,
    )


@asynccontextmanager
async def browser_context(
    directory: Path, *, headless: bool, close_seconds: float = 15.0
) -> AsyncIterator["BrowserContext"]:
    """Open one persistent Chromium context at an explicit profile directory.

    ``close_seconds`` bounds the wait for Chromium to report itself closed;
    raise it where a slow machine needs longer to shut a heavy profile down.
    """
    require_playwright()
    from playwright.async_api import Error as PlaywrightError, async_playwright

    directory.mkdir(parents=True, exist_ok=True)
    arguments = [AUTOMATION_FLAG]
    executable = browser_executable()
    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(directory),
                headless=headless,
                args=arguments,
                chromium_sandbox=True,
                executable_path=executable,
            )
        except PlaywrightError as error:
            raise ConversationBrowserError(
                "Could not start Chromium. Run `uv run playwright install chromium`."
            ) from error
        try:
            yield context
        finally:
            # A persistent context can leave `close()` awaiting a Chromium that
            # never reports having gone, which strands the caller after its work
            # is already on disk. Bound the wait and let the driver's own
            # teardown end the browser instead.
            try:
                await asyncio.wait_for(context.close(), timeout=close_seconds)
            except PlaywrightError:
                logger.debug("The browser context was already closed", exc_info=True)
            except TimeoutError:
                logger.warning(
                    "Chromium did not close within %ss; stopping its driver instead",
                    close_seconds,
                )


async def cookie_header(context: "BrowserContext", origin: str) -> str:
    """The browser's cookies for one origin as an HTTP Cookie header."""
    cookies = [
        BrowserCookie.model_validate(entry) for entry in await context.cookies(origin)
    ]
    return "; ".join(
        f"{cookie.name}={cookie.value}" for cookie in cookies if cookie.name
    )


async def login(directory: Path, page_url: str, label: str) -> None:
    """Open a real browser and finish when the operator closes its window."""
    async with browser_context(directory, headless=False) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
        typer.echo(
            f"Sign in to {label} in the browser window, "
            "then close the window to continue."
        )
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            logger.debug(
                "The login window closed with its browser context", exc_info=True
            )
