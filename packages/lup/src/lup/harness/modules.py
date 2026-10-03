"""A module: everything one subject contributes, taken or declined as a whole.

A subject reaches a project across five surfaces at once — skills and agents,
a page under ``docs/``, prose in the always-loaded document, a tool group, a
sub-app. A module is that subject as one value, so adopting it takes every
surface and declining it leaves none behind. What a project keeps of what it
took is :class:`Adoption`, resolved through the algebra :mod:`lup.seams`
already applies to the shell and edit tables.

Three properties fall out of the shape:

*Guidance is declined separately.* The always-loaded document sits under a
ceiling that truncates silently rather than failing, so prose cannot arrive as
an implication of adopting a subject — two modules taken for their tools would
spend a budget nobody was asked about. ``Adoption.guidance`` is its own
answer.

*A declined module is never built.* :class:`ModuleEntry` carries a builder, so
a module whose builder imports an optional extra costs that dependency only to
a project that has the subject.

*What one module needs from another is declared.* The resolver names
``skill.merge``, which git-workflow owns.
:class:`~lup.harness.models.Harness` refuses an invocation that resolves to
nothing, so the gap is caught either way — but it is caught at the end and it
names the skill, and an operator who declined a module is owed the answer in
the vocabulary they decided in. ``requires`` gives it, before anything builds.
A mention that is incidental rather than needed — a pointer to the resolver's
page from a guide that works without it — is wrapped in
:class:`~lup.harness.models.WhereTaken` instead, and adoption drops it where
that module was declined. `dev check` holds every module to one or the other,
so declining a module never breaks one that did not say it needed it.
"""

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

import lup.harness.models as models
from lup.devtools.subapps import SubAppSpec
from lup.harness.content.application import ApplicationLayout
from lup.harness.requirements import Manifest
from lup.seams import SelectableRule, Selection


class ModuleSpec(SelectableRule, frozen=True):
    """What a module is called, what it is for, and whether it is taken by default.

    Separate from the module it names for the reason
    :class:`~lup.devtools.subapps.SubAppSpec` is separate from its app: prose
    listing which modules exist is generated into the harness, and a document
    that built each one to learn its name would make the listing depend on
    every optional extra the listed modules import.
    """

    id: str
    title: str
    summary: str

    default_on: bool = False
    """Whether a project that has said nothing about this module has it.

    Off for a subject most projects do not have, because the costs are
    asymmetric: a module carried by default that nobody wanted spends guidance
    budget and session context every time, and one off by default that
    somebody wants is a single line to adopt.
    """

    requires: list[str] = []
    """Modules this one's declarations reach into, by id.

    Everything a module's content names — a skill it invokes, an agent it
    delegates to, a command whose tree another module owns, a page another
    module publishes — is either its own, reached through here, or inside a
    :class:`~lup.harness.models.WhereTaken` naming the module it needs. `dev
    check` holds every module to that, so declining one never breaks another
    that did not say it needed it.
    """

    essential: bool = False
    """Whether every project has this module, so no selection may decline it.

    What every other module stands on: the gate, the generator, the tree every
    session is launched from. A module may reach into an essential one without
    naming it in :attr:`requires`, because there is no composition without it
    — which is also why declining one is refused rather than honoured, since
    honouring it would break every module at once.
    """

    subapps: list[str] = []
    """Top-level CLI groups this module owns, by name.

    Here rather than on the module for the reason everything else here is: a
    name is not a declaration, and reading which commands a project serves must
    not build the subject that serves them. It is stronger than convenience —
    a skill that names the CLI roster is content, and content is what a module
    builder builds, so a roster read off built modules would be a roster that
    could not be handed to the declarations describing it.

    Top-level only: a command group nested inside another module's sub-app is
    not claimable here, so the resolver's commands under ``harness`` stay where
    they are and this names none of them. Nesting is a shape of its own and
    does not gate this one.
    """

    tool_groups: list[str] = []
    """MCP tool groups served to a session while this module is adopted.

    Beside :attr:`subapps` and for the same reason: a session composing its
    servers reads names, and building a module to learn them would make every
    launch import every subject the project happens to hold.
    """

    requirements: list[str] = []
    """External programs only this module's subject needs, by capability.

    Named, like the command trees and tool groups, because the requirement
    itself is the project's to declare — which constructor, which client,
    which recovery — and a module only says that its subject is why one is
    there. A requirement some module claims is asked of a machine only where a
    module claiming it is taken; one no module claims is the project's own and
    always asked. So a project that declined the sandbox is not told, at every
    launch, that a container runtime it has no use for is missing.
    """

    scaffold_only: bool = False
    """Whether only the repository shipping the scaffold is offered this.

    Not a default but the absence of a choice: a module demonstrating the
    scaffold to itself has nothing to say to a project built from it, so it is
    left out of what ``dev modules`` offers and what ``/lup:init`` asks about
    rather than being offered and declined. ``examples`` is the whole of it —
    a directory composing lup's own runtime against lup's own README, which an
    adopter inherits as a suite it must keep green and will never run.
    """

    def selection_id(self) -> str:
        return self.id


