"""A person's own dashboard keys: rebound by action name, checked against the one catalog the page runs.

Each entry of ``[dashboard.keys]`` is checked on its own, so a bad one refuses
only itself and says why — what was written, why it cannot apply, and the way
through — while the rest apply. A conflict refuses the override that made it
and never shadows lup's own key. The page is handed the keys in effect on the
stream's first frame and again whenever the file changes, and a tab's `:map`
lines are checked by the same code its config is.
"""

import re
from pathlib import Path

import pytest
import typer
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.keys import (
    DashboardKeys,
    KeyLine,
    KeymapCatalog,
    KeyNotationError,
    effective,
    keymap_json,
    read_keys,
)
from lup.devtools.dashboard.reviews import (
    ReviewStore,
    create_operator_dashboard_app,
    dashboard_app,
)
from lup.devtools.dashboard.supervision import SUPERVISED
from lup.devtools.dashboard.stream import KeysEvent, LiveFeed, Observation
from lup.providers.user_config import UserConfig, UserConfigFile

BASE_URL = "http://127.0.0.1:8766"
TOKEN = "operator-capability"
POSTING = {"Authorization": f"Bearer {TOKEN}", "Origin": BASE_URL}
CATALOG = KeymapCatalog()


def test_lup_s_own_keys_read_as_written_and_conflict_nowhere() -> None:
    for action in CATALOG.actions:
        for keys in action.keys:
            assert read_keys(keys, CATALOG) == keys, action.name
    plain = effective({})
    assert plain.report.refused == []
    assert plain.changed == []


def test_every_action_named_beside_another_exists_in_the_catalog() -> None:
    names = {action.name for action in CATALOG.actions}
    assert len(names) == len(CATALOG.actions)
    assert {also for action in CATALOG.actions for also in action.also} <= names
    assert {action.group for action in CATALOG.actions} <= {
        group.id for group in CATALOG.groups
    }


@pytest.mark.parametrize(
    ("written", "says"),
    [
        ("<Ctl-d>", "modifiers are C- (Ctrl), A- (Alt) and S- (Shift)"),
        ("<C-Return>", "no key is called Return"),
        ("<A-x>", "only Ctrl combines with one, as <C-x>"),
        ("<S-Enter>", "Shift alone does not change Enter"),
        ("<C-n>", "Firefox keeps it for itself (it opens a window)"),
        ("", "[] unbinds an action"),
    ],
)
def test_a_key_that_does_not_read_says_exactly_what_is_wrong(
    written: str, says: str
) -> None:
    with pytest.raises(KeyNotationError, match=re.escape(says)):
        read_keys(written, CATALOG)


def test_a_key_is_spelled_the_page_s_way_whatever_the_case_or_order_written() -> None:
    assert read_keys("<A-C-Up>", CATALOG) == "<C-A-Up>"
    assert read_keys("<C-D>", CATALOG) == "<C-d>"
    assert read_keys("<Space>fa", CATALOG) == "<leader>fa"
    assert read_keys(" am", CATALOG) == "<leader>am"
    assert read_keys("<A-Right>", CATALOG) == "<A-Right>"


def test_each_entry_applies_or_is_refused_alone_with_what_why_and_the_way_through() -> (
    None
):
    read = effective(
        {
            "agent.next": ["<A-Right>", ")"],
            "file.next": ["]", "gf"],
            "tree.stopped": [],
            "marker.nxt": ["m"],
            "page.halfdown": ["<Ctl-d>"],
            "answer.approve": ["a"],
            "box.next": ["gj"],
        }
    )
    applied = {entry.action: entry.keys for entry in read.report.applied}
    assert applied == {
        "agent.next": ["<A-Right>", ")"],
        "file.next": ["]", "gf"],
        "tree.stopped": [],
    }
    refused = {entry.action: entry for entry in read.report.refused}
    assert refused["marker.nxt"].why == "no action has that name"
    assert refused["marker.nxt"].way == "did you mean marker.next?"
    assert "modifiers are" in refused["page.halfdown"].why
    assert "works while you type" in refused["answer.approve"].why
    assert "one keystroke at a time" in refused["box.next"].why
    assert {entry.action for entry in read.changed} == {
        "agent.next",
        "file.next",
        "tree.stopped",
    }


