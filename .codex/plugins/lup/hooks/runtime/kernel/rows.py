"""Primitive row shapes the generated data file renders into."""

from typing import Literal, TypedDict, NotRequired

from .decision import CheckpointRequirement, SandboxPlacement, KernelDecision
from .effects import EffectRow
from .semantics import ReviewerRequirement

type PathRuleKind = Literal[
    "exact",
    "subtree",
    "name_prefix",
    "new_subtree",
    "contains_part",
    "new_devtools",
]

type RuleLevel = Literal["root", "command", "subcommand", "operation"]
"""Which nesting level of a shell table a resolved value was declared at.

A row's own level is the deepest name it carries; anything shallower means the
value was inherited. ``root`` is the fallback beneath every table, which a
command declaring its own effect always shadows.
"""


class UrlScopeRow(TypedDict):
    """One erased fetch scope: an origin, the path beneath it, and its reason.

    ``include_subdomains`` widens ``host`` to cover names beneath it, so a
    scope can name a documentation site once instead of every subdomain.
    ``any_port`` widens it the other way, for a host whose port is the
    caller's to choose — a local service started with ``--port`` is the same
    service at every one of them, and a scope pinned to one would put the
    question back the first time somebody moved it.
    """

    scheme: str
    host: str
    port: int | None
    path_prefix: str
    reason: str
    include_subdomains: bool
    any_port: bool


class PathRuleRow(TypedDict):
    """One erased protected-path rule and whether review may bypass it.

    ``allow_autonomous`` releases the rule for an identity that already
    reviews its own edits; every other rule holds regardless of caller.
    """

    kind: PathRuleKind
    value: str
    reason: str
    recovery: str
    allow_autonomous: bool


class DisplacedTargetRow(TypedDict):
    """One write target whose real location is not the one it spells.

    A role is read off a spelling, and only the filesystem can say whether the
    file is in the root that spelling names. Where a symlink says otherwise,
    this carries both halves: a question naming only the path the caller typed
    would be asking about the wrong file.
    """

    path: str
    lands: str


class WithheldWalkRow(TypedDict):
    """One root a recursive reader walks, and a withheld path the host found in it.

    A key or a login beneath a root is a fact only a filesystem has, so the
    host walks what the reader would and names the first it meets. ``found``
    is where the walk stopped instead, where the hook's deadline came before
    it could finish: a root nobody finished walking is not one known to hold
    nothing.
    """

    root: str
    found: str


type TargetLanding = Literal["container", "checkout", "host"]
"""Where one path an operation names lands, as the launch that holds it sees it.

``container`` is nothing the host shares: the image's own directories, the
container's temporary root, gone when the container is. ``checkout`` is the
session's own working tree -- mounted from the host, and the tree the session
was opened to work in. ``host`` is anything else the host lent: another
project, a cache a later launch reads, a credential file, a config volume.
"""


class TargetLandingRow(TypedDict):
    """One path an operation names, and where the host that measured it says it lands.

    Measured by the host, which reads the launch's ledger and its own mount
    table, and handed to the kernel as data, which reads neither. A path the
    host did not classify has no row, and a reader of these rows treats a
    missing one as landing nowhere it can vouch for.
    """

    path: str
    lands: TargetLanding


type PathRoleName = Literal["production", "test", "data", "scratch"]

type PathRoleKind = Literal["subtree", "contains_part"]
"""The two directory shapes :func:`root_matches` tells apart.

The narrow pair out of :data:`PathRuleKind`, named separately because that is
the whole of what one function answers for: ``subtree`` anchors a root at the
repository top, while ``contains_part`` matches the directory wherever it
sits, for a tree that is what it is regardless of which package holds it.
"""


class PathRoleRow(TypedDict):
    """One erased declaration of what a repository root is for.

    A role names the purpose a tree serves, which is what decides how much of
    the lattice applies to it. Tests are judged by whether they exercise
    production, and data by the evidence it retains, rather than by production's
    source conventions. ``scratch`` is disposable by construction, so the verbs
    that ask before destroying something have nothing to protect there.

    ``root`` is a pattern, which is what says how far the declaration reaches:
    a bare root is anchored at the repository top, and a leading ``**/`` names
    the directory wherever it sits.
    """

    root: str
    role: PathRoleName


class VerificationRow(TypedDict):
    """How this project spells the checks a delegated agent is pointed at.

    Two strings rather than one sentence, because the notice is composed
    where it is read and only the spellings are the project's. A repository
    that named its devtools CLI something else reads its own invocation here,
    where a verbatim notice names one it does not serve.
    """

    scoped: str
    tests: str


