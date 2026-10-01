"""Genuinely shared harness vocabulary: declarations and rendered artifacts.

The canonical declaration graph (``Harness`` down to prompt parts) that the
application declares and the adapter renderers, devtools generation flows, and
resolver consume, plus the rendered ``Artifact``/``ArtifactTree`` and probe
evidence shared by every pipeline stage. A model owned by one concern lives
beside its managing module instead (see the package docstring).
"""

import re  # lup: ignore[import-re] — prose has no parser; its shape is the rule
from abc import ABC, abstractmethod
from collections.abc import Sequence
from itertools import dropwhile
from json import dumps
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    Discriminator,
    Field,
    StringConstraints,
    model_validator,
)

from lup.harness.codescan.common import AntiPattern, RuleSelection
from lup.devtools.launcher import DEFAULT_ENVIRONMENT
from lup.formats.banner import ArtifactBanner, GeneratedBanner
from lup.harness.image import Image
from lup.harness.requirements import Manifest
from lup.formats.markdown import (
    ProseCell,
    ProseCode,
    CodeCell,
    MarkdownDocument,
    PlainCell,
    TableCell,
    escaped,
)
from lup.formats.toml import TomlDocument
from lup.harness.passages import passage_text, rendered
from lup.formats.yaml import PlainData, YamlDocument
from lup.tools.mcp import ToolDeclaration
from lup.policy.boundary import BoundaryCapability
from lup.policy.kernel.rows import AcceptanceGuardRow, PathRoleName, SpawnNameRow
from lup.policy.kernel.semantics import UnjudgedAmbient
from lup.policy.models import PolicyId, ProtectedRoot, UrlScope
from lup.policy.peer_policy import PeerPolicy
from lup.policy.refused_paths import (
    RefusedPaths,
    credential_files,
    secret_variable_names,
)
from lup.policy.refused_tools import RefusedTool
from lup.policy.edit_rules import EditRule
from lup.policy.imports import ImportBoundary
from lup.policy.everyday import CommandFamily
from lup.policy.shell_rules import RunnerTargetRule, ShellCommandRule
from lup.policy.vocabulary import default_vocabulary
from lup.seams import SelectableRule, Selection
from lup.types import JsonValue, ModelTier, SessionEffort, ToolGrant, ToolName

if TYPE_CHECKING:
    from lup.harness.contracts import NativeSpellings, PromptRenderer

type NativeName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")
]
"""A declaration name portable across adapters: lowercase alphanumerics with
interior hyphens or underscores."""

type QualifiedAgentName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*:[a-z0-9][a-z0-9_-]*$")
]
"""A delegation target, ``<plugin>:<agent>``, as a runtime addresses one."""

# lup: ignore[constant-declaration] — the characters the runtimes actually write,
# each of which proves its own sigil is one of these
INVOCATION_SIGILS = "/$"
"""Every character a runtime writes in front of a skill invocation.

Which words a runtime spells is the adapter's to know, but that an invocation
is a sigil followed by a qualified name is a shape prose can be held to on its
own — no plugin registry required. Each runtime proves its own sigil is one of
these, so the syntax the declaration layer refuses cannot drift from the syntax
the adapters render.
"""

RENDERED_INVOCATION = re.compile(  # lup: ignore[re-call] — a shape, not a parse
    f"[{INVOCATION_SIGILS}]"
    r"[a-z0-9][a-z0-9_-]*:(?:[a-z0-9][a-z0-9_-]*|\*|<[a-z][a-z0-9-]*>)"
)
"""What ``SkillInvocation`` and ``SkillPattern`` render to, in either sigil."""


def portable_prose(value: str) -> str:
    """Refuse text spelling an invocation only one runtime would understand."""
    spelled = RENDERED_INVOCATION.search(value)
    if spelled is None:
        return value
    raise ValueError(
        f"portable prose spells the invocation {spelled.group()!r}, which the "
        "other runtime's reader cannot use: issue one with SkillInvocation, or "
        "teach its shape with SkillPattern"
    )


type PortableText = Annotated[str, AfterValidator(portable_prose)]
"""Free text proven to spell no runtime's invocation syntax.

Every declaration field a native tree renders as prose is one of these, so the
invariant is answered where an author writes the words rather than by a scan
over the harness they eventually compose into."""


class SemanticPart(BaseModel, ABC, frozen=True):
    """One element of a prompt document, answering every question about itself.

    Whatever the rest of the harness needs to know about a part is declared
    here and answered — or declined — by the part, so a new kind of part is one
    class rather than an edit to every walk that would have to notice it. The
    declining answers are what make omission safe: a caller asking
    ``text_payload`` reaches every kind that carries prose, including kinds
    written long after the caller was.

    Pydantic's metaclass is an ``ABCMeta``, so ``spell`` binds like any
    abstract method: a subtype that does not answer it cannot be constructed.
    """

    @abstractmethod
    def spell(self, renderer: "PromptRenderer") -> str:
        """Render this part in the vocabulary of the runtime that reads it."""

    @property
    def text_payload(self) -> str | None:
        """Portable prose this part carries verbatim, if it carries any.

        Everything that weighs or reads what a declaration literally says asks
        this instead of naming the kinds of part that hold text.
        """
        return None

    @property
    def carried(self) -> list["PromptPart"]:
        """The parts this one holds inside itself, if it holds any.

        A passage names values, and a value is a part: an invocation it
        issues, a plugin path it spells, an argument reference it reaches.
        Every walk that asks a document's parts a question has to reach those
        too, or a skill's own invocation becomes invisible by being written
        inside a sentence rather than beside one.
        """
        return []

    @property
    def invocation(self) -> "SkillInvocation | None":
        """The skill invocation this part issues, if it issues one."""
        return None

    @property
    def named_plugin(self) -> NativeName | None:
        """The plugin this part names, which the harness must have declared."""
        return None

    @property
    def named_agent(self) -> QualifiedAgentName | None:
        """The agent this part delegates to, which must also be declared."""
        return None

    @property
    def references_arguments(self) -> bool:
        """Whether this part reaches the arguments its invocation supplied."""
        return False

    @property
    def shell_command(self) -> str | None:
        """The shell command this part tells its reader to run, if it names one.

        A part that names a command is asking the agent to run it, so the
        skill carrying it has to have granted the shell that takes. Asking
        the part is what lets that be checked where the two are declared,
        rather than discovered by an agent whose own instructions are denied.
        """
        return None

    def given(self, taken: list[str]) -> Self:
        """This part as a project that took *taken* modules reads it.

        Most parts are what they are whatever a project took, so this answers
        with the part itself; one holding parts that need a module the project
        declined answers without them. See :class:`WhereTaken`.
        """
        return self


class TextPart(SemanticPart, frozen=True):
    type: Literal["text"] = "text"
    text: PortableText

    def spell(self, renderer: "PromptRenderer") -> str:
        return self.text

    @property
    def text_payload(self) -> str:
        return self.text


class InlinePart(SemanticPart, frozen=True):
    """One derived value standing inside authored prose, escaped by its node.

    What a passage names is always a part, and a value that is not one of the
    parts already — a path, a count, a description read off a declaration —
    becomes this. The node it holds decides the formatting and does the
    escaping, which is the same node a table cell holds: a pipe cannot break
    a row, a backtick cannot close a span, whichever container it lands in.
    """

    type: Literal["inline"] = "inline"
    node: TableCell

    def spell(self, renderer: "PromptRenderer") -> str:
        return self.node.render()


def code(text: str | PurePath) -> InlinePart:
    """A derived value shown as code, which is most of what prose names.

    A path is taken as the path it is rather than as a string somebody
    spelled, because that is what the declarations carrying one hold.
    """
    return InlinePart(node=ProseCode(text=str(text)))


def plain(text: str | PurePath) -> InlinePart:
    """A derived value shown as it reads."""
    return InlinePart(node=ProseCell(text=str(text)))


def counted(total: int) -> InlinePart:
    """A number a document quotes, as the text a reader sees."""
    return InlinePart(node=ProseCell(text=str(total)))


class BulletItem(BaseModel, frozen=True):
    """One bullet of a derived list: what it names, and what it says of it.

    The lead is a node rather than a string because a roster shows what it
    names in whatever form the roster is about — a topic strong, an agent as
    the code its name is — and each of those is a node that already knows how
    to escape itself.
    """

    lead: TableCell
    text: str

    def render(self) -> str:
        """This bullet's line, both halves escaped where they enter it."""
        return f"- {self.lead.render()} — {ProseCell(text=self.text).render()}\n"


class BulletList(SemanticPart, frozen=True):
    """A list derived from declarations, one bullet per line.

    What a cell is for a value inside a sentence, this is for a roster: a
    list of topics, of skills, of whatever a declaration enumerates, laid out
    once here with every item escaped on the way in. An inline node cannot
    serve, holding one line by construction — right for a value in a
    sentence, wrong for a list that is several.
    """

    type: Literal["bullets"] = "bullets"
    items: list[BulletItem]

    def spell(self, renderer: "PromptRenderer") -> str:
        return self.text_payload

    @property
    def text_payload(self) -> str:
        return "".join(item.render() for item in self.items)


class Passage(SemanticPart, frozen=True):
    """Prose authored as Markdown beside its module, with its values placed.

    The parts around it stay parts: a table derived from declarations, an
    invocation each runtime spells its own way, an argument reference. What
    this carries is the words, which are prose and belong in a file that is
    prose — and the values those words name, each entering escaped because
    the type admits nothing that could enter otherwise.
    """

    type: Literal["passage"] = "passage"
    module: str = Field(min_length=1)
    """The module whose declaration this prose belongs to, spelled ``__name__``."""

    name: str = ""
    """Which passage beside that module, where it declares more than one."""

    values: dict[str, "PromptPart"] = {}
    """What the prose names, under the name it names it by.

    Parts rather than text, which is the guarantee: a part spells itself —
    escaped by its node, or in the vocabulary of the runtime reading it — so
    there is no way to name a value that arrives as somebody's unescaped
    string. An argument reference, a skill invocation and a path are all one
    kind of thing here, and the prose names each the same way.
    """

    def spell(self, renderer: "PromptRenderer") -> str:
        return rendered(
            self.module,
            self.name,
            {name: value.spell(renderer) for name, value in self.values.items()},
        )

    @property
    def carried(self) -> list["PromptPart"]:
        return [
            found for value in self.values.values() for found in [value, *value.carried]
        ]

    def given(self, taken: list[str]) -> Self:
        return self.model_copy(
            update={
                "values": {
                    name: value.given(taken) for name, value in self.values.items()
                }
            }
        )

    @property
    def text_payload(self) -> str:
        """The authored prose, which is what a reader of this document reads.

        The words rather than the rendering: the portability scan is about
        the prose somebody wrote, and what each value becomes is that value's
        own answer, checked where the value is declared.
        """
        return passage_text(self.module, self.name)


