"""The verbs an agent uses to reach the rest of its cohort, and the outside.

Every consumer that spawns agents needs the same small set — say who I have
running, say something to one of them, say something back to whoever spawned
me, and ask a question that somebody has to decide. Written per consumer they
come out slightly different each time, and the copy that disagrees with the
delivery path about what an address is is the one where a message is reported
sent and read by nobody.

A consumer takes what it wants. The steering verbs suit a session that spawns
agents as it goes and may want to redirect one; a run whose population is
fixed and whose members must not retarget each other takes the reporting and
question verbs alone. The selection is a parameter rather than a fork, so the
descriptions an agent reads are the same wherever they are served.

The descriptions are part of the mechanism rather than decoration. An agent
never sees this module; the description is its whole documentation, and the
distinction between telling something and stopping it is only useful if the
tool says which is which.
"""

import asyncio
from collections.abc import Callable

from pydantic import BaseModel, Field

from lup.coordination.cohort import ActorCohort
from lup.coordination.mailbox import (
    ANSWER_POLL_SECONDS,
    MailboxConflictError,
    PendingQuestion,
    QuestionMailbox,
    wait_for_answers,
)
from lup.coordination.progress import (
    ActorProgress,
    ProgressWindow,
    read_progress,
)
from lup.coordination.peers import USER_ADDRESS
from lup.coordination.questions import Question
from lup.coordination.refs import ActorRef
from lup.coordination.roster import Delivery, RosterMember
from lup.channels.models import Door, utc_now
from lup.tools.mcp import LupMcpTool, ToolError, lup_tool

TOOL_WAIT_SECONDS = 300.0
"""How long one wait call holds before it reports back unanswered.

An overridable default rather than a constant: how long an agent should sit on
a decision is the consumer's judgement about its own humans, not this layer's.
"""

# lup: ignore[constant-declaration] — the contract this tool states about its
# own blocking, declared beside the tools that block; a caller free to reword
# it is a caller free to tell an agent that polling is fine
WAIT_CONTRACT = (
    "This call blocks until a human answers. Blocking is expected and correct: "
    "you are not stuck, you are waiting. Do not poll, do not call this again in "
    "a loop, do not sleep and retry, and do not read files under .lup/. Make one "
    "call, wait for it to return, then act on what it gives you."
)


class NoInput(BaseModel):
    """A tool that asks the cohort about itself takes no arguments."""


class SpawnListOutput(BaseModel):
    """Every agent this session spawned, still working ones first."""

    spawns: list[RosterMember] = []


class SpawnSayInput(BaseModel):
    address: str = Field(
        description=(
            "Which spawn to reach. Anything the spawn listing printed works, "
            "including the bare id"
        )
    )
    text: str = Field(description="What the spawn should read")
    redirect: bool = Field(
        default=False,
        description=(
            "Whether to stop the spawn rather than inform it. A redirect "
            "refuses its next tool call and hands back this text as the "
            "reason, so it cannot carry on without reading why. Use it when "
            "the task was wrong, not when a fact has merely changed"
        ),
    )


class SpawnSayOutput(BaseModel):
    address: str
    reaches: bool = Field(
        description=(
            "Whether this address is still working, and so will read what was "
            "just posted. False means the message is recorded and nobody will "
            "ever be handed it — the spawn has ended"
        )
    )
    outstanding: int = Field(
        description=(
            "How much is queued for this spawn and not yet handed over. "
            "Nonzero after a spawn has ended means it read none of it"
        )
    )
    delivery: Delivery = Field(
        description=(
            "What carries this to the recipient. `hook` means its own hook "
            "puts the message in front of its next tool call, so a working "
            "recipient cannot fail to read it. `waiting` means the message "
            "waits in its mailbox until the recipient next looks, and nothing "
            "will wake it"
        )
    )


class SpawnReadInput(ProgressWindow):
    """Which spawn to read, and how much of it.

    The window is inherited rather than nested, so an agent that wants the
    default page fills in one field and an agent following a fast spawn can
    reach every knob at the same level.
    """

    address: str = Field(
        description=(
            "Which spawn to read. Anything the spawn listing printed works, "
            "including the bare id"
        )
    )


class SendMessageInput(BaseModel):
    text: str = Field(description="What whoever is watching should know")
    to_actor: str = Field(
        default="",
        description=(
            "Actor label to address, like 'worker:some-concern#1'. Leave it "
            "out to reach the humans watching, which is what you want unless "
            "you are answering another actor by name."
        ),
    )
    in_reply_to: str = Field(
        default="",
        description="The id of a message or question this answers, if any",
    )


