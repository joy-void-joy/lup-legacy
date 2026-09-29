"""The copied half as a compiled branch, and what a merge of it does.

The claim under test is the one the whole mechanism rests on: an adopter that
roots a branch at the scaffold it was stamped from can take every later
upstream change as an ordinary merge — upstream's own changes arriving for
free, changes to files the adopter also touched being reconciled where git can
and reported where it cannot, and the adopter's own work left alone.

Beside it is what a selection has to be for that to mean anything: a decline
list naming one half of a declaration and taking the other compiles cleanly,
merges cleanly, and fails at generation, so it is refused where it is still a
declaration somebody can edit.

Two repositories are built here rather than mocked, because what is being
tested is what git does: a merge base, three diffs, and a conflict.
"""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev.scaffold import (
    ScaffoldRoot,
    ScaffoldSource,
    adopt,
    advanced,
    branch_head,
    compiled,
    compiled_at,
    merged,
    merged_at,
    pinned_commit,
)
from lup.execution.shell import git
from tests.unit.test_ledger_placement import committed, repository

SOURCE = ScaffoldSource(
    project="upstream",
    roots=[
        ScaffoldRoot(upstream="src/lup_template", adopter="src/{package}"),
        ScaffoldRoot(upstream="tests", adopter="tests"),
    ],
)
PACKAGE = "demo"


def wrote(root: Path, relative: str, text: str) -> Path:
    """One file of a repository, with the directories above it."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def upstream_at_base(tmp_path: Path) -> tuple[Path, str]:
    """An upstream repository holding a copied half, and its first commit."""
    root = repository(tmp_path / "upstream")
    wrote(root, "src/lup_template/serve.py", "from lup_template.tools import all\n")
    wrote(root, "src/lup_template/tools.py", "all = []\n")
    wrote(root, "src/lup_template/catalog.py", "declared = 1\n")
    wrote(
        root,
        "src/lup_template/content/meta.py",
        "from lup.harness.models import Passage\n\nwords = Passage(module=__name__)\n",
    )
    wrote(root, "src/lup_template/content/meta.passage.md", "The words it places.\n")
    wrote(root, "tests/test_serve.py", "import lup_template.serve\n")
    wrote(root, "tests/test_demonstration.py", "assert True\n")
    wrote(root, "docs/upstream.md", "upstream's own\n")
    committed(root, "the scaffold")
    return root, git.out("-C", str(root), "rev-parse", "HEAD")


def adopter_from(
    tmp_path: Path, upstream: Path, base: str, source: ScaffoldSource = SOURCE
) -> Path:
    """A project stamped out of ``scaffold(base)``, with a domain of its own.

    ``source`` is what that project took, so a project that declined one of
    upstream's modules is stamped without it — the state every later reading
    of a decline has to start from, since a declined path is absent from the
    project and from every scaffold commit alike.
    """
    root = repository(tmp_path / "adopter")
    compiled(upstream, base, source, PACKAGE, root)
    wrote(root, "src/demo/domain.py", "answer = 42\n")
    wrote(root, "README.md", "the adopter's own\n")
    committed(root, "stamped out")
    return root


def test_the_compiled_scaffold_is_upstreams_tree_under_this_name(
    tmp_path: Path,
) -> None:
    upstream, base = upstream_at_base(tmp_path)

    built = compiled(upstream, base, SOURCE, PACKAGE, tmp_path / "out")

    assert built.files == [
        "src/demo/catalog.py",
        "src/demo/content/meta.passage.md",
        "src/demo/content/meta.py",
        "src/demo/serve.py",
        "src/demo/tools.py",
        "tests/test_demonstration.py",
        "tests/test_serve.py",
    ]
    assert (tmp_path / "out" / "src" / "demo" / "serve.py").read_text(
        encoding="utf-8"
    ) == "from demo.tools import all\n"


def test_a_tree_outside_the_roots_is_not_the_scaffolds(tmp_path: Path) -> None:
    """`docs/` and the root files are the adopting domain's from the first day."""
    upstream, base = upstream_at_base(tmp_path)

    compiled(upstream, base, SOURCE, PACKAGE, tmp_path / "out")

    assert not (tmp_path / "out" / "docs").exists()


