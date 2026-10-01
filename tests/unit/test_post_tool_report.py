"""What a finished call is told, through each runtime's two channels.

After a write, some findings ask the agent to act — a gate refuses what
landed — and some only inform it: a directive the sweep removed, a name an
edit still to come is about to supply, another repository's referral. One
channel for both labelled every notice a blocking error, so an agent could
not tell whether anything was asked of it; these pin the split, and the
reviews that feed it.
"""

import json
from pathlib import Path
from types import ModuleType

import pytest

from tests.unit.bundled import bundled
from tests.unit.repos import initialized_repo


def claude() -> ModuleType:
    return bundled(
        "bundled_claude_policy", Path(".claude/plugins/lup/hooks/scripts/policy.py")
    )


def codex() -> ModuleType:
    return bundled(
        "bundled_codex_policy",
        Path.cwd() / ".codex/plugins/lup/hooks/scripts/policy.py",
    )


def test_a_refusal_blocks_and_what_is_worth_knowing_rides_beside_it() -> None:
    answer = claude().post_tool_answer(
        {"blocking": ["module.py:3: error: wrong"], "context": ["line 2: removed"]}
    )

    assert answer == {
        "decision": "block",
        "reason": "module.py:3: error: wrong",
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "line 2: removed",
        },
    }


def test_what_is_only_worth_knowing_blocks_nothing() -> None:
    answer = claude().post_tool_answer({"blocking": [], "context": ["line 2: removed"]})

    assert "decision" not in answer
    assert answer["hookSpecificOutput"]["additionalContext"] == "line 2: removed"


def test_codex_hears_context_without_the_tool_result_replaced(
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex().post_tool_answer({"blocking": [], "context": ["line 2: removed"]})

    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["hookSpecificOutput"] == {
        "hookEventName": "PostToolUse",
        "additionalContext": "line 2: removed",
    }


def test_codex_hears_a_refusal_on_its_measured_channel_with_the_context_after(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        codex().post_tool_answer(
            {"blocking": ["module.py:3: wrong"], "context": ["line 2: removed"]}
        )

    assert exited.value.code == 2
    assert capsys.readouterr().err == "module.py:3: wrong\n\nline 2: removed"


def test_the_claude_hook_emits_the_split_it_was_handed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    dispatcher = claude()
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {}}
    monkeypatch.setattr(
        dispatcher,
        "observe",
        lambda _payload: {"blocking": [], "context": ["worth knowing"]},
    )
    monkeypatch.setattr(dispatcher, "plugin_data_root", lambda: tmp_path)

    dispatcher.judged(json.dumps(payload).encode())

    assert json.loads(capsys.readouterr().out) == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "worth knowing",
        }
    }


def sweeping(work: Path, rewritten: str, rule_id: str) -> None:
    """A stand-in sweep that takes one directive out of `module.py`."""
    script = work / "lup-devtools"
    report = {
        "repaired": [{"file": "module.py", "line": 1, "rule_id": rule_id}],
        "findings": [],
    }
    script.write_text(
        f"#!/bin/sh\nprintf '%s' '{rewritten}' > module.py\n"
        f"cat <<'JSON'\n{json.dumps(report)}\nJSON\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def test_a_repair_the_loaded_policy_would_refuse_is_put_back(tmp_path: Path) -> None:
    """The sweep judges by the checkout's rules, the gate by the loaded policy.

    After a rename the two disagreed: the gate demanded a directive the sweep
    deleted as dead, and every later edit to the file was refused for its
    absence. Where the loaded policy still needs what was taken out, the file
    is left as written and the disagreement is said.
    """
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "no-hooks")
    written = "import subprocess  # lup: ignore[subprocess] — a fixture\n"
    module = work / "module.py"
    module.write_text(written, encoding="utf-8")
    sweeping(work, "import subprocess\n", "subprocess")

    report = claude().reviewed_writes([str(module)], work, diagnosed=False)

    assert module.read_text(encoding="utf-8") == written
    assert report["blocking"] == []
    assert "left as written" in report["context"][0]


def test_a_repair_the_loaded_policy_agrees_with_stands_and_is_said(
    tmp_path: Path,
) -> None:
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "no-hooks")
    module = work / "module.py"
    module.write_text(
        "value = 1  # lup: ignore[subprocess] — stale\n", encoding="utf-8"
    )
    sweeping(work, "value = 1\n", "subprocess")

    report = claude().reviewed_writes([str(module)], work, diagnosed=False)

    assert module.read_text(encoding="utf-8") == "value = 1\n"
    assert report["context"] == [
        "module.py: line 1: removed `# lup: ignore[subprocess]` — it guarded no"
        " rule, so it silenced nothing"
    ]


def test_another_repositorys_referral_is_said_in_full_once_per_session(
    tmp_path: Path,
) -> None:
    """The verdict stands on every edit; the paragraph about it, the first time."""
    dispatcher = claude()
    work, other = tmp_path / "repo", tmp_path / "other"
    initialized_repo(work, tmp_path / "no-hooks")
    initialized_repo(other, tmp_path / "no-hooks")
    target = str(other / "notes.py")
    referral = dispatcher.KernelDecision(
        "ask",
        "this file belongs to a different repository",
        recovery="That repository's conventions are its own.",
        rule="edit:foreign-repository",
    )

    first = dispatcher.referred_once(referral, target, work, "session-a")
    again = dispatcher.referred_once(referral, target, work, "session-a")
    elsewhere = dispatcher.referred_once(referral, target, work, "session-b")

    assert first.recovery
    assert again.effect == "ask"
    assert again.recovery == ""
    assert elsewhere.recovery


def test_a_file_a_command_wrote_without_naming_it_is_reviewed(tmp_path: Path) -> None:
    """A script names none of the files it writes, and was reviewed for none.

    The claim window measured what moved across the command, so a file only
    it knows about is put to the same gates as a redirect's target.
    """
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "no-hooks")
    module = work / "module.py"
    module.write_text("import subprocess\n", encoding="utf-8")
    (work / "lup-devtools").write_text(
        '#!/bin/sh\necho \'{"repaired": [], "findings": []}\'\n', encoding="utf-8"
    )

    report = claude().written_review(
        "uv run python tmp/generate.py", work, [str(module)], "session"
    )

    assert any(
        line.startswith("module.py: line 1:") and "(rule subprocess)" in line
        for line in report["blocking"]
    )


def test_a_file_a_generator_rewrote_is_not_reported_for_where_it_sits(
    tmp_path: Path,
) -> None:
    """What the window saw move is put to the rule scan, not to the path gates.

    A generator rewrites its own trees, and the path gates refuse editing one
    by hand: read against them, every regeneration would come back refused.
    """
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "no-hooks")
    generated = work / ".claude/plugins/lup/hooks/scripts/policy.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("value = 1\n", encoding="utf-8")
    (work / "lup-devtools").write_text(
        '#!/bin/sh\necho \'{"repaired": [], "findings": []}\'\n', encoding="utf-8"
    )
    (work / "lup-devtools").chmod(0o755)

    report = claude().written_review(
        "uv run lup-devtools harness generate all", work, [str(generated)], "session"
    )

    assert report == {"blocking": [], "context": []}
