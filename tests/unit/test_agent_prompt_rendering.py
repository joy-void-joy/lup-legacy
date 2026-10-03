"""The system prompt renders its sections whole, with the date in its place.

A section may hold literal braces — a JSON example — so only the date token
is substituted, and the output format section is derived from the output
model rather than written beside it.
"""

from datetime import datetime

import pytest

from lup_template.agent import prompts
from lup_template.agent.models import AgentOutput


def test_prompt_renders_with_literal_braces_in_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_section = '## Output\n```json\n{"probability": 0.5, "factors": []}\n```'
    monkeypatch.setattr(prompts, "SECTIONS", ["Today is {date}.", json_section])

    rendered = prompts.get_system_prompt()

    assert '{"probability": 0.5, "factors": []}' in rendered
    assert "{date}" not in rendered


def test_prompt_substitutes_date_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompts, "SECTIONS", ["date={date}"])
    assert "date=2030-01-02" in prompts.get_system_prompt(date=datetime(2030, 1, 2))


def test_output_format_section_derives_from_model() -> None:
    section = prompts.output_format()
    for field_name in AgentOutput.model_fields:
        assert field_name in section