class SpawnNameRow(TypedDict):
    """One erased decision that a spawned agent goes out named, and how it is spelled.

    ``reason`` is what a spawn with nothing to read a name from is refused
    with; ``recovery`` says the shape a name takes, because a refusal an agent
    cannot act on becomes a retry, and the kernel opens it with the key the
    runtime reads the name from, which the row cannot hold since one row
    serves every runtime.

    ``punctuation`` is what a name may carry beside letters and digits, its
    first mark the one a normalized name joins its words with, and ``limit``
    how long it may be. Both are data rather than a check written into the
    judgement, because which spellings are safe is a property of the runtimes
    a project runs on, and a project running on one of them may widen what a
    project running on several cannot.
    """

    reason: str
    recovery: str
    punctuation: str
    limit: int


class AcceptanceGuardRow(TypedDict):
    """One erased decision to hold a project's acceptance tests still.

    A test states the behaviour production owes; editing one moves the target
    the implementation is aimed at, which is a judgement about what the work
    is rather than about how it was done. The two reasons are separate
    because the two callers are: an ordinary session is asked, since a test
    that genuinely encodes the wrong behaviour has to be changeable by
    someone who can weigh that, while a session implementing *against* these
    tests is refused, because for it the tests are the specification and
    rewriting a specification to match the implementation is the failure the
    guard exists to catch.
    """

    ask_reason: str
    autonomous_reason: str


class ResolutionRow(TypedDict):
    """What a checker settled about one text's receivers, per rule id.

    ``refuted`` holds the lines whose receiver resolved to a declaration
    outside the rule's family: no violation is there, and a directive naming
    the rule there guards nothing. ``unresolved`` holds the lines nothing
    could be shown about: the rule demands no directive there and refuses
    none, and a directive written there stands, because a gate that learned
    nothing about a receiver has no evidence against the line or against the
    marker on it. The audit reads the same two verdicts off the same
    resolution, so neither gate can call dead what the other demands.
    """

    refuted: dict[str, list[int]]
    unresolved: dict[str, list[int]]


class RewrittenDocumentRow(TypedDict):
    """One file an in-place rewrite names, as it stands and as the line leaves it.

    The host produces this by running the screened script over a *copy*, never
    over the file, so a command still refused has changed nothing — and the
    classifier reads the result as the ``before`` and ``after`` of an ordinary
    edit, which is what lets one gate answer for both spellings of a write.

    What the whole line leaves rather than what this one rewrite does, so a
    second rewrite of one file is judged by what it introduces over the
    first: ``before`` is the file before the line, ``None`` where the line
    creates it, and ``after`` what it holds once every step writing it ran,
    ``None`` where a later step removes it. ``operation`` is the class of
    that change, as an edit states it.

    ``target`` and ``path`` differ because two readers need different
    spellings of the same file. The rules match on ``path``, relative to the
    worktree that holds it, since a rule anchored at the repository top has to
    be asked about where the file sits; a refusal names ``target``, the word
    the writer actually typed and the one they would have to change.
    """

    target: str
    path: str
    before: str | None
    after: str | None
    operation: str
    foreign: bool
    """Whether the file belongs to a repository that is not this one."""

    outside_project: bool
    """Whether it sits in no checkout whose conventions these rules are."""

    checkout_path: str
    """The same file as the session's own checkout spells it, or "" outside it.

    Differs from ``path`` only where a repository nested inside the checkout
    holds the file, which is the one place the two anchors disagree.
    """

    resolution: ResolutionRow | None
    """What a checker settled about the rewritten text, where one was worth running."""
    decision: NotRequired[KernelDecision]
    """A host-resolved owner decision, including caller write restrictions."""


class PostToolReport(TypedDict):
    """What a finished call is told, split by whether it is asked to act.

    ``blocking`` is what a gate still refuses about what landed, and each
    runtime delivers it as the feedback that stops the turn to be read.
    ``context`` is what the agent should know and need not act on — a
    directive the sweep removed, a name an edit still to come may supply —
    delivered beside the result. One label for both taught agents to read
    every notice as an order: a justified suppression reported as a
    "blocking error" left the agent unable to tell whether anything was
    asked of it.
    """

    blocking: list[str]
    context: list[str]


class ImportBoundaryRow(TypedDict):
    """Module families whose dependencies belong in declared repository roots."""

    modules: list[str]
    owners: list[str]
    source_roots: list[str]
    rule_id: str
    message: str