class DocumentContext(BaseModel, frozen=True):
    """Everything a page under ``docs/`` may need that its module cannot know.

    Three of these pages describe the composition rather than a subject — the
    roster audit, the parity table, the index — so they are functions of what
    every *other* module contributed. A module holding them as built values
    would have to be built after the content it describes, which is the module
    it is part of; passing the composed roster instead breaks that circle in
    the one direction it actually runs, because content depends on no page.

    The rest name a checkout: one resolves fixture citations against lup's own
    suite and one draws the application's layout by walking it, so importing a
    document module reads no filesystem and building one does.
    """

    layout: ApplicationLayout
    """Where the reading project's own code sits, for prose that names it."""

    root: Path
    """The checkout being described, for the pages that walk or cite one."""

    skills: list[models.Skill] = []
    agents: list[models.Agent] = []
    subapps: list[SubAppSpec] = []
    """The composed roster, for the pages whose subject is the roster itself.

    The sub-apps are here for the same reason, one turn further round: which
    commands a CLI serves follows from which modules were adopted, so a page
    listing them describes a composition it is itself part of.
    """

    plugin: models.NativeName = "lup"
    """What the composed plugin is called, for an invocation a page renders."""

    claude_decodes: list[str] = []
    codex_decodes: list[str] = []
    """What each runtime's hook decodes, for the parity audit.

    Only a root composing the concrete runtimes may name these, which is why
    they arrive here rather than being read where the page is declared.
    """

    library_checkout: Path | None = None
    """The tree holding lup's own suite, or ``None`` where lup is a distribution."""


class DocumentEntry(SelectableRule, frozen=True, arbitrary_types_allowed=True):
    """One page a module publishes: what it is called, and how it is rendered.

    The same pairing :class:`ModuleEntry` and
    :class:`~lup.devtools.roster.RosterEntry` use, for the same reason. A page
    is selected, listed and recorded under its semantic id long before anything
    renders it, and rendering needs a context a listing has no way to build —
    so the id answers without the builder running, and a project that declined
    a page never renders it.
    """

    semantic_id: str
    """The name ownership records it under, and a project retires it by."""

    source: str
    """The module this page is written in, as a path in the declaring checkout.

    Carried rather than read off the rendered page, because knowing who
    publishes a page must not require the context that renders one — which is
    what the coverage sweep asks, over a tree it is deliberately not composing.
    """

    build: Callable[[DocumentContext], models.Document]
    """How the page is rendered, once there is a composition to render it from."""

    def selection_id(self) -> str:
        return self.semantic_id

    def given(self, taken: list[str]) -> "DocumentEntry":
        """This page as a project that took *taken* modules renders it."""
        build = self.build
        return self.model_copy(
            update={"build": lambda context: build(context).given(taken)}
        )


class Module(BaseModel, frozen=True):
    """One module as adopted: what it is, and everything it *declares*.

    Every surface defaults to nothing, because carrying only some of them is
    the ordinary case: a module can be one document and no skills, or policy
    and no prose at all. Defaulting them lets a declaration say what a subject
    has by naming it, rather than by an emptiness a reader has to infer.

    The two surfaces that are only names — sub-apps and tool groups — are on
    :class:`ModuleSpec` rather than here, so that reading which commands a CLI
    serves and which servers a session opens never builds a subject. What is
    left here is what has to be built to be known.
    """

    spec: ModuleSpec

    content: models.ContentRoster = models.ContentRoster()
    """The skills and agents this module's subject is served by."""

    guidance: list[models.GuidanceSection] = []
    """What it contributes to the always-loaded document, where one is taken."""

    documents: list[DocumentEntry] = []
    """Pages under ``docs/`` whose subject is this module's, unrendered."""

    def given(self, taken: list[str]) -> "Module":
        """This module as a project that took *taken* modules reads it.

        Every surface carrying prose is resolved, so a
        :class:`~lup.harness.models.WhereTaken` naming a module the project
        declined leaves nothing behind in a skill, a section, or a page.
        """
        return self.model_copy(
            update={
                "content": self.content.given(taken),
                "guidance": [section.given(taken) for section in self.guidance],
                "documents": [entry.given(taken) for entry in self.documents],
            }
        )


