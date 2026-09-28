"""Reading an agent that has not finished.

The capability these are written against is the one that turns steering from
a guess into a correction. A caller can already start three stances and
redirect any of them; what it could not do was find out which one was getting
somewhere first, so every redirect was aimed at an agent nobody could see.

What makes it reachable is that a turn drains into the journal as it happens.
These are written against the reader over that record: that it carries what
the agent said rather than what it was told, that it names the instrument
behind a refusal, that a follower resumes without repeating, and that nothing
it cannot fit is dropped without being counted.
"""

from pathlib import Path

import pytest

from lup.coordination.cohort import ActorCohort
from lup.coordination.progress import ProgressWindow, read_progress
from lup.coordination.tools import SpawnReadInput, create_cohort_tools
from lup.channels.models import Door
from lup.tools.mcp import LupMcpTool, ToolError
from lup.sessions.events import (
    MessageCompletedEvent,
    SessionId,
    TurnCompletedEvent,
    TurnId,
    TurnIdentifiers,
    TurnMessage,
    TurnTextBlock,
    TurnThinkingBlock,
    TurnToolCallBlock,
    TurnToolResultBlock,
)
from lup.types import JsonObject


def identifiers() -> TurnIdentifiers:
    return TurnIdentifiers(session=SessionId(value="s"), turn=TurnId(value="t"))


def said(text: str) -> MessageCompletedEvent:
    """One assistant message carrying nothing but words."""
    return MessageCompletedEvent(
        identifiers=identifiers(),
        message=TurnMessage(role="assistant", blocks=[TurnTextBlock(text=text)]),
    )


def thought(text: str) -> MessageCompletedEvent:
    """One assistant message carrying nothing but reasoning."""
    return MessageCompletedEvent(
        identifiers=identifiers(),
        message=TurnMessage(
            role="assistant", blocks=[TurnThinkingBlock(thinking=text)]
        ),
    )


def asked(text: str) -> MessageCompletedEvent:
    """One message putting something in front of the agent."""
    return MessageCompletedEvent(
        identifiers=identifiers(),
        message=TurnMessage(role="user", blocks=[TurnTextBlock(text=text)]),
    )


def called(name: str, call_id: str, arguments: JsonObject) -> MessageCompletedEvent:
    """One assistant message invoking an instrument."""
    return MessageCompletedEvent(
        identifiers=identifiers(),
        message=TurnMessage(
            role="assistant",
            blocks=[TurnToolCallBlock(id=call_id, name=name, arguments=arguments)],
        ),
    )


def answered(
    call_id: str, content: str, is_error: bool = False
) -> MessageCompletedEvent:
    """One tool message answering a call."""
    return MessageCompletedEvent(
        identifiers=identifiers(),
        message=TurnMessage(
            role="tool",
            blocks=[
                TurnToolResultBlock(
                    tool_call_id=call_id, content=content, is_error=is_error
                )
            ],
        ),
    )


def cohort_tools(cohort: ActorCohort) -> dict[str, LupMcpTool]:
    """The reading verbs, addressed by tool name."""
    return {
        tool.name: tool for tool in create_cohort_tools(cohort, roster=True, steer=True)
    }


def test_a_working_spawn_is_readable_before_it_returns(tmp_path: Path) -> None:
    """The whole point: findings on disk are findings a caller can act on."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.journal.append(actor, said("the 3-adic argument closes at n=27"))

    progress = read_progress(cohort, actor, ProgressWindow())

    assert progress.running
    assert [line.kind for line in progress.lines] == ["said"]
    assert progress.lines[0].text == "the 3-adic argument closes at n=27"


def test_what_the_agent_was_given_is_not_read_back(tmp_path: Path) -> None:
    """A caller reading a spawn is not reading its own task restated."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.journal.append(actor, asked("attack the drift bound"))
    cohort.journal.append(actor, said("drift is not the obstruction"))

    progress = read_progress(cohort, actor, ProgressWindow())

    assert [line.text for line in progress.lines] == ["drift is not the obstruction"]


