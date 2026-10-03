"""Every account a person's sessions can draw on, and how each one's windows are read.

An account is one runtime's login under one profile — or under the home no
profile selects — and what a subscription meters is the account, whichever
session spends it. The budget reads each one's windows the way ``usage``
does, by the reader its own runtime declares, and reads what a Codex session
spent off the rollout it writes; both are asked of the adapter here rather
than known by the reader holding a row, as :mod:`lup.providers.interrupts`
asks one to stop a turn.
"""

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from lup.harness.environment import inherited
from lup.observability.usage.models import PacingWindow, UsageReader, UsageUnavailable
from lup.providers.login import ProviderLogin
from lup.providers.profile_tree import profile_directory
from lup.providers.user_config import UserConfigFile
from lup.sessions.limits import Account
from lup.types import JsonObject


class RuntimeLogin(BaseModel, frozen=True):
    """One runtime, by its launch spelling, and where it keeps a login."""

    runtime: str
    login: ProviderLogin


def runtime_logins() -> list[RuntimeLogin]:
    """Every runtime whose accounts the budget meters."""
    from lup.providers.claude.login import CLAUDE_LOGIN
    from lup.providers.codex.login import CODEX_LOGIN

    return [
        RuntimeLogin(runtime="claude", login=CLAUDE_LOGIN),
        RuntimeLogin(runtime="codex", login=CODEX_LOGIN),
    ]


class AccountHome(BaseModel, frozen=True):
    """One account and the configuration home its login is kept in."""

    account: Account
    home: Path
    signed_in: bool
    identity: str = ""
    """Whose login it is, as the runtime's account document says; empty where it does not."""

    volume: str = ""
    """The repository volume the login is kept in, where it is kept in one
    rather than in *home*: *home* is then the checkout whose volume it is."""


def account_homes(
    checkouts: list[Path], config: UserConfigFile | None = None
) -> list[AccountHome]:
    """Every account the checkouts' and the person's profiles hold, and the home none selects.

    One per home: a global profile every checkout sees is one account, and a
    checkout's own profile of the same name a second, told apart by the
    checkout it is kept in. The home no profile selects is ``default``.
    """
    person = config or UserConfigFile()

    def homes(runtime: RuntimeLogin) -> list[AccountHome]:
        login = runtime.login
        ambient = login.selected_home(inherited())
        found = [
            AccountHome(
                account=Account(runtime=runtime.runtime),
                home=ambient,
                signed_in=login.logged_in(ambient),
            ),
            *[
                AccountHome(
                    account=Account(
                        runtime=runtime.runtime,
                        profile=entry.name
                        if entry.scope == "global"
                        else f"{entry.name}@{checkout.name}",
                    ),
                    home=entry.config_dir,
                    signed_in=entry.logged_in,
                )
                for checkout in checkouts
                for entry in profile_directory(login, person, checkout).entries()
            ],
        ]
        return [
            each
            for index, each in enumerate(found)
            if all(
                earlier.home.resolve() != each.home.resolve()
                for earlier in found[:index]
            )
        ]

    return [each for runtime in runtime_logins() for each in homes(runtime)]


def account_reader(
    account: AccountHome, stored: Callable[[], bytes | None] | None = None
) -> UsageReader:
    """The reader of one account's windows, in its runtime's own terms.

    *stored* reads the login to ask with where it is not *home*'s own: the
    copy a repository's volume keeps, which only a container reaches.
    """
    match account.account.runtime:
        case "claude":
            from lup.providers.claude.usage.api import account_record
            from lup.providers.claude.usage.reader import ClaudeUsageReader

            return ClaudeUsageReader(
                account.home,
                record=account_record(account.identity) if account.identity else None,
                stored=stored,
            )
        case "codex":
            from lup.providers.codex.usage.reader import CodexUsageReader

            return CodexUsageReader(Path("codex"), account.home)
        case runtime:
            raise UsageUnavailable(f"lup reads no windows of {runtime} accounts")


class TranscriptSpend(BaseModel, frozen=True):
    """What one transcript line says a request moved, and the account's windows as it moved them."""

    tokens: int = 0
    windows: list[PacingWindow] = []


def transcript_spend(runtime: str, record: JsonObject) -> TranscriptSpend | None:
    """One transcript line's spend, where its runtime records spend in its transcript.

    Codex writes a token count after every request; Claude's spend reaches
    the dashboard through its telemetry instead, so its lines answer nothing.
    """
    match runtime:
        case "codex":
            from lup.providers.codex.usage.reader import rollout_spend

            spent = rollout_spend(record)
            if spent is None:
                return None
            return TranscriptSpend(tokens=spent.tokens, windows=spent.windows)
        case _:
            return None
