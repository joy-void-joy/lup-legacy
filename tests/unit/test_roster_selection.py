"""A project's roster delta, and the two readings it has to reach together.

The failure this guards against is the one the roster exists to remove: a
sub-app the library ships that a project never learns it has. That failure is
silent by construction — an absent name looks exactly like a declined one — so
what is pinned here is that the default really is everything, that a retirement
reaches both the wired CLI and the documents describing it, and that this
repository has declined nothing without saying so.

The wired half is exercised end to end by ``test_devtools_cli``, which walks
the composed tree; what these need is the projection, so they take specs and
throwaway Typer apps rather than a whole declaration.
"""

from unittest import mock

import typer

from lup.harness.codescan.common import RuleSelection
from lup.harness.content.application import ApplicationLayout
from lup.harness.content.modules.catalog import library_modules
from lup.harness.content.modules.specs import LIBRARY_SPECS as LIBRARY_MODULE_SPECS
from lup.harness.modules import (
    DocumentContext,
    adopted,
    composed_content,
    composed_documents,
    scaffold_selection,
)
from lup.devtools.roster import (
    LIBRARY_ROSTER,
    LIBRARY_SPECS,
    DevtoolsDeclarations,
)
from lup.devtools.subapps import SubApp, SubAppSelection, subapp
import lup.harness.models as models
from lup.harness.models import ContentSelection
from lup.workspace.paths import project_root
from lup_template.harness.content.catalog import modules as composed_modules
from lup_template.harness.content.catalog import (
    APPLICATION_SPECS,
    SUBAPP_SELECTION,
    SUBAPP_SPECS,
)

SKILL_ADDED = models.Skill(
    id="skill.worked-example",
    name="worked-example",
    description="A skill no library ships, for the additive half of the algebra.",
    prompt=models.PromptDocument(
        source=__name__, parts=[models.TextPart(text="Do the worked example.")]
    ),
)
"""One declaration a project has and the library does not.

Built here rather than borrowed from either roster because what it exercises
is arrival: a skill taken from the library's own list would resolve as a
replacement of itself and prove nothing about the additive half."""

LIBRARY_MODULES = adopted(
    library_modules(ApplicationLayout(package="worked_example"), RuleSelection()),
    scaffold_selection(LIBRARY_MODULE_SPECS),
)
"""Every module lup ships, under a package name that is nobody's real one.

Selection is what these exercise, and it does not read a path — so naming a
package here that no checkout has keeps a roster assertion from passing only
because the layout happened to match this repository's own.
"""

LIBRARY_CONTENT = composed_content(LIBRARY_MODULES)
"""The whole library roster, read through the modules that declare it."""

RETIRED = SubAppSelection(retired=["dashboard", "report"])


def wired(name: str) -> SubApp:
    """One throwaway entry, for a projection that never runs a command."""
    return subapp(name, f"{name} help", typer.Typer())


def test_selecting_nothing_serves_every_subapp_the_library_ships() -> None:
    """The default has to be the whole roster, or adopting the seam changes it."""
    defaults = [wired(spec.name) for spec in LIBRARY_SPECS]

    served = SubAppSelection().over(defaults, [])

    assert [entry.spec.name for entry in served] == sorted(
        spec.name for spec in LIBRARY_SPECS
    )


def test_a_retired_subapp_leaves_the_wired_roster() -> None:
    defaults = [wired(spec.name) for spec in LIBRARY_SPECS]

    served = {entry.spec.name for entry in RETIRED.over(defaults, [])}

    assert served.isdisjoint(RETIRED.retired)
    assert "dev" in served


def test_a_retired_subapp_leaves_the_documents_that_name_the_roster() -> None:
    """A page promising a command tree nothing serves is worse than no page."""
    named = {spec.name for spec in RETIRED.specs(LIBRARY_SPECS, [])}

    assert named.isdisjoint(RETIRED.retired)
    assert "dev" in named


def test_an_added_subapp_naming_a_default_replaces_it() -> None:
    """Overriding is how a project keeps its own, not retire-then-add."""
    defaults = [wired(spec.name) for spec in LIBRARY_SPECS]
    own = subapp("trace", "this project's own tracing", typer.Typer())

    served = SubAppSelection().over(defaults, [own])

    assert [entry for entry in served if entry.spec.name == "trace"] == [own]
    assert len(served) == len(LIBRARY_SPECS)