def test_a_declined_path_is_absent_from_every_scaffold(tmp_path: Path) -> None:
    """What the project never took is never offered, so no merge reports it."""
    upstream, base = upstream_at_base(tmp_path)
    declining = SOURCE.model_copy(update={"declined": ["tests/test_demonstration.py"]})

    built = compiled(upstream, base, declining, PACKAGE, tmp_path / "out")

    assert "tests/test_demonstration.py" not in built.files
    assert not (tmp_path / "out" / "tests" / "test_demonstration.py").exists()


def test_declining_prose_and_taking_the_module_that_reads_it_is_refused(
    tmp_path: Path,
) -> None:
    """The half left behind would be read as a file that is not there.

    Refused where the selection is compiled rather than where generation
    finally opens the file, and the refusal names the module, because a
    reader holding a list of paths has no way to know which of them is the
    body of which.
    """
    upstream, base = upstream_at_base(tmp_path)
    declining = SOURCE.model_copy(
        update={"declined": ["src/lup_template/content/meta.passage.md"]}
    )

    with pytest.raises(ValueError) as refusal:
        compiled(upstream, base, declining, PACKAGE, tmp_path / "out")

    assert "src/lup_template/content/meta.py" in str(refusal.value)
    assert "src/lup_template/content/meta.passage.md" in str(refusal.value)
    assert "takes both or neither" in str(refusal.value)


def test_declining_the_module_and_taking_its_prose_is_refused(tmp_path: Path) -> None:
    """The other direction is an orphan: words nothing is left to read them."""
    upstream, base = upstream_at_base(tmp_path)
    declining = SOURCE.model_copy(
        update={"declined": ["src/lup_template/content/meta.py"]}
    )

    with pytest.raises(ValueError) as refusal:
        compiled(upstream, base, declining, PACKAGE, tmp_path / "out")

    assert "src/lup_template/content/meta.passage.md" in str(refusal.value)
    assert "the only reader of" in str(refusal.value)


def test_declining_both_halves_of_a_declaration_is_a_coherent_selection(
    tmp_path: Path,
) -> None:
    """A project that took neither half took no declaration, which is allowed."""
    upstream, base = upstream_at_base(tmp_path)
    declining = SOURCE.model_copy(
        update={
            "declined": [
                "src/lup_template/content/meta.py",
                "src/lup_template/content/meta.passage.md",
            ]
        }
    )

    built = compiled(upstream, base, declining, PACKAGE, tmp_path / "out")

    assert "src/demo/content/meta.py" not in built.files
    assert "src/demo/content/meta.passage.md" not in built.files


def test_declining_the_directory_a_declaration_sits_in_takes_both_halves(
    tmp_path: Path,
) -> None:
    """A directory declines everything beneath it, so neither half is halved."""
    upstream, base = upstream_at_base(tmp_path)
    declining = SOURCE.model_copy(update={"declined": ["src/lup_template/content"]})

    built = compiled(upstream, base, declining, PACKAGE, tmp_path / "out")

    assert not [path for path in built.files if "content" in path]


def test_taking_both_halves_of_a_declaration_is_a_coherent_selection(
    tmp_path: Path,
) -> None:
    """Nothing is declined here, and the prose arrives as the ordinary file it is."""
    upstream, base = upstream_at_base(tmp_path)

    built = compiled(upstream, base, SOURCE, PACKAGE, tmp_path / "out")

    assert "src/demo/content/meta.py" in built.files
    assert "src/demo/content/meta.passage.md" in built.files


