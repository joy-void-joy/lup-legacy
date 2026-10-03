# lup: ignore[empty-collection, import-re, re-call]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Shell segment, structure, and whole-command classification."""

import posixpath
import re
from typing import TypedDict

from .decision import (
    ESCALATE_HINT,
    KernelDecision,
    RELAY_HINT,
    RESHAPE_HINT,
    SUBSTITUTION_SENTINEL,
    joined_decision,
    judged_command,
    recovery_dischargeable,
    unjudged,
)
from .settlement import SettlementFacts, settle
from .rows import (
    AcceptanceGuardRow,
    AntiPatternRow,
    EditRuleRow,
    ImportBoundaryRow,
    PathRoleRow,
    PathRuleRow,
    DisplacedTargetRow,
    RefusedPathRow,
    TargetLandingRow,
    RewrittenDocumentRow,
    RunnerTargetRow,
    UnproducedDocumentRow,
    ShellRuleRow,
    UrlScopeRow,
    WithheldWalkRow,
)
from .words import (
    BINDING_BUILTINS,
    INTERPRETERS,
    asks_before_removing_a_directory,
    binding_reach,
    bound_names,
    command_words,
    dangerous_assignment,
    dangerous_assignment_reason,
    dangerous_env_name,
    effective_command,
    git_init_in_scratch,
    global_span,
    is_help_probe,
    is_trusted_script,
    opaque_argument,
    archive_lands_on_nothing,
    confined_to_recoverable_roots,
    refuses_generated_plugin_write,
    protected_deletion,
    protected_placement,
    env_payload,
    read_wrapper,
    uv_command_words,
    uv_run_words,
    write_checkpoint,
    write_scope,
    xargs_payload,
)
from .bindings import (
    ShellBinding,
    bind_name,
    bind_script,
    carried_words,
    command_lists,
    pure_assignment_names,
    references,
    unrollable,
)
from .escalation import read_escalation
from .programs import (
    PROGRAM_RULE,
    SCRIPT_INTERPRETERS,
    program_verdict,
    read_program,
)
from .roles import repository_relative
from .walks import names_read
from .withheld import (
    printed_secret,
    secret_name,
    secret_refusal,
    withheld_operand,
    withheld_redirect,
    withheld_walk,
)
from .semantics import UnjudgedAmbient
from .lex import (
    command_directory,
    command_segments,
    git_worked_tree,
    guarded_write_targets,
    joined_directory,
    list_commands,
    parse_shell,
    parse_shell_words,
    placed_path,
    placed_words,
    named_write_verdict,
    read_segments,
    shell_flag_write_targets,
    shell_path_verb_targets,
    shell_worked_trees,
    shell_write_targets,
    simple_commands,
    substitutions,
    tee_operands,
    uv_run_placement,
    verb_path_words,
)
from .syntax import Command, Script, Word, expands, readable_prefix, word_text
from .documents import spelled_command
from .effects import STRENGTH, declared_verdict, member_for
from .commands import (
    SedContext,
    WriteFacts,
    declares_command,
    unresolved_evidence,
    decide_awk_words,
    decide_command_rows,
    decide_copy_words,
    decide_download_words,
    decide_gh_words,
    decide_sed_words,
    decide_tool_run,
    decide_uv,
    git_checkout_pathspec,
    git_restore_source,
    git_restore_unchanged,
    git_symbolic_ref_read,
    Reading,
    objection_or_floor,
    strictest_reading,
    unread_programs,
    unread_readings,
    landing_words,
    matched_command_row,
    row_verdict,
)

ESCALATE_RE = re.compile(
    r"^\s*#[ \t]*lup[ \t]*:[ \t]*escalate\b[ \t]*:?[ \t]*(?P<why>[^\n]*)(?:\n|$)",
    re.IGNORECASE,
)


class ShellContext(TypedDict):
    """The declarations and host facts every segment is judged against.

    Each value is threaded unchanged through the whole recursion, so carrying
    them as one bundle is what makes a construct that forgets one impossible
    to write: a loop body, a conditional branch, and a ``find -exec`` payload
    are judged against exactly what the top-level command was.
    """

    rows: list[ShellRuleRow]
    allowed_scopes: list[UrlScopeRow]
    denied_scopes: list[UrlScopeRow]
    trusted_script_roots: list[str]
    path_roles: list[PathRoleRow]
    path_rules: list[PathRuleRow]
    existing_targets: list[str] | None
    tracked_targets: list[str]
    recoverable_targets: list[str]
    directory_targets: list[str]
    empty_directories: list[str]
    recoverable_target_limit: int
    runner_targets: list[RunnerTargetRow]
    target_tables: list[ShellRuleRow]
    contained: bool
    checkout_root: str
    """Where this repository sits, for reading an absolute path back against.

    Travels with the paths for the reason ``contained`` does: the write rows
    read it beside them. A declared role is anchored at the repository top and
    reaches no absolute spelling, so without this one file answers twice
    depending on how a caller named it. The machine's own path, so it arrives
    from the host per call rather than from any declaration."""

    displaced_targets: list[DisplacedTargetRow]
    """Write targets the host found landing under a role other than they claim.

    The settlement asks about a write through such a link; a segment reads
    them for one thing only, the scratch exception to the generated-plugin
    refusal, which a spelling under this checkout's scratch earns only where
    the host found nothing moving the bytes elsewhere."""

    host_ports: list[int]
    """Loopback ports a process outside this session's container listens on.

    Carried for `curl` and `wget` for the reason ``unscoped_fetch`` is: they
    hand their question to the fetch scopes, and a scope admitting this
    machine's loopback admits the operator's services on it wherever a
    container shares the host's network."""

    unscoped_fetch: UnjudgedAmbient
    """What an origin no fetch scope names answers, carried for the downloaders.

    A segment classifier does not settle anything, so almost nothing here
    needs this. `curl` and `wget` do, because their screen hands the question
    to the fetch scopes -- and an origin no scope names is the question
    `WebFetch` answers from the same declaration. Without it, one spelling of
    reaching an undeclared origin answered from the declaration and the other
    from a constant."""

    refused_paths: list[RefusedPathRow]
    """Paths no word of any command may name, each with what to do instead."""

    withheld_walks: list[WithheldWalkRow]
    """Roots a recursive reader walks that the host found holding a withheld path.

    A fact about the disk beneath a word rather than about the word, so it
    arrives from the host per call; absent, nothing was walked and nothing is
    refused for what lies beneath."""

    names_read: bool
    """Whether this line hands names read from its input to a program.

    Read once over the whole line, since a listing's names reach their
    reader through a pipe, a file or a loop: where they do, every listing
    in the line is read as the walk it feeds."""

    secret_variables: list[str]
    """Name patterns of the variables whose values no command may print."""

    antipattern_rows: dict[str, list[AntiPatternRow]]
    edit_rules: list[EditRuleRow]
    import_boundaries: list[ImportBoundaryRow]
    acceptance_guard: AcceptanceGuardRow | None
    maximum_added_lines: int
    autonomous: bool
    allowances: list[str]
    """The edit gates' own declarations, carried for the verbs that rewrite.

    A shell command that overwrites a file in place performs an edit by
    another spelling, and is judged by these rather than by a second set that
    would drift from them. They travel in the bundle for the reason everything
    else here does: a construct that forgot one would be a construct where a
    rewrite nested in a loop met a weaker lattice than the same rewrite at the
    top level."""

    unproduced_documents: list[UnproducedDocumentRow]
    """Why the host produced no document for a target that names one.

    Absent where nothing looked, present where something looked and was
    stopped -- which is the difference between a refusal that can say what
    to do about it and one that can only say a reading failed."""

    rewritten_documents: list[RewrittenDocumentRow]
    """What each in-place rewrite would leave behind, as the host produced it.

    Empty is not "nothing would change" but "nothing was read", and the
    classifier acts on the difference: a target with no row is asked about.
    That is what makes forgetting to resolve these safe rather than silently
    permissive."""


def write_facts(context: ShellContext) -> WriteFacts:
    """The readings a write flag's path is judged against, off the bundle.

    A projection rather than a second bundle, so the classifier below takes
    what it needs without being handed the URL scopes and runner targets it
    has no business reading -- and so the names it knows them by stay its own.

    ``contained`` travels with the paths because the write row reads it beside
    them: whether a write outside the checkout is confined by a boundary is a
    fact about this session rather than about the target. Left out, the
    classifier had nothing to answer with and reached for the row's *declared*
    placement instead -- a requirement about where a command must run, read as
    a measurement of where this one is. So a write flag landing outside asked
    in a contained session while the same path reached by a redirection
    allowed, which is exactly the divergence this table exists to remove.
    """
    return WriteFacts(
        existing=context["existing_targets"],
        tracked=context["tracked_targets"],
        path_roles=context["path_roles"],
        path_rules=context["path_rules"],
        contained=context["contained"],
        checkout_root=context["checkout_root"],
    )


