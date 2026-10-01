"""The targets a CLI selector names, and the accounts each runtime keeps.

Each runtime composes a project's content in its own adapter -- a
:class:`~lup.harness.generate.NativeComposer` bundling a generation recipe,
a runtime-readiness probe set, and a skill invocation renderer. A project
names its builders over those, and :class:`NativeTargets` maps the CLI
target selector onto the already concrete compositions they return. What a
project publishes through them is its own ``ProjectContent``, so nothing
here decides anything about content.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from lup.providers.claude.login import CLAUDE_LOGIN
from lup.harness.codescan.common import RuleSelection
from lup.devtools.harness.drift import refuse_generation
from lup.devtools.utils import refuse
from lup.harness.generate import (
    NativeHarnessComposition,
    obstruction_at,
)
from lup.policy.kernel.diagnostic import step
from lup.providers.profile_tree import profile_directory
from lup.providers.profiles import ProfileDirectory


def claude_profile_directory() -> ProfileDirectory:
    """The Claude side of the accounts this person keeps, as a directory to curate.

    What a project falls back to when it names no origin of its own: this
    checkout's own profiles, then the global ones every checkout shares, so
    an account signed in once opens in every repository.
    """
    return profile_directory(CLAUDE_LOGIN)


@runtime_checkable
class TargetBuilder(Protocol):
    """How one project turns a root into one runtime's whole composition.

    Takes an optional rule selection because the one caller with a reason to
    compile a tree against a different one is a launch — a session opened
    where the conventions are not the point. Declared on the seam rather than
    reached for through a global, so a project that wants no such launch
    simply ignores the argument, and one that does cannot be handed it
    through a channel nothing types.
    """

    def __call__(
        self, root: Path, rules: RuleSelection | None = None
    ) -> NativeHarnessComposition: ...


class NativeTargets(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Every native adapter a CLI selector can name, and how to build each.

    A project declares which runtimes it generates a tree for; the commands
    take already concrete compositions and never learn a target's name. The
    builders are keyed rather than listed because the selector a human types
    is the key, and the launch commands are the adapter's own surface.

    Arbitrary types because a builder is a callable seam rather than data:
    what pydantic would validate here is a signature, which is pyright's
    question and already answered there.
    """

    builders: dict[str, "TargetBuilder"]

    every: str = "all"
    """The selector reaching every declared tree at once, which is also what
    reaches the generated artifacts belonging to no single one of them."""

    def builder(self, name: str) -> "TargetBuilder | None":
        """How to build one named target, or nothing when it is not declared."""
        return self.builders.get(name)

    def resolve(self, value: str, root: Path) -> list[NativeHarnessComposition]:
        """Parse a generic CLI selector into already concrete compositions.

        The one boundary every command reaches a declaration through, so a
        declaration that will not compile is refused here, in the words it
        refused with, rather than travelling out as whatever its reader
        happened to raise: a missing passage file as an interpreter traceback,
        an invocation naming no skill as a hundred lines of pydantic field
        context. Both are declaration errors with a fix in the declaration,
        and this is where they are turned back into one.
        """

        def compiled(name: str, build: "TargetBuilder") -> NativeHarnessComposition:
            """One target's whole composition, or a refusal naming what stopped it."""
            try:
                return build(root)
            except Exception as refusal:
                refuse_generation(obstruction_at(name, refusal))

        if value == self.every:
            return [compiled(name, build) for name, build in self.builders.items()]
        build = self.builder(value)
        if build is not None:
            return [compiled(value, build)]
        refuse(
            "is not a target this project declares",
            what=value,
            steps=[step(f"name one of: {', '.join([*self.builders, self.every])}")],
            code=2,
        )
