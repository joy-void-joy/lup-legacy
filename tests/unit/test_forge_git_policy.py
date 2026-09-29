"""Git and forge collaboration, answered alike by every surface that judges it.

What a session may do to a shared repository was settled per spelling rather
than per verb: pushing, merging, and opening, editing or merging a pull
request allow, however they are reached; a force allows only under a lease
onto a named feature branch; deleting a remote branch, a request against a
repository nobody declared, and filing an issue ask. Each row below is one
spelling of one of those decisions, and it is judged three times -- by the
reading ``dev policy`` gives, by the compiled Claude dispatcher, and by the
compiled Codex one -- because a decision one of them answered differently
would be two policies, and the one a session meets is the compiled one.

An ask carries a reason, and the reason is what the person approving reads:
one line, saying what is at stake. The fragment beside each asking row is the
part of it that says so.
"""

import json
import os
from pathlib import Path

import pytest
import sh

from lup.devtools.dev.policy_explain import verdict_for
from lup_template.harness.catalog import declared_hook_set
from tests.unit.native import claude_effect, codex_denial, codex_effect
from tests.unit.repos import commit_file, initialized_repo

CLAUDE = Path(".claude/plugins/lup/hooks/scripts/policy.py")
CODEX = Path(".codex/plugins/lup/hooks/scripts/policy.py")

DECISIONS = [
    # Publishing and landing work, by every spelling that reaches it.
    ("git push origin feat", "allow", ""),
    ("git push -u origin HEAD", "allow", ""),
    ("git merge feat", "allow", ""),
    ("git merge --no-ff feat", "allow", ""),
    ("gh pr create --title t --body b", "allow", ""),
    ("gh pr edit 3 --title t", "allow", ""),
    ("gh pr merge 3 --squash", "allow", ""),
    ("uv run lup-devtools git pr create --title t --body b", "allow", ""),
    ("uv run lup-devtools git pr update 3 --title t", "allow", ""),
    ("uv run lup-devtools git pr merge 3", "allow", ""),
    ("uv run lup-devtools git pr push", "allow", ""),
    ("gh api repos/{owner}/{repo}/pulls -f title=t -f head=a -f base=b", "allow", ""),
    ("gh api -X PATCH repos/{owner}/{repo}/pulls/3 -f title=t", "allow", ""),
    ("gh api -X PUT repos/{owner}/{repo}/pulls/3/merge", "allow", ""),
    ("gh api repos/{owner}/{repo}/merges -f base=dev -f head=feat", "allow", ""),
    # A force replaces only what this checkout last saw when it is leased,
    # and names a branch nobody else builds on. The tool's own force is a
    # leased one, and refuses an integration branch itself.
    ("git push --force-with-lease origin feat", "allow", ""),
    ("git push --force-with-lease origin HEAD:feat", "allow", ""),
    ("uv run lup-devtools git pr push --force", "allow", ""),
    ("git push --force-with-lease origin main", "ask", "other people build on"),
    ("git push --force-with-lease origin dev", "ask", "other people build on"),
    ("git push --force-with-lease origin HEAD:main", "ask", "other people build on"),
    ("git push --force-with-lease origin", "ask", "names no branch"),
    ("git push --force origin feat", "ask", "discarding commits someone else"),
    ("git push -f origin feat", "ask", "discarding commits someone else"),
    ("git push origin +feat", "ask", "discarding commits someone else"),
    ("git push --force-with-lease origin +feat", "ask", "discarding commits"),
    # Deleting a remote branch, however it is spelled.
    ("git push --delete origin feat", "ask", "deleting a remote branch"),
    ("git push -d origin feat", "ask", "deleting a remote branch"),
    ("git push origin :feat", "ask", "deleting a remote branch"),
    ("git push --mirror origin", "ask", "deleting a remote branch"),
    ("git push --prune origin", "ask", "deleting a remote branch"),
    ("gh pr close 3 --delete-branch", "ask", "removes work no reopen restores"),
    ("uv run lup-devtools git delete feat --remote", "ask", "origin's copy"),
    (
        "gh api -X DELETE repos/{owner}/{repo}/git/refs/heads/feat",
        "ask",
        "deleting a remote branch",
    ),
    # A request against a repository nobody declared.
    ("gh pr create --repo other/x --title t --body b", "ask", "another repository"),
    ("gh pr create -R other/x --title t --body b", "ask", "another repository"),
    ("gh api repos/other/x/pulls -f title=t", "ask", "nobody declared"),
    # Filing an issue, by gh or by the friction report that files one; the
    # report pointed at one already filed amends it instead.
    ("gh issue create --title t --body b", "ask", "filing an issue"),
    ("gh api repos/{owner}/{repo}/issues -f title=t", "ask", "filing an issue"),
    (
        "uv run lup-devtools dev report-friction --summary s --component c"
        " --command x --error e --state s --recovery-cost r",
        "ask",
        "opens an issue",
    ),
    (
        "uv run lup-devtools dev report-friction --summary s --component c"
        " --command x --error e --state s --recovery-cost r --issue 7",
        "allow",
        "",
    ),
]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("forge")
    work = root / "repo"
    git = initialized_repo(work, root / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    return work


def payload(root: Path, command: str) -> str:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "requester",
            "turn_id": "turn-one",
            "cwd": str(root),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )


@pytest.mark.parametrize("autonomous", [False, True])
@pytest.mark.parametrize(("command", "effect", "stake"), DECISIONS)
def test_dev_policy_answers_the_decision_in_every_placement(
    repo: Path, command: str, effect: str, stake: str, autonomous: bool
) -> None:
    """Contained or not, and for a person or a self-reviewing identity alike."""
    verdict = verdict_for(command, "shell", autonomous, repo, declared_hook_set())

    for reading in verdict.readings:
        assert reading.effect == effect, (command, reading)
        assert stake in reading.reason, (command, reading.reason)
        assert "\n" not in reading.reason


@pytest.mark.parametrize(("command", "effect", "stake"), DECISIONS)
def test_the_compiled_claude_dispatcher_answers_it_alike(
    repo: Path, command: str, effect: str, stake: str
) -> None:
    answered = json.loads(
        str(sh.Command("python3")("-I", "-S", str(CLAUDE), _in=payload(repo, command)))
    )
    specific = answered["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert claude_effect(answered) == effect, command
    assert stake in str(specific["permissionDecisionReason"])


@pytest.mark.parametrize(("command", "effect", "stake"), DECISIONS)
def test_the_compiled_codex_dispatcher_answers_it_alike(
    repo: Path, tmp_path: Path, command: str, effect: str, stake: str
) -> None:
    """Codex has no prompt a hook can raise, so a question parks as a refusal."""
    result = sh.Command(str(CODEX.resolve()))(
        _in=payload(repo, command),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={**os.environ, "PLUGIN_DATA": str(tmp_path / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    if effect == "allow":
        assert codex_effect(result) == "allow", command
        return
    assert codex_effect(result) == "deny", command
    assert stake in codex_denial(result)
