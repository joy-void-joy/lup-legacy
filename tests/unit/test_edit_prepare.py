"""Preparation preserves exact content and never supplies review authority."""

from pathlib import Path

import pytest
import typer
from pydantic import ValidationError

import lup.devtools.dev.edit_prepare as preparing
from lup.devtools.project import DevProject
from lup.harness.codescan.antipatterns import RuleSet
from lup.harness.codescan.oracle import (
    ClassDeclaration,
    Declaration,
    FunctionDeclaration,
    SourceBuffer,
    SymbolQuery,
    TypeOracle,
    UnknownDeclaration,
)
from lup.harness.models import HookPathRole, HookSet
from lup.policy.models import EditBatch, EditChange
from lup.providers.harness import patch_review


class Oracle(TypeOracle):
    def __init__(self, declaration: Declaration) -> None:
        self.declaration = declaration
        self.buffers: list[SourceBuffer] = []

    def declarations(
        self, queries: list[SymbolQuery], buffers: list[SourceBuffer] | None = None
    ) -> list[Declaration]:
        self.buffers = buffers or []
        return [self.declaration for _ in queries]


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def project() -> DevProject:
    return DevProject(
        package="example", path_roles=[{"root": "tmp", "role": "scratch"}]
    )


@pytest.fixture
def hooks() -> HookSet:
    return HookSet(
        id="test",
        policy_ids=[],
        path_roles=[HookPathRole(root=Path("tmp"), role="scratch")],
    )


@pytest.fixture(autouse=True)
def scoped_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = RuleSet()
    selected = RuleSet(
        python=[
            rule
            for rule in rules.python
            if rule.id in {"import-re", "subprocess", "dict-get", "typing-generics"}
        ],
        typescript=[],
        project=[],
        composition=[],
    )
    monkeypatch.setattr(preparing, "declared_rules", lambda project: selected)


def proposal(
    text: str, before: str | None = None, path: str = "sample.py"
) -> EditBatch:
    return EditBatch(changes=[EditChange(path=Path(path), before=before, after=text)])


