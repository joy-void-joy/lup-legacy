"""The setup wizard keeps a host-only integration's keys in the project's host store.

An integration declaring ``host_only=True`` is answered into the host store
rather than ``.env.local``, and read back from it; ``setup secret <KEY>`` sets
a key no integration declares. Inside a container each refuses before
anything is typed, naming the command to run on the host.
"""

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import lup.devtools.setup as setup
from lup.devtools.setup import Integration, PromptField, create_setup_app
from lup.launch.secrets import HostSecrets


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "adlib"\n')
    monkeypatch.setattr(setup, "PROJECT_ROOT", root)
    monkeypatch.setattr(setup, "ENV_LOCAL", root / ".env.local")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LUP_CONTAINED", raising=False)
    return root


GEMINI = Integration(
    name="Gemini",
    command="gemini",
    help="The key the render companion calls Gemini with",
    env_keys=["GEMINI_API_KEY"],
    fields=[PromptField(key="GEMINI_API_KEY", prompt="Gemini API key")],
    host_only=True,
)


def app(integrations: list[Integration]) -> typer.Typer:
    return create_setup_app(integrations)


def test_a_host_only_integration_is_answered_into_the_host_store(
    checkout: Path,
) -> None:
    ran = CliRunner().invoke(app([GEMINI]), ["gemini"], input="the-key\n")

    assert ran.exit_code == 0, ran.output
    assert HostSecrets.for_checkout(checkout).read() == {"GEMINI_API_KEY": "the-key"}
    assert not (checkout / ".env.local").exists()
    assert GEMINI.check_status(GEMINI.stored()).ok


def test_a_key_no_integration_declares_is_set_by_name(checkout: Path) -> None:
    ran = CliRunner().invoke(app([]), ["secret", "RENDER_TOKEN"], input="tok\n")

    assert ran.exit_code == 0, ran.output
    assert HostSecrets.for_checkout(checkout).read() == {"RENDER_TOKEN": "tok"}
    assert "tok" not in ran.output


def test_a_secret_is_unset_by_name(checkout: Path) -> None:
    HostSecrets.for_checkout(checkout).write({"RENDER_TOKEN": "tok", "KEEP": "1"})

    ran = CliRunner().invoke(app([]), ["secret", "RENDER_TOKEN", "--unset"])

    assert ran.exit_code == 0, ran.output
    assert HostSecrets.for_checkout(checkout).read() == {"KEEP": "1"}


@pytest.mark.parametrize(
    ("words", "named"),
    [
        (["gemini"], "uv run lup-devtools setup gemini"),
        (["secret", "RENDER_TOKEN"], "uv run lup-devtools setup secret RENDER_TOKEN"),
    ],
)
def test_inside_a_container_each_refuses_before_anything_is_typed_naming_the_host(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, words: list[str], named: str
) -> None:
    monkeypatch.setenv("LUP_CONTAINED", "1")

    ran = CliRunner().invoke(app([GEMINI]), words, input="leaked\n")

    assert ran.exit_code != 0
    assert named in ran.output and "on the host" in ran.output
    assert HostSecrets.for_checkout(checkout).read() == {}


def test_the_walk_passes_a_host_only_integration_by_inside_a_container(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LUP_CONTAINED", "1")

    ran = CliRunner().invoke(app([GEMINI]), [], input="leaked\n")

    assert ran.exit_code == 0, ran.output
    assert "uv run lup-devtools setup gemini" in ran.output
    assert HostSecrets.for_checkout(checkout).read() == {}
    del checkout
