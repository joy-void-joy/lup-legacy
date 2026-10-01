"""A scratch copy mirroring a devtools module is scratch.

The rule asking before a new devtools module names one by its parts --
`src/<package>/devtools` -- wherever they sit, so a copy under `tmp/` that
mirrors the path it will land at would ask "new devtools module requires
approval" as the module itself does. Scratch is scratch whatever path it
mirrors, as it is for the manifest rules named the same way.
"""

from lup.harness.enforcement import declared_role_rows
from lup.policy.kernel.edit import decide_edit
from lup.policy.kernel.shell import decide_shell
from lup.policy.rules import invariant_path_rules, path_rule_row
from lup.policy.shell_rules import erase_shell_rules
from lup_template.harness.catalog import declared_hook_set

SHELL_ROWS = erase_shell_rules(declared_hook_set().resolved_shell_rules())
PATH_ROLES = declared_role_rows(list(declared_hook_set().path_roles))


def test_a_scratch_copy_of_a_new_devtools_module_is_scratch() -> None:
    """The rule names a devtools module by its parts, wherever they sit."""
    rules = [path_rule_row(rule) for rule in invariant_path_rules()]

    def created(path: str) -> str:
        return decide_edit(
            path,
            None,
            '"""A module."""\n',
            path_exists=False,
            antipattern_rows=[],
            path_rules=rules,
            path_roles=PATH_ROLES,
            operation="create",
        ).effect

    assert created("tmp/copy/packages/lup/src/lup/devtools/dev/fresh.py") == "allow"
    assert created("tmp/copy/src/app/devtools/fresh.py") == "allow"
    assert created("packages/lup/src/lup/devtools/dev/fresh.py") == "ask"
    written = decide_shell(
        "echo 'x = 1' > tmp/copy/packages/lup/src/lup/devtools/dev/fresh.py",
        SHELL_ROWS,
        path_roles=PATH_ROLES,
        path_rules=rules,
        existing_targets=[],
    )
    assert written.effect == "allow", written.reason
