"""Keep a session's record: its transcript, where runs are kept, and a ledger entry.

``record=Recording(...)`` starts the run's journal before the session opens
and closes it after, mirrors the runtime's own transcript into it where
``transcript`` asks, roots it under ``root``, names the kind of session in
``mode``, and records the session opening and closing in the project's
ledger where ``ledger`` is a recorder — the same record whichever way the
session was opened.

    uv run -m examples.launch_recording
"""

from pathlib import Path

from lup import Claude, Recording
from lup.coordination.refs import ActorRef
from lup.observability.sessions import Output, Session, session_recorder


def main() -> None:
    ledger = session_recorder(
        Path.cwd(), ActorRef(kind="harness", id="claude"), [Session, Output]
    )
    agent = Claude(
        record=Recording(
            transcript=True, root=Path("notes/runs"), mode="review", ledger=ledger
        )
    )
    print(agent.command())


if __name__ == "__main__":
    main()
