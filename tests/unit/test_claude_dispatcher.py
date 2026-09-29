"""The generated Claude permission dispatcher, executed for real.

The dispatcher is the only thing a live session actually runs, so a defect
there is invisible to every test that exercises the kernel directly. These
run the emitted script on a fresh interpreter with JSON on stdin, the way
the harness invokes it.
"""

import io
import json
import shlex
import sys
from pathlib import Path
from types import ModuleType

import pytest
import sh

from lup.providers.claude.hooks import claude_placed_input
from lup.policy.grants import allowance_grants_environment, write_allowance_grants
from lup.policy.identity import AGENT_IDENTITY_ENV, ConcernAllowance
from lup.policy.kernel.decision import (
    CONTAINED_ESCAPE_NOTICE,
    SANDBOX_ESCAPE_NOTICE,
    SandboxPlacement,
)
from lup.types import EnvVars, JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.bundled import bundled
from tests.unit.held import held_argv, holding
from tests.unit.repos import commit_file, git_in, initialized_repo

DISPATCHER = Path(".claude/plugins/lup/hooks/scripts/policy.py")

SWEEP_SIZE = declared_hook_set().recoverable_target_limit + 3
"""Enough restorable files to read as a sweep, taken from the declared cap.

Written against the same declaration the dispatcher is compiled from rather
than as a number of its own: what these cases pin is that a sweep still asks
however the cap is set, and a literal would instead pin the cap and go quiet
the moment somebody moved it.
"""


def decide(payload: object) -> dict[str, object]:  # lup: ignore[dict-str-payload]
    """Run the generated dispatcher over one hook payload."""
    output = str(
        sh.Command("python3")("-I", "-S", str(DISPATCHER), _in=json.dumps(payload))
    )
    return json.loads(output)


MULTI_SITE = "packages/lup/src/lup/harness/enforcement.py"
"""A production file carrying one preimage at several sites.

Production because the edit gates below are what these tests are about, and
a test root answers to the test-edit guard before any of them is reached."""


def edit_payload(path: str, old: str, new: str, replace_all: bool) -> JsonObject:
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": path,
            "old_string": old,
            "new_string": new,
            "replace_all": replace_all,
        },
    }


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"tool_name": "Bash", "tool_input": {}}]
)
def test_malformed_payload_fails_closed(payload: object) -> None:
    decision = decide(payload)
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "deny"
    assert "could not judge" in str(specific["permissionDecisionReason"])


def test_a_replace_all_edit_is_judged_rather_than_refused() -> None:
    """Every occurrence is spliced, so the rules decide instead of erroring.

    Requiring the preimage to occur exactly once rejected `replace_all`'s own
    semantics, and the rejection surfaced as an approval prompt no rule
    produced — leaving the whole class of multi-site edit ungoverned.
    """
    decision = decide(edit_payload(MULTI_SITE, "PathRoleRow", "RoleRow", True))
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "allow"
    assert specific["permissionDecisionReason"] == "small safe edit"


def test_a_preimage_that_is_absent_is_still_a_malformed_edit() -> None:
    decision = decide(edit_payload(MULTI_SITE, "no-such-text", "x", True))
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "deny"
    assert "does not occur" in str(specific["permissionDecisionReason"])


def test_an_ambiguous_single_edit_still_requires_an_unambiguous_preimage() -> None:
    """Without `replace_all` the exactly-once requirement is the tool's own."""
    decision = decide(edit_payload(MULTI_SITE, "PathRoleRow", "RoleRow", False))
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "deny"
    assert "exactly once" in str(specific["permissionDecisionReason"])


def test_a_declared_test_root_is_not_judged_against_production_conventions() -> None:
    """The role reaches the deployed dispatcher, not just the kernel.

    One identical edit, two roots. In production the conventions decide and
    refuse it. Under a test root they never run — a test's subject is
    production's behaviour rather than its own shape — so the same edit is an
    ordinary small change. The reason is asserted and not only the effect
    because an allow arrived at by some other route would read the same here,
    and the contrast with production is the whole evidence that the role
    resolved rather than the rules having quietly gone missing.
    """
    shared = "from lup.policy.kernel.roles import path_role"
    production = decide(
        edit_payload(
            "packages/lup/src/lup/devtools/dev/antipatterns.py",
            shared,
            "from typing import Any",
            False,
        )
    )
    under_test = decide(
        edit_payload(
            "packages/lup/tests/unit/test_path_roles.py",
            shared,
            "from typing import Any",
            False,
        )
    )
    denied = production["hookSpecificOutput"]
    guarded = under_test["hookSpecificOutput"]
    assert isinstance(denied, dict)
    assert isinstance(guarded, dict)
    assert denied["permissionDecision"] == "deny"
    assert guarded["permissionDecision"] == "allow"
    assert "small safe edit" in str(guarded["permissionDecisionReason"])


def test_an_overwide_suppression_is_placed_rather_than_left_to_the_author(
    tmp_path: Path,
) -> None:
    """The gate rewrites the call instead of making the author budget columns.

    An inline directive whose reason outgrows the line is what pushes an agent
    to shorten an identifier to buy room. The hook moves it onto the line
    above, so the reason survives whole and nobody had to choose.
    """
    reason = "a justification long enough that keeping it inline outgrows the line"
    payload = {
        **edit_payload(
            str(Path("packages/lup/src/lup/devtools/dev/antipatterns.py").resolve()),
            "from lup.devtools.utils import output_json",
            f"from typing import Any  # lup: ignore[any-type] — {reason}",
            False,
        ),
        "cwd": str(tmp_path),
        "session_id": "requester",
    }
    decision = decide(payload)
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    placed = specific["updatedInput"]
    assert isinstance(placed, dict)
    assert placed["new_string"] == (
        f"# lup: ignore[any-type] — {reason}\nfrom typing import Any"
    )


