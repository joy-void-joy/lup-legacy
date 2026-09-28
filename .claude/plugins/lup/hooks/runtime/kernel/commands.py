# lup: ignore[empty-collection, import-re, re-call, string-split]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Per-command shell classification for the judged executables."""

import posixpath
import re
from collections.abc import Sequence
from typing import TypedDict

from .decision import (
    CheckpointRequirement,
    DecisionEffect,
    KernelDecision,
    SUBSTITUTION_SENTINEL,
    SandboxPlacement,
    carrying_readings,
    joined_decision,
    objecting_reach,
    unjudged,
    unlisted,
)
from .rows import (
    AcceptanceGuardRow,
    AntiPatternRow,
    EditRuleRow,
    ImportBoundaryRow,
    PathRoleRow,
    PathRuleRow,
    RewrittenDocumentRow,
    RunnerTargetRow,
    ShellRuleRow,
    UnproducedCause,
    UnproducedDocumentRow,
    UrlScopeRow,
)
from .edit import decide_edit
from .effects import (
    STRENGTH,
    EffectEvidence,
    EffectRow,
    declare,
    declared_verdict,
    question_reach,
    purpose_of,
    verdict_for,
)
from .words import (
    UV_GLOBAL_VALUE_OPTIONS,
    UV_TOOL_RUN_GRAMMAR,
    INTERPRETERS,
    carried_setting,
    command_words,
    destination_form,
    flag_matches,
    flag_write_targets,
    git_restore_operands,
    key_matches,
    opaque_argument,
    operand_positions,
    operand_words,
    protected_write_target,
    refspec_destination,
    refspec_effects,
    sed_invocation,
    unlocated_write,
    unread_flags,
    unread_over_tracked,
    unread_prefix,
    unread_question,
    uv_command_words,
    uv_run_module_root,
    uv_run_words,
    write_checkpoint,
    write_scope,
    written_targets,
)
from .downloads import read_download
from .fetch import decide_fetch, loopback_port
from .lex import placed_path
from .syntax import expands, verbatim_piece
from .programs import program_verdict, read_program
from .semantics import UnjudgedAmbient

# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
IN_PLACE_SED_REFUSAL = (
    "in-place sed names no file, so what it rewrites cannot be checked"
)
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
IN_PLACE_SED_RECOVERY = (
    "For a rename across many sites, `rename_symbol` resolves scopes an"
    " exact-string substitution cannot tell apart; otherwise make the change"
    " with a file edit, which the edit gates read."
)
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
UNPRODUCED_SED_RECOVERY = (
    "The edit gates judge the file a rewrite would produce, and it could not be"
    " produced here. Make the change with a file edit, which carries its own"
    " content."
)


def row_verdict(
    row: ShellRuleRow,
    effect: DecisionEffect,
    reason: str,
    checkpoint: CheckpointRequirement | None = None,
    effects: list[EffectRow] | None = None,
    arguments: list[str] | None = None,
    asked: KernelDecision | None = None,
    reached: list[EffectRow] | None = None,
) -> KernelDecision:
    """One row's verdict, carrying every fact the row states about itself.

    ``arguments`` are the operand words the row was matched against, and an
    ask or a deny appends the invocation they spell to the reason. A row's
    sentence states the category — "deleting files requires approval" — and
    the reviewer reads the reason and nothing else, so the words that tripped
    the row travel with it: in a compound command they are what says *which*
    segment the question is about. Composed here once rather than templated
    into each of the hundred-odd rows that ask.

    The purpose comes from the effect that decided, because the effect is the
    thing being weighed. Inferred from two other columns -- an ask whose
    checkpoint is anything but ``unrecoverable`` a local mutation, one
    carrying an effect class an external consequence -- it would hold only
    while those columns happen to imply it, and say nothing at all for a row
    setting neither. Fifty-one of the hundred and eight rows that ask are in
    that last group, and would reach a reviewer's queue unclassified.

    Read off the row rather than off resolved paths, and that is exact rather
    than approximate: no member's ``purpose`` consults the evidence, and no row
    in the table changes which member decides it under any reading a host could
    supply. The second half is a property of the table rather than of this
    function, so it is asserted as a test instead of assumed here.

    ``checkpoint`` overrides the row's, for the one verdict that knows better
    than the row does. A row carries one value for every path it might touch,
    which is right until a path is resolved: `sort -o` into the checkout is a
    targeted loss a snapshot answers for, and `sort -o /etc/hosts` is not held
    by any capture this session took.

    ``effects`` overrides the row's for the same kind of reason. A guarded flag
    escalates an operation into a different one -- `git reset` mutates the
    repository reversibly and `git reset --hard` discards working-tree content
    besides -- so the caller that recognized the flag states what the question
    is now about, rather than leaving it read off the operation nobody asked
    about.

    The reach is read off the same effects, and ``reached`` narrows it where
    the question is about something those effects do not describe. A guarded
    flag on a row that declared nothing about the flag is the case: the row's
    effects say what the plain command does, and a `--pre` that runs a program
    is none of it -- so the question carries the reach the flag declared, and
    none where it declared nothing, which keeps it asking inside a container.
    A question another reading already put, ``asked``, carries its own.
    """
    if row["operator_only"]:
        return KernelDecision(
            "deny",
            row["reason"],
            hard=True,
            rule=row["rule"],
            evaluator="shell-vocabulary",
            recovery=row["recovery"],
        )
    settled = row["checkpoint"] if checkpoint is None else checkpoint
    declared = row["effects"] if effects is None else effects
    purpose = purpose_of(declared, EffectEvidence()) if effect == "ask" else None
    if arguments is not None and effect in ("ask", "deny"):
        prefix = [row["command"]] + ([row["subcommand"]] if row["subcommand"] else [])
        reason = f"{reason} — `{' '.join([*prefix, *arguments])}`"
    return KernelDecision(
        effect,
        reason,
        row["sandbox"],
        checkpoint=settled,
        reviewer=row["reviewer"],
        purpose=asked.purpose if asked is not None else purpose,
        rule=row["rule"],
        evaluator="shell-vocabulary",
        recovery=(
            asked.recovery
            if asked is not None
            else row["recovery"]
            if effect in ("ask", "deny")
            else ""
        ),
        reach=(
            asked.reach
            if asked is not None
            else question_reach(declared if reached is None else reached)
        ),
    )


def settles_unguarded(word: str, following: list[str], row: ShellRuleRow) -> bool:
    """Whether a guarded flag names a setting its row declared unremarkable.

    The one reading :func:`split_subcommand` gives a command's globals, offered
    to any row that declares which of its guarded flags carry a setting. Only a
    legible value outside the guarded ones stands the flag down: a value that
    expands at run time, or a spelling that cannot be separated from the flag,
    is one nobody can weigh, and keeps the question.
    """
    if not flag_matches(word, row["setting_flags"]):
        return False
    named = carried_setting(word, row["setting_flags"], following)
    return (
        bool(named["value"])
        and not opaque_argument(named["value"])
        and not key_matches(named["value"], row["guarded_settings"])
    )


def flagged_checkpoint(row: ShellRuleRow) -> CheckpointRequirement | None:
    """What capture puts back a guarded flag's loss, where its effects say none.

    A row carries one checkpoint for its ordinary form, and a guarded flag
    can send the same write somewhere no snapshot of this checkout holds --
    `git apply --unsafe-paths` patches outside it. Read off the effects the
    flag adds, the way :func:`~.words.write_checkpoint` reads a redirection's
    off its target. ``None`` leaves the row's own standing.
    """
    implied = [
        write_checkpoint(effect["scope"])
        if effect["kind"] == "writes_path"
        else effect["scope"]
        for effect in row["flag_effects"]
        if effect["kind"] in ("writes_path", "destroys_uncaptured")
    ]
    return "unrecoverable" if "unrecoverable" in implied else None


class WriteFacts(TypedDict):
    """What the host measured about the paths a command's write flags name.

    The same readings the redirection path takes, in the shape this module
    needs them. Declared here rather than shared with ``ShellContext``
    because that lives above this one: the classifier can be handed facts, and
    cannot reach up for them.
    """

    existing: list[str] | None
    tracked: list[str]
    path_roles: list[PathRoleRow]
    path_rules: list[PathRuleRow]
    contained: bool
    """Whether a measured boundary confines this session.

    A fact about the session rather than about any path, carried here because
    the write row reads it beside the paths: outside the checkout, whether
    anything confines the write is the whole of the answer.
    """

    checkout_root: str
    """Where this repository sits, for reading an absolute path back against.

    The declared roles are anchored at the repository top, so an absolute
    spelling reaches none of them and one file gets two answers depending on
    how a caller happened to name it. A machine's own path rather than
    anything this repository declares, so it crosses from the host per call;
    empty leaves every reading exactly as it was.
    """


class SedContext(TypedDict):
    """Everything judging an in-place rewrite as an edit needs, in this shape.

    Beside :class:`WriteFacts` and for its reason: the edit gates are declared
    above this module, so the classifier is handed what they read rather than
    reaching up for it. What arrives is the declarations an ``Edit`` is judged
    against — unchanged, so the two cannot come to disagree about a file — and
    the documents the host produced by running the rewrite into a copy.
    """

    path_roles: list[PathRoleRow]
    path_rules: list[PathRuleRow]
    antipattern_rows: dict[str, list[AntiPatternRow]]
    """The anti-pattern table by file suffix, as the edit gate reads it."""

    edit_rules: list[EditRuleRow]
    import_boundaries: list[ImportBoundaryRow]
    acceptance_guard: AcceptanceGuardRow | None
    maximum_added_lines: int
    autonomous: bool
    allowances: list[str]
    rewritten_documents: list[RewrittenDocumentRow]
    unproduced_documents: list[UnproducedDocumentRow]
    """What each named file would hold afterwards, where the host could say.

    A list rather than a mapping because it crosses the same boundary every
    other host reading crosses, and one absent row is the fact the classifier
    acts on: a rewrite nothing read is asked about rather than granted.
    """


class WriteAnswer(TypedDict):
    """What one write target earned, beside the scope that decided it.

    ``path`` and ``unread`` travel together for the one verdict whose reason
    is about the file rather than about the row: a write replacing reviewed
    content with content nobody read names that file when it asks.
    """

    effect: DecisionEffect
    scope: str
    path: str
    unread: bool


def no_write_facts() -> WriteFacts:
    """The reading a caller that measured nothing supplies.

    ``existing`` of ``None`` means nothing was established, which every reader
    of it takes as "assume the path is already there" -- so a caller with no
    filesystem behind it lands on the cautious side of the create test rather
    than the permissive one.
    """
    return WriteFacts(
        existing=None,
        tracked=[],
        path_roles=[],
        path_rules=[],
        contained=False,
        checkout_root="",
    )


def flag_write_verdict(
    row: ShellRuleRow, arguments: list[str], facts: WriteFacts
) -> KernelDecision:
    """What the files this row's write flags name earn, taken together.

    The row that every other spelling of a write reaches, reached at last by
    the flag spelling. `sort -o out.txt` lands the same bytes at the same
    path as `sort f > out.txt`, and until this existed the two were answered
    by different machinery -- one by resolving the path, the other by whether
    the row happened to carry a checkpoint some snapshot discharged.

    Unresolved keeps the row's own verdict. A flag whose value is clustered or
    missing names no path, and a write nobody can locate is exactly the case
    the guard was written for; relaxing it because the reading came back empty
    would turn every unreadable spelling into a grant.

    ``reviewed`` is true, which is the whole reason this row can be generous
    about the content. The axis asks whether the route this write takes has
    gates that read what it wrote, and a shell write's route does -- just
    afterwards, because the bytes are produced by running rather than carried
    in the call. So the question here is the one that *can* be answered in
    advance, which is about the path: a protected path asks and a scratch
    tree allows, while what only the result could settle is left to the gates
    that will see the result.
    """
    targets = flag_write_targets([row["command"], *arguments], row["write_flags"])
    if not targets:
        return row_verdict(
            row,
            "ask",
            row["reason"] or "this flag writes a file",
            arguments=arguments,
        )
    return targets_write_verdict(row, targets, arguments, facts)


