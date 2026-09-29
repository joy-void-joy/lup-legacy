"""Codex apply_patch decoding and the edit policy it finally reaches.

Codex hands a hook one complete patch command where Claude hands over
structured before/after text. These tests pin the native decode, the
per-file decisions it enables, and the path resolution that keeps
repo-relative rules matching inside a sibling worktree.
"""

import importlib.util
import io
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from lup.coordination.bare.store import Caller
from lup.providers.codex.patch import patched_files, patched_paths
from lup.types import JsonObject
from tests.unit.bundled import bundled

GREETING = 'def greet():\n    return "hi"\n'


def update_envelope(path: str, body: str) -> str:
    return f"*** Begin Patch\n*** Update File: {path}\n@@\n{body}\n*** End Patch"


def document(text: str) -> Callable[[str], str | None]:
    return lambda _path: text


def named_document(path: str, text: str) -> Callable[[str], str | None]:
    return lambda candidate: text if candidate == path else None


def moved_document(path: str) -> str | None:
    match path:
        case "old.py":
            return "context\n"
        case "new.py":
            return "replaced\n"
        case _:
            return None


def test_update_applies_hunks_over_the_current_document() -> None:
    envelope = update_envelope(
        "module.py", ' def greet():\n-    return "hi"\n+    return "hello"'
    )

    changes = patched_files(envelope, document(GREETING))

    assert len(changes) == 1
    assert changes[0].path == "module.py"
    assert changes[0].before == GREETING
    assert changes[0].after == 'def greet():\n    return "hello"\n'


def test_add_file_carries_no_preimage() -> None:
    envelope = "*** Begin Patch\n*** Add File: new.py\n+one\n+two\n*** End Patch"

    changes = patched_files(envelope, named_document("other.py", ""))

    assert not changes[0].path_exists
    assert changes[0].before is None
    assert changes[0].after == "one\ntwo\n"


def test_delete_file_carries_no_postimage() -> None:
    envelope = "*** Begin Patch\n*** Delete File: gone.py\n*** End Patch"

    changes = patched_files(envelope, document("body\n"))

    assert changes[0].before == "body\n"
    assert changes[0].after is None
    assert changes[0].path_exists


def test_move_reports_source_deletion_and_destination_write() -> None:
    envelope = (
        "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n"
        "@@\n context\n*** End Patch"
    )

    changes = patched_files(envelope, moved_document)

    assert [change.path for change in changes] == ["old.py", "new.py"]
    assert changes[0].before == "context\n"
    assert changes[0].after is None
    assert changes[1].before == "replaced\n"
    assert changes[1].after == "context\n"
    assert changes[1].path_exists


def test_one_envelope_yields_every_file_it_touches() -> None:
    envelope = (
        "*** Begin Patch\n*** Add File: a.py\n+a\n*** Delete File: b.py\n*** End Patch"
    )

    changes = patched_files(envelope, named_document("b.py", "b\n"))

    assert [change.path for change in changes] == ["a.py", "b.py"]


def test_paths_can_be_read_after_updates_moves_and_deletions() -> None:
    envelope = (
        "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n"
        "@@\n-old content\n+new content\n*** Delete File: gone.py\n"
        "*** Add File: added.py\n+*** Delete File: not-a-path.py\n"
        "*** Update File: new.py\n@@\n-new content\n+final content\n*** End Patch"
    )

    assert patched_paths(envelope) == ["old.py", "new.py", "gone.py", "added.py"]


@pytest.mark.parametrize(
    "envelope",
    [
        "garbage",
        "*** Begin Patch\n*** End Patch",
        "*** Begin Patch\n*** Delete File: \n*** End Patch",
        "*** Begin Patch\n*** Update File: x.py\n@@\nmalformed\n*** End Patch",
        "*** Begin Patch\n*** Update File: x.py\n*** Move to: x.py\n*** End Patch",
    ],
)
def test_path_only_parsing_still_rejects_malformed_patches(envelope: str) -> None:
    with pytest.raises(ValueError):
        patched_paths(envelope)


def test_context_headers_and_end_of_file_anchor_the_native_result() -> None:
    envelope = (
        "*** Begin Patch\n*** Update File: module.py\n@@ def second():\n"
        "-    return 2\n+    return 3\n*** End of File\n*** End Patch"
    )
    before = "def first():\n    return 2\n\ndef second():\n    return 2\n"

    changes = patched_files(envelope, document(before))

    assert changes[0].after == (
        "def first():\n    return 2\n\ndef second():\n    return 3\n"
    )


