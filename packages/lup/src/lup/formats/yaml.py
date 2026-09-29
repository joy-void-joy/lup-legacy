"""The nodes a generated YAML document is declared as.

The argument :mod:`lup.formats.markdown` makes about a table cell, made about
the format where the container is least forgiving. A derived value entering
YAML through an f-string can end the mapping it lands in — a colon, a newline,
a leading ``*``, a word a reader takes for a boolean — and the file stays
plausible enough that what notices is whatever consumes it later. Indentation
is a container here too: a value carrying a newline moves every line after it
into a different part of the document.

So nothing here writes YAML. A node holds the data it stands for, with the
comment and the blank line a generated file owes whoever reads it, and
:mod:`ruamel.yaml` emits all of it — quoting, indentation, block scalars and
comment placement alike. What this module is for is the declaration: a typed
tree a generator builds instead of a string it formats.

Two readers, one document. ruamel writes YAML 1.2, where ``on`` and ``yes``
are text; pyyaml reads YAML 1.1, where they are booleans; and a generated file
meets both, since the forge parses one version and this repository's own
loaders the other. :func:`unambiguous` asks the stricter reader whether a bare
word still reads as itself and quotes it where it does not, and
:meth:`YamlDocument.renders_what_it_holds` parses the whole document back
through that same stricter reader and holds it against what the nodes declare.
A value that broke out of its container cannot come back as the value that
went in, so the document refuses to be built rather than being written and
found later.
"""

import io
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Discriminator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString, SingleQuotedScalarString

type ScalarValue = str | int | float | bool | None
"""A value YAML writes as one scalar, whatever the spelling turns out to be."""

type PlainData = ScalarValue | list["PlainData"] | dict[str, "PlainData"]
"""What a node stands for, in the shape a parser hands it back."""

type EmittedValue = ScalarValue | CommentedSeq | CommentedMap
"""The same data in the shapes ruamel emits from, styles and comments carried."""


def unambiguous(text: str) -> str:
    """This text as a scalar both YAML versions read as the text it is.

    A bare ``on``, ``yes`` or ``no`` is a string to a 1.2 reader and a boolean
    to a 1.1 one, and `on:` is the key every workflow opens with. Quoting is
    not a judgement about which reader is right: a quoted scalar is a string
    to both, so the disagreement stops existing.

    Text the stricter reader cannot parse at all — a leading ``*``, a stray
    bracket — is quoted for the same reason, and the two readers agree about
    that one anyway.
    """
    try:
        bare = yaml.safe_load(text)
    except yaml.YAMLError:
        return SingleQuotedScalarString(text)
    if isinstance(bare, str) and bare == text:
        return text
    return SingleQuotedScalarString(text)


def emitted(value: PlainData) -> EmittedValue:
    """This data in the shapes ruamel emits it from.

    What it decides is which ruamel type each piece becomes, and a string is
    where that matters: multi-line as a literal block, so the script inside a
    workflow step reads as the script it is, and everything else through
    :func:`unambiguous`.
    """
    match value:
        case str() if "\n" in value:
            return LiteralScalarString(value)
        case str():
            return unambiguous(value)
        case list():
            return CommentedSeq(emitted(item) for item in value)
        case dict():
            return CommentedMap(
                (unambiguous(key), emitted(item)) for key, item in value.items()
            )
        case _:
            return value


class Layout(BaseModel, frozen=True):
    """Where each level of a generated document sits, in columns.

    ruamel counts a sequence's dash inside its indent and ``dash`` places it
    within that width, so 4 and 2 are the pair that write `  - item` under the
    key naming it — what a workflow is read and written with everywhere else.
    A comment is placed by column rather than by depth, which is the whole
    reason a node is told where it stands.
    """

    mapping: int = 2
    sequence: int = 4
    dash: int = 2


class YamlNode(BaseModel, ABC, frozen=True):
    """One node of a generated YAML document, holding the value it stands for.

    Two answers per kind: the data it means, which is what the document is
    checked against, and the object ruamel emits it from, which is where a
    kind says how it wants to be written.
    """

    @abstractmethod
    def plain(self) -> PlainData:
        """The data this node stands for, as a parser would hand it back."""

    @abstractmethod
    def composed(self, layout: Layout, indent: int) -> EmittedValue:
        """This node as the ruamel value the emitter writes, at this column."""


class YamlScalar(YamlNode, frozen=True):
    """One value, spelled the way the emitter spells it."""

    type: Literal["scalar"] = "scalar"
    value: ScalarValue

    def plain(self) -> PlainData:
        return self.value

    def composed(self, layout: Layout, indent: int) -> EmittedValue:
        return emitted(self.value)


