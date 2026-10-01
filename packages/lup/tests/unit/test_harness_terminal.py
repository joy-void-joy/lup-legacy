"""What crosses into a contained session from the terminal it was opened in.

Every case here is one the first contained session got wrong. Colour collapsed
to sixteen because no description crossed; the open-in-editor binding did
nothing because the variable naming an editor was unset and the image carried
none; and the encoding was ASCII because a container with no ``LANG`` runs in
the C locale.

The cases that matter most are the ones where carrying a variable is *worse*
than leaving it — a locale the image never generated, an editor it does not
have — because that is where a naive forward turns a missing feature into a
broken one.
"""

from pathlib import Path

from lup.harness.image import Image
from lup.harness.terminal import CarriedEditor, GeneratedLocale, TerminalHandoff
from lup.harness.requirements import Package


def test_the_operators_terminal_description_crosses_verbatim() -> None:
    """``TERM`` and ``COLORTERM`` are descriptions and are carried as they are.

    Left behind, a session loses 24-bit colour on entering the container, with
    nothing saying why. The engine sets ``TERM`` to a placeholder of its own
    and leaves ``COLORTERM`` unset, which is exactly the state a truecolour
    terminal is indistinguishable from.
    """
    crossing = (
        TerminalHandoff()
        .for_host({"TERM": "tmux-256color", "COLORTERM": "truecolor"})
        .environment
    )

    assert crossing["TERM"] == "tmux-256color"
    assert crossing["COLORTERM"] == "truecolor"


def test_a_description_the_host_never_set_is_left_out_rather_than_emptied() -> None:
    """An absent variable is the question a client was prepared for.

    An empty ``COLORTERM`` is a value a client reads and believes; an absent
    one it already knows how to handle.
    """
    crossing = TerminalHandoff().for_host({"TERM": "xterm"}).environment

    assert "COLORTERM" not in crossing


def test_the_multiplexer_between_the_session_and_the_screen_crosses() -> None:
    """Whether a sequence needs wrapping is a fact the container cannot see.

    Left behind, a clipboard escape is emitted bare into a tmux pane and
    swallowed there, because the runtime inside reads no ``TMUX`` and
    concludes there is no multiplexer to wrap for -- while the operator's own
    shift-selection, which bypasses every layer, keeps working and makes the
    loss look like the runtime's rather than the boundary's.
    """
    crossing = (
        TerminalHandoff()
        .for_host({"TERM": "tmux-256color", "TMUX": "/tmp/tmux-1000/default,7,0"})
        .environment
    )

    assert crossing["TMUX"] == "/tmp/tmux-1000/default,7,0"


def test_the_multiplexer_that_forwards_truecolour_crosses_with_the_one_that_caps() -> (
    None
):
    """The pair that has to travel together, which comes apart otherwise.

    A runtime that finds ``TMUX`` set caps colour at 256, on the assumption
    that a multiplexer may not forward 24-bit; the operator overrules it by
    setting this, which is them answering for the tmux they actually run.
    Carrying the assumption's trigger while leaving its answer behind renders
    a session at 24-bit outside the container and 256 inside it, against one
    unchanged terminal.
    """
    crossing = (
        TerminalHandoff()
        .for_host(
            {
                "TERM": "tmux-256color",
                "COLORTERM": "truecolor",
                "TMUX": "/tmp/tmux-1000/default,7,0",
                "CLAUDE_CODE_TMUX_TRUECOLOR": "1",
            }
        )
        .environment
    )

    assert crossing["CLAUDE_CODE_TMUX_TRUECOLOR"] == "1"
    assert crossing["TMUX"] == "/tmp/tmux-1000/default,7,0"