def test_reasoning_is_not_what_a_reader_pays_for(tmp_path: Path) -> None:
    """The largest thing in a turn is not the thing that answers the question."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.journal.append(actor, thought("let me try the parity vector" * 40))
    cohort.journal.append(actor, said("parity vectors do not close it"))

    progress = read_progress(cohort, actor, ProgressWindow())

    assert [line.text for line in progress.lines] == ["parity vectors do not close it"]


def test_a_refusal_names_the_instrument_that_refused(tmp_path: Path) -> None:
    """A stuck spawn is worth steering, and what stuck it is the useful half.

    The call and its result arrive in different messages, so naming the tool
    is a correlation across the page rather than a field on the failure.
    """
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("computator")
    cohort.spawn(actor, "measure the tail")
    cohort.journal.append(actor, called("orbit_range_stats", "call-1", {"start": "1"}))
    cohort.journal.append(actor, answered("call-1", "budget exhausted", is_error=True))

    progress = read_progress(cohort, actor, ProgressWindow())

    assert [line.kind for line in progress.lines] == ["called", "refused"]
    assert progress.lines[1].tool == "orbit_range_stats"
    assert progress.lines[1].text == "budget exhausted"


def test_a_call_that_returned_is_news_that_it_happened(tmp_path: Path) -> None:
    """A whole certificate is the artifact, not what a watcher reads."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("certifier")
    cohort.spawn(actor, "hunt a witness")
    cohort.journal.append(actor, called("cert_verify", "call-1", {"slug": "drift-7"}))
    cohort.journal.append(actor, answered("call-1", "x" * 5000))

    progress = read_progress(cohort, actor, ProgressWindow())

    assert [line.kind for line in progress.lines] == ["called"]
    assert progress.lines[0].tool == "cert_verify"
    assert "slug=drift-7" in progress.lines[0].text


def test_a_follower_resumes_without_reading_anything_twice(tmp_path: Path) -> None:
    """The cursor is what makes reading a running spawn repeatable."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.journal.append(actor, said("first"))

    first = read_progress(cohort, actor, ProgressWindow())
    cohort.journal.append(actor, said("second"))
    second = read_progress(cohort, actor, ProgressWindow(after=first.cursor))

    assert [line.text for line in first.lines] == ["first"]
    assert [line.text for line in second.lines] == ["second"]
    assert second.cursor > first.cursor


def test_a_read_with_nothing_new_keeps_the_readers_place(tmp_path: Path) -> None:
    """A quiet spawn must not send its follower back to the beginning."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.journal.append(actor, said("only line"))

    first = read_progress(cohort, actor, ProgressWindow())
    again = read_progress(cohort, actor, ProgressWindow(after=first.cursor))

    assert again.lines == []
    assert again.cursor == first.cursor


def test_only_this_conversation_is_read(tmp_path: Path) -> None:
    """Three stances running at once are three reads, not one merged one."""
    cohort = ActorCohort(tmp_path)
    analyst = cohort.actor("analyst")
    refuter = cohort.actor("refuter")
    cohort.spawn(analyst, "derive")
    cohort.spawn(refuter, "attack")
    cohort.journal.append(analyst, said("analyst line"))
    cohort.journal.append(refuter, said("refuter line"))

    progress = read_progress(cohort, analyst, ProgressWindow())

    assert [line.text for line in progress.lines] == ["analyst line"]


def test_a_second_round_carries_on_the_same_record(tmp_path: Path) -> None:
    """A round is another attempt by one agent, not another agent.

    A reader that filtered by the ref would watch a worker's record stop at
    exactly the moment it was given another go.
    """
    cohort = ActorCohort(tmp_path)
    first = cohort.actor("worker", id="drift")
    cohort.spawn(first, "prove it")
    cohort.journal.append(first, said("round one"))
    second = first.model_copy(update={"round": 2})
    cohort.journal.append(second, said("round two"))

    progress = read_progress(cohort, second, ProgressWindow())

    assert [line.text for line in progress.lines] == ["round one", "round two"]


def test_a_page_keeps_the_recent_end_and_counts_the_rest(tmp_path: Path) -> None:
    """An agent that outran its reader says so rather than looking quiet."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    for index in range(10):
        cohort.journal.append(actor, said(f"line {index}"))

    progress = read_progress(cohort, actor, ProgressWindow(limit=3))

    assert [line.text for line in progress.lines] == ["line 7", "line 8", "line 9"]
    assert progress.skipped == 7


def test_a_cut_line_says_how_much_it_stands_for(tmp_path: Path) -> None:
    """A shortened line is visibly shortened, never a whole one that lied."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.journal.append(actor, said("y" * 100))

    progress = read_progress(cohort, actor, ProgressWindow(chars=20))

    assert progress.lines[0].text == "y" * 20 + "… (+80 more chars)"


