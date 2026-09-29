"""Behavior tests for structured workflow-friction reports."""

import shlex
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sh
from typer.testing import CliRunner

from lup.devtools.dev import issues
from lup.devtools.dev import library
from lup_template.devtools.main import app


def friction_report() -> issues.FrictionReport:
    return issues.FrictionReport(
        summary="Detached resolver lost its run",
        component="Codex resolver launcher",
        command="uv run lup-devtools harness resolve start --detach",
        error="run `resolve-123` does not exist",
        state="a watcher path was printed but no run directory exists",
        recovery_cost="inspect state, abort stale runs, and restart in foreground",
    )


def friction_arguments(report: issues.FrictionReport | None = None) -> list[str]:
    report = report or friction_report()
    return [
        "dev",
        "report-friction",
        "--summary",
        report.summary,
        "--component",
        report.component,
        "--command",
        report.command,
        "--error",
        report.error,
        "--state",
        report.state,
        "--recovery-cost",
        report.recovery_cost,
    ]


def test_report_body_labels_every_observation() -> None:
    body = friction_report().body()
    assert "## Owning component" in body
    assert "## Exact command" in body
    assert "## Exact error" in body
    assert "## State left behind" in body
    assert "## Recovery cost" in body
    assert "uv run lup-devtools harness resolve start --detach" in body


def test_report_body_escapes_evidence_as_literal_text() -> None:
    report = friction_report().model_copy(update={"error": "</code>`boom` &"})
    body = report.body()
    assert "&lt;/code&gt;`boom` &amp;" in body
    assert "</code>`boom` &" not in body


def test_filing_targets_the_checkout_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    fake_gh = SimpleNamespace(
        out=lambda *arguments: (
            calls.append(arguments) or "https://github.test/o/r/issues/9\n"
        )
    )
    monkeypatch.setattr(issues, "gh", fake_gh)
    monkeypatch.setattr(issues, "repository_slug", lambda: "owner/repository")
    url = friction_report().file()
    assert url == "https://github.test/o/r/issues/9"
    assert calls == [
        (
            "issue",
            "create",
            "--repo",
            "owner/repository",
            "--title",
            friction_report().summary,
            "--body",
            friction_report().body(),
        )
    ]


def test_filing_requires_an_explicit_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(issues, "repository_slug", lambda: "")
    with pytest.raises(RuntimeError, match="origin names no GitHub repository"):
        friction_report().file()


def test_correction_edits_the_numbered_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    fake_gh = SimpleNamespace(
        out=lambda *arguments: (
            calls.append(arguments) or "https://github.test/o/r/issues/179\n"
        )
    )
    monkeypatch.setattr(issues, "gh", fake_gh)
    url = friction_report().file(repository="owner/repository", issue=179)
    assert url == "https://github.test/o/r/issues/179"
    assert calls == [
        (
            "issue",
            "edit",
            "179",
            "--repo",
            "owner/repository",
            "--title",
            friction_report().summary,
            "--body",
            friction_report().body(),
        )
    ]


def test_cli_reports_tracker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = Mock(side_effect=RuntimeError("tracker unavailable"))
    monkeypatch.setattr(issues.FrictionReport, "file", failure)
    result = CliRunner().invoke(app, friction_arguments())
    assert result.exit_code == 1
    assert "tracker unavailable" in result.output


def test_cli_forwards_correction_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """The number to correct, and the tracker the routing chose, both arrive.

    Both, because the command decides where a report lands as well as
    whether it is a correction: a stub that only recorded the number would
    pass a version that routed everything to one repository.
    """
    calls: list[tuple[issues.FrictionReport, str, int | None]] = []

    def record(
        self: issues.FrictionReport, repository: str = "", issue: int | None = None
    ) -> str:
        calls.append((self, repository, issue))
        return "https://github.test/o/r/issues/179"

    monkeypatch.setattr(issues.FrictionReport, "file", record)
    monkeypatch.setattr("lup.devtools.dev.app.repository_slug", lambda: "acme/widget")
    result = CliRunner().invoke(app, [*friction_arguments(), "--issue", "179"])
    assert result.exit_code == 0
    assert calls == [(friction_report(), "acme/widget", 179)]