def test_the_operator_who_set_no_truecolour_flag_is_not_given_one() -> None:
    """Parity is the point, so the sandbox does not out-colour the host.

    Deriving the flag from ``COLORTERM`` was the alternative and overclaims:
    that names the multiplexer's *client*, not whether the multiplexer
    forwards, and a session brighter inside the container than outside it is
    a handoff that stopped describing the terminal it was opened from.
    """
    crossing = (
        TerminalHandoff()
        .for_host(
            {
                "TERM": "tmux-256color",
                "COLORTERM": "truecolor",
                "TMUX": "/tmp/tmux-1000/default,7,0",
            }
        )
        .environment
    )

    assert "CLAUDE_CODE_TMUX_TRUECOLOR" not in crossing


def test_the_emulator_names_itself_rather_than_being_guessed_at() -> None:
    """Which modifier a runtime tells the operator to hold is read from this.

    A description like every other here: nothing follows it to a program, so
    a name this image never heard of costs nothing.
    """
    crossing = TerminalHandoff().for_host({"TERM_PROGRAM": "iTerm.app"}).environment

    assert crossing["TERM_PROGRAM"] == "iTerm.app"


def test_an_editor_variable_is_written_even_when_the_host_sets_none() -> None:
    """The container has no editor of its own to fall back to.

    Unset here does not degrade to a default — it degrades to the
    open-in-editor binding doing nothing at all when pressed, which reads as
    the runtime having lost a feature rather than as a variable not crossing.
    """
    crossing = TerminalHandoff().for_host({}).environment

    assert crossing["EDITOR"] == "vim"
    assert crossing["LANG"] == "C.UTF-8"


def test_an_editor_this_image_carries_crosses_unchanged_and_says_nothing() -> None:
    """A handoff that worked is not news, and a line saying so trains the eye."""
    answer = TerminalHandoff().for_host({"EDITOR": "nvim"})

    assert answer.environment["EDITOR"] == "nvim"
    assert answer.substitutions == []


def test_an_editor_this_image_lacks_is_substituted_out_loud() -> None:
    """Because the substituted thing works, which is what hides it.

    An operator whose ``EDITOR`` was quietly swapped gets an editor. The only
    evidence it is not theirs is that it is not theirs, which is exactly the
    evidence nobody goes looking for.
    """
    answer = TerminalHandoff().for_host({"EDITOR": "code --wait"})
    (swapped,) = answer.substitutions

    assert answer.environment["EDITOR"] == "vim"
    assert swapped.asked == "code --wait"
    assert swapped.instead == "vim"
    assert answer.notices()


def test_an_editor_is_matched_on_its_program_not_its_whole_value() -> None:
    """An editor variable is a command line, and often a path.

    ``EDITOR=/usr/bin/vim`` and ``EDITOR='vim -u NONE'`` both name an editor
    this image carries, and matching the whole value would substitute both.

    The quoted path is the case that decides which parser: a shell reads it as
    one word, and splitting on the space would find ``my`` where the editor is
    ``vim``. Unquoted, the same characters are genuinely two words and no
    reading recovers the intent — which is a property of the value rather than
    of this code, and is why the convention quotes it.
    """
    handoff = TerminalHandoff()

    assert handoff.carries("/usr/bin/vim")
    assert handoff.carries("vim -u NONE")
    assert handoff.carries("'/opt/my editor/vim'")
    assert not handoff.carries("kak")


def test_visual_and_editor_keep_the_precedence_the_host_gave_them() -> None:
    """Filling the group in from whichever answered first flattens the host.

    A host that points the two at different programs meant to. Copying its own
    state is the only reading that is right for both that host and the one
    that sets only ``EDITOR``.
    """
    crossing = (
        TerminalHandoff().for_host({"VISUAL": "emacs", "EDITOR": "vi"}).environment
    )

    assert crossing["VISUAL"] == "emacs"
    assert crossing["EDITOR"] == "vi"


def test_a_variable_the_host_left_unset_is_not_written_for_it() -> None:
    """``LC_ALL`` unset is a decision, and writing it would override the rest.

    An operator leaves ``LC_ALL`` unset so per-category settings still apply.
    A handoff that filled it in would not be carrying their terminal across,
    it would be silencing every ``LC_*`` a session might later set.
    """
    crossing = TerminalHandoff().for_host({"LANG": "en_US.UTF-8"}).environment

    assert crossing["LANG"] == "en_US.UTF-8"
    assert "LC_ALL" not in crossing