def test_a_suppression_that_fits_is_left_exactly_where_it_was_written() -> None:
    """Placing is a normal form, so what is already canonical is not touched."""
    decision = decide(
        edit_payload(
            "packages/lup/src/lup/devtools/dev/antipatterns.py",
            "from lup.devtools.utils import output_json",
            "from typing import Any  # lup: ignore[any-type] — at a boundary",
            False,
        )
    )

    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert "updatedInput" not in specific


def write_payload(path: str, content: str) -> JsonObject:
    """One Write hook payload, the way a live session sends it."""
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": content}}


def decide_from(
    payload: JsonObject, cwd: Path, held: Path | None = None
) -> dict[str, object]:  # lup: ignore[dict-str-payload]
    """Run the dispatcher from a working directory that is not the repo.

    ``held`` is a ledger directory read through a read-only mount of itself,
    as a contained launch holds it.
    """
    dispatching = ["python3", "-I", "-S", str(DISPATCHER.resolve())]
    if held is not None:
        holding()
        dispatching = held_argv(held, dispatching)
    output = str(
        sh.Command(dispatching[0])(
            *dispatching[1:],
            _in=json.dumps(payload),
            _cwd=str(cwd),
        )
    )
    return json.loads(output)


def test_absolute_paths_resolve_against_their_worktree_not_the_launch_directory() -> (
    None
):
    """A session always sends absolute paths, and may be launched anywhere.

    Every repo-relative rule matches on the relativized path, so anchoring it
    on the working directory decides policy by where the runtime happened to
    start: from a sibling directory nothing matched, which left the role
    relaxations off and — far worse — let a protected path through.

    The reason is what carries the proof on the second path. `x = {}` trips
    the empty-collection rule in production and is refused; under a test
    root the conventions never run, so the same line is an ordinary small
    change. That contrast is the evidence the role resolved rather than the
    rules having quietly gone missing.
    """
    root = Path(".").resolve()
    outside = root.parent
    protected = decide_from(
        write_payload(str(root / "README.md"), "# replaced\n"), outside
    )
    under_test = decide_from(
        write_payload(str(root / "tests" / "unit" / "probe.py"), "x = {}\n"), outside
    )
    asked = protected["hookSpecificOutput"]
    guarded = under_test["hookSpecificOutput"]
    assert isinstance(asked, dict)
    assert isinstance(guarded, dict)
    assert asked["permissionDecision"] == "ask"
    assert guarded["permissionDecision"] == "allow"
    assert "small safe edit" in str(guarded["permissionDecisionReason"])


def bash_payload(command: str) -> JsonObject:
    """One Bash hook payload, the way a live session sends it."""
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def effect_from(command: str, cwd: Path) -> tuple[str, str]:
    """The effect and reason the emitted dispatcher returns for one command."""
    specific = decide_from(bash_payload(command), cwd)["hookSpecificOutput"]
    assert isinstance(specific, dict)
    return str(specific["permissionDecision"]), str(
        specific["permissionDecisionReason"]
    )


