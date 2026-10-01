"""The external programs a project needs, declared once and exercised.

A launch that assumes its toolchain is there fails later, inside whatever it
was doing, in that thing's own vocabulary: a missing program reads as
`command not found`, a dead socket as "the daemon is down", a boundary that
refused a write as a broken filesystem. Each of those points somewhere other
than the cause, and each has to be diagnosed by hand before any work starts.

So a requirement is declared, and the declaration carries what is needed, how
to *exercise* it, what to say when the exercise fails, **where** it is
needed, and what absence costs. From that one declaration come the probe, the
sentence a human reads, and the packages an image installs -- so a
requirement cannot be checked without being announced, announced without
being checked, or announced while the image it is checked against was built
from a different list.

Three properties are load-bearing and easy to lose:

**Exercise, not presence.** ``shutil.which(tool) is None`` passes for a
program installed against a socket nobody is serving, for a client redirected
by an environment variable to a path that does not exist, and for a session
whose supplementary groups were fixed before its account joined the group it
needs. The check runs the smallest real operation instead -- evaluate an
expression, ask the daemon what it is, render one page -- because every one
of those failures survives a presence test.

**Where decides who is asked.** Some capabilities belong to the host and must
never reach the image: a container runtime is the clearest, since a container
holding one could start a sibling with the whole host mounted. Others belong
only to the image, and exercising those here would report a perfectly good
machine broken for lacking a toolchain nothing outside a container was going
to run. Checking everything everywhere is how a manifest starts lying.

**Absence is usually a valid answer**, in grades. Most cost a named
capability and are worth saying at every launch. A few are conveniences worth
saying once, to somebody setting a machine up, because a nicety repeated
before every session becomes a line people learn to skip -- along with the
line above it, which mattered. Only a requirement whose absence would leave
something silently untrue refuses to open at all.
"""

import grp
import os
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

import sh
from pydantic import BaseModel, Discriminator, Field, model_validator

from lup.harness.notice import Notice, Urgency
from lup.types import EnvVars


type PackageManager = Literal["pacman", "bun", "uv", "script"]
"""Which ecosystem obtains an installable.

Three that verify what they fetch on their own, and one that has to say how.
``pacman`` takes distribution packages, signed by the distribution and checked
before they unpack; ``bun`` and ``uv`` take a registry name at a pinned
version, whose integrity hash the lockfile records. ``script`` runs a shell
line, which verifies nothing by itself -- so it carries a ``digest``, and what
the build gets is checked against that before it is used.

``script`` stays the last resort rather than a fourth equal option, and the
``package-install-script`` rule still asks about one, because a project
reaching for it is usually reaching past a package that exists. What the digest
changes is the case where none does: a toolchain a distribution has never
packaged is obtainable exactly one way, and an adopter pinning its release and
its checksum has done the verifying the other three managers do for them. That
is a different thing from `curl | sh`, and the declaration tells them apart.
"""


class Package(BaseModel, frozen=True):
    """One installable, and the ecosystem that obtains it.

    A bare name cannot install anything, and a flat list of names hides that
    for as long as nothing consumes the list. What such a list silently
    promises is one ``apt-get install`` line, and measured against a Debian
    base every entry in it is false: ``gh`` is not in the stable archive,
    ``bun`` ships only as an install script, and ``typescript`` is a registry
    package. A declaration that can only be rendered one way, into a line
    that does not work, is worse than no declaration.

    So the manager is part of the declaration rather than an assumption the
    renderer makes -- and the base is chosen to make the honest answer the
    short one. A bare string parses as a distribution package because, on the
    distribution this harness builds from, that is the right answer for every
    package in the toolchain.
    """

    name: str = Field(description="What the manager is asked for")
    manager: PackageManager = Field(
        default="pacman",
        description=(
            "``pacman`` is the base image's own distribution and the answer "
            "wherever it has the package, which is everywhere this "
            "repository's toolchain reaches. ``bun`` takes a JavaScript "
            "registry package and ``uv`` a Python one, both at a pinned "
            "version whose integrity the lockfile records. ``script`` "
            "verifies nothing and is refused by rule"
        ),
    )
    version: str = Field(
        default="",
        description=(
            "Pinned version for a registry package. Empty takes the "
            "registry's current release, which is a decision to re-make on "
            "every build rather than a version to reproduce"
        ),
    )
    command: str = Field(
        default="",
        description="The shell line that installs a ``script`` package",
    )
    digest: str = Field(
        default="",
        description=(
            "What a ``script`` package's download must hash to, which is how "
            "such a package is verified at all. Required for ``script`` and "
            "refused for the rest, whose managers check their own downloads -- "
            "a digest beside a registry name would be a second claim about "
            "integrity that nothing compares against the first"
        ),
    )

    def verified(self) -> bool:
        """Whether what this installs is checked against what was declared.

        The question the roster is actually asked, and the reason it is a
        method rather than a manager comparison at each call site: a caller
        testing ``manager != "script"`` is asking about the ecosystem when it
        means to ask about integrity, and those are two questions the moment
        a script can carry a digest.
        """
        return self.manager != "script" or bool(self.digest)

    def requested(self) -> str:
        """How this package is named to its manager, version included."""
        return f"{self.name}@{self.version}" if self.version else self.name

    @model_validator(mode="before")
    @classmethod
    # lup: ignore[bare-object] — pydantic hands a before-hook whatever the
    # caller wrote, which is the untyped boundary the rule says to narrow at
    def a_bare_name_is_a_distribution_package(cls, value: object) -> object:
        """Accept the common case spelled shortest, as a plain string."""
        return {"name": value} if isinstance(value, str) else value

    @model_validator(mode="after")
    def a_script_carries_the_line_that_runs_it(self) -> "Package":
        """A script package names the line that runs it and the hash to check.

        Caught here rather than at render time, because a package that
        silently installs nothing produces an image missing a tool and an
        error naming that tool -- pointing at the toolchain rather than at
        the declaration that forgot to say how to get it. The digest is caught
        here for the sharper version of the same reason: an unverified install
        produces an image that is *not* missing anything, and nothing later
        asks what it got.
        """
        if self.manager == "script" and not self.command:
            raise ValueError(
                f"package {self.name!r} installs by script but names no command"
            )
        if self.manager == "script" and not self.digest:
            raise ValueError(
                f"package {self.name!r} installs by script but names no digest: "
                "a shell line verifies nothing on its own, so the artifact it "
                "fetches has to be checked against a hash the declaration "
                "pins. Every other manager does this for you"
            )
        if self.manager != "script" and self.digest:
            raise ValueError(
                f"package {self.name!r} names a digest but is obtained by "
                f"{self.manager!r}, which verifies its own downloads: a second "
                "integrity claim nothing compares against the first is a "
                "reassurance rather than a check"
            )
        return self


