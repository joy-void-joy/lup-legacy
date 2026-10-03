"""What `git -C` costs: nothing, because the verb behind it is what is judged.

A directory redirect moves the command to another tree and changes nothing
about what the command does there. `cd there && git <verb>` is two segments
judged on their own, so a question on the `-C` spelling of the same act would
deter nothing -- the `cd` spelling is always the way through -- while costing a
turn on every sibling worktree, every project the sync registry mounts, and
every relative or variable-carried path.

A commit's reversibility belongs to the reflog of the tree it runs in, and
that reflog is exactly as present in the other tree: the redirect makes it
that tree's reflog, not nobody's. So the verb's own row answers -- a commit
allows, a merge asks, a push with a deleted ref asks -- wherever the flag
points and however the path is spelled.
"""

from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.rows import PathRoleRow, ShellRuleRow
from lup.policy.kernel.shell import decide_shell
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

SCRATCH = [PathRoleRow(root="tmp", role="scratch")]


def rows() -> list[ShellRuleRow]:
    """The library's offered table, which is what the contract describes."""
    return erase_shell_rules(default_vocabulary())


def verdict(command: str) -> KernelDecision:
    return decide_shell(command, rows(), path_roles=SCRATCH)


def test_a_redirect_in_front_of_a_read_costs_nothing() -> None:
    for command in (
        "git -C . log",
        "git -C packages/lup status",
        "git -C /etc/somerepo log",
        "git -C ../sibling status",
        "git -C /home/other/project diff",
    ):
        assert verdict(command).effect == "allow", command


def test_a_redirect_in_front_of_a_reversible_mutation_costs_nothing_either() -> None:
    """A mutation in another tree, which `cd` never asks about either.

    Every spelling of the path -- absolute, relative, a variable, any other
    repository -- because reaching another checkout is the work.
    """
    for command in (
        "git -C packages/lup commit -m x",
        "git -C . commit -m x",
        "git -C ../sibling commit -m x",
        'git -C "$W" add -A',
        "git -C /tmp/elsewhere commit -m x",
        "git --git-dir /tmp/elsewhere/.git status",
        "git --work-tree /tmp/elsewhere add -A",
    ):
        assert verdict(command).effect == "allow", command

    assert verdict("cd /tmp/elsewhere && git commit -m x").effect == "allow"


def test_the_verb_behind_the_redirect_is_still_judged() -> None:
    """Stepping aside hands the question on rather than answering it.

    A force without a lease asks for its own reason -- it can discard what
    somebody else pushed -- and a push that deletes a remote ref asks for
    its own, and neither reason changes with the checkout the verb runs in.
    """
    forced = verdict("git -C ../sibling push --force origin topic")
    assert forced.effect == "ask"
    assert "--force-with-lease" in forced.addressed()

    assert verdict("git -C ../sibling push --delete origin topic").effect == "ask"
    assert verdict("git -C ../sibling reset --hard").effect == "ask"


def test_a_global_that_changes_how_git_runs_still_asks() -> None:
    """The globals that stay guarded: they name a program or move a ref."""
    for command in (
        "git --exec-path=/tmp/x status",
        "git --namespace=other log",
        "git -c core.pager=less log",
    ):
        settled = verdict(command)
        assert settled.effect == "ask", command
        assert "global flag" in settled.reason, command
