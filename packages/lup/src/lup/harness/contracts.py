"""Neutral capability seams composed by neutral harness code.

Each ABC names one narrow operation of the harness domain: the generation
pipeline stages from rendering canonical declarations through materializing
files on disk. Neutral orchestration composes these seams; provider-specific
implementations live in adapter packages, while complete process and
validation boundaries live beside their deterministic implementations.
Parameter and result models live with their owning concern, so imports here
are type-only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from pathlib import Path

    from lup.harness.materialization import MaterializationResult
    from lup.harness.models import (
        ArtifactTree,
        CapabilityEvidence,
        LocatedPart,
        PluginLocation,
        PromptDocument,
        QualifiedAgentName,
        SkillInvocation,
        TreeLocation,
    )
    from lup.harness.reconciliation import CurrentTree, ReconciliationProposal
    from lup.types import ModelTier


class ArtifactRenderer[S](ABC):
    """Render one cohesive artifact family."""

    @abstractmethod
    def render(self, source: S) -> ArtifactTree:
        """Render source into a complete in-memory artifact tree."""


class SkillInvocationRenderer(ABC):
    """Own one native runtime's complete skill invocation spelling."""

    @abstractmethod
    def render(self, invocation: SkillInvocation) -> str:
        """Render qualification, escaping, and arguments together."""


class Spelling(BaseModel, ABC, frozen=True):
    """What one runtime says for a portable idea, or why it has nothing to say.

    A portable idea one runtime cannot express is the case absence handles
    worst: a method left off a vocabulary, or one returning nothing, reads
    exactly like a method nobody has written yet, and the reason it is missing
    lives wherever the reader happens to look. Both answers are values of this
    type instead, so declining is a thing a runtime states rather than a thing
    it omits, and the seam stays closed by construction.

    Both operations are total, so neither answer is a case a caller has to
    remember to check for. Prose places what :meth:`in_prose` returns and a
    declined answer contributes nothing there; a parity audit reads
    :meth:`audited`, which is where a declined answer says why.
    """

    @abstractmethod
    def in_prose(self) -> str:
        """The words a prompt places here, and none where the runtime declines."""

    @abstractmethod
    def audited(self) -> str:
        """How a parity audit reports this answer, declined answers included."""


class Atom(str):
    """One native name spelled whole: a path, a product name, a reference.

    Portable prose may not contain one, because whatever names it should have
    been a prompt part. Every character is the runtime's own, so the whole
    string is vocabulary.
    """


class Instruction(str):
    """One native sentence carrying the caller's own words inside it.

    Only the runtime's identifiers are its own here — the interpolated text
    belongs to whoever declared it — so vocabulary is the identifier-shaped
    tokens rather than the whole sentence.
    """


class Spelled(Spelling, frozen=True):
    """The runtime's own words for the idea, ready to be placed in prose.

    What a runtime hands over is an :class:`Instruction`, since these
    sentences all frame text their caller supplied; the field is a plain
    string because a validated model copies what it is given, and the
    distinction the two str types carry is about which words a rule may
    judge, not about the object that survives validation.
    """

    words: str

    def in_prose(self) -> str:
        return self.words

    def audited(self) -> str:
        return self.words


class Unsupported(Spelling, frozen=True):
    """One portable idea this runtime has no way to say, and why not.

    The reason is the whole point: a runtime declines because of something
    true about it — a roster with no such tool, an override that only exists
    at session scope — and that fact is what a reader needs, whether they are
    auditing the two vocabularies against each other or wondering why a
    sentence they expected is not in their prompt. Approximating the idea
    would be worse than declining it, because a reader cannot tell an
    approximation from the real thing until it fails.
    """

    reason: str
    """Why this runtime cannot say it, in words that stand on their own."""

    def in_prose(self) -> str:
        return ""

    def audited(self) -> str:
        return f"unsupported — {self.reason}"


