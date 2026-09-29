# lup: ignore[constant-declaration]
# Every constant here is this repository's own composition — which runtimes it
# builds for, and what it calls its own harness session. A composition root is
# where a judgement is finally made rather than passed on, so there is no
# caller above it to take these from.
"""Root of the project-owned harness declaration graph.

The declaration leaves — skills, agents, prompt documents, settings, and
assets — live under ``content/``, aggregated by ``content.catalog``. This
module assembles those leaves with the hook policy and the resolver spec
into the portable ``Harness`` that ``generate`` compiles into both native
trees. Generated guidance and the adopter docs point here as the file that
owns URL scopes, protected edit roots, and the shell vocabulary declared in
``content.shell_vocabulary``, so its path is part of the documented surface.
"""

from pathlib import Path

from pydantic import AnyHttpUrl

from lup.harness.models import (
    CarrierPins,
    Harness,
    HookPathRole,
    HookSandbox,
    HookSet,
    HookUrlScope,
    Plugin,
    ResolveSpec,
    SkillInvocation,
)
from lup.providers.claude.harness import ClaudeSpellings
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.harness import CodexSpellings
from lup.providers.codex.login import CODEX_LOGIN
from lup.policy.refused_paths import credential_files
from lup.policy.rules import dependency_declarations
from lup.harness.codescan.common import ApplicationRoots
from lup.harness.codescan.boundaries import (
    generated_tree_paths,
    native_import_boundaries,
)
from lup.harness.content.modules.specs import RESOLVER
from lup.devtools.dev.documented import MENTION, WrittenCommand
from lup.harness.dependencies import Published
from lup_template.harness.content.modules.specs import TEMPLATE_INIT
from lup_template.harness.content.template_claude import DOCUMENT as TEMPLATE_CLAUDE
from lup_template.harness.content.template_codex import DOCUMENT as TEMPLATE_CODEX
from lup.devtools.dev.check import BunTestRoot, TestRoot, collected_test_roles
from lup.devtools.dev.library import DISTRIBUTION, VENDORED_ROOT, library_trackers
from lup.devtools.dev.release import ReleaseSpec
from lup.devtools.dev.reach import Spread
from lup.devtools.dev.scaffold import ScaffoldSource
from lup.devtools.dev.seams import DECLARED_SEAMS, Seam
from lup.devtools.dev.workflow import FrontendSpec, PublishSpec, WorkflowSpec
from lup.devtools.project import DevProject
from lup.harness.contracts import NativeSpellings
from lup.harness.enforcement import declared_role_rows
from lup.policy.boundary import depends_on
from lup.coordination.policy import peer_policy, wake_socket_refusal
from lup.policy.refused_tools import RefusedTool
from lup.workspace.paths import (
    declared_project_root,
    project_root,
    read_project_name,
)
from lup.mcp import ServeLaunch, ToolServer
from lup.tools.toolsets import startup_names
from lup_template.agent.toolsets import (
    declared_tool_groups,
    declared_tool_servers,
    session_needs,
)
from lup.devtools.roster import LIBRARY_SPECS as LIBRARY_SUBAPPS
from lup.harness.coverage import ContentRoot, ModuleCoverage
from lup.harness.modules import Composition
import lup_template.harness.content.guidance as guidance
from lup_template.devtools.subapps import APPLICATION_ROSTER
from lup_template.harness.content.catalog import (
    COMPOSITION,
    LAYOUT,
    MODULE_SELECTION,
    RULES,
    SUBAPP_SELECTION,
    WITHHELD_TOOL_GROUPS,
    entries,
)
from lup_template.harness.content.docs.catalog import context
from lup_template.harness.content.image import agent_image
from lup_template.harness.content.requirements import manifest
from lup_template.harness.content.shell_vocabulary import (
    EVERYDAY_COMMANDS,
    INTEGRATION_BRANCHES,
    RUNNER_TARGETS,
    SHELL_RULES,
)

EXCLUDED_COMMANDS = [
    # Egress the sandbox proxy cannot carry: it allowlists hostnames over
    # HTTP, and the transport underneath a git remote is SSH on port 22. No
    # narrower lever reaches this — a credential path takes a mode and not a
    # command, and narrowing the path would still leave ssh unable to read the
    # key it authenticates with.
    "ssh *",
    "git *",
    # `gh` reaches hosts the allowlist already admits, but it drives `git` for
    # anything touching a remote, and a child of a confined command is
    # confined too — so excluding git without it moves the same failure one
    # call deeper.
    "gh *",
    # The toolchain opens agent sessions, and a runtime keeps per-session
    # state under its own configuration directory — for Claude Code,
    # `~/.claude/session-env/<session id>`, following `CLAUDE_CONFIG_DIR`.
    # That directory is the runtime protecting its own state rather than a
    # path a grant can widen: a live session lists the repository root under
    # `allowOnly` and the configuration home below it again under
    # `denyWithinAllow`, enumerated by the runtime whether or not a project
    # declares anything. Nor is a private home the remedy — every entry such
    # a home does not own links back to the shared one — and session state is
    # only the first thing this toolchain writes outside the tree; a
    # worktree, a plugin cache, and the git configuration behind them follow.
    # A session opened inside that boundary dies on its first shell call with
    # a bare EROFS, which reads to an agent like a broken repository rather
    # than like a boundary: one planning run finished that way and looked
    # normal.
    "uv run lup-devtools harness *",
    # A resolver run opens native sessions, which is the same requirement the
    # line above states from its own sub-app, and the exclusion follows the
    # commands rather than whichever name they are nested under.
    "uv run lup-devtools resolve *",
    # The verbs that drive git rather than read it. `git *` is excluded above
    # and a child of a confined command is confined too, so leaving these
    # inside moves the same failure one call deeper: measured, `git worktree
    # create` cannot take the lock its config write needs while the identical
    # `git config --local` succeeds one call away. Named verb by verb because
    # most of the toolchain reads a repository, and confining that costs
    # nothing at all.
    "uv run lup-devtools git worktree *",
    "uv run lup-devtools git pr *",
    "uv run lup-devtools git conflict *",
    "uv run lup-devtools git hooks *",
    "uv run lup-devtools dev undo *",
]
"""Commands this project runs with no OS boundary beneath them.

Each is a requirement the boundary cannot express any other way, and the
count is the point: an exclusion is not a widened rule but a removed one, so
the list stays as short as the toolchain's actual incompatibilities."""


