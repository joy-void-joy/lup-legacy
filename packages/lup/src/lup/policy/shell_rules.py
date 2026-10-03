"""The shape a shell vocabulary takes, and its erasure into kernel rows.

The hermetic kernel classifies one shell command by consulting primitive rows,
exactly as it consults URL scopes and protected-path rows: the control flow
lives in :mod:`lup.policy.kernel`, and the *vocabulary* — which tools are safe
to run unattended, which forms need a human — is a judgement about one
project's toolchain, so it arrives from outside. This module declares the
shape that judgement takes as a nested pydantic table a human can read and
extend, and :func:`erase_shell_rules` flattens it into the ``ShellRuleRow``
tuples the kernel interprets.

An application declares its own table and hands it to ``ShellPolicy`` and to
``HookSet.shell_rules``; :mod:`lup.policy.bundle` erases that same table into
``policy_data.py`` at generation time, so the canonical ``ShellPolicy`` and
every generated dispatcher decide identically. This repository's table is
``lup_template.harness.content.shell_vocabulary``.

Three nesting levels mirror how real tools are shaped:

* a bare command — ``ls``, ``sort`` — declares the effects it has (``ls``
  reads a path), optionally with ``ask_flags`` that turn a reader into a
  writer (``sort -o``, ``find -delete``);
* a subcommand command — ``git``, ``gh`` — declares what falls off the end of
  its enumeration (an unjudged subcommand bounces back to the agent) and lists
  the subcommands it has judged (``git status`` allows, ``git push`` asks);
  its ``value_flags``
  skip value-taking globals (``git -C <path>``) so the value is never read as
  the subcommand, its ``ask_flags`` guard dangerous globals (``git -c``), and
  its ``setting_flags`` say which of those carry a setting, so the guard reads
  the setting rather than the flag;
* a subcommand whose *operation* word decides safety — ``git worktree add`` is
  reversible, ``git worktree remove`` is not — carries ``operations``.

Every axis cascades down that nesting, and absence has exactly one meaning
everywhere: a level that omits ``effects`` or ``sandbox`` inherits the value
from the level above it, and a level that states one overrides what it
inherited in either direction — widening a restrictive parent is as ordinary
as narrowing a permissive one. So ``git`` says once where its subcommands run,
and each of them says only what differs.

What a rule states is what it *does*, never what it earns. A verdict stated
beside the effects would be one judgement recorded twice, which is the drift
this model removes, so :func:`~lup.policy.kernel.effects.declared_verdict`
derives the answer where it is used. A rule that means to refuse a spelling says
``refuses``; one that means to raise a question declares an effect that asks.

The runner table says it the same way. ``uv`` is parsed rather than matched,
so a ``uv run`` target is declared on a surface of its own, stating effects
like every other rule: a verdict stated there would leave a target with
subcommands declaring both halves separately — what the target earns, and what
the command rows beneath it do.

Absence is distinguished from a stated value of the same word by what pydantic
already records about which fields a declaration supplied, so no field is
retyped as optional to carry the distinction. The two fields with no such
escape are ``ShellCommandRule.effects`` and ``RunnerTargetRule.effects``, both
required: a declaration stating none derives an allow, so one that forgot to
say would be a grant nobody wrote down rather than a gap a reader sees. A
command that genuinely does nothing this table guards says so —
``changes_nothing`` exists to be sayable.
"""

from collections.abc import Callable
from typing import TypedDict

from pydantic import BaseModel

from lup.policy.kernel.diagnostic import Step
from lup.policy.kernel.effects import EffectRow, external_effects
from lup.policy.kernel.semantics import EffectClass, ReviewerRequirement
from lup.policy.kernel.decision import (
    CheckpointRequirement,
    SandboxPlacement,
)
from lup.policy.kernel.rows import (
    DestinationForm,
    RefspecEffect,
    RunnerTargetRow,
    RuleLevel,
    ShellRuleRow,
)
from lup.seams import SelectableRule

ROOT_SANDBOX: SandboxPlacement = "ambient"
"""Where a call runs, beneath a table that says nothing — wherever it lands.

Unlike the effects axis, this one has a member meaning "no opinion", so
omission is a statement here rather than a gap, and a command is not made to
repeat it.
"""