class Advisory(BaseModel, frozen=True):
    """Absence makes the experience worse and takes nothing away.

    The softest grade, and the one that keeps the other two worth reading. A
    convenience reported at every launch becomes a line people learn to skip,
    and the line above it is the one that mattered -- so this is said where
    somebody is setting a machine up and can act on it, and stays quiet in
    front of the sessions that follow.
    """

    kind: Literal["advisory"] = "advisory"
    improves: str = Field(description="What having it would make nicer")

    def refuses(self) -> bool:
        return False

    def costly(self) -> bool:
        """No. Advice declined is a finished setup, not an unfinished one."""
        return False

    def consequence(self) -> str:
        return f"Optional support for {self.improves} is unavailable."


class LostCapability(BaseModel, frozen=True):
    """Absence costs one named capability and stops nothing else.

    The capability is named rather than described so the sentence a session
    opens with says what the operator no longer has, not merely that
    something is missing: "no container -- multi-worker resolve unavailable"
    is actionable where "docker: missing" is a fact somebody has to interpret.
    """

    kind: Literal["degrades"] = "degrades"
    capability: str = Field(description="What this session cannot do without it")

    def refuses(self) -> bool:
        return False

    def costly(self) -> bool:
        """Yes. Something this machine could do, it now cannot."""
        return True

    def consequence(self) -> str:
        return f"{self.capability} is unavailable"


class MisleadingAbsence(BaseModel, frozen=True):
    """Absence does not read as absence: something answers, and answers wrongly.

    The grade between :class:`LostCapability` and :class:`RefusedLaunch`, and
    the one a session cannot see for itself. A capability lost is a session that
    knows what it cannot do, because reaching for it raises something a
    caller can see. This is the case where reaching for it does not: a
    missing program exits 127, and a shell spends its exit codes on *meaning*
    rather than on availability, so ``cmp -s A B && echo same || echo
    differs`` prints ``differs`` for two byte-identical files on a machine
    carrying no ``cmp``. Nothing is unavailable to whoever reads that line.
    The answer is simply false, in the voice of a true one.

    So it is said in its own words rather than folded into a degradation,
    and what it says is what the absence is *mistaken for* -- because a
    reader who is told a capability is missing goes without it, and a reader
    who is not told keeps acting on what it said.
    """

    kind: Literal["misleads"] = "misleads"
    capability: str = Field(description="What this session cannot do without it")
    mistaken_for: str = Field(
        description=(
            "The answer a caller gets instead of an error, in the caller's "
            "own terms. Stated rather than derived because only the "
            "declaration knows which wrong answer this particular absence "
            "produces, and a generic sentence about failure is exactly the "
            "reading that makes it invisible"
        )
    )

    def refuses(self) -> bool:
        """No. A session told what it is holding can still work around it."""
        return False

    def costly(self) -> bool:
        """Yes, and more so than a plain loss: what is spent is trust."""
        return True

    def consequence(self) -> str:
        return (
            f"{self.capability} is unavailable. "
            f"Failures can be mistaken for {self.mistaken_for}."
        )


class RefusedLaunch(BaseModel, frozen=True):
    """Absence makes a claimed boundary worth less than it says, so nothing opens.

    Reserved for the case where continuing would be *quietly* weaker rather
    than visibly smaller: a rootful daemon, a confinement that did not start.
    A session that opens anyway there is one whose operator believes in a wall
    that is not standing.
    """

    kind: Literal["refuses"] = "refuses"
    because: str = Field(description="What would be silently untrue if this opened")

    def refuses(self) -> bool:
        return True

    def costly(self) -> bool:
        return True

    def consequence(self) -> str:
        return self.because


type Absence = Annotated[
    Advisory | LostCapability | MisleadingAbsence | RefusedLaunch,
    Discriminator("kind"),
]


class EnvironmentRedirect(BaseModel, frozen=True):
    """A variable aiming a client somewhere, named when that place is absent.

    The failure this catches is the one that reads most convincingly as
    something else: a client redirected to a socket that was never created
    reports that it cannot reach the daemon, which sends the reader to start
    a service that had nothing to do with it.
    """

    kind: Literal["environment_redirect"] = "environment_redirect"
    variable: str
    scheme: str = Field(
        default="unix://",
        description="Prefix stripped before the value is read as a path",
    )

    def cause(self, environment: EnvVars, outcome: "ExerciseOutcome") -> str:
        """Why this redirect explains the failure, or nothing when it does not.

        Ungated on what the failure said, unlike its sibling: this one checks
        the redirect target itself, so it only ever speaks when a variable is
        genuinely aiming a client at something absent. That is a cause
        whatever words the client chose to fail in.
        """
        value = environment.get(self.variable, "")
        if not value or not value.startswith(self.scheme):
            return ""
        target = Path(value.removeprefix(self.scheme))
        if target.exists():
            return ""
        return (
            f"{self.variable} points at {target}, which does not exist. "
            f"Unset {self.variable} to use the default socket, or start the "
            "service that provides this socket."
        )


class SupplementaryGroup(BaseModel, frozen=True):
    """Membership the account holds that this process was started without.

    Supplementary groups are captured when a process starts, so joining a
    group leaves every already-running shell outside it. The permission error
    that follows names no group and no remedy, and the remedy is not a
    permission change at all -- it is a new session.
    """

    kind: Literal["supplementary_group"] = "supplementary_group"
    group: str
    when: list[str] = Field(
        default=["permission denied", "Permission denied", "EACCES"],
        description=(
            "Markers that make a group difference the plausible cause. A "
            "default rather than a constant: which words a toolchain uses "
            "for 'you may not' is that toolchain's, not this module's"
        ),
    )

    def cause(self, environment: EnvVars, outcome: "ExerciseOutcome") -> str:
        """Why the group explains *this* failure, or nothing when it does not.

        Gated on the failure looking like a refusal, because a group
        difference is nearly always present and nearly never the cause. Left
        ungated it volunteered "this session is not in the docker group" for
        a daemon reached over a socket the user already owns outright --
        true about the groups, unrelated to the failure, and worse than
        silence, since a reader who acts on it starts a new session and finds
        nothing changed.
        """
        if not any(marker in outcome.detail for marker in self.when):
            return ""
        try:
            entry = grp.getgrnam(self.group)
        except KeyError:
            return f"There is no {self.group} group on this host."
        if environment.get("USER", "") not in entry.gr_mem:  # lup: ignore[dict-get]
            return (
                f"This account is not in the {self.group} group. If that group "
                "owns the socket, join it and log out and back in."
            )
        if entry.gr_gid in os.getgroups():
            return ""
        return (
            f"This account belongs to {self.group}, but the current process "
            "does not have that membership. Log out and back in."
        )


