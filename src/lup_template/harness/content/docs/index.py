# lup: ignore[constant-declaration]
# Both constants here are blocks of this index's own prose, and the index is
# the document rather than a value some caller passes into one.
"""The documentation index: what this repository is, and where each part is.

The component table and the closing note are this repository's own words. The
page tables are built from the documents each half actually declares, so a
page that is renamed or added on either side of the library boundary carries
its own row rather than waiting for someone to notice a dead link.
"""

import lup.harness.models as models
from lup.harness.content.docs.index import (
    IndexEntry,
    IndexGroup,
    document_index,
    page_index,
)

PREAMBLE = r"""# Lup documentation

Lup is three components in one repository. Start with the one you are actually
touching.

| Component | What it is | Guide |
| --- | --- | --- |
| `packages/lup` | The reusable, provider-neutral library. Session and turn engine, harness compiler, permission kernel, resolver, code scanner. Published standalone; never imports the application. | [library.md](library.md) |
| `src/lup_template` | The application template. The agent you customize, the `lup-devtools` CLI, and the environment that runs a session. Built on the library. | [template.md](template.md) |
| `.claude/`, `.codex/`, `.agents/`, `AGENTS.md` | The harness — the native plugin trees. Compiler output from typed Python, committed so a checkout is launchable with no build step. Carries the roster of every skill and agent the plugin ships. | [harness.md](harness.md) |

"""

EPILOGUE = r"""## Every page here is generated

Files under `docs/` are compiler output from typed Python — the pages about
the library from `packages/lup/src/lup/harness/content/docs/`, the
pages about this repository from
`src/lup_template/harness/content/docs/` — the same way the native
trees are. Each opens with a banner naming its source module. Edit the module
and regenerate; a hand-edit is preserved and reported as a conflict rather
than silently overwritten. [harness.md](harness.md) is the whole story.
"""

RULES_REFERENCE = IndexEntry(
    link="rules.md",
    answers=(
        "Every executable Lup rule, its matching shape, its diagnostic, and "
        "the module that enforces it."
    ),
)
"""The one page with no declaring module: it renders from the rule registry."""

COMMAND_REFERENCE = IndexEntry(
    link="commands.md",
    answers=(
        "Every command `lup-devtools` serves, walked from the composed CLI "
        "rather than listed by hand."
    ),
)
"""Also declared by no module: it renders from the wired app at generation."""

GENERATED_PATHS = IndexEntry(
    link="generated-paths.md",
    answers=(
        "Every file the recipes compile and what each is compiled from, "
        "walked from the trees themselves rather than listed by hand."
    ),
)
"""The third page no module declares: it renders from the compiled trees."""

WORK_STATUS = IndexEntry(
    link="work-status.md",
    answers=(
        "What this repository's coordination ledger still holds to do — the "
        "open tasks, what waits on a person, the open handoffs — generated "
        "from the ledger by `ledger writeup`."
    ),
)
"""Declared by `lup_template.writeups` and written from this machine's log.

Listed only where the ledger module is taken, because only there is it
written: a writeup is `ledger writeup`'s output, and a project without the
ledger has no log to render one from."""


def document(
    pages: list[models.Document], taken: list[str] | None = None
) -> models.PromptDocument:
    """Compose the index over the pages the adopted modules published.

    A row is looked up by the identity ownership already records a page under,
    and a lookup that finds nothing renders nothing. That is the whole of what
    modules decide here: a page is absent because its subject is a module this
    project declined, so the index says less rather than linking to a file
    generation was never asked to write.

    Three rows name no page at all, because three pages are declared by no
    module — they render from the rule registry, the wired CLI, and the
    compiled trees, none of which is a subject a project can decline. The
    fourth such row is the ledger's writeup, listed where *taken* — every
    module unless a caller says otherwise — holds the ledger.
    """
    page = page_index(pages)
    return document_index(
        preamble=[models.TextPart(text=PREAMBLE)],
        groups=[
            IndexGroup(
                title="Reference",
                blurb=(
                    "Subjects that span the three components, or that are "
                    "large enough to own a page."
                ),
                entries=[
                    *page.rows(
                        "docs.architecture",
                        "Why the seams are where they are: one capability per "
                        "ABC, adapters at the edge, structured output with one "
                        "mechanism.",
                    ),
                    *page.rows(
                        "docs.patterns",
                        "The recurring code shapes: declaration-plus-renderer, "
                        "closed-by-construction, the typed-matcher router, and "
                        "the engine-versus-surface split.",
                    ),
                    *page.rows(
                        "docs.orchestration",
                        "The delegation catalog: subagent, nested, background, "
                        "and deferred tools, and when to reach for each.",
                    ),
                    *page.rows(
                        "docs.permissions",
                        "How a shell command, fetch, or edit becomes allow, "
                        "ask, defer, or deny — and how the generated hooks "
                        "decide identically without importing the library.",
                    ),
                    RULES_REFERENCE,
                    *page.rows(
                        "docs.resolver",
                        "How reviewed feedback becomes concerns, worktrees, "
                        "workers, and an accepted integration branch.",
                    ),
                    *page.rows(
                        "docs.supervisor",
                        "The local page that watches a resolver run and "
                        "answers its questions.",
                    ),
                    *page.rows(
                        "docs.platform-differentiation",
                        "Every intended Claude/Codex difference, and the "
                        "parity decision for each generated artifact family.",
                    ),
                    *page.rows(
                        "docs.native-capabilities",
                        "The evidence ledger: which native contracts are "
                        "proven, at which versions, and the release gaps.",
                    ),
                    *page.rows(
                        "docs.self-improvement",
                        "How to turn an observed agent failure into a durable "
                        "capability change.",
                    ),
                ],
            ),
            IndexGroup(
                title="Working in this repository",
                entries=[
                    *page.rows(
                        "docs.contributing",
                        "How to get set up, where a change of each kind "
                        "belongs, and what has to be green before it lands.",
                    ),
                    *page.rows(
                        "docs.conventions",
                        "The lookup behind each code-convention rule: which "
                        "library, which typed stand-in for a dict, which "
                        "parser, which resolver tool.",
                    ),
                    COMMAND_REFERENCE,
                    GENERATED_PATHS,
                    *([WORK_STATUS] if taken is None or "ledger" in taken else []),
                    *page.rows(
                        "docs.quality-pipeline",
                        "The three check layers, and what each one uniquely catches.",
                    ),
                    *page.rows(
                        "docs.dev-tooling-decisions",
                        "The architectural decisions behind the development "
                        "tooling, each stated against the current system.",
                    ),
                    *page.rows(
                        "docs.corpus",
                        "The corpus scaffold an adopter may declare — claims, "
                        "evidence, questions and corrections over the ledger, and "
                        "how each stands; this repository declares none of it.",
                    ),
                ],
            ),
        ],
        epilogue=[models.TextPart(text=EPILOGUE)],
    )
