"""What an adopting domain inherits from the scaffold and never wanted.

`examples/` composes lup's own runtime against lup's own README, so a project
built *on* lup inherits demonstrations of the thing it is merely a consumer
of. Removing them is mechanical and always right, which is why it is a command
rather than a judgement in the interview — and why what it reports afterwards
matters more than what it deletes: the lines still naming a directory that has
gone are the part somebody has to repair by hand.
"""

from pathlib import Path

import pytest

from lup.devtools.dev.scaffold import ScaffoldSource
from lup.harness.ownership import OwnedArtifact, OwnershipManifest
from lup_template.devtools.dev.init import (
    SCAFFOLD_DEMONSTRATIONS,
    drop_scaffold_demonstrations,
    mention_pattern,
    surviving_mentions,
    undeclined_copies,
)
from tests.unit.repos import initialized_repo


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A committed tree holding what the scaffold ships, and prose naming it.

    A repository rather than a directory, because what the scan reads is what
    git holds: the ignore rules are half of what it is asked.
    """
    work = tmp_path / "checkout"
    git = initialized_repo(work, tmp_path / "no-hooks")
    for path in SCAFFOLD_DEMONSTRATIONS:
        target = work / path
        if path.suffix:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("from examples.one_shot import main\n", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / "one_shot.py").write_text(
                "from examples.common import Summary\n", encoding="utf-8"
            )
    (work / "README.md").write_text(
        "See [runtime examples](examples/README.md), which run end to end.\n"
        "This paragraph merely mentions examples. It names no path.\n",
        encoding="utf-8",
    )
    (work / "catalog.py").write_text('composition = ["examples/"]\n', "utf-8")
    (work / ".gitignore").write_text(
        ".venv-contained\npackages/lup/web/node_modules/\n", encoding="utf-8"
    )
    git("add", "--all")
    git("commit", "--quiet", "-m", "scaffold")
    return work


def test_a_dry_run_reports_what_it_would_remove_and_removes_nothing(
    checkout: Path,
) -> None:
    removed = drop_scaffold_demonstrations(checkout, dry_run=True)

    assert len(removed) == len(SCAFFOLD_DEMONSTRATIONS)
    for path in SCAFFOLD_DEMONSTRATIONS:
        assert (checkout / path).exists()


def test_what_is_absent_already_is_not_reported_as_removed(tmp_path: Path) -> None:
    """Running twice, or on a domain that pruned by hand, says so rather than fails."""
    assert drop_scaffold_demonstrations(tmp_path, dry_run=False) == []


def test_a_path_reference_is_reported_and_a_bare_word_is_not(checkout: Path) -> None:
    """The name is an ordinary English word; only the separator makes it a path."""
    mentions = surviving_mentions(checkout, SCAFFOLD_DEMONSTRATIONS)

    assert any("README.md:1" in line for line in mentions)
    assert not any("README.md:2" in line for line in mentions)


def test_the_composition_root_that_goes_dead_is_reported(checkout: Path) -> None:
    """`"examples/"` is a sanctioned root, and sanctioning a gone tree is dead."""
    assert any(
        "catalog.py" in line
        for line in surviving_mentions(checkout, SCAFFOLD_DEMONSTRATIONS)
    )


def test_a_file_inside_what_is_going_is_not_reported(checkout: Path) -> None:
    """It names its siblings constantly and leaves with them."""
    mentions = surviving_mentions(checkout, SCAFFOLD_DEMONSTRATIONS)

    assert not any("examples/one_shot.py" in line for line in mentions)


def test_what_the_ignore_rules_keep_out_is_never_read(checkout: Path) -> None:
    """An environment and a dependency tree name the directory, and are nobody's.

    The two trees an adopter's report was buried under: the contained
    session's virtual environment, whose installed packages say `examples/`
    in their own docstrings, and the frontend's dependencies. Neither is a
    line anybody repairs, and both went unlisted in a skip list that could
    only ever name the trees somebody had already thought of.
    """
    for tree in [
        ".venv-contained/lib/python3.14/site-packages/pydantic",
        "packages/lup/web/node_modules/vite",
    ]:
        (checkout / tree).mkdir(parents=True)
        (checkout / tree / "fields.py").write_text(
            '"""See examples/README.md for a worked schema."""\n', encoding="utf-8"
        )
        (checkout / tree / "README.md").write_text(
            "Run `uv run -m examples.one_shot` first.\n", encoding="utf-8"
        )

    mentions = surviving_mentions(checkout, SCAFFOLD_DEMONSTRATIONS)

    assert not any(".venv-contained" in line for line in mentions)
    assert not any("node_modules" in line for line in mentions)
    assert any(line.startswith("  README.md:1:") for line in mentions)


def test_a_file_written_since_the_last_commit_is_still_read(checkout: Path) -> None:
    """Untracked is not ignored: prose written a minute ago is somebody's."""
    (checkout / "NOTES.md").write_text(
        "Walk through examples/one_shot.py before the review.\n", encoding="utf-8"
    )

    mentions = surviving_mentions(checkout, SCAFFOLD_DEMONSTRATIONS)

    assert any("NOTES.md:1" in line for line in mentions)


