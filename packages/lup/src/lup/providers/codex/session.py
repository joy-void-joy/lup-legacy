"""What a launched Codex session needs around it that the CLI does not do.

Its login verified through the native account API before the session
opens, its plugin installed into the home it will read, and what it
changed of the person's settings carried back when it closes -- each
through the same execution boundary the session itself runs behind.
"""

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import sh
from rich.prompt import Confirm

from lup.providers.user_config import UserConfigFile
from lup.launch.config_volume import named_file
from lup.launch.container import read_config_home
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.account import read_account
from lup.providers.codex.harness_runtime import revision_snapshot
from lup.providers.codex.install import PreparedPlugin, install_codex_plugin
from lup.providers.codex.marketplace import CodexMarketplace
from lup.providers.codex.profile import CodexAccountSettings
from lup.harness.image import Image
from lup.harness.notice import Notice
from lup.launch.refusal import LaunchRefused
from lup.types import EnvVars, JsonObject
from lup.providers.codex.home import (
    SEED_RECORD,
    CodexWorktreeHomeStore,
    seeded_codex_settings,
)


def asked(question: str) -> bool:
    """Put a yes-or-no question to the person at this terminal, yes by default."""
    return Confirm.ask(question, default=True)


def codex_login_preflight(
    home: Path,
    environment: EnvVars,
    command: list[str] | None = None,
    *,
    headless: bool = False,
    consent: Callable[[str], bool] = asked,
) -> None:
    """Refresh managed authentication through its native owner before launch.

    A local token deadline proves neither renewal nor acceptance by another
    service. Native account/read owns renewal; its absence or failure is an
    unverified login, never a successful local-file check. Declining sign-in
    remains explicit so a deliberately offline session is still possible.

    ``consent`` is how the person is asked whether to sign in now: at this
    terminal unless the caller has its own way of asking.
    """
    selected = {**environment, **CODEX_LOGIN.environment(home)}
    executable, *arguments = command or ["codex"]

    def verified() -> bool:
        try:
            state = asyncio.run(
                read_account(
                    Path(executable), selected, refresh_token=True, arguments=arguments
                )
            )
        except (OSError, RuntimeError, ValueError, sh.CommandNotFound) as error:
            # Native error bodies can contain credentials or account identity.
            Notice(
                text=(
                    f"Codex authentication in {home}: not verified; "
                    f"native account check failed ({type(error).__name__})."
                ),
                urgency="warning",
            ).say()
            return False
        if state.ready:
            return True
        Notice(
            text=f"Codex authentication in {home}: not signed in.",
            urgency="warning",
        ).say()
        return False

    if verified():
        return
    if not consent("Sign in to Codex now?"):
        Notice(
            text=(
                "Continuing with authentication not verified — "
                "Codex will report its own authentication errors."
            ),
            urgency="warning",
        ).say()
        return
    try:
        sh.Command(executable)(
            *arguments,
            "login",
            *(["--device-auth"] if headless else []),
            _fg=True,
            _env=selected,
        )
    except sh.ErrorReturnCode as error:
        raise LaunchRefused("Codex sign-in did not complete") from error
    if not verified():
        raise LaunchRefused(
            f"Codex authentication in {home} remains unverified after sign-in; "
            "the session was not opened."
        )


def settled_codex_seed(image: Image, root: Path, theirs: JsonObject) -> JsonObject:
    """What a contained Codex home will be given, and where the person's settings won.

    The three-way merge the home's own installation runs
    (:func:`~lup.providers.codex.home.seeded_codex_settings`), run first on
    the volume as it stands, so the launch knows what its session starts
    from and can say which settings a running session had changed too.
    """
    files = read_config_home(image, root, CODEX_LOGIN, ["config.toml", SEED_RECORD])
    current = named_file(files, "config.toml")
    recorded = named_file(files, SEED_RECORD)
    seeded = seeded_codex_settings(
        current.text() if current is not None else None,
        recorded.text() if recorded is not None else None,
        theirs,
    )
    for conflict in seeded.conflicts:
        Notice(
            text=(
                f"Settings: {conflict} was changed both by a session still "
                "running in this repository and in your own settings; yours win."
            ),
            urgency="warning",
        ).say()
    return seeded.settings


