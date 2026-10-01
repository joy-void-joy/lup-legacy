"""Which words name a withheld path, and which variables a command prints.

A pattern spelled from the root names one place; one spelled from a home or
from anywhere names whatever path its trailing names end, because the kernel
knows no home. A glob on either side reaches what it could expand to, except
the dot-named files a shell's own expansion skips, and an exemption covers a
word only when everything the word could name is exempt.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.policy.kernel.diagnostic import step
from lup.policy.kernel.rows import RefusedPathRow
from lup.policy.kernel.withheld import reaches, secret_name, withheld_path
from lup.policy.refused_paths import (
    RefusedPaths,
    credential_files,
    secret_variable_names,
)
from lup.providers.login import ProviderLogin

ROWS: list[RefusedPathRow] = [
    credential_files().erased(),
    RefusedPaths(
        paths=["/tmp/lup-wake/**"],
        reason="a wake socket",
        recovery=[step("use the tool")],
    ).erased(),
]


@pytest.mark.parametrize(
    ("pattern", "path", "reached"),
    [
        ("~/.ssh/**", "~/.ssh/id_rsa", True),
        ("~/.ssh/**", "/home/u/.ssh/id_rsa", True),
        ("~/.ssh/**", "$HOME/.ssh/id_rsa", True),
        ("~/.ssh/**", "~/.ssh", True),
        ("~/.ssh/**", "~/.ssh/../.ssh/id_rsa", True),
        ("~/.ssh/**", "~/.sshkeys/id_rsa", False),
        ("~/.ssh/**", "~", False),
        ("~/.aws/credentials", ".*/credentials", True),
        ("~/.aws/credentials", "*/credentials", False),
        ("~/.netrc", "*", False),
        ("~/.netrc", ".n*", False),
        ("~/.netrc", "~/.n*", True),
        ("~/.netrc", "$HOME/.*", True),
        ("~/.netrc", "/home/u/.*", False),
        ("**/codex-home/auth.json", ".lup/codex-home/auth.json", True),
        ("**/codex-home/auth.json", "src/auth.json", False),
        ("**/.env.local", ".*", True),
        ("**/.env.local", "/srv/checkout/.*", True),
        ("**/.env.*.local", ".env*", True),
        ("**/.env.*.local", ".env", False),
        ("/proc/*/environ", "/proc/self/environ", True),
        ("/proc/*/environ", "proc/self/environ", False),
        ("/tmp/lup-wake/**", "/tmp/lup-wake/dev.sock", True),
        ("/tmp/lup-wake/**", "/var/tmp/lup-wake/dev.sock", False),
    ],
)
def test_a_pattern_reaches_what_its_anchor_says(
    pattern: str, path: str, reached: bool
) -> None:
    assert reaches(pattern, path) is reached


@pytest.mark.parametrize(
    ("word", "directory", "refused"),
    [
        ("~/.ssh/id_ed25519", "", True),
        ("~/.ssh/id_ed25519.pub", "", False),
        ("~/.ssh/*.pub", "", False),
        ("~/.ssh/*", "", True),
        ("--file=~/.netrc", "", True),
        (".ssh/id_rsa", "/home/u", True),
        ("../../tmp/lup-wake/dev.sock", "", True),
        ("lup-wake/dev.sock", "/tmp", True),
        ("lup-wake/dev.sock", None, False),
        ("UNIX-CONNECT:/tmp/lup-wake/dev.sock,retry=3", "", True),
        ("host:~/.ssh/id_rsa", "", True),
        ("HEAD:README.md", "", False),
        ("README.md", "", False),
        (".env.local", "", True),
        (".env.production.local", "", True),
        ("../dev/.env.local", "", True),
        ("--env-file=.env.local", "", True),
        (".env", "", False),
        (".env.example", "", False),
        (".env.local.example", "", False),
    ],
)
def test_a_word_is_placed_before_it_is_matched(
    word: str, directory: str | None, refused: bool
) -> None:
    """Relative to where the command stands, and to the checkout above that."""
    decision = withheld_path(word, directory, "/srv/checkout", ROWS)
    assert (decision is not None) is refused
    if decision is not None:
        assert decision.effect == "deny"
        assert decision.subject == word


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("GH_TOKEN", True),
        ("github_token", True),
        ("ANTHROPIC_API_KEY", True),
        ("AWS_SECRET_ACCESS_KEY", True),
        ("PGPASSWORD", True),
        ("NPM_CONFIG__AUTH", True),
        ("GITHUB_PAT", True),
        ("HOME", False),
        ("PATH", False),
        ("GIT_AUTHOR_NAME", False),
        ("SSH_AUTH_SOCK", False),
        ("KEYBOARD", False),
    ],
)
def test_a_secret_is_named_by_the_declared_patterns(name: str, secret: bool) -> None:
    assert secret_name(name, secret_variable_names()) is secret


def test_a_pattern_saying_nowhere_where_it_starts_is_refused() -> None:
    """A bare name would be read from wherever a command happened to stand."""
    with pytest.raises(ValidationError, match="spelled from the root"):
        RefusedPaths(paths=[".ssh/id_rsa"], reason="keys", recovery=[step("ask")])
    with pytest.raises(ValidationError, match="spelled from the root"):
        RefusedPaths(
            paths=["~/.ssh/**"], exempt=["x.pub"], reason="k", recovery=[step("a")]
        )
    with pytest.raises(ValidationError):
        RefusedPaths(paths=[], reason="keys", recovery=[step("ask")])


def test_a_runtime_login_is_withheld_in_its_home_and_in_every_profile() -> None:
    login = ProviderLogin(
        config_home_env="RUNTIME_HOME",
        credentials_file="auth.json",
        ambient_home=Path.home() / ".runtime",
        home_subdir="runtime-home",
        state_volume="runtime",
    )
    assert login.withheld_logins() == [
        "~/.runtime/auth.json",
        "**/runtime-home/auth.json",
    ]
