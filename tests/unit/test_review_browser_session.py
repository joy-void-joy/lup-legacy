"""Browser authority stays in an explicit header, never a host-scoped cookie."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from lup.devtools.dev.questions import review_app


async def test_browser_authority_is_not_emitted_or_accepted_as_a_cookie(
    tmp_path: Path,
) -> None:
    url = "http://127.0.0.1:8766"
    app = review_app(url, "operator-capability", (tmp_path,))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=url) as client:
        authenticated = await client.get(
            "/api/reviews", headers={"Authorization": "Bearer operator-capability"}
        )
        assert authenticated.status_code == 200
        assert "set-cookie" not in authenticated.headers
        client.cookies.set("lup-review-token", "operator-capability")
        assert (await client.get("/api/reviews")).status_code == 401


@pytest.mark.parametrize(
    ("origin", "media_type", "status"),
    [
        ("http://attacker.invalid", "application/json", 403),
        ("http://127.0.0.1:9999", "application/json", 403),
        ("", "application/json", 403),
        ("http://127.0.0.1:8766", "text/plain", 415),
        ("http://127.0.0.1:8766", "application/json", 404),
    ],
)
async def test_browser_answers_require_exact_origin_and_json(
    tmp_path: Path, origin: str, media_type: str, status: int
) -> None:
    url = "http://127.0.0.1:8766"
    app = review_app(url, "operator-capability", (tmp_path,))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=url) as client:
        response = await client.post(
            "/api/reviews/missing/answer",
            headers={
                "Authorization": "Bearer operator-capability",
                "Origin": origin,
                "Content-Type": media_type,
            },
            json={"approved": True, "note": "", "fingerprint": "missing"},
        )

    assert response.status_code == status


async def test_server_restart_invalidates_the_prior_browser_capability(
    tmp_path: Path,
) -> None:
    url = "http://127.0.0.1:8766"
    restarted = review_app(url, "second-capability", (tmp_path,))
    async with AsyncClient(
        transport=ASGITransport(app=restarted), base_url=url
    ) as client:
        assert (
            await client.get(
                "/api/reviews", headers={"Authorization": "Bearer first-capability"}
            )
        ).status_code == 401
        assert (
            await client.get(
                "/api/reviews", headers={"Authorization": "Bearer second-capability"}
            )
        ).status_code == 200