ROOT_CHECKPOINT: CheckpointRequirement = "unrecoverable"
"""What puts back a loss nobody named — nothing does, so the question stands.

The restrictive value, and for the reason every root here is: this axis is
read to *relax* an approval question, so the value a forgotten declaration falls
to has to be the one that relaxes nothing. Annotating a rule is what says a
boundary answers for it, and silence can then only cost a prompt somebody did
not need, never a loss nobody could put back.
"""


ROOT_REVIEWER: ReviewerRequirement = "human_only"
"""Who answers a question no rule said anything about — a person.

The restrictive value, and for the same reason: this axis is read to
*widen* who may answer, so what a forgotten declaration falls
to has to widen nothing. A rule that means its question to be answerable by a
supervisor says so, and silence can then only cost a person a question a
supervisor could have taken, never route a release past them.
"""


class DeclaredAxes(BaseModel, frozen=True):
    """What one level of a table states, leaving the rest to the level above.

    ``None`` is inheritance, and it is the only thing absence means anywhere in
    the table — never a reset to the most permissive value.
    """

    effects: list[EffectRow] | None = None
    refuses: str | None = None
    sandbox: SandboxPlacement | None = None
    checkpoint: CheckpointRequirement | None = None
    reviewer: ReviewerRequirement | None = None
    effect_class: EffectClass | None = None


class RowAxes(TypedDict):
    """The fields one resolved level contributes to an erased row.

    Provenance travels beside each value because the question a reader
    has at a verdict they did not expect is which level said so, and a
    resolved value alone cannot answer it.
    """

    effects: list[EffectRow]
    effects_source: RuleLevel
    refuses: str
    sandbox: SandboxPlacement
    sandbox_source: RuleLevel
    checkpoint: CheckpointRequirement
    checkpoint_source: RuleLevel
    reviewer: ReviewerRequirement


class ResolvedAxes(BaseModel, frozen=True):
    """Every axis as one level resolved it, and where each value came from."""

    effects: list[EffectRow]
    effects_source: RuleLevel
    refuses: str
    sandbox: SandboxPlacement
    sandbox_source: RuleLevel
    checkpoint: CheckpointRequirement
    checkpoint_source: RuleLevel
    reviewer: ReviewerRequirement
    effect_class: EffectClass | None
    """The class a level stated, kept for the levels beneath to inherit.

    Not a row field. In the compiled table it would sit beside the effects it
    derives, which is the same judgement twice, and nothing would read it
    there, since no purpose is inferred from it. It stays here because it
    is a *declaration*: most of the remote table says what it does by
    naming its class and letting :func:`external_effects` turn that into
    effects, which is one judgement read once rather than a hundred remote
    operations transcribed by hand.
    """

    def row_fields(self) -> RowAxes:
        """This triple as the erased row spells it, provenance included.

        The effects arrive resolved. Deriving them here would read the class
        this level *ended up carrying* rather than the one it stated, and
        those are the same value only until a level above declares effects of
        its own -- after which every level below it that states a class and no
        effects reports what the level above does.
        """
        return RowAxes(
            effects=self.effects,
            effects_source=self.effects_source,
            refuses=self.refuses,
            sandbox=self.sandbox,
            sandbox_source=self.sandbox_source,
            checkpoint=self.checkpoint,
            checkpoint_source=self.checkpoint_source,
            reviewer=self.reviewer,
        )

    def inherit(
        self,
        declared: DeclaredAxes,
        level: RuleLevel,
        derive: Callable[[str], list[EffectRow]] = external_effects,
    ) -> "ResolvedAxes":
        """These axes as the level beneath resolves them over its own statements.

        A declaration wins in either direction: widening a restrictive parent is
        as ordinary as narrowing a permissive one, so nothing here compares the
        two values — only whether the level supplied one.

        Effects take a third source between those two, because a level has two
        ways of stating them. Declaring them outright is the first; declaring
        the external class they follow from is the second, and it is the one
        most of the remote table uses. Both are this level speaking, so both
        have to outrank the level above -- a subcommand that refuses what fell
        off its enumeration must not hand that refusal to the operations the
        enumeration actually lists.
        """
        stated = declared.effects is not None or declared.effect_class is not None
        return ResolvedAxes(
            effects=(
                declared.effects
                if declared.effects is not None
                else derive(declared.effect_class)
                if declared.effect_class is not None
                else self.effects
            ),
            effects_source=level if stated else self.effects_source,
            refuses=self.refuses if declared.refuses is None else declared.refuses,
            sandbox=self.sandbox if declared.sandbox is None else declared.sandbox,
            sandbox_source=self.sandbox_source if declared.sandbox is None else level,
            checkpoint=self.checkpoint
            if declared.checkpoint is None
            else declared.checkpoint,
            checkpoint_source=(
                self.checkpoint_source if declared.checkpoint is None else level
            ),
            reviewer=(
                self.reviewer if declared.reviewer is None else declared.reviewer
            ),
            effect_class=(
                self.effect_class
                if declared.effect_class is None
                else declared.effect_class
            ),
        )


