"""A session's status line: which session, what waits on the operator, what other agents need, the dashboard.

Rendered from the dashboard's pulse and what the runtime hands the command
on stdin, which names the session by its conversation, beginning with the
repository's name. What waits and what other agents need show only while
something does; the dashboard is one glyph and the whole address the
operator opens it at, always, as a link whose text is that address and
never its capability. A narrow terminal drops whole pieces in their order,
the address never, and a pulse from before the dashboard listed sessions
shows what it can. The route answers on the CLI's fast path, loading nothing
of the dashboard beyond the pulse.
"""

import io
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath

import pytest
import sh

from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.pulse import (
    DashboardPulse,
    LineFacts,
    PulseFile,
    PulseSession,
    RunningCode,
    StatusInput,
    StatusWorkspace,
    repository_name,
    status_line,
)
from lup.launch.companions import lent_directory

URL = "http://127.0.0.1:8767"
REPOSITORY = "/work/lup.git"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
CONVERSATION = "7adb8bf2-7de0-4265-9f3c-f27bc3c42124"
REVIEW = "41cb73e1a2b3c4d5e6f708192a3b4c5d"
ASKING = StatusInput(session_id=CONVERSATION)


def session(worktree: str = "dev", reviews: list[str] | None = None) -> PulseSession:
    return PulseSession(
        repository=REPOSITORY,
        project="lup",
        id="983914fca179",
        name="dev",
        worktree=f"{REPOSITORY}/tree/{worktree}",
        runtime=[CONVERSATION],
        reviews=reviews or [],
    )


def pulse(**fields: object) -> DashboardPulse:
    return DashboardPulse.model_validate(
        {
            "url": URL,
            "pid": 1,
            "beat": NOW,
            "repositories": [REPOSITORY],
            "members": [session()],
            **fields,
        }
    )


def shown(
    each: DashboardPulse | None, columns: int = 0, asking: StatusInput = ASKING
) -> str:
    return LineFacts.of(each, asking, NOW).fitted(columns).plain()


@pytest.mark.parametrize(
    ("each", "line"),
    [
        (pulse(), f"lup · dev · tree/dev │ ● {URL}"),
        (
            pulse(pending=2, unread=1, members=[session("fix-x", [REVIEW])]),
            f"lup · dev · tree/fix-x │ ?2 reviews (1 here: 41cb73e1) · ✉1 │ ● {URL}",
        ),
        (
            pulse(pending=3, members=[session("fix-x", [REVIEW, "q-2"])]),
            f"lup · dev · tree/fix-x │ ?3 reviews (2 here) │ ● {URL}",
        ),
        (pulse(unread=1), f"lup · dev · tree/dev │ ✉1 │ ● {URL}"),
        (
            pulse(quiet=1, code=RunningCode(older=True)),
            f"lup · dev · tree/dev │ ⚠ 1 quiet │ ◐ {URL} restarting",
        ),
        (
            pulse(code=RunningCode(older=True, failing="SyntaxError")),
            f"lup · dev · tree/dev │ ◐ {URL} runs older code; "
            "its newer code does not start",
        ),
        (pulse(contested=1), f"lup · dev · tree/dev │ ⚠ held twice │ ● {URL}"),
        (
            pulse(contested=2),
            f"lup · dev · tree/dev │ ⚠ 2 paths held twice │ ● {URL}",
        ),
        (
            pulse(code=RunningCode(restarted="it was ended by SIGKILL")),
            f"lup · dev · tree/dev │ ● {URL} · "
            "restarted after it stopped: it was ended by SIGKILL",
        ),
        (
            pulse(address="https://their.proxy.name"),
            "lup · dev · tree/dev │ ● https://their.proxy.name",
        ),
        (
            pulse(beat=NOW - timedelta(minutes=5), pending=4),
            f"lup · dev · tree/dev │ ○ {URL} down · dashboard restart",
        ),
        (
            pulse(
                beat=NOW - timedelta(hours=1),
                halted="dashboard stopped by the operator; `dashboard restart` starts it",
            ),
            f"lup · dev · tree/dev │ ○ {URL} · dashboard stopped by the operator; "
            "`dashboard restart` starts it",
        ),
    ],
    ids=[
        "calm",
        "one-here",
        "several-here",
        "messages",
        "quiet-restarting",
        "newer-code-fails",
        "held-twice",
        "held-twice-over",
        "restarted",
        "declared-origin",
        "down",
        "halted",
    ],
)
def test_each_state_reads_as_the_operator_decided(
    each: DashboardPulse, line: str
) -> None:
    assert shown(each) == line