class Adoption(BaseModel, frozen=True):
    """What one project changed about one module, surface by surface.

    Adopting a module is not all-or-nothing, and neither is any surface within
    it. A project retires one module's skill, rewrites a second module's skill
    under the same id, adds a third skill of its own beside them, drops a
    section of a fourth module's prose, and says nothing at all about the rest
    — each of those is one entry naming one thing, against a module that goes
    on growing underneath.

    Every surface carries the same algebra, so what a project learns once it
    can spell everywhere: name what you drop, declare what you have, and stay
    silent about the rest. A surface a project says nothing about is the
    module's own answer for it, including for what the module grows after that
    project last read it.
    """

    module: str
    """Which module this is about, by id."""

    taken: bool | None = None
    """Whether this project has it — ``None`` defers to the module's default."""

    content: models.ContentSelection = models.ContentSelection()
    """Which of the module's skills and agents this project ships, and its own."""

    guidance: Selection[models.GuidanceSection] = Selection()
    """Which of its sections reach the document, and what this project rewrote.

    A section declared here replaces the module's under that id and renders in
    its chapter, so a project that keeps a subject but states it differently
    edits prose rather than forking the module.
    """

    loads_guidance: bool | None = None
    """Whether its prose reaches the document at all — ``None`` takes what it has.

    The coarse answer beside the fine one, because the two questions are
    different and only one of them is about words. Guidance is paid for in
    every session whether the subject comes up or not, and a project adopting
    a module for its tools is entitled to none of its prose without listing
    the sections it is declining — a list that would go stale the moment the
    module grew one.
    """

    documents: Selection[DocumentEntry] = Selection()
    """Which pages under ``docs/`` it publishes, by semantic id, and its own."""

    subapps: list[str] = []
    """Sub-app names from this module that its CLI does not serve."""

    tool_groups: list[str] = []
    """Tool groups from this module that its sessions are not offered.

    Retirement only, here and for ``subapps``: both are names resolved
    elsewhere rather than declarations carried here, so replacing one is
    something the module that owns it does, and adding one is something a
    project does by declaring a module of its own.
    """


