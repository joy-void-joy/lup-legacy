"""Shared test fixtures.

Add fixtures here that are used across multiple test files.
"""

import os
import socket
import warnings
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

import lup.policy.assets.host as policy_host
import lup.providers.profile_tree as profile_tree
from lup.devtools.gitguard import TEST_IDENTITY, GuardVerdict, RepositoryWatch
from lup.harness.environment import launcher_decided_names
from lup.providers.identity import runtime_decided_names


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
        taken = [
            *launcher_decided_names(os.environ),
            *runtime_decided_names(os.environ),
        ]
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
    checkout the suite runs from, and a name resolves through it first. Each
    test reads an empty checkout of its own, made the first time it asks.
    """

    @cache
    def checkout() -> Path:
        return tmp_path_factory.mktemp("checkout")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(profile_tree, "project_root", checkout)
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

    Autouse and session-scoped because the failure it catches is one no test
    can be asked to notice: a fixture that binds git to the working directory
    instead of its own throwaway repository commits successfully, passes, and
    leaves the developer's branch moved. See :mod:`lup.devtools.gitguard` for how that
    was found here, and what it cost. Under xdist each worker is a session of
    its own over one shared ref store, so the watch carries the worker's name
    for its report to say who saw what.
    """
    root = Path(__file__).resolve().parent.parent
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
def launch_record_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ledger this process reads, read as a contained launch holds it.

    A policy composed in process believes a ledger's containment only
    through a read-only mount of its directory, which a test cannot make in
    its own process; a dispatcher run as its own process is held for real,
    in a user namespace -- see `tests/unit/held.py`.
    """
    monkeypatch.setattr(policy_host, "record_held", lambda *_arguments: True)


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