def served_exclusions(composed: Composition = COMPOSITION) -> list[str]:
    """The exclusions a project holding *composed* has a command for.

    One naming a `lup-devtools` tree a declined module owns excludes nothing
    that project can run, and the compiled policy is a file a session reads:
    it would go on naming a command the CLI does not serve.
    """
    served = composed.subapps()
    return [
        command
        for command in EXCLUDED_COMMANDS
        if all(
            words[0] in served
            for tail in MENTION.findall(command)
            if (
                words := WrittenCommand(
                    file="", line=0, spelled=tail.strip()
                ).command_words()
            )
        )
    ]


ARTIFACT_REFUSAL = "publishing a page puts this work outside the repository"
"""Why an artifact is the wrong reflex here, as the approver of one reads it."""

ARTIFACT_RECOVERY = (
    "Run `uv run lup-devtools dev report` for everything left to implement, or"
    " the report skill to write it whole to a file named for the work, under"
    " tmp/."
)
"""What answers the same need inside the repository.

The redirect is the point rather than the refusal, exactly as the
generated-tree refusal names the source to edit instead of only saying no. A
report that leaves the repository is one nothing in this project can read
back; the report surface is where the same question is answered in a place
every later session, scan, and gate can reach.
"""

WORKTREE_ENTRY_REFUSAL = (
    "entering a worktree this way makes Claude Code refuse ordinary shell words"
    " such as hash, alias and let for the rest of the session"
)

WORKTREE_ENTRY_RECOVERY = (
    "The tool call arms worktree isolation wherever the session is, and it then"
    " refuses eval, source, fc, coproc, trap, enable, mapfile, readarray, hash,"
    " bind, complete, compgen, alias and let in any argv position, even in"
    " read-only commands, so `grep -c hash file.py` stops working."
    " `git worktree create` already made the tree: launch a session rooted in"
    " it, or address its files by absolute path from here."
)
"""Why the tool that moves a session into a worktree is the wrong way in.

The refusal names the cost rather than the rule, because the cost is what is
hard to believe: a gate that has nothing to do with this project refuses
fourteen ordinary shell words, for the rest of the session, on a command that
reads. An agent told only "do not call this" reaches for it the moment the
guidance is out of context; one told what happens does not.

Not walled off. A deliberate use escalates with the marker the shell lattice
already uses, and gets an approval question carrying this reason — which is
the right shape, because a human who knows they are about to lose those words
may still have a reason to.
"""

REFUSED_TOOLS = [
    RefusedTool(tool="Artifact", reason=ARTIFACT_REFUSAL, recovery=ARTIFACT_RECOVERY),
    RefusedTool(
        tool="Skill",
        specifier="artifact-design",
        reason=ARTIFACT_REFUSAL,
        recovery=ARTIFACT_RECOVERY,
    ),
    RefusedTool(
        tool="EnterWorktree",
        reason=WORKTREE_ENTRY_REFUSAL,
        recovery=WORKTREE_ENTRY_RECOVERY,
    ),
]
"""The calls this project has decided against, each naming what to reach for.

Both are Claude Code's spellings, and it is there the reflex they stop exists.
Every runtime consults the table all the same, because which names are worth
refusing is this declaration's answer rather than an adapter's — so a name
Codex does offer would be refused there by writing one line here. Neither is
walled off — a deliberate use escalates with the marker the shell lattice
already uses, and gets an approval question carrying its own stated reason.
"""

HARNESS_SESSION = "harness"
"""The session a natively launched tool server opens for itself.

One name per worktree, shared by every group's process, so the tools of one
native session write where the next one will find them. It names where a
session's notes go and is no identity: what the roster knows a session by is
the launcher's id, or the id its runtime gave the process, which
:mod:`lup.providers.identity` asks the adapter for — never this word, which
every session of one worktree would share."""


def launched_tool_servers(
    withheld: list[str] = WITHHELD_TOOL_GROUPS,
) -> list[ToolServer]:
    """The servers every session this project launches carries.

    Read off the same declaration the in-process and subprocess backends
    assemble from, so a group added there reaches a launched session too.
    Realtime is the relay mode of a persistent run and belongs to no
    interactive session, so its group is not among them; *withheld* are the
    groups a declined module owns.
    """
    started = startup_names(declared_tool_groups())
    return [
        server
        for server in declared_tool_servers()
        if server.name in started and server.name not in withheld
    ]


