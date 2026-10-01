"""What the operator's terminal is, carried across into the container.

A container inherits none of the terminal it was opened from. The engine sets
``TERM`` to a placeholder of its own and leaves every other description unset,
so a session that ran in the operator's terminal a moment ago now runs in a
generic one: colour collapses to sixteen, and every variable naming a program
the operator prefers is simply absent.

That is a boundary nobody chose. The filesystem is scoped deliberately, the
network is scoped deliberately, and the *description of the screen* is scoped
by accident -- it crosses no secret and grants no reach, and the session is
worse for every part of it that is missing. So it is declared and carried,
rather than left to whatever the engine defaults to.

**Named here, valued at launch.** Which variables cross is a declaration, the
same for every adopter, and it is hashed into the ownership digest. What they
*contain* is a fact about the machine in front of you -- ``TERM`` is that
operator's terminal emulator and ``EDITOR`` is that operator's habit. Folding
the second into the first is the trap :func:`~lup.harness.toolchain.for_host`
exists for: folded in, the values move a generated tree's digest between two
checkouts of one commit. So this module holds names, and
:meth:`TerminalHandoff.for_host` is where a machine answers them.

**Some variables name a thing the image has to have.** ``EDITOR`` is not a
description, it is a command the session will try to run; ``LANG`` is not a
description either -- it names a compiled locale, and glibc answers a name it
cannot find by falling back to ASCII and complaining once per program; and
``TZ`` names a zone in a database. In each case forwarding blind turns a
missing feature into a broken one, and the operator sees an editor fail to
open or a screenful of ``setlocale`` warnings rather than learning what the
image was not carrying.

The answer for all three is the same, and it is to *carry more*, not to
forward less: the image installs the editors and the zone database and
generates the locales this declares, so an operator's own variable crosses
unchanged. Substitution is what happens when it names something outside that
list, and it is said out loud with the one-line widening that would carry it.

**And one of them the host does not keep in a variable at all.** A Linux
machine keeps its zone in ``/etc/localtime`` and exports ``TZ`` for nobody, so
a handoff that carried the variable by name carried an absence: the
declaration read as satisfied and every contained session ran in UTC.
:func:`host_timezone` is where that one is read instead, which is why a group
here can be answered from somewhere other than the environment.
"""

import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from tzlocal import get_localzone_name

from lup.harness.notice import Notice
from lup.harness.requirements import Package
from lup.types import EnvVars


def host_timezone() -> str:
    """The IANA zone this machine keeps, or ``""`` where it names none.

    Read here rather than taken from the environment, because on the machines
    this launches from the environment does not hold it: a Linux host keeps
    its zone in ``/etc/localtime`` and exports ``TZ`` for nobody, so a handoff
    that only forwards the variable forwards an absence: ``TZ`` declared as
    crossing, the operator's shell never setting it, and every contained
    session running in UTC with the declaration looking satisfied.

    Delegated to ``tzlocal``, which reads the source each platform actually
    keeps it in -- ``/etc/localtime`` on Linux, the registry on Windows,
    system preferences on macOS -- rather than string-munging the one this
    machine happens to be.
    """
    try:
        return get_localzone_name()
    except ZoneInfoNotFoundError:
        return ""


class CarriedEditor(BaseModel, frozen=True):
    """One editor an image carries, and the package that puts it there.

    Two fields rather than one because the two names differ often enough that
    assuming they match would be wrong for half this list: an ``EDITOR`` of
    ``nvim`` is satisfied by the ``neovim`` package and an ``emacs`` by
    ``emacs-nox``. Matching a host's ``EDITOR`` against package names would
    refuse an editor the image is carrying, and installing the command name
    would ask the distribution for a package that does not exist.
    """

    command: str = Field(
        description="What an ``EDITOR`` variable spells, e.g. ``nvim``"
    )
    package: Package = Field(
        description="What installs it in the image, e.g. ``neovim``"
    )