type Diagnosis = Annotated[
    EnvironmentRedirect | SupplementaryGroup, Discriminator("kind")
]


class ExerciseOutcome(BaseModel, frozen=True):
    """What running the smallest real operation established.

    ``detail`` carries the operation's own words either way: on success it is
    what proves the claim, and on failure it is the message a reader would
    otherwise have gone looking for. Neither is thrown away, because the whole
    point of exercising is to have something concrete to show.

    ``exercised`` separates the two ways of not proving a claim, which read
    alike here and send a reader to opposite places. An operation that ran and
    answered no is evidence about the capability; one that never ran is
    evidence about whatever stopped it, and reporting the second as the first
    announces a container that refused to start as a proxy nobody can reach.
    """

    proved: bool
    exercised: bool = True
    detail: str = ""


class Run(BaseModel, frozen=True):
    """The smallest real operation that proves a requirement actually works.

    ``expect`` is what the operation has to have produced. Left empty, a clean
    exit is the whole claim -- right for a program asked its own version, and
    wrong for anything whose failure mode is succeeding at the wrong thing,
    which is what the field exists for.
    """

    kind: Literal["run"] = "run"
    command: list[str] = Field(min_length=1)
    expect: str = Field(
        default="",
        description="Text the output must contain; empty means a clean exit suffices",
    )
    contained: bool = Field(
        default=False,
        description=(
            "Whether this command is prefixed with a container start, which "
            "decides how to read its exit code: an engine that could not "
            "start the container failed before the probe existed, and set by "
            "`behind` because that is the only thing that puts one there"
        ),
    )
    environment: EnvVars = Field(
        default={},
        description=(
            "Variables set over the launcher's own for this command alone: "
            "how a value minted for one launch reaches the probe observing "
            "it without marking the launcher itself"
        ),
    )

    def programs(self) -> list[str]:
        """Which executables this exercise runs, for a reader that has to name them.

        Asked of the exercise rather than read off its fields, so a caller
        never has to test which member of the union it is holding -- and so a
        third shape of exercise answers by writing this rather than by being
        found at every site that enumerated the first two.
        """
        return [self.command[0]]

    def pointed_at(self, program: str) -> "Run":
        """This exercise with its program replaced, arguments untouched.

        Asked of the exercise for the same reason :meth:`programs` is: a
        caller that tested which member of the union it held would be a
        caller a third shape has to be added to. What it is *for* is one
        thing -- a container client is a fact about the machine, and the
        declaration this sits in is hashed into the ownership digest, so the
        program cannot be chosen until the exercise is about to run.
        """
        return self.model_copy(update={"command": [program, *self.command[1:]]})

    def behind(self, opening: list[str]) -> "Run":
        """This exercise carried out inside whatever *opening* starts.

        How an image-side requirement stops being a claim. The command an
        image requirement declares -- ``bun --version``, ``claude -p`` -- is
        the right command, and what it needs is somewhere to run: without
        that, the declaration sits unexercised while its docstring describes
        what it proves. Prefixing the argv a session opens with is the whole
        of the answer, and it matters that it is *that* argv rather than a fresh
        ``run``: a probe assembled separately verifies a container no session
        opens, which is how a boundary passes its own preflight and then
        fails the first session behind it.
        """
        return self.model_copy(
            update={"command": [*opening, *self.command], "contained": True}
        )

    def given(self, facts: "HostFacts") -> "Run":
        """This exercise with anything the declaration could not name filled in.

        Nothing, for a plain run: its command is portable, which is what
        being a plain run means. The method is here rather than only on the
        members that need it so a caller never tests which member it holds --
        the same argument :meth:`programs` makes, applied to the second thing
        a machine has to supply.
        """
        return self

    def run(self) -> ExerciseOutcome:
        """Carry the operation out, answering whether it proved the claim."""
        # lup: ignore[os-environ] — the command inherits this process's environment, with this exercise's own laid over it
        environment = {**os.environ, **self.environment}
        try:
            output = str(
                sh.Command(self.command[0])(*self.command[1:], _env=environment)
            )
        except sh.CommandNotFound:
            return ExerciseOutcome(
                proved=False,
                exercised=not self.contained,
                detail=f"{self.command[0]} is not on PATH",
            )
        except sh.ErrorReturnCode as failure:
            spoken = failure.stderr.decode("utf-8", "replace").strip()
            # 125 is the engine speaking about itself: docker and podman both
            # reserve it for a run that failed before the container's command
            # existed, and spend 126 and 127 on one that could not be invoked
            # or found -- those are answers about the image, and belong to the
            # capability like any other. Read from the code and not the
            # message, which is the engine's prose and moves with its version.
            return ExerciseOutcome(
                proved=False,
                exercised=not (self.contained and failure.exit_code == 125),
                detail=spoken or f"{self.command[0]} exited {failure.exit_code}",
            )
        if self.expect and self.expect not in output:
            return ExerciseOutcome(
                proved=False,
                detail=(
                    f"{' '.join(self.command)} exited successfully, but its "
                    f"output did not contain {self.expect!r}. Output: {output.strip()!r}"
                ),
            )
        return ExerciseOutcome(proved=True, detail=output.strip())