def launched_serve(root: Path, startup_deadline_seconds: float = 60.0) -> ServeLaunch:
    """How a launched session starts those servers: this project's CLI, in its environment.

    Through ``uv run --directory`` naming the checkout, so a server started
    inside a container or from another directory still resolves this
    project's environment; under :data:`HARNESS_SESSION`, the session every
    group's process of one worktree shares; with this project's needs hook.
    Each server asks for what the launcher exported for it: whichever group
    it serves, it is one process of the launched session, so it answers to
    that session's roster identity and spends that session's recursion
    allowance.

    The deadline is sized to a cold first boot rather than a warm one. Every
    server here starts through ``uv run``, which on a checkout without an
    environment resolves, downloads, and builds one before the process can
    speak — while its siblings, spawned in the same instant, block on the
    same environment lock. A runtime default chosen for an installed server
    (Codex gives ten seconds) drops the losers of that race, and what the
    session sees is two tool groups simply missing on the boot that built
    the environment and present on every boot after.
    """
    return ServeLaunch(
        program=[
            "uv",
            "run",
            "--directory",
            str(root),
            "lup-devtools",
            "tools",
            "serve",
        ],
        session=HARNESS_SESSION,
        needs=session_needs,
        startup_timeout_seconds=startup_deadline_seconds,
    )


def declared_plugin() -> Plugin:
    """The one plugin this project publishes, as generation renders it.

    Its name and marketplace are decided once, here, so a command that has
    to spell either reads the declaration rather than repeating it.
    """
    return portable_harness().plugins[0]


def declared_hook_set() -> HookSet:
    """The hook set this project declares, for a session composed in process.

    Generated plugins read it off the harness they are compiled from. A
    session this program builds itself has to reach the same declaration, or
    it enforces something the generated tree does not.
    """
    return portable_harness().declared_hooks


def declared_test_roots() -> list[TestRoot]:
    """The suites the gate runs: one pytest per installed root, and bun's own.

    Both pytest suites are installed separately — the workspace root and the
    vendored library — so the gate runs pytest once per root rather than
    reporting a green tree that never exercised half of it. The frontend's
    own tests are a third suite, run by bun from the workspace that holds
    them. Declared beside the hook set because the policy reads the list
    too: the files a suite collects carry the test role, derived from here.
    Read where a command runs, since the first root is the working directory.
    """
    return [
        TestRoot(name="pytest", directory=Path.cwd()),
        TestRoot(name="pytest (lup)", directory=Path("packages/lup")),
        BunTestRoot(name="bun test", directory=Path("packages/lup/web")),
    ]


def declared_spread() -> Spread:
    """Which of this repository's trees reach a project built on it, and how.

    lup is two things at once and an adopter receives them by two different
    mechanisms: `packages/lup/` arrives as a dependency, while `src/` and
    `tests/` are stamped out once at initialization and owned from then on.
    Naming both here is what lets `dev reach` say which mechanism carried each
    commit — the only fact about the cost of this scaffold that no file in
    either tree records.

    The generated prefixes are the ones the seam guard already resolves, taken
    from there rather than restated: a tree is generated because a recipe
    writes it, and a second list saying so would be the drift this measures.
    """
    return Spread(
        library=["packages/lup/"],
        copied=["src/", "tests/"],
        generated=application_roots().generated,
    )


def declared_release() -> ReleaseSpec:
    """Which files a release moves here, and what its tag is called.

    The distribution this repository publishes sits under ``packages/``, not
    at the root. The root manifest is ``lup-template``, the scaffold, whose
    version belongs to whoever adopts it and is never a release's to move —
    and ``[tool.lup] agent_version`` beside it is not a release number at all,
    it names the directory a project's traces are kept under. Three version
    numbers, one of which ships, so the one that ships is named.

    A project stamped out of this tree publishes itself from its own root and
    inherits nothing here: the library's default already describes that, and
    this override is a fact about lup's own layout.
    """
    return ReleaseSpec(version_file=f"{VENDORED_ROOT}/pyproject.toml")


def declared_scaffold() -> ScaffoldSource:
    """Where this project's copied half comes from, and what of it it took.

    Inherited rather than written at initialization: a project stamped out of
    this tree receives this declaration with the rest of the copied half, and
    the registration `sync.json` ships names the repository it compiles from
    -- or follows the pin, where the project resolves lup from a repository;
    the roots are the ones the stamp copied.
    What it says of *this* checkout is that this is the scaffold
    itself, which `dev update` refuses on the strength of the template flag
    rather than of anything said here: the origin of every copy has nothing
    upstream to merge from.

    A project that declines part of the scaffold says so here, spelling the
    paths as upstream spells them, and every later update leaves them out
    instead of offering them again.
    """
    return ScaffoldSource(project="lup")