def test_both_readings_of_the_roster_name_the_same_subapps() -> None:
    """One table, two projections — a document and a CLI cannot disagree."""
    assert LIBRARY_SPECS == [entry.spec for entry in LIBRARY_ROSTER]


def test_a_retired_skill_leaves_the_roster_the_plugin_ships() -> None:
    """Named by declaration id, which prefixes what a skill and an agent share."""
    retired = ContentSelection(retired=["skill.commit"])

    kept = LIBRARY_CONTENT.selected(retired)

    assert {skill.id for skill in kept.skills}.isdisjoint(retired.retired)
    assert any(skill.id == "skill.review" for skill in kept.skills)
    assert kept.agents == LIBRARY_CONTENT.agents


def test_one_selection_reaches_agents_as_well_as_skills() -> None:
    retired = ContentSelection(retired=["agent.implementer"])

    kept = LIBRARY_CONTENT.selected(retired)

    assert {agent.id for agent in kept.agents}.isdisjoint(retired.retired)
    assert kept.skills == LIBRARY_CONTENT.skills


def test_selecting_nothing_ships_every_declaration_the_library_has() -> None:
    whole = LIBRARY_CONTENT.selected(ContentSelection())

    assert whole == LIBRARY_CONTENT


def test_a_declared_skill_follows_the_inherited_half() -> None:
    """Additive after subtractive, which is what makes the order readable."""
    added = SKILL_ADDED
    extended = LIBRARY_CONTENT.selected(ContentSelection(skills=[added]))

    assert extended.skills == [*LIBRARY_CONTENT.skills, added]


def test_a_declared_skill_replaces_the_library_one_of_its_id_in_place() -> None:
    """The capability a retire-then-re-add could not express.

    Two declarations under one name is the ambiguity the algebra exists to
    remove: whichever a walk reached first would be the one that shipped, and
    which that is would depend on the order a composition happened to build.
    So a project's own declaration takes the library's seat rather than
    following it, and the roster is one shorter than a concatenation.
    """
    mine = SKILL_ADDED.model_copy(update={"id": "skill.commit", "name": "commit"})

    resolved = LIBRARY_CONTENT.selected(ContentSelection(skills=[mine]))

    assert len(resolved.skills) == len(LIBRARY_CONTENT.skills)
    assert [skill for skill in resolved.skills if skill.id == "skill.commit"] == [mine]


def test_the_older_retire_then_re_add_idiom_still_ships_the_projects_own() -> None:
    """``retired`` names the library's declarations, never the project's own.

    Replacing a skill can also be spelled as retiring the id and adding a
    whole declaration back — and a project that writes it that way gets the
    roster an override would give, not an empty seat. So the two
    halves read in one direction: the retirement takes the library's out, the
    declaration puts the project's in, and a project that no longer wants the
    id at all deletes its own declaration rather than retiring around it.
    """
    mine = SKILL_ADDED.model_copy(update={"id": "skill.commit", "name": "commit"})

    resolved = LIBRARY_CONTENT.selected(
        ContentSelection(retired=["skill.commit"], skills=[mine])
    )

    assert [skill for skill in resolved.skills if skill.id == "skill.commit"] == [mine]


def test_no_library_declaration_names_the_template_package() -> None:
    """A library skill must name the reading project's package, not the template's.

    The template's own package is `lup_template`, and for most of this
    library's life every declaration that needed to name an application path
    wrote that literal. It reads correctly in exactly one repository: the
    renamer rewrites imports and dotted module paths but not prose, and a
    project resolving lup from the index, from git, or from a linked checkout
    never renames the library at all. So the literal survives into every
    downstream tree and sends its reader to a directory that is not there.

    Built here under a package no checkout has, so the assertion cannot pass
    by the layout happening to match this repository's own.
    """
    declarations = [*LIBRARY_CONTENT.skills, *LIBRARY_CONTENT.agents]
    leaked = {
        declaration.id
        for declaration in declarations
        for part in declaration.prompt.parts
        if "lup_template" in (part.text_payload or "")
    }

    assert leaked == set()