class SpellingExample(SemanticPart, frozen=True):
    """Prose whose subject is a runtime's own spelling, quoted verbatim.

    Ordinary prose refuses a rendered invocation because a reader on the other
    runtime cannot use one. A document *comparing* the runtimes has to quote
    both, so the exemption is declared here rather than left as a rule that
    quietly does not fire. It is deliberately narrow: the same words reach
    every tree, so this can only ever exhibit a spelling — never issue one,
    which is what :class:`SkillInvocation` is for.
    """

    type: Literal["spelling_example"] = "spelling_example"
    text: str

    def spell(self, renderer: "PromptRenderer") -> str:
        return self.text

    @property
    def text_payload(self) -> str:
        return self.text


class MarkdownTable(SemanticPart, frozen=True):
    """A table derived from declarations, laid out and escaped as it renders.

    Rows arrive as the values they stand for rather than as finished Markdown,
    so the escaping that keeps a pipe or a newline from breaking the row it
    lands in happens here — a caller composes a table into a document the way
    it composes any other part, and has no way to splice one in wrong. Every
    runtime reads the same Markdown, so this spells itself.

    Headers and cells hold data rather than authored prose — a rule's matching
    shape, a path a document already renders to — so they are not held to the
    portable-prose invariant a :class:`TextPart` answers for. What the table
    renders still reaches ``text_payload``, so a native spelling that arrived
    through a cell is caught where the assembled document is checked.
    """

    type: Literal["markdown_table"] = "markdown_table"
    headers: list[str]
    rows: list[list[TableCell]]

    @model_validator(mode="after")
    def rows_match_the_header(self) -> "MarkdownTable":
        ragged = [len(row) for row in self.rows if len(row) != len(self.headers)]
        if ragged:
            raise ValueError(
                f"table rows hold {ragged} cells under {len(self.headers)} headers"
            )
        return self

    def spell(self, renderer: "PromptRenderer") -> str:
        return self.text_payload

    @property
    def text_payload(self) -> str:
        """The table as Markdown, one line per row, newline-terminated."""
        lines = [
            [escaped(header) for header in self.headers],
            ["---"] * len(self.headers),
            *[[cell.render() for cell in row] for row in self.rows],
        ]
        return "".join(f"| {' | '.join(line)} |\n" for line in lines)


class ToolRoster(SemanticPart, frozen=True):
    """Agent-facing tool metadata rendered from registration declarations."""

    type: Literal["tool_roster"] = "tool_roster"
    tools: list[ToolDeclaration]

    def spell(self, renderer: "PromptRenderer") -> str:
        return self.text_payload

    @property
    def text_payload(self) -> str:
        return MarkdownTable(
            headers=["Tool", "Contract"],
            rows=[
                [CodeCell(text=tool.name), PlainCell(text=tool.description)]
                for tool in self.tools
            ],
        ).text_payload


class InvocationArgument(BaseModel, frozen=True):
    name: NativeName
    value: JsonValue


class SkillInvocation(SemanticPart, frozen=True):
    type: Literal["skill_invocation"] = "skill_invocation"
    plugin: NativeName
    skill: NativeName
    arguments: list[InvocationArgument] = []

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.render(self)

    @property
    def invocation(self) -> "SkillInvocation":
        return self

    @property
    def named_plugin(self) -> NativeName:
        return self.plugin


type TreeLocation = Literal[
    "tree_root",
    "guidance_file",
    "ownership_manifest",
    "project_settings",
    "personal_settings",
    "marketplace",
]
"""One harness-tree location every runtime spells for itself."""


type PluginLocation = Literal[
    "root",
    "manifest",
    "skills",
    "agents",
    "hooks",
    "guidance_template",
]
"""One location an installed plugin owns, or that a runtime keeps beside it."""


type PathScope = Literal["this_tree", "every_tree"]
"""Whether a path addresses the reader's own tree or teaches every tree."""


type PathMember = Annotated[
    str, StringConstraints(pattern=r"^(\*|<[a-z][a-z0-9-]*>|[a-z0-9][a-z0-9-]*)$")
]
"""One leaf inside a location: a name, a ``<placeholder>``, or ``*``."""


class LocatedPart(SemanticPart, ABC, frozen=True):
    """One path a prompt names, spelled by whichever adapter renders it.

    Scope is the same question for every location — whether the reader's own
    tree answers it or every tree must be taught at once — so the renderer asks
    it once and each kind only says how it spells itself in one vocabulary.
    """

    scope: PathScope = "this_tree"

    @abstractmethod
    def spell_in(self, runtime: "NativeSpellings") -> str:
        """Spell this location in one runtime's own vocabulary."""

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.location(self)


class NativePath(LocatedPart, frozen=True):
    """One harness-tree location, spelled by whichever adapter renders it."""

    type: Literal["native_path"] = "native_path"
    location: TreeLocation

    def spell_in(self, runtime: "NativeSpellings") -> str:
        return runtime.tree(self.location)


class PluginPath(LocatedPart, frozen=True):
    """One plugin-owned location, spelled by whichever adapter renders it.

    ``member`` selects a leaf whose whole path differs per runtime — a skill is
    one file under one runtime and a directory under another — while omitting
    it names the containing directory.
    """

    type: Literal["plugin_path"] = "plugin_path"
    plugin: NativeName
    location: PluginLocation
    member: PathMember | None = None

    def spell_in(self, runtime: "NativeSpellings") -> str:
        return runtime.plugin(self.plugin, self.location, self.member)

    @property
    def named_plugin(self) -> NativeName:
        return self.plugin


class SkillPattern(SemanticPart, frozen=True):
    """An invocation shape standing in for a skill the reader will name.

    ``SkillInvocation`` resolves against the declaration registry, so it cannot
    express the placeholder or wildcard a prompt uses when it teaches the shape
    of an invocation rather than issuing one.
    """

    type: Literal["skill_pattern"] = "skill_pattern"
    plugin: NativeName
    placeholder: PathMember

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.invocation_pattern(self.plugin, self.placeholder)

    @property
    def named_plugin(self) -> NativeName:
        return self.plugin


class RuntimeDocs(SemanticPart, frozen=True):
    """The reader's own runtime documentation, wherever that runtime is."""

    type: Literal["runtime_docs"] = "runtime_docs"

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.runtime_docs()


class AskUser(SemanticPart, frozen=True):
    type: Literal["ask_user"] = "ask_user"
    question: PortableText

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.ask_user(self.question)


class Delegate(SemanticPart, frozen=True):
    type: Literal["delegate"] = "delegate"
    subagent_type: QualifiedAgentName
    prompt: PortableText
    name: str = ""
    """What the spawned subagent is called, as its listing, a message and a
    stop address it; the task in two or three words. Empty leaves the runtime
    showing the type, which a project requiring names refuses."""

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.delegate(self.subagent_type, self.prompt, self.name)

    @property
    def named_agent(self) -> QualifiedAgentName:
        return self.subagent_type


class RequestApproval(SemanticPart, frozen=True):
    type: Literal["request_approval"] = "request_approval"
    action: PortableText
    reason: PortableText

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.request_approval(self.action, self.reason)


class RelocateSession(SemanticPart, frozen=True):
    """Continue work inside an already-created worktree.

    Runtimes differ on whether a running session can move: one relocates in
    place, another can only be replaced by a session started there. Naming
    the intent lets each adapter spell the move it actually supports -- and
    lets one that supports both spell the cheaper of them, which is why every
    adapter currently answers with a launch. Relocation is not free where it
    exists: it arms a runtime's own worktree isolation, whose refusals are
    about command shape rather than about anything this policy judges.
    """

    type: Literal["relocate_session"] = "relocate_session"
    path: PortableText
    """Where the reader finds the path, e.g. "the path step 1 prints"."""

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.relocate_session(self.path)


class WatchOutput(SemanticPart, frozen=True):
    """Wait on a command that reports progress before it exits.

    Runtimes differ in what waiting *is*: one pushes each line to the agent,
    another hands it a live session to read. Prose that named the idea
    without naming the mechanism left a reader to guess, and the guess is an
    ordinary command with a long timeout — which reports once, at the end.
    """

    type: Literal["watch_output"] = "watch_output"
    command: PortableText
    """The command to watch, as the reader will run it."""

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.watch_output(self.command)

    @property
    def shell_command(self) -> str:
        return self.command


class NestedRun(SemanticPart, frozen=True):
    """Run this runtime once, non-interactively, over a throwaway project.

    A probe kit measures what a hook receives by registering it in a
    directory's own settings and launching the runtime there. Each runtime
    spells that launch differently — its print mode, its approval switch —
    so prose naming "a non-interactive run" leaves a reader to guess the
    flags and stop at the first approval prompt nobody is there to answer.
    """

    type: Literal["nested_run"] = "nested_run"
    prompt: PortableText
    """The first prompt the run answers, as the reader will pass it."""

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.nested_run(self.prompt)


class CommandInvocation(SemanticPart, frozen=True):
    """One `lup-devtools` command, named by its path rather than spelled out.

    A document telling its reader to run something is issuing an instruction,
    and one naming a command that does not exist fails for whoever follows it.
    Twenty-two of those shipped at once — `dev hookssweep` in the hooks
    workflow's own step 7, `py info` without its `dev` group in the
    introspection tools' own docstring — each written beside the command it
    named, which is why nobody reading either noticed.

    Declared the way this document declares a plugin path or a skill it
    invokes: the part holds what it means and the harness spells it, so the
    executable's name is written once and the path is a value the walked CLI
    can be asked about. ``arguments`` is whatever follows, which is the
    reader's to fill in as often as not — placeholders belong there and are
    resolved against nothing.

    What this cannot reach is a docstring or a checked-in Markdown file, where
    most of those twenty-two lived. The sweep in
    :mod:`lup.devtools.dev.documented` covers those, and covers this too.
    """

    type: Literal["command_invocation"] = "command_invocation"
    path: list[str] = Field(min_length=1)
    """The command's own words, as the CLI mounts them: ``["dev", "check"]``."""

    arguments: PortableText = ""
    """Whatever follows the command — flags, operands, a placeholder."""

    def spelled(self) -> str:
        """This invocation as a reader types it, executable and all."""
        return " ".join(["uv run lup-devtools", *self.path, self.arguments]).strip()

    def spell(self, renderer: "PromptRenderer") -> str:
        """The same words for every runtime, which all reach the same shell."""
        return self.spelled()

    @property
    def text_payload(self) -> str:
        return self.spelled()

    @property
    def shell_command(self) -> str:
        return self.spelled()


