"""One review for a batch of edits a session wrote under scratch.

A session making several edits the operator has to see parks one question
per edit, and each is answered alone -- against a file the next edit then
moves, which stales the rest. So a session writes the new versions under
scratch instead, at the path each has in its checkout (`tmp/cdx/<path>`),
tests them there, and `review propose tmp/cdx --why "…"` parks one review
holding every file.

Each file meets the edit gates a direct write of it would meet -- protected
paths, anti-patterns, markers, size -- and the review records each verdict:
a file the gates refuse outright refuses the whole proposal, naming it; one
they would let through is carried in the review all the same, folded on the
page where it needs no reading. What the files stand as now is recorded, and
an approval releases all of them or none: the requester's waiter writes
them only where every recorded file still stands as recorded, and a
proposal a file moved under goes stale whole.

What the agent says about it is its own: ``--why`` for the whole, and a
note per file in the directory's own manifest, ``.proposal.json``, which
also names the files the proposal deletes -- a deletion has no document to
write under scratch, so it is declared::

    {"about": {"src/app/host.py": "`patched_documents` now reads all the
                files first and only then writes them, replacing a 3-way
                if/elif. Same behaviour."},
     "delete": ["docs/retired.md"]}

(one line per note in the real file). The operator decides from these
words, so they are plain: what changes, then why, naming the file, function
or command, and whether behaviour changes. `review propose --help` gives the
rule with an example of each kind; a proposal of several files where one has
no note parks all the same, with a warning naming it.
"""

from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from lup.coordination.identity import session_member_id
from lup.devtools.dev.policy_explain import session_placement
from lup.harness.enforcement import semantic_policy_for
from lup.harness.models import HookSet
from lup.policy.models import Decision, EditBatch, EditChange
from lup.policy.operations import Operation
from lup.policy.relay import (
    Account,
    CapturedFileReview,
    PersistentQuestion,
    QuestionRelay,
)
from lup.policy.review import FilePreview, ReviewedFile, reviewed_preview
from lup.providers.harness import patch_review
from lup.providers.identity import native_session_ids

# lup: ignore[constant-declaration] — an identity this module defines: the name
# a session writes its notes under, which the guidance and the docs spell too
MANIFEST = ".proposal.json"
"""The file in a proposal's directory that notes its files and names its deletions."""

# lup: ignore[constant-declaration] — an identity this module defines, by which
# the page and the waiter tell a proposal from a runtime's own call
PROPOSE_TOOL = "Propose"
"""The tool a proposal's operation names, which no runtime has."""


class ProposalManifest(BaseModel, frozen=True, extra="forbid"):
    """What a proposal's directory says beside the documents it holds."""

    about: dict[Path, str] = {}
    """A note on each file, by its path in the checkout: one or two plain
    sentences saying what changes in it and whether behaviour does."""

    delete: list[Path] = []
    """The files in the checkout the proposal deletes."""


class ProposedFile(BaseModel, frozen=True):
    """One file of a proposal: where it lands, what it becomes, and what the agent says of it."""

    path: Path
    content: str | None
    """The document it becomes; ``None`` where the proposal deletes it."""

    about: str = ""


class Proposal(BaseModel, frozen=True):
    """What a proposal's operation carries: why, and every file it writes or deletes."""

    why: str
    files: list[ProposedFile]

    def unnoted(self) -> list[Path]:
        """The files the operator would read with no note, where there are several to tell apart.

        One file needs none: ``--why`` already speaks for it.
        """
        return (
            [proposed.path for proposed in self.files if not proposed.about.strip()]
            if len(self.files) > 1
            else []
        )

    def reviewed(self, preconditions: dict[Path, str | None]) -> list[ReviewedFile]:
        """Each file's documents on either side, the before side as the review recorded it."""
        return [
            ReviewedFile(
                path=proposed.path,
                before=preconditions[proposed.path]
                if proposed.path in preconditions
                else None,
                after=proposed.content,
                overwrite=proposed.path in preconditions
                and preconditions[proposed.path] is not None,
            )
            for proposed in self.files
        ]


def proposal_of(question: PersistentQuestion) -> Proposal | None:
    """The proposal a question carries, or ``None`` where it is not one."""
    if question.operation.tool != PROPOSE_TOOL:
        return None
    try:
        return Proposal.model_validate(question.operation.payload)
    except ValidationError:
        return None


def previewed(question: PersistentQuestion) -> FilePreview:
    """Every file change a parked question proposes, as before/after pairs.

    A proposal carries its documents whole, as a write does; every other
    question is read as the native call it parked implies, patch envelopes
    included -- which is why the one place both are asked is here, where a
    provider's patch reader is already named.
    """
    proposal = proposal_of(question)
    if proposal is not None:
        return FilePreview(files=proposal.reviewed(question.preconditions))
    return reviewed_preview(question, patch_review)


class ProposalRefused(ValueError):
    """A proposal that cannot be parked, saying which file and why."""


