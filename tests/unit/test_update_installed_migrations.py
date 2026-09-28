"""An updater's already-imported catalog cannot describe the newly installed one."""

from pathlib import Path

import pytest

from lup.devtools.dev import migrations, update


def test_update_reads_migrations_from_a_fresh_installed_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    installed = migrations.unapplied(
        migrations.MigrationRecord().declared(), "HEAD", root
    )
    expected = (
        [f"{len(installed)} migration(s) pending:", *migrations.rendered(installed)]
        if installed
        else []
    )
    stale = migrations.Migration(
        subjects=["stale-parent-only"],
        reason="This process predates the install.",
        steps=[],
    )
    monkeypatch.setattr(migrations.MigrationRecord, "declared", lambda _record: [stale])

    lines = update.owed_since("HEAD", root, lambda _line: None, root)

    assert lines == expected
    assert all("stale-parent-only" not in line for line in lines)


def test_first_adoption_owes_no_migration_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(update, "uv", None)

    assert update.owed_since("", tmp_path, lambda _line: None, tmp_path) == []
