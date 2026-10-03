"""The profile surface a launch selects an account through.

A wrong answer here launches the harness under the wrong account, or silently
takes over a config home the caller had already chosen: these pin that an
explicit name beats the selected one, that naming neither inherits the
surrounding environment rather than forcing a default, that the command tree
reports an unknown name with the roster instead of a traceback, and that one
name is one account on every runtime.

Two registries answer: a checkout's own ``.lup/profiles`` and the global
profiles beside the person's lup config. These pin that a checkout's profile
of a name wins over the global one, that its ``.active`` beats the config
file's selection, that curating acts on the checkout's unless ``--global``
names the other, and that a listing says which entry a name resolves to.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lup.devtools.harness.launch as launch
import lup.devtools.harness.profile_app as profile_app
from lup.coordination.refs import ActorRef
from lup.coordination.roster import RecordedProcess, RosterMember
from lup.coordination.wake import WakePath, WakeRuntime
from lup.devtools.harness.launch import (
    relaunch_command,
    session_moves,
    switch_repository_login,
)
from lup.devtools.harness.profile_app import create_profile_app
from lup.launch.config_volume import (
    HandedLogin,
    LaunchedAccount,
    LaunchedAccounts,
    LoginOwner,
    VolumeLogin,
    VolumeLogins,
)
from lup.launch.container import VolumeSeeding
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.harness import AdapterName
from lup.providers.login import ProviderLogin
from lup.providers.profile_tree import profile_directory, profile_environment
from lup.providers.profiles import ProfileDirectory, UnknownProfile
from lup.providers.user_config import UserConfigFile

PLAIN_CONSOLE = {"FORCE_COLOR": None, "NO_COLOR": "1", "TERM": "dumb"}

runner = CliRunner(env=PLAIN_CONSOLE)


@pytest.fixture
def config(tmp_path: Path) -> UserConfigFile:
    return UserConfigFile(tmp_path / "lup")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return tmp_path / "checkout"


@pytest.fixture
def directory(config: UserConfigFile, checkout: Path) -> ProfileDirectory:
    return profile_directory(CLAUDE_LOGIN, config, checkout)


def local_home(checkout: Path, name: str, login: ProviderLogin = CLAUDE_LOGIN) -> Path:
    """Where the checkout keeps that name's home for one runtime."""
    return checkout / ".lup" / "profiles" / name / login.home_subdir


def global_home(
    config: UserConfigFile, name: str, login: ProviderLogin = CLAUDE_LOGIN
) -> Path:
    """Where the person keeps that name's home for one runtime, for every checkout."""
    return config.profiles_root() / name / login.home_subdir


def active_file(checkout: Path) -> Path:
    return checkout / ".lup" / "profiles" / ".active"


def sign_in(home: Path) -> None:
    """Leave the file Claude Code writes on a completed login."""
    home.mkdir(parents=True, exist_ok=True)
    CLAUDE_LOGIN.credentials_path(home).write_text("{}", encoding="utf-8")


def test_a_person_who_has_started_no_profiles_reads_an_empty_roster(
    directory: ProfileDirectory,
) -> None:
    assert directory.entries() == []
    assert directory.launch_home(None) is None


