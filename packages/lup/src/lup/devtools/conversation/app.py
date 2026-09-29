"""Provider commands for retaining authenticated AI conversations."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import typer
from pydantic import BaseModel

from lup.devtools.conversation.browser import (
    browser_context,
    cookie_header,
    login,
    require_playwright,
)
from lup.devtools.conversation.checkpoint import checkpoint_delivery
from lup.devtools.conversation.errors import ConversationDownloadError
from lup.devtools.conversation.selection import RetentionAttempt, RetentionRequest
from lup.devtools.harness.composition import claude_profile_directory
from lup.providers.profiles import ProfileDirectory
from lup.workspace.paths import project_root

logger = logging.getLogger(__name__)


class BrowserDirectories(BaseModel, frozen=True):
    """The selected browser state followed by compatible persisted states."""

    primary: Path
    fallbacks: tuple[Path, ...] = ()

    def candidates(self) -> tuple[Path, ...]:
        """Every state a headless download may try, in preference order."""
        return (self.primary, *self.fallbacks)


def browser_directory(
    root: Path,
    provider: str,
    profiles: ProfileDirectory | None,
    profile: str | None,
) -> Path:
    """Resolve explicit, active, then unprofiled browser state."""
    if profiles is None and profile is not None:
        raise typer.BadParameter(
            "this project declares no named profiles", param_hint="--profile"
        )
    if profiles is not None:
        try:
            selected = profiles.state_dir(profile, f"{provider}-web")
        except KeyError as error:
            raise typer.BadParameter(str(error), param_hint="--profile") from error
        if selected is not None:
            return selected
    return root / ".lup" / "conversations" / f"{provider}-web"


def browser_directories(
    root: Path,
    provider: str,
    profiles: ProfileDirectory | None,
    profile: str | None,
) -> BrowserDirectories:
    """Resolve selected state plus persisted unprofiled compatibility state."""
    primary = browser_directory(root, provider, profiles, profile)
    unprofiled = root / ".lup" / "conversations" / f"{provider}-web"
    legacy = root / ".lup" / "conversations" / f"{provider}-browser"
    fallbacks = (legacy,) if primary == unprofiled and legacy.exists() else ()
    return BrowserDirectories(primary=primary, fallbacks=fallbacks)


type StateRun = Callable[
    [Path, Sequence[RetentionAttempt]], Awaitable[tuple[RetentionAttempt, ...]]
]


def settled(
    pending: RetentionAttempt, destination: Path | None = None, error: str = ""
) -> RetentionAttempt:
    """One pending request carried to what its attempt produced."""
    return RetentionAttempt(
        position=pending.position,
        request=pending.request,
        destination=destination,
        error=error,
    )


async def retained_through(
    directories: BrowserDirectories,
    requests: Sequence[RetentionRequest],
    run: StateRun,
    expired: str,
) -> list[RetentionAttempt]:
    """Retain every request, trying each browser state a login may live in.

    Only an unauthenticated request moves on to the next stored state, so one
    expired login costs the batch nothing that a working state can serve, and
    a refusal a fresh login would not fix is reported where it happened.
    """
    require_playwright()
    pending = tuple(
        RetentionAttempt(position=position, request=request)
        for position, request in enumerate(requests)
    )
    attempts: tuple[RetentionAttempt, ...] = ()
    for directory in directories.candidates():
        if not pending:
            break
        attempted = await run(directory, pending)
        attempts += tuple(item for item in attempted if not item.unauthenticated)
        pending = tuple(item for item in attempted if item.unauthenticated)
    attempts += tuple(settled(item, error=expired) for item in pending)
    return sorted(attempts, key=lambda item: item.position)


async def retain_chatgpt(
    requests: Sequence[RetentionRequest],
    root: Path,
    directories: BrowserDirectories,
    output: Path,
) -> list[RetentionAttempt]:
    """Retain every requested ChatGPT conversation through one browser each."""
    from lup.devtools.conversation.chatgpt import (
        ChatGPTAuthenticationRequired,
        ConversationReference,
        download_chatgpt,
    )

    async def run(
        directory: Path, pending: Sequence[RetentionAttempt]
    ) -> tuple[RetentionAttempt, ...]:
        """Retain every still-pending request through one persistent state."""
        attempted: tuple[RetentionAttempt, ...] = ()
        async with browser_context(directory, headless=True) as context:
            for item in pending:
                try:
                    destination = await download_chatgpt(
                        ConversationReference(value=item.request.url),
                        root=root,
                        api=context.request,
                        output=output,
                        artifact=item.request.artifact,
                    )
                except ChatGPTAuthenticationRequired as error:
                    attempted += (
                        RetentionAttempt(
                            position=item.position,
                            request=item.request,
                            error=str(error),
                            unauthenticated=True,
                        ),
                    )
                except ConversationDownloadError as error:
                    # The message reaches the operator through the attempt, so
                    # a traceback per refused URL would only bury the batch's
                    # own report under a stack the message already summarises.
                    logger.debug(
                        "Could not retain %s",
                        item.request.describe(),
                        exc_info=True,
                    )
                    attempted += (settled(item, error=str(error)),)
                else:
                    attempted += (settled(item, destination=destination),)
        return attempted

    return await retained_through(
        directories,
        requests,
        run,
        "The ChatGPT browser login is missing or expired. Run "
        "`uv run lup-devtools conversation setup chatgpt`, then retry.",
    )


async def retain_claude(
    requests: Sequence[RetentionRequest],
    root: Path,
    directories: BrowserDirectories,
    output: Path,
) -> list[RetentionAttempt]:
    """Retain every requested Claude conversation through one cookie each."""
    from lup.devtools.conversation.claude import (
        CLAUDE_ORIGIN,
        ClaudeAuthenticationRequired,
        ConversationReference,
        download_claude,
    )

    async def run(
        directory: Path, pending: Sequence[RetentionAttempt]
    ) -> tuple[RetentionAttempt, ...]:
        """Retain every still-pending request through one persistent state."""
        async with browser_context(directory, headless=True) as context:
            cookie = await cookie_header(context, CLAUDE_ORIGIN)
        attempted: tuple[RetentionAttempt, ...] = ()
        for item in pending:
            try:
                destination = await download_claude(
                    ConversationReference(value=item.request.url),
                    root=root,
                    cookie=cookie,
                    output=output,
                    artifact=item.request.artifact,
                )
            except ClaudeAuthenticationRequired as error:
                attempted += (
                    RetentionAttempt(
                        position=item.position,
                        request=item.request,
                        error=str(error),
                        unauthenticated=True,
                    ),
                )
            except ConversationDownloadError as error:
                logger.exception("Could not retain %s", item.request.describe())
                attempted += (settled(item, error=str(error)),)
            else:
                attempted += (settled(item, destination=destination),)
        return attempted

    return await retained_through(
        directories,
        requests,
        run,
        "The Claude browser login is missing or expired. Run "
        "`uv run lup-devtools conversation setup claude`, then retry.",
    )


def setup_browser_login(
    provider: str,
    label: str,
    page_url: str,
    profiles: ProfileDirectory | None,
    profile: str | None,
) -> None:
    """Open the explicit setup flow for one provider's selected browser state."""
    root = project_root()
    directories = browser_directories(root, provider, profiles, profile)
    asyncio.run(login(directories.primary, page_url, label))
    typer.echo(f"Saved the {label} browser login")


