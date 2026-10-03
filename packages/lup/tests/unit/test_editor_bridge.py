"""Which directory the editor bridge binds, and why it is never the profile's.

The bridge mounts one directory so an editor on the host and a CLI in a
container can find each other through a lockfile. The home the *launch*
chooses is, under ``--profile``, derived from a name no editor has heard of --
bound there, the container gets an empty directory, the editor never connects,
and nothing says why. So the source is resolved from the variable the editor
itself reads, a runtime that offers no rendezvous gets no mount, and the bind
lands at the same name inside.
"""

from pathlib import Path

from lup.harness.image import Image
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.login import ProviderLogin


def test_the_editors_own_variable_selects_the_directory_a_bridge_binds() -> None:
    """A sibling process's variable, which is the question a mount asks."""
    chosen = Path("/elsewhere/config")

    rendezvous = CLAUDE_LOGIN.editor_rendezvous({"CLAUDE_CONFIG_DIR": str(chosen)})

    assert rendezvous == chosen / "ide"


def test_a_variable_nothing_set_falls_back_to_the_runtimes_own_default() -> None:
    """Declared per provider, so no caller writes a default of its own."""
    assert CLAUDE_LOGIN.editor_rendezvous({}) == CLAUDE_LOGIN.ambient_home / "ide"


def test_an_empty_variable_is_absence_rather_than_a_home_at_the_root() -> None:
    """An exported-but-empty variable is how a shell says nothing, not `/`."""
    assert CLAUDE_LOGIN.selected_home({"CLAUDE_CONFIG_DIR": ""}) == (
        CLAUDE_LOGIN.ambient_home
    )
    assert CODEX_LOGIN.selected_home({"CODEX_HOME": ""}) == (
        CODEX_LOGIN.ambient_home.resolve()
    )


def test_with_nothing_named_the_home_is_joined_onto_the_environment_s_home(
    tmp_path: Path,
) -> None:
    """What the CLI itself does: its default sits in the effective user's ``HOME``."""
    other = tmp_path / "other"

    assert CLAUDE_LOGIN.selected_home({"HOME": str(other)}) == other / ".claude"
    assert (
        CODEX_LOGIN.selected_home({"HOME": str(other)}) == (other / ".codex").resolve()
    )


def test_a_runtime_that_canonicalises_its_home_is_answered_resolved(
    tmp_path: Path,
) -> None:
    """Codex resolves the home it is pointed at; Claude Code takes the spelling."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real)
    named = str(tmp_path / "linked")

    assert CODEX_LOGIN.selected_home({"CODEX_HOME": named}) == real
    assert CLAUDE_LOGIN.selected_home({"CLAUDE_CONFIG_DIR": named}) == Path(named)


def test_a_runtime_declaring_no_rendezvous_is_bridged_nowhere() -> None:
    """Codex's extension spawns its own core, so there is nothing to bridge.

    ``None`` rather than a directory nothing reads: a mount built anyway would
    be the same silent no-op this whole change exists to end, one vendor over.
    """
    assert not CODEX_LOGIN.editor_lockfiles
    assert CODEX_LOGIN.editor_rendezvous({}) is None
    assert CODEX_LOGIN.editor_rendezvous({"CODEX_HOME": "/somewhere"}) is None


def test_a_profile_home_can_never_be_what_the_bridge_resolves(tmp_path: Path) -> None:
    """The defect itself: the launch's home and the editor's are two answers.

    A profile home is derived from a name inside the checkout, so nothing the
    editor reads can produce one -- which is the property that makes the
    resolution correct rather than merely different.
    """
    profile = tmp_path / ".lup" / "profiles" / "test" / "claude-config"
    login = ProviderLogin(
        config_home_env="CLAUDE_CONFIG_DIR",
        credentials_file=".credentials.json",
        ambient_home=tmp_path / "home" / ".claude",
        editor_lockfiles="ide",
        home_subdir="claude-config",
        state_volume="claude",
    )

    assert login.editor_rendezvous({}) == login.ambient_home / "ide"
    assert login.editor_rendezvous({}) != profile / "ide"


def test_the_bind_lands_at_the_same_name_inside_the_container() -> None:
    """The container's CLI looks under *its* home, by the runtime's own name."""
    image = Image()

    flag, mount = image.ide_bridge(Path("/host/home/.claude/ide"))

    assert flag == "-v"
    assert mount == f"/host/home/.claude/ide:{image.config_home}/ide:rw"