class AntiPatternRow(TypedDict):
    """One erased anti-pattern rule and the syntactic context it inspects."""

    id: str
    pattern: str
    message: str
    context: str
    matcher: str
    """The AST selector this rule declares, or ``""`` where it declares none.

    Named rather than carried, because a row crossing into the hermetic
    runtime is primitive and a callable is not. The association lives at the
    declaration and travels here, so the gate resolves a rule's selector from
    the row it is already matching on instead of from a second list of ids
    that has to be kept in step with it. Where a row names one and the source
    parses, the selector decides the rule and ``pattern`` is not consulted;
    where the source will not parse, the pattern is all there is and it
    decides alone.
    """
    strength: str
    """"strong" when no directive may silence this rule, "soft" when one may.

    The audit refuses a directive on a strong rule; the hook has to refuse the
    same one, or an edit the hook admits is an edit `dev check` then rejects.
    """
    resolution: str
    """"required" when this rule's verdict turns on a resolved declaration.

    The tree is wider than the defect for these, and what settles the
    difference is what a receiver's declaration resolves to. The kernel never
    resolves anything itself — it is a pure function of its inputs — so the
    answer arrives as an input, from whatever the dispatcher was able to run.
    This flag is what tells the dispatcher an answer is worth paying for, and
    almost no edit trips a rule carrying it. Denying one unresolved states a
    verdict the audit then contradicts, and the two block on opposite states
    with no version of the file passing both. So the gate says what it knows:
    resolved, it decides; unresolved, it asks.
    """
    roles: list[PathRoleName]
    """The path roles whose files this rule judges.

    Production for most: a convention about how code reads is about the code
    other code reads. A rule about how prose is written also reaches a test,
    whose docstring is read as the spec of what the test pins. Declared on
    the row rather than decided by the gate, because which roles a rule is
    about is a fact about the rule.
    """


class RefusedToolRow(TypedDict):
    """One erased refusal of a native call, and where to go instead.

    ``specifier`` is ``""`` when the whole tool is refused, and otherwise the
    subject that selects one of its uses — the ``artifact-design`` in
    ``Skill(artifact-design)``. ``reason`` says what the call would have done
    and ``recovery`` names the surface to reach for instead, so a refusal is
    never only a refusal.
    """

    tool: str
    specifier: str
    reason: str
    recovery: str


class RefusedPathRow(TypedDict):
    """One erased set of paths no shell command may name, and where to go instead.

    ``paths`` are patterns a word is matched against, ``exempt`` the ones
    beneath them that stay reachable, and :mod:`lup.policy.kernel.withheld`
    says how either is read. ``reason`` says what naming one would have done
    and ``recovery`` what reaches the same end, as a tool refusal does.
    """

    paths: list[str]
    exempt: list[str]
    reason: str
    recovery: str


class RunnerTargetRow(TypedDict):
    """One erased ``uv run <target>`` a project judges, and how.

    ``sandbox`` is the same axis :class:`ShellRuleRow` carries, on the one
    surface a command row cannot reach: ``uv`` is parsed rather than matched,
    so a target's placement has nowhere else to be declared. ``effects``,
    ``refuses`` and ``reason`` are there for the same reason — a target a
    project means to refuse has nowhere else to say so, and leaving it off the
    table is not a refusal but an absence of one: the verdict becomes no
    judgment, which a confined session leaves to the runtime's own
    permissions. For a target that spends money or runs for an hour, not
    refusing is precisely what the declaration existed to prevent.

    Those two are the pair :class:`ShellRuleRow` carries, read by the same
    :func:`~lup.policy.kernel.effects.declared_verdict`. A target stating a
    verdict outright would say what it earns rather than what it does, and a
    target with subcommands would then say both — once here and once in the
    effects the command rows beneath it are judged by.
    """

    name: str
    sandbox: SandboxPlacement
    effects: list[EffectRow]
    refuses: str
    reason: str
    recovery: str


type RunnerTargetField = Literal[
    "name", "sandbox", "effects", "refuses", "reason", "recovery"
]
"""Every field one erased runner target carries.

Closed and enumerable on the same terms as :data:`ShellRowField`, and for the
same reason: the generated dispatcher indexes these keys inside a hook, where
a missing one is a permission that never happens.
"""


def runner_target_values(
    row: RunnerTargetRow,
) -> dict[RunnerTargetField, str | list[EffectRow]]:
    """Every field of one erased runner target, as a mapping, in order.

    The arrangement :func:`shell_row_values` makes for the row beside this one,
    and for the reason that one gives. A renderer spelling the fields itself is
    a second enumeration of the shape, kept in step by nobody.
    """
    return {
        "name": row["name"],
        "sandbox": row["sandbox"],
        "effects": row["effects"],
        "refuses": row["refuses"],
        "reason": row["reason"],
        "recovery": row["recovery"],
    }


