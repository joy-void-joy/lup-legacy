"""Native transcript ingestion: scope, cursors, and per-runtime vocabulary."""

from pathlib import Path

from lup.providers.claude.transcripts import ClaudeTranscripts
from lup.providers.codex.transcripts import CodexTranscripts
from lup.observability.audit import (
    TraceActor,
    TraceContext,
    TraceJournal,
    read_observable_events,
)
from lup.observability.native import NativeTranscriptWatcher
from lup.types import JsonObject


def journal_at(path: Path) -> TraceJournal:
    return TraceJournal(
        path,
        TraceContext.root("run-1", TraceActor(kind="harness", name="native")),
    )


def test_each_runtime_reads_a_shared_word_in_its_own_vocabulary() -> None:
    # The reason the two tables are not merged: `reasoning` is a block Codex
    # emits, and a merged table would read it the same way in either record.
    reasoning: JsonObject = {"type": "reasoning", "text": "considering"}

    assert CodexTranscripts().semantic_blocks(reasoning) != []
    assert ClaudeTranscripts().semantic_blocks(reasoning) == []


def test_each_runtime_recognizes_its_own_tool_call_spelling() -> None:
    claude_call: JsonObject = {"type": "tool_use", "name": "Read"}
    codex_call: JsonObject = {"type": "function_call", "name": "read"}

    assert [
        block.kind for block in ClaudeTranscripts().semantic_blocks(claude_call)
    ] == ["tool_call"]
    assert [block.kind for block in CodexTranscripts().semantic_blocks(codex_call)] == [
        "tool_call"
    ]
    assert ClaudeTranscripts().semantic_blocks(codex_call) == []


def test_a_nested_block_is_found_without_naming_its_envelope() -> None:
    record: JsonObject = {
        "payload": {"content": [{"type": "tool_use", "name": "Read"}]}
    }

    assert [block.kind for block in ClaudeTranscripts().semantic_blocks(record)] == [
        "tool_call"
    ]


def test_codex_finds_a_cwd_carried_inside_session_meta() -> None:
    record: JsonObject = {"type": "session_meta", "payload": {"cwd": "/work/project"}}

    assert CodexTranscripts().belongs_to(record).directory == Path("/work/project")


def test_a_record_naming_no_directory_has_no_origin() -> None:
    record: JsonObject = {"type": "user"}

    assert ClaudeTranscripts().belongs_to(record).directory is None


def test_only_bytes_written_after_the_snapshot_are_ingested(tmp_path: Path) -> None:
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    transcript = sessions / "session.jsonl"
    transcript.write_text('{"type":"user","cwd":"/work"}\n', encoding="utf-8")

    watcher = NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"), journal_at(tmp_path / "journal.jsonl")
    )
    watcher.snapshot()
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write('{"type":"tool_use","name":"Read","cwd":"/work"}\n')
    watcher.scan()

    kinds = [event.kind for event in read_observable_events(tmp_path / "journal.jsonl")]
    assert kinds == ["message", "tool_call"]


def test_a_transcript_outside_the_scope_is_not_mirrored(tmp_path: Path) -> None:
    # The roots hold every project's sessions, so an unscoped watcher would
    # pull a concurrent unrelated session into this project's record.
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    (sessions / "elsewhere.jsonl").write_text(
        '{"type":"tool_use","name":"Read","cwd":"/other/project"}\n', encoding="utf-8"
    )

    watcher = NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"),
        journal_at(tmp_path / "journal.jsonl"),
        scope=Path("/work"),
    )
    watcher.scan()

    assert read_observable_events(tmp_path / "journal.jsonl") == []


def test_each_runtime_reads_its_own_spelling_of_the_session() -> None:
    """Claude Code spells it camelCase and Codex snake case, one payload each."""
    claude: JsonObject = {"type": "user", "sessionId": "abc"}
    codex: JsonObject = {"type": "session_meta", "payload": {"session_id": "xyz"}}

    assert ClaudeTranscripts().belongs_to(claude).session == "abc"
    assert CodexTranscripts().belongs_to(codex).session == "xyz"
    assert ClaudeTranscripts().belongs_to(codex).session is None


