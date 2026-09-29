# lup: ignore[constant-declaration]
# Every constant here is this repository's own composition — which modules it
# takes, what it changed about each, and what its plugin is called. A
# composition root is where a judgement is finally made rather than passed on,
# so there is no caller above it to take these from.
"""Which modules this repository takes, and what it changed about each.

What reaches a project is a set of modules rather than a list of skills, so
this states a delta against that set and nothing else. Everything downstream
— the plugin's skills and agents, the pages under ``docs/``, the always-loaded
document, which sub-apps the CLI serves, which tool groups a session is
offered — is derived from the result rather than declared beside it, which is
what makes declining a subject remove all five at once instead of four.

This is also where the library learns what this application is called. Several
of its skills name a path inside the reading project's own package, and only
the project knows that name, so it is supplied here rather than assumed there.
"""

from pathlib import Path

import lup.harness.models as models
import lup_template.harness.content.guidance as guidance
from lup.devtools.roster import LIBRARY_SPECS as LIBRARY_SUBAPPS
from lup.devtools.subapps import SubAppSelection, SubAppSpec, unowned
from lup.harness.codescan.common import RuleSelection
from lup.harness.content.application import ApplicationLayout
from lup.harness.modules import (
    Adoption,
    Composition,
    Module,
    ModuleEntry,
    ModuleSelection,
    scaffold_selection,
)
from lup.harness.content.docs.catalog import page
from lup.seams import Selection
from lup_template.devtools.subapps import APPLICATION_ROSTER
from lup_template.harness.content.docs import corpus
from lup_template.harness.content.modules.catalog import composed_entries
from lup_template.harness.content.skills.meta import skill as build_meta
from lup_template.harness.content.skills.review import skill as build_review

LAYOUT = ApplicationLayout(package=Path(__file__).resolve().parents[2].name)
"""Where this application's own code sits, for the library prose that names it.

Derived from where this file actually sits rather than written down, for the
reason ``DevProject.package`` derives its own: initialization renames the
package, and a literal would go on naming one that is gone.
"""

RULES = RuleSelection(retired=[])
"""Which of the library's scan rules this repository holds itself to.

# lup: template: which of the library's scan rules this domain holds itself to.
Spelled empty rather than left to the default, because a default nobody was
shown is not a decision — and a repository that settled a convention
differently is not defective there. Name the few it drops with
`dev seams --retire <rule-id>`, or drop the family outright with
`--retire-all`, which is one answer here instead of thirty retirements one
denial at a time.

Read by two things that have to agree: the hook set enforcing these rules, and
the design-principles section teaching them. A rule retired in one place and
taught in the other would be prose describing a gate that never fires, so both
take this one value rather than each declaring its own.
"""