class ShellRuleRow(TypedDict):
    """One erased shell-command rule the kernel matches by executable name.

    ``subcommand`` and ``operation`` are ``""`` at the levels a rule does not
    constrain; ``ask_flags`` downgrades an ``allow`` to ``ask`` when one of the
    named flags appears among the command's remaining words. On the
    command-level row of a subcommand-gated command, ``ask_flags`` guard the
    global options before the subcommand and ``value_flags`` name globals that
    consume the following word (``git -C <path>``), so a flag value is never
    read as the subcommand. ``allow_flags`` name a pure read-only form of a
    non-allow row (``ssh-add -l``): the row de-escalates to allow only when
    every remaining word is exactly one of the named flags, so clusters,
    ``=`` values, paths, and unresolved expansions never qualify.
    ``directory_flags`` name the globals whose value is the directory the
    command runs its paths from, which is a smaller set than ``value_flags``:
    both consume a word, and only these move where an operand resolves.
    ``read_verbs`` name action-selecting flags of a command that enforces one
    action at a time (``git config --get``): a non-allow row de-escalates to
    allow when a declared verb appears among words that are all literal and
    free of guarded flags, because the verb pins the invocation to its query
    action regardless of the other words. ``probe_flags`` name the flags after
    which the command performs nothing (``git push --dry-run``): unlike a read
    verb, a literal probe flag stands even beside guarded flags and refspec
    grammar, because what those guard is an effect the probe form does not
    perform — so a non-allow row de-escalates to allow, and an allow row keeps
    its verdict past its ``ask_flags`` and ``ask_refspecs``. Destination
    grammar still asks: a probe still contacts the repository it names, and
    where the work would land is guarded as a place, not as a write.
    ``frozen_flags`` name the flags that pin a dependency restore to what its
    lockfile already declares (``bun install --frozen-lockfile``): a non-allow
    row de-escalates to allow when one appears among literal words free of
    guarded flags, because a frozen restore fetches nothing the lock does not
    pin by integrity hash — the restore ``uv run`` performs unasked.
    ``write_markers`` are the same
    de-escalation stated negatively, for a command whose read-only form is the
    one with nothing extra in it (``dd if=x`` with no ``of=``): a non-allow row
    de-escalates when no literal word carries a declared marker. Stated as
    absence because no membership test can recognize a form defined by what it
    lacks -- the gap that stopped every read-only ``dd`` as a write.
    ``bare_reads`` is the end of that same line: a command whose reading form
    carries no words at all (``mount`` alone lists what is mounted, and every
    acting form names something). Absence cannot be tested against a marker
    here because there is nothing to test, so the emptiness itself is what the
    row declares. It is stated per command rather than inferred from the other
    fields, since a bare invocation is an action for plenty of commands --
    ``ssh-add`` with no words adds the default key.

    ``guarded_keys`` states the same absence test about a write's *subject*
    rather than its form. ``write_markers`` asks whether a command writes at
    all; this asks whether what it writes is one of the settings that decide
    how later commands execute (``git config core.hooksPath``), and holds the
    row's effect only for those. Glob patterns matched case-blind, because
    the families worth guarding are shaped around a subsection
    (``merge.*.driver``) and git reads a key's section and name without
    regard to case. Without it a row can only ask about every write it
    judges, which makes its stated reason false for most of them -- and a
    question whose reason does not hold is one nobody can answer well.

    ``read_operands`` and ``read_options`` state the reading form a guarded
    subject still has: `git config core.hooksPath` names the guarded key and
    only looks it up, because the value that would set it is absent. So a
    non-allow row de-escalates where its legible words name no more operands
    than the count, and every option among them is one the list allows --
    an allowlist, since the options that write (`--unset`, `--add`) are the
    ones a later release adds to.

    ``setting_flags`` and ``guarded_settings`` state the same absence test
    about the *globals* a subcommand-gated command reads before its verb.
    ``git -c <key>=<value>`` and ``git config <key> <value>`` set the same
    setting, and without these only the second could say which settings its
    question is about — so every `-c` would ask, with a stated reason, that
    git config can change how commands execute, false of `-c color.ui=false`
    and every other display setting the guidance here asks for by name. The
    keys are the pair of ``guarded_keys`` and the flags say which globals
    carry one; they are a separate column rather than that one because a
    command-level row of a gated command is also what an unclassified verb
    falls back to, where an absence test over the whole argument list would
    turn ``git something-new`` into an allow.

    ``outward_settings`` are the guarded settings whose effect leaves the
    session's own machine: a credential helper hands a lent secret to a
    program, and a URL rewrite sends a push somewhere no destination guard
    read. The rest of ``guarded_settings`` run a program where the session
    runs, which a container holds; these keep their question inside one too.

    ``write_flags`` name the options whose value is a path the command writes
    — ``sort -o``, ``yq -i``, ``git log --output``. Among the ``ask_flags``
    they would leave that list holding two unlike things: a flag that lands a
    file and a flag that runs a program both escalate the row, so one column
    decides both and neither can be read for what it is. It shows as
    `sort -o out.txt` asking while `sort f > out.txt` allows, and as
    `base64 -o README.md` allowing while `echo x > README.md` asks — the same
    file, written two ways, answered by whether a checkpoint happens to
    discharge the row.

    Separated so the path can be resolved and judged as a write, by the row
    every other spelling of a write reaches. What stays in ``ask_flags`` is
    everything that escalates for some other reason: ``--compress-program``
    runs a program, ``--repo`` retargets a remote, ``--delete`` removes a ref.

    ``flag_effects`` is what a guarded flag *adds* to what the row does, as
    against which spellings guard it. Escalating on a flag and describing the
    escalation are two different statements, and a row stating only the first
    describes the harmless operation: ``git reset`` mutates the repository
    reversibly, and ``git reset --hard`` also discards working-tree content the
    object store never held — one row, two operations. An escalated verdict
    would then be a question whose subject nothing in the table names, its
    subject guessed from ``checkpoint``.

    One list for every flag the row guards, because a row guards one kind of
    thing: ``--hard``, ``--merge`` and ``--keep`` all discard working-tree
    content, and a flag adding something else belongs to a rule that says so
    rather than to a longer list here. Empty is the common case — most guarded
    flags escalate an operation whose effects already describe them.

    ``ask_destinations`` names the forms of inline repository this row asks
    about, read off the first operand that is not a flag — the one word a
    push takes to say where it lands. A destination reached through the
    remote table is a destination somebody approved, because every way of
    putting one in that table asks; a URL or a path in the command line
    reaches a repository the table never heard of, and no key guard can see
    it because there is no key and no configuration write. Declared as forms
    rather than as spellings for the reason below it: the transports are
    open-ended and the grammar is not.

    ``ask_refspecs`` names the effects a refspec operand may carry that this
    row asks about — the same downgrade ``ask_flags`` states, about a word
    whose grammar rather than whose spelling says what it does. A push
    removes a remote ref by flag (``--delete``) or by empty source
    (``:refs/heads/main``), and replaces one non-fast-forward by flag
    (``--force``) or by leading plus (``+main:main``); a row that guarded
    only the flags held half of each effect. Declared as effects rather than
    as more spellings because the second list of spellings is what missed
    these.

    ``force_flags``, ``lease_flags`` and ``protected_refs`` judge a forced
    update, which is safe or not according to what it can discard. A
    ``lease_flags`` spelling (``--force-with-lease``) forces only while the
    remote still holds what this checkout last saw of it, so it cannot discard
    work somebody else pushed; a ``force_flags`` spelling (``--force``), or a
    refspec's leading plus, forces past any lease, so asks. A leased force
    still asks where it names a ref in ``protected_refs`` -- a branch other
    people build on, where rewriting is the loss whatever the lease holds --
    or names no ref at all, because the one it reaches is then the checkout's
    current branch, which a hermetic reader cannot see. ``value_flags`` on a
    subcommand row name the options whose value is the next word, so the
    operand reading here steps over it rather than taking it for a repository
    or a refspec.

    ``sandbox`` says where this command has to run, independently of who
    decides it: a verb that reaches a remote is unusable confined however the
    effect reads, and a verb whose blast radius wants the OS boundary keeps it
    however ordinary the effect reads.

    ``recovery`` says what would put back what this command destroys, which
    is what makes the effect a function of the session rather than of the
    command alone: an approval question exists because a loss is permanent,
    and a session carrying the restorer named here has no such loss to ask
    about.


    ``rule`` is this row's stable id, derived from the levels it matches —
    ``shell:git.push``, ``shell:rm`` — rather than written down, because a
    derived id cannot drift from the row it names and a written one has to be
    kept in step with a rename. It is what an audit counts by and what a
    person answering the same question twice is pointed at; the reason beside
    it is prose, and a taxonomy built on prose is a taxonomy of phrasings.

    ``reviewer`` is who may answer a question this row produces. It cascades
    down the nesting like the axes above, so a level says once who answers for
    the verbs beneath it and only the ones that differ say otherwise.

    Every axis arrives already resolved down the nesting, so matching one
    row is the whole answer. ``effects_source``, ``sandbox_source`` and
    ``checkpoint_source`` say which level supplied each value, which is what a
    reader needs at a verdict they did not expect; none is consulted in
    reaching one.

    ``effects`` is what this row says the invocation *does*, and it is the
    whole of what the row states about its verdict. It carried a second column
    saying what that earns, written beside it and agreeing with it on every one
    of the table's rows -- which is one judgement recorded twice, and the drift
    this model exists to make unrepresentable. What a row earns is derived
    where it is used instead, against evidence the row cannot hold: whether the
    target exists, whether git tracks it, whether a boundary confines this
    session. That is the question a table of declared verdicts has to guess at.

    A row stating no effects allows, which is the reading of a rule that
    positively says this does nothing worth guarding. Saying it is the point:
    ``ShellCommandRule.effects`` is required for the reason a stated verdict
    would be, so a command nobody classified is a gap a reader sees rather
    than a grant nothing wrote down.

    ``refuses`` is the one verdict those effects do not reach, and it is
    stated apart from them because it is not about them. ``pip install`` takes
    the same dependency ``uv add`` takes; ``eval`` runs code the agent could
    have written out; ``git checkout -- <path>`` discards what ``git restore``
    discards. Each refusal is a route this project prefers, so the row names
    the route -- and one that spelled the preference as an effect instead
    would have the operation claiming to do something it does not, which is
    the drift the effects exist to end. Empty is the common case: derive the
    verdict, and say nothing about how it was spelled.
    """

    rule: str
    command: str
    subcommand: str
    operation: str
    operation_path: list[str]
    operator_only: bool
    sandbox: SandboxPlacement
    sandbox_source: RuleLevel
    checkpoint: CheckpointRequirement
    checkpoint_source: RuleLevel
    reviewer: ReviewerRequirement
    effects: list[EffectRow]
    effects_source: RuleLevel
    refuses: str
    ask_destinations: list[str]
    ask_refspecs: list[str]
    force_flags: list[str]
    lease_flags: list[str]
    protected_refs: list[str]
    ask_flags: list[str]
    flag_effects: list[EffectRow]
    write_flags: list[str]
    allow_flags: list[str]
    read_verbs: list[str]
    probe_flags: list[str]
    frozen_flags: list[str]
    write_markers: list[str]
    guarded_keys: list[str]
    read_operands: int
    read_options: list[str]
    setting_flags: list[str]
    guarded_settings: list[str]
    outward_settings: list[str]
    landing_operands: int
    landing_flags: list[str]
    bare_reads: bool
    value_flags: list[str]
    directory_flags: list[str]
    reason: str
    recovery: str


