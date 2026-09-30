"""A review whose recorded files moved leaves the queue as stale, wherever that is noticed.

Review 85008796 could not be approved from the page: a `cp` of three live
files into scratch, then a `sed -i` of the copies, parked with the three
sources recorded as its preimages, and the session editing those sources
changed them a hundred seconds later. From then on no approval could release
it -- the waiter carries a command out only where every recorded file
stands as recorded -- yet the page showed it waiting, with Approve enabled
from a detail read before the change, and the refusal landed out of sight.
Now whoever notices first -- the dashboard on its next look, the page
opening it, an approval, the requester's own waiter, `review list` -- retires
it into ``stale``, naming what moved, and the requester is told to re-read
the file and ask again.
"""

import os
from hashlib import sha256
from pathlib import Path
from typing import Final

import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from lup.coordination.identity import MEMBER_ENV
from lup.devtools.dashboard import reviews as dashboard
from lup.devtools.dashboard.reviews import ReviewSnapshot
from lup.devtools.review.app import ReviewDetail, create_review_app, relay
from lup.devtools.review.preimages import moved
from lup.policy.operations import Operation
from lup.policy.relay import CapturedFileReview, PersistentQuestion
from lup.providers.claude.identity import CLAUDE_SESSION_ENV
from tests.unit.native import bound

BASE_URL: Final = "http://127.0.0.1:8765"
TOKEN: Final = "stale-operator-secret"
AUTHORIZATION: Final = {"Authorization": f"Bearer {TOKEN}"}
ANSWER_HEADERS: Final = {**AUTHORIZATION, "Origin": BASE_URL}
SESSION: Final = "stale-session"
RUNNER: Final = CliRunner()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    monkeypatch.setenv(CLAUDE_SESSION_ENV, SESSION)
    monkeypatch.delenv(MEMBER_ENV, raising=False)
    return checkout


def copied_then_rewritten(root: Path) -> PersistentQuestion:
    """Review 85008796's shape: live sources copied into scratch, the copies rewritten in place.

    Its preimages are the three sources as they stood, and the scratch
    directory and copies as absent -- which is what the hook records.
    """
    sources = [root / "packages" / name for name in ("shell_rules.py", "rows.py")]
    for source in sources:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"# {source.name}\namending_flags = []\n")
    scratch = root / "tmp/amend"
    operation = Operation(
        id="operation-85008796",
        session=SESSION,
        requester=SESSION,
        tool="Bash",
        payload={
            "command": "mkdir -p tmp/amend && cp packages/shell_rules.py "
            "packages/rows.py tmp/amend/ && sed -i -e '/amending_flags/d' "
            "tmp/amend/shell_rules.py tmp/amend/rows.py"
        },
        cwd=root,
        worktree=root,
    )
    return relay(root).record(
        bound(
            PersistentQuestion(
                id="85008796371a98c0cb05f3795d8bca7c",
                operation=operation,
                fingerprint="",
                reason="sed would rewrite tmp/amend/shell_rules.py in place, and no "
                "file stands there",
                eligible=[],
                chain_resolved=False,
                resumption="native_retry",
                preconditions={
                    **{source: source.read_text() for source in sources},
                    scratch: None,
                    scratch / "shell_rules.py": None,
                    scratch / "rows.py": None,
                },
            )
        )
    )


def client(root: Path) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=dashboard.dashboard_app(BASE_URL, TOKEN, (root,))),
        base_url=BASE_URL,
    )


async def snapshot(http: AsyncClient) -> ReviewSnapshot:
    response = await http.get("/api/reviews", headers=AUTHORIZATION)
    assert response.status_code == 200
    return ReviewSnapshot.model_validate(response.json())


async def test_a_source_edited_after_parking_retires_the_review_on_the_next_look(
    root: Path,
) -> None:
    question = copied_then_rewritten(root)
    async with client(root) as http:
        (waiting,) = (await snapshot(http)).reviews
        assert waiting.state == "pending" and waiting.answerable
        (root / "packages/rows.py").write_text("# rows.py, edited by its owner\n")
        (retired,) = (await snapshot(http)).reviews

    assert retired.state == "stale"
    assert not retired.answerable
    assert [each.path for each in retired.stale] == [root / "packages/rows.py"]
    assert retired.stale[0].cause == "changed"
    stored = relay(root).find(question.id)
    assert stored is not None and stored.state == "stale"
    assert stored.moved == [root / "packages/rows.py"]
    assert relay(root).pending() == []


