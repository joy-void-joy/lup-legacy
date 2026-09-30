"""Claude Code's half of session naming, run as a bare script.

Shipped verbatim into the plugin's ``hooks/runtime/`` beside the coordination
package it imports, and registered under ``UserPromptSubmit``. It holds only
what Claude Code spells for itself: the title the payload reports, the CLI a
name is asked through, and the answer that sets the title. When to ask, what a
name may be and how one is settled on the roster are
:mod:`coordination.naming`'s.

Measured rather than only read from https://code.claude.com/docs/en/hooks,
which documents ``sessionTitle`` for this event:

- ``hookSpecificOutput.sessionTitle`` replaces the title a launch set with
  ``--name``, and a title set with ``/rename`` too (2.1.280) — so an answer
  repeated at every prompt would undo a person's rename, which is why the
  roster's name is carried once per change. The title it sets is the one a
  resume of that conversation reports (2.1.285).
- Only the hook's own answer sets the title: an async hook's answer carries
  ``additionalContext`` and ``systemMessage`` and nothing else. So the ask
  runs in a process of its own and the prompt goes on at once; the name it
  settles on the roster is this hook's answer at the next prompt.
- The payload carries ``session_title``, the title the chrome shows as the
  prompt arrives, including a ``/rename`` made since the last one — a
  ``/rename`` fires no prompt hook of its own — though the docs list the
  field for ``SessionStart`` alone. A conversation reopened with
  ``--resume`` or ``--continue`` and no ``--name`` reports the title it last
  had, exactly as it was set — ``Renamed By Hand`` stayed ``Renamed By
  Hand`` — at ``SessionStart`` and at its prompts; one never named reports
  no ``session_title`` at all (2.1.285, in print mode).
- ``claude --safe-mode -p`` answers with plugins, hooks and MCP servers off,
  so the ask cannot re-enter this hook, and with ``--output-format json`` and
  ``--json-schema`` the name arrives as ``structured_output``. On the
  strongest tier's ``opus`` at ``low`` effort it took 3.5 to 3.7 seconds
  (2.1.285).

A title the payload reports that this hook did not put there — a resumed
conversation's, a ``/rename`` — is taken up by the roster and left in the
chrome as it was set. Every failure is silence: the prompt goes on, and the
session keeps its name.
"""

import json
import sys
from pathlib import Path
from typing import TypedDict

# The hook is launched as a bare script, promised no cwd, PYTHONPATH, or
# interpreter environment, and this file sits in the `runtime/` directory
# that holds the coordination package. Naming it as a search path is what
# lets the imports below resolve.
sys.path.insert(0, str(Path(__file__).parent))
from coordination.naming import (
    Arrival,
    Naming,
    answer_schema,
    compiled_for,
    concluded,
    detached,
    named,
    prompted,
    ran,
    request_for,
    said,
    session_of,
    settled,
)


class Payload(Arrival, total=False):
    """What ``UserPromptSubmit`` hands the hook, with the one field only Claude Code sends."""

    session_title: str


class Titled(TypedDict):
    hookEventName: str
    sessionTitle: str


class Answered(TypedDict):
    """The event's answer: the title, and nothing that could refuse the prompt."""

    hookSpecificOutput: Titled


def shown(payload: Payload) -> str:
    """The title the chrome shows as the prompt arrives, blank where it reports none."""
    match payload:
        case {"session_title": str(title)}:
            return title
        case _:
            return ""


def asked(prompt: str, naming: Naming) -> str:
    """The name Claude gives the work *prompt* describes, blank for none.

    The prompt travels on stdin rather than as an argument, which a long
    pasted one would overrun, and whole: a prompt too long for the naming
    model is an ask that fails, not one to cut. The compiled arguments name
    the model, its effort, and no tool at all.
    """
    printed = ran(
        [
            "claude",
            "--safe-mode",
            "-p",
            "--no-session-persistence",
            *naming["arguments"],
            "--output-format",
            "json",
            "--json-schema",
            answer_schema(),
            "--system-prompt",
            naming["instruction"],
        ],
        request_for(prompt),
        naming["deadline_seconds"],
    )
    try:
        result = json.loads(printed or "")
    except ValueError:
        return ""
    match result:
        case {"is_error": False, "structured_output": answer}:
            return named(answer, naming["longest"])
        case _:
            return ""


def named_in_background(root: Path, member_id: str, prompt: str) -> None:
    """Ask for a name and settle the roster on it; the next prompt hands it on.

    Nothing reaches the chrome from here — only the hook's own answer can —
    so the roster's new name is what the hook finds waiting at the next
    prompt, unless somebody set a title there first, which then wins.
    """
    naming = compiled_for(Path(__file__))
    if naming is None:
        return
    wanted = asked(prompt, naming) if prompt.strip() else ""
    if wanted:
        settled(root, member_id, wanted)
    concluded(root, member_id)


def titled(root: Path, member_id: str, payload: Payload, naming: Naming) -> str:
    """The title this prompt sets, blank where it sets none, having started any ask."""
    step = prompted(root, member_id, payload, naming, shown(payload))
    match step["move"]:
        case "push":
            return step["value"]
        case "ask":
            detached(Path(__file__), ["ask", str(root), member_id], said(payload))
            return ""
        case _:
            return ""


def main() -> None:
    """Answer the prompt, or run the ask it started; print nothing but a title.

    As the hook, the store root, the launcher-proven member id (blank where
    nothing launched this session) and the event's name arrive as arguments,
    and the compiled declaration is read under this file's name. As the ask,
    the store root and the member id do, and the prompt on stdin.
    """
    try:
        match sys.argv[1:]:
            case ["ask", root, member_id]:
                named_in_background(Path(root), member_id, sys.stdin.read())
                return
            case [root, member_id, event]:
                payload: Payload = json.load(sys.stdin)
                naming = compiled_for(Path(__file__))
                title = (
                    titled(
                        Path(root), member_id or session_of(payload), payload, naming
                    )
                    if naming is not None
                    else ""
                )
            case _:
                return
    except Exception:
        return
    if title:
        print(
            json.dumps(
                Answered(
                    hookSpecificOutput=Titled(hookEventName=event, sessionTitle=title)
                )
            )
        )


if __name__ == "__main__":
    main()
