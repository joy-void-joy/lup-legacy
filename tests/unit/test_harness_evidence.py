"""Evidence-register version-drift trigger tests."""

from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import BaseModel, Field
import yaml

from lup.devtools.dev.git_guards import CHECK_COMMAND, DRIFT_COMMAND
from lup.devtools.dev.workflow import WORKFLOW_PATH, write_workflow
from lup_template.harness.catalog import WORKFLOW
from lup.harness.evidence import (
    EVIDENCE_REGISTER,
    SCHEMA_COMMAND,
    EvidenceEntry,
    SchemaDigest,
    accepted,
    cited_fixture,
    digest_drift,
    evidence_drift,
    parse_version,
    sdk_evidence_drift,
)

STALE_LEDGER = [
    EvidenceEntry(capability="codex-cli", version="0.144.4", refreshed="2026-06-01")
]


class WorkflowStep(BaseModel, frozen=True):
    """Workflow step fields relevant to native evidence execution."""

    name: str | None = None
    run: str | None = None


class WorkflowJob(BaseModel, frozen=True):
    """Workflow job fields that enforce evidence ordering."""

    needs: list[str] = Field(default_factory=list)
    condition: str | None = Field(default=None, alias="if")
    continue_on_error: bool = Field(default=False, alias="continue-on-error")
    steps: list[WorkflowStep]


class NativeWorkflow(BaseModel, frozen=True):
    """Validated native-workflow job graph."""

    jobs: dict[str, WorkflowJob]


def test_doctor_flags_an_installed_component_newer_than_the_register() -> None:
    drift = evidence_drift("codex-cli", "codex-cli 0.145.0", STALE_LEDGER)

    assert drift is not None
    assert drift.installed == "0.145.0"
    assert drift.accepted == "0.144.4"
    assert "re-probe" in drift.message
    assert "docs/native-capabilities.md" in drift.message


def test_matching_and_older_installed_versions_do_not_drift() -> None:
    assert evidence_drift("codex-cli", "codex-cli 0.144.4", STALE_LEDGER) is None
    assert evidence_drift("codex-cli", "codex-cli 0.143.9", STALE_LEDGER) is None


def test_unknown_capabilities_and_unparseable_banners_stay_silent() -> None:
    assert evidence_drift("novel-cli", "novel-cli 9.9.9", STALE_LEDGER) is None
    assert evidence_drift("codex-cli", "development build", STALE_LEDGER) is None


def test_version_parsing_handles_real_banner_shapes() -> None:
    assert parse_version("codex-cli 0.144.5") == [0, 144, 5]
    assert parse_version("2.1.211 (Claude Code)") == [2, 1, 211]
    assert parse_version("no digits here") is None


def test_longer_component_counts_compare_componentwise() -> None:
    register = [
        EvidenceEntry(capability="claude-cli", version="2.1", refreshed="2026-06-01")
    ]

    assert evidence_drift("claude-cli", "2.1.1", register) is not None
    assert evidence_drift("claude-cli", "2.1.0", register) is None


def test_sdk_drift_reads_the_installed_distribution() -> None:
    newer = [
        EvidenceEntry(
            capability="claude-agent-sdk", version="0.0.1", refreshed="2026-06-01"
        )
    ]
    ancient = [
        EvidenceEntry(
            capability="claude-agent-sdk", version="999.0.0", refreshed="2026-06-01"
        )
    ]

    drift = sdk_evidence_drift(newer)
    assert drift is not None and drift.capability == "claude-agent-sdk"
    assert sdk_evidence_drift(ancient) is None


def test_shipping_register_carries_every_probed_contract() -> None:
    capabilities = [entry.capability for entry in EVIDENCE_REGISTER]

    assert capabilities == ["claude-cli", "claude-agent-sdk", "codex-cli"]


def test_digest_drift_reports_a_schema_whose_content_moved(tmp_path: Path) -> None:
    accepted = [
        SchemaDigest(path="v2/Thread.json", sha256=sha256(b"first").hexdigest())
    ]
    target = tmp_path / "v2" / "Thread.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"first")

    assert digest_drift(tmp_path, accepted) == []

    target.write_bytes(b"second")
    drift = digest_drift(tmp_path, accepted)

    assert [item.path for item in drift] == ["v2/Thread.json"]
    assert drift[0].accepted == accepted[0].sha256


