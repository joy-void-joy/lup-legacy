"""Whether every module names only what it stands on.

A module is taken or declined whole, and that promise is only as good as what
its content names. A skill invoking another module's skill, a page linking
another module's page, a section telling a session to run a command another
module's tree serves — each is fine exactly as long as that other module is
there, and nothing says so when it is not. The harness refuses the invocation
at generation and the command sweep refuses the mention, and whoever meets
either is the operator who declined some *unrelated* module, reading a failure
in a subject they kept.

So each module is read as the smallest project that could hold it — itself,
what it requires, and every essential module — and what its content names is
held to that. A mention of anything else is either a requirement nobody wrote
down or a sentence that should exist only where its subject is taken, and the
report names the module either answer needs. Read from the declarations
rather than from a generated tree, so it answers for every module at once and
for a selection nobody has generated yet.
"""

# lup: ignore[import-re] — the command and page names written inside rendered
# English, which no parser owns: a skill or an agent is read off the parts that
# name them, and only what prose spells is recognized by its shape
import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from lup.devtools.dev.documented import MENTION, WrittenCommand
from lup.harness.contracts import PromptRenderer
from lup.harness.models import NativeName, PromptDocument
from lup.harness.modules import DocumentContext, Module, standing

# lup: ignore[re-call] — a page named by its path or linked beside another,
# which is the whole of how a document under `docs/` is pointed at
PAGE = re.compile(r"(?:docs/|\]\()([a-z0-9][a-z0-9-]*)\.md")
"""Where prose points at a page: its path, or a link from a sibling page."""

# lup: ignore[re-call] — a skill spelled the way a runtime spells an
# invocation, which is what a sentence carrying one literally looks like
SPELLED_SKILL = re.compile(r"(?<![\w/.$-])[/$]([a-z][a-z0-9-]*):([a-z][a-z0-9-]*)")
"""A skill written out as ``/plugin:name`` or ``$plugin:name`` in rendered text.

Beside the parts naming one, because prose can spell an invocation literally
and a page is not held to the portable-prose rule that refuses it in a skill —
so a core page can name a skill in a spelling no other check reads.
"""

# lup: defer: an MCP tool named in prose (`coordination_peers`) is a fifth kind
# nothing here reads — its names are known only by building a group against a
# session — so such a mention is wrapped by hand and caught by review alone.
type ReachKind = Literal["skill", "agent", "command", "page"]
"""The four things one module's content can name that another contributes."""


class Named(BaseModel, frozen=True):
    """One thing a document names, before anybody asks whose it is."""

    kind: ReachKind
    name: str
    """Spelled the way the harness addresses it: a skill's name, an agent's
    qualified by its plugin, a sub-app's, or a page's semantic id."""


class Site(BaseModel, frozen=True):
    """One document a module publishes, under the name a report gives it."""

    site: str
    """The skill, agent, section or page, as a reader looks for it."""

    document: PromptDocument


class Reach(BaseModel, frozen=True):
    """One thing a module's content names from a module it does not stand on."""

    module: str
    """The module whose content names it."""

    site: str
    """Where: the skill, agent, section or page doing the naming."""

    named: Named
    owner: str
    """The module contributing it, which the naming one does not stand on."""

    def describe(self) -> str:
        """One line naming both answers, so a reader chooses rather than guesses."""
        return (
            f"{self.module}: {self.site} names {self.named.kind} "
            f"{self.named.name!r}, which {self.owner!r} contributes — add "
            f"{self.owner!r} to the module's `requires`, or wrap the mention in "
            f"WhereTaken(module={self.owner!r})"
        )


def owners(modules: list[Module], plugin: NativeName) -> dict[Named, str]:
    """Which module contributes each nameable thing, read off the modules."""
    return {
        **{
            Named(kind="skill", name=skill.name): module.spec.id
            for module in modules
            for skill in module.content.skills
        },
        **{
            Named(kind="agent", name=f"{plugin}:{agent.name}"): module.spec.id
            for module in modules
            for agent in module.content.agents
        },
        **{
            Named(kind="command", name=name): module.spec.id
            for module in modules
            for name in module.spec.subapps
        },
        **{
            Named(kind="page", name=entry.semantic_id): module.spec.id
            for module in modules
            for entry in module.documents
        },
    }


