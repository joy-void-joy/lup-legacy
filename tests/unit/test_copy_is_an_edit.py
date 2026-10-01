"""A copy over a file is judged as the edit it makes.

`cp new.py src/app.py` leaves in `src/app.py` what `new.py` holds, which is the
edit an `Edit` of the file would make. It was judged as a loss instead --
"copying over files requires approval", settled only where a capture of the
session's checkout held the file -- so a copy into a sibling worktree parked
for the operator while an `Edit` of the same file went through, and a copy
into this checkout went through with no content gate reading what landed.
"""

from pathlib import Path

import pytest

from lup.harness.enforcement import declared_role_rows, semantic_policy_for
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.rows import RewrittenDocumentRow
from lup.policy.kernel.shell import decide_shell
from lup.policy.models import ShellCommand
from lup.policy.shell_rules import erase_shell_rules
from lup_template.harness.catalog import declared_hook_set
from tests.unit.repos import initialized_repo

MODULE = "src/app/module.py"
"""A production module in the fixture checkout, which no rule protects."""

SHELL_ROWS = erase_shell_rules(declared_hook_set().resolved_shell_rules())
PATH_ROLES = declared_role_rows(list(declared_hook_set().path_roles))


def module_text(lines: int) -> str:
    """A module of this many one-line functions."""
    return "".join(
        f"def f{index}() -> int:\n    return {index}\n\n\n" for index in range(lines)
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout with one committed production module and a scratch tree."""
    root = tmp_path / "checkout"
    git = initialized_repo(root, tmp_path / "hooks")
    (root / "src/app").mkdir(parents=True)
    (root / MODULE).write_text(module_text(8), encoding="utf-8")
    (root / "tmp").mkdir()
    git("add", "-A")
    git("commit", "-m", "base")
    return root


def scratch(checkout: Path, name: str, text: str) -> str:
    """A file under the checkout's scratch tree, named as a command names it."""
    (checkout / "tmp" / name).write_text(text, encoding="utf-8")
    return f"tmp/{name}"


def judged(checkout: Path, command: str) -> tuple[str, str, str | None]:
    """What the composed policy answers, and why, and whose answer a deferral is."""
    decision = semantic_policy_for(declared_hook_set()).decide(
        ShellCommand(command=command, cwd=checkout)
    )
    return decision.effect, decision.reason, decision.abstention


def test_a_copy_over_a_module_is_judged_as_the_edit_it_makes(checkout: Path) -> None:
    """The real difference is read, as an `Edit` of the file is read."""
    small = scratch(checkout, "small.py", module_text(8) + "X = 1\n")
    large = scratch(
        checkout,
        "large.py",
        module_text(8) + "".join(f"G{index} = {index}\n" for index in range(12)),
    )

    assert judged(checkout, f"cp {small} {MODULE}")[0] == "allow"
    effect, reason, abstention = judged(checkout, f"cp {large} {MODULE}")
    assert (effect, abstention) == ("defer", "provider_native")
    assert f"cp would write {MODULE}" in reason


def test_a_copy_is_read_by_the_gates_an_edit_meets(checkout: Path) -> None:
    """A dropped note and an introduced anti-pattern are refused as an edit's are."""
    (checkout / MODULE).write_text(
        module_text(2) + "# lup: fix the second function\n", encoding="utf-8"
    )
    dropped = scratch(checkout, "dropped.py", module_text(2))
    cast = scratch(
        checkout,
        "cast.py",
        module_text(2) + "# lup: fix the second function\n"
        "from typing import cast\nVALUE = cast(int, 1)\n",
    )

    assert judged(checkout, f"cp {dropped} {MODULE}")[0] == "deny"
    assert judged(checkout, f"cp {cast} {MODULE}")[0] != "allow"


def test_a_copy_into_a_sibling_worktree_answers_as_one_here_does() -> None:
    """No capture of this checkout holds a sibling's file, and none is asked for.

    Measured from a session rooted in one worktree, a copy over a module in
    another reached by its absolute path parked as "copying over files
    requires approval", while an `Edit` of the same file went through. The
    host hands the kernel the document the copy leaves, and the kernel
    answers with the edit gates' verdict on it; a copy the host could not
    read keeps the verb's own question. Spelled under `/home` because every
    path under `/tmp`, where a fixture would stand, is scratch.
    """
    target = f"/home/someone/sibling/{MODULE}"
    command = f"cp tmp/small.py {target}"
    document = RewrittenDocumentRow(
        target=target,
        path=MODULE,
        before=module_text(8),
        after=module_text(8) + "X = 1\n",
        operation="modify",
        foreign=False,
        outside_project=False,
        checkout_path="",
        resolution=None,
    )

    def decided(rows: list[RewrittenDocumentRow]) -> KernelDecision:
        return decide_shell(
            command,
            SHELL_ROWS,
            path_roles=PATH_ROLES,
            existing_targets=[target],
            checkout_root="/home/someone/checkout",
            rewritten_documents=rows,
        )

    judged_copy = decided([document])
    unread_copy = decided([])

    assert judged_copy.effect == "allow", judged_copy.reason
    assert (unread_copy.effect, unread_copy.headline()) == (
        "ask",
        "asks: `cp` — copying over files requires approval",
    )


def test_a_copy_that_brings_a_file_into_being_keeps_the_verb_s_row(
    checkout: Path,
) -> None:
    """Only a copy over a file is an edit of it; a new file is a creation."""
    small = scratch(checkout, "small.py", module_text(8))

    effect, reason, _ = judged(checkout, f"cp {small} src/app/fresh.py")

    assert "copying over files requires approval" in reason
    assert effect in ("ask", "allow")
