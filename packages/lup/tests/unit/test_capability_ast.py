"""Project-indexed tests for both rules that read a class's base list.

`abc-capability` reads it to tell a capability seam from a variant union;
`abstract-declaration` requires it to carry that answer at all. They are
tested together because they are one reading — a change to what either takes
a base to mean is a change to what the other sees.
"""

from pathlib import Path

import pytest

from lup.harness.codescan.capabilities import (
    audit_abstract_declarations,
    audit_capabilities,
)
from lup.harness.codescan.common import PythonSource


def source(text: str, name: str = "sample") -> PythonSource:
    return PythonSource(path=Path(f"{name}.py"), module=name, text=text)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_cohesive_one_to_three_method_capabilities_pass(count: int) -> None:
    methods = "\n".join(
        f"    @abstractmethod\n    def method_{index}(self) -> None: ..."
        for index in range(count)
    )
    findings = audit_capabilities(
        [source(f"from abc import ABC, abstractmethod\nclass Good(ABC):\n{methods}\n")]
    )
    assert findings == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("    pass", "has 0 abstract behavior methods"),
        (
            "\n".join(
                f"    @abstractmethod\n    def m{index}(self) -> None: ..."
                for index in range(4)
            ),
            "has 4 abstract behavior methods",
        ),
        (
            "    @property\n    @abstractmethod\n    def value(self) -> int: ...",
            "declares abstract property value",
        ),
        (
            "    @abstractmethod\n    def run(self) -> None: ...\n"
            "    def helper(self) -> None: pass",
            "has concrete callable helper",
        ),
    ],
)
def test_invalid_capability_shapes_fail(body: str, message: str) -> None:
    findings = audit_capabilities(
        [source(f"from abc import ABC, abstractmethod\nclass Bad(ABC):\n{body}\n")]
    )
    assert any(message in finding.message for finding in findings)


def test_cross_module_inheritance_and_multiple_capabilities_fail() -> None:
    capabilities = source(
        "from abc import ABC, abstractmethod\n"
        "class Read(ABC):\n"
        "    @abstractmethod\n"
        "    def read(self) -> str: ...\n"
        "class Write(ABC):\n"
        "    @abstractmethod\n"
        "    def write(self) -> None: ...\n",
        "caps",
    )
    implementation = source(
        "from caps import Read, Write\n"
        "class Both(Read, Write):\n"
        "    def read(self) -> str: return ''\n"
        "    def write(self) -> None: pass\n",
        "impl",
    )
    findings = audit_capabilities([capabilities, implementation])
    assert any("implements multiple capabilities" in item.message for item in findings)


def test_inherited_abstract_method_keeps_subclass_abstract() -> None:
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "class Capability(ABC):\n"
                "    @abstractmethod\n"
                "    def run(self) -> None: ...\n"
                "class InvalidChild(Capability):\n"
                "    pass\n"
            )
        ]
    )

    assert any(
        finding.line == 5 and "inherits capability Capability" in finding.message
        for finding in findings
    )


def test_implemented_inherited_abstract_method_is_concrete() -> None:
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "class Capability(ABC):\n"
                "    @abstractmethod\n"
                "    def run(self) -> None: ...\n"
                "class Implementation(Capability):\n"
                "    def run(self) -> None: return None\n"
            )
        ]
    )

    assert findings == []


def test_transitive_implementation_inheritance_fails() -> None:
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "class Capability(ABC):\n"
                "    @abstractmethod\n"
                "    def run(self) -> None: ...\n"
                "class First(Capability):\n"
                "    def run(self) -> None: pass\n"
                "class Second(First):\n"
                "    pass\n"
                "class Third(Second):\n"
                "    pass\n"
            )
        ]
    )

    inherited = [
        finding
        for finding in findings
        if "inherits an implementation" in finding.message
    ]
    assert [finding.line for finding in inherited] == [7, 9]


def test_typed_suppression_is_used_and_spurious_one_is_reported() -> None:
    used = source(
        "from abc import ABC\n"
        "class Marker(ABC):  # lup: ignore[abc-capability]\n"
        "    pass\n",
        "used",
    )
    spurious = source(
        "class Plain:  # lup: ignore[abc-capability]\n    pass\n",
        "spurious",
    )
    findings = audit_capabilities([used, spurious])
    assert len(findings) == 1
    assert findings[0].kind == "spurious"


