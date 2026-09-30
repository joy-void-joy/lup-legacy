"""What a parked question proposes, derived from the operation as submitted.

The surface a Codex whole-file review has is `review show`, because no
Codex hook can ask: an `ask` becomes a queued review that denies the call and
sends the operator to another terminal. What that terminal showed was the raw
payload with the preimage printed beneath it. These pin the derivation that
replaces it, and in particular that it is derived from the *record* — an
approval binds to the operation somebody was shown, not to whatever the file
says by the time they answer.
"""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from lup.policy.operations import Operation
from lup.types import JsonObject
from lup.policy.relay import CapturedFileReview, PersistentQuestion
from lup.policy.review import ReviewedFile, reviewed_files, spliced
from lup.providers.harness import patch_review

ROOT = Path("/repo")


def parked(
    tool: str,
    payload: JsonObject,
    preconditions: dict[Path, str | None] | None = None,
) -> PersistentQuestion:
    """One question as the relay would have recorded it."""
    operation = Operation(
        id="op",
        session="session",
        requester="session",
        tool=tool,
        payload=payload,
        cwd=ROOT,
        worktree=ROOT,
    )
    return PersistentQuestion(
        id="q",
        operation=operation,
        fingerprint=operation.fingerprint(),
        preconditions=preconditions or {},
        reason="review",
        created=datetime.now(UTC),
    )


def test_a_whole_file_write_carries_its_own_result() -> None:
    """The case the surface exists for: the payload already holds the after."""
    question = parked(
        "Write",
        {"file_path": "docs/plan.md", "content": "new\n"},
        {ROOT / "docs/plan.md": "old\n"},
    )

    [change] = reviewed_files(question)

    assert change.path == ROOT / "docs/plan.md"
    assert change.before == "old\n"
    assert change.after == "new\n"


def test_a_write_over_nothing_is_a_creation_rather_than_an_overwrite() -> None:
    """Absence and emptiness are different acts and get different words."""
    question = parked("Write", {"file_path": "new.md", "content": "hello\n"})

    [change] = reviewed_files(question)

    assert change.before is None
    assert change.operation() == "create"


def test_a_fragment_edit_is_spliced_against_the_captured_preimage() -> None:
    """Against the record, because that is what the reviewer is answering."""
    question = parked(
        "Edit",
        {"file_path": "a.py", "old_string": "one", "new_string": "two"},
        {ROOT / "a.py": "one and only\n"},
    )

    [change] = reviewed_files(question)

    assert change.path == ROOT / "a.py"
    assert change.after == "two and only\n"
    assert change.operation() == "modify"


def test_a_fragment_edit_with_no_captured_preimage_yields_no_diff() -> None:
    """Nothing to splice against is reported as no diff, never as a guess.

    Reading the file off disk instead would show a splice against a document
    the operation was never judged against, which is the one rendering a
    reviewer must not be given.
    """
    question = parked(
        "Edit", {"file_path": "a.py", "old_string": "one", "new_string": "two"}
    )

    assert reviewed_files(question) == []


def test_a_shell_command_has_nothing_to_diff_and_says_so_by_being_empty() -> None:
    """Empty means "not a file change", which is what the payload is for."""
    question = parked("Bash", {"command": "git status"})

    assert reviewed_files(question) == []


def test_an_edit_whose_preimage_no_longer_fits_is_unapplyable_not_fatal() -> None:
    """A record outlives the tree it was recorded against; a listing must not die."""
    assert spliced("nothing here\n", "absent", "x", False) is None


def test_an_ambiguous_fragment_applies_only_where_every_was_asked_for() -> None:
    """The Edit tool's own semantics: one occurrence, or explicitly all of them."""
    assert spliced("a a\n", "a", "b", False) is None
    assert spliced("a a\n", "a", "b", True) == "b b\n"


def test_an_overwrite_is_carried_because_the_documents_cannot_say() -> None:
    """A fragment edit that rewrites every line and a whole-file write agree."""
    replaced = ReviewedFile(path=Path("a"), before="x", after="y", overwrite=True)
    edited = ReviewedFile(path=Path("a"), before="x", after="y")

    assert replaced.operation() == "overwrite"
    assert edited.operation() == "modify"


