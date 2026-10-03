"""What a contained session can and cannot reach through the clipboard bridge.

The shim is exercised as a program rather than imported, because a program is
what the container runs: the image bakes this text at `/usr/local/bin/xclip`
and whatever asks for a clipboard finds it by that name. A test that called
the protocol directly would prove the protocol and leave the four spellings
of "read the clipboard" unproven, which is the half that decides whether a
paste works.
"""

import base64
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

from lup.devtools.clipboard import ClipboardImage
from lup.harness.clipboard import SHIM_ASSET, ClipboardBridge, shim_program

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A stand-in for the operator's clipboard, so no test touches the real one."""
    board = {"text": "on the clipboard"}

    def image(media_types: tuple[str, ...]) -> ClipboardImage | None:
        if "image/png" not in media_types:
            return None
        return ClipboardImage(media_type="image/png", data=PNG)

    monkeypatch.setattr("lup.harness.clipboard.clipboard_text", lambda: board["text"])
    monkeypatch.setattr("lup.harness.clipboard.clipboard_image", image)
    monkeypatch.setattr(
        "lup.harness.clipboard.copy_to_clipboard",
        lambda text: board.__setitem__("text", text) or True,
    )
    return board


def request(endpoint: Path, payload: dict[str, str]) -> dict[str, str]:
    """One question over the socket, the way the shim asks it."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.connect(str(endpoint))
        channel.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        received = b""
        while not received.endswith(b"\n"):
            block = channel.recv(65536)
            if not block:
                break
            received += block
    return json.loads(received.decode("utf-8"))


def serving(bridge: ClipboardBridge) -> Path:
    """A live broker, with its socket.

    Skipped rather than failed where no unix socket can be bound: a sandbox
    that refuses `bind` is the same environment this bridge correctly declines
    to open in, and a gate running inside one would otherwise report the
    machine's own restriction as this code's defect.
    """
    directory = bridge.serve()
    if directory is None:
        pytest.skip("no unix socket can be bound here, so no broker can listen")
    return directory / bridge.channel


def test_text_crosses_both_ways(held: dict[str, str]) -> None:
    endpoint = serving(ClipboardBridge())
    assert request(endpoint, {"op": "text"})["text"] == "on the clipboard"
    assert request(endpoint, {"op": "set", "text": "written back"})["ok"]
    assert held["text"] == "written back"


def test_an_image_crosses_whole(held: dict[str, str]) -> None:
    """The bytes arrive identical, which is the paste this exists for."""
    endpoint = serving(ClipboardBridge())
    assert "image/png" in request(endpoint, {"op": "types"})["types"]
    reply = request(endpoint, {"op": "typed", "media_type": "image/png"})
    assert base64.b64decode(reply["data"]) == PNG


def test_a_type_the_bridge_does_not_carry_is_refused(held: dict[str, str]) -> None:
    endpoint = serving(ClipboardBridge(media_types=["image/png"]))
    reply = request(endpoint, {"op": "typed", "media_type": "text/x-secret"})
    assert not reply["ok"]
    assert "not carried" in reply["error"]
    assert reply["code"] == "unsupported_type"


def test_an_oversized_clipboard_is_refused_rather_than_cut(
    held: dict[str, str],
) -> None:
    """A truncated image is a file that opens to nothing and blames the paste."""
    endpoint = serving(ClipboardBridge(limit=16))
    reply = request(endpoint, {"op": "typed", "media_type": "image/png"})
    assert not reply["ok"]
    assert "over this bridge's 16" in reply["error"]


def test_a_malformed_request_does_not_take_the_broker_down(
    held: dict[str, str],
) -> None:
    endpoint = serving(ClipboardBridge())
    assert not request(endpoint, {"op": "nonsense"})["ok"]
    assert request(endpoint, {"op": "text"})["text"] == "on the clipboard"


def test_two_sessions_get_two_brokers_that_cannot_see_each_other(
    held: dict[str, str],
) -> None:
    """Concurrent launches are ordinary, so the socket is the launch's own."""
    first = serving(ClipboardBridge())
    second = serving(ClipboardBridge())
    assert first != second
    assert request(first, {"op": "set", "text": "from the first"})["ok"]
    assert request(second, {"op": "text"})["text"] == "from the first"


