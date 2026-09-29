"""`dev init upstream`: the shipped registration, pointed at where a project came from.

GitHub records the template a repository was generated from, and a project
generated from a fork of lup builds on the fork -- so initialization reads
that record and writes it into the registration `sync.json` ships, or, where
a git pin decides which repository the registration means, says which
command moves the pin. The forge is a double here: what is under test is what
the answer does to the registration, not whether GitHub is reachable.
"""

import json
from pathlib import Path

import pytest
import sh

import lup.devtools.dev.origin as origin
from lup.devtools import sync
from tests.unit.repos import initialized_repo

SHIPPED = sync.load_json(Path("sync.json"))
"""The registration this checkout ships, which every generated project starts from."""

FORK = "https://github.com/someone/lup-fork.git"


class Forge:
    """`gh`, answering one repository document and remembering what it was asked."""

    def __init__(self, template: str | None) -> None:
        self.template = template
        self.asked: list[tuple[str, ...]] = []

    def out(self, *args: str) -> str:
        self.asked.append(args)
        document = (
            {"template_repository": {"clone_url": self.template}}
            if self.template is not None
            else {"template_repository": None}
        )
        return json.dumps({"full_name": "someone/project", **document})


class Unreachable:
    """`gh` refusing, as it does signed out or offline."""

    def out(self, *args: str) -> str:
        raise sh.ErrorReturnCode_1(
            "gh api", b"", b"HTTP 401: Bad credentials (https://api.github.com)"
        )


@pytest.fixture
def generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project generated from the template, its origin on GitHub, not yet pinned."""
    root = tmp_path / "project"
    git = initialized_repo(root, tmp_path / "hooks")
    git("remote", "add", "origin", "git@github.com:someone/project.git")
    (root / "pyproject.toml").write_text('[tool.lup]\nagent_version = "0.1.0"\n')
    (root / "sync.json").write_text(json.dumps(SHIPPED, indent=2) + "\n")
    monkeypatch.setattr(sync, "project_root", lambda: root)
    monkeypatch.setattr(sync, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(origin, "resolved_host", lambda alias: alias)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    return root


def registered(root: Path) -> str:
    """The url the shipped entry carries on disk."""
    document = json.loads((root / "sync.json").read_text())
    return next(entry for entry in document["projects"] if entry["name"] == "lup")[
        "url"
    ]


def test_a_project_generated_from_a_fork_registers_the_fork(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = Forge(FORK)
    monkeypatch.setattr(origin, "gh", forge)

    assert origin.point_at_template(generated, "lup", dry_run=False)

    assert registered(generated) == FORK
    assert sync.find_project("lup").get("mount") == "rw"
    assert forge.asked == [("api", "--hostname", "github.com", "repos/someone/project")]


def test_a_dry_run_writes_nothing(
    generated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = (generated / "sync.json").read_text()
    monkeypatch.setattr(origin, "gh", Forge(FORK))

    assert origin.point_at_template(generated, "lup", dry_run=True)

    assert (generated / "sync.json").read_text() == before
    assert f"Would point 'lup' at {FORK}" in capsys.readouterr().out


def test_a_project_generated_from_lup_itself_is_left_as_shipped(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named over another transport, and still the same repository."""
    before = (generated / "sync.json").read_text()
    monkeypatch.setattr(origin, "gh", Forge("https://github.com/joy-void-joy/lup.git"))

    assert origin.point_at_template(generated, "lup", dry_run=False)

    assert (generated / "sync.json").read_text() == before


def test_a_repository_generated_from_no_template_keeps_the_shipped_entry(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (generated / "sync.json").read_text()
    monkeypatch.setattr(origin, "gh", Forge(None))

    assert origin.point_at_template(generated, "lup", dry_run=False)

    assert (generated / "sync.json").read_text() == before


def test_an_unanswered_forge_is_said_and_changes_nothing(
    generated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = (generated / "sync.json").read_text()
    monkeypatch.setattr(origin, "gh", Unreachable())

    assert not origin.point_at_template(generated, "lup", dry_run=False)

    assert (generated / "sync.json").read_text() == before
    assert "Bad credentials" in capsys.readouterr().out


def test_a_pinned_project_is_told_the_command_that_moves_the_pin(
    generated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The registration follows the pin, so writing its url would be read over."""
    (generated / "pyproject.toml").write_text(
        '[tool.lup]\nagent_version = "0.1.0"\n\n[tool.uv.sources]\n'
        'lup-agents = { git = "https://github.com/joy-void-joy/lup", branch = "dev" }\n'
    )
    before = (generated / "sync.json").read_text()
    monkeypatch.setattr(origin, "gh", Forge(FORK))

    assert not origin.point_at_template(generated, "lup", dry_run=False)

    assert (generated / "sync.json").read_text() == before
    assert (
        f"uv run lup-devtools dev library git --url {FORK} --branch dev"
        in capsys.readouterr().out
    )


def test_a_clone_of_the_repository_it_used_to_mean_is_named(
    generated: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cache is keyed by name, so the old clone would be refused next launch."""
    cached = tmp_path / "cache" / "lup.git"
    sh.git("init", "--quiet", "--bare", str(cached))
    sh.git(
        "-C",
        str(cached),
        "remote",
        "add",
        "origin",
        SHIPPED["projects"][0].get("url", ""),
    )
    monkeypatch.setattr(origin, "gh", Forge(FORK))

    assert origin.point_at_template(generated, "lup", dry_run=False)

    assert f"{cached} holds a clone of" in capsys.readouterr().out
