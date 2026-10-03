"""A file verdict names the protected-path rule it met, not only that one tripped.

A parked question said `edit:protected-path` for the policy's own code, a
lockfile, an environment file and a human-owned README alike, so whoever
answered it had to work out from the path which tree a person owns. Each
file's verdict carries the matched rule: how it matched, the root it names,
and that root in plain words -- the description the hook set declares, or the
root itself.

Each kind is put to the in-process policy and to both generated dispatchers,
over one repository, and read back from the question each runtime parks.
"""

from pathlib import Path

import pytest

from lup.policy.relay import CapturedFileReview, ProtectedMatch
from lup.providers.harness import every_runtime, runtime_trees
from lup_template.harness.catalog import declared_hook_set
from tests.unit.test_command_evidence import Runtime, judged, parked
from tests.unit.repos import commit_file, initialized_repo

MATCHED = [
    pytest.param(
        "packages/lup/src/lup/policy/notes.md",
        ProtectedMatch(
            kind="subtree",
            root="packages/lup/src/lup/policy",
            description="the policy's own code",
        ),
        id="declared-root",
    ),
    # Each runtime's own tree, as its adapter declares it, protected on both
    # runtimes whichever one runs.
    pytest.param(
        ".claude/agents/note.md",
        ProtectedMatch(
            kind="subtree",
            root=".claude",
            description="Claude Code's settings, trust state and skills",
        ),
        id="claude-tree",
    ),
    pytest.param(
        ".codex/agents/note.md",
        ProtectedMatch(
            kind="subtree",
            root=".codex",
            description="Codex's settings, trust state and skills",
        ),
        id="codex-tree",
    ),
    pytest.param(
        ".agents/plugins/marketplace.json",
        ProtectedMatch(
            kind="subtree",
            root=".agents/plugins",
            description="Codex's plugin marketplace, which decides the plugins "
            "Codex loads",
        ),
        id="codex-marketplace",
    ),
    pytest.param(
        ".lup/reviews/note.md",
        ProtectedMatch(
            kind="subtree",
            root=".lup/reviews",
            description="lup's own launch and review state",
        ),
        id="lup-state",
    ),
    pytest.param(
        ".env.example",
        ProtectedMatch(
            kind="name_prefix",
            root=".env",
            description="an environment file, where secrets are kept",
        ),
        id="environment-file",
    ),
    pytest.param(
        "src/app/devtools/fresh.py",
        ProtectedMatch(
            kind="new_devtools", root="src", description="a new devtools module"
        ),
        id="new-devtools-module",
    ),
    pytest.param(
        "README.md",
        ProtectedMatch(
            kind="exact", root="README.md", description="a file its human author owns"
        ),
        id="human-owned",
    ),
    pytest.param(
        "packages/app/uv.lock",
        ProtectedMatch(
            kind="contains_part",
            root="uv.lock",
            description="a manifest or lockfile an install trusts",
        ),
        id="lockfile",
    ),
]
"""A file under each kind of rule, and the match its verdict records."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repository holding a committed README, and nothing else of note."""
    root = tmp_path / "checkout"
    git = initialized_repo(root, tmp_path / "no-hooks")
    commit_file(git, root, "README.md", "read me\n", "chore: add README.md")
    (root / "tmp").mkdir()
    return root


def written(path: str) -> str:
    """A command writing one document whole into ``path``, and nothing else."""
    return f"cat > {path} <<'EOF'\nnew\nEOF"


@pytest.mark.parametrize(("path", "match"), MATCHED)
def test_each_file_names_the_rule_it_met_on_every_runtime(
    runtime: Runtime, checkout: Path, path: str, match: ProtectedMatch
) -> None:
    (checkout / path).parent.mkdir(parents=True, exist_ok=True)
    question = parked(runtime, checkout, written(path))

    rows = {row.path: row for row in question.file_reviews or []}

    assert rows[checkout / path].protected == match


@pytest.mark.parametrize(("path", "match"), MATCHED)
def test_the_in_process_policy_names_the_same_rule(
    checkout: Path, path: str, match: ProtectedMatch
) -> None:
    (checkout / path).parent.mkdir(parents=True, exist_ok=True)
    decision = judged(checkout, written(path))

    (row,) = [
        row
        for row in decision.file_reviews
        if Path(row["path"]).name == Path(path).name
    ]

    assert row["protected"] is not None
    assert ProtectedMatch.model_validate(row["protected"]) == match


def test_a_file_no_rule_protects_names_none(checkout: Path) -> None:
    decision = judged(checkout, written("tmp/scratch.md"))

    assert [row["protected"] for row in decision.file_reviews] == [None]


def test_a_verdict_recorded_before_the_rule_was_kept_still_reads() -> None:
    """The field is new, and a question already parked carries none."""
    recorded = {
        "path": "README.md",
        "effect": "ask",
        "reason": "README.md is human-authored",
        "rule": "edit:protected-path",
        "rules": ["edit:protected-path"],
        "before_sha256": None,
        "after_sha256": None,
    }

    assert CapturedFileReview.model_validate(recorded).protected is None


def test_every_supported_runtime_declares_its_own_tree_and_the_hooks_protect_each() -> (
    None
):
    """Each adapter names its tree, and the hook set holds every one of them.

    The provider-neutral catalog names no runtime's tree; it takes what every
    adapter declares, so a session on one runtime meets the other's tree
    protected too.
    """
    trees = runtime_trees()

    assert trees == [
        tree for runtime in every_runtime() for tree in runtime.protected_trees
    ]
    assert {tree.path for tree in trees} == {
        Path(".claude"),
        Path(".codex"),
        Path(".agents/plugins"),
    }
    assert set(trees) <= set(declared_hook_set().protected_roots())
