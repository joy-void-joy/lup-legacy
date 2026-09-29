"""A shell vocabulary an adopter starts from, offered as groups to compose.

:mod:`lup.policy.shell_rules` declares the *shape* a vocabulary takes and
says the words themselves are a judgement about one project's toolchain, so
they arrive from outside. That is true, and it left the library shipping
nothing — which is not the neutral position it reads as. An empty table
matches no command and every command is then unlisted, which is a question
put to whoever is there and a refusal where nobody is: a fresh adopter's
agent prompts for ``ls`` in front of a human and cannot run it in a worker,
until several hundred lines of vocabulary exist. Shipping nothing chose a
verdict for them just as surely as shipping something would have, and chose
the least useful one.

So each group below is a function returning rules, and every word it declares
is a parameter default rather than a table. An adopter calls the group to
take the judgement as offered, passes their own words to replace it, or
splices extra rules around it::

    SHELL_RULES = [
        *read_only_rules(),
        *judged_ask_rules(),
        *guarded_tool_rules(),
        git_rule(integration_branches=("main", "dev"), redirect_checkout=True),
        gh_rule(),
        docker_rule(),
        my_own_cli_rule(),
    ]

The judgement running through all of it: generous for reading and for local
work a second attempt undoes, conservative for anything that loses something.
Networked is deliberately not the line — publishing is how work becomes
reviewable and happens many times a session, so ``git push``, the pull
request verbs that open and describe one, and the merge that lands it are
ordinary. What stays guarded is the direction that removes something no
second attempt restores.

Where a group's default encodes a judgement a reasonable project would make
differently, it takes a parameter instead of a fork: which branches a leased
force push still asks about, whether ``checkout`` is redirected toward
``switch``/``restore``, whether opening a pull request is authoring or
publishing.
"""

from collections.abc import Sequence
from functools import cache

from pydantic import BaseModel

from lup.policy.kernel.decision import CheckpointRequirement, SandboxPlacement
from lup.policy.kernel.effects import EffectRow, declare
from lup.policy.kernel.rows import DestinationForm
from lup.policy.kernel.words import UV_GLOBAL_VALUE_OPTIONS
from lup.policy.kernel.semantics import EffectClass, ReviewerRequirement
from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    ShellOperationRule,
    ShellSubcommandRule,
)


class JudgedCommand(BaseModel, frozen=True):
    """One command that stops for a human, and the reason it gives them."""

    name: str
    reason: str
    recovery: str = ""
    """What the agent can do instead, where the command has a better route."""
    effects: list[EffectRow] = []
    """What this command does, where the group cannot say it for every member.

    A group whose members share one effect derives it (the destructive verbs
    all destroy); one whose members differ has to be told. `export` decides
    what a later command sees and `eval` runs code nothing read -- both are
    refused for the same reason and neither does the other's thing."""
    checkpoint: CheckpointRequirement = "unrecoverable"
    """What capture would put back what this command destroys, if any would.

    The question this row asks exists because a loss is permanent, so naming
    the capture that makes it impermanent is naming when the question stops
    being worth a person's attention. Silence keeps the question.

    Only a loss made of *paths in this checkout* has a capture to name. A
    process, a package database, a unit's state and a crontab are none of
    them, so a row claiming one reached an unprompted allow against its own
    stated reason -- and reached it on every invocation, since the settlement
    layer is told a capture completed once per session rather than once per
    command."""
    read_verbs: list[str] = []
    """This command's own spellings of its query action, which de-escalate it.

    Without somewhere to say "except this verb", listing an archive is as
    much an approval as extracting one."""
    write_markers: list[str] = []
    """Argument prefixes whose absence makes this command read-only.

    The same exception stated negatively, for a command that has no query
    verb because its query form is the plain one: `dd` writes given an `of=`
    and reads without it."""
    bare_reads: bool = False
    """Whether this command reads when handed nothing at all.

    Where `write_markers` still needs a word to find no marker in, this needs
    every word to be absent: `mount` alone prints the mount table, and each
    form that acts names a device or a mountpoint."""
    write_flags: list[str] = []
    """Options whose value is the path this command lands on.

    What `write_markers` says about the *form* said in terms of the path, for
    the same word: `dd of=x` writes `x`, and a row that only knew a marker was
    present left the scope column claiming a capture covered wherever it
    pointed. Named here, the path is read like any other destination."""
    reach: str = ""
    """Where the harm this command's question guards against lands, where the
    group's effect does not say: `kill` destroys a process the container owns,
    `ssh` reaches another machine, and both are one loss nothing captures."""
    landing_operands: int = 0
    """The operand, counted from one, from which every operand is a place this
    command changes: `chmod <mode> <file>...` changes the second on."""


def read_only_rules(
    commands: Sequence[str] = (
        "ls",
        "cat",
        "echo",
        "printf",
        "test",
        "file",
        "wc",
        "head",
        "tail",
        "nl",
        "tac",
        "rev",
        "fold",
        "cut",
        "tr",
        "expr",
        "numfmt",
        "comm",
        "join",
        "paste",
        "column",
        "col",
        "uniq",
        "grep",
        "egrep",
        "fgrep",
        "diff",
        "cmp",
        "jq",
        "stat",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "date",
        "seq",
        "du",
        "df",
        "cksum",
        "md5sum",
        "sha256sum",
        "which",
        "man",
        "true",
        "false",
        "set",
        "continue",
        "break",
        "shift",
        "return",
        "local",
        "exit",
        "sleep",
        "pwd",
        "id",
        "whoami",
        "hostname",
        "uname",
        "ps",
        "pgrep",
        "pidof",
        "lsof",
        "free",
        "uptime",
        "nproc",
        "xxd",
        "od",
        "strings",
        "getent",
        "zcat",
        "[",
    ),
) -> list[ShellCommandRule]:
    """Commands that read and report, and change nothing by running.

    The process and socket listings sit here for the same reason the file
    ones do: establishing that a service came up is a read, and a session
    that just started one is asking about its own process.

    The control-flow builtins report nothing and are here on the second half
    of that test: they change nothing by running, and nothing they do can
    reach a later command. `eval`, `exec`, `export`, `declare` and `unset`
    are deliberately absent — each decides what some later command sees or
    does, which is the thing this list promises a reader it does not touch.
    """
    return [
        ShellCommandRule(
            name=name,
            effects=[declare("reads_path", scope="project")],
        )
        for name in commands
    ]


