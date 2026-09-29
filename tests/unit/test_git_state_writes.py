"""Git's own pointers and refs are written by git, and by nothing else.

A linked worktree lives between three pointer files: its `.git` names an
entry under the shared directory, and the entry's `commondir` and `gitdir`
name the shared directory and the way back. Host git follows them to the
config it reads, so a session that rewrites one chooses the `core.hooksPath`
the operator's next git command runs. The refs are the same kind of file for
commits. None of them can be a read-only mount, because `git worktree remove`
unlinks exactly these, so they are recognized by what they are: a native edit
is refused by the edit gate, and a shell write by the read-only-write row,
whichever verb spells it. git's own worktree commands keep their work.

Every surface is driven the way a session drives it: each runtime's generated
dispatcher on the payload its harness sends, and `dev policy`'s own reading.
"""

import json
import os
import sys
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.devtools.dev.policy_explain import verdict_for
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.native import claude_effect, codex_effect
from tests.unit.repos import commit_file, initialized_repo

type Runtime = Literal["claude", "codex"]

DISPATCHERS: dict[Runtime, Path] = {
    "claude": Path(".claude/plugins/lup/hooks/scripts/policy.py"),
    "codex": Path(".codex/plugins/lup/hooks/scripts/policy.py"),
}


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repository with one linked worktree beside it, as `tree/` holds them."""
    work = tmp_path / "checkout"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "README.md", "hello\n", "initial")
    git("worktree", "add", "-b", "wt", str(tmp_path / "wt"))
    (work / "tmp").mkdir()
    (work / "tmp" / "forged").write_text("/tmp/evil\n", encoding="utf-8")
    return work


def met(runtime: Runtime, name: str, tool_input: JsonObject, checkout: Path) -> str:
    """The effect a session meets before the call runs.

    Codex has no ask at this boundary: it parks the question as a review and
    answers with a structured refusal on stdout, so only exit 2 is a refusal.
    """
    payload: JsonObject = {
        "session_id": "git-state-probe",
        "hook_event_name": "PreToolUse",
        "cwd": str(checkout),
        "tool_name": name,
        "tool_input": tool_input,
    }
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(DISPATCHERS[runtime].resolve()),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={**os.environ, "PLUGIN_DATA": str(checkout.parent / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    if runtime == "codex":
        return (
            "ask" if result.exit_code == 0 and result.stdout else codex_effect(result)
        )
    if result.exit_code == 2:
        return "deny"
    return claude_effect(json.loads(str(result)))


def edited(runtime: Runtime, path: str, checkout: Path) -> str:
    """What a native whole-file write of this checkout-relative path meets."""
    if runtime == "codex":
        patch = f"*** Begin Patch\n*** Add File: {path}\n+forged\n*** End Patch"
        return met(runtime, "apply_patch", {"command": patch}, checkout)
    written: JsonObject = {"file_path": str(checkout / path), "content": "forged\n"}
    return met(runtime, "Write", written, checkout)


def previewed(subject: str, kind: str, checkout: Path) -> set[str]:
    """What `dev policy` answers, under every placement it reads."""
    verdict = verdict_for(subject, kind, False, checkout, declared_hook_set())
    return {reading.effect for reading in verdict.readings}


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(".git/worktrees/wt/commondir", id="commondir"),
        pytest.param(".git/worktrees/wt/gitdir", id="gitdir"),
        pytest.param(".git/worktrees/wt/HEAD", id="entry-head"),
        pytest.param(".git/refs/heads/forged", id="ref"),
        pytest.param(".git/packed-refs", id="packed-refs"),
        pytest.param(".git/ORIG_HEAD", id="orig-head"),
        pytest.param(".git/config.worktree", id="config-worktree"),
    ],
)
def test_a_native_edit_of_a_pointer_or_ref_is_refused(
    runtime: Runtime, checkout: Path, path: str
) -> None:
    assert edited(runtime, path, checkout) == "deny"
    assert previewed(path, "edit", checkout) == {"deny"}


def test_a_native_edit_of_git_s_own_ignore_list_is_not_a_pointer(
    runtime: Runtime, checkout: Path
) -> None:
    """`info/exclude` names no repository, commit or config, so it stays an edit."""
    assert edited(runtime, ".git/info/exclude", checkout) != "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("echo 'gitdir: /tmp/evil' > ../wt/.git", id="pointer-redirect"),
        pytest.param("ln -sf /tmp/evil ../wt/.git", id="pointer-link"),
        pytest.param("cp tmp/forged .git/worktrees/wt/commondir", id="commondir-cp"),
        pytest.param("tee .git/worktrees/wt/gitdir", id="gitdir-tee"),
        pytest.param("sed -i s/wt/x/ .git/worktrees/wt/gitdir", id="gitdir-sed"),
        pytest.param("mv .git/worktrees/wt tmp/moved", id="entry-rename"),
        pytest.param("mkdir .git/worktrees/rebuilt", id="entry-recreate"),
        pytest.param("sort -o ../wt/.git tmp/forged", id="pointer-flag"),
    ],
)
def test_a_shell_write_to_a_pointer_is_refused(
    runtime: Runtime, checkout: Path, command: str
) -> None:
    assert met(runtime, "Bash", {"command": command}, checkout) == "deny"
    assert previewed(command, "shell", checkout) == {"deny"}


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("truncate -s0 .git/refs/heads/main", id="ref-truncate"),
        pytest.param("sort -o .git/ORIG_HEAD tmp/forged", id="pseudo-ref-flag"),
    ],
)
def test_a_shell_write_to_a_ref_keeps_its_question(
    runtime: Runtime, checkout: Path, command: str
) -> None:
    """A ref names a commit rather than a config, and a person may still move one."""
    assert met(runtime, "Bash", {"command": command}, checkout) == "ask"
    assert previewed(command, "shell", checkout) == {"ask"}


def test_a_ref_written_with_its_content_meets_the_edit_gate(
    runtime: Runtime, checkout: Path
) -> None:
    """A command carrying the bytes it writes is an edit, and meets the edit's refusal."""
    command = "echo 'ref: refs/heads/forged' > .git/HEAD"

    assert met(runtime, "Bash", {"command": command}, checkout) == "deny"
    assert previewed(command, "shell", checkout) == {"deny"}


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git worktree add ../other", id="add"),
        pytest.param("git worktree move ../wt ../moved", id="move"),
        pytest.param("git worktree list", id="list"),
        pytest.param("cat ../wt/.git .git/worktrees/wt/commondir", id="read"),
    ],
)
def test_git_s_own_worktree_commands_and_reads_keep_their_work(
    runtime: Runtime, checkout: Path, command: str
) -> None:
    assert met(runtime, "Bash", {"command": command}, checkout) == "allow"
    assert previewed(command, "shell", checkout) == {"allow"}
