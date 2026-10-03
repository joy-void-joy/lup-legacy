"""The dashboard keeps every copy of an account's login in step as one of them renews.

It runs on a thread of its own while the dashboard every launch holds serves,
for each account whose runtime dates its renewals
(:attr:`~lup.providers.login.ProviderLogin.renewed_at`) -- Claude Code's;
a Codex launch converges its home with the account when it opens and closes
instead. One account's
copies are its profile's own login, every home derived from that profile in a
served checkout, and every served repository's volume lup last handed that
profile's login. A pass writes the newest into the rest
(:func:`~lup.providers.login_sync.carried`). A volume is reached through a
helper container, which costs a container start, so volumes are read every
``volume_passes`` passes rather than every one.
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from lup.harness.image import Image
from lup.launch.config_volume import VolumeLogins
from lup.launch.container import (
    VolumeLoginCopy,
    VolumeUnreachable,
    offered_login,
    state_volume_name,
)
from lup.providers.accounts import AccountHome, account_homes, runtime_logins
from lup.providers.login import ProviderLogin
from lup.providers.login_sync import (
    Carried,
    LoginFile,
    LoginPlace,
    carried,
    profile_lock,
)
from lup.providers.session_home import SessionHomeLayout
from lup.providers.user_config import UserConfigFile

logger = logging.getLogger(__name__)


def derived_copies(checkout: Path, login: ProviderLogin, home: Path) -> list[Path]:
    """Every copy of *home*'s login kept in a home derived from it in *checkout*.

    A derived home links back to the home it was derived from, so one whose
    links lead into *home* is one of its; the copy is its own login file.
    """
    derived = checkout / SessionHomeLayout().derived_dir
    if not derived.is_dir():
        return []
    shared = home.resolve()

    def from_home(candidate: Path) -> bool:
        return any(
            entry.is_symlink() and entry.resolve().parent == shared
            for entry in candidate.iterdir()
        )

    return [
        login.credentials_path(candidate)
        for candidate in sorted(derived.iterdir())
        if candidate.is_dir() and from_home(candidate)
    ]


class LoginKeeper:
    """Carry each account's newest login into every copy of it, every ``every`` seconds."""

    def __init__(
        self,
        checkouts: Callable[[], list[Path]],
        config: UserConfigFile,
        every: float = 30.0,
        volume_passes: int = 4,
        homes: Callable[[list[Path]], list[AccountHome]] = account_homes,
        volumes: VolumeLogins | None = None,
        config_home: str = Image().config_home,
    ) -> None:
        self.checkouts = checkouts
        self.config = config
        self.every = every
        self.volume_passes = volume_passes
        self.homes = homes
        self.volumes = volumes or VolumeLogins()
        self.config_home = config_home
        self.passes = 0
        self.stopping = threading.Event()
        self.thread = threading.Thread(
            target=self.keeping, name="lup-login-keeper", daemon=True
        )

    def copies(
        self,
        account: AccountHome,
        login: ProviderLogin,
        checkouts: list[Path],
        volumes: bool,
    ) -> list[LoginPlace]:
        """The profile's own login, each derived home's copy, and each volume handed it."""
        owned = account.home.resolve()
        handed = (
            [
                checkout
                for checkout in checkouts
                for held in [self.volumes.held(state_volume_name(checkout, login))]
                if held is not None and held.owner.home.expanduser().resolve() == owned
            ]
            if volumes
            else []
        )
        return [
            LoginFile(
                login.credentials_path(account.home),
                login.account_document(account.home),
            ),
            *(
                LoginFile(path, path.parent / login.trust_document)
                for checkout in checkouts
                for path in derived_copies(checkout, login, account.home)
            ),
            *(
                VolumeLoginCopy(
                    checkout,
                    login,
                    self.config_home,
                    offered_login(state_volume_name(checkout, login)),
                )
                for checkout in handed
            ),
        ]

    def keep(self, volumes: bool = True) -> list[Carried]:
        """One pass over every account: the newest copy of each carried into the rest."""
        checkouts = self.checkouts()
        dated = {
            each.runtime: each.login
            for each in runtime_logins()
            if each.login.renewed_at
        }
        passes: list[Carried] = []
        for account in self.homes(checkouts):
            login = (
                dated[account.account.runtime]
                if account.account.runtime in dated
                else None
            )
            if login is None or not account.signed_in:
                continue
            try:
                passes.append(
                    carried(
                        login,
                        self.copies(account, login, checkouts, volumes),
                        profile_lock(login, account.home),
                    )
                )
            except VolumeUnreachable as unreachable:
                logger.info("Logins in volumes are not kept in step: %s", unreachable)
                passes.append(
                    carried(
                        login,
                        self.copies(account, login, checkouts, volumes=False),
                        profile_lock(login, account.home),
                    )
                )
        for each in passes:
            for switched in each.switched:
                logger.info("%s holds another account's login; left as it is", switched)
            if each.unknown:
                logger.info("Logins not kept in step: %s", each.unknown)
        return passes

    def keeping(self) -> None:
        while not self.stopping.is_set():
            try:
                self.keep(volumes=self.passes % self.volume_passes == 0)
            except Exception:
                logger.exception("A pass keeping logins in step failed")
            self.passes += 1
            self.stopping.wait(self.every)

    def start(self) -> "LoginKeeper":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