def test_digest_drift_reports_a_schema_the_generator_stopped_writing(
    tmp_path: Path,
) -> None:
    accepted = [SchemaDigest(path="v2/Gone.json", sha256=sha256(b"kept").hexdigest())]

    drift = digest_drift(tmp_path, accepted)

    assert [item.found for item in drift] == [sha256(b"").hexdigest()]


def test_the_page_and_the_probe_read_one_schema_command() -> None:
    spelled = SCHEMA_COMMAND.spelled("<temporary-directory>")

    assert spelled.startswith(SCHEMA_COMMAND.executable)
    assert spelled.endswith("<temporary-directory>")
    assert all(argument in spelled for argument in SCHEMA_COMMAND.arguments)


def test_accepted_refuses_a_capability_no_row_carries() -> None:
    assert accepted("codex-cli") == accepted("codex-cli", EVIDENCE_REGISTER)
    with pytest.raises(KeyError):
        accepted("gemini-cli")


def test_drift_names_the_reading_date_of_the_row_it_drifted_from() -> None:
    """One date over the register would move whenever any one probe ran.

    The failure it prevents is a reading nobody did: a Codex re-probe that
    also restamped Claude's row would tell an operator their Claude evidence
    was read on a day that describes somebody else's work. So the date the
    message quotes has to come from the row that drifted and from nowhere
    else, which is what a register of two differently-dated rows can show.
    """
    register = [
        EvidenceEntry(capability="claude-cli", version="1.0", refreshed="2026-01-01"),
        EvidenceEntry(capability="codex-cli", version="1.0", refreshed="2026-02-02"),
    ]

    drift = evidence_drift("codex-cli", "1.1", register)

    assert drift is not None
    assert "2026-02-02" in drift.message
    assert "2026-01-01" not in drift.message


def test_a_cited_fixture_that_moved_fails_generation(tmp_path: Path) -> None:
    """The citation is checked against the tree, not retyped beside it.

    A suite that moves leaves prose citing it reading exactly as it did while
    pointing at nothing, which is the one failure a reader cannot see. Asking
    the tree turns it into a generation error naming the path to repoint.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_present.py").write_text("", encoding="utf-8")

    assert cited_fixture(tmp_path, "tests/test_present.py") == "tests/test_present.py"
    with pytest.raises(ValueError, match="cited as evidence"):
        cited_fixture(tmp_path, "tests/test_moved_away.py")


def test_native_workflow_probes_even_when_strict_evidence_fails() -> None:
    document = yaml.safe_load(
        Path(".github/workflows/native-nightly.yml").read_text(encoding="utf-8")
    )
    workflow = NativeWorkflow.model_validate(document)
    evidence = workflow.jobs["evidence"]
    native = workflow.jobs["native"]

    assert any(
        step.run == "uv run lup-devtools harness doctor all --strict-evidence"
        for step in evidence.steps
    )
    assert native.needs == ["evidence", "credentials"]
    assert native.condition is not None
    assert "always()" in native.condition
    assert "needs.credentials.outputs.live == 'true'" in native.condition
    assert native.continue_on_error is False
    assert any(step.run == "uv run pytest -m integration -v" for step in native.steps)


def test_pull_request_workflow_runs_the_same_gate_a_checkout_runs() -> None:
    """The gate is one command, so what CI enforces cannot drift from it.

    Every row it could spell out is a row of that command, harness drift
    included; naming them again here is what lets the two lists disagree. The
    drift step ahead of it is not a second list: it is the constant the commit
    hook installs, so a contributor who never armed the hook meets the same
    refusal here.
    """
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    workflow = NativeWorkflow.model_validate(document)
    commands = [step.run for step in workflow.jobs["check"].steps if step.run]

    # The frontend install is the one step declared rather than constant: a
    # project with a bun workspace restores it from the lockfile before the
    # gate rebuilds the bundles it compares against what is committed.
    frontend = (
        ["bun install --frozen-lockfile"] if WORKFLOW.frontend is not None else []
    )
    assert commands == [
        *frontend,
        "uv sync --all-extras",
        "uv run lup-devtools git merge-driver",
        DRIFT_COMMAND,
        *(step.run for step in WORKFLOW.namespace_steps()),
        CHECK_COMMAND,
    ]


def test_the_workflow_on_disk_is_the_one_the_declaration_renders() -> None:
    """Generated rather than scaffolded, so `dev check` reports it when it drifts."""
    write_workflow(WORKFLOW, check=True)