WORKFLOW = WorkflowSpec(
    branches=list(INTEGRATION_BRANCHES),
    frontend=FrontendSpec(workspace="packages/lup/web", bun_version="1.3.14"),
    user_namespaces=True,
)
"""This project's gate: the two-tier model, where `dev` integrates and `main`
carries what has landed, so both deserve a run of their own -- the branches
the shell vocabulary declares as the ones other people build on. The frontend
workspace is the library's, installed first because `dev check` rebuilds the
bundles it compares against what is committed. The contained-launch tests
mount inside a user namespace of their own, so the runner is told to allow one."""


PUBLISH = PublishSpec(package=DISTRIBUTION, tag_prefix=declared_release().tag_prefix)
"""What a release tag publishes here: the library, not the scaffold.

The workspace root is `lup-template`, which nobody installs — so the member
is named, and `uv build` is told which of the two distributions in this
repository is the one that ships. The tag it publishes on is the one the
release writes, read from that declaration rather than spelled twice."""


NATIVE_RUNTIMES: list[NativeSpellings] = [ClaudeSpellings(), CodexSpellings()]
"""Every runtime this project generates a tree for."""


def application_roots(plugin_names: list[str] | None = None) -> ApplicationRoots:
    """Where this project composes concrete native implementations.

    The generated trees are asked of the runtimes rather than written down, so
    a location a runtime learns sanctions its own tree. The rest are this
    project's own homes, derived from where this package actually sits, so
    renaming it during initialization moves them instead of leaving the rule
    pointing at a package that is gone.

    Where it sits is resolved against the declaration enclosing *the package*,
    never against the working directory. The CLI is reached from wherever it
    is invoked — a scratch checkout, a repository whose own manifest will not
    parse — and a root taken from the caller's location makes this package
    relative to a tree it is not under, which raises at import and before
    Typer has a command to fail. That is the whole class of failure the
    documented launcher exists to survive.
    """
    package_path = Path(__file__).resolve().parents[1]
    package_root = declared_project_root(package_path)
    if package_root is None:
        raise ValueError(f"No project declaration encloses {package_path}")
    package = package_path.relative_to(package_root).as_posix()
    harness = f"{package}/harness/"
    plugins = (
        [plugin.name for plugin in portable_harness().plugins]
        if plugin_names is None
        else plugin_names
    )
    generated = [
        *generated_tree_paths(NATIVE_RUNTIMES, plugins),
        # The frontend bundles: compiled by Vite into lup.web's package data
        # and owned by a manifest, so every scan skips them the way it skips
        # the native trees — a minified bundle is nobody's code to audit.
        "packages/lup/src/lup/web/bundles/",
    ]
    return ApplicationRoots(
        generated=generated,
        composition=[
            *generated,
            "tests/",
            "packages/lup/tests/",
            "examples/",
            f"{package}/agent/core.py",
            # Which backends this project runs on is a composition decision
            # like any other: `usage` reads each backend's own account, and
            # the declaration the inherited roster is wired over names them.
            f"{package}/devtools/main.py",
            harness,
            f"{package}/devtools/setup.py",
        ],
        portable_prose=[f"{harness}content/"],
        native_dependencies=["tests/", "packages/lup/tests/", "examples/"],
        source_roots=[f"{Path(package).parent.as_posix()}/"],
    )


def declared_coverage() -> ModuleCoverage:
    """Everything this checkout declares, for the sweep that asks who claims it.

    Every module is built and none is filtered, and the selection travels
    beside them rather than being applied first. The three are separate
    answers and the sweep needs exactly this pairing: a module nobody took
    still owns its declarations, so filtering would hide the failure by
    removing the module that was meant to answer for it — while what this
    project changed about a module is read in both directions, since a skill
    added under a new id is visible only once the selection is applied and a
    skill rewritten under the library's id hides the library's own file once
    it is. Building them all costs this gate the imports the roster reaches,
    and costs nothing anywhere else.

    Only the index is the composition's own. Its subject is what every other
    module contributed, so no module can see enough to declare it; everything
    else under ``docs/`` belongs to the subject it describes. The three pages
    with no module at all — the rule reference, the command reference, the
    generated-path table — are not declaration modules and never reach this
    census: each renders from a registry, the wired CLI, or the compiled trees.
    """
    return ModuleCoverage(
        modules=[entry.build() for entry in entries()],
        selection=MODULE_SELECTION,
        roots=[
            ContentRoot(
                directory=Path("packages/lup/src/lup/harness/content"),
                package="lup.harness.content",
            ),
            ContentRoot(
                directory=Path(LAYOUT.path("harness", "content")),
                package=f"{LAYOUT.package}.harness.content",
            ),
        ],
        composed=[f"{LAYOUT.docs().package}.index"],
        subapps=[*LIBRARY_SUBAPPS, *APPLICATION_ROSTER],
        # Every group this project declares, served or not: one no module
        # claims is served to every project, and only the declaration names it.
        tool_groups=[group.name for group in declared_tool_groups()],
        context=context(project_root()),
        # The guidance `/lup:install` carries into another repository is
        # published only where template-init is taken, and names what the rest
        # of the roster ships — so it is held to what that module stands on.
        beside=[
            Published(
                module=TEMPLATE_INIT.id,
                site=f"template {document.source}",
                document=document,
            )
            for document in [TEMPLATE_CLAUDE, TEMPLATE_CODEX]
        ],
    )


