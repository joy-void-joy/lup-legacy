"""Installed migration reports remain readable throughout conflicted updates."""

import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from lup.devtools import entrypoint
from lup.devtools.dev import migrations, update
from lup.devtools.dev.scaffold import adopt, merging
from lup.execution.shell import git
from tests.unit.test_ledger_placement import committed, repository
from tests.unit.test_scaffold import (
    PACKAGE,
    SOURCE,
    adopter_from,
    upstream_at_base,
    wrote,
)
from tests.unit.test_update_resumption import DECLINING


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("explicit_repository", [False, True])
def test_pending_entrypoint_preserves_repository_and_output_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
    pending: bool,
    explicit_repository: bool,
) -> None:
    upstream = repository(tmp_path / "upstream")
    wrote(upstream, "one.py", "value = 1\n")
    committed(upstream, "base")
    base = git.out("-C", str(upstream), "rev-parse", "HEAD")
    wrote(upstream, "one.py", "value = 2\n")
    committed(upstream, "migration")
    landed = git.out("-C", str(upstream), "rev-parse", "HEAD")
    migration = migrations.Migration(
        subjects=["example"],
        reason="The example changed.",
        steps=[migrations.MigrationStep(instruction="Choose the replacement.")],
        commit=landed,
    )
    monkeypatch.setattr(migrations.MigrationRecord, "declared", lambda _: [migration])
    monkeypatch.chdir(tmp_path if explicit_repository else upstream)
    revision = base if pending else landed
    words = ["lup-devtools", "dev", "migrate", "pending", revision]
    if explicit_repository:
        words.extend(["--repository", str(upstream)])
    if as_json:
        words.append("--json")
    monkeypatch.setattr(sys, "argv", words)

    def project_application() -> typer.Typer:
        raise AssertionError("a library report imported the project application")

    monkeypatch.setattr(entrypoint, "project_application", project_application)
    with pytest.raises(SystemExit) as completed:
        entrypoint.main()
    assert completed.value.code == 0
    output = capsys.readouterr().out
    expected = [migration] if pending else []
    if as_json:
        assert migrations.RenderedMigrations.model_validate_json(output) == (
            migrations.RenderedMigrations(
                count=len(expected), lines=migrations.rendered(expected)
            )
        )
    elif pending:
        assert output == f"1 migration(s) since {revision}:\n" + "".join(
            f"  {line}\n" for line in migration.spelled()
        )
    else:
        assert output == f"nothing declared since {revision}\n"


@pytest.mark.parametrize("manifest", [None, "not valid toml", "<<<<<<< HEAD\n"])
def test_pending_entrypoint_does_not_load_a_conflicted_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manifest: str | None,
) -> None:
    if manifest is not None:
        (tmp_path / "pyproject.toml").write_text(manifest, encoding="utf-8")
    catalog = wrote(tmp_path, "catalog.py", "<<<<<<< HEAD\n=======\n>>>>>>> branch\n")

    def project_application() -> typer.Typer:
        compile(catalog.read_text(encoding="utf-8"), str(catalog), "exec")
        raise AssertionError("the conflicted catalog unexpectedly imported")

    monkeypatch.setattr(entrypoint, "project_application", project_application)
    monkeypatch.setattr(migrations.MigrationRecord, "declared", lambda _: [])
    monkeypatch.setattr(
        sys, "argv", ["lup-devtools", "dev", "migrate", "pending", "HEAD", "--json"]
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as completed:
        entrypoint.main()

    assert completed.value.code == 0
    assert migrations.RenderedMigrations.model_validate_json(
        capsys.readouterr().out
    ) == (migrations.RenderedMigrations(count=0, lines=[]))


@pytest.mark.parametrize(
    "words",
    [
        ["dev", "update"],
        ["dev", "migrate", "check"],
        ["dev", "migrate", "pyright-environment"],
    ],
)
def test_only_pending_reports_bypass_project_composition(
    monkeypatch: pytest.MonkeyPatch, words: list[str]
) -> None:
    def project_application() -> typer.Typer:
        raise RuntimeError("project application requested")

    monkeypatch.setattr(entrypoint, "project_application", project_application)
    monkeypatch.setattr(sys, "argv", ["lup-devtools", *words])

    with pytest.raises(RuntimeError, match="project application requested"):
        entrypoint.main()


def test_migrations_are_reported_before_conflicts_and_not_repeated_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base, DECLINING)
    wrote(adopter, "src/demo/catalog.py", "declared = 2\n")
    wrote(adopter, "tests/test_demonstration.py", "assert 'domain'\n")
    committed(adopter, "domain catalog and declined test")
    wrote(upstream, "src/lup_template/catalog.py", "declared = 3\n")
    committed(upstream, "upstream catalog")
    head = git.out("-C", str(upstream), "rev-parse", "HEAD")
    adopt(adopter, upstream, DECLINING, PACKAGE, base)
    migration = migrations.Migration(
        subjects=["example"],
        reason="Choose the domain behavior.",
        steps=[migrations.MigrationStep(instruction="Keep the domain declaration.")],
        commit=head,
    )
    monkeypatch.setattr(migrations.MigrationRecord, "declared", lambda _: [migration])
    said: list[str] = []
    generated: list[Path] = []
    processes: list[tuple[str, ...]] = []

    class InstalledCli:
        """Only the installed library's pending-report route runs in the child."""

        def out(self, *words: str, **_named: str) -> str:
            processes.append(words)
            result = CliRunner().invoke(
                entrypoint.migration_application(), list(words[3:])
            )
            if result.exception is not None:
                raise result.exception
            return result.output

    monkeypatch.setattr(update, "uv", InstalledCli())
    monkeypatch.setattr(update, "upstream_checkout", lambda _project, _report: upstream)
    monkeypatch.setattr(
        update, "regenerated", lambda root, _report: generated.append(root)
    )

    first = update.settled(
        adopter, upstream, DECLINING, PACKAGE, head, base, said.append
    )
    assert first is not None and first.conflicted == ["src/demo/catalog.py"]
    assert "  conflicted  src/demo/catalog.py" in said
    assert any("git add" in line and "dev update" in line for line in said)
    assert any(migration.reason in line for line in said)
    assert generated == []

    wrote(adopter, "src/demo/catalog.py", "declared = 2\n")
    git("-C", str(adopter), "add", "src/demo/catalog.py")
    second = update.updated(adopter, SOURCE, PACKAGE, "", "lup", said.append)
    assert second is not None and second.conflicted == ["tests/test_demonstration.py"]
    assert generated == []

    wrote(adopter, "tests/test_demonstration.py", "assert True\nassert 'domain'\n")
    git("-C", str(adopter), "add", "tests/test_demonstration.py")
    update.updated(adopter, SOURCE, PACKAGE, "", "lup", said.append)
    assert not merging(adopter)
    assert generated == [adopter]
    assert sum(migration.reason in line for line in said) == 1

    reads = len(processes)
    update.settled(adopter, upstream, SOURCE, PACKAGE, head, head, said.append)
    assert len(processes) == reads
    assert generated == [adopter, adopter]
