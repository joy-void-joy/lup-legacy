"""A claim's standing read from its evidence, and a document held to it.

Written against the one failure, met twice, that the design answers: a claim
kept a label after the thing supporting it went away, and nothing reported
the difference. Every test here is a way that can happen, and the system
noticing.
"""

from pathlib import Path

import pytest

from lup.coordination.refs import ActorRef
from lup.ledger.journal import LedgerRefusal, LedgerStore
from lup.ledger.models import LedgerNode
from lup.ledger.cite import cites_in, read_cites
from lup.formats import digest
from lup_template.corpus import (
    Artifact,
    Claim,
    Correction,
    Refutes,
    Scoped,
    Source,
    Supersedes,
    Supports,
    Validation,
    Verifies,
)

CLASSES: list[type[LedgerNode]] = [Claim, Correction, Artifact, Source]


def opened(root: Path, who: str = "alice") -> LedgerStore:
    return LedgerStore(root, ActorRef(kind="session", id=who))


def backed(root: Path, store: LedgerStore, scope: Path) -> tuple[Claim, Artifact]:
    """A claim with one supporting artifact checked against one file."""
    claim = store.record(Claim, "quotes nest to depth two", text="2", grade="measured")
    validation = Validation(
        schema_id="pytest",
        subject_digest="abc",
        scope=[
            Scoped(path=str(scope.relative_to(root)), digest=digest.file(scope) or "")
        ],
    )
    artifact = store.record(
        Artifact, "run.log", attachments=[b"ok"], validation=validation.model_dump()
    )
    store.relate(Supports, artifact, claim)
    return claim, artifact


def test_a_claim_with_no_evidence_says_so_and_still_stands(tmp_path: Path) -> None:
    store = opened(tmp_path)
    claim = store.record(Claim, "the parser is total")

    reading = store.standing(claim, CLASSES)

    assert reading.label == "unsupported" and reading.sound


def test_supported_while_its_scope_is_unchanged_and_stale_the_moment_it_is(
    tmp_path: Path,
) -> None:
    """The label was never stored, so it cannot outlive the file it rested on."""
    scope = tmp_path / "parser.py"
    scope.write_text("v1", encoding="utf-8")
    store = opened(tmp_path)
    claim, _artifact = backed(tmp_path, store, scope)

    assert store.standing(claim, CLASSES).label == "supported"

    scope.write_text("v2", encoding="utf-8")
    reading = store.standing(claim, CLASSES)

    assert reading.label == "stale" and not reading.sound
    assert "parser.py changed" in reading.reason


def test_a_live_counterexample_beside_live_support_is_reported_as_both(
    tmp_path: Path,
) -> None:
    """Refusing to pick a side is the point: shown one and not the other, a
    reader is shown a lie by omission.
    """
    scope = tmp_path / "parser.py"
    scope.write_text("v1", encoding="utf-8")
    store = opened(tmp_path)
    claim, _artifact = backed(tmp_path, store, scope)
    counter = store.record(
        Artifact,
        "counter.log",
        attachments=[b"depth 3 fails"],
        validation=Validation(schema_id="pytest", subject_digest="d").model_dump(),
    )
    store.relate(Refutes, counter, claim)

    reading = store.standing(claim, CLASSES)

    assert reading.label == "contradicted" and not reading.sound


def test_a_correction_supersedes_and_outranks_every_piece_of_evidence(
    tmp_path: Path,
) -> None:
    scope = tmp_path / "parser.py"
    scope.write_text("v1", encoding="utf-8")
    store = opened(tmp_path)
    claim, _artifact = backed(tmp_path, store, scope)
    correction = store.record(
        Correction, "depth is three", where="README", wrong="said two"
    )
    store.relate(
        Supersedes, correction, claim, changes=["the figure"], survives=["the method"]
    )

    reading = store.standing(claim, CLASSES)

    assert reading.label == "superseded" and not reading.sound
    assert correction.id in reading.reason


