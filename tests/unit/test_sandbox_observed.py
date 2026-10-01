"""Behavior tests for reading the mount table from inside the boundary.

The interesting failure here is not an exception. A topology read wrongly
still parses, still answers, and simply never attributes anything -- which
looks exactly like a boundary that was not involved. So these pin the shape
of what is read, not just that reading succeeded.
"""

from pathlib import Path

import pytest

import lup.sandbox.observed as observed
from lup.sandbox.models import Mount
from lup.sandbox.observed import (
    ObservedMount,
    is_mount_point,
    observed_topology,
    parsed,
)
from lup.sandbox.translation import MountTopology

LISTED = """
{
   "filesystems": [
      {"target": "/", "fsroot": "/"},
      {"target": "/repo", "fsroot": "/host/repo"},
      {"target": "/repo/siblings", "fsroot": "/host/siblings"}
   ]
}
"""

TREE = """
{
   "filesystems": [
      {"target": "/", "fsroot": "/", "children": [
         {"target": "/repo", "fsroot": "/host/repo"},
         {"target": "/repo/siblings", "fsroot": "/host/siblings"}
      ]}
   ]
}
"""


def test_a_flat_listing_becomes_one_row_per_mount() -> None:
    """The ordinary read: every mount the table names is a row."""
    assert [row.target for row in parsed(LISTED).filesystems] == [
        "/",
        "/repo",
        "/repo/siblings",
    ]


def test_a_tree_listing_collapses_to_its_root() -> None:
    """Why the caller asks for a list, pinned as the silence it would cause.

    `findmnt` nests by default, and a nested payload validates cleanly into a
    table holding only the root -- no error, no warning, just a topology that
    covers one path and therefore attributes nothing: a boundary that has
    stopped explaining itself, with nothing saying so.
    """
    assert [row.target for row in parsed(TREE).filesystems] == ["/"]


def test_output_that_is_not_json_reads_as_an_empty_table() -> None:
    """A missing tool degrades to no claim rather than to a second failure."""
    assert parsed("findmnt: unrecognized option").filesystems == []


def test_the_host_side_is_the_bind_subpath() -> None:
    """``fsroot`` already holds what ``source`` only carries in brackets."""
    assert ObservedMount(target="/repo", fsroot="/host/repo").declared().source == (
        "/host/repo"
    )


def test_a_row_with_no_fsroot_names_itself() -> None:
    """A mount with no separate host side is still a mount worth reporting."""
    assert ObservedMount(target="/proc").declared().source == "/proc"


def test_the_running_system_answers_with_more_than_its_root() -> None:
    """The whole read, against whatever this machine actually has mounted.

    Deliberately weak about *which* mounts: the fixtures above pin the shape,
    and this pins only that the real call reaches the same flat listing rather
    than the tree. A machine with exactly one mount would make this vacuous,
    and there is no such machine running a test suite.
    """
    assert len(observed_topology().mounts) > 1


def test_a_mount_point_is_read_off_the_table_not_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe answers from what is mounted, which is the whole point.

    ``Path.is_mount`` compares device numbers, so a bind mount from the same
    filesystem -- every checkout a launch binds -- reads as an ordinary
    directory. A real one cannot be made without privileges, so this pins the
    seam instead: a directory the table names is a mount point though nothing
    about it on disk says so, which is false under the device comparison and
    true under this one.
    """
    bound = tmp_path / "checkout"
    bound.mkdir()
    monkeypatch.setattr(
        observed,
        "observed_topology",
        lambda: MountTopology(
            mounts=[
                Mount(
                    container_path=str(bound),
                    source="/host/checkout",
                    kind="bind",
                    mode="rw",
                    purpose="test",
                )
            ]
        ),
    )

    assert not bound.is_mount()
    assert is_mount_point(bound)
    assert not is_mount_point(tmp_path / "unbound")
