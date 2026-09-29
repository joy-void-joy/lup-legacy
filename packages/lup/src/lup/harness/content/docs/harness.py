"""Guide to the harness: authoring typed declarations and generating trees."""

import lup.harness.models as models

from lup.harness.content.application import ApplicationLayout
from lup.harness.content.catalog import (
    agent_roster_bullets,
    skill_roster_parts,
)
from lup.policy.bundle import policy_kernel_modules


def kernel_module_count() -> int:
    """How many modules the generated policy runtime carries, from the copier.

    Asked of :func:`policy_kernel_modules` rather than counted here, because
    that is the function whose output lands in the tree. A number written down
    here goes on describing a single `kernel.py` where the kernel is a
    package — a source path that resolves to nothing.
    """
    return len(policy_kernel_modules())


def document(
    skills: list[models.Skill],
    agents: list[models.Agent],
    plugin: models.NativeName,
    layout: ApplicationLayout,
) -> models.PromptDocument:
    """The harness guide, carrying the roster the plugin it describes ships.

    The rosters are rendered from the declarations rather than restated, so
    a skill added on either side of the library boundary appears here without
    anyone remembering to add a line.
    """
    return models.PromptDocument(
        source=__name__,
        parts=[
            models.Passage(
                module=__name__,
                values={
                    "harness_content_directory": models.code(
                        layout.directory("harness", "content")
                    ),
                    "harness_catalog_py": models.code(
                        layout.path("harness", "catalog.py")
                    ),
                    "kernel_module_count": models.counted(kernel_module_count()),
                    "harness_content_modules": models.code(
                        layout.path("harness", "content", "modules")
                    ),
                    "harness_content_catalog_py": models.code(
                        layout.path("harness", "content", "catalog.py")
                    ),
                },
            ),
            *skill_roster_parts(skills, plugin),
            models.TextPart(
                text=r"""
**Agents:**

"""
            ),
            agent_roster_bullets(agents),
            models.Passage(
                module=__name__,
                name="authoring",
                values={
                    "harness_catalog_py": models.plain(
                        layout.path("harness", "catalog.py")
                    ),
                    "project_directory": models.plain(layout.directory()),
                    # A page about passages has to show the tags a passage
                    # refuses, and cannot hold them: they are values here for
                    # the same reason every other derived piece is one.
                    "statement_tag": models.code("{%"),
                    "comment_tag": models.code("{#"),
                    "machine_profile": models.WhereTaken(
                        module="setup",
                        parts=[
                            models.TextPart(text="`"),
                            models.SkillInvocation(plugin="lup", skill="profile"),
                            models.TextPart(
                                text=(
                                    "` is the machine's own: its hint names the "
                                    "profiles this machine keeps, which no "
                                    "committed file may, so it is in neither "
                                    "tree. `harness generate` and every launch "
                                    "render it from the registry into a "
                                    "gitignored overlay beside each tree — "
                                    "`.claude/plugins/local/`, which a Claude "
                                    "launch loads after the committed plugin, "
                                    "and `.codex/skills/`, which Codex reads "
                                    "where it stands — rewritten whole each "
                                    "time, so a profile added since is named at "
                                    "the next launch and one removed is gone.\n"
                                )
                            ),
                        ],
                    ),
                },
            ),
        ],
    )
