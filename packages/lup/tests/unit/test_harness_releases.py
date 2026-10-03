"""What a launch claims an unpinned agent CLI's version is, and on whose word.

The failure this guards against is quiet in the way only a cache can be:
an unpinned install rendered as a bare name is fetched once, frozen into a
layer, and served as "latest" for months -- so a session's CLI can predate
the model it is asked to run. Resolution turns the
registry's answer into a concrete pin in the rendered text, so the
content-addressed tag is what notices a release and nothing here trusts a
layer's memory.

Every test stubs the registry through the ``ask`` seam and points the
ledger at its own file: the network is exactly the dependency whose absence
these tests also have to describe.
"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pydantic
import pytest

from lup.launch.container import image_tag
from lup.harness.image import Image
from lup.harness.notice import Notice
from lup.harness.releases import (
    ReleaseLedger,
    ReleaseRecord,
    current_release,
    resolved_agent_clis,
)
from lup.harness.requirements import Manifest, Package


def quietly(notice: Notice) -> None:
    """Swallow the progress line: these tests assert on the returned notices."""


def test_an_unpinned_cli_is_pinned_to_what_the_registry_answers(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    def answering(url: str) -> str:
        asked.append(url)
        return "9.9.9"

    resolution = resolved_agent_clis(
        Image(), cache=tmp_path / "releases.json", ask=answering, say=quietly
    )

    rendered = resolution.image.dockerfile(Manifest())
    assert "@anthropic-ai/claude-code@9.9.9" in rendered
    assert "@openai/codex@9.9.9" in rendered
    assert asked == [
        "https://registry.npmjs.org/@anthropic-ai/claude-code/latest",
        "https://registry.npmjs.org/@openai/codex/latest",
    ]
    assert resolution.said == []


def test_a_fresh_answer_is_remembered_for_the_launch_that_gets_none(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "releases.json"

    resolved_agent_clis(Image(), cache=ledger, ask=lambda url: "9.9.9", say=quietly)

    remembered = ReleaseLedger.model_validate_json(ledger.read_bytes())
    assert {record.name for record in remembered.records} == {
        "@anthropic-ai/claude-code",
        "@openai/codex",
    }
    assert all(record.version == "9.9.9" for record in remembered.records)


def test_a_declared_pin_is_a_decision_resolution_does_not_touch(
    tmp_path: Path,
) -> None:
    pinned = Image(
        agent_clis=[
            Package(name="@anthropic-ai/claude-code", manager="bun", version="1.0.0"),
        ]
    )

    def refusing(url: str) -> str:
        raise AssertionError(f"asked {url} about a pinned package")

    resolution = resolved_agent_clis(
        pinned, cache=tmp_path / "releases.json", ask=refusing, say=quietly
    )

    assert resolution.image == pinned
    assert resolution.said == []
    assert not (tmp_path / "releases.json").exists()


def test_a_silent_registry_is_answered_by_the_last_release_this_machine_resolved(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "releases.json"
    remembered = ReleaseLedger(
        records=[
            ReleaseRecord(
                name="@anthropic-ai/claude-code",
                version="8.8.8",
                resolved_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
            ReleaseRecord(
                name="@openai/codex",
                version="7.7.7",
                resolved_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ]
    )
    ledger.write_text(remembered.model_dump_json())

    def down(url: str) -> str:
        raise httpx.ConnectError("registry unreachable")

    resolution = resolved_agent_clis(Image(), cache=ledger, ask=down, say=quietly)

    rendered = resolution.image.dockerfile(Manifest())
    assert "@anthropic-ai/claude-code@8.8.8" in rendered
    assert "@openai/codex@7.7.7" in rendered
    warnings = [line for line in resolution.said if line.urgency == "warning"]
    dated = [line for line in resolution.said if line.indent == 1]
    assert len(warnings) == 2
    assert any("8.8.8" in line.text and "2026-01-02" in line.text for line in dated)


def test_a_silent_registry_and_an_empty_ledger_leave_the_version_to_the_build(
    tmp_path: Path,
) -> None:
    def down(url: str) -> str:
        raise httpx.ConnectError("registry unreachable")

    resolution = resolved_agent_clis(
        Image(), cache=tmp_path / "releases.json", ask=down, say=quietly
    )

    rendered = resolution.image.dockerfile(Manifest())
    assert all(not item.version for item in resolution.image.agent_clis)
    assert "bun add -g @anthropic-ai/claude-code @openai/codex" in rendered
    assert all(line.urgency == "warning" for line in resolution.said)
    assert len(resolution.said) == 2


def test_the_wait_is_named_before_the_registry_is_asked(tmp_path: Path) -> None:
    order: list[str] = []

    def watching(notice: Notice) -> None:
        order.append("said")

    def answering(url: str) -> str:
        order.append("asked")
        return "9.9.9"

    resolved_agent_clis(
        Image(), cache=tmp_path / "releases.json", ask=answering, say=watching
    )

    assert order[0] == "said"
    assert order.count("said") == 1


def test_current_release_reads_the_registry_wire_shape() -> None:
    def latest(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "1.2.3", "name": "x", "dist": {}})

    with httpx.Client(transport=httpx.MockTransport(latest)) as client:
        assert current_release("https://registry.test/x/latest", client) == "1.2.3"


def test_current_release_refuses_an_answer_with_no_version() -> None:
    def shapeless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "not found"})

    with httpx.Client(transport=httpx.MockTransport(shapeless)) as client:
        with pytest.raises(pydantic.ValidationError):
            current_release("https://registry.test/x/latest", client)


def test_current_release_raises_on_a_registry_that_answers_unwell() -> None:
    def unwell(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(unwell)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            current_release("https://registry.test/x/latest", client)


def test_a_moved_release_is_what_moves_the_image_tag(tmp_path: Path) -> None:
    """The rebuild decision stays with the machinery that already makes it."""
    tags = {
        image_tag(
            resolved_agent_clis(
                Image(),
                cache=tmp_path / "releases.json",
                ask=lambda url: answer,
                say=quietly,
            ).image.dockerfile(Manifest())
        )
        for answer in ("1.0.0", "2.0.0")
    }

    assert len(tags) == 2
