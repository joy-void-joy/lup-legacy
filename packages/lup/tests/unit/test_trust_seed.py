"""Every container start trusts its checkout in one shared document, safely.

Measured on a config volume: two containers started a few milliseconds apart
both ran the entrypoint's merge through one fixed temporary name. The second
truncated it while the first was still writing, the first renamed what was
left over the document, and the next session read 24576 NUL bytes followed by
the back half of its configuration.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lup.harness.assets.trust_seed import main, record_trust
from lup.types import JsonObject


def write(path: Path, value: JsonObject) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def trusted(document: Path, checkout: str) -> bool:
    projects = json.loads(document.read_text(encoding="utf-8"))["projects"]
    return projects[checkout]["hasTrustDialogAccepted"] is True


def seeded(tmp_path: Path) -> Path:
    seed = tmp_path / "trust-seed.json"
    write(seed, {"hasCompletedOnboarding": True, "projects": {}})
    return seed


def test_a_missing_document_starts_from_the_seed(tmp_path: Path) -> None:
    document = tmp_path / ".claude.json"

    assert record_trust(document, seeded(tmp_path), ["/w/tree/dev", "/w"])

    content = json.loads(document.read_text(encoding="utf-8"))
    assert content["hasCompletedOnboarding"] is True
    assert trusted(document, "/w/tree/dev")
    assert trusted(document, "/w")
    assert document.stat().st_mode & 0o777 == 0o600


def test_an_empty_document_starts_from_the_seed(tmp_path: Path) -> None:
    document = tmp_path / ".claude.json"
    document.write_bytes(b"")

    assert record_trust(document, seeded(tmp_path), ["/w"])

    assert trusted(document, "/w")


def test_an_existing_document_is_amended_rather_than_replaced(tmp_path: Path) -> None:
    document = tmp_path / ".claude.json"
    write(
        document,
        {
            "theme": "dark",
            "projects": {"/w": {"allowedTools": ["Read"]}, "/other": {"x": 1}},
        },
    )

    assert record_trust(document, seeded(tmp_path), ["/w", "/w"])

    content = json.loads(document.read_text(encoding="utf-8"))
    assert content["theme"] == "dark"
    assert "hasCompletedOnboarding" not in content
    assert content["projects"]["/w"] == {
        "allowedTools": ["Read"],
        "hasTrustDialogAccepted": True,
    }
    assert content["projects"]["/other"] == {"x": 1}


def test_a_start_whose_trust_is_recorded_writes_nothing(tmp_path: Path) -> None:
    """Nearly every start, and the one that would race a running session's save."""
    document = tmp_path / ".claude.json"
    write(document, {"projects": {"/w": {"hasTrustDialogAccepted": True}}})
    before = document.stat()

    assert not record_trust(document, seeded(tmp_path), ["/w"])

    after = document.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize(
    ("raw", "refusal"),
    [
        (b"\0" * 8 + b'"projects": {}}\n', "does not parse"),
        (b"[]", "no projects object"),
        (b'{"projects": {"/w": null}}', "not an object"),
    ],
)
def test_an_unreadable_document_is_left_for_the_runtime(
    tmp_path: Path, raw: bytes, refusal: str
) -> None:
    """Its own recovery keeps a copy before resetting; a rewrite here would not."""
    document = tmp_path / ".claude.json"
    document.write_bytes(raw)

    with pytest.raises(ValueError, match=refusal):
        record_trust(document, seeded(tmp_path), ["/w"])

    assert document.read_bytes() == raw


def test_a_failure_is_said_and_the_start_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / ".claude.json"
    document.write_text("{", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["trust-seed", str(seeded(tmp_path)), str(document), "/w"]
    )

    main()

    assert "lup: workspace trust was not recorded" in capsys.readouterr().err


def test_concurrent_starts_leave_one_document_trusting_every_checkout(
    tmp_path: Path,
) -> None:
    document = tmp_path / ".claude.json"
    bulk: JsonObject = {
        f"/cached/{number}": {"lastCost": number} for number in range(2000)
    }
    write(document, {"projects": bulk, "numStartups": 7})
    checkouts = [f"/w/tree/{number}" for number in range(24)]

    with ThreadPoolExecutor(max_workers=len(checkouts)) as pool:
        written = list(
            pool.map(
                lambda checkout: record_trust(
                    document, seeded(tmp_path), [checkout, "/w"]
                ),
                checkouts,
            )
        )

    assert all(written)
    content = json.loads(document.read_text(encoding="utf-8"))
    assert content["numStartups"] == 7
    assert len(content["projects"]) == len(bulk) + len(checkouts) + 1
    assert all(trusted(document, checkout) for checkout in [*checkouts, "/w"])
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".claude.json",
        ".lup-trust.lock",
        "trust-seed.json",
    ]