class AnyOf(BaseModel, frozen=True):
    """Several spellings of one capability, where holding any is holding it.

    A clipboard is `xclip` or `xsel` or `wl-copy` or `pbcopy` depending on the
    desktop; a display is X11 or Wayland or neither. Declaring one of them and
    calling its absence the capability's absence is how a machine that has the
    capability gets told it does not -- and the report then names a program the
    operator has no reason to install.
    """

    kind: Literal["any_of"] = "any_of"
    alternatives: list[Run] = Field(min_length=1)

    def programs(self) -> list[str]:
        """Every executable any spelling of this capability would run."""
        return [
            name for candidate in self.alternatives for name in candidate.programs()
        ]

    def pointed_at(self, program: str) -> "AnyOf":
        """Every spelling repointed, which is what holding several means here.

        Nothing declares a client-carried capability this way today. Written
        anyway, because the alternative is the union answering for one member
        and raising for the other, and a shape that answers only sometimes is
        one the caller has to test for -- which is the test this method
        exists to remove.
        """
        return self.model_copy(
            update={
                "alternatives": [
                    candidate.pointed_at(program) for candidate in self.alternatives
                ]
            }
        )

    def behind(self, opening: list[str]) -> "AnyOf":
        """Every spelling carried out inside what *opening* starts."""
        return self.model_copy(
            update={
                "alternatives": [
                    candidate.behind(opening) for candidate in self.alternatives
                ]
            }
        )

    def given(self, facts: "HostFacts") -> "AnyOf":
        """Every spelling given what the declaration could not name."""
        return self.model_copy(
            update={
                "alternatives": [
                    candidate.given(facts) for candidate in self.alternatives
                ]
            }
        )

    def run(self) -> ExerciseOutcome:
        """Take the first spelling that works, or report what each one said."""
        outcomes = [candidate.run() for candidate in self.alternatives]
        proved = next((item for item in outcomes if item.proved), None)
        if proved is not None:
            return proved
        return ExerciseOutcome(
            proved=False,
            # Exercised if any spelling got as far as answering: alternatives
            # that all failed to start share one blocker, and one that ran and
            # said no is evidence about the capability whatever its siblings hit.
            exercised=any(item.exercised for item in outcomes),
            detail="; ".join(
                f"{' '.join(candidate.command)}: {outcome.detail}"
                for candidate, outcome in zip(self.alternatives, outcomes, strict=True)
            ),
        )


class HostFacts(BaseModel, frozen=True):
    """What a machine supplies to an exercise the declaration could not name.

    The counterpart to every "no fact about a machine belongs in a hashed
    declaration" argument in this module, gathered into one object so the
    resolution is one call rather than one per fact. Every field here, hashed,
    is measured moving a generated tree's ownership digest between two
    checkouts of the same commit: the container client, because ``DOCKER_HOST`` decides
    it; and the checkout path, because a worktree is where somebody put it.

    The sentinels join them for the same reason and one stronger: they are
    minted per *launch*, so a declaration carrying one would move the digest
    every time a session opened rather than merely between machines.
    """

    client: str = Field(
        default="docker", description="The container client this machine answered with"
    )
    checkout: Path = Field(
        default=Path(),
        description="Where this checkout sits, for a probe aimed at the real tree",
    )
    inside_sentinel: str = Field(
        default="",
        description=(
            "What a command run inside this launch's boundary must observe. "
            "Minted per launch and injected through the opening argv, so "
            "observing it proves the command ran inside *this* container "
            "rather than inside any container -- which a constant baked into "
            "an image cannot distinguish, and which a variable inherited from "
            "the launching shell asserts for free"
        ),
    )
    host_sentinel: str = Field(
        default="",
        description=(
            "What a command run on the launcher's host must observe. Distinct "
            "from the inside sentinel rather than derived from it: the whole "
            "question a placement probe asks is which side answered, and two "
            "values one can be computed from are one value"
        ),
    )
    read_only_binds: list[Path] = Field(
        default=[],
        description=(
            "Every path this launch's lease binds read-only, at its own path "
            "inside. A lease fact rather than a declaration: which worktrees "
            "and repositories it covers is this machine's, decided at launch"
        ),
    )


class BindProbe(BaseModel, frozen=True):
    """Ask the container's mount table whether every read-only bind is there.

    The lease is a list of paths the engine was asked to bind read-only, and
    nothing about asking proves the engine did -- or that a bind is still in
    place. A read-only *file* bind is detached by the kernel the moment the
    host renames over the file it sits on, which is how git rewrites
    `config`; what is left is a writable file with nothing saying so. So the
    probe reads the table rather than trusting the argv: each path must be a
    mount point, mounted read-only.

    `findmnt --mountpoint P --options ro` rather than a pass over
    `/proc/self/mountinfo`, because that file escapes a space in a path as
    `\\040` and a hand-rolled comparison would report every such path
    missing, and because the exit status answers both halves -- a mount
    point, read-only -- with no option list taken apart. A shape, like
    :class:`SentinelProbe`: the paths are this launch's, so :class:`HostFacts`
    carries them in and :meth:`given` aims it.
    """

    kind: Literal["bind_probe"] = "bind_probe"
    marker: str = Field(
        default="every read-only bind held",
        description="What the probe prints when every bind is in place",
    )

    def resolved(self, facts: HostFacts) -> Run:
        """This probe as portable shell over this launch's read-only binds.

        The missing paths go to stderr and the marker to stdout, the split
        :meth:`Run.run` reads, so a failure's detail names every bind that is
        not there rather than the first.
        """
        binds = " ".join(shlex.quote(str(path)) for path in facts.read_only_binds)
        script = (
            'missing=""; '
            f"for bind in {binds}; do "
            'findmnt --mountpoint "$bind" --options ro >/dev/null 2>&1 '
            '|| missing="$missing $bind"; '
            "done; "
            'if [ -n "$missing" ]; then '
            'printf "Not bound read-only:%s" "$missing" >&2; exit 1; fi; '
            f'printf "{self.marker}"'
        )
        return Run(command=["sh", "-c", script], expect=self.marker)

    def programs(self) -> list[str]:
        """The shell and the table reader, which is all this runs."""
        return ["sh", "findmnt"]

    def pointed_at(self, program: str) -> "BindProbe":
        """Unchanged: what varies here is the lease, not the program."""
        return self

    def behind(self, opening: list[str]) -> "BindProbe":
        """Unchanged: :meth:`given` renders the ``Run`` that *opening* prefixes."""
        return self

    def given(self, facts: HostFacts) -> Run:
        """The resolved command, aimed at this launch's binds."""
        return self.resolved(facts)

    def run(self) -> ExerciseOutcome:
        """Refuse rather than pass, because no lease has aimed it yet.

        The answer :class:`SentinelProbe` gives and for its reason: an unaimed
        probe has checked nothing, and a pass here would vouch for binds no
        launch declared.
        """
        return ExerciseOutcome(
            proved=False,
            exercised=False,
            detail=(
                "this bind probe was never given a lease to check, so it "
                "tested nothing — exercise it through `for_host`"
            ),
        )


