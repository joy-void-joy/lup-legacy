"""One native call as everything downstream of the adapters understands it.

A provider adapter's whole job is to turn its own wire payload into this, and
everything after it — classification, settlement, the relay, the recovery
coordinator, the host executor, the audit — reads this and never the payload.
That is what makes "the same operation through Claude and Codex" a statement
with content: the two adapters produce the same value, so they cannot reach
different verdicts without one of them having normalized differently.

The semantic kind matters more than the native spelling. A production full
replacement, a remote ref deletion, and a pull request merge mean the same
thing whichever tool carried them, and a policy keyed on tool names is a
policy that can be stepped around by using a different tool.

The fingerprint is what an approval binds to. It covers the tool, the exact
arguments, the working directory, the resolved targets, and the placement —
everything a person was shown — so a payload that changed after they answered
is a fresh question rather than a stale approval quietly reused.
"""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.policy.kernel.decision import SandboxPlacement
from lup.policy.kernel.semantics import EffectClass
from lup.types import JsonObject, JsonValue

type OperationKind = Literal[
    "read",
    "local_mutation",
    "full_write",
    "remote_mutation",
    "process_execution",
    "unknown",
]
"""What an operation is, once the native spelling has been set aside.

Coarse on purpose: this decides which plans are worth resolving, not what the
verdict is. The rules answer the second question, and they read the plans.
"""


class ReadPlan(BaseModel, frozen=True):
    """Which resources an operation intends to inspect.

    Explicit intent, never a syscall trace: Lup does not authorize every read
    Git, a compiler, or a package manager performs internally, because doing
    so would make a question out of a toolchain reading its own configuration.
    What is recorded here is what the operation *named*.

    ``credential_targets`` is the half that matters. Normal use of a
    credential inside the boundary is part of a classified operation; naming
    one as the thing to print, copy, or upload is a different operation, and
    the two are only distinguishable if the naming is recorded.
    """

    targets: list[Path] = []
    unresolved: list[str] = []
    credential_targets: list[Path] = []
    system_inspection: bool = False
    """Whether this reads outside the workspace — /etc, /proc, the session home.

    Recorded rather than refused. Contained, such a read observes the
    contained environment and is settled inside; a host path that is not there
    fails inside with a boundary diagnostic naming the escalation that would
    reach it, which is a better answer than a question, because the agent
    usually did not need the host and finds that out itself.
    """


class Move(BaseModel, frozen=True):
    """One path an operation renames, and where it lands.

    Named rather than paired, because which half of a pair is the source is a
    fact a reader has to already know — and the reader who does not is the one
    writing the restore.
    """

    source: Path
    destination: Path


class MutationFootprint(BaseModel, frozen=True):
    """Every local path an operation can change, and how exactly it is known.

    ``exact`` is the whole of what decides which capture covers it. Targets
    that resolve statically take a targeted snapshot of exactly those paths;
    variables, globs, substitutions, or a directory walk mean the footprint
    cannot be enumerated, and the capture widens to every precious writable
    root rather than the operation being refused. The wider capture is what
    the opacity costs.
    """

    deletions: list[Path] = []
    overwrites: list[Path] = []
    creations: list[Path] = []
    moves: list[Move] = []
    exact: bool = True
    opacity: str = ""
    """Why the footprint could not be enumerated, where it could not be.

    Carried so the wider capture is explicable: "a glob" and "a variable this
    call did not bind" are different reasons to widen, and a person reading an
    audit record needs to know which one they are looking at.
    """

    def touches(self) -> list[Path]:
        """Every path this operation can affect, deduplicated in a stable order."""
        moved = [
            path for move in self.moves for path in (move.source, move.destination)
        ]
        seen = dict.fromkeys(
            [*self.deletions, *self.overwrites, *self.creations, *moved]
        )
        return list(seen)


class NetworkPlan(BaseModel, frozen=True):
    """Where an operation reaches, recorded as evidence rather than policed.

    Lup builds no egress proxy, chases no redirects, and polices no DNS: the
    boundary's own network posture stands, and this is what the classifier
    could see. An operation whose only unusual fact is an unfamiliar
    destination settles like any other operation — contained it runs inside,
    ambient it follows the profile's declaration.
    """

    destinations: list[str] = []
    classified: bool = False
    """Whether every destination is one the profile declared.

    False is not a refusal. It is the fact that makes a destination worth
    recording in the audit trail, and nothing more: external effects keep
    their own rules regardless of where they point, which is why reaching
    GitHub can be routine while a merge still asks.
    """


