"""The anti-pattern sweep answers for what a tree changed, and nothing more.

A scope names the files a caller is answerable for; declarations outside it
still inform the rules that judge inside it, and a ref that names no commit
is refused rather than read as a scope holding nothing.
"""

from pathlib import Path

import pytest
import typer

from lup.devtools.dev.antipatterns import mirrored_file, scan_antipatterns
from lup.devtools.dev.check import changed_paths
from lup.devtools.project import DevProject
from lup.policy.kernel.rows import PathRoleRow
from tests.unit.repos import commit_file, git_in, initialized_repo


class TestTheAntiPatternSweepIsScopedToWhatATreeChanged:
    """`dev check --since` scopes this gate, as its own docstring says it does.

    Unscoped, it would judge a resolver lease against every finding standing
    in the tree it was cut from, rejecting otherwise-green work over a
    finding in a file the lease never touched, and every retry would
    re-derive it.
    """

    def two_files_that_trip_a_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> DevProject:
        """A repository whose every tracked file has one finding to report."""
        work = tmp_path / "repo"
        git = initialized_repo(work, tmp_path / "no-hooks")
        for name in ("mine.py", "theirs.py"):
            (work / name).write_text(
                "from typing import Any\n\n\ndef f(x: Any) -> None: ...\n",
                encoding="utf-8",
            )
        git("add", "-A")
        git("commit", "-m", "chore: two files that trip a rule")
        monkeypatch.chdir(work)
        return DevProject(package="app")

    def test_naming_no_path_sweeps_the_whole_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self.two_files_that_trip_a_rule(tmp_path, monkeypatch)

        scan = scan_antipatterns(project)

        assert {finding.file for finding in scan.findings} == {"mine.py", "theirs.py"}

    def test_a_scope_leaves_out_what_it_does_not_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self.two_files_that_trip_a_rule(tmp_path, monkeypatch)

        scan = scan_antipatterns(project, ["mine.py"])

        assert {finding.file for finding in scan.findings} == {"mine.py"}

    @pytest.mark.parametrize("landing", ["mine.py", "fresh.py"])
    def test_a_scratch_copy_is_judged_as_the_file_it_will_land_as(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landing: str
    ) -> None:
        """No rule reads scratch, so a copy there is read at the path it mirrors.

        Its findings are the copy's -- the file somebody is about to fix --
        and a landing nothing stands at yet is read all the same.
        """
        project = self.two_files_that_trip_a_rule(tmp_path, monkeypatch)
        copy = tmp_path / "repo/tmp/copy.py"
        copy.parent.mkdir()
        copy.write_text(
            "from typing import Any\n\n\ndef g(x: Any) -> None: ...\n\n\n"
            "def h(y: Any) -> None: ...\n",
            encoding="utf-8",
        )
        read = scan_antipatterns(project, ["tmp/copy.py"])
        scratch = project.model_copy(
            update={"path_roles": [PathRoleRow(root="tmp", role="scratch")]}
        )

        unjudged = scan_antipatterns(scratch, ["tmp/copy.py"])
        mirrored = mirrored_file("tmp/copy.py", landing)
        judged = scan_antipatterns(scratch, [mirrored.judged_as], mirrored)

        assert unjudged.findings == []
        assert judged.findings == read.findings
        assert {finding.line for finding in judged.findings} >= {4, 7}

    def test_a_tree_that_changed_nothing_is_answerable_for_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty scope is a scope, not an absent one."""
        project = self.two_files_that_trip_a_rule(tmp_path, monkeypatch)

        scan = scan_antipatterns(project, [])

        assert scan.findings == []

    def test_a_scope_holding_no_production_file_reads_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sweep asked about files no rule reads judges nothing.

        The post-write review asks about every file a call wrote, and building
        the whole project's context to judge none of them costs seconds a call.
        """
        project = self.two_files_that_trip_a_rule(tmp_path, monkeypatch)
        (tmp_path / "repo/notes.md").write_text("# Notes\n", encoding="utf-8")

        def unread(**_fields: object) -> list[object]:
            raise AssertionError("a project rule was run for no production file")

        monkeypatch.setattr("lup.devtools.dev.antipatterns.AuditedProject", unread)

        assert scan_antipatterns(project, ["notes.md"]).findings == []

    @pytest.mark.parametrize(
        "suppression", ["", "    # lup: ignore[own-model-dispatch]\n"]
    )
    def test_scoped_dispatch_uses_declarations_outside_its_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suppression: str
    ) -> None:
        work = tmp_path / "repo"
        git = initialized_repo(work, tmp_path / "no-hooks")
        # A variant with a sibling, which is what the rule dispatches over: a
        # lone model is one type, and a branch on it goes stale for nobody.
        (work / "models.py").write_text(
            "from pydantic import BaseModel\n\nclass Kind(BaseModel):\n    pass\n"
            "\nclass Entry(Kind):\n    pass\n\nclass Other(Kind):\n    pass\n",
            encoding="utf-8",
        )
        (work / "adapter.py").write_text(
            "from models import Entry\n\ndef convert(value: Entry) -> bool:\n"
            f"{suppression}    return isinstance(value, Entry)\n",
            encoding="utf-8",
        )
        git("add", "-A")
        monkeypatch.chdir(work)
        project = DevProject(package="app")

        whole = scan_antipatterns(project)
        scoped = scan_antipatterns(project, ["adapter.py"])
        expected = [
            finding for finding in whole.findings if finding.file == "adapter.py"
        ]

        assert scoped.findings == expected
        dispatch = [
            finding for finding in expected if finding.rule_id == "own-model-dispatch"
        ]
        assert [finding.kind for finding in dispatch] == (
            [] if suppression else ["missing"]
        )

    def test_a_ref_git_cannot_resolve_refuses_instead_of_scoping_to_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mistyped ref is refused rather than read as "changed nothing".

        Read that way, it would scope the blocking gates to zero files and
        report ok, which is the one answer a gate must never give by accident.
        """
        self.two_files_that_trip_a_rule(tmp_path, monkeypatch)

        with pytest.raises(typer.BadParameter, match="does not name a commit"):
            changed_paths("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    def test_a_ref_that_resolves_names_what_changed_since_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.two_files_that_trip_a_rule(tmp_path, monkeypatch)
        git = git_in(tmp_path / "repo", tmp_path / "no-hooks")
        commit_file(git, tmp_path / "repo", "later.py", "x = 1\n", "feat: later")

        assert changed_paths("HEAD~1") == ["later.py"]
        assert changed_paths("HEAD") == []