def test_a_locale_the_image_generated_crosses_rather_than_being_replaced() -> None:
    """Which is the whole reason the build runs ``locale-gen`` at all.

    Substituting ``C.UTF-8`` for every operator would have been the cheaper
    build and the wrong answer: a locale decides sorting, dates and number
    formatting, not only whether the encoding is UTF-8.
    """
    answer = TerminalHandoff().for_host({"LANG": "en_US.UTF-8"})

    assert answer.environment["LANG"] == "en_US.UTF-8"
    assert answer.substitutions == []


def test_a_locale_nobody_generated_is_substituted_rather_than_forwarded() -> None:
    """Forwarding it is worse than substituting, which is why this is not a carry.

    glibc answers a locale name it cannot find by falling back to ASCII and
    warning once per program — so the session loses its box drawing *and*
    gains a screenful of noise, with nothing blaming the image.
    """
    answer = TerminalHandoff().for_host({"LANG": "fr_FR.UTF-8"})
    (swapped,) = answer.substitutions

    assert answer.environment["LANG"] == "C.UTF-8"
    assert swapped.asked == "fr_FR.UTF-8"


def test_the_built_in_locale_counts_as_carried() -> None:
    """It is compiled into glibc, so an operator already on it hears nothing."""
    assert TerminalHandoff().for_host({"LANG": "C.UTF-8"}).substitutions == []


def test_the_generated_list_leaves_out_the_one_glibc_carries() -> None:
    """``C.UTF-8`` has no line in ``/etc/locale.gen``, so asking for it fails."""
    handoff = TerminalHandoff(
        locales=[
            GeneratedLocale(name="C.UTF-8"),
            GeneratedLocale(name="en_US.UTF-8"),
        ]
    )

    assert [item.name for item in handoff.generated()] == ["en_US.UTF-8"]


def test_a_locale_names_its_charmap_rather_than_having_one_derived() -> None:
    """The charmap is not reliably the name's suffix, and a wrong one is silent.

    ``ja_JP.EUC-JP`` pairs with ``EUC-JP`` and ``en_US`` with ``ISO-8859-1``.
    A derived charmap produces a ``locale.gen`` line the generator skips, so
    the locale is simply absent and the build reports nothing.
    """
    legacy = GeneratedLocale(name="en_US", charmap="ISO-8859-1")

    assert legacy.line() == "en_US ISO-8859-1"


def test_the_image_installs_exactly_the_editors_it_matches_against() -> None:
    """One list, so a launch cannot forward a name no layer installed.

    Written twice they come apart in the direction hardest to see: the launch
    forwards a name it believes is carried, the layer never installed it, and
    the operator's editor fails to open with the runtime blamed for it.
    """
    installed = {item.name for item in Image().packages(manifest_of_nothing())}

    assert {"vim", "neovim", "nano", "emacs-nox", "helix"} <= installed


def test_two_editor_commands_from_one_package_install_it_once() -> None:
    """``vi`` and ``vim`` are one package, and one layer should say so once."""
    handoff = TerminalHandoff(
        editors=[
            CarriedEditor(command="vim", package=Package(name="vim")),
            CarriedEditor(command="vi", package=Package(name="vim")),
        ]
    )

    assert handoff.packages().count(Package(name="vim")) == 1


def test_the_build_compiles_every_locale_the_handoff_declares() -> None:
    """Declared and not generated is the failure this layer exists to prevent."""
    rendered = Image().dockerfile(manifest_of_nothing())

    assert "en_US.UTF-8 UTF-8" in rendered
    assert "locale-gen" in rendered


def manifest_of_nothing():
    """An empty manifest, for asking the image about what it carries itself.

    Nested rather than imported: what these cases are about is the image's own
    baseline and its terminal handoff, and a manifest with requirements in it
    would put a second source of packages into the answer.
    """
    from lup.harness.requirements import Manifest

    return Manifest()