def shell_context(
    rows: list[ShellRuleRow],
    allowed_scopes: list[UrlScopeRow] | None = None,
    denied_scopes: list[UrlScopeRow] | None = None,
    trusted_script_roots: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    existing_targets: list[str] | None = None,
    tracked_targets: list[str] | None = None,
    recoverable_targets: list[str] | None = None,
    directory_targets: list[str] | None = None,
    empty_directories: list[str] | None = None,
    recoverable_target_limit: int = 5,
    runner_targets: list[RunnerTargetRow] | None = None,
    target_tables: list[ShellRuleRow] | None = None,
    contained: bool = False,
    checkout_root: str = "",
    unscoped_fetch: UnjudgedAmbient = "ask",
    refused_paths: list[RefusedPathRow] | None = None,
    secret_variables: list[str] | None = None,
    antipattern_rows: dict[str, list[AntiPatternRow]] | None = None,
    edit_rules: list[EditRuleRow] | None = None,
    import_boundaries: list[ImportBoundaryRow] | None = None,
    acceptance_guard: AcceptanceGuardRow | None = None,
    maximum_added_lines: int = 3,
    autonomous: bool = False,
    allowances: list[str] | None = None,
    rewritten_documents: list[RewrittenDocumentRow] | None = None,
    unproduced_documents: list[UnproducedDocumentRow] | None = None,
    displaced_targets: list[DisplacedTargetRow] | None = None,
    host_ports: list[int] | None = None,
    withheld_walks: list[WithheldWalkRow] | None = None,
    names_read: bool = False,
) -> ShellContext:
    """Bundle one classification's declarations, normalizing absent lists.

    ``existing_targets`` keeps its ``None``, because that is a fact about the
    caller rather than an empty list of paths: nothing was established, so
    every write target is treated as already there.

    ``tracked_targets`` takes an empty list instead, and the asymmetry is the
    difference between the two facts. Absence of an existing-target reading
    means "assume something is there", which is the cautious reading; absence
    of a tracked reading has to mean "assume nothing is under review", because
    the alternative would refuse every write on a host that could not answer.
    """
    return ShellContext(
        rows=rows,
        allowed_scopes=allowed_scopes or [],
        denied_scopes=denied_scopes or [],
        trusted_script_roots=trusted_script_roots or [],
        path_roles=path_roles or [],
        path_rules=path_rules or [],
        existing_targets=existing_targets,
        tracked_targets=tracked_targets or [],
        recoverable_targets=recoverable_targets or [],
        directory_targets=directory_targets or [],
        empty_directories=empty_directories or [],
        recoverable_target_limit=recoverable_target_limit,
        runner_targets=runner_targets or [],
        target_tables=target_tables or [],
        contained=contained,
        checkout_root=checkout_root,
        displaced_targets=displaced_targets or [],
        host_ports=host_ports or [],
        unscoped_fetch=unscoped_fetch,
        refused_paths=refused_paths or [],
        withheld_walks=withheld_walks or [],
        names_read=names_read,
        secret_variables=secret_variables or [],
        antipattern_rows=antipattern_rows or {},
        edit_rules=edit_rules or [],
        import_boundaries=import_boundaries or [],
        acceptance_guard=acceptance_guard,
        maximum_added_lines=maximum_added_lines,
        autonomous=autonomous,
        allowances=allowances or [],
        rewritten_documents=rewritten_documents or [],
        unproduced_documents=unproduced_documents or [],
    )


def sed_facts(context: ShellContext) -> SedContext:
    """The declarations an in-place rewrite is judged against, off the bundle.

    The projection :func:`write_facts` is, for the other half of what a shell
    command can do to a file. Nothing is reshaped on the way through: the
    rewrite meets the edit gates' own rows, so a file this policy would refuse
    an ``Edit`` of is a file it refuses a rewrite of, without a second table
    to keep in step.
    """
    return SedContext(
        path_roles=context["path_roles"],
        path_rules=context["path_rules"],
        antipattern_rows=context["antipattern_rows"],
        edit_rules=context["edit_rules"],
        import_boundaries=context["import_boundaries"],
        acceptance_guard=context["acceptance_guard"],
        maximum_added_lines=context["maximum_added_lines"],
        autonomous=context["autonomous"],
        allowances=context["allowances"],
        rewritten_documents=context["rewritten_documents"],
        unproduced_documents=context["unproduced_documents"],
    )


def find_starting_points(words: list[str]) -> list[str]:
    """The paths a `find` walks from, `.` where it names none.

    They follow find's own options (`-H`, `-L`, `-P`, `-D <debug>`,
    `-O<level>`) and end at the first word opening the expression.
    """
    roots: list[str] = []
    valued = False
    for word in words[1:]:
        if valued:
            valued = False
            continue
        if not roots and (word in ("-H", "-L", "-P") or word.startswith("-O")):
            continue
        if not roots and word == "-D":
            valued = True
            continue
        if word.startswith(("-", "(", ")", "!", ",")):
            break
        roots.append(word)
    return roots or ["."]


def decide_find_words(
    words: list[str], context: ShellContext, directory: str | None = ""
) -> KernelDecision:
    """Classify find, recursing into -exec payloads once per path a {} could be.

    What ``{}`` becomes is whatever the walk finds, so it is spelled as a path
    only the run resolves, beneath each starting point in turn -- where
    find's own output begins, so it reads as a path and never as a flag. A
    literal stand-in would be judged as the one file it names, `rm ./x` a
    deletion a capture restores, where `find packages -exec rm {} +` removes
    every file under `packages`, protected ones among them.
    ``-execdir`` runs its payload beside each file found, in a directory the
    run chooses, so every path it names is read from one nothing here can
    name. The interactive ``-ok`` forms would hang a non-interactive shell.
    """
    roots = find_starting_points(words)
    remaining = [words[0]]
    position = 1
    while position < len(words):
        word = words[position]
        if word in ("-ok", "-okdir"):
            return KernelDecision(
                "deny",
                "find -ok waits for an answer on a terminal nobody is at",
                recovery="Use -exec instead.",
            )
        if word in ("-exec", "-execdir"):
            terminator = next(
                (
                    index
                    for index in range(position + 1, len(words))
                    if words[index] in (";", "+")
                ),
                None,
            )
            if terminator is None:
                return unjudged("find -exec payload does not terminate")
            pieces = words[position + 1 : terminator]
            if not pieces:
                return unjudged("find -exec payload is empty")
            beside = word == "-execdir"
            found = ["./$FOUND"] if beside else [f"{root}/$FOUND" for root in roots]
            verdict = max(
                (
                    decide_shell_segment(
                        # lup: ignore[string-replace] — find substitutes `{}` as text, wherever it stands in a word
                        [piece.replace("{}", path) for piece in pieces],
                        context,
                        None if beside else directory,
                    )
                    for path in found
                ),
                key=lambda reading: STRENGTH.index(reading.effect),
            )
            if verdict.effect != "allow":
                return verdict
            position = terminator + 1
            continue
        remaining.append(word)
        position += 1
    return decide_command_rows(remaining, context["rows"], write_facts(context))


def decide_xargs_words(
    words: list[str], context: ShellContext, directory: str | None = ""
) -> KernelDecision:
    """Judge one `xargs` by its payload, and by what the payload is handed.

    xargs appends the words it reads to the command it runs, so the payload
    judged as written is the payload with none of its operands: `echo
    README.md | xargs rm` read as a bare `rm`, which names nothing a
    human-owned rule could match, and a capture settled it. What those
    operands are is on stdin, which nothing here reads.

    So a payload keeps its own verdict only where the operands cannot matter
    to it: a refusal or a deferral stands, and an allow stands where the row
    that decided it only observes -- `xargs grep`, `xargs wc`, `xargs cat`.
    Everything else asks, and at a checkpoint no capture settles, because
    the files it would change are the ones nobody has named.
    """
    payload = xargs_payload(words)
    if payload is None:
        return KernelDecision(
            "deny",
            "an xargs option this policy does not read could take the next word"
            " as its value, so the command xargs runs is unread",
            recovery="Spell xargs's options as `xargs --help` lists them, or"
            " attach an option's value (`-n1`, `--max-procs=4`).",
        )
    if not payload:
        return unjudged("xargs payload is not classified")
    verdict = decide_shell_segment(payload, context, directory)
    if verdict.effect in ("deny", "defer"):
        return verdict
    deciding = next(
        (
            row
            for row in [*context["rows"], *context["target_tables"]]
            if verdict.rule and row["rule"] == verdict.rule
        ),
        None,
    )
    if (
        verdict.effect == "allow"
        and deciding is not None
        and all(member_for(effect["kind"]).observes for effect in deciding["effects"])
    ):
        return verdict
    return KernelDecision(
        "ask",
        f"xargs hands `{' '.join(payload)}` operands read from its input, so"
        " what it changes is named nowhere in the command",
        checkpoint="unrecoverable",
        purpose="unrecovered_local_mutation",
        recovery="Name the files in the command, or loop over a literal list"
        " of them, so each one can be judged.",
    )


