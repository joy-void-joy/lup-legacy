"""Changing the machinery rather than the product.

Two subjects that look separate until they are listed side by side:
authoring the harness a session runs under, and the walks that move code
without losing it. Both answer the same question — the thing being edited is
the apparatus, not the thing it was built to make — and a project doing one of
them is doing the other within the week.

So adding a command, changing one, standing an investigator up, the refactor
walk, the principle pass, and the tools that resolve a name rather than match
text are one module.

What the module does not hold is the generator or the reference behind it.
Every module ends at regenerating the trees and every session is launched from
them, so the `harness` command tree, the page every generated banner points
at, and the audit of what differs between the runtimes it produces for are
core's: a project declining the authoring skills still regenerates, and still
reads what it regenerated, whatever it kept.
"""

from lup.harness.content.application import ApplicationLayout
from lup.harness.content.modules.specs import META
from lup.harness.content.skills.add_command import skill as build_add_command
from lup.harness.content.skills.create_investigator import (
    skill as build_create_investigator,
)
from lup.harness.content.skills.modify_command import skill as build_modify_command
from lup.harness.content.skills.principle import skill as build_principle
from lup.harness.content.skills.refactor import SKILL as SKILL_REFACTOR
from lup.harness.content.skills.refactor_tools import skill as build_refactor_tools
from lup.harness.models import ContentRoster
from lup.harness.modules import Module


def module(layout: ApplicationLayout) -> Module:
    """Harness authoring and refactoring as one value, against the layout."""
    return Module(
        spec=META,
        content=ContentRoster(
            skills=[
                build_add_command(layout),
                build_create_investigator(layout),
                build_modify_command(layout),
                build_principle(layout),
                SKILL_REFACTOR,
                build_refactor_tools(layout),
            ]
        ),
    )