class MountProbe(BaseModel, frozen=True):
    """Read a file back through a bind mount of a directory at its own path.

    The prerequisite the worktree rail rests on, as a shape rather than as a
    spelled-out command. Every part of that command that differs between two
    machines -- which client, which directory -- is supplied by
    :class:`HostFacts` when the probe is about to run, and what is declared
    here is only what is the same everywhere: the throwaway image, and the
    name of a file the checkout is known to contain.

    Spelled out, this requirement would put an absolute path into a
    declaration the ownership digest hashes, and the digest would then move
    between two worktrees of one commit -- so every checkout but the one that
    last generated would read its own committed tree as stale, for a fact
    about where somebody put it.

    Why a *read* rather than a presence check: asking ``test -d`` about the
    mounted directory answers false on rootless podman for every worktree
    this rail leases, which reads exactly like an absent mount and is not
    one. And a container creates an empty directory at any mount target it is
    given, so the check can pass with no mount having happened at all.
    Reading a file across the boundary can do neither.
    """

    kind: Literal["mount_probe"] = "mount_probe"
    image: str = Field(
        default="docker.io/library/busybox:latest",
        description="A throwaway image with a shell, for reading one file",
    )
    witness: str = Field(
        default="pyproject.toml",
        description="A file the probed directory is known to hold",
    )

    def resolved(self, facts: HostFacts) -> Run:
        """This probe as the command a machine runs, host facts filled in.

        The read is discarded rather than returned. What proves the mount is
        that the read *succeeded*, and the bytes are a whole file: they end
        up as the finding's detail, which is what every report of this
        capability then carries and what one of them printed -- a launch
        opening under an entire copy of ``pyproject.toml``.
        """
        mount = f"{facts.checkout}:{facts.checkout}:ro"
        return Run(
            command=[
                facts.client,
                "run",
                "--rm",
                "-v",
                mount,
                self.image,
                "sh",
                "-c",
                f"cat {facts.checkout / self.witness} >/dev/null",
            ]
        )

    def programs(self) -> list[str]:
        """The client, which is the one executable this runs on the host."""
        return ["docker"]

    def pointed_at(self, program: str) -> "MountProbe":
        """Unchanged: which client runs this arrives through :class:`HostFacts`.

        Answered rather than omitted so the union stays uniform -- a caller
        that had to know this member ignores repointing is a caller testing
        which member it holds.
        """
        return self

    def behind(self, opening: list[str]) -> "MountProbe":
        """Unchanged: this probe *is* a container start, so it opens its own."""
        return self

    def given(self, facts: HostFacts) -> Run:
        """The resolved command, which is what a host fact turns this into."""
        return self.resolved(facts)

    def run(self) -> ExerciseOutcome:
        """Refuse rather than pass, because nothing has aimed this yet.

        An unresolved probe has no directory to read and no client to read it
        with. Reporting that plainly is the only honest answer: a silent
        success here would vouch for the mount topology the whole worktree
        rail stands on, having tested nothing.
        """
        return ExerciseOutcome(
            proved=False,
            # Unexercised by the field's plain meaning: an unaimed probe ran
            # nothing, so it is evidence about this wiring and not about the
            # mount topology it would have read.
            exercised=False,
            detail=(
                "this mount probe was never given a checkout to aim at, so it "
                "tested nothing — exercise it through `for_host`"
            ),
        )


type SentinelSide = Literal["inside", "host"]
"""Which of a launch's two minted values a probe has to observe.

Two rather than reusing :data:`Side`, whose third value is ``both``: a
requirement can be satisfied on either side of the image boundary, and a
placement probe asking "either" is asking nothing at all.
"""


SENTINEL_VARIABLE = "LUP_BOUNDARY_SENTINEL"
"""Where a probe looks for the value its side of the boundary was given.

One variable rather than two, because which value it holds *is* the answer: a
command reporting the inside value ran inside, one reporting the host value ran
on the host, and one reporting neither ran somewhere nobody arranged.
"""


class SentinelProbe(BaseModel, frozen=True):
    """Ask a command which side of the boundary it is on, and make it prove it.

    A placement is the one claim a presence test cannot check. ``LUP_CONTAINED``
    is a constant an image bakes, so it answers "yes" for any container built
    from that image, for a bare ``run`` that has none of the lease, and -- since
    a launcher forwards its own environment -- for an uncontained session
    started from a shell that happened to export it. Each of those is a session
    reporting a boundary nothing put under it, which is the failure
    :class:`~lup.policy.boundary.CapabilityEvidence` exists to name.

    A value minted per launch answers instead which *this* launch's boundary
    is, because nothing but this launch's opening argv carries it. What the
    probe is aimed with therefore cannot be declared: the ownership digest
    hashes this model, and a sentinel written here would move it every time a
    session opened. So this is a shape, and :class:`HostFacts` fills it in --
    the same arrangement, and for the same reason, as :class:`MountProbe`.

    ``witness`` is the second half, and it is here rather than in a probe of
    its own because this one already runs behind the argv a session opens
    with. Reading a file back at its own absolute path is what proves the
    same-path mount the worktree rail stands on, and the requirement that
    declares that today is exercised at setup against a container no session
    opens. Folded in here it is measured at launch, in the session's own
    container, for no extra container start.
    """

    kind: Literal["sentinel_probe"] = "sentinel_probe"
    variable: str = Field(
        default=SENTINEL_VARIABLE,
        description="The variable a launch injects its minted value into",
    )
    side: SentinelSide = Field(
        default="inside",
        description="Which of the launch's two minted values this asks for",
    )
    witness: str = Field(
        default="",
        description=(
            "A file the checkout is known to hold, read back at its own "
            "absolute path. Empty asks for the sentinel alone, which is the "
            "right probe for a side that has no mount to prove"
        ),
    )

    def sentinel(self, facts: HostFacts) -> str:
        """Which minted value this side has to observe."""
        return facts.host_sentinel if self.side == "host" else facts.inside_sentinel

    def resolved(self, facts: HostFacts) -> Run:
        """This probe as the command a machine runs, launch facts filled in.

        Printed with a marker around it rather than bare, because a sentinel
        is hex and ``expect`` asks only for containment: an output that
        happened to carry the value in some other field would satisfy a bare
        comparison. The witness is read in the same shell so a mount that is
        not there fails the exercise instead of being reported beside it, and
        discarded once read: what proves the mount is the exit status, where
        the bytes are a whole file that would otherwise become this finding's
        detail, printing ``pyproject.toml`` into every contained launch's
        opening block under the sentinel it is there to report.
        """
        read = (
            f" && cat {facts.checkout / self.witness} >/dev/null"
            if self.witness
            else ""
        )
        return Run(
            command=[
                "sh",
                "-c",
                f'printf "sentinel=%s" "${self.variable}"{read}',
            ],
            expect=f"sentinel={self.sentinel(facts)}",
            environment=(
                {self.variable: facts.host_sentinel} if self.side == "host" else {}
            ),
        )

    def programs(self) -> list[str]:
        """The shell, which is all this runs before a launch aims it."""
        return ["sh"]

    def pointed_at(self, program: str) -> "SentinelProbe":
        """Unchanged: what varies here is the minted value, not the program."""
        return self

    def behind(self, opening: list[str]) -> "SentinelProbe":
        """Unchanged: this is aimed before it is placed.

        Answered rather than omitted so the union stays uniform, and
        deliberately a no-op: :meth:`given` turns this into a :class:`Run`,
        and it is that ``Run`` the opening argv prefixes. Prefixing here would
        put the container start in front of a command with no sentinel in it
        yet.
        """
        return self

    def given(self, facts: HostFacts) -> Run:
        """The resolved command, which is what a launch fact turns this into."""
        return self.resolved(facts)

    def run(self) -> ExerciseOutcome:
        """Refuse rather than pass, because nothing has minted a value yet.

        The same answer :meth:`MountProbe.run` gives and for the same reason:
        an unaimed probe has tested nothing, and a silent success here would
        vouch for the placement every other verdict in the policy is read
        against.
        """
        return ExerciseOutcome(
            proved=False,
            exercised=False,
            detail=(
                "this sentinel probe was never given a launch to aim at, so it "
                "tested nothing — exercise it through `for_host`"
            ),
        )


