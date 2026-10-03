"""Permission hooks: RW/RO enforcement and the notes RO grant.

Includes the logs exclusion: setup_notes' RO grant must cover sessions/
and outputs/ of every version while leaving logs/ invisible to the agent.
"""

from pathlib import Path

import pytest

from lup.policy.hooks import (
    LupHookInput,
    LupHooksConfig,
    create_git_inspection_hook,
    create_permission_hooks,
)
from lup.workspace.notes import setup_notes
from lup.workspace.paths import path_is_under
from lup.types import JsonObject


async def decision_for(
    config: LupHooksConfig,
    tool_name: str,
    tool_input: JsonObject,
) -> str | None:
    match tool_name, tool_input:
        case ("Write" | "Edit" | "Read", {"file_path": str(path)}):
            tool_path = path
        case ("Grep" | "Glob", {"path": str(path)}):
            tool_path = path
        case ("Glob", {"pattern": str(pattern)}):
            from lup.workspace.paths import extract_glob_dir

            tool_path = extract_glob_dir(pattern)
        case _:
            tool_path = ""
    input_data = LupHookInput(
        event="PreToolUse",
        tool_name=tool_name,
        tool_input=tool_input,
        tool_path=tool_path,
    )
    output = await config.pre_tool_use[0].hook(input_data)
    return output.decision


async def test_write_allowed_only_under_rw(tmp_path: Path) -> None:
    rw = tmp_path / "rw"
    ro = tmp_path / "ro"
    rw.mkdir()
    ro.mkdir()
    config = create_permission_hooks([rw], [ro])

    assert await decision_for(config, "Write", {"file_path": str(rw / "f.txt")}) == (
        "allow"
    )
    assert await decision_for(config, "Edit", {"file_path": str(ro / "f.txt")}) == (
        "deny"
    )
    assert (
        await decision_for(
            config, "Write", {"file_path": str(tmp_path / "outside.txt")}
        )
        == "deny"
    )


async def test_read_allowed_under_rw_and_ro_only(tmp_path: Path) -> None:
    rw = tmp_path / "rw"
    ro = tmp_path / "ro"
    rw.mkdir()
    ro.mkdir()
    config = create_permission_hooks([rw], [ro])

    assert await decision_for(config, "Read", {"file_path": str(rw / "a")}) == "allow"
    assert await decision_for(config, "Read", {"file_path": str(ro / "b")}) == "allow"
    assert (
        await decision_for(config, "Read", {"file_path": str(tmp_path / "secret")})
        == "deny"
    )


async def test_glob_requires_a_readable_path(tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    config = create_permission_hooks([], [ro])

    assert await decision_for(config, "Glob", {"pattern": "**/*.md"}) == "deny"
    assert await decision_for(config, "Glob", {"pattern": f"{ro}/**/*.md"}) == "allow"
    assert await decision_for(config, "Grep", {"path": str(ro)}) == "allow"


async def test_other_tools_pass_through(tmp_path: Path) -> None:
    config = create_permission_hooks([tmp_path], [])
    assert await decision_for(config, "WebSearch", {"query": "x"}) == "allow"


async def test_resolver_workers_can_inspect_git_but_cannot_mutate_it() -> None:
    config = create_git_inspection_hook()

    assert await decision_for(config, "Bash", {"command": "git status"}) == "allow"
    assert await decision_for(config, "Bash", {"command": "git commit -am done"}) == (
        "deny"
    )
    assert await decision_for(config, "Bash", {"command": "sh -c 'git commit'"}) == (
        "deny"
    )
    assert await decision_for(config, "Bash", {"command": "echo x > .git/HEAD"}) == (
        "deny"
    )
    assert await decision_for(config, "Edit", {"file_path": ".git"}) == "deny"


@pytest.mark.parametrize(
    ("command", "effect"),
    [
        ("git grep --open-files-in-pager=rm x", "deny"),
        ("git grep --open-files x", "deny"),
        ("git grep -Orm x", "deny"),
        ("git grep -iO x", "deny"),
        ("git diff --output=README.md", "deny"),
        ("git log -p --out README.md", "deny"),
        ("git show --output x HEAD", "deny"),
        ("git grep -n x", "allow"),
        ("git diff --stat", "allow"),
        ("git diff --output-indicator-new=+", "allow"),
        ("git diff -O order.txt", "allow"),
        ("git diff -- --output=x", "allow"),
    ],
)
async def test_resolver_workers_cannot_hand_an_inspection_a_program_or_a_file(
    command: str, effect: str
) -> None:
    """`grep -O` runs a program over its matches and `--output` writes a file.

    Both ride on verbs the hook lets through, so each is caught however git
    would read it: abbreviated, attached, or inside a cluster.
    """
    config = create_git_inspection_hook()

    assert await decision_for(config, "Bash", {"command": command}) == effect


# ---------------------------------------------------------------------------
# Notes RO grant (logs/ stays invisible)
# ---------------------------------------------------------------------------


def test_notes_ro_grant_covers_versions_but_excludes_logs(
    tmp_lup_project: Path,
) -> None:
    old_version = tmp_lup_project / "notes" / "traces" / "0.0.9"
    (old_version / "sessions" / "old-sess").mkdir(parents=True)
    (old_version / "outputs" / "old-task").mkdir(parents=True)
    (old_version / "logs" / "old-sess").mkdir(parents=True)

    notes = setup_notes("sess-1", "task-1")

    assert notes.ro, "RO grant must not be empty"
    assert all(d.name in ("sessions", "outputs") for d in notes.ro)

    assert path_is_under(old_version / "sessions" / "old-sess", notes.ro)
    assert path_is_under(old_version / "outputs" / "old-task", notes.ro)

    assert not path_is_under(old_version / "logs" / "old-sess" / "x.md", notes.ro)
    assert not path_is_under(notes.trace_log, notes.rw + notes.ro)


async def test_permission_hooks_deny_trace_log_access(tmp_lup_project: Path) -> None:
    notes = setup_notes("sess-1", "task-1")
    config = create_permission_hooks(notes.rw, notes.ro)

    assert (
        await decision_for(config, "Read", {"file_path": str(notes.trace_log)})
        == "deny"
    )
    assert (
        await decision_for(
            config, "Read", {"file_path": str(notes.session / "scratch.md")}
        )
        == "allow"
    )
    assert (
        await decision_for(
            config, "Write", {"file_path": str(notes.output / "result.json")}
        )
        == "allow"
    )