def targets_write_verdict(
    row: ShellRuleRow, targets: list[str], arguments: list[str], facts: WriteFacts
) -> KernelDecision:
    """What the files one row's command writes earn, taken together.

    The flag spelling's answer, for a caller that found the paths itself: a
    download lands its response at a name the URL chooses, which no write flag
    names, and it is the same write to the same path whichever reader found it.
    """

    def judged(target: str) -> WriteAnswer:
        """What one named path earns, beside the scope that earned it.

        The scope travels with the verdict because the checkpoint is read off
        it: a question about a path inside the checkout is a loss the snapshot
        answers for, and one about a path beyond it is held by nothing.
        """
        known = facts["existing"]
        existing = known is None or target in known
        scope = write_scope(target, facts["path_roles"], facts["checkout_root"])
        # A flag never carries the bytes it writes -- `sort -o` names a file
        # and reads another -- so the carried case is false here rather than
        # unreachable, and the same question the redirection spelling puts is
        # put by this one.
        if unread_over_tracked(scope, False, existing, target in facts["tracked"]):
            return WriteAnswer(effect="ask", scope=scope, path=target, unread=True)
        return WriteAnswer(
            path=target,
            unread=False,
            effect=verdict_for(
                [
                    declare(
                        "writes_path",
                        scope=scope,
                        write="overwrite" if existing else "create",
                        reviewed=True,
                    )
                ],
                EffectEvidence(
                    existing=existing,
                    tracked=target in facts["tracked"],
                    contained=facts["contained"],
                ),
                # Where this session *is*, which is what the write row asks
                # about. The row's own `sandbox` is where a command must run,
                # and reading it here answered a measurement with a
                # requirement -- every row declares `ambient`, so a write
                # outside the checkout asked however well confined it was,
                # while the redirection spelling of the same write allowed.
                "inside" if facts["contained"] else "ambient",
            ),
            scope=scope,
        )

    for target in targets:
        known = facts["existing"]
        protected = protected_write_target(
            [target], facts["path_rules"], known is None or target in known
        )
        if protected is not None:
            return protected
    answers = [judged(target) for target in targets]
    # A path nobody can locate speaks for the line ahead of any other question,
    # because it is the one no reading of a path can settle.
    answered = next(
        (answer for answer in answers if answer["scope"] == "unbounded"),
        max(answers, key=lambda answer: STRENGTH.index(answer["effect"])),
    )
    if answered["effect"] == "allow":
        return row_verdict(row, "allow", "this write lands where nothing is reviewed")
    if answered["unread"] or answered["scope"] == "unbounded":
        # Through the row rather than beside it, so an operator-only row still
        # denies and the sandbox, rule and reviewer the row states still
        # travel; what this verdict knows better is the reason it asks for.
        asked = (
            unread_question(answered["path"])
            if answered["unread"]
            else unlocated_write(f"the write target {answered['path']}")
        )
        return row_verdict(
            row,
            "ask",
            asked.reason,
            write_checkpoint(answered["scope"]),
            arguments=arguments,
            asked=asked,
        )
    return row_verdict(
        row,
        answered["effect"],
        row["reason"] or "this flag writes a file",
        write_checkpoint(answered["scope"]),
        arguments=arguments,
        # Where the file lands, rather than what the plain command does: `sort`
        # reads, and `sort -o` over somebody's file is a write to it.
        reached=[declare("writes_path", scope=answered["scope"])],
    )


def verb_loss_scope(
    words: list[str], facts: WriteFacts, write_flags: Sequence[str] = ()
) -> CheckpointRequirement | None:
    """What a verb's own targets say the loss is, read one target at a time.

    A row carries one value for every path it might touch, and for these verbs
    that value is `boundary_wide`: a glob prevents an exact footprint, so the
    wider capture is what the opacity costs. It is the right reading for a
    delete inside the checkout and a false one the moment a path leaves it --
    ``rm /etc/hosts`` was settled as "the affected paths are captured and
    restorable", said of a file no snapshot of this checkout has ever held, and
    the settlement row that discharges a covered loss took it at its word. A
    variable is the second of those rather than the first: ``rm tmp/$X`` names
    wherever ``$X`` climbs to, so :func:`write_scope` reads it ``unbounded``
    and no capture settles it.

    So the scope is read off the targets the way :func:`write_checkpoint` reads
    it for a redirection, and for exactly that reason: getting it from the row
    is what let a write outside the tree be settled by a capture that never saw
    it. Only the paths the verb *writes* are read, so a source `cp` merely
    reads is an ordinary read however far outside it sits.

    Which verbs those are is :func:`written_targets`' question rather than
    this one's, and the row's declared ``write_flags`` go with the words so a
    command naming its destination in an option is read from the column that
    already states it rather than from a second table saying the same thing.

    It answers for the archives too. They read their targets
    already -- to grant an extraction that lands on nothing -- and where the
    grant did not apply the row's own claim stood: ``gzip /etc/hosts`` and
    ``tar -xf a.tgz -C /etc`` were settled by a capture that has never held
    either path, which is the same defect the delete verbs were fixed for.

    ``None`` leaves the row's own scope standing -- an unmodelled verb, or
    targets that are all inside the checkout, where the row was already right.
    """
    targets = written_targets(words, write_flags)
    if targets is None:
        return None
    if any(
        write_checkpoint(
            write_scope(target, facts["path_roles"], facts["checkout_root"])
        )
        == "unrecoverable"
        for target in targets
    ):
        return "unrecoverable"
    return None


def unresolved_evidence(facts: WriteFacts) -> EffectEvidence:
    """What a row can be judged against before any of its paths are resolved.

    The classifier decides on the words alone, so containment is the only
    measured fact it holds. The other three are stated rather than measured,
    and each is stated at the cautious end:

    ``existing`` assumes the target is already there, which is what
    :func:`no_write_facts` means by a ``None`` reading -- the create grant is
    the permissive branch, and taking it on a path nobody looked at would grant
    every unresolved write.

    ``tracked`` assumes a reviewer would see the file, so the write row keeps
    its refusal rather than losing it to an absent reading.

    ``captured`` assumes nothing holds what this replaces. That is not caution
    standing in for a measurement but the truth at this point: no snapshot has
    been consulted yet. Settlement consults one afterwards and discharges the
    question if a capture exists, which is the arrangement the checkpoint
    column already describes.
    """
    return EffectEvidence(
        contained=facts["contained"], existing=True, tracked=True, captured=False
    )


def frozen_restore(
    arguments: list[str], frozen_flags: list[str], guarded: list[str]
) -> KernelDecision | None:
    """The allow a dependency restore earns when a frozen flag pins it.

    One judgement for every package manager, which is why it is a function
    the row walk and the uv parser both reach rather than a sentence each
    carries: a restore that may not touch the lockfile fetches nothing the
    lock does not already pin by integrity hash, which is what `uv run`
    fetches before running anything, unasked. The flag has to be legible and
    stand among unguarded words, on the terms a read verb is honored — an
    unresolved expansion might be a guarded flag, and a guarded flag beside
    the freeze would still act. Nothing pinned answers ``None``, leaving the
    row's own verdict to stand.
    """
    if not arguments or not frozen_flags:
        return None
    if any(opaque_argument(word) or flag_matches(word, guarded) for word in arguments):
        return None
    if not any(word in frozen_flags for word in arguments):
        return None
    return KernelDecision(
        "allow",
        "a frozen lockfile pins every package to what this project already declares",
    )


def forced_update(row: ShellRuleRow, arguments: list[str]) -> str:
    """Why a forced update this row judges asks, or ``""`` where it does not.

    What a force can discard is the whole question. An unconditional force --
    a force flag, or a refspec's leading plus, which git lets override a lease
    as readily as the flag does -- replaces whatever the remote holds, so it
    can discard commits somebody else pushed. A lease replaces only what this
    checkout last saw there, so on a branch of the caller's own it discards
    nothing anybody else wrote, and that is the one force that allows. On a
    protected branch the rewrite is itself the loss, since other people have
    built on what it replaces; and a push naming no branch reaches the current
    one, which a reader that cannot run git cannot tell apart from those.

    Git applies the last of a lease flag and its `--no-` form, so this reads
    them in order rather than asking whether one appears.
    """
    if not (row["force_flags"] or row["lease_flags"]):
        return ""
    lease = next(iter(row["lease_flags"]), "")
    cancels = [f"--no-{flag.removeprefix('--')}" for flag in row["lease_flags"]]
    unconditional = next(
        (
            word
            for word in arguments
            if flag_matches(word, row["force_flags"])
            or "force" in refspec_effects(word)
        ),
        None,
    )
    if unconditional is not None:
        refusal = f"; {lease} would refuse that" if lease else ""
        return (
            f"{unconditional} overwrites the remote branch whatever it holds,"
            f" discarding commits someone else pushed{refusal}"
        )
    toggles = [
        word
        for word in arguments
        if flag_matches(word, row["lease_flags"]) or word in cancels
    ]
    if not toggles or toggles[-1] in cancels:
        return ""
    refspecs = [
        word
        for word in operand_words(arguments, row["value_flags"])[1:]
        if not word.startswith("^")
    ]
    named = [refspec_destination(word) for word in refspecs]
    if not named or "" in named:
        return (
            "this forced push names no branch, so it rewrites whichever one the"
            " checkout is on; name the branch so a shared one is not rewritten"
        )
    shared = next((ref for ref in named if ref in row["protected_refs"]), None)
    if shared is not None:
        return f"forcing {shared} rewrites a branch other people build on"
    return ""


def unread_argument_readings(
    row: ShellRuleRow, arguments: list[str], guarding: list[str], measured: WriteFacts
) -> tuple[KernelDecision, ...]:
    """Every command an unread argument could make, judged.

    The word could be any flag the row guards, and is read as each in turn
    beside the words that can be read; the others nobody can read are left
    out, so a line of several reads each flag once rather than every
    combination of what each could be. It could as well be an operand, and a
    row that judges its operands -- a destination, a refspec -- asks about
    one on its own effects.
    """
    legible = [word for word in arguments if not opaque_argument(word)]
    flagged = tuple(
        apply_command_row(row, [*legible, flag], measured)
        for flag in dict.fromkeys(guarding)
    )
    if row["ask_destinations"] or row["ask_refspecs"]:
        return (*flagged, row_verdict(row, "ask", row["reason"]))
    return flagged


