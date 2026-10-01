"""What a contained session can reach, and what it deliberately cannot.

The boundary is one HTTP proxy on a network with no gateway, and the whole of
its correctness is which requests go to it. A destination that should be
refused and is not is a hole; a destination that never needed the proxy and is
sent there anyway is a wall across the middle of the container, which is the
failure these hold still.
"""

from pathlib import Path

from lup.formats import digest
from lup.harness.egress import SessionEgress
from lup.harness.image import Image


def test_a_session_reaches_its_own_loopback_without_the_proxy() -> None:
    """An agent that starts a dev server and curls it is talking to itself.

    Measured before this was written: with ``NO_PROXY`` emptied, ``curl -v
    http://localhost:3000`` inside the container answered ``Uses proxy env
    variable http_proxy`` and went to squid, which refused it as a denied
    local name and again as a port outside 80 and 443. Nothing was protected
    -- the container is on a network with no gateway and cannot reach the
    host's loopback whatever the proxy says.
    """
    environment = SessionEgress().environment("10.89.0.29")

    assert environment["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert environment["no_proxy"] == environment["NO_PROXY"]


def test_the_exemption_is_written_rather_than_inherited() -> None:
    """The original reason for emptying it survives the fix.

    A host exporting ``NO_PROXY=some.corp.host`` would otherwise hand the
    session an exemption for a destination the internal network has no route
    to, which is a hang rather than a refusal. Writing the value keeps that
    out; it is only the *emptiness* that was wrong.
    """
    assert "corp" not in SessionEgress().environment("10.89.0.29")["NO_PROXY"]


def test_an_unfiltered_session_is_handed_no_proxy_variables_at_all() -> None:
    """There is no proxy to point at, so naming one would point at nothing."""
    assert SessionEgress(mode="bridge").environment("10.89.0.29") == {}


def test_a_session_publishes_nothing_to_the_host_by_default() -> None:
    """Publishing is the one hole in the container that faces the operator."""
    assert Image().published_ports == []
    assert "-p" not in Image().run_arguments(Path("/checkout"), 1000, 1000)


def test_a_declared_port_reaches_the_host_on_the_same_number() -> None:
    """Same number both sides, so a URL an agent prints is one a human can open."""
    arguments = Image(published_ports=[5173]).run_arguments(
        Path("/checkout"), 1000, 1000
    )

    assert arguments[arguments.index("-p") + 1] == "5173:5173"


def test_an_image_built_from_a_different_declaration_is_stale() -> None:
    """Presence was the wrong question: a tag is built once and edited never.

    Every later change to the declaration -- a pinned CLI, a package, the
    entrypoint -- landed in the repository and never in the thing a session
    actually ran in, because from the outside a stale image and a current one
    are the same tag.
    """
    assert digest.text("FROM a") != digest.text("FROM b")
    assert digest.text("FROM a") == digest.text("FROM a")


def test_a_session_with_no_proxy_address_is_handed_no_variables() -> None:
    """Pointing at a proxy that is not there is worse than pointing at nothing.

    A session with no proxy variables fails at its first request in the
    client's own words. One pointed at an address nothing answers on fails in
    the proxy's — a sentence about a boundary that was never built, which is
    the vocabulary this whole design exists to keep out of a session.
    """
    assert SessionEgress().environment("") == {}