def test_a_key_that_acts_while_you_type_is_one_that_never_types() -> None:
    read = effective(
        {
            "focus.next": ["<F2>"],
            "focus.previous": ["<S-Tab>", "<C-j>"],
            "answer.send": ["s"],
        }
    )
    assert [entry.action for entry in read.report.refused] == ["answer.send"]
    assert "held with Ctrl or Alt (like <C-Enter>), Tab, or a function key" in (
        read.report.refused[0].why
    )


def test_an_editor_motion_meets_every_key_acting_wherever_focus_is() -> None:
    [refusal] = effective({"word.next": ["x"]}).report.refused
    assert refusal.why == "it is delete's where both act"
    assert CATALOG.action("word.next") is not None
    assert {action.focus for action in CATALOG.actions} == {"any", "buffer"}


def test_a_conflict_refuses_the_override_that_made_it_and_lup_s_key_stands() -> None:
    read = effective({"exception.next": ["x"]})
    [refusal] = read.report.refused
    assert refusal.action == "exception.next"
    assert refusal.why == "it is delete's where both act"
    assert refusal.way == 'unbind it there ("delete" = []) or pick another key'
    assert read.changed == []


def test_two_overrides_on_one_key_refuse_the_later_one_and_unbinding_frees_the_key() -> (
    None
):
    later = effective({"hover": ["Q"], "help": ["Q"]})
    assert [entry.action for entry in later.report.refused] == ["help"]
    freed = effective({"delete": [], "exception.next": ["x"]})
    assert freed.report.refused == []
    assert {entry.action: entry.keys for entry in freed.changed} == {
        "delete": [],
        "exception.next": ["x"],
    }


def test_keys_in_places_that_never_meet_do_not_conflict() -> None:
    read = effective({"inbox.readall": ["n"]})
    assert read.report.refused == []


def test_a_key_starting_a_longer_one_where_both_act_is_a_wait_not_a_refusal() -> None:
    read = effective({"file.whole": ["g"]})
    assert read.report.refused == []
    assert any(
        "g (file.whole) also starts gd (definition)" in wait
        for wait in read.report.waits
    )


def test_a_tab_maps_and_unmaps_over_the_person_s_own() -> None:
    read = effective(
        {"agent.next": ["<A-Right>"]},
        [KeyLine(action="search.next", keys=["ü"]), KeyLine(keys=["("])],
    )
    origins = {entry.action: (entry.keys, entry.origin) for entry in read.changed}
    assert origins["agent.next"] == (["<A-Right>"], "config")
    assert origins["search.next"] == (["ü"], "tab")
    assert origins["agent.previous"] == ([], "tab")
    nothing = effective({}, [KeyLine(keys=["ö"])])
    assert nothing.report.refused[0].why == "no action runs on it"


def test_the_config_takes_a_key_or_a_list_and_refuses_another_shape(
    tmp_path: Path,
) -> None:
    config = UserConfigFile(home=tmp_path)
    config.path().write_text(
        '[dashboard.keys]\n"help" = "<F1>"\n"agent.next" = [")", "<A-Right>"]\n',
        encoding="utf-8",
    )
    assert config.load().dashboard.keys == {
        "help": ["<F1>"],
        "agent.next": [")", "<A-Right>"],
    }
    config.path().write_text('[dashboard.keys]\n"help" = 3\n', encoding="utf-8")
    with pytest.raises(ValueError, match="holds a setting lup cannot read"):
        config.load()
    assert UserConfig().dashboard.keys == {}


def test_the_reader_reads_again_only_once_the_file_moves_and_says_why_it_cannot(
    tmp_path: Path,
) -> None:
    config = UserConfigFile(home=tmp_path)
    keys = DashboardKeys(config)
    assert keys().changed == []
    assert keys().source == ""
    config.path().write_text('[dashboard.keys]\n"help" = "<F1>"\n', encoding="utf-8")
    read = keys()
    assert read.source == str(config.path())
    assert [entry.keys for entry in read.changed] == [["<F1>"]]
    assert keys() is read
    config.path().write_text("[dashboard\n", encoding="utf-8")
    broken = keys()
    assert "is not valid TOML" in broken.unread
    assert broken.changed == []


