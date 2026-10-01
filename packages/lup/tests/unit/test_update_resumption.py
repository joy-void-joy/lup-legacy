"""The second half of an update a conflict interrupted, driven to completion.

The claim under test is that the instruction a conflicted update prints can be
followed. Resolving a scaffold conflict is where a project decides what of
upstream it wants, so the resolution is free to widen that set — and the
scaffold commit under the standing merge was compiled from the declaration
that resolution has just replaced, so the widened path is in neither side of
it. An update asking for the merge to be committed first would deadlock
exactly there: the commit guard regenerates, generation reads the modules the
resolution un-declined, their bodies arrive only from a later compile, and
that compile cannot start until the commit it is blocking has landed.

Two repositories are built here rather than mocked, because what is being
tested is what git does with a merge somebody resolved. What is stubbed is the
regeneration and the upstream checkout: one is a subprocess under a library
this process has not installed, the other a registration in somebody's
`sync.json`, and neither is this pass's subject.
"""

from pathlib import Path

import pytest

import lup.devtools.dev.update as update
from lup.devtools.dev.scaffold import (
    adopt,
    advanced,
    branch_head,
    merged,
    merging,
    unresolved,
)
from lup.execution.shell import git
from tests.unit.test_scaffold import (
    PACKAGE,
    SOURCE,
    adopter_from,
    upstream_at_base,
    wrote,
)

DECLINING = SOURCE.model_copy(update={"declined": ["tests/test_demonstration.py"]})
"""What the project took before the resolution: upstream's tree, less one test."""


def interrupted(tmp_path: Path) -> tuple[Path, Path]:
    """An adopter holding a conflicted scaffold merge, resolved but uncommitted.

    The state a person is in when they have done everything the first pass
    asked of them: both sides of the one conflicting file kept, the file
    staged, and the merge still open because nothing has committed it.
    """
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base, DECLINING)

    wrote(adopter, "src/demo/tools.py", "all = ['domain tool']\n")
    git("-C", str(adopter), "add", "-A")
    git("-C", str(adopter), "commit", "-q", "-m", "the adopter's own work")

    wrote(upstream, "src/lup_template/tools.py", "all = []\npulse = True\n")
    wrote(upstream, "src/lup_template/kinds.py", "kinds = []\n")
    git("-C", str(upstream), "add", "-A")
    git("-C", str(upstream), "commit", "-q", "-m", "the pulse, and a new module")
    head = git.out("-C", str(upstream), "rev-parse", "HEAD")

    adopt(adopter, upstream, DECLINING, PACKAGE, base)
    advanced(adopter, upstream, DECLINING, PACKAGE, head)
    outcome = merged(adopter, DECLINING)
    assert outcome.conflicted == ["src/demo/tools.py"]

    wrote(adopter, "src/demo/tools.py", "all = ['domain tool']\npulse = True\n")
    git("-C", str(adopter), "add", "src/demo/tools.py")
    return adopter, upstream


def resuming(monkeypatch: pytest.MonkeyPatch, upstream: Path) -> list[Path]:
    """Stand in for the three things a resumption reaches outside this checkout.

    The regeneration answers with the roots it was asked to write, so a test
    can assert that it ran at all.

    `uv` refuses to move a carrier. A resumption finishes the pass an earlier
    one started, at the commit the standing merge already carries; resolving
    the pin there would move the carriers out from under a merge that is only
    part-way applied. Reading is a different act and is allowed: the pass
    reports what migrations are owed, and asks for that with `--no-sync`,
    which says in the invocation that it changes nothing.
    """
    regenerated: list[Path] = []

    class Standing:
        """`uv` as a resumption may use it: a read answers, a move refuses."""

        def __call__(self, *words: str, **named: str) -> str:
            return self.out(*words, **named)

        def out(self, *words: str, **named: str) -> str:
            if words and words[0] in ("lock", "sync", "add", "remove"):
                raise AssertionError(f"a resumption moved a carrier: {words}")
            # The one read a resumption makes is the owed-migration report,
            # whose caller parses it, so the stand-in answers in its shape.
            return '{"count": 0, "lines": []}'

    monkeypatch.setattr(update, "upstream_checkout", lambda project, report: upstream)
    monkeypatch.setattr(
        update, "regenerated", lambda root, report: regenerated.append(root)
    )
    monkeypatch.setattr(update, "uv", Standing())
    return regenerated


