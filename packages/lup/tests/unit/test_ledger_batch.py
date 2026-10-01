"""A batch holds one indexed fold, grown with each append, and reads the same log everybody else does.

Exercised as the writer it exists for: a trove of thousands of nodes,
each resolved by slug before it is recorded so the same thing read twice is
one node. Inside the batch a slug is a lookup; after it, a fresh store reads
what the batch wrote as if it had been written one call at a time.
"""

from pathlib import Path
from typing import Literal

import pytest

from lup.coordination.refs import ActorRef
from lup.coordination.tasks import Blocks, Task
from lup.ledger.journal import LedgerRefusal, LedgerStore
from lup.ledger.models import LedgerNode

AUTHOR = ActorRef(kind="session", id="bulk")


def test_a_batch_resolves_by_slug_and_id_refuses_a_taken_slug_and_lands_every_record(
    tmp_path: Path,
) -> None:
    before = LedgerStore(tmp_path, AUTHOR)
    earlier = before.record(Task, "already here", slug="earlier")

    with LedgerStore(tmp_path, AUTHOR).batch() as store:
        assert store.slug_holder("earlier") == earlier.id
        first = store.record(Task, "first of many", slug="first")
        assert store.resolve("first", [Task]) is not None
        assert store.resolve(first.id, [Task]) is not None
        assert store.kind_at(first.id) == "coordination:task"
        with pytest.raises(LedgerRefusal, match="already names"):
            store.record(Task, "again", slug="first")
        rest = [store.record(Task, f"task {index}") for index in range(50)]
        store.relate(Blocks, first, rest[0])
        done = store.amend(first.completed())
        resolved = store.resolve("first", [Task])
        assert isinstance(resolved, Task) and resolved.done and resolved.id == done.id
        assert len(store.read(Task)) == 52

    after = LedgerStore(tmp_path, AUTHOR)
    assert after.fold is None
    assert len(after.read(Task)) == 52
    assert after.slug_holder("first") == first.id
    [edge] = after.edges(Blocks)
    assert (edge.source, edge.target) == (first.id, rest[0].id)
    resolved = after.resolve("first", [Task])
    assert isinstance(resolved, Task) and resolved.done


def test_inside_a_batch_edges_are_looked_up_by_their_ends_and_a_nested_batch_keeps_the_fold(
    tmp_path: Path,
) -> None:
    store = LedgerStore(tmp_path, AUTHOR)
    first = store.record(Task, "first")
    second = store.record(Task, "second")
    store.relate(Blocks, first, second)
    with store.batch() as held:
        fold = held.fold
        assert fold is not None
        assert [edge.source for edge in held.into(second.id)] == [first.id]
        assert [edge.target for edge in held.out_of(first.id)] == [second.id]
        assert held.into(first.id) == [] and held.out_of(second.id) == []
        # Standing reads the same edges through the fold: second is blocked.
        assert held.standing(second, [Task]).label == "blocked"
        third = held.record(Task, "third")
        held.relate(Blocks, third, first)
        assert [edge.source for edge in held.into(first.id)] == [third.id]
        with held.batch() as inner:
            assert inner.fold is fold
        assert held.fold is fold
    assert store.fold is None
    assert [edge.source for edge in store.into(first.id)] == [third.id]


def test_a_batch_does_not_see_what_another_store_appends_meanwhile(
    tmp_path: Path,
) -> None:
    """The trade a bulk writer makes, stated: the fold is read on entry."""
    other = LedgerStore(tmp_path, ActorRef(kind="session", id="other"))
    with LedgerStore(tmp_path, AUTHOR).batch() as store:
        other.record(Task, "from outside", slug="outside")
        assert store.slug_holder("outside") == ""
    assert LedgerStore(tmp_path, AUTHOR).slug_holder("outside") != ""


def test_a_line_is_read_as_the_class_its_kind_names_and_an_undeclared_kind_as_the_base(
    tmp_path: Path,
) -> None:
    class Note(LedgerNode, frozen=True):
        kind: Literal["test:note"] = "test:note"
        body: str = ""

    store = LedgerStore(tmp_path, AUTHOR)
    task = store.record(Task, "a task")
    note = store.record(Note, "a note", body="b")

    read = store.reader([Note, Task])
    assert isinstance(read({**task.model_dump(mode="json")}), Task)
    assert isinstance(read({**note.model_dump(mode="json")}), Note)
    both = store.all_nodes([Note, Task])
    assert [type(node).__name__ for node in both] == ["Task", "Note"]
    stranger = read({**note.model_dump(mode="json"), "kind": "test:stranger"})
    assert stranger is not None and type(stranger) is LedgerNode
    assert type(store.all_nodes([Task])[1]) is LedgerNode