class ExternalEffectPlan(BaseModel, frozen=True):
    """What an operation does to state or people beyond this machine.

    ``compensable`` is a claim about the remote *state* — that a normal
    follow-up operation restores it — and never about observation. Nothing
    un-sends the mail a closed pull request generated, and saying so is what
    keeps "compensable" from being read as "free".
    """

    effects: list[EffectClass] = []
    destinations: list[str] = []

    def compensable(self) -> bool:
        """Whether every effect here is restored by a normal follow-up.

        Every, not any: an operation that is mostly ordinary collaboration and
        contains one deletion is an operation containing a deletion, and a
        safe outer verb does not erase an unsafe inner one.
        """
        return bool(self.effects) and all(
            effect == "compensable" for effect in self.effects
        )


class Operation(BaseModel, frozen=True):
    """One immutable native call, normalized, with everything it plans to do.

    Immutable because an approval binds to it. A model that could be edited
    after a person answered would make "the exact operation was approved" a
    claim nothing checks — which is the whole content of exact approval.
    """

    id: str
    session: str
    requester: str
    supervisor: str = ""
    tool: str
    payload: JsonObject = {}
    cwd: Path
    worktree: Path
    kind: OperationKind = "unknown"
    placement: SandboxPlacement = "ambient"
    reads: ReadPlan = Field(default=ReadPlan())
    mutations: MutationFootprint = Field(default=MutationFootprint())
    network: NetworkPlan = Field(default=NetworkPlan())
    external: ExternalEffectPlan = Field(default=ExternalEffectPlan())
    nested: list["Operation"] = []
    escalation_raw: str = ""
    escalation_normalized: str = ""
    provider: str = ""

    def fingerprint_parts(self) -> dict[str, JsonValue]:
        """The facts an approval binds to, each under the name a reviewer knows.

        Split out from the digest so a mismatch can say *which* fact moved:
        the coordinator holds the approved operation whole, and comparing
        part by part is what turns "the operation changed" into "its targets
        changed". Each key is the word the refusal sentence uses, so the two
        cannot drift — and each is part of the hashed material, so renaming
        one re-asks every question parked across the rename.
        """
        targets: list[JsonValue] = [
            path.as_posix() for path in self.mutations.touches()
        ]
        reads: list[JsonValue] = [path.as_posix() for path in self.reads.targets]
        nested: list[JsonValue] = [child.fingerprint() for child in self.nested]
        destinations: list[JsonValue] = [*sorted(self.external.destinations)]
        return {
            "tool": self.tool,
            "arguments": self.payload,
            "directory": self.cwd.as_posix(),
            "placement": self.placement,
            "targets": targets,
            "read paths": reads,
            "destinations": destinations,
            "nested operations": nested,
        }

    def fingerprint(self) -> str:
        """What an approval binds to, and what a change of it invalidates.

        Everything a person was shown: the tool, the exact arguments, where it
        runs, the paths it resolved to, and the placement. Not the id — two
        submissions of the same operation should fingerprint alike, because
        the question is whether *this* was approved and not whether this
        submission was.

        Nested operations are included because a safe outer command carrying
        a different inner one is a different operation, which is exactly the
        substitution an approval must not survive.
        """
        material = json.dumps(
            self.fingerprint_parts(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def matches(self, other: "Operation") -> bool:
        """Whether an approval of ``other`` authorizes this exact operation."""
        return self.fingerprint() == other.fingerprint()

    def summary(self) -> str:
        """One line a reviewer reads, which is the operation and not its id.

        Where it acts: the directory it runs from, unless it changes another
        checkout than the one holding that directory -- a session editing a
        sibling worktree -- which is then the one named.
        """
        placed = "" if self.placement == "ambient" else f" [{self.placement}]"
        where = self.cwd if self.cwd.is_relative_to(self.worktree) else self.worktree
        return f"{self.tool}{placed} in {where.as_posix()}"