def dev_project() -> DevProject:
    """What this project tells the shared development tooling about itself.

    The package name is derived from where this file actually sits rather
    than written down, so initialization renaming the package moves the
    scans with it instead of leaving them resolving against a name that is
    gone. The roles and the rule selection come from the same hook set the
    generated trees enforce, so a scan and a hook cannot disagree about what
    a path is for, nor about which rules are live here.

    The other two selections come from the modules that apply them rather than
    being restated, so what the gate reports as retired is what the CLI and the
    plugin actually decline.
    """
    hooks = declared_hook_set()
    package = Path(__file__).resolve().parents[1].name
    return DevProject(
        package=package,
        # What the preservation gate judges is what an adopter could have
        # imported, and two subtrees here are reachable by name without
        # anybody being able to hold one.
        internal_modules=[
            # The scaffold. `dev init` copies this half into the adopting
            # repository and renames it, so its names never reach anybody as
            # an import: they arrive as the adopter's own source, under the
            # adopter's own package, theirs to edit. An instruction about one
            # would say to repoint an import nobody was able to write.
            package,
            # The permission kernel, compiled into the hermetic dispatcher
            # every generated plugin runs. The generated copy is already left
            # out of the walk; this is the source it is projected from, and
            # the dispatcher reaches it as bare `kernel.*` rather than through
            # this package at all. What went from it is a hand-rolled shell
            # tokenizer and the control-flow readers beside it, replaced in
            # place, which is the freedom not publishing them is for.
            "lup.policy.kernel",
        ],
        roots=application_roots(),
        rules=hooks.rules,
        import_boundaries=hooks.import_boundaries,
        subapps=SUBAPP_SELECTION,
        # lup: template: which trackers beyond this checkout this project may
        # report to. What is here is lup's own, and an adopted scaffold
        # inheriting it is the point rather than a leak: a project built on
        # lup meets most of its friction in lup's machinery — the resolver,
        # the permission policy, the sandbox — none of which is editable from
        # the consuming tree, and a report filed against the consuming
        # repository becomes evidence for a run that will plan a repair it
        # cannot make. A project that outgrows this replaces the entry; one
        # that owns everything it runs empties the list, and `dev tracker`
        # then reaches nowhere but here.
        trackers=library_trackers(project_root(), declared_scaffold().project),
        modules=MODULE_SELECTION,
        coverage=declared_coverage(),
        path_roles=declared_role_rows(list(hooks.path_roles)),
        # This file: what this repository settled about itself is written
        # here, so `dev seams` reads and edits it rather than looking
        # somewhere a library guessed at.
        catalog=Path(__file__).resolve().relative_to(project_root()),
        # The library's seams plus this repository's own. What the image
        # carries is settled where the image is composed rather than here,
        # because that is the call it is a keyword of — so the seam names the
        # module, and the library's list stays free of this layout.
        seams=[
            # The library's seams, less the rule selection, which this project
            # declares beside the guidance that teaches it rather than here.
            *[seam for seam in DECLARED_SEAMS if seam.keyword != "retired"],
            Seam(
                call="RuleSelection",
                keyword="retired",
                summary="library scan rules this project does not hold itself to",
                module=Path(LAYOUT.path("harness", "content", "catalog.py")),
            ),
            Seam(
                call="Image",
                keyword="tooling",
                summary="programs this project's work needs inside the image",
                module=Path(LAYOUT.path("harness", "content", "image.py")),
            ),
        ],
    )