def apply_command_row(
    row: ShellRuleRow, arguments: list[str], facts: WriteFacts | None = None
) -> KernelDecision:
    """Return a row's effect, downgrading an allow to ask on a guarded flag.

    On a flag-guarded row an unresolved expansion could become the guarded
    flag at runtime, so opaque words deny toward an explicit literal binding.
    A non-allow row with ``allow_flags`` de-escalates only when every
    argument is exactly one of those flags — the command's declared pure
    read-only form. One with ``read_verbs`` de-escalates when a declared
    verb appears and every word is a literal free of guarded flags — the
    verb pins the invocation to its query action. One with ``probe_flags``
    de-escalates on a literal probe flag even beside guarded words, because
    the dry-run form performs none of what they guard — destination grammar
    alone stands, being about where the probe reaches. One with ``frozen_flags``
    de-escalates on a literal frozen flag among unguarded words, because a
    restore that may not touch the lockfile fetches only what it already
    pins. One with ``write_markers``
    states that de-escalation negatively, for a command whose read-only form
    is the one carrying nothing extra: it allows when no legible word carries
    a marker. One with ``bare_reads`` carries that to its limit, for a command
    whose reading form carries no words at all: it allows the empty argument
    list and nothing else. One with ``guarded_keys`` states absence about the
    write's subject instead of its form: it allows when no legible word names
    a setting that decides how later commands execute, so the row keeps its
    effect for ``core.hooksPath`` and lets ``user.email`` past.

    What the row earns before any of that is derived from what it says it
    does, rather than read off a verdict written beside the declaration. The
    two agreed on all 411 rows by the time the column came out, which is what
    made removing it a deletion rather than a change.
    """
    measured = no_write_facts() if facts is None else facts
    stated = declared_verdict(
        row["effects"],
        row["refuses"],
        unresolved_evidence(measured),
        "inside" if measured["contained"] else "ambient",
    )
    if stated != "allow" and row["allow_flags"] and arguments:
        if all(word in row["allow_flags"] for word in arguments):
            return row_verdict(
                row, "allow", "every argument is a declared read-only flag"
            )
    if stated != "allow" and row["guarded_keys"] and arguments:
        # Absence is the test, so every word has to be legible on the same
        # strict bar `write_markers` sets: a word this cannot read might be
        # the guarded key, and "no guarded key found" would otherwise be
        # indistinguishable from "none was readable". `git config --local
        # "$KEY" v` is the shape that has to keep asking.
        #
        # Guarded flags block the de-escalation too, which is what keeps
        # `--file` from turning an allowed write into one aimed at a path of
        # the caller's choosing.
        readable = not any(
            opaque_argument(word)
            or expands(word)
            or flag_matches(word, row["ask_flags"])
            for word in arguments
        )
        if readable and not any(
            key_matches(word, row["guarded_keys"]) for word in arguments
        ):
            return row_verdict(
                row,
                "allow",
                "no setting that redirects how commands execute is named",
            )
    if stated != "allow" and row["read_verbs"] and arguments:
        clean = not any(
            opaque_argument(word) or flag_matches(word, row["ask_flags"])
            for word in arguments
        )
        if clean and any(word in row["read_verbs"] for word in arguments):
            return row_verdict(
                row, "allow", "a declared read-only verb pins the query action"
            )
    if stated != "allow":
        pinned = frozen_restore(arguments, row["frozen_flags"], row["ask_flags"])
        if pinned is not None:
            return row_verdict(row, pinned.effect, pinned.reason)
    # Unlike a read verb, a probe stands beside guarded flags: what they guard
    # is an effect the dry-run form does not perform. Only the literal
    # spelling counts — a cluster (`git clean -fdn`) keeps the row's effect,
    # which costs a question rather than an unperformed loss.
    if stated != "allow" and row["probe_flags"] and arguments:
        if any(word in row["probe_flags"] for word in arguments):
            return row_verdict(
                row, "allow", "a declared dry-run flag makes this a probe"
            )
    # Held to the read verb's bar rather than the probe's: an amendment still
    # performs something, so a guarded flag beside it keeps its question.
    if stated != "allow" and row["amending_flags"] and arguments:
        clean = not any(
            opaque_argument(word) or flag_matches(word, row["ask_flags"])
            for word in arguments
        )
        if clean and any(flag_matches(w, row["amending_flags"]) for w in arguments):
            return row_verdict(
                row, "allow", "a declared flag points this at a record that exists"
            )
    if stated != "allow" and row["write_markers"] and arguments:
        # Absence is the test, so every word has to be legible: one this
        # cannot read might carry the marker, and "no marker found" would
        # otherwise be indistinguishable from "no marker was readable".
        #
        # A stricter bar than `opaque_argument`, deliberately. That catches a
        # `$` opening a word, because what it guards are positive tests where
        # a missed expansion still leaves the required verb absent. Here a
        # missed expansion is the whole verdict, and `dd if=$X` splits into
        # `of=` at runtime if `$X` holds a space — measured allowing until
        # this test replaced it.
        legible = not any(opaque_argument(word) or expands(word) for word in arguments)
        if legible and not any(
            word.startswith(marker)
            for word in arguments
            for marker in row["write_markers"]
        ):
            return row_verdict(
                row,
                "allow",
                "no declared write marker is present, so this only reads",
            )
    # No opacity test here, unlike every de-escalation above. Those read the
    # words to decide, so a word they cannot read is a word that might carry
    # what they are looking for; this one is deciding *on* the absence of
    # words, and a list with nothing in it has nothing to misread.
    if stated != "allow" and row["bare_reads"] and not arguments:
        return row_verdict(row, "allow", "this command's argument-less form only reads")
    # Both lists are read against the same opacity test and reach the same
    # escalation, because an unresolved word could become either. What
    # separates them is what happens next: a write flag names a path, and
    # naming it is what lets the write be judged where every other spelling
    # of a write is judged rather than by this row's single verdict. The force
    # spellings join them for the opacity test alone: they are judged below.
    guarding = [
        *row["ask_flags"],
        *row["write_flags"],
        *row["force_flags"],
        *row["lease_flags"],
    ]
    # A literal probe flag says the invocation performs nothing, so the flag-
    # and refspec-earned questions below stand down. The opacity bounce stays:
    # an unreadable word could name a destination, and destination grammar is
    # about where the probe reaches rather than what it writes.
    probing = any(word in row["probe_flags"] for word in arguments)
    if stated == "allow" and guarding:
        opaque = next(
            (word for word in arguments if opaque_argument(word)),
            None,
        )
        if opaque is not None:
            # Nobody judged it as written, and yet every command it could make
            # was, so a container settles it only where each would stay inside.
            return carrying_readings(
                unjudged(
                    f"argument {opaque!r} could expand into a guarded flag — bind"
                    " it to a literal value first"
                ),
                unread_argument_readings(row, arguments, guarding, measured),
            )
        guarded = next(
            (
                word
                for position, word in enumerate(arguments)
                if not probing
                and flag_matches(word, row["ask_flags"])
                and not settles_unguarded(word, arguments[position + 1 :], row)
            ),
            None,
        )
        if guarded is not None:
            return row_verdict(
                row,
                "ask",
                row["reason"] or f"{guarded} requires approval",
                checkpoint=flagged_checkpoint(row),
                effects=[*row["effects"], *row["flag_effects"]],
                arguments=arguments,
                # What the flag adds is what the flag's effects say, and a flag
                # that declared none was placed by nobody.
                reached=(
                    [*row["effects"], *row["flag_effects"]]
                    if row["flag_effects"]
                    else []
                ),
            )
        # After the ask-flags, so a command carrying both keeps the stronger
        # question: `sort --compress-program=x -o out.txt` runs a program
        # whatever the file it lands turns out to be.
        if any(flag_matches(word, row["write_flags"]) for word in arguments):
            return flag_write_verdict(
                row, arguments, no_write_facts() if facts is None else facts
            )
    if stated == "allow" and row["ask_destinations"]:
        # The first operand, which is where git reads the repository from and
        # nowhere else: every later operand is a refspec. A flag declared as
        # taking a separate value takes it along, so `-o <option>` never reads
        # as the repository.
        #
        # No opacity test, on the same grounds as the block below: a row
        # declaring destination forms declares flag effects too, so an
        # unreadable word has already been bounced above.
        named = next(iter(operand_words(arguments, row["value_flags"])), "")
        form = destination_form(named)
        if form in row["ask_destinations"]:
            return row_verdict(
                row,
                "ask",
                f"{named} sends work to a {form} rather than to a remote this"
                " repository holds",
            )
    if stated == "allow" and row["ask_refspecs"] and not probing:
        # No opacity test of its own: a row declaring refspec effects declares
        # flag effects too, so the block above has already bounced every word
        # this one could not read.
        carried = next(
            (
                (word, effect)
                for word in arguments
                for effect in refspec_effects(word)
                if effect in row["ask_refspecs"]
            ),
            None,
        )
        if carried is not None:
            return row_verdict(
                row,
                "ask",
                row["reason"] or f"{carried[0]} would {carried[1]} a ref",
                arguments=arguments,
            )
    if stated == "allow" and not probing:
        # No opacity test of its own either: the force spellings joined the
        # guarded list above, so an unreadable word has already been bounced.
        forced = forced_update(row, arguments)
        if forced:
            return row_verdict(row, "ask", forced, arguments=arguments)
    # Read after every de-escalation above and before the row's own answer,
    # because it changes what the loss *is* rather than whether the row asks:
    # a scratch grant is still a scratch grant, and a delete reaching outside
    # the checkout is a loss no capture of this session holds.
    loss = verb_loss_scope([row["command"], *arguments], measured, row["write_flags"])
    if loss is not None:
        return row_verdict(
            row,
            stated,
            row["reason"],
            checkpoint=loss,
            effects=[declare("destroys_uncaptured", scope=loss)],
            arguments=arguments,
        )
    return row_verdict(row, stated, row["reason"], arguments=arguments)


class Subcommand(TypedDict):
    """The subcommand word a command line names, and the arguments after it.

    ``word`` is empty when the line carried only global flags, which leaves
    the default row to answer for it. ``asked`` are the questions guarded
    globals before it put, each to be joined with the verdict on the verb
    rather than standing in for it: a question about the global is not the
    answer to a subcommand the vocabulary refuses.
    """

    word: str
    remainder: list[str]
    asked: list[KernelDecision]


def split_subcommand(
    executable: str,
    arguments: list[str],
    default: ShellRuleRow | None,
) -> Subcommand | KernelDecision:
    """Find the subcommand word, honoring global value-taking and guarded flags.

    A guarded global is answered from the command's own row, so the approval it
    raises carries that row's placement: the call still has to run where the
    command declared it runs, and a question that dropped the placement would
    approve one thing and perform another.

    A global that only moves the command to another directory is a value flag
    and nothing more: the parser steps over its argument and the verb behind
    it is judged by its own row, exactly as ``cd there && git <verb>`` is
    judged by two segments. Asking about ``git -C /elsewhere commit`` on the
    ground that the reflog which makes a commit reversible is in the other
    tree grants the ground and misses it -- it is that tree's reflog, which
    commit exactly as this one's would. Every other spelling of the same act
    is allowed, so such a question deters nothing and costs a turn each time.

    A guarded global whose value is a *setting* is judged by the setting, the
    way `git config` is judged by the key it writes. `git -c core.pager=x`
    arranges for a program to run and `git -c color.ui=false diff` turns off
    colour; guarding both on the strength of the first put a question in front
    of the second and told whoever answered it that git config can change how
    commands execute, which is not what the command in front of them did.
    Unreadable keeps its question: a value that expands at run time, or a
    spelling this cannot separate from the flag, is one nobody can weigh.

    A global whose width this cannot read answers for the whole command where
    it stands, since where the subcommand starts is then unknown. Every other
    question is carried on to the verb and joined with that verb's verdict:
    `git -c core.hooksPath=x worktree add` is still the worktree the vocabulary
    refuses, and answering it with the global's question alone softened a
    refusal into an approval. The join keeps what each question reaches --
    `git -c core.pager=less push --force` is a force push, which no container
    holds, as well as a pager, which one does -- and a question placed by
    nobody settles the whole command nowhere.
    """
    ask_flags = default["ask_flags"] if default else []
    setting_flags = default["setting_flags"] if default else []
    asked: list[KernelDecision] = []
    position = 0
    while position < len(arguments):
        word = arguments[position]
        if not word.startswith("-"):
            return Subcommand(
                word=word, remainder=arguments[position + 1 :], asked=asked
            )
        if flag_matches(word, ask_flags) and default is not None:
            following = arguments[position + 1 :]
            named = carried_setting(word, setting_flags, following)
            if settles_unguarded(word, following, default):
                position += global_width(word, following, default)
                continue
            # A setting the row can read, and which only runs a program where
            # the session runs, carries what the flags declared. One that
            # reaches past this machine, one that cannot be read, and a global
            # that is not a setting at all -- `--exec-path` chooses the very
            # helpers a push runs -- were placed by nobody.
            inward = (
                flag_matches(word, setting_flags)
                and bool(named["value"])
                and not opaque_argument(named["value"])
                and not key_matches(named["value"], default["outward_settings"])
            )
            question = global_flag_question(
                default,
                executable,
                word,
                reached=default["flag_effects"] if inward else [],
            )
            if not named["value"]:
                return question
            asked.append(question)
            position += global_width(word, following, default)
            continue
        position += global_width(word, arguments[position + 1 :], default)
    return Subcommand(word="", remainder=[], asked=asked)


