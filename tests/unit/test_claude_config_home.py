"""Per-session Claude configuration homes, and the trust they record."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lup.providers.claude.config_home import (
    CLAUDE_BACKUP_DIR,
    CLAUDE_HOME_DOCUMENT,
    CLAUDE_HOME_LAYOUT,
    CLAUDE_LEGACY_DOCUMENT,
    CLAUDE_OAUTH_DOCUMENT,
    CLAUDE_OAUTH_URL_ENV,
    CLAUDE_SESSION_ENV,
    ClaudeConfigHome,
    ClaudeConfigUnreadable,
    load_document,
    project_entries,
    record_trust,
    restorable_backups,
    restoration_advice,
    save_document,
    selected_config_home,
    trusts,
    untrusted_degradation,
    workspace_config_environment,
)
from lup.providers.claude.login import CLAUDE_CONFIG_DIR, CLAUDE_LOGIN
from lup.providers.session_home import SessionHomeLayout, SessionHomes
from lup.types import EnvVars

SEEDED_PROJECT = "/already/trusted"
SEEDED_THEME = "dark-daltonized"


def shared_home(root: Path, document: str = CLAUDE_HOME_DOCUMENT) -> Path:
    """A configuration home holding what a real profile carries.

    Its document is under the current name by default, which is what Claude
    Code writes into a named home that never held a legacy one."""
    home = root / "profile"
    (home / "plugins").mkdir(parents=True)
    (home / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
    (home / ".credentials.json").write_text('{"token": "shared"}', encoding="utf-8")
    save_document(
        home / document,
        {
            "theme": SEEDED_THEME,
            "projects": {SEEDED_PROJECT: {"hasTrustDialogAccepted": True}},
        },
    )
    return home


def homes_under(root: Path) -> SessionHomes:
    return SessionHomes(shared_home(root), CLAUDE_HOME_LAYOUT)


def session_document(environment: EnvVars, workspace: Path) -> Path:
    """The document a session of that workspace reads, as Claude Code finds it."""
    derived = workspace_config_environment(environment, workspace)
    return selected_config_home({**environment, **derived}).document


def test_a_derived_home_shares_everything_but_the_document(tmp_path: Path) -> None:
    """Isolation is of the racing file alone, never of the whole home."""
    derived = homes_under(tmp_path).derive(tmp_path / "lease-a")

    assert (derived / "settings.json").is_symlink()
    assert (derived / "plugins").is_symlink()
    assert (derived / ".credentials.json").is_symlink()
    assert not (derived / CLAUDE_HOME_DOCUMENT).exists()


@pytest.mark.parametrize(
    "document", [CLAUDE_LEGACY_DOCUMENT, CLAUDE_HOME_DOCUMENT, CLAUDE_OAUTH_DOCUMENT]
)
def test_no_name_claude_reads_its_document_under_is_linked(
    tmp_path: Path, document: str
) -> None:
    """A linked document is the shared one, whichever name it is read under."""
    home = shared_home(tmp_path, document)

    derived = SessionHomes(home, CLAUDE_HOME_LAYOUT).derive(tmp_path / "lease-a")

    assert not (derived / document).exists()
    assert not (derived / document).is_symlink()


def test_a_derived_home_lives_in_the_checkout_and_still_reads_the_account(
    tmp_path: Path,
) -> None:
    """Where a home lives and what it reads are answered separately.

    It lives in the checkout, because the state is the project's. It reads
    the selected home, because the login is the account's. Selecting an
    account home is not a licence to write inside it, and the symlink is
    what lets both hold at once."""
    homes = homes_under(tmp_path)
    workspace = tmp_path / "lease-a"

    derived = homes.derive(workspace)
    shared = homes.shared

    assert shared not in derived.parents
    assert workspace in derived.parents
    assert (derived / "settings.json").resolve().is_relative_to(shared)


def test_two_workspaces_in_one_checkout_keep_separate_documents(
    tmp_path: Path,
) -> None:
    """Rooting at the checkout must not merge the homes racing inside it.

    The document is per-workspace because that is what runs concurrently;
    a checkout holding two of them still owes each its own."""
    root = tmp_path / "project"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.lup]\nversion = "1.0.0"\n', encoding="utf-8"
    )
    homes = homes_under(tmp_path)

    first = homes.derive(root / "a")
    second = homes.derive(root / "b")

    assert first != second
    assert first.parent == second.parent == root / ".lup" / "sessions"


def test_a_refreshed_login_reaches_every_derived_home(tmp_path: Path) -> None:
    """One file for every session, because a rotation invalidates copies."""
    homes = homes_under(tmp_path)
    first = homes.derive(tmp_path / "lease-a")
    second = homes.derive(tmp_path / "lease-b")

    (first / ".credentials.json").write_text('{"token": "rotated"}', encoding="utf-8")

    rotated = '{"token": "rotated"}'
    assert (homes.shared / ".credentials.json").read_text(encoding="utf-8") == rotated
    assert (second / ".credentials.json").read_text(encoding="utf-8") == rotated


def test_switching_account_does_not_reuse_the_previous_login(tmp_path: Path) -> None:
    """Two accounts working one checkout each get a derived home of their own.

    A home kept in the checkout has no parent directory to carry the account, so
    a name derived from the workspace alone hands the second account a home the
    first already derived — whose entries are symlinked to the first and never
    re-pointed. Nothing fails: the session opens and runs as the wrong login,
    which is the reading a profile exists to make impossible.
    """
    workspace = tmp_path / "lease-a"
    first = homes_under(tmp_path / "account-one")
    second = homes_under(tmp_path / "account-two")

    before = first.derive(workspace)
    after = second.derive(workspace)

    assert before != after
    assert (before / ".credentials.json").resolve().is_relative_to(first.shared)
    assert (after / ".credentials.json").resolve().is_relative_to(second.shared)


def test_an_entry_made_later_reaches_a_home_derived_before_it(tmp_path: Path) -> None:
    """A runtime makes directories the first time it needs one."""
    homes = homes_under(tmp_path)
    workspace = tmp_path / "lease-a"
    homes.derive(workspace)

    (homes.shared / "session-env").mkdir()

    assert (homes.derive(workspace) / "session-env").is_symlink()


def test_a_home_holds_no_link_where_its_layout_names_a_private_entry(
    tmp_path: Path,
) -> None:
    """A private entry is the home's own file or nothing, whatever linked it.

    One home, derived under a layout sharing everything and then under one
    naming an entry private: a derivation refreshes the home toward the layout
    it is derived under rather than trusting it to match already."""
    shared = shared_home(tmp_path)
    workspace = tmp_path / "lease-a"
    SessionHomes(shared, SessionHomeLayout()).derive(workspace)

    private = SessionHomeLayout(private_files=["settings.json"])
    derived = SessionHomes(shared, private).derive(workspace)

    assert not (derived / "settings.json").is_symlink()
    assert not (derived / "settings.json").exists()
    assert (derived / ".credentials.json").is_symlink()


def test_two_leases_sharing_a_basename_do_not_share_a_home(tmp_path: Path) -> None:
    """A run names every lease for its concern, under a root per run."""
    homes = homes_under(tmp_path)

    left = homes.derive(tmp_path / "run-a" / "notes-gate")
    right = homes.derive(tmp_path / "run-b" / "notes-gate")

    assert left != right


def test_a_worker_phase_of_twenty_eight_corrupts_no_document(tmp_path: Path) -> None:
    """The failing run opened one session per leased concern at once.

    Each start reads the whole document and writes it back, so sessions
    sharing one file read each other's partial writes and die on a truncated
    parse. This asserts what removes that: a document per workspace, every
    one of them still parsing and still carrying what it was seeded from.
    """
    home = shared_home(tmp_path)
    leases = [tmp_path / "leases" / f"concern-{number}" for number in range(28)]

    def open_session(workspace: Path) -> Path:
        environment = {CLAUDE_CONFIG_DIR: str(home)}
        derived = workspace_config_environment(environment, workspace, trust=True)
        document = selected_config_home({**environment, **derived}).document
        for version in range(20):
            content = load_document(document)
            content["migrationVersion"] = version
            save_document(document, content)
        return document

    with ThreadPoolExecutor(max_workers=len(leases)) as pool:
        documents = list(pool.map(open_session, leases))

    assert len(set(documents)) == len(leases)
    for document, workspace in zip(documents, leases, strict=True):
        content = load_document(document)
        assert trusts(content, workspace)
        assert SEEDED_PROJECT in json.dumps(content)


def test_trust_is_recorded_for_the_lease_and_not_the_operator(tmp_path: Path) -> None:
    """The exception to project-level-only settings stays this narrow."""
    home = shared_home(tmp_path)
    before = (home / CLAUDE_HOME_DOCUMENT).read_bytes()
    lease = tmp_path / "lease"
    source = tmp_path / "source"

    def document_for(workspace: Path, trust: bool) -> Path:
        environment = {CLAUDE_CONFIG_DIR: str(home)}
        derived = workspace_config_environment(environment, workspace, trust=trust)
        return selected_config_home({**environment, **derived}).document

    assert trusts(load_document(document_for(lease, True)), lease)
    assert not trusts(load_document(document_for(source, False)), source)
    assert (home / CLAUDE_HOME_DOCUMENT).read_bytes() == before


def operator_at(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Stand ``home`` in for the operator's, as the login declares it.

    The login fixes the default home when it is imported, for the reason
    ``CLAUDE_LOGIN`` gives, so moving ``HOME`` alone moves only the document
    beside it; the declaration is bound to the same directory.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "lup.providers.claude.config_home.CLAUDE_LOGIN",
        CLAUDE_LOGIN.model_copy(
            update={"ambient_home": home / CLAUDE_LOGIN.ambient_home.name}
        ),
    )


def test_an_unnamed_home_reads_the_document_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset, Claude reads ``~/.claude.json`` rather than an entry in the home."""
    operator_at(monkeypatch, tmp_path)

    selected = selected_config_home({})

    assert selected.directory == tmp_path / ".claude"
    assert selected.document == tmp_path / ".claude.json"


