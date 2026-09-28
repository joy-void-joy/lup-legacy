"""What a repository's surface holds, at any revision, so a range cannot lose part of it.

A reorganisation that moves modules is judged by a claim nothing reads the
tree to check: *no capability disappeared*. The claim is made once, in a plan,
against a surface too large to hold in one reading — and a capability that
went missing looks exactly like one that moved, because both leave the old
import path unresolvable. So the assumption survives review by being
unfalsifiable rather than by being true.

Two surfaces make it falsifiable. Every name one revision declared is resolved
against a later one: a name that resolves nowhere has disappeared, and that is
the failure; a name that resolves somewhere else has moved, and that is not —
it is the migration map, which is the second thing this is for, since an
adopter's imports are repointed from the same difference that proves nothing
was lost.

Both surfaces are *read*, never imported. A name's declaration is in its
module's text, and a revision worth comparing against is one this process
could not import anyway: every name would resolve to the code already loaded
here, under the layout the range was about to change. Reading also makes the
question free of storage — git holds every revision, so a checked-in fixture
would be a copy of what the repository already has, going stale against it.

Identity is the declared name, because that is what a caller depends on; the
module holding it is exactly what a reorganisation is entitled to change.
Location rides along as evidence rather than as identity, so a name the later
surface declares anywhere has survived. Where a name several modules share —
``logger``, ``app`` — moves with one of them, the move is recorded but casts
no vote in the migration map, whose pairs are drawn only from the names that
landed in exactly one place.

A pair is a claim about a whole module — respell every import of this path —
so it is made only for a module that kept nothing back. One that gave a name
away and went on declaring the rest has not moved, and a pair for it would
send the names that stayed to a module which never held them, breaking a
correct importer in a way that surfaces as an unresolved import far from the
rewrite that caused it. That a name resolves elsewhere is in any case weaker
evidence than it looks: two modules choosing one word for two purposes leave
the same trace as a move, and only what the module kept separates them. So
where no pair can be drawn the names are reported as they stand — which of
them resolve where now, and which the module still declares — because the
map is worth having only if it can be trusted whole.

Only what a published root can import is walked. A generated plugin tree is
derived from the typed catalog that emits it, and a test names what it tests,
so neither is a surface an adopter can hold — counting them would grow the
walk by half and make every regeneration read as a capability change.

A command is not walked separately: it is declared by a function whose name is
in this surface already, so a command that goes takes its own declaration with
it. Walking the composed CLI would mean importing a revision, which is the one
thing that cannot be done from here.
"""

import tempfile
from collections.abc import Iterable, Iterator, Set as AbstractSet
from itertools import groupby
from pathlib import Path

from pydantic import BaseModel

from lup.devtools.dev.library import LibraryMode, read_mode
from lup.devtools.dev.model_config import materialize_revision
from lup.harness.codescan.common import PACKAGE_ROOTS, module_name
from lup.harness.codescan.symbols import defined_symbols
from lup.devtools.dev.boundaries import TrackedSource, tracked_python_sources
from lup.devtools.dev.relocate import name_parts
from lup.devtools.project import DevProject
from lup.execution.shell import git


class Capability(BaseModel, frozen=True):
    """One name the repository offers, and the module it offered it from."""

    identity: str
    """What a caller depends on: the name, wherever it is declared."""

    location: str
    """The module declaring it."""

    def spelled(self) -> str:
        """This entry as one line, for a reader comparing two surfaces."""
        return f"{self.identity} ({self.location})"


class ModuleSurface(BaseModel, frozen=True):
    """One module, and every name it declares at a scope an importer can reach.

    Grouped rather than held flat because that is how the file is read and how
    a refactor moves: a module arriving, leaving, or being renamed is one
    block in the diff instead of several hundred adjacent lines.
    """

    module: str
    declares: list[str]