type ShellRowField = Literal[
    "rule",
    "command",
    "subcommand",
    "operation",
    "operation_path",
    "operator_only",
    "sandbox",
    "sandbox_source",
    "checkpoint",
    "checkpoint_source",
    "reviewer",
    "effects",
    "effects_source",
    "refuses",
    "ask_destinations",
    "ask_refspecs",
    "force_flags",
    "lease_flags",
    "protected_refs",
    "ask_flags",
    "flag_effects",
    "write_flags",
    "allow_flags",
    "read_verbs",
    "probe_flags",
    "frozen_flags",
    "write_markers",
    "guarded_keys",
    "read_operands",
    "read_options",
    "setting_flags",
    "guarded_settings",
    "outward_settings",
    "landing_operands",
    "landing_flags",
    "bare_reads",
    "value_flags",
    "directory_flags",
    "reason",
    "recovery",
]
"""Every field name one erased shell row carries.

Closed and enumerable, which is what lets the mapping below be typed by its
keys rather than by "some string": a field renamed on the shape and not here
is a type error, where a bare string key would be a ``KeyError`` raised inside
a hook — and a permission that never happens looks exactly like one granted.
"""


def shell_row_values(
    row: ShellRuleRow,
) -> dict[ShellRowField, str | bool | int | list[str] | list[EffectRow]]:
    """Every field of one erased row, as a mapping, in declaration order.

    Declared beside the shape rather than at the renderer that needs it, so a
    column added above and not mapped here is one screen apart instead of two
    files — and the test that compares these keys against the shape's own
    annotations turns "one screen apart" into a failure rather than a habit.

    The generated dispatcher indexes every one of these keys, and it does so
    inside a hook, where a missing key is a permission that never happens. So
    the mapping is total by construction and checked as total by that test.
    """
    return {
        "rule": row["rule"],
        "command": row["command"],
        "subcommand": row["subcommand"],
        "operation": row["operation"],
        "operation_path": row["operation_path"],
        "operator_only": row["operator_only"],
        "sandbox": row["sandbox"],
        "sandbox_source": row["sandbox_source"],
        "checkpoint": row["checkpoint"],
        "checkpoint_source": row["checkpoint_source"],
        "reviewer": row["reviewer"],
        "effects": row["effects"],
        "effects_source": row["effects_source"],
        "refuses": row["refuses"],
        "ask_destinations": row["ask_destinations"],
        "ask_refspecs": row["ask_refspecs"],
        "force_flags": row["force_flags"],
        "lease_flags": row["lease_flags"],
        "protected_refs": row["protected_refs"],
        "ask_flags": row["ask_flags"],
        "flag_effects": row["flag_effects"],
        "write_flags": row["write_flags"],
        "allow_flags": row["allow_flags"],
        "read_verbs": row["read_verbs"],
        "probe_flags": row["probe_flags"],
        "frozen_flags": row["frozen_flags"],
        "write_markers": row["write_markers"],
        "guarded_keys": row["guarded_keys"],
        "read_operands": row["read_operands"],
        "read_options": row["read_options"],
        "setting_flags": row["setting_flags"],
        "guarded_settings": row["guarded_settings"],
        "outward_settings": row["outward_settings"],
        "landing_operands": row["landing_operands"],
        "landing_flags": row["landing_flags"],
        "bare_reads": row["bare_reads"],
        "value_flags": row["value_flags"],
        "directory_flags": row["directory_flags"],
        "reason": row["reason"],
        "recovery": row["recovery"],
    }