def test_a_named_home_reads_the_document_inside_it(tmp_path: Path) -> None:
    """Named, the same name sits inside the home rather than beside it."""
    selected = selected_config_home({CLAUDE_CONFIG_DIR: str(tmp_path / "account")})

    assert selected.directory == tmp_path / "account"
    assert selected.document == tmp_path / "account" / ".claude.json"


@pytest.mark.parametrize("named", [True, False])
def test_a_legacy_document_is_read_wherever_one_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool
) -> None:
    """Existence alone decides it, ahead of a current document beside it."""
    operator_at(monkeypatch, tmp_path)
    home = tmp_path / "account" if named else tmp_path / ".claude"
    environment = {CLAUDE_CONFIG_DIR: str(home)} if named else {}
    save_document((home if named else tmp_path) / ".claude.json", {"theme": "light"})
    save_document(home / ".config.json", {})

    assert selected_config_home(environment).document == home / ".config.json"


def test_a_custom_oauth_server_reads_a_document_of_its_own(tmp_path: Path) -> None:
    """The variable selecting the server renames the document, in one place."""
    save_document(tmp_path / ".claude.json", {"theme": "light"})
    environment = {
        CLAUDE_CONFIG_DIR: str(tmp_path),
        CLAUDE_OAUTH_URL_ENV: "https://claude.fedstart.com",
    }

    document = selected_config_home(environment).document

    assert document == tmp_path / ".claude-custom-oauth.json"