class GeneratedLocale(BaseModel, frozen=True):
    """One locale an image compiles, named the two ways ``locale-gen`` needs.

    Two fields rather than one, because ``/etc/locale.gen`` takes a name and a
    charmap as separate words and the second is not reliably the first's
    suffix: ``en_US.UTF-8`` pairs with ``UTF-8`` but ``en_US`` pairs with
    ``ISO-8859-1`` and ``ja_JP.EUC-JP`` with ``EUC-JP``. Deriving the charmap
    by cutting the name at its dot is right for the common case and silently
    wrong for the rest -- it produces a ``locale.gen`` line the generator
    skips, so the locale is simply absent and the image reports nothing.

    Declaring both is also what keeps this out of :meth:`str.split`, which is
    the same argument the anti-pattern rule makes: the pair is structured
    data, and structure that was written down does not have to be recovered.
    """

    name: str = Field(description="What ``LANG`` would be set to, e.g. ``en_US.UTF-8``")
    charmap: str = Field(
        default="UTF-8",
        description=(
            "The encoding ``locale-gen`` compiles it against. ``UTF-8`` by "
            "default because a terminal session wants no other, and an image "
            "generating a legacy charmap is answering a host this handoff "
            "would rather have talked out of it"
        ),
    )

    def line(self) -> str:
        """This locale as ``/etc/locale.gen`` spells it."""
        return f"{self.name} {self.charmap}"


class Substitution(BaseModel, frozen=True):
    """A variable the host set to something this image does not carry.

    Carried rather than applied silently, because the substituted thing works.
    An operator whose ``EDITOR`` was quietly swapped gets an editor, and an
    operator whose ``LANG`` was quietly swapped gets a locale; the only
    evidence either way is that it is not the one they asked for, which is
    exactly the evidence nobody goes looking for.
    """

    variable: str = Field(description="Which variable was answered, e.g. ``EDITOR``")
    asked: str = Field(description="What the host had it set to")
    instead: str = Field(description="What the session got")
    widen: str = Field(
        description="The one-line declaration change that would carry the asked-for one"
    )

    def notices(self) -> list[Notice]:
        """This substitution as an operator reads it: what, and how to undo it."""
        return [
            Notice(
                text=(
                    f"{self.variable}: this image does not carry "
                    f"{self.asked}, so the session gets {self.instead}."
                ),
                urgency="warning",
            ),
            Notice(text=f"Carry it with {self.widen}.", urgency="detail", indent=1),
        ]


class TerminalResolution(BaseModel, frozen=True):
    """One machine's answer to a handoff: what crosses, and what was swapped."""

    environment: EnvVars = Field(
        default={}, description="What the container is started with"
    )
    substitutions: list[Substitution] = Field(
        default=[], description="Every variable the image could not answer as asked"
    )

    def notices(self) -> list[Notice]:
        """What a launch says about the terminal the session is about to get.

        Silent when nothing was substituted. A handoff that worked is not news
        -- it is the session behaving as the terminal it was opened from --
        and a line saying so at every launch is a line that trains the eye to
        skip the block it sits in.
        """
        return [notice for item in self.substitutions for notice in item.notices()]