type DestinationForm = Literal["url", "path"]
"""How an operand names the repository a command sends its work to.

The two ways of naming one inline, kept apart because a project can
reasonably guard them separately: a URL leaves this machine, and a path stays
on it. A remote this repository has configured is neither, and is spelled as
absence rather than as a third word — a table declares the forms it asks
about, and the bare name every ordinary push carries is the one it does not.
"""


type RefspecEffect = Literal["delete", "force"]
"""What a refspec operand can do to the ref it names, beyond writing it.

The two effects a push spells twice — once as a flag and once as grammar.
Named so a table declares the effect it guards rather than a second list of
the spellings that reach it.
"""


type EditOperation = Literal["create", "overwrite", "modify", "delete"]
"""What a change does to the file it names.

Carried from the adapter rather than inferred, because the two whole-file
operations are indistinguishable once a preimage is resolved: a ``Write`` over
an existing file and an ``Edit`` that happens to replace every line both
arrive as one whole document replacing another. The native call knows which it
was, so it says.
"""


class EditRuleRow(TypedDict):
    """One erased edit rule: which changes it speaks about, and what it says.

    ``gates``, ``suffixes``, ``roles`` and ``operations`` are the axes a rule
    may constrain, each empty where the rule is silent about it — so matching
    a row is four containment tests and nothing composed. Unlike
    :class:`ShellRuleRow`, which is matched by name and answers alone, these
    rows overlap deliberately and the *last* one matching decides, the way
    `.gitignore` reads: a broad statement first, its exceptions after it.

    ``effect`` is ``""`` where a rule moves only the threshold, and
    ``maximum_added_lines`` is ``None`` where it moves only the verdict. The
    two are separate fields because a rule may state either without the other,
    and a single optional verdict could not say "leave who decides alone, but
    count differently here".
    """

    name: str
    gates: list[str]
    suffixes: list[str]
    roles: list[str]
    operations: list[str]
    effect: str
    maximum_added_lines: int | None
    reason: str