def carry_codex_home(
    store: CodexWorktreeHomeStore,
    image: Image | None,
    root: Path,
    config: UserConfigFile,
    applied: JsonObject | None = None,
) -> None:
    """Bring back what a Codex session changed of the person's, and say what stayed.

    ``image`` is the container a contained session ran in, whose volume
    holds the configuration it left; ``None`` reads the worktree home a
    session on the host ran in. A volume that cannot be read moves
    nothing, said aloud.
    """
    left = None
    if image is not None:
        read = named_file(
            read_config_home(image, root, CODEX_LOGIN, ["config.toml"]),
            "config.toml",
        )
        if read is None:
            Notice(
                text=(
                    "Could not read this session's Codex settings back out of "
                    "its volume, so nothing it changed was returned."
                ),
                urgency="warning",
            ).say()
            return
        left = read.text()
    returned = store.return_settings(root, config, current=left, applied=applied)
    if returned.carried():
        Notice(
            text=(
                "Returned the Codex settings this session changed: "
                + ", ".join(returned.carried())
            ),
            urgency="detail",
        ).say()
    stayed = [
        *(f"{key} (never leaves its home)" for key in returned.withheld),
        *(f"{key} (the session's own)" for key in returned.session),
    ]
    if stayed:
        Notice(
            text="Kept in the session's home: " + ", ".join(stayed),
            urgency="detail",
        ).say()


# lup: solved: a contained Codex session runs its hooks from the revision
# installed here, inside its home volume, which the session can write; the
# Claude plugin is held with the generated trees, this copy is not. Hold the
# installed revision read-only for the session (a volume sub-mount: Docker's
# volume-subpath, podman's subpath) or install from a launch-time snapshot
# the session cannot reach -- a choice between engine versions this launch
# cannot yet assume
def prepare_codex_plugin(
    prefix: list[str],
    home: Path,
    root: Path,
    environment: EnvVars,
    force: bool = False,
    trusted: bool = False,
    settings: CodexAccountSettings | None = None,
) -> PreparedPlugin:
    """Prepare the home where the launch runs, through its own execution boundary.

    Answers with the revision installed, by its path in the home: read back
    from the preparation as JSON where it ran inside the container, since
    only it knows the name the home's own cache made it take. What it says
    along the way reaches the terminal as it is said.
    """
    if not prefix:
        if settings is not None:
            settings.install(
                home, enforce_policy=CodexMarketplace.declared(root) is not None
            )
        return install_codex_plugin(root, home, force, trusted)
    assert CODEX_LOGIN.home_preparation is not None
    command = [
        *prefix,
        *CODEX_LOGIN.home_preparation.command(
            root, home, force, settings is not None, report=True
        ),
    ]
    reported = sh.Command(command[0])(
        *command[1:],
        _env=environment,
        _in=settings.model_dump_json() if settings is not None else None,
        _err=sys.stderr,
    )
    return PreparedPlugin.model_validate_json(str(reported))


def held_revision(
    prepared: PreparedPlugin,
    declared: CodexMarketplace,
    config_home: str,
    snapshots: Path,
) -> dict[Path, str]:
    """The installed revision, held read-only over the plugin's cache in the home.

    Codex runs a plugin's hooks from the revision installed in its home, and
    a contained session's home is the volume it writes -- so the revision is
    written again on the host, outside the checkout, and mounted read-only
    over the plugin's whole cache there. The session then runs the hooks it
    was launched with, and no other revision the volume holds is in its
    sight, whatever it writes beside them.

    The reported revision has to sit in that cache: the preparation ran in
    the session's own checkout, and a path anywhere else is refused rather
    than trusted for where the hold lands.
    """
    if prepared.installed_root is None:
        return {}
    cache = PurePosixPath(
        config_home, "plugins", "cache", declared.name, declared.plugin
    )
    installed = PurePosixPath(prepared.installed_root)
    if installed.parent != cache:
        raise ValueError(
            f"The Codex home's preparation reported {installed}, outside the "
            f"plugin's cache at {cache}, so nothing holds the hooks this "
            "session would run. Launch again; if it recurs, the checkout's "
            "lup library is not the one this launch installs from."
        )
    snapshot = revision_snapshot(declared.source, installed.name, snapshots)
    return {snapshot: cache.as_posix()}
