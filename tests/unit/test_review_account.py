"""A parked call keeps who asked, what each command of its line decided, and the asker's own words.

The policy's reason says why a call asks; what it is for is the agent's to
say, and a reviewer reads both. So when a call parks, the runtime's half reads
what the agent said: the note its tool call carries beside the command --
Claude Code's ``description``, Codex's ``justification`` for leaving its
sandbox -- and the words it wrote since it last heard anything, off the
transcript of the conversation that made the call. Where it said nothing
there, what its roster row says it is on stands in, named as that.

A line of several commands keeps each with the verdict it reached on its own,
so a reviewer reads every command that asks with its own reason.
"""

import json
import os
from pathlib import Path
from typing import Literal

import pytest
import sh
from typer.testing import CliRunner

from lup.coordination.bare.store import Caller
from lup.coordination.identity import MEMBER_ENV
from lup.coordination.repository import RepositoryPeers
from lup.devtools.review.app import ReviewDetail, create_review_app
from lup.policy.assets.host import (
    records_backwards,
    review_fingerprint,
    words_before,
)
from lup.policy.identity import DASHBOARD_URL_ENV
from lup.policy.relay import Account, PersistentQuestion, QuestionRelay
from lup.types import JsonObject, JsonValue
from tests.unit.repos import initialized_repo

type Runtime = Literal["claude", "codex"]

ESCALATED = "# lup: escalate[decision]: the operator reviews this\n"
WRITE = f"{ESCALATED}echo carried > marker.txt"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository of its own, where the roster the hook reads is kept."""
    checkout = tmp_path / "checkout"
    initialized_repo(checkout, tmp_path / "hooks", branch="feature")
    monkeypatch.delenv(MEMBER_ENV, raising=False)
    return checkout


def hooked(
    root: Path, runtime: Runtime, tool_input: JsonObject, **fields: JsonValue
) -> str:
    """One shell call put to a runtime's generated hook, under a launch holding a dashboard."""
    payload: JsonObject = {
        "session_id": "asking-session",
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": tool_input,
        "tool_use_id": "call-1",
        **fields,
    }
    result = sh.Command(
        str(Path(f".{runtime}/plugins/lup/hooks/scripts/policy.py").resolve())
    )(
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={
            **os.environ,
            "CLAUDE_PLUGIN_DATA": str(root / "plugin-data"),
            "PLUGIN_DATA": str(root / "plugin-data"),
            DASHBOARD_URL_ENV: "http://127.0.0.1:8766",
        },
    )
    assert isinstance(result, sh.RunningCommand)
    return str(result) + str(result.stderr, "utf-8")


def parked(root: Path) -> PersistentQuestion:
    """The one question waiting, read back whole from the relay's store."""
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    return store.resolve(question)


def lines(path: Path, records: list[JsonObject]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def claude_said(text: str) -> JsonObject:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def claude_heard(text: str) -> JsonObject:
    return {"type": "user", "message": {"content": text}}


def claude_called(identifier: str) -> JsonObject:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": identifier, "name": "Bash", "input": {}}
            ]
        },
    }


def codex_item(payload: JsonObject) -> JsonObject:
    return {
        "timestamp": "2026-09-30T00:00:00Z",
        "type": "response_item",
        "payload": payload,
    }


def codex_said(text: str) -> JsonObject:
    return codex_item(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    )


def test_claude_keeps_the_tool_s_note_and_what_the_subagent_said_first(
    root: Path, tmp_path: Path
) -> None:
    """A subagent's words are in its own transcript, beside the session's that the payload names."""
    session = lines(
        tmp_path / "session.jsonl", [claude_said("the session's own words")]
    )
    lines(
        tmp_path / "session/subagents/agent-a1b2.jsonl",
        [
            claude_heard("Write the marker."),
            claude_said("An earlier thought, answered since."),
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-0",
                            "content": "done",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "unseen"}]},
            },
            claude_said("The marker records that the run finished."),
            {"type": "attachment", "attachment": {"type": "hook"}},
            claude_said("It is one line, and nothing reads it yet."),
            claude_called("call-1"),
        ],
    )

    hooked(
        root,
        "claude",
        {"command": WRITE, "description": "Write the run marker"},
        agent_id="a1b2",
        agent_type="general-purpose",
        transcript_path=str(session),
    )

    question = parked(root)
    assert question.agent == "a1b2"
    assert question.account == [
        Account(source="description", text="Write the run marker"),
        Account(
            source="preceding",
            text="The marker records that the run finished.\n\n"
            "It is one line, and nothing reads it yet.",
        ),
    ]