def global_width(word: str, following: list[str], default: ShellRuleRow | None) -> int:
    """How many words one global before the verb takes, its value among them.

    Spelled once for both walks that step over a command's globals -- the one
    judging them and the one only finding the verb -- so the two cannot stop
    on different words.
    """
    if default is None:
        return 1
    if flag_matches(word, default["ask_flags"]):
        return carried_setting(word, default["setting_flags"], following)["words"]
    return 2 if word in default["value_flags"] else 1


def verb_word(arguments: list[str], default: ShellRuleRow | None) -> str:
    """The verb a command's words name past its globals, judging none of them.

    Where :func:`split_subcommand` stops at a global no container settles,
    because that global answers for the whole command, this goes on to the
    word it steps over them to: which verb follows is a question about the
    words, and a guarded global in front of it does not change the answer.
    """
    position = 0
    while position < len(arguments):
        word = arguments[position]
        if not word.startswith("-"):
            return word
        position += global_width(word, arguments[position + 1 :], default)
    return ""


def global_flag_question(
    default: ShellRuleRow, executable: str, flag: str, reached: list[EffectRow]
) -> KernelDecision:
    """The question a guarded global puts, whatever verb follows it.

    Asked of the command's own row, for the placement it carries, and asked
    before the verb is judged: the global changes how every verb runs.
    ``reached`` is what the global's setting declared it reaches, and nothing
    where no declaration placed it, which keeps the question inside a
    container too.
    """
    return row_verdict(
        default,
        "ask",
        f"{executable} global flag {flag} changes how the command runs",
        reached=reached,
    )


def declares_command(executable: str, rows: list[ShellRuleRow]) -> bool:
    """Whether the vocabulary says anything at all about this executable.

    Asked of an interpreter, which is otherwise refused outright for having
    an eval mode at all. That blanket refusal denied `bun install` -- a
    package operation carrying no code -- in the vocabulary of inline code,
    which is judging a token rather than an effect.

    A positive test, rather than a list of the flags that mean "inline code".
    Such a list is a denylist carrying a security guarantee, so it has to be
    complete to be worth anything -- and a first draft of one already missed
    ``php -r``, the ``-s`` that takes a program on stdin, the bare ``-`` that
    does the same, and ``deno eval``, which is a subcommand no flag list
    could catch. Asking whether the vocabulary declares the executable fails
    the right way instead: an interpreter nothing declares keeps denying
    entirely, and one a project does declare still denies everything outside
    the forms it named, including spellings nobody thought of.
    """
    return any(row["command"] == executable for row in rows)


class MatchedRow(TypedDict):
    """The row one command's words reach, and the words it is applied to.

    ``arguments`` are what :func:`apply_command_row` reads: everything after
    the command word for a command's own row, and everything after the
    subcommand -- operation words included -- for a row beneath one.
    ``asked`` are the questions the guarded globals before the subcommand put,
    which :func:`beside_globals` joins with the row's verdict.
    """

    row: ShellRuleRow
    arguments: list[str]
    asked: list[KernelDecision]


def decide_command_rows(
    words: list[str], rows: list[ShellRuleRow], facts: WriteFacts | None = None
) -> KernelDecision:
    """Classify a command against the erased vocabulary rows by name and depth.

    ``facts`` are what the host measured about the paths this command's write
    flags name, and they default to nothing measured -- which every reader of
    them takes as the cautious reading rather than the permissive one, so a
    caller with no filesystem behind it loses a relaxation and gains no grant.

    A word nobody can read until the command runs is answered by what it
    could be: :func:`unread_readings` names each command it could stand for,
    and :func:`strictest_reading` keeps the strictest verdict among them where
    that is stricter than the spelling earns as written.
    """
    measured = no_write_facts() if facts is None else facts
    return strictest_reading(
        decide_as_spelled(words, rows, measured),
        unread_readings(words, rows, measured),
    )


def decide_as_spelled(
    words: list[str], rows: list[ShellRuleRow], measured: WriteFacts
) -> KernelDecision:
    """The verdict the rows give a command whose every word is read as spelled.

    A word nobody can read is taken for the spelling it shows here -- an
    unread verb names no row and falls to the default above it -- which is
    why nothing but :func:`decide_command_rows` answers with this alone.
    """
    matched = matched_command_row(words, rows)
    if isinstance(matched, KernelDecision):
        return matched
    return beside_globals(
        apply_command_row(matched["row"], matched["arguments"], measured),
        matched["asked"],
    )


def beside_globals(
    decided: KernelDecision, asked: list[KernelDecision]
) -> KernelDecision:
    """A command's verdict joined with the questions its guarded globals put.

    The verdict leads, because it is about the act, and each global's question
    is one more thing the same answer settles. Joined as a line joins its
    segments, so a container settles the whole only where every part that
    objects stays inside it.
    """
    if not asked:
        return decided
    return joined_decision([decided, *asked])


def matched_command_row(
    words: list[str], rows: list[ShellRuleRow]
) -> MatchedRow | KernelDecision:
    """Walk a command's words to the row that answers for them, by name and depth.

    Separate from the verdict so a reader asking *which* row and *which*
    operands -- where a clone lands, say -- walks the same globals, the same
    subcommand and the same operation path the verdict does, rather than
    guessing them from positions. A walk that ends without a row is the verdict
    that says so.
    """
    executable = posixpath.basename(words[0])
    matches = [row for row in rows if row["command"] == executable]
    if not matches:
        # Unlisted only where the name itself was legible. `$CMD --flag`
        # names nothing a reviewer could weigh: they would be approving
        # whatever the expansion becomes, which is not what they were shown.
        if opaque_argument(executable):
            return unjudged(f"command {executable!r} is not classified")
        return unlisted(f"command {executable!r} is not classified")
    arguments = words[1:]
    if not any(row["subcommand"] for row in matches):
        return MatchedRow(
            row=next(row for row in matches if not row["subcommand"]),
            arguments=arguments,
            asked=[],
        )
    default = next((row for row in matches if not row["subcommand"]), None)
    split = split_subcommand(executable, arguments, default)
    if isinstance(split, KernelDecision):
        return split
    subword = split["word"]
    remainder = split["remainder"]
    # lup: solved: a verb word this walk cannot read -- `$OP`, a `$(...)`
    # result, `set$X` -- matches no operation row and falls to the sub-app's
    # default, as an unread sub-app word falls to the command's: `uv run
    # lup-devtools sync $OP lup /x --mount rw` and `dev questions $(echo
    # answer) <id> --as operator` both reach the target's allow. Decide what an
    # unread verb earns: unjudged still defers inside a sandbox, where either
    # write lands in the checkout, so perhaps the strictest row it could name
    asked = split["asked"]
    subrows = [row for row in matches if subword and row["subcommand"] == subword]
    if not subrows:
        if default is None:
            return unlisted(f"{executable} {subword} is not classified")
        return MatchedRow(row=default, arguments=arguments, asked=asked)
    if any(row["operation"] for row in subrows):
        # The command's globals may stand between a subcommand and its verb --
        # uv takes them anywhere -- so a value one consumes is not an operand:
        # `uv pip --cache-dir list install x` installs, and does not list.
        operands = [
            remainder[at]
            for at in operand_positions(
                remainder, default["value_flags"] if default else []
            )
        ]
        opword = next(iter(operands), "")
        oprows = [
            row
            for row in subrows
            if row["operation_path"]
            and operands[: len(row["operation_path"])] == row["operation_path"]
        ]
        if oprows:
            matched = max(oprows, key=lambda row: len(row["operation_path"]))
            return MatchedRow(row=matched, arguments=remainder, asked=asked)
        subdefault = next((row for row in subrows if not row["operation"]), None)
        if subdefault is not None:
            return MatchedRow(row=subdefault, arguments=remainder, asked=asked)
        return beside_globals(
            unlisted(f"{executable} {subword} {opword} is not classified"), asked
        )
    return MatchedRow(row=subrows[0], arguments=remainder, asked=asked)


def landing_words(matched: MatchedRow) -> list[str]:
    """The words naming where a matched row's operation writes, as its row declares.

    Empty where the row declares no landing. Declared and nothing names one,
    the operation writes where it runs, spelled ``.``. Operands are the literal
    words after the operation path that are neither options nor an option's
    value, up to a ``--`` that hands the rest to another program; the row says
    from which operand on they name a place.
    """
    row = matched["row"]
    if not row["landing_operands"] and not row["landing_flags"]:
        return []
    arguments = matched["arguments"]
    ended = arguments.index("--") if "--" in arguments else len(arguments)
    carried = [
        (
            position,
            carried_setting(word, row["landing_flags"], arguments[position + 1 :]),
        )
        for position, word in enumerate(arguments[:ended])
        if flag_matches(word, row["landing_flags"])
    ]
    consumed = {position + 1 for position, named in carried if named["words"] == 2}
    operands = [
        word
        for position, word in enumerate(arguments[:ended])
        if position not in consumed and not word.startswith("-")
    ][len(row["operation_path"]) :]
    landed = [
        *(operands[row["landing_operands"] - 1 :] if row["landing_operands"] else []),
        *(named["value"] for _position, named in carried if named["value"]),
    ]
    return landed or ["."]


class Reading(TypedDict):
    """One command a word nobody can read could stand for, and its verdict.

    ``word`` is the word as the walk saw it, and ``spelled`` what it was read
    as: the verb path a row names, or the guarded flag it could finish as.
    """

    word: str
    spelled: str
    decision: KernelDecision


def strictest_reading(
    decided: KernelDecision, readings: list[Reading]
) -> KernelDecision:
    """What a command earns when one of its words cannot be read.

    The strictest verdict any command the word could stand for would earn,
    where that is stricter than the spelling earns as written, and the
    spelling's own verdict otherwise -- so a word under a program that guards
    nothing is answered as it always was. An expansion is a choice the
    command makes at run time, and a verdict read off the spelling alone gave
    the permissive default to whichever choice it did not show: `sync $OP`
    reached `sync`'s allow while `sync setup` asks.

    The reason names the word and the command it was read as ahead of that
    command's own reason, because whoever answers is being asked about a
    command they were not shown.

    Whichever verdict is kept carries the reach of every one that objects,
    the spelling's own among them. The verdict names one command, but the word
    could be any of them, and a container settles the question only where the
    harm of each would stay inside it: `codex l$OP` asks as an unknown word
    does, which a container holds, and could be `codex login`, which it does
    not. A deferral kept as the spelling's own verdict carries them the same
    way, marked as standing for what the word could be. So is a reading kept
    in its place, which no capture retires: `$X rm tmp/x` could be the `git
    rm` a capture restores, and could as well be anything else.
    """
    strictest = max(
        readings,
        key=lambda reading: STRENGTH.index(reading["decision"].effect),
        default=None,
    )
    reach = objecting_reach((decided, *(reading["decision"] for reading in readings)))
    if strictest is None or STRENGTH.index(
        strictest["decision"].effect
    ) <= STRENGTH.index(decided.effect):
        if readings and decided.effect in ("ask", "deny"):
            return decided.revised(reach=reach)
        if decided.effect == "defer":
            read = tuple(reading["decision"] for reading in readings)
            return carrying_readings(decided, read)
        return decided
    word = strictest["word"]
    named = (
        "a word a command substitution builds"
        if SUBSTITUTION_SENTINEL in word
        else f"`{word}`"
    )
    return (
        strictest["decision"]
        .revised(
            reason=(
                f"{named} could not be read and could be"
                f" `{strictest['spelled']}`: {strictest['decision'].reason}"
            ),
            reach=reach,
            unread=True,
        )
        .advising(
            "Spell that word out, and the command is judged as the one it is"
            " rather than as the strictest one it could be."
        )
    )


