"""Which tree the reference pages resolve lup's fixture citations against.

The capability page cites fixtures living in lup's own repository, and a
project that took lup as a distribution has the code they pin and none of
them. Which of the two a checkout is is what `[tool.uv.sources]` declares, so
what these pin is that the declaration decides it: a `packages/lup/tests/`
left on disk by `--keep-vendored`, or by an un-vendoring that stopped early,
describes no lup that is being resolved and gets no vote on the pages.
"""

from pathlib import Path

import pytest

from lup_template.harness.content.docs import catalog

RUNTIME_FIXTURES = "packages/lup/tests/unit/test_adapter_runtime.py"
"""The citation only a tree holding lup's own suite can resolve."""

DISPATCHER_FIXTURES = "tests/unit/test_harness_compilation.py"
"""The other one, cited against lup's repository for the same reason."""

EXEC_FIXTURES = "tests/integration/test_codex_exec_governance.py"
"""The third, which settles whether a generated tree governs a `codex exec`."""

PUBLICATION_FIXTURES = "packages/lup/tests/unit/test_codex_plugin_publication.py"
"""The fourth, which settles that publishing a plugin keeps live revisions."""

AUTHENTICATION_FIXTURES = "packages/lup/tests/unit/test_codex_launch_auth.py"
"""The fifth, which settles that a launch verifies its own credential."""

GIT_SOURCE = '{ git = "https://github.com/example/lup", branch = "main" }'
"""One of the three distribution modes, spelled as pyproject spells it."""


def project_resolving_lup(root: Path, source: str) -> Path:
    """A checkout whose only declaration is where its ``lup`` comes from.

    The package directory is there because one page draws the application's
    layout by walking it, and a tree with nothing to walk is not a checkout
    these pages describe. Nothing here reads its contents.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\n\n[tool.uv.sources]\nlup-agents = {source}\n',
        encoding="utf-8",
    )
    package = root / "src" / "lup_template" / "agent"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").touch()
    return root


def cited_fixtures(root: Path, *paths: str) -> None:
    """Place the files the capability page names, where a tree holds them."""
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).touch()


def test_a_vendored_tree_left_behind_does_not_make_a_git_mode_local(
    tmp_path: Path,
) -> None:
    """The directory is present and the mode still says the fixtures are not."""
    project = project_resolving_lup(tmp_path, GIT_SOURCE)
    (project / "packages" / "lup" / "tests").mkdir(parents=True)

    assert catalog.documents(project)


def test_the_mode_decides_whether_a_citation_is_checked_not_what_is_published(
    tmp_path: Path,
) -> None:
    """Both modes publish the same roster; only one of them resolves paths."""
    distribution = project_resolving_lup(tmp_path / "adopter", GIT_SOURCE)
    vendored = project_resolving_lup(tmp_path / "lup", "{ workspace = true }")
    cited_fixtures(
        vendored,
        RUNTIME_FIXTURES,
        DISPATCHER_FIXTURES,
        EXEC_FIXTURES,
        PUBLICATION_FIXTURES,
        AUTHENTICATION_FIXTURES,
    )

    published = [page.semantic_id for page in catalog.documents(distribution)]

    assert published == [page.semantic_id for page in catalog.documents(vendored)]


def test_a_local_mode_still_fails_on_a_citation_that_moved(tmp_path: Path) -> None:
    """Where the fixtures are required to be, a dead citation stops generation."""
    vendored = project_resolving_lup(tmp_path, "{ workspace = true }")
    cited_fixtures(
        vendored,
        DISPATCHER_FIXTURES,
        EXEC_FIXTURES,
        PUBLICATION_FIXTURES,
        AUTHENTICATION_FIXTURES,
    )

    with pytest.raises(ValueError, match="test_adapter_runtime.py"):
        catalog.documents(vendored)