def test_all_findings_arrive_before_any_write(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    report = preparing.prepare_edit(
        proposal("import re\nimport subprocess\n"), project, hooks, None, [], []
    )
    assert {row.rule_id for row in report.findings} == {"import-re", "subprocess"}
    assert report.patch is None
    assert not (checkout / "sample.py").exists()
    assert not (checkout / ".lup/questions.jsonl").exists()


def test_explicit_exception_is_prepared_for_one_review(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    request = preparing.SuppressionRequest(
        path=Path("sample.py"),
        line=1,
        rule_id="import-re",
        reason="This module implements the grammar itself.",
    )
    report = preparing.prepare_edit(
        proposal("import re\n"), project, hooks, None, [], [request]
    )
    assert report.patch is not None
    assert "lup: ignore[import-re]" in report.patch
    assert report.readings[0].effect == "ask"
    assert not report.findings
    assert not (checkout / "sample.py").exists()
    assert not (checkout / ".lup/questions.jsonl").exists()


def test_multiple_requested_rules_merge_one_existing_marker(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    text = "import re, subprocess  # lup: ignore[import-re] — owns grammar\n"
    request = preparing.SuppressionRequest(
        path=Path("sample.py"),
        line=1,
        rule_id="subprocess",
        reason="Needs the standard library process contract.",
    )
    report = preparing.prepare_edit(proposal(text), project, hooks, None, [], [request])
    assert report.patch is not None
    assert "ignore[import-re, subprocess]" in report.patch
    assert "owns grammar" in report.patch


@pytest.mark.parametrize("reason", ["", "  ", "first\nsecond"])
def test_suppression_reason_is_explicit_and_single_line(reason: str) -> None:
    with pytest.raises(ValidationError):
        preparing.SuppressionRequest(
            path=Path("sample.py"), line=1, rule_id="import-re", reason=reason
        )


def test_strong_rule_cannot_be_suppressed(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    request = preparing.SuppressionRequest(
        path=Path("sample.py"),
        line=1,
        rule_id="typing-generics",
        reason="A requested exception.",
    )
    with pytest.raises(ValueError, match="not a proven"):
        preparing.prepare_edit(
            proposal("value: List[str]\n"), project, hooks, None, [], [request]
        )


@pytest.mark.parametrize(
    "declaration",
    [
        FunctionDeclaration(name="get", path=Path("client.py"), line=1),
        UnknownDeclaration(),
    ],
)
def test_refuted_and_unresolved_sites_cannot_be_suppressed(
    checkout: Path, project: DevProject, hooks: HookSet, declaration: Declaration
) -> None:
    oracle = Oracle(declaration)
    request = preparing.SuppressionRequest(
        path=Path("sample.py"),
        line=1,
        rule_id="dict-get",
        reason="An explicit request.",
    )
    with pytest.raises(ValueError, match="not a proven|unresolved"):
        preparing.prepare_edit(
            proposal("value = client.get('name')\n"),
            project,
            hooks,
            oracle,
            [],
            [request],
        )
    assert oracle.buffers[0].text == "value = client.get('name')\n"


def test_proven_mapping_site_accepts_explicit_suppression(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    oracle = Oracle(
        ClassDeclaration(name="dict", bases=[], path=Path("builtins.pyi"), line=1)
    )
    request = preparing.SuppressionRequest(
        path=Path("sample.py"), line=1, rule_id="dict-get", reason="Open external keys."
    )
    report = preparing.prepare_edit(
        proposal("value = payload.get('name')\n"), project, hooks, oracle, [], [request]
    )
    assert report.patch is not None
    assert not report.findings


def test_unavailable_resolution_is_visible_without_automatic_suppression(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    report = preparing.prepare_edit(
        proposal("value = payload.get('name')\n"), project, hooks, None, [], []
    )
    assert [(row.kind, row.rule_id) for row in report.findings] == [
        ("unresolved", "dict-get")
    ]
    assert report.readings[0].effect == "ask"
    assert report.patch is not None and "lup: ignore" not in report.patch


@pytest.mark.parametrize(
    "before,after",
    [
        (None, ""),
        (None, "no newline"),
        (None, "crlf\r\n"),
        ("old\n", "new\r\n"),
        ("old\n", "new"),
    ],
)
def test_unrepresentable_documents_fail_round_trip(
    checkout: Path, before: str | None, after: str
) -> None:
    batch = EditBatch(
        cwd=checkout,
        changes=[
            EditChange(
                path=checkout / "sample.txt",
                before=before,
                after=after,
                operation="create" if before is None else "modify",
            )
        ],
    )
    with pytest.raises(ValueError, match="cannot preserve"):
        preparing.native_patch(batch)


@pytest.mark.parametrize(
    "before,after,operation",
    [
        (None, "\n", "create"),
        ("old\n", "", "modify"),
        ("", "new\n", "modify"),
        ("old", "new\n", "modify"),
        ("same", "same\n", "modify"),
        ("old\n", None, "delete"),
        ("old\n", "new\n", "overwrite"),
    ],
)
def test_exact_native_round_trip(
    checkout: Path, before: str | None, after: str | None, operation: str
) -> None:
    batch = EditBatch.model_validate(
        {
            "cwd": checkout,
            "changes": [
                {
                    "path": checkout / "sample.txt",
                    "before": before,
                    "after": after,
                    "operation": operation,
                }
            ],
        }
    )
    patch = preparing.native_patch(batch)
    rows = patch_review(patch, checkout, {checkout / "sample.txt": before}, False)
    assert len(rows) == 1
    assert (rows[0].before, rows[0].after, rows[0].operation()) == (
        before,
        after,
        operation,
    )
    assert "@@ -" not in patch


def test_stale_preimage_fails_before_audit(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    (checkout / "sample.py").write_text("newer\n")
    with pytest.raises(ValueError, match="preimage"):
        preparing.prepare_edit(
            proposal("proposed\n", "older\n"), project, hooks, None, [], []
        )


def test_symlink_escape_and_duplicate_targets_are_refused(
    checkout: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (checkout / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside this checkout"):
        preparing.concrete_batch(proposal("new\n", path="escape/file.py"), checkout)
    batch = EditBatch(
        changes=[
            EditChange(path=Path("sample.py"), after="x\n"),
            EditChange(path=checkout / "sample.py", after="y\n"),
        ]
    )
    with pytest.raises(ValueError, match="exactly once"):
        preparing.concrete_batch(batch, checkout)


def test_inconsistent_operation_is_refused(checkout: Path) -> None:
    (checkout / "sample.py").write_text("old\n")
    batch = EditBatch(
        changes=[
            EditChange(
                path=Path("sample.py"),
                before="old\n",
                after="new\n",
                operation="create",
            )
        ]
    )
    with pytest.raises(ValueError, match="operation disagrees"):
        preparing.concrete_batch(batch, checkout)


def test_cli_writes_only_fresh_scratch_artifact(
    checkout: Path,
    project: DevProject,
    hooks: HookSet,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (checkout / "batch.json").write_text(proposal("print('hello')\n").model_dump_json())
    monkeypatch.setattr(preparing, "default_oracle", lambda: None)
    monkeypatch.setattr(preparing, "scanned_files", lambda project: [])
    preparing.run(
        Path("batch.json"), Path("tmp/ready.patch"), None, project, hooks, False
    )
    assert "No review is queued by this preview" in capsys.readouterr().out
    assert (checkout / "tmp/ready.patch").is_file()
    assert not (checkout / "sample.py").exists()
    assert not (checkout / ".lup/questions.jsonl").exists()
    with pytest.raises(typer.BadParameter, match="already exists"):
        preparing.run(
            Path("batch.json"), Path("tmp/ready.patch"), None, project, hooks, False
        )
    with pytest.raises(typer.BadParameter, match="scratch"):
        preparing.run(
            Path("batch.json"), Path("source.patch"), None, project, hooks, False
        )


def test_output_cannot_write_a_proposed_target(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    (checkout / "batch.json").write_text(
        proposal("body\n", path="tmp/target.patch").model_dump_json()
    )
    with pytest.raises(typer.BadParameter, match="edit target"):
        preparing.run(
            Path("batch.json"), Path("tmp/target.patch"), None, project, hooks, False
        )


def test_output_parent_cannot_create_a_proposed_target(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    (checkout / "batch.json").write_text(
        proposal("body\n", path="tmp/new_target").model_dump_json()
    )
    with pytest.raises(typer.BadParameter, match="beneath one"):
        preparing.run(
            Path("batch.json"),
            Path("tmp/new_target/proposal.patch"),
            None,
            project,
            hooks,
            False,
        )
    assert not (checkout / "tmp/new_target").exists()


def test_quoted_marker_is_not_merged(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    text = 'import re; label = "# lup: ignore[not-a-rule]"\n'
    request = preparing.SuppressionRequest(
        path=Path("sample.py"),
        line=1,
        rule_id="import-re",
        reason="The grammar is the subject.",
    )
    report = preparing.prepare_edit(proposal(text), project, hooks, None, [], [request])
    assert report.patch is not None
    assert 'label = "# lup: ignore[not-a-rule]"' in report.patch
    assert "ignore[import-re]" in report.patch
    assert "ignore[import-re, not-a-rule]" not in report.patch


def test_unresolved_evidence_wins_over_a_missing_finding(checkout: Path) -> None:
    batch = preparing.concrete_batch(proposal("import re\n"), checkout)
    missing = preparing.EditFinding(
        path=Path("sample.py"),
        line=1,
        rule_id="import-re",
        kind="missing",
        message="missing",
        suppressible=True,
    )
    unresolved = missing.model_copy(
        update={"kind": "unresolved", "suppressible": False}
    )
    audit = preparing.CandidateAudit(
        findings=[missing, unresolved], refutations={}, resolved=True
    )
    request = preparing.SuppressionRequest(
        path=Path("sample.py"), line=1, rule_id="import-re", reason="Explicit reason."
    )
    with pytest.raises(ValueError, match="unresolved"):
        preparing.requested_suppressions(batch, [request], audit)


@pytest.mark.parametrize("ending", ["\r\n", "\r"])
def test_explicit_insertion_does_not_silently_normalize_line_endings(
    checkout: Path, project: DevProject, hooks: HookSet, ending: str
) -> None:
    request = preparing.SuppressionRequest(
        path=Path("sample.py"), line=1, rule_id="import-re", reason="Explicit reason."
    )
    with pytest.raises(ValueError, match="cannot preserve non-LF"):
        preparing.prepare_edit(
            proposal(f"import re{ending}"), project, hooks, None, [], [request]
        )


def test_native_patch_uses_compact_hunks_without_unified_line_ranges(
    checkout: Path,
) -> None:
    lines = [f"line {number}\n" for number in range(1000)]
    before = "".join(lines)
    lines[500] = "revised\n"
    after = "".join(lines)
    (checkout / "sample.txt").write_text(before)
    batch = preparing.concrete_batch(
        EditBatch(
            changes=[EditChange(path=Path("sample.txt"), before=before, after=after)]
        ),
        checkout,
    )

    patch = preparing.native_patch(batch)

    assert len(patch) < 500
    assert "\n@@\n" in patch
    assert "@@ -" not in patch
    assert (
        patch_review(patch, checkout, {checkout / "sample.txt": before}, False)[0].after
        == after
    )


def test_ambiguous_native_context_expands_until_exact(checkout: Path) -> None:
    block = [f"repeat {number}\n" for number in range(12)]
    lines = ["first\n", *block, "second\n", *block, "end\n"]
    before = "".join(lines)
    lines[20] = "revised\n"
    after = "".join(lines)
    (checkout / "sample.txt").write_text(before)
    batch = preparing.concrete_batch(
        EditBatch(
            changes=[EditChange(path=Path("sample.txt"), before=before, after=after)]
        ),
        checkout,
    )

    patch = preparing.native_patch(batch)

    assert (
        patch_review(patch, checkout, {checkout / "sample.txt": before}, False)[0].after
        == after
    )


def test_deleted_candidate_is_hidden_from_type_resolution(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    (checkout / "dependency.py").write_text("class Dict(dict): pass\n")
    oracle = Oracle(UnknownDeclaration())
    batch = EditBatch(
        changes=[
            EditChange(
                path=Path("sample.py"),
                after="from dependency import Dict\nvalue = Dict().get('name')\n",
            ),
            EditChange(
                path=Path("dependency.py"),
                before="class Dict(dict): pass\n",
                after=None,
            ),
        ]
    )
    preparing.prepare_edit(batch, project, hooks, oracle, [], [])
    assert any(
        buffer.path == Path("dependency.py") and buffer.text == ""
        for buffer in oracle.buffers
    )


def test_target_change_during_resolution_invalidates_preparation(
    checkout: Path, project: DevProject, hooks: HookSet
) -> None:
    target = checkout / "sample.py"
    target.write_text("before\n")

    class ConcurrentOracle(Oracle):
        def declarations(
            self, queries: list[SymbolQuery], buffers: list[SourceBuffer] | None = None
        ) -> list[Declaration]:
            target.write_text("another writer\n")
            return super().declarations(queries, buffers)

    with pytest.raises(ValueError, match="preimage"):
        preparing.prepare_edit(
            proposal("value = client.get('name')\n", "before\n"),
            project,
            hooks,
            ConcurrentOracle(UnknownDeclaration()),
            [],
            [],
        )
