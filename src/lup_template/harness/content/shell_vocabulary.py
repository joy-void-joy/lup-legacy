"""Where this project's shell vocabulary differs from the one lup offers.

The rule models, :func:`~lup.policy.shell_rules.erase_shell_rules`, and the
groups in :mod:`lup.policy.vocabulary` are library mechanism; what is *this
project's* is the selection below — the commands it judges differently and the
one rule no other project has. A downstream project writes its own selection
and never edits lup to change a verdict.

Stated as differences rather than as a table, the way a project states which
anti-patterns it retires: :func:`~lup.policy.vocabulary.default_vocabulary` is
what a selection layers over, so adding one command costs one entry instead of
a copy of every command the library already judged.

Two judgements here differ from the library's offered defaults, and both are
arguments rather than a fork:

``integration_branches=INTEGRATION_BRANCHES`` — this repository integrates on
``dev`` and lands on ``main``, so a forced push asks about both even under a
lease, where the library's default names only the forge's default branch.

``redirect_checkout=True`` — this repository has settled on ``git switch``
and ``git restore``, so ``checkout`` denies and names them instead of asking.
"""

from lup.policy.everyday import CommandFamily, everyday_commands
from lup.policy.kernel.effects import declare
from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    ShellOperationRule,
    ShellSubcommandRule,
)
from lup.policy.vocabulary import (
    bun_rule,
    devtools_rules,
    git_rule,
    runner_target_rules,
    typescript_rule,
)
from lup.seams import Selection

# lup: ignore[constant-declaration] — the branches this repository integrates
# and lands on, an identity it defines
INTEGRATION_BRANCHES = ("main", "dev")
"""The branches other people build on here: `dev` integrates, `main` lands.

A forced push onto either asks even under `--force-with-lease`, and each is
a branch the gate runs on when it is pushed to, so the workflow reads its
branches from here rather than keeping a second list in step.
"""


def lup_devtools_rule() -> ShellCommandRule:
    """Admit this toolchain reached without `uv`, only where it has to be.

    `uv run lup-devtools` is the entry point everywhere else and stays one:
    parsing `pyproject.toml` is how it guarantees a synced environment. The
    conflict workflow is the single place that guarantee cannot be paid for,
    because its commands exist to repair the merge that left the manifest
    unparseable — so those are documented reaching the console script
    directly, and a documented invocation the classifier does not resolve is
    a denial rather than a fix. The rows match on the executable's name, which
    is what a console script presents however it was reached: by a path into
    this project's environment, or bare off `PATH` where that environment is
    not inside the checkout at all. One rule covers both, so a project's
    layout never becomes a second policy. Every other subcommand
    bounces back naming the spelling that is admitted, which is what an agent
    reaching past `uv` for no reason should be told.

    The one operation it admits sits under `git`, the sub-app the wired CLI
    mounts it in and the spelling the merge skill, the command reference and
    the conflicted-manifest notice all name; the console script's import-safe
    dispatch answers the same words. It states no placement of its own, so it
    carries the one `RUNNER_TARGETS` gives the same toolchain reached through
    `uv`: a verdict that depended on which spelling reached it would be two
    policies.
    """
    reach_through_uv = (
        "reach this toolchain through `uv run lup-devtools`, which guarantees"
        " the environment it runs in — only the conflict workflow, whose"
        " commands must start while the manifest does not parse, is documented"
        " without it"
    )
    # What it does is run this project's own toolchain, and that is the same
    # whichever spelling reached it -- so the refusal is stated as one rather
    # than as an effect. `uv run lup-devtools dev check` and `lup-devtools dev
    # check` do identical things; only one of them guarantees the environment.
    #
    # lup's own verb table is carried for the one thing this spelling needs of
    # it: an operator-only verb stays a refusal no marker escalates. Every
    # other verb it judges is refused here for its spelling, so each sub-app
    # tells the agent the route rather than a verdict about the verb. `git` is
    # stated once, below, where its conflict workflow is the one exception.
    routed = [
        judged.model_copy(
            update={
                "reason": reach_through_uv,
                "operations": [
                    operation
                    for operation in judged.operations
                    if operation.operator_only
                ],
            }
        )
        for judged in devtools_rules()
        if judged.name != "git"
    ]
    return ShellCommandRule(
        name="lup-devtools",
        effects=[declare("runs_declared_target", scope="lup-devtools")],
        refuses=reach_through_uv,
        subcommands=[
            *routed,
            ShellSubcommandRule(
                name="git",
                operations=[
                    # The documented exception, which clears the refusal it
                    # would otherwise inherit rather than restating the grant.
                    ShellOperationRule(name="conflict", refuses="")
                ],
                reason=reach_through_uv,
            ),
        ],
        reason=reach_through_uv,
    )