ROOT_AXES = ResolvedAxes(
    effects=[],
    effects_source="root",
    refuses="",
    sandbox=ROOT_SANDBOX,
    sandbox_source="root",
    checkpoint=ROOT_CHECKPOINT,
    checkpoint_source="root",
    reviewer=ROOT_REVIEWER,
    effect_class=None,
)
"""What the outermost level of every table inherits from."""


class RunnerTargetRule(BaseModel, frozen=True):
    """One ``uv run <target>`` a project blesses, and where it has to run.

    ``uv`` is parsed rather than matched against the command table, so this is
    the only surface on which a runner target can carry the sandbox axis. A
    toolchain that opens its own agent sessions needs ``outside``: the runtime
    creates per-session state under its configuration directory, and a session
    launched from inside a sandbox that does not grant that path loses its
    shell entirely. Declaring it beside the name is what keeps the escape off
    the call sites — one that has to remember a flag is one that forgets it.

    The field is the whole placement vocabulary and not a two-valued switch:
    one field says where every run of a target goes, which is what stops a
    second way of saying it from growing. A target that only sometimes needs
    the outside stays confined here, and the one call that has to leave asks
    with ``# lup: escalate[sandbox]: <why>``, which a reviewer answers.
    """

    name: str
    sandbox: SandboxPlacement = "ambient"

    effects: list[EffectRow]
    """What reaching this target does, which is the whole of what it earns.

    Required, for the reason :attr:`ShellCommandRule.effects` is: a target
    stating none derives an allow, so an omission would be a grant nobody
    wrote down rather than a gap a reader sees. Blessing a toolchain is the
    common case and it is said in one word — ``runs_declared_target``.

    One statement serving both halves. A target with subcommands is erased
    twice, as a runner row and as the command rows its verbs are judged by,
    and a verdict stated here as well would declare the two halves separately
    — a target blessing itself and refusing its own verbs, or the reverse,
    with nothing noticing.
    """

    refuses: str = ""
    """Where the agent goes instead, when this project refuses the target.

    The command table's field, on the same terms: set, the target denies
    whatever its effects would have earned, and :attr:`reason` carries the
    route. A target that spends money, runs for an hour, or publishes
    something is refused here rather than left undeclared — leaving it off is
    not a refusal but an absence of judgment, which a confined session hands
    to the runtime's own permissions.
    """

    reason: str = ""
    """What the target does that stopped it, as the approver of one reads it."""

    recovery: list[Step] = []
    """What the agent is told to do instead, which for a refusal is its value.

    A refused target usually has a right way to reach the same end — print
    the command for a human to run, use the dry-run flag, go through the
    review step — and this is the channel that carries it.
    """

    subcommands: list["ShellSubcommandRule"] = []
    """Verbs beneath the target that answer differently from the target itself.

    A toolchain reached through ``uv run`` is one target and many commands,
    and a project that blesses the toolchain rarely means to bless every verb
    in it — a devtools CLI that mostly reads a repository may also have one
    subcommand that opens a paid agent session. Without this the choice is
    between blessing that verb and refusing the whole toolchain.

    The shape is the shell table's own, and so is the matching: a target with
    subcommands is judged by the same rows and the same walk as a command
    named directly, with the target's own effect as the default beneath them.
    Empty leaves the target a single verdict, which is what most are.
    """