def create_conversation_setup_app(
    profiles: ProfileDirectory | None = None,
) -> typer.Typer:
    """Build the interactive-login tree owned by the conversation module."""
    application = typer.Typer(
        no_args_is_help=True,
        help="Authenticate browser sessions used for conversation retention",
    )

    @application.command("chatgpt")
    def chatgpt_login(
        profile: str | None = typer.Option(
            None,
            "--profile",
            help="Named profile whose ChatGPT web session should be authenticated",
        ),
    ) -> None:
        """Open a browser to authenticate ChatGPT conversation access."""
        setup_browser_login(
            "chatgpt", "ChatGPT", "https://chatgpt.com/", profiles, profile
        )

    @application.command("claude")
    def claude_login(
        profile: str | None = typer.Option(
            None,
            "--profile",
            help="Named profile whose Claude web session should be authenticated",
        ),
    ) -> None:
        """Open a browser to authenticate Claude conversation access."""
        setup_browser_login("claude", "Claude", "https://claude.ai/", profiles, profile)

    return application


def report(attempts: list[RetentionAttempt], provider: str, root: Path) -> None:
    """Announce every retention, checkpoint them together, then refuse a gap.

    One command run is one checkpoint however many URLs it was given, so a
    batch reaches the history as the single act the operator asked for.
    """
    for attempt in attempts:
        if attempt.destination is None:
            typer.echo(f"{attempt.request.describe()}: {attempt.error}", err=True)
            continue
        try:
            displayed = attempt.destination.relative_to(root)
        except ValueError:
            displayed = attempt.destination
        typer.echo(f"Retained {provider} conversation at {displayed}")
    retained = [
        attempt.destination for attempt in attempts if attempt.destination is not None
    ]
    if retained:
        checkpoint_delivery(root, retained, provider=provider)
    if len(retained) != len(attempts):
        raise typer.Exit(1)