def decide_env_words(
    words: list[str], context: ShellContext, directory: str | None = ""
) -> KernelDecision:
    """Judge one `env` invocation by whatever it was going to run.

    `env` is a command prefix wearing the shape of a report. Read as a report
    it passes its payload through unjudged: `env rm -rf <path>` allowed where
    the same `rm` asks, and `env -i <interpreter> <script>` read as a
    command named `-i` and allowed inside the boundary, where the interpreter
    alone is refused outright.

    The three answers the payload reading gives are kept apart here, because
    two of them otherwise read alike. A command is judged as though `env` were
    not there, which is what it amounts to. Nothing to run is a dump of the
    whole environment, which is refused rather than asked: every variable the
    launcher sets is in it, credentials among them, and the output lands in a
    transcript that outlives the turn — a question would be answered yes on
    the way to something else. A payload this cannot read is unjudged, which
    is the honest answer for `-S` and for a flag no version here knows.

    `-C` moves the payload into another directory, so it is judged there, as
    `cd <dir> && <command>` is: stepped over, `env -C /etc rm hosts` would
    remove `hosts` from the directory the session stands in. The payload is handed
    on with its assignments, so a dangerous one is still asked about.
    """
    payload = env_payload(words)
    if payload is None:
        return KernelDecision(
            "deny",
            "`env -S` re-splits the rest of the line by its own quoting rules,"
            " so what it runs cannot be read here",
            recovery="Write the command without `-S`, so the words that run"
            " are the words in the command line.",
        )
    if not payload:
        return environment_dump()
    reading = read_wrapper(words, 1, "env")
    here = directory
    for option in reading["options"]:
        if option["name"] not in ("-C", "--chdir"):
            continue
        moved = option["value"] or ""
        if not moved or opaque_argument(moved) or expands(moved):
            return unjudged(
                "`env -C` runs its command in a directory only the run resolves"
            ).advising("Spell the directory literally, or `cd` there first.")
        here = joined_directory(here, moved)
    return decide_shell_segment(words[reading["payload"] :], context, here)


def decide_time_words(
    words: list[str], context: ShellContext, directory: str | None = ""
) -> KernelDecision:
    """Judge a `time` that writes its report into a file, and what it times.

    Stepped over as a wrapper, `time -o <file>` would lose the write:
    `time -o README.md ls` read as `ls`, allowed, and the human-owned file
    truncated to a timing report. The file is a write no redirection names,
    so nothing the host resolves stands behind it and it asks; the command it
    times is judged as it would be alone, and the stronger of the two stands.
    """
    reading = read_wrapper(words, 1, "time")
    written = [
        option["value"] or ""
        for option in reading["options"]
        if option["name"] in ("-o", "--output")
    ]
    asked = KernelDecision(
        "ask",
        f"time writes its report into {', '.join(written)}, a file no"
        " redirection names, so nothing judged the write",
        purpose="unrecovered_local_mutation",
        recovery="Redirect the report instead -- `{ time <command>; } 2> <file>`"
        " -- so the file is judged as any redirection's is.",
    )
    payload = words[reading["payload"] :]
    if not payload:
        return asked
    timed = decide_shell_segment(payload, context, directory)
    return timed if STRENGTH.index(timed.effect) >= STRENGTH.index("ask") else asked


def environment_dump() -> KernelDecision:
    """The refusal for printing every variable at once, by whichever spelling."""
    return KernelDecision(
        "deny",
        "the whole environment is a credential store, and printing it writes"
        " every secret in it into this transcript",
        recovery="Name the variables you want: `printenv <NAME>`.",
    )


def decide_printenv_words(words: list[str], secrets: list[str]) -> KernelDecision:
    """Judge one `printenv` by whether it says what it wants.

    Named, it is an ordinary read and one of the most useful there is -- unless
    a name is one ``secrets`` matches, which is the dump narrowed to the one
    variable that mattered. Bare, it is the same dump `env` refuses, reached by
    a second spelling — and a refusal that held only one of them would have
    taught the habit of reaching for the other.

    Options are not read, only whether anything survives them, because
    `printenv` has exactly two (`-0` and the informational pair) and none
    changes what it selects.
    """
    named = [word for word in words[1:] if not word.startswith("-")]
    secret = next((name for name in named if secret_name(name, secrets)), None)
    if secret is not None:
        return secret_refusal(secret)
    if named:
        return KernelDecision("allow", "reads the variables it names")
    return environment_dump()


def decide_interpreter_words(
    words: list[str],
    context: ShellContext,
    runs_scripts: tuple[str, ...] = SCRIPT_INTERPRETERS,
) -> KernelDecision | None:
    """Judge one interpreter invocation by what it hands the interpreter to run.

    The criterion `uv run` already applies: refused where the invocation
    leaves nothing reviewable behind, which a script file does not do. An
    interpreter in ``runs_scripts`` runs a named file; inline code, a program
    fetched from elsewhere, and -- undeclared -- an interpreter handed nothing
    are refused. The first three hold even where the vocabulary names the
    interpreter, because a row declaring `bun install` speaks for a
    subcommand and not for a program nobody can read.

    ``None`` hands a declared interpreter's other forms to its row. Anything
    else refuses as a bare interpreter, which Python meets over a file too:
    it runs through `uv run python <script>`, in this project's environment.
    """
    executable = posixpath.basename(words[0])
    declared = declares_command(executable, context["rows"])
    reading = read_program(words)
    # Its version or usage runs no program, whichever interpreter prints it.
    if reading["kind"] == "informational":
        return program_verdict(executable, reading)
    if executable in runs_scripts:
        verdict = program_verdict(executable, reading)
        if verdict is not None and (
            reading["kind"] in ("script", "inline", "remote") or not declared
        ):
            return verdict
    if declared:
        return None
    if len(words) > 1 and is_trusted_script(words[1], context["trusted_script_roots"]):
        return KernelDecision("allow", "native-managed skill script")
    return KernelDecision(
        "deny",
        f"{executable}: a bare interpreter or inline code leaves nothing"
        " behind to review",
        recovery="Write the code to a named script file and run it through"
        " `uv run python <script>`; a bare interpreter is refused even"
        " over a file.",
        # A subcommand is the tool's own, and reads its own `--help`.
        rule="" if reading["kind"] == "subcommand" else PROGRAM_RULE,
    )


def standing_interpreter_refusal(
    words: list[str], context: ShellContext
) -> KernelDecision | None:
    """An interpreter's refusal that no word nobody can read could lift.

    Inline code and a program fetched from elsewhere leave nothing behind to
    review whatever the words after them turn out to be, and an interpreter
    this project does not run directly is refused over any file. Where the
    words deciding that are spelled out -- the interpreter, and every word up
    to the one naming the code -- the refusal stands beside an argument
    nobody can read, rather than being handed to a boundary with it:
    `perl -pi -e … $files` is the inline code `perl -pi -e …` is.

    The program itself unread is judged as the strictest one it could be:
    `bash $x` runs whatever `$x` holds, and that could as well be `-c` and
    code split out of the word, or `-` and its input, as a script file. So
    it is refused on every posture, declared interpreter or not, and through
    `uv run` as directly. An interpreter a project declared otherwise keeps
    its row.
    """
    if not words or opaque_argument(words[0]):
        return None
    executable = posixpath.basename(words[0])
    if executable == "uv":
        normalized = uv_command_words(words)
        handed = (
            uv_run_words(normalized)
            if normalized is not None and normalized[1:2] == ["run"]
            else []
        )
        return standing_interpreter_refusal(handed, context) if handed else None
    if executable not in INTERPRETERS:
        return None
    program = read_program(words)
    if program["kind"] == "unread" and opaque_argument(program["subject"]):
        return program_verdict(executable, program)
    verdict = decide_interpreter_words(words, context)
    if verdict is None or verdict.effect != "deny":
        return None
    unread = next(
        (index for index, word in enumerate(words) if opaque_argument(word)),
        len(words),
    )
    if executable not in SCRIPT_INTERPRETERS:
        return verdict if unread > 1 else None
    reading = read_program(words)
    deciding = next(
        (index for index, word in enumerate(words) if word == reading["subject"]),
        unread,
    )
    if reading["kind"] in ("inline", "remote") and deciding < unread:
        return verdict
    return None