class ShellOperationRule(BaseModel, frozen=True):
    """One operation word under a subcommand — e.g. ``worktree remove``."""

    name: str
    parents: list[str] = []
    operator_only: bool = False
    effects: list[EffectRow] = []
    refuses: str = ""
    ask_flags: list[str] = []
    flag_effects: list[EffectRow] = []
    write_flags: list[str] = []
    probe_flags: list[str] = []
    """Flags after which this operation performs nothing — its dry-run spelling.

    The subcommand's column, for a verb nested beneath one: `dev init upstream
    --dry-run` prints the registration it would write and writes none, so the
    question its effects raise is about an effect the probe never performs."""
    setting_flags: list[str] = []
    """Guarded flags whose value names the setting the question is about.

    The command's column, for an operation whose guarded flag takes a value
    and only some values are worth a question: a launch's `--sandbox none`
    opens a session with no boundary, and `--sandbox outer` opens one with the
    strongest. Read against :attr:`guarded_settings` in the two spellings a
    command's own globals are read in."""
    guarded_settings: list[str] = []
    """Which values those flags must name for the question to stand."""
    landing_operands: int = 0
    """The subcommand's column, for an operation that writes where an operand
    says: `gh repo clone <repo> <dir>` writes the second."""
    landing_flags: list[str] = []
    """Flags whose value is a place this operation writes: `--dir`, `--output`."""
    sandbox: SandboxPlacement = ROOT_SANDBOX
    checkpoint: CheckpointRequirement = ROOT_CHECKPOINT
    reviewer: ReviewerRequirement = ROOT_REVIEWER
    effect_class: EffectClass | None = None
    reason: str = ""
    recovery: list[Step] = []

    def declared(self) -> DeclaredAxes:
        """The axes this operation states itself, leaving the rest to inherit."""
        supplied = self.model_fields_set
        return DeclaredAxes(
            effects=self.effects if "effects" in supplied else None,
            refuses=self.refuses if "refuses" in supplied else None,
            sandbox=self.sandbox if "sandbox" in supplied else None,
            checkpoint=self.checkpoint if "checkpoint" in supplied else None,
            reviewer=self.reviewer if "reviewer" in supplied else None,
            effect_class=(self.effect_class if "effect_class" in supplied else None),
        )


class ShellSubcommandRule(BaseModel, frozen=True):
    """One subcommand under a command — e.g. ``git worktree``, ``gh pr``.

    ``read_verbs`` name action-selecting flags that pin a one-action-at-a-time
    subcommand to its query form (``git config --get``); their presence among
    literal, unguarded words de-escalates a non-allow effect to allow.
    ``guarded_keys`` state the inverse, for a subcommand whose writes are not
    all alike: their *absence* among legible words de-escalates, so the
    effect is kept only for the settings that redirect how commands execute.
    ``ask_refspecs`` states the ``ask_flags`` downgrade about an operand's
    grammar instead of a word's spelling, for a subcommand whose refspecs
    carry the same effects its flags do. ``ask_destinations`` states the same
    downgrade about the first operand that is not a flag, for a subcommand
    that takes a repository there and accepts one spelled out inline as
    readily as one the remote table holds. ``force_flags``, ``lease_flags``
    and ``protected_refs`` judge a forced update by what it can discard, and
    ``value_flags`` name the options whose value is the next word, so neither
    reading takes that value for an operand; the erased row states each.
    """

    name: str
    effects: list[EffectRow] = []
    refuses: str = ""
    ask_destinations: list[DestinationForm] = []
    ask_refspecs: list[RefspecEffect] = []
    force_flags: list[str] = []
    lease_flags: list[str] = []
    protected_refs: list[str] = []
    value_flags: list[str] = []
    ask_flags: list[str] = []
    flag_effects: list[EffectRow] = []
    write_flags: list[str] = []
    read_verbs: list[str] = []
    probe_flags: list[str] = []
    frozen_flags: list[str] = []
    guarded_keys: list[str] = []
    read_operands: int = 0
    """The most operands this subcommand's reading form names.

    `git config core.hooksPath` looks the setting up and `git config
    core.hooksPath /x` sets it: the value is the whole difference, so no flag
    or marker tells the two apart and the count is what the row declares.
    Zero declares no such form."""
    read_options: list[str] = []
    """The options that reading form may carry beside its operands.

    Named as the ones allowed rather than the ones that write, because the
    flags turning a lookup into a write -- `--unset`, `--add`,
    `--replace-all` -- are the open-ended half: a flag nobody listed keeps
    the row's question. `--type=bool` is read as `--type`."""
    landing_operands: int = 0
    """The operand, counted from one, from which every operand is a place written.

    What a question whose harm lands on paths reads its targets from, where no
    other reader names them: `git clone <repo> <dir>` writes the second.
    Zero where no operand does. Declared and nothing names one, the operation
    writes where it runs."""
    landing_flags: list[str] = []
    """Flags whose value is a place this operation writes: `--dir`, `--output`."""
    operations: list[ShellOperationRule] = []
    sandbox: SandboxPlacement = ROOT_SANDBOX
    checkpoint: CheckpointRequirement = ROOT_CHECKPOINT
    reviewer: ReviewerRequirement = ROOT_REVIEWER
    effect_class: EffectClass | None = None
    reason: str = ""
    recovery: list[Step] = []

    def declared(self) -> DeclaredAxes:
        """The axes this subcommand states itself, leaving the rest to inherit."""
        supplied = self.model_fields_set
        return DeclaredAxes(
            effects=self.effects if "effects" in supplied else None,
            refuses=self.refuses if "refuses" in supplied else None,
            sandbox=self.sandbox if "sandbox" in supplied else None,
            checkpoint=self.checkpoint if "checkpoint" in supplied else None,
            reviewer=self.reviewer if "reviewer" in supplied else None,
            effect_class=(self.effect_class if "effect_class" in supplied else None),
        )


