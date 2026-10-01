"""Non-interactive shell environment for agent-run commands.

Credential helpers prompt on ``/dev/tty`` — the terminal the native TUI or
resolver console owns — so a passphrase or editor prompt raised from an agent
shell wedges the whole session instead of failing. These variables make every
such command fail fast with a readable error: ssh refuses to prompt (keys
already loaded in ssh-agent keep working), git never opens an editor or pager,
and gh and keyring lookups stay non-interactive. The devtools launch and
resolver flows merge these defaults beneath the inherited environment at
every agent spawn point. Explicit caller values win except ``VIRTUAL_ENV``:
the child project must select its own environment from its working directory.
"""

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from lup.coordination.identity import MEMBER_ENV, NAME_ENV, MemberEnv
from lup.devtools.dashboard.pulse import DASHBOARD_PULSE_ENV
from lup.policy.identity import (
    AGENT_IDENTITY_ENV,
    DASHBOARD_URL_ENV,
    REVIEW_ANSWERS_ENV,
)
from lup.devtools.launcher import ENVIRONMENT_VARIABLE
from lup.sessions.recursion import RecursiveAgentSettings
from lup.types import EnvVars
from lup.workspace.context import SESSION_DIR_ENV, SESSION_ID_ENV

# lup: ignore[library-default] — each pair is the variable and off-value git, ssh, gh, and keyring document
NON_INTERACTIVE_SHELL_ENV: EnvVars = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
    "SSH_ASKPASS_REQUIRE": "never",
    "GIT_EDITOR": "true",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GH_PAGER": "cat",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
}


def non_interactive_environment(
    base: Mapping[str, str],  # lup: ignore[dict-str-payload] — open env-var map
) -> EnvVars:
    """Merge shell defaults without binding a session to its caller's venv."""
    merged = {**NON_INTERACTIVE_SHELL_ENV, **base}
    return {name: value for name, value in merged.items() if name != "VIRTUAL_ENV"}


class PlacementEnv(BaseSettings, extra="ignore"):
    """The variables a launch sets to say where this process runs, as they arrive."""

    contained: str = Field(default="", validation_alias="LUP_CONTAINED")
    sandbox: str = Field(default="", validation_alias="LUP_SANDBOX_ACTIVE")
    nonce: str = Field(default="", validation_alias="LUP_BOUNDARY_NONCE")
    member: str = Field(default="", validation_alias=MEMBER_ENV)
    agent: str = Field(default="", validation_alias=AGENT_IDENTITY_ENV)
    session_dir: str = Field(default="", validation_alias=SESSION_DIR_ENV)
    session_id: str = Field(default="", validation_alias=SESSION_ID_ENV)


class GivenPlacementEnv(PlacementEnv):
    """The same variables, read from an environment handed over and nothing else.

    A launch's environment is asked about before it is anybody's process, so
    this process's own variables must not answer for it.
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


class Placement(BaseModel, frozen=True):
    """Where a process runs, as the environment a launch made for it says.

    Three facts, each its own variables and none the negation of another — a
    session the runtime sandboxes on the host is in no container and is
    still not the operator's terminal. Each is a placement hint and never a
    boundary: a shell that exported one on the host costs that process what
    the hint withholds there and nothing else. What the launch *measured*
    is the boundary ledger's, which the policy host reads.

    Which runtime's session a process is in is not among them: it is read
    off the configuration-home variable each launcher exports, which only
    the providers may spell, by :func:`~lup.providers.runtime_homes.selected_runtime`.
    """

    # lup: defer: carry `runtime` here once this module holds the sanctioned
    # whole-environment reader (`inherited`, on refactor-lib-files-state):
    # `here()` then asks selected_runtime of that environment rather than
    # devtools/harness/launch.py reading os.environ for it
    contained: bool = False
    """Inside a session's container: the image bakes ``LUP_CONTAINED=1``."""

    sandboxed: bool = False
    """Under the runtime's own sandbox, which the launcher arms and marks."""

    in_session: bool = False
    """Inside an agent session: a launch hands each one its boundary nonce, a
    roster identity, an agent identity or a session directory, and a command
    the operator runs from a terminal carries none of them."""

    @classmethod
    def of(cls, environment: EnvVars) -> Self:
        """The placement a launch's environment describes, read from it alone."""
        return cls.read(GivenPlacementEnv.model_validate(environment))

    @classmethod
    def here(cls) -> Self:
        """The placement of this process."""
        return cls.read(PlacementEnv())

    @classmethod
    def read(cls, variables: PlacementEnv) -> Self:
        """The placement the launch's variables state."""
        return cls(
            contained=variables.contained == "1",
            sandboxed=variables.sandbox == "1",
            in_session=any(
                (
                    variables.nonce,
                    variables.member,
                    variables.agent,
                    variables.session_dir,
                    variables.session_id,
                )
            ),
        )

    @property
    def host(self) -> bool:
        """Whether this is the operator's own terminal: no session, sandbox or container.

        What the known-repositories store is written from, since a session
        that could add to it could add its own repository.
        """
        return not (self.contained or self.sandboxed or self.in_session)


