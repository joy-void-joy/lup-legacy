"""Behavior tests for telling a boundary refusal apart from a bug.

Most of these pin the *negative*: that a failure which merely looks like
confinement is reported as unattributed. A wrong boundary claim teaches an
agent to reach for the host when the bug is in its own code, so the tests
that matter are the ones where nothing is claimed.
"""

from lup.sandbox.attribution import (
    EgressRefusal,
    FilesystemRefusal,
    Unattributed,
    attribute_egress,
    attribute_filesystem,
    candidate_paths,
    requested_host,
)
from lup.sandbox.models import Mount
from lup.sandbox.translation import MountTopology

TOPOLOGY = MountTopology(
    mounts=[
        Mount(
            container_path="/repo",
            source="/host/repo",
            kind="bind",
            mode="rw",
            purpose="the worktree this session owns",
        ),
        Mount(
            container_path="/repo/siblings",
            source="/host/siblings",
            kind="bind",
            mode="ro",
            purpose="other worktrees, present and unwritable",
        ),
    ]
)


def test_a_refused_write_under_a_read_only_mount_names_the_mount() -> None:
    """The whole point: the agent is told it is railed, not that a disk broke."""
    found = attribute_filesystem(
        "OSError: [Errno 30] Read-only file system: '/repo/siblings/dev/src/x.py'",
        TOPOLOGY,
    )
    assert isinstance(found, FilesystemRefusal)
    assert found.path == "/repo/siblings/dev/src/x.py"
    assert found.mount is not None and found.mount.source == "/host/siblings"
    assert "confinement, not a broken filesystem" in found.sentence()


def test_a_refused_write_to_an_unmounted_path_is_not_claimed() -> None:
    """No mount refused it, so the mount table has nothing to say about it.

    A write to an unmounted path inside a container ordinarily *succeeds*,
    into the container's own filesystem -- so a refusal there is not
    confinement, and claiming it would also have swept in every path on the
    machine the table never mentions.
    """
    found = attribute_filesystem(
        "touch: cannot touch '/elsewhere/file': Read-only file system", TOPOLOGY
    )
    assert isinstance(found, Unattributed)


def test_a_write_refused_inside_a_writable_mount_is_not_the_boundary() -> None:
    """The mount is rw, so confinement is not what refused this.

    The failure text carries the marker and the path is mounted -- everything
    a careless attribution needs to claim the boundary. The mode is what
    settles it, and this is the case where guessing would be wrong.
    """
    found = attribute_filesystem(
        "PermissionError: [Errno 13] Permission denied: '/repo/src/x.py'", TOPOLOGY
    )
    assert isinstance(found, Unattributed)


def test_a_failure_with_no_refusal_marker_is_not_the_boundary() -> None:
    """An ordinary bug that happens to mention a read-only path."""
    found = attribute_filesystem(
        "ModuleNotFoundError: no module named 'lup' (searched /repo/siblings)",
        TOPOLOGY,
    )
    assert isinstance(found, Unattributed)


def test_a_marker_alone_never_carries_a_claim() -> None:
    """`Read-only file system` is also what a genuinely read-only disk says."""
    found = attribute_filesystem("mount: /dev/sda1: Read-only file system", TOPOLOGY)
    assert isinstance(found, Unattributed)
    assert "not attributable to the boundary" in found.sentence()


def test_candidate_paths_survive_the_punctuation_diagnostics_wrap_them_in() -> None:
    """Quotes and trailing colons are how error messages actually print paths."""
    found = candidate_paths("cannot open '/a/b': No such file, tried \"/c/d\", (/e/f)")
    assert found == ["/a/b", "/c/d", "/e/f"]


def test_candidate_paths_ignores_words_that_are_not_paths() -> None:
    assert candidate_paths("a bare / and a flag -/x and nothing else") == []


def test_a_proxy_denial_names_the_host_it_refused() -> None:
    """Read from the proxy's own line, which knows what the client only guesses."""
    found = attribute_egress(
        [
            "1787 10 172.17.0.3 TCP_DENIED/403 3813 CONNECT crates.io:443 - HIER_NONE/-",
            "1787 12 172.17.0.3 TCP_TUNNEL/200 5 CONNECT pypi.org:443 - ORIGINAL_DST/-",
        ]
    )
    assert found == [EgressRefusal(host="crates.io")]
    assert "add the host to the egress declaration" in found[0].sentence()


def test_a_proxy_log_with_no_denials_attributes_nothing() -> None:
    assert attribute_egress(["1787 12 x TCP_TUNNEL/200 5 CONNECT pypi.org:443 -"]) == []


def test_a_denied_plain_request_names_the_host_from_its_url() -> None:
    """A tunnel carries `host:port`, an ordinary request carries a whole URL."""
    found = attribute_egress(
        ["1787 3 172.17.0.3 TCP_DENIED/403 4 GET http://example.com/x - HIER_NONE/-"]
    )
    assert found == [EgressRefusal(host="example.com")]


def test_requested_host_reports_nothing_for_a_line_naming_no_host() -> None:
    """Attributing nothing is the right answer, and has to be reachable."""
    assert requested_host("1787 3 172.17.0.3 TCP_DENIED/403 4 - - HIER_NONE/-") == ""


def test_a_busy_mount_point_is_the_boundary_refusing_a_replacement() -> None:
    """A bind-mounted file cannot be unlinked, so renaming over it says busy, not read-only."""
    found = attribute_filesystem(
        "error: unable to unlink old '/repo/siblings/README.md': Device or resource busy",
        TOPOLOGY,
    )
    assert isinstance(found, FilesystemRefusal)
    assert found.path == "/repo/siblings/README.md"


def test_git_names_its_path_relative_to_the_checkout() -> None:
    """The refusal an agent most needs explained names no absolute path.

    `unable to unlink old 'README.md'` is what git writes when a read-only
    mount refuses a fast-forward, and an absolute-only reading finds no path
    in it at all — so the one message the rail exists to explain arrived
    unattributed.
    """
    assert candidate_paths("unable to unlink old 'README.md'") == []
    assert candidate_paths("unable to unlink old 'README.md'", "/work") == [
        "/work/README.md"
    ]


def test_a_bare_word_is_not_a_path_however_a_directory_is_named() -> None:
    """Quoted or nothing: crude is safe for `/x`, and not for every noun."""
    assert candidate_paths("Device or resource busy", "/work") == []
