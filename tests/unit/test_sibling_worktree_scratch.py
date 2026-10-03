"""Another checkout of this repository keeps this one's scratch.

The permissions page states one rule for writing into a sibling worktree's
`tmp/` and for deleting there, and a `cp` into it is no copy over production.
A session told to reach another checkout by absolute path would otherwise
meet a question on every delete of its own scratch. The
host names the other checkouts; the kernel roots this checkout's declared
scratch at each, and nothing else of theirs.

Every question asking whether a path is this repository's scratch reads those
rows: a `git init` making a probe kit there, a hand-written plugin tree in
that kit, an edit of a file in it, and where a write lands in a container --
which had put a sensitive assignment beside a write into a sibling's `tmp/`
to the operator. And the Git question naming the other checkouts is asked
before the edit gates, which can spend the hook's whole deadline on a type
checker and had left it answering that there were none.
"""

import os
import time
from pathlib import Path

import pytest

from lup.harness.codescan.antipatterns import PYTHON_ANTI_PATTERNS
from lup.harness.enforcement import declared_role_rows
from lup.policy.assets.host import landed_targets, sibling_worktrees, this_checkout_path
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.edit import awaits_resolution
from lup.policy.kernel.roles import declared_scratch, path_role, sibling_scratch_rows
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.kernel.shell import decide_shell
from lup.policy.rules import antipattern_row
from lup.policy.shell_rules import erase_shell_rules
from lup_template.harness.catalog import declared_hook_set
from tests.unit.bundled import bundled
from tests.unit.repos import commit_file, initialized_repo

ROLES = [
    PathRoleRow(root="**/tmp", role="scratch"),
    PathRoleRow(root="tests", role="test"),
]

CHECKOUT = "/home/someone/checkout"
"""Where the session's own checkout stands, clear of the machine's `/tmp`."""

SIBLING = "/home/someone/sibling"
"""Another worktree of the same repository, beside the session's."""

SHELL_ROWS = erase_shell_rules(declared_hook_set().resolved_shell_rules())
PATH_ROLES = declared_role_rows(list(declared_hook_set().path_roles))

DISPATCHER = Path(".claude/plugins/lup/hooks/scripts/policy.py")


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


def test_another_checkouts_scratch_is_declared_scratch_and_nothing_beside_it() -> None:
    """The rows the host roots at a sibling answer the narrower question too.

    Declared scratch is what a `git init` and a plugin tree are granted on, and
    it read only paths spelled relative to the session's own checkout, so a
    sibling's `tmp/` was nobody's. The roots the kernel knows unaided -- the
    machine's temporary root -- still belong to no checkout.
    """
    rows = [*ROLES, *sibling_scratch_rows(["/srv/tree/sibling"], ROLES)]

    assert declared_scratch("tmp/kit/probe.py", rows)
    assert declared_scratch("/srv/tree/sibling/tmp/kit/probe.py", rows)
    assert not declared_scratch("/srv/tree/sibling/tests/test_a.py", rows)
    assert not declared_scratch("/srv/tree/other/tmp/probe.py", rows)
    assert not declared_scratch("/tmp/probe.py", rows)
    assert not declared_scratch("/srv/tree/sibling/tmp/$X", rows)


def judged(command: str) -> KernelDecision:
    """One command from the session's checkout, with its sibling's scratch rooted."""
    return decide_shell(
        command,
        SHELL_ROWS,
        path_roles=[*PATH_ROLES, *sibling_scratch_rows([SIBLING], PATH_ROLES)],
        checkout_root=CHECKOUT,
        plugin_roots=[
            root.as_posix() for root in declared_hook_set().generated_plugin_roots
        ],
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"git init -q {SIBLING}/tmp/stage", id="absolute"),
        pytest.param(
            f"cd {SIBLING} && mkdir -p tmp/stage && git init -q tmp/stage",
            id="after-cd",
        ),
        pytest.param(f"git -C {SIBLING} init -q tmp/stage", id="git-C"),
    ],
)
def test_a_repository_made_in_another_checkouts_scratch_is_a_scratch_write(
    command: str,
) -> None:
    """A `git init` in another checkout's scratch is allowed as it is in this one's."""
    decided = judged(command)

    assert decided.effect == "allow", decided.reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"git init -q {SIBLING}/src/stage", id="sibling-production"),
        pytest.param("git init -q /home/someone/other/tmp/stage", id="no-checkout"),
        pytest.param(f"git init -q --template=/x {SIBLING}/tmp/stage", id="template"),
    ],
)
def test_a_repository_made_anywhere_else_stays_unclassified(command: str) -> None:
    assert judged(command).effect != "allow"