def decide_segment_words(
    words: list[str],
    context: ShellContext,
    directory: str | None = "",
    operands_judged: bool = False,
) -> KernelDecision:
    """Classify one command's words against the vocabulary and handlers.

    Separate from the segment above it because a verdict and a placement are
    two axes, and only one of them is a help probe's to answer. Reached with
    the word list a segment settles to, so a construct that recurses into a
    payload — ``xargs``, ``find -exec`` — goes back through the segment and
    meets the same reading of it.

    ``operands_judged`` says the line's write walk
    (:func:`~lup.policy.kernel.lex.named_write_verdict`) has already judged
    the files these words write, which it does for the words a command
    itself carries. A payload is handed operands no word names -- `find
    -exec tee {}` writes every file it finds -- so it is never set there.
    """
    executable = posixpath.basename(words[0])
    # No program is spelled with a leading dash, so reaching one means a
    # wrapper's option grammar ran out above this and the word that followed
    # was taken for the command. Refused rather than left unclassified: an
    # unclassified command is allowed inside the boundary, so a wrapper
    # spelling would carry through an interpreter this refuses outright.
    if executable.startswith("-"):
        return KernelDecision(
            "deny",
            f"{executable!r} is an option rather than a command, so what this"
            " would run was never read",
            recovery="Write the command without the wrapper, or name the"
            " wrapper's options so the command after them can be read.",
        )
    if executable in INTERPRETERS:
        interpreted = decide_interpreter_words(words, context)
        if interpreted is not None:
            return interpreted
    if executable == "git" and any("ext::" in word for word in words):
        transport = next(word for word in words if "ext::" in word)
        return KernelDecision(
            "ask",
            f"the git ext transport in {transport!r} can run commands",
        )
    if executable == "git":
        # lup: solved: the segment reading consumes `--work-tree` before these
        # grants read the words, so `git --work-tree=/tmp restore --source=HEAD
        # README.md` and `git --work-tree /tmp checkout HEAD -- README.md` are
        # granted as a restore of this checkout while they overwrite a tree
        # outside it that no reflog or capture holds. Decide whether a consumed
        # `--work-tree` withdraws the grants or places their operands in that
        # tree, where the write scope would then answer for them.
        # Placed there: the reading spells each pathspec from the tree, and a
        # grant resting on this checkout's history holds only for paths the
        # checkout answers for. Elsewhere the row's question stands, and the
        # write scope says where the loss lands.
        held = not any(
            write_checkpoint(
                write_scope(
                    operand["path"], context["path_roles"], context["checkout_root"]
                )
            )
            == "unrecoverable"
            for operand in verb_path_words(words, context["rows"])
        )
        # A refusal of the form stands wherever its paths are: only the grant
        # rests on this checkout's history.
        checkout = git_checkout_pathspec(words, context["rows"])
        if checkout is not None and checkout.effect == "deny":
            return checkout
        recognized = (
            (checkout if held else None)
            or (git_restore_source(words) if held else None)
            or git_restore_unchanged(
                words,
                context["recoverable_targets"],
                context["path_rules"],
                context["path_roles"],
            )
            or git_symbolic_ref_read(words)
        )
        if recognized is not None:
            return recognized
        # The readings above grant, so they read the subcommand where it is
        # written: a global they did not model could change what they cover.
        # One answer among them is a question rather than a grant -- a restore
        # reaching a protected file -- and that is owed however the restore
        # was spelled, so it is asked again past git's globals and only the
        # question is kept. `git --no-pager restore README.md` is the restore.
        at = global_span(words, context["rows"])
        owned = git_restore_unchanged(
            [words[0], *words[at:]],
            context["recoverable_targets"],
            context["path_rules"],
            context["path_roles"],
        )
        if owned is not None and owned.effect != "allow":
            return owned
    refused = refuses_generated_plugin_write(
        words,
        context["path_roles"],
        context["checkout_root"],
        context["displaced_targets"],
    )
    if refused is not None:
        return refused
    deleted = protected_deletion(
        words,
        context["path_rules"],
        context["rows"],
        context["path_roles"],
        context["checkout_root"],
    ) or protected_placement(
        words,
        context["path_rules"],
        context["rows"],
        context["path_roles"],
        context["existing_targets"],
        context["checkout_root"],
    )
    if deleted is not None:
        return deleted
    # `tee f` writes what `> f` writes, and the write walk judged its files
    # the way it judges a redirection target. The row asking about every tee
    # has nothing left to add, and keeping it is what gave the two spellings
    # of one write two answers. It still speaks for the verdict, so a table
    # that declares no tee leaves it unlisted rather than allowed, and one
    # that refuses tee keeps the refusal.
    teed = next(
        (row for row in context["rows"] if row["command"] == "tee"),
        None,
    )
    if (
        operands_judged
        and teed is not None
        and executable == "tee"
        and tee_operands(words) is not None
        and declared_verdict(
            teed["effects"],
            teed["refuses"],
            unresolved_evidence(write_facts(context)),
            "inside" if context["contained"] else "ambient",
        )
        != "deny"
    ):
        return row_verdict(
            teed, "allow", "tee's files are judged as a redirection's are"
        )
    # A copy over files that stand there is the edit it makes of each, and is
    # judged as one before any grant reads it as a loss that could be undone.
    copied = decide_copy_words(words, sed_facts(context), directory)
    if copied is not None:
        return copied
    recoverable = confined_to_recoverable_roots(
        words,
        context["path_roles"],
        context["recoverable_targets"],
        context["recoverable_target_limit"],
        context["path_rules"],
        context["existing_targets"],
    )
    if recoverable is not None:
        return recoverable
    landed = archive_lands_on_nothing(
        words,
        context["path_roles"],
        context["recoverable_targets"],
        context["path_rules"],
        context["existing_targets"],
        context["empty_directories"],
    )
    if landed is not None:
        return landed
    removal = asks_before_removing_a_directory(
        words, context["path_roles"], context["directory_targets"]
    )
    if removal is not None:
        return removal
    if executable == "xargs":
        return decide_xargs_words(words, context, directory)
    if executable == "env":
        return decide_env_words(words, context, directory)
    if executable == "time":
        return decide_time_words(words, context, directory)
    if executable == "printenv":
        return decide_printenv_words(words, context["secret_variables"])
    # `set` alone lists every variable the shell holds, exported or not.
    if executable == "set" and len(words) == 1:
        return environment_dump()
    if executable in ("curl", "wget"):
        return decide_download_words(
            words,
            context["allowed_scopes"],
            context["denied_scopes"],
            context["unscoped_fetch"],
            context["rows"],
            write_facts(context),
            directory,
            host_ports=context["host_ports"],
        )
    if executable == "gh":
        return decide_gh_words(words, context["rows"], write_facts(context))
    if executable == "find":
        return decide_find_words(words, context, directory)
    if executable == "sed":
        return decide_sed_words(words, sed_facts(context), directory)
    if executable in ("awk", "gawk", "mawk"):
        return decide_awk_words(words)
    if executable == "uvx":
        refused = decide_tool_run("uvx", words[1:])
        if refused is not None:
            return refused
        return decide_command_rows(words, context["rows"], write_facts(context))
    if executable == "uv" and len(words) > 1:
        # What `uv run` hands its words to, judged as its own command where
        # uv runs it; `decide_uv` says which programs that verdict answers for.
        handed = uv_run_placement(words, directory)
        return decide_uv(
            words,
            context["runner_targets"],
            context["target_tables"],
            write_facts(context),
            rows=context["rows"],
            program=None
            if handed is None
            else decide_shell_segment(handed["words"], context, handed["directory"]),
        )
    target = reached_target(words[0], directory, context)
    if target:
        # The same program `uv run` reaches, spelled without it: one row for
        # it, whichever spelling found it.
        return decide_uv(
            ["uv", "run", target, *words[1:]],
            context["runner_targets"],
            context["target_tables"],
            write_facts(context),
            rows=context["rows"],
        )
    decided = decide_command_rows(words, context["rows"], write_facts(context))
    # A variable a later command sees stays in this process, unless it is one
    # that swaps who that command acts as: the row states the first, and the
    # names say which one this is.
    if executable in BINDING_BUILTINS and decided.reach == "container":
        return decided.revised(reach=binding_reach(bound_names(words[1:])))
    return decided


def reached_target(word: str, directory: str | None, context: ShellContext) -> str:
    """The runner target a command word reaches without `uv run`, or nothing.

    A declared target is one program however a session reaches it: `uv run
    pytest`, `pytest` found on the path, and `.venv/bin/pytest` from this
    checkout's own environment run the same code, so each is judged by the
    one row `uv run` reads rather than by a row per spelling. A file that
    only shares the name -- under `tmp/`, in another project's environment
    -- is some other program. A command the vocabulary states a row for
    answers by that row instead, which is where a project declines a
    spelling on purpose.
    """
    name = posixpath.basename(word)
    if not any(row["name"] == name for row in context["runner_targets"]):
        return ""
    if declares_command(name, context["rows"]):
        return ""
    if word == name:
        return name
    placed = placed_path(word, directory)
    spelled = repository_relative(placed, context["checkout_root"]) if placed else ""
    return name if posixpath.normpath(spelled) == f".venv/bin/{name}" else ""


