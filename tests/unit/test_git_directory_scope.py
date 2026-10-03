"""Which writes reach the repository, in a layout that keeps checkouts inside it.

A bare repository is a directory named ``<name>.git``, and `lup.devtools.layout`
puts every sibling worktree under ``tree/`` inside it. So the segment that names
the repository also prefixes every source file of every checkout, and a reading
that stops at the segment cannot tell the two apart -- it grades an ordinary
edit as a write to the repository, for every absolute path in the tree.

These pin the discriminator rather than the verdict: what follows the segment.
"""

from lup.policy.kernel.effects import WritesPath
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.kernel.words import (
    SCOPE_PHRASES,
    reaches_git_administration,
    write_scope,
)

ROLES = [PathRoleRow(root="tmp", role="scratch")]

BARE = "/home/user/project.git"
INTREE = "/home/user/project/.git"


def test_gits_own_contents_are_the_repository() -> None:
    """The entries a write there goes around every judged verb to reach."""
    for entry in ("config", "hooks/pre-commit", "objects/ab/cdef", "refs/heads/main"):
        assert reaches_git_administration(f"{BARE}/{entry}"), entry
        assert reaches_git_administration(f"{INTREE}/{entry}"), entry


def test_a_linked_worktrees_administration_is_the_repository() -> None:
    """The case the segment reading reaches past the top-level directory for."""
    assert reaches_git_administration(f"{BARE}/worktrees/feature/gitdir")


def test_the_directory_itself_counts_as_reaching_it() -> None:
    """A path that stops at the segment names the repository and nothing else."""
    assert reaches_git_administration(BARE)


def test_a_checkout_kept_inside_the_repository_directory_is_not_it() -> None:
    """The defect: source under ``project.git/tree/<name>`` is source.

    Every file of every worktree in this layout is spelled through the
    repository's own segment, so a reading that stopped there left no absolute
    path to a source file that was not graded as the repository.
    """
    assert not reaches_git_administration(f"{BARE}/tree/dev/src/module.py")
    assert not reaches_git_administration(f"{BARE}/tree/dev/tmp/scratch.py")


def test_what_a_layout_stores_beside_the_repository_is_not_it() -> None:
    """``tree`` is not special -- anything git does not own reads the same."""
    assert not reaches_git_administration(f"{BARE}/trace-archive/feature")


def test_a_directory_that_merely_ends_in_git_is_not_the_repository() -> None:
    """The segment is necessary and was never sufficient."""
    assert not reaches_git_administration("/home/user/notgit/src/module.py")
    assert not reaches_git_administration("/home/user/projectgit/src/module.py")


def test_the_scope_a_checkout_write_earns_is_no_longer_protected() -> None:
    """The reading the verdict rests on, through the function that serves it."""
    assert write_scope(f"{BARE}/config", ROLES) == "protected"
    assert write_scope(f"{BARE}/tree/dev/src/module.py", ROLES) != "protected"


def test_a_declared_role_still_outranks_the_spelling() -> None:
    """Unchanged: somebody saying where a path belongs beats this guessing."""
    assert write_scope("tmp/scratch.py", ROLES) == "scratch"


def test_every_scope_is_spelled_for_the_reason_line() -> None:
    """The table's whole job: a scope added upstream cannot arrive unspelled.

    A reason line naming the scope is the one place the vocabulary becomes
    prose, and the members do not share an article. Deriving it would read a
    letter; this asserts the set instead.
    """
    assert set(SCOPE_PHRASES) == set(WritesPath.scopes)