def test_the_stream_hands_the_keys_over_whole_and_again_when_the_config_changes(
    tmp_path: Path,
) -> None:
    config = UserConfigFile(home=tmp_path)
    reader = DashboardKeys(config)
    root = tmp_path / "repo"
    root.mkdir()
    feed = LiveFeed(
        lambda: [KnownRepository(repository=root, checkout=root)],
        ReviewStore(roots=(root,)),
        keys=reader,
    )
    feed.state.observed(feed.observe())
    assert feed.state.snapshot().keys == reader()
    config.path().write_text('[dashboard.keys]\n"help" = "<F1>"\n', encoding="utf-8")
    moved = feed.state.observed(
        Observation(repositories=[], sessions=[], messages=[], keys=reader())
    )
    [keys] = [event for event in moved if isinstance(event, KeysEvent)]
    assert [entry.action for entry in keys.keys.changed] == ["help"]
    assert feed.state.snapshot().keys == keys.keys


def client(root: Path, config: UserConfigFile) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(
            app=dashboard_app(BASE_URL, TOKEN, (root,), config=config)
        ),
        base_url=BASE_URL,
    )


async def test_a_tab_s_lines_are_checked_by_the_server_as_the_config_is(
    tmp_path: Path,
) -> None:
    config = UserConfigFile(home=tmp_path / "config")
    async with client(tmp_path, config) as http:
        tried = await http.post(
            "/api/keys/try",
            headers=POSTING,
            json={"lines": [{"action": "search.next", "keys": ["n"]}]},
        )
        refused = await http.post(
            "/api/keys/try",
            json={"lines": []},
            headers={"Origin": BASE_URL},
        )
    assert tried.status_code == 200
    [refusal] = tried.json()["report"]["refused"]
    assert refusal["why"] == "it is exception.next's where both act"
    assert refused.status_code == 401


async def test_mapwrite_writes_the_tab_s_lines_into_the_config_keeping_its_comments(
    tmp_path: Path,
) -> None:
    config = UserConfigFile(home=tmp_path / "config")
    config.home.mkdir()
    config.path().write_text(
        "# my own keys\n[dashboard]\nreopen = false  # quiet, please\n",
        encoding="utf-8",
    )
    async with client(tmp_path, config) as http:
        written = await http.post(
            "/api/keys",
            headers=POSTING,
            json={"lines": [{"action": "agent.next", "keys": ["<A-Right>", ")"]}]},
        )
    assert written.status_code == 200
    text = config.path().read_text(encoding="utf-8")
    assert "# my own keys" in text and "# quiet, please" in text
    assert config.load().dashboard.keys == {"agent.next": ["<A-Right>", ")"]}
    assert [entry["action"] for entry in written.json()["changed"]] == ["agent.next"]


def test_dashboard_keys_prints_the_keys_in_effect_and_every_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "lup").mkdir()
    (tmp_path / "lup" / "config.toml").write_text(
        '[dashboard.keys]\n"help" = "<F1>"\n"marker.nxt" = "m"\n', encoding="utf-8"
    )
    cli = typer.Typer()
    cli.add_typer(create_operator_dashboard_app(tmp_path), name="dashboard")
    result = CliRunner().invoke(cli, ["dashboard", "keys"])
    assert result.exit_code == 0, result.output
    assert (
        "help" in result.output
        and "F1  (yours; lup's is ?, <leader>?)" in result.output
    )
    assert (
        "refused marker.nxt: `marker.nxt` no action has that name — did you mean marker.next?"
        in result.output
    )


def test_the_keymap_the_page_compiles_in_is_the_catalog_whole() -> None:
    assert KeymapCatalog.model_validate_json(keymap_json()) == CATALOG


def test_every_supervising_action_needs_what_the_routes_serve() -> None:
    """A page meeting this dashboard runs every action the catalog marks as needing supervision."""
    needed = {action.needs for action in CATALOG.actions if action.needs is not None}

    # A post into a discussion is the box's own send there, no action of its own;
    # the budget's actions need what the budget's own routes serve.
    assert needed == (set(SUPERVISED) - {"thread-post"}) | {"budgets"}
