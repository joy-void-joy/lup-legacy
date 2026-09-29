"""Conversation retention is part of Lup's inherited defaults."""

from lup.harness.codescan.common import RuleSelection
from lup.harness.content.application import ApplicationLayout
from lup.harness.content.modules.catalog import library_modules
from lup.harness.content.modules.specs import CONVERSATION
from lup.harness.content.modules.specs import LIBRARY_SPECS as LIBRARY_MODULES
from lup.harness.modules import (
    Adoption,
    ModuleSelection,
    adopted,
    composed_content,
    scaffold_selection,
    unmet_requirements,
)
from lup.devtools.roster import LIBRARY_SPECS


def library_roster() -> list[str]:
    """Every skill lup ships, read through the modules that declare them.

    There is no second list to read it off: a skill reaches a project because
    its module does, so composing the roster is the only statement of what lup
    ships. That a module claims every declaration on disk is `dev check`'s
    business rather than this file's.
    """
    modules = adopted(
        library_modules(ApplicationLayout(package="example"), RuleSelection()),
        scaffold_selection(LIBRARY_MODULES),
    )
    return [skill.name for skill in composed_content(modules).skills]


def test_conversation_is_a_library_subapp_default() -> None:
    assert "conversation" in [spec.name for spec in LIBRARY_SPECS]


def test_analyze_is_a_library_skill_default() -> None:
    assert "analyze" in library_roster()


def test_analyze_leaves_with_the_conversation_module() -> None:
    """The skill's first step is the conversation sub-app, so it is that module's.

    Declared anywhere else, a project declining retention would ship a
    walkthrough that fails at step one, naming a command its CLI does not
    serve — which is what the documented-command sweep refuses a tree for.
    """
    without = adopted(
        library_modules(ApplicationLayout(package="example"), RuleSelection()),
        ModuleSelection(
            adoptions=[
                *scaffold_selection(
                    [spec for spec in LIBRARY_MODULES if spec.id != "conversation"]
                ).adoptions,
                Adoption(module="conversation", taken=False),
            ]
        ),
    )

    assert "analyze" not in [skill.name for skill in composed_content(without).skills]


def test_retention_logs_in_through_its_own_command_tree() -> None:
    """The login the skill names when a browser session is missing is `conversation setup`."""
    assert unmet_requirements([CONVERSATION]) == []