class ResolverEntry(SemanticPart, frozen=True):
    type: Literal["resolver_entry"] = "resolver_entry"

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.resolver_entry()


class ArgumentsRef(SemanticPart, frozen=True):
    type: Literal["arguments_ref"] = "arguments_ref"

    def spell(self, renderer: "PromptRenderer") -> str:
        return renderer.own.arguments_ref()

    @property
    def references_arguments(self) -> bool:
        return True


class WhereTaken(SemanticPart, frozen=True):
    """Parts that exist only in a project that took one other module.

    A module's content may name what another module contributes only where it
    stands on that module: through ``requires``, or here. Adoption resolves
    each of these against the modules a project took — kept whole where the
    named one is there, emptied where it is not — so a sentence pointing at
    the resolver never reaches a project that declined it, and nothing
    downstream of adoption has to know a selection exists.

    Unresolved, it renders what it holds: content read without a selection is
    content read as if everything were taken, which is what a roster built
    for a listing or a census is.
    """

    type: Literal["where_taken"] = "where_taken"
    module: str = Field(min_length=1)
    """The module these parts need, by id."""

    parts: list["PromptPart"] = []
    """What renders where that module is taken, in reading order."""

    def spell(self, renderer: "PromptRenderer") -> str:
        return "".join(part.spell(renderer) for part in self.parts)

    @property
    def carried(self) -> list["PromptPart"]:
        return [found for part in self.parts for found in [part, *part.carried]]

    def given(self, taken: list[str]) -> Self:
        """Keep these parts where their module is taken, and none where it is not."""
        kept = [part.given(taken) for part in self.parts]
        return self.model_copy(update={"parts": kept if self.module in taken else []})


type PromptPart = Annotated[
    TextPart
    | Passage
    | InlinePart
    | BulletList
    | SpellingExample
    | MarkdownTable
    | ToolRoster
    | SkillInvocation
    | NativePath
    | PluginPath
    | SkillPattern
    | RuntimeDocs
    | AskUser
    | Delegate
    | RequestApproval
    | RelocateSession
    | WatchOutput
    | NestedRun
    | CommandInvocation
    | ResolverEntry
    | ArgumentsRef
    | WhereTaken,
    Discriminator("type"),
]


class PromptDocument(BaseModel, frozen=True):
    parts: list[PromptPart]
    source: str | None = None
    """The module declaring this document, and where a reader edits it.

    Every document reaching a file of its own carries one — a skill's or an
    agent's prompt included, whose artifact renders the frontmatter and the
    prose out of the single module declaring both. What names no source is a
    document composed into another rather than compiled: a shared section
    several skills fold in has no file of its own to point anybody at."""

    def declared_source(self) -> str:
        """The declaring module, required because this document becomes a file."""
        if self.source is None:
            raise ValueError("a document rendered to its own artifact needs a source")
        return self.source

    def walked(self) -> list[PromptPart]:
        """Every part this document holds, the ones inside a passage included.

        What a walk asks a part is asked of the values a passage names too: a
        skill invoked inside a sentence is invoked, and a plugin named there
        is named. Reading order, so a report listing what a document does
        lists it the way the document reads.
        """
        return [found for part in self.parts for found in [part, *part.carried]]

    def prose(self) -> list[str]:
        """Every literal prose payload this document carries, in reading order."""
        return [
            text for part in self.walked() if (text := part.text_payload) is not None
        ]

    def text_size(self) -> int:
        """Lower bound on what this document costs a session, in UTF-8 bytes.

        Every part renders to something, so the rendered document is never
        smaller. This is the share a neutral module can measure without
        reaching for an adapter to spell the rest.
        """
        return sum(document_byte_size(text) for text in self.prose())

    def given(self, taken: list[str]) -> "PromptDocument":
        """This document as a project that took *taken* modules reads it."""
        return self.model_copy(
            update={"parts": [part.given(taken) for part in self.parts]}
        )


class Document(SelectableRule, frozen=True):
    """One generated repository document and where it renders.

    Separate from the roster that lists them: which documents a project
    publishes is its own decision, but that each is a prompt document with a
    path, an identity, and a declaring module is what makes the roster
    renderable by machinery no project writes.

    Selectable under its semantic id, which is the name ownership records it
    under — so a project declining a page, and the manifest saying no file
    there is owned, answer to one string.
    """

    path: Path
    semantic_id: str
    source: str
    document: PromptDocument

    def selection_id(self) -> str:
        return self.semantic_id

    def given(self, taken: list[str]) -> "Document":
        """This page as a project that took *taken* modules reads it."""
        return self.model_copy(update={"document": self.document.given(taken)})


class GuidanceBudget(BaseModel, frozen=True):
    """What the always-loaded document may weigh, and what a scaffold keeps back.

    One declaration rather than two numbers, because the third value is the one
    every caller actually wants and neither number carries it: what a *template*
    may spend is the ceiling less the reserve, and that subtraction was written
    out by hand at every site that needed it. Derived here instead, so a project
    that moves either number moves the answer everywhere rather than moving it
    at seven sites and missing the eighth.

    Field defaults rather than module constants for the reason
    ``docs/patterns.md`` § *A Constant Should Probably Be An Overridable
    Default* gives: both are judgements a project may make differently, and
    ``ge=0`` is what the fields buy over integers — a negative reserve is not a
    stricter scaffold, it is a check that passes whatever the document weighs.
    """

    ceiling: int = Field(default=32_768, ge=0)
    """Ceiling, in UTF-8 bytes, on the always-loaded guidance document.

    Codex stops adding project documentation once the combined size reaches
    ``project_doc_max_bytes``, whose own default is 32 KiB — so exceeding this
    is not an error a reader ever sees, it is *silent truncation*. The unit is
    bytes for the same reason: that is what the vendor limits, and UTF-8
    punctuation makes a document's byte count exceed its character count, so a
    character-based check runs looser than the real cap and passes documents
    that would be cut.

    Claude has no equivalent setting — its guidance file is loaded in full
    whatever its length. Lup applies one number to both trees anyway, so the two
    runtimes read the same document rather than one reading a longer one.

    What a session pays for is the rendered document, so that is what the
    adapters check as they compile it. A typed part costs whatever its adapter
    spells it as, however little literal text the declaration holds. Reference
    material that a skill or a denial message surfaces at the right moment
    belongs in a generated document under ``docs/`` instead, reached by a
    file-path pointer."""

    template_headroom: int = Field(default=11_776, ge=0)
    """Bytes a scaffold holds back, out of the ceiling above, for its adopter.

    The ceiling is what a *runtime* will load. This is what a **template** may
    not spend of it, and the difference is the whole point: a repository that is
    still the scaffold is writing guidance every domain built on it inherits,
    and that domain then has to describe its own architecture, conventions and
    workflow inside whatever is left. A scaffold that fills the runtime's
    ceiling has not passed its budget on, it has spent it — and the adopter
    discovers this by writing three paragraphs about its own project and being
    refused.

    11.5 KiB, half a kilobyte under what this repository's own architecture,
    conventions and tooling sections cost together: a scaffold that also has to
    tell every domain how to answer its runtime's ambient instructions spends
    that much of the reserve on their behalf. Enough for a domain to say the
    equivalent about itself, rather than a round number that sounds generous.

    Only ``dev check`` weighs this, and only while ``[tool.lup] template =
    true``. It never reaches :attr:`ceiling`: that decides what a real runtime
    is told to load, and a scaffold's self-restraint is not a fact about any
    runtime's ceiling."""

    @property
    def scaffold_ceiling(self) -> int:
        """What a repository still shipping as a template may spend.

        The value every caller subtracted for itself. A derived property rather
        than a third field, because it is not a judgement anybody makes — it is
        what the two judgements above already imply, and a field would let a
        project set three numbers that disagree.
        """
        return self.ceiling - self.template_headroom

    @model_validator(mode="after")
    def reserve_fits_inside_the_ceiling(self) -> "GuidanceBudget":
        """A reserve larger than the ceiling leaves a scaffold negative room.

        Refused rather than clamped, because both readings of a clamp are
        wrong: silently keeping nothing back abandons the reserve a project
        asked for, and silently lowering the ceiling tells a runtime to load
        less than it will. The project stated two numbers that cannot both
        hold, and only the project can say which it meant.
        """
        if self.template_headroom > self.ceiling:
            raise ValueError(
                f"a scaffold reserve of {self.template_headroom} bytes does not "
                f"fit inside a {self.ceiling}-byte ceiling"
            )
        return self


GUIDANCE_BUDGET = GuidanceBudget()
"""The budget a project that has said nothing about it carries.

Named so every default argument reads the same value rather than constructing
its own, which is the one thing a bare default would get wrong here: a project
that replaced the budget would have to be threaded to each of them, and a site
that constructed its own would go on answering with the library's."""


def document_byte_size(text: str) -> int:
    """What a rendered document costs the runtime that loads it, in UTF-8 bytes."""
    return len(text.encode("utf-8"))


class Argument(BaseModel, frozen=True):
    name: NativeName
    description: PortableText = Field(min_length=1, max_length=1024)
    required: bool = False


class BashGrant(BaseModel, frozen=True):
    """What one declared ``Bash`` grant lets its holder run.

    A grant reaches a declaration as a constrained string, because that is the
    vocabulary the runtimes read it back in. The shape inside it still has to
    be understood to answer whether a command is covered, and reading it once
    here is what keeps each caller from re-deriving it with its own slicing.
    """

    prefixes: list[str] = []
    """Command prefixes admitted, empty when the grant admits every command."""

    def admits(self, command: str) -> bool:
        """Whether this grant lets its holder run ``command``."""
        if not self.prefixes:
            return True
        return any(command.startswith(prefix) for prefix in self.prefixes)

    @classmethod
    def read(cls, grant: ToolGrant) -> "BashGrant | None":
        """This grant as Bash coverage, or ``None`` when it grants another tool."""
        if grant == "Bash":
            return cls()
        if not grant.startswith("Bash(") or not grant.endswith(")"):
            return None
        scoped = grant.removeprefix("Bash(").removesuffix(")")
        # lup: ignore[string-split] — the parenthesized specifier is a runtime's
        # own grant syntax, which no stdlib parser reads
        specifiers = scoped.split(",")
        return cls(prefixes=[scope.strip().removesuffix(":*") for scope in specifiers])


