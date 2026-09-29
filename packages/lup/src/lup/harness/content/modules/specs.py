"""What every module lup ships is called, and what it is for.

Separate from the modules themselves, and importing nothing but the type, so
that listing the roster costs nothing. A module file imports its own skills,
its own documents, and whatever optional extra its subject needs; a catalog
that reached into those files to learn a module's name would make the listing
depend on every dependency the listed modules have — which is the cost the
whole arrangement exists to avoid.

So this is the half a reader reaches: ``dev modules`` renders it, generation
resolves requirements against it, and only a module somebody took is ever
built. The other half is :mod:`lup.harness.content.modules.catalog`, which
pairs each of these with a builder that imports its subject when called.

**A default is advice to an adopter, not a description of this repository.**
Four of these are off, and this repository takes all four — it is the
demonstration of its own machinery, so it exercises what it ships. What it
does *not* do is load their prose: adopting a module for its code is a
different question from carrying its paragraph in every session, which is why
``loads_guidance`` is a separate answer. lup builds no realtime app, so it
takes ``realtime`` and declines the section teaching how to write one.
"""

from lup.harness.modules import ModuleSpec

CORE = ModuleSpec(
    id="core",
    title="Core",
    summary=(
        "Reading a codebase, designing a change, reporting what is left, "
        "debugging, and querying the permission policy — with the gate, the "
        "generator and the git machinery every other module stands on."
    ),
    default_on=True,
    essential=True,
    # The debug skill reads a session's trace, which is the one command tree
    # it needs and does not own.
    requires=["observability"],
    subapps=["dev", "harness", "git", "tools", "review"],
    tool_groups=["codeintel"],
)

GIT_WORKFLOW = ModuleSpec(
    id="git-workflow",
    title="Git workflow",
    summary=(
        "Committing, rebasing, merging, and landing a branch, and the pages "
        "that say what has to be green before one does."
    ),
    default_on=True,
)

META = ModuleSpec(
    id="meta",
    title="Meta",
    summary=(
        "Changing the machinery rather than the product: authoring the "
        "harness a session runs under, and the walks that move code without "
        "losing it."
    ),
    default_on=True,
)

RESOLVER = ModuleSpec(
    id="resolver",
    title="Resolver",
    summary=(
        "Reviewed feedback becoming concerns, worktrees, workers, and an "
        "accepted integration branch, with the supervisor watching it."
    ),
    default_on=True,
    requires=["git-workflow"],
    subapps=["resolve"],
    # Each actor a run opens gets a container of its own unless the project
    # declared its actors uncontained, and a host with no engine is refused.
    requirements=["container runtime"],
)

VERSION = ModuleSpec(
    id="version",
    title="Version",
    summary=(
        "Deciding and making a version change, with the two agents that "
        "gather the evidence and review the proposal independently."
    ),
    default_on=True,
    # The reviewer reads the traces of the version under review.
    requires=["observability"],
    subapps=["version"],
)

OBSERVABILITY = ModuleSpec(
    id="observability",
    title="Observability",
    summary=(
        "What a session left behind: its trace, and the archive a worktree's "
        "records are kept in once the worktree is gone."
    ),
    default_on=True,
    subapps=["trace"],
)

SANDBOX = ModuleSpec(
    id="sandbox",
    title="Sandbox",
    summary=(
        "The confined execution a session is offered, and the policy deciding "
        "what may leave it."
    ),
    default_on=True,
    tool_groups=["sandbox"],
    # The code a session evaluates runs in a container, so Docker or Podman is
    # this module's cost: a project declining it is asked for neither.
    requirements=["container runtime", "Python sandbox"],
)

DASHBOARD = ModuleSpec(
    id="dashboard",
    title="Dashboard",
    summary=(
        "One page per person over every session's parked reviews, in every "
        "repository, with each repository's setup beside them — a service "
        "every launch holds and the last session's end stops."
    ),
    default_on=True,
    subapps=["dashboard"],
)

SETUP = ModuleSpec(
    id="setup",
    title="Setup",
    summary=(
        "Interactive configuration of keys, integrations, and profiles — the "
        "wizard, and the page that is the same thing in the dashboard."
    ),
    default_on=True,
    subapps=["setup"],
)

