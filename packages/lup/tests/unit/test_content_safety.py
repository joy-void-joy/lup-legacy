"""Spilling oversized tool results."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from lup.workspace.content_safety import (
    ContentSafetyConfig,
    configure,
    guard_result,
    save_content,
    slugify_label,
    spill_oversized_result,
    state,
)


@pytest.fixture(autouse=True)
def clean_state(tmp_path: Path) -> Iterator[None]:
    state.config = ContentSafetyConfig(directory=tmp_path)
    yield
    state.config = None


class Fetched(BaseModel):
    url: str
    body: str


class TestSpill:
    def test_a_result_under_the_threshold_is_returned_unchanged(self) -> None:
        configure(spill_threshold=1_000)
        result = Fetched(url="https://example.com/a", body="short")

        assert spill_oversized_result("fetch", "a", result) is result

    def test_an_oversized_field_becomes_a_pointer_to_a_written_file(
        self, tmp_path: Path
    ) -> None:
        configure(spill_threshold=50)
        body = "x" * 200
        result = Fetched(url="https://example.com/a", body=body)

        spilled = spill_oversized_result("fetch", "a", result)

        assert spilled.url == result.url
        assert "Content written to" in spilled.body
        written = list(tmp_path.glob("fetch_a*.md"))
        assert [path.read_text(encoding="utf-8") for path in written] == [body]

    def test_identical_content_reuses_one_file(self, tmp_path: Path) -> None:
        save_content("fetch", "same", "body text")
        save_content("fetch", "same", "body text")

        assert len(list(tmp_path.glob("fetch_same*.md"))) == 1

    def test_differing_content_takes_the_next_free_name(self, tmp_path: Path) -> None:
        save_content("fetch", "same", "first")
        save_content("fetch", "same", "second")

        assert len(list(tmp_path.glob("fetch_same*.md"))) == 2


class TestGuardResult:
    def test_the_file_is_named_from_the_input_that_identifies_it(
        self, tmp_path: Path
    ) -> None:
        configure(spill_threshold=50)
        params = Fetched(url="https://arxiv.org/abs/2401.12345", body="")
        result = Fetched(url=params.url, body="y" * 200)

        guard_result("fetch", params, result)

        assert [path.name for path in tmp_path.glob("*.md")] == [
            "fetch_abs-2401-12345.md"
        ]

    def test_the_tool_name_stands_in_when_nothing_names_the_document(
        self, tmp_path: Path
    ) -> None:
        class Unnamed(BaseModel):
            body: str

        configure(spill_threshold=50)
        result = Unnamed(body="z" * 200)

        guard_result("fetch", Unnamed(body=""), result)

        assert [path.name for path in tmp_path.glob("*.md")] == ["fetch_fetch.md"]


class TestSlugifyLabel:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("https://www.example.com/docs/intro", "docs-intro"),
            ("https://example.com", "example-com"),
            ("A Plain Label", "a-plain-label"),
            ("https://example.com/only", "example-com-only"),
        ],
    )
    def test_the_identifying_tail_survives(self, label: str, expected: str) -> None:
        assert slugify_label(label) == expected


class NestedResult(BaseModel):
    """A result whose weight is records rather than prose."""

    query: str = ""
    hits: list[dict[str, str]] = []


class TestNothingSpillableIsReported:
    """A payload a pointer cannot shrink is named, not passed on quietly.

    Only a top-level string can hold a pointer, so a model carrying its
    weight in nested collections is invisible to the spill. Left silent,
    the tool that owns it learns nothing, and the runtime on the far side
    writes the payload somewhere the adopter's policy never granted.
    """

    def oversized(self) -> NestedResult:
        return NestedResult(query="q", hits=[{"text": "x" * 200} for _ in range(20)])

    def test_it_is_returned_unchanged(self) -> None:
        configure(spill_threshold=100)
        result = self.oversized()

        assert spill_oversized_result("wide", "q", result) is result

    def test_it_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        configure(spill_threshold=100)

        with caplog.at_level("WARNING"):
            spill_oversized_result("wide", "q", self.oversized())

        assert "no spillable field" in caplog.text
        assert "wide" in caplog.text

    def test_a_payload_under_the_threshold_is_quiet(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        configure(spill_threshold=100_000)

        with caplog.at_level("WARNING"):
            spill_oversized_result("wide", "q", self.oversized())

        assert "no spillable field" not in caplog.text


class TestThresholdIsBelowTheRuntimeCeiling:
    def test_default_leaves_room_under_the_mcp_cap(self) -> None:
        """25,000 tokens estimated at four characters each, halved."""
        assert ContentSafetyConfig(directory=Path(".")).spill_threshold <= 50_000