async def test_an_approval_given_from_a_detail_read_before_the_edit_is_refused_saying_why(
    root: Path,
) -> None:
    question = copied_then_rewritten(root)
    async with client(root) as http:
        (waiting,) = (await snapshot(http)).reviews
        opened = await http.get(f"/api/reviews/{waiting.key}", headers=AUTHORIZATION)
        assert ReviewDetail.model_validate(opened.json()).summary.answerable
        (root / "packages/shell_rules.py").write_text("# edited\n")
        approved = await http.post(
            f"/api/reviews/{waiting.key}/answer",
            headers=ANSWER_HEADERS,
            json={"approved": True, "fingerprint": question.fingerprint},
        )

    assert approved.status_code == 409
    reason = approved.json()["detail"]
    assert "went stale" in reason
    assert "shell_rules.py" in reason
    stored = relay(root).find(question.id)
    assert stored is not None and stored.state == "stale" and stored.answer is None


async def test_opening_a_review_retires_it_when_its_files_moved(root: Path) -> None:
    question = copied_then_rewritten(root)
    async with client(root) as http:
        (waiting,) = (await snapshot(http)).reviews
        (root / "tmp/amend").mkdir(parents=True)
        opened = await http.get(f"/api/reviews/{waiting.key}", headers=AUTHORIZATION)

    detail = ReviewDetail.model_validate(opened.json())
    assert detail.summary.state == "stale"
    assert [each.path for each in detail.summary.stale] == [root / "tmp/amend"]
    stored = relay(root).find(question.id)
    assert stored is not None and stored.moved == [root / "tmp/amend"]


def test_a_directory_where_absence_was_recorded_is_a_move(root: Path) -> None:
    question = copied_then_rewritten(root)
    assert moved(question) == []

    (root / "tmp/amend").mkdir(parents=True)

    (found,) = moved(question)
    assert found.path == root / "tmp/amend"
    assert found.cause == "directory"
    assert "a directory now stands at" in found.sentence()


def test_the_requester_s_waiter_retires_it_and_says_to_ask_again(root: Path) -> None:
    question = copied_then_rewritten(root)
    (root / "packages/rows.py").write_text("# rows.py, edited by its owner\n")

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert waited.exit_code == 1, waited.output
    assert f"review {question.id} — stale:" in waited.output
    assert str(root / "packages/rows.py") in waited.output
    assert "re-read it and ask again" in waited.output
    stored = relay(root).find(question.id)
    assert stored is not None and stored.state == "stale"


def test_listing_the_queue_retires_what_moved(root: Path) -> None:
    question = copied_then_rewritten(root)
    (root / "packages/rows.py").unlink()

    listed = RUNNER.invoke(create_review_app(root), ["list"])

    assert listed.exit_code == 0, listed.output
    assert "nothing is waiting" in listed.output
    stored = relay(root).find(question.id)
    assert stored is not None and stored.state == "stale"
    assert stored.moved == [root / "packages/rows.py"]


def test_a_preimage_is_compared_byte_for_byte_line_endings_included(
    root: Path,
) -> None:
    """A file still holding its recorded CRLF text stands; the same text with LF has moved."""
    question = copied_then_rewritten(root)
    source = root / "packages/rows.py"
    recorded = "# rows.py\r\namending_flags = []\r\n"
    source.write_bytes(recorded.encode())
    question = relay(root).record(
        bound(
            question.model_copy(
                update={"preconditions": {**question.preconditions, source: recorded}}
            )
        )
    )
    assert moved(question) == []

    source.write_bytes(recorded.replace("\r\n", "\n").encode())

    (found,) = moved(question)
    assert found.path == source and found.cause == "changed"


