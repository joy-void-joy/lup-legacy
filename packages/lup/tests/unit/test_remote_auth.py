"""Two questions about one remote, and why asking one for both fails.

A forwarded ssh agent answers for the transport and says nothing about the
forge API, so a session can push all day and be unable to open the request
describing what it pushed. These hold the split to the case that motivated
it: an ssh remote, perfectly reachable, on a session whose forge client holds
no credential at all.
"""

import pytest
import sh

from lup.devtools.dev import remote_auth

SSH_REMOTE = "git@github.com:acme/widget.git"


class UnconfiguredGit:
    """A git whose configuration names no ssh command."""

    def out(self, *arguments: str, **keywords: object) -> str:
        """What `git config --get core.sshCommand` prints with nothing set."""
        return ""


class StubForgeClient:
    """A forge client holding nothing, which is how a logged-out one behaves.

    It records what it was asked, because half of what these tests check is
    that it was asked at all -- the defect guarded against is a probe that
    reports ready having consulted nothing, and only the call log tells that
    from a probe that asked and was answered.
    """

    def __init__(self) -> None:
        self.asked: list[tuple[str, ...]] = []

    def __call__(self, *arguments: str, **keywords: object) -> str:
        self.asked.append(arguments)
        raise sh.CommandNotFound("gh")


def test_an_ssh_remote_reaches_the_forge_client_for_the_api_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe runs on the remotes that most need it.

    Dispatching on the remote's scheme would send every ssh remote to the
    transport arm, asking the client only where git already speaks https --
    the one case where a failure would surface anyway.
    """
    client = StubForgeClient()
    monkeypatch.setattr(remote_auth, "origin_url", lambda: SSH_REMOTE)
    monkeypatch.setattr(remote_auth, "gh", client)

    assert remote_auth.check_forge_api() is False
    assert client.asked == [("auth", "status")]


def test_the_transport_probe_still_declines_to_ask_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correct on its own terms, and exactly why it could not answer for the API.

    This is the shape the module's promise would break on: a key that works,
    a client that holds nothing, and one probe reporting ready for both.
    """
    client = StubForgeClient()

    def reachable(destination: str, remote_url: str) -> remote_auth.RemoteRefusal:
        """An ssh destination that answers, which a forwarded agent's does."""
        return remote_auth.RemoteRefusal()

    monkeypatch.setattr(remote_auth, "origin_url", lambda: SSH_REMOTE)
    monkeypatch.setattr(remote_auth, "gh", client)
    monkeypatch.setattr(remote_auth, "ssh_auth_refusal", reachable)

    assert remote_auth.check_remote_auth() is True
    assert client.asked == []


def test_the_probe_speaks_the_ssh_command_git_was_handed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe reads the config git reads, so it cannot blank a sweep.

    A sandbox hands git its own ``known_hosts`` through ``GIT_SSH_COMMAND``.
    A probe spelling ``ssh`` itself would read a different configuration,
    fail host key verification against a remote git reaches perfectly, and
    every reader gated on that answer would spend the failure as a fact --
    the sweep reporting no branch carrying a pull request and no remote
    carrying branches, neither of which anything had looked at.
    """
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -F /run/lup/ssh/config")

    program = remote_auth.git_ssh_program()

    assert program.startswith("ssh -F /run/lup/ssh/config")
    assert "BatchMode=yes" in program


def test_a_session_handed_no_ssh_command_still_probes_without_prompting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-interactivity is the probe's own, not something it inherits.

    Appending it to a configured command covers the sandbox; supplying it
    where nothing is configured covers the host, whose ssh would otherwise
    be free to stop on a passphrase prompt nobody is there to answer.
    """
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setattr(remote_auth, "git", UnconfiguredGit())

    assert remote_auth.git_ssh_program() == "ssh -o BatchMode=yes -o ConnectTimeout=5"


def test_the_refusal_names_both_places_a_credential_comes_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host and a contained session are fixed differently, and both read this.

    Naming only the sign-in sends whoever is inside a container after the one
    remedy that does not outlive it.
    """
    monkeypatch.setattr(remote_auth, "gh", StubForgeClient())

    refusal = remote_auth.gh_auth_refusal(SSH_REMOTE)

    assert refusal.credential is True
    assert "gh auth login" in refusal.complaint
    assert "LUP_GIT_TOKEN" in refusal.complaint
    assert "token_source" in refusal.complaint