def create_conversation_app(
    profiles: ProfileDirectory | None = None,
) -> typer.Typer:
    """Build the conversation command tree over a project's profile directory.

    A project naming no directory of its own logs in and retains under the
    person's, so the state a login writes is the state a retention reads.
    """
    directory = profiles or claude_profile_directory()
    application = typer.Typer(no_args_is_help=True)
    application.add_typer(create_conversation_setup_app(directory), name="setup")

    @application.command("chatgpt")
    def chatgpt_cmd(
        urls: list[str] = typer.Argument(
            help="chatgpt.com/c/... or /share/... URLs, each optionally suffixed "
            ":<artifact> to retain that one file instead of every attachment",
            metavar="URL...",
        ),
        profile: str | None = typer.Option(
            None,
            "--profile",
            help="Named profile whose ChatGPT web session should be reused",
        ),
        output: Path = typer.Option(
            Path("tmp/conversations"),
            "--output",
            "-o",
            help="Directory under which provider and conversation ids are stored",
        ),
    ) -> None:
        """Retain ChatGPT conversations and their downloadable attachments."""
        root = project_root()
        directories = browser_directories(root, "chatgpt", directory, profile)
        target = output if output.is_absolute() else root / output
        requests = [RetentionRequest.parse(value) for value in urls]
        report(
            asyncio.run(retain_chatgpt(requests, root, directories, target)),
            "chatgpt",
            root,
        )

    @application.command("claude")
    def claude_cmd(
        urls: list[str] = typer.Argument(
            help="claude.ai/chat/... or /share/... URLs, each optionally suffixed "
            ":<artifact> to retain that one file instead of every attachment",
            metavar="URL...",
        ),
        profile: str | None = typer.Option(
            None,
            "--profile",
            help="Named profile whose Claude web session should be reused",
        ),
        output: Path = typer.Option(
            Path("tmp/conversations"),
            "--output",
            "-o",
            help="Directory under which provider and conversation ids are stored",
        ),
    ) -> None:
        """Retain Claude conversations and their API-provided attachments."""
        root = project_root()
        directories = browser_directories(root, "claude", directory, profile)
        target = output if output.is_absolute() else root / output
        requests = [RetentionRequest.parse(value) for value in urls]
        report(
            asyncio.run(retain_claude(requests, root, directories, target)),
            "claude",
            root,
        )

    return application


app = create_conversation_app()