def unread_programs(words: list[str], rows: list[ShellRuleRow]) -> list[str]:
    """The programs a command word nobody can read could be, by the verbs after it.

    Any program at all, as far as the word goes, so the words after it decide:
    one counts where they, walked as its own, name one of its verbs --
    `$CMD push origin feat` could be `git push`, and `$CMD pr merge 12` could
    be `gh pr merge`. The walk steps over that program's own globals, a
    guarded one among them, so `$CMD -c core.sshCommand=x push` is still read
    as `git`, and the reading judges the global too. A legible part narrows
    it to the names it could finish.
    Where the words name no program's verb -- `$EDITOR file`, `"$PYTHON" x.py`
    -- nothing is read in, and the word keeps the abstention it had.
    """
    prefix = unread_prefix(verbatim_piece(words[0], posixpath.basename(words[0])))
    if prefix is None:
        return []

    def names_verb(program: str) -> bool:
        matches = [row for row in rows if row["command"] == program]
        default = next((row for row in matches if not row["subcommand"]), None)
        verb = verb_word(words[1:], default)
        return any(row["subcommand"] and row["subcommand"] == verb for row in matches)

    programs = dict.fromkeys(
        row["command"]
        for row in rows
        if row["subcommand"] and row["command"].startswith(prefix)
    )
    return [program for program in programs if names_verb(program)]


def unread_readings(
    words: list[str], rows: list[ShellRuleRow], measured: WriteFacts
) -> list[Reading]:
    """Every command a word the walk cannot read could stand for, judged.

    Three kinds of word decide which row speaks, and an expansion in any of
    them leaves that choice to run time. A verb word -- the subcommand, or
    an operation beneath it -- could be any row whose name its legible part
    begins, among the rows the words before it still leave, and is read as
    each with that row's whole path in its place, since a result that splits
    can supply every word of one. A global could be each guarded global its
    legible part begins, and is asked about as that global is. A flag after
    the verb could be each guarded flag its legible part begins, and is read
    as that flag moved to the end, where it takes no following word for a
    value and so names no path a write could be judged by.

    A verb reading is the whole walk over the command it spells, so a second
    unread word is read by this same rule inside it. A flag reading is the
    walk as spelled, the other words read as written: one unread word at a
    time keeps a line of several from multiplying into every combination of
    what each could be, and what a probe flag or a frozen lock beside it
    relaxes is still relaxed as it is for the flag written out.
    """
    executable = posixpath.basename(words[0])
    matches = [row for row in rows if row["command"] == executable]
    default = next((row for row in matches if not row["subcommand"]), None)
    guarded = list(
        dict.fromkeys(
            flag for row in matches for flag in [*row["ask_flags"], *row["write_flags"]]
        )
    )
    gated = any(row["subcommand"] for row in matches)
    split = (
        split_subcommand(executable, words[1:], default)
        if gated
        else Subcommand(word="", remainder=words[1:], asked=[])
    )
    if isinstance(split, KernelDecision):
        # A guarded global whose width cannot be read already asks, whatever
        # the verb and flags after it.
        return []
    # Where the verb stands: every word before it is a global, and every word
    # after it the verb's own. Nothing is global to a command without verbs,
    # and everything is to a line of globals alone.
    verb = (
        len(words) - len(split["remainder"]) - 1
        if split["word"] or not gated
        else len(words)
    )
    readings = [
        *[
            Reading(
                word=word,
                spelled=flag,
                # A setting nobody can read was placed by nobody.
                decision=global_flag_question(default, executable, flag, reached=[]),
            )
            for word in words[1:verb]
            if default is not None
            for flag in unread_flags(word, default["ask_flags"])
        ],
        *[
            Reading(
                word=word,
                spelled=flag,
                decision=decide_as_spelled(
                    [*words[:index], *words[index + 1 :], flag], rows, measured
                ),
            )
            for index, word in enumerate(words)
            if index > verb
            for flag in unread_flags(word, guarded)
        ],
    ]
    if not split["word"]:
        return readings
    prefix = unread_prefix(split["word"])
    if prefix is not None:
        paths = {
            " ".join(path): path
            for path in (
                [row["subcommand"], *row["operation_path"]]
                for row in matches
                if row["subcommand"] and row["subcommand"].startswith(prefix)
            )
        }
        return [
            *readings,
            *[
                Reading(
                    word=split["word"],
                    spelled=f"{executable} {spelled}",
                    decision=decide_command_rows(
                        [*words[:verb], *path, *split["remainder"]], rows, measured
                    ),
                )
                for spelled, path in paths.items()
            ],
        ]
    positions = [
        verb + 1 + index
        for index, word in enumerate(split["remainder"])
        if not word.startswith("-")
    ]
    operands = [words[position] for position in positions]
    first = next(
        (
            index
            for index, word in enumerate(operands)
            if unread_prefix(word) is not None
        ),
        None,
    )
    if first is None:
        return readings
    begun = unread_prefix(operands[first]) or ""
    at = positions[first]
    paths = {
        " ".join(row["operation_path"]): row["operation_path"]
        for row in matches
        if row["subcommand"] == split["word"]
        and len(row["operation_path"]) > first
        and row["operation_path"][:first] == operands[:first]
        and row["operation_path"][first].startswith(begun)
    }
    return [
        *readings,
        *[
            Reading(
                word=operands[first],
                spelled=f"{executable} {split['word']} {spelled}",
                decision=decide_command_rows(
                    [*words[:at], *path[first:], *words[at + 1 :]], rows, measured
                ),
            )
            for spelled, path in paths.items()
        ],
    ]


def decide_sed_words(words: list[str], context: "SedContext") -> KernelDecision:
    """Allow read-only sed; judge an in-place rewrite as the edit it performs.

    ``--sandbox`` makes sed itself reject the write and execute commands, so
    the script screen is skipped under it; a script file stays denied toward
    an inline script, because nothing screens what is in it.

    In-place editing was two objections wearing one refusal, and only one of
    them was ever answered. *Being wrong is unrepairable* is answered by the
    files themselves, and a rewrite whose every named file could be brought
    back was allowed on that ground alone. *It walks past the gates an edit
    is judged by* was answered by nothing — so the grant it produced was a
    grant to bypass the anti-pattern table, the review-note gate and the size
    gate, given on the strength of an undo that answers a different question.

    So the after-document is judged instead. The host runs the screened
    script over a copy of each named file and hands back what would land;
    each goes to :func:`~lup.policy.kernel.edit.decide_edit`, the same gate an
    ``Edit`` meets, and the strongest verdict is this command's. A rewrite
    that introduces nothing those rules refuse allows because it is a
    permissible edit, not because it could be undone.

    **A target with no document asks.** A word that expands at run time, a
    file the host could not read, a script sed itself refused — each leaves
    a rewrite about to happen that nothing has read, and an unjudgeable
    rewrite is exactly the one that must not go through unasked. Composition
    paths that forget to resolve the documents therefore ask rather than
    allow, which is the only arrangement in which forgetting is safe.
    """
    invocation = sed_invocation(words)
    if isinstance(invocation, KernelDecision):
        return invocation
    if not invocation["screened"]:
        return unjudged("sed script is not classified as read-only")
    if not invocation["in_place"]:
        return KernelDecision("allow", "read-only sed script")
    if not invocation["targets"]:
        return KernelDecision(
            "deny", IN_PLACE_SED_REFUSAL, recovery=IN_PLACE_SED_RECOVERY
        )
    documents = {row["target"]: row for row in context["rewritten_documents"]}
    unread: dict[str, UnproducedCause] = {
        row["target"]: row["cause"] for row in context["unproduced_documents"]
    }
    verdicts = [
        rewrite_verdict(target, documents[target], context)
        if target in documents
        else unproduced_verdict(target, unread.get(target))
        for target in invocation["targets"]
    ]
    stopped = [verdict for verdict in verdicts if verdict.effect != "allow"]
    if not stopped:
        return KernelDecision(
            "allow",
            "every file this rewrites was judged as the edit it performs, and"
            " the edit gates passed it",
        )
    return max(stopped, key=lambda verdict: STRENGTH.index(verdict.effect))


def unproduced_verdict(target: str, cause: UnproducedCause | None) -> KernelDecision:
    """What to say about a file an in-place rewrite names and nothing produced.

    Four causes and a fifth silence, each sending the writer somewhere else.
    One sentence standing for all five tells a mistyped path, a rewrite aimed
    at a directory and a script sed will not run the same thing, and offers
    the one recovery that fits none of them -- make the change as an edit
    instead, which answers only the case where the document exists and could
    not be judged.

    ``None`` is the silence, and the general sentence is true of it: nothing
    read the rewrite. That is what a composition reaching the documents late,
    or not at all, leaves behind, and it is a question on that ground.
    """
    match cause:
        case "missing":
            return KernelDecision(
                "ask",
                f"sed would rewrite {target} in place, and no file stands there",
                purpose="quality_review",
                recovery=(
                    "Check the path, and the directory the command runs in: a"
                    " relative operand after a `cd` resolves from where the"
                    " `cd` left the shell."
                ),
            )
        case "irregular":
            return KernelDecision(
                "ask",
                f"sed would rewrite {target} in place, and that is not a regular file",
                purpose="quality_review",
                recovery=(
                    "`-i` replaces the file with the script's output, so a"
                    " directory or a device is not something it can rewrite."
                    " Name the file itself, or drop `-i` to print instead."
                ),
            )
        case "refused":
            return KernelDecision(
                "ask",
                f"sed would rewrite {target} in place, and sed itself would not"
                " run the script over it",
                purpose="quality_review",
                recovery=(
                    "Run the same script without `-i` to see what sed says"
                    " about it; nothing is judged until it runs."
                ),
            )
        case "unreadable":
            return KernelDecision(
                "ask",
                f"sed would rewrite {target} in place, and neither what stands"
                " there nor what would replace it reads as text",
                purpose="quality_review",
                recovery=UNPRODUCED_SED_RECOVERY,
            )
    return KernelDecision(
        "ask",
        f"sed would rewrite {target} in place, and nothing read what it would"
        " leave behind",
        purpose="quality_review",
        recovery=UNPRODUCED_SED_RECOVERY,
    )


def rewrite_verdict(
    target: str, document: RewrittenDocumentRow, context: "SedContext"
) -> KernelDecision:
    """What the edit gates say about one file an in-place rewrite would produce.

    The path the rules match on is the document's, not the word's: a rule
    anchored at the repository top has to be asked about where the file sits,
    and the word is spelled relative to wherever the session was launched.
    The word is still what the reason names, because that is what the writer
    typed and what they would have to change.
    """
    if "decision" in document:
        return document["decision"]
    suffix = posixpath.splitext(document["path"])[1].lower()
    verdict = decide_edit(
        document["path"],
        document["before"],
        document["after"],
        path_exists=True,
        path_rules=context["path_rules"],
        antipattern_rows=context["antipattern_rows"].get(suffix, []),
        path_roles=context["path_roles"],
        maximum_added_lines=context["maximum_added_lines"],
        autonomous=context["autonomous"],
        allowances=context["allowances"],
        python_source=suffix in (".py", ".pyi"),
        acceptance_guard=context["acceptance_guard"],
        resolution=document["resolution"],
        suffix=suffix,
        operation="modify",
        edit_rules=context["edit_rules"],
        foreign=document["foreign"],
        outside_project=document["outside_project"],
        checkout_path=document["checkout_path"],
        import_boundaries=context["import_boundaries"],
    )
    if verdict.effect == "allow":
        return verdict
    return KernelDecision(
        verdict.effect,
        f"sed would rewrite {target} in place: {verdict.reason}",
        recovery=verdict.recovery,
        checkpoint=verdict.checkpoint,
        purpose=verdict.purpose,
        rule=verdict.rule,
        reviewer=verdict.reviewer,
    )


def safe_awk_program(program: str) -> bool:
    """Accept only awk programs with no exec, command input, or write path.

    ``system`` and ``getline`` reach commands and files, ``@`` covers gawk's
    include/load directives and indirect calls, and a pipe that is not ``||``
    feeds or reads a command. A bare ``>`` (not ``>=``) only writes when the
    program can also ``print``; without a print there is no write path, so
    comparison-only programs like ``$3 > 5`` stay read-only.
    """
    if any(token in program for token in ("system", "getline", "@")):
        return False
    if re.search(r"(?<!\|)\|(?!\|)", program) is not None:
        return False
    redirects = re.search(r">(?!=)", program) is not None
    return not (redirects and "print" in program)