def test_what_waits_is_painted_in_the_warning_colour_and_the_address_is_a_link() -> (
    None
):
    each = pulse(pending=2, unread=1, members=[session("fix-x", [REVIEW])])

    painted = LineFacts.of(each, ASKING, NOW).fitted(0).painted()

    assert painted == (
        "\x1b[2mlup · dev · tree/fix-x\x1b[0m"
        "\x1b[2m │ \x1b[0m"
        "\x1b[33m?2 reviews (1 here: 41cb73e1)\x1b[0m"
        "\x1b[2m · \x1b[0m"
        "\x1b[33m✉1\x1b[0m"
        "\x1b[2m │ \x1b[0m"
        "\x1b[32m● \x1b[0m"
        f"\x1b]8;;{URL}\x07{URL}\x1b]8;;\x07"
    )


def test_nothing_serving_is_down_and_says_what_starts_it() -> None:
    asking = StatusInput(
        workspace=StatusWorkspace(project_dir=f"{REPOSITORY}/tree/dev")
    )

    assert shown(None, asking=asking) == "dev │ ○ dashboard down · dashboard restart"


@pytest.mark.parametrize(
    ("columns", "line"),
    [
        (
            0,
            f"lup · dev · tree/fix-x │ ?3 reviews (1 here: 41cb73e1) · ✉2 │ "
            f"⚠ 1 quiet · ⚠ held twice │ ● {URL}",
        ),
        (
            100,
            f"lup · dev · tree/fix-x │ ?3 reviews (1 here: 41cb73e1) · ✉2 │ ● {URL}",
        ),
        (80, f"lup · dev │ ?3 reviews (1 here: 41cb73e1) · ✉2 │ ● {URL}"),
        (70, f"lup · dev │ ?3 reviews (1 here) · ✉2 │ ● {URL}"),
        (60, f"lup │ ?3 reviews (1 here) · ✉2 │ ● {URL}"),
        (52, f"?3 reviews (1 here) · ✉2 │ ● {URL}"),
        (20, f"?3 reviews (1 here) · ✉2 │ ● {URL}"),
    ],
)
def test_a_narrow_terminal_drops_whole_pieces_in_their_order(
    columns: int, line: str
) -> None:
    """Other agents' needs, the worktree, the review's id, the session's name, the repository's; never the address."""
    busy = pulse(
        pending=3,
        unread=2,
        quiet=1,
        contested=1,
        members=[session("fix-x", [REVIEW])],
    )

    assert shown(busy, columns) == line


def test_the_runtime_input_names_the_session_by_its_conversation() -> None:
    """Claude Code's documented statusLine input; a conversation opened since the launch is found by its transcript."""
    handed = {
        "cwd": f"{REPOSITORY}/tree/dev",
        "session_id": "a-conversation-opened-since",
        "session_name": "my-session",
        "transcript_path": f"/home/me/.claude/projects/lup/{CONVERSATION}.jsonl",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "workspace": {
            "current_dir": f"{REPOSITORY}/tree/dev",
            "project_dir": f"{REPOSITORY}/tree/dev",
            "added_dirs": [],
            "git_worktree": "dev",
        },
        "version": "2.1.90",
        "cost": {"total_cost_usd": 0.01, "total_duration_ms": 45000},
        "context_window": {"used_percentage": 8, "current_usage": None},
    }

    asking = StatusInput.read(io.StringIO(json.dumps(handed)))

    assert asking.identities() == ["a-conversation-opened-since", CONVERSATION]
    assert asking.directory() == f"{REPOSITORY}/tree/dev"
    assert shown(pulse(), asking=asking) == f"lup · dev · tree/dev │ ● {URL}"
    assert shown(pulse(), asking=StatusInput()) == f"● {URL}"