@pytest.mark.parametrize(
    ("component", "expected"),
    [
        ("lup/sandbox, lup/devtools", "forge.example/upstream/framework"),
        ("lup.resolver.state", "forge.example/upstream/framework"),
        ("lup/policy/resolver", "forge.example/upstream/framework"),
        ("aib.devtools.trace", "acme/widget"),
    ],
)
def test_cli_routes_to_the_configured_dependency_owner(
    monkeypatch: pytest.MonkeyPatch, component: str, expected: str
) -> None:
    """The copied catalog and wired command preserve component ownership.

    Read through a correction, which goes wherever routing sends it: a
    report already filed on the dependency's tracker is corrected there.
    """
    monkeypatch.setattr(
        library,
        "configured_repository",
        lambda *_args: "https://forge.example/upstream/framework.git",
    )
    monkeypatch.setattr("lup.devtools.dev.app.repository_slug", lambda: "acme/widget")
    report = friction_report().model_copy(update={"component": component})
    filed = Mock(return_value="https://forge.example/upstream/framework/issues/1")
    monkeypatch.setattr(issues.FrictionReport, "file", filed)

    result = CliRunner().invoke(app, [*friction_arguments(report), "--issue", "4"])

    assert result.exit_code == 0, result.output
    filed.assert_called_once_with(repository=expected, issue=4)


@pytest.mark.parametrize(
    "component",
    ["lup/sandbox, lup/devtools", "lup.resolver.state", "lup/policy/resolver"],
)
def test_a_new_report_routed_to_a_dependency_prints_what_files_it(
    monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    """Filed unasked only here; the dependency's tracker is named, and asks.

    The permission policy reads the words, and an unnamed report reads as one
    filed on this checkout's repository -- so filed on the dependency's
    tracker it would reach that project's watchers with nobody asked. It
    stops instead, and prints the same invocation naming the tracker. That
    line is the whole route out, so it is run here: it files the very report
    that stopped, where routing said it belongs.
    """
    monkeypatch.setattr(
        library,
        "configured_repository",
        lambda *_args: "https://forge.example/upstream/framework.git",
    )
    monkeypatch.setattr("lup.devtools.dev.app.repository_slug", lambda: "acme/widget")
    report = friction_report().model_copy(update={"component": component})
    filed = Mock(return_value="https://forge.example/upstream/framework/issues/1")
    monkeypatch.setattr(issues.FrictionReport, "file", filed)
    named = ["--repo", "forge.example/upstream/framework"]
    route = ["uv", "run", "lup-devtools", *friction_arguments(report), *named]

    stopped = CliRunner().invoke(app, friction_arguments(report))

    assert stopped.exit_code == 1
    filed.assert_not_called()
    assert shlex.join(route) in stopped.output

    rerun = CliRunner().invoke(app, shlex.split(shlex.join(route))[3:])

    assert rerun.exit_code == 0, rerun.output
    filed.assert_called_once_with(
        repository="forge.example/upstream/framework", issue=None
    )


def test_a_new_report_this_checkout_owns_is_filed_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every defect this tree owns is filed without a question.

    Including where the tracker claiming the component is this checkout's own
    repository written another way, which is lup's own checkout reporting
    against lup.
    """
    monkeypatch.setattr(
        library,
        "configured_repository",
        lambda *_args: "https://github.com/acme/widget.git",
    )
    monkeypatch.setattr("lup.devtools.dev.app.repository_slug", lambda: "acme/widget")
    filed = Mock(return_value="https://github.com/acme/widget/issues/1")
    monkeypatch.setattr(issues.FrictionReport, "file", filed)

    for component in ("aib.devtools.trace", "lup/policy"):
        report = friction_report().model_copy(update={"component": component})
        result = CliRunner().invoke(app, friction_arguments(report))
        assert result.exit_code == 0, result.output

    assert [call.kwargs["issue"] for call in filed.call_args_list] == [None, None]


def test_disabled_issues_offer_an_explicit_declared_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed report is not silently filed in another project's intake."""
    monkeypatch.setattr(
        library,
        "configured_repository",
        lambda *_args: "https://forge.example/upstream/framework.git",
    )
    monkeypatch.setattr("lup.devtools.dev.app.repository_slug", lambda: "acme/widget")
    filed = Mock(
        side_effect=[
            sh.ErrorReturnCode_1(
                "gh issue create",
                b"",
                b"the 'acme/widget' repository has disabled issues",
            ),
            "https://forge.example/upstream/framework/issues/1",
        ]
    )
    monkeypatch.setattr(issues.FrictionReport, "file", filed)

    refused = CliRunner().invoke(app, friction_arguments())

    assert refused.exit_code == 1
    assert "has disabled issues" in refused.output
    assert "forge.example/upstream/framework" in refused.output
    assert "--repo" in refused.output
    filed.assert_called_once_with(repository="acme/widget", issue=None)

    recovered = CliRunner().invoke(
        app,
        [*friction_arguments(), "--repo", "forge.example/upstream/framework"],
    )

    assert recovered.exit_code == 0, recovered.output
    assert filed.call_count == 2
    filed.assert_called_with(repository="forge.example/upstream/framework", issue=None)