def adoptions(layout: ApplicationLayout) -> list[Adoption]:
    """What this repository declares against modules whose subjects it shares.

    Additions rather than forks. Each of these is a section, a skill or a page
    this repository wrote about a subject the library owns — how *this*
    project's policy is changed, what its markers mean, where its deferred work
    goes, what it records in the ledger — and each arrives under a new id, so
    it leaves with the module it was written against and the module goes on growing underneath
    while this states only what it added. The one declared under the library's
    own id is ``review``: the library reviews a trace against the harness,
    which is what every project has, and this repository reviews it against
    the agent its sessions run as well, so the same id has to render the
    fuller walk here.

    Built against a layout rather than declared, for the same reason the
    library's builders are: ``/lup:meta`` names a path inside the reading
    project's own package, so a constant here would go on naming this one
    after an adopter renamed theirs.

    Nothing here says ``taken`` or ``loads_guidance``: those two are derived
    from the roster by :func:`~lup.harness.modules.scaffold_selection`,
    because a scaffold settling them module by module is how a list goes
    stale.
    """
    return [
        Adoption(
            module="core",
            guidance=Selection(
                overrides=[
                    guidance.CHANGING_THE_POLICY,
                    guidance.MARKER_VOCABULARY,
                    guidance.DEFERRED_WORK,
                ]
            ),
        ),
        Adoption(
            module="git-workflow",
            guidance=Selection(overrides=[guidance.COMMIT_TYPE_POINTER]),
        ),
        Adoption(
            module="feedback-loop",
            # The one rewrite under the library's own id: this repository's
            # sessions run an agent, and a review of their traces reads its
            # prompt and toolset sources beside the harness every project has.
            content=models.ContentSelection(skills=[build_review(layout)]),
            guidance=Selection(overrides=[guidance.SELF_IMPROVEMENT]),
        ),
        Adoption(
            module="meta",
            content=models.ContentSelection(skills=[build_meta(layout)]),
        ),
        Adoption(
            module="ledger",
            # This repository's worked example of knowledge kinds over the
            # ledger: its page is this project's, and its subject is the
            # ledger's, so it is published where the ledger is and not
            # elsewhere — the library ships the mechanism and no epistemics.
            documents=Selection(
                overrides=[
                    page(
                        "corpus",
                        "corpus.md",
                        lambda _: corpus.document(layout),
                        layout.docs(),
                    )
                ]
            ),
        ),
    ]


def entries(layout: ApplicationLayout = LAYOUT) -> list[ModuleEntry]:
    """Every module this repository could take, each beside its builder."""
    return composed_entries(layout, RULES)


DECLINED: list[str] = []
"""Modules this project does not have, by id.

# lup: template: which of lup's modules this domain has no subject for.
Empty here and spelled anyway, because a default nobody was shown is not a
decision. This repository takes every module it ships — a scaffold is the
demonstration of its own machinery, so a subject nobody composes is a subject
nobody would notice breaking — and that is a statement about *this* repository
rather than advice to a project built from it.

A domain names what it declines here and gets none of it: no skill, no page, no
paragraph, no command tree, no tool group, and no program asked of a machine
that only it needed. A module requiring one named here has to be named too,
and an essential one — ``core``, ``project`` — is refused, since every other
module stands on it. `dev modules` prints the roster with
each module's summary and what its prose costs, which is the reading this list
is written against; `/lup:init` walks it once with the user. Every module left
unnamed arrives under its own default, including the ones lup grows after this
line was last edited — which is the whole reason this is a list of refusals
rather than a list of what is kept.
"""


def selection(
    layout: ApplicationLayout = LAYOUT, declined: list[str] = DECLINED
) -> ModuleSelection:
    """What this project has: the derived answers, less what it declined.

    :func:`~lup.harness.modules.scaffold_selection` settles the two answers a
    scaffold should not be making module by module — whether a module is taken,
    and whether its prose loads — and the refusals are applied over that. There
    is no contradiction between the two: naming a module you do not have *is*
    the decision the derivation stands in for, so a stated refusal wins over a
    rule that exists because nobody had stated anything.

    *declined* is this project's list unless a caller asks what another would
    compose — which is how the gate generates the tree each module's absence
    would leave, without anybody editing the list to find out.
    """
    derived = scaffold_selection(
        [entry.spec for entry in entries(layout)], adoptions(layout)
    )
    return ModuleSelection(
        adoptions=[
            entry.model_copy(update={"taken": False})
            if entry.module in declined
            else entry
            for entry in derived.adoptions
        ]
    )


def composition(
    layout: ApplicationLayout = LAYOUT, declined: list[str] = DECLINED
) -> Composition:
    """The modules this repository composes, and every surface read off them.

    The layout is a parameter so the roster can be resolved under a package
    name that is not this one. Under this repository's own name a declaration
    that took the layout and one that wrote ``lup_template`` down render the
    same string, so only a roster built as some other project can tell them
    apart — which is the one thing an adopter needs to be true.
    """
    return Composition.of(entries(layout), selection(layout, declined))