class ModuleSelection(BaseModel, frozen=True):
    """Which modules a project has, and what it changed about each.

    A delta rather than a list of what is taken, so a module the library grows
    arrives under its own default instead of being absent from an enumeration
    nobody extended — the inversion every selection here takes, for the reason
    every one of them states.
    """

    adoptions: list[Adoption] = []

    def adoption(self, module_id: str) -> Adoption:
        """What this project said about one module, or that it said nothing."""
        for entry in self.adoptions:
            if entry.module == module_id:
                return entry
        return Adoption(module=module_id)

    def declined(self) -> list[str]:
        """Modules this project turned off outright, by id.

        Only the explicit refusals: a module left to its own default said
        nothing and is nobody's decision to meet again, while one written down
        as ``taken=False`` is a choice somebody made against the roster as it
        stood then, which keeps growing after it.
        """
        return [entry.module for entry in self.adoptions if entry.taken is False]

    def takes(self, spec: ModuleSpec) -> bool:
        """Whether this project has a module, deferring where it stated nothing.

        An essential module is taken by saying nothing whatever its default,
        since there is no composition without it; one declined anyway is
        answered as declined, so the refusal can name it.
        """
        taken = self.adoption(spec.id).taken
        return (spec.default_on or spec.essential) if taken is None else taken

    def loads(self, spec: ModuleSpec) -> bool:
        """Whether a module's prose reaches this project's always-loaded document.

        Two answers, and a module has to pass both. One it never took says
        nothing about any subject; one it took for the code and declined the
        prose of contributes every other surface and no words.
        """
        return self.takes(spec) and self.adoption(spec.id).loads_guidance is not False

    def resolved(self, module: Module) -> Module:
        """One module as this project takes it, every surface narrowed.

        Returning a module rather than a list per surface is what keeps the
        result readable: whatever walks the composition sees the same type it
        would have seen with no selection at all, and a reader asking what a
        project actually ships reads it off one value.

        ``loads_guidance`` is deliberately *not* applied here. It answers where
        the prose goes rather than what the module has, and the difference is
        load-bearing: a budget asking what an adopter turning this module on
        would carry, and a listing saying what each module's paragraph costs,
        both need the prose of a module this project keeps quiet. So the
        silence is applied by :func:`composed_guidance`, which is the one place
        the document is assembled.
        """
        entry = self.adoption(module.spec.id)
        return Module(
            spec=module.spec,
            content=module.content.selected(entry.content),
            guidance=entry.guidance.over(module.guidance),
            documents=entry.documents.over(module.documents),
        )

    def subapps(self, specs: list[ModuleSpec]) -> list[str]:
        """Every top-level CLI group the adopted modules own, in roster order.

        Read off the specs rather than off built modules, which is what lets a
        skill that names the CLI roster be handed one: the roster is known
        before the first builder runs, and every builder runs to produce the
        content that would otherwise have had to describe it.
        """
        return [
            name
            for spec in specs
            if self.takes(spec)
            for name in spec.subapps
            if name not in self.adoption(spec.id).subapps
        ]

    def tool_groups(self, specs: list[ModuleSpec]) -> list[str]:
        """Every MCP tool group an adopted module offers a session, in roster order."""
        return [
            group
            for spec in specs
            if self.takes(spec)
            for group in spec.tool_groups
            if group not in self.adoption(spec.id).tool_groups
        ]

    def withheld_tool_groups(self, specs: list[ModuleSpec]) -> list[str]:
        """Every tool group a module offers that this project's sessions are not.

        The other side of :meth:`tool_groups`, for the declaration that lists a
        project's groups by builder: a group no module claims is the project's
        own and stays, so what a session loses is exactly what was declined.
        """
        offered = self.tool_groups(specs)
        return [
            group
            for spec in specs
            for group in spec.tool_groups
            if group not in offered
        ]

    def requirements(self, manifest: Manifest, specs: list[ModuleSpec]) -> Manifest:
        """What this project asks of a machine, less what only declined modules need.

        A requirement is kept where no module claims it — the project's own —
        or where a module claiming it is taken. Narrowed here rather than where
        the manifest is written, so a module declined in the catalog stops
        costing a machine its programs without anybody editing the roster of
        requirements to match.
        """
        claimed = [name for spec in specs for name in spec.requirements]
        needed = [
            name for spec in specs if self.takes(spec) for name in spec.requirements
        ]
        return manifest.model_copy(
            update={
                "requirements": [
                    item
                    for item in manifest.requirements
                    if item.capability not in claimed or item.capability in needed
                ]
            }
        )