def recorded_beside(
    root: Path, elsewhere: dict[Path, str | None]
) -> PersistentQuestion:
    """The copy-then-rewrite review, recording *elsewhere* too, as the hook would."""
    question = copied_then_rewritten(root)
    return relay(root).record(
        bound(
            question.model_copy(
                update={"preconditions": {**question.preconditions, **elsewhere}}
            )
        )
    )


async def test_a_path_the_dashboard_cannot_see_is_never_stale(
    root: Path, tmp_path: Path
) -> None:
    """A source inside the session's container is nowhere on the host: nothing says it moved."""
    container = tmp_path / "container/tmp/claude-1000/scratchpad/source.py"
    question = recorded_beside(root, {container: "# copied from inside\n"})

    assert moved(question) == []
    async with client(root) as http:
        (waiting,) = (await snapshot(http)).reviews
        opened = await http.get(f"/api/reviews/{waiting.key}", headers=AUTHORIZATION)
    listed = RUNNER.invoke(create_review_app(root), ["list"])

    assert waiting.state == "pending" and waiting.answerable
    assert waiting.stale == []
    assert ReviewDetail.model_validate(opened.json()).summary.answerable
    assert question.id in listed.output
    stored = relay(root).find(question.id)
    assert stored is not None and stored.state == "pending"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
async def test_a_file_the_dashboard_may_not_read_is_never_stale(root: Path) -> None:
    sealed = root / "packages/sealed.py"
    sealed.parent.mkdir(parents=True)
    sealed.write_text("# sealed\n")
    question = recorded_beside(root, {sealed: "# as the session read it\n"})
    sealed.chmod(0)
    try:
        assert moved(question) == []
        async with client(root) as http:
            (waiting,) = (await snapshot(http)).reviews
    finally:
        sealed.chmod(0o644)

    assert waiting.state == "pending" and waiting.answerable


def copied_by_a_command(root: Path) -> PersistentQuestion:
    """A command's record as the hook keeps one: the copy it writes, and the source it reads."""
    source = root / "packages/notes.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# notes\n")
    target = root / "packages/copy.md"
    operation = Operation(
        id="operation-copy",
        session=SESSION,
        requester=SESSION,
        tool="Bash",
        payload={"command": "cp packages/notes.md packages/copy.md"},
        cwd=root,
        worktree=root,
    )
    return relay(root).record(
        bound(
            PersistentQuestion(
                id="copy-review",
                operation=operation,
                fingerprint="",
                reason="copying over a protected file",
                eligible=[],
                chain_resolved=False,
                resumption="native_retry",
                preconditions={source: "# notes\n", target: None},
                file_reviews=[
                    CapturedFileReview(
                        path=target,
                        effect="ask",
                        reason="copying over a protected file",
                        rule="shell:cp",
                        rules=["shell:cp"],
                        before_sha256=None,
                        after_sha256=sha256(b"# notes\n").hexdigest(),
                        after="# notes\n",
                    )
                ],
                unpreviewed=[],
            )
        )
    )


async def test_a_source_a_command_reads_from_is_not_what_the_page_judges(
    root: Path,
) -> None:
    """Only the files a call writes decide whether its review can still be answered."""
    question = copied_by_a_command(root)
    (root / "packages/notes.md").write_text("# notes, edited since\n")

    assert moved(question) == []
    async with client(root) as http:
        (waiting,) = (await snapshot(http)).reviews
    assert waiting.state == "pending" and waiting.answerable


def test_the_waiter_runs_no_copy_of_a_source_that_moved(root: Path) -> None:
    """Running it would land something other than what the operator approved."""
    question = copied_by_a_command(root)
    relay(root).answer(question.id, "operator", True)
    (root / "packages/notes.md").write_text("# notes, edited since\n")

    waited = RUNNER.invoke(create_review_app(root), ["wait", question.id])

    assert f"review {question.id} — stale:" in waited.output
    assert "packages/notes.md changed since the operator saw this copy" in waited.output
    assert "nothing ran — re-read it and ask again" in waited.output
    assert not (root / "packages/copy.md").exists()
    stored = relay(root).find(question.id)
    assert stored is not None and stored.state == "stale"
    assert stored.moved == [root / "packages/notes.md"]