class SurfaceCapture(BaseModel, frozen=True):
    """A whole surface as it stood at one revision, with what was walked."""

    revision: str
    """The commit the capture read, so a divergence traces to a range."""

    roots: list[str]
    """The import roots walked, named so a later capture covers the same tree."""

    modules: list[ModuleSurface]

    def capabilities(self) -> Iterator[Capability]:
        """Every entry this capture holds, flattened to one shape."""
        for surface in self.modules:
            for name in surface.declares:
                yield Capability(identity=name, location=surface.module)

    def homes(self) -> dict[str, list[str]]:
        """Each export identity this capture holds, and the modules declaring it."""
        declared = sorted(
            (name, surface.module)
            for surface in self.modules
            for name in surface.declares
        )
        return {
            identity: [module for _, module in group]
            for identity, group in groupby(declared, key=lambda pair: pair[0])
        }


class Relocation(BaseModel, frozen=True):
    """A capability that survived, at an address none of its captures named."""

    capability: Capability

    homes: list[str]
    """Where it is declared now — several, when the name is a shared one."""

    def spelled(self) -> str:
        """The move as one line: what it was, and where it went."""
        return f"{self.capability.spelled()} → {', '.join(self.homes)}"


class ModuleDestination(BaseModel, frozen=True):
    """One module that took names out of another, and which names it took."""

    module: str
    names: list[str]


class ModuleMove(BaseModel, frozen=True):
    """One module's exported names, and where each of them resolves now.

    Evidence rather than a verdict, because the two are not the same thing.
    A name this module declared and no longer does, now declared in exactly
    one other module, is consistent with the module having moved — and just
    as consistent with two modules having chosen the same word: a ``GUARD``
    or a ``runtime_source`` written fresh somewhere unrelated resolves the
    same way a moved one does, and no count of such names tells them apart.

    What does tell them apart is what the module kept. A module declaring
    none of its own names any more, with one module holding all of them, has
    moved whole and takes a pair. One still declaring some of them has not
    moved, whether it split or only shares a word, and a pair for it would
    respell every import of it — sending the names that stayed to a module
    that never held them, met long after as an unresolved import.
    """

    module: str
    """The module the names were exported from at the earlier revision."""

    destinations: list[ModuleDestination]
    """Every module now declaring some of them, and which ones it declares."""

    retained: list[str]
    """What this module still declares itself, which is what refuses a pair."""

    def wholesale(self) -> str | None:
        """The one module this one's whole surface became, if it became one."""
        match (self.destinations, self.retained):
            case ([only], []):
                return only.module
            case _:
                return None

    def spelled(self) -> str:
        """One line: which names resolve where now, and which did not move."""
        went = "; ".join(
            f"{', '.join(destination.names)} now in {destination.module}"
            for destination in self.destinations
        )
        stayed = f"; still declares {', '.join(self.retained)}" if self.retained else ""
        return f"{self.module}: {went}{stayed}"


class Divergence(BaseModel, frozen=True):
    """What a live tree does and does not still answer for a capture."""

    disappeared: list[Capability]
    """Captured, and now declared by nothing — the failure this exists to find."""

    relocated: list[Relocation]
    """Captured, still reachable, and no longer where it was."""

    arrived: list[Capability]
    """Live and uncaptured, so a reader can see what the range added."""

    moves: list[ModuleMove]
    """Every module that lost a name, and where each of its names ended up."""

    def intact(self) -> bool:
        """Whether every captured capability is still reachable somewhere."""
        return not self.disappeared

    # lup: ignore[dict-str-payload] — module paths on both sides, open and
    # data-driven: whichever modules the range under review happened to move
    def module_moves(self) -> dict[str, str]:
        """Old module to new, for every module whose whole surface moved.

        The argument list for repointing an adopter, derived rather than
        written down. A pair says *every* import of the old path is to be
        respelled, so it is drawn only where that is true: one module holds
        the names now, and the old one declares none of them any more.
        """
        return {
            move.module: destination
            for move in sorted(self.moves, key=lambda move: move.module)
            if (destination := move.wholesale()) is not None
        }

    def unmapped_modules(self) -> list[ModuleMove]:
        """Every module that lost names and that no pair can repoint.

        Reported rather than dropped: a reader told which of a module's names
        resolve where now decides what to do about each, where one handed a
        silently shorter map learns nothing and keeps the imports that broke.
        """
        return sorted(
            (move for move in self.moves if move.wholesale() is None),
            key=lambda move: move.module,
        )


