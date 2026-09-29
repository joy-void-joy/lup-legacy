"""What a parked question actually proposes to do to each file.

A question carries the operation whole and, where a native hook captured them,
the preimage of every file it would touch. What it does not carry is the
*result*: that is implied by the tool's own arguments, and unless something
works it out, a reviewer answering a whole-file write reads a JSON payload
with the new contents inside it and the old contents printed underneath, and
compares them by eye.

So the pair is derived here rather than stored. Deriving it keeps the record
append-only and keeps an approval bound to the operation it was given — the
same arguments produce the same pair, and a record that stored a rendering
could disagree with the arguments beside it.

The arithmetic is per tool and most of it is neutral: a whole-file write
carries its result, and a fragment edit is a splice against the preimage the
hook captured. The one shape this cannot do alone is a patch envelope, whose
format a provider owns — so a caller that has one hands in the reader for it,
and a caller that does not gets the rest.
"""

import difflib
from functools import cache
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from lup.policy.assets.host import sed_output
from lup.policy.kernel.bindings import literal_loop_word
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.review import single_command
from lup.policy.kernel.syntax import word_text
from lup.policy.kernel.words import safe_sed_script, sed_invocation
from lup.policy.relay import PersistentQuestion

type FileOperation = Literal["create", "modify", "overwrite", "delete"]

type PatchReader = Callable[
    [str, Path, dict[Path, str | None], bool], list["ReviewedFile"]
]
"""Decode one provider's patch envelope into the pairs it would produce.

Taken as an argument rather than imported, because the envelope's grammar is
that provider's word and this module is read by both. A caller with no patch
to decode passes nothing and loses only that one shape.
"""


class ReviewedFile(BaseModel, frozen=True):
    """One file's documents on either side of an operation waiting for review.

    ``None`` on either side is absence rather than emptiness, and the two are
    worth keeping apart: a file created where nothing stood and a file
    truncated to nothing are different acts, and a reviewer shown "" for both
    is being asked to approve the wrong one.
    """

    path: Path
    before: str | None = None
    after: str | None = None
    overwrite: bool = False
    """Whether the call replaced the whole document rather than a fragment of it.

    Carried rather than derived, because the documents cannot answer it: a
    fragment edit that happens to rewrite every line and a whole-file write
    produce the same pair, and the difference is in how the call was
    expressed. It is also the difference the gate cares about most, so
    guessing it from the text is exactly where a guess costs something.
    """

    def operation(self) -> FileOperation:
        """Which class of change this is, the reading `PatchedFile` also takes.

        Absence on either side settles the two ends, and the flag settles the
        middle — so nothing here re-reads the documents to decide a verb the
        caller already knew.
        """
        if self.after is None:
            return "delete"
        if self.before is None:
            return "create"
        return "overwrite" if self.overwrite else "modify"

    def unchanged(self) -> bool:
        """Whether this operation would leave the file exactly as it stands."""
        return self.before == self.after

    def lines(self, document: str | None) -> list[str]:
        """One side as diff input, with absence read as no lines at all."""
        return document.splitlines(keepends=True) if document else []

    def unified(self, context: int = 3) -> str:
        """This change as a unified diff, labelled with what it does.

        Trailing newlines are normalized onto every line so a file whose last
        line has none does not end the diff with the marker `difflib` emits
        mid-hunk, which reads as content.
        """
        rendered = difflib.unified_diff(
            self.lines(self.before),
            self.lines(self.after),
            fromfile=f"{self.path} (before)"
            if self.before is not None
            else "/dev/null",
            tofile=f"{self.path} (after)" if self.after is not None else "/dev/null",
            n=context,
        )
        return "".join(
            line if line.endswith("\n") else line + "\n" for line in rendered
        )


