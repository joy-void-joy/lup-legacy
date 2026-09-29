"""Declining conversation removes its browser setup and its imports together."""

import importlib
import sys
from unittest.mock import Mock

import pytest

import lup.devtools.setup as setup
from lup.devtools.dev.commands import CommandSurface
from lup.devtools.roster import LIBRARY_ROSTER, DevtoolsDeclarations


def test_setup_imports_without_the_declined_conversation_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "lup.devtools.conversation.app", None)

    imported = importlib.reload(setup)
    surface = CommandSurface.of(imported.create_setup_app([]))

    assert all("conversation" not in command.path for command in surface.reached)
    assert surface.runs(["status"])


def test_retiring_conversation_removes_browser_setup_from_the_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "lup.devtools.conversation.app", None)
    declared = Mock(spec=DevtoolsDeclarations, integrations=[], profiles=None)
    retired = [
        entry.spec.name for entry in LIBRARY_ROSTER if entry.spec.name != "setup"
    ]

    entries = DevtoolsDeclarations.roster(declared, retired)

    assert [entry.spec.name for entry in entries] == ["setup"]
    surface = CommandSurface.of(entries[0].app)
    assert all("conversation" not in command.path for command in surface.reached)
    assert surface.runs(["status"])