class VocabularyProbe(BaseModel, frozen=True):
    """Ask an environment for every program a permission policy promised it.

    The one requirement whose subject is a *list* rather than a capability,
    and it is here because the two halves it joins are each correct alone
    and wrong together. A shell vocabulary declares which commands an agent
    may run unattended; an image declares which packages it installs. Left
    uncompared, a word can be declared safe and never carried, and what that
    produces is not a refusal an agent can read -- with ``diff`` and ``cmp``
    declared and absent, the ordinary comparison idiom answers ``DIFFERS``
    for two byte-identical files, because the ``||`` arm cannot tell "they
    differ" from "that program was never here".

    Asked as one probe over the whole list rather than one requirement per
    word, because the finding a reader wants is the *set*: eight absences
    reported as eight lines is eight capabilities to weigh, and reported as
    one line naming eight programs is a single fact about an image that was
    built from a stale list.

    ``command -v`` is presence, and this module argues against presence
    everywhere else. The argument holds where a program is the visible half
    of something larger -- a client with no daemon, a binary with no group --
    and these words are not that: a coreutil on ``PATH`` is the whole
    capability. What matters is *where* the question is asked, and it is
    asked in the environment itself, through the argv a session opens with,
    rather than by a launcher consulting its own ``PATH`` on behalf of a
    container it is not inside.
    """

    kind: Literal["vocabulary_probe"] = "vocabulary_probe"
    vocabulary: list[str] = Field(
        min_length=1,
        description=(
            "Programs the permission policy declares safe to run unattended, "
            "which is the promise this probe holds the environment to"
        ),
    )
    marker: str = Field(
        default="every declared program answered",
        description="What the probe prints when nothing is missing",
    )

    def resolved(self) -> Run:
        """This probe as the command an environment runs, in portable shell.

        The absent names go to stderr and the marker to stdout, because that
        is the split :meth:`Run.run` reads: a clean exit is proved by the
        marker, and a failing one carries stderr into the finding's detail.
        Printed to stdout, the one thing a reader needs -- which words were
        missing -- would be discarded at the moment it became true.
        """
        words = " ".join(self.vocabulary)
        script = (
            'absent=""; '
            f"for word in {words}; do "
            'command -v "$word" >/dev/null 2>&1 || absent="$absent $word"; '
            "done; "
            'if [ -n "$absent" ]; then '
            'printf "Missing commands:%s" '
            '"$absent" >&2; exit 1; fi; '
            f'printf "{self.marker}"'
        )
        return Run(command=["sh", "-c", script], expect=self.marker)

    def programs(self) -> list[str]:
        """Every word this asks for, which is what a reader has to be told."""
        return list(self.vocabulary)

    def pointed_at(self, program: str) -> "VocabularyProbe":
        """Unchanged: the shell that asks is not a fact about any machine."""
        return self

    def behind(self, opening: list[str]) -> "VocabularyProbe":
        """Unchanged: :meth:`given` renders the ``Run`` that *opening* prefixes."""
        return self

    def given(self, facts: HostFacts) -> Run:
        """The rendered command, which needs no host fact to be complete."""
        return self.resolved()

    def run(self) -> ExerciseOutcome:
        """Ask here, which is a real answer -- unlike its sibling probes.

        A mount and a sentinel are shapes until a launch aims them, so both
        refuse to answer unaimed. This one is whole as declared: the words
        come from the policy and the shell is wherever this runs, so the host
        roster gets a true reading for free and only the image roster needs
        the opening argv.
        """
        return self.resolved().run()


type Exercise = Annotated[
    Run | AnyOf | MountProbe | SentinelProbe | VocabularyProbe | BindProbe,
    Discriminator("kind"),
]


type Location = Literal["host", "image"]


type Side = Literal["host", "image", "both", "session"]
"""Which side of the boundary is expected to satisfy a requirement."""