class PeerPolicyRow(TypedDict):
    """Where this project's sessions find each other, and what a sender is told.

    ``store`` says where the roster lives — parts beneath the repository's
    shared git directory rather than a joined path, because the dispatcher
    rebuilds it with the host's own separator and a compiled literal carrying
    one platform's answers on one platform. What is *in* that directory is not
    carried: the fold the dispatcher reads it with ships beside the dispatcher
    and owns every file name, so a row restating them would be the second
    spelling that can drift. ``windows_dir`` is the exception, being the one
    place under the store nothing but the dispatcher writes or reads.

    ``send_reason`` says why a send was stopped and ``send_recovery`` names the
    surface reaching the same peer durably, so a sender is never only refused.
    ``claim_reason`` is what an approver of a write into a held path reads, and
    ``claim_recovery`` what the writing agent can do about the holder.
    ``operator`` is the holder name the person watching is held under, and
    ``operator_reason`` what an approver reads where they hold the path.
    ``listing_note`` frames the roster attached to a listing that speaks for a
    wider population, so a reader can tell the two apart.

    No native tool name is carried. Which call sends and which lists is what a
    runtime spells for itself, and the branch recognizing one belongs to that
    runtime's own dispatcher — this row answers what to do once it has.
    """

    store: list[str]
    windows_dir: str
    member_env: str
    send_reason: str
    send_recovery: str
    listing_note: str
    claim_reason: str
    claim_recovery: str
    operator: str
    operator_reason: str


