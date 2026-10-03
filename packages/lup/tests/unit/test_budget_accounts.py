"""Every account the budget meters, and what a Codex rollout says one request moved."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lup.providers.accounts import account_homes, transcript_spend
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.usage.reader import rollout_spend
from lup.providers.user_config import UserConfigFile
from lup.types import JsonObject


def signed_in(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    CLAUDE_LOGIN.credentials_path(home).write_text("{}", encoding="utf-8")
    return home


def test_every_profile_is_an_account_per_runtime_and_a_shared_one_is_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ambient-codex"))
    config = UserConfigFile(tmp_path / "lup")
    work = signed_in(config.profiles_root() / "work" / CLAUDE_LOGIN.home_subdir)
    first, second = tmp_path / "first", tmp_path / "second"
    signed_in(first / ".lup" / "profiles" / "work" / CLAUDE_LOGIN.home_subdir)
    homes = account_homes([first, second], config)
    found = {(each.account.key, each.signed_in) for each in homes}
    assert ("claude:default", False) in found
    assert ("claude:work", True) in found
    assert ("claude:work@first", True) in found
    assert ("codex:default", False) in found
    assert [each.home for each in homes if each.account.key == "claude:work"] == [work]
    assert len([each for each in homes if each.account.key == "claude:work"]) == 1
    assert ("codex:work", False) in found
    codex_work = [each.home for each in homes if each.account.key == "codex:work"]
    assert codex_work == [config.profiles_root() / "work" / CODEX_LOGIN.home_subdir]


def token_count(tokens: int, used: float, resets_at: int) -> JsonObject:
    """A ``token_count`` line as Codex 0.159.2 persists one (protocol.rs, ``TokenCountEvent``)."""
    return {
        "timestamp": "2026-10-05T12:00:00.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"total_tokens": tokens * 3},
                "last_token_usage": {"total_tokens": tokens},
                "model_context_window": 272000,
            },
            "rate_limits": {
                "limit_id": "codex",
                "primary": {
                    "used_percent": used,
                    "window_minutes": 300,
                    "resets_at": resets_at,
                },
                "secondary": {
                    "used_percent": 7.0,
                    "window_minutes": 10080,
                    "resets_at": resets_at + 86400,
                },
                "credits": None,
                "plan_type": "pro",
            },
        },
    }


def test_a_token_count_says_what_its_request_moved_and_the_windows_then() -> None:
    resets = int(datetime(2026, 10, 5, 15, tzinfo=UTC).timestamp())
    spent = rollout_spend(token_count(1234, 41.5, resets))
    assert spent is not None
    assert spent.tokens == 1234
    assert [
        (each.label, each.utilization_pct, each.window_hours) for each in spent.windows
    ] == [
        ("weekly", 7.0, 168.0),
        ("5-hour", 41.5, 5.0),
    ]
    assert transcript_spend("codex", token_count(5, 1.0, resets)) is not None


def test_other_lines_and_other_runtimes_say_nothing() -> None:
    assert (
        rollout_spend({"type": "response_item", "payload": {"type": "message"}}) is None
    )
    assert (
        rollout_spend({"type": "event_msg", "payload": ["not", "an", "object"]}) is None
    )
    no_info: JsonObject = {
        "type": "event_msg",
        "payload": {"type": "token_count", "info": None, "rate_limits": None},
    }
    spent = rollout_spend(no_info)
    assert spent is not None and (spent.tokens, spent.windows) == (0, [])
    assert transcript_spend("claude", {"type": "assistant"}) is None