class Requirement(BaseModel, frozen=True):
    """One external program or condition, with the cost of not having it."""

    capability: str = Field(description="Short handle, e.g. 'docker' or 'clipboard'")
    purpose: str = Field(description="What this project uses it for")
    where: Side = Field(
        default="host",
        description=(
            "Which side needs this. A host requirement is exercised here and "
            "never installed into an image -- a container runtime is the "
            "clearest case, since a container holding one could start a "
            "sibling with the whole host mounted. An image requirement is "
            "the reverse: the host is not expected to have it, so exercising "
            "it here would report a machine broken for lacking something it "
            "was never meant to carry. A session requirement is checked where "
            "the agent runs: in the container for a contained launch, on the "
            "host otherwise"
        ),
    )
    checked: Literal["always", "setup"] = Field(
        default="always",
        description=(
            "How often this is exercised, which is a fact about the probe's "
            "cost and not about the requirement's importance. `setup` is for "
            "an exercise too slow to pay before every session -- one that "
            "starts a container, reaches a network, or builds something -- "
            "and says nothing about what absence costs, which `absence` "
            "answers on its own. Conflating the two made an expensive check "
            "of an important thing inexpressible"
        ),
    )
    at_launch: bool = Field(
        default=False,
        description=(
            "Whether a contained launch pays a container start to verify "
            "this on the way in. A separate field from ``checked`` because "
            "``checked`` is about an exercise's cost and the same exercise "
            "costs differently on each side: ``uv --version`` is free on the "
            "host and a container start inside, so one field marking it "
            "cheap reads as cheap in both places -- and a launch roster "
            "read that way grows to six container starts, a ``bunx tsc`` "
            "and a ``gh auth status`` among them. False by default, so "
            "nothing costs a launch anything unless it says so, and what "
            "says so is the boundary: the part whose failure is invisible "
            "from outside and leaves the session unable to do anything"
        ),
    )
    exercise: Exercise
    absence: Absence
    recovery: str = Field(default="", description="The next step after a failed check")
    recovery_by_location: dict[Location, str] = Field(
        default={}, description="Recovery specific to the environment actually checked"
    )
    diagnoses: list[Diagnosis] = Field(
        default=[],
        description=(
            "Causes to look for when the exercise fails, in the order a "
            "reader should hear them. Each is a failure whose own error "
            "message points somewhere else"
        ),
    )
    install: list[Package] = Field(
        default=[],
        description=(
            "Packages that satisfy this *inside a container "
            "image built from this harness*, when a package manager is how "
            "it is obtained there. Empty is the common answer and means one "
            "of two things: the base image already ships it, or the "
            "capability is the host's and the container deliberately does "
            "not have it"
        ),
    )

    by_client: bool = Field(
        default=False,
        description=(
            "Whether the host's container client carries this exercise out. "
            "Which client that is stays out of the declaration deliberately: "
            "the ownership digest hashes this model, so a probe of the host "
            "in here reports generated artifacts as stale on any machine "
            "whose client differs -- measured moving twice on one machine, "
            "minutes apart, when a stale pid file was cleaned up between the "
            "runs. The exercise names the portable spelling and "
            "`lup.harness.toolchain.for_host` points it at what answered"
        ),
    )

    def check(self, environment: EnvVars, location: Location = "host") -> "Finding":
        """Exercise this requirement and say what was found, in whose words."""
        outcome = self.exercise.run()
        if outcome.proved:
            return Finding(
                requirement=self, working=True, detail=outcome.detail, location=location
            )
        # Only when something was actually exercised. A diagnosis explains why
        # this capability failed, and an operation that never ran did not fail
        # for any of the reasons they look for -- so asking them here would
        # dress the wrong failure in a confident cause.
        causes = (
            [
                found
                for found in (
                    probe.cause(environment, outcome) for probe in self.diagnoses
                )
                if found
            ]
            if outcome.exercised
            else []
        )
        return Finding(
            requirement=self,
            working=False,
            exercised=outcome.exercised,
            detail=outcome.detail,
            causes=causes,
            location=location,
        )


class Finding(BaseModel, frozen=True):
    """What one requirement's exercise established, ready to be read aloud."""

    requirement: Requirement
    working: bool
    exercised: bool = True
    detail: str = ""
    causes: list[str] = []
    location: Location | None = Field(
        default=None,
        description="Where the check ran, independent of where it is required",
    )

    def refuses(self) -> bool:
        """Whether this finding is one a launch must not continue past."""
        return not self.working and self.requirement.absence.refuses()

    def notices(self) -> list[Notice]:
        """Report the environment, evidence, effect, and next step separately.

        A check that could not start establishes no loss of capability and
        offers no capability-specific repair. Healthy checks appear only in
        an explicit report; launches use :meth:`alarms`.
        """
        label = self.requirement.capability
        if self.location is not None:
            label += " (container)" if self.location == "image" else " (host)"
        if self.working:
            return [Notice(text=f"{label}: working", urgency="ready")]
        urgency: Urgency = "refusal" if self.refuses() else "warning"
        if not self.exercised:
            return [
                Notice(
                    text=f"{label}: check could not run",
                    urgency=urgency,
                ),
                Notice(text=self.detail, urgency=urgency, indent=1),
                Notice(
                    text=(
                        "Result unknown. Resolve the error above and rerun the check."
                    ),
                    urgency="detail",
                    indent=1,
                ),
            ]
        consequence = self.requirement.absence.consequence()
        recovery = self.requirement.recovery
        if self.location is not None:
            recovery = self.requirement.recovery_by_location.get(
                self.location, recovery
            )
        return [
            Notice(
                text=f"{label}: {'launch blocked' if self.refuses() else 'check failed'}",
                urgency=urgency,
            ),
            *[
                Notice(text=cause, urgency=urgency, indent=1)
                for cause in [self.detail, *self.causes]
                if cause
            ],
            Notice(
                text=f"Impact: {consequence}",
                urgency="detail",
                indent=1,
            ),
            *(
                [Notice(text=f"Fix: {recovery}", urgency=urgency, indent=1)]
                if recovery
                else []
            ),
        ]

    def alarms(self) -> list[Notice]:
        """Only what a reader has to act on, which is nothing when it works.

        What a roster exercised on the way to somewhere else says. A launch
        asked nobody, so a line per healthy capability is a block that grows
        with the roster, is identical every session, and is what the one
        absence beside it has to be picked out of -- the module's own
        argument about a convenience repeated before every session, applied
        to the whole roster rather than to one entry of it.

        An absence still speaks in full. It is the fact the agent inside
        cannot discover except by failing at it, and the count that replaces
        the rest is no substitute for it.
        """
        return [] if self.working else self.notices()