class YamlFlow(YamlNode, frozen=True):
    """A short sequence written on the line that names it, as `[main]`."""

    type: Literal["flow"] = "flow"
    items: Sequence[ScalarValue]
    """Taken as a sequence rather than a list, so a caller hands over the
    values it already holds instead of copying them into an invariant one."""

    def plain(self) -> PlainData:
        return list(self.items)

    def composed(self, layout: Layout, indent: int) -> EmittedValue:
        sequence = CommentedSeq(emitted(item) for item in self.items)
        sequence.fa.set_flow_style()
        return sequence


class YamlItem(BaseModel, frozen=True):
    """One item of a sequence, with what a reader needs above its dash.

    The sequence's counterpart to :class:`YamlEntry`: a workflow step is
    explained where it starts, and a comment on the first key of the mapping
    it holds would land between the dash and that key instead.
    """

    value: "YamlAny"
    comment: str = ""


class YamlList(YamlNode, frozen=True):
    """A sequence written one dashed item per line.

    Told the column its key's value would start at, as every node is, while
    its dash stands ``dash`` columns in from the key and each item's own
    keys ``sequence`` columns in — which is where a comment on either goes.
    """

    type: Literal["list"] = "list"
    items: list[YamlItem]

    def plain(self) -> PlainData:
        return [item.value.plain() for item in self.items]

    def composed(self, layout: Layout, indent: int) -> EmittedValue:
        key = indent - layout.mapping
        sequence = CommentedSeq(
            item.value.composed(layout, key + layout.sequence) for item in self.items
        )
        for index, item in enumerate(self.items):
            if item.comment:
                sequence.yaml_set_comment_before_after_key(
                    index, before=item.comment, indent=key + layout.dash
                )
        return sequence


class YamlEntry(BaseModel, frozen=True):
    """One key of a mapping, with what a reader needs above it.

    ``comment`` is why a document is declared rather than dumped: a generated
    file is read, and the line explaining a choice belongs beside the choice.
    ``spaced`` opens a blank line before the entry, which is how a long
    mapping stays readable and the only other thing a plain dump discards.
    """

    key: str
    value: "YamlAny"
    comment: str = ""
    spaced: bool = False

    def prose(self) -> str:
        """What stands above this entry, as the block the emitter places there."""
        return f"{'\n' if self.spaced else ''}{self.comment}"


def scalars(pairs: Mapping[str, ScalarValue]) -> list[YamlEntry]:
    """One entry per named value, in the order given, skipping what is empty.

    Skipping is what a declared default with nothing to say needs: a step
    naming no working directory carries no such key, rather than one whose
    value is the empty string — which is a directory, and not the one meant.
    A value that is ``None`` or ``False`` is kept, being something said.
    """
    return [
        YamlEntry(key=key, value=YamlScalar(value=value))
        for key, value in pairs.items()
        if value != ""
    ]


class YamlMap(YamlNode, frozen=True):
    """A mapping written one key per line, in the order its entries are declared."""

    type: Literal["map"] = "map"
    entries: list[YamlEntry]

    @model_validator(mode="after")
    def keys_are_distinct(self) -> "YamlMap":
        keys = [entry.key for entry in self.entries]
        if len(keys) != len(dict.fromkeys(keys)):
            raise ValueError(f"mapping declares one key twice: {keys}")
        return self

    def plain(self) -> PlainData:
        return {entry.key: entry.value.plain() for entry in self.entries}

    def composed(self, layout: Layout, indent: int) -> EmittedValue:
        mapping = CommentedMap(
            (
                unambiguous(entry.key),
                entry.value.composed(layout, indent + layout.mapping),
            )
            for entry in self.entries
        )
        for entry in self.entries:
            if entry.prose():
                mapping.yaml_set_comment_before_after_key(
                    unambiguous(entry.key), before=entry.prose(), indent=indent
                )
        return mapping


type YamlAny = Annotated[
    YamlScalar | YamlFlow | YamlList | YamlMap, Discriminator("type")
]
"""Any node a generated document holds, parseable back from what it rendered."""


class YamlDocument(BaseModel, frozen=True):
    """A whole YAML file as the data it carries, emitted and then checked."""

    root: YamlMap
    layout: Layout = Layout()

    width: int = 1 << 20
    """Past any line a generated document holds, so nothing is wrapped.

    Folding a long command across lines is the emitter answering a question
    about a terminal that nothing here asked, and a reader grepping the file
    for the command it runs would not find it.
    """

    @model_validator(mode="after")
    def renders_what_it_holds(self) -> "YamlDocument":
        parsed = yaml.safe_load(self.text())
        if parsed != self.root.plain():
            raise ValueError(
                "yaml document does not parse back to what it declares: "
                f"{parsed!r} from {self.root.plain()!r}"
            )
        return self

    def text(self) -> str:
        """This document as YAML, newline-terminated."""
        emitter = YAML()
        emitter.indent(
            mapping=self.layout.mapping,
            sequence=self.layout.sequence,
            offset=self.layout.dash,
        )
        emitter.width = self.width
        stream = io.StringIO()
        emitter.dump(self.root.composed(self.layout, 0), stream)
        return stream.getvalue()