def test_a_derived_home_carries_the_document_its_account_reads(
    tmp_path: Path,
) -> None:
    """An account holding only the current document seeds the derived one.

    Seeded from a legacy name the account never held, a derived home starts
    from an empty document — no trusted project, no theme, no onboarding —
    and Claude reads that emptiness rather than anything beside it."""
    home = shared_home(tmp_path)
    workspace = tmp_path / "lease"

    document = session_document({CLAUDE_CONFIG_DIR: str(home)}, workspace)

    assert document.name == ".claude.json"
    assert workspace in document.parents
    assert not document.is_symlink()
    assert load_document(document) == load_document(home / CLAUDE_HOME_DOCUMENT)


def test_a_derived_home_under_no_named_home_carries_the_one_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing named, the account's document is ``~/.claude.json``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    save_document(tmp_path / ".claude.json", {"theme": SEEDED_THEME})
    workspace = tmp_path / "lease"

    document = session_document({}, workspace)

    assert document.name == ".claude.json"
    assert workspace in document.parents
    assert load_document(document)["theme"] == SEEDED_THEME


def test_a_derived_home_is_the_operator_s_whatever_home_a_session_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session's own ``HOME`` is for the tools it runs, and the session still
    authenticates as the operator: with no home named, the account it derives
    from is the operator's, though Claude Code left to choose would join the
    session's ``HOME``."""
    operator = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.delenv(CLAUDE_CONFIG_DIR, raising=False)
    monkeypatch.delenv(CLAUDE_OAUTH_URL_ENV, raising=False)
    login = CLAUDE_LOGIN.model_copy(
        update={"ambient_home": operator / CLAUDE_LOGIN.ambient_home.name}
    )
    monkeypatch.setattr("lup.providers.claude.config_home.CLAUDE_LOGIN", login)
    elsewhere = tmp_path / "elsewhere"
    for user in (operator, elsewhere):
        account = user / CLAUDE_LOGIN.ambient_home.name
        account.mkdir(parents=True)
        credentials = json.dumps({"account": user.name})
        CLAUDE_LOGIN.credentials_path(account).write_text(credentials, encoding="utf-8")
        save_document(user / CLAUDE_HOME_DOCUMENT, {"account": user.name})

    derived = workspace_config_environment({"HOME": str(elsewhere)}, tmp_path / "lease")
    home = Path(derived[CLAUDE_CONFIG_DIR])

    linked = CLAUDE_LOGIN.credentials_path(home).resolve()
    assert linked == login.credentials_path(login.ambient_home).resolve()
    assert load_document(home / CLAUDE_HOME_DOCUMENT) == {"account": "operator"}