@pytest.mark.parametrize(
    ("envelope", "message"),
    [
        ("no header at all", "Begin Patch"),
        ("*** Begin Patch\n*** Add File: a.py\n+x", "End Patch"),
        ("*** Begin Patch\n*** End Patch", "no file changes"),
        (
            "*** Begin Patch\n*** Add File: a.py\nnot-added\n*** End Patch",
            "no added lines",
        ),
        (
            "*** Begin Patch\n*** Mystery File: a.py\n*** End Patch",
            "unsupported patch section",
        ),
        (update_envelope("m.py", " missing"), "does not match"),
    ],
)
def test_an_envelope_the_parser_cannot_vouch_for_raises(
    envelope: str, message: str
) -> None:
    """Refusing to guess keeps the caller on its conservative branch."""
    with pytest.raises(ValueError, match=message):
        patched_files(envelope, document("other\n"))


def bundled_dispatcher() -> ModuleType:
    return bundled(
        "bundled_codex_policy",
        Path.cwd() / ".codex/plugins/lup/hooks/scripts/policy.py",
    )


def test_bundled_dispatcher_reads_its_own_policy_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another runtime's declaration cached in this process is not the one read."""
    path = Path.cwd() / ".claude/plugins/lup/hooks/runtime/policy_data.py"
    spec = importlib.util.spec_from_file_location("policy_data", path)
    assert spec is not None and spec.loader is not None
    foreign = importlib.util.module_from_spec(spec)
    with (
        patch.dict(sys.modules),
        patch.object(sys, "path", [str(path.parent), *sys.path]),
    ):
        spec.loader.exec_module(foreign)
    monkeypatch.setitem(sys.modules, "policy_data", foreign)

    dispatcher = bundled_dispatcher()

    assert dispatcher.declared_policy is not foreign
    assert Path(dispatcher.declared_policy.__file__) == (
        Path.cwd() / ".codex/plugins/lup/hooks/runtime/policy_data.py"
    )