def test_a_change_that_changes_nothing_is_reported_rather_than_diffed() -> None:
    """An empty diff reads as a rendering failure; saying so reads as an answer."""
    unchanged = ReviewedFile(path=Path("a"), before="same\n", after="same\n")

    assert unchanged.unchanged()


def test_the_diff_labels_absence_as_no_file_rather_than_an_empty_one() -> None:
    """What a reviewer must be able to tell apart at a glance."""
    created = ReviewedFile(path=Path("a.md"), after="hello\n")

    rendered = created.unified()

    assert "/dev/null" in rendered
    assert "+hello" in rendered


def test_every_diff_line_ends_in_a_newline_however_the_file_ended() -> None:
    """`difflib` emits its own marker mid-hunk, which reads as content."""
    change = ReviewedFile(path=Path("a"), before="one", after="two")

    rendered = change.unified()

    assert all(line for line in rendered.splitlines())
    assert rendered.endswith("\n")


def test_patch_review_uses_captured_preimages_after_the_file_changes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "design.md"
    target.write_text("another writer\n")
    command = "*** Begin Patch\n*** Update File: design.md\n@@\n-original\n+approved\n*** End Patch"
    [change] = patch_review(command, tmp_path, {target: "original\n"}, False)
    assert change.before == "original\n"
    assert change.after == "approved\n"
    assert target.read_text() == "another writer\n"


def test_shell_patch_review_reads_the_same_literal_envelope(tmp_path: Path) -> None:
    target = tmp_path / "design.md"
    command = "apply_patch <<'PATCH'\n*** Begin Patch\n*** Add File: design.md\n+approved\n*** End Patch\nPATCH"
    [change] = patch_review(command, tmp_path, {target: "original\n"}, True)
    assert change.before == "original\n"
    assert change.after == "approved\n"


def test_copy_review_shows_the_recorded_document_over_the_captured_destination(
    tmp_path: Path,
) -> None:
    """A copy is shown as the policy judged it, not as the source reads now."""
    source = tmp_path / "proposal.md"
    target = tmp_path / "design.md"
    source.write_text("changed proposal\n")
    target.write_text("changed destination\n")
    operation = Operation(
        id="op",
        session="session",
        requester="session",
        tool="Bash",
        payload={"command": "cp proposal.md design.md"},
        cwd=tmp_path,
        worktree=tmp_path,
    )
    question = PersistentQuestion(
        id="q",
        operation=operation,
        fingerprint=operation.fingerprint(),
        preconditions={source: "approved\n", target: "original\n"},
        file_reviews=[
            CapturedFileReview(
                path=target,
                effect="ask",
                reason="protected",
                rule="edit:protected-path",
                rules=[],
                before_sha256=sha256(b"original\n").hexdigest(),
                after_sha256=sha256(b"approved\n").hexdigest(),
                after="approved\n",
            )
        ],
        reason="review",
    )
    assert patch_review("cp proposal.md design.md", tmp_path, {}, True) == []
    [change] = reviewed_files(question, patch_review)
    assert change.before == "original\n"
    assert change.after == "approved\n"


def test_uncaptured_patch_never_reads_current_files(tmp_path: Path) -> None:
    (tmp_path / "design.md").write_text("original\n")
    command = "*** Begin Patch\n*** Add File: design.md\n+approved\n*** End Patch"
    assert patch_review(command, tmp_path, {}, False) == []


def test_a_change_to_another_checkout_is_labelled_with_that_checkout() -> None:
    """The label names where the call acts, not where the session that asked sits."""
    elsewhere = Operation(
        id="op",
        session="session",
        requester="session",
        tool="Edit",
        payload={},
        cwd=ROOT,
        worktree=Path("/tree/feature"),
    )
    beneath = elsewhere.model_copy(update={"cwd": ROOT / "packages", "worktree": ROOT})

    assert elsewhere.summary() == "Edit in /tree/feature"
    assert beneath.summary() == "Edit in /repo/packages"
