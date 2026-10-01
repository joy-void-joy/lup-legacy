"""The two channel primitives: a value that settles, and an ordered log."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter

from lup.channels.models import (
    ChannelConflictError,
    ChannelCorruptionError,
    ChannelOverflowError,
    Door,
    DoorPolicy,
    write_atomic,
)
from lup.channels.slot import Slot, SlotSet
from lup.channels.stream import Stream
from lup.execution.locks import exclusive, try_exclusive


class Decision(BaseModel):
    value: str


ADAPTER = TypeAdapter(Decision)


def test_a_slot_settles_once_and_the_first_writer_wins(tmp_path: Path) -> None:
    slot = Slot(tmp_path / "d", Decision)

    assert slot.settle(Decision(value="first")) is True
    assert slot.settle(Decision(value="second")) is False
    settled = slot.settled()
    assert settled is not None
    assert settled.value == "first"


def test_an_offer_is_correctable_right_up_until_it_counts(tmp_path: Path) -> None:
    slot = Slot(tmp_path / "d", Decision)

    slot.offer(Decision(value="typo"))
    slot.offer(Decision(value="corrected"))
    offered = slot.offered()
    assert offered is not None
    assert offered.value == "corrected"


def test_an_offer_may_precede_the_decision_it_answers(tmp_path: Path) -> None:
    """A flag can settle a question the run has not reached yet."""
    slot = Slot(tmp_path / "d", Decision)

    slot.offer(Decision(value="early"))
    assert slot.declared() is None

    slot.declare(Decision(value="asked"))
    offered = slot.offered()
    assert offered is not None
    assert offered.value == "early"


def test_redeclaring_the_same_decision_is_a_no_op(tmp_path: Path) -> None:
    slot = Slot(tmp_path / "d", Decision)
    slot.declare(Decision(value="asked"))
    slot.declare(Decision(value="asked"))

    with pytest.raises(ChannelConflictError, match="already declared differently"):
        slot.declare(Decision(value="asked something else"))


def test_a_slot_refuses_a_door_its_policy_excludes(tmp_path: Path) -> None:
    """Excluding AGENT is how a run is stopped from releasing its own park."""
    slot = Slot(tmp_path / "d", Decision, DoorPolicy(excluded=[Door.AGENT]))

    assert slot.settle(Decision(value="human said so"), Door.PAGE) is True
    with pytest.raises(ChannelConflictError, match="does not accept"):
        slot.settle(Decision(value="I say so"), Door.AGENT)


def test_an_unreadable_slot_is_named_rather_than_read_as_absent(
    tmp_path: Path,
) -> None:
    slot = Slot(tmp_path / "d", Decision)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "settled.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ChannelCorruptionError, match="settled.json"):
        slot.settled()


def test_clearing_a_slot_lets_a_resumed_run_decide_it_again(tmp_path: Path) -> None:
    slot = Slot(tmp_path / "d", Decision)
    slot.declare(Decision(value="asked"))
    slot.offer(Decision(value="maybe"))
    slot.settle(Decision(value="settled"))

    slot.clear()

    assert slot.declared() is None
    assert slot.offered() is None
    assert slot.settled() is None


def test_a_slot_set_reports_only_the_slots_that_reached_each_state(
    tmp_path: Path,
) -> None:
    slots: SlotSet[Decision] = SlotSet(tmp_path, Decision)
    slots.slot("a").declare(Decision(value="asked a"))
    slots.slot("b").declare(Decision(value="asked b"))
    slots.slot("b").settle(Decision(value="answered b"))

    assert slots.names() == ["a", "b"]
    assert len(slots.declared()) == 2
    assert slots.settled_names() == ["b"]


def test_a_stream_reads_complete_records_from_any_offset(tmp_path: Path) -> None:
    stream: Stream[Decision] = Stream(tmp_path / "log.jsonl", ADAPTER)
    stream.append(Decision(value="one"))
    after_first = stream.append(Decision(value="two"))
    stream.append(Decision(value="three"))

    assert [item.value for item in stream.read_all()] == ["one", "two", "three"]
    assert [pair.item.value for pair in stream.read_from(after_first)] == ["three"]


def test_a_partial_trailing_record_is_not_read(tmp_path: Path) -> None:
    """A reader must never see a record a writer is still appending."""
    path = tmp_path / "log.jsonl"
    stream: Stream[Decision] = Stream(path, ADAPTER)
    stream.append(Decision(value="complete"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"value": "half-writ')

    assert [item.value for item in stream.read_all()] == ["complete"]


def test_a_malformed_record_is_skipped_rather_than_poisoning_the_log(
    tmp_path: Path,
) -> None:
    path = tmp_path / "log.jsonl"
    stream: Stream[Decision] = Stream(path, ADAPTER)
    stream.append(Decision(value="before"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
    stream.append(Decision(value="after"))

    assert [item.value for item in stream.read_all()] == ["before", "after"]


def test_committing_per_record_never_drops_an_unapplied_one(tmp_path: Path) -> None:
    """Applying one record then committing its offset consumes exactly it."""
    stream: Stream[Decision] = Stream(tmp_path / "log.jsonl", ADAPTER)
    stream.append(Decision(value="one"))
    stream.append(Decision(value="two"))

    pairs = stream.read_from(0)
    resumed = stream.read_from(pairs[0].commit_offset)
    assert [pair.item.value for pair in resumed] == ["two"]


def test_a_capped_stream_pushes_back_on_a_writer_nobody_is_reading(
    tmp_path: Path,
) -> None:
    stream: Stream[Decision] = Stream(tmp_path / "log.jsonl", ADAPTER, max_bytes=40)
    stream.append(Decision(value="x" * 40))

    with pytest.raises(ChannelOverflowError, match="nobody is consuming"):
        stream.append(Decision(value="more"))


def test_an_uncapped_stream_never_refuses_to_record(tmp_path: Path) -> None:
    """A journal must record whatever happens, so its cap is optional."""
    stream: Stream[Decision] = Stream(tmp_path / "log.jsonl", ADAPTER)
    for index in range(50):
        stream.append(Decision(value="x" * 100 + str(index)))

    assert len(stream.read_all()) == 50


def test_writers_racing_on_one_file_never_publish_a_torn_one(tmp_path: Path) -> None:
    """Each stages under a name of its own, so the file is always one writer's whole bytes.

    A staging name they shared was truncated by the next writer while the
    first was still filling it, and the first's rename then published a file
    whose front was NUL bytes.
    """
    path = tmp_path / "state.json"
    bodies = [bytes([ord("a") + number]) * 200_000 for number in range(16)]
    finished = threading.Event()

    def torn_reads() -> list[int]:
        torn: list[int] = []
        for _ in iter(finished.is_set, True):
            read = path.read_bytes() if path.exists() else bodies[0]
            if read not in bodies:
                torn.append(len(read))
        return torn

    with ThreadPoolExecutor(max_workers=len(bodies) + 1) as pool:
        reading = pool.submit(torn_reads)
        try:
            list(pool.map(lambda body: write_atomic(path, body), bodies * 4))
        finally:
            finished.set()

    assert reading.result() == []
    assert path.read_bytes() in bodies
    assert [held.name for held in tmp_path.iterdir()] == ["state.json"]


def test_an_atomic_write_gets_the_mode_a_plain_one_would(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.write_bytes(b"{}")

    write_atomic(tmp_path / "atomic", b"{}")

    assert (tmp_path / "atomic").stat().st_mode == plain.stat().st_mode


def test_an_atomic_write_given_a_mode_lands_with_exactly_it(tmp_path: Path) -> None:
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"OLD=1\n")
    secret.chmod(0o644)

    write_atomic(secret, b"NEW=1\n", mode=0o600)

    assert secret.stat().st_mode & 0o777 == 0o600
    assert secret.read_bytes() == b"NEW=1\n"


def test_a_durable_atomic_write_lands_whole(tmp_path: Path) -> None:
    write_atomic(tmp_path / "nested" / "login.json", b"{}", durable=True)

    assert (tmp_path / "nested" / "login.json").read_bytes() == b"{}"
    assert [held.name for held in (tmp_path / "nested").iterdir()] == ["login.json"]


def test_a_compare_and_swap_replaces_only_what_it_read(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_bytes(b"read")

    write_atomic(ledger, b"mine", expected=b"read")
    with pytest.raises(ChannelConflictError, match="changed after it was read"):
        write_atomic(ledger, b"stale", expected=b"read")

    assert ledger.read_bytes() == b"mine"
    assert [held.name for held in tmp_path.iterdir()] == ["ledger.json"]


def test_a_held_section_is_refused_to_a_second_holder_and_free_after(
    tmp_path: Path,
) -> None:
    """The file the writers agree on, beside what they write, made where it is missing."""
    lock = tmp_path / "nested" / "state.lock"

    with exclusive(lock):
        with try_exclusive(lock) as second:
            assert second is False
    with try_exclusive(lock) as after:
        assert after is True


def test_a_section_waits_for_its_holder_rather_than_interleaving(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "state.lock"
    order: list[str] = []
    entered = threading.Event()

    def holder() -> None:
        with exclusive(lock):
            order.append("held")
            entered.set()
            threading.Event().wait(0.2)
            order.append("released")

    with ThreadPoolExecutor(max_workers=1) as pool:
        held = pool.submit(holder)
        entered.wait()
        with exclusive(lock):
            order.append("waited")
        held.result()

    assert order == ["held", "released", "waited"]
