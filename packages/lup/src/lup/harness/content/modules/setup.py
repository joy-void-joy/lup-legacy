"""Interactive configuration: keys, integrations, profiles.

The wizard and its page are one subject seen twice — the same declarations
rendered for somebody who would rather click than answer prompts, shown as
this repository's pane of the dashboard. Keeping them in one module is what
stops a project from declining one and keeping the other, which would leave a
page serving a wizard nobody can run.

This is deliberately *not* ``template-init``'s. Standing a project up happens
once; configuring its keys and integrations happens whenever a key rotates, so
a project that finished initializing and declined the init skills still has
every reason to keep this.
"""

from lup.harness.content.modules.specs import SETUP
from lup.harness.content.skills.profile import SKILL as SKILL_PROFILE
from lup.harness.models import ContentRoster
from lup.harness.modules import Module


def module() -> Module:
    """Interactive configuration as one value."""
    return Module(spec=SETUP, content=ContentRoster(skills=[SKILL_PROFILE]))