def test_a_session_stays_recorded_after_it_changes_directory(tmp_path: Path) -> None:
    """A directory change opens a fresh transcript that stays in scope.

    A native CLI keys a transcript to the directory a session started in while
    stamping the directory on every record, so entering a worktree opens a new
    transcript under a different project root. Judged per record, every line of
    it would fall outside the launching project and recording would stop
    mid-session.
    """
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    (sessions / "aaa-in-scope.jsonl").write_text(
        '{"type":"user","sessionId":"s1","cwd":"/work/project"}\n', encoding="utf-8"
    )
    (sessions / "zzz-after-cd.jsonl").write_text(
        '{"type":"tool_use","name":"Read","sessionId":"s1","cwd":"/elsewhere/tree"}\n',
        encoding="utf-8",
    )
    journal_path = tmp_path / "journal.jsonl"

    NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"),
        journal_at(journal_path),
        scope=Path("/work/project"),
    ).scan()

    kinds = [event.kind for event in read_observable_events(journal_path)]
    assert kinds == ["message", "message", "tool_call"]


def test_a_session_first_seen_outside_the_scope_is_never_adopted(
    tmp_path: Path,
) -> None:
    """Following a session must not become a way in for another launch's work."""
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    (sessions / "theirs.jsonl").write_text(
        '{"type":"user","sessionId":"other","cwd":"/other/project"}\n'
        '{"type":"tool_use","name":"Read","sessionId":"other","cwd":"/work/project"}\n',
        encoding="utf-8",
    )
    journal_path = tmp_path / "journal.jsonl"
    watcher = NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"),
        journal_at(journal_path),
        scope=Path("/work/project"),
    )

    watcher.scan()

    assert watcher.sessions == {"other": "foreign"}
    assert read_observable_events(journal_path) == []


def test_a_foreign_session_stepping_into_the_scope_stays_out(tmp_path: Path) -> None:
    """Following a session is a door, and this is the lock on it.

    A concurrent launch's session that later works inside this project would
    otherwise hand over its whole remaining transcript on the strength of one
    in-scope record — the failure scoping exists to prevent, arriving through
    the mechanism that keeps our own session recorded across a directory change.
    """
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    (sessions / "theirs.jsonl").write_text(
        '{"type":"user","sessionId":"other","cwd":"/other/project"}\n', encoding="utf-8"
    )
    journal_path = tmp_path / "journal.jsonl"
    watcher = NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"),
        journal_at(journal_path),
        scope=Path("/work/project"),
    )
    watcher.scan()

    with (sessions / "theirs.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            '{"type":"tool_use","name":"Read","sessionId":"other",'
            '"cwd":"/work/project"}\n'
        )
    watcher.scan()

    assert read_observable_events(journal_path) == []


def test_an_ingested_event_carries_the_session_it_came_from(tmp_path: Path) -> None:
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(
        '{"type":"user","sessionId":"s1","cwd":"/work"}\n', encoding="utf-8"
    )
    journal_path = tmp_path / "journal.jsonl"

    NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"), journal_at(journal_path)
    ).scan()

    sessions_seen = [event.session_id for event in read_observable_events(journal_path)]
    assert sessions_seen == ["s1"]


def test_one_unreadable_transcript_does_not_end_the_pass(tmp_path: Path) -> None:
    """A CLI rotates its own files, so a listed transcript can vanish mid-pass.

    Letting that raise out of the whole scan let an unrelated project's churn
    stop this project's recording.
    """
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    (sessions / "aaa-vanishes.jsonl").write_text("{}\n", encoding="utf-8")
    (sessions / "zzz-survives.jsonl").write_text(
        '{"type":"user","cwd":"/work"}\n', encoding="utf-8"
    )
    journal_path = tmp_path / "journal.jsonl"
    watcher = NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"), journal_at(journal_path)
    )
    listed = watcher.discover()
    (sessions / "aaa-vanishes.jsonl").unlink()
    watcher.discover = lambda: listed

    watcher.scan()

    assert len(read_observable_events(journal_path)) == 1


def test_a_partial_line_waits_for_the_rest_but_lands_on_the_final_scan(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text('{"type":"user"}', encoding="utf-8")
    journal_path = tmp_path / "journal.jsonl"

    watcher = NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"), journal_at(journal_path)
    )
    watcher.scan()
    assert read_observable_events(journal_path) == []

    watcher.scan(final=True)
    assert len(read_observable_events(journal_path)) == 1