class ProfileHint(BaseModel, frozen=True):
    """An argument hint naming the profiles one machine keeps, spelled on that machine.

    A skill acting on an account is invoked with a profile's name, and which
    names answer is a fact about the machine: a hint written into a committed
    tree would name one machine's accounts to every other, or none of them.
    So the verbs are declared, and the names are filled in where they are
    known.
    """

    alone: list[NativeName] = []
    """Verbs taken with no profile."""

    naming: list[NativeName] = []
    """Verbs taking one profile's name."""

    def spelled(self, profiles: Sequence[str]) -> str:
        """The hint on a machine keeping ``profiles``, a placeholder where it keeps none."""
        chosen = "|".join(profiles) if profiles else "name"
        verbs = [*self.alone, *(f"{verb} <{chosen}>" for verb in self.naming)]
        return f"[{' | '.join(verbs)}]"


class Skill(SelectableRule, frozen=True):
    id: str
    name: NativeName
    description: PortableText = Field(min_length=1, max_length=1024)
    arguments: list[Argument] = []
    tools: list[ToolGrant] = []
    argument_hint: PortableText | None = None
    machine_hint: ProfileHint | None = None
    """Where set, this skill is the machine's: rendered with this hint, naming
    the profiles the machine keeps, into a gitignored overlay every launch
    loads — never into the committed tree."""

    prompt: PromptDocument

    def selection_id(self) -> str:
        return self.id

    def given(self, taken: list[str]) -> "Skill":
        """This skill as a project that took *taken* modules reads it."""
        return self.model_copy(update={"prompt": self.prompt.given(taken)})

    @model_validator(mode="after")
    def one_hint(self) -> "Skill":
        """Refuse a fixed hint beside the machine's, which would never be shown."""
        if self.argument_hint is not None and self.machine_hint is not None:
            raise ValueError(
                f"skill {self.id!r} declares an argument_hint and a machine_hint; "
                "the machine's is the one shown, so drop argument_hint"
            )
        return self

    @model_validator(mode="after")
    def coherent_arguments(self) -> "Skill":
        names = [argument.name for argument in self.arguments]
        if len(names) != len(dict.fromkeys(names)):
            raise ValueError(f"skill {self.id!r} has duplicate argument names")
        past_the_required = dropwhile(
            lambda argument: argument.required, self.arguments
        )
        if any(argument.required for argument in past_the_required):
            raise ValueError(
                f"skill {self.id!r} has a required argument after an optional one"
            )
        references_arguments = any(
            part.references_arguments for part in self.prompt.walked()
        )
        if bool(self.arguments) != references_arguments:
            raise ValueError(
                f"skill {self.id!r} argument declarations and ArgumentsRef disagree"
            )
        return self

    @model_validator(mode="after")
    def commands_it_names_are_commands_it_may_run(self) -> "Skill":
        """A command this skill tells its reader to run must be one it granted.

        The two are declared side by side and nothing made them agree, so a
        skill could instruct a step its own `tools` list forbids — and did:
        one told the agent to watch a `dev check` while granting no shell at
        all. That failure surfaces as a denial mid-run, to an agent following
        instructions correctly, which is the worst place to learn it. An empty
        grant list restricts nothing and is left alone.
        """
        if not self.tools:
            return self
        granted = [
            read for grant in self.tools if (read := BashGrant.read(grant)) is not None
        ]
        for part in self.prompt.walked():
            command = part.shell_command
            if command is None:
                continue
            if not any(grant.admits(command) for grant in granted):
                raise ValueError(
                    f"skill {self.id!r} names the command {command!r} but grants "
                    "no Bash that runs it: add the grant, or stop naming it"
                )
        return self


type AgentColor = Literal[
    "red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"
]
"""The closed agent accent-color palette native runtimes accept."""


class Agent(SelectableRule, frozen=True):
    id: str
    name: NativeName
    description: PortableText = Field(min_length=1, max_length=1024)
    prompt: PromptDocument
    tools: list[ToolName] = []
    model: ModelTier | None = "strongest"
    """The tier this agent runs on; ``None`` leaves its metadata unsaid."""

    color: AgentColor | None = None

    def selection_id(self) -> str:
        return self.id

    def given(self, taken: list[str]) -> "Agent":
        """This agent as a project that took *taken* modules reads it."""
        return self.model_copy(update={"prompt": self.prompt.given(taken)})


class ContentSelection(BaseModel, frozen=True):
    """Which of the skills and agents a library ships a project's plugin carries.

    Subtractive first and additive second, which is :class:`~lup.seams.Selection`
    read over two lists rather than one. A project declining two should name
    those two, because a restated roster is re-copied on every addition and the
    copy that fell behind looks like a decision — and a project that wants one
    skill's prose to read differently declares a skill under that id, which
    *replaces* the library's in place instead of sitting beside it.

    Skills and agents share one ``retired`` because they share one namespace of
    stable ids: a project retiring a skill that a retired agent existed to serve
    should not have to say so in two places. They do not share one override
    list, because a roster is a pair everywhere else and folding them into a
    discriminated union would buy one field at the price of taking the pair
    apart again at every reader.
    """

    retired: list[str] = []
    """Declaration ids this project's plugin does not ship."""

    skills: list[Skill] = []
    """Skills this project declares — replacing a library skill of the same id."""

    agents: list[Agent] = []
    """Agents this project declares — replacing a library agent of the same id."""

    def keeps(self, declaration_id: str) -> bool:
        """Whether a declaration is live here, for a roster composing itself."""
        return declaration_id not in self.retired

    def over_skills(self) -> Selection[Skill]:
        """This selection as the skill half of the algebra resolves it."""
        return Selection(retired=self.retired, overrides=self.skills)

    def over_agents(self) -> Selection[Agent]:
        """This selection as the agent half of the algebra resolves it."""
        return Selection(retired=self.retired, overrides=self.agents)

    def declared(self) -> list[str]:
        """Every id this project declares itself, whether replacing or adding."""
        return [declaration.id for declaration in [*self.skills, *self.agents]]


type GuidanceChapter = Literal[
    "orientation",
    "gates",
    "workflow",
    "code",
    "tooling",
    "process",
    "meta",
]
"""Where in the always-loaded document one section belongs.

The document has a spine — orient the reader, tell them what will stop them,
the change loop, how to write code, what to run, how to report, and the loop
over all of it — and that spine crosses ownership constantly: what to run is
three subjects in a row. Naming the chapter is what lets a subject place its
own prose without reading the whole document or asking anyone to sequence it.
"""


def default_chapters() -> list[GuidanceChapter]:
    """The spine in reading order — the batteries-included sequence.

    Offered rather than imposed: which part of a document an agent should meet
    first is a judgement, and a project whose reader needs its domain before
    its gates is answering a question this library had no standing to close.
    A project with no opinion composes this; one that has an opinion passes
    its own order to the composition rather than editing this call.
    """
    return [
        "orientation",
        "gates",
        "workflow",
        "code",
        "tooling",
        "process",
        "meta",
    ]


class GuidanceSection(SelectableRule, frozen=True):
    """One identified stretch of the always-loaded document.

    Named, so a project can retire one, replace one, or add its own beside
    them through the same algebra every other table here resolves through, and
    so a report can attribute bytes to something a reader can act on.

    The parts are a rendered stretch rather than a heading and a body, because
    that is what a section is: several open no heading at all — a pointer
    paragraph folded under the heading above — and requiring one would put a
    heading in the document that nobody wrote.
    """

    id: str
    """The name this section is retired, replaced, or reported under."""

    chapter: GuidanceChapter
    """Which part of the spine it belongs to, which is where it renders."""

    parts: list[PromptPart]
    """What it contributes, in the order it contributes it."""

    def selection_id(self) -> str:
        return self.id

    def given(self, taken: list[str]) -> "GuidanceSection":
        """This section as a project that took *taken* modules reads it."""
        return self.model_copy(
            update={"parts": [part.given(taken) for part in self.parts]}
        )

    @property
    def text(self) -> str:
        """The portable prose this section carries, for a weigher or a search."""
        return "".join(
            payload for part in self.parts if (payload := part.text_payload) is not None
        )


def sectioned(sections: list[GuidanceSection]) -> list[PromptPart]:
    """One document's parts, in the order its sections were resolved.

    The flattening is here rather than at each composition root so that a
    reader wanting the sections and a reader wanting the parts take the same
    list through one function, instead of one of them re-deriving what the
    other already had.

    How many newlines the document ends on is the renderer's, because that is
    the one reader that sees a whole document whatever kinds of part composed
    it. Trimming the last part here can only reach a part that carries its
    text in the declaration, and a section whose words are a passage carries
    none.
    """
    return [part for section in sections for part in section.parts]


class ContentRoster(BaseModel, frozen=True):
    """The skills and agents one plugin ships, as the pair every reader wants.

    A pair rather than two lists because every consumer takes both — the
    compiled plugin, the roster documents, the drift check — and a project
    composing them separately is two places for the same decision to be made
    differently.
    """

    skills: list[Skill] = []
    agents: list[Agent] = []

    def selected(self, selection: ContentSelection) -> "ContentRoster":
        """This roster as one project resolved it: retired, replaced, extended.

        Narrowing the roster rather than filtering at each surface is what
        keeps a retired declaration from reaching any of them: it is not
        compiled, not rendered into the documents that say what the plugin
        ships, and not named by prose describing a skill nobody can invoke.

        One call rather than a narrowing followed by an extension, because the
        two were never independent: a project replacing a skill had to retire
        the id and re-add the declaration, and forgetting the retirement left
        two declarations answering to one name. The algebra resolves that by
        id in one pass, so the second step cannot be the one that was skipped.
        """
        return ContentRoster(
            skills=selection.over_skills().over(self.skills),
            agents=selection.over_agents().over(self.agents),
        )

    def given(self, taken: list[str]) -> "ContentRoster":
        """Every declaration here as a project that took *taken* modules reads it."""
        return ContentRoster(
            skills=[skill.given(taken) for skill in self.skills],
            agents=[agent.given(taken) for agent in self.agents],
        )