class SendMessageOutput(BaseModel):
    sent: bool = Field(description="Whether the message reached the record")


class AskedQuestion(BaseModel):
    """One question an agent wants decided, in the terms it states it."""

    id: str = Field(description="Short identifier, unique among the questions you ask")
    prompt: str = Field(description="What you need decided, stated in full")
    choices: list[str] = Field(
        default=[], description="The options, where the answer is a choice"
    )
    recommendation: str | None = Field(
        default=None, description="Which choice you would take, and it must be one"
    )


class QueueQuestionsInput(BaseModel):
    questions: list[AskedQuestion] = Field(description="Everything you need decided")


class QueueQuestionsOutput(BaseModel):
    question_ids: list[str] = Field(description="The ids to wait on")
    already_answered: list[str] = []
    pending: list[str] = []


class AwaitAnswersInput(BaseModel):
    question_ids: list[str] = Field(
        default=[], description="Which to wait for; empty waits for all you queued"
    )


class AnsweredQuestion(BaseModel):
    id: str
    prompt: str
    value: str


class AwaitAnswersOutput(BaseModel):
    status: str = Field(description="'answered' or 'parked'")
    answers: list[AnsweredQuestion] = []
    unanswered: list[str] = []
    instruction: str = Field(description="What to do with this outcome")


class QuestionDesk[Q: Question]:
    """One asker's bound question channel: where to post, and under whose name.

    A plain class rather than a model because it holds a builder and a live
    mailbox — behaviour and a seam, on the same terms as
    :class:`~lup.sessions.turns.LazyTurn`.

    ``asked_by`` is bound at construction rather than taken per call, so an
    agent structurally cannot post a question against a sibling, and ids are
    composed rather than trusted: two agents inventing the same short id must
    not collide in one flat namespace.
    """

    def __init__(
        self,
        mailbox: QuestionMailbox[Q],
        asked_by: str,
        build: Callable[[str, AskedQuestion], Q],
        *,
        run_id: str,
        wait_seconds: float = TOOL_WAIT_SECONDS,
        poll_interval_seconds: float = ANSWER_POLL_SECONDS,
        wake: asyncio.Event | None = None,
        parked_instruction: str = "",
    ) -> None:
        self.mailbox = mailbox
        self.asked_by = asked_by
        self.build = build
        self.run_id = run_id
        self.wait_seconds = wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.wake = wake
        self.parked_instruction = parked_instruction
        self.queued: list[str] = []

    def compose(self, identifier: str) -> str:
        """This asker's own namespace for a short id it made up."""
        return f"{self.asked_by}-{identifier}"

    def prompts(self) -> dict[str, str]:  # lup: ignore[dict-str-payload] — open id map
        return {
            item.question.id: item.question.prompt for item in self.mailbox.questions()
        }

    def queue(self, asked: list[AskedQuestion]) -> QueueQuestionsOutput:
        """Post questions under this asker's name, and say which already stand."""
        answered = self.mailbox.answered_ids()
        composed = [self.compose(item.id) for item in asked]
        for identifier, item in zip(composed, asked, strict=True):
            try:
                question = self.build(identifier, item)
            except ValueError as error:
                raise ToolError(
                    f"question {item.id!r} is not well formed: {error}"
                ) from error
            try:
                self.mailbox.queue(
                    PendingQuestion(
                        run_id=self.run_id,
                        question=question,
                        asked_by=self.asked_by,
                        asked_at=utc_now(),
                    )
                )
            except MailboxConflictError as error:
                raise ToolError(str(error)) from error
            if identifier not in self.queued:
                self.queued.append(identifier)
        return QueueQuestionsOutput(
            question_ids=composed,
            already_answered=[name for name in composed if name in answered],
            pending=[name for name in composed if name not in answered],
        )

    async def settled(self, question_ids: list[str]) -> AwaitAnswersOutput:
        """Wait for the named answers, or for everything this asker queued."""
        wanted = question_ids or list(self.queued)
        if not wanted:
            raise ToolError("queue a question before waiting for an answer")
        known = self.prompts()
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise ToolError(
                "no question is queued under "
                + ", ".join(repr(name) for name in unknown)
                + "; call queue_questions first and pass the ids it returns"
            )
        result = await wait_for_answers(
            self.mailbox,
            wanted,
            wait_seconds=self.wait_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
            wake=self.wake,
        )
        answers = [
            AnsweredQuestion(
                id=record.answer.question_id,
                prompt=known[record.answer.question_id],
                value=record.answer.value,
            )
            for record in result.answered
        ]
        if not result.unanswered:
            return AwaitAnswersOutput(
                status="answered",
                answers=answers,
                unanswered=[],
                instruction="Act on these answers and finish your work.",
            )
        return AwaitAnswersOutput(
            status="parked",
            answers=answers,
            unanswered=result.unanswered,
            instruction=(
                f"No answer arrived ({result.reason}). {self.parked_instruction}"
            ),
        )

    async def ask(self, asked: list[AskedQuestion]) -> AwaitAnswersOutput:
        """Queue and wait in one call, for a caller that has nothing else to do.

        Here rather than only in the tool, so a consumer's own tool that needs
        a decision — an allowance request, a gate — reaches the same path an
        agent's question takes rather than reimplementing it beside it.
        """
        return await self.settled(self.queue(asked).question_ids)