def test_questions_asked_at_once_are_all_answered(held: dict[str, str]) -> None:
    """Threaded, so a paste does not wait on whatever a copy is talking to."""
    endpoint = serving(ClipboardBridge())
    answers: list[str] = []
    lock = threading.Lock()

    def ask() -> None:
        reply = request(endpoint, {"op": "text"})
        with lock:
            answers.append(reply["text"])

    workers = [threading.Thread(target=ask) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert answers == ["on the clipboard"] * 8


def shim(tmp_path: Path, name: str) -> Path:
    """The baked program, installed under one of the names it answers to."""
    program = tmp_path / name
    program.write_text(shim_program(), encoding="utf-8")
    return program


def test_the_shim_reads_text_the_way_xclip_is_asked_to(
    held: dict[str, str], tmp_path: Path
) -> None:
    import sh

    endpoint = serving(ClipboardBridge())
    printed = sh.Command(sys.executable)(
        shim(tmp_path, "xclip"),
        "-selection",
        "clipboard",
        "-o",
        _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
    )
    assert str(printed) == "on the clipboard"


def test_the_shim_hands_back_an_image_unaltered(
    held: dict[str, str], tmp_path: Path
) -> None:
    """`xclip -o -t image/png` inside the container is what an image paste runs."""
    import io

    import sh

    endpoint = serving(ClipboardBridge())
    buffer = io.BytesIO()
    sh.Command(sys.executable)(
        shim(tmp_path, "xclip"),
        "-selection",
        "clipboard",
        "-o",
        "-t",
        "image/png",
        _out=buffer,
        _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
    )
    assert buffer.getvalue() == PNG


def test_the_shim_writes_the_clipboard_when_invoked_as_a_copier(
    held: dict[str, str], tmp_path: Path
) -> None:
    import sh

    endpoint = serving(ClipboardBridge())
    sh.Command(sys.executable)(
        shim(tmp_path, "wl-copy"),
        _in="pasted from inside",
        _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
    )
    assert held["text"] == "pasted from inside"


def test_the_shim_lists_types_for_a_targets_query(
    held: dict[str, str], tmp_path: Path
) -> None:
    import sh

    endpoint = serving(ClipboardBridge())
    listed = sh.Command(sys.executable)(
        shim(tmp_path, "wl-paste"),
        "--list-types",
        _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
    )
    assert "image/png" in str(listed).split()


def test_the_shim_fails_rather_than_hangs_with_no_broker(tmp_path: Path) -> None:
    """No bridge is an ordinary state, and the caller has to learn it promptly."""
    import sh

    with pytest.raises(sh.ErrorReturnCode):
        sh.Command(sys.executable)(
            shim(tmp_path, "xclip"),
            "-o",
            _env={"PATH": "/usr/bin:/bin"},
        )


def test_the_baked_program_is_the_asset_on_disk() -> None:
    """What the image bakes is the file the linters and this suite see.

    The point of the asset being a file rather than a literal: a program
    embedded in the module that installs it is checked by nothing until it is
    already inside a container.
    """
    assert SHIM_ASSET.is_file()
    assert shim_program() == SHIM_ASSET.read_text(encoding="utf-8")
    compile(shim_program(), str(SHIM_ASSET), "exec")


def test_text_is_served_to_a_typed_read_the_way_a_runtime_asks_for_it(
    held: dict[str, str],
) -> None:
    """Measured against the runtime: it reads `text/plain` as a typed read.

    A bridge serving only its image types would refuse the ordinary paste
    while answering the exotic one, and the refusal would read as an empty
    clipboard.
    """
    endpoint = serving(ClipboardBridge())
    reply = request(endpoint, {"op": "typed", "media_type": "text/plain"})
    assert base64.b64decode(reply["data"]).decode("utf-8") == "on the clipboard"
    assert "text/plain" in request(endpoint, {"op": "types"})["types"]


def test_the_shim_answers_a_targets_query_with_text_and_image_alike(
    held: dict[str, str], tmp_path: Path
) -> None:
    """`xclip -t TARGETS -o` is what a runtime enumerates a clipboard with."""
    import sh

    endpoint = serving(ClipboardBridge())
    listed = sh.Command(sys.executable)(
        shim(tmp_path, "xclip"),
        "-selection",
        "clipboard",
        "-t",
        "TARGETS",
        "-o",
        _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
    )
    assert "image/png" in str(listed).split()
    assert "text/plain" in str(listed).split()


def test_the_shim_is_executable_in_its_own_right(
    held: dict[str, str], tmp_path: Path
) -> None:
    """The image links every shim name at it and runs whichever is spelled.

    Exercised by executing the file rather than by handing it to an
    interpreter, which is what the container does and what every other test
    here does not: a shim without a shebang passes each of those and execs to
    nothing under the name a caller actually types.
    """
    import sh

    endpoint = serving(ClipboardBridge())
    program = shim(tmp_path, "xclip")
    program.chmod(0o755)
    printed = sh.Command(str(program))(
        "-selection",
        "clipboard",
        "-o",
        _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
    )
    assert str(printed) == "on the clipboard"


def test_the_session_is_handed_the_bridge_and_no_display() -> None:
    """The bridge speaks no X protocol, so nothing in the session is told it does.

    A `DISPLAY` naming it sent every X client in the session to connect to
    an address with no server behind it: headless Chromium's ANGLE backend
    tried, failed, and gave every page no WebGL at all, with nothing saying
    why. The bridge's own variable is the one address handed in; a runtime
    that finds no display copies through the `tmux` shim or the terminal's
    own escape sequence, and reads through the shims by name.
    """
    bridge = ClipboardBridge()

    assert bridge.environment() == {bridge.variable: bridge.path()}


def test_a_copy_spelled_the_way_a_multiplexer_takes_one_reaches_the_clipboard(
    held: dict[str, str], tmp_path: Path
) -> None:
    """The measured gap: `TMUX` crosses and the socket it names does not.

    A runtime told a multiplexer owns the screen spells its copy as
    `load-buffer` on standard input, and finds no `tmux` at all -- a session
    contradicting itself, with the runtime's own fallbacks the only reason a
    copy lands. `-w` is carried because that is how the spelling
    arrives, and it asks for the terminal's clipboard too, which is the one
    this bridge already is.
    """
    import sh

    endpoint = serving(ClipboardBridge())
    sh.Command(sys.executable)(
        shim(tmp_path, "tmux"),
        "load-buffer",
        "-w",
        "-",
        _in="taken by the buffer",
        _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
    )
    assert held["text"] == "taken by the buffer"


def test_a_buffer_set_from_its_argument_is_not_read_from_stdin(
    held: dict[str, str], tmp_path: Path
) -> None:
    """The two verbs differ in where the text is, which is the whole parse.

    `-b` names a buffer and takes a value that is not the text, so a shim
    reading the first word after the verb would copy the buffer's name.
    """
    import sh

    endpoint = serving(ClipboardBridge())
    sh.Command(sys.executable)(
        shim(tmp_path, "tmux"),
        "set-buffer",
        "-b",
        "scratch",
        "written as an argument",
        _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
    )
    assert held["text"] == "written as an argument"


def test_a_buffer_loaded_from_a_file_is_read_from_it(
    held: dict[str, str], tmp_path: Path
) -> None:
    """`load-buffer` takes a path as readily as `-`, so both are answered."""
    import sh

    endpoint = serving(ClipboardBridge())
    source = tmp_path / "buffer.txt"
    source.write_text("read off the disk", encoding="utf-8")
    sh.Command(sys.executable)(
        shim(tmp_path, "tmux"),
        "load-buffer",
        str(source),
        _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
    )
    assert held["text"] == "read off the disk"


def test_the_tmux_questions_this_session_cannot_answer_are_refused_by_name(
    held: dict[str, str], tmp_path: Path
) -> None:
    """Refused rather than answered, because the server really is out of reach.

    The alternative is a silent success, and it is the shape this bridge's
        header argues against everywhere else: a runtime asking which panes exist
        would get agreement and no panes, and read the emptiness as the answer.
        What it gets instead names where tmux actually is, which is more than a
        `command not found` -- that one contradicts the `TMUX` the same session
        was handed.
    """
    import sh

    endpoint = serving(ClipboardBridge())
    with pytest.raises(sh.ErrorReturnCode) as refusal:
        sh.Command(sys.executable)(
            shim(tmp_path, "tmux"),
            "list-panes",
            _env={"LUP_CLIPBOARD_SOCKET": str(endpoint), "PATH": "/usr/bin:/bin"},
        )
    assert "tmux runs on the host" in refusal.value.stderr.decode("utf-8")