class HookPathRole(BaseModel, frozen=True):
    """One repository root and the purpose the tree beneath it serves.

    Production is the default and needs no declaration: it is what the
    conventions are written about. A ``test`` root is judged by whether it
    exercises production rather than by production's own shape, and a
    ``scratch`` root holds files that are disposable by construction, so the
    verbs that ask before destroying something have nothing to protect there.

    ``root`` is a pattern, which says how far the declaration reaches. A bare
    root is anchored at the repository top, which is what a laid-out tree
    means; a directory that is what it is wherever it sits — a scratch
    directory beside whichever package opened it — is named ``**/<name>``.
    """

    root: Path
    role: PathRoleName


class AcceptanceGuard(BaseModel, frozen=True):
    """A project's decision to hold the tests its work is measured against.

    Declaring one turns every ``test``-role root into a gate: an ordinary
    session is asked before it edits a test, and a session declared
    autonomous — the resolver worker, the implementer — is refused outright.
    Undeclared, tests are judged by the ordinary lattice, which is what a
    project that does not work against fixed acceptance tests wants.

    Both reasons default to wording that says what to do instead, because a
    refusal an agent cannot act on becomes a retry. A project with its own
    workflow to name replaces them; a project without one should not have to
    invent them to turn the gate on.
    """

    ask_reason: str = (
        "editing a test changes what the implementation is measured against —"
        " approve this only if the test encodes the wrong behaviour, and fix"
        " the implementation otherwise"
    )
    autonomous_reason: str = (
        "this session implements against these tests, so they are its"
        " specification rather than its material — report what the test"
        " demands and why it cannot be met, and leave the change to whoever"
        " can weigh it"
    )

    def erased(self) -> AcceptanceGuardRow:
        """This guard as the kernel reads it, primitive and dependency-free.

        The single projection to the runtime shape, so the live policy and
        the hermetic runtime compiled into each plugin cannot come to hold
        different wording of the same refusal.
        """
        return AcceptanceGuardRow(
            ask_reason=self.ask_reason, autonomous_reason=self.autonomous_reason
        )


class SpawnNames(BaseModel, frozen=True):
    """A project's decision that every subagent it spawns goes out named.

    A runtime lists, addresses and stops a subagent by the name it was
    spawned with, and shows its type where none was given — a generic word
    such as the default agent's, which says nothing about what the subagent
    is doing. Declaring this sends every spawn out under a name of the shape
    below: one given in that shape goes as given, one outside it is
    normalized, and a spawn given none takes one read out of its description.

    The caller is not asked for it, because the schema it reads may not list
    the argument. Claude Code 2.1.280 and 2.1.283 show the model an `Agent`
    schema with no `name`, `additionalProperties` false, and take a `name`
    all the same; a session refused with "pass a name beside the agent type"
    put it in `description` twice before trying the key the schema did not
    list, and sessions refused that way were the commonest spawn friction.
    The description is the argument every spawn there carries, so the name
    is read out of it and handed back as a rewrite of the call — measured on
    2.1.283, where the runtime recorded the rewritten spawn under that name.

    The spelling is settled here rather than left to the runtime, because
    leaving it was measured to fail quietly. Claude Code 2.1.278 validates it
    and says so — "name must start with a letter or digit and contain only
    letters, digits, underscores, or hyphens (max 64 chars)", read out of the
    shipped binary. Codex 0.155.1 rejects a hyphen with no hook record at all:
    a spawn named `pty-arming-probe` produced no `PostToolUse`, and the model
    retried as `pty_arming_probe` unprompted, having learned the shape by
    guessing. So the default is the intersection, which is also what a name
    written into portable guidance needs: a project running on one runtime
    alone may widen `punctuation` to what that runtime takes.

    Only a spawn with nothing to read a name from is refused, and then
    ``recovery`` states the shape alone while the kernel opens it with the
    key the dispatcher read, `name` on Claude Code and `task_name` on Codex.

    On by default, since it costs the caller nothing and every listing,
    message and stop then names the work rather than the type.
    """

    reason: str = (
        "a subagent spawned without a name is listed, addressed and stopped"
        " by its type alone, which says nothing about what it is doing"
    )
    recovery: str = (
        "the task in two or three words, starting with a letter or digit and"
        " carrying only letters, digits and underscores, at most 64 characters"
        " — it is what the listing shows and what a message or a stop addresses"
    )
    """The shape of a name, and nothing about where it goes: the key a runtime
    reads it from is that runtime's, and the kernel opens the recovery with the
    one the dispatcher read, so the sentence a caller meets names the argument
    whether or not the tool schema they were shown did."""
    punctuation: str = "_"
    """What a name may carry beside letters and digits: the intersection of
    what every runtime this project runs on accepts, a hyphen being one
    runtime's alone. The first mark is what a normalized name joins its
    words with."""
    limit: int = 64
    """The longest name accepted, which is the shorter of the two limits."""

    def erased(self) -> SpawnNameRow:
        """This declaration as the kernel reads it, primitive and dependency-free."""
        return SpawnNameRow(
            reason=self.reason,
            recovery=self.recovery,
            punctuation=self.punctuation,
            limit=self.limit,
        )


class SubagentCleanup(BaseModel, frozen=True):
    """A project's decision that a subagent hands back its report only after its background work stops.

    Declaring one registers the fold under the runtime's subagent events: as a
    subagent starts it is told that what it arms in the background is its own
    to stop, and at the stop that hands its report back, while any shell work
    its own run started is still listed, that stop is refused once with a
    reason naming each task and the call that ends it. A stop that only waits
    on that work goes through, and a subagent it started is never named.
    Undeclared, a subagent's report goes through with its watches running,
    and each line they emit resumes it — the leak this exists to close.

    On by default, because every project delegating to subagents that wait on
    pushed output meets the same leak; the main agent is never gated, since
    its own stop fires with background subagents listed and that wait is
    wanted.
    """

    notice_at_start: bool = True
    """Whether the subagent is told at its start; the stop-time refusal is the
    declaration itself."""

    scoped: str = "`uv run lup-devtools dev check --changed`"
    tests: str = "`uv run lup-devtools dev test`"
    """How this project spells the scoped check and the runner a delegated
    agent points at the tests its change reaches.

    Declared rather than written into the notice, because the notice ships
    into a project that named its own devtools CLI and would otherwise read
    an agent an invocation it does not serve. The defaults are this
    repository's, which is what an adopter inherits until it says otherwise —
    the same arrangement every other spelling in this declaration has."""


class SessionNaming(BaseModel, frozen=True):
    """A project's decision that a session is named for its work, at its first prompt.

    A session is called after its worktree until something renames it, so
    every session opened in one checkout answers to the same word with a
    number on it — and so does the runtime chrome that agrees with the
    roster. Declaring this registers a hook under the runtime's prompt event:
    at the first prompt that says what the work is, a model is asked for a
    short name for it in a process of its own, the roster is renamed to the
    answer, and the runtime's own name for the session follows. Whatever the
    roster is renamed to later reaches the runtime the same way, once; a
    title somebody sets in the runtime wins over the answer, and the roster
    takes it up.

    Registered only where a roster is declared, since a name is what the
    roster addresses. On by default: the cost is one model call early in a
    session, which no prompt waits on, and the gain is every listing,
    message and resume naming the work rather than the checkout.
    """

    tier: ModelTier = "strongest"
    reason: str = ""
    """Why a tier below the strongest was named, which every such role states."""

    effort: SessionEffort = "low"
    """How hard the naming model thinks: the lowest rung, since the answer is
    one line — a label read off a request, with nothing to work out first."""

    instruction: str = (
        "You name a coding session after the work described by the request "
        "between <request> markers, which opened the session. Do not carry "
        "out the request. Answer with a name of two to four lowercase words "
        "joined by hyphens, specific to the work — what a colleague would call "
        "the task, not a restatement of the request — or null when the request "
        "does not say what the work is."
    )
    """What the naming model is told; the prompt, quoted, is its whole input."""

    attempts: int = Field(default=3, ge=1)
    """How many prompts are asked before the default name is left standing."""

    deadline_seconds: float = Field(default=30.0, gt=0)
    """How long one ask may take before it is killed, and another may start."""

    longest: int = Field(default=48, ge=8)
    """The longest name taken; a longer answer names nothing."""

    @model_validator(mode="after")
    def a_hook_has_no_model_to_inherit(self) -> "SessionNaming":
        """Refuse ``inherit``: a hook asks outside any session whose model it could take."""
        if self.tier == "inherit":
            raise ValueError(
                "session naming asks outside any session, so it has no model to "
                "inherit; name a tier"
            )
        return self

    @model_validator(mode="after")
    def a_lesser_tier_says_why(self) -> "SessionNaming":
        """Refuse a tier below the strongest named without the reason it was chosen."""
        if self.tier in ("balanced", "fast") and not self.reason.strip():
            raise ValueError(
                f"session naming names the {self.tier!r} tier without a reason; "
                "say why it asks below the strongest, or leave the tier unset"
            )
        return self


class HookSandbox(BaseModel, frozen=True):
    """OS sandbox declaration compiled into native settings and launchers.

    Fetch-scope hostnames join extra_domains as the network allowlist,
    human-owned files become OS-level write denials, and writable_paths become
    the grants that let a sandboxed toolchain reach its caches, so one
    declaration feeds both the semantic policy and the kernel-enforced
    boundary. excluded_commands travels the same pair in the other direction:
    it widens the settings and narrows what the policy will hand to the OS,
    because work the boundary never confined cannot be deferred to it.

    That makes allowed_fetch the home for any origin an agent should be able
    to read: declaring it there grants both the fetch and the egress. Reserve
    extra_domains for hosts that need egress but are not readable sources —
    an authenticated API a library calls, never a document the agent opens.
    Listing a readable origin here instead is what lets the two boundaries
    disagree, with the OS admitting a host the fetch policy still asks about.
    """

    extra_domains: list[str] = []
    excluded_commands: list[str] = Field(
        default=[],
        description=(
            "Commands the OS boundary does not confine at all. This is the "
            "only per-command lever a sandbox offers: it takes the command "
            "out of isolation rather than lifting one rule, so it is what a "
            "requirement no path or domain can express has to be stated as — "
            "a daemon socket the isolation blocks outright, or egress over a "
            "protocol an HTTP proxy cannot carry. Each entry is a command "
            "prefix written with a trailing ``*``, matching that word run "
            "alone or followed by arguments."
        ),
    )
    writable_paths: list[str] = Field(
        default=[],
        description=(
            "Paths outside the workspace a sandboxed toolchain must write. "
            "A tool that cannot reach its cache fails only when the cache is "
            "cold, so an undeclared path reads as an intermittent fault rather "
            "than a boundary; declaring it here states the requirement where "
            "the rest of the boundary is stated."
        ),
    )