def decide_shell_segment(
    segment: list[str],
    context: ShellContext,
    directory: str | None = "",
    operands_judged: bool = False,
) -> KernelDecision:
    """Classify one parsed shell segment, letting a help probe soften the effect.

    ``directory`` is where the shell stands to run it, and every path word the
    segment names is rewritten from it before a rule is matched against one --
    so a declared role anchored at the repository top is asked about the file
    the command would reach rather than about that spelling at the launch
    directory. A payload this recurses into runs where its carrier does, so it
    is classified in the same one.

    Printing usage says nothing about *where* the command has to run, and the
    two are separate axes — so the probe replaces the verdict and the walk
    still answers for the placement. Short-circuited above that walk it
    would answer for both, and drop every declared placement a ``--help``
    sits in: ``uv run lup-devtools dev check`` placed ``outside`` while
    ``uv run lup-devtools --help`` — the same toolchain, one word apart — and
    every other help probe in the vocabulary placed ``ambient``.

    That is the half of the defect a session reaches: a toolchain declared
    ``outside`` because it opens agent sessions would be asked for its own
    usage from inside the sandbox that placement exists to escape.
    """
    while segment and segment[0] == "!":
        segment = segment[1:]
    if not segment:
        return unjudged("shell segment has no command")
    if segment[0] == "[[":
        return KernelDecision("allow", "test expression is read-only")
    effective = effective_command(segment)
    words = effective["words"]
    dangerous = effective["dangerous"]
    if dangerous:
        # The command the binding rides on is judged beside it. The binding's
        # own question stands either way, and a boundary that settles that
        # question must not settle the push it rode in on:
        # `PYTHONPATH=x git push --delete origin b` asks about both.
        assigned = dangerous_assignment("assigning", dangerous)
        if not words:
            return assigned
        return joined_decision(
            [assigned, decide_shell_segment(words, context, directory, operands_judged)]
        )
    if not words:
        return unjudged("shell segment has no command")
    withheld = withheld_operand(
        words, directory, context["checkout_root"], context["refused_paths"]
    ) or withheld_walk(
        words,
        directory,
        context["checkout_root"],
        context["withheld_walks"],
        context["refused_paths"],
        context["names_read"],
    )
    if withheld is not None:
        return withheld
    placement = placed_words(words, directory, context["rows"])
    placed = placement["words"]
    # Where the command's own globals stand it, which the placed words no
    # longer spell: `uv --directory d run rm x` hands `rm x` to `d`.
    moved = command_directory(words, context["rows"])
    here = (
        None
        if moved is None
        else directory
        if not moved
        else joined_directory(directory, moved)
    )
    # Read off the placed words, where a `cd` and `git -C` already stand, and
    # only where no `--git-dir` moved the repository: consumed with the other
    # globals, it is the one of them that says where `init` makes one.
    head = words[: global_span(words, context["rows"])]
    if not any(word == "--git-dir" or word.startswith("--git-dir=") for word in head):
        made = git_init_in_scratch(
            placed, context["path_roles"], context["checkout_root"]
        )
        if made is not None:
            return made
    # A command word nobody can read is read as each program whose verb the
    # words after it name, and the spelling's own verdict is the floor.
    verdict = strictest_reading(
        decide_placed_words(placed, context, here, operands_judged),
        [
            Reading(
                word=placed[0],
                spelled=program,
                decision=decide_placed_words([program, *placed[1:]], context, here),
            )
            for program in unread_programs(placed, context["rows"])
        ],
    )
    worked = git_worked_tree(words, directory, context["rows"])
    if (
        worked
        and recovery_dischargeable(verdict)
        and write_checkpoint(
            write_scope(worked, context["path_roles"], context["checkout_root"])
        )
        == "unrecoverable"
    ):
        verdict = unheld_loss(verdict, worked)
    if not placement["unplaced"]:
        return verdict
    return objection_or_floor(
        unjudged(
            "this segment names a file from a directory a `cd` left unreadable"
        ).advising("Spell the path in full, or run the command in its own call."),
        (verdict,),
    )


def unheld_loss(decision: KernelDecision, tree: str) -> KernelDecision:
    """A loss a capture would answer for, landing in a tree no capture holds.

    A capture is a snapshot of this checkout, so the question it retires is
    about a loss here. Git moved into another tree -- `--work-tree` naming
    one, `-C` standing in one -- changes files there, and `reset --hard` or
    a bare `clean` changes that whole tree without naming a path the loss
    could be read from. So every part of the question keeps it.
    """
    return decision.revised(
        reason=f"{decision.reason} — in {tree}, which no capture of this checkout"
        " holds",
        checkpoint="unrecoverable",
        findings=tuple(
            part.revised(checkpoint="unrecoverable") for part in decision.findings
        ),
    )


def decide_placed_words(
    words: list[str],
    context: ShellContext,
    directory: str | None,
    operands_judged: bool = False,
) -> KernelDecision:
    """One segment's verdict once its words are placed, each read as spelled.

    ``operands_judged`` is :func:`decide_segment_words`' own, and holds only for
    the words as spelled: a reading of an unread command word as another
    program was never walked for the files that program writes.
    """
    if SUBSTITUTION_SENTINEL in words[0]:
        return unjudged("a command substitution in command position is not classified")
    if any(
        SUBSTITUTION_SENTINEL in word for word in words[1:]
    ) and not argument_safe_words(words, context):
        refused = standing_interpreter_refusal(words, context)
        if refused is not None:
            return refused
        # Abstaining is the floor rather than the answer: a result standing
        # where a verb or a guarded flag goes is read as the strictest one it
        # could be, as any other word nobody can read is. The result could as
        # well be the operand it is standing in for, so what the command as
        # spelled asks is asked -- `sed -i 1d $(cat f)` could rewrite a
        # protected file -- and the floor carries what it objects to
        # otherwise: `git push $(cat f)` could name a branch to force or
        # delete.
        floor = unjudged(
            "a command substitution result could become a guarded flag"
        ).advising("Run it in its own call and splice the literal output.")
        spelled = decide_segment_words(words, context, directory, operands_judged)
        return strictest_reading(
            objection_or_floor(floor, (spelled,)),
            unread_readings(words, context["rows"], write_facts(context)),
        )
    decision = decide_segment_words(words, context, directory, operands_judged)
    if is_help_probe(words[1:]) and not refuses_a_program(decision):
        return decision.revised(
            effect="allow",
            reason="a help probe only prints usage",
            purpose=None,
            cause=None,
            abstention=None,
        )
    return decision


def refuses_a_program(decision: KernelDecision) -> bool:
    """Whether an interpreter's refusal of the program it runs is in this verdict.

    A help probe answers for the command that reads the `--help`, and one an
    interpreter's program is handed is that program's argument: `bash -c ls
    --help` runs `ls`, and `bash -h` is a flag while stdin carries the
    program. Read through every part, since a carrier -- `uv run`, `xargs`,
    `find -exec` -- hands the payload's refusal on as its own or beside
    another, and the carrier's words hold the same `--help`.
    """
    return decision.rule == PROGRAM_RULE or any(
        refuses_a_program(part) for part in decision.findings
    )


def uv_post_target_words_safe(
    words: list[str], runner_targets: list[RunnerTargetRow]
) -> bool:
    """True when every unknown word sits strictly after a blessed uv run target.

    uv stops parsing its own options at the first positional word, so a word
    after a literal blessed target only ever reaches that target's argv —
    the trust literal arguments already receive there. An unknown word at or
    before the target could become a uv flag, a flag value, or the target
    itself, so any such word keeps the conservative gate.

    The target is found as `decide_uv` finds it, past uv's globals and the
    options of `run` with their values: read as the first word not beginning
    with a dash, `uv run --refresh-package lup-devtools python $(...)` named
    the blessed target where it names a package, and `uv --quiet run
    lup-devtools $(...)` named no `run` at all.
    """
    normalized = uv_command_words(words)
    if normalized is None or normalized[1:2] != ["run"]:
        return False
    run_words = uv_run_words(normalized)
    options = normalized[2 : len(normalized) - len(run_words)]
    if not run_words or any(opaque_argument(word) for word in options):
        return False
    target = run_words[0]
    return (
        not opaque_argument(target)
        and "/" not in target
        and any(row["name"] == target for row in runner_targets)
    )


def argument_safe_words(words: list[str], context: ShellContext) -> bool:
    """True when the command's row allows regardless of argument content.

    A loop variable bound to a non-literal word list can expand to any word,
    including a flag-shaped one, so only a single unguarded command-level
    allow row qualifies — flag-guarded rows and the specially parsed
    executables do not. ``uv run`` is the carved-out exception: unknown
    words strictly behind a literal blessed target are inert.
    """
    executable = posixpath.basename(words[0])
    if executable == "uv":
        return uv_post_target_words_safe(words, context["runner_targets"])
    if executable in INTERPRETERS or executable in (
        "sed",
        "git",
        "uvx",
        "xargs",
        "curl",
        "wget",
    ):
        return False
    matches = [row for row in context["rows"] if row["command"] == executable]
    if len(matches) != 1 or matches[0]["subcommand"] or matches[0]["ask_flags"]:
        return False
    # The same reading the classifier reaches for the same row, so a command
    # judged safe for an opaque argument here and judged again there cannot
    # come to differ -- which is the whole reason the verdict is derived from
    # one declaration rather than written down twice.
    return (
        declared_verdict(
            matches[0]["effects"],
            matches[0]["refuses"],
            unresolved_evidence(write_facts(context)),
            "inside" if context["contained"] else "ambient",
        )
        == "allow"
    )