def create_cohort_tools[Q: Question](
    cohort: ActorCohort,
    *,
    roster: bool = False,
    steer: bool = False,
    report: bool = True,
    questions: QuestionDesk[Q] | None = None,
    door: Door = Door.AGENT,
) -> list[LupMcpTool]:
    """The common verbs, bound to one cohort, in whatever mix a consumer wants.

    Reporting is on by default because it is the one every population needs:
    an agent with no way to say anything has only a blocking question, so
    everything it notices costs somebody a decision they were never asked for.

    Steering is off by default, because handing every member the power to
    redirect its siblings is a real grant. A session that spawned its agents
    holds it naturally; a run whose members each hold a lease should not.

    Listing and reading come together. They are the same grant at two
    resolutions — what you have running, and what one of them has been doing
    — and a consumer that hands out one while withholding the other is
    drawing a line nobody asked it to draw.
    """

    def reaching(address: str) -> ActorRef:
        """The agent an operator's spelling reaches, or a refusal naming who is.

        One resolution for every verb that takes an address, so a spelling
        one tool accepts is not a spelling the next one rejects, and a
        failure says who is actually here instead of only that this was not.
        """
        actor = cohort.reaching(address)
        if actor is None:
            known = ", ".join(spawn.address for spawn in cohort.live()) or "none"
            raise ToolError(
                f"no spawn of this session answers to {address!r}; "
                f"this session has spawned: {known}"
            )
        return actor

    @lup_tool(
        "List the agents you have spawned and what each was asked, with the "
        "ones still working first. Their addresses are what the say tool "
        "takes. Use it when you want to steer something you started and do "
        "not remember its address, or to see what a finished spawn concluded "
        "without re-reading its output. The person watching is not listed and "
        "needs no listing: they are always reachable at the address `user`, "
        "which is what an unaddressed report goes to. Returns {spawns: "
        "[{address, kind, task, running, summary, error}]}.",
        name="spawn_actors",
    )
    async def spawn_actors(_params: NoInput) -> SpawnListOutput:
        return SpawnListOutput(spawns=cohort.live())

    @lup_tool(
        "Read what a spawn has found so far, without waiting for it to "
        "finish. Hands back its own words, the instruments it called, "
        "anything that refused it, and anything you have said to it — "
        "oldest first, with a cursor to resume from on the next read.\n\n"
        "This is what makes steering worth doing. You can already redirect a "
        "running spawn, but until you can see what it is doing, every "
        "redirect is a guess about an agent you cannot see. Read it first "
        "and the same message becomes a correction: it is grinding on a case "
        "you have already closed, or it has just produced the bound you were "
        "about to ask for and the useful thing to send is a different "
        "question.\n\n"
        "Reach for it when several stances are running against one note and "
        "you want to know which is getting somewhere before any of them "
        "return; when one has been going long enough that you wonder whether "
        "it is stuck rather than thinking; and before any redirect, because "
        "a redirect aimed at the wrong problem costs that spawn its context "
        "for nothing.\n\n"
        "Its reasoning is not carried, and neither is what its calls "
        "returned: a whole certificate or a search sweep is the artifact "
        "rather than news about it, and the spawn's own words are what it "
        "made of one. `running` says whether it is still working, so a quiet "
        "page is never mistaken for a finished agent, and `skipped` counts "
        "what moved faster than you read. Returns {address, running, lines: "
        "[{seq, at, kind, text, tool, door}], cursor, skipped, summary, "
        "error}.",
        name="spawn_read",
    )
    async def spawn_read(params: SpawnReadInput) -> ActorProgress:
        return read_progress(cohort, reaching(params.address), params)

    @lup_tool(
        "Say something to an agent you spawned, while it is still working. "
        "It lands in front of that agent's next tool call whether or not it "
        "thinks to look, so nothing has to be arranged with it in advance.\n\n"
        "Two verbs, and the difference matters. A message rides alongside "
        "the agent's next call and it keeps going — use it to hand over a "
        "fact it could not have had: a bound you just computed, a result "
        "that makes half its task moot. A redirect refuses that call and "
        "hands back your text as the reason, so the agent cannot take one "
        "more step down what it was doing without reading why it was "
        "stopped — use it when the task itself was wrong.\n\n"
        "This is what makes a long spawn worth starting: an agent checking "
        "the wrong statement, or working a branch you have since closed, is "
        "otherwise unreachable until it finishes. Address it by anything the "
        "spawn listing printed — the bare id works. Returns {address, "
        "reaches, outstanding, delivery}, where outstanding counts what is "
        "queued and not yet handed over — a spawn that has ended reads "
        "nothing more — and delivery says what carries it there.",
        name="spawn_say",
    )
    async def spawn_say(params: SpawnSayInput) -> SpawnSayOutput:
        actor = reaching(params.address)
        cohort.say(actor, params.text, redirect=params.redirect, door=door)
        return SpawnSayOutput(
            address=actor.label(),
            reaches=cohort.reaches(actor),
            outstanding=cohort.outstanding(actor),
            delivery=cohort.delivery(actor),
        )

    @lup_tool(
        "Tell whoever is watching — or one other actor working alongside you "
        "— something, without waiting for a reply. Use this to volunteer what "
        "you have found, to flag a consequence for whoever picks your work "
        "up, to answer something you were asked by naming the actor that "
        "asked, or to say that you are stuck on something that is not a "
        "decision: a gate that refused you, a file you cannot remove, an "
        "environment you cannot repair.\n\n"
        "That last case is worth naming, because the alternative is worse. A "
        "question blocks you until a human answers it, so raising one over "
        "housekeeping spends your turn and their attention on something that "
        "was never a decision. Say it here instead and keep working on "
        "whatever does not depend on it.\n\n"
        "This never blocks and never stops anything. If you genuinely need a "
        "decision before you can continue, that is a question, not a message.",
        name="send_message",
    )
    async def send_message(params: SendMessageInput) -> SendMessageOutput:
        # An unaddressed message goes to the person, which is the address a
        # report is for. Blank matching every actor's own address list would
        # deliver a report meant for the humans into every sibling's context,
        # consume it there, and show it on no surface anyone watches.
        cohort.post(
            params.to_actor or USER_ADDRESS,
            params.text,
            door=door,
            in_reply_to=params.in_reply_to,
        )
        return SendMessageOutput(sent=True)

    def asking(desk: QuestionDesk[Q]) -> list[LupMcpTool]:
        """The three question verbs, each a thin wrapper over one desk."""

        @lup_tool(
            "Ask the human one or more questions and return immediately. Use "
            "this the moment you know a decision is not yours to make — queue "
            "every question you have at once so a human can answer them in "
            "one pass, keep working on whatever does not depend on them, then "
            "call await_answers. Returns the ids to wait on.",
            name="queue_questions",
        )
        async def queue_questions(
            params: QueueQuestionsInput,
        ) -> QueueQuestionsOutput:
            return desk.queue(params.questions)

        @lup_tool(
            "Wait for the human's answers to questions you queued, then "
            "return them. " + WAIT_CONTRACT,
            name="await_answers",
        )
        async def await_answers(params: AwaitAnswersInput) -> AwaitAnswersOutput:
            return await desk.settled(params.question_ids)

        @lup_tool(
            "Ask the human one or more questions and wait for the answers. "
            "This is queue_questions followed by await_answers. " + WAIT_CONTRACT,
            name="ask_questions",
        )
        async def ask_questions(params: QueueQuestionsInput) -> AwaitAnswersOutput:
            return await desk.ask(params.questions)

        return [queue_questions, await_answers, ask_questions]

    return [
        *([spawn_actors, spawn_read] if roster else []),
        *([spawn_say] if steer else []),
        *([send_message] if report else []),
        *(asking(questions) if questions is not None else []),
    ]