def test_a_derived_home_keeps_a_legacy_seed_under_the_current_name(
    tmp_path: Path,
) -> None:
    """An account read from a legacy document seeds from that one instead."""
    home = shared_home(tmp_path, CLAUDE_LEGACY_DOCUMENT)
    save_document(home / CLAUDE_HOME_DOCUMENT, {"theme": "light"})
    workspace = tmp_path / "lease"

    document = session_document({CLAUDE_CONFIG_DIR: str(home)}, workspace)

    assert document.name == ".claude.json"
    assert load_document(document)["theme"] == SEEDED_THEME
    assert not (document.parent / ".config.json").exists()


def test_a_derived_home_keeps_a_custom_oauth_document_of_its_own(
    tmp_path: Path,
) -> None:
    """Under a custom OAuth server the seed and the copy both take its name."""
    home = shared_home(tmp_path, CLAUDE_OAUTH_DOCUMENT)
    environment = {
        CLAUDE_CONFIG_DIR: str(home),
        CLAUDE_OAUTH_URL_ENV: "https://claude.fedstart.com",
    }

    document = session_document(environment, tmp_path / "lease")

    assert document.name == ".claude-custom-oauth.json"
    assert not document.is_symlink()
    assert load_document(document)["theme"] == SEEDED_THEME


def test_an_empty_legacy_document_in_a_derived_home_stops_shadowing(
    tmp_path: Path,
) -> None:
    """A derived home is lup's, so whatever it holds is brought to the layout.

    The home here reads nothing but an empty legacy document, seeded from an
    account that never had one, while its current name links to the account's
    own — which Claude writes through, so the link would share the one file
    every session was given a home to stop sharing. Derived again, it holds
    neither: a document of its own under the current name, seeded from the
    account's."""
    home = shared_home(tmp_path)
    workspace = tmp_path / "lease"
    earlier = SessionHomeLayout(private_files=[CLAUDE_LEGACY_DOCUMENT])
    derived = SessionHomes(home, earlier).derive(workspace)
    save_document(derived / CLAUDE_LEGACY_DOCUMENT, {})
    assert (derived / CLAUDE_HOME_DOCUMENT).is_symlink()

    document = session_document({CLAUDE_CONFIG_DIR: str(home)}, workspace)

    assert document == derived / ".claude.json"
    assert not (derived / ".config.json").exists()
    assert not document.is_symlink()
    assert load_document(document)["theme"] == SEEDED_THEME


def test_trust_is_recorded_in_the_document_a_session_reads(tmp_path: Path) -> None:
    """Trust written anywhere else is trust no session ever sees."""
    home = shared_home(tmp_path)
    workspace = tmp_path / "lease"
    environment = {CLAUDE_CONFIG_DIR: str(home)}

    derived = workspace_config_environment(environment, workspace, trust=True)

    read = selected_config_home({**environment, **derived}).document
    assert trusts(load_document(read), workspace)
    assert SEEDED_PROJECT in project_entries(load_document(read))


def test_an_untrusted_workspace_names_what_it_is_dropping(tmp_path: Path) -> None:
    """Untrusted-and-degraded is stated once rather than warned per session."""
    workspace = tmp_path / "repo"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps(
            {"permissions": {"allow": ["Bash(git status:*)", "Read(//tmp/**)"]}}
        ),
        encoding="utf-8",
    )
    document = tmp_path / CLAUDE_HOME_DOCUMENT
    save_document(document, {})

    message = untrusted_degradation(workspace, document)
    assert message is not None
    assert "2 permissions.allow entries" in message

    record_trust(document, workspace)
    assert untrusted_degradation(workspace, document) is None


def test_a_workspace_declaring_nothing_loses_nothing(tmp_path: Path) -> None:
    document = tmp_path / CLAUDE_HOME_DOCUMENT
    save_document(document, {})

    assert untrusted_degradation(tmp_path / "repo", document) is None


