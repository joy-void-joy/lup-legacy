"""A parked shell command carries every file it writes, as the policy judged it.

The operator answering a question about a shell command reads a diff per file,
the way an `Edit` is read -- which needs the document each file would hold,
and the only honest source of that is the verdict: the policy works out what
a sed, a copy, a move, a heredoc or a patch would leave, in the order the line
leaves it, and judges the edit gates over it. So the question records exactly
that, and nothing is re-derived or run where the review is read. What only
running the command produces is named as that.

Each case is put to the in-process policy and to both generated dispatchers,
over one real repository whose declared protected root makes a write there a
question in every runtime.
"""

import json
import os
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.devtools.review.app import ReviewDetail
from lup.harness.enforcement import semantic_policy_for
from lup.policy.assets.host import document_digest
from lup.policy.models import Decision, ShellCommand
from lup.policy.relay import PersistentQuestion, QuestionRelay
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.native import claude_effect, codex_effect
from tests.unit.repos import commit_file, initialized_repo

type Runtime = Literal["claude", "codex"]

DISPATCHERS: dict[Runtime, Path] = {
    "claude": Path(".claude/plugins/lup/hooks/scripts/policy.py"),
    "codex": Path(".codex/plugins/lup/hooks/scripts/policy.py"),
}

PROTECTED = "packages/lup/src/lup/policy"
"""A root the declaration protects, so a write under it asks in every runtime."""

