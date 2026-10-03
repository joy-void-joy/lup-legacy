"""Which clipboard backend answers, and what each one can honestly be asked.

Nothing here talks to a real clipboard: the point of the module is the
declaration, and a test that needed a display server would be skipped on
exactly the machines whose absence of one this is about. What is pinned is
the shape -- the ordering that keeps XWayland from answering for the wrong
clipboard, and the refusal to ask a text-only backend for an image.
"""

import socket

from lup.devtools.clipboard import (
    CLIPBOARD_TOOLS,
    ClipboardTool,
    clipboard_image,
    clipboard_probes,
    clipboard_text,
    copy_to_clipboard,
    reachable_backend,
    readable_backends,
)
from lup.harness.clipboard import ClipboardBridge, ClipboardHandler, ClipboardServer
from lup.harness.toolchain import clipboard_requirement


def test_wayland_is_tried_before_x11() -> None:
    """The trap this ordering exists for, and the one a reorder would restore.

    A Wayland session commonly also has `xclip`, answering through XWayland
    against a clipboard the user is not copying into. Trying X11 first finds a
    tool that works and gives wrong answers, which is worse than finding none.
    """
    names = [tool.writer for tool in CLIPBOARD_TOOLS]
    assert names.index("wl-copy") < names.index("xclip")


def test_the_backend_that_can_enumerate_is_tried_before_the_one_that_cannot() -> None:
    """An image paste needs types, and only some backends know about them."""
    names = [tool.writer for tool in CLIPBOARD_TOOLS]
    assert names.index("xclip") < names.index("xsel")


def test_only_the_backends_that_can_enumerate_say_they_can() -> None:
    """`xsel` and `pbpaste` print characters and know nothing about types."""
    enumerating = {tool.writer for tool in CLIPBOARD_TOOLS if tool.enumerates()}
    assert enumerating == {"wl-copy", "xclip"}


def test_a_text_only_backend_refuses_a_typed_read_rather_than_guessing() -> None:
    """Asking `pbpaste` for a PNG returns its text, which is not a PNG.

    Bytes that are not an image, saved with a `.png` suffix, is a paste that
    looks like it worked until something tries to open the file.
    """
    text_only = ClipboardTool(writer="pbcopy", reader="pbpaste")
    assert text_only.typed("image/png") == b""
    assert text_only.types() == []


def test_a_backend_with_no_reader_answers_nothing_rather_than_failing() -> None:
    """Absence is a valid answer here: CI and containers have no clipboard."""
    write_only = ClipboardTool(writer="clip")
    assert write_only.text() is None


def test_every_backend_reads_through_its_own_binary() -> None:
    """Half of them use a different program for each direction.

    A roster that let the writer and the reader be chosen separately could
    pick `wl-copy` on a machine that has it beside `pbpaste` on one that does
    not, and the pairing is the whole reason this is one declaration.
    """
    assert {(tool.writer, tool.reader) for tool in CLIPBOARD_TOOLS} >= {
        ("wl-copy", "wl-paste"),
        ("pbcopy", "pbpaste"),
    }


def test_nothing_answers_when_no_backend_is_installed() -> None:
    """The whole roster missing is an ordinary machine, not an error."""
    absent = (ClipboardTool(writer="lup-no-such-writer", reader="lup-no-such-reader"),)
    assert copy_to_clipboard("x", absent) is False
    assert clipboard_text(absent) is None
    assert clipboard_image(("image/png",), absent) is None


def test_the_manifest_exercises_the_same_backends_the_code_reaches_for() -> None:
    """Derived from the code, the requirement cannot list a backend it never tries.

    A requirement naming Wayland while the code that reaches for a clipboard
    tries only X11 tools tells a Wayland machine it has a clipboard that then
    silently fails to work.
    """
    probed = set(clipboard_requirement().exercise.programs())
    assert probed == {tool.reader for tool in CLIPBOARD_TOOLS if tool.reader}


def test_every_probe_reads_the_clipboard_rather_than_writing_it() -> None:
    """A probe that wrote would destroy what the operator had put there.

    Every launch, to establish something they were not asking about.
    """
    written = {tool.writer for tool in CLIPBOARD_TOOLS} - {
        tool.reader for tool in CLIPBOARD_TOOLS
    }
    assert not [probe for probe in clipboard_probes() if probe[0] in written]


def test_no_probe_settles_for_asking_a_program_its_version() -> None:
    """`wl-copy --version` succeeds with no compositor and `xclip -version` with
    no DISPLAY, which is exactly the machine where pasting does nothing."""
    assert not [
        probe
        for probe in clipboard_probes()
        if any("version" in word for word in probe)
    ]


# `true` and `false` rather than a clipboard tool, because the subject is the
# exit status and those two are the only programs every platform this runs on
# agrees about. A test naming `xclip` would pass or fail on what the machine
# happens to have installed, which is the thing being measured.
ANSWERS = ClipboardTool(writer="true", reader="true", text_arguments=[])
"""A backend reachable from this process, whatever the machine is."""

REFUSES = ClipboardTool(writer="false", reader="false", text_arguments=[])
"""One installed and unable to answer — a display it cannot reach."""

ABSENT = ClipboardTool(writer="lup-no-such-writer", reader="lup-no-such-reader")
"""One that is not installed at all, which is a different thing to report."""


def test_an_empty_clipboard_is_not_an_unreachable_backend() -> None:
    """The distinction the whole function exists for.

    Both print nothing. Reading the output would report a working machine as
    broken every time the operator happens to have copied nothing.
    """
    assert reachable_backend((ANSWERS,)) == "true"


def test_a_backend_that_cannot_answer_is_passed_over_for_one_that_can() -> None:
    """Installed is not reachable, and the order decides which is reported."""
    assert reachable_backend((REFUSES, ANSWERS)) == "true"
    assert reachable_backend((ABSENT, REFUSES, ANSWERS)) == "true"


def test_nothing_answering_is_reported_as_nothing_rather_than_a_guess() -> None:
    """A headless host is an ordinary machine, not a broken one."""
    assert reachable_backend((ABSENT, REFUSES)) == ""


def test_the_backends_named_in_a_diagnostic_are_the_ones_actually_tried() -> None:
    """A list written twice comes apart, and this one is read by an operator
    whose desktop this repository knows nothing about."""
    assert readable_backends() == [
        tool.reader for tool in CLIPBOARD_TOOLS if tool.reader
    ]


def test_a_peer_that_closed_before_its_answer_is_not_an_error() -> None:
    """A copy that worked is never followed by a traceback over the session.

    A client may close as soon as it stops caring about the reply, and the
    write that lands on the closed socket is the only thing that notices.
    Left to escape, it reaches the operator through the base class's stderr
    -- painted over a session drawing a full-screen interface, blaming a
    clipboard that did its job.

    Refused rather than answered, so this stays a test about the socket and
    talks to no real clipboard, as this module's header promises.
    """
    ours, theirs = socket.socketpair()
    theirs.sendall(b"not a request\n")
    theirs.close()
    listener = ClipboardServer.__new__(ClipboardServer)
    listener.bridge = ClipboardBridge()
    ClipboardHandler(ours, "", listener)