def test_a_redirect_reads_as_a_redirect(tmp_path: Path) -> None:
    """Telling and stopping stay apart wherever either is recorded.

    A caller checking whether its redirect landed cannot be shown a line
    that reads like an ordinary message.
    """
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.say(actor, "that branch is closed", redirect=True)
    cohort.say(actor, "here is a bound")
    cohort.mailbox(actor).take()

    progress = read_progress(cohort, actor, ProgressWindow())

    assert [line.kind for line in progress.lines] == ["redirected", "received"]
    assert progress.lines[0].door == Door.AGENT


def test_what_a_closing_spawn_never_read_is_read_here(tmp_path: Path) -> None:
    """A redirect nobody read is the failure of an operation somebody performed."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.say(actor, "stop and check n=27", redirect=True)
    cohort.mailbox(actor).record_outstanding()

    progress = read_progress(cohort, actor, ProgressWindow())

    assert [line.kind for line in progress.lines] == ["unread"]
    assert progress.lines[0].text == "stop and check n=27"


async def test_a_finished_spawn_is_read_as_finished(tmp_path: Path) -> None:
    """A quiet page from a working agent and from a finished one differ."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.journal.append(actor, said("the bound holds"))
    await cohort.finish(actor, summary="bound holds to 2^40")

    progress = read_progress(cohort, actor, ProgressWindow())

    assert not progress.running
    assert progress.summary == "bound holds to 2^40"
    assert [line.text for line in progress.lines] == ["the bound holds"]


async def test_a_spawn_that_died_reads_as_died(tmp_path: Path) -> None:
    """How a delegation ended is information about how hard the target was."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("formalizer")
    cohort.spawn(actor, "drive it in Lean")
    await cohort.finish(actor, error="the elaborator timed out")

    progress = read_progress(cohort, actor, ProgressWindow())

    assert not progress.running
    assert progress.error == "the elaborator timed out"


def test_a_turn_that_completed_nothing_contributes_nothing(tmp_path: Path) -> None:
    """Lifecycle events say when a turn moved, never what it said."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.journal.append(actor, TurnCompletedEvent(identifiers=identifiers()))

    assert read_progress(cohort, actor, ProgressWindow()).lines == []


async def test_the_tool_reads_the_address_the_listing_printed(tmp_path: Path) -> None:
    """Whatever a reader saw is a handle the reading verb takes."""
    cohort = ActorCohort(tmp_path)
    actor = cohort.actor("analyst")
    cohort.spawn(actor, "locate the barrier")
    cohort.journal.append(actor, said("found something"))
    tools = cohort_tools(cohort)

    printed = cohort.live()[0].address
    progress = await tools["spawn_read"](SpawnReadInput(address=printed))
    bare = await tools["spawn_read"](SpawnReadInput(address=actor.id))

    assert progress.lines == bare.lines
    assert progress.address == printed


async def test_reading_an_address_nobody_spawned_says_who_is_here(
    tmp_path: Path,
) -> None:
    """A miss that only says no leaves a caller guessing at its own spelling."""
    cohort = ActorCohort(tmp_path)
    cohort.spawn(cohort.actor("analyst"), "locate the barrier")
    tools = cohort_tools(cohort)

    with pytest.raises(ToolError) as raised:
        await tools["spawn_read"](SpawnReadInput(address="analyst:nosuch"))

    assert "analyst:" in str(raised.value)


def test_reading_comes_with_listing_rather_than_with_steering(tmp_path: Path) -> None:
    """Reading is the roster's grant refined, not the power to redirect.

    A population whose members must not retarget each other can still be
    watched, and a consumer taking the listing has already been handed what
    each member was asked and what it concluded.
    """
    cohort = ActorCohort(tmp_path)

    listing = {tool.name for tool in create_cohort_tools(cohort, roster=True)}
    steering = {tool.name for tool in create_cohort_tools(cohort, steer=True)}

    assert "spawn_read" in listing
    assert "spawn_read" not in steering