def test_no_published_page_names_the_template_package() -> None:
    """The pages lup publishes answer for the literal the same way its skills do.

    `docs/` is generated into whichever repository runs generation, so a page
    naming `src/lup_template/` sends every downstream reader to a directory
    that is not theirs — the same defect as in a skill, and invisible in this
    repository for the same reason.
    """
    layout = ApplicationLayout(package="worked_example")
    pages = composed_documents(
        LIBRARY_MODULES,
        DocumentContext(
            layout=layout,
            root=project_root(),
            skills=LIBRARY_CONTENT.skills,
            agents=LIBRARY_CONTENT.agents,
            library_checkout=project_root(),
        ),
    )
    leaked = {
        page.semantic_id
        for page in pages
        for part in page.document.parts
        if "lup_template" in (part.text_payload or "")
    }

    assert leaked == set()


def test_only_the_skills_about_renaming_name_the_template_package() -> None:
    """This repository's own skills answer for the literal the same way.

    Two of them may keep it, because for those the literal is the subject
    rather than a path: `/lup:init` renames `src/lup_template/` and `/lup:install`
    teaches that rename, so both have to spell the directory they are about.
    Every other declaration names a path a renamed project still has to find,
    and takes it from the layout.
    """
    allowed = {"skill.init", "skill.install"}
    named = {
        skill.id
        for module in composed_modules(ApplicationLayout(package="worked_example"))
        for skill in module.content.skills
        for part in skill.prompt.parts
        if "lup_template" in (part.text_payload or "")
    }

    assert named == allowed


def test_this_repository_declines_nothing_without_saying_so() -> None:
    """Every sub-app is owned by a module this repository takes.

    The retirement is derived from the module roster rather than written down,
    so an empty one is the claim that every command tree lup ships answers to a
    subject this repository adopted — and a sub-app that fell out of every
    module's ownership would show up here as a retirement nobody decided.
    """
    assert SUBAPP_SELECTION.retired == []
    assert {spec.name for spec in SUBAPP_SPECS} == {
        *(spec.name for spec in LIBRARY_SPECS),
        *(spec.name for spec in APPLICATION_SPECS),
    }


def test_a_retired_subapp_is_never_built_at_all() -> None:
    """Dropped after construction is not the same as never constructed.

    A builder may import an optional extra — the dashboard serves over HTTP,
    the conversation sub-app drives a browser — so building one a project
    declined makes that project carry a dependency for a command it does not
    serve. Measured: a project retiring `dashboard` still could not start its
    CLI without `lup-agents[web]`, because the roster built the app before the
    selection ever saw it.
    """
    built: list[str] = []

    def watched(name: str):
        def build(declared: DevtoolsDeclarations) -> typer.Typer:
            built.append(name)
            return typer.Typer()

        return build

    entries = [
        entry.model_copy(update={"build": watched(entry.spec.name)})
        for entry in LIBRARY_ROSTER
    ]
    with mock.patch("lup.devtools.roster.LIBRARY_ROSTER", entries):
        declarations = mock.Mock(spec=DevtoolsDeclarations)
        DevtoolsDeclarations.roster(declarations, ["dashboard", "conversation"])

    assert "dashboard" not in built
    assert "conversation" not in built
    assert "dev" in built


def test_declining_nothing_builds_everything_it_used_to() -> None:
    """The default is what every existing caller already got."""
    built: list[str] = []

    def watched(name: str):
        def build(declared: DevtoolsDeclarations) -> typer.Typer:
            built.append(name)
            return typer.Typer()

        return build

    entries = [
        entry.model_copy(update={"build": watched(entry.spec.name)})
        for entry in LIBRARY_ROSTER
    ]
    with mock.patch("lup.devtools.roster.LIBRARY_ROSTER", entries):
        declarations = mock.Mock(spec=DevtoolsDeclarations)
        DevtoolsDeclarations.roster(declarations)

    assert sorted(built) == sorted(entry.spec.name for entry in LIBRARY_ROSTER)