class ShellCommandRule(SelectableRule, frozen=True):
    """One executable — a read-only tool, or a subcommand-gated command.

    Selectable by its executable name, which is also what the kernel matches
    on, so a project replacing ``git`` replaces exactly the rule that would
    have judged ``git`` and leaves no second rule under that name for a walk
    to reach first.

    On a subcommand-gated command, ``value_flags`` name the global options that
    consume the following word (``git -C <path>``) so the value is never read
    as the subcommand, and ``ask_flags`` guard dangerous globals in that same
    pre-subcommand position (``git -c``). ``allow_flags`` declare the pure
    read-only form of a non-allow command: the row de-escalates to allow only
    when every argument is exactly one of the named flags (``ssh-add -l``).
    ``read_verbs`` do the same for a command whose read-only form still takes
    operands, so no all-flags test can recognize it (``nc -z host port``): a
    declared verb among otherwise literal, unguarded words pins the action.

    ``sandbox`` is the other axis, declared here rather than inferred by a
    renderer: it says where an invocation matched by this rule has to run,
    whatever effect the rule reaches. Reads that need a remote take
    ``outside`` and run unprompted; writes that need one take ``outside``
    beside an ``ask``, so the approval says both things at once. A command
    that usually belongs inside but sometimes has to leave stays confined
    here, and the call that has to leave asks with ``# lup: escalate[sandbox]:
    <why>`` — one field says every placement, so a rule that permits the
    outside never has a second way to say it.
    """

    name: str
    effects: list[EffectRow]
    refuses: str = ""
    """Where the agent goes instead, when this project refuses the spelling.

    Set, the row denies whatever its effects would have earned, and the text
    is the whole of what the agent is told -- so it names the route rather
    than the objection: `uv add` for `pip install`, writing the command out
    for `eval`. Empty is the ordinary case and derives as usual.

    Apart from the effects because it is not one of them. `pip install` takes
    exactly the dependency `uv add` takes, and a row that spelled the
    preference as a distinct effect would have the command claiming to do
    something it does not -- which is the drift this whole table is removing.
    A refusal is about the route, and the route is not what an operation does.

    Inherited like every other axis, so a toolchain refused at the command
    keeps its one documented entry point by clearing it (`refuses=""`) on the
    subcommand that has one.
    """
    ask_flags: list[str] = []
    flag_effects: list[EffectRow] = []
    """What a guarded flag adds to what this command does.

    ``ask_flags`` says which spellings escalate; this says what the escalation
    is about, and the two are different statements. A row that made only the
    first described the command without the flag and then asked about the
    command with it, so the question's subject was named nowhere.
    """
    write_flags: list[str] = []
    """Options whose *value* is a path this command writes.

    Held apart from ``ask_flags`` because the two escalate for unlike
    reasons and only one of them names something a write row can judge:
    `sort -o out.txt` lands a file where the command would have written
    stdout, and `sort --compress-program=x` runs a program. One list
    answering for both would let the row's single verdict decide each, so a
    write to scratch would ask and a write over tracked source would not.

    The value is what makes an entry belong here, not the writing. `yq -i`
    rewrites a file and stays an ``ask_flags`` entry, because the file it
    rewrites is the operand and the flag carries nothing -- reading the next
    word as its path would name the expression instead.
    """
    allow_flags: list[str] = []
    read_verbs: list[str] = []
    probe_flags: list[str] = []
    """Flags after which this command performs nothing — its dry-run spelling.

    A read verb pins a many-actioned command to its query action, and is
    honored only among unguarded words, because a guarded flag beside it
    would still act. A probe flag is stronger: `git push --dry-run --force`
    replaces no ref however the rest of the line reads, so its literal
    presence stands down the flag- and refspec-earned questions along with
    the row's own effects. What it does not stand down is destination
    grammar — a probe still contacts the repository it names, so where the
    work would go stays guarded as a place rather than as a write.
    """
    frozen_flags: list[str] = []
    """Flags that pin a dependency restore to what its lockfile already declares.

    `bun install --frozen-lockfile` fetches nothing the lock does not pin by
    integrity hash, which is the restore `uv run` performs before running
    anything, unasked — so the flag de-escalates the row to allow, on the
    terms a read verb is honored: legible, and among words free of guarded
    flags. Without it the same verb is free to rewrite the lockfile first,
    which resolves what the project depends on anew and keeps the question.
    """
    write_markers: list[str] = []
    """Argument prefixes whose *absence* makes this command read-only.

    Every other de-escalation here is a positive test: some word has to be
    present for the rule to relax. A few commands are the other way round --
    `dd` writes only when handed an `of=`, and with no `of=` it is a plain
    read of `if=` to stdout. There is no verb to list, because the read-only
    form is the one with nothing extra in it, so a membership test can never
    recognize it and every such invocation stopped for approval as a write.
    """
    guarded_keys: list[str] = []
    """Setting names whose *absence* makes this command's write an ordinary one.

    The same absence test as `write_markers`, asked about a write's subject
    instead of its form. That one asks whether a command writes at all; this
    assumes it does and asks whether what it writes decides how later
    commands execute. `git config` is the case: it can set `user.email` or it
    can set `core.hooksPath`, and only the second arranges for a program to
    run. A row without this can only hold one effect over both, which makes
    its reason -- "git config can change how commands execute" -- false for
    nearly every invocation it stops, and an approval question whose stated
    reason does not hold is one nobody can weigh.

    Glob patterns, matched case-blind. Git reads a key's section and name
    without regard to case, so a guard written `core.hookspath` has to catch
    `core.hooksPath`, and the families worth naming are shaped around a
    subsection the caller chooses (`merge.*.driver`).
    """
    setting_flags: list[str] = []
    """Guarded globals whose value names a setting this command will apply.

    `git -c <key>=<value>` and `git config <key> <value>` set the same thing,
    so both say which settings their question is about. Without this, every
    `-c` would ask on the strength of what `-c` can reach — with a reason,
    that a setting can change how commands execute, that is untrue of
    `-c color.ui=false`, the spelling this repository's own guidance asks for
    whenever a diff is captured.

    Read against :attr:`guarded_settings`, and only in the two spellings that
    can be read without guessing: the value in the next word, or after the
    `=` of a long option. A short flag with its value pressed against it keeps
    asking, because the `=` in that word belongs to the setting rather than to
    the flag.
    """
    guarded_settings: list[str] = []
    """Which settings those globals must name for the question to stand.

    `guarded_keys` for the globals, and the same glob patterns — the point of
    both being that one judgement about which settings hand over execution
    answers for every spelling that reaches them. Declared separately from
    `guarded_keys` because that one is an absence test over the whole argument
    list, and on a subcommand-gated command's own row it would also answer for
    the verbs that fell off the enumeration.
    """
    outward_settings: list[str] = []
    """Which of those settings reach beyond the machine the session runs on.

    A credential helper hands a lent secret to the program it names, and a
    URL rewrite sends the next push somewhere no destination guard read. The
    rest of the guarded settings run a program where the session runs, which
    a container holds -- so these are the ones whose question stands inside
    one too, stated rather than inferred from which pattern they look like.
    """
    landing_operands: int = 0
    """The operand, counted from one, from which every operand is a place written.

    The subcommand's column, for a command with no subcommand: `chmod <mode>
    <file>...` changes the files from the second operand on."""
    landing_flags: list[str] = []
    """Flags whose value is a place this command writes."""
    bare_reads: bool = False
    """Whether this command only reads when handed no arguments at all.

    The one case `write_markers` cannot reach. That recognizes a reading form
    by the marker it lacks, which still needs a word to examine; `mount` reads
    in the form that has no words, and every form that acts names a device or
    a mountpoint. So the emptiness itself is the whole signal.

    Declared per command rather than derived, because a bare invocation is an
    action for plenty of commands and nothing in the row's shape says which:
    `ssh-add` with nothing after it adds the default key.
    """
    value_flags: list[str] = []
    directory_flags: list[str] = []
    """Flags whose value is the directory this command runs its paths from.

    Not every flag that consumes a word: `git -C <dir>` runs git as though it
    had been started there, so every path operand beside it resolves from
    there, while `--git-dir` names a repository and moves nothing, and
    `--work-tree` moves a pathspec alone, where git stands outside the tree.
    Each consumes a word, and only the first answers this, so they are
    declared apart.

    Read where a `cd` is read, and joined onto it: a command carrying one
    inside a segment that already moved runs in the directory the two name
    together.
    """
    subcommands: list[ShellSubcommandRule] = []
    sandbox: SandboxPlacement = ROOT_SANDBOX
    checkpoint: CheckpointRequirement = ROOT_CHECKPOINT
    reviewer: ReviewerRequirement = ROOT_REVIEWER
    effect_class: EffectClass | None = None
    reason: str = ""
    """What the command does that stopped it, as the approver of one reads it."""
    recovery: list[Step] = []
    """What the agent can do instead, where the command has a better route."""

    def selection_id(self) -> str:
        return self.name

    def declared(self) -> DeclaredAxes:
        """The axes this command states itself, leaving the rest to inherit."""
        supplied = self.model_fields_set
        return DeclaredAxes(
            effects=self.effects if "effects" in supplied else None,
            refuses=self.refuses if "refuses" in supplied else None,
            sandbox=self.sandbox if "sandbox" in supplied else None,
            checkpoint=self.checkpoint if "checkpoint" in supplied else None,
            reviewer=self.reviewer if "reviewer" in supplied else None,
            effect_class=(self.effect_class if "effect_class" in supplied else None),
        )