def test_what_the_checkout_holds_for_somebody_else_is_not_read(
    checkout: Path,
) -> None:
    """A generated page, the vendored library, and a file about the removal itself.

    Each names the directory and none is this project's line to repair: the
    page is rewritten from its source, the library is upstream's prose, and
    the skill running the removal names what it removes because that is its
    subject.
    """
    naming = "Run `uv run -m examples.one_shot` first.\n"
    page = checkout / "docs" / "runs.md"
    library = checkout / "packages" / "lup" / "src" / "lup" / "sessions.py"
    skill = (
        checkout / "src" / "pkg" / "harness" / "content" / "skills" / "init.passage.md"
    )
    for written in [page, library, skill]:
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(naming, encoding="utf-8")
    manifest = OwnershipManifest(
        schema_version=1,
        generator_version="0",
        source_digest="0",
        target_requirements=[],
        files=[
            OwnedArtifact(
                path=Path("docs/runs.md"),
                category="generated",
                sha256="0",
                semantic_id="docs.runs",
            )
        ],
    )
    (checkout / ".claude").mkdir()
    (checkout / ".claude" / ".lup-ownership.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )

    mentions = surviving_mentions(checkout, SCAFFOLD_DEMONSTRATIONS)

    assert not any("docs/runs.md" in line for line in mentions)
    assert not any("packages/lup" in line for line in mentions)
    assert not any("init.passage.md" in line for line in mentions)
    assert any(line.startswith("  README.md:1:") for line in mentions)


def test_a_removed_copy_is_named_until_the_scaffold_declines_it() -> None:
    """Deleted here and still compiled upstream, a file is an update's conflict.

    Spelled as upstream spells it, which is how a decline is written; a path
    outside every copied root is this project's own and has nothing to decline.
    """
    removed = [Path("examples"), Path("tests/unit/test_policy_examples.py")]

    assert undeclined_copies(removed, ScaffoldSource(), "pkg") == [
        "tests/unit/test_policy_examples.py"
    ]
    assert undeclined_copies([Path("src/pkg/demo.py")], ScaffoldSource(), "pkg") == [
        "src/lup_template/demo.py"
    ]
    assert (
        undeclined_copies(removed, ScaffoldSource(declined=["tests/unit"]), "pkg") == []
    )


@pytest.mark.parametrize(
    ("line", "named"),
    [
        ("[runtime examples](examples/README.md)", True),
        ("uv run -m examples.one_shot", True),
        ("from examples.common import Summary", True),
        ("updating examples.", False),
        ("a paragraph about examples", False),
        ("these examples, and others", False),
    ],
)
def test_the_module_spelling_needs_a_name_after_its_dot(line: str, named: bool) -> None:
    """A sentence ending in "examples." is prose, not a reference to the package."""
    assert bool(mention_pattern(Path("examples")).search(line)) is named