LAUNCHER_DECIDED_ENV: list[str] = [
    "LUP_CONTAINED",
    "LUP_BOUNDARY_NONCE",
    "LUP_BOUNDARY_ROOT",
    "GIT_CONFIG_COUNT",
    MEMBER_ENV,
    NAME_ENV,
    DASHBOARD_URL_ENV,
    DASHBOARD_PULSE_ENV,
    REVIEW_ANSWERS_ENV,
]
"""What a launched process is told about where it is, rather than what it does.

The coordination pair is one session's own identity: a launcher mints the id
and the name together and exports both, so a process that inherits them and is
not that session answers to somebody else's address. It arrives by reference
rather than respelled, because a second spelling is a second place a variable
has to be added, and the one that is missed is the one nobody takes away. The
dashboard's address is where that launch's companion listens, which a process
that did not hold it has no business reporting as its own; the answers store
is the operator's, lent to that launch, where a suite recording an answer
would write into the person's own reviews, or fail on a read-only mount.

A suite is the case that matters. A test joining a roster without saying what
to call the session is named after whichever worktree pytest was started from
— so the roster's naming tests, asking which name a session with none is
called after, measure the launcher's name for whoever runs them and pass on
the machine that wrote them alone.

Deliberately not ``UV_PROJECT_ENVIRONMENT``, which reads like one and is not.
It names where this machine's toolchain is, and
:func:`~lup.policy.assets.host.declared_program` consults it on purpose so a
declaration can name a program rather than a layout. Taking it away would not
make a suite posture-independent -- it would make it measure a machine it is
not running on, and the gate that resolves a bare name would resolve nothing.
"""


def tool_server_env() -> list[str]:
    """What a launched session's tool servers read from the environment its launcher made.

    The coordination pair is who the session is on the roster, which a server
    joins under and answers to; the recursion allowance is how many more agent
    levels its tools may open. A server that sees none of them serves a session
    nobody can address and opens agents without limit, and neither says so.
    The project-environment selector keeps a tool server's interpreter and
    language-server settings aligned with the checkout's selected toolchain.

    Read off the settings that read them rather than listed beside them, so a
    variable either grows is forwarded with nobody remembering to. Declared as
    each such server's ``env_vars`` rather than trusted to arrive, because one
    runtime does not pass them on. Measured on Codex 0.155.1: a stdio server it
    starts sees ``HOME``, ``LANG``, ``LC_ALL``, ``LOGNAME``, ``PATH``,
    ``SHELL``, ``TERM``, ``TMPDIR`` and ``USER`` and nothing else, with these
    exported in Codex's own environment — and all of them once ``env_vars``
    names them. Claude Code hands a server its whole environment, so the
    declaration changes nothing there.
    """
    return [
        field.validation_alias
        for settings in (MemberEnv, RecursiveAgentSettings)
        for field in settings.model_fields.values()
        if isinstance(field.validation_alias, str)
    ] + [ENVIRONMENT_VARIABLE]


def launcher_decided_names(
    environ: Mapping[str, str],  # lup: ignore[dict-str-payload] — open env-var map
    declared: list[str] = LAUNCHER_DECIDED_ENV,
    prefix: str = "LUP_",
) -> list[str]:
    """Every variable name a launcher set, with the numbered ones expanded.

    A test suite is the caller this exists for. Each of these answers a
    question a test means to ask of the code: whether a boundary sits under
    it, where a sync lands, who the checkout commits as. A suite that
    inherits them measures the session it runs in -- so the same test passes
    on the machine that wrote it and fails inside the container that machine
    builds, which is where the whole class was found.

    ``GIT_CONFIG_COUNT`` is expanded rather than cleared alone, because git
    reads it as the length of a numbered series. Clearing the count is enough
    to stop git reading the pairs and leaves them behind for whoever reads the
    environment next, and a half-cleared scope is worse than either.

    Every name carrying lup's own *prefix* is taken too. The declared list
    says what a launch is known to export, and trails what launches actually
    export: a credential key, a policy root and a trust document each reached
    a session's environment without joining it.
    """
    counted = (
        environ["GIT_CONFIG_COUNT"].strip() if "GIT_CONFIG_COUNT" in environ else ""
    )
    return [
        *declared,
        *(
            f"GIT_CONFIG_{half}_{index}"
            for index in range(int(counted) if counted.isdigit() else 0)
            for half in ("KEY", "VALUE")
        ),
        *(name for name in environ if name.startswith(prefix)),
    ]