def judged_ask_rules(
    commands: Sequence[JudgedCommand] = (
        JudgedCommand(
            name="rm",
            reason="deleting files requires approval",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="rmdir",
            reason="deleting directories requires approval",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="mv",
            reason="moving files requires approval",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="cp",
            reason="copying over files requires approval",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="chmod",
            reason="changing permissions requires approval",
            reach="container",
            landing_operands=2,
        ),
        JudgedCommand(
            name="chown",
            reason="changing ownership requires approval",
            landing_operands=2,
        ),
        JudgedCommand(
            name="ln",
            reason="creating links requires approval",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="tee",
            reason="writing files requires approval",
            recovery="Prefer a file write, which the edit gates read.",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="dd",
            # `dd` writes when handed an `of=` and reads to stdout without
            # one, so its read-only form is the invocation with nothing extra
            # in it. No verb list can name that, which is why every
            # `dd if=x` stopped for approval as a write.
            write_markers=["of="],
            write_flags=["of"],
            reason="raw device or file writes require approval",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="mount",
            # Alone it prints the mount table, which is how a session finds out
            # what its own boundary is made of. Every form that acts names a
            # device or a mountpoint, so there is no marker to test for and no
            # verb to list -- what separates the two is that one carries words
            # and the other carries none.
            bare_reads=True,
            reason="mounting a filesystem requires approval",
            reach="container",
        ),
        JudgedCommand(
            name="truncate",
            reason="truncating files requires approval",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="kill",
            reason="terminating processes requires approval",
            reach="container",
        ),
        JudgedCommand(
            name="pkill",
            reason="terminating processes requires approval",
            reach="container",
        ),
        JudgedCommand(
            name="command",
            # Reached only in the query shape: every other spelling runs the
            # program after it, which `effective_command` unwraps to instead.
            read_verbs=["-v", "-V"],
            reason="'command' runs a program through a modified lookup",
            recovery="Name the program directly.",
        ),
        JudgedCommand(
            name="tar",
            # Named whole rather than scanned for a `t`, which a bundled
            # `-xzf` also contains: these are tar's own list-mode spellings.
            read_verbs=["-t", "--list", "-tf", "-tvf", "-tzf", "-tzvf", "-tjf", "-tJf"],
            reason="archive operations write files",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="unzip",
            read_verbs=["-l", "-t", "-v", "-z"],
            reason="archive extraction writes files",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="zip",
            read_verbs=["-sf", "--show-files"],
            reason="archive creation writes files",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="gzip",
            read_verbs=["-l", "--list", "-t", "--test"],
            reason="compression rewrites files",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(
            name="gunzip",
            read_verbs=["-l", "--list", "-t", "--test"],
            reason="decompression rewrites files",
            checkpoint="boundary_wide",
        ),
        JudgedCommand(name="sudo", reason="privilege escalation requires approval"),
        JudgedCommand(name="doas", reason="privilege escalation requires approval"),
        JudgedCommand(
            name="ssh", reason="remote access requires approval", reach="host_later"
        ),
        JudgedCommand(
            name="scp", reason="remote copies require approval", reach="host_later"
        ),
        JudgedCommand(
            name="rsync", reason="remote sync requires approval", reach="host_later"
        ),
        JudgedCommand(
            name="make",
            read_verbs=["-n", "--dry-run", "--just-print", "-q", "--question"],
            reason="make runs whatever its recipes say",
            reach="container",
        ),
        JudgedCommand(
            name="npm",
            read_verbs=["ls", "list", "view", "outdated", "why", "explain"],
            reason="package tools fetch and run code",
            reach="dependency",
        ),
        JudgedCommand(
            name="pnpm",
            reason="package tools fetch and run code",
            reach="dependency",
        ),
        JudgedCommand(
            name="yarn",
            reason="package tools fetch and run code",
            reach="dependency",
        ),
        JudgedCommand(
            name="apt",
            reason="system package changes require approval",
            reach="container",
        ),
        JudgedCommand(
            name="apt-get",
            reason="system package changes require approval",
            reach="container",
        ),
        JudgedCommand(
            name="pacman",
            reason="system package changes require approval",
            reach="container",
        ),
        JudgedCommand(
            name="brew",
            reason="system package changes require approval",
            reach="container",
        ),
        JudgedCommand(
            name="systemctl",
            reason="service management requires approval",
            reach="container",
        ),
        JudgedCommand(
            name="crontab",
            reason="schedule changes require approval",
            reach="container",
        ),
        # The system package managers change the image a container runs and go
        # with it. These build what the AUR's user-submitted recipes say and
        # install the result, which is a dependency arriving however
        # ephemeral the system it lands in.
        *(
            JudgedCommand(
                name=helper,
                reason="an AUR helper builds and installs what a user-submitted"
                " recipe says",
                effects=[declare("installs_dependency", scope="AUR package")],
            )
            for helper in ("yay", "paru", "pikaur", "trizen", "aurman", "pakku")
        ),
        # makepkg is that build on its own, from whatever PKGBUILD the checkout
        # holds, and installs what it built under `-i`.
        JudgedCommand(
            name="makepkg",
            reason="building a package runs its PKGBUILD and can install it",
            effects=[declare("installs_dependency", scope="PKGBUILD package")],
        ),
    ),
) -> list[ShellCommandRule]:
    """Commands that ask on every production path, with the reason each carries.

    Inside a root declared as scratch these allow instead, which the kernel
    settles by resolving each target's role — so the ask lands on production
    work rather than on a disposable tree.
    """
    return [
        ShellCommandRule(
            name=command.name,
            # The checkpoint column read as behaviour rather than as a
            # requirement: it already names what capture would put back what
            # this destroys, which is the whole of what the loss row asks.
            effects=command.effects
            or [
                declare(
                    "destroys_uncaptured", scope=command.checkpoint, reach=command.reach
                )
            ],
            read_verbs=command.read_verbs,
            write_markers=command.write_markers,
            write_flags=command.write_flags,
            bare_reads=command.bare_reads,
            landing_operands=command.landing_operands,
            checkpoint=command.checkpoint,
            reason=command.reason,
            recovery=command.recovery,
        )
        for command in commands
    ]


def redirected_rules(
    commands: Sequence[JudgedCommand] = (
        JudgedCommand(
            name="pip",
            reason="pip changes packages outside this project's lockfile",
            recovery="Use uv add / uv remove instead of pip.",
            effects=[declare("installs_dependency", scope="python package")],
        ),
        JudgedCommand(
            name="pip3",
            reason="pip changes packages outside this project's lockfile",
            recovery="Use uv add / uv remove instead of pip.",
            effects=[declare("installs_dependency", scope="python package")],
        ),
    ),
) -> list[ShellCommandRule]:
    """Commands denied toward the spelling a project actually uses.

    A redirect is house style rather than a safety verdict, which is why the
    pairs are a parameter: a project on a different package manager replaces
    them instead of inheriting an argument about uv.

    It is also why the refusal is stated as one rather than folded into the
    effects. `pip install` takes the very dependency `uv add` takes -- the
    trust question is identical and is answered identically -- and what
    differs is which command this project keeps its lockfile through. A rule
    that expressed that as an effect would be describing the project rather
    than the operation.
    """
    return [
        ShellCommandRule(
            name=command.name,
            effects=command.effects,
            refuses=command.reason,
            reason=command.reason,
            recovery=command.recovery,
        )
        for command in commands
    ]


def reaching_builtin_rules(
    commands: Sequence[JudgedCommand] = (
        JudgedCommand(
            name="eval",
            reason="eval runs text as code that nothing checked",
            recovery="Write the command out.",
            effects=[declare("runs_undeclared_program", scope="unread code")],
        ),
        JudgedCommand(
            name="source",
            reason="sourcing a script runs code in this shell that nothing checked",
            recovery="Run the commands it holds.",
            effects=[declare("runs_undeclared_program", scope="unread code")],
        ),
        JudgedCommand(
            name=".",
            reason="sourcing a script runs code in this shell that nothing checked",
            recovery="Run the commands it holds.",
            effects=[declare("runs_undeclared_program", scope="unread code")],
        ),
        JudgedCommand(
            name="export",
            reason="an exported variable changes what later commands see",
            recovery="Set it on the command that needs it.",
            effects=[
                declare(
                    "mutates_environment", scope="shell variable", reach="container"
                )
            ],
        ),
        JudgedCommand(
            name="declare",
            reason="a declared variable changes what later commands see",
            recovery="Set it on the command that needs it.",
            effects=[
                declare(
                    "mutates_environment", scope="shell variable", reach="container"
                )
            ],
        ),
        JudgedCommand(
            name="unset",
            reason="unsetting a variable changes what later commands see",
            recovery="Set it on the command that needs it.",
            effects=[
                declare(
                    "mutates_environment", scope="shell variable", reach="container"
                )
            ],
        ),
    ),
) -> list[ShellCommandRule]:
    """Builtins that run unread code, or decide what a *later* command sees.

    :func:`read_only_rules` carries the control-flow builtins because they
    change nothing and nothing they do reaches a later command. These fail
    that second half, and holding them out of that list to say so leaves an
    omission, which is not a judgement anything can read. Unlisted they refuse
    with "command 'eval' is not classified", the one thing that is not true
    of them: they are classified, by being left out, and the agent is told
    the opposite.

    Declared so the refusal carries its own reason, and so the row answering
    for work nobody classified is not also the row enforcing a decision
    somebody made. The same verdict either way, arrived at on the record.

    Declaring them also settles what a sandbox does about them, and settles
    it the way the inline-code refusal beside them already answered: a
    judged deny survives a boundary, because the objection to ``eval`` is
    that nothing read what it runs, and confining unread code does not read
    it. Unlisted they defer to the boundary instead — so ``python -c
    'x'`` and ``eval echo x``, which are one objection, are enforced two
    ways depending on which of them somebody wrote down.

    ``exec`` is absent though it is held out of that list too: reading a
    command's words already resolves it to the command it wraps, so ``exec rm -rf src`` is
    judged as ``rm -rf src`` and a row here would never be reached. Which is
    also the right answer — what ``exec`` runs is the whole of what it does
    to anything outside the shell it replaces.

    Each states what it does beside the refusal, and the two families here do
    different things: `eval` and the two sourcing spellings run code no gate
    read, while `export`, `declare` and `unset` change what a later command
    sees. Both refuse, and neither refuses because of what the other does.
    """
    return [
        ShellCommandRule(
            name=command.name,
            effects=command.effects,
            refuses=command.reason,
            reason=command.reason,
            recovery=command.recovery,
        )
        for command in commands
    ]


def downloader_rules(
    commands: Sequence[str] = ("curl", "wget"),
) -> list[ShellCommandRule]:
    """The downloaders, whose rows say what the files they land are judged by.

    What a download reads is the fetch scopes' to answer and what it sends
    asks, both read by the kernel's downloader screen before any row is
    walked. The row answers for the rest: every file the response lands at is
    a write to that path, judged the way `sort -o` and a redirection are, so
    the reason here is the one a write the destination policy stops is asked
    with. Only the tools that screen reads belong here.
    """
    return [
        ShellCommandRule(
            name=command,
            effects=[declare("fetches", scope="declared")],
            reason="a download writing that file requires approval",
        )
        for command in commands
    ]


def uv_rules(
    global_values: Sequence[str] = UV_GLOBAL_VALUE_OPTIONS,
    pip_reads: Sequence[str] = ("list", "show", "freeze", "check", "tree"),
    tool_reads: Sequence[str] = ("list", "dir"),
) -> list[ShellCommandRule]:
    """The uv routes that bring in or send out a package this project never declared.

    `uv add`, `sync`, `lock`, `remove` and `run` are the kernel's, which reads
    them against the lockfile and the runner targets. What reaches these rows
    is the rest of the surface, found past uv's global options the way every
    subcommand-gated command is: `uv pip` and `uv tool` install into an
    environment the lockfile does not describe, `uvx` fetches and runs a
    package nobody declared, and `uv publish` uploads one. Each asks wherever
    it runs, because what it weighs is trust in the package rather than where
    its files land. Their read-only verbs list what is already installed, and
    a verb this table does not name falls to the question.
    """
    installs = [declare("installs_dependency", scope="python package")]
    reads = [declare("reads_environment", scope="python environment")]
    return [
        ShellCommandRule(
            name="uv",
            # Only bare `uv` reaches this level, which prints its usage: every
            # verb is the kernel's or one of the subcommands below.
            effects=[declare("changes_nothing")],
            value_flags=list(global_values),
            # Runs uv from another directory, as `git -C` runs git, so the
            # program a `uv run` hands its words to stands there.
            directory_flags=["--directory"],
            subcommands=[
                ShellSubcommandRule(
                    name="pip",
                    effects=installs,
                    reason="uv pip changes packages outside this project's lockfile",
                    recovery="Use uv add / uv remove, which keep the lockfile.",
                    operations=[
                        ShellOperationRule(name=verb, effects=reads)
                        for verb in pip_reads
                    ],
                ),
                ShellSubcommandRule(
                    name="tool",
                    effects=[declare("installs_dependency", scope="python tool")],
                    reason="uv tool fetches and runs a package that is not a"
                    " declared dependency",
                    recovery="Declare it with uv add and run it through uv run.",
                    operations=[
                        ShellOperationRule(name=verb, effects=reads)
                        for verb in tool_reads
                    ],
                ),
                ShellSubcommandRule(
                    name="publish",
                    effects=[declare("external_mutation", scope="package index")],
                    reason="uv publish uploads a package where anyone can install it",
                ),
            ],
        ),
        ShellCommandRule(
            name="uvx",
            effects=[declare("installs_dependency", scope="python tool")],
            reason="uvx fetches and runs a package that is not a declared dependency",
            recovery="Declare it with uv add and run it through uv run.",
        ),
    ]


def guarded_tool_rules() -> list[ShellCommandRule]:
    """Readers whose own flags can turn them into writers, and the guards.

    Each entry is a fact about the utility rather than a preference: ``sort
    -o`` writes a file, ``ss -K`` closes sockets, ``nc`` moves bytes unless
    ``-z`` pins it to a scan. What varies between projects is which of these
    are installed, not what they do, so nothing here takes a parameter.
    """
    return [
        ShellCommandRule(
            # Not read-only, and not judged either. The verbs in the ask list
            # are destructive or content-bearing: they overwrite something, or
            # author a file that later holds code. An empty directory does
            # neither — `-p` is a no-op on one that exists, and it holds
            # nothing to run. Everything landing inside it still passes the
            # write and edit gates on its own path.
            #
            # Which is also why the scope here is not read off the targets, as
            # a delete's and an archive's are. `production` is untrue of
            # `mkdir /etc/newdir` on the face of it, and the reading that
            # would correct it — outside the checkout, so ask — is answering
            # a question this verb does not raise: what makes a write outside
            # worth an approval is the content arriving there, and the first
            # write that carries any is judged on its own path, wherever the
            # directory turned out to be. Emptiness is the claim, not the tree.
            name="mkdir",
            effects=[declare("writes_path", scope="production", write="create")],
        ),
        ShellCommandRule(
            # The same test `mkdir` passes, for the same reason. `touch` has
            # no form that writes content: on a path that exists it moves
            # timestamps and nothing else, and on one that does not it
            # authors an empty file, which holds nothing to run. Whatever
            # lands in that file afterwards passes the write and edit gates
            # on its own path, so asking here buys a prompt and no decision.
            # `touch /etc/newfile` is the same answer for the reason spelled
            # out above: the scope is stated rather than resolved because
            # emptiness is what this row is claiming, and emptiness is the
            # same fact in every tree.
            name="touch",
            effects=[declare("writes_path", scope="production", write="create")],
        ),
        ShellCommandRule(
            # -l/-L print fingerprints and public keys — the read-only
            # diagnostic for push-auth failures; every other form mutates the
            # agent.
            name="ssh-add",
            # The agent is the user's, not this session's, and it outlives the
            # session either way -- which is machine state that is neither
            # this checkout nor another host.
            effects=[
                declare(
                    "mutates_environment", scope="credential agent", reach="credential"
                )
            ],
            refuses="credential-agent changes stay with the user",
            allow_flags=["-l", "-L"],
            reason="credential-agent changes stay with the user",
            recovery="Ask the user to run it.",
        ),
        ShellCommandRule(
            name="ssh-agent",
            effects=[
                declare(
                    "mutates_environment", scope="credential agent", reach="credential"
                )
            ],
            refuses="credential-agent lifecycle stays with the user",
            reason="credential-agent lifecycle stays with the user",
            recovery="Ask the user to run it.",
        ),
        ShellCommandRule(
            name="sort",
            effects=[declare("reads_path", scope="project")],
            # The two halves of what was one list, and the reason it had to
            # be two: `-o` lands the sorted output at a path, and
            # `--compress-program` names a program run over the temporaries.
            write_flags=["-o", "--output"],
            ask_flags=["--compress-program"],
            flag_effects=[
                declare(
                    "runs_undeclared_program",
                    scope="a program a flag names",
                    reach="container",
                )
            ],
            reason="a sort flag that writes a file or runs a program requires approval",
        ),
        ShellCommandRule(
            # Listing a directory is a read, and `-o` lands that listing in a
            # file — the same flag on the same kind of tool as `sort -o`.
            name="tree",
            effects=[declare("reads_path", scope="project")],
            write_flags=["-o"],
            checkpoint="boundary_wide",
            reason="a tree flag that writes a file requires approval",
        ),
        ShellCommandRule(
            # A search that runs a program. `--pre` and `--hostname-bin` name
            # one ripgrep invokes for every file it touches, which is
            # arbitrary execution wearing a search's clothes, and `-z` hands
            # the input to whichever decompressor the extension implies.
            name="rg",
            effects=[declare("reads_path", scope="project")],
            ask_flags=["--pre", "--hostname-bin", "--search-zip", "-z"],
            flag_effects=[
                declare(
                    "runs_undeclared_program",
                    scope="a program a flag names",
                    reach="container",
                )
            ],
            reason="a ripgrep flag that runs another program requires approval",
        ),
        ShellCommandRule(
            # Encoding is a filter; `-o` is the one form that lands a file.
            name="base64",
            effects=[declare("reads_path", scope="project")],
            write_flags=["-o", "--output"],
            checkpoint="boundary_wide",
            reason="a base64 flag that writes a file requires approval",
        ),
        ShellCommandRule(
            name="yq",
            effects=[declare("reads_path", scope="project")],
            # Not `write_flags`, though every one of these writes. That list
            # is for options whose *value* is the path, and none of these
            # carries one: `-i` is a bare marker and the file it rewrites is
            # the operand, while `-s` takes an expression that computes the
            # names it splits into. Nothing here resolves to a path, so the
            # row keeps its own question rather than relaxing on a word that
            # was never a filename.
            ask_flags=["-i", "--inplace", "--in-place", "-s", "--split-exp"],
            checkpoint="boundary_wide",
            reason=(
                "a yq flag that edits files in place or splits into files"
                " requires approval"
            ),
        ),
        ShellCommandRule(
            # xmllint reads every option with one or two leading dashes, so
            # both spellings are guarded; a two-char "-o" guard would
            # cluster-match benign words like -noout, and no bare "-o" option
            # exists.
            name="xmllint",
            effects=[declare("reads_path", scope="project")],
            write_flags=["--output", "-output"],
            ask_flags=["--shell", "-shell"],
            flag_effects=[
                declare(
                    "runs_undeclared_program",
                    scope="a program a flag names",
                    reach="container",
                )
            ],
            reason=(
                "an xmllint flag that writes files or opens a shell requires approval"
            ),
        ),
        ShellCommandRule(
            # -exec/-execdir payloads recurse through the kernel's find
            # screen; only the file-writing and deleting actions remain
            # flag-guarded.
            name="find",
            effects=[declare("reads_path", scope="project")],
            # The printing actions each name the file they land in; `-delete`
            # names nothing and removes what it matched, so it is not a write
            # this row can resolve a path for.
            write_flags=["-fprint", "-fprint0", "-fprintf", "-fls"],
            ask_flags=["-delete"],
            checkpoint="boundary_wide",
            reason="a mutating find action requires approval",
        ),
        ShellCommandRule(
            # `ss -K` closes established sockets; every other form reports.
            name="ss",
            effects=[declare("reads_path", scope="project")],
            ask_flags=["-K", "--kill"],
            checkpoint="boundary_wide",
            reason="killing sockets requires approval",
        ),
        ShellCommandRule(
            # Establishing that a port answers is how a session learns the
            # service it just started came up, and `-z` does exactly that and
            # nothing else: connect, report, close. Every other form moves
            # bytes — `-l` listens, `-e`/`-c` hand the socket to a program.
            name="nc",
            # Reaching a host is what it does; `-z` pins it to asking whether
            # a port answers, which is the reading form the verb list keeps.
            effects=[declare("reaches_host", scope="netcat")],
            refuses="netcat moves data unless -z pins it to a port scan",
            read_verbs=["-z"],
            ask_flags=["-l", "-e", "-c"],
            reason="netcat moves data unless -z pins it to a port scan",
        ),
        ShellCommandRule(
            name="cd",
            effects=[declare("changes_nothing", reason="directory navigation")],
            reason="directory navigation",
        ),
    ]


def devtools_rules() -> list[ShellSubcommandRule]:
    """The verbs of lup's own toolchain that a person answers, not the session.

    Two kinds, which differ in who the person is. Answering the review queue
    and accepting replacement policy are the operator's, from a terminal
    outside the session, so the requester is refused outright rather than
    asked. Retiring a claim and widening what a later launch reaches are
    questions for whoever is watching.

    The widening is the registry's. `sync.json` and `sync.json.local` are
    protected edit roots because a registration there decides what a session
    may mount, at which mode, and cloned from which repository -- and a
    command writing the same key unasked is the confined session choosing
    what confines it by another spelling. So every writer that can add or
    move a mount, change the repository a registration names, or grant a
    device asks the way an edit of that file does, and a launcher flag that
    lends the one session it opens a folder or a device asks as well. Each
    reason is the one line an approver reads, naming what widens.

    What only reads or keeps books stays allowed: `sync status`, `fetch`,
    `log`, `diff` and `mark-synced`; `sync revoke`, which only narrows; `dev
    library git` without `--url`, whose pin keeps the repository it names;
    and every dry run, which writes nothing.
    """
    registry_write = declare("writes_path", scope="protected", write="overwrite")
    registry_recovery = (
        "A registration decides what every later launch reaches, so widening "
        "one is the user's call: `sync status` shows what each reaches now, and "
        "where nobody can approve this, report the command for the user to run."
    )
    return [
        ShellSubcommandRule(
            name="review",
            operations=[
                ShellOperationRule(
                    name=action,
                    operator_only=True,
                    reason="a requesting agent cannot approve or decline a review",
                    recovery=(
                        "The operator answers on the dashboard or from a terminal "
                        "outside the agent session."
                    ),
                )
                for action in ("approve", "decline")
            ],
        ),
        ShellSubcommandRule(
            name="dashboard",
            operations=[
                ShellOperationRule(
                    name="serve",
                    operator_only=True,
                    reason="a requesting agent cannot mint operator credentials for the dashboard",
                    recovery=(
                        "The operator serves it from a terminal outside the agent session."
                    ),
                ),
                # Opening reads the capability the page is opened with, and
                # stopping takes the page away from every session's operator;
                # `status` reads neither, and stays the session's to ask.
                ShellOperationRule(
                    name="open",
                    operator_only=True,
                    reason="a requesting agent cannot read the dashboard's operator credentials",
                    recovery=(
                        "`dashboard status` says where it is; the operator opens "
                        "it from a terminal outside the agent session."
                    ),
                ),
                ShellOperationRule(
                    name="stop",
                    operator_only=True,
                    reason="a requesting agent cannot stop the operator's dashboard",
                    recovery=(
                        "The operator stops it from a terminal outside the agent "
                        "session; it stops by itself once the last session ends."
                    ),
                ),
            ],
        ),
        ShellSubcommandRule(
            name="dev",
            operations=[
                # Retiring deletes the note and the words it was written in,
                # which is the one step of the verify-solved pass nothing can
                # undo: a claim wrongly retired takes the concern with it,
                # while a claim wrongly restored costs one more pass. The
                # reader deciding that is the person the note was written for,
                # so the judgement is theirs to confirm rather than the
                # session's to record. `--restore` is left alone -- it keeps
                # the original words and only reopens the question.
                ShellOperationRule(
                    name="comments",
                    ask_flags=["--retire"],
                    reason=(
                        "retiring a claimed-resolved note deletes what was asked, "
                        "and only a reader who checked the code can say it was met"
                    ),
                    recovery=(
                        "Read the claim against the code first — "
                        "`uv run lup-devtools dev comments` prints each with its "
                        "original words. Where it is not met, `--restore` reopens "
                        "it with those words intact, and `--narrow` reopens the "
                        "part still outstanding."
                    ),
                ),
                # Files a GitHub issue on whichever tracker owns the component,
                # the act `gh issue create` asks about. `--issue N` corrects a
                # report already filed, which a follow-up restores the way an
                # edited issue is.
                ShellOperationRule(
                    name="report-friction",
                    effect_class="publication",
                    reviewer="human_only",
                    amending_flags=["--issue"],
                    reason=(
                        "filing a friction report opens an issue the tracker's"
                        " watchers are notified of"
                    ),
                    recovery=(
                        "`--issue N` adds to a report already filed instead; "
                        "`dev issues` lists the open ones."
                    ),
                ),
                # The scaffold's own initialization verb, whose body is
                # `lup.devtools.dev.origin`: it writes the URL the forge says
                # this project was generated from into the committed entry.
                ShellOperationRule(
                    name="upstream",
                    parents=["init"],
                    effects=[registry_write],
                    probe_flags=["--dry-run", "-n"],
                    reason=(
                        "rewrites the URL of the committed lup registration, the "
                        "repository every later launch clones and mounts under it"
                    ),
                    recovery=(
                        "`--dry-run` prints the URL it would write and writes "
                        "nothing; where nobody can approve the write, report that "
                        "URL for the user."
                    ),
                ),
                # The library's registration takes its URL from the git pin
                # wherever there is one (`lup.devtools.sync.completed`), so
                # repointing the pin repoints the registration. Without `--url`
                # the pin keeps the repository it names and only the ref moves,
                # and `dev library use` returns the registration to the URL the
                # registry files already declare -- neither widens anything.
                ShellOperationRule(
                    name="git",
                    parents=["library"],
                    ask_flags=["--url"],
                    flag_effects=[registry_write],
                    probe_flags=["--dry-run", "-n"],
                    reason=(
                        "the lup registration follows this pin, so `--url` names "
                        "the repository every later launch clones and mounts under it"
                    ),
                    recovery=(
                        "Without `--url` the pin keeps the repository it names and "
                        "only the ref moves; `--dry-run` shows the change without "
                        "writing it."
                    ),
                ),
                # Retiring the scaffold's Pyright environment pair rewrites the
                # manifest, a protected root, the way an edit of it would.
                ShellOperationRule(
                    name="pyright-environment",
                    parents=["migrate"],
                    effects=[
                        declare("writes_path", scope="protected", write="overwrite")
                    ],
                    probe_flags=["--dry-run"],
                    reason="retiring Pyright environment defaults rewrites protected pyproject.toml",
                    recovery="Review the change with --dry-run before applying the migration.",
                ),
            ],
        ),
        ShellSubcommandRule(
            name="harness",
            operations=[
                ShellOperationRule(
                    name="policy-refresh",
                    operator_only=True,
                    reason="a requesting agent cannot accept replacement destination policy",
                    recovery="The operator must refresh from a terminal outside the agent session.",
                ),
                # A launch from inside a session opens a session of its own, and
                # these flags lend it what no registration names -- for that one
                # launch, the widening a registration makes for every launch
                # after it -- or open it with no boundary at all. `--generate-only`
                # launches nothing, so lends nothing.
                #
                # Both stay inside a container the parent runs in: there is no
                # engine socket to lend a host folder through, and a child with
                # no sandbox of its own is still behind the parent's walls.
                *[
                    ShellOperationRule(
                        name=launcher,
                        ask_flags=["--mount", "--mount-ro", "--device", "--sandbox"],
                        setting_flags=["--sandbox"],
                        guarded_settings=["none"],
                        flag_effects=[
                            declare(
                                "mutates_environment",
                                scope="launch boundary",
                                reach="container",
                            ),
                            declare("escapes_containment", scope="child launch"),
                        ],
                        probe_flags=["--generate-only"],
                        reason=(
                            "`--mount`, `--mount-ro` and `--device` hand a host "
                            "folder or device to the session this launches, and "
                            "`--sandbox none` opens it with no boundary"
                        ),
                        recovery=(
                            "`--generate-only` generates without launching. A "
                            "session that needs another folder, a device or no "
                            "boundary is the user's to open, from their own "
                            "terminal."
                        ),
                    )
                    for launcher in ("claude", "codex")
                ],
            ],
        ),
        ShellSubcommandRule(
            name="sync",
            operations=[
                # With or without `--mount` on the line: a registration that
                # already carries one -- the lup entry every scaffold ships --
                # opens whatever path this names at the next launch.
                ShellOperationRule(
                    name="setup",
                    effects=[registry_write],
                    reason=(
                        "the path a registration names is what its mount opens to "
                        "every later launch, at the mode `--mount` gives or its "
                        "declaration already carries"
                    ),
                    recovery=registry_recovery,
                ),
                ShellOperationRule(
                    name="remote",
                    effects=[registry_write],
                    reason=(
                        "a registration's remote is the repository every later "
                        "launch clones and mounts under its name"
                    ),
                    recovery=registry_recovery,
                ),
                ShellOperationRule(
                    name="grant",
                    effects=[registry_write],
                    reason=(
                        "a device grant hands that host device to every session "
                        "later launched on this machine"
                    ),
                    recovery=registry_recovery,
                ),
            ],
        ),
        # Deleting a branch takes origin's copy along only once it is spent --
        # its commits reachable from the integration branch -- which loses
        # nothing. `--remote` deletes that copy whatever it holds, which is
        # the remote-branch deletion `git push --delete` asks about.
        ShellSubcommandRule(
            name="git",
            operations=[
                ShellOperationRule(
                    name="delete",
                    ask_flags=["--remote"],
                    probe_flags=["--dry-run", "-n"],
                    reason=(
                        "`--remote` deletes origin's copy of the branch even where"
                        " it holds commits no other branch has"
                    ),
                    recovery=(
                        "Without `--remote`, origin's copy goes only once the"
                        " integration branch holds its commits."
                    ),
                ),
            ],
        ),
    ]


def runner_target_rules(
    ambient: Sequence[str] = ("pyright", "pytest", "ruff"),
    session_opening: Sequence[str] = ("lup-devtools",),
    also: Sequence[str] = (),
) -> list[RunnerTargetRule]:
    """The ``uv run`` targets a project blesses, grouped by what each needs.

    A checker reads the tree and writes inside it, so it runs wherever the
    session runs and takes ``ambient``.

    A toolchain that opens agent sessions cannot. The runtime keeps
    per-session state under its own configuration directory — for Claude Code,
    ``~/.claude/session-env/<session id>``, following ``CLAUDE_CONFIG_DIR`` —
    and a session opened from inside a sandbox that does not grant that path
    dies on its first shell call with a bare ``EROFS``, which reads to an agent
    like a broken repository rather than like a boundary. It then retries,
    works around it, or reports success from a session that never ran a
    command; one planning run finished that way and looked normal.

    The deny is the runtime protecting its own configuration directory, and a
    grant does not lift it. A live session's filesystem policy shows the shape:
    the repository root sits in ``allowOnly`` while the configuration home
    below it — ``session-env`` and its neighbours — is listed again under
    ``denyWithinAllow``, a carve-out applied within the grant. The runtime
    enumerates that directory itself, so the entries appear whether or not a
    project declares them. Nor would lifting it be the
    remedy: the runtime chooses that path per session, so the grant is a family
    rather than a path; deriving a private configuration home does not move it,
    because every entry such a home does not own links back to the shared one;
    and session state is only the first thing such a
    toolchain writes outside the tree — a worktree, a plugin cache, and the
    git configuration behind them follow it.

    That requirement is the *boundary's* to meet rather than a placement to
    declare. A placement says where an operation runs; what a session-opening
    toolchain needs is for wherever it already runs to grant a path — which is
    a statement about the profile, measured at launch, and stated with the rest
    of the boundary. Declared as a placement instead it was unmeasurable: the
    profile that grants the path and the profile that does not both read as
    ``outside``, and the second one only finds out at the first shell call.

    Every target here does the one thing a blessing is: it runs something this
    repository declares as its own. What such a target then goes on to write
    is answered by the rows its own commands match, so nothing is said about
    it twice.

    ``also`` is the direction the three groups cannot reach: a name that is
    nobody else's. A module root goes there — the table answers
    ``uv run -m <root>.<module>`` by its root segment, so a project declaring
    ``examples`` admits every entry point beneath it — and so does a console
    script no library rule knows. What such a name is worth is the same
    ``runs_declared_target`` the rest carry, for the same reason: what a
    project declares as its own is reviewed as source before anything runs it.
    """
    return [
        RunnerTargetRule(
            name=name,
            effects=[declare("runs_declared_target")],
            subcommands=devtools_rules() if name == "lup-devtools" else [],
        )
        for name in (*ambient, *session_opening, *also)
    ]


# One criterion decides this table, applied across git's surface rather than to
# whichever word was last found missing: a subcommand belongs here when it moves
# no ref, mutates no index entry, and writes nothing into the working tree.
#
# That is a question about what a verb *reaches*, not about whether it happens
# to write bytes, which is why the object-construction verbs pass it. An object
# nothing points at is unreachable the moment it exists and git collects it, so
# there is no ref to restore and nothing to undo — `merge-tree --write-tree`,
# the way to ask whether two branches still merge, is as unremarkable as
# `merge-base`, and refusing it refuses the question rather than the write.
#
# What fails the criterion is absent on purpose and meets git's own deny:
# `read-tree` and `update-index` write the index, `update-ref` and `pack-refs`
# move refs, and `format-patch`, `unpack-file`, and `difftool --dir-diff` each
# land files in the working tree.
# lup: ignore[library-default] — git's own query and object-construction verbs, taken by the criterion above; which of them reaches a ref is a fact about git rather than a choice made for an adopter
GIT_READ_ONLY_SUBCOMMANDS = (
    # Reporting on the object store, the refs, the index, and the config.
    "status",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "ls-remote",
    "cat-file",
    "blame",
    "annotate",
    "describe",
    "rev-list",
    "name-rev",
    "merge-base",
    "show-ref",
    "for-each-ref",
    "count-objects",
    "cherry",
    "check-ignore",
    "check-attr",
    "check-mailmap",
    "check-ref-format",
    "column",
    "fmt-merge-msg",
    "show-branch",
    "show-index",
    "verify-commit",
    "verify-tag",
    "verify-pack",
    "pack-redundant",
    "fsck",
    "patch-id",
    "request-pull",
    "stripspace",
    "get-tar-commit-id",
    "var",
    "version",
    "help",
    # Constructing an object, which no ref yet points at.
    "merge-tree",
    "hash-object",
    "commit-tree",
    "mktree",
    "mktag",
    "write-tree",
)

# lup: ignore[library-default] — git subcommands the reflog or a second invocation undoes; fixed by what git records rather than by taste
GIT_REVERSIBLE_SUBCOMMANDS = (
    "add",
    "commit",
    "mv",
    "cherry-pick",
    "revert",
    "notes",
    "stage",
)
"""Subcommands whose changes the object store still holds afterwards.

`merge` is deliberately not among them. What it does to *this* checkout is as
reversible as a cherry-pick, but that is not what a merge is for: it is the
step that puts work onto a branch other people build on, so its row declares
that effect rather than one about recovering the tree.
"""

GIT_CONFIG_EXECUTING_KEYS = (
    "core.hookspath",
    "core.pager",
    "core.editor",
    "core.sshcommand",
    "core.fsmonitor",
    "alias.*",
    "credential.helper",
    "credential.*.helper",
    "init.templatedir",
    "merge.*.driver",
    "filter.*.clean",
    "filter.*.smudge",
    "diff.*.command",
    "diff.*.textconv",
    "url.*.insteadof",
)
"""The git settings whose value is a program, or decides which one runs.

Writing any of these arranges for code to run at somebody else's next git
command, which is what separates them from the rest of `git config`: setting
`user.email` or a branch's base records a fact, and setting `core.hooksPath`
hands over execution. The distinction is the whole reason the row can ask
about one and not the other, and it is why this list is about execution
rather than about importance -- a key that merely matters is not one of
these.

Lowercase because the match folds case, and globbed where git lets the caller
name the middle segment. `credential.*.helper` is listed beside
`credential.helper` because the per-URL form is a separate key rather than a
spelling of the same one, and it runs a program just as readily.
"""

GIT_CONFIG_RETARGETING_KEYS = (
    "remote.*.url",
    "remote.*.pushurl",
    "remote.*.push",
    "remote.*.mirror",
)
"""The git settings that name where a later command's work lands.

A second class beside the executing keys, guarded by the same absence test
because it answers the same question about a write: is what this sets read
back by something that then acts on the caller's behalf. `remote.<name>.url`
is where a push lands and where a fetch comes from, and it is also how `gh`
resolves which repository an issue comment, a close, or a pull request is
about — so a write here moves the whole compensable band of forge operations
onto a repository nobody approved, without touching one of them.

`remote.<name>.push` and `remote.<name>.mirror` retarget within it: the first
is the refspec a push naming no branch runs, which a leading plus forces or an
empty source deletes, and the second makes every push a mirror, deleting each
remote branch this checkout lacks. Either turns a later plain `git push` into
the force or the deletion that asks when it is spelled on the command line.

Only the keys that name a destination outright. `remote.pushdefault` and
`branch.<name>.pushremote` choose among the remotes the table already holds,
and every way of putting one there — `git remote add`, `git remote rename`,
`git remote set-url` and these keys — asks, so choosing between destinations
somebody approved is not the retarget.
"""


INTEGRATION_BRANCHES = ("main", "master")
"""The branches a forced push asks about even under a lease.

The names a forge gives a repository's default branch, which is the one
branch every project has that other people build on. A project integrating
through a second long-lived branch names it beside them.
"""


def git_rule(
    integration_branches: tuple[str, ...] = INTEGRATION_BRANCHES,
    redirect_checkout: bool = False,
    sandbox: SandboxPlacement = "ambient",
    config_executing_keys: tuple[str, ...] = GIT_CONFIG_EXECUTING_KEYS,
    config_retargeting_keys: tuple[str, ...] = GIT_CONFIG_RETARGETING_KEYS,
    push_destinations: tuple[DestinationForm, ...] = ("url", "path"),
) -> ShellCommandRule:
    """Compile the git surface: reads and reversible work allow, losses ask.

    The judgements a project can reasonably differ on are parameters rather
    than a reason to fork the table.

    ``integration_branches`` are the branches other people build on, which
    is what decides whether replacing what a remote ref points at is worth a
    question. A review flow rebases and republishes its own branch every
    round, so a force there is the ordinary case -- and under
    ``--force-with-lease`` it replaces only what this checkout last saw, so
    it discards nothing anybody else pushed, and allows. Every other force
    asks: ``--force`` and a refspec's leading plus replace whatever the
    remote holds, a leased force onto an integration branch rewrites what
    others built on, and a leased force naming no branch reaches whichever
    one the checkout stands on. What removes a ref outright asks whatever it
    names: no second push restores it.

    Both effects are read twice over, because push spells each of them
    twice: as a flag, and as refspec grammar. ``--delete origin main`` and
    ``origin :refs/heads/main`` remove the same ref, ``--force`` and
    ``+main:main`` replace the same one, and a guard written only as flag
    spellings held the first half of each pair while allowing the second.

    ``redirect_checkout`` decides how ``git checkout`` is met. Off, it asks —
    the branch-switching form is harmless, but ``checkout -- <path>``
    discards work. On, it denies and names ``switch`` and ``restore``
    instead, which suits a project that has settled on the newer verbs. The
    ref-sourced ``checkout <ref> -- <path>`` form is recognized by the kernel
    ahead of this row either way, because committed content stays
    recoverable.

    ``sandbox`` is where git runs, stated once here and inherited by every
    subcommand. The default is ``ambient``, because what git needs is not the
    launcher's host but a boundary that grants a route to the remote and the
    repository's own locks — which is a fact about the profile, declared and
    measured with the rest of the boundary rather than requested per command.
    A profile whose boundary cannot grant them says so at launch, where the
    gap is actionable; a placement could only say it per call, after the
    failure. ``outside`` remains available for a verb that genuinely has to
    run on the launcher's host, and is reviewed every time it is used.

    ``config_executing_keys`` are the settings whose value is a program, and
    so the only `git config` writes worth a question. A project can add to
    them -- a bespoke `merge.*.driver` family, or a key its own tooling reads
    and executes -- and one that keeps its configuration under review by
    other means can pass fewer. Passing none makes every config write allow,
    which is a coherent answer for a project whose config is not writable
    from where the agent runs; it is not the default, because it usually is.

    ``config_retargeting_keys`` are the settings that decide which repository
    a later command talks to, guarded through the same absence test and kept
    separate because the two classes are separate claims: one is about what
    runs, the other about where the work goes. A project whose forge access
    is scoped elsewhere -- a token that reaches one repository and no other --
    can pass none of them and lose nothing.

    ``push_destinations`` are the ways of naming a repository inline that a
    push has to ask about. `git push <url> main` needs no remote and writes
    no configuration, so every guard over the remote table looks straight
    past it -- and the table is worth trusting precisely because putting a
    destination in it asks. Both forms are guarded by default. A project that
    mirrors into a bare repository beside its checkout drops ``path`` and
    keeps the one that leaves the machine; a project whose network is closed
    to everything but its forge can drop ``url`` on the same reasoning, and
    one that passes neither is saying its push has nowhere unapproved to go.

    Structural, and only structural: whether a bare word is a remote this
    repository holds is a question for `git remote`, which the kernel reading
    this is hermetic in order not to run. A bare name that is not configured
    reaches nothing -- git fails before any object moves -- so the forms that
    do reach somewhere are the whole of what is left to guard.
    """
    # One statement of which settings are worth a question, read by both
    # spellings that reach them: the `config` verb and the `-c` global.
    guarded_config = [*config_executing_keys, *config_retargeting_keys]
    leaf = [
        *[
            ShellSubcommandRule(
                name=name,
                effects=[declare("reads_path", scope="project")],
            )
            for name in GIT_READ_ONLY_SUBCOMMANDS
        ],
        *[
            ShellSubcommandRule(
                name=name,
                effects=[declare("mutates_repository", scope="reversible")],
            )
            for name in GIT_REVERSIBLE_SUBCOMMANDS
        ],
        ShellSubcommandRule(name="merge", effects=[declare("integrates")]),
    ]
    # `--repo` is the one spelling of a destination the operand reading below
    # cannot reach: it carries the repository as a flag value, and a flag is
    # exactly what that reading skips. It asks whatever it names, because the
    # flag is legacy — git documents it as relevant only when no repository
    # operand is passed — and a question on an invocation nobody writes costs
    # less than a second reader for one word. `--mirror` and `--prune` delete
    # every remote branch this checkout lacks, so they ask beside `--delete`;
    # `--receive-pack` names the program the far side runs, the question
    # `fetch --upload-pack` asks from the other direction.
    push_flags = [
        "--delete",
        "-d",
        "--mirror",
        "--prune",
        "--repo",
        "--receive-pack",
        "--exec",
    ]
    guarded = [
        *[
            ShellSubcommandRule(
                name=name,
                # The read is what the verb does; `--output` is what one flag
                # makes it also do, and the flag still carries that alone
                # until an argument shape can add an effect rather than
                # escalate a verdict.
                effects=[declare("reads_path", scope="project")],
                write_flags=["--output"],
                checkpoint="boundary_wide",
                reason="writing command output to a file requires approval",
            )
            # `--output` names a path on the command line and lands a file
            # there, which is the clause that disqualified `format-patch` from
            # the query family above. The plumbing spellings are not a quieter
            # kind of read: `diff-tree --output=` lands a file exactly where
            # `log --output=` does.
            #
            # The guard has to follow forwarding rather than only the verbs
            # that document the flag, because several reach it by handing their
            # arguments to `log` or `diff` — `stash list`, `stash show` and
            # `bisect view` carry it for that reason, on their own rows below.
            # Where a verb forwards is a fact about git worth checking against
            # its source; guarding one that turns out to reject the flag costs
            # nothing, since git refuses it either way.
            #
            # `--ext-diff` and `--textconv` are deliberately absent, by the
            # same reasoning that leaves `--paginate` alone: neither names a
            # program. They enable a driver already configured, and reaching
            # that configuration means `-c` or `git config`, which ask. That is
            # what separates them from `rg --pre`, which takes its program as
            # the next word.
            for name in (
                "log",
                "diff",
                "show",
                "whatchanged",
                "diff-tree",
                "diff-index",
                "diff-files",
                "diff-pairs",
                "range-diff",
                "shortlog",
            )
        ],
        ShellSubcommandRule(
            name="grep",
            effects=[declare("reads_path", scope="project")],
            ask_flags=["-O", "--open-files-in-pager"],
            flag_effects=[
                declare(
                    "runs_undeclared_program",
                    scope="a program a flag names",
                    reach="container",
                )
            ],
            reason="opening matches in an arbitrary program requires approval",
        ),
        ShellSubcommandRule(
            name="rebase",
            # Rewrites commits the reflog still holds, which is what makes
            # replaying them ordinary and `--exec` a separate question.
            effects=[declare("mutates_repository", scope="reversible")],
            ask_flags=["-x", "--exec"],
            flag_effects=[
                declare(
                    "runs_undeclared_program",
                    scope="a program a flag names",
                    reach="container",
                )
            ],
            reason="replaying commits through a shell command requires approval",
        ),
        # Both reach a declared remote and land objects in the store. Only
        # `pull` puts any of it in the working tree, and it does so through a
        # merge of refs this repository already tracks -- which is why neither
        # carries the trust row that `clone` and `submodule` do, where the
        # code arriving is somebody else's by definition.
        ShellSubcommandRule(
            name="fetch",
            effects=[
                declare("fetches", scope="declared"),
                declare("mutates_repository", scope="reversible"),
            ],
            ask_flags=["--upload-pack"],
            reason="overriding the transport program requires approval",
        ),
        ShellSubcommandRule(
            name="pull",
            effects=[
                declare("fetches", scope="declared"),
                declare("mutates_repository", scope="reversible"),
            ],
            ask_flags=["--upload-pack"],
            reason="overriding the transport program requires approval",
        ),
        ShellSubcommandRule(
            name="push",
            effects=[declare("publishes", scope="branch")],
            ask_destinations=list(push_destinations),
            ask_refspecs=["delete"],
            ask_flags=push_flags,
            force_flags=["-f", "--force"],
            lease_flags=["--force-with-lease"],
            protected_refs=list(integration_branches),
            value_flags=[
                "-o",
                "--push-option",
                "--repo",
                "--receive-pack",
                "--exec",
                "--recurse-submodules",
            ],
            probe_flags=["-n", "--dry-run"],
            reason=(
                "deleting a remote branch loses work no later push restores, and"
                " --repo or --receive-pack redirects the push; each needs approval"
            ),
        ),
        # The same arrival `gh repo clone` is, reached by the other spelling:
        # a whole tree of somebody's code, landing where a build can run it.
        # Where the tree lands is the whole of what the question weighs: a
        # clone into a directory the container owns reaches nothing a build
        # outside it would run, and one into the checkout is the arrival.
        ShellSubcommandRule(
            name="clone",
            effects=[
                declare("fetches", scope="undeclared", reach="mount"),
                declare("installs_dependency", scope="repository", reach="mount"),
                declare("writes_path", scope="production", write="create"),
            ],
            landing_operands=2,
            landing_flags=["--separate-git-dir"],
            reason="cloning fetches external code",
        ),
        ShellSubcommandRule(
            name="apply",
            # Replaces tracked content wholesale, which is what the write row
            # refuses -- but refusing is the one answer that cannot be right
            # here. A refusal names a route this project prefers, and for a
            # patch there is none: re-authoring every hunk through `Edit` is
            # not the same operation, it is a transcription of it.
            #
            # So the content is read where it can be, which is afterwards.
            # `written_review` asks Git which paths the patch touches and puts
            # each result to the gates an edit passes -- the same arrangement
            # a redirection already gets, reached by the one route whose
            # targets are inside a file rather than in the command.
            effects=[
                declare(
                    "writes_path",
                    scope="production",
                    write="overwrite",
                    reviewed=True,
                )
            ],
            ask_flags=["--unsafe-paths", "--build-fake-ancestor"],
            # Each lands a write where no snapshot of this checkout reaches --
            # a patch path outside it, an index file wherever it was named --
            # so the question stands however much of the checkout was captured.
            flag_effects=[
                declare("writes_path", scope="outside", write="overwrite"),
            ],
            checkpoint="boundary_wide",
            reason="a patch that writes outside the working area requires approval",
        ),
        ShellSubcommandRule(
            name="restore",
            # Discards working-tree changes, which is the one thing in a
            # checkout the object store was never holding.
            effects=[declare("destroys_uncaptured", scope="targeted")],
            checkpoint="targeted",
            reason="restoring files discards working-tree changes",
        ),
        ShellSubcommandRule(
            name="rm",
            effects=[declare("destroys_uncaptured", scope="targeted")],
            probe_flags=["-n", "--dry-run"],
            checkpoint="targeted",
            reason="removing tracked files requires approval",
        ),
        ShellSubcommandRule(
            # The one destructive git verb the snapshot does not answer, and
            # the reason it keeps asking wherever the others stop. `-fdx`
            # takes ignored files, and ignored files are exactly what the
            # snapshot leaves out: `.env.local`, the resolver's state, a
            # virtual environment. Naming a restorer here would relax the one
            # command whose whole purpose is destroying what nothing holds.
            name="clean",
            effects=[declare("destroys_uncaptured", scope="unrecoverable")],
            # Only the literal spellings: `-fdxn` is a cluster this cannot
            # read, and it keeps asking rather than trusting the `n`.
            probe_flags=["-n", "--dry-run"],
            reason="deleting untracked files is destructive",
        ),
        ShellSubcommandRule(
            name="config",
            # A write to a file a rule names, which is what `protected` is
            # for. What makes it worth naming is on the key list below: some
            # of these settings are the program git runs next.
            effects=[declare("writes_path", scope="protected", write="overwrite")],
            # Where the write lands, when the key says nothing about it. The
            # guarded keys below judge a write to this repository's own
            # configuration; these flags aim the same write at a file the
            # caller names, so a key that reads as ordinary is not. `--edit`
            # defeats the same test from the other side: it opens every key
            # in the file while naming none, so absence of a guarded word is
            # not absence of a guarded write.
            ask_flags=["--file", "-f", "--blob", "--edit", "-e"],
            read_verbs=[
                "--get",
                "--get-all",
                "--get-regexp",
                "--get-urlmatch",
                "--get-color",
                "--get-colorbool",
                "--list",
                "-l",
            ],
            guarded_keys=guarded_config,
            reason=(
                "this git config write can change what program git runs, which"
                " repository it talks to, or what a later push forces or deletes"
            ),
        ),
        ShellSubcommandRule(
            name="checkout",
            # What the verb does is the same either way the parameter goes:
            # `checkout -- <path>` discards working-tree changes. What
            # `redirect_checkout` changes is which spelling reaches that,
            # which is a refusal rather than a different effect -- `switch`
            # and `restore` do the same things to the same checkout.
            effects=[declare("destroys_uncaptured", scope="targeted")],
            refuses=(
                "this project checks out through git switch and git restore"
                if redirect_checkout
                else ""
            ),
            checkpoint="targeted",
            reason=(
                "this project checks out through git switch and git restore"
                if redirect_checkout
                else "checkout can discard working-tree changes"
            ),
            recovery=(
                "Use git switch for branches or git restore for files."
                if redirect_checkout
                else ""
            ),
        ),
        ShellSubcommandRule(
            name="reflog",
            effects=[declare("reads_path", scope="project")],
            operations=[
                # Unrecoverable in the strict sense the scope means: the
                # reflog is what would have recovered the thing being
                # removed, so no capture is holding it once this runs.
                ShellOperationRule(
                    name="expire",
                    effects=[declare("destroys_uncaptured", scope="unrecoverable")],
                    reason="expiring reflog entries is destructive",
                ),
                ShellOperationRule(
                    name="delete",
                    effects=[declare("destroys_uncaptured", scope="unrecoverable")],
                    reason="deleting reflog entries is destructive",
                ),
            ],
        ),
        ShellSubcommandRule(
            name="branch",
            effects=[declare("mutates_repository", scope="reversible")],
            ask_flags=["-d", "-D", "--delete", "-m", "-M", "--move"],
            reason="deleting or moving a branch requires approval",
        ),
        ShellSubcommandRule(
            name="bisect",
            effects=[declare("destroys_uncaptured", scope="targeted")],
            operations=[
                ShellOperationRule(
                    name="log",
                    effects=[declare("reads_path", scope="project")],
                ),
                ShellOperationRule(
                    # Falls back to `git log` where no display is available,
                    # and hands it the arguments it was given.
                    name="view",
                    effects=[declare("reads_path", scope="project")],
                    write_flags=["--output"],
                    checkpoint="boundary_wide",
                    reason="writing command output to a file requires approval",
                ),
            ],
            checkpoint="targeted",
            reason="a bisect step moves HEAD across commits",
        ),
        # The reason has always said what this is: fetching and checking out
        # external code. That is the trust row the clones reach, spelled as a
        # git verb, so it answers there rather than on a rule of its own.
        ShellSubcommandRule(
            name="submodule",
            effects=[
                declare("fetches", scope="undeclared"),
                declare("installs_dependency", scope="submodule"),
            ],
            operations=[
                ShellOperationRule(
                    name="status",
                    effects=[declare("reads_path", scope="project")],
                ),
                ShellOperationRule(
                    name="summary",
                    effects=[declare("reads_path", scope="project")],
                ),
            ],
            reason="submodule operations fetch and check out external code",
        ),
        ShellSubcommandRule(
            name="tag",
            effects=[declare("mutates_repository", scope="reversible")],
            ask_flags=["-d", "--delete"],
            reason="deleting a tag requires approval",
        ),
        ShellSubcommandRule(
            # The one query verb whose write form is spelled by arity rather
            # than by a flag: a second operand points the ref somewhere else.
            # The kernel recognizes the reading form ahead of this row.
            name="symbolic-ref",
            # A write to a ref this rule names, which is what `protected` is:
            # the reflog holds where HEAD was, so the question is about the
            # ref rather than about recovering anything.
            effects=[declare("writes_path", scope="protected", write="overwrite")],
            reason="pointing a symbolic ref somewhere else moves HEAD",
        ),
        ShellSubcommandRule(
            name="reset",
            effects=[declare("mutates_repository", scope="reversible")],
            ask_flags=["--hard", "--merge", "--keep"],
            # Moving a ref is what the object store already holds. Each of
            # these also throws away what the working tree was carrying, which
            # no commit records, so the escalated command is a different
            # operation and says so.
            flag_effects=[
                declare(
                    "destroys_uncaptured",
                    scope="targeted",
                    reason="a working-tree-destroying reset requires approval",
                )
            ],
            checkpoint="targeted",
            reason="a working-tree-destroying reset requires approval",
        ),
        ShellSubcommandRule(
            name="switch",
            effects=[declare("mutates_repository", scope="reversible")],
            ask_flags=["-f", "--force", "--discard-changes"],
            # Switching refuses to lose work; forcing it is the instruction to
            # lose the work anyway, which is the whole of the difference.
            flag_effects=[
                declare(
                    "destroys_uncaptured",
                    scope="targeted",
                    reason="a force switch can discard working-tree changes",
                )
            ],
            checkpoint="targeted",
            reason="a force switch can discard working-tree changes",
        ),
        ShellSubcommandRule(
            name="worktree",
            effects=[declare("unclassified_operation", scope="git worktree")],
            operations=[
                ShellOperationRule(
                    name="list",
                    effects=[declare("reads_path", scope="project")],
                ),
                # Lands a checkout at a path nothing was occupying, which is
                # the create the write row never questions.
                ShellOperationRule(
                    name="add",
                    effects=[
                        declare("writes_path", scope="production", write="create"),
                        declare("mutates_repository", scope="reversible"),
                    ],
                ),
                ShellOperationRule(
                    name="move",
                    effects=[declare("mutates_repository", scope="reversible")],
                ),
                ShellOperationRule(
                    name="repair",
                    effects=[declare("mutates_repository", scope="reversible")],
                ),
                # A worktree holds whatever was not committed in it, and no
                # capture of this checkout reaches into another one.
                ShellOperationRule(
                    name="remove",
                    effects=[declare("destroys_uncaptured", scope="unrecoverable")],
                    reason="removing a worktree deletes it",
                ),
                ShellOperationRule(
                    name="prune",
                    effects=[declare("destroys_uncaptured", scope="unrecoverable")],
                    reason="pruning worktrees is destructive",
                ),
            ],
            reason="this worktree operation is not classified",
        ),
        ShellSubcommandRule(
            name="stash",
            effects=[declare("mutates_repository", scope="reversible")],
            operations=[
                ShellOperationRule(
                    # Forwards its arguments to `git log`, `--output` included.
                    name="list",
                    effects=[declare("reads_path", scope="project")],
                    write_flags=["--output"],
                    checkpoint="boundary_wide",
                    reason="writing command output to a file requires approval",
                ),
                ShellOperationRule(
                    # Accepts any format git diff knows, `--output` included.
                    name="show",
                    effects=[declare("reads_path", scope="project")],
                    write_flags=["--output"],
                    checkpoint="boundary_wide",
                    reason="writing command output to a file requires approval",
                ),
                # Putting work into the stash and taking it back out. The
                # object store holds what each of these moves, so a later
                # command reaches it by an ordinary operation and no snapshot
                # is standing in for one.
                ShellOperationRule(
                    name="push",
                    effects=[declare("mutates_repository", scope="reversible")],
                ),
                ShellOperationRule(
                    name="save",
                    effects=[declare("mutates_repository", scope="reversible")],
                ),
                ShellOperationRule(
                    name="pop",
                    effects=[declare("mutates_repository", scope="reversible")],
                ),
                ShellOperationRule(
                    name="apply",
                    effects=[declare("mutates_repository", scope="reversible")],
                ),
                # A dropped stash leaves a commit nothing references, which
                # is not what a snapshot of the working tree is holding.
                ShellOperationRule(
                    name="drop",
                    effects=[declare("destroys_uncaptured", scope="unrecoverable")],
                    reason="dropping a stash is destructive",
                ),
                ShellOperationRule(
                    name="clear",
                    effects=[declare("destroys_uncaptured", scope="unrecoverable")],
                    reason="clearing stashes is destructive",
                ),
            ],
        ),
        ShellSubcommandRule(
            name="remote",
            effects=[declare("mutates_repository", scope="reversible")],
            operations=[
                # Removing takes the tracking refs with it, and pruning is
                # that deletion on its own. Neither is in the object store
                # afterwards in any form a later command reaches by name.
                ShellOperationRule(
                    name="remove",
                    effects=[declare("destroys_uncaptured", scope="targeted")],
                    reason="removing a remote requires approval",
                ),
                ShellOperationRule(
                    name="rm",
                    effects=[declare("destroys_uncaptured", scope="targeted")],
                    reason="removing a remote requires approval",
                ),
                # Destroys nothing and decides where a later command sends
                # this repository, which is the configured-path question
                # rather than the loss. `add` puts a destination in the table
                # that was not there — `git push <name>` at a named remote
                # allows, so the approval a repository-wide copy needs is
                # this one — and `rename` moves which destination answers to
                # `origin`, which is the name `gh` follows.
                ShellOperationRule(
                    name="add",
                    effects=[
                        declare("writes_path", scope="protected", write="overwrite")
                    ],
                    reason="adding a remote requires approval",
                ),
                ShellOperationRule(
                    name="rename",
                    effects=[
                        declare("writes_path", scope="protected", write="overwrite")
                    ],
                    reason="renaming a remote requires approval",
                ),
                ShellOperationRule(
                    name="set-url",
                    effects=[
                        declare("writes_path", scope="protected", write="overwrite")
                    ],
                    reason="changing a remote URL requires approval",
                ),
                ShellOperationRule(
                    name="prune",
                    effects=[declare("destroys_uncaptured", scope="targeted")],
                    reason="pruning a remote is destructive",
                ),
            ],
        ),
    ]
    # The globals that change how git *runs* are judged here rather than per
    # subcommand, because the subcommand word is found only after these are
    # read: `--exec-path` names where git's own programs come from,
    # `--super-prefix` and `--namespace` redirect what a ref means, and `-c`
    # carries a setting, judged by its key below.
    #
    # The three that name a directory are value flags and not guarded. They
    # move the command to another tree, and the verb behind them is judged by
    # its own row there exactly as `cd there && git <verb>` is judged by two
    # allowed segments -- so a question on the redirect deterred nothing and
    # cost a turn on every sibling worktree and every project the sync
    # registry mounts, each addressed by absolute path. The reflog that makes
    # a commit reversible is the other tree's, and it undoes the commit
    # exactly as this one's would.
    #
    # `--paginate` is deliberately not among them, though it is on the list this
    # sweep was measured against. It moves no ref, no index entry, and no file:
    # it forces the pager these subcommands already run by default, and the
    # program that pager names is reachable only through `-c` or `git config`,
    # which ask. Gating it would spend a question on the flag rather than on
    # what the flag could reach.
    # Three globals consume the word after them, and one of the three moves
    # where every operand resolves: `-C` runs git as though it had been
    # started in that directory. `--git-dir` names a repository and moves
    # nothing, and `--work-tree` moves only a pathspec, and only where git
    # stands outside the tree -- a reading of git's own the kernel makes.
    # Two lists, because they answer two questions.
    valued_flags = ["-C", "--git-dir", "--work-tree"]
    return ShellCommandRule(
        name="git",
        effects=[declare("unclassified_operation", scope="git")],
        # `git version` is classified read-only as a subcommand, and the same
        # question spelled as a flag was reaching the default deny -- so the
        # policy answered "this git subcommand is not classified" about a
        # command carrying no subcommand at all. The same shape `bun` fixes
        # above, and the same class as the pure reads §4.2 closed: asking a
        # program what it is cannot change anything.
        allow_flags=["--version", "--help"],
        ask_flags=[
            "-c",
            "--config-env",
            "--exec-path",
            "--super-prefix",
            "--namespace",
        ],
        value_flags=valued_flags,
        directory_flags=["-C"],
        # The two globals that set a setting, judged by the same keys the
        # `config` verb is judged by — one statement about which settings hand
        # over execution or a destination, answering both spellings. Only
        # `-c` and `--config-env` are here: the directory flags carry a path,
        # and `--exec-path` and `--super-prefix` carry one too.
        setting_flags=["-c", "--config-env"],
        guarded_settings=guarded_config,
        # What a guarded setting runs, it runs where the session runs -- a
        # pager, an editor, a merge driver -- except for these: a credential
        # helper is handed a lent secret, and the rest decide where the next
        # fetch or push goes, past every guard a destination meets.
        flag_effects=[
            declare(
                "runs_undeclared_program",
                scope="a program a setting names",
                reach="container",
            )
        ],
        outward_settings=[
            "credential.helper",
            "credential.*.helper",
            "core.sshcommand",
            "url.*.insteadof",
            "remote.*.url",
            "remote.*.pushurl",
        ],
        sandbox=sandbox,
        subcommands=[*leaf, *guarded],
        reason="this git subcommand is not classified as read-only or reversible",
    )


def protected_branches(rules: list[ShellCommandRule]) -> list[str]:
    """The branches a vocabulary asks about before a forced push reaches them.

    Read off the declared push row rather than declared a second time, so a
    tool forcing a push on the caller's behalf refuses exactly the branches a
    forced push spelled out in the shell would have put to the user.
    """
    return [
        branch
        for rule in rules
        if rule.name == "git"
        for subcommand in rule.subcommands
        if subcommand.name == "push"
        for branch in subcommand.protected_refs
    ]


def gh_rule(allow_authoring: bool = True) -> ShellCommandRule:
    """Compile the gh surface by what each operation does beyond this machine.

    Three bands rather than the two a read/write split offers.

    **Compensable collaboration allows.** Opening a pull request, retitling
    it, marking it ready, commenting, closing, reopening, and the same set for
    an existing issue: every one of them is restored by a normal follow-up
    operation, and a review flow performs several of them every round.
    Compensable is a claim about the remote *state*, never about observation
    — reopening a pull request does not un-send the mail that closing it
    generated — so it is the right test for whether a person needs to see the
    moment, and the wrong one for whether the effect was free. Merging a pull
    request allows beside them for the reason `git merge` does: it is how a
    landing workflow finishes, and it declares that it integrates.

    **Execution, attestation, publication, and repository security ask.** A
    workflow run runs something; an approving or request-changes review says
    something in the caller's name; a release publishes, and so does a new
    issue, a report filed where other people are notified of it; a secret, a
    ruleset, or a repository setting is the security posture of the
    repository itself, which a merge past the branch's protection (``--admin``)
    overrides. A later compensating action may exist for each and does not
    make them compensable: what happened was an event, and events are what a
    person is being asked about.

    **A deletion nested inside an allowed operation survives it.** ``gh pr
    close`` allows and ``gh pr close --delete-branch`` asks, because a safe
    outer verb cannot erase an unsafe inner one. The same shape as a push
    whose refspec deletes.

    ``allow_authoring`` decides whether opening and describing a pull request
    is the author describing their own work or a publication worth a question.
    On, they allow — the branch is already pushed by then. Off, they join the
    verbs that ask.

    Both halves of that grant are claims about *this* repository — the work is
    the author's own, and the branch is already pushed — and ``--repo`` is what
    makes them someone else's, so the authoring verbs carry it as a guard the
    way git's redirecting globals do. Spelled as a flag, the redirect is judged
    per verb, so reading another repository keeps its grant: a read is a read
    wherever it points.

    The flag is only half of it. ``GH_REPO`` and ``GH_HOST`` reach the same
    retarget through the environment, and that spelling is caught by the
    dangerous-assignment prefixes in :mod:`lup.policy.kernel.words` rather than
    here — a guard that reads the assignment before any verb is known, so
    unlike the flag it stops a redirected read as well. The asymmetry is the
    price of catching the variable gh has not learned yet.
    """
    elsewhere = ["-R", "--repo"]
    authoring = ["create", "edit", "ready"]
    # The two spellings of each attestation. gh accepts the short forms, and a
    # guard written as the long ones alone holds half of each — the same shape
    # a push guard written as flag spellings had before refspec grammar was
    # read structurally.
    attesting = ["--approve", "-a", "--request-changes", "-r"]

    def reads(names: list[str]) -> list[ShellOperationRule]:
        """Operations that report and change nothing.

        A query still reaches GitHub, and saying so is what separates these
        from the local reads elsewhere in this table: the host is declared, so
        the fetch allows, and a project that stops declaring it stops allowing
        these without anybody editing the rows.
        """
        return [
            ShellOperationRule(
                name=name,
                effects=[declare("fetches", scope="declared")],
            )
            for name in names
        ]

    def lands(
        names: list[str],
        scope: str,
        reason: str,
        operands: int = 0,
        flags: Sequence[str] = (),
    ) -> list[ShellOperationRule]:
        """Fetches that leave somebody else's code on the disk.

        Not the same act as the queries above, and the difference is not that
        these write. A query puts what it found in the terminal, where the
        agent reads it and nothing else does; these put it in the tree, where
        a build, a test run, or an import can reach it later. What separates
        them is trust rather than observation.

        So they ask, on the row that already guards trust rather than on the
        write. The write is a create -- git refuses to clone onto an occupied
        path, a download lands beside what is there -- and a create is the one
        write shape this table never questions, which is exactly why the
        question has to come from what the bytes *are* instead.

        This is also what puts the two clones on one answer. `git clone` has
        always asked and `gh repo clone` always allowed, which is the same
        code arriving in the same tree by two spellings and the divergence
        this model exists to make unrepresentable.

        ``operands`` and ``flags`` say where the bytes land, because that is
        the whole of what the trust question weighs inside a container: a
        tree landing in a directory the container owns reaches nothing a build
        outside it runs.
        """
        return [
            ShellOperationRule(
                name=name,
                effects=[
                    declare("fetches", scope="declared"),
                    declare("installs_dependency", scope=scope, reach="mount"),
                    declare("writes_path", scope="production", write="create"),
                ],
                landing_operands=operands,
                landing_flags=list(flags),
                reason=reason,
            )
            for name in names
        ]

    def compensable(names: list[str]) -> list[ShellOperationRule]:
        """Collaboration a normal follow-up operation restores."""
        return [
            ShellOperationRule(
                name=name,
                effect_class="compensable",
                ask_flags=elsewhere,
                reason="this operation against another repository requires approval",
            )
            for name in names
        ]

    def judged(
        names: list[str],
        effect_class: EffectClass,
        reason: str,
        reviewer: ReviewerRequirement = "human_only",
    ) -> list[ShellOperationRule]:
        """Operations whose effect a person is asked about every time."""
        return [
            ShellOperationRule(
                name=name,
                effect_class=effect_class,
                reviewer=reviewer,
                reason=reason,
            )
            for name in names
        ]

    def group(name: str, operations: list[ShellOperationRule]) -> ShellSubcommandRule:
        """One gh noun, and the refusal for a verb of it nobody classified.

        The operations below it are a finished list, so falling past them is
        that list saying no rather than a gap: `gh pr` reaches a remote, and
        no boundary this session runs in covers what an unclassified verb
        would do there.
        """
        return ShellSubcommandRule(
            name=name,
            effects=[declare("unclassified_operation", scope=f"gh {name}")],
            operations=operations,
            reason=f"this gh {name} operation is not classified",
        )

    return ShellCommandRule(
        name="gh",
        effects=[declare("unclassified_operation", scope="gh")],
        subcommands=[
            group(
                "pr",
                [
                    *reads(["list", "view", "diff", "status", "checks"]),
                    # Leaves a branch the object store holds like any other,
                    # and a working tree holding whatever the head contained.
                    # A pull request is a stranger's work by design -- that is
                    # what makes it a request -- so this is the same arrival
                    # the clones are, and the branch it moves is the lesser
                    # half of what it did.
                    ShellOperationRule(
                        name="checkout",
                        effects=[
                            declare("fetches", scope="declared"),
                            declare("installs_dependency", scope="pull request"),
                            declare("mutates_repository", scope="reversible"),
                        ],
                        reason="checking out a pull request puts its author's"
                        " code in this tree",
                    ),
                    *compensable(
                        [
                            "comment",
                            "reopen",
                            *(authoring if allow_authoring else []),
                        ]
                    ),
                    # Closing restores by reopening; deleting the branch does
                    # not, so the deletion nested inside the allowed verb keeps
                    # its own question.
                    ShellOperationRule(
                        name="close",
                        effect_class="compensable",
                        ask_flags=[*elsewhere, "--delete-branch", "-d"],
                        reason="deleting the branch alongside the close"
                        " removes work no reopen restores",
                    ),
                    # A review that carries neither verdict is a comment. The
                    # two that do are claims made in the caller's name, and
                    # saying something else later is not unsaying them.
                    ShellOperationRule(
                        name="review",
                        effect_class="compensable",
                        ask_flags=[*elsewhere, *attesting],
                        reason="approving or requesting changes attests in your name",
                    ),
                    # Deleting the head alongside is not the loss `close
                    # --delete-branch` is: gh deletes it only once the merge
                    # has landed what it held.
                    ShellOperationRule(
                        name="merge",
                        effects=[declare("integrates", scope="pull request")],
                        ask_flags=[*elsewhere, "--admin"],
                        flag_effects=[
                            declare("external_mutation", scope="repository_security")
                        ],
                        reason="--admin merges past the reviews and checks the base"
                        " branch requires, and --repo merges in another repository",
                    ),
                    *(
                        []
                        if allow_authoring
                        else judged(
                            authoring,
                            "publication",
                            "opening or describing a pull request publishes",
                        )
                    ),
                ],
            ),
            group(
                "issue",
                [
                    *reads(["list", "view", "status"]),
                    *compensable(
                        ["edit", "comment", "close", "reopen", "pin", "unpin"]
                    ),
                    *judged(
                        ["create"],
                        "publication",
                        "filing an issue publishes a report the repository's"
                        " watchers are notified of",
                    ),
                    *judged(
                        ["delete", "transfer"],
                        "execution",
                        "removing an issue from this repository is not"
                        " restored by a follow-up",
                    ),
                ],
            ),
            group(
                "run",
                [
                    *reads(["list", "view", "watch"]),
                    *judged(
                        ["rerun", "cancel"],
                        "execution",
                        "changing what a workflow run is doing requires approval",
                    ),
                    # Classed compensable once, which read as a publication
                    # restored by a follow-up. Nothing is published: the
                    # artifact comes here and the remote is untouched. What
                    # it is instead is a build's output arriving as files,
                    # which is somebody else's code on this disk.
                    *lands(
                        ["download"],
                        "workflow artifact",
                        "a workflow artifact is code from a build",
                        flags=["-D", "--dir"],
                    ),
                ],
            ),
            group(
                "repo",
                [
                    *reads(["view", "list"]),
                    # Reaches nobody there and brings a whole tree here, which
                    # is the half worth the question. `git clone` already
                    # asked; this is the same arrival by the other spelling.
                    *lands(
                        ["clone"],
                        "repository",
                        "cloning brings a repository's code into this tree",
                        operands=2,
                    ),
                    *judged(
                        ["create", "fork", "rename", "archive", "delete", "edit"],
                        "repository_security",
                        "changing what this repository is, or creating another,"
                        " requires approval",
                    ),
                ],
            ),
            group(
                "release",
                [
                    *reads(["list", "view"]),
                    *lands(
                        ["download"],
                        "release asset",
                        "a release asset is a published binary",
                        flags=["-D", "--dir", "-O", "--output"],
                    ),
                    *judged(
                        ["create", "upload", "edit", "delete"],
                        "publication",
                        "a release is published where people consume it",
                    ),
                ],
            ),
            group(
                "secret",
                [
                    *reads(["list"]),
                    *judged(
                        ["set", "delete"],
                        "repository_security",
                        "repository secrets decide what automation can reach",
                    ),
                ],
            ),
            group(
                "variable",
                [
                    *reads(["list", "get"]),
                    *judged(
                        ["set", "delete"],
                        "repository_security",
                        "repository variables configure automation",
                    ),
                ],
            ),
            group(
                "ruleset",
                [
                    *reads(["list", "view", "check"]),
                    *judged(
                        ["create", "edit", "delete"],
                        "repository_security",
                        "rulesets are this repository's own protection",
                    ),
                ],
            ),
            group(
                "workflow",
                [
                    *reads(["list", "view"]),
                    *judged(
                        ["run", "enable", "disable"],
                        "execution",
                        "dispatching or gating a workflow runs something",
                    ),
                ],
            ),
            group(
                "cache",
                [
                    *reads(["list"]),
                    *compensable([]),
                    *judged(
                        ["delete"],
                        "execution",
                        "a deleted cache is not restored by a follow-up",
                    ),
                ],
            ),
            group(
                "auth",
                [
                    # `-t` adds the token itself to the report.
                    ShellOperationRule(
                        name="status",
                        effects=[declare("fetches", scope="declared")],
                        ask_flags=["-t", "--show-token"],
                        reason="showing the token gh holds writes it into this"
                        " transcript",
                    ),
                    # The token gh holds, printed and nothing else: the same
                    # disclosure `printenv GH_TOKEN` is refused for.
                    ShellOperationRule(
                        name="token",
                        effects=[declare("reads_path", scope="secret")],
                        refuses="printing the token gh holds writes it into this"
                        " transcript",
                        reason="printing the token gh holds writes it into this"
                        " transcript",
                        recovery="gh reads its own token: run the gh command that"
                        " needs it.",
                    ),
                ],
            ),
            group("search", reads(["repos", "issues", "prs", "code", "commits"])),
            group(
                "label",
                [
                    *reads(["list"]),
                    *compensable(["create", "edit", "clone"]),
                    *judged(
                        ["delete"],
                        "execution",
                        "deleting a label removes it from everything carrying it",
                    ),
                ],
            ),
            group(
                "gist",
                [
                    *reads(["list", "view"]),
                    *lands(
                        ["clone"],
                        "gist",
                        "a gist is somebody's code, cloned into this tree",
                        operands=2,
                    ),
                    *judged(
                        ["create", "edit", "delete"],
                        "publication",
                        "a gist is published outside this repository",
                    ),
                ],
            ),
            group(
                "project",
                [
                    *reads(["list", "view", "item-list", "field-list"]),
                    *compensable(["item-add", "edit", "close"]),
                    *judged(
                        ["create", "delete", "item-delete"],
                        "execution",
                        "creating or removing project state is not restored by"
                        " a follow-up",
                    ),
                ],
            ),
            group("config", [*reads(["get", "list"]), *compensable(["set"])]),
            ShellSubcommandRule(
                name="status",
                effects=[declare("fetches", scope="declared")],
            ),
            # `--no-browser` prints the URL and the bare form opens it. Neither
            # reaches further than the query above: what the browser then does
            # is the browser's, and this table answers for what gh does.
            ShellSubcommandRule(
                name="browse",
                effects=[declare("fetches", scope="declared")],
            ),
            ShellSubcommandRule(
                # The default method is GET, so the unguarded form is a read.
                # Every way of making it something else — naming a method,
                # attaching a field, reading a body from a file — is guarded,
                # which leaves the mutation ask exactly where it belongs
                # instead of on every query that shares the subcommand.
                #
                # Opaque rather than classified: this is the one gh surface
                # that can reach any endpoint, so what a mutation here does is
                # exactly what the table cannot say.
                name="api",
                # Stated rather than read off the class, because the class
                # describes the flagged form. A bare call is a read of a host
                # this repository declared; what `--method` and `--field` reach
                # is the opaque mutation, and it is those that carry it.
                effects=[declare("fetches", scope="declared")],
                effect_class="opaque",
                ask_flags=[
                    "-X",
                    "--method",
                    "-f",
                    "--field",
                    "-F",
                    "--raw-field",
                    "--input",
                ],
                reason="gh api can mutate anything",
            ),
        ],
        reason="this gh command is not classified",
    )


def docker_rule() -> ShellCommandRule:
    """Compile the docker surface: queries allow, everything else asks.

    Nothing here is a parameter because the split is not a judgement: a verb
    either reports on containers, images, volumes and the daemon, or changes
    one of them. An unclassified or expansion-obscured subcommand keeps the
    ask rather than falling through to a grant.
    """

    def noun(name: str, verbs: list[str]) -> ShellSubcommandRule:
        return ShellSubcommandRule(
            name=name,
            effects=[declare("mutates_environment", scope=name)],
            operations=[
                ShellOperationRule(
                    name=verb,
                    effects=[declare("reads_environment", scope=name)],
                )
                for verb in verbs
            ],
            reason="container operations require approval",
        )

    queries = [
        ShellSubcommandRule(
            name=name,
            effects=[declare("reads_environment", scope="docker")],
        )
        for name in (
            "info",
            "version",
            "ps",
            "images",
            "inspect",
            "logs",
            "top",
            "port",
            "diff",
            "history",
            "stats",
            "events",
        )
    ]
    return ShellCommandRule(
        name="docker",
        effects=[declare("mutates_environment", scope="docker")],
        # docker's own globals that consume the word after them, as `docker
        # --help` lists them. Unlisted, the walk read that word as the
        # subcommand: `docker --context version rm -f x` was `docker version`
        # and allowed, while docker removes the container.
        value_flags=[
            "-c",
            "--context",
            "--config",
            "-H",
            "--host",
            "-l",
            "--log-level",
            "--tlscacert",
            "--tlscert",
            "--tlskey",
        ],
        subcommands=[
            *queries,
            noun(
                "container", ["ls", "inspect", "logs", "top", "port", "diff", "stats"]
            ),
            noun("image", ["ls", "inspect", "history"]),
            noun("volume", ["ls", "inspect"]),
            noun("network", ["ls", "inspect"]),
            noun("context", ["ls", "show", "inspect"]),
            noun("system", ["df", "info", "events"]),
        ],
        reason="container operations require approval",
    )


def bun_rule() -> ShellCommandRule:
    """Compile the bun surface, which is a package manager wearing a runtime.

    `bun` is in the kernel's interpreter set, and the kernel reads bun's own
    grammar before this row: `bun <script file>` runs, and the inline
    spellings (`-e`, `--eval`, `-p`, `--print`) are refused whatever a row
    says. What reaches here is a subcommand, and without a rule naming them
    every one would be refused as a bare interpreter — including `bun
    install`, which carries no program. Declaring the safe forms separates
    the two the safe way round: the default is `deny`, so a spelling this
    table never anticipated is refused by falling through rather than by
    being listed.

    The split between allow and ask is what a verb does to the lockfile
    rather than to the filesystem: restoring what it already pins, as
    `install --frozen-lockfile` does, is the ordinary case and the act
    `uv run` performs unasked, while an install free to rewrite the lock,
    adding a dependency, removing one, or fetching a package that is not
    declared at all changes what this project depends on and is worth a
    question.
    """
    return ShellCommandRule(
        name="bun",
        # The refusal is of the spelling: handing an interpreter a program is
        # the thing this project does not do, and writing the program to a
        # file is where it goes instead.
        effects=[declare("runs_undeclared_program", scope="unread code")],
        refuses="bare interpreters and inline code are not allowed",
        # The version banner is not a subcommand and would otherwise fall to
        # the default deny, which is the wrong answer for a pure read.
        allow_flags=["--version", "--revision"],
        subcommands=[
            # Turns a lockfile into code on disk. Bare, it is free to rewrite
            # the lockfile first wherever the manifest moved, which resolves
            # what this project depends on anew and asks the way `uv sync`
            # does. Frozen, it fetches nothing the lock does not pin by
            # integrity hash — the restore `uv run` performs before running,
            # and the one the gate performs before `bun test` — so the flag
            # answers it, on the terms `uv sync --frozen` is answered.
            ShellSubcommandRule(
                name="install",
                effects=[declare("materializes_lockfile", scope="bun lockfile")],
                refuses="",
                frozen_flags=["--frozen-lockfile"],
                reason="an install free to rewrite the lockfile resolves what"
                " this project depends on anew",
                recovery="`--frozen-lockfile` restores what the lockfile already"
                " pins, and runs without asking.",
            ),
            *[
                ShellSubcommandRule(
                    name=name,
                    effects=[declare("runs_declared_target", scope="bun")],
                    refuses="",
                )
                for name in ("run", "test", "build")
            ],
            ShellSubcommandRule(
                name="add",
                effects=[declare("installs_dependency", scope="bun package")],
                refuses="",
                reason="adding a dependency changes what this project needs",
            ),
            # Rewrites the lock and re-materializes what it names, which is
            # the same arrival `install` makes rather than a removal only.
            ShellSubcommandRule(
                name="remove",
                effects=[declare("materializes_lockfile", scope="bun lockfile")],
                refuses="",
                reason="removing a dependency changes what this project needs",
            ),
            ShellSubcommandRule(
                name="x",
                effects=[declare("installs_dependency", scope="bun package")],
                refuses="",
                reason="running a package that is not a declared dependency",
            ),
        ],
        reason="bare interpreters and inline code are not allowed",
    )


def typescript_rule() -> list[ShellCommandRule]:
    """Compile the TypeScript compiler: checking allows, emitting asks.

    `--noEmit` is the whole of the read-only form and it still takes
    operands, so no all-flags test recognizes it — which is what
    ``read_verbs`` exists for. A bare invocation writes output at paths a
    configuration file chooses, so nothing in the command bounds what it
    touches, which is the shape this table asks about everywhere else.

    Both package runners are declared alongside it because reaching a
    project-local compiler through one is how a pinned version gets used, and
    a globally installed `tsc` checks against whatever somebody last
    installed. Each default asks, since a runner will fetch a package that is
    not a declared dependency rather than report that it is missing — but the
    compiler they most often reach is named beneath them, so a type check
    spelled through a runner is the read it is. Without that, a verify line
    ending in `npx tsc --noEmit` asked about its last segment and made the
    whole line ask, which is a question about running the type checker.

    They differ on one axis and it is not a preference. `bunx` is placed
    outside the boundary because reaching the registry is what it is for;
    `npx` is left unplaced, because the invocation this rule exists to
    recognize resolves a dependency the project already has and needs no
    network at all. A project whose runner does have to fetch says so by
    widening this declaration, which is a change a reviewer sees.
    """
    checking = ShellSubcommandRule(
        name="tsc",
        read_verbs=["--noEmit", "--version"],
        reason="emitting compiler output writes files the command does not bound",
    )
    return [
        ShellCommandRule(
            name="tsc",
            # A configuration file chooses where the output lands, so no word
            # of the invocation names what is about to be replaced.
            effects=[
                declare(
                    "writes_path",
                    scope="unbounded",
                    reason="emitting compiler output writes files the command does not bound",
                )
            ],
            allow_flags=["--version"],
            read_verbs=["--noEmit"],
            reason="emitting compiler output writes files the command does not bound",
        ),
        # Fetching a package that is not a declared dependency is the trust
        # question, reached by a runner rather than by an install verb.
        ShellCommandRule(
            name="bunx",
            effects=[declare("installs_dependency", scope="package runner")],
            subcommands=[checking],
            reason="the package runner fetches what is not already a dependency",
        ),
        ShellCommandRule(
            name="npx",
            effects=[declare("installs_dependency", scope="package runner")],
            subcommands=[checking],
            reason="the package runner fetches what is not already a dependency",
        ),
    ]


def codex_rule() -> ShellCommandRule:
    """Compile the codex surface: reads allow, session-reaching verbs are routed.

    Declared for one verb's sake and enumerated for everything around it.
    `codex queue --thread <id> --message <text>` reaches another session from
    any process on this machine, leaving its text only inside whichever process
    received it — the act `lup.policy.kernel.peers` already refuses when a
    runtime spells it as a tool call, arriving here as a command line instead.
    A table that refused only the tool would have redirected one spelling of
    one act and left the other open.

    The default is `runs_undeclared_program` rather than the
    `unclassified_operation` that `git` and `gh` take, and the table's own
    distinction is why: what falls off `gh` reaches a remote no boundary
    covers, so containment has nothing to offer it, while a word codex does
    not recognize is taken as the *prompt* of an interactive session on this
    machine — which a sandbox confines. It also means a verb a later release
    grows costs a question rather than a refusal nobody anticipated, which
    matters more here than elsewhere: this CLI's own readers swallow a failed
    invocation into "nothing to report".

    That swallowing is the reason the bare form is guarded at all. `codex
    notaverb` does not fail — it starts an interactive session with the typo
    as its prompt, so the shape that looks like a mistyped read is the shape
    that opens an agent. `allow_flags` keeps the two spellings that genuinely
    only report — `--version` and `--help` — as reads.
    """

    def reading(names: list[str], scope: str) -> list[ShellSubcommandRule]:
        """Verbs that render what is already there and change nothing."""
        return [
            ShellSubcommandRule(
                name=name, effects=[declare("changes_nothing", scope=scope)]
            )
            for name in names
        ]

    def opening(names: list[str]) -> list[ShellSubcommandRule]:
        """Verbs that start an agent, a server, or a program of their own."""
        return [
            ShellSubcommandRule(
                name=name,
                effects=[
                    declare(
                        "runs_undeclared_program",
                        scope=f"codex {name}",
                        reach="container",
                    )
                ],
                reason=f"`codex {name}` opens an agent or a server of its own",
            )
            for name in names
        ]

    return ShellCommandRule(
        name="codex",
        effects=[declare("runs_undeclared_program", scope="codex", reach="container")],
        # Every global that consumes the following word, so a value is never
        # read as the subcommand. `--image` takes several, which this reading
        # cannot express; it lands on the ask the bare form already carries.
        value_flags=[
            "-c",
            "--config",
            "--enable",
            "--disable",
            "--remote",
            "--remote-auth-token-env",
            "-i",
            "--image",
            "-m",
            "--model",
            "--local-provider",
            "-p",
            "--profile",
            "-s",
            "--sandbox",
            "-C",
            "--cd",
            "--add-dir",
            "-a",
            "--ask-for-approval",
        ],
        allow_flags=["-V", "--version", "-h", "--help"],
        reason=(
            "a word codex does not recognize is taken as the prompt of an"
            " interactive session rather than refused"
        ),
        recovery=("Name a verb `codex --help` lists, or say what the session is for."),
        subcommands=[
            *reading(["agents", "completion", "doctor", "features", "help"], "codex"),
            *opening(
                ["exec", "e", "review", "resume", "fork", "sandbox", "exec-server"]
            ),
            ShellSubcommandRule(
                name="queue",
                effects=[declare("changes_nothing", scope="codex queue")],
                refuses=(
                    "Reach the peer with coordination_send, which records what"
                    " it carries."
                ),
                reason=(
                    "queueing a message reaches another session leaving the"
                    " text only inside whichever process received it"
                ),
                recovery=(
                    "Use coordination_send, which records what it carries; the"
                    " roster's own watcher nudges an idle Codex session through"
                    " this verb once the record exists."
                ),
            ),
            ShellSubcommandRule(
                name="debug",
                effects=[
                    declare(
                        "runs_undeclared_program",
                        scope="codex debug",
                        reach="container",
                    )
                ],
                reason="this codex debug tool is not one that only renders",
                operations=[
                    ShellOperationRule(
                        name=name,
                        effects=[declare("changes_nothing", scope="codex debug")],
                    )
                    for name in ("models", "prompt-input")
                ],
            ),
            ShellSubcommandRule(
                name="mcp",
                effects=[declare("mutates_environment", scope="codex mcp")],
                operations=[
                    ShellOperationRule(
                        name=name,
                        effects=[declare("changes_nothing", scope="codex mcp")],
                    )
                    for name in ("list", "get")
                ],
                reason="an MCP server changes what every later session can reach",
            ),
            ShellSubcommandRule(
                name="plugin",
                effects=[declare("installs_dependency", scope="codex plugin")],
                reason="a plugin arrives from a marketplace and runs in a session",
                operations=[
                    ShellOperationRule(
                        name="list",
                        effects=[declare("changes_nothing", scope="codex plugin")],
                    )
                ],
            ),
            ShellSubcommandRule(
                name="app-server",
                effects=[
                    declare(
                        "runs_undeclared_program",
                        scope="codex app-server",
                        reach="container",
                    )
                ],
                reason="the app server is the process every Codex session runs in",
                operations=[
                    ShellOperationRule(
                        name=name,
                        effects=[declare("writes_path", scope="unbounded")],
                        write_flags=["--out"],
                        reason="the generated schema lands wherever --out names",
                    )
                    for name in ("generate-ts", "generate-json-schema")
                ],
            ),
            ShellSubcommandRule(
                name="remote-control",
                effects=[declare("reaches_host", scope="codex remote-control")],
                reason=(
                    "remote control lets a process off this machine drive a"
                    " session here"
                ),
            ),
            *[
                ShellSubcommandRule(
                    name=name,
                    effects=[
                        declare(
                            "mutates_environment", scope="codex", reach="credential"
                        )
                    ],
                    reason="stored Codex credentials are what every session runs as",
                )
                for name in ("login", "logout")
            ],
            ShellSubcommandRule(
                name="update",
                effects=[declare("installs_dependency", scope="codex")],
                reason="updating replaces the CLI every session here runs",
            ),
            ShellSubcommandRule(
                name="apply",
                effects=[declare("destroys_uncaptured", scope="boundary_wide")],
                reason="applying a diff writes over whatever the tree holds now",
            ),
            ShellSubcommandRule(
                name="a",
                effects=[declare("destroys_uncaptured", scope="boundary_wide")],
                reason="applying a diff writes over whatever the tree holds now",
            ),
            *[
                ShellSubcommandRule(
                    name=name,
                    effects=[declare("destroys_uncaptured", scope="unrecoverable")],
                    reason="a saved session is not in the object store",
                )
                for name in ("delete", "migrate-rollouts")
            ],
            *[
                ShellSubcommandRule(
                    name=name,
                    effects=[declare("mutates_environment", scope="codex")],
                    reason="which saved sessions are listed is shared state",
                )
                for name in ("archive", "unarchive")
            ],
            ShellSubcommandRule(
                name="cloud",
                effects=[declare("fetches", scope="undeclared")],
                reason="cloud tasks arrive from off this machine and land here",
            ),
        ],
    )


def default_vocabulary() -> list[ShellCommandRule]:
    """Every group at its offered defaults — the batteries-included table.

    A project with no opinion yet composes this and gets a working agent; one
    that has an opinion replaces the groups it differs on rather than this
    call. The list is the caller's own; the rules in it are
    :func:`offered_rules`, built once and shared.
    """
    return list(offered_rules())


@cache
def offered_rules() -> tuple[ShellCommandRule, ...]:
    """The offered groups in their declared order, built once per process.

    Several hundred models, tens of milliseconds to build, asked for by every
    policy, survey and rendered dispatcher -- by a test suite, thousands of
    times. Frozen, so one set serves every caller; a tuple, so no caller can
    reorder or extend what the next one reads.
    """
    return (
        *read_only_rules(),
        *judged_ask_rules(),
        *redirected_rules(),
        *reaching_builtin_rules(),
        *downloader_rules(),
        *uv_rules(),
        *guarded_tool_rules(),
        git_rule(),
        gh_rule(),
        docker_rule(),
        codex_rule(),
    )
