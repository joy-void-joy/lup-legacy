"""Each repository's setup page is a pane of the dashboard, and an idle page reads little.

A pane is that repository's own CLI serving its own page, started the first
time it is opened and reached under the dashboard's origin behind a path its
capability admits. The queue a page follows is read again only where a relay
changed on disk.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import lup.devtools.dashboard.panes as panes_module
import lup.devtools.dashboard.reviews as reviews
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.panes import SetupPanes
from lup.devtools.dashboard.reviews import ReviewStore, dashboard_app
from lup.devtools.review.app import relay
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion, QuestionRelay
from tests.unit.reviews import bound

BASE_URL = "http://127.0.0.1:8766"
TOKEN = "operator-capability"


def parked(root: Path, question_id: str) -> PersistentQuestion:
    operation = Operation(
        id=f"operation-{question_id}",
        session="native-session",
        requester="asking-session",
        tool="Bash",
        payload={"command": "touch must-not-run"},
        cwd=root,
        worktree=root,
    )
    return relay(root).record(
        bound(
            PersistentQuestion(
                id=question_id,
                operation=operation,
                fingerprint="",
                reason="The operator reviews this command.",
                eligible=["operator"],
                resumption="native_retry",
            )
        )
    )


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SetupPanes:
    """Panes whose repository serves a plain page, as its setup page would."""
    page = tmp_path / "page"
    page.mkdir()
    (page / "index.html").write_text("<main>the repository's setup</main>")

    def setup_command(checkout: Path, port: int) -> list[str]:
        del checkout
        return [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(page),
        ]

    monkeypatch.setattr(panes_module, "setup_command", setup_command)
    known = KnownRepository(repository=tmp_path / ".git", checkout=tmp_path)
    return SetupPanes(lambda: [known], TOKEN, tmp_path / "logs", ready_within=20.0)


async def test_a_pane_serves_its_repository_page_behind_the_dashboards_capability(
    tmp_path: Path, served: SetupPanes
) -> None:
    app = dashboard_app(BASE_URL, TOKEN, (tmp_path,), panes=served)
    [pane] = served.listed()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=BASE_URL
        ) as http:
            listed = await http.get(
                "/api/setup", headers={"Authorization": f"Bearer {TOKEN}"}
            )
            page = await http.get(f"{pane.path}index.html")
            guessed = await http.get(f"/setup/{pane.key}/not-the-capability/")
            unlisted = await http.get("/api/setup")
    finally:
        await asyncio.to_thread(served.close)

    assert [each["name"] for each in listed.json()] == [tmp_path.name]
    assert page.status_code == 200
    assert "the repository's setup" in page.text
    assert page.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    assert guessed.status_code == 404
    assert unlisted.status_code == 401


async def test_a_pane_submission_from_another_origin_is_refused(
    tmp_path: Path, served: SetupPanes
) -> None:
    app = dashboard_app(BASE_URL, TOKEN, (tmp_path,), panes=served)
    [pane] = served.listed()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as http:
        refused = await http.post(
            f"{pane.path}api/wizard/slack/run",
            headers={
                "Origin": "http://attacker.example",
                "content-type": "application/json",
            },
            json={},
        )

    assert refused.status_code == 403
    assert served.children == {}


def test_a_queue_is_read_again_only_where_its_relay_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parked(tmp_path, "first")
    reads: list[Path] = []
    original = reviews.ReviewQueue.read

    def counted(root: Path, store: QuestionRelay) -> reviews.ReviewQueue:
        reads.append(root)
        return original(root, store)

    monkeypatch.setattr(reviews.ReviewQueue, "read", counted)
    store = ReviewStore(roots=(tmp_path,))

    first = store.snapshot()
    again = store.snapshot()
    parked(tmp_path, "second")
    grown = store.snapshot()

    assert again == first
    assert len(reads) == 2
    assert sorted(row.id for row in grown.reviews) == ["first", "second"]