def test_the_repository_is_named_as_the_dashboard_tree_names_it() -> None:
    """A row an older dashboard listed without it, and a checkout it lists none for, are named from their repository."""
    older = session().model_copy(update={"project": ""})
    checkout = StatusInput(cwd="/work/project/tree/fix-y")
    plain = pulse(members=[], repositories=["/work/project/.git"])

    assert shown(pulse(members=[older])) == f"lup · dev · tree/dev │ ● {URL}"
    assert shown(plain, asking=checkout) == f"project · tree/fix-y │ ● {URL}"
    assert shown(plain, asking=StatusInput(cwd="/work/project")) == f"project │ ● {URL}"
    assert [
        KnownRepository(repository=Path(each), checkout=Path("/work")).name()
        for each in ("/work/lup.git", "/work/project/.git")
    ] == [
        repository_name(PurePath(each))
        for each in ("/work/lup.git", "/work/project/.git")
    ]


def test_input_that_is_a_terminal_or_not_json_names_no_session() -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert StatusInput.read(Terminal('{"session_id": "typed"}')) == StatusInput()
    assert StatusInput.read(io.StringIO("not json")) == StatusInput()
    assert StatusInput.read(None) == StatusInput()


def test_a_pulse_from_before_sessions_were_listed_shows_what_it_can(
    tmp_path: Path,
) -> None:
    """An older dashboard's pulse lacks sessions and what needs the operator."""
    kept = PulseFile.of(lent_directory(tmp_path))
    kept.path.parent.mkdir(parents=True)
    kept.path.write_text(
        json.dumps(
            {
                "url": URL,
                "pid": 1,
                "pending": 1,
                "sessions": 2,
                "repositories": [REPOSITORY],
                "tabs": 0,
                "beat": NOW.isoformat(),
                "code": {"source": "abc", "older": False},
                "restarts": 0,
                "halted": "",
            }
        )
    )
    asking = StatusInput(session_id=CONVERSATION, cwd=f"{REPOSITORY}/tree/dev")

    assert status_line(kept.path, asking, NOW).plain() == (
        f"lup · tree/dev │ ?1 review │ ● {URL}"
    )


def test_a_render_costs_a_fraction_of_a_millisecond(tmp_path: Path) -> None:
    """Measured in processor time, which a loaded machine does not stretch."""
    kept = PulseFile.of(lent_directory(tmp_path))
    kept.path.parent.mkdir(parents=True)
    kept.path.write_text(
        pulse(
            pending=3,
            unread=2,
            quiet=1,
            contested=1,
            members=[session(str(each), [REVIEW]) for each in range(40)],
        ).model_dump_json()
    )
    handed = io.StringIO(json.dumps({"session_id": CONVERSATION}))
    renders = 200

    began = time.process_time()
    for _ in range(renders):
        handed.seek(0)
        status_line(kept.path, StatusInput.read(handed), NOW, 80).painted()
    took = (time.process_time() - began) / renders

    assert took < 0.005, f"{took * 1000:.2f} ms a render"


def test_the_route_loads_nothing_of_the_dashboard_beyond_its_pulse(
    tmp_path: Path,
) -> None:
    """What a status line costs is the interpreter and `import lup`: nothing it runs pulls in more."""
    kept = PulseFile.of(lent_directory(tmp_path))
    kept.path.parent.mkdir(parents=True)
    kept.path.write_text(pulse(pending=1, beat=datetime.now(UTC)).model_dump_json())
    route = tmp_path / "route.py"
    route.write_text(
        "import sys\n"
        "from lup.devtools.entrypoint import main\n"
        "sys.argv = ['lup-devtools', 'dashboard', 'line', sys.argv[1]]\n"
        "main()\n"
        "print(' '.join(sorted(sys.modules)))\n",
        encoding="utf-8",
    )

    printed = str(
        sh.Command(sys.executable)(
            str(route), str(kept.path), _in=json.dumps({"session_id": CONVERSATION})
        )
    )
    shown_line, loaded = printed.splitlines()

    assert "?1 review" in shown_line and "lup · dev · tree/dev" in shown_line
    assert f"\x1b]8;;{URL}\x07{URL}\x1b]8;;\x07" in shown_line
    assert "token" not in shown_line
    heavy = {
        "fastapi",
        "uvicorn",
        "httpx",
        "lup.devtools.dashboard.companion",
        "lup.devtools.dashboard.live",
        "lup.devtools.dashboard.reviews",
        "lup.devtools.dashboard.service",
        "lup.coordination.repository",
        "lup.policy.relay",
    }
    assert heavy.isdisjoint(loaded.split(" "))
