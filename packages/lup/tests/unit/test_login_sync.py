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
from lup.providers.login_sync import (
    LoginCopy,
    LoginFile,
    LoginPlace,
    carried,
    profile_lock,
)
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


def stored(path: Path, document: dict[str, object], account: str = "first") -> Path:
    """A login kept at *path*, its home's account document saying it is *account*'s."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    (path.parent / ".claude.json").write_text(json.dumps(signed_in(account)))
    return path


def signed_in(account: str) -> dict[str, object]:
    """An account document, in the shape Claude Code 2.1.285 writes it at sign-in."""
    return {
        "oauthAccount": {
            "accountUuid": f"uuid-{account}",
            "emailAddress": f"{account}@example.com",
        }
    }


def held(path: Path) -> LoginFile:
    """The copy kept at *path*, its account document beside it."""
    return LoginFile(path, path.parent / ".claude.json")


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
        [held(profile), held(first), held(second)],
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
    places: list[LoginPlace] = [held(first), held(profile), held(second)]

    carried(CLAUDE_LOGIN, places, profile_lock(CLAUDE_LOGIN, profile.parent))
    again = carried(CLAUDE_LOGIN, places, profile_lock(CLAUDE_LOGIN, profile.parent))

    assert [refresh_of(each) for each in (profile, first, second)] == ["r2"] * 3
    assert again.replaced == []


class Renewing(LoginPlace):
    """A copy its session renews again between a pass's read and its write."""

    def __init__(self, path: Path) -> None:
        self.file = held(path)

    def named(self) -> str:
        return self.file.named()

    def read(self) -> LoginCopy:
        copy = self.file.read()
        stored(self.file.path, login("r9", 9000))
        return copy

    def write(self, content: bytes, expected: bytes | None) -> None:
        self.file.write(content, expected)


def test_a_copy_renewed_during_a_pass_keeps_its_renewal(tmp_path: Path) -> None:
    profile = stored(tmp_path / "profile" / ".credentials.json", login("r1", 2000))
    racing = stored(tmp_path / "a" / ".credentials.json", login("r0", 1000))

    outcome = carried(
        CLAUDE_LOGIN,
        [held(profile), Renewing(racing)],
        profile_lock(CLAUDE_LOGIN, profile.parent),
    )

    assert outcome.moved == [str(racing)] and refresh_of(racing) == "r9"
    with pytest.raises(ChannelConflictError):
        held(racing).write(b"{}", b"not what it holds")


def test_a_copy_that_cannot_renew_never_wins(tmp_path: Path) -> None:
    profile = stored(tmp_path / "profile" / ".credentials.json", login("r1", 2000))
    dead = stored(tmp_path / "a" / ".credentials.json", login("r5", 5000, spent=True))

    carried(
        CLAUDE_LOGIN,
        [held(profile), held(dead)],
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

    def __init__(self, held: bytes, account: str = "first") -> None:
        self.held = held
        self.account = json.dumps(signed_in(account)).encode()
        self.handed: list[Path] = []

    def read(self, volume: str, names: list[str]) -> list[HomeFile]:
        del volume
        kept = {".credentials.json": self.held, ".claude.json": self.account}
        return [
            HomeFile(name=name, content=kept[name]) for name in names if name in kept
        ]

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
        [held(profile), VolumeLoginCopy(tmp_path, CLAUDE_LOGIN, "/cfg", offered)],
        profile_lock(CLAUDE_LOGIN, profile.parent),
    )

    assert json.loads(helper.held)["claudeAiOauth"]["refreshToken"] == "r1"
    assert helper.handed == [offered] and not offered.exists()


def test_a_copy_someone_signed_in_to_another_account_from_is_never_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = stored(tmp_path / "profile" / ".credentials.json", login("r0", 1000))
    renewed = stored(tmp_path / "a" / ".credentials.json", login("r1", 2000))
    # A session's `/login` to the second account: a newer login, another account.
    switched = stored(
        tmp_path / "b" / ".credentials.json", login("s9", 9000), account="second"
    )
    helper = Helper(json.dumps(login("s8", 8000)).encode(), account="second")
    monkeypatch.setattr(container, "volume_helper", lambda root, volume, home: helper)
    monkeypatch.setattr(
        container, "state_volume_name", lambda root, login: "lup-claude-repo"
    )
    volume = VolumeLoginCopy(
        tmp_path, CLAUDE_LOGIN, "/cfg", tmp_path / "state" / ".offered.json"
    )

    outcome = carried(
        CLAUDE_LOGIN,
        [held(profile), held(renewed), held(switched), volume],
        profile_lock(CLAUDE_LOGIN, profile.parent),
    )

    assert refresh_of(profile) == "r1", "the profile's own account still carries"
    assert refresh_of(switched) == "s9" and helper.handed == []
    assert json.loads(helper.held)["claudeAiOauth"]["refreshToken"] == "s8"
    assert outcome.switched == [str(switched), "volume lup-claude-repo"]


def test_a_profile_that_says_no_account_carries_nothing(tmp_path: Path) -> None:
    profile = stored(tmp_path / "profile" / ".credentials.json", login("r0", 1000))
    (profile.parent / ".claude.json").unlink()
    renewed = stored(tmp_path / "a" / ".credentials.json", login("r1", 2000))

    outcome = carried(
        CLAUDE_LOGIN,
        [held(profile), held(renewed)],
        profile_lock(CLAUDE_LOGIN, profile.parent),
    )

    assert refresh_of(profile) == "r0" and "says no account" in outcome.unknown
