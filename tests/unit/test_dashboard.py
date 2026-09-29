"""Setup dashboard behavior over the shared declarative registry.

The dashboard and the CLI wizard read one ``Integration`` list, so what is
pinned here is that the projection keeps its promises: every declared
integration reaches the page, a flow that only prompts on a terminal is drawn
as one rather than quietly dropped, and an integration writes the keys it
declared or none.

The last test is about the origin rather than the registry, and it is the one
that matters most: this surface writes the user's credentials.
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from lup.devtools import setup
from lup.devtools.dashboard.setup_page import create_setup_page
from lup.devtools.dashboard.wizard import WizardView
from lup_template.devtools.setup import INTEGRATIONS

BASE_URL = "http://127.0.0.1:8765"

SCOPE = "?scope=env"
"""The one scope this library's own dashboard has."""


@pytest.fixture
def isolated_dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the dashboard against an isolated env file."""
    monkeypatch.setattr(setup, "ENV_LOCAL", tmp_path / ".env.local")


def client() -> AsyncClient:
    """A client over the dashboard, bound to the host it will answer for."""
    return AsyncClient(
        transport=ASGITransport(app=create_setup_page(BASE_URL, INTEGRATIONS)),
        base_url=BASE_URL,
    )


def step_named(view: WizardView, slug: str):
    """One step of a drawn page."""
    return next(step for step in view.steps if step.slug == slug)


async def test_the_dashboard_serves_its_built_page(isolated_dashboard: None) -> None:
    """The page is the `wizard` bundle under `lup.web`'s package data.

    Served by name from the built surface, with its script reachable under
    the bundle's own assets — the version that read a loose asset shipped
    without it, so every request here raised ``FileNotFoundError`` and
    adopters retired the sub-app.
    """
    del isolated_dashboard
    async with client() as http:
        response = await http.get("/")
        script = next(
            name
            for name in response.text.split('"')
            if name.startswith("./assets/") and name.endswith(".js")
        )
        asset = await http.get(script.removeprefix("./"))

    assert response.status_code == 200
    assert asset.status_code == 200 and "api/wizard" in asset.text


async def test_dashboard_projects_cli_registry_status(
    isolated_dashboard: None,
) -> None:
    del isolated_dashboard
    async with client() as http:
        response = await http.get("/api/wizard" + SCOPE)
    view = WizardView.model_validate(response.json())

    assert response.status_code == 200
    assert {step.slug for step in view.steps} == {item.command for item in INTEGRATIONS}


async def test_a_terminal_only_flow_is_drawn_and_withheld(
    isolated_dashboard: None,
) -> None:
    """It has no browser shape to infer, so the page names the command instead.

    Drawn rather than dropped: an integration missing from the page reads as
    one this project does not have.
    """
    del isolated_dashboard
    async with client() as http:
        response = await http.get("/api/wizard" + SCOPE)
    google = step_named(WizardView.model_validate(response.json()), "google")

    assert not google.standing.offered
    assert "lup-devtools setup google" in google.standing.blocked


async def test_dashboard_writes_only_declared_integration_fields(
    isolated_dashboard: None,
) -> None:
    del isolated_dashboard
    async with client() as http:
        response = await http.post(
            "/api/wizard/api-key/run" + SCOPE,
            json={"answers": [{"key": "EXAMPLE_API_KEY", "value": "configured"}]},
        )

    assert response.status_code == 200
    assert setup.read_env_local() == {"EXAMPLE_API_KEY": "configured"}


async def test_dashboard_rejects_cross_integration_and_bespoke_writes(
    isolated_dashboard: None,
) -> None:
    """A key belonging to another integration is not this one's to write.

    What a page posts is untrusted, so a step reads its own declared fields and
    nothing else — and a step the page never offered cannot be run by naming
    it.
    """
    del isolated_dashboard
    async with client() as http:
        cross = await http.post(
            "/api/wizard/api-key/run" + SCOPE,
            json={"answers": [{"key": "NOTION_TOKEN", "value": "wrong-surface"}]},
        )
        bespoke = await http.post(
            "/api/wizard/google/run" + SCOPE,
            json={"answers": [{"key": "GMAIL_CREDENTIALS_PATH", "value": "x"}]},
        )

    assert not cross.json()["outcome"]["ok"]
    assert not bespoke.json()["outcome"]["ok"]
    assert setup.read_env_local() == {}


async def test_a_rebound_host_cannot_write_the_env_file(
    isolated_dashboard: None,
) -> None:
    """A loopback bind does not stop the browser being pointed here.

    DNS rebinding makes a page the user is merely visiting into this origin,
    so the same-origin policy permits it and CORS never runs. The Host header
    is the one thing that still names the attacker, and this surface writes
    the user's credentials — so it is the one that most needs to check.
    """
    del isolated_dashboard
    async with AsyncClient(
        transport=ASGITransport(app=create_setup_page(BASE_URL, INTEGRATIONS)),
        base_url="http://evil.example",
    ) as http:
        page = await http.get("/")
        write = await http.post(
            "/api/wizard/api-key/run" + SCOPE,
            json={"answers": [{"key": "EXAMPLE_API_KEY", "value": "stolen"}]},
        )

    assert page.status_code == 421
    assert write.status_code == 421
    assert setup.read_env_local() == {}
