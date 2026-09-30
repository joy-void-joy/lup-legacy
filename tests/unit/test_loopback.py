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


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("https://their.proxy.name", "their.proxy.name"),
        ("https://their.proxy.name", "their.proxy.name:443"),
        ("http://box.lan", "box.lan:80"),
        ("http://box.lan:8080", "box.lan:8080"),
        ("http://[::1]:9000", "[::1]:9000"),
    ],
)
async def test_a_declared_origins_authority_reaches_the_page(
    origin: str, host: str
) -> None:
    """With and without the scheme's default port; a port of its own only with it."""
    url = "http://127.0.0.1:8766"
    app = page_app("Local page", url, "<main>Local</main>", origins=lambda: [origin])
    async with AsyncClient(transport=ASGITransport(app=app), base_url=url) as client:
        response = await client.get("/", headers={"Host": host})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "their.proxy.name:8443",
        "box.lan",
        "box.lan:80",
        "rebound.their.proxy.name",
    ],
)
async def test_with_origins_declared_every_other_name_is_refused(host: str) -> None:
    url = "http://127.0.0.1:8766"
    app = page_app(
        "Local page",
        url,
        "<main>Local</main>",
        origins=lambda: ["https://their.proxy.name", "http://box.lan:8080"],
        refusal="declare the origin first",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=url) as client:
        response = await client.get("/", headers={"Host": host})
    assert response.status_code == 421
    assert response.text == "declare the origin first"


async def test_an_origin_declared_while_it_serves_is_answered_from_the_next_request() -> (
    None
):
    url = "http://127.0.0.1:8766"
    declared: list[str] = []
    app = page_app("Local page", url, "<main>Local</main>", origins=lambda: declared)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=url) as client:
        before = await client.get("/", headers={"Host": "their.proxy.name"})
        declared.append("https://their.proxy.name")
        after = await client.get("/", headers={"Host": "their.proxy.name"})
    assert before.status_code == 421
    assert after.status_code == 200