class Manifest(BaseModel, frozen=True):
    """Every requirement a project declares, asked all at once.

    Held as one object rather than a loose list so the launch, the standalone
    command, and the image declaration read the same roster: a requirement
    added for one of them is a requirement all three see.
    """

    requirements: list[Requirement] = []

    @classmethod
    def across(cls, manifests: Sequence["Manifest"]) -> "Manifest":
        """One roster from several, keeping the first declaration of a capability.

        What a *host* is expected to satisfy is a fact about the machine, not
        about the target being launched, so a command spanning every target
        holds several manifests answering the same question. Exercised one
        manifest at a time, the machine is asked everything once per target:
        the reader gets the roster twice with nothing saying why, and a probe
        that starts a container is paid for twice to establish what the first
        one already had.

        Keyed on the capability rather than on the whole requirement, because
        that is the name the report prints and so the one a reader would see
        repeated. Two targets declaring one capability with different
        exercises is a disagreement between declarations, and exercising both
        would report a machine that has and has not got it -- so the first
        wins here, and the disagreement stays where it was written rather
        than becoming two lines that contradict each other.
        """
        declared = [item for manifest in manifests for item in manifest.requirements]
        return cls(
            requirements=[
                item
                for index, item in enumerate(declared)
                if item.capability
                not in [prior.capability for prior in declared[:index]]
            ]
        )

    def on_the_host(
        self, setting_up: bool = False, contained: bool = False
    ) -> list[Requirement]:
        """The requirements this machine is expected to satisfy itself.

        A contained launch checks session tools in the container instead.
        Otherwise this roster includes them for a session running on the host.

        Image-side entries are excluded outright rather than exercised and
        forgiven: a host without `bun` is not a host with a problem when
        nothing outside the container was ever going to run it, and a line
        saying otherwise is the false report this whole module exists to
        stop.

        *setting_up* widens the answer to everything checked only at setup --
        the conveniences, and the exercises too slow to pay for before every
        session. Both are things somebody configuring a machine wants to hear
        once and a session opening for the hundredth time does not.
        """
        return [
            item
            for item in self.requirements
            if (
                item.where in ("host", "both")
                or (item.where == "session" and not contained)
            )
            and (setting_up or item.checked == "always")
        ]

    def inside_the_image(self, setting_up: bool = True) -> list[Requirement]:
        """The requirements the container is expected to satisfy, not this machine.

        The other half of :meth:`on_the_host`. An image-side entry is excluded
        from the host roster -- correctly, since a laptop without ``bun`` is
        not a laptop with a problem -- and excluded alone it would stop
        there: declared, rendered into a package list, and never once
        exercised, leaving a manifest whose image half is a claim, with each
        entry's docstring describing a proof nothing performed.

        *setting_up* is off for a launch, which narrows this to the entries
        that asked to be verified there. Every entry in this roster costs a
        container start -- there is no cheap one -- so what a launch pays for
        is named rather than inferred: the boundary, whose failure is
        invisible from outside and leaves the session unable to do anything.
        A toolchain version and a model call are what somebody setting a
        machine up hears once.
        """
        return [
            item
            for item in self.requirements
            if item.where in ("image", "both", "session")
            and (setting_up or item.at_launch)
        ]

    def check(
        self, environment: EnvVars, setting_up: bool = False, contained: bool = False
    ) -> list["Finding"]:
        """Exercise every host-side requirement, in declaration order."""
        return [
            item.check(environment, location="host")
            for item in self.on_the_host(setting_up, contained=contained)
        ]

    def inside_signatures(self, setting_up: bool = True) -> list[str]:
        """What each image-side check asks, as one string per requirement.

        The capability and the exercise as declared, before any launch aims
        or places it. Two runtimes composed against one image declare the
        same ``uv`` check and different session probes, and this is the
        reading that tells those apart: the argv each runtime opens with
        differs, so the placed exercise never compares equal, while the
        declared one does exactly where the question is the same.
        """
        return [
            f"{item.capability}\t{item.exercise.model_dump_json()}"
            for item in self.inside_the_image(setting_up)
        ]

    def check_inside(
        self,
        environment: EnvVars,
        opening: list[str],
        setting_up: bool = True,
        facts: HostFacts = HostFacts(),
        skipped: Sequence[str] = (),
    ) -> list["Finding"]:
        """Exercise every image-side requirement inside what *opening* starts.

        *opening* is the argv a session opens with, minus the CLI it would
        have run. Passed in rather than built here because assembling it
        needs the launcher's whole world -- the lease, the credential, the
        engine, the network -- and a manifest that reached for those would be
        a manifest only a launcher could hold.

        *facts* is aimed before the exercise is placed, and the order is the
        whole of it: a placement probe is a shape until a launch mints it a
        value, and prefixing the opening argv onto an unaimed shape would put
        a container start in front of a command asking for a variable nothing
        had set. Every other shape answers ``given`` with itself, so the extra
        call costs the rest of the roster nothing.

        *skipped* names, by :meth:`inside_signatures`, the checks another
        composition over the same image already exercised. Every entry costs
        a container start, and a second runtime asking the image whether
        ``uv`` works is asking the same question of the same image.
        """
        return [
            item.model_copy(
                update={"exercise": item.exercise.given(facts).behind(opening)}
            ).check(environment, location="image")
            for item, signature in zip(
                self.inside_the_image(setting_up), self.inside_signatures(setting_up)
            )
            if signature not in skipped
        ]

    def packages(self) -> list[Package]:
        """Everything an image installs to satisfy this manifest, deduplicated.

        Ordered by first declaration rather than sorted: an image layer reads
        better grouped by what asked for each package, and a stable order is
        what keeps a rebuild from invalidating the layer for no reason.

        For an image, and not for a CI runner, which is a distinct audience
        however alike the two lists look. Fed straight into a workflow's
        system packages, this generates ``apt-get install -y uv``, which no
        Ubuntu runner can satisfy and which that workflow already solves with
        a setup action -- and it installs a container runtime the image must
        never carry.
        A requirement is one thing; where each place gets it is another.

        Each entry carries the manager that obtains it, because the three
        this repository declares need three different ones and a list of
        bare names can only ever be rendered as one.
        """
        return list(
            dict.fromkeys(
                package
                for item in self.requirements
                if item.where in ("image", "both", "session")
                for package in item.install
            )
        )


def refused(findings: Sequence[Finding]) -> list[Finding]:
    """The findings that stop a launch, which is usually none of them."""
    return [finding for finding in findings if finding.refuses()]