def spliced(before: str, old: str, new: str, every: bool) -> str | None:
    """The document one fragment edit produces, or None where it cannot apply.

    ``None`` rather than a raise, because this runs over a record: a question
    parked against a preimage the edit no longer fits is a question to report
    as unapplyable, not an error to take the whole listing down with.
    """
    occurrences = before.count(old)
    if occurrences == 0 or (occurrences != 1 and not every):
        return None
    if every:
        # The Edit tool's own splice, over a literal the caller already chose.
        return before.replace(old, new)  # lup: ignore[string-replace]
    position = before.find(old)
    return before[:position] + new + before[position + len(old) :]


def written_file(path: Path, content: str, before: str | None) -> list[ReviewedFile]:
    """The pair a whole-file write produces: its argument is the result.

    An overwrite wherever something stood, which is the classification the
    edit gate turns on and the reason this surface was worth building.
    """
    return [
        ReviewedFile(
            path=path, before=before, after=content, overwrite=before is not None
        )
    ]


def edited_file(
    path: Path,
    old: str,
    new: str,
    every: bool,
    before: str | None,
) -> list[ReviewedFile]:
    """The pair a fragment edit produces, against the preimage that was captured.

    Against the captured preimage and never the file as it stands now. What a
    reviewer is answering is the operation as it was submitted, and a splice
    recomputed from a file that has since moved would show them a change
    nobody proposed.
    """
    if before is None:
        return []
    after = spliced(before, old, new, every)
    if after is None:
        return []
    return [ReviewedFile(path=path, before=before, after=after)]


class FilePreview(BaseModel, frozen=True):
    """Captured file results, or the reason an exact preview is unavailable."""

    files: list[ReviewedFile] = []
    unavailable: str = ""
    notice: str = ""


@cache
def captured_sed_output(
    scripts: tuple[str, ...], options: tuple[str, ...], before: str
) -> dict[Literal["text", "cause"], str | None]:
    """Reuse the same captured-input simulation without caching live target checks."""
    return sed_output(list(scripts), list(options), before=before)


def shell_preview(question: PersistentQuestion) -> FilePreview:
    """Preview a literal sed rewrite over captured input, without running a shell."""
    payload = question.operation.payload
    command = payload["command"] if "command" in payload else None
    if not isinstance(command, str):
        return FilePreview(unavailable="No shell command was captured.")
    if not question.operation.cwd.is_absolute():
        return FilePreview(unavailable="No absolute command directory was captured.")
    if question.execution_payload is not None and question.execution_payload != payload:
        return FilePreview(
            unavailable="The native execution rewrites this command; no exact preview is available."
        )
    for field in ("workdir", "cwd"):
        if field in payload and payload[field] != str(question.operation.cwd):
            return FilePreview(
                unavailable="The command directory differs from the directory bound to this review."
            )
    parsed = single_command(command)
    if parsed is None or parsed["redirects"]:
        return FilePreview(
            unavailable="File previews require one literal command without pipelines, redirections, or adjacent shell effects."
        )
    if not all(literal_loop_word(word) for word in parsed["words"]):
        return FilePreview(
            unavailable="Shell expansion prevents an exact captured-file preview."
        )
    words = [word_text(word) for word in parsed["words"]]
    if not words or words[0] != "sed":
        return FilePreview(
            unavailable="No captured before-and-after preview is available for this command."
        )
    invocation = sed_invocation(words)
    if isinstance(invocation, KernelDecision):
        return FilePreview(unavailable=invocation.reason)
    if not invocation["in_place"] or not invocation["targets"]:
        return FilePreview(
            unavailable="This sed command does not name files to rewrite in place."
        )
    if invocation["backup"]:
        return FilePreview(
            unavailable="A sed backup suffix adds file effects that this preview cannot capture."
        )
    if not all(
        safe_sed_script(script, captured=True) for script in invocation["scripts"]
    ):
        return FilePreview(
            unavailable="This sed script has external effects, depends on filename or process-exit behavior, or uses locale-sensitive constructs whose execution environment was not captured."
        )
    try:
        paths = [
            (question.operation.cwd / target).resolve()
            for target in invocation["targets"]
        ]
    except (OSError, RuntimeError) as error:
        return FilePreview(
            unavailable=f"A captured sed target cannot be resolved: {error}"
        )
    if len(dict.fromkeys(paths)) != len(paths):
        return FilePreview(
            unavailable="Repeated sed targets need sequential file effects; no exact preview is available."
        )
    if any(
        path not in question.preconditions or question.preconditions[path] is None
        for path in paths
    ):
        return FilePreview(
            unavailable="One or more sed targets have no captured text preimage; no live file is substituted."
        )

    def transformed(path: Path) -> FilePreview:
        before = question.preconditions[path]
        if before is None:
            return FilePreview(
                unavailable=f"No captured text preimage exists for {path}."
            )
        if not before.isascii():
            return FilePreview(
                unavailable=f"The captured input for {path} is not ASCII and the execution locale was not captured; no output is inferred."
            )
        attempt = captured_sed_output(
            tuple(invocation["scripts"]), tuple(invocation["options"]), before
        )
        after = attempt["text"]
        if after is None:
            return FilePreview(
                unavailable=f"The sandboxed sed preview could not produce {path}: {attempt['cause']}."
            )
        return FilePreview(files=[ReviewedFile(path=path, before=before, after=after)])

    results = [transformed(path) for path in paths]
    failures = [result.unavailable for result in results if result.unavailable]
    return (
        FilePreview(unavailable="\n".join(failures))
        if failures
        else FilePreview(
            files=[file for result in results for file in result.files],
            notice=(
                "Preview computed in inbox environment using sandboxed GNU sed and the captured input. "
                "The request did not capture its execution environment; compare this simulation with the exact command."
            ),
        )
    )