class CarrierPins(BaseModel, frozen=True):
    """What a prompt-time fold needs to ask whether the carriers still agree.

    Two words rather than the whole scaffold declaration, because a fold
    shipped into a plugin runs on a bare interpreter and can import neither
    the declaration nor the reader that understands it. Rendered into the
    guard that starts it, and derived from the declaration that owns them, so
    a project that renamed its scaffold branch gets a hook naming the branch
    it has.
    """

    branch: str
    """Where the copied half is merged from, whose merge base carries the commit."""

    distribution: str
    """The package whose pin `uv.lock` records the other commit for."""


class HookSet(BaseModel, frozen=True):
    id: str
    policy_ids: list[PolicyId]
    allowed_fetch: list[UrlScope] = []
    denied_fetch: list[UrlScope] = []
    protected_edit_roots: list[ProtectedRoot | Path] = Field(
        default=[],
        description=(
            "Trees an edit needs approval into: a bare path, or a ProtectedRoot "
            "naming in plain words what the tree is, which a reviewer reads "
            "beside each file that met it"
        ),
    )
    import_boundaries: list[ImportBoundary] = []
    path_roles: list[HookPathRole] = Field(
        default=[],
        description=(
            "What each repository root is for. The lattice judges an action by "
            "what it does; a role supplies what the thing acted upon is for, "
            "which is what decides how much of the lattice applies"
        ),
    )
    human_owned_files: list[Path] = Field(
        default=[],
        description=(
            "Files whose content the human author owns; every edit is surfaced "
            "as Ask so agents propose changes instead of applying them"
        ),
    )
    acceptance_guard: AcceptanceGuard | None = Field(
        default=None,
        description=(
            "Whether every test-role root is held still: an ordinary session "
            "is asked before editing a test and an autonomous one is refused. "
            "None declares no guard, which judges tests by the ordinary "
            "lattice — right for a project that does not implement against "
            "fixed acceptance tests, and wrong for one that runs the resolver"
        ),
    )
    shell_rules: Selection[ShellCommandRule] = Field(
        default=Selection[ShellCommandRule](),
        description=(
            "How this project differs from the shell vocabulary the library "
            "ships — a downstream toolchain to add, a command it judges "
            "differently, one it drops. An empty selection is "
            "`default_vocabulary()` unchanged, so a project declares `lake` "
            "without restating `ls`, `grep` and `git` around it"
        ),
    )
    edit_rules: Selection[EditRule] = Field(
        default=Selection[EditRule](),
        description=(
            "How this project moves the edit gates the kernel decides on its "
            "own — which whole-file writes it reviews at the hook, how much "
            "counts as a small change, and for which files. An empty "
            "selection decides exactly what the kernel decides unaided, so a "
            "project states only its differences and every gate it says "
            "nothing about keeps the library's answer"
        ),
    )
    diagnostics_command: list[str] = Field(
        default=[],
        description=(
            "How to type-check one edited file, run from the checkout that "
            "holds it with the file appended. Empty declares no checker, and "
            "reports nothing rather than guessing at one"
        ),
    )
    resolution_command: list[str] = Field(
        default=[],
        description=(
            "How to resolve one edited file's receivers, run from the checkout "
            "that holds it with the proposed text on stdin and the file named "
            "by --path. Empty declares no resolver, and a rule whose verdict "
            "turns on a declaration then asks rather than refusing"
        ),
    )
    repair_command: list[str] = Field(
        default=[],
        description=(
            "How to take the dead suppression directives out of one written "
            "file, run from the checkout that holds it with the file named by "
            "--path and its report on stdout as JSON. Empty declares no "
            "repair, and a directive that silences nothing is left standing "
            "for the audit to report instead"
        ),
    )
    refused_tools: list[RefusedTool] = Field(
        default=[],
        description=(
            "Native calls this project has decided against outright, each "
            "carrying the surface to reach for instead. Whether a tool is "
            "against the point of a project is that project's judgement, so "
            "an empty list — the library's own answer — refuses nothing"
        ),
    )
    refused_paths: list[RefusedPaths] = Field(
        default=[credential_files()],
        description=(
            "Paths no word of any shell command may name, each carrying what "
            "to do instead: whichever verb would have reached one — a read, a "
            "copy, an archive, a connection — is refused by the name. The "
            "library's answer is the key and login files a machine keeps; a "
            "project replacing it states the whole set, and adds its own "
            "runtimes' logins with `credential_files(also=...)`"
        ),
    )
    secret_variables: list[str] = Field(
        default=secret_variable_names(),
        description=(
            "Name patterns of the variables whose values no command may print "
            "into the transcript: `printenv NAME`, `echo $NAME`, a printf or a "
            "here-string carrying one. Matched against the whole name without "
            "case"
        ),
    )
    carriers: CarrierPins | None = Field(
        default=None,
        description=(
            "Which branch this project's copied half is merged from and which "
            "distribution its library pin resolves, so a session is told at "
            "prompt time when the two stand at different upstream commits. "
            "None is a project that took no copied half from anywhere — the "
            "scaffold itself included, being the origin of every copy — and "
            "registers no hook at all"
        ),
    )
    spawn_names: SpawnNames | None = Field(
        default=SpawnNames(),
        description=(
            "Whether every subagent this project spawns has to carry a name: "
            "a spawn without one is refused with the shape a name takes. None "
            "declines, and leaves a nameless subagent listed by its type"
        ),
    )
    subagent_cleanup: SubagentCleanup | None = Field(
        default=SubagentCleanup(),
        description=(
            "Whether a subagent's report waits for the background work it "
            "started: told at its start that what it arms is its own to stop, "
            "and refused once at the stop that hands its report back while "
            "any of it is still listed. None declines, and leaves a "
            "subagent's leftovers to whoever notices them"
        ),
    )
    session_naming: SessionNaming | None = Field(
        default=SessionNaming(),
        description=(
            "Whether a session is named for its work at its first prompt: a "
            "model is asked for a short name without holding the prompt, the "
            "roster is renamed to it, and the runtime's own name for the "
            "session follows, as it follows every later rename of the roster; "
            "a title somebody sets in the runtime wins, and the roster takes it "
            "up. Registered only where a roster is declared. None declines, and "
            "leaves every session called after its worktree until somebody "
            "renames it"
        ),
    )
    peer_policy: PeerPolicy | None = Field(
        default=None,
        description=(
            "Where this project's sessions find each other, so a native call "
            "reaching one is judged against the roster it would bypass. None "
            "is a project whose sessions do not coordinate, and leaves every "
            "such call entirely to the runtime's own permissions. "
            "`lup.coordination.policy.peer_policy` builds one from the "
            "store's own layout, so the directory is spelled in the module "
            "that owns it rather than again in a compiled hook. Declaring one "
            "also registers the hook that pushes the roster's changes to a "
            "session at each prompt, since a roster nobody declared has "
            "nothing to push"
        ),
    )
    runner_targets: list[RunnerTargetRule] = Field(
        default=[],
        description=(
            "Which bare targets `uv run <target>` may reach without a question, "
            "and where each has to run. A project's own toolchain, so the "
            "library holds no opinion: an "
            "empty list judges every runner invocation by the ordinary shell "
            "vocabulary instead"
        ),
    )
    everyday_commands: list[CommandFamily] = Field(
        default=[],
        description=(
            "Commands an ordinary session runs here, which this policy must "
            "keep allowing. Swept by `dev check` against the declared table, "
            "so a rule that tightens something it did not mean to fails there "
            "rather than in somebody's session. Read nowhere else and "
            "compiled into nothing: it is a claim about the table, not a row "
            "in it. `lup.policy.everyday.everyday_commands` offers the "
            "families every project shares; an empty list asserts nothing, "
            "which leaves the only measurement of this vocabulary the one "
            "direction the recorded asks already read"
        ),
    )
    rules: RuleSelection = Field(
        default=RuleSelection(),
        description=(
            "Which of the scan rules the library ships this project holds "
            "itself to, named subtractively so a project states the few it "
            "retired rather than restating the many it keeps. One selection "
            "reaches the edit hook compiled from this set, the repository "
            "sweep, and the generated rule reference, so none of the three "
            "can enforce a rule the others stopped enforcing"
        ),
    )
    anti_patterns: list[AntiPattern] = Field(
        default=[],
        description=(
            "Code shapes only this project refuses, added to the tables the "
            "library ships. Declare one here the way shell_rules declares a "
            "downstream toolchain: the library settles what every project "
            "wants and holds no opinion on the rest, so a repository that "
            "named a defect of its own enforces it instead of waiting to be "
            "adopted. Additive, because `rules` already says what a project "
            "drops — between them a project states only where it differs"
        ),
    )
    recoverable_target_limit: int = Field(
        default=20,
        ge=0,
        description=(
            "How many committed, unmodified files one command may destroy "
            "without asking. Git restores each of them, but restoring is a "
            "repair somebody has to know to perform, so past this count a "
            "delete reads as a sweep and is worth a question. The line sits "
            "where an ordinary refactor stops and a sweep starts: deleting a "
            "package's worth of tracked, unmodified files is a normal edit "
            "whose worst case is one checkout, and pricing it at an approval "
            "buys a prompt that teaches nobody anything"
        ),
    )
    policy_timeout: int = Field(
        default=30,
        ge=10,
        description=(
            "Seconds each runtime gives the policy hook before it lets the call "
            "through unjudged: both runtimes treat a hook that overran as one "
            "that said nothing. Declared once and read twice — by the hooks "
            "file each runtime reads, and by the deadline every wait inside the "
            "hook shares, which ends early enough to answer inside it"
        ),
    )
    sandbox: HookSandbox | None = None
    unjudged_ambient: UnjudgedAmbient = Field(
        default="ask",
        description=(
            "What this project does with a legible operation nothing judged, "
            "with no containment beneath it. `ask` keeps unjudged work visible "
            "and is the default; `defer` hands the long tail to the runtime's "
            "own mode for a seamless posture. Read only where a session is "
            "uncontained, because a contained one settles the same operation a "
            "row earlier -- its effects are confined, so nothing is left here "
            "to answer"
        ),
    )
    unscoped_fetch: UnjudgedAmbient | None = Field(
        default=None,
        description=(
            "What a fetch outside every declared scope answers, whichever "
            "route reaches it: a web fetch, `curl` or `wget`. `defer` hands "
            "the origin to the runtime's own permission system; `ask` puts "
            "it to a reviewer. Unset, it follows `unjudged_ambient`. Its own "
            "declaration because reading an unlisted origin is not work the "
            "vocabulary forgot, and a project may hand one to the runtime "
            "while keeping unjudged shell work visible"
        ),
    )
    boundary_capabilities: list[BoundaryCapability] = Field(
        default=[],
        description=(
            "The runtime guarantees this project's sessions depend on, each "
            "naming the manifest handle that measures it. Declared rather than "
            "inferred for the reason a capability always is: a profile depends "
            "on what it says it depends on, and making every capability "
            "implicitly required would turn adding one to the vocabulary into "
            "a change that fails every existing project's launch"
        ),
    )

    @model_validator(mode="after")
    def a_toolchain_is_named_rather_than_located(self) -> "HookSet":
        """Refuse a declared program that spells the environment holding it.

        Resolution already asks the checkout where its environment is, and so
        answers for the ones no path can reach: a redirected
        ``UV_PROJECT_ENVIRONMENT``, a conda or pyenv install found on
        ``PATH``. Spelling that directory here re-derives what resolution
        derives, and gets it wrong for every layout but the one spelled —
        where the program resolves to nothing, the gate reports no findings,
        and silence is exactly what a clean file looks like. Nothing
        downstream is placed to say otherwise, which is why this is refused
        at construction rather than reported later.

        A path stays declarable, for a program a project genuinely vendors at
        a fixed place of its own. Naming *the environment* is what is
        refused, because that question already has a better answer.
        """
        for field, command in (
            ("diagnostics_command", self.diagnostics_command),
            ("resolution_command", self.resolution_command),
            ("repair_command", self.repair_command),
        ):
            program = PurePosixPath(command[0] if command else "")
            if DEFAULT_ENVIRONMENT in program.parts:
                raise ValueError(
                    f"{field} spells {DEFAULT_ENVIRONMENT!r} in {command[0]!r}; "
                    f"declare {program.name!r} and let the checkout answer "
                    "where its environment is"
                )
        return self

    def excluded_commands(self) -> list[str]:
        """Commands no OS boundary confines, declared sandbox or not.

        Undeclared reads the same as declared-with-nothing-excluded here,
        which is what lets every compiled dispatcher take the answer without
        first asking whether a sandbox exists to have an opinion.
        """
        return list(self.sandbox.excluded_commands) if self.sandbox else []

    def resolved_unscoped_fetch(self) -> UnjudgedAmbient:
        """What an origin no fetch scope names answers in this project.

        The fetch declaration where one was made, the unjudged posture where
        none was: one answer for `WebFetch`, `curl` and `wget` alike, asked
        here so the canonical policy composes no second reading of it.
        """
        return self.unscoped_fetch or self.unjudged_ambient

    def protected_roots(self) -> list[ProtectedRoot]:
        """Every declared protected root, a bare path read as one with no description."""
        return [
            root if isinstance(root, ProtectedRoot) else ProtectedRoot(path=root)
            for root in self.protected_edit_roots
        ]

    def resolved_shell_rules(self) -> list[ShellCommandRule]:
        """The shell vocabulary this project actually judges by.

        Asked here rather than resolved at each caller, because the canonical
        policy and both generated dispatchers have to walk the same table. A
        second place that knew which defaults a selection layers over is the
        shape of a policy that decides one way in a session and another way in
        the plugin that session's own declaration generated.
        """
        return self.shell_rules.over(default_vocabulary())

    def resolved_edit_rules(self) -> list[EditRule]:
        """The edit table this project layers over the kernel's own verdicts.

        Resolved over an empty library table on purpose: the defaults for this
        family live in the gates themselves, where a selection cannot retire
        one out from under the project that never asked to.
        """
        return self.edit_rules.over([])

    def resolved_import_boundaries(self) -> list[ImportBoundary]:
        """The dependency rules retained by the same selection as the auditor."""
        return [
            boundary
            for boundary in self.import_boundaries
            if self.rules.keeps(boundary.rule_id)
        ]


