"""Accepted destination policies govern edits without borrowing caller authority."""

import json
import os
import shutil
import shlex
import sys
from pathlib import Path
from subprocess import TimeoutExpired

import pytest
import sh

from lup.devtools.dev.policy_explain import verdict_for
import lup.policy.assets.host as policy_host
from lup.policy.kernel.decision import KernelDecision
from lup.policy.identity import AGENT_IDENTITY_ENV
from lup.policy.models import EditBatch, EditChange
from lup.policy.rules import EditPolicy
from lup.policy.kernel.policy_protocol import decision_wire, read_response
from lup.policy.snapshots import DestinationPolicy, accept_destination_policies
from lup.sandbox.rail import AccessibleRoot, Lease
from lup_template.harness.catalog import declared_hook_set
from tests.unit.repos import initialized_repo


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def repositories(tmp_path: Path, runtime: str) -> tuple[Path, Path]:
    origin, owner = tmp_path / "origin", tmp_path / "owner"
    for root in (origin, owner):
        initialized_repo(root, tmp_path / "hooks")
        shutil.copytree(
            Path(f".{runtime}/plugins/lup/hooks"),
            root / f".{runtime}/plugins/lup/hooks",
        )
        (root / "value.txt").write_text("before\n")
        (root / "OWNED.md").write_text("before\n")
    data = owner / f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"
    data.write_text(
        data.read_text()
        + "\nPATH_RULES.append({'kind': 'exact', 'value': 'OWNED.md', 'reason': 'destination human author', 'recovery': '', 'allow_autonomous': False})\n"
    )
    return origin, owner


def authorize(
    origin: Path,
    owner: Path,
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
    readonly: list[Path] | None = None,
) -> DestinationPolicy:
    lease = Lease(
        writable={origin: str(origin), owner: str(owner)},
        read_only={path: str(path) for path in readonly or []},
    )
    rows = accept_destination_policies(
        origin,
        [AccessibleRoot(path=owner, writable=True)],
        lease,
        runtime,
    )
    (binding,) = rows
    assert not binding.error
    directory = origin / ".lup/preflight"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "routing-test.json").write_text(
        json.dumps(
            {
                "writable_roots": [str(origin), str(owner)],
                "read_only_roots": [str(path) for path in readonly or []],
                "destination_policies": [binding.model_dump_json()],
            }
        )
    )
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "routing-test")
    monkeypatch.setenv("LUP_BOUNDARY_ROOT", str(origin))
    return binding