def test_bare_suppression_is_audited_as_untyped() -> None:
    findings = audit_capabilities(
        [source("from abc import ABC\nclass Marker(ABC):  # lup: ignore\n    pass\n")]
    )
    assert len(findings) == 1
    assert findings[0].kind == "untyped"


def test_abstract_overloads_count_as_one_capability_operation() -> None:
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from typing import overload\n"
                "class Capability(ABC):\n"
                "    @overload\n"
                "    @abstractmethod\n"
                "    def run(self, value: int) -> int: ...\n"
                "    @overload\n"
                "    @abstractmethod\n"
                "    def run(self, value: str) -> str: ...\n"
                "    @abstractmethod\n"
                "    def run(self, value: int | str) -> int | str: ...\n"
            )
        ]
    )

    assert findings == []


def test_model_union_base_is_not_judged_as_a_capability() -> None:
    """A `BaseModel + ABC` base declares a closed set of kinds, not a seam.

    Its declining answers are the shape that lets a walk reach a kind written
    after it, and are exactly what the capability rule refuses on a seam — so
    reading them as concrete behaviour would report the pattern the
    architecture asks for.
    """
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel, ABC):\n"
                "    @abstractmethod\n"
                "    def spell(self) -> str: ...\n"
                "    @property\n"
                "    def text_payload(self) -> str | None:\n"
                "        return None\n"
                "    def invocation(self) -> str | None:\n"
                "        return None\n"
            )
        ]
    )

    assert findings == []


def test_settings_union_base_is_not_judged_as_a_capability() -> None:
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from pydantic_settings import BaseSettings\n"
                "class Tuning(BaseSettings, ABC):\n"
                "    @abstractmethod\n"
                "    def resolve(self) -> str: ...\n"
                "    def helper(self) -> None: pass\n"
            )
        ]
    )

    assert findings == []


def test_variant_of_a_model_union_is_not_pulled_in_as_a_capability() -> None:
    """A variant leaving a member unanswered is still a variant, not a seam."""
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel, ABC):\n"
                "    @abstractmethod\n"
                "    def spell(self) -> str: ...\n"
                "    @abstractmethod\n"
                "    def audited(self) -> str: ...\n"
                "class LocatedPart(Part):\n"
                "    def spell(self) -> str:\n"
                "        return ''\n"
            )
        ]
    )

    assert findings == []


def test_a_seam_declaring_only_abc_is_still_judged() -> None:
    """Excluding unions must not stand a real capability down."""
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Declaration(BaseModel, ABC):\n"
                "    @abstractmethod\n"
                "    def spell(self) -> str: ...\n"
                "    def helper(self) -> None: pass\n"
                "class Renderer(ABC):\n"
                "    @abstractmethod\n"
                "    def render(self) -> str: ...\n"
                "    def helper(self) -> None: pass\n"
            )
        ]
    )

    assert len(findings) == 1
    assert "capability Renderer has concrete callable helper" in findings[0].message


def test_a_model_declaring_an_abstract_member_has_to_name_abc() -> None:
    """The shape this rule exists for: abstract by metaclass, silent in prose.

    Pydantic's metaclass is an `ABCMeta`, so the member binds and the class
    turns uninstantiable while the base list says nothing about it, so a union
    turns abstract without anything saying so.
    """
    findings = audit_abstract_declarations(
        [
            source(
                "from abc import abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel):\n"
                "    @abstractmethod\n"
                "    def spell(self) -> str: ...\n"
            )
        ]
    )

    assert len(findings) == 1
    assert findings[0].kind == "missing"
    assert findings[0].line == 3
    assert "Part declares abstract spell" in findings[0].message


def test_a_declared_abstract_base_is_left_alone() -> None:
    findings = audit_abstract_declarations(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel, ABC):\n"
                "    @abstractmethod\n"
                "    def spell(self) -> str: ...\n"
            )
        ]
    )

    assert findings == []