def test_the_machines_zone_crosses_although_the_host_exports_no_variable() -> None:
    """A contained session runs in the machine's zone, not in UTC.

    ``TZ`` is not a description that crosses the way ``TERM`` is: a Linux
    host keeps its zone in ``/etc/localtime`` and exports the variable for
    nobody, so forwarding the variable alone hands over an absence and the
    container falls back to UTC -- and every commit authored inside records
    ``+00:00`` in a repository whose others carry the machine's offset.
    """
    crossing = TerminalHandoff().for_host({"TERM": "xterm"}, "Europe/Paris").environment

    assert crossing["TZ"] == "Europe/Paris"


def test_an_exported_zone_beats_the_file_it_would_have_been_read_from() -> None:
    """An operator who exported ``TZ`` is overriding their own system zone.

    A launch that preferred ``/etc/localtime`` would undo that override at
    the boundary -- the one place they cannot watch it happen.
    """
    crossing = (
        TerminalHandoff().for_host({"TZ": "Asia/Tokyo"}, "Europe/Paris").environment
    )

    assert crossing["TZ"] == "Asia/Tokyo"


def test_a_machine_naming_no_zone_is_given_utc_rather_than_nothing() -> None:
    """The base is written whether anything answered it or not.

    UTC is what a container with no ``TZ`` was already doing, so this changes
    no clock -- it changes who chose it, from a fallback nobody saw to a
    value a reader can find in the argv.
    """
    crossing = TerminalHandoff().for_host({"TERM": "xterm"}).environment

    assert crossing["TZ"] == "UTC"


def test_a_zone_no_database_resolves_is_substituted_out_loud() -> None:
    """A name that reaches the container unresolvable is silently UTC inside.

    Which is the failure this whole handoff exists to refuse: the session
    gets a working clock in the wrong zone, and the only evidence is that it
    is not the one that was asked for.
    """
    resolution = TerminalHandoff().for_host({"TZ": "Mars/Olympus_Mons"}, "")

    assert resolution.environment["TZ"] == "UTC"
    assert [item.asked for item in resolution.substitutions] == ["Mars/Olympus_Mons"]


def test_an_image_that_carries_no_zone_database_substitutes_every_name() -> None:
    """``zones_carried`` is the adopter's answer, not a probe of this machine.

    The check runs against the launcher's own tzdata, which an adopter who
    stripped the package out of the image would have lying to them. Saying so
    in the declaration is what turns that into a notice rather than a
    ``TZ`` naming a zone nothing inside can resolve.
    """
    resolution = TerminalHandoff(zones_carried=False).for_host({}, "Europe/Paris")

    assert resolution.environment["TZ"] == "UTC"
    assert [item.variable for item in resolution.substitutions] == ["TZ"]


def test_the_image_installs_the_zone_database_the_handoff_resolves_against() -> None:
    """An unnamed dependency is one a later slimming removes.

    The base image carries tzdata already, which is exactly why it is written
    down: what its absence would break is a timestamp rather than a command,
    so nothing would fail loudly enough to be blamed on it.
    """
    assert Package(name="tzdata") in TerminalHandoff().packages()


def test_the_resolved_zone_reaches_the_engine_as_a_value_in_the_argv(
    tmp_path: Path,
) -> None:
    """The half of this the handoff's own tests cannot see.

    Every case above ends at a mapping, and a mapping the launch never
    forwarded is the same UTC session with a passing suite behind it. This
    ends at the flag the engine reads.
    """
    crossing = TerminalHandoff().for_host({"TERM": "xterm"}, "Europe/Paris")
    argv = Image().session_arguments(
        tag="lup-agent:test",
        checkout=tmp_path,
        uid=1000,
        gid=1000,
        writable={},
        read_only={},
        state_volume="lup-cfg-test",
        config_home_env="CLAUDE_CONFIG_DIR",
        terminal=crossing.environment,
    )

    index = argv.index("TZ=Europe/Paris")
    assert argv[index - 1] == "-e"
