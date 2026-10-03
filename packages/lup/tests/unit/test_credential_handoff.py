"""A host login is seeded once per change without clobbering private renewals."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lup.harness.assets.credential_seed import seed_login, stamp_of
from lup.launch.config_volume import HandedLogin, LoginOwner
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.login import ProviderLogin
from lup.types import JsonObject


def write(path: Path, value: JsonObject) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_changed_host_login_replaces_saved_login_but_unchanged_seed_does_not(
    tmp_path: Path,
) -> None:
    source, stored = tmp_path / "host.json", tmp_path / "auth.json"
    write(source, {"tokens": {"access_token": "host-one"}})
    write(stored, {"tokens": {"access_token": "stale-container"}})

    assert seed_login(source, stored)
    assert stored.read_bytes() == source.read_bytes()
    write(stored, {"tokens": {"access_token": "private-renewal"}})
    assert not seed_login(source, stored)
    assert json.loads(stored.read_text())["tokens"]["access_token"] == "private-renewal"
    write(source, {"tokens": {"access_token": "host-two"}})
    assert seed_login(source, stored)
    assert json.loads(stored.read_text())["tokens"]["access_token"] == "host-two"
    assert stored.stat().st_mode & 0o777 == 0o600


def test_only_login_fields_change_and_only_the_login_affects_the_fingerprint(
    tmp_path: Path,
) -> None:
    source, stored = tmp_path / "host.json", tmp_path / ".credentials.json"
    write(source, {"claudeAiOauth": {"token": "host"}, "mcpOAuth": {"host": "private"}})
    write(
        stored, {"claudeAiOauth": {"token": "old"}, "mcpOAuth": {"container": "kept"}}
    )

    assert seed_login(source, stored, keys=["claudeAiOauth"])
    assert json.loads(stored.read_text())["mcpOAuth"] == {"container": "kept"}
    write(
        stored,
        {"claudeAiOauth": {"token": "renewed"}, "mcpOAuth": {"container": "kept"}},
    )
    write(source, {"claudeAiOauth": {"token": "host"}, "mcpOAuth": {"host": "changed"}})
    assert not seed_login(source, stored, keys=["claudeAiOauth"])
    assert json.loads(stored.read_text())["claudeAiOauth"] == {"token": "renewed"}


@pytest.mark.parametrize("payload", [{}, {"other": "not a login"}])
def test_missing_selected_login_does_not_replace_the_container(
    tmp_path: Path,
    payload: JsonObject,
) -> None:
    source, stored = tmp_path / "host.json", tmp_path / "auth.json"
    write(source, payload)
    write(stored, {"claudeAiOauth": {"token": "kept"}})
    before = stored.read_bytes()

    assert not seed_login(source, stored, keys=["claudeAiOauth"])
    assert stored.read_bytes() == before


def test_missing_or_unrenewable_seed_preserves_private_login(tmp_path: Path) -> None:
    source, stored = tmp_path / "host.json", tmp_path / "auth.json"
    write(stored, {"token": "kept"})
    assert not seed_login(source, stored)
    write(source, {"token": "expired"})
    assert not seed_login(source, stored, renewable="false")
    assert json.loads(stored.read_text()) == {"token": "kept"}


def test_corrupt_credential_is_not_overwritten_or_printed(tmp_path: Path) -> None:
    source, stored = tmp_path / "host.json", tmp_path / "auth.json"
    write(source, {"token": "host"})
    stored.write_text("secret malformed content")

    with pytest.raises(ValueError, match="credential JSON") as failure:
        seed_login(source, stored)

    assert "secret" not in str(failure.value)
    assert stored.read_text() == "secret malformed content"


def test_first_shared_file_seed_contains_only_selected_login(tmp_path: Path) -> None:
    source, stored = tmp_path / "host.json", tmp_path / "auth.json"
    write(source, {"claudeAiOauth": {"token": "host"}, "mcpOAuth": {"host": "private"}})
    assert seed_login(source, stored, keys=CLAUDE_LOGIN.credential_fields)
    assert json.loads(stored.read_text()) == {"claudeAiOauth": {"token": "host"}}
    assert CODEX_LOGIN.credential_fields == []


def test_ordering_changes_do_not_replace_private_renewals(tmp_path: Path) -> None:
    source, stored = tmp_path / "host.json", tmp_path / "auth.json"
    write(source, {"access": "host", "refresh": "host-refresh"})
    assert seed_login(source, stored)
    write(stored, {"access": "renewed", "refresh": "renewed-refresh"})
    write(source, {"refresh": "host-refresh", "access": "host"})
    assert not seed_login(source, stored)
    assert json.loads(stored.read_text())["access"] == "renewed"


def test_same_seed_recovers_an_empty_saved_login(tmp_path: Path) -> None:
    source, stored = tmp_path / "host.json", tmp_path / "auth.json"
    write(source, {"token": "host"})
    assert seed_login(source, stored)
    stored.write_bytes(b"")
    assert seed_login(source, stored)
    assert stored.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("login", [CLAUDE_LOGIN, CODEX_LOGIN])
def test_the_host_reads_the_fingerprint_the_seed_stamps(
    tmp_path: Path, login: ProviderLogin
) -> None:
    """What a launch records of a handed login is what the volume's stamp says."""
    source, stored = tmp_path / "host.json", tmp_path / login.credentials_file
    write(source, {"claudeAiOauth": {"token": "host"}, "tokens": {"id": "host"}})
    handed = HandedLogin(credential=source, owner=LoginOwner(home=tmp_path))

    assert seed_login(source, stored, keys=login.credential_fields)
    assert stamp_of(stored).read_text() == handed.fingerprint(login)
    write(source, {})
    assert handed.fingerprint(login) == ""


def test_concurrent_launches_seed_one_atomic_login(tmp_path: Path) -> None:
    source, stored = tmp_path / "host.json", tmp_path / "auth.json"
    write(source, {"token": "host"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        calls = [pool.submit(seed_login, source, stored) for _ in range(8)]
        assert sum(call.result() for call in calls) == 1
    assert stored.read_bytes() == source.read_bytes()