@pytest.mark.parametrize("spelling", ["ABC", "abc.ABC"])
def test_either_spelling_of_the_base_declares_it(spelling: str) -> None:
    findings = audit_abstract_declarations(
        [
            source(
                "import abc\n"
                "from abc import ABC, abstractmethod\n"
                f"class Seam({spelling}):\n"
                "    @abstractmethod\n"
                "    def run(self) -> None: ...\n"
            )
        ]
    )

    assert findings == []


def test_an_abstract_property_counts_as_a_declaration() -> None:
    findings = audit_abstract_declarations(
        [
            source(
                "from abc import abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel):\n"
                "    @property\n"
                "    @abstractmethod\n"
                "    def value(self) -> int: ...\n"
            )
        ]
    )

    assert len(findings) == 1
    assert "declares abstract value" in findings[0].message


def test_every_member_is_named_at_the_one_line_the_remedy_edits() -> None:
    """One finding per class, because one edit to the base list answers them all."""
    findings = audit_abstract_declarations(
        [
            source(
                "from abc import abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel):\n"
                "    @abstractmethod\n"
                "    def spell(self) -> str: ...\n"
                "    @abstractmethod\n"
                "    def audited(self) -> str: ...\n"
            )
        ]
    )

    assert len(findings) == 1
    assert findings[0].line == 3
    assert "abstract audited, spell" in findings[0].message


def test_a_subclass_adding_an_abstract_member_states_it_too() -> None:
    """Inheriting abstractness is the inference this rule refuses to accept.

    `LocatedPart` is the worked case: abstract through `SemanticPart` and
    abstract again on its own account, and a reader should not have to walk a
    hierarchy to learn either.
    """
    findings = audit_abstract_declarations(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel, ABC):\n"
                "    @abstractmethod\n"
                "    def spell(self) -> str: ...\n"
                "class Located(Part):\n"
                "    @abstractmethod\n"
                "    def where(self) -> str: ...\n"
            )
        ]
    )

    assert len(findings) == 1
    assert findings[0].line == 6
    assert "Located declares abstract where" in findings[0].message


def test_a_variant_answering_the_base_is_not_reported() -> None:
    """Abstractness is read off the class's own body, not its effective set."""
    findings = audit_abstract_declarations(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel, ABC):\n"
                "    @abstractmethod\n"
                "    def spell(self) -> str: ...\n"
                "class Text(Part):\n"
                "    def spell(self) -> str:\n"
                "        return ''\n"
            )
        ]
    )

    assert findings == []


def test_a_protocol_is_satisfied_structurally_and_declares_nothing() -> None:
    """`ABC` beside `Protocol` would claim implementers register, and they do not."""
    findings = audit_abstract_declarations(
        [
            source(
                "from abc import abstractmethod\n"
                "from typing import Protocol\n"
                "class Journal(Protocol):\n"
                "    @abstractmethod\n"
                "    def append(self, event: str) -> None: ...\n"
            )
        ]
    )

    assert findings == []


def test_a_typed_suppression_is_used_and_a_spurious_one_is_reported() -> None:
    used = source(
        "from abc import abstractmethod\n"
        "from pydantic import BaseModel\n"
        "class Part(BaseModel):  # lup: ignore[abstract-declaration]\n"
        "    @abstractmethod\n"
        "    def spell(self) -> str: ...\n",
        "used",
    )
    spurious = source(
        "from pydantic import BaseModel\n"
        "class Plain(BaseModel):  # lup: ignore[abstract-declaration]\n"
        "    value: int\n",
        "spurious",
    )

    findings = audit_abstract_declarations([used, spurious])

    assert len(findings) == 1
    assert findings[0].kind == "spurious"


def test_the_two_rules_reading_the_base_list_do_not_report_each_other() -> None:
    """A union and a seam, each correctly declared, are silent to both rules."""
    declared = source(
        "from abc import ABC, abstractmethod\n"
        "from pydantic import BaseModel\n"
        "class Part(BaseModel, ABC):\n"
        "    @abstractmethod\n"
        "    def spell(self) -> str: ...\n"
        "class Renderer(ABC):\n"
        "    @abstractmethod\n"
        "    def render(self) -> str: ...\n"
    )

    assert audit_capabilities([declared]) == []
    assert audit_abstract_declarations([declared]) == []
