"""What this project publishes through each native target, and what writes it.

The builders and the selector are the library's; named here is only what is
this project's own — the content its harness compiles beside, the per-runtime
guidance each tree carries, and the generated files that belong to no native
tree at all.
"""

from functools import partial
from pathlib import Path

from lup.harness.codescan.common import RuleSelection
from lup.devtools.dev.model_catalog import catalog_writers, library_catalogs
from lup.devtools.dev.settings_schema import SettingsSchemaSource, settings_writers
from lup.devtools.dev.rules import write_rule_reference
from lup.devtools.dev.git_guards import write_git_guards
from lup.devtools.dev.subprojects import write_sub_projects
from lup.devtools.dev.workflow import write_publish, write_workflow
from lup.devtools.harness.composition import NativeTargets
from lup.providers.claude.composition import ClaudeComposer
from lup.providers.codex.composition import CodexComposer
from lup.devtools.harness.drift import RepositoryWriter
from lup.harness.generate import (
    NativeHarnessComposition,
    ProjectContent,
)
from lup.devtools.harness.generated_paths import write_generated_paths
from lup.devtools.dashboard.keys import write_keymap
from lup.devtools.surfaces import LIBRARY_SURFACES
from lup.web.build import write_web_bundles
from lup.web.schema import write_view_schema
from lup.harness.models import PromptDocument
from lup.harness.modules import Composition
from lup_template.harness.catalog import (
    PUBLISH,
    WORKFLOW,
    declared_hook_set,
    declared_sub_projects,
    launched_serve,
    launched_tool_servers,
    portable_harness,
)
from lup_template.devtools.dev.app import declared
from lup_template.harness.content.catalog import COMPOSITION
from lup_template.harness.content.docs.catalog import documents
from lup_template.harness.content.modules.specs import TEMPLATE_INIT
import lup_template.harness.content.settings as settings_module
from lup_template.harness.content.settings import project_settings
from lup_template.harness.content.template_claude import (
    DOCUMENT as TEMPLATE_CLAUDE,
)
from lup_template.harness.content.template_codex import (
    DOCUMENT as TEMPLATE_CODEX,
)

CONTENT_ROOT = Path(__file__).parent / "content"


def project_content(
    root: Path,
    rules: RuleSelection | None = None,
    composed: Composition = COMPOSITION,
) -> ProjectContent:
    """Everything this repository publishes beside its compiled plugin tree.

    ``rules`` is a launch overruling what this repository holds itself to for
    one session, and nothing else reads it: what the repository actually
    settled stays the declaration in its catalog, which is what a review sees
    and what `dev seams` writes. *composed* is this repository's roster unless
    a caller asks what another selection would publish.
    """
    harness = portable_harness(root=root, composed=composed)
    if rules is not None:
        harness = harness.holding(rules)
    servers = launched_tool_servers(composed.withheld_tool_groups())
    return ProjectContent(
        harness=harness,
        documents=documents(root, composed),
        assets=[CONTENT_ROOT / "assets" / "file_suggest.sh"],
        settings=project_settings(harness.plugins[0], servers),
        settings_source=settings_module.__name__,
        servers=servers,
        serve=launched_serve(root),
    )


def installer_guidance(
    document: PromptDocument, composed: Composition = COMPOSITION
) -> PromptDocument | None:
    """The guidance `/lup:install` merges into a target, where this project has it.

    Template-init's, because the install skill is what carries it into
    another repository: a project that declined the module installs nothing
    and publishes no template for it. Where it is taken, the template is read
    as this roster reads it, so a line pointing at a declined module's skill
    is not handed to a downstream project either.
    """
    if TEMPLATE_INIT.id not in composed.taken():
        return None
    return document.given(composed.taken())


def claude_target(
    root: Path, rules: RuleSelection | None = None
) -> NativeHarnessComposition:
    """This project's content, compiled through the Claude adapter."""
    return ClaudeComposer().compose(
        root, project_content(root, rules), installer_guidance(TEMPLATE_CLAUDE)
    )


def codex_target(
    root: Path, rules: RuleSelection | None = None
) -> NativeHarnessComposition:
    """This project's content, compiled through the Codex adapter."""
    return CodexComposer().compose(
        root, project_content(root, rules), installer_guidance(TEMPLATE_CODEX)
    )


TARGETS = NativeTargets(builders={"claude": claude_target, "codex": codex_target})
"""Every native runtime this project generates a tree for, by CLI selector."""


def write_declared_git_guards(root: Path | None = None, *, check: bool = False) -> Path:
    """Compile the dev tree's own git guards, so the hooks run what it installs.

    Read when generation runs rather than when this module loads, because the
    declaration reads the checkout, and a manifest a merge left conflicted
    must not stop the CLI that repairs it from importing.
    """
    return write_git_guards(declared().git_guards, root, check=check)


# lup: ignore[constant-declaration] — which files outside a runtime tree this
# project generates, decided here because nothing sits above it to be asked
REPOSITORY_WIDE: list[RepositoryWriter] = [
    partial(write_rule_reference, selection=declared_hook_set().rules),
    partial(write_workflow, WORKFLOW),
    partial(write_publish, PUBLISH),
    partial(write_sub_projects, declared_sub_projects()),
    write_declared_git_guards,
    partial(write_generated_paths, TARGETS),
    # The schema before the bundles, because the frontend build compiles its
    # types from it: written in this order, one generation leaves both true.
    # Both read the one surface list, so a page and its types cannot disagree
    # about which surfaces exist.
    partial(
        write_view_schema, Path("packages/lup/web/schema/views.json"), LIBRARY_SURFACES
    ),
    # The dashboard's keymap, which its page compiles in as its key table.
    partial(write_keymap, Path("packages/lup/web/schema/keymap.json")),
    partial(
        write_web_bundles,
        Path("packages/lup/web"),
        Path("packages/lup/src/lup/web/bundles"),
        LIBRARY_SURFACES,
    ),
    *catalog_writers(library_catalogs()),
    *settings_writers(SettingsSchemaSource()),
]
"""Every project-owned generated file outside a native runtime tree."""