class TerminalHandoff(BaseModel, frozen=True):
    """Which facts about the operator's terminal reach the session inside.

    Every field here is a *name*. Nothing in this model differs between two
    machines, which is what lets it sit in a hashed declaration; the values
    arrive from :meth:`for_host`, at the one place a launch knows them.
    """

    described_by: list[str] = Field(
        default=[
            "TERM",
            "COLORTERM",
            "CLAUDE_CODE_TMUX_TRUECOLOR",
            "TMUX",
            "STY",
            "ZELLIJ",
            "TERM_PROGRAM",
            "LC_TERMINAL",
            "VTE_VERSION",
        ],
        description=(
            "Variables carried across verbatim, because their value is a "
            "description that names nothing the image has to hold. ``TERM`` "
            "and ``COLORTERM`` are the pair that decides colour depth: an "
            "engine sets ``TERM`` to a placeholder of its own and leaves "
            "``COLORTERM`` unset, which is exactly the state a truecolour "
            "terminal is indistinguishable from -- a session loses 24-bit "
            "colour on entering the container with nothing saying why. "
            "``CLAUDE_CODE_TMUX_TRUECOLOR`` is the operator "
            "overruling a runtime that caps colour at 256 whenever it finds "
            "``TMUX`` set, on the assumption that a multiplexer may not "
            "forward 24-bit -- an assumption their tmux answers, and their "
            "having set the variable is them saying so. Carried for the "
            "parity this whole model is for: the same terminal, capped in "
            "both places or in neither. Left behind while ``TMUX`` crosses, "
            "a session that renders 24-bit on the host drops to 256 colour "
            "on entering the container. ``TMUX``, ``STY`` "
            "and ``ZELLIJ`` say which multiplexer sits between what the "
            "session prints and the screen, which is what decides whether a "
            "runtime wraps an escape sequence in the passthrough that "
            "survives one -- a clipboard sequence emitted bare into a tmux "
            "pane is swallowed there, while the operator's own "
            "shift-selection works and nothing says why. ``TERM_PROGRAM``, "
            "``LC_TERMINAL`` and ``VTE_VERSION`` name the emulator itself, "
            "which is what a runtime reads to tell the operator which "
            "modifier to hold and which sequences it may spell. A "
            "multiplexer variable is still only a description here: it is "
            "read for whether it is set, never followed to the host socket "
            "it points at, which is the one thing in it that does not cross"
        ),
    )
    locale_variables: list[str] = Field(
        default=["LANG", "LC_ALL", "LC_CTYPE"],
        description=(
            "Variables naming a compiled locale, which is a thing the image "
            "has to have generated rather than a description it can take on "
            "trust. The first is the *base*: the one written even when the "
            "host set none of them, because a container whose ``LANG`` is "
            "unset runs in ASCII. The rest cross only where the host set "
            "them -- ``LC_ALL`` in particular is an override an operator "
            "leaves unset on purpose, and writing it for them would silence "
            "every per-category setting a session might later make"
        ),
    )
    locales: list[GeneratedLocale] = Field(
        default=[GeneratedLocale(name="en_US.UTF-8")],
        description=(
            "Locales this image generates, beyond the ``C.UTF-8`` glibc "
            "carries built in, so the operator's own ``LANG`` crosses "
            "unchanged. Generated rather than forwarded-and-hoped: a name "
            "glibc cannot find is answered by falling back to ASCII and "
            "warning once per program, which costs a session its box "
            "drawing and its accented characters and blames neither the "
            "image nor the launch. One ``locale-gen`` line in the build is "
            "the whole price, so an adopter whose operators run another "
            "locale adds it here rather than living with a substitution"
        ),
    )
    fallback_locale: str = Field(
        default="C.UTF-8",
        description=(
            "What a locale variable is set to when the host names one this "
            "image did not generate, and what the image bakes so a container "
            "started without the launcher is still UTF-8 rather than ASCII. "
            "``C.UTF-8`` because glibc carries it without generation, so "
            "this fallback cannot itself be missing"
        ),
    )
    zone_variables: list[str] = Field(
        default=["TZ"],
        description=(
            "Variables naming the zone a session reads its own clock in. "
            "``TZ`` is the *base*, written whether the host exported it or "
            "not, and that is the whole repair: unlike every description "
            "above, a Linux host keeps its zone in ``/etc/localtime`` and "
            "exports the variable for nobody, so carrying ``TZ`` by name "
            "carried an absence and the container fell back to UTC. What "
            "fills it is :func:`host_timezone`, not the environment. The "
            "cost of leaving it unset is not cosmetic: git reads ``TZ`` for "
            "the author offset, so a session's commits record ``+00:00`` in "
            "a history whose every other commit carries the operator's own"
        ),
    )
    zones_carried: bool = Field(
        default=True,
        description=(
            "Whether the image holds a zone database, which decides whether "
            "a resolved name is written or substituted for. The image "
            "installs :attr:`zone_package`, so this is true here; it is a "
            "field rather than a constant for the adopter who strips tzdata "
            "out, whose sessions should then read the substitution notice "
            "rather than a ``TZ`` naming a zone nothing inside can resolve"
        ),
    )
    zone_package: Package = Field(
        default=Package(name="tzdata"),
        description=(
            "What puts the zone database in the image, declared for the same "
            "reason the editors are: a name the operator's machine answered "
            "reaches a container that has to be able to answer it too. The "
            "base image happens to carry it already, which is exactly why it "
            "is written down -- an unnamed dependency is one a later slimming "
            "removes, and what it would break is a timestamp rather than a "
            "command, so nothing would fail loudly enough to blame it"
        ),
    )
    fallback_zone: str = Field(
        default="UTC",
        description=(
            "What ``TZ`` is set to when this machine names no zone, which is "
            "what a container without the variable was already doing. Written "
            "rather than left out, so a session reading UTC is reading a zone "
            "somebody chose rather than a default nobody saw"
        ),
    )
    editor_variables: list[str] = Field(
        default=["EDITOR", "VISUAL"],
        description=(
            "Variables naming the program a session opens for an edit -- "
            "what Claude Code's Ctrl+G and git's commit message both read. "
            "The first is the *base*, written even when the host set "
            "neither, because a container has no editor of its own to fall "
            "back to: an unset ``EDITOR`` there does not degrade to a "
            "default, it degrades to the open-in-editor binding doing "
            "nothing at all. ``VISUAL`` crosses only where the host set it, "
            "which is what keeps an operator's own precedence between the "
            "two intact rather than flattening both onto one answer"
        ),
    )
    editors: list[CarriedEditor] = Field(
        default=[
            CarriedEditor(command="vim", package=Package(name="vim")),
            CarriedEditor(command="vi", package=Package(name="vim")),
            CarriedEditor(command="nvim", package=Package(name="neovim")),
            CarriedEditor(command="nano", package=Package(name="nano")),
            CarriedEditor(command="emacs", package=Package(name="emacs-nox")),
            CarriedEditor(command="hx", package=Package(name="helix")),
        ],
        description=(
            "The editors this image carries, so the operator's own choice "
            "crosses unchanged rather than being substituted. Several rather "
            "than one, because carrying only the fallback would make every "
            "operator who uses something else read a substitution notice at "
            "every launch -- which teaches them the boundary is a thing to "
            "work around. ``emacs-nox`` rather than ``emacs``: the graphical "
            "build pulls a display stack into an image with no display. An "
            "adopter who wants a smaller image cuts this list and pays a "
            "substitution notice for whoever used what was cut"
        ),
    )
    fallback: str = Field(
        default="vim",
        description=(
            "What an editor variable is pointed at when the host names a "
            "program this image does not carry, and what it is set to when "
            "the host names none at all. The second case is the one that "
            "matters most: an unset ``EDITOR`` in a container with no editor "
            "leaves the open-in-editor binding doing nothing at all, which "
            "reads as the runtime having lost a feature"
        ),
    )

    def packages(self) -> list[Package]:
        """Everything the image installs to carry this handoff, deduplicated.

        Deduplicated because two commands legitimately come from one package
        -- ``vi`` and ``vim`` are the standing case -- and asking pacman for
        the same package twice in one layer is a line a reader has to stop at.

        The zone database joins the editors because it answers the same kind
        of variable: a name the host resolved, which a container has to be
        able to resolve too.
        """
        carried = [item.package for item in self.editors]
        return list(dict.fromkeys([*carried, self.zone_package]))

    def commands(self) -> list[str]:
        """Every editor command this image answers to, for a probe to check."""
        return [item.command for item in self.editors]

    def generated(self) -> list[GeneratedLocale]:
        """The locales the image compiles, with the built-in one excluded.

        Excluded rather than filtered by the build, because ``C.UTF-8`` is not
        in ``/etc/locale.gen`` at all: glibc carries it compiled in, and a
        build that asked ``locale-gen`` for it fails on a name that file has
        no line for.
        """
        return [item for item in self.locales if item.name != self.fallback_locale]

    def carries(self, editor: str) -> bool:
        """Whether a host's editor variable names a program this image has.

        Compared on the leading word rather than the whole value, because an
        editor variable is a command line and not a path: ``EDITOR='code
        --wait'`` and ``EDITOR=/usr/bin/vim`` are both ordinary, and matching
        either whole would call an image that carries the editor one that
        does not.

        Both readings go through the parser that owns them -- ``shlex`` for
        the command line, ``Path`` for the program's basename. Splitting on a
        space and slicing at the last slash is the same answer for the cases
        above and the wrong one for ``EDITOR="/opt/my editor/vim"``, which a
        shell quotes and neither hand-written rule would.
        """
        if not editor:
            return False
        # lup: ignore[string-split] — `shlex.split` *is* the parser this rule
        # asks for: a command line is what an editor variable holds
        return Path(shlex.split(editor)[0]).name in self.commands()

    def speaks(self, locale: str) -> bool:
        """Whether a host's locale variable names one this image generated.

        The fallback counts as carried: it is compiled into glibc, so an
        operator already running ``C.UTF-8`` is answered exactly and hears
        nothing about a substitution that did not happen.
        """
        return locale == self.fallback_locale or any(
            item.name == locale for item in self.locales
        )

    def keeps(self, zone: str) -> bool:
        """Whether a zone name is one the image's database can resolve.

        Asked of this machine's own database rather than the container's,
        which is the same trade the locale check makes and sound for the same
        reason: both sides carry a whole distribution's tzdata, so a name one
        resolves the other resolves. What it cannot catch is an adopter who
        stripped the package out, which is what :attr:`zones_carried` is for
        -- set false there, every name substitutes and the notice says so.
        """
        if not zone or not self.zones_carried:
            return False
        try:
            return bool(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            return False

    def for_host(
        self,
        environment: Mapping[str, str],  # lup: ignore[dict-str-payload] — env map
        zone: str = "",
    ) -> TerminalResolution:
        """This handoff answered by the machine the launch is running on.

        A described variable the host has not set is left out rather than
        crossed as empty: an empty ``COLORTERM`` is a value a client reads and
        believes, where an absent one is the question it was already prepared
        for.

        The three groups that name something are handled by :meth:`answered`,
        which copies the host's own state rather than filling the group in.

        ``zone`` is what :func:`host_timezone` read off this machine, passed
        in rather than read here for the reason every machine fact in this
        file is passed in: a declaration that resolved it would hash a
        different digest on every machine in a different zone. It is the
        group's answer only where the operator exported no ``TZ`` of their
        own, which keeps an explicitly set variable ahead of the file it
        would have been read from.
        """
        described = {
            name: environment[name]
            for name in self.described_by
            if name in environment and environment[name]
        }
        editor = self.answered(
            environment,
            self.editor_variables,
            self.carries,
            self.fallback,
            "a CarriedEditor on the image's terminal handoff",
        )
        locale = self.answered(
            environment,
            self.locale_variables,
            self.speaks,
            self.fallback_locale,
            "another entry in the handoff's `locales`, which the build generates",
        )
        # Merged rather than passed alongside, so the zone travels the same
        # path as the two groups above: the host's own variable wins where it
        # set one, and what this machine keeps fills the base where it did not.
        keeping = self.answered(
            {**environment, self.zone_variables[0]: self.chosen(environment, zone)},
            self.zone_variables,
            self.keeps,
            self.fallback_zone,
            "the handoff's `zone_package`, which the image installs",
        )
        return TerminalResolution(
            environment={
                **described,
                **editor.environment,
                **locale.environment,
                **keeping.environment,
            },
            substitutions=[
                *editor.substitutions,
                *locale.substitutions,
                *keeping.substitutions,
            ],
        )

    def chosen(
        self,
        environment: Mapping[str, str],  # lup: ignore[dict-str-payload] — env map
        zone: str,
    ) -> str:
        """The operator's own ``TZ`` where they exported one, else the machine's.

        The precedence matters in the one direction a reader would not guess:
        an operator who exported ``TZ`` is overriding their own
        ``/etc/localtime``, and a launch that preferred the file would undo
        that override at the boundary -- the one place they cannot see it
        happen.
        """
        base = self.zone_variables[0]
        exported = base in environment and environment[base]
        return environment[base] if exported else zone

    def answered(
        self,
        environment: Mapping[str, str],  # lup: ignore[dict-str-payload] — env map
        variables: list[str],
        carried: Callable[[str], bool],
        fallback: str,
        widen: str,
    ) -> TerminalResolution:
        """One group of variables naming a thing, resolved against what is carried.

        Shared by the editor and the locale because the two differ in nothing
        but their vocabulary: each is a set of spellings for one question, a
        test for whether the image holds the answer, and something to fall
        back to. Written once, so a third such variable is a call rather than
        a copy -- and so a fix to the precedence rule cannot land on one of
        them and miss the other.

        Each spelling is resolved on its own and only where the host set it,
        which is what keeps the operator's own precedence between them intact.
        Filling the whole group in from whichever member answers first is
        wrong in both directions: it flattens a host that
        deliberately points ``VISUAL`` and ``EDITOR`` at different programs
        onto one of them, and it writes ``LC_ALL`` for a host that left it
        unset on purpose -- which is not a handoff but an override, silencing
        every per-category locale a session might set afterwards.

        The exception is the group's base, the first entry, which is written
        whether the host set it or not. That is the whole reason a group has a
        base: the container carries no editor and no locale of its own, so an
        unset base does not degrade to a default -- an unset ``EDITOR``
        degrades to the open-in-editor binding doing nothing at all, and an
        unset ``LANG`` to a session whose encoding is ASCII.
        """
        # Set *and* non-empty, which are two conditions rather than one. An
        # exported-but-empty `COLORTERM` is a value a client reads and
        # believes, so it is treated as the absence it means rather than
        # carried across as the empty string the host happened to leave.
        answers = {
            name: environment[name]
            for name in variables
            if name in environment and environment[name]
        }
        base = variables[0]
        return TerminalResolution(
            environment={
                base: fallback,
                **{
                    name: value if carried(value) else fallback
                    for name, value in answers.items()
                },
            },
            substitutions=[
                Substitution(variable=name, asked=value, instead=fallback, widen=widen)
                for name, value in answers.items()
                if not carried(value)
            ],
        )