def decide_awk_words(words: list[str]) -> KernelDecision:
    """Allow only read-only awk: separator and variable flags plus a safe program."""
    positional: list[str] = []
    value_expected = False
    options_ended = False
    for word in words[1:]:
        if value_expected:
            value_expected = False
            continue
        if options_ended or not word.startswith("-") or word == "-":
            positional.append(word)
            options_ended = True
            continue
        if word == "--":
            options_ended = True
            continue
        if word in ("-F", "-v"):
            value_expected = True
            continue
        if word.startswith(("-F", "-v")) and not word.startswith("--"):
            continue
        return unjudged(f"awk option {word!r} is not classified")
    if value_expected:
        return unjudged("awk option flag has no value")
    if not positional:
        return unjudged("awk has no program")
    if not safe_awk_program(positional[0]):
        return unjudged("awk program is not classified as read-only")
    return KernelDecision("allow", "read-only awk program")


# lup: ignore[library-default] — gh's own value-taking flags; misreading one shifts the argument scan
GH_API_VALUE_FLAGS = (
    "-H",
    "--header",
    "-q",
    "--jq",
    "-t",
    "--template",
    "--cache",
    "--hostname",
    "-p",
    "--preview",
)
# lup: ignore[library-default] — gh's own flags that send a request body, which is what makes a call a write however it is spelled
GH_API_BODY_FLAGS = (
    "-f",
    "--raw-field",
    "-F",
    "--field",
    "--input",
)
GH_API_READ_METHODS = ("GET", "HEAD")
"""The HTTP methods that do not change state, fixed by the protocol.

The same pair the downloader screen reads, where it arrives as a default."""


class GhApiRoute(TypedDict):
    """One REST route a ``gh api`` write is judged by, beyond its method.

    ``path`` is the route below ``repos/<owner>/<repo>``, a segment per entry:
    ``*`` stands for any one segment, and a trailing ``**`` for the rest of the
    path however many segments it has -- a branch name can carry slashes.

    An ``allow`` holds only where the endpoint names this checkout's own
    repository through gh's ``{owner}/{repo}`` placeholders, because a literal
    owner and name can be anybody's: the same line ``gh pr create --repo``
    draws. An ``ask`` holds wherever the route points, and is here for the
    reason it gives, which says more than the method does.
    """

    methods: list[str]
    path: list[str]
    act: str
    effect: DecisionEffect
    reason: str


GH_API_ROUTES: tuple[GhApiRoute, ...] = (
    GhApiRoute(
        methods=["POST"],
        path=["pulls"],
        act="opening a pull request",
        effect="allow",
        reason="gh api opening a pull request is `gh pr create` by another name",
    ),
    GhApiRoute(
        methods=["PATCH"],
        path=["pulls", "*"],
        act="editing a pull request",
        effect="allow",
        reason="gh api editing a pull request is `gh pr edit` by another name",
    ),
    GhApiRoute(
        methods=["POST", "DELETE"],
        path=["pulls", "*", "requested_reviewers"],
        act="changing who reviews a pull request",
        effect="allow",
        reason="gh api changing a pull request's reviewers is `gh pr edit`"
        " by another name",
    ),
    GhApiRoute(
        methods=["PUT"],
        path=["pulls", "*", "merge"],
        act="merging a pull request",
        effect="allow",
        reason="gh api merging a pull request is `gh pr merge` by another name",
    ),
    GhApiRoute(
        methods=["POST"],
        path=["merges"],
        act="merging one branch into another",
        effect="allow",
        reason="gh api merging one branch into another is `git merge` on the forge",
    ),
    GhApiRoute(
        methods=["POST"],
        path=["issues"],
        act="filing an issue",
        effect="ask",
        reason="filing an issue publishes a report the repository's watchers"
        " are notified of",
    ),
    GhApiRoute(
        methods=["DELETE"],
        path=["git", "refs", "heads", "**"],
        act="deleting a remote branch",
        effect="ask",
        reason="deleting a remote branch loses work no later push restores",
    ),
)
"""The routes a write through ``gh api`` is judged by, as ``gh`` judges them.

The pull-request and merge routes allow because the typed verbs reaching them
do: an endpoint is one more spelling of opening, editing or merging a request,
and a verdict that changed with the spelling would be two policies. Filing an
issue and deleting a branch ask for what they are, rather than for the method
that happens to reach them. A project whose forge access differs passes its
own routes.
"""


def gh_api_route(
    endpoint: str, method: str, routes: tuple[GhApiRoute, ...]
) -> KernelDecision | None:
    """What the declared routes say about one write, or ``None`` where none match.

    gh accepts the endpoint with or without a leading slash, and a query
    string names no route, so both are set aside before the segments are
    compared.
    """
    segments = endpoint.removeprefix("/").partition("?")[0].split("/")
    if len(segments) < 4 or segments[0] != "repos":
        return None
    owner, repository, below = segments[1], segments[2], segments[3:]
    own = owner in ("{owner}", ":owner") and repository in ("{repo}", ":repo")
    for route in routes:
        pattern = route["path"]
        spread = pattern[-1:] == ["**"]
        fixed = pattern[:-1] if spread else pattern
        shaped = (
            len(below) > len(fixed) if spread else len(below) == len(fixed)
        ) and all(
            expected in ("*", actual)
            for expected, actual in zip(fixed, below, strict=False)
        )
        if not shaped or method not in route["methods"]:
            continue
        if route["effect"] == "allow" and not own:
            return KernelDecision(
                "ask",
                f"{route['act']} in {owner}/{repository} reaches a repository"
                " nobody declared; `{owner}/{repo}` names this checkout's own",
                purpose="external_consequence",
                rule="shell:gh.api",
                evaluator="gh-api-screen",
            )
        return KernelDecision(
            route["effect"],
            route["reason"],
            purpose="external_consequence" if route["effect"] == "ask" else None,
            rule="shell:gh.api",
            evaluator="gh-api-screen",
        )
    return None


def decide_gh_api_words(
    words: list[str], routes: tuple[GhApiRoute, ...] = GH_API_ROUTES
) -> KernelDecision:
    """Allow read-method ``gh api`` calls, and writes to a declared route.

    ``gh api`` is the read path for everything the typed ``gh`` subcommands
    cannot express, so a blanket ask on it asks about the ordinary case. What
    separates a read from a write here is the same thing that separates them
    in curl: the method, plus whether a body is being sent. A field flag
    implies POST even with no ``-X``, which is why it decides on its own
    rather than only informing the method.

    A write then meets ``routes``, which judge it as the typed verb reaching
    the same route would be judged. Only a call this reads whole reaches
    them: a word it cannot classify keeps the write's own question, and a
    call aimed at another host (``--hostname``) keeps it too.
    """
    method = ""
    body = False
    elsewhere = False
    endpoints: list[str] = []
    expect_value = False
    expect_method = False

    def writing(unread: str) -> KernelDecision:
        """The body's question where one was sent, else the unread word's.

        The unread word could be a method or a body as well as anything
        else, so what it stands for reaches the remote: a container holds
        none of that, and settles none of it.
        """
        if body:
            return KernelDecision(
                "ask",
                "gh api sending a request body can change remote state",
                purpose="external_consequence",
                rule="shell:gh.api",
                evaluator="gh-api-screen",
            )
        return unjudged(unread).revised(unread=True, reach="host_later")

    for word in words[2:]:
        if expect_value:
            expect_value = False
            continue
        if expect_method:
            method = word
            expect_method = False
            continue
        if word in ("-X", "--method"):
            expect_method = True
            continue
        if word.startswith("--method="):
            method = word.partition("=")[2]
            continue
        # gh reads a short flag's value where it is attached, `=` or not, so
        # `-XDELETE` and `-X=DELETE` are the method `-X DELETE` spells apart.
        shorthand, attached = word[:2], word[2:].removeprefix("=")
        if word.startswith("-") and not word.startswith("--") and attached:
            if shorthand == "-X":
                method = attached
                continue
            if shorthand in GH_API_BODY_FLAGS:
                body = True
                continue
            if shorthand in GH_API_VALUE_FLAGS:
                continue
        if word in GH_API_BODY_FLAGS or word.partition("=")[0] in GH_API_BODY_FLAGS:
            body = True
            expect_value = word in GH_API_BODY_FLAGS
            continue
        if word in GH_API_VALUE_FLAGS or word.partition("=")[0] in GH_API_VALUE_FLAGS:
            elsewhere = elsewhere or word.partition("=")[0] == "--hostname"
            expect_value = word in GH_API_VALUE_FLAGS
            continue
        if word.startswith("-"):
            return writing(f"gh api option {word!r} is not classified")
        if opaque_argument(word):
            return writing(
                "a gh api endpoint that expands at run time is not classified"
            )
        endpoints.append(word)
    if expect_value or expect_method:
        return writing("gh api option has no value")
    stated = method.upper() or ("POST" if body else "GET")
    if not body and stated in GH_API_READ_METHODS:
        return KernelDecision(
            "allow",
            "read-only gh api call",
            rule="shell:gh.api",
            evaluator="gh-api-screen",
        )
    routed = (
        gh_api_route(endpoints[0], stated, routes)
        if len(endpoints) == 1 and not elsewhere
        else None
    )
    if routed is not None:
        return routed
    if body:
        return writing("")
    return KernelDecision(
        "ask",
        f"gh api {method} can change remote state",
        purpose="external_consequence",
        rule="shell:gh.api",
        evaluator="gh-api-screen",
    )


def decide_gh_words(
    words: list[str], rows: list[ShellRuleRow], facts: WriteFacts | None = None
) -> KernelDecision:
    """Judge a gh command whose subcommand and operation stand where it wrote them.

    gh finds its subcommand the way cobra does, and cobra does not step over a
    flag the way this walk does: one written before a subcommand, without an
    ``=`` and not a switch that level knows, takes the next word as its value,
    and every flag written there is handed on to the subcommand it reaches.
    So `gh -t status api -X DELETE x` runs `api` with `--template status`,
    `gh -Xpost api x` sends a POST, and `gh pr -t view merge 1` merges --
    each read here as the `gh status`, `gh api` or `gh pr view` it spells.

    Refused rather than modelled. Which word cobra takes as the subcommand
    turns on which flags each level of gh knows, and reading that would be a
    second copy of gh's own flag tables to keep in step with every release.
    A flag before gh's subcommand, or before the operation of a subcommand
    that has operations, is a spelling nobody needs: the same command with
    its flags after the operation is read by the rows that declare them, and
    a help probe is answered before this is asked.

    Past that, ``words[1]`` is the subcommand gh runs, which is what lets `gh
    api` be screened by its method and body here.
    """
    subcommand = words[1:2]
    grouped = bool(subcommand) and any(
        row["command"] == "gh"
        and row["subcommand"] == subcommand[0]
        and row["operation"]
        for row in rows
    )
    leading = next(
        (word for word in words[1 : 3 if grouped else 2] if word.startswith("-")),
        None,
    )
    if leading is not None:
        return KernelDecision(
            "deny",
            f"gh hands `{leading}`, written before its subcommand, to whichever"
            " subcommand it reaches, and a flag there without `=` takes the next"
            " word as its value, so which subcommand runs is not read here",
            recovery="Write gh's flags after its subcommand and operation:"
            " `gh pr merge 1 --repo owner/repo`, `gh api -X GET <endpoint>`.",
        )
    if subcommand == ["api"]:
        return decide_gh_api_words(words)
    return decide_command_rows(words, rows, facts)