def native_edit(
    origin: Path, path: Path, runtime: str, before: str = "before", after: str = "after"
) -> tuple[str, str]:
    payload = {
        "session_id": "routing-requester",
        "cwd": str(origin),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit" if runtime == "claude" else "apply_patch",
        "tool_input": (
            {"file_path": str(path), "old_string": before, "new_string": after}
            if runtime == "claude"
            else {
                "command": f"*** Begin Patch\n*** Update File: {path}\n@@\n-{before}\n+{after}\n*** End Patch"
            }
        ),
    }
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(origin / f".{runtime}/plugins/lup/hooks/scripts/policy.py"),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={**os.environ, "PLUGIN_DATA": str(origin / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    if result.exit_code == 2:
        return "deny", result.stderr.decode()
    if not str(result):
        return "allow", ""
    answer = json.loads(str(result))
    specific = answer.get("hookSpecificOutput", {})
    return specific.get("permissionDecision", "allow"), json.dumps(answer)


def test_destination_allows_ordinary_edit_and_preserves_its_human_gate(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = repositories
    authorize(origin, owner, runtime, monkeypatch)
    assert native_edit(origin, owner / "value.txt", runtime)[0] == "allow"
    effect, detail = native_edit(origin, owner / "OWNED.md", runtime)
    assert effect == ("ask" if runtime == "claude" else "deny")
    assert "destination human author" in detail
    assert native_edit(origin, origin / "OWNED.md", runtime)[0] == "allow"
    assert (owner / "OWNED.md").read_text() == "before\n"


def test_destination_feedback_and_generated_gates_survive_routing(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = repositories
    authorize(origin, owner, runtime, monkeypatch)
    target = owner / "value.txt"
    target.write_text("# lup: retain this review\n")
    assert (
        native_edit(origin, target, runtime, "# lup: retain this review", "gone")[0]
        == "deny"
    )
    generated = owner / f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"
    effect, _detail = native_edit(
        origin, generated, runtime, "MAXIMUM_ADDED_LINES = 3", "MAXIMUM_ADDED_LINES = 4"
    )
    assert effect != "allow"


def test_readonly_mount_and_symlink_escape_keep_caller_boundary(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    origin, owner = repositories
    authorize(origin, owner, runtime, monkeypatch, [owner / "value.txt"])
    effect, detail = native_edit(origin, owner / "value.txt", runtime)
    assert effect == "deny" and "writable boundary" in detail
    outside = tmp_path / "outside.txt"
    outside.write_text("before\n")
    link = owner / "escape.txt"
    link.symlink_to(outside)
    assert native_edit(origin, link, runtime)[0] == "deny"
    assert outside.read_text() == "before\n"


@pytest.mark.parametrize("damaged", ["source", "snapshot", "missing", "protocol"])
def test_unavailable_policy_never_executes_changed_code_or_falls_back(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
    damaged: str,
) -> None:
    origin, owner = repositories
    binding = authorize(origin, owner, runtime, monkeypatch)
    if damaged == "missing":
        (Path(binding.snapshot) / "scripts/policy_evaluator.py").unlink()
    elif damaged == "protocol":
        ledger = origin / ".lup/preflight/routing-test.json"
        measured = json.loads(ledger.read_text())
        row = json.loads(measured["destination_policies"][0])
        row["protocol"] = 999
        measured["destination_policies"] = [json.dumps(row)]
        ledger.write_text(json.dumps(measured))
    else:
        directory = Path(binding.source if damaged == "source" else binding.snapshot)
        evaluator = directory / "scripts/policy_evaluator.py"
        evaluator.write_text(
            f"from pathlib import Path\nPath({str(owner / 'executed')!r}).touch()\n"
        )
    effect, detail = native_edit(origin, owner / "value.txt", runtime)
    assert effect == "deny"
    assert "Destination policy unavailable" in detail
    assert not (owner / "executed").exists()


@pytest.mark.parametrize("filename", ["policy_data.so", "policy_data.pyc"])
def test_accepted_snapshot_rejects_unhashed_import_candidates(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    origin, owner = repositories
    binding = authorize(origin, owner, runtime, monkeypatch)
    (Path(binding.snapshot) / "runtime" / filename).write_bytes(b"unaccepted code")

    effect, detail = native_edit(origin, owner / "value.txt", runtime)

    assert effect == "deny"
    assert "unsupported code" in detail


def test_destination_does_not_execute_mutable_checkout_resolver(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = repositories
    marker = owner / "resolver-executed"
    resolver = owner / "resolver"
    resolver.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    resolver.chmod(0o755)
    data = owner / f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"
    data.write_text(data.read_text() + f"\nRESOLUTION_COMMAND = [{str(resolver)!r}]\n")
    target = owner / "engine.py"
    before = 'def read(client):\n    return client.get("old")\n'
    after = 'def read(client):\n    return client.get("new")\n'
    target.write_text(before)
    authorize(origin, owner, runtime, monkeypatch)

    effect, detail = native_tool_response(
        origin,
        runtime,
        "Bash",
        {"command": f"cat > {shlex.quote(str(target))} <<'CONTENT'\n{after}CONTENT"},
    )

    assert effect == ("ask" if runtime == "claude" else "deny"), detail
    assert "dict-get" in detail
    assert not marker.exists()
    assert target.read_text() == before


def test_concrete_preflight_matches_native_owner_decision(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = repositories
    authorize(origin, owner, runtime, monkeypatch)
    document = origin / "edit.json"
    document.write_text(
        json.dumps(
            {
                "changes": [
                    {
                        "path": str(owner / "OWNED.md"),
                        "before": "before\n",
                        "after": "after\n",
                        "operation": "modify",
                    }
                ]
            }
        )
    )
    verdict = verdict_for(
        str(document), "edit-batch", False, origin, declared_hook_set()
    )
    assert {reading.effect for reading in verdict.readings} == {"ask"}
    assert all(
        "destination human author" in reading.reason for reading in verdict.readings
    )
    preview = verdict_for(
        str(owner / "OWNED.md"), "edit", False, origin, declared_hook_set()
    )
    assert {reading.effect for reading in preview.readings} == {"ask"}
    assert preview.unavailable and "path-only" in preview.unavailable[0]
    (owner / "OWNED.md").write_text("changed\n")
    with pytest.raises(ValueError, match="preimage does not match"):
        verdict_for(str(document), "edit-batch", False, origin, declared_hook_set())


def test_unregistered_foreign_repo_keeps_foreign_review(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = repositories
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    effect, detail = native_edit(origin, owner / "value.txt", runtime)
    assert effect != "allow" and "different repository" in detail
    verdict = verdict_for(
        str(owner / "value.txt"), "edit", False, origin, declared_hook_set()
    )
    assert {reading.effect for reading in verdict.readings} == {"ask"}
    assert all("different repository" in reading.reason for reading in verdict.readings)


def test_preflight_from_a_subdirectory_keeps_the_launch_destination_authority(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = repositories
    authorize(origin, owner, runtime, monkeypatch, [owner / "OWNED.md"])
    cwd = origin / "src"
    cwd.mkdir()

    ordinary = verdict_for(
        str(owner / "value.txt"), "edit", False, cwd, declared_hook_set()
    )
    protected = verdict_for(
        str(owner / "OWNED.md"), "edit", False, cwd, declared_hook_set()
    )

    assert {reading.effect for reading in ordinary.readings} == {"allow"}
    assert {reading.effect for reading in protected.readings} == {"deny"}
    assert all("writable boundary" in reading.reason for reading in protected.readings)


def test_protocol_preserves_all_semantic_fields_and_rejects_malformed_responses() -> (
    None
):
    source = KernelDecision(
        "ask",
        "owner review",
        sandbox="outside",
        escalated="requested",
        checkpoint="targeted",
        reviewer="supervisor_allowed",
        purpose="quality_review",
        visibility="notice",
        hard=True,
        rule="owner:rule",
        evaluator="edit",
        recovery="review",
        findings=(KernelDecision("deny", "other file", hard=True),),
    )
    row = decision_wire(source)
    restored = read_response(json.loads(json.dumps({"protocol": 1, "decision": row})))
    assert decision_wire(restored) == row
    assert set(row) == set(vars(source)) - {"file_reviews"}
    for malformed in (
        {"protocol": 2, "decision": row},
        {"protocol": 1, "decision": {"effect": "allow"}},
        {"protocol": 1, "decision": {**row, "reviewer": "requester"}},
    ):
        with pytest.raises(ValueError):
            read_response(json.loads(json.dumps(malformed)))


def native_tool_response(
    origin: Path,
    runtime: str,
    name: str,
    tool_input: dict[str, str],
    agent_type: str = "",
) -> tuple[str, str]:
    """Run a concrete native hook envelope without executing the requested tool."""
    payload = {
        "session_id": "routing-requester",
        "cwd": str(origin),
        "hook_event_name": "PreToolUse",
        "tool_name": name,
        "tool_input": tool_input,
        "agent_type": agent_type,
    }
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(origin / f".{runtime}/plugins/lup/hooks/scripts/policy.py"),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={**os.environ, "PLUGIN_DATA": str(origin / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    if result.exit_code == 2:
        return "deny", result.stderr.decode()
    if not str(result):
        return "allow", ""
    answer = json.loads(str(result))
    specific = answer.get("hookSpecificOutput", {})
    return specific.get("permissionDecision", "allow"), json.dumps(answer)


@pytest.mark.parametrize("shape", ["authored", "sed"])
def test_shell_edit_routes_preserve_destination_human_authority(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    origin, owner = repositories
    authorize(origin, owner, runtime, monkeypatch)
    target = shlex.quote(str(owner / "OWNED.md"))
    command = (
        f"cat > {target} <<'CONTENT'\nafter\nCONTENT"
        if shape == "authored"
        else f"sed -i 's/before/after/' {target}"
    )

    effect, detail = native_tool_response(origin, runtime, "Bash", {"command": command})

    assert effect == ("ask" if runtime == "claude" else "deny")
    assert "destination human author" in detail
    assert (owner / "OWNED.md").read_text() == "before\n"


def test_a_mixed_repository_batch_denial_wins_over_destination_approval(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = repositories
    authorize(origin, owner, runtime, monkeypatch)
    (origin / "review.txt").write_text("# lup: keep this note\n")
    batch = origin / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "changes": [
                    {
                        "path": str(owner / "OWNED.md"),
                        "before": "before\n",
                        "after": "after\n",
                        "operation": "modify",
                    },
                    {
                        "path": str(origin / "review.txt"),
                        "before": "# lup: keep this note\n",
                        "after": "gone\n",
                        "operation": "modify",
                    },
                ]
            }
        )
    )

    decision = verdict_for(str(batch), "edit-batch", False, origin, declared_hook_set())

    assert {reading.effect for reading in decision.readings} == {"deny"}
    assert all(
        "review" in reading.reason.lower() or "lup:" in reading.reason
        for reading in decision.readings
    )


def test_a_codex_cross_repository_move_judges_the_deleted_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = "codex"
    origin, owner = tmp_path / "origin", tmp_path / "owner"
    for checkout in (origin, owner):
        initialized_repo(checkout, tmp_path / "hooks")
        shutil.copytree(
            Path(f".{runtime}/plugins/lup/hooks"),
            checkout / f".{runtime}/plugins/lup/hooks",
        )
    source = owner / "review.txt"
    source.write_text("# lup: keep the original review\nbefore\n")
    destination = origin / "moved.txt"
    authorize(origin, owner, runtime, monkeypatch)
    patch = f"*** Begin Patch\n*** Update File: {source}\n*** Move to: {destination}\n@@\n # lup: keep the original review\n-before\n+after\n*** End Patch"

    effect, detail = native_tool_response(
        origin, runtime, "apply_patch", {"command": patch}
    )

    assert effect == "deny"
    assert "review" in detail.lower()
    assert source.read_text() == "# lup: keep the original review\nbefore\n"
    assert not destination.exists()


@pytest.mark.parametrize("failure", ["malformed", "failed"])
def test_an_accepted_evaluator_failure_blocks_with_its_diagnostic(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    origin, owner = repositories
    evaluator = owner / f".{runtime}/plugins/lup/hooks/scripts/policy_evaluator.py"
    evaluator.write_text(
        "print('{')\n"
        if failure == "malformed"
        else "raise RuntimeError('destination failure')\n"
    )
    authorize(origin, owner, runtime, monkeypatch)

    effect, detail = native_edit(origin, owner / "value.txt", runtime)

    assert effect == "deny"
    assert "Destination policy unavailable" in detail
    if failure == "failed":
        assert "destination failure" in detail


def test_evaluator_timeout_is_bounded_and_routed_as_unavailable(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = repositories
    evaluator = owner / f".{runtime}/plugins/lup/hooks/scripts/policy_evaluator.py"
    evaluator.write_text("import time\ntime.sleep(2)\n")
    binding = authorize(origin, owner, runtime, monkeypatch)
    with pytest.raises(TimeoutExpired):
        policy_host.destination_evaluation(
            binding.model_dump_json(), "{}", timeout=0.01
        )

    def timeout(_binding: str, _request: str) -> str:
        raise TimeoutExpired("destination evaluator", 0.01)

    monkeypatch.setattr(policy_host, "destination_evaluation", timeout)
    with pytest.raises(ValueError, match="evaluator timed out"):
        policy_host.routed_edit_response(
            str(owner / "value.txt"),
            "before\n",
            "after\n",
            True,
            False,
            "modify",
            origin,
        )


def test_a_destination_grant_without_measured_write_authority_cannot_run(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = repositories
    authorize(origin, owner, runtime, monkeypatch)
    ledger = origin / ".lup/preflight/routing-test.json"
    measured = json.loads(ledger.read_text())
    del measured["writable_roots"]
    ledger.write_text(json.dumps(measured))

    effect, detail = native_edit(origin, owner / "value.txt", runtime)

    assert effect == "deny"
    assert "measured writable boundary" in detail


def test_an_explicit_sibling_grant_uses_that_worktrees_accepted_policy(
    tmp_path: Path,
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, owner = tmp_path / "origin", tmp_path / "sibling"
    git = initialized_repo(origin, tmp_path / "hooks")
    git("worktree", "add", "-q", "--orphan", "-b", "sibling", str(owner))
    for checkout in (origin, owner):
        shutil.copytree(
            Path(f".{runtime}/plugins/lup/hooks"),
            checkout / f".{runtime}/plugins/lup/hooks",
        )
        (checkout / "OWNED.md").write_text("before\n")
    data = owner / f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"
    data.write_text(
        data.read_text()
        + "\nPATH_RULES.append({'kind': 'exact', 'value': 'OWNED.md', 'reason': 'sibling human author', 'recovery': '', 'allow_autonomous': False})\n"
    )
    assert policy_host.shared_git_directory(
        str(origin)
    ) == policy_host.shared_git_directory(str(owner))
    assert native_edit(origin, owner / "OWNED.md", runtime)[0] == "allow"
    authorize(origin, owner, runtime, monkeypatch)

    effect, detail = native_edit(origin, owner / "OWNED.md", runtime)

    assert effect == ("ask" if runtime == "claude" else "deny")
    assert "sibling human author" in detail
    assert native_edit(origin, origin / "OWNED.md", runtime)[0] == "allow"


@pytest.mark.parametrize(
    "runtime, channel",
    [("claude", "native"), ("claude", "environment"), ("codex", "environment")],
)
@pytest.mark.parametrize("recognized", ["both", "origin", "destination"])
@pytest.mark.parametrize("shape", ["edit", "write", "heredoc", "sed"])
def test_autonomy_requires_the_same_identity_in_both_policies(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    recognized: str,
    shape: str,
) -> None:
    origin, owner = repositories
    worker = "routing-worker"
    for root, side in ((origin, "origin"), (owner, "destination")):
        data = root / f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"
        roster = [worker] if recognized in ("both", side) else ["other-worker"]
        data.write_text(
            data.read_text()
            + f"\nAUTONOMOUS_AGENT_IDENTITIES = {roster!r}\n"
            + "PATH_RULES.append({'kind': 'exact', 'value': 'WORKER.md', 'reason': 'destination reviewed worker', 'recovery': '', 'allow_autonomous': True})\n"
        )
    target = owner / "WORKER.md"
    target.write_text("before\n")
    authorize(origin, owner, runtime, monkeypatch)
    if channel == "environment":
        monkeypatch.setenv(AGENT_IDENTITY_ENV, worker)
    else:
        monkeypatch.delenv(AGENT_IDENTITY_ENV, raising=False)
    if shape in ("edit", "write"):
        if runtime == "codex":
            name = "apply_patch"
            tool_input = {
                "command": f"*** Begin Patch\n*** Update File: {target}\n@@\n-before\n+after\n*** End Patch"
            }
        elif shape == "edit":
            name = "Edit"
            tool_input = {
                "file_path": str(target),
                "old_string": "before",
                "new_string": "after",
            }
        else:
            name = "Write"
            tool_input = {"file_path": str(target), "content": "after\n"}
    else:
        name = "Bash"
        path = shlex.quote(str(target))
        tool_input = {
            "command": (
                f"cat > {path} <<'CONTENT'\nafter\nCONTENT"
                if shape == "heredoc"
                else f"sed -i 's/before/after/' {path}"
            )
        }
    effect, detail = native_tool_response(
        origin, runtime, name, tool_input, worker if channel == "native" else ""
    )
    if recognized == "both":
        assert "destination reviewed worker" not in detail
        if shape in ("edit", "write"):
            assert effect == "allow"
    else:
        assert effect != "allow"
        assert "destination reviewed worker" in detail
    if channel == "environment" and shape == "edit":
        policy = EditPolicy(protected=[], autonomous=recognized != "destination")
        decision = policy.decide(
            EditBatch(
                changes=[EditChange(path=target, before="before\n", after="after\n")],
                cwd=origin,
            )
        )
        assert decision.effect == ("allow" if recognized == "both" else "ask")
    assert target.read_text() == "before\n"


@pytest.mark.parametrize("damage", ["missing", "replacement", "nested-owner"])
def test_accepted_checkout_identity_cannot_disappear_into_a_writable_mount(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    origin, owner = repositories
    authorize(origin, owner, runtime, monkeypatch)
    target = owner / "value.txt"
    if damage == "nested-owner":
        nested = owner / "nested"
        initialized_repo(nested, owner.parent / "hooks")
        target = nested / "value.txt"
        target.write_text("before\n")
    else:
        (owner / ".git").rename(owner.parent / "retired-owner-git")
        if damage == "replacement":
            (owner / ".git").write_text(f"gitdir: {origin / '.git'}\n")

    effect, detail = native_edit(origin, target, runtime)

    assert effect == "deny"
    assert "destination" in detail.lower() and "identity" in detail
    assert target.read_text() == "before\n"
    preview = verdict_for(str(target), "edit", False, origin, declared_hook_set())
    assert {reading.effect for reading in preview.readings} == {"deny"}
    assert all("identity" in reading.reason for reading in preview.readings)


def test_removing_nested_destination_git_does_not_make_it_caller_owned(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, _owner = repositories
    owner = origin / "nested"
    initialized_repo(owner, origin.parent / "hooks")
    shutil.copytree(
        origin / f".{runtime}/plugins/lup/hooks",
        owner / f".{runtime}/plugins/lup/hooks",
    )
    target = owner / "value.txt"
    target.write_text("before\n")
    authorize(origin, owner, runtime, monkeypatch)
    (owner / ".git").rename(origin.parent / "retired-nested-git")
    assert policy_host.worktree_root(str(target)) == str(origin)

    effect, detail = native_edit(origin, target, runtime)

    assert effect == "deny"
    assert "identity" in detail


def test_an_explicit_nested_owner_grant_keeps_its_own_authority(
    repositories: tuple[Path, Path],
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, parent = repositories
    nested = parent / "nested"
    initialized_repo(nested, parent.parent / "hooks")
    shutil.copytree(
        origin / f".{runtime}/plugins/lup/hooks",
        nested / f".{runtime}/plugins/lup/hooks",
    )
    target = nested / "value.txt"
    target.write_text("before\n")
    authorize(origin, parent, runtime, monkeypatch)
    rows = accept_destination_policies(
        origin,
        [AccessibleRoot(path=parent), AccessibleRoot(path=nested)],
        Lease(writable={origin: str(origin), parent: str(parent)}),
        runtime,
    )
    ledger = origin / ".lup/preflight/routing-test.json"
    measured = json.loads(ledger.read_text())
    measured["destination_policies"] = [row.model_dump_json() for row in rows]
    ledger.write_text(json.dumps(measured))

    assert native_edit(origin, target, runtime)[0] == "allow"
