"""One account's login kept the same in every copy, the newest winning: a renewal in one copy reaches the rest."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lup.channels.models import ChannelConflictError
from lup.devtools.dashboard.logins import LoginKeeper, derived_copies
from lup.launch import container
from lup.launch.config_volume import HomeFile, VolumeLogins
from lup.launch.container import VolumeLoginCopy
from lup.providers.accounts import AccountHome
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.login import ProviderLogin
from lup.providers.login_sync import LoginFile, LoginPlace, carried, profile_lock
from lup.providers.user_config import UserConfigFile
from lup.sessions.limits import Account

LATER = int((datetime.now(UTC) + timedelta(days=30)).timestamp() * 1000)


def login(refresh: str, expires: int, spent: bool = False) -> dict[str, object]:
    """A stored Claude login, in the shape Claude Code 2.1.285 writes it; *spent* holds no refresh token."""
    return {
        "claudeAiOauth": {
            "accessToken": f"access-{refresh}",
            "refreshToken": "" if spent else refresh,
            "expiresAt": expires,
            "refreshTokenExpiresAt": LATER,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
    }


def stored(path: Path, document: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return path


def refresh_of(path: Path) -> str:
    return json.loads(path.read_text())["claudeAiOauth"]["refreshToken"]


def test_a_renewal_in_one_copy_reaches_the_profile_and_every_other_copy(
    tmp_path: Path,
) -> None:
    profile = stored(tmp_path / "profile" / ".credentials.json", login("r0", 1000))
    first = stored(tmp_path / "a" / ".credentials.json", login("r0", 1000))
    second = stored(
        tmp_path / "b" / ".credentials.json",
        {**login("r0", 1000), "mcpOAuth": {"server": "kept"}},
    )
    # Copy A refreshes: its old refresh token is spent, a new one issued.
    stored(first, login("r1", 2000))

    outcome = carried(
        CLAUDE_LOGIN,
        [LoginFile(profile), LoginFile(first), LoginFile(second)],
        profile_lock(CLAUDE_LOGIN, profile.parent),
    )

    assert outcome.newest == str(first)
    assert sorted(outcome.replaced) == sorted([str(profile), str(second)])
    # B's next request reads a refresh token the provider still answers.
    assert refresh_of(second) == "r1" and refresh_of(profile) == "r1"
    assert json.loads(second.read_text())["mcpOAuth"] == {"server": "kept"}
    assert oct(second.stat().st_mode & 0o777) == "0o600"


def test_two_copies_renewed_at_once_end_on_the_later_renewal(tmp_path: Path) -> None:
    profile = stored(tmp_path / "profile" / ".credentials.json", login("r0", 1000))
    first = stored(tmp_path / "a" / ".credentials.json", login("r1", 2000))
    second = stored(tmp_path / "b" / ".credentials.json", login("r2", 3000))
    places: list[LoginPlace] = [LoginFile(first), LoginFile(profile), LoginFile(second)]

    carried(CLAUDE_LOGIN, places, profile_lock(CLAUDE_LOGIN, profile.parent))
    again = carried(CLAUDE_LOGIN, places, profile_lock(CLAUDE_LOGIN, profile.parent))

    assert [refresh_of(each) for each in (profile, first, second)] == ["r2"] * 3
    assert again.replaced == []


class Renewing(LoginPlace):
    """A copy its session renews again between a pass's read and its write."""

    def __init__(self, path: Path) -> None:
        self.file = LoginFile(path)

    def named(self) -> str:
        return self.file.named()

    def read(self) -> bytes | None:
        held = self.file.read()
        stored(self.file.path, login("r9", 9000))
        return held

    def write(self, content: bytes, expected: bytes | None) -> None:
        self.file.write(content, expected)


