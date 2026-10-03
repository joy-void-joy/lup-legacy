"""A verbatim asset names its source the same from every checkout.

Generation reads an asset's bytes from wherever its declaring package was
imported, and writes them into whatever root it was handed. Those are the same
directory only when the command runs from the tree it targets — regenerate a
sibling worktree and they part, which is ordinary rather than exotic: it is
what a sweep landing several branches does all afternoon.

The attribution is committed, so naming the asset against the target root does
not merely read badly when the two part. It pins one machine's directory
layout into a tracked file, points a reader at a checkout that may not exist
for them, and makes one declaration compile to different bytes depending on the
caller's working directory — which is the thing a generated tree must never do,
because the drift check can then no longer tell a stale artifact from a
relocated one.

Driven through the recipe rather than through the wired targets: a target
builds the documents too, and those read directories under the root, so a root
chosen to be foreign would fail there long before reaching the question these
ask.
"""

from pathlib import Path

from lup.harness.generate import ProjectContent
from lup.providers.claude.composition import claude_generation_recipe
from lup_template.harness.catalog import portable_harness

ASSET = Path("src/lup_template/harness/content/assets/file_suggest.sh")
"""The one file copied verbatim, whose source is a path and not a module."""


def attribution(root: Path) -> str:
    """What the copied asset says it came from, when generating into ``root``."""
    content = ProjectContent(
        harness=portable_harness(root=root), assets=[Path.cwd() / ASSET]
    )
    return next(
        one.banner.attribution()
        for one in claude_generation_recipe(root, content).desired.artifacts
        if one.banner and one.path.name == ASSET.name
    )


def test_the_asset_is_named_relative_to_the_project_that_holds_it() -> None:
    """The row is a path inside a checkout, not a path on one machine."""
    named = attribution(Path.cwd())

    assert named == ASSET.as_posix()
    assert not Path(named).is_absolute()


def test_a_foreign_root_does_not_change_what_the_asset_is_named() -> None:
    """Generating into a sibling worktree is where the absolute path leaked."""
    sibling = Path.cwd().parent / "some-other-worktree"

    assert attribution(sibling) == attribution(Path.cwd())


def test_no_root_anywhere_makes_the_attribution_absolute() -> None:
    """A root sharing no prefix with the asset is the sharpest form of it."""
    assert not Path(attribution(Path("/nowhere/at/all"))).is_absolute()