def curl_url(word: str) -> str:
    """Spell one downloader operand the way curl and wget resolve it.

    Both accept a URL with no ``scheme://`` and guess one, defaulting to HTTP
    — which is how a liveness probe is actually typed, and how curl's own
    manual documents it. Reading the bare form as malformed put an approval
    question on ``curl localhost:8000/health`` while the identical request
    spelled in full was already declared safe.

    Guessing HTTP where the tools guess HTTP keeps the verdict conservative on
    its own terms: a scope declared for ``https`` alone does not match the
    guess, so an origin reachable only over TLS still asks rather than
    inheriting a grant its scheme never gave.
    """
    return word if "://" in word else f"http://{word}"


def decide_download_words(
    words: list[str],
    allowed_scopes: list[UrlScopeRow],
    denied_scopes: list[UrlScopeRow],
    unscoped: UnjudgedAmbient,
    rows: list[ShellRuleRow],
    facts: WriteFacts,
    directory: str | None = "",
    host_ports: Sequence[int] = (),
    read_methods: tuple[str, ...] = GH_API_READ_METHODS,
) -> KernelDecision:
    """Judge one `curl` or `wget` by what it reads, sends, and writes.

    Three answers, joined strongest first. Every URL is the fetch policy's:
    a denied origin denies, a declared one allows, and one no scope names is
    the fetch declaration's to answer -- the one `WebFetch` reads, so one
    spelling of reaching an undeclared origin cannot answer differently from
    another. A request body, or a method beyond the read pair, asks: it can
    change state on the far end. Every file the response lands at is a write
    to that path, judged by the tool's row the way any other written path is,
    so a download into scratch is ordinary and one over a reviewed file asks.

    A redirect `-L` follows is not re-judged: the scope answers for the origin
    the command names, and the network boundary for where it is sent next.
    ``host_ports`` are the loopback ports a process outside this session's
    container listens on, and a URL reaching one asks as `WebFetch` asks.

    A `defer` returned from here is settled by `ProviderNative`, which is read
    before the rule that would otherwise allow unjudged work inside a
    boundary. That ordering is what makes this safe to thread: the contained
    reading never sees it, and it must not, because its argument is that every
    effect is confined there -- true of a command's writes and false of a
    document entering the agent's context.
    """
    tool = posixpath.basename(words[0])
    reading = read_download(words)
    if reading["unread"]:
        return unjudged(f"{tool} option {reading['unread']!r} is not classified")
    if not reading["urls"]:
        return unjudged(f"{tool} has no URL")
    row = next(
        (row for row in rows if row["command"] == tool and not row["subcommand"]),
        None,
    )
    found = [
        decide_fetch(
            curl_url(url),
            allowed_scopes,
            denied_scopes,
            unscoped,
            host_listener=loopback_port(curl_url(url)) in host_ports,
        )
        for url in reading["urls"]
    ]
    method = reading["method"].upper() or "GET"
    if reading["sends"] or method not in read_methods:
        asked = (
            f"{tool} {reading['sends']} sends a request body, which can change"
            " remote state"
            if reading["sends"]
            else f"{tool} {method} can change remote state"
        )
        found.append(
            KernelDecision("ask", asked, purpose="external_consequence")
            if row is None
            else row_verdict(
                row,
                "ask",
                asked,
                effects=[declare("external_mutation", scope="upload")],
                arguments=words[1:],
            )
        )
    placed = [placed_path(target, directory) for target in reading["targets"]]
    written = [target for target in placed if target is not None]
    if placed:
        found.append(
            unjudged(f"{tool} writes a file this policy cannot place")
            if row is None or len(written) < len(placed)
            else targets_write_verdict(row, written, words[1:], facts)
        )
    for effect in ("deny", "ask", "defer"):
        held = next((verdict for verdict in found if verdict.effect == effect), None)
        if held is not None:
            return held
    return KernelDecision(
        "allow",
        f"{tool} reads within the declared fetch scopes"
        + (", landing where nothing is reviewed" if placed else ""),
    )


UV_FOREIGN_SOURCE_FLAGS = (
    "--index",
    "--index-url",
    "--extra-index-url",
    "--default-index",
    "--find-links",
    "-f",
    "--no-build-isolation",
    "--no-build-isolation-package",
    "--no-sources",
    "--no-sources-package",
)
"""Where a uv invocation stops obeying what this project declared.

The spellings are uv's; which of them count is a judgement, so this is the
default a caller overrides rather than a table anybody has to fork. A project
that pins its own index, or that has a reason to build without isolation,
says so by naming a different set here.
"""

UV_FROZEN_FLAGS = ("--frozen", "--locked")
"""The spellings under which `uv sync` may not touch the lockfile.

Both restore exactly what the lock pins — `--frozen` without reading the
manifest again, `--locked` refusing where the lock is behind it — which is
the restore `uv run` performs before running anything. The spellings are
uv's; that they answer the install question is the judgement
:func:`frozen_restore` states, so a project reading the flags differently
names a different set here.
"""


UV_ADD_VALUE_FLAGS = (
    "--package",
    "--group",
    "--optional",
    "--extra",
    "--index",
    "--default-index",
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "-f",
    "--branch",
    "--tag",
    "--rev",
    "--python",
    "-p",
    "--constraint",
    "-c",
    "--bounds",
    "--script",
    "--project",
    "--directory",
    "--marker",
    "-m",
)
"""The `uv add` options whose next word is a value rather than a package.

uv's own spellings: what a question names as installed is every other operand,
so an option that takes a value has to be stepped over or its value is read as
a package. A project spelling more of them names a longer set here.
"""


def uv_add_operands(
    arguments: list[str], value_flags: tuple[str, ...] = UV_ADD_VALUE_FLAGS
) -> list[str]:
    """The packages a `uv add` names, which is what its question is about."""

    def operands():
        expecting = False
        for word in arguments:
            if expecting:
                expecting = False
                continue
            expecting = word in value_flags
            if not expecting and not word.startswith("-"):
                yield word

    return list(operands())


def uv_package_source(
    arguments: list[str], guarded: tuple[str, ...] = UV_FOREIGN_SOURCE_FLAGS
) -> str | None:
    """The flag naming where packages come from, when one is present.

    A verb obeying the project's own declaration is only obeying it while
    nothing on the command line redirects where packages come from or removes
    the isolation their build code runs in. Either makes it a different act
    wearing the same verb, so it is named back rather than folded into an
    allow.

    An unreadable word answers too. What is being tested is the absence of a
    flag, and a word this cannot read might be one, so the conservative
    direction is to treat it as though it were.
    """
    for word in arguments:
        if opaque_argument(word) or expands(word):
            return word
        if flag_matches(word, list(guarded)):
            return word
    return None


def declared_target_decision(
    declared: RunnerTargetRow,
    run_words: list[str],
    target_tables: list[ShellRuleRow] | None,
    measured: WriteFacts,
) -> KernelDecision:
    """What one declared ``uv run`` target earns, from what it says it does.

    Two spellings reach a declared target — the executable itself, and the
    root package a ``-m`` names — and they are the same declaration, so they
    read it here rather than each deriving a verdict of its own.

    A target carrying a verb table is judged by that table, which states what
    each of its operations does. Otherwise the row's own effects answer,
    against the placement the host measured.
    """
    tabled = [
        row for row in (target_tables or []) if row["command"] == declared["name"]
    ]
    if tabled:
        return decide_command_rows(run_words, tabled, measured)
    evidence = unresolved_evidence(measured)
    placement: SandboxPlacement = "inside" if measured["contained"] else "ambient"
    stated = declared_verdict(
        declared["effects"], declared["refuses"], evidence, placement
    )
    # Only a question carries one, and the refusal is why this asks about the
    # verdict rather than about the effects alone: a refused spelling denies
    # whatever its effects would have earned, and a purpose on a deny names a
    # decision nobody is being asked to make.
    return KernelDecision(
        stated,
        declared["reason"],
        declared["sandbox"],
        purpose=(
            purpose_of(declared["effects"], evidence, placement)
            if stated == "ask"
            else None
        ),
        recovery=declared["recovery"] if stated in ("ask", "deny") else "",
    )


def decide_tool_run(spelled: str, arguments: list[str]) -> KernelDecision | None:
    """Refuse a tool run handed an interpreter, or one whose tool is unread.

    `uvx` and `uv tool run` fetch a tool and run it, and where the tool is an
    interpreter what it runs is whatever follows -- inline code as often as
    not, which leaves nothing behind to review. The tool is the first operand
    past the options, read by their own grammar: read as the word after the
    command, `uvx --from foo python -c 1` and `uvx -q python -c 1` handed the
    interpreter over unseen while `uvx python -c 1` was refused. A version
    pinned onto the name (`python@3.12`) names the interpreter still. An
    option the grammar does not list could take the next word, so it leaves
    the tool unread and refuses rather than guessing.

    ``None`` where neither holds, which leaves the row's question standing.
    """
    reading = read_program(["uvx", *arguments], {"uvx": UV_TOOL_RUN_GRAMMAR})
    subject = reading["subject"]
    if reading["kind"] == "unread":
        unread = (
            "an option this policy does not read"
            if subject.startswith("-")
            else "a name only the run resolves"
        )
        return KernelDecision(
            "deny",
            f"{spelled} {subject}: {unread}, so the tool it runs is unread",
            recovery="Name the tool literally, and spell an option's value with"
            " `=` or run the tool without it.",
        )
    tool = posixpath.basename(subject).partition("@")[0]
    if reading["kind"] == "script" and tool in INTERPRETERS:
        return KernelDecision(
            "deny",
            f"{spelled} {subject}: inline code leaves nothing behind to review",
            recovery="Write the code to a named script file and run it through"
            " `uv run python <script>`; a bare interpreter is refused even"
            " over a file.",
        )
    return None