def modules(layout: ApplicationLayout = LAYOUT) -> list[Module]:
    """The modules this repository composes, in reading order."""
    return composition(layout).modules


COMPOSITION = composition()
"""This repository's roster as it settled it, built once because every surface reads it."""

MODULE_SPECS = COMPOSITION.specs
"""Every module this repository could take, in the order it lays them out.

The roster read through the half that costs nothing. This repository's own
framing opens it and what it says about its own tooling closes it, with the
library's modules in between — a statement about the document rather than about
importance, since guidance renders as the chapter spine crossed with this order.

Projected from the entries rather than listed again, so the order cannot be
stated twice and come out differently the second time.
"""

MODULE_SELECTION = COMPOSITION.selection
"""What this repository settled, under its own package name."""

SUBAPPS = COMPOSITION.subapps()
"""Every top-level CLI group the adopted modules own.

Read before anything is built, which is what makes it usable: the CLI has to
know which command trees it serves in order to compose them, and a name is
known from the spec while a skill is not. What the CLI serves is the
intersection of this and what the library ships, so a module nobody took takes
its commands with it rather than leaving them answering for machinery the
project does not have.
"""

TOOL_GROUPS = COMPOSITION.tool_groups()
"""Every MCP tool group a session is offered, by adopted module."""

WITHHELD_TOOL_GROUPS = COMPOSITION.withheld_tool_groups()
"""Every tool group a declined module owns, which no plugin here starts a server for.

What the plugin's server list subtracts from the groups this project declares,
so declining the sandbox leaves no harness session starting a container nobody
asked for. The application's own agent composes its toolset from the
declaration itself: which verbs the product carries is its runtime's decision,
and its review gate expects the reflection tools to be there.
"""


def subapp_selection(composed: Composition = COMPOSITION) -> SubAppSelection:
    """Which of lup's own sub-apps a CLI composed from *composed* declines."""
    return unowned(composed.subapps(), LIBRARY_SUBAPPS)


def application_specs(composed: Composition = COMPOSITION) -> list[SubAppSpec]:
    """The sub-apps only this application has, narrowed to what *composed* owns."""
    served = composed.subapps()
    return [spec for spec in APPLICATION_ROSTER if spec.name in served]


def subapp_specs(composed: Composition = COMPOSITION) -> list[SubAppSpec]:
    """Every sub-app a CLI composed from *composed* serves, in `--help` order."""
    return subapp_selection(composed).specs(
        LIBRARY_SUBAPPS, application_specs(composed)
    )


SUBAPP_SELECTION = subapp_selection()
"""Which of lup's own sub-apps this CLI declines, as the roster decided it.

Empty for this repository, which takes every module it ships — and that is the
derivation working rather than a statement about lup.
"""

APPLICATION_SPECS = application_specs()
"""The sub-apps only this application has, narrowed the same way.

``agent`` is served because this project took ``project``, whose subject it is.
"""

SUBAPP_SPECS = subapp_specs()
"""Every sub-app this CLI serves, in the order `--help` lists them."""

MODULES = COMPOSITION.modules
"""The composed roster, which every surface below reads."""

CONTENT = COMPOSITION.content()
"""Every skill and agent this repository's plugin ships, in module order."""

SKILLS = CONTENT.skills
"""Every skill this repository's plugin ships."""

AGENTS = CONTENT.agents
"""Every agent this repository's plugin ships."""

GUIDANCE_SECTIONS = COMPOSITION.guidance()
"""The always-loaded document's sections, chapter by chapter and module by module."""

GUIDANCE = guidance.document(GUIDANCE_SECTIONS)
"""The always-loaded document itself."""

PLUGIN_NAME: models.NativeName = "lup"
"""The plugin every declared skill is invoked through.

Named here rather than read off the harness because the roster documents are
compiled into that harness: reading it back to render them would close the
loop between what is declared and what describes the declaration.
"""