# lup: template: which module roots this domain runs `uv run -m` against, if
# any. `examples` is the scaffold's own, and goes with the tree
# `dev init drop-examples` removes.
RUNNER_TARGETS: list[RunnerTargetRule] = runner_target_rules(also=("examples",))
"""What `uv run <target>` may reach here, and where each target has to run.

The three groups are taken as the library offers them: the checkers are this
project's, and `lup-devtools` is the toolchain the group places outside the
sandbox. `examples` is the one name that is nobody else's — the module root
this repository's runnable exemplars live under, which admits
`uv run -m examples.<name>` for every one of them and is the spelling each
example's own docstring documents. A domain adopting this scaffold drops the
tree and this name with it.
"""


EVERYDAY_COMMANDS: list[CommandFamily] = everyday_commands(
    also=[
        CommandFamily(
            what="reaching this project's own toolchain",
            commands=[
                "uv run lup-devtools dev check",
                "uv run lup-devtools dev pending",
                "uv run lup-devtools dev issues",
                "uv run lup-devtools dev hooks classify 'ls'",
                "uv run lup-devtools dev hooks sweep tmp/commands.txt",
                "uv run lup-devtools dev py info lup.policy.kernel.effects",
                "uv run lup-devtools dev py search 'declare'",
                "uv run lup-devtools harness generate all",
                "uv run lup-devtools review wait 0f3c9b1e2d4a",
                "uv run --directory . lup-devtools review wait --any",
                "uv run --directory . lup-devtools review propose "
                "./tmp/cdx --why 'Retire the unused flag.'",
                "uv run --directory . lup-devtools review reply 0f3c9b1e2d4a "
                "'Renamed it as you asked.'",
            ],
        )
    ]
)
"""What an ordinary session here must keep being allowed to run.

The library's families, plus the half no other project inherits: every one of
these reaches `lup-devtools` through `uv run`, and a rule that stopped one of
them would stop a step the guidance documents by name. The review commands
among them, in the shape a parked call's refusal spells them -- run in the
session's own checkout with ``--directory`` -- since a rule that stopped
`review wait` would leave every parked call waiting on a waiter that cannot
run, and one stopping `review propose` or `reply` would leave the operator
without the batch or the answer the guidance asks for.
"""


SHELL_RULES: Selection[ShellCommandRule] = Selection[ShellCommandRule](
    overrides=[
        lup_devtools_rule(),
        git_rule(integration_branches=INTEGRATION_BRANCHES, redirect_checkout=True),
        # The TypeScript half of this project's toolchain. Composed here
        # rather than inherited, because whether a project has a JS toolchain
        # at all is that project's fact — and until `bun` is named by some
        # rule, the kernel refuses every one of its subcommands as inline
        # code, `bun install` included.
        bun_rule(),
        *typescript_rule(),
    ]
)
"""Where this project's shell vocabulary differs from the one lup offers.

`git` is declared again to carry the two arguments above; it replaces the
offered rule rather than sitting beside it, so nothing has to reason about
which of two rules named `git` a walk reaches first. `lup-devtools`, `bun`
and the TypeScript compiler are this project's own and have no library rule
to replace.

Everything else — `ls`, `grep`, `gh`, `docker`, the guarded tools, the
redirected verbs — arrives from `default_vocabulary()` and is not restated
here. A table that restated them would have to be re-copied every time the
library judged a new command, and the copy that fell behind would read as a
decision rather than as the oversight it was.
"""