def test_an_update_is_a_merge_against_the_commit_the_project_was_stamped_from(
    tmp_path: Path,
) -> None:
    """The whole mechanism, in one pass: adopt once, then merge each update.

    Upstream changes three files; the adopter changed one of those itself and
    one of its own. What the merge owes a report is exactly the split it makes
    here — taken whole, reconciled, or handed back.
    """
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)

    wrote(adopter, "src/demo/tools.py", "all = ['domain tool']\n")
    wrote(adopter, "src/demo/domain.py", "answer = 43\n")
    committed(adopter, "the adopter's own work")

    wrote(
        upstream,
        "src/lup_template/serve.py",
        "from lup_template.tools import all\nserve = True\n",
    )
    wrote(upstream, "src/lup_template/tools.py", "all = []\npulse = True\n")
    wrote(upstream, "src/lup_template/kinds.py", "kinds = []\n")
    committed(upstream, "the pulse, and a new module")
    head = git.out("-C", str(upstream), "rev-parse", "HEAD")

    adopt(adopter, upstream, SOURCE, PACKAGE, base)
    advanced(adopter, upstream, SOURCE, PACKAGE, head)
    outcome = merged(adopter, SOURCE)

    assert outcome.fast_forwarded() == ["src/demo/kinds.py", "src/demo/serve.py"]
    assert outcome.merged_clean() == []
    assert outcome.conflicted == ["src/demo/tools.py"]
    assert (adopter / "src" / "demo" / "kinds.py").exists()
    assert (adopter / "src" / "demo" / "domain.py").read_text(
        encoding="utf-8"
    ) == "answer = 43\n"


def test_a_compile_of_what_the_branch_already_holds_records_nothing(
    tmp_path: Path,
) -> None:
    """The branch advances on what it holds, not on having been asked again.

    An update compiles the copied half whenever the project's declaration of
    what it takes may have moved, which is every pass — so a pass where
    neither the commit nor the declaration moved has to leave the branch
    where it is. A second commit of identical bytes would give git a merge to
    report with nothing in it.
    """
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    rooted = adopt(adopter, upstream, SOURCE, PACKAGE, base)

    again = advanced(adopter, upstream, SOURCE, PACKAGE, base)
    widened = advanced(
        adopter,
        upstream,
        SOURCE.model_copy(update={"declined": ["tests/test_demonstration.py"]}),
        PACKAGE,
        base,
    )

    assert again == rooted
    assert widened != rooted
    assert compiled_at(adopter, widened) == base


def test_an_adoption_whose_merge_refuses_leaves_no_branch_standing(
    tmp_path: Path,
) -> None:
    """Both or neither: a branch rooted and never recorded blocks every retry.

    A staged change is what a freshly renamed checkout holds, and git refuses
    the recording merge over one -- after the branch was already rooted.
    """
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    wrote(adopter, "src/demo/staged.py", "staged = True\n")
    git("-C", str(adopter), "add", "src/demo/staged.py")

    with pytest.raises(sh.ErrorReturnCode):
        adopt(adopter, upstream, SOURCE, PACKAGE, base)

    assert branch_head(adopter, SOURCE.branch) == ""
    committed(adopter, "the staged work")
    adopt(adopter, upstream, SOURCE, PACKAGE, base)
    assert merged_at(adopter, SOURCE.branch) == base


def test_the_merge_base_carries_which_upstream_commit_was_taken(
    tmp_path: Path,
) -> None:
    """What each carrier stands at is read from git, not from a file kept beside it."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)

    assert merged_at(adopter, SOURCE.branch) == ""

    rooted = adopt(adopter, upstream, SOURCE, PACKAGE, base)

    assert compiled_at(adopter, rooted) == base
    assert merged_at(adopter, SOURCE.branch) == base


def test_the_library_pin_is_the_commit_the_lock_resolved(tmp_path: Path) -> None:
    """A project pinned to a branch names no commit; the lock names the one it got."""
    root = tmp_path / "project"
    wrote(
        root,
        "uv.lock",
        "version = 1\n\n"
        '[[package]]\nname = "other"\nversion = "1.0"\n\n'
        '[[package]]\nname = "lup-agents"\nversion = "0.2.0"\n'
        'source = { git = "https://example.test/lup?'
        'subdirectory=packages%2Flup&branch=dev#9f1c2d3e4a5b6c7d8e9f" }\n',
    )

    assert pinned_commit(root) == "9f1c2d3e4a5b6c7d8e9f"


def test_a_project_resolving_no_git_source_pins_no_commit(tmp_path: Path) -> None:
    """Every mode but git answers nothing here, and nothing is the honest answer."""
    root = tmp_path / "project"
    wrote(root, "uv.lock", 'version = 1\n\n[[package]]\nname = "lup-agents"\n')

    assert pinned_commit(root) == ""