def decide_uv(
    words: list[str],
    runner_targets: list[RunnerTargetRow],
    target_tables: list[ShellRuleRow] | None = None,
    facts: WriteFacts | None = None,
    frozen: tuple[str, ...] = UV_FROZEN_FLAGS,
    rows: list[ShellRuleRow] | None = None,
    program: KernelDecision | None = None,
) -> KernelDecision:
    """Classify a uv invocation, gating dependency and inline-code forms.

    ``program`` is the verdict the command `uv run` hands its words to earns
    as its own, which answers for a program that is neither an interpreter, a
    module nor a declared target: `uv run pip install x` is `pip install x`
    with this project's environment on its path, and answers as it does. A
    refusal there stands before any question uv's own options raise; a program
    the vocabulary names nothing about leaves `uv run` unjudged as it was.

    A declared target states what reaching it does, its placement and its
    reason, so a toolchain that has to run outside the sandbox says so once
    here rather than at each call site — and a target a project refuses is
    refused here rather than falling through to no judgment, which is a
    different answer: it leaves the verdict to the runtime rather than
    stating one.

    ``facts`` are what the host measured, on the same terms
    :func:`decide_command_rows` takes them: the target's own verdict is
    derived from its effects against them, and a target carrying a verb table
    hands them on to the walk that reads it. Nothing measured is the cautious
    reading rather than the permissive one.

    Installing is where the line sits. Fetching a package runs its build
    code, and that code is the escape a supply-chain compromise arrives
    through — so `add`, and a `sync` free to rewrite the lockfile, both ask:
    each resolves anew, and what the index serves under a name today is not
    what was reviewed when the name was declared. A sync pinned by one of
    ``frozen`` is the other side of that line, and :func:`frozen_restore`
    says why once for every package manager: it fetches nothing the lock
    does not already pin by integrity hash, which is exactly what `uv run`
    restores before running anything, unasked. Asking about the frozen
    spelling and not about the run was the same act answered two ways.

    Two verbs sit below that line and one sat above it by omission. `lock`
    and `remove` write files and fetch nothing to execute. A cache is
    reproducible by the command that reads it, so clearing one destroys
    nothing anybody has — and it was reaching no rule at all, which is why a
    refresh line asked with the cache verb as one of its reasons.

    A flag naming where packages come from, or dropping the isolation build
    code runs in, is not the verb it rides on: it is a source nobody
    declared, and it asks even where the bare verb would not. Inline-code
    refusals and operator-only prohibitions answer first; approving a package
    source cannot authorize an operation the requester may never perform.
    """
    measured = no_write_facts() if facts is None else facts
    spelled = words
    normalized = uv_command_words(words)
    # Asking uv what it is names no verb, which is how the reading below fails,
    # and changes nothing: `uv --version` was refused as though a global had
    # hidden one. Only the informational globals, and nothing beside them.
    if len(words) > 1 and all(
        word in ("--version", "-V", "--help", "-h") for word in words[1:]
    ):
        return KernelDecision("allow", "uv reports its own version or usage")
    if normalized is None:
        return KernelDecision(
            "deny", "uv global options do not identify a literal command to judge"
        )
    words = normalized
    subcommand = words[1]
    if subcommand == "sync" and uv_package_source(words[2:]) is None:
        pinned = frozen_restore(words[2:], list(frozen), list(UV_FOREIGN_SOURCE_FLAGS))
        if pinned is not None:
            return pinned
    # Named in the question: which packages `add` fetches, and that `sync`
    # resolves the whole declaration anew, which is what the pinned spelling
    # the recovery names does not. "Installing a package" said neither.
    if subcommand == "add":
        named = uv_add_operands(
            words[2:], (*UV_ADD_VALUE_FLAGS, *UV_GLOBAL_VALUE_OPTIONS)
        )
        return KernelDecision(
            "ask",
            f"uv add fetches and runs the build code of {', '.join(named)}"
            if named
            else f"uv add fetches and runs the build code of what it adds — `{' '.join(words)}`",
        )
    if subcommand == "sync":
        return KernelDecision(
            "ask",
            "uv sync resolves every dependency anew and runs each package's build"
            f" code — `{' '.join(words)}`",
            recovery=(
                "`uv sync --frozen` or `--locked` installs what the lockfile already"
                " pins by hash, and is allowed."
            ),
        )
    if subcommand in ("remove", "lock"):
        redirect = uv_package_source(words[2:])
        if redirect is not None:
            return KernelDecision(
                "ask",
                f"{redirect} takes packages from somewhere this project does not"
                " declare",
            )
        return KernelDecision("allow", "writes what this project already declares")
    if subcommand == "cache":
        return KernelDecision(
            "allow", "a package cache is rebuilt by the command that reads it"
        )
    if subcommand == "run" and len(words) > 2:
        run_words = uv_run_words(words)
        if not run_words:
            return unjudged("uv run has no command")
        run_command = posixpath.basename(run_words[0])
        bare_target = "/" not in run_words[0]
        # A script file and an inline program are different questions, and
        # answering them together denied the rung the guidance points at for
        # computing something once. One criterion decides every form: an
        # invocation is refused when it leaves no reviewable artifact behind.
        # `-c` leaves nothing to read and a bare interpreter runs nothing at
        # all; a path and a module in a declared root are both openable,
        # diffable and runnable again, so neither is inline code. The
        # interpreter's own grammar says which word is the program, the same
        # reading a bare `bash <script>` meets.
        interpreted = run_command in INTERPRETERS
        reading = read_program(run_words) if interpreted else None
        # A module is as readable as the file it lives in, so what decides one
        # is whether this project declares its root — read off the table that
        # already answers `uv run <target>`, because a blessed module root and
        # a blessed executable are the same statement: it runs something this
        # repository declares as its own. One declaration admits every module
        # beneath the root, and an undeclared root keeps the refusal, which is
        # what `-m http.server` meets.
        module_root = uv_run_module_root(run_words)
        declared_root = next(
            (row for row in runner_targets if row["name"] == module_root), None
        )
        if run_command == "-c":
            return KernelDecision(
                "deny",
                "uv run -c: inline code leaves nothing behind to review",
                recovery="Write it to a named script file, which can be reviewed"
                " and run again.",
            )
        refused = (
            None
            if reading is None
            else program_verdict(f"uv run {run_command}", reading)
        )
        if refused is not None and refused.effect == "deny":
            return refused
        if module_root is not None and declared_root is None:
            return KernelDecision(
                "deny",
                f"uv run -m: `{module_root}` is not a module root this project"
                " declares",
                recovery="Name a script file instead, or declare the root as a"
                " runner target.",
            )
        if reading is not None and reading["kind"] == "subcommand":
            return KernelDecision(
                "deny",
                f"uv run {run_command} {reading['subject']}: only a script file"
                " is read through `uv run`",
                recovery="Run the command itself, where its own rules judge it.",
            )
        # Transparent wrappers and nested runners cannot hide an operator-only
        # operation. Only its hard prohibition propagates: recognizing a target
        # here grants neither its wrapper nor an executable selected by path.
        guarded_words = command_words(run_words)
        if guarded_words:
            guarded = decide_command_rows(guarded_words, target_tables or [], measured)
            if guarded.hard:
                return guarded
            if posixpath.basename(guarded_words[0]) == "uv":
                nested = decide_uv(
                    guarded_words, runner_targets, target_tables, measured, frozen
                )
                if nested.hard:
                    return nested
        # A program the kernel knows answers as itself, and its refusal before
        # any question about uv's own options: `uv run --with x pip install y`
        # is `pip install y`, which is refused whatever else is fetched.
        declared = next(
            (row for row in runner_targets if row["name"] == run_command), None
        )
        ran = (
            program
            if not interpreted
            and module_root is None
            and not (bare_target and declared is not None)
            else None
        )
        if ran is not None and ran.effect == "deny":
            return ran
        risky = ["-w", "--with", "--with-editable", "--with-requirements"]
        # Named in the question, because what is being installed is the whole
        # of what an approver weighs: "external code" told them a source was
        # involved and nothing about which one.
        fetched = [
            f"{option} {carried['value']}"
            for position, word in enumerate(words[2:], start=2)
            for option in risky
            if (carried := carried_setting(word, [option], words[position + 1 :]))[
                "value"
            ]
        ]
        fetched.extend(
            f"--with {word[2:]}"
            for word in words[2:]
            if word.startswith("-w") and word != "-w"
        )
        # A secrets file is not code anybody fetched: it is loaded into the
        # environment the target runs with, and that is the question to put.
        loaded = [
            carried["value"]
            for position, word in enumerate(words[2:], start=2)
            if (
                carried := carried_setting(word, ["--env-file"], words[position + 1 :])
            )["value"]
        ]
        asked = [
            *(
                [f"uv run fetches and runs external code: {' '.join(fetched)}"]
                if fetched
                else []
            ),
            *(
                f"uv run --env-file {secrets} loads a secrets file into the process"
                " environment"
                for secrets in loaded
            ),
        ]
        if asked:
            return KernelDecision(
                "ask",
                "; ".join(asked),
                purpose="sensitive_access" if loaded and not fetched else None,
            )
        redirect = uv_package_source(words[2 : len(words) - len(run_words)])
        if redirect is not None:
            return KernelDecision(
                "ask",
                f"{redirect} takes packages from somewhere this project does not"
                " declare",
            )
        # Above the interpreter's own allow, because `python -m examples.x`
        # reaches both and the module root is the more specific statement:
        # what runs is the module, not a script path the interpreter was
        # handed.
        if declared_root is not None:
            return declared_target_decision(
                declared_root, run_words, target_tables, measured
            )
        if interpreted:
            return KernelDecision(
                "allow", "a script file can be read, where inline code cannot"
            )
        if bare_target and declared is not None:
            return declared_target_decision(
                declared, run_words, target_tables, measured
            )
        if bare_target and len(run_words) == 2 and run_words[1] == "--help":
            return KernelDecision("allow", "command help is read-only")
        if ran is not None and not ran.unlisted:
            return ran
    # `uv tool run` is `uvx` by its other name, so the tool it runs is read by
    # the same grammar, past uv's globals on either side of `run`.
    if subcommand == "tool":
        verbs = operand_positions(words[2:], UV_GLOBAL_VALUE_OPTIONS)
        at = 2 + verbs[0] if verbs else len(words)
        if words[at : at + 1] == ["run"]:
            refused = decide_tool_run("uv tool run", [*words[2:at], *words[at + 1 :]])
            if refused is not None:
                return refused
    # A verb the vocabulary declares -- `pip`, `tool`, `publish` -- is walked
    # from the command as spelled, so the row walker finds it past uv's global
    # options exactly as it finds any subcommand, and what it answers is the
    # row's rather than a second reading of the same verb here.
    declared = [
        row
        for row in rows or []
        if row["command"] == "uv" and row["subcommand"] == subcommand
    ]
    if declared:
        return decide_command_rows(spelled, rows or [], measured)
    return unjudged(f"uv {subcommand} is not classified")


def git_checkout_pathspec(words: list[str]) -> KernelDecision | None:
    """Recognize ``git checkout <ref> -- <path>...`` — a ref-sourced restore.

    Content comes from a named commit, so committed state is recoverable
    through the reflog; the branch-switch and index-sourced ``checkout --
    <path>`` forms fall through to their redirect rows, and opaque words
    deny toward explicit literal bindings.
    """
    if len(words) < 5 or words[1] != "checkout" or words[3] != "--":
        return None
    ref = words[2]
    if ref.startswith("-") or opaque_argument(ref):
        return None
    if any(opaque_argument(word) for word in words[4:]):
        return None
    return KernelDecision(
        "allow", "checkout from a named ref restores committed file state"
    )


def git_restore_source(words: list[str]) -> KernelDecision | None:
    """Recognize ``git restore --source=<ref> [--staged|--worktree] <path>...``.

    The ref-sourced twin of ``git checkout <ref> -- <path>``: content comes
    from a named commit, so committed state stays recoverable through the
    reflog. The index-sourced form and opaque words fall through to the
    restore row's ask.

    A grant, so the subcommand is read where it is written.
    """
    parsed = git_restore_operands(words, 1)
    if parsed is None or parsed["source"] is None:
        return None
    return KernelDecision(
        "allow", "restore from a named ref recovers committed file state"
    )


def git_restore_unchanged(
    words: list[str], recoverable_targets: list[str], path_rules: list[PathRuleRow]
) -> KernelDecision | None:
    """Recognize an index-sourced ``git restore`` whose paths hold no pending work.

    The row asks because restoring discards what the working tree has that the
    index does not. Where the host reports a path as tracked and carrying no
    uncommitted change, it has nothing the index does not, and the restore
    writes back the bytes already on disk — so the row's question has no
    answer worth putting to anyone. A path with pending work keeps it, which
    is the only case the question was ever about.

    Nothing bounds this the way the delete grant is bounded. That cap counts
    how much committed work one command destroys before a sweep is worth a
    question; this destroys none of it, however many paths are named. What
    does bound it is ownership, which the delete grant defers to for the same
    reason: what a path costs to rebuild is the wrong question about a file
    protected by whose it is, and the two gates read one table so they cannot
    come to differ about one.

    A grant, so the subcommand is read where it is written; the segment
    reading asks the ownership half again past git's globals.
    """
    parsed = git_restore_operands(words, 1)
    if parsed is None or parsed["source"] is not None:
        return None
    if not all(path in recoverable_targets for path in parsed["paths"]):
        return None
    protected = protected_write_target(parsed["paths"], path_rules, True)
    if protected is not None:
        return protected
    return KernelDecision("allow", "every restored path already matches the index")


def git_symbolic_ref_read(words: list[str]) -> KernelDecision | None:
    """Recognize ``git symbolic-ref [--short] <name>`` — the form that reports.

    Alone among the query verbs, symbolic-ref spells its write as a second
    operand rather than as a flag, so no flag list separates reading HEAD from
    pointing it elsewhere. One operand and nothing but the reporting flags is
    the read; a second operand, ``--delete``, or a word that expands at run
    time falls through to the row's ask.
    """
    if len(words) < 3 or words[1] != "symbolic-ref":
        return None
    operands = [word for word in words[2:] if not word.startswith("-")]
    flags = [word for word in words[2:] if word.startswith("-")]
    if len(operands) != 1 or any(opaque_argument(word) for word in words[2:]):
        return None
    if any(flag not in ("--short", "-q", "--quiet") for flag in flags):
        return None
    return KernelDecision("allow", "reading a symbolic ref reports where it points")
