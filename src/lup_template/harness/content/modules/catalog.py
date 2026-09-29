"""The modules only this repository has, each spec beside its builder.

The template half of the roster, paired the same way the library's is and for
the same reason: a listing, a requirement check and ``--help`` all read specs,
and only a module somebody took is built.

One of these extends a library module rather than standing beside it.
``meta`` gains this repository's ``/lup:meta`` skill through an
:class:`~lup.harness.modules.Adoption` — an override under a new id, which the
selection adds in place — so the skill ships as part of the module whose
subject it shares instead of forking the module to hold it. That is the shape
an adopting project uses for every change it makes to what lup ships.
"""

from lup.harness.codescan.common import RuleSelection
from lup.harness.content.application import ApplicationLayout
from lup.harness.content.modules.catalog import library_modules
from lup.harness.modules import Module, ModuleEntry
from lup_template.harness.content.modules import specs


def project_module(layout: ApplicationLayout) -> Module:
    from lup_template.harness.content.modules.project import module

    return module(layout)


def template_init_module(layout: ApplicationLayout) -> Module:
    from lup_template.harness.content.modules.template_init import module

    return module(layout)


def upstream_module() -> Module:
    from lup_template.harness.content.modules.upstream import module

    return module()


def examples_module() -> Module:
    from lup_template.harness.content.modules.examples import module

    return module()


def opening_modules(layout: ApplicationLayout) -> list[ModuleEntry]:
    """What this repository says before the library says anything.

    Split from the closing half because a roster's order is a document's
    order: this repository's own framing opens each chapter and the library's
    general statement of the same subject follows, which is expressible only
    by sitting ahead of every library module rather than after them.
    """
    return [ModuleEntry(spec=specs.PROJECT, build=lambda: project_module(layout))]


def closing_modules(layout: ApplicationLayout) -> list[ModuleEntry]:
    """What this repository says once the library has said its piece.

    Standing a project up and keeping it in step with upstream are both
    subjects that only make sense against everything already established, and
    the configuration section closes the tooling chapter for the same reason.
    """
    return [
        ModuleEntry(
            spec=specs.TEMPLATE_INIT, build=lambda: template_init_module(layout)
        ),
        ModuleEntry(spec=specs.UPSTREAM, build=upstream_module),
        ModuleEntry(spec=specs.EXAMPLES, build=examples_module),
    ]


def composed_entries(
    layout: ApplicationLayout, rules: RuleSelection
) -> list[ModuleEntry]:
    """Every module this repository could take, in the order it lays them out.

    Nothing is built by this call, which is the property the rest depends on.
    Every entry closes over its arguments and opens its subject only when a
    project that took the module asks for it — so the roster's *names* are
    known before any subject is imported, and the sub-apps and tool groups a
    project serves can be read off the specs and handed to the content that
    describes them.
    """
    return [
        *opening_modules(layout),
        *library_modules(layout, rules),
        *closing_modules(layout),
    ]