def test_codex_keeps_the_justification_and_what_the_agent_said_first(
    root: Path, tmp_path: Path
) -> None:
    """The rollout's own call line need not be written yet: the words read the same."""
    rollout = lines(
        tmp_path / "rollout.jsonl",
        [
            {
                "timestamp": "2026-09-30T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "thread"},
            },
            codex_item(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Write it."}],
                }
            ),
            codex_said("Checked the directory."),
            codex_item(
                {"type": "function_call_output", "call_id": "call-0", "output": "ok"}
            ),
            codex_item({"type": "reasoning", "summary": []}),
            codex_said("The marker has to exist before the next step reads it."),
            {
                "timestamp": "2026-09-30T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "duplicated"},
            },
        ],
    )

    hooked(
        root,
        "codex",
        {
            "command": WRITE,
            "sandbox_permissions": "require_escalated",
            "justification": "The marker is outside the writable roots",
        },
        agent_id="thread-sub",
        transcript_path=str(rollout),
    )

    question = parked(root)
    assert question.agent == "thread-sub"
    assert question.account == [
        Account(
            source="justification", text="The marker is outside the writable roots"
        ),
        Account(
            source="preceding",
            text="The marker has to exist before the next step reads it.",
        ),
    ]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_where_the_agent_said_nothing_its_roster_row_stands_in(
    root: Path, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers = RepositoryPeers(root)
    peers.join("asking-member", root)
    peers.describe("asking-member", "Wiring the run marker into the release check")
    monkeypatch.setenv(MEMBER_ENV, "asking-member")

    hooked(root, runtime, {"command": WRITE})

    assert parked(root).account == [
        Account(source="doing", text="Wiring the run marker into the release check")
    ]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_subagent_s_own_roster_row_is_the_one_read(
    root: Path, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers = RepositoryPeers(root)
    peers.join("asking-member", root)
    peers.describe("asking-member", "Leading the release")
    helper = peers.join_subagent(
        "asking-member",
        Caller(
            agent_id="helper",
            agent_type="general-purpose",
            cwd=str(root),
            name="helper",
        ),
    )
    peers.describe(helper.id, "Writing the run marker")
    monkeypatch.setenv(MEMBER_ENV, "asking-member")

    hooked(root, runtime, {"command": WRITE}, agent_id="helper")

    question = parked(root)
    assert question.agent == "helper"
    assert question.account == [Account(source="doing", text="Writing the run marker")]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_nothing_said_anywhere_is_recorded_as_nothing(
    root: Path, runtime: Runtime
) -> None:
    hooked(root, runtime, {"command": WRITE})

    question = parked(root)
    assert question.account == []
    assert question.agent == ""


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_each_command_of_a_parked_line_keeps_its_own_verdict(
    root: Path, runtime: Runtime
) -> None:
    hooked(
        root,
        runtime,
        {
            "command": "ls && git push --delete origin old && git push --force origin feature"
        },
    )

    question = parked(root)
    assert [
        (segment.command, segment.effect) for segment in question.segments or []
    ] == [
        ("ls", "allow"),
        ("git push --delete origin old", "ask"),
        ("git push --force origin feature", "ask"),
    ]
    assert all(
        segment.reason
        for segment in question.segments or []
        if segment.effect != "allow"
    )
    assert question.bound()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_two_edits_waiting_are_told_how_to_ask_once(
    root: Path, runtime: Runtime
) -> None:
    first = hooked(root, runtime, {"command": f"{ESCALATED}echo one > one.txt"})
    second = hooked(root, runtime, {"command": f"{ESCALATED}echo two > two.txt"})

    assert "review propose" not in first
    assert "2 of your edits now wait on the operator" in second
    assert "lup-devtools review propose <directory> --why" in second


def test_a_transcript_is_read_back_across_blocks_and_past_a_torn_line(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(claude_heard("go"))
        + "\n"
        + "".join(
            json.dumps(claude_said(f"line {index} " + "x" * 40)) + "\n"
            for index in range(50)
        )
        + '{"type": "assistant", "mess'
    )

    read = list(records_backwards(transcript, block=64))

    assert len(read) == 51
    assert read[0] == claude_said("line 49 " + "x" * 40)
    said = words_before(
        transcript,
        lambda record: (
            None
            if record["type"] == "user"
            else record["message"]["content"][0]["text"]
        ),
    )
    assert said.split("\n\n") == [f"line {index} " + "x" * 40 for index in range(50)]


def test_the_terminal_shows_the_asker_s_words_and_each_command_that_asks(
    root: Path, tmp_path: Path
) -> None:
    session = lines(
        tmp_path / "session.jsonl",
        [claude_heard("Clean up."), claude_said("Both branches are merged.")],
    )
    hooked(
        root,
        "claude",
        {
            "command": "ls && git push --delete origin old && git push --force origin feature",
            "description": "Drop the merged branch",
        },
        transcript_path=str(session),
    )
    question = parked(root)

    shown = CliRunner().invoke(create_review_app(root), ["show", question.id])

    assert shown.exit_code == 0, shown.output
    assert "  description Drop the merged branch" in shown.output
    assert "  preceding   Both branches are merged." in shown.output
    assert "  ask         git push --delete origin old" in shown.output
    assert "  ask         git push --force origin feature" in shown.output
    assert "  allow       ls" in shown.output


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_parked_record_names_the_parts_its_fingerprint_binds(
    root: Path, runtime: Runtime
) -> None:
    hooked(root, runtime, {"command": WRITE})

    question = parked(root)
    assert question.scheme == ["file_reviews", "unpreviewed", "segments"]
    assert question.unverifiable() == ""
    assert question.bound()


def test_a_record_parked_before_the_scheme_was_kept_still_checks(root: Path) -> None:
    """What a record carries is what the hook that parked it bound."""
    hooked(root, "claude", {"command": WRITE})
    question = parked(root)
    operation = question.operation
    legacy = question.model_dump(mode="json", exclude={"scheme", "segments"})
    legacy["fingerprint"] = review_fingerprint(
        operation.session,
        str(operation.cwd),
        operation.tool,
        operation.payload,
        {str(path): before for path, before in question.preconditions.items()},
        question.reason,
        question.rule,
        question.purpose or "",
        question.requirement,
        question.execution_payload
        if question.execution_payload is not None
        else operation.payload,
        question.policy_identity,
        {str(path): str(landed) for path, landed in question.resolved.items()},
        {
            "file_reviews": legacy["file_reviews"],
            "unpreviewed": legacy["unpreviewed"],
        },
    )
    QuestionRelay(root / ".lup/questions.jsonl").record(
        PersistentQuestion.model_validate(legacy)
    )

    older = parked(root)
    assert older.scheme is None and older.segments is None
    assert older.unverifiable() == ""
    assert older.bound()


def test_a_record_from_a_newer_hook_is_named_so_and_never_called_changed(
    root: Path,
) -> None:
    hooked(root, "claude", {"command": WRITE})
    relay = QuestionRelay(root / ".lup/questions.jsonl")
    newer = relay.record(
        parked(root).model_copy(
            update={"scheme": ["file_reviews", "unpreviewed", "segments", "later"]}
        )
    )

    assert "runs older code than the hook that parked this review" in (
        newer.unverifiable()
    )
    assert not newer.bound()
    with pytest.raises(ValueError, match="runs older code") as refused:
        relay.answer(newer.id, "operator", True)
    assert "changed after it was parked" not in str(refused.value)
    summary = ReviewDetail.of(root, newer, "operator").summary
    assert not summary.answerable
    assert summary.unanswerable.startswith("This dashboard runs older code")


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_checker_that_only_reads_is_no_step_only_running_shows(
    root: Path, runtime: Runtime
) -> None:
    """Its output goes to a stream, and a reader writes nothing only running would show."""
    (root / "notes.md").write_text("notes\n")
    checked = "uv run pyright tmp/restart/refresh.py 2>&1"
    hooked(root, runtime, {"command": f"{checked} && cp notes.md .claude/notes.md"})

    question = parked(root)
    assert question.unpreviewed == []
    assert [
        (segment.command, segment.effect) for segment in question.segments or []
    ] == [(checked, "allow"), ("cp notes.md .claude/notes.md", "ask")]


@pytest.mark.parametrize(
    "command",
    [
        "uv run ruff format src/app.py",
        "uv run pyright --createstub requests",
        "uv run pyright src > report.txt",
    ],
)
def test_what_may_write_unforeseen_stays_a_step_only_running_shows(
    root: Path, command: str
) -> None:
    (root / "notes.md").write_text("notes\n")
    hooked(root, "claude", {"command": f"{command} && cp notes.md .claude/notes.md"})

    question = parked(root)
    assert [step.command for step in question.unpreviewed or []] == [command]
