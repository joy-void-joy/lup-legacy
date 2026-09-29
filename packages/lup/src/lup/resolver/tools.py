"""The tools a resolver worker asks its material questions through.

Surfacing a question by ending a turn with it in the typed report costs a
whole new session to deliver the answer. These tools let a worker ask
mid-turn and keep working.

The handlers touch nothing but the mailbox and the lease they are bound to,
so one factory serves both transports: Claude registers the server
in-process, Codex spawns it as a stdio subprocess that rebuilds both from
the relayed context. Only where that context comes from differs.
"""

import asyncio
from collections.abc import Collection
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from lup.harness.process import LocalProcessLauncher, ProcessLauncher
from lup.tools.mcp import LupMcpTool, ToolError, lup_tool
from lup.policy.assets.host import recoverable_write_targets
from lup.resolver.declaration import declaration_delta, inspect_changes
from lup.coordination.cohort import ActorCohort
from lup.coordination.tools import (
    AskedQuestion,
    AwaitAnswersOutput,
    QuestionDesk,
    create_cohort_tools,
)
from lup.resolver.record import Journal
from lup.coordination.mailbox import (
    ANSWER_POLL_SECONDS,
)
from lup.resolver.mailbox import QuestionMailbox
from lup.policy.identity import ConcernAllowance
from lup.resolver.models import (
    AllowanceRuling,
    MaterialQuestion,
    asks_for_an_allowance,
)
from lup.types import EnvVars

# Four env var names the orchestrator sets and a worker's tool process reads,
# so each is a handshake between two processes rather than a value either picks.
RESOLVER_RUN_DIR_ENV = "LUP_RESOLVER_RUN_DIR"  # lup: ignore[constant-declaration]
RESOLVER_CONCERN_ENV = "LUP_RESOLVER_CONCERN"  # lup: ignore[constant-declaration]
RESOLVER_LEASE_ROOT_ENV = "LUP_RESOLVER_LEASE_ROOT"  # lup: ignore[constant-declaration]
RESOLVER_ACTOR_ENV = "LUP_RESOLVER_ACTOR"  # lup: ignore[constant-declaration]
TOOL_WAIT_SECONDS = 300.0

# lup: ignore[constant-declaration] — one sentence every waiting tool's schema
# states identically, declared beside the tools rather than chosen per caller
WAIT_CONTRACT = (
    "This call blocks until a human answers. Blocking is expected and correct: "
    "you are not stuck, you are waiting. Do not poll, do not call this again in "
    "a loop, do not sleep and retry, and do not read files under .lup/. Make one "
    "call, wait for it to return, then act on what it gives you."
)


class ResolverToolContext(BaseModel):
    """What a tool-serving subprocess needs to find its run's mailbox."""

    run_dir: Path
    concern_id: str
    lease_root: Path
    actor_kind: str = ""
    """Which actor this subprocess serves, where the tools differ by it.

    A merger carries the verbs that sequence a join and a worker must not:
    commit authority over the tree everything lands in is the whole of what
    separates them, and one lease can open both.
    """

    def to_env(self) -> EnvVars:
        return {
            RESOLVER_RUN_DIR_ENV: str(self.run_dir),
            RESOLVER_CONCERN_ENV: self.concern_id,
            RESOLVER_LEASE_ROOT_ENV: str(self.lease_root),
            RESOLVER_ACTOR_ENV: self.actor_kind,
        }


class ResolverToolEnv(BaseSettings):
    """The relay's consumer side, parsed from the environment."""

    run_dir: Path | None = Field(default=None, validation_alias=RESOLVER_RUN_DIR_ENV)
    concern_id: str | None = Field(default=None, validation_alias=RESOLVER_CONCERN_ENV)
    lease_root: Path | None = Field(
        default=None, validation_alias=RESOLVER_LEASE_ROOT_ENV
    )
    actor_kind: str = Field(default="", validation_alias=RESOLVER_ACTOR_ENV)


def read_resolver_tool_context() -> ResolverToolContext | None:
    """Rebuild the relayed context, or None when this is not a tool subprocess."""
    env = ResolverToolEnv()
    if env.run_dir is None or env.concern_id is None or env.lease_root is None:
        return None
    return ResolverToolContext(
        run_dir=env.run_dir,
        concern_id=env.concern_id,
        lease_root=env.lease_root,
        actor_kind=env.actor_kind,
    )


class CheckDeclarationInput(BaseModel):
    files_changed: list[Path] = Field(
        default=[],
        description="Every path you believe you changed, as you would report it",
    )
    swept_beyond_scope: list[Path] = Field(
        default=[],
        description="Paths you would report as swept beyond your concern's scope",
    )


class CheckDeclarationOutput(BaseModel):
    settled: bool = Field(
        description="Whether this account would pass the declaration gate"
    )
    changed: list[str] = Field(description="Every path git sees your worktree moved")
    undeclared: list[str] = Field(
        description="Changed, and named nowhere in the account you passed"
    )
    unswept: list[str] = Field(
        description="Claimed as swept beyond scope, and not changed at all"
    )
    instruction: str


IRREVERSIBLE_VERBS = dict.fromkeys(
    ["push", "reset", "checkout", "restore", "clean", "rebase", "filter-branch"]
)