def test_a_truncated_document_is_refused_rather_than_emptied(tmp_path: Path) -> None:
    """``Unexpected EOF`` must not read as a profile trusting nothing."""
    document = tmp_path / CLAUDE_HOME_DOCUMENT
    document.write_text('{"projects": {', encoding="utf-8")

    with pytest.raises(ClaudeConfigUnreadable):
        load_document(document)


def test_an_absent_document_is_not_an_error(tmp_path: Path) -> None:
    assert load_document(tmp_path / CLAUDE_HOME_DOCUMENT) == {}


def test_a_document_behind_a_permission_reads_as_unreadable(tmp_path: Path) -> None:
    """Unopenable and unparseable are one fact to every caller of this.

    A document the session may not open holds the trusted projects exactly as
    a malformed one does, so it owes the same typed answer. Escaping as a bare
    `PermissionError` took out the fault report written to say a run cannot
    open a session anywhere — it crashed instead of saying so.
    """
    document = tmp_path / CLAUDE_HOME_DOCUMENT
    document.write_text('{"projects": {}}', encoding="utf-8")
    document.chmod(0o000)

    try:
        if os.access(document, os.R_OK):
            pytest.skip("this user reads a mode-000 file, so nothing is denied")
        with pytest.raises(ClaudeConfigUnreadable):
            load_document(document)
    finally:
        document.chmod(0o600)


def backup(home: Path, name: str, content: str) -> Path:
    """One file where Claude Code copies a document it could not read."""
    written = home / CLAUDE_BACKUP_DIR / f"{CLAUDE_HOME_DOCUMENT}.backup.{name}"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(content, encoding="utf-8")
    return written


def test_a_backup_that_cannot_restore_is_not_offered(tmp_path: Path) -> None:
    """The hint named a zero-byte file, and following it emptied a healed one."""
    backup(tmp_path, "empty", "")
    backup(tmp_path, "flags", json.dumps({"migrationVersion": 13}))

    assert restorable_backups(tmp_path) == []
    assert "No backup under" in restoration_advice(tmp_path)


def test_the_newest_backup_carrying_projects_is_the_one_named(
    tmp_path: Path,
) -> None:
    """Newest by when it was written, so restoring loses the least."""
    older = backup(tmp_path, "older", json.dumps({"projects": {"/a": {}}}))
    newer = backup(tmp_path, "newer", json.dumps({"projects": {"/b": {}}}))
    backup(tmp_path, "truncated", '{"projects": {')
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    assert restorable_backups(tmp_path) == [newer, older]
    assert str(newer) in restoration_advice(tmp_path)


def test_an_unreadable_document_stops_a_run_before_it_leases(
    tmp_path: Path,
) -> None:
    """Every derived home is seeded from this one, so nothing can start."""
    home = selected_config_home({CLAUDE_CONFIG_DIR: str(tmp_path)})
    home.document.write_text('{"projects": {', encoding="utf-8")
    restorable = backup(tmp_path, "whole", json.dumps({"projects": {"/a": {}}}))

    fault = home.configuration_fault()
    assert fault is not None
    assert "does not parse" in fault
    assert str(restorable) in fault


def test_a_readable_document_is_no_fault(tmp_path: Path) -> None:
    home = selected_config_home({CLAUDE_CONFIG_DIR: str(tmp_path)})
    save_document(home.document, {})

    assert home.configuration_fault() is None


def test_a_home_that_cannot_keep_a_session_environment_says_which_boundary(
    tmp_path: Path,
) -> None:
    """Named up front, rather than found by losing Bash in every worker at once.

    A session keeps each shell call's environment under `session-env` and
    makes its own entry at startup, so a home that refuses a write there
    opens sessions whose every Bash call dies as a read-only filesystem — an
    error naming no boundary, which is then debugged as a broken repository.

    Established by writing rather than by reading a permission or an
    environment variable. The runtime carves this directory out of grants it
    otherwise honours, and the variable saying a sandbox is running is
    inherited by a command placed outside it, so neither tells a carved-out
    path from a granted one.
    """
    home = ClaudeConfigHome(
        directory=tmp_path / "home", document=tmp_path / "home" / CLAUDE_HOME_DOCUMENT
    )
    assert home.shell_fault() is None

    refused = tmp_path / "home" / CLAUDE_SESSION_ENV
    refused.chmod(0o500)
    try:
        fault = home.shell_fault()
    finally:
        refused.chmod(0o700)

    assert fault is not None
    assert CLAUDE_SESSION_ENV in fault
    assert "boundary rather than the repository" in fault
    assert "widening the declaration does not reach it" in fault