def test_a_copy_renewed_during_a_pass_keeps_its_renewal(tmp_path: Path) -> None:
    profile = stored(tmp_path / "profile" / ".credentials.json", login("r1", 2000))
    racing = stored(tmp_path / "a" / ".credentials.json", login("r0", 1000))

    outcome = carried(
        CLAUDE_LOGIN,
        [LoginFile(profile), Renewing(racing)],
        profile_lock(CLAUDE_LOGIN, profile.parent),
    )

    assert outcome.moved == [str(racing)] and refresh_of(racing) == "r9"
    with pytest.raises(ChannelConflictError):
        LoginFile(racing).write(b"{}", b"not what it holds")


def test_a_copy_that_cannot_renew_never_wins(tmp_path: Path) -> None:
    profile = stored(tmp_path / "profile" / ".credentials.json", login("r1", 2000))
    dead = stored(tmp_path / "a" / ".credentials.json", login("r5", 5000, spent=True))

    carried(
        CLAUDE_LOGIN,
        [LoginFile(profile), LoginFile(dead)],
        profile_lock(CLAUDE_LOGIN, profile.parent),
    )

    assert refresh_of(profile) == "r1" and refresh_of(dead) == "r1"


def test_the_keeper_finds_each_home_derived_from_a_profile_and_keeps_it(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profiles" / "work" / "claude-config"
    stored(profile / ".credentials.json", login("r0", 1000))
    (profile / "settings.json").write_text("{}")
    checkout = tmp_path / "checkout"
    derived = checkout / ".lup" / "sessions" / "checkout-abc"
    derived.mkdir(parents=True)
    (derived / "settings.json").symlink_to(profile / "settings.json")
    stored(derived / ".credentials.json", login("r1", 2000))
    elsewhere = checkout / ".lup" / "sessions" / "other-def"
    elsewhere.mkdir(parents=True)
    stored(elsewhere / ".credentials.json", login("rx", 9000))

    assert derived_copies(checkout, CLAUDE_LOGIN, profile) == [
        derived / ".credentials.json"
    ]
    keeper = LoginKeeper(
        lambda: [checkout],
        UserConfigFile(tmp_path / "config"),
        homes=lambda roots: [
            AccountHome(
                account=Account(runtime="claude", profile="work"),
                home=profile,
                signed_in=True,
            )
        ],
        volumes=VolumeLogins(tmp_path / "volumes"),
    )

    keeper.keep(volumes=False)

    assert refresh_of(profile / ".credentials.json") == "r1"
    assert refresh_of(elsewhere / ".credentials.json") == "rx"


class Helper:
    """A helper container standing in for one volume: what it holds, and what it was handed."""

    def __init__(self, held: bytes) -> None:
        self.held = held
        self.handed: list[Path] = []

    def read(self, volume: str, names: list[str]) -> list[HomeFile]:
        del volume
        return [HomeFile(name=names[0], content=self.held)]

    def seed_login(self, volume: str, credential: Path, login: ProviderLogin) -> str:
        del volume, login
        self.handed.append(credential)
        self.held = credential.read_bytes()
        return ""


def test_a_volume_copy_is_read_and_handed_a_newer_login_through_a_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = Helper(json.dumps(login("r0", 1000)).encode())
    monkeypatch.setattr(container, "volume_helper", lambda root, volume, home: helper)
    monkeypatch.setattr(
        container, "state_volume_name", lambda root, login: "lup-claude-repo"
    )
    offered = tmp_path / "state" / "logins" / ".lup-claude-repo.json"
    profile = stored(tmp_path / "profile" / ".credentials.json", login("r1", 2000))

    carried(
        CLAUDE_LOGIN,
        [LoginFile(profile), VolumeLoginCopy(tmp_path, CLAUDE_LOGIN, "/cfg", offered)],
        profile_lock(CLAUDE_LOGIN, profile.parent),
    )

    assert json.loads(helper.held)["claudeAiOauth"]["refreshToken"] == "r1"
    assert helper.handed == [offered] and not offered.exists()