def agent_may_approve(
    command: str, root: Path, irreversible: Collection[str] = IRREVERSIBLE_VERBS
) -> bool:
    """Whether an orchestrating agent may answer this ask, or only a human.

    Recoverability decides, not the verb. Removing a file the object store
    already holds is recoverable, so an agent may approve it; removing an
    untracked one is not, and neither is a hard reset over uncommitted work, a
    force-push, or anything that leaves this machine.

    What counts as recoverable is the permission kernel's own answer, taken
    from the host half rather than asked again here. Two definitions of the
    word is how one of them ends up weaker: this asked only whether Git
    tracked the path, so a tracked file carrying uncommitted edits read as
    recoverable and approving its removal discarded work nothing could
    restore. Directories are excluded there for the same reason, and now here.
    """
    words = command.split()
    if not words:
        return False
    if any(word in irreversible for word in words[:2]):
        return False
    if words[0] != "rm":
        return True
    targets = [word for word in words[1:] if not word.startswith("-")]
    return bool(targets) and recoverable_write_targets(targets, root) == targets


class RequestAllowanceInput(BaseModel):
    allowance: ConcernAllowance = Field(
        description="The gate you need, which must be one this run knows"
    )
    reason: str = Field(
        description="Why the work cannot be done without it, stated concretely"
    )


def create_question_tools(
    mailbox: QuestionMailbox,
    concern_id: str,
    *,
    run_id: str,
    lease_root: Path,
    launcher: ProcessLauncher | None = None,
    poll_interval_seconds: float = ANSWER_POLL_SECONDS,
    wait_seconds: float = TOOL_WAIT_SECONDS,
    wake: asyncio.Event | None = None,
) -> list[LupMcpTool]:
    """Build the ask tools bound to one concern's mailbox.

    ``concern_id`` is bound here rather than taken as an argument, so a
    worker structurally cannot post a question against a sibling concern,
    and ids are composed rather than trusted — two workers inventing the
    same short id must not collide in one flat namespace.
    """
    inspector = launcher if launcher is not None else LocalProcessLauncher()
    # Over the mailbox's own mail and the run's own journal, so this process
    # and the orchestrator share one message stream and one record. Left to
    # build its own, a cohort here would open a second stream beside the one
    # every door writes, and a journal on the path the run's own already
    # holds.
    cohort = ActorCohort(
        mailbox.root,
        journal=Journal(mailbox.root),
        mail=mailbox.mail,
        run_id=run_id,
    )

    def material(identifier: str, asked: AskedQuestion) -> MaterialQuestion:
        """This run's own question type, under the id the desk composed.

        The only thing the resolver adds to a question: which concern is
        asking, and whether the answer domain is closed — a gate whose reader
        tests for a literal token closes it, where an ordinary question leaves
        the human free to answer in their own words.
        """
        return MaterialQuestion(
            id=identifier,
            concern_id=concern_id,
            prompt=asked.prompt,
            choices=asked.choices,
            recommendation=asked.recommendation,
            closed_choices=asks_for_an_allowance(concern_id, identifier),
        )

    desk = QuestionDesk(
        mailbox,
        concern_id,
        material,
        run_id=run_id,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        wake=wake,
        parked_instruction=(
            "Stop working on this concern and submit your report now — the "
            "orchestrator parks the run and resumes it once a human answers."
        ),
    )

    @lup_tool(
        "Ask for a gate your concern was not approved for. A plan-time "
        "allowance is granted when a concern is planned, and a need nobody "
        "could have foreseen — a rule that only meets its exception once two "
        "branches are joined — has no other route. This asks a human and "
        "waits, like any other question. A grant takes effect in this "
        "session, from the moment it is answered: retry the call that was "
        "refused and carry on from where you stopped. Nothing restarts, and "
        "nothing you have already done needs doing again.",
        name="request_allowance",
    )
    async def request_allowance(params: RequestAllowanceInput) -> AwaitAnswersOutput:
        return await desk.ask(
            [
                AskedQuestion(
                    id=f"allow-{params.allowance}",
                    prompt=(
                        f"Grant `{params.allowance}` to {concern_id}?\n\n"
                        f"{params.reason}"
                    ),
                    choices=AllowanceRuling.choices(),
                )
            ]
        )

    @lup_tool(
        "Check the file account you are about to submit against what your "
        "worktree actually changed. Your report must name every path you "
        "changed, and must not claim to have swept a path you left alone — "
        "two directions, where correcting one is what violates the other. "
        "Call this before you submit: it runs the same reading the gate runs, "
        "so an account it settles is one the gate accepts. You cannot run git "
        "yourself here, and a report the gate rejects costs you a whole "
        "session to correct.",
        name="check_declaration",
    )
    async def check_declaration(
        params: CheckDeclarationInput,
    ) -> CheckDeclarationOutput:
        # HEAD is the commit the gate measures from: a worker holds no commit
        # authority, so the lease's head is where its turn opened and stays
        # there for as long as the turn runs.
        inspected = inspect_changes(inspector, lease_root, "HEAD")
        if inspected.failure:
            raise ToolError(inspected.failure)
        delta = declaration_delta(
            inspected.paths, params.files_changed, params.swept_beyond_scope
        )
        return CheckDeclarationOutput(
            settled=delta.settled,
            changed=sorted(path.as_posix() for path in inspected.paths),
            undeclared=delta.undeclared,
            unswept=delta.unswept,
            instruction=(
                "This account passes the declaration gate. Submit it as it stands."
                if delta.settled
                else f"{delta.reason}. Fix the account, not the work: add "
                "every undeclared path to files_changed, and drop every "
                "unswept path from swept_beyond_scope. Then call this again."
            ),
        )

    # The common verbs come from the library, which is where they are written
    # once: reporting and asking, but not steering — a worker holds a lease on
    # one concern, and letting it redirect a sibling's session would be commit
    # authority over somebody else's attention that no lease grants.
    return [
        *create_cohort_tools(cohort, report=True, questions=desk),
        request_allowance,
        check_declaration,
    ]
