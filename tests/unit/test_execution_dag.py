"""The ordering both the resolver and a run schedule by.

Two products ride one graph, so what these pin is that the graph carries the
caller's vocabulary rather than either product's: the same cycle is reported
as a concern cycle to one and a step cycle to the other. The graph walks
downstream too — what rests on a node — because that is the direction
invalidation travels, and a run that could only walk upward would have to
maintain by hand the list of what a recomputed step spoils.
"""

import pytest
from pydantic import BaseModel

from lup.execution.dag import DependencyGraph, DependencyGraphError


class Node(BaseModel, frozen=True):
    """The least a graph needs of a node."""

    id: str
    dependencies: list[str] = []


def chain() -> DependencyGraph[Node]:
    """A diamond: two independent middles over one root, joined by one leaf."""
    return DependencyGraph(
        [
            Node(id="root"),
            Node(id="left", dependencies=["root"]),
            Node(id="right", dependencies=["root"]),
            Node(id="leaf", dependencies=["left", "right"]),
        ],
        subject="step",
    )


def test_independent_nodes_come_back_in_one_batch() -> None:
    batches = [[node.id for node in batch] for batch in chain().topological_batches()]
    assert batches == [["root"], ["left", "right"], ["leaf"]]


def test_ancestors_are_everything_a_node_rests_on() -> None:
    assert [node.id for node in chain().ancestors("leaf")] == [
        "root",
        "left",
        "right",
    ]
    assert chain().ancestors("root") == []


def test_descendants_are_everything_that_rests_on_a_node() -> None:
    """The direction a forced rerun spoils, which is why it is ordered too."""
    assert [node.id for node in chain().descendants("root")] == [
        "left",
        "right",
        "leaf",
    ]
    assert [node.id for node in chain().descendants("left")] == ["leaf"]
    assert chain().descendants("leaf") == []


def test_a_cycle_is_reported_in_the_caller_s_own_word() -> None:
    with pytest.raises(DependencyGraphError, match="step graph contains a cycle"):
        DependencyGraph(
            [Node(id="a", dependencies=["b"]), Node(id="b", dependencies=["a"])],
            subject="step",
        )
    with pytest.raises(DependencyGraphError, match="concern graph contains a cycle"):
        DependencyGraph(
            [Node(id="a", dependencies=["b"]), Node(id="b", dependencies=["a"])],
            subject="concern",
        )


def test_a_missing_dependency_is_refused_at_construction() -> None:
    with pytest.raises(DependencyGraphError, match="references missing nodes: ghost"):
        DependencyGraph([Node(id="a", dependencies=["ghost"])], subject="step")


def test_duplicate_ids_are_refused() -> None:
    with pytest.raises(DependencyGraphError, match="ids must be unique"):
        DependencyGraph([Node(id="a"), Node(id="a")], subject="step")


def test_walking_from_an_unknown_node_says_which_one() -> None:
    with pytest.raises(DependencyGraphError, match="unknown step 'ghost'"):
        chain().descendants("ghost")