def test_evidence_a_correction_retired_reads_superseded_and_stops_counting(
    tmp_path: Path,
) -> None:
    """An artifact whose log recorded the wrong thing is retired the way a
    claim is, before its digests get a word, and the claim it backed no
    longer counts it: only what stands supports.
    """
    scope = tmp_path / "parser.py"
    scope.write_text("v1", encoding="utf-8")
    store = opened(tmp_path)
    claim, artifact = backed(tmp_path, store, scope)
    correction = store.record(
        Correction, "the log was a collection error", where="run.log", wrong="no run"
    )
    store.relate(
        Supersedes, correction, artifact, changes=["the attached log and its digest"]
    )

    retired = store.standing(artifact, CLASSES)
    assert retired.label == "superseded" and not retired.sound
    assert correction.id in retired.reason

    withered = store.standing(claim, CLASSES)
    assert withered.label == "stale" and not withered.sound
    assert correction.id in withered.reason

    _claim, second = backed(tmp_path, store, scope)
    store.relate(Supports, second, claim)
    reading = store.standing(claim, CLASSES)

    assert reading.label == "supported" and reading.reason == "1 artifact(s) stand"


def test_a_source_a_correction_retired_reads_superseded(tmp_path: Path) -> None:
    store = opened(tmp_path)
    source = store.record(Source, "the paper", origin="https://example.org/p")

    assert store.standing(source, CLASSES).label == "recorded"

    correction = store.record(Correction, "wrong paper", where="refs", wrong="p")
    store.relate(Supersedes, correction, source, changes=["the origin"])
    reading = store.standing(source, CLASSES)

    assert reading.label == "superseded" and not reading.sound
    assert correction.id in reading.reason


def test_a_correction_that_changes_nothing_is_refused_at_the_edge(
    tmp_path: Path,
) -> None:
    store = opened(tmp_path)
    claim = store.record(Claim, "the figure")
    correction = store.record(Correction, "note", where="x", wrong="y")

    with pytest.raises(ValueError):
        store.relate(Supersedes, correction, claim, changes=[])


def test_an_author_cannot_verify_their_own_claim_and_somebody_else_can(
    tmp_path: Path,
) -> None:
    """The one rule kept about who may say what, and it lives on the edge."""
    alice = opened(tmp_path, "alice")
    claim = alice.record(Claim, "the parser is total")

    with pytest.raises(LedgerRefusal, match="own work"):
        alice.relate(Verifies, claim, claim)

    bob = opened(tmp_path, "bob")
    bob.relate(Verifies, claim, claim)
    scope = tmp_path / "parser.py"
    scope.write_text("v1", encoding="utf-8")
    _claim, artifact = backed(tmp_path, bob, scope)
    bob.relate(Supports, artifact, claim)

    assert bob.standing(claim, CLASSES).label == "verified"


def test_a_cite_is_found_through_the_parser_and_not_inside_a_fence() -> None:
    text = "See [the depth](lup:abc123) here.\n\n```\n[not one](lup:zzz)\n```\n"

    [cite] = cites_in(text)

    assert cite.node_id == "abc123" and cite.label == "the depth" and cite.line == 1


def test_a_cite_of_a_corrected_claim_fails_and_of_a_sound_one_holds(
    tmp_path: Path,
) -> None:
    store = opened(tmp_path)
    good = store.record(Claim, "depth", text="2")
    bad = store.record(Claim, "width", text="9")
    correction = store.record(Correction, "width is 7", where="doc", wrong="9")
    store.relate(Supersedes, correction, bad, changes=["the figure"])
    text = f"Depth is [two](lup:{good.id}) and width [nine](lup:{bad.id}).\n"

    readings = read_cites(text, store, CLASSES)

    assert [reading.holds() for reading in readings] == [True, False]
    assert "superseded" in readings[1].problem()


def test_a_cite_of_nothing_is_a_failure_that_names_the_id(tmp_path: Path) -> None:
    store = opened(tmp_path)

    [reading] = read_cites("[x](lup:nope)", store, CLASSES)

    assert not reading.holds() and "'nope'" in reading.problem()