def worktree(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def patch_payload(envelope: str) -> JsonObject:
    return {"tool_name": "apply_patch", "tool_input": {"command": envelope}}


class TestDispatchedPatches:
    @pytest.mark.parametrize(
        ("agent_type", "effect"),
        [
            ("", "ask"),
            ("default", "ask"),
            ("resolver-worker", "allow"),
            ("lup:resolver-worker", "ask"),
        ],
    )
    def test_native_worker_identity_releases_only_declared_edit_rules(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        agent_type: str,
        effect: str,
    ) -> None:
        root = worktree(tmp_path / "feature")
        monkeypatch.delenv("LUP_AGENT_IDENTITY", raising=False)
        dispatcher = bundled_dispatcher()
        additions = "\n".join(f"+value_{index} = {index}" for index in range(8))
        payload = {
            **patch_payload(
                f"*** Begin Patch\n*** Add File: module.py\n{additions}\n*** End Patch"
            ),
            "cwd": str(root),
            "agent_type": agent_type,
        }

        decision = dispatcher.dispatch(payload)

        assert decision.effect == effect, decision.stated_whole()
        payload["tool_input"] = {
            "command": "*** Begin Patch\n*** Add File: README.md\n+# Title\n*** End Patch"
        }
        assert dispatcher.dispatch(payload).effect == "ask"

    @pytest.mark.parametrize("shell", [False, True])
    def test_post_patch_checks_only_its_paths_without_replaying_old_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: bool
    ) -> None:
        root = worktree(tmp_path / "feature")
        target = root / "new.py"
        (root / "old.py").write_text("value = 1\n", encoding="utf-8")
        (root / "gone.py").write_text("gone = True\n", encoding="utf-8")
        envelope = (
            "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n"
            "@@\n-value = 1\n+value = 2\n*** Delete File: gone.py\n*** End Patch"
        )
        dispatcher = bundled_dispatcher()
        calls: list[tuple[str, str]] = []

        def repair(path: str, _command: list[str]) -> list[str]:
            calls.append(("repair", path))
            return []

        def diagnostics(path: str, _command: list[str]) -> dict[str, list[str]]:
            calls.append(("diagnostics", path))
            found = [f"{path}: type mismatch"] if Path(path).is_file() else []
            return {"blocking": found, "context": []}

        monkeypatch.setattr(dispatcher, "repaired_directives", repair)
        monkeypatch.setattr(dispatcher, "file_diagnostics", diagnostics)
        monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
        command = f"apply_patch <<'PATCH'\n{envelope}\nPATCH" if shell else envelope
        payload = {
            "tool_name": "Bash" if shell else "apply_patch",
            "tool_input": {"command": command},
            "cwd": str(root),
            "session_id": "session",
            "tool_use_id": "patch-call",
        }
        dispatcher.remember_patch(payload)
        (root / "old.py").unlink()
        (root / "gone.py").unlink()
        target.write_text("value = 2\n", encoding="utf-8")

        assert dispatcher.observe(payload) == {
            "blocking": [f"{target}: type mismatch"],
            "context": [],
        }
        # Every changed path repaired, then each one type-checked.
        changed = [str(root / path) for path in ("old.py", "new.py", "gone.py")]
        assert calls == [
            *(("repair", path) for path in changed),
            *(("diagnostics", path) for path in changed),
        ]

    @pytest.mark.parametrize("partial", [False, True])
    @pytest.mark.parametrize("shell", [False, True])
    def test_failed_patches_only_observe_files_that_actually_changed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        partial: bool,
        shell: bool,
    ) -> None:
        root = worktree(tmp_path / "feature")
        first, second = root / "first.py", root / "second.py"
        for path in (first, second):
            path.write_text("value = 1\n", encoding="utf-8")
        envelope = (
            "*** Begin Patch\n*** Update File: first.py\n@@\n-value = 1\n+value = 2\n"
            "*** Update File: second.py\n@@\n-value = 1\n+value = 2\n*** End Patch"
        )
        dispatcher = bundled_dispatcher()
        calls: list[str] = []

        def swept(path: str, _command: list[str]) -> list[str]:
            calls.append(path)
            return []

        def record(path: str, _command: list[str]) -> dict[str, list[str]]:
            calls.append(path)
            return {"blocking": [], "context": []}

        def claimed(path: str, _cwd: Path, _caller: Caller) -> None:
            calls.append(path)

        monkeypatch.setattr(dispatcher, "repaired_directives", swept)
        monkeypatch.setattr(dispatcher, "file_diagnostics", record)
        monkeypatch.setattr(dispatcher, "named_claim_recorded", claimed)
        monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
        payload = {
            "tool_name": "Bash" if shell else "apply_patch",
            "tool_input": {
                "command": f"apply_patch <<'PATCH'\n{envelope}\nPATCH"
                if shell
                else envelope
            },
            "cwd": str(root),
            "session_id": "session",
            "tool_use_id": "failed-call",
        }
        dispatcher.remember_patch(payload)
        if partial:
            first.write_text("value = 2\n", encoding="utf-8")
        payload["tool_response"] = {"exit_code": 1, "output": "failed to apply"}

        assert dispatcher.observe(payload) == {"blocking": [], "context": []}
        assert calls == ([str(first)] * 3 if partial else [])
        assert second.read_text(encoding="utf-8") == "value = 1\n"
        assert not dispatcher.patch_snapshot(payload).exists()

    def test_a_post_patch_without_its_own_snapshot_cannot_repair_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher = bundled_dispatcher()
        monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
        payload = {
            **patch_payload(
                "*** Begin Patch\n*** Add File: module.py\n+value = 1\n*** End Patch"
            ),
            "cwd": str(tmp_path),
            "session_id": "session",
            "tool_use_id": "first-call",
        }
        dispatcher.remember_patch(payload)
        (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")
        payload["tool_use_id"] = "different-call"

        assert (
            "no matching PreToolUse snapshot"
            in (dispatcher.observe(payload)["blocking"][0])
        )

    def test_native_pretool_entrypoint_records_only_allowed_calls(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        dispatcher = bundled_dispatcher()
        root = worktree(tmp_path / "feature")
        target = root / "module.py"
        target.write_text(GREETING, encoding="utf-8")
        monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
        payload = {
            **patch_payload(
                update_envelope(
                    str(target), ' def greet():\n-    return "hi"\n+    return "hello"'
                )
            ),
            "cwd": str(root),
            "session_id": "session",
            "tool_use_id": "allowed-call",
            "hook_event_name": "PreToolUse",
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        dispatcher.main()

        snapshot = dispatcher.patch_snapshot(payload)
        assert snapshot.is_file()
        assert str(target) in json.loads(snapshot.read_text())
        assert "return" not in snapshot.read_text()
        payload["hook_event_name"] = "PermissionRequest"
        target.write_text("a permission request must not replace the preimage\n")
        recorded = snapshot.read_text()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        dispatcher.main()
        assert (
            json.loads(capsys.readouterr().out)["hookSpecificOutput"]["decision"][
                "behavior"
            ]
            == "deny"
        )
        assert snapshot.read_text() == recorded

    def test_an_ordinary_small_edit_is_allowed(self, tmp_path: Path) -> None:
        root = worktree(tmp_path / "feature")
        target = root / "module.py"
        target.write_text(GREETING, encoding="utf-8")
        envelope = update_envelope(
            str(target), ' def greet():\n-    return "hi"\n+    return "hello"'
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "allow"

    def test_a_human_owned_file_is_still_protected_in_a_sibling_worktree(
        self, tmp_path: Path
    ) -> None:
        """The rule matches README.md repo-relative, not cwd-relative."""
        root = worktree(tmp_path / "feature")
        target = root / "README.md"
        target.write_text("# Title\n", encoding="utf-8")
        envelope = update_envelope(str(target), "-# Title\n+# Rewritten")

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect != "allow"

    def test_an_antipattern_is_denied_after_native_decoding(
        self, tmp_path: Path
    ) -> None:
        root = worktree(tmp_path / "feature")
        target = root / "module.py"
        target.write_text(GREETING, encoding="utf-8")
        envelope = update_envelope(
            str(target),
            '+import re\n def greet():\n     return "hi"',
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "deny"
        assert "import-re" in decision.reason

    def test_every_file_in_a_safe_multi_file_patch_is_allowed(
        self, tmp_path: Path
    ) -> None:
        root = worktree(tmp_path / "feature")
        first = root / "first.py"
        second = root / "second.py"
        first.write_text("value = 1\n", encoding="utf-8")
        second.write_text("value = 2\n", encoding="utf-8")
        envelope = (
            f"*** Begin Patch\n*** Update File: {first}\n@@\n"
            "-value = 1\n+value = 3\n"
            f"*** Update File: {second}\n@@\n"
            "-value = 2\n+value = 4\n*** End Patch"
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "allow"

    def test_a_protected_file_wins_a_multi_file_batch(self, tmp_path: Path) -> None:
        root = worktree(tmp_path / "feature")
        safe = root / "module.py"
        protected = root / "README.md"
        safe.write_text("value = 1\n", encoding="utf-8")
        protected.write_text("# Title\n", encoding="utf-8")
        envelope = (
            f"*** Begin Patch\n*** Update File: {safe}\n@@\n"
            "-value = 1\n+value = 2\n"
            f"*** Update File: {protected}\n@@\n"
            "-# Title\n+# Rewritten\n*** End Patch"
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "ask"

    def test_an_ordinary_delete_reaches_the_deletion_policy(
        self, tmp_path: Path
    ) -> None:
        root = worktree(tmp_path / "feature")
        target = root / "obsolete.py"
        target.write_text("value = 1\n", encoding="utf-8")
        envelope = f"*** Begin Patch\n*** Delete File: {target}\n*** End Patch"

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "allow"

    def test_a_move_checks_the_protected_source_path(self, tmp_path: Path) -> None:
        root = worktree(tmp_path / "feature")
        source = root / "README.md"
        destination = root / "README.old.md"
        source.write_text("# Title\n", encoding="utf-8")
        envelope = (
            f"*** Begin Patch\n*** Update File: {source}\n"
            f"*** Move to: {destination}\n@@\n # Title\n*** End Patch"
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "ask"
        assert "human-authored" in decision.reason

    def test_an_unparsable_envelope_never_reports_allow(self) -> None:
        with pytest.raises(ValueError):
            bundled_dispatcher().dispatch(patch_payload("garbage"))

    def test_an_unparsable_envelope_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = json.dumps(patch_payload("garbage"))
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

        with pytest.raises(SystemExit) as raised:
            bundled_dispatcher().main()

        assert raised.value.code == 2


def test_worktree_path_relativizes_against_the_holding_worktree(
    tmp_path: Path,
) -> None:
    root = worktree(tmp_path / "feature")

    resolved = bundled_dispatcher().worktree_path(str(root / "src" / "app.py"))

    assert resolved == "src/app.py"