def read_bindings(
    words: list[str], bindings: tuple[ShellBinding, ...]
) -> tuple[ShellBinding, ...] | KernelDecision:
    """Bindings extended by the read builtin's targets as opaque values."""
    names: list[str] = []
    for word in words[1:]:
        if word == "-r":
            continue
        if word.startswith("-"):
            return unjudged(f"read option {word!r} is not classified")
        if not word.isidentifier():
            return unjudged("read target is not a plain variable")
        names.append(word)
    dangerous = [name for name in names or ["REPLY"] if dangerous_env_name(name)]
    if dangerous:
        return dangerous_assignment("binding", dangerous)
    for name in names or ["REPLY"]:
        bindings = bind_name(bindings, name, None)
    return bindings


def gate_references(
    words: list[Word], bindings: tuple[ShellBinding, ...], context: ShellContext
) -> KernelDecision | None:
    """The floor under a command referencing a name this walk bound opaquely.

    Literal bindings were already expanded, once, by the binding pass over
    the command's tree (:func:`~lup.policy.kernel.bindings.bind_script`),
    which every other reader of the command shares. A reference still
    standing is one that pass would not resolve -- a ``read``, a non-literal
    assignment, a loop over words nobody can list, or a name rebound inside a
    construct that may or may not run -- so it can expand to any word, and a
    referencing command that is not argument-safe earns at least this floor.
    It is a floor and not the answer: :func:`decide_simple` judges the
    command as spelled beside it, with the reference standing, and whatever
    that asks is asked (:func:`~lup.policy.kernel.commands.objection_or_floor`).
    Expanding it here instead would judge a word the host's readers never
    see, which is the disagreement the pass removed.
    """
    for binding in bindings:
        if not references(words, binding["name"]):
            continue
        effective = command_words([word_text(word) for word in words])
        if not effective or not argument_safe_words(effective, context):
            return standing_interpreter_refusal(effective, context) or unjudged(
                "an opaquely bound variable could become a guarded flag"
            ).advising(
                "Run what computes the value in its own call and write the"
                " literal it printed into this one, or do the whole computation"
                " in a script file (`uv run python tmp/<name>.py`), which is read"
                " as the file it is."
            )
    return None


class Walked(TypedDict):
    """What classifying a list decided, and the bindings standing after it."""

    decisions: list[KernelDecision]
    bindings: tuple[ShellBinding, ...]


def decide_for_body(
    command: Command,
    context: ShellContext,
    depth: int,
    bindings: tuple[ShellBinding, ...] = (),
) -> list[KernelDecision]:
    """Classify a ``for`` body once per literal loop word, or with its name unread.

    A literal word list instantiates the body exactly -- once per word, in the
    binding pass every reader shares -- so a word landing in a guarded flag
    position is judged as the flag it becomes. Any other loop -- over a glob,
    an expansion, the positional parameters, more words than one line is
    worth reading, or with a body assigning the loop's own name -- leaves
    each reference to the name as spelled, and the body is judged with the
    name bound to nothing anybody can read: every command referencing it
    stands on :func:`gate_references`' floor, and asks whatever it asks with
    the reference standing, so `for f in src/*.py; do sed -i 1d $f; done` asks
    as `sed -i 1d $f` does.
    """
    name = command["name"]
    if dangerous_env_name(name):
        return [
            KernelDecision("ask", dangerous_assignment_reason("looping over", [name]))
        ]
    # The binding pass read a literal list's body once per word already, for
    # every reader of the line, so each pass is in the body as it stands.
    scope = bindings if unrollable(command) else bind_name(bindings, name, None)
    return decide_list(command["body"], context, depth + 1, scope)["decisions"]


def unreadable_construct(
    reason: str, command: Command, context: ShellContext
) -> KernelDecision:
    """What a construct the walk does not read earns: its floor, or what it runs asks.

    A function, a ``select``, an arithmetic command and a construct nested
    past the depth the walk opens are left unjudged as structures, but the
    commands inside them are commands all the same: `function f { rm
    README.md; }` removes a human-owned file whenever `f` runs, and a body
    runs wherever the call is, which nothing here follows. Each is judged where it
    stands, flat, and what any of them asks is asked, over the floor the
    construct earns on its own.
    """
    inside = [
        decide_shell_segment(texts, context, inner["directory"], operands_judged=True)
        for script in command_lists(command)
        for inner in simple_commands(script)
        if inner["kind"] == "simple"
        for texts in [[word_text(word) for word in inner["words"]]]
        if texts
    ]
    return objection_or_floor(unjudged(reason), tuple(inside))


def joined_lists(scripts: list[Script]) -> Script:
    """Several lists read as one, in order.

    A construct's condition and bodies classify as one sequential list, so a
    ``read`` in a ``while`` condition binds for the body it guards.
    """
    return Script(items=[item for script in scripts for item in script["items"]])


def decide_substitutions(
    words: list[Word],
    context: ShellContext,
    depth: int,
    bindings: tuple[ShellBinding, ...],
) -> list[KernelDecision]:
    """Classify every command the substitutions in these words run.

    Each runs in a subshell of its own, so nothing it binds reaches the words
    after it.
    """
    return [
        decision
        for inner in substitutions(words)
        for decision in decide_list(inner, context, depth, bindings)["decisions"]
    ]


def decide_simple(
    command: Command,
    context: ShellContext,
    bindings: tuple[ShellBinding, ...],
) -> Walked:
    """Classify one simple command, rebinding where it assigns or reads.

    A reference to a name bound to something nobody can read puts the
    command on :func:`gate_references`' floor, and the command as spelled is
    judged beside it: what that asks is asked.
    """
    walked = decide_simple_words(command, context, bindings)
    gated = gate_references(command["words"], bindings, context)
    if gated is None:
        return walked
    return Walked(
        decisions=[objection_or_floor(gated, tuple(walked["decisions"]))],
        bindings=walked["bindings"],
    )


def decide_simple_words(
    command: Command,
    context: ShellContext,
    bindings: tuple[ShellBinding, ...],
) -> Walked:
    """One simple command's own verdict, and the bindings it leaves behind."""
    texts = [word_text(word) for word in command["words"]]
    if not texts:
        return Walked(decisions=[], bindings=bindings)
    assignments = pure_assignment_names(texts)
    if assignments is not None:
        dangerous = [
            pair["name"] for pair in assignments if dangerous_env_name(pair["name"])
        ]
        if dangerous:
            return Walked(
                decisions=[dangerous_assignment("assigning", dangerous)],
                bindings=bindings,
            )
        for pair in assignments:
            bindings = bind_name(bindings, pair["name"], pair["value"])
        return Walked(decisions=[], bindings=bindings)
    words = effective_command(texts)["words"]
    printed = printed_secret(
        command["words"][len(texts) - len(words) :],
        command["redirects"],
        context["secret_variables"],
    )
    if printed is not None:
        return Walked(decisions=[printed], bindings=bindings)
    if words and posixpath.basename(words[0]) == "read":
        extended = read_bindings(words, bindings)
        if isinstance(extended, KernelDecision):
            return Walked(decisions=[extended], bindings=bindings)
        return Walked(decisions=[], bindings=extended)
    return Walked(
        decisions=[
            decide_shell_segment(
                texts, context, command["directory"], operands_judged=True
            )
        ],
        bindings=bindings,
    )


def decide_command(
    command: Command,
    context: ShellContext,
    depth: int,
    bindings: tuple[ShellBinding, ...],
) -> Walked:
    """Classify one command of any kind, recursing into the lists it runs.

    A loop, a conditional and a case construct each open one level, and a
    third level is read flat rather than walked (:func:`unreadable_construct`).
    A brace group runs in this shell, so what it binds stands after it; a
    subshell does not.
    """
    if command["kind"] == "simple":
        judged = decide_simple(command, context, bindings)
        spelled = spelled_command(command)
        inner = decide_substitutions(carried_words(command), context, depth, bindings)
        return Walked(
            decisions=[
                *[
                    judged_command(decision, spelled)
                    for decision in judged["decisions"]
                ],
                *inner,
            ],
            bindings=judged["bindings"],
        )
    header = decide_substitutions(carried_words(command), context, depth, bindings)

    def unread(reason: str) -> Walked:
        return Walked(
            decisions=[*header, unreadable_construct(reason, command, context)],
            bindings=bindings,
        )

    def nested(scripts: list[Script]) -> Walked:
        walked = decide_list(joined_lists(scripts), context, depth + 1, bindings)
        return Walked(decisions=[*header, *walked["decisions"]], bindings=bindings)

    match command["kind"]:
        case "test":
            return Walked(
                decisions=[
                    *header,
                    KernelDecision("allow", "test expression is read-only"),
                ],
                bindings=bindings,
            )
        case "brace" | "subshell":
            walked = decide_list(command["body"], context, depth, bindings)
            return Walked(
                decisions=[*header, *walked["decisions"]],
                bindings=walked["bindings"] if command["kind"] == "brace" else bindings,
            )
        case "for" if depth >= 2:
            return unread("loops nest too deeply")
        case "for":
            return Walked(
                decisions=[
                    *header,
                    *decide_for_body(command, context, depth, bindings),
                ],
                bindings=bindings,
            )
        case "while" | "until" if depth >= 2:
            return unread("loops nest too deeply")
        case "while" | "until":
            clause = command["clauses"][0]
            return nested([clause["condition"], clause["body"]])
        case "if" if depth >= 2:
            return unread("conditionals nest too deeply")
        case "if":
            return nested(
                [
                    *(
                        script
                        for clause in command["clauses"]
                        for script in (clause["condition"], clause["body"])
                    ),
                    command["body"],
                ]
            )
        case "case" if depth >= 2:
            return unread("case constructs nest too deeply")
        case "case":
            return nested([arm["body"] for arm in command["arms"]])
        case "select":
            return unread("loop form is not classified")
        case "function":
            return unread("shell function definitions are not classified")
        case _:
            return unread("arithmetic command is not classified")