def test_a_resolution_that_widens_the_taken_set_lands_in_the_same_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadlock, driven to completion by the instruction's own words.

    The resolution stops declining `tests/test_demonstration.py`, which is
    what resolving a scaffold conflict looks like when the conflict is about
    what the project takes. One pass ends with that path present, the merge
    committed, and the trees regenerated — none of which the project could
    reach while the pass demanded a commit the guard would refuse.
    """
    adopter, upstream = interrupted(tmp_path)
    said: list[str] = []
    regenerated = resuming(monkeypatch, upstream)
    taken = SOURCE

    outcome = update.updated(adopter, taken, PACKAGE, "", "lup", said.append)

    assert (adopter / "tests" / "test_demonstration.py").is_file()
    assert (adopter / "src" / "demo" / "tools.py").read_text(encoding="utf-8") == (
        "all = ['domain tool']\npulse = True\n"
    )
    assert merging(adopter) == ""
    assert unresolved(adopter) == []
    assert regenerated == [adopter]
    assert outcome is not None and outcome.complete()


def test_a_resolution_that_changes_no_declaration_still_concludes_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary resumption: nothing further to compile, and it says so.

    The same pass has to finish a merge whose resolution touched only the
    project's own code, and finish it without a second scaffold commit — the
    compile is a pure function of the commit and the declaration, and neither
    moved.
    """
    adopter, upstream = interrupted(tmp_path)
    said: list[str] = []
    regenerated = resuming(monkeypatch, upstream)
    standing = branch_head(adopter, DECLINING.branch)

    update.updated(adopter, DECLINING, PACKAGE, "", "lup", said.append)

    assert branch_head(adopter, DECLINING.branch) == standing
    assert merging(adopter) == ""
    assert regenerated == [adopter]
    assert not (adopter / "tests" / "test_demonstration.py").exists()
    assert any("already merged" in line for line in said)


def test_a_merge_still_holding_a_conflict_is_reported_and_compiles_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a resumption owes somebody who has not finished resolving.

    Compiling over an index that still holds a conflict would read half a
    declaration, so the pass names every unmerged path and stops — and says
    which two steps reach the pass that finishes.
    """
    adopter, upstream = interrupted(tmp_path)
    git("-C", str(adopter), "checkout", "--merge", "--", "src/demo/tools.py")
    said: list[str] = []
    regenerated = resuming(monkeypatch, upstream)

    outcome = update.updated(adopter, SOURCE, PACKAGE, "", "lup", said.append)

    assert outcome is not None and outcome.conflicted == ["src/demo/tools.py"]
    assert merging(adopter) != ""
    assert regenerated == []
    assert "  conflicted  src/demo/tools.py" in said
    assert any("`git add`" in line for line in said)


def test_a_merge_of_something_else_is_named_rather_than_merged_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One merge at a time, said rather than discovered through git's refusal."""
    adopter, upstream = interrupted(tmp_path)
    git("-C", str(adopter), "commit", "--no-verify", "--no-edit", "-q")
    git("-C", str(adopter), "checkout", "-q", "-b", "elsewhere")
    wrote(adopter, "src/demo/domain.py", "answer = 43\n")
    git("-C", str(adopter), "commit", "-q", "-a", "-m", "elsewhere's own")
    git("-C", str(adopter), "checkout", "-q", "main")
    wrote(adopter, "src/demo/domain.py", "answer = 44\n")
    git("-C", str(adopter), "commit", "-q", "-a", "-m", "main's own")
    git("-C", str(adopter), "merge", "--no-edit", "elsewhere", _ok_code=[0, 1])
    said: list[str] = []
    regenerated = resuming(monkeypatch, upstream)

    outcome = update.updated(adopter, SOURCE, PACKAGE, "", "lup", said.append)

    assert outcome is None
    assert regenerated == []
    assert any("it is not lup-scaffold's" in line for line in said)
