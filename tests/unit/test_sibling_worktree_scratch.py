"""Another checkout of this repository keeps this one's scratch.

The permissions page states one rule for writing into a sibling worktree's
`tmp/` and for deleting there, and a `cp` into it is no copy over production.
A session told to reach another checkout by absolute path would otherwise
meet a question on every delete of its own scratch. The
host names the other checkouts; the kernel roots this checkout's declared
scratch at each, and nothing else of theirs.
"""

from pathlib import Path

from lup.policy.assets.host import sibling_worktrees
from lup.policy.kernel.roles import path_role, sibling_scratch_rows
from lup.policy.kernel.rows import PathRoleRow
from tests.unit.repos import commit_file, initialized_repo

ROLES = [
    PathRoleRow(root="**/tmp", role="scratch"),
    PathRoleRow(root="tests", role="test"),
]


def test_the_host_names_every_other_checkout_and_neither_this_one_nor_the_bare(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    git = initialized_repo(main, tmp_path / "no-hooks")
    commit_file(git, main, "README.md", "probe\n", "init")
    git("worktree", "add", str(tmp_path / "sibling"), "-b", "sibling")

    assert sibling_worktrees(main) == [str(tmp_path / "sibling")]
    assert sibling_worktrees(tmp_path / "sibling") == [str(main)]


def test_only_scratch_crosses_into_another_checkout() -> None:
    rows = sibling_scratch_rows(["/srv/tree/sibling"], ROLES)

    assert rows == [PathRoleRow(root="/srv/tree/sibling/**/tmp", role="scratch")]
    assert path_role("/srv/tree/sibling/tmp/run.log", rows) == "scratch"
    assert path_role("/srv/tree/sibling/pkg/tmp/run.log", rows) == "scratch"
    assert path_role("/srv/tree/sibling/tests/test_a.py", rows) == "production"
    assert path_role("/srv/tree/other/tmp/run.log", rows) == "production"