def test_a_profile_is_added_to_the_checkout_unless_global_is_named(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    local = directory.add("work")
    shared = directory.add("mine", scope="global")

    assert (local.scope, local.config_dir) == ("local", local_home(checkout, "work"))
    assert (shared.scope, shared.config_dir) == ("global", global_home(config, "mine"))
    assert local.config_dir.is_dir() and shared.config_dir.is_dir()
    assert directory.launch_home("work") == local.config_dir
    assert directory.launch_home("mine") == shared.config_dir


def test_a_checkouts_profile_wins_over_a_global_one_of_the_same_name(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("work", scope="global")
    directory.add("work")

    assert directory.launch_home("work") == local_home(checkout, "work")
    assert directory.account("work").variables == CLAUDE_LOGIN.environment(
        local_home(checkout, "work")
    )
    assert profile_environment(
        CLAUDE_LOGIN, "work", config, checkout
    ) == CLAUDE_LOGIN.environment(local_home(checkout, "work"))


def test_a_global_profile_answers_where_the_checkout_keeps_none(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("other")
    directory.add("work", scope="global")

    assert directory.launch_home("work") == global_home(config, "work")
    assert profile_environment(
        CLAUDE_LOGIN, "work", config, checkout
    ) == CLAUDE_LOGIN.environment(global_home(config, "work"))


def test_one_name_holds_a_home_for_each_runtime_side_by_side(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("work", scope="global")
    codex = profile_directory(CODEX_LOGIN, config, checkout)

    assert codex.launch_home("work") == global_home(config, "work", CODEX_LOGIN)
    assert directory.launch_home("work") != codex.launch_home("work")


def test_a_codex_profile_resolves_through_both_registries(
    config: UserConfigFile, checkout: Path
) -> None:
    codex = profile_directory(CODEX_LOGIN, config, checkout)
    codex.add("me", scope="global")
    codex.add("work", scope="global")
    codex.add("work")

    assert codex.launch_home("work") == local_home(checkout, "work", CODEX_LOGIN)
    assert codex.launch_home("me") == global_home(config, "me", CODEX_LOGIN)
    assert codex.launch_home(None) == global_home(config, "me", CODEX_LOGIN)

    codex.use("work")

    assert codex.launch_home(None) == local_home(checkout, "work", CODEX_LOGIN)


def test_the_first_global_profile_becomes_the_selection_in_the_config_file(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("work", scope="global")
    directory.add("personal", scope="global")

    assert config.load().profile == "work"
    assert not active_file(checkout).exists()
    assert directory.launch_home(None) == directory.profile("work").config_dir

    directory.use("personal", "global")
    assert config.load().profile == "personal"
    assert directory.launch_home(None) == directory.profile("personal").config_dir


def test_the_first_local_profile_becomes_the_checkouts_selection(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("work")
    directory.add("personal")

    assert active_file(checkout).read_text(encoding="utf-8").strip() == "work"
    assert config.load().profile is None
    assert directory.launch_home(None) == local_home(checkout, "work")


def test_a_checkouts_first_profile_leaves_a_global_selection_standing(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("me", scope="global")
    directory.add("work")

    assert not active_file(checkout).exists()
    assert directory.launch_home(None) == global_home(config, "me")


def test_the_checkouts_selection_beats_the_config_file(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("me", scope="global")
    directory.add("work")

    selected = directory.use("work")

    assert config.load().profile == "me"
    assert (selected.scope, selected.active) == ("local", True)
    assert directory.launch_home(None) == local_home(checkout, "work")
    assert directory.account(None).name == "work"


def test_a_checkouts_selection_may_name_a_global_profile(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("me", scope="global")
    directory.add("other", scope="global")

    directory.use("other")

    assert active_file(checkout).read_text(encoding="utf-8").strip() == "other"
    assert config.load().profile == "me"
    assert directory.launch_home(None) == global_home(config, "other")


def test_the_global_selection_names_only_a_profile_every_checkout_reaches(
    directory: ProfileDirectory, config: UserConfigFile
) -> None:
    """A config file read by every checkout cannot select one checkout's account."""
    directory.add("work")

    with pytest.raises(UnknownProfile, match="unknown profile 'work'; known: none"):
        directory.use("work", "global")

    assert config.load().profile is None


def test_launch_prefers_an_explicit_name_over_the_selected_one(
    directory: ProfileDirectory,
) -> None:
    directory.add("work")
    directory.add("personal")

    assert directory.launch_home("personal") == directory.profile("personal").config_dir


def test_entries_report_activeness_and_whether_a_home_holds_a_login(
    directory: ProfileDirectory,
) -> None:
    sign_in(directory.add("work").config_dir)
    directory.add("personal")

    entries = {entry.name: entry for entry in directory.entries()}

    assert entries["work"].active and entries["work"].logged_in
    assert not entries["personal"].active
    assert not entries["personal"].logged_in


def test_entries_list_both_registries_and_which_entry_a_name_resolves_to(
    directory: ProfileDirectory,
) -> None:
    directory.add("me", scope="global")
    directory.add("work", scope="global")
    directory.add("work")

    assert [
        (entry.name, entry.scope, entry.resolved, entry.active)
        for entry in directory.entries()
    ] == [
        ("me", "global", True, True),
        ("work", "local", True, False),
        ("work", "global", False, False),
    ]


def test_a_profile_refuses_a_home_its_name_does_not_derive(
    directory: ProfileDirectory, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="symlink"):
        directory.add("work", tmp_path / "elsewhere")


def test_forgetting_a_profile_says_to_remove_the_directory(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("work")
    directory.add("work", scope="global")

    with pytest.raises(ValueError, match=str(checkout / ".lup" / "profiles" / "work")):
        directory.remove("work")
    with pytest.raises(ValueError, match=str(config.profiles_root() / "work")):
        directory.remove("work", "global")


def test_forgetting_the_selected_profile_names_its_selection_too(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    """Removed alone, the directory leaves launches refused for a ghost."""
    directory.add("work", scope="global")
    directory.add("personal", scope="global")
    directory.add("here")

    with pytest.raises(ValueError) as selected:
        directory.remove("work", "global")
    with pytest.raises(ValueError) as unselected:
        directory.remove("personal", "global")
    directory.use("here")
    with pytest.raises(ValueError) as checkout_selected:
        directory.remove("here")

    assert f"the `profile` line in {config.path()}, which selects it" in str(
        selected.value
    )
    assert "selects it" not in str(unselected.value)
    assert f"{active_file(checkout)}, which selects it" in str(checkout_selected.value)


def test_a_selection_whose_directory_is_gone_reports_the_roster(
    directory: ProfileDirectory, config: UserConfigFile
) -> None:
    """The launch comment's second case: a selected name nothing answers to."""
    directory.add("work", scope="global")
    config.select_profile("departed")

    with pytest.raises(UnknownProfile, match="unknown profile 'departed'"):
        directory.launch_home(None)


def test_an_unknown_name_carries_the_roster_however_it_was_resolved(
    directory: ProfileDirectory,
) -> None:
    """What the launcher renders, so it stops reporting a bare ``KeyError``."""
    directory.add("work")
    directory.add("mine", scope="global")

    with pytest.raises(UnknownProfile) as raised:
        directory.launch_home("ghost")

    assert "unknown profile 'ghost'" in str(raised.value)
    assert "known: mine, work" in str(raised.value)
    assert "profile add ghost" in str(raised.value)
    assert "--global" in str(raised.value)


def test_the_command_tree_lists_every_profile_and_marks_the_active_one(
    directory: ProfileDirectory,
) -> None:
    directory.add("work")
    directory.add("personal")

    result = runner.invoke(create_profile_app(directory), ["list"])

    assert result.exit_code == 0
    assert "* work  local" in result.output
    assert "  personal  local" in result.output
    assert "no login yet" in result.output


def test_the_command_tree_marks_each_registry_and_what_a_name_resolves_to(
    directory: ProfileDirectory,
) -> None:
    directory.add("me", scope="global")
    directory.add("work", scope="global")
    directory.add("work")

    lines = runner.invoke(create_profile_app(directory), ["list"]).output.splitlines()

    assert [(line[0], *line[2:].split()[:2]) for line in lines] == [
        ("*", "me", "global"),
        (" ", "work", "local"),
        (" ", "work", "global"),
    ]
    assert "shadowed" not in lines[1]
    assert lines[2].endswith("; shadowed by the local work)")


def test_the_command_tree_names_the_roster_on_an_unknown_profile(
    directory: ProfileDirectory,
) -> None:
    directory.add("work")

    result = runner.invoke(create_profile_app(directory), ["use", "ghost"])

    assert result.exit_code != 0
    assert "unknown profile 'ghost'" in result.output
    assert "known: work" in result.output


def test_adding_through_the_command_tree_says_how_to_sign_the_home_in(
    directory: ProfileDirectory,
) -> None:
    result = runner.invoke(create_profile_app(directory), ["add", "work"])

    assert result.exit_code == 0
    assert CLAUDE_LOGIN.config_home_env in result.output
    assert directory.launch_home("work") == directory.profile("work").config_dir


def test_adding_through_the_command_tree_is_local_unless_global_is_named(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    app = create_profile_app(directory)

    local = runner.invoke(app, ["add", "work"])
    shared = runner.invoke(app, ["add", "mine", "--global"])

    assert local.exit_code == 0 and shared.exit_code == 0
    assert f"Added local work: {local_home(checkout, 'work')}" in local.output
    assert f"Added global mine: {global_home(config, 'mine')}" in shared.output
    assert local_home(checkout, "work").is_dir()
    assert global_home(config, "mine").is_dir()
    assert not (config.profiles_root() / "work").exists()


def test_a_shadowed_global_profile_says_so_when_added(
    directory: ProfileDirectory,
) -> None:
    directory.add("work")

    result = runner.invoke(create_profile_app(directory), ["add", "work", "--global"])

    assert result.exit_code == 0
    assert "Not used here; shadowed by the local work" in result.output


def test_using_through_the_command_tree_selects_locally_unless_global_is_named(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("me", scope="global")
    directory.add("other", scope="global")
    directory.add("work")
    app = create_profile_app(directory)

    local = runner.invoke(app, ["use", "work"])
    shared = runner.invoke(app, ["use", "other", "--global"])

    assert local.exit_code == 0 and shared.exit_code == 0
    assert active_file(checkout).read_text(encoding="utf-8").strip() == "work"
    assert config.load().profile == "other"
    assert "Selected work for this checkout" in local.output
    assert "Selected other for every checkout" in shared.output
    assert "This checkout's own selection, work, still answers here" in shared.output
    assert directory.launch_home(None) == local_home(checkout, "work")


def test_removing_through_the_command_tree_names_the_registry_asked_for(
    directory: ProfileDirectory, config: UserConfigFile, checkout: Path
) -> None:
    directory.add("work")
    directory.add("work", scope="global")
    app = create_profile_app(directory)

    wide = {"COLUMNS": "400"}

    local = runner.invoke(app, ["remove", "work"], env=wide)
    shared = runner.invoke(app, ["remove", "work", "--global"], env=wide)

    assert local.exit_code != 0 and shared.exit_code != 0
    assert str(checkout / ".lup" / "profiles" / "work") in local.output
    assert str(config.profiles_root() / "work") in shared.output


def test_an_account_is_named_where_a_run_starts_rather_than_inherited(
    directory: ProfileDirectory,
) -> None:
    """The seam an entry point takes instead of reading the console.

    Selecting a profile has to reach everything a run opens — planners,
    workers, reviewers — and it reached only what a launcher opened, because
    every other entry point derived its environment from whatever the shell
    exported. Resolving once, into a value the run is handed, is what makes
    an entry point that forgets impossible to write rather than merely wrong.
    """
    home = directory.add("work").config_dir

    account = directory.account("work")

    assert (account.name, account.home) == ("work", home)
    exported = account.exported({"PATH": "/usr/bin"})
    assert exported["PATH"] == "/usr/bin"
    assert exported == {**exported, **CLAUDE_LOGIN.environment(home)}


def test_an_explicit_name_beats_the_selection_for_an_account(
    directory: ProfileDirectory,
) -> None:
    directory.add("work")
    directory.add("personal")
    directory.use("personal")

    assert directory.account(None).name == "personal"
    assert directory.account("work").name == "work"
    assert directory.account("work").home == directory.profile("work").config_dir


def test_naming_no_account_where_none_is_selected_stays_on_the_surrounding_one(
    directory: ProfileDirectory,
) -> None:
    """A real answer rather than a gap, and it writes nothing.

    A person who keeps no profiles has no account to name, and a session
    opened inside another one should stay on the account it was started
    under. Both are the same fact: this account exports nothing, so what the
    environment already carries survives.
    """
    account = directory.account(None)

    assert (account.name, account.home) == (None, None)
    assert account.exported({"CLAUDE_CONFIG_DIR": "/already/chosen"}) == {
        "CLAUDE_CONFIG_DIR": "/already/chosen"
    }


def running(
    member_id: str,
    runtime: WakeRuntime = "claude",
    scope: str = "host",
    session: str = "",
    handle: str = "",
    parent: str = "",
    alive: bool = True,
) -> RosterMember:
    """One roster row, its runtime process read in ``scope``'s pid namespace."""
    return RosterMember(
        actor=ActorRef(kind="session", id=member_id),
        task="working",
        running=alive,
        worktree=f"/tree/{member_id}",
        wake=WakePath(runtime=runtime, session=session, handle=handle),
        parent=parent,
        cli_name=member_id,
        process=RecordedProcess(pid=7, scope=scope),
    )


def test_a_relaunch_reopens_a_conversation_wherever_another_account_still_finds_it() -> (
    None
):
    """The volume and a checkout's Codex home keep it; a Claude profile's home does not."""
    inside = relaunch_command(
        running("inside", scope="box", session="abc"), "work", contained=True
    )
    host = relaunch_command(running("host", session="abc"), "work", contained=False)
    codex = relaunch_command(
        running("codex", runtime="codex", handle="thread-1"), "work", contained=False
    )
    unnamed = relaunch_command(running("unnamed"), "work", contained=True)

    assert inside.words == [
        *("uv", "run", "--directory", "/tree/inside", "lup-devtools", "harness"),
        *("claude", "--profile", "work", "--session", "abc"),
    ]
    assert inside.resumes and "repository's volume" in inside.why
    assert host.words[-4:] == ["--profile", "work", "--sandbox", "inner"]
    assert not host.resumes and "profile's own home" in host.why
    assert codex.words[-6:] == [
        *("--profile", "work", "--session", "thread-1", "--sandbox", "inner")
    ]
    assert codex.resumes and "Codex home" in codex.why
    assert not unnamed.resumes and "no session id" in unnamed.why
    assert inside.spelled().startswith("uv run --directory /tree/inside lup-devtools")
    with pytest.raises(ValueError, match="declared no runtime"):
        relaunch_command(running("quiet", runtime=""), "work", contained=False)


def test_a_switch_moves_contained_claude_sessions_and_answers_the_rest_with_commands(
    tmp_path: Path,
) -> None:
    work = LoginOwner(home=tmp_path / "work", profile="work")
    accounts = LaunchedAccounts(tmp_path / "launched")
    accounts.record(
        LaunchedAccount(
            member="on-work",
            runtime="claude",
            owner=work,
            checkout=tmp_path,
            contained=False,
            at=datetime.now(UTC),
        )
    )
    members = [
        running("inside", scope="box", session="abc"),
        running("host", session="def"),
        running("on-work"),
        running("subagent", scope="box", parent="inside"),
        running("codex", runtime="codex", scope="box"),
        running("stopped", scope="box", alive=False),
    ]

    def moves(seeded: bool) -> dict[str, tuple[bool, bool]]:
        return {
            move.member: (move.moved, move.relaunch is not None)
            for move in session_moves(
                members, CLAUDE_LOGIN, work, seeded, "host", accounts
            )
        }

    assert moves(seeded=True) == {
        "inside": (True, False),
        "host": (False, True),
        "on-work": (True, False),
    }
    assert moves(seeded=False)["inside"] == (False, False)


def test_a_switch_tells_each_contained_codex_session_how_to_reopen_on_the_profile(
    tmp_path: Path,
) -> None:
    """Codex keeps the login it started with, so the volume alone moves nobody."""
    [move] = session_moves(
        [running("inside", runtime="codex", scope="box", handle="thread-1")],
        CODEX_LOGIN,
        LoginOwner(home=tmp_path / "work", profile="work"),
        True,
        "host",
        LaunchedAccounts(tmp_path / "launched"),
    )

    assert not move.moved and move.contained
    assert "keeps the login it started with" in move.why
    assert move.relaunch is not None
    assert move.relaunch.words[-4:] == ["--profile", "work", "--session", "thread-1"]


class Roster:
    """A repository's roster holding exactly these rows."""

    def __init__(self, members: list[RosterMember]) -> None:
        self.members = members

    def __call__(self, checkout: Path) -> "Roster":
        del checkout
        return self

    def present(self) -> list[RosterMember]:
        return self.members


@pytest.fixture
def switched(
    monkeypatch: pytest.MonkeyPatch, checkout: Path, tmp_path: Path
) -> list[HandedLogin]:
    """A repository called `lup` whose volume takes whatever login it is handed."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    handed: list[HandedLogin] = []

    def seeded(
        root: Path,
        login: ProviderLogin,
        offered: HandedLogin,
        config_home: str,
        logins: VolumeLogins,
        now: datetime,
    ) -> VolumeSeeding:
        del root, config_home
        handed.append(offered)
        held = VolumeLogin(
            volume=f"lup-{login.state_volume}-lup",
            runtime=login.state_volume,
            owner=offered.owner,
            handed_at=now,
        )
        logins.record(held)
        return VolumeSeeding(held=held)

    monkeypatch.setattr(launch, "seed_volume_login", seeded)
    monkeypatch.setattr(
        launch, "state_volume_name", lambda root, login: f"lup-{login.state_volume}-lup"
    )
    monkeypatch.setattr(
        launch,
        "RepositoryPeers",
        Roster([running("inside", scope="box"), running("host", session="def")]),
    )
    monkeypatch.setattr(launch, "process_scope", lambda: "host")
    monkeypatch.setattr(profile_app, "project_root", lambda: checkout)
    return handed


def test_switching_hands_the_profiles_login_to_the_repositorys_volume(
    directory: ProfileDirectory, checkout: Path, switched: list[HandedLogin]
) -> None:
    directory.add("work")
    sign_in(local_home(checkout, "work"))
    VolumeLogins().record(
        VolumeLogin(
            volume="lup-claude-lup",
            runtime="claude",
            owner=LoginOwner(home=checkout / "personal", profile="personal"),
            handed_at=datetime.now(UTC),
        )
    )

    outcome = switch_repository_login(checkout, AdapterName.CLAUDE, "work", directory)

    [offered] = switched
    assert offered.owner == LoginOwner(
        home=local_home(checkout, "work"), profile="work"
    )
    assert offered.moving == "move"
    assert offered.credential == CLAUDE_LOGIN.credentials_path(offered.owner.home)
    assert outcome.before is not None and outcome.before.owner.profile == "personal"
    assert outcome.held is not None and outcome.held.owner.profile == "work"
    held = VolumeLogins().held("lup-claude-lup")
    assert held is not None and held.owner.profile == "work"
    lines = outcome.lines()
    assert (
        lines[0] == "lup-claude-lup now holds work's claude login (it held personal's)."
    )
    assert "  inside: takes work's login from the volume at its next request" in lines
    assert any(
        line.startswith("  host: runs on the host") and "--sandbox inner" in line
        for line in lines
    )


def test_a_profile_with_no_login_hands_the_volume_nothing(
    directory: ProfileDirectory, checkout: Path, switched: list[HandedLogin]
) -> None:
    directory.add("work")

    outcome = switch_repository_login(checkout, AdapterName.CLAUDE, "work", directory)

    assert switched == [] and outcome.held is None
    assert "work holds no claude login yet" in outcome.why
    assert outcome.lines()[0].startswith("lup-claude-lup still holds its login: work")
    with pytest.raises(UnknownProfile):
        switch_repository_login(checkout, AdapterName.CLAUDE, "ghost", directory)


def test_the_command_tree_switches_this_repository_and_fails_where_nothing_took(
    directory: ProfileDirectory, checkout: Path, switched: list[HandedLogin]
) -> None:
    directory.add("work")
    directory.add("empty")
    sign_in(local_home(checkout, "work"))
    app = create_profile_app(directory)

    moved = runner.invoke(app, ["switch", "work"])
    empty = runner.invoke(app, ["switch", "empty"])
    ghost = runner.invoke(app, ["switch", "ghost"])

    assert moved.exit_code == 0
    assert "lup-claude-lup now holds work's claude login." in moved.output
    assert empty.exit_code == 1 and "empty holds no claude login yet" in empty.output
    assert ghost.exit_code != 0 and "unknown profile 'ghost'" in ghost.output
    assert len(switched) == 1