def named(document: PromptDocument, renderers: Sequence[PromptRenderer]) -> list[Named]:
    """Everything one document names that some module could contribute.

    Skills and agents are read off the parts that name them, which is exact.
    Commands, pages, and a skill written out in its runtime's own spelling are
    read off the document as each runtime renders it, because prose names them
    and a value inside a sentence is spelled only once it renders — a command a
    passage places is as much an instruction as one it writes out.
    """
    rendered = [renderer.render(document) for renderer in renderers]
    return list(
        dict.fromkeys(
            [
                *(
                    Named(kind="skill", name=issued.skill)
                    for part in document.walked()
                    if (issued := part.invocation) is not None
                ),
                *(
                    Named(kind="skill", name=skill)
                    for text in rendered
                    for _, skill in SPELLED_SKILL.findall(text)
                ),
                *(
                    Named(kind="agent", name=delegated)
                    for part in document.walked()
                    if (delegated := part.named_agent) is not None
                ),
                *(
                    Named(kind="command", name=words[0])
                    for text in rendered
                    for tail in MENTION.findall(text)
                    if (
                        words := WrittenCommand(
                            file="", line=0, spelled=tail.strip()
                        ).command_words()
                    )
                ),
                *(
                    Named(kind="page", name=f"docs.{stem}")
                    for text in rendered
                    for stem in PAGE.findall(text)
                ),
            ]
        )
    )


class Published(BaseModel, frozen=True):
    """A document a composition root publishes on one module's behalf.

    What reaches a reader without being any module's surface: the guidance an
    installer carries into another repository is the clearest case, published
    only where the module installing it is taken and naming whatever the rest
    of the roster ships. It is held to what that module stands on, like the
    module's own content.
    """

    module: str
    """The module it is published for, by id."""

    site: str
    document: PromptDocument


def narrowed(
    context: DocumentContext, modules: list[Module], held: list[str]
) -> DocumentContext:
    """The context a page renders against in a project holding only *held*.

    Three pages describe the roster itself, so reading one against the whole
    composition would report every module it lists as a module it names. Read
    against the modules the page's own module stands on, a listing names only
    what that project would have — which is what the page would say there. A
    sub-app no module owns is served whatever a project took, so it stays.
    """
    kept = [module.given(held) for module in modules if module.spec.id in held]
    claimed = owners(modules, context.plugin)
    return context.model_copy(
        update={
            "skills": [skill for module in kept for skill in module.content.skills],
            "agents": [agent for module in kept for agent in module.content.agents],
            "subapps": [
                spec
                for spec in context.subapps
                if claimed.get(Named(kind="command", name=spec.name), held[0]) in held
            ],
        }
    )


def published(
    module: Module, held: list[str], context: DocumentContext | None
) -> list[Site]:
    """Every document one module publishes, as a project holding *held* reads it.

    Its pages only where there is a *context* to render them against, since a
    page is a function of the composition it describes.
    """
    view = module.given(held)
    pages = [entry.build(context) for entry in view.documents] if context else []
    return [
        *(
            Site(site=f"skill {skill.name}", document=skill.prompt)
            for skill in view.content.skills
        ),
        *(
            Site(site=f"agent {agent.name}", document=agent.prompt)
            for agent in view.content.agents
        ),
        *(
            Site(
                site=f"section {section.id}",
                document=PromptDocument(parts=section.parts),
            )
            for section in view.guidance
        ),
        *(
            Site(site=f"page {page.path.as_posix()}", document=page.document)
            for page in pages
        ),
    ]


def reaches(
    modules: list[Module],
    renderers: Sequence[PromptRenderer],
    context: DocumentContext | None = None,
    beside: Sequence[Published] = (),
    plugin: NativeName = "lup",
) -> list[Reach]:
    """Every mention, across the roster, of a module its module does not stand on.

    *modules* is the whole roster as the project resolved it and before
    adoption: every module is judged, including the ones this project
    declined, because a module one project does not take is one another
    project does. Pages are read only where a *context* is given, each against
    that context narrowed to what its module stands on; *beside* are the
    documents a root publishes on a module's behalf, held to the same.
    """
    specs = [module.spec for module in modules]
    claimed = owners(modules, context.plugin if context is not None else plugin)
    held = {
        name: standing(name, specs)
        for name in [
            *(module.spec.id for module in modules),
            *(extra.module for extra in beside),
        ]
    }
    sites = [
        *(
            Published(module=module.spec.id, site=site.site, document=site.document)
            for module in modules
            for site in published(
                module,
                held[module.spec.id],
                narrowed(context, modules, held[module.spec.id])
                if context is not None
                else None,
            )
        ),
        *(
            extra.model_copy(
                update={"document": extra.document.given(held[extra.module])}
            )
            for extra in beside
        ),
    ]
    return [
        Reach(module=site.module, site=site.site, named=found, owner=owner)
        for site in sites
        for found in named(site.document, renderers)
        if (owner := claimed.get(found)) is not None and owner not in held[site.module]
    ]
