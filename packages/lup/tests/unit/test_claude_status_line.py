"""A companion's status line is what a launched Claude Code session shows under its prompt.

The dashboard contributes one — how many reviews wait, and where — and Claude
Code draws it from its `statusLine` setting, run again every few seconds so a
review parked by another session shows while this one is idle. It is lup's
answer, so it is the last: a status line the person's account, their lup
config or the project already names is theirs, and stays.
"""

import json
from pathlib import Path

import pytest

from lup.coordination.identity import LaunchedMember
from lup.launch.companions import Contribution, Joined, StatusLine
from lup.launch.refusal import LaunchRefused
from lup.providers.claude import Claude
from lup.providers.claude.config_home import ClaudeConfigHome
from lup.providers.claude.launch import claude_arguments, claude_status_line
from lup.providers.user_config import UserConfig, UserRuntimeSettings
from lup.types import JsonObject

MEMBER = LaunchedMember(member_id="member-1", cli_name="reviewer")
SHOWN = StatusLine(argv=["uv", "run", "lup-devtools", "dashboard", "line", "/p.json"])
THEIRS: JsonObject = {"type": "command", "command": "their-status"}


@pytest.fixture
def account(tmp_path: Path) -> ClaudeConfigHome:
    directory = tmp_path / "account"
    directory.mkdir()
    return ClaudeConfigHome(directory=directory, document=tmp_path / ".claude.json")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    checkout = tmp_path / "project"
    (checkout / ".claude").mkdir(parents=True)
    return checkout


def test_where_nobody_named_one_the_companion_s_line_is_shown(
    root: Path, account: ClaudeConfigHome
) -> None:
    shown = claude_status_line(SHOWN, root, account, UserConfig())

    assert shown == {
        "statusLine": {
            "type": "command",
            "command": "uv run lup-devtools dashboard line /p.json",
            "refreshInterval": 15,
        }
    }
    words = claude_arguments(Claude(), MEMBER, None, [], shown=shown)
    settings = json.loads(words[words.index("--settings") + 1])
    assert settings["statusLine"] == shown["statusLine"]


@pytest.mark.parametrize("named_by", ["account", "person", "project", "local"])
def test_a_status_line_somebody_else_named_stays_theirs(
    root: Path, account: ClaudeConfigHome, named_by: str
) -> None:
    held = json.dumps({"statusLine": THEIRS})
    match named_by:
        case "account":
            (account.directory / "settings.json").write_text(held)
        case "project":
            (root / ".claude" / "settings.json").write_text(held)
        case "local":
            (root / ".claude" / "settings.local.json").write_text(held)
    personal = (
        UserConfig(claude=UserRuntimeSettings(settings={"statusLine": THEIRS}))
        if named_by == "person"
        else UserConfig()
    )

    assert claude_status_line(SHOWN, root, account, personal) == {}


def test_no_companion_line_leaves_the_settings_without_one(
    root: Path, account: ClaudeConfigHome
) -> None:
    assert claude_status_line(None, root, account, UserConfig()) == {}
    words = claude_arguments(Claude(), MEMBER, None, [])
    assert "statusLine" not in json.loads(words[words.index("--settings") + 1])


def test_two_companions_claiming_the_status_line_are_refused() -> None:
    with pytest.raises(LaunchRefused, match="status line"):
        Joined.of(
            {
                "first": Contribution(status_line=SHOWN),
                "second": Contribution(status_line=SHOWN),
            }
        )