def portable_harness(
    version: str = "0.2.0",
    root: Path | None = None,
    composed: Composition = COMPOSITION,
) -> Harness:
    """Build the canonical declaration graph consumed by every adapter.

    Deliberately one declaration, not one per platform: every intended
    Claude/Codex difference is a rendering decision in the adapters
    (``compile_claude`` / ``compile_codex``) or a support artifact in the
    generation recipes, mapped in ``docs/platform-differentiation.md``.
    Per-platform declarations overriding a shared default were rejected
    because they would let semantic content fork silently.

    *composed* is this repository's roster unless a caller builds what another
    selection would compose — every surface that follows from the modules is
    read off it, and nothing else here depends on them.
    """
    content = composed.content()
    plugin_name = "lup"
    plugin = Plugin(
        id="plugin.lup",
        name=plugin_name,
        marketplace=f"{read_project_name(root or project_root())}-repository",
        version=version,
        description=(
            "Self-improvement harness with feedback, review, and safe resolution flows"
        ),
        skills=content.skills,
        agents=content.agents,
        hooks=HookSet(
            id="hooks.lup-policy",
            policy_ids=["fetch", "shell", "edit", "unknown-tool"],
            # Derived from the scaffold this project declares rather than
            # spelled again: the branch a session is told about at prompt time
            # is the branch `dev update` merges, and two spellings of it are
            # how a fold ends up watching a branch nothing advances.
            carriers=CarrierPins(
                branch=declared_scaffold().branch, distribution=DISTRIBUTION
            ),
            # The one selection, declared where the guidance reads it too, so
            # the hooks enforcing a rule and the section teaching it cannot
            # disagree; `dev seams --retire` edits it there.
            rules=RULES,
            import_boundaries=native_import_boundaries(
                application_roots([plugin_name])
            ),
            allowed_fetch=[
                HookUrlScope(origin=AnyHttpUrl("https://docs.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("http://docs.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("https://code.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("http://code.claude.com")),
                # docs.claude.com now redirects the Agent SDK and API paths
                # here, so the route the guidance prescribes leaves the
                # declared scopes one hop in.
                HookUrlScope(origin=AnyHttpUrl("https://platform.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("http://platform.claude.com")),
                # docs.anthropic.com 301s both of those routes onward: the
                # Claude Code paths to code.claude.com and the API paths to
                # platform.claude.com, each declared above. Admitting the
                # legacy host admits the origin a redirect starts at, not a
                # document these scopes did not already carry.
                HookUrlScope(origin=AnyHttpUrl("https://docs.anthropic.com")),
                HookUrlScope(origin=AnyHttpUrl("http://docs.anthropic.com")),
                # The product's own pages — what it is, what it costs, what it
                # claims — which no reference manual answers and which a
                # question about the product rather than the API lands on.
                # Declared for what they are rather than as a redirect: this
                # admits an origin these scopes did not already reach, and
                # widens the egress the same table grants to it.
                HookUrlScope(origin=AnyHttpUrl("https://claude.com")),
                HookUrlScope(origin=AnyHttpUrl("https://www.claude.com")),
                # Where a session publishes a settled classification for a
                # later one to read back, so a briefing can cite the artifact
                # rather than restate it.
                HookUrlScope(origin=AnyHttpUrl("https://claude.ai")),
                HookUrlScope(origin=AnyHttpUrl("https://ai.pydantic.dev")),
                HookUrlScope(origin=AnyHttpUrl("http://ai.pydantic.dev")),
                HookUrlScope(origin=AnyHttpUrl("https://learn.chatgpt.com")),
                HookUrlScope(origin=AnyHttpUrl("http://learn.chatgpt.com")),
                HookUrlScope(origin=AnyHttpUrl("https://developers.openai.com")),
                HookUrlScope(origin=AnyHttpUrl("http://developers.openai.com")),
                HookUrlScope(origin=AnyHttpUrl("https://github.com")),
                HookUrlScope(origin=AnyHttpUrl("https://api.github.com")),
                HookUrlScope(
                    origin=AnyHttpUrl("https://githubusercontent.com"),
                    include_subdomains=True,
                ),
                HookUrlScope(origin=AnyHttpUrl("https://pypi.org")),
                HookUrlScope(origin=AnyHttpUrl("https://files.pythonhosted.org")),
                # This machine's own services: the setup dashboard, the
                # resolver supervisor, and whatever a session is running to
                # look at. Reaching one is how a session establishes that it
                # came up at all, and asking for that is asking about a
                # process the same session just started. No port is named
                # because every one of these surfaces takes `--port`, and a
                # scope that went stale on a flag would put the question back.
                *(
                    HookUrlScope(
                        origin=AnyHttpUrl(f"http://{host}"),
                        any_port=True,
                        reason="this machine's own pages, on whatever port they took",
                    )
                    for host in ("127.0.0.1", "localhost")
                ),
            ],
            # An origin outside those is handed to the runtime's own
            # permission system rather than asked about here, by every route
            # that reads one: a web fetch, `curl` and `wget` alike.
            unscoped_fetch="defer",
            # lup: template: which trees this domain will not let an agent edit
            # without a question. What is here answers for a framework that
            # generates its own plugin trees and carries its own policy; a
            # domain whose sensitive files are a data directory, a migration
            # set or a deployment manifest says so instead.
            protected_edit_roots=[
                # Both runtimes' trees, because one of them being protected
                # and the other open is a hole with no reason behind it: the
                # settings, trust state and hand-written skills under each
                # decide the same things about the session that reads them.
                Path(".claude"),
                Path(".codex"),
                # Every manifest and lockfile, in whichever package holds it:
                # what an install fetches and runs is declared there, and the
                # commands that write them for a reason are judged by the
                # dependency rows rather than by a path.
                *dependency_declarations(),
                # CI runs with the repository's secrets and on every push, so
                # a workflow or an action is code somebody else executes.
                Path(".github"),
                # And the same by other hands, later and outside the session:
                # an editor's tasks and launch configurations, a container
                # recipe, and the hooks `git commit` runs. None of them has
                # this policy in front of it when it runs.
                Path(".vscode"),
                Path(".devcontainer"),
                Path(".pre-commit-config.yaml"),
                Path("sync.json"),
                # The gitignored half alongside it, because a registration
                # there can now carry a `mount` — and that key is what a
                # session may open, at which mode, wherever the project sits
                # on this machine. An agent free to write one would be
                # choosing what its next launch mounts, which is the confined
                # thing choosing what confines it: the same reason remotes,
                # identity and the forge credential are resolved on the host.
                # Gitignored is not a substitute. The gate is who may write
                # it, and nothing was asking.
                Path("sync.json.local"),
                Path(".lup/preflight"),
                Path(".lup/policy-snapshots"),
                # The review queue, and the claims that spend an answer once.
                # A hook parks a question here and releases the retry an
                # approved row names, writing both from its own process; the
                # session's own call writing either is the requester recording
                # its own answer, or putting a spent approval back.
                Path(".lup/questions.jsonl"),
                Path(".lup/review-claims"),
                Path(".lup/review-stage-claims"),
                # What the agent is allowed to do at all is declared here, and
                # an agent that can widen its own policy without a question
                # has a preference rather than a boundary. Protected so the
                # widening is the thing approved: the agent writes the change,
                # the diff is in front of whoever answers, and it is durable —
                # a declaration appears in a review, is drift-checked, and
                # holds for the next session, where a per-call escape helps
                # once and evaporates.
                Path("packages/lup/src/lup/policy"),
                Path(LAYOUT.path("harness", "catalog.py")),
                # Which of those rules apply is the same widening by another
                # name: retiring a scan rule, or judging a command differently,
                # is decided in these, and the next generation compiles it into
                # the hooks as surely as an edit of the policy would.
                Path(LAYOUT.path("harness", "content", "catalog.py")),
                Path(LAYOUT.path("harness", "content", "shell_vocabulary.py")),
                Path("packages/lup/src/lup/harness/codescan"),
            ],
            # lup: template: what each tree in this domain is *for*. A role is
            # how a gate tells a fixture from production and a build product
            # from work — so a domain with a data directory, a notebook tree or
            # a generated client says so here, and every gate reads it at once.
            path_roles=[
                HookPathRole(root=Path("tests"), role="test"),
                HookPathRole(root=Path("packages/lup/tests"), role="test"),
                # Scratch is "disposable by construction", and a build product
                # qualifies as squarely as a scratchpad does: every one of
                # these is reproduced by a command, so destroying one costs
                # the command rather than any information. Leaving them
                # production made `rm` and `cp` ask about caches and virtual
                # environments, which is an approval that teaches nobody
                # anything.
                HookPathRole(root=Path(".venv"), role="scratch"),
                HookPathRole(root=Path("build"), role="scratch"),
                HookPathRole(root=Path("dist"), role="scratch"),
                HookPathRole(root=Path("htmlcov"), role="scratch"),
                # A build product appears beside whatever produced it, so
                # these name a shape rather than a place: `__pycache__` alone
                # stands in thirty-odd directories here, and a declared
                # `.pytest_cache` matched the top-level one while the sibling
                # under `packages/lup/` stayed production. `tmp` is the same
                # claim — a package opens its own beside itself, and the
                # protected-path table already reads the name that way, so
                # anchoring the role at the top left the two halves
                # disagreeing about which directories the word covers.
                # `build`, `dist`, and `htmlcov` keep their single roots — a
                # nested directory by those names is plausibly somebody's
                # source.
                HookPathRole(root=Path("**/tmp"), role="scratch"),
                HookPathRole(root=Path("**/__pycache__"), role="scratch"),
                HookPathRole(root=Path("**/*.egg-info"), role="scratch"),
                HookPathRole(root=Path("**/.ruff_cache"), role="scratch"),
                HookPathRole(root=Path("**/.pytest_cache"), role="scratch"),
                HookPathRole(root=Path("**/node_modules"), role="scratch"),
                # The frontend bundles Vite builds into lup.web's package data
                # are a build product too — reproduced by `harness generate
                # all` — and committed only so the wheel carries them. Scratch
                # keeps the source audits off a minified bundle; what keeps a
                # hand from editing one is the ownership manifest and the
                # drift check, which read it as a generated tree.
                HookPathRole(
                    root=Path("packages/lup/src/lup/web/bundles"), role="scratch"
                ),
                # A pending migration is a declaration a break's own commit
                # writes, one TOML file each: data read by `dev migrate` and
                # the release, not source a whole-file or size gate reviews
                # for how it reads. Data rather than scratch, because it is
                # the only copy of what an adopter is told to do. Pending
                # only: a released record is `dev release`'s to write.
                HookPathRole(
                    root=Path("packages/lup/src/lup/migrations/pending"), role="data"
                ),
                # What each suite the gate runs collects is a test by
                # derivation rather than by a second table: bun collects
                # `*.test.ts` beside its source, where no directory root
                # could name it, and a file the gate runs as a test that the
                # policy budgets as source is the disagreement deriving one
                # from the other rules out. After the scratch rows, so a test
                # under `node_modules` stays scratch.
                *collected_test_roles(declared_test_roots()),
                # Deliberately absent, though Git ignores every one of them:
                # `.env.local`, `notes/`, `.lup/`, and the `*.local` configs
                # each hold the only copy of what is in them. Ignored means
                # untracked, which is not the same claim as disposable, and
                # a role is the place that claim gets made explicitly.
            ],
            # lup: template: who owns README.md, and anything else this domain
            # wants proposed rather than written. A human-owned file surfaces
            # every change as an approval and the agent does not write it —
            # which is right for a scaffold whose README describes the scaffold,
            # and often wrong for a domain whose README is the one file it most
            # wants written for it. `dev seams --disown README.md` is the answer
            # to that, and it edits this line rather than asking anyone to.
            human_owned_files=[Path("README.md")],
            # Sessions here coordinate, so a native call that would reach one
            # of them is judged against the roster it would otherwise bypass.
            # Built from the store's own layout rather than spelled: renaming
            # the coordination directory moves the compiled hook with it.
            peer_policy=peer_policy(),
            refused_tools=REFUSED_TOOLS,
            # The library's key and login files, and the logins of the two
            # runtimes this project runs on, each spelled by its own login
            # declaration: a session reads neither its own token nor the
            # other runtime's. And the directory this image binds session
            # wake sockets in, read off the image rather than spelled, so a
            # peer is reached through the roster rather than a raw frame.
            #
            # The one declaration every reader is refused by: the shell's
            # words on both runtimes, and on Claude the `Read` deny rules the
            # file tools obey, which the runtime merges into its sandbox's
            # read restrictions too. Defense in depth once a contained session
            # is lent an ssh identity, and honest about what that is worth: it
            # stops an agent *reading* key material, not `ssh` and `git`
            # *using* it -- `ssh git@github.com` names no credential path.
            # `docs/permissions.md` states the grant in those words.
            refused_paths=[
                credential_files(
                    also=[
                        *CLAUDE_LOGIN.withheld_logins(),
                        *CODEX_LOGIN.withheld_logins(),
                    ]
                ),
                *wake_socket_refusal(agent_image().wake_sockets.directory),
            ],
            # Which checker answers for an edit is this project's toolchain,
            # not the library's, and it is named rather than located: the
            # resolution asks the checkout's own environment where the program
            # is, so a worktree still runs its own copy without this having to
            # know whether that environment is `.venv`, somewhere
            # `UV_PROJECT_ENVIRONMENT` put it, or a conda or pyenv install
            # reached through PATH. Spelling a path here would answer only for
            # the layout it spelled, and gate every other one in silence.
            diagnostics_command=["pyright", "--outputjson"],
            resolution_command=["lup-devtools", "dev", "refutations"],
            # The audit's own sweep, narrowed to the file that was just
            # written. Declared rather than left empty because the edit gate
            # does not name a dead directive in the prompt: what goes
            # unmentioned has to go, or it stays in the tree unread.
            repair_command=[
                "lup-devtools",
                "dev",
                "check",
                "--antipatterns",
                "--fix",
                "--json",
            ],
            shell_rules=SHELL_RULES,
            # This project's toolchain: what `uv run <target>` may reach here
            # without a question, which is nothing any other project inherits.
            # The group places `lup-devtools` outside the sandbox, because
            # every command of it that opens an agent session is unusable
            # confined.
            runner_targets=RUNNER_TARGETS,
            # What this table has to keep allowing, swept by `dev check`
            # against the table above. Every other measurement of the
            # vocabulary reads the direction a tightening never shows up in.
            everyday_commands=EVERYDAY_COMMANDS,
            # lup: template: what this domain's sessions cannot run without,
            # and what they merely lose. Each names the manifest handle that
            # measures it, so a capability declared here without a probe there
            # is reported absent rather than assumed present.
            boundary_capabilities=[
                # Containment is what this project's placements mean. A
                # session that could not observe it would still say `inside`
                # about every operation, and be wrong for all of them.
                depends_on(
                    "inside_placement",
                    "inside placement",
                    contained_only=True,
                    reason="every placement this policy states is read against it",
                ),
                # The order refuses an ask nobody can record, so an unwritable
                # queue is a session where each reviewed operation fails
                # naming the operation — which was never the problem.
                depends_on(
                    "question_relay",
                    "question relay",
                    reason="every final ask is written here before anybody sees it",
                ),
                # Costly rather than fatal: without it, destructive local work
                # asks instead of being allowed against a capture. That is the
                # safe direction and worth opening for.
                depends_on(
                    "checkpoint_store",
                    "checkpoint store",
                    required=False,
                    reason="recovery-backed permission rests on a proven capture",
                ),
                # Declared with no probe on purpose. `hostexec` holds the
                # contract and no transport carries a request yet, so `outside`
                # is capability-blocked in every profile — the correct
                # fail-closed answer, and not a working feature. Omitting it
                # would say this project does not depend on it, which is what
                # would quietly permit the operations no channel can carry.
                depends_on(
                    "host_executor",
                    required=False,
                    reason="no transport carries a request to the host yet",
                ),
            ],
            sandbox=HookSandbox(
                extra_domains=["api.anthropic.com"],
                # Every command in this project reaches its toolchain through
                # `uv`, which locks its cache whenever it resolves dependencies
                # — which a changed pyproject.toml forces, and an integration
                # merge is what changes pyproject.toml.
                #
                # `/tmp` is where a session puts what it is not keeping: a
                # command's captured output, a scratch script, the scratchpad
                # its runtime hands it. It is POSIX's own disposable root and
                # holds nothing this policy protects, so it is granted for
                # every machine rather than met one redirect at a time — and
                # undeclared it refuses the write while the classifier allows
                # it, which reads as a broken command rather than a boundary.
                writable_paths=["~/.cache/uv", "/tmp"],
                excluded_commands=served_exclusions(composed),
            ),
        ),
    )
    return Harness(
        generator_version=version,
        source_evidence={"content": "typed-python"},
        requirements=composed.requirements(manifest()),
        image=agent_image(),
        plugins=[plugin],
        guidance=guidance.document(composed.guidance()),
        # A project that declined the resolver module has no worker, review
        # or merge skill for a spec to name, so it declares none.
        resolver=(
            ResolveSpec(
                id="resolver.lup",
                worker_identity="resolver-worker",
                worker_skill=SkillInvocation(plugin="lup", skill="implementer"),
                review_skill=SkillInvocation(plugin="lup", skill="resolve-reviewer"),
                merge_skill=SkillInvocation(plugin="lup", skill="merge"),
            )
            if RESOLVER.id in composed.taken()
            else None
        ),
    )