class PathWord(TypedDict):
    """One word of a command that a reader read a path out of.

    What a reader knows and would otherwise throw away: not only *which* file
    a command names, but which word names it and how that word spells it. Both
    halves are needed to put a path word back after resolving it -- ``at``
    says which word, and ``prefix`` is whatever the word carries before the
    path, so ``--output=dist/x`` is rewritten at its value and a bare operand
    whole.

    A word, not a file. A verb that derives a path from an operand rather
    than reading one -- ``gzip f`` authoring ``f.gz`` -- names ``f`` here and
    nothing else, because ``f.gz`` is not in the command to be rewritten. The
    derivation comes out right anyway: every reader takes the command's words
    afresh, so one whose operand has been resolved derives from the resolved
    one.
    """

    at: int
    prefix: str
    path: str


UnproducedCause = Literal["missing", "irregular", "unreadable", "refused", "run"]
"""Why running a screened script over one file produced no after-document.

A cause and not a sentence, because the host establishes these and the kernel
words them -- the same division ``resolution`` crosses the boundary by. Five,
because each sends its writer somewhere different: ``missing`` names no file
at that path, ``irregular`` a path holding something an in-place rewrite
cannot replace, ``unreadable`` a file whose text could not be read either
before or after, ``refused`` a script sed itself would not run, and ``run`` a
file an earlier step of the same line writes with what only running it makes.
"""


class UnproducedDocumentRow(TypedDict):
    """One file an in-place rewrite names that the host could not produce.

    The difference between "nothing was read" and "this is why", which the
    classifier acts on: a target with neither a document nor a row here was
    never looked at by anything, and that stays a question a composition
    cannot forget its way past. A target with this row *was* looked at, so
    the refusal can say what stopped it and what to do instead.
    """

    target: str
    cause: UnproducedCause


class RewriteReading(TypedDict):
    """Every in-place rewrite a command names, each produced or each explained.

    The two travel together because they are one reading: a target reaches
    exactly one of them, and a caller that computed the documents has, by
    construction, computed the reasons for the rest. Splitting them at the
    classifier's door is safe in the direction that matters -- a composition
    passing the documents and not these leaves the classifier saying only
    that nothing read the rewrite, which is the answer it gives when nothing
    did.
    """

    documents: list[RewrittenDocumentRow]
    unproduced: list[UnproducedDocumentRow]


def unproduced_cause(reported: str | None) -> UnproducedCause:
    """The kernel's own literal for what the host reported about a rewrite.

    The host half may name no kernel type, so what stopped it reading a file
    crosses as the word it read off its own reading; this is the one place
    that word becomes the value the classifier words, and both halves narrow
    it here rather than each keeping a copy of the mapping.

    A spelling this does not know reads as ``unreadable``, the least specific
    of them, because a refusal that says less is still a refusal — and a
    classifier that raised instead would turn an unknown reading into a
    crashed hook, which grants rather than refuses.
    """
    match reported:
        case "missing" | "irregular" | "unreadable" | "refused" | "run":
            return reported
        case "directory":
            return "irregular"
    return "unreadable"


def landing_rows(pairs: list[list[str]]) -> list[TargetLandingRow]:
    """The host's ``[path, landing]`` pairs as rows, each landing read as a word.

    The host half returns plain data, since it may reach nothing of the
    kernel's. A landing that is not one of the three words is read as ``host``
    -- the place nobody can vouch for -- and so is a pair of the wrong shape,
    under a path no target spells: dropping it would leave the operation
    with one fewer path that could have kept its question.
    """

    def row(pair: list[str]) -> list[TargetLandingRow]:
        match pair:
            case [path, "container" | "checkout" | "host" as lands]:
                return [TargetLandingRow(path=path, lands=lands)]
            case [path, _]:
                return [TargetLandingRow(path=path, lands="host")]
            case _:
                return [TargetLandingRow(path="$", lands="host")]

    return [landed for pair in pairs for landed in row(pair)]
