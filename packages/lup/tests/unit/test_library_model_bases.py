"""A project built on this library resolves its bases through the library's classes.

A rule that indexes the project's own files alone sees a library class as a
name resolving to nothing: a ledger kind declared over ``LedgerNode`` would be
reported as a capability inheriting reusable behaviour, because the one base
that makes it a variant union is outside the index, and a walk over the
library's own parts would be reported nowhere. The library's
sources say what its classes are, so the index reads them beside the
project's -- for resolution, and never for a finding about the library.
"""

from pathlib import Path

from lup.harness.codescan.capabilities import audit_capabilities
from lup.harness.codescan.common import PythonSource
from lup.harness.codescan.dispatch import audit_own_model_dispatch
from lup.harness.codescan.project import installed_library_sources


def source(text: str, name: str = "downstream") -> PythonSource:
    return PythonSource(path=Path(f"{name}.py"), module=name, text=text)


def test_a_kind_over_a_library_model_is_a_variant_union_not_a_capability() -> None:
    """A kind over a library model, cleared: the base is a model, so the class is a union."""
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from lup.ledger.models import LedgerNode\n"
                "\n"
                "\n"
                "class KnowledgeItem(LedgerNode, ABC):\n"
                "    title: str\n"
                "\n"
                "    @abstractmethod\n"
                "    def summary(self) -> str: ...\n"
            )
        ]
    )

    assert findings == []


def test_a_walk_over_a_library_variant_is_reported_downstream() -> None:
    """A part among the library's parts is a variant wherever the walk is."""
    findings = audit_own_model_dispatch(
        [
            source(
                "from lup.harness.models import TextPart\n"
                "def walk(part):\n"
                "    match part:\n"
                "        case TextPart(text=text):\n"
                "            return text\n"
                "        case _:\n"
                "            return ''\n"
            )
        ]
    )

    assert [f"{finding.kind}:{finding.line}" for finding in findings] == ["missing:4"]


def test_the_library_contributes_resolution_and_no_findings() -> None:
    """Whatever the library's own classes look like, they are not this project's."""
    assert audit_capabilities([source("x = 1\n")]) == []
    assert audit_own_model_dispatch([source("x = 1\n")]) == []


def test_the_library_is_read_under_the_names_an_import_uses() -> None:
    held = {item.module: item.path for item in installed_library_sources()}

    assert held["lup.ledger.models"].name == "models.py"
    assert held["lup"].name == "__init__.py"
