"""Whether this project's carriers still stand at one upstream commit.

A project built on a scaffold holds three things that came from upstream —
the library, the generated trees, and the copied half — and the moment nobody
thinks to ask whether they agree is the moment a session starts work on them.
This is the fold that answers at that moment, run by a hook the runtime fires
when a prompt is submitted, and it says nothing at all while they agree.

Shipped verbatim into each plugin's ``hooks/runtime/``, so everything here
resolves on a bare interpreter: standard library only, no ``lup`` import. That
is what lets it reach a person's own session, which has the plugin and no
process of lup's to close over. :mod:`lup.devtools.dev.scaffold` owns what the
two readings mean and writes the trailer this reads; the spellings the halves
share are pinned by a test rather than shared by an import.

**It fails open.** Every failure is silence: a project mid-merge, a lock being
rewritten, a git that answers nothing costs the session one look and never the
prompt it was attached to.

``TypedDict`` for both documents it reads, partial throughout: the lock is
uv's file and the envelope is the runtime's, so each is modelled as what this
reads out of it rather than as whatever either program happens to write.

The output is the shape both runtimes document for the event that fires on a
submitted prompt — ``hookSpecificOutput`` carrying ``hookEventName`` and
``additionalContext``, read from stdout on exit 0. The event's name is the one
word here a runtime owns, so it arrives as an argument from the adapter that
spells it.
"""

import json
import sys
import tomllib

# lup: ignore[subprocess] — `sh` is third-party and this half is shipped into a
# plugin that has no virtual environment to resolve it from
import subprocess
from typing import TypedDict
from urllib.parse import urlsplit

# lup: ignore[constant-declaration] — an identity this repository defines: the
# word it writes into its own commits and reads back out of them
SCAFFOLD_TRAILER = "Lup-Scaffold-Commit"
"""The trailer a scaffold commit carries, restated because a copy cannot import it.

Pinned against the writer by a test, which is what stands in for the import: a
rename there fails the suite here rather than quietly folding a trailer nobody
writes.
"""


class LockedSource(TypedDict, total=False):
    """Where uv resolved one package from, in uv's own spelling."""

    git: str


class LockedPackage(TypedDict, total=False):
    """One ``[[package]]`` table of ``uv.lock``, as much of it as this reads."""

    name: str
    source: LockedSource


class Lock(TypedDict, total=False):
    """``uv.lock`` itself, as much of it as this reads."""

    package: list[LockedPackage]


class Pushed(TypedDict):
    """The runtime's own envelope fields, in the runtime's own spelling."""

    hookEventName: str
    additionalContext: str


class HookOutput(TypedDict):
    """What this hook prints. ``additionalContext`` nested here is what is read."""

    hookSpecificOutput: Pushed


def text(value: str | None) -> str:
    """A field as a string, blank where the document carries something else.

    The lock's declared shape says string; the file another program wrote says
    whatever it says, so the value is checked rather than trusted.
    """
    return value if isinstance(value, str) else ""


def asked(*args: str) -> str:
    """One git answer, or nothing at all where git declines to give one."""
    finished = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    return finished.stdout.strip() if finished.returncode == 0 else ""


def pinned(lock: str, distribution: str) -> str:
    """The commit ``uv.lock`` resolved the library at, or nothing.

    Nothing covers three different projects and wants no distinction here: one
    resolving a release, one whose lock has not been written, and one whose
    lock names a source this cannot read. None of them holds a commit that
    could be out of step with anything.
    """
    try:
        document = Lock(**tomllib.loads(lock))
    except (tomllib.TOMLDecodeError, TypeError, ValueError):
        return ""
    for package in document.get("package", []):
        if text(package.get("name")) != distribution:
            continue
        return urlsplit(text(package.get("source", LockedSource()).get("git"))).fragment
    return ""


def merged(branch: str) -> str:
    """The upstream commit the copied half was last merged at, by its trailer."""
    base = asked("merge-base", "HEAD", f"refs/heads/{branch}")
    if not base:
        return ""
    return asked(
        "log", "-1", f"--format=%(trailers:key={SCAFFOLD_TRAILER},valueonly)", base
    )


def drifted(library: str, scaffold: str, distribution: str) -> list[str]:
    """The one line a session needs, or none because the carriers agree.

    Silence in every case but the one a reader can act on. A project with no
    pinned commit, or none merged, is not out of step with anything — it is a
    project this does not apply to, and a line saying so would be a line on
    every prompt forever.

    Both commits in full, because the reader of this line is as likely to be
    resolving them against a log as to be recognizing them, and a prompt has
    no width this has to fit.
    """
    if not library or not scaffold or library == scaffold:
        return []
    return [
        f"This project holds two commits of {distribution}: the library at "
        f"{library}, the copied half merged at {scaffold}. Until `dev update` "
        f"runs, library code and the code calling it come from different "
        f"commits."
    ]


def envelope(event: str, lines: list[str]) -> HookOutput:
    """The hook output carrying these lines, in the shape both runtimes read."""
    return HookOutput(
        hookSpecificOutput=Pushed(
            hookEventName=event, additionalContext="\n".join(lines)
        )
    )


def main() -> None:
    """Say which two commits this project is holding, or say nothing.

    The scaffold branch, the distribution the pin resolves, and the event's
    name arrive as arguments — each from the declaration the plugin was
    generated out of, because a copy shipped into a plugin can import none of
    them. The lock is read from the checkout the prompt was submitted in.
    Every failure is silence: a prompt is not something an unreadable lock may
    stop.
    """
    try:
        branch, distribution, event = sys.argv[1], sys.argv[2], sys.argv[3]
        root = asked("rev-parse", "--show-toplevel")
        with open(f"{root}/uv.lock", encoding="utf-8") as handle:
            lines = drifted(
                pinned(handle.read(), distribution), merged(branch), distribution
            )
    except Exception:
        return
    if lines:
        print(json.dumps(envelope(event, lines)))


if __name__ == "__main__":
    main()