class ModuleEntry(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One module the library ships: what it says it is, and how it is built.

    The pair is what makes the two readings one table. A roster listed in a
    document and a roster composed into a harness are the same entries seen
    through different halves of this model, so neither can name a module the
    other does not — and the builder answers only for a module somebody took.
    """

    spec: ModuleSpec
    build: Callable[[], Module]


def unmet_requirements(specs: list[ModuleSpec]) -> list[str]:
    """Every ``requires`` among these modules naming one that is not among them.

    Returned rather than raised, so the caller decides what a gap means: a
    composition refuses on it, and a listing command shows the same rows as
    something an operator is about to settle.
    """
    present = {spec.id for spec in specs}
    return [
        f"module {spec.id!r} requires {needed!r}, which this project does not take"
        for spec in specs
        for needed in spec.requires
        if needed not in present
    ]


def declined_essentials(
    specs: list[ModuleSpec], selection: ModuleSelection
) -> list[str]:
    """Every essential module this selection declines, as the refusal it earns.

    Returned rather than raised for the reason :func:`unmet_requirements` is:
    a composition refuses on it, and a listing shows the same rows.
    """
    return [
        f"module {spec.id!r} is essential and cannot be declined: every other "
        "module stands on it"
        for spec in specs
        if spec.essential and not selection.takes(spec)
    ]


def required_closure(start: list[str], specs: list[ModuleSpec]) -> list[str]:
    """These modules, and every module their ``requires`` reach, in reach order."""
    needs = {spec.id: spec.requires for spec in specs}
    reached = list(dict.fromkeys(start))
    # Appending while walking is the breadth-first closure: each module
    # reached is itself walked once, in the order it was reached.
    for held in reached:
        for needed in needs.get(held, []):
            if needed not in reached:
                reached.append(needed)
    return reached


def anchored(specs: list[ModuleSpec]) -> list[str]:
    """Every module every project has: the essential ones, and what they require.

    Only the first half is declared. A module an essential one requires is
    just as impossible to decline — the requirement check refuses it — so a
    listing saying what a project may decline has to say that too.
    """
    return required_closure([spec.id for spec in specs if spec.essential], specs)


def standing(module: str, specs: list[ModuleSpec]) -> list[str]:
    """Every module one module can count on being there, itself included.

    What it requires, what those require in turn, and every module every
    project has — the set a declaration may name without wrapping the mention
    in a :class:`~lup.harness.models.WhereTaken`.
    """
    return required_closure([module, *anchored(specs)], specs)


def scaffold_selection(
    specs: list[ModuleSpec], adoptions: list[Adoption] | None = None
) -> ModuleSelection:
    """What the repository shipping the scaffold takes: everything, quietly.

    A scaffold is the demonstration of its own machinery, so it takes every
    module it ships — a subject nobody here composes is a subject nobody here
    would notice breaking. What it does not do is carry every module's prose,
    because taking a module for its code and being told about it in every
    session are different questions, and the always-loaded document is the one
    surface where the second is paid for whether or not the subject comes up.

    So the rule is derived rather than written down: a module offered to
    adopters is one this repository is presumed to work in, and its section
    loads; a module off by default is one most projects do not have, and the
    scaffold takes it without teaching it. Deriving matters more than the rule
    — a hand-kept list of which modules speak would go stale the moment the
    library grew one, and the copy that fell behind reads as a decision.

    Two answers are derived and the rest is the project's. Whatever a scaffold
    stated about a module — a skill it added, a section it rewrote, a page it
    declined — is carried through untouched; only *taken* and *loads_guidance*
    are answered here, because those are the two a scaffold should not be
    settling module by module.
    """
    stated = {entry.module: entry for entry in adoptions or []}
    return ModuleSelection(
        adoptions=[
            (stated.get(spec.id) or Adoption(module=spec.id)).model_copy(
                update={"taken": True, "loads_guidance": spec.default_on}
            )
            for spec in specs
        ]
    )


def adopted(
    entries: list[ModuleEntry], selection: ModuleSelection | None = None
) -> list[Module]:
    """Build the modules this project takes, and only those.

    The requirement check runs before any builder, which is what keeps a
    declined subject from costing an import: a module reaching into one the
    project does not have is answered here rather than by whichever
    declaration first fails to resolve. An essential module declined is
    refused in the same breath, since every other module stands on it.

    Each module is then read as a project with exactly these modules reads
    it, so a mention another module's content wrapped in
    :class:`~lup.harness.models.WhereTaken` survives only where the module it
    names was taken too.
    """
    resolved = selection or ModuleSelection()
    taken = [entry for entry in entries if resolved.takes(entry.spec)]
    unmet = [
        *declined_essentials([entry.spec for entry in entries], resolved),
        *unmet_requirements([entry.spec for entry in taken]),
    ]
    if unmet:
        raise ValueError("; ".join(unmet))
    ids = [entry.spec.id for entry in taken]
    return [resolved.resolved(entry.build()).given(ids) for entry in taken]


def composed_content(modules: list[Module]) -> models.ContentRoster:
    """Every adopted module's roster, in module order.

    The narrowing happened where the module was resolved, so this is a
    concatenation and nothing else: a surface that had to consult the
    selection a second time would be a second place for the same decision to
    come out differently.
    """
    return models.ContentRoster(
        skills=[skill for module in modules for skill in module.content.skills],
        agents=[agent for module in modules for agent in module.content.agents],
    )


def composed_documents(
    modules: list[Module], context: DocumentContext
) -> list[models.Document]:
    """Every page the adopted modules publish, rendered, in module order.

    Rendering is here rather than where each module declared its pages because
    the context is the composition's own: a module knows which pages are its
    subject, and only the root that gathered every module knows the roster
    three of them describe.
    """
    return [entry.build(context) for module in modules for entry in module.documents]


def composed_guidance(
    modules: list[Module],
    selection: ModuleSelection | None = None,
    chapters: list[models.GuidanceChapter] | None = None,
) -> list[models.GuidanceSection]:
    """The always-loaded document, chapter by chapter and module by module.

    Reading order is the spine crossed with the module roster, and neither
    half alone will do. Ordering by module scatters a chapter across the
    document — what to run is three subjects in a row — and ordering by a
    central list of section ids puts every module the library grows through an
    edit in every project that adopts it. A section names only its chapter;
    where it sits inside one is where its module sits, which is declared
    already.

    This is where a module kept for its code and declined its prose falls
    silent, because this is where the document exists. A module told not to
    speak contributes none of its sections, including the ones this project
    itself wrote about that subject — keeping those would read as a module
    going on talking after being asked to stop.
    """
    order = models.default_chapters() if chapters is None else chapters
    resolved = selection or ModuleSelection()
    return [
        section
        for chapter in order
        for module in modules
        if resolved.loads(module.spec)
        for section in module.guidance
        if section.chapter == chapter
    ]


def unloaded_guidance(
    modules: list[Module], selection: ModuleSelection | None = None
) -> list[models.GuidanceSection]:
    """Every section this roster offers that this project's document omits.

    The distance between what a tree pays and what bounds a project taking
    everything. A module declined outright and one kept quiet contribute the
    same nothing to the document and the same weight to that bound, so both
    answer here.
    """
    resolved = selection or ModuleSelection()
    return [
        section
        for module in modules
        if not resolved.loads(module.spec)
        for section in module.guidance
    ]


class Composition(BaseModel, frozen=True):
    """One roster as one selection adopted it, and every surface read off it.

    Every surface a tree is built from — the plugin's skills and agents, the
    always-loaded document, the pages, the command trees, the tool groups, the
    requirements asked of a machine — is a function of the same three values,
    so they travel as one. A root composes its own selection into one of these;
    a check asking what another selection would compose builds a second from
    the same entries, and nothing downstream can tell the two apart — which is
    what lets a gate generate the tree a project declining one module would
    get, without being that project.
    """

    specs: list[ModuleSpec]
    """The whole roster, taken or not, in the order the composition lays it out."""

    selection: ModuleSelection
    modules: list[Module]
    """The modules taken, each read as a project holding exactly these reads it."""

    @classmethod
    def of(
        cls, entries: list[ModuleEntry], selection: ModuleSelection | None = None
    ) -> "Composition":
        """Adopt *selection* over *entries*, refusing a selection that cannot stand."""
        chosen = selection or ModuleSelection()
        return cls(
            specs=[entry.spec for entry in entries],
            selection=chosen,
            modules=adopted(entries, chosen),
        )

    def taken(self) -> list[str]:
        """Every module this composition holds, by id, in roster order."""
        return [module.spec.id for module in self.modules]

    def content(self) -> models.ContentRoster:
        """Every skill and agent the plugin ships."""
        return composed_content(self.modules)

    def guidance(
        self, chapters: list[models.GuidanceChapter] | None = None
    ) -> list[models.GuidanceSection]:
        """The always-loaded document's sections, chapter by chapter."""
        return composed_guidance(self.modules, self.selection, chapters)

    def documents(self, context: DocumentContext) -> list[models.Document]:
        """Every page the taken modules publish, rendered against *context*."""
        return composed_documents(self.modules, context)

    def subapps(self) -> list[str]:
        """Every command tree a taken module owns, in roster order."""
        return self.selection.subapps(self.specs)

    def tool_groups(self) -> list[str]:
        """Every tool group a session is offered."""
        return self.selection.tool_groups(self.specs)

    def withheld_tool_groups(self) -> list[str]:
        """Every tool group a declined module owns."""
        return self.selection.withheld_tool_groups(self.specs)

    def requirements(self, manifest: Manifest) -> Manifest:
        """What a machine is asked for, less what only declined modules need."""
        return self.selection.requirements(manifest, self.specs)
