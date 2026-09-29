"""A local browser's HTTP authority uses brackets around an IPv6 address."""

import pytest
from httpx import ASGITransport, AsyncClient

from lup.web.serve import page_app


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8766", "http://localhost:8766", "http://[::1]:8766"],
)
@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "localhost",
        "[::1]",
        "127.0.0.1:8766",
        "localhost:8766",
        "[::1]:8766",
    ],
)
async def test_local_authorities_reach_the_page(url: str, host: str) -> None:
    app = page_app("Local page", url, "<main>Local</main>")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=url) as client:
        response = await client.get("/", headers={"Host": host})
    assert response.status_code == 200
    assert response.text == "<main>Local</main>"


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example:8766",
        "[::1].attacker.example:8766",
        "::1:8766",
        "[::1]:9999",
        "127.0.0.1:9999",
    ],
)
async def test_foreign_or_malformed_authorities_are_refused(host: str) -> None:
    url = "http://[::1]:8766"
    app = page_app("Local page", url, "<main>Local</main>")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=url) as client:
        response = await client.get("/", headers={"Host": host})
    assert response.status_code == 421


async def test_a_default_port_url_accepts_its_bracketed_ipv6_authority() -> None:
    url = "http://[::1]"
    app = page_app("Local page", url, "<main>Local</main>")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=url) as client:
        response = await client.get("/")
        malformed = await client.get("/", headers={"Host": "[::1]:None"})
    assert response.status_code == 200
    assert malformed.status_code == 421
