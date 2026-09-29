"""Shared fixtures for the library's own suite.

Separate from the template's `tests/conftest.py` and deliberately not importing
it: a library test reaching for a template fixture passes here and fails where
the library ships.
"""

import os
import shutil
import socket
import tempfile
import warnings
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

import lup.policy.assets.host as policy_host
import lup.providers.claude.launch as claude_launch
import lup.providers.profile_tree as profile_tree
from lup.devtools.gitguard import TEST_IDENTITY, GuardVerdict, RepositoryWatch
from lup.harness.environment import launcher_decided_names
from lup.harness.messaging import WakeSockets
from lup.providers.claude.config_home import ClaudeConfigHome, selected_config_home
from lup.providers.claude.login import CLAUDE_CONFIG_DIR
from lup.providers.codex.login import CODEX_HOME
from lup.providers.identity import RUNTIME_DECIDED_ENV
from lup.types import EnvVars


@pytest.fixture(scope="session", autouse=True)
def launcher_decisions_taken_away() -> Iterator[None]:
    """Measure the code, not the session this suite happens to run in.

    Autouse and session-scoped for the same reason as the guard below: no
    test can be asked to notice it. A variable the launcher set answers the
    question a test meant to put to the code, and answers it consistently —
    so the test passes on the machine that wrote it, and fails inside the
    container that machine builds, which is where every one of these was
    found. See :func:`~lup.harness.environment.launcher_decided_names`.
    """
    with pytest.MonkeyPatch.context() as environment:
        taken = [*launcher_decided_names(os.environ), *RUNTIME_DECIDED_ENV]
        for name in taken:
            environment.delenv(name, raising=False)
        yield


def pytest_configure(config: pytest.Config) -> None:
    """Keep `sh`'s forks from warning wherever a thread runs beside one.

    Python warns about a fork from a threaded process, and `sh` execs straight
    after it forks, which the warning cannot see: a suite running pools beside
    it — xdist's own worker thread among them — printed hundreds per run,
    burying the warnings that were news. Added to the configuration rather
    than set with `warnings.filterwarnings`, because pytest opens every test's
    warnings afresh from its configuration, and a session fixture's git calls
    land outside any window a fixture could open.
    """
    config.addinivalue_line(
        "filterwarnings",
        r"ignore:This process \(pid=\d+\) is multi-threaded:DeprecationWarning:sh",
    )


@pytest.fixture(scope="session", autouse=True)
def trusted_repository_store_isolated(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep the store of trusted repositories the suite's own.

    With the launcher's variables taken away every test runs as the host, and
    any command a test drives that verifies a worktree also remembers the
    repositories vouching for it -- into the machine's own store, trusting
    throwaway repositories long after the suite has deleted them. See
    :mod:`lup.sandbox.known`.
    """
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))
        yield


@pytest.fixture(scope="session", autouse=True)
def personal_config_withheld(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Answer as lup's defaults, whatever the person running the suite decided.

    Session-scoped and autouse for the reason the launcher's variables are
    taken away above: a developer's own ``~/.config/lup/config.toml`` — a
    theme, a tier, a selected profile — answers the question a test meant to
    put to the code. Pointed at an empty directory rather than unset, which
    would fall back to that same file. See :mod:`lup.providers.user_config`.
    The state lup keeps for the person is withheld the same way: a record
    of the volumes a split superseded is the developer's, not a test's.
    """
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv(
            "XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config"))
        )
        environment.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
        yield