@pytest.fixture
def delete_repo(tmp_path: Path) -> Path:
    """A repository holding one committed file of each kind that matters."""
    work = tmp_path / "repo"
    (work / "src").mkdir(parents=True)
    git = initialized_repo(work, tmp_path / "no-hooks")
    for index in range(SWEEP_SIZE):
        (work / "src" / f"file{index}.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "src")
    git("commit", "-m", "chore: base")
    (work / "untracked.py").write_text("value = 2\n", encoding="utf-8")
    return work


@pytest.fixture
def other_repository(tmp_path: Path) -> Path:
    """A checkout of a different repository, holding one file lup would refuse."""
    work = tmp_path / "elsewhere"
    (work / "src").mkdir(parents=True)
    git = initialized_repo(work, tmp_path / "no-hooks")
    (work / "src" / "theirs.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "src")
    git("commit", "-m", "chore: base")
    return work


def foreign_verdict(path: Path, old: str, new: str, cwd: Path) -> tuple[str, str]:
    """One edit judged with the session directory a live session always sends."""
    payload = {
        **edit_payload(str(path), old, new, False),
        "cwd": str(cwd),
    }
    specific = decide_from(payload, cwd)["hookSpecificOutput"]
    assert isinstance(specific, dict)
    return str(specific["permissionDecision"]), str(
        specific["permissionDecisionReason"]
    )


def ownership_context(accessible: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Measure the file grant separately from the policy ownership under test."""
    caller = accessible.parent / "caller"
    initialized_repo(caller, accessible.parent / "caller-hooks")
    ledger = caller / ".lup" / "preflight" / "ownership.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "writable_roots": [str(caller), str(accessible)],
                "read_only_roots": [],
                "destination_policies": [],
            }
        )
    )
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "ownership")
    return caller


def test_another_repositorys_file_is_not_judged_by_this_projects_conventions(
    other_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect #206 and #188 describe, at the dispatcher a session runs.

    `Any` is a production denial here. Applied to a checkout that never
    adopted these conventions it produced dozens of refusals naming lup rules,
    and the only ways through were to restyle somebody else's code inside an
    unrelated diff or to write a suppression directive into a repository with
    no rule checker to read it.
    """
    effect, reason = foreign_verdict(
        other_repository / "src" / "theirs.py",
        "value = 1",
        "from typing import Any",
        ownership_context(other_repository, monkeypatch),
    )

    assert effect == "ask"
    assert "different repository" in reason
    assert "Any" not in reason


def test_this_projects_own_file_is_still_judged(other_repository: Path) -> None:
    """The half that must not move. Lifting the gates on a guess would silence
    them here, which costs more than friction in somebody else's tree."""
    decision = decide(
        edit_payload(
            "packages/lup/src/lup/devtools/dev/antipatterns.py",
            "from lup.policy.kernel.roles import path_role",
            "from typing import Any",
            False,
        )
    )
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "deny"


def test_a_sibling_worktree_of_this_repository_is_not_foreign() -> None:
    """The discriminator is the repository, never the checkout.

    Most work here happens in a worktree, and comparing checkout roots would
    lift every rule the moment a session edited a file one directory
    sideways -- in this repository's own code.
    """
    judged = "packages/lup/src/lup/devtools/dev/antipatterns.py"
    siblings = [
        Path(line.removeprefix("worktree "))
        for line in str(
            sh.Command("git")("worktree", "list", "--porcelain")
        ).splitlines()
        if line.startswith("worktree ")
    ]
    # A sibling that actually carries the file, because an absent preimage
    # asks for its own reasons and would read here as the gate having fired.
    # The bare repository is in this listing too and carries no checkout.
    elsewhere = next(
        (
            path
            for path in siblings
            if path != Path.cwd().resolve() and (path / judged).exists()
        ),
        None,
    )
    if elsewhere is None:
        pytest.skip("this checkout has no sibling worktree carrying the judged file")
    effect, _reason = foreign_verdict(
        elsewhere / judged,
        "from lup.policy.kernel.roles import path_role",
        "from typing import Any",
        Path.cwd(),
    )

    assert effect == "deny"


NOTE_SITE = "packages/lup/src/lup/devtools/dev/antipatterns.py"
"""A production file of this repository carrying one unique preimage."""

NOTE_PREIMAGE = "from lup.policy.kernel.roles import path_role"

ADDED_NOTE = "# lup: write down what this leaves open"
"""The note these cases put to the gate.

A string literal rather than a comment, which is what keeps it a subject
under test instead of an open note this file owes work on.
"""


def note_verdict(path: str, cwd: Path) -> tuple[str, str]:
    """One edit that leaves a review note on *path*, judged from a session cwd."""
    payload = {
        **edit_payload(path, NOTE_PREIMAGE, f"{NOTE_PREIMAGE}\n{ADDED_NOTE}", False),
        "cwd": str(cwd),
    }
    specific = decide_from(payload, cwd)["hookSpecificOutput"]
    assert isinstance(specific, dict)
    return str(specific["permissionDecision"]), str(
        specific["permissionDecisionReason"]
    )


@pytest.fixture
def unversioned_directory(tmp_path: Path) -> Path:
    """A tree belonging to no repository at all, holding one ordinary file."""
    work = tmp_path / "notes"
    work.mkdir()
    (work / "scratch.py").write_text(f"{NOTE_PREIMAGE}\n", encoding="utf-8")
    return work


def test_a_note_written_outside_every_repository_is_not_this_projects_feedback(
    unversioned_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `# lup:` marker is this project's review instrument, not a syntax.

    Nothing of ours walks a tree outside the repository — `dev check` and
    `dev comments` read this checkout — so a note left there has no reader to
    protect and no pass that could ever check a claim against it. Judging it
    means an agent cannot write down a probe *of* this policy anywhere the
    policy is not, which is the one place such a probe belongs.
    """
    caller = ownership_context(unversioned_directory, monkeypatch)
    effect, reason = note_verdict(str(unversioned_directory / "scratch.py"), caller)

    assert effect == "allow"
    assert "feedback" not in reason


def test_a_note_in_this_repositorys_own_file_is_judged_however_it_is_spelled() -> None:
    """The half that must not move, in both spellings a session sends.

    A session names files absolutely and a shell command names them relative
    to where it runs, and the same file has to answer the same way through
    either — otherwise the scoping above is an evasion: address a governed
    file by the spelling that misses, and the gate goes quiet on this
    repository's own code.
    """
    absolute = note_verdict(str(Path.cwd().resolve() / NOTE_SITE), Path.cwd())
    relative = note_verdict(NOTE_SITE, Path.cwd())

    assert absolute == relative
    assert absolute[0] == "ask"
    assert "inline review feedback" in absolute[1]


def undo_refs(work: Path) -> list[str]:
    """Every snapshot this checkout holds, read the way a human would find them."""
    return [
        line
        for line in str(
            sh.Command("git")(
                "-C", str(work), "for-each-ref", "--format=%(subject)", "refs/lup/undo"
            )
        ).splitlines()
        if line
    ]


def snapshotting_effect(command: str, cwd: Path) -> tuple[str, str]:
    """One command judged with the ``cwd`` a live session always sends.

    The other cases here leave it out and are answered correctly anyway,
    because the runtime spawns a hook with the session's own directory and
    the filesystem questions resolve against it either way. The snapshot
    cannot take that route: it *writes*, and a writer that guessed at its
    tree from the process it happened to be started in would put refs
    somewhere nobody asked. So it takes no snapshot at all without being told
    where, and these cases have to say.
    """
    payload = {**bash_payload(command), "cwd": str(cwd)}
    specific = decide_from(payload, cwd)["hookSpecificOutput"]
    assert isinstance(specific, dict)
    return str(specific["permissionDecision"]), str(
        specific["permissionDecisionReason"]
    )


def test_a_command_that_could_destroy_work_is_snapshotted_first(
    delete_repo: Path,
) -> None:
    """The recoverability the relaxed lattice rests on, taken by the dispatcher.

    Nothing else stands in front of a command, so this only passes when the
    emitted script wrote the snapshot itself -- a devtools subprocess would
    cost an interpreter start on every mutating command and be the first thing
    somebody turned off. The capture is taken before the verdict reads it,
    which is what lets the verdict be a permission: the loss the question was
    protecting against has already been established not to happen.
    """
    effect, reason = snapshotting_effect("rm untracked.py", delete_repo)

    assert effect == "allow"
    assert "captured and restorable" in reason
    assert undo_refs(delete_repo) == ["lup undo: rm untracked.py"]


def test_a_question_a_capture_cannot_settle_stays_silent_about_it(
    delete_repo: Path,
) -> None:
    """The question asks its question; the snapshot is looked up, not narrated.

    A ref appended to every approval prompt is a line nobody reads by the
    third time, in the one place reading matters — and `dev undo` is where a
    snapshot is looked for anyway. So the question carries only its reason,
    while the capture it stays silent about is still on disk for the moment
    somebody reaches for it.
    """
    _effect, reason = snapshotting_effect("git clean -fdx", delete_repo)

    assert "snapshotted" not in reason and "refs/lup/undo/" not in reason
    assert undo_refs(delete_repo) == ["lup undo: git clean -fdx"]


def test_a_command_whose_writes_are_not_in_its_argv_is_snapshotted(
    delete_repo: Path,
) -> None:
    """Coverage is what the net is for, and no trigger delivered it.

    The trigger this replaced fired on the paths a command named plus the
    verdict the classifier reached, and a build, an installer or a script
    announces neither: `bun install` rewrites a tracked lockfile from an argv
    naming no path, under a verdict that waves it through, and was
    snapshotted by nothing. `git status --short` is that shape without the
    toolchain — allowed, naming no target, and held anyway.
    """
    effect, _reason = snapshotting_effect("git status --short", delete_repo)

    assert effect == "allow"
    assert undo_refs(delete_repo) == ["lup undo: git status --short"]


def test_commands_that_change_nothing_leave_one_snapshot_between_them(
    delete_repo: Path,
) -> None:
    """What makes holding every command affordable, and the listing readable.

    Git addresses content, so a command that changed nothing writes a
    byte-identical tree and the ref named after it overwrites the earlier
    one. The listing carries one entry per distinct state the tree was ever
    in rather than one per command — which is the property the trigger was
    reached for and did not deliver, since what it mostly caught was
    commands it did not recognise.
    """
    snapshotting_effect("git status --short", delete_repo)
    snapshotting_effect("ls src", delete_repo)

    assert undo_refs(delete_repo) == ["lup undo: ls src"]


def test_an_allowed_delete_is_snapshotted_without_saying_so(
    delete_repo: Path,
) -> None:
    """Allowed-because-recoverable is still a delete, and git's copy is not the tree.

    `rm src/file0.py` is granted because the object store holds that file byte
    for byte -- but the rest of the working tree is what a mistaken sweep
    takes with it, and that is what the snapshot holds.
    """
    effect, reason = snapshotting_effect("rm src/file0.py", delete_repo)

    assert effect == "allow"
    assert "snapshotted" not in reason
    assert undo_refs(delete_repo) == ["lup undo: rm src/file0.py"]


def test_removing_a_committed_unmodified_file_is_granted(delete_repo: Path) -> None:
    """Git holds exactly what is on disk, so the delete costs a checkout.

    The kernel reads no filesystem and runs no Git, so this only passes when
    the emitted script resolved recoverability itself and handed it over.
    """
    effect, _reason = effect_from("rm src/file0.py", delete_repo)

    assert effect == "allow"


def test_removing_an_untracked_file_still_asks(delete_repo: Path) -> None:
    """Nothing holds a copy, so nothing could restore it afterwards."""
    effect, _reason = effect_from("rm untracked.py", delete_repo)

    assert effect == "ask"


def test_removing_a_directory_asks_where_no_capture_covers_it(
    delete_repo: Path,
) -> None:
    """Nothing in the command bounds what a directory holds, however clean.

    A question rather than a wall, and worded as one. The earlier text said
    the operation "is never granted" and offered approval in the same
    sentence, which reads as a refusal and was worked around as one — so the
    assertion is that the reason states what is being asked rather than
    announcing an outcome nobody can reach.

    A directory this project protects nothing under, because one that holds a
    protected path -- `src` holds the catalog here -- is asked about that
    path's owner first, which no capture settles.
    """
    git = git_in(delete_repo, delete_repo.parent / "no-hooks")
    (delete_repo / "lib").mkdir()
    commit_file(git, delete_repo, "lib/mod.py", "value = 1\n", "chore: lib")
    effect, reason = effect_from("rm -rf lib", delete_repo)

    assert effect == "ask"
    assert "nothing in the command bounds what it holds" in reason
    assert "never granted" not in reason


def test_a_build_product_is_disposable_wherever_it_sits(delete_repo: Path) -> None:
    """The declared pattern reaches the caches scattered through the tree.

    A prefix root could only ever have named the top-level one, so every
    `__pycache__` beside a package stayed production and asked.
    """
    effect, _reason = effect_from(
        "rm packages/lup/src/lup/__pycache__/roles.cpython-314.pyc", delete_repo
    )

    assert effect == "allow"


def test_an_ignored_file_holding_the_only_copy_still_asks(delete_repo: Path) -> None:
    """Untracked is not disposable, and no pattern declares these.

    Git ignores every path here, and each is the only copy of what it
    holds — secrets, the trace corpus, resolver state. A grant keyed on
    `.gitignore` rather than on a declared role would take all three.
    """
    for path in ("notes/traces/session.jsonl", ".lup/run/state.json"):
        effect, _reason = effect_from(f"rm {path}", delete_repo)

        assert effect == "ask", path
    # The secrets are withheld outright, which answers before any grant could.
    assert effect_from("rm .env.local", delete_repo)[0] == "deny"


def test_a_delete_at_the_cap_is_granted(delete_repo: Path) -> None:
    """The cap is a line between a refactor and a sweep, so both sides are pinned.

    Its companion below fixes where the grant stops; without this one, a cap
    of zero would satisfy that test and refuse everything.
    """
    limit = declared_hook_set().recoverable_target_limit
    named = " ".join(f"src/file{index}.py" for index in range(limit))

    effect, _reason = effect_from(f"rm {named}", delete_repo)

    assert effect == "allow"


def test_a_sweep_of_restorable_files_asks_even_though_each_is_restorable(
    delete_repo: Path,
) -> None:
    """Restoring is a repair somebody has to know to perform.

    Every file here is committed and clean, so the per-file grant would take
    all of them; past the declared limit the delete reads as a sweep instead.
    """
    named = " ".join(f"src/file{index}.py" for index in range(SWEEP_SIZE))

    effect, _reason = effect_from(f"rm {named}", delete_repo)

    assert effect == "ask"


def test_writing_a_generated_plugin_tree_is_refused_by_absolute_path(
    delete_repo: Path,
) -> None:
    """A session sends whatever spelling it likes, and may run anywhere.

    Recognizing only the repo-relative spelling would fail open on exactly
    the form that reaches past the worktree the runtime started in.
    """
    outside = delete_repo / ".claude" / "plugins" / "lup" / "hooks" / "policy.py"

    effect, reason = effect_from(f"rm {outside}", delete_repo)

    assert effect == "deny"
    assert "harness generate all" in reason


def test_an_unreadable_target_asks_instead_of_letting_the_edit_through() -> None:
    """Reading the edited file is part of judging it, so a read failure asks.

    `OSError` sat outside the handled set, so an unreadable target raised past
    it and the script died with a traceback. That exit reaches PreToolUse as a
    non-blocking error, which lets the very call the dispatcher could not judge
    proceed — the inverse of the property this boundary exists to hold.
    """
    decision = decide(edit_payload("packages/lup/src/lup/absent.py", "a", "b", False))

    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "deny"


def test_a_remote_read_runs_where_the_boundary_already_grants_it() -> None:
    """What the retired placement on this command was actually asking for.

    A remote read needs a route to the remote, which the boundary declares
    and a launch measures — not the launcher's host, which is what a
    placement would now be requesting and what a reviewer would then have
    to answer for every ordinary fetch. So the verdict is an ordinary
    ambient allow and nothing is rewritten.
    """
    decision = decide(bash_payload("git ls-remote origin HEAD"))

    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "allow"
    assert "updatedInput" not in specific


def test_the_toolchain_runs_where_the_boundary_declaration_leaves_it() -> None:
    """The declaration carries the requirement, so no call site remembers it.

    Every `lup-devtools harness` command opens an agent session, and a
    runtime creates per-session state under its own configuration directory
    — a path the runtime protects rather than one a grant can widen. The
    boundary declaration excludes those verbs from isolation, which is the
    same requirement stated where a launch can measure it; the verdict
    itself is an ordinary ambient allow and rewrites nothing.
    """
    decision = decide(bash_payload("uv run lup-devtools harness resolve"))

    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "allow"
    assert "updatedInput" not in specific


def test_a_checker_target_is_left_where_the_session_already_runs() -> None:
    """The escape is the toolchain's, not every blessed runner target's.

    A checker reads the tree and writes inside it, so placing it outside would
    widen the boundary for the commands that have no need of it.
    """
    decision = decide(bash_payload("uv run pytest tests/unit"))

    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "allow"
    assert "updatedInput" not in specific


def test_an_unplaced_call_carries_no_rewrite_at_all() -> None:
    """Saying nothing about the sandbox must not restate the session's own mode.

    An `updatedInput` on every call would make the dispatcher the author of a
    placement it never decided, and the rewrite replaces the arguments — so a
    verdict with nothing to say about where the call runs says nothing.
    """
    decision = decide(bash_payload("ls"))

    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "allow"
    assert "updatedInput" not in specific


NEW_DEVTOOLS_MODULE = write_payload(
    "src/lup_template/devtools/harness/newborn_probe.py", '"""Newborn."""\n'
)
"""Creating a devtools module: the gate a `new-devtools-module` grant opens."""


def session_environment(document: Path | None) -> EnvVars:
    """The whole environment one worker session is launched with, and keeps.

    Nothing is inherited, so an operator with either variable exported cannot
    decide the outcome of an assertion below. The grants variable names a
    document; what that document says is not part of the environment, and is
    free to change while the session runs.
    """
    return {
        AGENT_IDENTITY_ENV: "resolver-worker",
        **allowance_grants_environment(document),
    }


def effect_under(payload: JsonObject, environment: EnvVars) -> str:
    """The verdict the deployed dispatcher returns for one launched session."""
    output = str(
        sh.Command("python3")(
            "-I",
            "-S",
            str(DISPATCHER),
            _in=json.dumps(payload),
            _env=environment,
        )
    )
    specific = json.loads(output)["hookSpecificOutput"]
    assert isinstance(specific, dict)
    return str(specific["permissionDecision"])


def test_a_grant_made_after_a_session_started_releases_its_very_next_call(
    tmp_path: Path,
) -> None:
    """The environment never changes here; only the human's answer does.

    This is the whole point of reading at judgment. A worker that discovers
    mid-flight that it needs a gate asks, a human answers while that session
    is still running, and the answer has to reach the process that asked —
    which an allowance rendered into the environment at launch could not do.
    """
    document = tmp_path / "grants.json"
    launched = session_environment(document)
    assert effect_under(NEW_DEVTOOLS_MODULE, launched) == "ask"

    write_allowance_grants(document, [ConcernAllowance.NEW_DEVTOOLS_MODULE])

    assert effect_under(NEW_DEVTOOLS_MODULE, launched) == "allow"


def test_a_grant_taken_back_stops_releasing_its_gate_just_as_immediately(
    tmp_path: Path,
) -> None:
    """Symmetric by construction: the document is read, not remembered."""
    document = tmp_path / "grants.json"
    launched = session_environment(document)
    write_allowance_grants(document, [ConcernAllowance.NEW_DEVTOOLS_MODULE])
    assert effect_under(NEW_DEVTOOLS_MODULE, launched) == "allow"

    write_allowance_grants(document, [])

    assert effect_under(NEW_DEVTOOLS_MODULE, launched) == "ask"


def test_a_grant_made_before_the_session_started_is_honoured_too(
    tmp_path: Path,
) -> None:
    """A gate approved with the plan reaches the lease by the same route."""
    document = tmp_path / "grants.json"
    write_allowance_grants(document, [ConcernAllowance.NEW_DEVTOOLS_MODULE])

    assert effect_under(NEW_DEVTOOLS_MODULE, session_environment(document)) == "allow"


def test_a_session_holding_no_grant_sees_the_unchanged_lattice(
    tmp_path: Path,
) -> None:
    """Naming no document, and naming an empty one, both grant nothing."""
    empty = tmp_path / "grants.json"
    write_allowance_grants(empty, [])

    assert effect_under(NEW_DEVTOOLS_MODULE, session_environment(None)) == "ask"
    assert effect_under(NEW_DEVTOOLS_MODULE, session_environment(empty)) == "ask"


def test_one_leases_grant_cannot_release_a_siblings_gate(tmp_path: Path) -> None:
    """A session reads the document it was pointed at and no other."""
    write_allowance_grants(
        tmp_path / "sibling.json", [ConcernAllowance.NEW_DEVTOOLS_MODULE]
    )

    launched = session_environment(tmp_path / "mine.json")

    assert effect_under(NEW_DEVTOOLS_MODULE, launched) == "ask"


def test_a_stale_environment_cannot_grant_what_the_document_does_not(
    tmp_path: Path,
) -> None:
    """The retired variable is inert, so there is one answer and not two.

    An allowance carried as a value in the environment outlives whatever
    decided it. Left readable it would be a second source for a fact that has
    one, and the one it disagreed with would be the live one.
    """
    document = tmp_path / "grants.json"
    write_allowance_grants(document, [])
    launched = {
        **session_environment(document),
        "LUP_CONCERN_ALLOWANCES": '["new-devtools-module"]',
    }

    assert effect_under(NEW_DEVTOOLS_MODULE, launched) == "ask"


def bundled_dispatcher() -> ModuleType:
    """Import the emitted dispatcher so its own `rendered` can be called.

    A placement reaches that function on a decision, so building the decision
    here pins every placement the vocabulary has, where driving one from a
    command would pin only the placements some rule happens to declare.
    """
    return bundled("bundled_claude_policy", DISPATCHER)


def dispatcher_rewrite(
    placement: SandboxPlacement, call: JsonObject
) -> JsonObject | None:
    """What the emitted dispatcher rewrites one placed Bash call to, if anything.

    ``None`` where the verdict states no placement, which is a real answer
    rather than a missing one: an ambient verdict hands the question to the
    session, and rewriting the field to say so would be stating a placement it
    deliberately did not reach.
    """
    dispatcher = bundled_dispatcher()
    answer = dispatcher.rendered(
        dispatcher.KernelDecision("allow", "placed", placement),
        {"tool_name": "Bash", "tool_input": call},
        None,
        "",
    )
    specific = answer["hookSpecificOutput"]
    assert isinstance(specific, dict)
    rewritten = specific["updatedInput"] if "updatedInput" in specific else None
    assert rewritten is None or isinstance(rewritten, dict)
    return rewritten


@pytest.mark.parametrize(
    "findings",
    [
        [],
        [
            'evidence.py:188: error: "EVIDENCE_REFRESHED" is not defined',
            'evidence.py:211: error: Argument missing for parameter "refreshed"',
        ],
    ],
)
def test_post_tool_findings_are_feedback_without_a_process_error(
    findings: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    dispatcher = bundled_dispatcher()
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        dispatcher, "observe", lambda _payload: {"blocking": findings, "context": []}
    )
    monkeypatch.setattr(dispatcher, "plugin_data_root", lambda: tmp_path)

    dispatcher.main()

    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == (
        {"decision": "block", "reason": "\n".join(findings)} if findings else {}
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "hook-events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["phase"] == "completed"


def test_a_failed_post_tool_check_does_not_request_permission_for_the_edit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def failed_check(_payload: object) -> list[str]:
        raise ValueError("checker output has no range")

    dispatcher = bundled_dispatcher()
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(dispatcher, "observe", failed_check)
    monkeypatch.setattr(dispatcher, "plugin_data_root", lambda: tmp_path)

    dispatcher.main()

    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {
        "decision": "block",
        "reason": "Lup post-tool check failed: checker output has no range",
    }
    events = [
        json.loads(line)
        for line in (tmp_path / "hook-events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["phase"] == "failed"


def test_registered_post_tool_command_reports_the_edited_file(tmp_path: Path) -> None:
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "no-hooks")
    edited = work / "evidence.py"
    edited.write_text("value = missing\n", encoding="utf-8")
    diagnostics = {
        "generalDiagnostics": [
            {
                "file": str(edited),
                "severity": "error",
                "range": {"start": {"line": 0}},
                "message": '"missing" is not defined',
            }
        ]
    }
    binaries = work / ".venv" / "bin"
    binaries.mkdir(parents=True)
    declaration = declared_hook_set()
    for command, report in (
        (declaration.diagnostics_command, diagnostics),
        (declaration.repair_command, {"repaired": []}),
    ):
        program = binaries / command[0]
        program.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(json.dumps(report))}\n",
            encoding="utf-8",
        )
        program.chmod(0o755)
    plugin = Path(".claude/plugins/lup").resolve()
    config = json.loads((plugin / "hooks" / "hooks.json").read_text())
    command = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]

    done = sh.Command("sh")(
        "-c",
        command,
        _in=json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(edited)},
            }
        ),
        _env={"PATH": "/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(plugin)},
        _return_cmd=True,
    )

    assert done.exit_code == 0
    assert done.stderr == b""
    assert json.loads(done.stdout) == {
        "decision": "block",
        "reason": 'evidence.py:1: error: "missing" is not defined',
    }


def spent_call(spent: bool) -> JsonObject:
    """One Bash call, with or without the escape its agent already asked for."""
    call: JsonObject = {"command": "uv run lup-devtools dev check"}
    if spent:
        call["dangerouslyDisableSandbox"] = True
    return call


def test_the_dispatcher_overwrites_an_escape_the_call_asked_for_itself() -> None:
    """A placement Lup states is not the call negotiating with it.

    The rewrite replaces the call's arguments outright, so a call carrying
    the native flag has it answered by the placement rather than honoured:
    ``inside`` holds however the call was written, which is the whole of
    what makes containment independent of the session's own mode.

    Asking for the launcher's host is the other route and does not come
    through this field at all — it is a marker a reviewer answers.
    """
    spent = dispatcher_rewrite("inside", spent_call(True))
    unspent = dispatcher_rewrite("inside", spent_call(False))

    assert spent == {**spent_call(True), "dangerouslyDisableSandbox": False}
    assert unspent == {**spent_call(False), "dangerouslyDisableSandbox": False}


@pytest.mark.parametrize("placement", ["inside", "ambient", "outside"])
@pytest.mark.parametrize("spent", [True, False])
def test_both_boundaries_place_one_call_the_same_way(
    placement: SandboxPlacement, spent: bool
) -> None:
    """One field two boundaries fill is one they can fill differently.

    The in-process seam and the emitted dispatcher render the same rewrite
    for two different readers, and only the second is what a native session
    runs — so a suite exercising the first alone reports a placement working
    while the shipped path strips it. They are compared against each other
    rather than each against its own expectation, because what went wrong was
    never either one alone.

    Both sides are the entry a session reaches: `claude_placed_input` is what
    the in-process handler calls, so a renderer nothing constructs cannot
    stand in for it here.
    """
    call = spent_call(spent)

    assert dispatcher_rewrite(placement, call) == claude_placed_input(
        "Bash", call, placement
    )


def test_a_loss_the_capture_holds_is_permitted_by_this_policy(
    delete_repo: Path,
) -> None:
    """The relaxation, through the only thing a session runs.

    `git reset --hard` destroys working-tree content and nothing else, and
    the tree is in the object store before the command is judged — so the
    loss the question was protecting against has been established not to
    happen, and the operation is authorized rather than handed on.

    A permission and not a deferral: handing it to the session's own gate
    would make the outcome depend on which mode the session happened to be
    started in, for a fact that has nothing to do with the session's mode.
    The ref is the evidence the capture the permission rests on was actually
    taken, and taken *before* the verdict read it.
    """
    payload = {**bash_payload("git reset --hard"), "cwd": str(delete_repo)}

    specific = decide_from(payload, delete_repo)["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "allow"
    assert "captured and restorable" in str(specific["permissionDecisionReason"])
    assert undo_refs(delete_repo) == ["lup undo: git reset --hard"]


def test_the_one_loss_the_snapshot_cannot_hold_still_asks(delete_repo: Path) -> None:
    """`git clean -fdx` is the deliberate exception, not an unannotated gap.

    The snapshot leaves ignored files out — `.env.local`, the resolver's
    state, a virtual environment — and `git clean -fdx` is the one command
    whose whole purpose is destroying exactly those. So it keeps asking in
    the posture where every neighbour of it stops.
    """
    effect, _reason = snapshotting_effect("git clean -fdx", delete_repo)

    assert effect == "ask"


def test_a_write_the_gates_already_read_is_not_reported_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The after-the-fact review answers for the writes nothing could read first.

    A shell write is reported afterwards because its content only exists once
    the command has run — true of `dev render > docs/api.md`, and false of a
    command carrying its own bytes, which reaches the same gates before it
    runs. A path both readers name is one finding told twice: once as the
    question, once as the account of what landed.
    """
    work = tmp_path / "repo"
    (work / "src").mkdir(parents=True)
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(
        git,
        work,
        "src/engine.py",
        "# lup: this needs a second look\nvalue = 1\n",
        "chore: base",
    )
    (work / "src" / "engine.py").write_text("value = 2\n", encoding="utf-8")
    dispatcher = bundled_dispatcher()
    # The sweep answers for rules the gate does not run, which is not this.
    monkeypatch.setattr(dispatcher, "swept_files", lambda _paths, _command: {})
    review = dispatcher.written_review

    assert review("cat > src/engine.py <<'EOF'\nvalue = 2\nEOF", work) == {
        "blocking": [],
        "context": [],
    }
    assert review("dev render > src/engine.py", work)["blocking"] != []


def test_an_effect_no_boundary_here_reaches_still_asks(delete_repo: Path) -> None:
    """The blanket relaxation this axis exists to avoid.

    Nothing in a session puts back a deleted remote ref, so relaxing every
    approval question under a boundary would have handed remote-ref deletion,
    issue creation and package publication to whatever the runtime's mode
    happened to be.
    """
    effect, _reason = snapshotting_effect("git push --delete origin feat", delete_repo)

    assert effect == "ask"


CONTAINED_LEDGER = {
    "profile": ["contained"],
    "contained": ["yes"],
    "unjudged_ambient": ["ask"],
    "delivered": ["inside_placement", "question_relay"],
    "blocked": ["host_executor"],
}
"""What a launch that opened a container and measured its placement wrote."""


def unjudged_effect_under(
    ledger: dict[str, list[str]], root: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """What the deployed dispatcher decides about unjudged work in one session.

    The measurement is written where that launch would have put it and named
    by the nonce this session is entitled to believe, because the dispatcher
    reads the ledger rather than any variable. The native sandbox is switched
    off, so a containment that fails to carry has nothing standing in for it.
    """
    written = root / ".lup" / "preflight" / "launch.json"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.delenv("LUP_SANDBOX_ACTIVE", raising=False)

    decision = decide_from(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "frobnicate"},
            "cwd": str(root),
        },
        root,
        written.parent if "yes" in ledger["contained"] else None,
    )
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    return str(specific["permissionDecision"])


def test_a_contained_session_settles_unjudged_work_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary this project ships, reaching the row named for it.

    Every fact behind this is measured by the launch and read here, and the
    dispatcher is the only thing a session runs — so a kernel that joins both
    terms correctly still asks on every real call if the deployed script
    hands it only one of them. That is exactly what happened: `contained`
    travelled and the placement measurement beside it did not, which left
    `bounded()` collapsed onto the native sandbox and a contained session
    judged as an exposed one.
    """
    assert unjudged_effect_under(CONTAINED_LEDGER, tmp_path, monkeypatch) == "allow"


def test_a_container_whose_placement_went_unmeasured_still_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the pair, so the row is not passing on `contained` alone.

    A container is a promise about where an operation lands, and a promise no
    probe confirmed is not evidence for settling one there.
    """
    unmeasured = {**CONTAINED_LEDGER, "delivered": ["question_relay"]}

    assert unjudged_effect_under(unmeasured, tmp_path, monkeypatch) == "ask"


def escalated_reason_under(
    ledger: dict[str, list[str]] | None, root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, object]:
    """What the deployed dispatcher asks about one sandbox escalation.

    The ledger, where one is given, is written where that launch would have
    put it and named by the nonce this session is entitled to believe; none
    given is a launch that measured nothing, which the reader answers as
    uncontained. The native sandbox is off in both, as it is in every
    contained launch. Returns the effect, the reason the approver reads, and
    the rewrite the call goes out with.
    """
    written = root / ".lup" / "preflight" / "launch.json"
    if ledger is None:
        monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    else:
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(json.dumps(ledger), encoding="utf-8")
        monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.delenv("LUP_SANDBOX_ACTIVE", raising=False)
    payload = {
        **bash_payload("# lup: escalate[sandbox]: the host has it\nls"),
        "cwd": str(root),
        "session_id": "requester",
    }
    contained = ledger is not None and "yes" in ledger["contained"]
    specific = decide_from(payload, root, written.parent if contained else None)[
        "hookSpecificOutput"
    ]
    assert isinstance(specific, dict)
    rewritten = specific["updatedInput"] if "updatedInput" in specific else None
    return (
        str(specific["permissionDecision"]),
        str(specific["permissionDecisionReason"]),
        rewritten,
    )


def test_an_approved_crossing_on_a_host_is_described_as_leaving_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployed dispatcher, on a launch that measured no container.

    The rewrite lifts the runtime's per-call sandbox, which on a host is the
    only boundary there is, so the question says the call leaves for the host.
    """
    effect, reason, rewritten = escalated_reason_under(None, tmp_path, monkeypatch)

    assert effect == "ask"
    assert reason.endswith(SANDBOX_ESCAPE_NOTICE)
    assert isinstance(rewritten, dict)
    assert rewritten["dangerouslyDisableSandbox"] is True


def test_an_approved_crossing_inside_a_container_is_described_as_staying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rewrite under a contained launch, and a different sentence.

    Measured under this boundary: an escalated `git merge --ff-only` failed
    with `unable to unlink old 'README.md': Device or resource busy`, exactly
    as it fails unmarked, because the per-call flag the rewrite sets is never
    armed in a container and lifts no mount. The question the approver reads
    says so. The rewrite itself is the same as on a host -- the sentence was
    what had to change, never the placement.
    """
    effect, reason, rewritten = escalated_reason_under(
        CONTAINED_LEDGER, tmp_path, monkeypatch
    )

    assert effect == "ask"
    assert reason.endswith(CONTAINED_ESCAPE_NOTICE)
    assert isinstance(rewritten, dict)
    assert rewritten["dangerouslyDisableSandbox"] is True


def created(path: str, content: str) -> JsonObject:
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": content}}


def test_a_write_announces_what_its_own_prompt_will_not_show() -> None:
    """This runtime's create-file dialog takes nothing from a hook.

    Measured: a `Write` ask renders as that dialog's own path, preview and two
    answers, and the reason handed alongside it is dropped — where the same
    field is shown for a shell command. So a verdict that enumerated something
    the approver cannot otherwise see says it through the one field this
    runtime displays to a person from every hook.
    """
    carrying = "value: Any = 1  # lup: ignore[any-type]\n"
    decision = decide(created("src/lup_template/zz_probe.py", carrying))
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    announcement = specific["permissionDecisionReason"]

    assert isinstance(announcement, str)
    assert "arrives carrying antipattern suppressions" in announcement
    assert "line 1 silences any-type" in announcement


def test_a_reason_naming_only_its_category_announces_nothing() -> None:
    """The dialog already shows the file and its content.

    Repeating that a whole file is being written adds a line telling the
    reader what they are looking at, which is how a channel that exists to
    carry evidence becomes one nobody reads.
    """
    decision = decide(created("src/lup_template/zz_plain.py", "value = 1\n"))

    assert "systemMessage" not in decision


def test_a_shell_prompt_is_not_told_twice() -> None:
    """A command's prompt renders the reason itself, so announcing it repeats it."""
    decision = decide({"tool_name": "Bash", "tool_input": {"command": "rm -rf src"}})

    assert "systemMessage" not in decision


@pytest.mark.parametrize(
    ("prose", "refused"),
    [
        ('"""Reflection gate, previously a flag on the session."""', True),
        ('"""Reflection gate, one verdict per output."""', False),
        ("# previously a flag on the session", True),
        ("# one verdict per output", False),
    ],
    ids=["docstring-prior-state", "docstring-clean", "comment-prior-state", "clean"],
)
def test_prose_narrating_a_change_is_refused_wherever_it_is_written(
    prose: str, refused: bool
) -> None:
    """The gate reads a sentence wherever one is written.

    A docstring carries no inline directive, so the whole-file audit skips it
    and this gate is the only surface that reads it. Covered here rather than
    by the rule's own examples, which both surfaces have to answer and one of
    them structurally cannot.
    """
    anchor = "class ReflectionGate"
    target = "packages/lup/src/lup/orchestration/reflection.py"
    decision = decide(edit_payload(target, anchor, f"{prose}\n{anchor}", False))

    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    reason = str(specific["permissionDecisionReason"])
    assert ("historical-voice" in reason) is refused