def walked_roots(
    project: DevProject,
    roots: AbstractSet[str] = PACKAGE_ROOTS,
    checkout: Path = Path(),
) -> AbstractSet[str]:
    """The roots a capture covers: the library's where it is vendored, plus the app's.

    Read from the declaration rather than written down, for the reason
    :class:`~lup.devtools.project.DevProject` exists: initialization renames
    the application's package, and a root named here would go on naming one
    that is gone.

    The library's roots are this checkout's surface only while it builds the
    library from its own tree. A project resolving ``lup`` from its repository
    or from a release imports every one of those names from the dependency, so
    the commit that stops vendoring it has taken nothing from anybody — while
    a walk reading the library at the base and none of it at the tip reports
    every name the library declares as gone. Asked of the checkout rather than
    of each revision, so both captures of one comparison cover the same roots;
    a checkout with no manifest has no workspace to vendor into.
    """
    manifest = checkout / "pyproject.toml"
    vendored = manifest.is_file() and read_mode(checkout) is LibraryMode.LOCAL
    return {*roots, project.package} if vendored else {project.package}


def offers_a_surface(parts: list[str], internal: Iterable[str]) -> bool:
    """Whether a name declared in this module is one anybody could import.

    A declared prefix says no, and says it for the whole subtree beneath it:
    :attr:`~lup.devtools.project.DevProject.internal_modules` carries what
    this repository publishes nothing out of. Compared as the segments both
    names parse to, so ``lup.policy.kernel`` reaches ``lup.policy.kernel.lex``
    without also reaching a ``lup.policy.kernels`` that a test on the text
    would have swallowed.
    """
    prefixes = [parsed for entry in internal if (parsed := name_parts(entry))]
    return not any(parts[: len(prefix)] == prefix for prefix in prefixes)


def surfaces(
    sources: Iterable[TrackedSource],
    roots: AbstractSet[str],
    internal: Iterable[str] = (),
) -> Iterator[ModuleSurface]:
    """Every walked module one of ``roots`` can import, and what it declares.

    A source resolving to a module path outside every root is not skipped for
    being uninteresting — it is unreachable, and a name nothing can import is
    not a capability whatever else it is. A path no import statement could
    spell at all — a generated tree under a dot directory — parses to no
    names and is left out by the same test.

    ``internal`` is that same judgement made by declaration rather than by
    layout: a module an importer can reach by name, out of a subtree this
    repository publishes nothing from. Empty walks everything, which is what
    a repository that declared nothing means.
    """
    for source in sources:
        module = module_name(source.path, roots)
        parts = name_parts(module)
        if (
            parts is not None
            and parts[0] in roots
            and offers_a_surface(parts, internal)
        ):
            yield ModuleSurface(
                module=module,
                declares=[
                    symbol.name
                    for symbol in defined_symbols(source.text)
                    if symbol.reachable
                ],
            )


