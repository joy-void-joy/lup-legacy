"""Every module lup ships, each spec beside the builder that answers for it.

The pairing is what makes a declined module cost nothing. Each ``build`` here
imports its subject inside the call rather than at module scope, so importing
this catalog — which a listing, a requirement check, and every ``--help`` line
does — pulls in the type and the specs and no skill, no page, and no optional
extra. A project that declined the resolver never imports the resolver's
declarations, and one that declined a module whose builder needs
``lup-agents[web]`` never needs ``lup-agents[web]``.

That is the same arrangement :data:`~lup.devtools.roster.LIBRARY_ROSTER` uses
for sub-apps, and it exists here for a stronger reason: a sub-app's builder
imports an optional extra, and a module's builder imports a whole subject.
"""

from lup.harness.codescan.common import RuleSelection
from lup.harness.content.application import ApplicationLayout
from lup.harness.content.modules import specs
from lup.harness.modules import Module, ModuleEntry


def core_module(layout: ApplicationLayout, rules: RuleSelection) -> Module:
    from lup.harness.content.modules.core import module

    return module(layout, rules)


def git_workflow_module() -> Module:
    from lup.harness.content.modules.git_workflow import module

    return module()


def meta_module(layout: ApplicationLayout) -> Module:
    from lup.harness.content.modules.meta import module

    return module(layout)


def resolver_module() -> Module:
    from lup.harness.content.modules.resolver import module

    return module()


def version_module(layout: ApplicationLayout) -> Module:
    from lup.harness.content.modules.version import module

    return module(layout)


def observability_module() -> Module:
    from lup.harness.content.modules.observability import module

    return module()


def sandbox_module() -> Module:
    from lup.harness.content.modules.sandbox import module

    return module()


def setup_module() -> Module:
    from lup.harness.content.modules.setup import module

    return module()


def conversation_module() -> Module:
    from lup.harness.content.modules.conversation import module

    return module()


def feedback_loop_module(layout: ApplicationLayout) -> Module:
    from lup.harness.content.modules.feedback_loop import module

    return module(layout)


def runs_module() -> Module:
    from lup.harness.content.modules.runs import module

    return module()


def ledger_module() -> Module:
    from lup.harness.content.modules.ledger import module

    return module()


def coordination_module() -> Module:
    from lup.harness.content.modules.coordination import module

    return module()


def realtime_module() -> Module:
    from lup.harness.content.modules.realtime import module

    return module()


def reflection_module() -> Module:
    from lup.harness.content.modules.reflection import module

    return module()


def library_modules(
    layout: ApplicationLayout, rules: RuleSelection
) -> list[ModuleEntry]:
    """The roster a project starts from, in the order a composition lays it out.

    Binding the layout and the rule selection here rather than passing them
    down through :func:`~lup.harness.modules.adopted` is what keeps a builder
    a nullary call: a module that needs neither declares neither, and nothing
    in the algebra has to know which of them any subject happens to want.

    Nothing is built by this call. Every entry closes over its arguments and
    opens its subject only when a project that took the module asks for it.
    """
    return [
        ModuleEntry(spec=specs.CORE, build=lambda: core_module(layout, rules)),
        ModuleEntry(spec=specs.GIT_WORKFLOW, build=git_workflow_module),
        ModuleEntry(spec=specs.META, build=lambda: meta_module(layout)),
        ModuleEntry(spec=specs.RESOLVER, build=resolver_module),
        ModuleEntry(spec=specs.VERSION, build=lambda: version_module(layout)),
        ModuleEntry(spec=specs.OBSERVABILITY, build=observability_module),
        ModuleEntry(spec=specs.SANDBOX, build=sandbox_module),
        ModuleEntry(spec=specs.SETUP, build=setup_module),
        ModuleEntry(spec=specs.CONVERSATION, build=conversation_module),
        ModuleEntry(
            spec=specs.FEEDBACK_LOOP, build=lambda: feedback_loop_module(layout)
        ),
        ModuleEntry(spec=specs.RUNS, build=runs_module),
        ModuleEntry(spec=specs.LEDGER, build=ledger_module),
        ModuleEntry(spec=specs.COORDINATION, build=coordination_module),
        ModuleEntry(spec=specs.REALTIME, build=realtime_module),
        ModuleEntry(spec=specs.REFLECTION, build=reflection_module),
    ]