CONVERSATION = ModuleSpec(
    id="conversation",
    title="Conversation",
    summary=(
        "Retaining authenticated AI conversations for later reading, and the "
        "skill that answers from one."
    ),
    default_on=True,
    # Retention drives a browser the operator signed in through `conversation
    # setup`, its own tree, so the module needs no other.
    subapps=["conversation"],
)

FEEDBACK_LOOP = ModuleSpec(
    id="feedback-loop",
    title="Feedback loop",
    summary=(
        "Turning an observed agent failure into a durable capability change: "
        "the fb- phases, the review pass, and the trace explorer."
    ),
    # Every phase reads traces, and the status phase reads them per version.
    requires=["observability", "version"],
    subapps=["feedback"],
)

RUNS = ModuleSpec(
    id="runs",
    title="Runs",
    summary=(
        "Work that outlives its tool call, declared as a resumable pipeline "
        "rather than scripted, and watchable while it runs."
    ),
    default_on=True,
    subapps=["run"],
)
"""On, unlike the other three that carry no skills.

Its section is the argument. Nothing fires when a long-running job is scripted
rather than declared — the failure is a job nobody can resume, discovered when
somebody needs to resume it — so the rule has to reach a session before it
launches anything, and 401 bytes is what that costs. The subjects that are off
either cost a great deal more or announce themselves when wanted.
"""

LEDGER = ModuleSpec(
    id="ledger",
    title="Ledger",
    summary=(
        "One append-only log per repository, each kind committed with the code "
        "or kept outside every worktree, holding typed nodes whose standing is "
        "read rather than stored."
    ),
    default_on=True,
    subapps=["ledger"],
    tool_groups=["ledger"],
)
"""On, because what it answers is a failure nobody sees happen.

Work that outlives its session has to live somewhere a later session finds.
Prose rots because nothing checks it, a per-branch file forks because every
worktree holds one, and a stored status keeps its label after the support for
it goes away — none of which fails loudly. What it costs a repository that
records nothing is an empty store and a command nobody runs; what it saves is
a claim that quietly stopped being true.

It declares no node type and no edge type, so adopting it is adopting the
mechanism. What a claim owes is the project's question: the scaffold answers
it with a corpus declared beside its tasks, and a project that wants an answer
of its own declares its own kinds the same way.
"""

COORDINATION = ModuleSpec(
    id="coordination",
    title="Coordination",
    summary=(
        "Sessions that already exist finding each other: one roster per "
        "repository, mail that outlives the process that sent it, and the "
        "person on that roster as a peer like any other."
    ),
    default_on=True,
    requires=["ledger"],
    subapps=["coordination"],
    tool_groups=["coordination"],
)
"""On, because the failure it answers is silent and is paid in whole runs.

Work that cannot see itself does not fail loudly: two sessions crawl the same
thing, one re-derives what another settled an hour ago, and a question that
needed a person is asked of nobody. Nothing fires, and what it costs is work
already done. A repository whose sessions never overlap carries a roster with
one member on it and pays a paragraph for the privilege, which is the cheap
side of the asymmetry.
"""

REALTIME = ModuleSpec(
    id="realtime",
    title="Realtime",
    summary=(
        "Persistent agents that control their own attention: the sleep/wake "
        "loop, and the relay that spells it for subprocess backends."
    ),
    tool_groups=["session"],
)

REFLECTION = ModuleSpec(
    id="reflection",
    title="Reflection",
    summary=(
        "The gate an agent meets on its own output: an independent reviewer "
        "between finishing the work and submitting it."
    ),
    tool_groups=["notes"],
)

# lup: ignore[library-default] — the modules this library authors, so the
# roster is what it ships rather than a choice made for an adopter
LIBRARY_SPECS = [
    CORE,
    GIT_WORKFLOW,
    META,
    RESOLVER,
    VERSION,
    OBSERVABILITY,
    SANDBOX,
    DASHBOARD,
    SETUP,
    CONVERSATION,
    FEEDBACK_LOOP,
    RUNS,
    LEDGER,
    COORDINATION,
    REALTIME,
    REFLECTION,
]
"""Every module lup ships, in the order a composition lays them out.

The order is the document's, not an alphabet's: guidance renders as the
chapter spine crossed with this roster, so where a module sits here is where
its prose sits inside whichever chapter its sections named.
"""