def erase_runner_targets(targets: list[RunnerTargetRule]) -> list[RunnerTargetRow]:
    """Flatten the declared runner targets into the kernel's primitive rows."""
    return [
        RunnerTargetRow(
            name=target.name,
            sandbox=target.sandbox,
            effects=list(target.effects),
            refuses=target.refuses,
            reason=target.reason,
            recovery=target.recovery,
        )
        for target in targets
    ]


def runner_target_tables(targets: list[RunnerTargetRule]) -> list[ShellRuleRow]:
    """The verb tables of every target that declares one, as command rows.

    A target with subcommands is a command table wearing a runner target's
    name, so it is erased as one and matched by the walk that already reads
    those rows. Targets without subcommands contribute nothing here and keep
    their single verdict on the runner row.
    """
    return erase_shell_rules(
        [
            ShellCommandRule(
                name=target.name,
                effects=target.effects,
                refuses=target.refuses,
                subcommands=target.subcommands,
                sandbox=target.sandbox,
                reason=target.reason,
                recovery=target.recovery,
            )
            for target in targets
            if target.subcommands
        ]
    )


def rule_id(command: str, subcommand: str = "", operation: str = "") -> str:
    """The stable id of the row matching these levels.

    Derived from what the row matches rather than declared beside it, so a
    renamed subcommand renames its id and no second place has to be kept in
    step. The ``shell:`` prefix is what keeps it distinguishable from an edit
    or fetch rule in a single audit table.
    """
    return "shell:" + ".".join(
        part for part in (command, subcommand, operation) if part
    )