class NativeSpellings(SkillInvocationRenderer, ABC):  # lup: ignore[abc-capability]
    """Own one native runtime's spelling of everything portable prose names.

    Deliberately wider than the three-method shape the capability rule wants: a
    runtime's vocabulary is one concept with many words, and splitting it into
    a handful of ABCs would make a caller compose several objects that never
    vary independently. It extends the invocation renderer rather than being
    implemented alongside it, so a single object satisfies both seams and no
    implementation inherits two capabilities.

    Every native word a prompt can reach arrives through one of these, so a
    declaration states intent and never a platform. A new prompt part adds an
    abstract method here, which no runtime can be constructed without
    answering — the rendering seam is closed by construction rather than by a
    reminder to edit two renderers.

    The return type says what kind of spelling a method produces, and that is
    also the answer the portable-content rule reads: an :class:`Atom` is the
    runtime's own word end to end, an :class:`Instruction` wraps words the
    caller supplied. A method returning a bare ``str`` spells into an artifact
    rather than into prose, and no prose is judged against it.

    A method returning :class:`Spelling` names an idea a runtime may not be
    able to express at all. It still has to answer, and the answer it gives
    when it cannot is an :class:`Unsupported` carrying the reason — which is
    what keeps a runtime from being silently absent on an idea the other one
    spells.
    """

    # lup: ignore[abc-capability] — NativeSpellings owns one runtime's whole vocabulary, deliberately wider than the three-method shape; the class docstring carries the argument
    @property
    @abstractmethod
    def runtime_name(self) -> Atom:
        """Name the runtime the way prose addresses it."""

    # lup: ignore[abc-capability] — NativeSpellings owns one runtime's whole vocabulary, deliberately wider than the three-method shape; the class docstring carries the argument
    @property
    @abstractmethod
    def native_identifiers(self) -> list[Atom]:
        """This runtime's own words that appear inside its instructions.

        An instruction frames text its caller supplied, so the sentence as a
        whole says nothing about prose — but the runtime's own words within it
        are exactly what prose must not reach for. Each one has to occur in
        something this runtime actually spells, which is checked, so the list
        cannot drift away from the sentences it describes.
        """

    @abstractmethod
    def tree(self, location: TreeLocation) -> Atom:
        """Spell one harness-tree location."""

    @abstractmethod
    def plugin(self, plugin: str, location: PluginLocation, member: str | None) -> Atom:
        """Spell one plugin-owned location, with or without a leaf."""

    @abstractmethod
    def invocation_pattern(self, plugin: str, placeholder: str) -> Atom:
        """Spell an invocation whose skill the reader supplies."""

    @abstractmethod
    def arguments_ref(self) -> Atom:
        """Spell how this runtime reaches the arguments of an invocation."""

    @abstractmethod
    def ask_user(self, question: str) -> Instruction:
        """Instruct the runtime to put one material question to the user."""

    @abstractmethod
    def delegate(
        self, subagent_type: QualifiedAgentName, prompt: str, name: str = ""
    ) -> Instruction:
        """Instruct the runtime to hand one task to one of its agents."""

    @abstractmethod
    def request_approval(self, action: str, reason: str) -> Instruction:
        """Instruct the runtime to obtain explicit approval before acting."""

    @abstractmethod
    def relocate_session(self, path: str) -> Instruction:
        """Spell the move into an already-created worktree this runtime allows."""

    @abstractmethod
    def escape_sandbox(self, reason: str) -> Spelling:
        """Spell how one command this runtime runs escapes its own sandbox.

        The test is whether an agent can be told words that take one call
        out, not whether the runtime ever crosses the boundary. A runtime
        that crosses it only through configuration written before the session
        started has nothing a prompt could ask for — and naming a flag the
        reader cannot pass would read as an instruction. Declining is the
        honest answer there, which is why this returns a :class:`Spelling`
        rather than a sentence every runtime must invent.

        What an agent is told to write to *request* the host is Lup's own
        marker, ``# lup: escalate[sandbox]: <why>``, and it is the same
        sentence under every runtime because the request is answered by
        Lup's reviewer rather than by a native flag. These words are the
        adapter mechanism beneath that answer: how this runtime carries an
        approved ``outside`` placement out of its own per-call sandbox in a
        session whose ambient location is already the launcher's host.
        """

    @abstractmethod
    def watch_output(self, command: str) -> Instruction:
        """Spell how to wait on a command that reports before it exits.

        Neutral prose cannot carry this advice, because the runtimes differ in
        what waiting *is*. One pushes each line to the agent as it arrives, so
        the right instruction is to open one waiter and yield; the other hands
        the agent a live session it reads, so the same instruction is
        impossible and reading is the mechanism rather than the mistake.
        Wording that told every reader not to poll would be correct on the
        first and unfollowable on the second, which is how prose naming only
        "the runtime's event-driven waiter" leaves a reader to guess — and a
        reader who guesses reaches for an ordinary command with a long
        timeout, which is the polling loop the advice existed to prevent.
        """

    @abstractmethod
    def nested_run(self, prompt: str) -> Instruction:
        """Spell how to run this runtime once, non-interactively, from a directory.

        A probe kit measures a hook or a payload by launching the runtime over
        a throwaway project whose settings register the hook. Each runtime has
        its own print mode, its own approval switch, and its own answer to
        whether a nested launch is allowed at all, so prose naming "a
        non-interactive run" leaves the reader to guess the flags, and a
        wrong guess stops at the first approval prompt nobody is there to
        answer.
        """

    @abstractmethod
    def read_document(self, path: str) -> Spelling:
        """Name the tool this runtime hands a whole document to, path and all.

        A document a text extractor cannot read — a scanned page, a slide, an
        image-only PDF — comes back from one as an empty string that reads
        like an empty document, so prose steers to the runtime's own reader
        instead. A roster with nothing that takes a document declines.
        """

    @abstractmethod
    def resolver_entry(self) -> Instruction:
        """Spell how this runtime enters the shared resolver."""

    @abstractmethod
    def runtime_docs(self) -> Instruction:
        """Name this runtime's own documentation, wherever it lives."""

    @abstractmethod
    def model_alias(self, tier: ModelTier) -> str | None:
        """Spell one portable tier into agent metadata, or decline it.

        Neither an atom nor an instruction: this reaches a generated artifact,
        never a prompt, so tier words like ``inherit`` stay ordinary English
        everywhere prose is judged.
        """


