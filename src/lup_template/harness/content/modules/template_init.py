"""Standing a lup project up, and keeping it configured once it is standing.

Three skills whose subject is the project rather than the work inside it:
initializing one, installing the plugin into it, and restarting one from a
predecessor that was explored rather than finished. They have nothing to say
inside a working repository, which is why they are this repository's while
the rest of the roster is the library's.

Designing is not among them. `/lup:brainstorm` shapes a new agent before
initialization *and* a feature inside a project that already exists, so it
has as much to say in a working repository as in an empty one — it is core's,
and a project declining this module keeps it. Distill is here: restarting from
an explored predecessor is standing a project up again, and it reaches for
upstream's import and tracking the way initialization does, which is why the
spec requires upstream. Nor is the guide to this project's own layout here:
`docs/template.md` describes the application a project *is*, whether or not
it still initializes anything, so it is the essential project module's.

On for an adopter, not only for the scaffold. A project built from this one
still runs ``/lup:init`` — the whole point of the module system is that
initialization becomes a module selection — and still installs the plugin into
whatever it grows into next. What an adopter drops is ``examples``.

The configuration section is here because it is the same subject at rest: what
a project reads from its environment, and where the variables are written
down. It closes the tooling chapter, after the library's word on long-running
work, which is the ordering this module's position in the roster produces.
"""

import lup_template.harness.content.guidance as guidance
from lup.harness.content.application import ApplicationLayout
from lup.harness.models import ContentRoster
from lup.harness.modules import Module
from lup_template.harness.content.modules.specs import TEMPLATE_INIT
from lup_template.harness.content.skills.distill import SKILL as SKILL_DISTILL
from lup_template.harness.content.skills.init import SKILL as SKILL_INIT
from lup_template.harness.content.skills.install import SKILL as SKILL_INSTALL


def module(layout: ApplicationLayout) -> Module:
    """Standing a project up as one value, its configuration named in its own layout."""
    return Module(
        spec=TEMPLATE_INIT,
        content=ContentRoster(skills=[SKILL_DISTILL, SKILL_INIT, SKILL_INSTALL]),
        guidance=[guidance.configuration(layout)],
    )