def decide_list(
    script: Script,
    context: ShellContext,
    depth: int = 0,
    bindings: tuple[ShellBinding, ...] = (),
) -> Walked:
    """Classify a command list in order, threading bindings through it.

    Bindings are frozen pairs: an assignment or read rebinds by producing a
    new tuple for the commands that follow, recursion receives the current
    value, and nothing mutates across scopes. A reference the binding pass
    left live stays a live ``$`` word for the guarded-flag gates.

    Every command is judged, a construct the walk does not read among them:
    what runs after one is still what runs, and a verdict that stopped there
    left each command behind it to whichever boundary settles the floor.
    """
    decisions: list[KernelDecision] = []
    for command in list_commands(script):
        walked = decide_command(command, context, depth, bindings)
        decisions.extend(walked["decisions"])
        bindings = walked["bindings"]
    return Walked(decisions=decisions, bindings=bindings)


def classify_shell(
    command: str,
    rows: list[ShellRuleRow],
    allowed_scopes: list[UrlScopeRow] | None = None,
    denied_scopes: list[UrlScopeRow] | None = None,
    trusted_script_roots: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    existing_targets: list[str] | None = None,
    tracked_targets: list[str] | None = None,
    recoverable_targets: list[str] | None = None,
    directory_targets: list[str] | None = None,
    empty_directories: list[str] | None = None,
    recoverable_target_limit: int = 5,
    runner_targets: list[RunnerTargetRow] | None = None,
    target_tables: list[ShellRuleRow] | None = None,
    contained: bool = False,
    checkout_root: str = "",
    unscoped_fetch: UnjudgedAmbient = "ask",
    refused_paths: list[RefusedPathRow] | None = None,
    secret_variables: list[str] | None = None,
    antipattern_rows: dict[str, list[AntiPatternRow]] | None = None,
    edit_rules: list[EditRuleRow] | None = None,
    import_boundaries: list[ImportBoundaryRow] | None = None,
    acceptance_guard: AcceptanceGuardRow | None = None,
    maximum_added_lines: int = 3,
    autonomous: bool = False,
    allowances: list[str] | None = None,
    rewritten_documents: list[RewrittenDocumentRow] | None = None,
    unproduced_documents: list[UnproducedDocumentRow] | None = None,
    displaced_targets: list[DisplacedTargetRow] | None = None,
    host_ports: list[int] | None = None,
    withheld_walks: list[WithheldWalkRow] | None = None,
) -> KernelDecision:
    """Conservatively classify every command in one shell command line.

    A line the grammar stops reading partway is still judged as far as it
    was read. The shell runs a line's complete commands before it meets the
    one that does not parse, so what reached a verdict before that point
    keeps it -- an unjudged remainder is carried by a boundary, and a boundary
    carrying `rm -rf ~` because a later line was malformed would be a
    relaxation bought by a typo.
    """
    # Named rather than positional: twelve lists of the same shape, and a
    # thirteenth inserted anywhere but the end silently re-seats every one
    # after it — passing a limit where a path list belongs.
    context = shell_context(
        rows,
        allowed_scopes=allowed_scopes,
        denied_scopes=denied_scopes,
        trusted_script_roots=trusted_script_roots,
        path_roles=path_roles,
        path_rules=path_rules,
        existing_targets=existing_targets,
        tracked_targets=tracked_targets,
        recoverable_targets=recoverable_targets,
        directory_targets=directory_targets,
        empty_directories=empty_directories,
        recoverable_target_limit=recoverable_target_limit,
        runner_targets=runner_targets,
        target_tables=target_tables,
        contained=contained,
        checkout_root=checkout_root,
        unscoped_fetch=unscoped_fetch,
        refused_paths=refused_paths,
        withheld_walks=withheld_walks,
        secret_variables=secret_variables,
        antipattern_rows=antipattern_rows,
        edit_rules=edit_rules,
        import_boundaries=import_boundaries,
        acceptance_guard=acceptance_guard,
        maximum_added_lines=maximum_added_lines,
        autonomous=autonomous,
        allowances=allowances,
        rewritten_documents=rewritten_documents,
        unproduced_documents=unproduced_documents,
        displaced_targets=displaced_targets,
        host_ports=host_ports,
        names_read=names_read(command, rows),
    )
    tree = parse_shell(command)
    if isinstance(tree, KernelDecision):
        if tree.effect != "defer":
            return tree
        read = bind_script(readable_prefix(command))
        if not read["items"]:
            return tree
        redirected = named_write_verdict(
            read,
            existing_targets,
            path_roles,
            path_rules,
            recoverable_targets,
            contained,
            checkout_root,
            tracked_targets,
            displaced_targets,
        )
        withheld = withheld_redirect(read, checkout_root, context["refused_paths"])
        return joined_decision(
            [
                *([] if withheld is None else [withheld]),
                *([] if redirected is None else [redirected]),
                *decide_list(read, context)["decisions"],
                tree,
            ]
        )
    withheld = withheld_redirect(tree, checkout_root, context["refused_paths"])
    if withheld is not None:
        return withheld
    redirected = named_write_verdict(
        tree,
        existing_targets,
        path_roles,
        path_rules,
        recoverable_targets,
        contained,
        checkout_root,
        tracked_targets,
        displaced_targets,
    )
    if redirected is not None:
        return redirected
    if not command_segments(tree):
        return unjudged("shell command has no executable segment")
    return joined_decision(decide_list(tree, context)["decisions"])


def excluded_prefix(pattern: str) -> list[str]:
    """The literal words a sandbox-exclusion pattern matches a segment by.

    Reading a pattern down to its literal head is the conservative half of
    its meaning: whatever a wildcard goes on to match, the boundary drops at
    least everything the head covers, so taking the head for the whole rule
    can only classify more commands as unconfined, never fewer.
    """
    words = pattern.split()
    wildcards = [index for index, word in enumerate(words) if "*" in word]
    return words[: wildcards[0]] if wildcards else words


def sandbox_excluded(command: str, patterns: list[str]) -> bool:
    """Whether the boundary was told to leave this command out of isolation.

    Exclusion is the sandbox's only per-command lever, and it removes the
    command entirely rather than lifting one rule — so a command that
    matches runs with nothing beneath it, and work the policy would have
    handed to the boundary has to be judged here instead. Every segment is
    tested, because a compound command carries an exclusion as a whole, and
    one the parser cannot read falls back to a single segment, which is what
    the boundary does with it too.
    """
    prefixes = [excluded_prefix(pattern) for pattern in patterns]
    segments = parse_shell_words(command)
    read = segments if isinstance(segments, list) else [command.split()]
    return any(
        bool(prefix) and segment[: len(prefix)] == prefix
        for segment in read
        for prefix in prefixes
    )


def auto_escape_matches(command: str, prefixes: list[list[str]]) -> bool:
    """Whether one simple command has a native auto-escape prefix."""
    tree = parse_shell(command)
    if isinstance(tree, KernelDecision):
        return False
    commands = list_commands(tree)
    if (
        len(commands) != 1
        or commands[0]["kind"] != "simple"
        or substitutions(carried_words(commands[0]))
    ):
        return False
    words = [word_text(word) for word in commands[0]["words"]]
    return any(bool(prefix) and words[: len(prefix)] == prefix for prefix in prefixes)