class ResolveSpec(BaseModel, frozen=True):
    id: str
    worker_identity: NativeName
    """The identity a worker session declares, and the one the edit policy
    grants autonomy to. Both adapters derive their autonomous list from this
    single fact, so a runtime cannot silently ship an empty one."""

    worker_skill: SkillInvocation
    review_skill: SkillInvocation
    merge_skill: SkillInvocation

    contain_actors: bool = True
    """Whether each actor a run opens gets a container, and a lease, of its own.

    Declared rather than assumed because it decides what a run *requires*: with
    this set, a host with no container engine cannot start one and is told so,
    rather than quietly running its actors on the host. That refusal is the
    point. Two actors sharing a repository is the concurrency the mount rail
    exists for, and a run that silently dropped the boundary would look exactly
    like one that held it.

    True because the alternative was never a decision anybody made. Actors ran
    unconfined for as long as the lease was a launch-time snapshot, which
    covered the checkouts a lone operator was landing and none of the worktrees
    a run leases -- so the protection reached the sessions working alone and
    missed the ones working at once. A project that means to run its actors on
    the host overrules this here, in one place, where it reads as the posture
    it is.
    """


class Plugin(BaseModel, frozen=True):
    id: str
    name: NativeName
    # Namespaces the plugin inside the selected CODEX_HOME and remains required
    # for callers that deliberately share one home across projects.
    marketplace: NativeName
    version: str
    description: PortableText = Field(min_length=1, max_length=1024)
    skills: list[Skill]
    agents: list[Agent]
    hooks: HookSet | None = None

    def committed_skills(self) -> list[Skill]:
        """The skills every checkout's tree carries: all but the machine's own."""
        return [skill for skill in self.skills if skill.machine_hint is None]

    def machine_skills(self) -> list[Skill]:
        """The skills each machine renders for itself, naming what it keeps."""
        return [skill for skill in self.skills if skill.machine_hint is not None]

    @model_validator(mode="after")
    def unique_effective_names(self) -> "Plugin":
        skill_names = [skill.name for skill in self.skills]
        agent_names = [agent.name for agent in self.agents]
        if len(skill_names) != len(dict.fromkeys(skill_names)):
            raise ValueError(f"plugin {self.id!r} has duplicate skill names")
        if len(agent_names) != len(dict.fromkeys(agent_names)):
            raise ValueError(f"plugin {self.id!r} has duplicate agent names")
        return self


class Harness(BaseModel, frozen=True):
    schema_version: int = 1
    generator_version: str
    source_evidence: dict[str, str] = {}  # lup: ignore[dict-str-payload]
    plugins: list[Plugin]
    guidance: PromptDocument
    resolver: ResolveSpec | None = None
    """How a resolver run is spelled, or nothing for a project without one.

    Its three invocations are checked against the declared skills only when
    a spec is here: a project that declined the resolver module ships no
    worker, review or merge skill, and must not have to declare a spec
    naming skills it does not have."""
    dashboard: bool = False
    """Whether every launch holds the operator's dashboard, as the dashboard module does.

    Independent of the sandbox: a contained session's reviews are answered on
    the host's page like any other. A harness without it starts no service and
    imports none of the page's dependencies."""
    requirements: Manifest = Manifest()
    """The external programs this project needs, exercised before a launch.

    Empty declares no requirement and checks nothing, which is right for a
    project whose toolchain is entirely Python: a preflight that invented
    prerequisites would refuse machines that were fine. What a project does
    declare here is checked by the launch, printed by the standalone command,
    and installed into any image built from this harness -- one roster, so
    the three cannot describe different toolchains.
    """
    image: Image = Image()
    """The container an agent session runs in, built from the roster above.

    Separate from ``requirements`` because it answers a different question:
    that says *what* the toolchain is, this says how a container carrying it
    is assembled and started. The package list is not repeated here -- it is
    read off the manifest, so an image cannot be built from a roster the
    preflight never exercised.
    """

    @property
    def declared_hooks(self) -> HookSet:
        """The hook set this harness enforces, wherever a plugin declares it.

        A session composed in process reaches the same declaration the
        generated plugins are compiled from, so what a launched tree enforces
        and what an in-process session enforces cannot come apart.
        """
        return next(plugin.hooks for plugin in self.plugins if plugin.hooks is not None)

    def holding(self, rules: RuleSelection) -> "Harness":
        """This harness compiled against a different selection of scan rules.

        One way in, for the one caller that has a reason: a launch opening a
        session the rules are not the point of. Every plugin is rewritten
        together, because one selection reaches the sweep, the edit hook and
        the generated reference — and a tree relaxed on one runtime and not
        another would be two policies wearing one name.

        A copy rather than a mutation, and only generation reads it. What a
        repository holds itself to stays the declaration in its catalog,
        which is where a durable answer belongs and where `dev seams` writes
        one.
        """
        return self.model_copy(
            update={
                "plugins": [
                    plugin
                    if plugin.hooks is None
                    else plugin.model_copy(
                        update={
                            "hooks": plugin.hooks.model_copy(update={"rules": rules})
                        }
                    )
                    for plugin in self.plugins
                ]
            }
        )

    @property
    def rendered_ids(self) -> list[str]:
        """Every declaration a target renders as an artifact of its own.

        The roster as the source states it, before any target has shaped it
        into files. Each id is what a rendered artifact carries back, so this
        is the one list both trees can be measured against — which is how a
        target that silently renders one fewer skill than another is caught
        without either tree's own path shapes entering the comparison. A
        machine's own skill is absent: no committed tree renders it, each
        machine's overlay does.
        """
        return [
            declaration_id
            for plugin in self.plugins
            for declaration_id in [
                plugin.id,
                *[skill.id for skill in plugin.committed_skills()],
                *[agent.id for agent in plugin.agents],
            ]
        ]

    @property
    def declared_ids(self) -> list[str]:
        """Every semantic id this source names anywhere, in declaration order.

        Wider than :attr:`rendered_ids` by the tool servers, because this
        answers whether two declarations collide rather than whether a target
        rendered one, and a server's id collides with a skill's exactly as a
        skill's does.
        """
        return [
            declaration_id
            for plugin in self.plugins
            for declaration_id in [
                plugin.id,
                *[skill.id for skill in plugin.skills],
                *[agent.id for agent in plugin.agents],
            ]
        ]

    @model_validator(mode="after")
    def unique_semantic_ids(self) -> "Harness":
        ids = self.declared_ids
        if len(ids) != len(dict.fromkeys(ids)):
            raise ValueError("harness semantic ids must be globally unique")
        plugin_names = [plugin.name for plugin in self.plugins]
        if len(plugin_names) != len(dict.fromkeys(plugin_names)):
            raise ValueError("harness plugin names must be unique")
        agent_names = [agent.name for plugin in self.plugins for agent in plugin.agents]
        if len(agent_names) != len(dict.fromkeys(agent_names)):
            raise ValueError("harness agent names must be globally unique")
        discovery_size = sum(
            len(declaration.description)
            for plugin in self.plugins
            for declaration in [plugin, *plugin.skills, *plugin.agents]
        )
        if discovery_size > 32_768:
            raise ValueError("harness discovery descriptions exceed 32768 characters")

        skills = {
            (plugin.name, skill.name): skill
            for plugin in self.plugins
            for skill in plugin.skills
        }
        prompts = [
            self.guidance,
            *[
                declaration.prompt
                for plugin in self.plugins
                for declaration in [*plugin.skills, *plugin.agents]
            ],
        ]
        invocations = [
            issued
            for prompt in prompts
            for part in prompt.walked()
            if (issued := part.invocation) is not None
        ]
        if self.resolver is not None:
            invocations.extend(
                [
                    self.resolver.worker_skill,
                    self.resolver.review_skill,
                    self.resolver.merge_skill,
                ]
            )
        for invocation in invocations:
            skill = skills.get((invocation.plugin, invocation.skill))
            if skill is None:
                raise ValueError(
                    "skill invocation refers to an unknown declaration: "
                    f"{invocation.plugin}:{invocation.skill}"
                )
            supplied = [argument.name for argument in invocation.arguments]
            if len(supplied) != len(dict.fromkeys(supplied)):
                raise ValueError(
                    f"skill invocation {invocation.plugin}:{invocation.skill} "
                    "has duplicate arguments"
                )
            declared = [argument.name for argument in skill.arguments]
            if any(name not in declared for name in supplied):
                raise ValueError(
                    f"skill invocation {invocation.plugin}:{invocation.skill} "
                    "has an unknown argument"
                )
            expected_order = [name for name in declared if name in supplied]
            if supplied != expected_order:
                raise ValueError(
                    f"skill invocation {invocation.plugin}:{invocation.skill} "
                    "arguments are not in declaration order"
                )
            missing = [
                argument.name
                for argument in skill.arguments
                if argument.required and argument.name not in supplied
            ]
            if missing:
                raise ValueError(
                    f"skill invocation {invocation.plugin}:{invocation.skill} "
                    f"is missing required arguments: {missing}"
                )

        declared_agents = [
            f"{plugin.name}:{agent.name}"
            for plugin in self.plugins
            for agent in plugin.agents
        ]
        parts = [part for prompt in prompts for part in prompt.walked()]
        unknown_plugins = [
            named
            for part in parts
            if (named := part.named_plugin) is not None and named not in plugin_names
        ]
        if unknown_plugins:
            raise ValueError(f"prompt parts name unknown plugins: {unknown_plugins}")
        unknown_agents = [
            delegated
            for part in parts
            if (delegated := part.named_agent) is not None
            and delegated not in declared_agents
        ]
        if unknown_agents:
            raise ValueError(f"delegations name unknown agents: {unknown_agents}")

        used = self.guidance.text_size()
        if used > GUIDANCE_BUDGET.ceiling:
            raise ValueError(
                f"always-loaded guidance is {used} bytes, over the "
                f"{GUIDANCE_BUDGET.ceiling} budget by "
                f"{used - GUIDANCE_BUDGET.ceiling}. Move a section to a "
                "generated document under docs/ and leave a file-path pointer, "
                "the way Self-Improvement Loop and Permission Hooks were split."
            )
        return self