def revision_sources(revision: str, roots: AbstractSet[str]) -> list[TrackedSource]:
    """Every Python file one revision held under the walked roots.

    Out of git rather than a checkout, and read rather than imported: what a
    module declares is in its text, and a revision old enough to be worth
    comparing against is one this process could not import anyway — every
    name would resolve to the code already loaded here, under the layout the
    range was about to change.

    Extracted whole rather than blob by blob. Asking git for one file at a
    time is a subprocess per module, which is thirteen seconds for this
    repository and rising; one archive is one subprocess whatever the tree
    grows to, and the gate reads it on every run.
    """
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        materialize_revision(revision, root)
        return [
            TrackedSource(
                rel=path.relative_to(root).as_posix(),
                path=path,
                text=path.read_text(encoding="utf-8"),
            )
            for path in sorted(root.rglob("*.py"))
            if not any(part.startswith(".") for part in path.relative_to(root).parts)
            and roots & {*path.relative_to(root).parts}
        ]


def surface_at(revision: str, project: DevProject) -> SurfaceCapture:
    """The surface one revision offered, read out of git without a checkout."""
    roots = walked_roots(project)
    return SurfaceCapture(
        revision=revision,
        roots=sorted(roots),
        modules=sorted(
            surfaces(
                revision_sources(revision, roots), roots, project.internal_modules
            ),
            key=lambda one: one.module,
        ),
    )


def surface_now(project: DevProject) -> SurfaceCapture:
    """The surface the working tree offers, uncommitted work included."""
    roots = walked_roots(project)
    return SurfaceCapture(
        revision=git.out("rev-parse", "HEAD"),
        roots=sorted(roots),
        modules=sorted(
            surfaces(tracked_python_sources(project), roots, project.internal_modules),
            key=lambda one: one.module,
        ),
    )


def compare(captured: SurfaceCapture, live: SurfaceCapture) -> Divergence:
    """Resolve every captured identity against a later walk of the same tree.

    An export is judged against the whole live surface rather than against its
    own module, which is what lets a move read as a move: the name resolving
    anywhere is enough to say it survived, and where it resolves is what makes
    the migration map.

    The map is settled here, where both surfaces are in hand, rather than from
    the relocations alone: whether a module kept any of its own names is a
    question about the module, and a list of the names that left cannot answer
    it.
    """
    homes = live.homes()
    held = {*captured.capabilities()}

    def answers(capability: Capability) -> list[str]:
        """Which modules the later surface declares this name in, if any."""
        return homes.get(capability.identity, [])

    def moved_modules() -> Iterator[ModuleMove]:
        """Each captured module that lost a name, and where its names went.

        A name several modules declare names no destination: which of them
        took it is exactly what cannot be told, and the module's unambiguous
        siblings say where the module went anyway.
        """
        for surface in sorted(captured.modules, key=lambda one: one.module):
            landed = sorted(
                (found[0], name)
                for name in surface.declares
                if len(found := homes.get(name, [])) == 1 and found[0] != surface.module
            )
            destinations = [
                ModuleDestination(
                    module=module, names=[name for _, name in list(group)]
                )
                for module, group in groupby(landed, key=lambda pair: pair[0])
            ]
            if destinations and surface.module:
                yield ModuleMove(
                    module=surface.module,
                    destinations=destinations,
                    retained=[
                        name
                        for name in surface.declares
                        if surface.module in homes.get(name, [])
                    ],
                )

    return Divergence(
        disappeared=[
            capability
            for capability in captured.capabilities()
            if not answers(capability)
        ],
        relocated=[
            Relocation(capability=capability, homes=found)
            for capability in captured.capabilities()
            if (found := answers(capability))
            and capability.location
            and capability.location not in found
        ],
        arrived=[
            capability for capability in live.capabilities() if capability not in held
        ],
        moves=list(moved_modules()),
    )


class Span(BaseModel, frozen=True):
    """The two ends a divergence is read between, as git's ``base..head`` names them."""

    base: str
    head: str = ""
    """The later end, or empty for the working tree.

    The working tree is what a gate asks about: uncommitted work is exactly
    where a capability goes missing before anybody notices.
    """

    def divergence(self, project: DevProject) -> Divergence:
        """The surfaces at both ends, compared."""
        return compare(
            surface_at(self.base, project),
            surface_at(self.head, project) if self.head else surface_now(project),
        )