def gathered(root: Path, directory: Path, why: str) -> Proposal:
    """Every file under *directory* mapped onto *root*, and the manifest's deletions.

    A file's path under the directory is its path in the checkout. A file
    that is not UTF-8 text, a link, or a manifest naming a file the
    proposal does not hold, is refused rather than guessed at.
    """
    if not why.strip():
        raise ProposalRefused(
            "--why is empty: say in a short paragraph what the change does and "
            "why. `review propose --help` shows how."
        )
    if not directory.is_dir():
        raise ProposalRefused(f"{directory} is not a directory of proposed files")
    manifest_path = directory / MANIFEST
    try:
        manifest = (
            ProposalManifest.model_validate_json(manifest_path.read_text())
            if manifest_path.is_file()
            else ProposalManifest()
        )
    except ValidationError as error:
        raise ProposalRefused(f"{manifest_path} does not read: {error}") from error
    found = sorted(
        path
        for path in directory.rglob("*")
        if (path.is_file() or path.is_symlink()) and path != manifest_path
    )
    linked = [path for path in found if path.is_symlink()]
    if linked:
        raise ProposalRefused(
            "a proposal holds documents, not links: "
            + ", ".join(str(path) for path in linked)
        )

    def text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ProposalRefused(f"{path} is not UTF-8 text: {error}") from error

    written = {path.relative_to(directory): text(path) for path in found}
    unknown = [
        str(path)
        for path in manifest.about
        if path not in written and path not in manifest.delete
    ]
    if unknown:
        raise ProposalRefused(
            f"{MANIFEST} notes files the proposal does not hold: {', '.join(unknown)}"
        )
    missing = [str(path) for path in manifest.delete if not (root / path).is_file()]
    if missing:
        raise ProposalRefused(
            f"{MANIFEST} deletes files the checkout does not hold: {', '.join(missing)}"
        )
    if not written and not manifest.delete:
        raise ProposalRefused(f"{directory} proposes nothing: it holds no file")
    return Proposal(
        why=why,
        files=[
            *(
                ProposedFile(
                    path=root / relative,
                    content=content,
                    about=manifest.about[relative]
                    if relative in manifest.about
                    else "",
                )
                for relative, content in written.items()
            ),
            *(
                ProposedFile(
                    path=root / relative,
                    content=None,
                    about=manifest.about[relative]
                    if relative in manifest.about
                    else "",
                )
                for relative in manifest.delete
            ),
        ],
    )


def judged(root: Path, hooks: HookSet, proposal: Proposal) -> list[Decision]:
    """Each file's verdict under the edit gates this session's direct write of it meets."""
    placement = session_placement(root)
    policy = semantic_policy_for(
        hooks,
        sandbox_active=placement.sandboxed,
        escapable=placement.host_executor,
        contained=placement.contained,
        inside_placement=placement.inside_placement,
        unjudged_ambient=placement.unjudged,
    )

    def change(proposed: ProposedFile) -> EditChange:
        """The change one proposed file makes, judged as the edit a session would make.

        A modification rather than a whole-file write where a file stands:
        the session writes the document whole only because scratch is where
        it can test it, and the gates read what changed, line by line, as
        they read an `Edit` -- the same size budget, the same markers.
        """
        before = (
            proposed.path.read_text(encoding="utf-8")
            if proposed.path.is_file()
            else None
        )
        match proposed.content, before:
            case None, _:
                operation = "delete"
            case _, None:
                operation = "create"
            case _:
                operation = "modify"
        return EditChange(
            path=proposed.path,
            before=before,
            after=proposed.content,
            operation=operation,
        )

    return [
        policy.decide(EditBatch(cwd=root, changes=[change(proposed)]))
        for proposed in proposal.files
    ]


def parked(
    root: Path, hooks: HookSet, proposal: Proposal, relay: QuestionRelay
) -> PersistentQuestion:
    """Park one review holding every file of *proposal*, or refuse it naming what the gates refuse."""
    verdicts = judged(root, hooks, proposal)
    refused = [
        f"{proposed.path}: {verdict.reason}"
        for proposed, verdict in zip(proposal.files, verdicts, strict=True)
        if verdict.effect == "deny"
    ]
    if refused:
        raise ProposalRefused(
            "the edit gates refuse what this proposal writes, so it is not parked:\n  "
            + "\n  ".join(refused)
        )
    asking = [
        (proposed, verdict)
        for proposed, verdict in zip(proposal.files, verdicts, strict=True)
        if verdict.effect == "ask"
    ]
    reason = (
        f"a proposal of {len(proposal.files)} "
        f"{'file' if len(proposal.files) == 1 else 'files'}: "
        + (
            "; ".join(
                f"{proposed.path.relative_to(root)} — {verdict.reason}"
                for proposed, verdict in asking
            )
            if asking
            else "the edit gates would let every file through, and the session asked anyway"
        )
    )
    preconditions = {
        proposed.path: proposed.path.read_text(encoding="utf-8")
        if proposed.path.is_file()
        else None
        for proposed in proposal.files
    }
    sessions = native_session_ids()
    operation = Operation(
        id=uuid4().hex,
        session=sessions[0] if sessions else "",
        requester=sessions[0] if sessions else session_member_id(),
        tool=PROPOSE_TOOL,
        payload=proposal.model_dump(mode="json"),
        cwd=root,
        worktree=root,
    )
    question = PersistentQuestion(
        id=operation.id,
        operation=operation,
        fingerprint="",
        preconditions=preconditions,
        file_reviews=[
            CapturedFileReview.model_validate(row)
            for verdict in verdicts
            for row in verdict.file_reviews
        ],
        resumption="native_retry",
        reason=reason,
        rule=asking[0][1].rule if asking else "review:propose",
        purpose=asking[0][1].purpose if asking else None,
        chain_resolved=False,
        resolved={path: path.resolve() for path in preconditions},
        member=session_member_id(),
        account=[Account(source="proposal", text=proposal.why)],
    )
    return relay.record(
        question.model_copy(update={"fingerprint": question.native_fingerprint()})
    )
