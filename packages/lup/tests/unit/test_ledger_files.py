"""Files as nodes: pinned to their bytes when recorded, and standing only while
the tree still holds those bytes."""

from pathlib import Path

from lup.coordination.refs import ActorRef
from lup.formats import digest
from lup.ledger.files import File
from lup.ledger.journal import LedgerStore
from lup.ledger.models import Surroundings

AUTHOR = ActorRef(kind="test", id="t")


def test_a_file_is_pinned_when_recorded_and_stands_while_its_bytes_do(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "parser.py"
    target.write_text("one", encoding="utf-8")
    store = LedgerStore(tmp_path, AUTHOR)

    node = store.record(File, "", path="src/parser.py")

    assert node.title == "src/parser.py" and node.digest == digest.file(target)
    assert store.standing(node).label == "fresh"
    target.write_text("two", encoding="utf-8")
    stale = store.standing(node)
    assert (
        stale.label == "stale" and not stale.sound and "src/parser.py" in stale.reason
    )
    target.unlink()
    assert store.standing(node).label == "missing"
    assert node.standing(Surroundings()).label == "unchecked"


def test_a_digest_recorded_elsewhere_is_kept_and_a_title_is_honoured(
    tmp_path: Path,
) -> None:
    store = LedgerStore(tmp_path, AUTHOR)

    replayed = store.record(File, "the parser", path="src/parser.py", digest="abc")

    assert replayed.digest == "abc" and replayed.title == "the parser"
    assert store.standing(replayed).label == "missing"
    assert [found.id for found in store.read(File)] == [replayed.id]