def test_a_plugin_tree_in_another_checkouts_scratch_is_a_kits_own() -> None:
    """Nothing this project generates lands in its scratch, on any branch."""
    assert judged(f"date > {SIBLING}/tmp/kit/.claude/plugins/p/x.md").effect == "allow"
    assert judged(f"date > {SIBLING}/.claude/plugins/lup/x.md").effect == "deny"


def test_another_checkout_lands_as_the_checkout_and_a_lent_tree_does_not(
    tmp_path: Path,
) -> None:
    """A sibling worktree is the tree a session works in, not one lent to it.

    Landed as the host's, a write into a sibling's `tmp/` kept a sensitive
    assignment on the same line from being settled inside the container,
    which the same write into the session's own `tmp/` did not.
    """
    checkout, sibling, lent = (
        tmp_path / name for name in ("checkout", "sibling", "lent")
    )
    for tree in (checkout, sibling, lent):
        tree.mkdir()
    targets = ["notes.md", str(sibling / "tmp" / "x"), str(lent / "x")]

    assert landed_targets(
        targets, [str(lent), str(sibling)], checkout, [str(sibling)]
    ) == [
        ["notes.md", "checkout"],
        [str(sibling / "tmp" / "x"), "checkout"],
        [str(lent / "x"), "host"],
    ]
    assert landed_targets(targets, [str(lent), str(sibling)], checkout)[1] == [
        str(sibling / "tmp" / "x"),
        "host",
    ]


def test_a_file_in_another_checkout_is_spelled_as_that_checkout_spells_it(
    tmp_path: Path,
) -> None:
    """What the edit gates read a kit's scratch claim off, sibling or not."""
    main = tmp_path / "main"
    git = initialized_repo(main, tmp_path / "no-hooks")
    commit_file(git, main, "README.md", "probe\n", "init")
    sibling = tmp_path / "sibling"
    git("worktree", "add", str(sibling), "-b", "sibling")

    assert this_checkout_path("tmp/kit/probe.py", main) == "tmp/kit/probe.py"
    assert this_checkout_path(str(sibling / "tmp/kit/probe.py"), main) == (
        "tmp/kit/probe.py"
    )
    assert this_checkout_path(str(tmp_path / "elsewhere/probe.py"), main) == ""


def test_a_checker_is_started_only_where_its_answer_decides_something() -> None:
    """The conventions read production alone, so nothing else needs a checker.

    A type checker started for a scratch script spent seconds of the hook's
    deadline on an answer no gate would read.
    """
    rows = [antipattern_row(rule) for rule in PYTHON_ANTI_PATTERNS]
    line = "value = str(text).replace('a', 'b')\n"

    assert awaits_resolution(None, line, rows, True, "src/app/module.py", PATH_ROLES)
    assert not awaits_resolution(None, line, rows, True, "tmp/probe.py", PATH_ROLES)
    assert not awaits_resolution(
        None, line, rows, True, "tests/unit/test_probe.py", PATH_ROLES
    )


def test_a_checker_that_spends_the_deadline_leaves_every_git_fact_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Git facts are read before the edit gates, so a slow checker starves none.

    Read after them, a checker answering at the hook's deadline left `git`
    nothing to answer in: no file was tracked, so a redirect replacing
    reviewed source behind a heredoc the checker read went unasked.
    """
    root = tmp_path / "checkout"
    git = initialized_repo(root, tmp_path / "no-hooks")
    (root / "src/app").mkdir(parents=True)
    commit_file(git, root, "src/app/module.py", "VALUE = 1\n", "init")
    commit_file(git, root, "src/app/other.py", "OTHER = 1\n", "other")
    dispatcher = bundled("bundled_claude_policy_deadline", DISPATCHER)

    def spent(
        path_text: str, proposed: str, command: list[str], timeout_seconds: float = 30
    ) -> dict[str, dict[str, list[int]]]:
        """A checker that answers, and leaves the hook no time after it."""
        os.environ["LUP_HOOK_DEADLINE"] = repr(time.monotonic())
        return {"refuted": {"string-replace": [2]}, "unresolved": {}}

    monkeypatch.setenv("LUP_HOOK_DEADLINE", repr(time.monotonic() + 25))
    monkeypatch.setattr(dispatcher, "resolved_refutations", spent)
    command = (
        "cat >> src/app/other.py <<'X'\nvalue = str(OTHER).replace('a', 'b')\nX\n"
        "date > src/app/module.py"
    )

    decided = dispatcher.bash_decision(
        command, None, False, True, True, root, park=False
    )

    assert decided.effect == "ask"
    assert "src/app/module.py" in decided.addressed()