class PromptRenderer(ABC):
    """Own one native runtime's complete prompt-document spelling.

    A part spells itself against this, so what a renderer offers is the whole
    of what a part may reach for: the reader's vocabulary, and the one question
    — scope — that no single vocabulary can answer alone.
    """

    own: NativeSpellings
    """The vocabulary of the runtime that will read what this renders."""

    @abstractmethod
    def render(self, prompt: PromptDocument) -> str:
        """Render every semantic prompt part into native prompt text."""

    @abstractmethod
    def location(self, part: LocatedPart) -> str:
        """Spell one location for the reader, or for every runtime at once."""


class CurrentTreeReader(ABC):
    """Read and classify current artifact ownership."""

    @abstractmethod
    def read(self, root: Path) -> CurrentTree:
        """Read current files without mutating the root."""


class Reconciler(ABC):
    """Compare current and desired trees."""

    @abstractmethod
    def propose(
        self, current: CurrentTree, desired: ArtifactTree
    ) -> ReconciliationProposal:
        """Return writes, proven deletions, and explicit conflicts."""


class Materializer(ABC):
    """Apply one validated reconciliation proposal."""

    @abstractmethod
    def apply(self, proposal: ReconciliationProposal) -> MaterializationResult:
        """Atomically update only proven generator-owned paths."""


class CapabilityProbe[C](ABC):
    """Probe one named capability contract."""

    @abstractmethod
    def probe(self) -> CapabilityEvidence[C]:
        """Return evidence without invoking an unsupported operation."""