def decide_shell(
    command: str,
    rows: list[ShellRuleRow],
    allowed_scopes: list[UrlScopeRow] | None = None,
    denied_scopes: list[UrlScopeRow] | None = None,
    sandboxed: bool = False,
    excluded_commands: list[str] | None = None,
    trusted_script_roots: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    interactive: bool = True,
    existing_targets: list[str] | None = None,
    tracked_targets: list[str] | None = None,
    recoverable_targets: list[str] | None = None,
    directory_targets: list[str] | None = None,
    empty_directories: list[str] | None = None,
    recoverable_target_limit: int = 5,
    runner_targets: list[RunnerTargetRow] | None = None,
    target_tables: list[ShellRuleRow] | None = None,
    escapable: bool = False,
    contained: bool = False,
    checkout_root: str = "",
    inside_placement: bool = False,
    recovered: bool = False,
    relayed: bool = False,
    unjudged_ambient: UnjudgedAmbient = "ask",
    unscoped_fetch: UnjudgedAmbient | None = None,
    refused_paths: list[RefusedPathRow] | None = None,
    secret_variables: list[str] | None = None,
    unleased_targets: list[str] | None = None,
    readonly_targets: list[str] | None = None,
    displaced_targets: list[DisplacedTargetRow] | None = None,
    host_ports: list[int] | None = None,
    landings: list[TargetLandingRow] | None = None,
    antipattern_rows: dict[str, list[AntiPatternRow]] | None = None,
    edit_rules: list[EditRuleRow] | None = None,
    import_boundaries: list[ImportBoundaryRow] | None = None,
    acceptance_guard: AcceptanceGuardRow | None = None,
    maximum_added_lines: int = 3,
    autonomous: bool = False,
    allowances: list[str] | None = None,
    rewritten_documents: list[RewrittenDocumentRow] | None = None,
    unproduced_documents: list[UnproducedDocumentRow] | None = None,
    withheld_walks: list[WithheldWalkRow] | None = None,
) -> KernelDecision:
    """Classify one command, honoring an escalation marker and hinting denies.

    Two steps, and only the first is here. This reads the leading
    ``# lup: escalate[<kind>]: <why>`` line off the command, refuses a marker
    that states no reason or names no kind, and hands the classified verdict to the settlement
    order in ``settlement.py`` along with every session fact that bears on
    it: whether a boundary is running, whether this host can put one call
    outside it, and whether there is anybody to ask.

    What that order says, in the order it says it. A stated reason turns
    anything not already permitted into the approval question the agent asked
    for, carrying the reason it gave. A call declared ``outside`` on a host
    with no channel to put it there is refused outright, because approval
    would only move the failure to a bare filesystem error with the boundary
    misreported as a bug in the code. A question on a host with nobody to put
    it to is no judgment at all. What nobody judged, a boundary carries — and
    without one, the refusal names the escalation recipe, so unjudged work
    bounces back to the agent to be reshaped into the allowed vocabulary or
    deliberately promoted. A judged deny is never rescued by the sandbox: it
    is somebody's answer, and running it confined would still be running it.

    A command the declaration excludes from the sandbox is an escape without
    saying so — the boundary was told to leave it alone — so it is judged as
    though no sandbox were running at all, which is what the sandbox term
    passed on says that ``sandboxed`` does not. It reduces that term only: a
    container holds a command the sandbox was told to skip, having never been
    asked about it in the first place.

    ``contained`` and ``inside_placement`` are the container's half of the
    same question, and both are the launch's own measurements rather than
    anything read here. They are passed apart and joined by
    :meth:`~lup.policy.kernel.settlement.SettlementFacts.bounded`, so no
    caller decides what confinement means on its own.

    ``escapable`` is whether this host can put one call outside its own
    sandbox, and the pair with the declaration is what makes a placement safe
    to give a toolchain: where the escape is carried out it is unprompted,
    and where nothing can carry it out the refusal names the sandbox instead
    of a bare write error.

    ``relayed`` says a non-interactive session is not therefore *alone*. A
    reviewed worker holds a question mailbox reaching the human supervising
    the run, so a refusal telling it to reshape the command names the only
    route it has as unavailable — and measured, it does what anybody would
    and queues a material question instead, parking the whole run on a
    decision nobody needed to make. Three states rather than two, because
    "nobody to ask" and "somebody, but not right now" are different answers,
    and two states would make them share one.

    ``unscoped_fetch`` is what a `curl` or `wget` of an origin no fetch scope
    names answers, and ``None`` reads ``unjudged_ambient`` for it.
    """
    hint = ESCALATE_HINT if interactive else RELAY_HINT if relayed else RESHAPE_HINT
    # Net of exclusion here, because exclusion is the native sandbox's own
    # per-command lever and reduces no other boundary. What that leaves is one
    # of the two terms `SettlementFacts.bounded` joins, never the whole answer.
    natively = sandboxed and not sandbox_excluded(command, excluded_commands or [])

    reading = read_escalation(command)
    if reading.refusal:
        return KernelDecision(
            "deny", reading.refusal, cause="deliberate", hard=True, recovery=hint
        )
    return settle(
        SettlementFacts(
            classify_shell(
                reading.remainder,
                rows,
                allowed_scopes=allowed_scopes,
                denied_scopes=denied_scopes,
                trusted_script_roots=trusted_script_roots,
                path_roles=path_roles,
                path_rules=path_rules,
                existing_targets=existing_targets,
                tracked_targets=tracked_targets,
                recoverable_targets=recoverable_targets,
                directory_targets=directory_targets,
                empty_directories=empty_directories,
                recoverable_target_limit=recoverable_target_limit,
                runner_targets=runner_targets,
                target_tables=target_tables,
                # A write outside the checkout is confined by a boundary the
                # same way a read of one is, so the redirection reading needs
                # the same fact the settlement below already has.
                contained=contained,
                # And the same root, because a declared role is anchored at
                # the repository top: without it the absolute spelling of a
                # path inside the checkout reaches no declaration, and one
                # file answers twice depending on how it was named.
                checkout_root=checkout_root,
                # And the downloaders need what an unlisted origin answers:
                # the fetch declaration where the caller holds one, and the
                # settlement's own posture below where it does not.
                unscoped_fetch=unscoped_fetch or unjudged_ambient,
                # What no word may name and no builtin may print, declared by
                # the project rather than known here -- and which walks the
                # host found reaching one without naming it.
                refused_paths=refused_paths,
                withheld_walks=withheld_walks,
                secret_variables=secret_variables,
                # The edit gates, for the verbs that rewrite a file in place.
                # Absent, every such rewrite asks, which is the arrangement
                # that makes a composition forgetting them safe rather than
                # silently permissive.
                antipattern_rows=antipattern_rows,
                edit_rules=edit_rules,
                import_boundaries=import_boundaries,
                acceptance_guard=acceptance_guard,
                maximum_added_lines=maximum_added_lines,
                autonomous=autonomous,
                allowances=allowances,
                rewritten_documents=rewritten_documents,
                unproduced_documents=unproduced_documents,
                # Where a link moves a write, for the one grant a segment
                # reads off a spelling it has to be able to trust: the scratch
                # exception to the generated-plugin refusal.
                displaced_targets=displaced_targets,
                # The loopback ports the operator's own processes hold, which a
                # container sharing their network reaches through the same
                # scope its own development servers are declared under.
                host_ports=host_ports,
            ),
            escalation=reading.request,
            contained=contained,
            inside_placement=inside_placement,
            sandbox_confined=natively,
            host_executor=escapable,
            human_execution=interactive or relayed,
            reviewable=interactive or relayed,
            checkpoint="complete" if recovered else "absent",
            unjudged_ambient=unjudged_ambient,
            unleased=unleased_targets,
            readonly=readonly_targets,
            # Read here rather than handed in: what a file *is* needs no host,
            # so every caller -- a composed policy, either dispatcher, `dev
            # policy` -- refuses the same pointers with no fact to forget.
            guarded=guarded_write_targets(reading.remainder, rows, displaced_targets),
            displaced=displaced_targets,
            landings=landings,
            hint=hint,
        )
    )


def shell_posture_targets(command: str, rows: list[ShellRuleRow]) -> list[str]:
    """Every path this command names as a place it changes, for the posture question.

    What :class:`~lup.policy.kernel.settlement.ContainedJudgement` asks the
    host to place: whether each lands in the container, in the session's own
    checkout, or somewhere else the host lent. The readers every other
    question already trusts -- redirections, a path verb's operands, a write
    flag's value -- and the landing a row declares, read off the row the same
    walk the verdict takes reaches.

    Over-naming is safe here and under-naming is not, the opposite of the
    lease question: a path named that the command never writes can only keep
    a question, and one missed could let a harm on it through. So a segment
    whose directory a `cd` left unreadable names ``$``, which no host can
    place and every reader takes as landing somewhere lent.
    """
    segments = read_segments(command, rows)
    # lup: defer: an unread verb is walked here as spelled, so a landing only
    # one of its readings declares -- `gh repo cl$OP o/r <dir>` could be `gh
    # repo clone` -- goes unnamed, while the verdict carries that reading's
    # reach. Unreachable while every gated command with a landing row refuses
    # an unknown verb with a reach no container holds (git, gh); name each
    # verb reading's landing here, from the spellings `unread_readings` judges,
    # before a row makes it reachable.
    landed = [
        placed
        for segment in segments
        if segment["directory"] is not None and segment["words"]
        for matched in [matched_command_row(segment["words"], rows)]
        if not isinstance(matched, KernelDecision)
        for word in landing_words(matched)
        for placed in [placed_path(word, segment["directory"])]
        if placed is not None
    ]
    unplaced = (
        ["$"] if any(segment["directory"] is None for segment in segments) else []
    )
    return list(
        dict.fromkeys(
            [
                *shell_write_targets(command),
                *shell_path_verb_targets(command, rows),
                *shell_flag_write_targets(command, rows),
                *shell_worked_trees(command, rows),
                *landed,
                *unplaced,
            ]
        )
    )