def erase_shell_rules(rules: list[ShellCommandRule]) -> list[ShellRuleRow]:
    """Flatten the nested table into the kernel's primitive command rows.

    Each row carries its match levels, its flag lists, its reason, and both
    axes already resolved against every level above it, so the kernel matching
    one row has the whole answer and never composes anything. An empty string
    at a level means "the default at that level". A command contributes one
    default row plus, per subcommand, one row per operation and a
    subcommand-default row for the bare form.

    Resolving here rather than at decision time is what keeps the canonical
    policy and every generated dispatcher from disagreeing about what a level
    inherited: they read the same rows, so there is nothing left to disagree
    about.
    """

    def subcommand_rows(
        command_name: str, subcommand: ShellSubcommandRule, above: ResolvedAxes
    ) -> list[ShellRuleRow]:
        axes = above.inherit(subcommand.declared(), "subcommand")
        operations = [
            ShellRuleRow(
                rule=rule_id(
                    command_name,
                    subcommand.name,
                    ".".join([*operation.parents, operation.name]),
                ),
                command=command_name,
                subcommand=subcommand.name,
                operation=operation.name,
                operation_path=[*operation.parents, operation.name],
                operator_only=operation.operator_only,
                ask_destinations=[],
                ask_refspecs=[],
                force_flags=[],
                lease_flags=[],
                protected_refs=[],
                ask_flags=list(operation.ask_flags),
                flag_effects=list(operation.flag_effects),
                write_flags=list(operation.write_flags),
                allow_flags=[],
                read_verbs=[],
                probe_flags=list(operation.probe_flags),
                frozen_flags=[],
                write_markers=[],
                guarded_keys=[],
                read_operands=0,
                read_options=[],
                setting_flags=list(operation.setting_flags),
                guarded_settings=list(operation.guarded_settings),
                outward_settings=[],
                landing_operands=operation.landing_operands,
                landing_flags=list(operation.landing_flags),
                bare_reads=False,
                value_flags=[],
                directory_flags=[],
                reason=operation.reason,
                recovery=operation.recovery,
                **axes.inherit(operation.declared(), "operation").row_fields(),
            )
            for operation in subcommand.operations
        ]
        default = ShellRuleRow(
            rule=rule_id(command_name, subcommand.name),
            command=command_name,
            subcommand=subcommand.name,
            operation="",
            operation_path=[],
            operator_only=False,
            ask_destinations=list(subcommand.ask_destinations),
            ask_refspecs=list(subcommand.ask_refspecs),
            force_flags=list(subcommand.force_flags),
            lease_flags=list(subcommand.lease_flags),
            protected_refs=list(subcommand.protected_refs),
            ask_flags=list(subcommand.ask_flags),
            flag_effects=list(subcommand.flag_effects),
            write_flags=list(subcommand.write_flags),
            allow_flags=[],
            read_verbs=list(subcommand.read_verbs),
            probe_flags=list(subcommand.probe_flags),
            frozen_flags=list(subcommand.frozen_flags),
            write_markers=[],
            guarded_keys=list(subcommand.guarded_keys),
            read_operands=subcommand.read_operands,
            read_options=list(subcommand.read_options),
            setting_flags=[],
            guarded_settings=[],
            outward_settings=[],
            landing_operands=subcommand.landing_operands,
            landing_flags=list(subcommand.landing_flags),
            bare_reads=False,
            value_flags=list(subcommand.value_flags),
            directory_flags=[],
            reason=subcommand.reason,
            recovery=subcommand.recovery,
            **axes.row_fields(),
        )
        return [*operations, default]

    def command_rows(command: ShellCommandRule) -> list[ShellRuleRow]:
        axes = ROOT_AXES.inherit(command.declared(), "command")
        default = ShellRuleRow(
            rule=rule_id(command.name),
            command=command.name,
            subcommand="",
            operation="",
            operation_path=[],
            operator_only=False,
            ask_destinations=[],
            ask_refspecs=[],
            force_flags=[],
            lease_flags=[],
            protected_refs=[],
            ask_flags=list(command.ask_flags),
            flag_effects=list(command.flag_effects),
            write_flags=list(command.write_flags),
            allow_flags=list(command.allow_flags),
            read_verbs=list(command.read_verbs),
            probe_flags=list(command.probe_flags),
            frozen_flags=list(command.frozen_flags),
            write_markers=list(command.write_markers),
            guarded_keys=list(command.guarded_keys),
            read_operands=0,
            read_options=[],
            setting_flags=list(command.setting_flags),
            guarded_settings=list(command.guarded_settings),
            outward_settings=list(command.outward_settings),
            landing_operands=command.landing_operands,
            landing_flags=list(command.landing_flags),
            bare_reads=command.bare_reads,
            value_flags=list(command.value_flags),
            directory_flags=list(command.directory_flags),
            reason=command.reason,
            recovery=command.recovery,
            **axes.row_fields(),
        )
        nested = [
            row
            for subcommand in command.subcommands
            for row in subcommand_rows(command.name, subcommand, axes)
        ]
        return [default, *nested]

    return [row for command in rules for row in command_rows(command)]