NOTES = "the old way — ünïcödé\nkeep\n"
"""A document outside ASCII, which the dashboard's own sed once refused to read."""


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repository holding committed files under the protected root, and one outside."""
    root = tmp_path / "checkout"
    git = initialized_repo(root, tmp_path / "no-hooks")
    (root / PROTECTED).mkdir(parents=True)
    (root / "tmp").mkdir()
    for name, body in {
        f"{PROTECTED}/notes.md": NOTES,
        f"{PROTECTED}/extra.md": "extra\n",
        f"{PROTECTED}/gone.md": "bye\n",
        "app.py": "a = 1\n",
    }.items():
        commit_file(git, root, name, body, f"chore: add {name}")
    return root


def judged(root: Path, command: str) -> Decision:
    """What the in-process policy decides about one command run from ``root``."""
    return semantic_policy_for(declared_hook_set(), recovered=True).decide(
        ShellCommand(command=command, cwd=root)
    )


def dispatched(runtime: Runtime, root: Path, command: str) -> sh.RunningCommand:
    """One runtime's generated dispatcher over the command, as its harness sends it."""
    payload: JsonObject = {
        "session_id": "requester",
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "call-one",
    }
    if runtime == "codex":
        payload["turn_id"] = "turn-one"
    result = sh.Command("python3")(
        "-I",
        "-S",
        str(DISPATCHERS[runtime].resolve()),
        _in=json.dumps(payload),
        _cwd=str(root),
        _ok_code=[0, 2],
        _env={
            **{
                name: value
                for name, value in os.environ.items()
                if not name.startswith("LUP_")
            },
            "PLUGIN_DATA": str(root.parent / "plugin-data"),
            "CLAUDE_PLUGIN_DATA": str(root.parent / "plugin-data"),
        },
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    return result


def effect(runtime: Runtime, answer: sh.RunningCommand) -> str:
    """The effect one runtime's answer carries, over whichever channel it took."""
    if runtime == "claude":
        return claude_effect(json.loads(answer.stdout))
    return codex_effect(answer)


def parked(runtime: Runtime, root: Path, command: str) -> PersistentQuestion:
    """The one question a runtime parks for a command that asks."""
    dispatched(runtime, root, command)
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    return question


CHAIN = (
    f"cd {PROTECTED} && sed -i -e 's/old/new/' -e 's/keep/kept/' notes.md"
    " && cp notes.md copy.md && mv extra.md moved.md && rm gone.md"
    " && printf 'x\\n' > made.txt && echo tail >> made.txt"
    " && sort -o sorted.txt notes.md"
)
"""Every modifying class the policy reads, in one line, beside one it cannot."""


def expected_documents(root: Path) -> list[tuple[str, str | None]]:
    """The files the chain leaves other than it found them, in the order it writes them."""
    written = "the new way — ünïcödé\nkept\n"
    return [
        (str(root / PROTECTED / "notes.md"), written),
        (str(root / PROTECTED / "copy.md"), written),
        (str(root / PROTECTED / "moved.md"), "extra\n"),
        (str(root / PROTECTED / "extra.md"), None),
        (str(root / PROTECTED / "gone.md"), None),
        (str(root / PROTECTED / "made.txt"), "x\ntail\n"),
    ]


def test_a_chained_rewrite_is_judged_by_what_the_whole_line_leaves(
    checkout: Path,
) -> None:
    """The second rewrite of one file is read against what the first left.

    Judged per rewrite against the file as it stood, a harmless first sed made
    the second one's document the first one's -- so a suppression the gate
    refuses outright went through unasked as soon as anything rewrote the
    file before it.
    """
    alone = "sed -i '1a x = 1  # noqa' app.py"
    chained = f"sed -i 's/zz/zz/' app.py && {alone}"
    assert judged(checkout, alone).effect == "deny"
    assert judged(checkout, chained).effect == "deny"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_every_runtime_refuses_the_chained_rewrite(
    checkout: Path, runtime: Runtime
) -> None:
    chained = "sed -i 's/zz/zz/' app.py && sed -i '1a x = 1  # noqa' app.py"
    assert effect(runtime, dispatched(runtime, checkout, chained)) == "deny"


def test_each_file_a_questioned_command_writes_carries_its_judged_document(
    checkout: Path,
) -> None:
    decision = judged(checkout, CHAIN)
    assert decision.effect == "ask"
    assert [(row["path"], row["after"]) for row in decision.file_reviews] == (
        expected_documents(checkout)
    )
    assert [
        (row["command"], row["cause"], row["paths"]) for row in decision.unpreviewed
    ] == [
        (
            "sort -o sorted.txt notes.md",
            "run",
            [str(checkout / PROTECTED / "sorted.txt")],
        )
    ]
    assert (checkout / PROTECTED / "notes.md").read_text(encoding="utf-8") == NOTES


def test_a_file_the_policy_allows_on_its_own_is_marked_so(checkout: Path) -> None:
    """Scratch is allowed however it is written, so a reviewer need not read it."""
    decision = judged(
        checkout,
        f"printf 'x\\n' > tmp/scratch.txt && sed -i 's/old/new/' {PROTECTED}/notes.md",
    )
    assert decision.effect == "ask"
    assert {Path(row["path"]).name: row["effect"] for row in decision.file_reviews} == {
        "scratch.txt": "allow",
        "notes.md": "ask",
    }


def test_files_no_gate_read_are_judged_only_when_somebody_is_asked(
    checkout: Path,
) -> None:
    """A copy's destination is shown to a reviewer, and costs nothing otherwise."""
    allowed = judged(checkout, "cp app.py tmp/copy.py")
    assert allowed.effect == "allow"
    assert allowed.file_reviews == ()
    asked = judged(
        checkout, f"cp app.py tmp/copy.py && sed -i 's/old/new/' {PROTECTED}/notes.md"
    )
    assert [Path(row["path"]).name for row in asked.file_reviews] == [
        "copy.py",
        "notes.md",
    ]


def test_a_rewrite_of_scratch_the_line_copied_in_is_not_asked(checkout: Path) -> None:
    """A sed over copies an earlier step put in scratch reads them, and allows.

    Read file by file, the copies were not there when the rewrite was judged,
    so it asked about a file that "no file stands there" -- which the line
    itself had just put there.
    """
    command = (
        f"mkdir -p tmp/amend && cp {PROTECTED}/notes.md {PROTECTED}/extra.md"
        " tmp/amend/ && sed -i -e 's/old/new/' tmp/amend/notes.md tmp/amend/extra.md"
    )
    decision = judged(checkout, command)
    assert decision.effect == "allow", decision.reason
    assert not (checkout / "tmp/amend").exists()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_parked_command_records_each_document_and_binds_each_preimage(
    checkout: Path, runtime: Runtime
) -> None:
    question = parked(runtime, checkout, CHAIN)
    rows = question.file_reviews or []
    assert [(str(row.path), row.after) for row in rows] == expected_documents(checkout)
    for row in rows:
        assert row.path in question.preconditions
        assert document_digest(question.preconditions[row.path]) == row.before_sha256
    assert [(entry.command, entry.cause) for entry in question.unpreviewed or []] == [
        ("sort -o sorted.txt notes.md", "run")
    ]
    assert question.bound()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_patch_is_shown_applied_to_a_copy(checkout: Path, runtime: Runtime) -> None:
    (checkout / "fix.diff").write_text(
        f"--- a/{PROTECTED}/extra.md\n+++ b/{PROTECTED}/extra.md\n"
        "@@ -1 +1 @@\n-extra\n+patched\n",
        encoding="utf-8",
    )
    question = parked(
        runtime,
        checkout,
        f"git apply fix.diff && sed -i 's/old/new/' {PROTECTED}/notes.md",
    )
    assert [(row.path.name, row.after) for row in question.file_reviews or []] == [
        ("extra.md", "patched\n"),
        ("notes.md", "the new way — ünïcödé\nkeep\n"),
    ]
    assert (checkout / PROTECTED / "extra.md").read_text(encoding="utf-8") == "extra\n"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_the_dashboard_shows_what_was_judged_without_running_anything(
    checkout: Path, runtime: Runtime
) -> None:
    question = parked(runtime, checkout, CHAIN)
    (checkout / PROTECTED / "notes.md").write_text("moved on\n", encoding="utf-8")
    detail = ReviewDetail.of(checkout, question, "operator")
    shown = {Path(file.path).name: (file.before, file.after) for file in detail.files}
    assert shown["notes.md"] == (NOTES, "the new way — ünïcödé\nkept\n")
    assert shown["gone.md"] == ("bye\n", None)
    assert shown["copy.md"] == (None, "the new way — ünïcödé\nkept\n")
    assert detail.preview_unavailable == ""
    assert detail.stale_reason
    assert [entry.command for entry in detail.question.unpreviewed or []] == [
        "sort -o sorted.txt notes.md"
    ]
    assert not (checkout / PROTECTED / "sorted.txt").exists()