@pytest.fixture(autouse=True)
def checkout_profiles_withheld(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Resolve profiles as though the checkout under test kept none of its own.

    For the reason the personal config is withheld above: a developer's own
    ``.lup/profiles`` — a profile, an ``.active`` selecting it — sits in the
    checkout the suite runs from, and a name resolves through it first. The
    checkout a profile directory reads when none is named is an empty one of
    each test's own, made the first time the test asks, since adding a
    profile writes there by default and one test's must not reach the next.
    Its own patch rather than the ``monkeypatch`` fixture, which an autouse
    fixture would set up first and so undo last, after a guard that runs git.
    """

    @cache
    def checkout() -> Path:
        return tmp_path_factory.mktemp("checkout")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(profile_tree, "project_root", checkout)
        yield


@pytest.fixture(scope="session", autouse=True)
def personal_claude_account_withheld(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep every launch under test out of the developer's own accounts.

    A host launch settles its theme into the account it runs as, and a launch
    naming no profile runs as the operator's default home — fixed when the
    login is imported, so no ``HOME`` a test sets moves it — or as whatever
    home this process's environment names, which inside a session is that
    session's own. So the homes this suite's environment names are taken
    away, and the home a launch reads when none is named is bound to an
    empty directory for the whole suite; one a test names outright is still
    the one it named.
    """
    account = tmp_path_factory.mktemp("claude-account")

    def withheld(environment: EnvVars) -> ClaudeConfigHome:
        if environment.get(CLAUDE_CONFIG_DIR):
            return selected_config_home(environment)
        return ClaudeConfigHome(
            directory=account / ".claude", document=account / ".claude.json"
        )

    with pytest.MonkeyPatch.context() as patched:
        patched.delenv(CLAUDE_CONFIG_DIR, raising=False)
        patched.delenv(CODEX_HOME, raising=False)
        patched.setattr(claude_launch, "selected_config_home", withheld)
        yield


@pytest.fixture(scope="session", autouse=True)
def committer_identity_armed() -> Iterator[None]:
    """Give every throwaway repository somebody to commit as, writing no file.

    Session-scoped and autouse because the git commands that need it are not
    all the suite's own: a resolver under test runs its own `git commit`, and
    reaches whatever the environment holds. See :mod:`lup.devtools.gitguard`.
    """
    with pytest.MonkeyPatch.context() as environment:
        for name, value in TEST_IDENTITY.environment().items():
            environment.setenv(name, value)
        yield


@pytest.fixture(scope="session", autouse=True)
def enclosing_repository_watched() -> Iterator[RepositoryWatch]:
    """Read the checkout this suite runs inside before its first test and after its last.

    The same guard the template suite arms, for the same reason: this suite
    builds throwaway repositories too, and a fixture that forgets to bind git
    to one reaches the developer's checkout instead. See :mod:`lup.devtools.gitguard`.
    Under xdist each worker is a session of its own over one shared ref
    store, so the watch carries the worker's name for its report to say who
    saw what.
    """
    root = Path(__file__).resolve().parents[3]
    watch = RepositoryWatch.armed(root, os.environ.get("PYTEST_XDIST_WORKER", "main"))
    yield watch
    settled(watch.after("the teardown after this worker's last test"))


@pytest.fixture(autouse=True)
def enclosing_repository_untouched(
    enclosing_repository_watched: RepositoryWatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Fail the test whose window saw the checkout change, not whichever ran last.

    Function-scoped so the comparison closes around one test: a difference
    closed once per session lands under xdist on the last test the noticing
    worker ran, which is a policy row about `gh pr create` as easily as the
    fixture that escaped.
    """
    yield
    settled(enclosing_repository_watched.after(request.node.nodeid))


def settled(verdict: GuardVerdict) -> None:
    """Say what a sibling worktree moved, and fail on what this run moved."""
    if verdict.notice:
        warnings.warn(verdict.notice, stacklevel=2)
    if verdict.failure:
        pytest.fail(verdict.failure, pytrace=False)


@pytest.fixture
def unix_socket() -> None:
    """Skip, saying why, where this process may not open a Unix socket at all.

    A test measuring delivery through a real socket has nothing to measure
    where the socket itself is refused — a Claude Code Bash sandbox refuses
    ``socket(AF_UNIX)`` with EPERM — and failing there would report a defect
    the code does not have. Any other failure a socket meets stays a failure.
    """
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
    except PermissionError as refused:
        pytest.skip(
            f"this process may not open a Unix socket ({refused}); delivery "
            "through one cannot be measured here"
        )


@pytest.fixture
def launch_record_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ledger this process reads, read as a contained launch holds it.

    A policy composed in process believes a ledger's containment only
    through a read-only mount of its directory, which a test cannot make in
    its own process; a dispatcher run as its own process is held for real,
    in a user namespace.
    """
    monkeypatch.setattr(policy_host, "record_held", lambda *_arguments: True)


@pytest.fixture
def wake_sockets() -> Iterator[WakeSockets]:
    """Wake sockets placed in a directory short enough to key a whole member id.

    Beside ``/tmp`` rather than under pytest's own temporary path, whose depth
    leaves too few of a Unix socket address's bytes for a repository, a digest
    and an id -- which the placement refuses rather than cuts.
    """
    directory = Path(tempfile.mkdtemp(prefix="lupw", dir="/tmp"))
    yield WakeSockets(directory=str(directory))
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def socket_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process is refused ``socket(AF_UNIX)``, as a Claude Code Bash sandbox is.

    Refused at construction with EPERM, before any path is tried, which is
    the one thing the code under test has to tell apart from a peer that is
    not listening.
    """

    def refused(*_arguments: int) -> socket.socket:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(socket, "socket", refused)
