"""The Codex approval policy is one of the app-server's own spellings.

Each is passed through as written, and any other — a camelCase spelling
included — is refused with the four the app-server accepts.
"""

import pytest

from lup_template.agent.core import normalize_codex_approval


def test_codex_approval_passes_the_app_server_spellings_through() -> None:
    accepted = {
        "untrusted": "untrusted",
        "on-request": "on-request",
        "granular": "granular",
        "never": "never",
    }
    assert {value: normalize_codex_approval(value) for value in accepted} == accepted
    assert normalize_codex_approval(None) is None


@pytest.mark.parametrize("spelling", ["always", "unlessTrusted", "onRequest"])
def test_codex_approval_rejects_a_spelling_the_app_server_does_not_take(
    spelling: str,
) -> None:
    with pytest.raises(ValueError, match="app-server accepts"):
        normalize_codex_approval(spelling)
