"""A Claude session binds the wake socket lup places, and takes lup's frame from it.

The wake rests on four behaviours of the runtime's messaging socket, none of
them documented: a session binds the
path ``--messaging-socket-path`` names rather than the directory its peers
scan; a frame needs no authentication frame before it; a frame naming the
session is delivered as a user message mid-turn; and a frame naming any
other session is dropped. A release that changed any of them would leave
every wake reporting success while nobody looked, so they are asked of the
installed binary here, through the library's own placement and writer.

It costs one short model turn.
"""

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from lup.coordination.identity import mint_member_id
from lup.providers.claude.wake import injected
from lup.harness.wake_sockets import WakeSockets
from lup.types import JsonObject, JsonValue

pytestmark = pytest.mark.integration

# lup: defer: the suite's conftest takes the runtime's own variables away,
# CLAUDE_CONFIG_DIR among them, so where the account lives there — inside a
# lup session's container — the `claude` these start meets no login and exits
# at once; they pass with the account in reach (`pytest --noconftest -m
# integration` here), and want a way to be handed it under the suite

COUNTING = (
    "Count from one to two hundred, one number per line. If a new message "
    "arrives while you count, stop and do what it says."
)
"""A turn long enough to still be running when the frames arrive."""

LONG_COUNT = (
    "Write the numbers from one to three hundred as English words, one per "
    "line, with nothing else."
)
"""A turn that generates for well over ten seconds and calls no tool."""


class StreamDelta(BaseModel, extra="ignore"):
    """What an ``--include-partial-messages`` record carries, as far as this reads it."""

    type: str
    event: JsonObject = Field(default_factory=dict[str, JsonValue])
    result: str = ""


class StreamRecord(BaseModel, extra="ignore"):
    """One line of ``--output-format stream-json``, as far as this reads it."""

    type: str
    message: JsonObject = Field(default_factory=dict[str, JsonValue])

    def said(self) -> str:
        """The text of a user message, whether sent as a string or as parts."""
        content = self.message["content"] if "content" in self.message else ""
        match content:
            case str():
                return content
            case list():
                return "\n".join(str(part) for part in content)
            case _:
                return ""


@pytest.fixture
def placed() -> Iterator[Path]:
    """A short, private wake socket directory, as the runtime insists on one."""
    directory = Path(tempfile.mkdtemp(prefix="lupi", dir="/tmp"))
    directory.chmod(0o700)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def test_a_placed_wake_socket_takes_its_own_session_s_frame_and_drops_another_s(
    placed: Path, tmp_path: Path
) -> None:
    binary = shutil.which("claude")
    if binary is None:
        pytest.skip("no claude CLI on PATH")
    address = Path(
        WakeSockets(directory=str(placed)).socket(
            Path("/probe/repo.git"), mint_member_id()
        )
    )
    session = str(uuid.uuid4())
    running = subprocess.Popen(
        [
            binary,
            "-p",
            COUNTING,
            "--session-id",
            session,
            "--messaging-socket-path",
            str(address),
            "--output-format",
            "stream-json",
            "--verbose",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
    )
    try:
        bound = any(address.is_socket() or time.sleep(0.5) for _ in range(60))
        scanned = [
            directory / f"{running.pid}.sock"
            for directory in (
                Path("/tmp/cc-socks"),
                Path(f"/run/user/{os.getuid()}/cc-socks"),
            )
        ]
        published = [path for path in scanned if path.exists()]
        stray = injected(
            address,
            "Stop counting and reply with exactly WRONG-SESSION-41",
            str(uuid.uuid4()),
        )
        time.sleep(1.0)
        meant = injected(
            address, "Stop counting and reply with exactly RIGHT-SESSION-73", session
        )
        out, err = running.communicate(timeout=300)
    finally:
        if running.poll() is None:
            running.kill()

    assert bound, f"no socket at the placed path {address}: {err[-400:]}"
    assert published == [], "the session also bound where its peers scan"
    assert stray.reached and meant.reached, (stray.reason, meant.reason)
    records = [
        StreamRecord.model_validate_json(line) for line in out.splitlines() if line
    ]
    heard = [record.said() for record in records if record.type == "user"]
    assert any("RIGHT-SESSION-73" in text for text in heard), heard
    assert not any("WRONG-SESSION-41" in text for text in heard), heard


def test_a_now_frame_ends_a_turn_that_is_generating(
    placed: Path, tmp_path: Path
) -> None:
    """What the dashboard's interrupt rests on: a `now` frame stops a generating turn.

    Measured by hand first in an interactive session, with the runtime's debug
    log as witness and a `next` frame as the control, which lets the turn
    finish. Here the turn is a long piece of writing with no tool call, and
    the frame goes in once its first words stream: the turn has to end within
    seconds, short of where it was going.
    """
    binary = shutil.which("claude")
    if binary is None:
        pytest.skip("no claude CLI on PATH")
    address = Path(
        WakeSockets(directory=str(placed)).socket(
            Path("/probe/repo.git"), mint_member_id()
        )
    )
    session = str(uuid.uuid4())
    running = subprocess.Popen(
        [
            binary,
            "-p",
            LONG_COUNT,
            "--session-id",
            session,
            "--messaging-socket-path",
            str(address),
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=tmp_path,
    )
    sent_at: float | None = None
    ended_at: float | None = None
    first = ""
    try:
        assert running.stdout is not None
        for line in running.stdout:
            record = StreamDelta.model_validate_json(line)
            match record.type:
                case "stream_event" if sent_at is None and "delta" in record.event:
                    interrupt = injected(
                        address, "Stop and reply with OK.", session, priority="now"
                    )
                    assert interrupt.reached, interrupt.reason
                    sent_at = time.monotonic()
                case "result" if ended_at is None:
                    ended_at = time.monotonic()
                    first = record.result
                    break
                case _:
                    continue
    finally:
        running.kill()
        running.wait(timeout=30)

    assert sent_at is not None and ended_at is not None
    assert ended_at - sent_at < 5, ended_at - sent_at
    assert "three hundred" not in first, first[-200:]