def path_beneath_root(value: Path) -> Path:
    raw = str(value)
    portable = PurePosixPath(value.as_posix())
    if (
        "\\" in raw
        or "\0" in raw
        or value.is_absolute()
        or ".." in portable.parts
        or portable == PurePosixPath(".")
    ):
        raise ValueError(f"artifact path must stay beneath its root: {value}")
    return value


type ArtifactPath = Annotated[Path, AfterValidator(path_beneath_root)]
"""A relative path proven unable to escape or alias the root it joins."""


def lf_normalized(value: str) -> str:
    if "\r" in value:
        raise ValueError("artifact content must use LF newlines")
    return value if not value or value.endswith("\n") else value + "\n"


type NormalizedText = Annotated[str, AfterValidator(lf_normalized)]
"""LF-only text, normalized to terminate in a newline."""


class Artifact(BaseModel, frozen=True):
    path: ArtifactPath
    content: NormalizedText
    semantic_id: str = Field(min_length=1)
    executable: bool = False
    banner: ArtifactBanner | None = None
    """This artifact's provenance, or its declared reason for carrying none.
    Leaving it unset states nothing, which :mod:`lup.harness.validation`
    accepts only for a format that admits no comment at all."""

    @classmethod
    def generated(
        cls,
        *,
        path: ArtifactPath,
        body: str,
        semantic_id: str,
        banner: GeneratedBanner,
        executable: bool = False,
    ) -> "Artifact":
        """Compose one artifact beneath the banner naming what produced it."""
        return cls(
            path=path,
            content=banner.applied_to(path, body),
            semantic_id=semantic_id,
            executable=executable,
            banner=banner,
        )

    @classmethod
    def in_yaml(
        cls,
        *,
        path: ArtifactPath,
        document: YamlDocument,
        semantic_id: str,
        banner: GeneratedBanner,
    ) -> "Artifact":
        """One artifact whose body is a YAML document rather than text about one.

        The constructor a generator reaches for instead of formatting the
        file: what it is handed is a tree that has already been emitted and
        parsed back, so a derived value cannot arrive having ended the mapping
        it was written into. YAML inside a Markdown file is frontmatter, which
        :class:`lup.formats.markdown.MarkdownDocument` holds instead.
        """
        return cls.generated(
            path=path,
            body=document.text(),
            semantic_id=semantic_id,
            banner=banner,
        )

    @classmethod
    def in_markdown(
        cls,
        *,
        path: ArtifactPath,
        document: MarkdownDocument,
        semantic_id: str,
        banner: ArtifactBanner,
    ) -> "Artifact":
        """One artifact whose body is a Markdown document, frontmatter included.

        The banner is asked rather than applied, because most of these carry
        the exemption instead: a skill file is verbatim model-facing text, and
        a comment opening it would open every prompt compiled from it.
        """
        return cls(
            path=path,
            content=banner.applied_to(path, document.text()),
            semantic_id=semantic_id,
            banner=banner,
        )

    @classmethod
    def in_toml(
        cls,
        *,
        path: ArtifactPath,
        document: TomlDocument,
        semantic_id: str,
        banner: GeneratedBanner,
    ) -> "Artifact":
        """One artifact whose body is a TOML document rather than text about one."""
        return cls.generated(
            path=path,
            body=document.text(),
            semantic_id=semantic_id,
            banner=banner,
        )

    @classmethod
    def in_json(
        cls,
        *,
        path: ArtifactPath,
        data: PlainData,
        semantic_id: str,
        banner: ArtifactBanner,
        indent: int = 2,
    ) -> "Artifact":
        """One artifact whose body is JSON, serialized rather than formatted.

        Keys are sorted, because the artifact is compared against what is
        committed and a mapping that reordered itself between runs would read
        as a change nobody made. JSON holds no comment, so the banner most of
        these carry is the exemption saying why.
        """
        return cls(
            path=path,
            content=dumps(data, indent=indent, sort_keys=True),
            semantic_id=semantic_id,
            banner=banner,
        )

    @model_validator(mode="after")
    def banner_opens_content(self) -> "Artifact":
        if self.banner is not None and not self.banner.opens(self.path, self.content):
            raise ValueError(
                f"artifact {self.path.as_posix()} does not open with the banner "
                "it declares"
            )
        return self


class ArtifactTree(BaseModel, frozen=True):
    artifacts: list[Artifact]

    @model_validator(mode="after")
    def unique_paths(self) -> "ArtifactTree":
        paths = [artifact.path for artifact in self.artifacts]
        if len(paths) != len(dict.fromkeys(paths)):
            raise ValueError("artifact paths must be unique")
        return self


class CapabilityReport(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """A runtime probe's verdict, without the payload that proves it.

    Split from the evidence because the commands that report readiness read
    only the verdict — which capability, whether it is there, at what
    version. The proof is the adapter's own shape, so a command typed
    against this stays free of every runtime it reports on, and one that
    genuinely needs the proof asks for :class:`CapabilityEvidence` instead.
    """

    capability: str
    supported: bool
    version: str


class CapabilityEvidence[C](CapabilityReport, frozen=True):
    """One probe's verdict together with the adapter-shaped proof of it."""

    evidence: C


class Resumption(BaseModel, frozen=True):
    """Which earlier session a launch reopens, if any.

    One shape for every launcher, because what an operator is asking for is
    the same whichever runtime answers and only the spelling differs — a flag
    on one, a subcommand on another. Declared once rather than as three loose
    booleans threaded through each launcher, so a runtime added later answers
    one question instead of being handed three that can disagree.

    Reopening matters beyond convenience, which is what makes it worth a
    declaration. The policy a session enforces is compiled into a plugin tree
    its runtime loads at startup, so widening that policy takes effect only in
    a new process — and a new process that started from nothing costs the whole
    conversation that established what the widening was for. That price is
    what pushes an agent toward a per-call escape, which helps once and
    evaporates. With reopening, the durable path is also the cheap one:
    propose the declaration edit, have it approved, regenerate, reopen.
    """

    latest: bool = False
    """Reopen the most recent session here, without choosing one."""

    pick: bool = False
    """Offer the runtime's own picker over this project's sessions."""

    session: str | None = None
    """Reopen one session by the id its runtime knows it as."""

    def wanted(self) -> bool:
        """Whether this launch is reopening anything at all."""
        return self.latest or self.pick or self.session is not None

    def contradicted(self) -> str | None:
        """The complaint, when more than one session was named at once.

        Refused rather than ranked: an order of precedence here would be this
        module deciding which of two things an operator meant, and being
        wrong about it silently.
        """
        asked = [
            name
            for name, given in (
                ("--continue", self.latest),
                ("--resume", self.pick),
                ("--session", self.session is not None),
            )
            if given
        ]
        if len(asked) < 2:
            return None
        return f"a launch reopens one session; got {', '.join(asked)}"