def reviewed_preview(
    question: PersistentQuestion, patches: PatchReader | None = None
) -> FilePreview:
    """Every file change one parked question proposes, as before/after pairs.

    Captured tool arguments and preimages are the only source of file results.
    An unsupported shell command carries an explanation alongside its complete
    input, so a missing preview cannot be mistaken for a command with no effects.
    """
    payload = question.operation.payload
    cwd = question.operation.cwd

    def resolved(path: str) -> Path:
        """One payload path against the directory the operation ran in.

        Resolved rather than passed through, because a reviewer answering from
        another terminal has no reason to share the agent's working directory
        — and `docs/plan.md` names a different file in each of the worktrees
        this repository keeps open at once.
        """
        named = Path(path)
        return named if named.is_absolute() else cwd / named

    def captured(path: str) -> str | None:
        """The preimage the hook recorded for one path, keyed as it recorded it."""
        return question.preconditions.get(resolved(path))

    def text(key: str) -> str:
        """One payload field as text, with a missing or non-string field empty."""
        value = payload.get(key)
        return value if isinstance(value, str) else ""

    def flag(key: str) -> bool:
        """One payload field as a flag, with anything but a true absent.

        `True` rather than truthiness: a payload is whatever the runtime put
        on the wire, and reading `"false"` or `1` as "yes, every occurrence"
        would widen an edit nobody asked to widen.
        """
        return payload.get(key) is True

    match question.operation.tool:
        case "Write" if text("file_path"):
            named = text("file_path")
            return FilePreview(
                files=written_file(resolved(named), text("content"), captured(named))
            )
        case "Edit" if text("file_path"):
            named = text("file_path")
            return FilePreview(
                files=edited_file(
                    resolved(named),
                    text("old_string"),
                    text("new_string"),
                    flag("replace_all"),
                    captured(named),
                )
            )
        case "apply_patch" | "Bash" if patches is not None and text("command"):
            files = patches(
                text("command"),
                cwd,
                question.preconditions,
                question.operation.tool == "Bash",
            )
            if files:
                return FilePreview(files=files)
    return (
        shell_preview(question) if question.operation.tool == "Bash" else FilePreview()
    )


def reviewed_files(
    question: PersistentQuestion, patches: PatchReader | None = None
) -> list[ReviewedFile]:
    """The exact file results exposed to terminal and browser reviewers."""
    return reviewed_preview(question, patches).files
