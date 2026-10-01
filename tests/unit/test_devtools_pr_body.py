"""What ``dev pr-body`` says about a branch, against what the branch holds.

A generated body is pasted into a pull request and read there as a
description of the change, so a summary naming one of two commits and a test
plan nobody wrote are both claims the branch does not support. These pin the
three: every commit appears, the English is the project's own, and the test
plan is derived from the diff or absent.
"""

from pathlib import Path

import pytest

from lup.devtools.dev import branches


COMMITS = [
    "66f3fe1e fix(harness): say that a bridged sign-in ends on a browser error",
    "a6ad379b fix(harness): re-seed a contained login that can no longer be renewed",
]


class StubGit:
    """A git answering one log and one diff, whichever order they are asked in."""

    def __init__(self, log: list[str], diff: list[str]) -> None:
        self.log = log
        self.diff = diff

    def lines(self, *arguments: str, **options: object) -> list[str]:
        return self.log if arguments[0] == "log" else self.diff


def body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    log: list[str],
    diff: list[str],
) -> str:
    monkeypatch.setattr(branches, "git", StubGit(log, diff))
    branches.pr_body(base_override="feat-boundary")
    return capsys.readouterr().out


def test_every_commit_reaches_the_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The second commit is the substantive one, and it was the one folded away."""
    rendered = body(monkeypatch, capsys, COMMITS, [])

    assert "- say that a bridged sign-in ends on a browser error" in rendered
    assert "- re-seed a contained login that can no longer be renewed" in rendered
    assert "more)" not in rendered


def test_the_type_stands_over_its_commits_rather_than_opening_a_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A declarative subject reads under a noun phrase and not after a verb."""
    rendered = body(monkeypatch, capsys, COMMITS, [])

    assert "**Fixes**" in rendered
    assert "Fixed say" not in rendered


def test_the_test_plan_names_the_tests_the_branch_touches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    changed = [
        "packages/lup/src/lup/devtools/dev/pr.py",
        "tests/unit/test_devtools_check_state.py",
    ]

    rendered = body(monkeypatch, capsys, COMMITS, changed)

    assert "## Test plan" in rendered
    assert "- [ ] `tests/unit/test_devtools_check_state.py`" in rendered
    assert "pr.py`" not in rendered


def test_a_branch_touching_no_test_claims_no_plan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An absent section is what the branch supports; a checklist is not."""
    rendered = body(monkeypatch, capsys, COMMITS, ["docs/commands.md"])

    assert "## Test plan" not in rendered
    assert "Verify changes" not in rendered


def test_a_path_is_read_for_the_conventions_a_test_is_named_by() -> None:
    assert branches.names_a_test("tests/unit/test_devtools_pr_body.py")
    assert branches.names_a_test("src/thing/thing_test.go")
    assert branches.names_a_test("web/components/Button.test.tsx")
    assert not branches.names_a_test("packages/lup/src/lup/devtools/dev/pr.py")
    assert not branches.names_a_test("src/greatest.py")


class TestPrCreate:
    def test_create_does_not_call_gh_with_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lup.devtools.dev.pr as pr

        calls: list[tuple[str, ...]] = []

        class FakeGh:
            def out(self, *args: str) -> str:
                calls.append(tuple(str(a) for a in args))
                return "https://github.com/org/repo/pull/42"

        monkeypatch.setattr(pr, "gh", FakeGh())
        monkeypatch.setattr(pr, "check_forge_api", lambda: True)

        results: list[pr.CreateResult] = []
        monkeypatch.setattr(pr, "output_result", lambda r, _as_json: results.append(r))

        pr.create(base="dev", title="feat: x", body="body", as_json=False)

        assert len(calls) == 1, "URL parsing must not need a second gh call"
        assert "--json" not in calls[0]
        assert results[0].number == 42
        assert results[0].url == "https://github.com/org/repo/pull/42"

    def test_parse_pr_url_picks_url_line(self) -> None:
        from lup.devtools.dev.pr import parse_pr_url

        stdout = "Warning: 1 uncommitted change\nhttps://github.com/org/repo/pull/7\n"
        assert parse_pr_url(stdout) == "https://github.com/org/repo/pull/7"

    def test_body_file_read_verbatim(self, tmp_path: Path) -> None:
        from lup.devtools.dev.pr import resolve_body

        # The quoting a shell argument would have made the caller escape.
        written = "## It's here\n\nA `--body` with 'quotes' and \"doubles\".\n"
        source = tmp_path / "body.md"
        source.write_text(written, encoding="utf-8")

        assert resolve_body(None, source) == written

    def test_body_and_body_file_together_refused(self, tmp_path: Path) -> None:
        import typer

        from lup.devtools.dev.pr import resolve_body

        source = tmp_path / "body.md"
        source.write_text("from the file", encoding="utf-8")

        with pytest.raises(typer.BadParameter):
            resolve_body("inline", source)

    def test_neither_body_refused(self) -> None:
        import typer

        from lup.devtools.dev.pr import resolve_body

        with pytest.raises(typer.BadParameter):
            resolve_body(None, None)

    def test_unreadable_body_file_names_the_path(self, tmp_path: Path) -> None:
        import typer

        from lup.devtools.dev.pr import resolve_body

        missing = tmp_path / "absent.md"
        with pytest.raises(typer.BadParameter, match="absent.md"):
            resolve_body(None, missing)
