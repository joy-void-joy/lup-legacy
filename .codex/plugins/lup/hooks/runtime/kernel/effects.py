"""What an operation does, and the one table that says what that earns it.

A lattice keyed on how a command is spelled has a rule name an executable
and state a verdict, so two commands with the same effect reach different
answers whenever two people write the two rules -- `echo x > f` allowed,
`tee f < g` asking, `sed -i` allowed once recoverability is proven, and the
Edit tool running three gates none of them reaches. Each is defensible
alone. Together they are not a policy, because nothing in the model says what
they have in common.

What they have in common is the effect. So a rule declares effects and this
derives the verdict, which makes the divergence unrepresentable rather than
merely fixed: there is one row for "replace the contents of a tracked source
file" and every spelling that does it reaches that row.

Read against the threat this actually guards. The agent is over-eager, not
hostile -- an approval question is how a person comes to hear about something
surprising, not a barrier a determined process could not walk around. That is
why the table spends questions on the unnecessary rather than on the dangerous,
and why a refusal is cheap here: a denial costs the agent one turn and a
reshape, and costs the person nothing at all.
"""

from .decision import DecisionEffect, SandboxPlacement
from .semantics import REACHES, Reach, ReviewPurpose

from typing import Literal, TypedDict


class EffectRow(TypedDict):
    """One declared effect, in the shape a compiled rule table can hold.

    Data rather than an object because the erased rows are serialized into
    ``policy_data.py`` and read back by a script with no ``lup`` importable.
    The behaviour lives in the member classes below, which this names.

    ``reach`` is where the harm this effect's question guards against lands,
    one of :data:`~lup.policy.kernel.semantics.REACHES`. A string rather than
    the literal for the reason the other axes are: the compiled table is read
    back as plain data, and :func:`declare` is where the word is checked.
    """

    kind: str
    scope: str
    write: str
    reviewed: bool
    reason: str
    reach: str


type EffectRowField = Literal["kind", "scope", "write", "reviewed", "reason", "reach"]
"""Every axis name one declared effect carries.

Closed and enumerable on the same terms as
:data:`~lup.policy.kernel.rows.ShellRowField`: an axis renamed on the shape
above and not here is a type error, rather than a key missing from the table a
hook reads.
"""


def effect_row_values(row: EffectRow) -> dict[EffectRowField, str | bool]:
    """Every axis of one declared effect, as a mapping, in declaration order.

    The arrangement :func:`~lup.policy.kernel.rows.shell_row_values` makes for
    the row that carries these, and for the same reason. What renders the
    compiled table needs each field beside its name, and reading them off the
    mapping directly hands back ``object`` -- which the renderer answered by
    coercing every one with ``str``, and so rendered the first boolean axis as
    ``"False"``: a true value in the table a dispatcher reads back.
    """
    return {
        "kind": row["kind"],
        "scope": row["scope"],
        "write": row["write"],
        "reviewed": row["reviewed"],
        "reason": row["reason"],
        "reach": row["reach"],
    }


class EffectEvidence:
    """What the host measured about the paths and placement one effect touches.

    Named fields rather than a bag, because every one of them is a fact some
    row reads and no row should be able to reach a fact nobody supplied. A
    default here is the conservative reading, so a caller that cannot measure
    something gets the answer it would have got before it could.
    """

    def __init__(
        self,
        contained: bool = False,
        tracked: bool = False,
        existing: bool = False,
        captured: bool = False,
    ) -> None:
        self.contained = contained
        """Whether a measured boundary confines this session's effects."""
        self.tracked = tracked
        """Whether git holds the path, so a reviewer could diff the change."""
        self.existing = existing
        """Whether the path is already on disk, which separates create from overwrite."""
        self.captured = captured
        """Whether a snapshot holds what this would replace."""


class Effect:
    """One thing an operation does, and what it earns.

    A union that answers through its members. Every member states its own
    verdict from evidence and placement, so adding a kind of effect adds a
    class rather than a branch in a function that already knows too much.
    """

    kind = ""
    """The name a declared row uses to reach this member."""

    scopes: list[str] = []
    """The scope words this member's verdict reads, empty where it reads none.

    Stated so that :func:`declare` can check one, for the same reason it
    checks the kind. A member that branches on scope ends its branches with a
    permissive fall-through -- there is nothing else it could do with a word
    it does not know -- so a misspelling silently earns the mildest verdict on
    the list, which is the one failure shape this model exists to refuse. The
    word is checked where it is written instead.

    Empty means the scope is a label this member carries without reading:
    which container noun was touched, which external class was reached. There
    is no closed set to check those against, and inventing one would be
    checking prose rather than a decision.
    """

    writes: list[str] = []
    """The write words this member's verdict reads, on the same terms as scopes.

    Only :class:`WritesPath` reads one. It is declared here rather than there
    because ``EffectRow`` carries the axis for every member, and an axis a
    row can state is an axis some member has to be able to refuse.
    """

    observes = False
    """Whether this member leaves the world exactly as it found it.

    Not the same question as the verdict, and the difference is what makes it
    worth a column. A verdict says whether anybody is asked, and plenty of
    mutations are allowed for being reversible -- a commit is undone by the
    reflog, so it earns ``allow`` while changing the repository. A caller that
    needs to know whether an operation *changes* anything and reads the
    verdict instead gets "yes" for a read and for a commit alike.

    Read by the guard on a directory redirect, whose whole premise is that a
    verb's reversibility is a fact about the tree it runs in: what survives
    being pointed at another tree is exactly what does nothing to either.

    Stated by the member, because the member is the thing that knows. Kept as
    a class attribute rather than a set of kind names somewhere else, which
    would be a second statement of the table free to disagree with it.
    """

    reviewable = False
    """Whether this member's verdict reads the route axis, on the same terms.

    The axis is a fact about the *rule* rather than about a path, so it is
    stated where the rule is written. Nothing else here reads it, and a member
    that does not would take ``reviewed=True`` in silence -- a declaration
    saying "the gates see this" against a verdict that never asked.
    """

    reach: Reach = "host_later"
    """Where the harm this member guards against lands, unless a row says.

    The widest word is the default, because a member nobody placed is one
    whose question should hold wherever the session runs: a reach written too
    narrow relaxes a question inside a container, and one written too wide
    only keeps asking it.
    """

    def default_reach(self, scope: str) -> Reach:
        """Where this member's harm lands for one scope, before a row overrides it.

        The member's own word for every scope unless the scope changes what is
        at stake, which is a fact about the member rather than the row.
        """
        return self.reach

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        """What this effect earns, given what the host measured."""
        raise NotImplementedError

    def reason(self, row: EffectRow) -> str:
        """What to tell somebody who met this effect's verdict."""
        return row["reason"]

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """Which kind of decision this effect asks a person to make.

        Stated by the member rather than derived from the verdict's other
        columns. A shell row inferring it from ``checkpoint`` and
        ``effect_class`` works only while those two columns happen to imply
        it, and says nothing at all for a row that sets neither. The effect
        knows, because the effect is the thing being weighed.
        """
        return None


class ChangesNothing(Effect):
    """An operation that does nothing this table guards, said instead of omitted.

    ``cd`` is the whole of it today. Navigation moves no bytes, reaches no host
    and leaves behind nothing a later command could tell was there -- so every
    other member would be false of it, and the accurate declaration is the
    empty one.

    Empty is the one thing it cannot be. A rule stating no effects and a rule
    stating this one both allow, and only one of them has been thought about;
    an audit for the rules nobody finished reads the table for silence, so a
    silence meaning "deliberately nothing" has to be spelled to be told apart
    from the silence meaning "nobody has got to this yet". That is the argument
    :class:`MutatesRepository` makes for staging and committing, and it does
    not stop at the operations that do something.
    """

    kind = "changes_nothing"
    reach: Reach = "container"
    observes = True

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "allow"


class ReadsPath(Effect):
    """Reading something. Free inside the checkout, a question outside it.

    Containment is the whole of the difference. A read of a path outside the
    checkout is confined by the sandbox to the sandbox, so inside one it costs
    nobody anything and asking would be noise on every `cat /etc/hostname`.
    Unsandboxed the same read reaches the real machine, and an agent wandering
    into ``/etc`` or a home directory is exactly the over-eagerness the
    question exists to interrupt.

    A credential is the exception at both placements, because containment does
    not help: reading key material into an agent's context *is* the disclosure,
    and no boundary puts it back.
    """

    kind = "reads_path"
    reach: Reach = "container"
    observes = True
    scopes = ["project", "outside", "secret"]
    """Anywhere the checkout covers, anywhere it does not, and key material.

    A read does not distinguish the trees a write does: reading scratch and
    reading reviewable source are the same act with the same answer, so both
    are ``project`` rather than two words this verdict would treat alike.
    """

    def default_reach(self, scope: str) -> Reach:
        """Key material is a lent secret wherever it is read from."""
        return "credential" if scope == "secret" else self.reach

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        if row["scope"] == "secret":
            return "ask"
        if row["scope"] == "outside" and placement != "inside":
            return "ask"
        return "allow"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """Access, at both the scopes that ask."""
        return "sensitive_access"


class WritesPath(Effect):
    """Writing something, by whichever spelling reaches the path.

    The row this module exists for. Redirection, ``tee``, an in-place stream
    edit and the Edit tool all arrive here, so the answer cannot depend on
    which one a session happened to reach for.

    **Replacing reviewable content is refused, not asked.** A refusal costs a
    person nothing and routes the write through the tool whose gates read
    content -- the note gate, the size budget, the anti-pattern audit -- which
    is the arrangement that already works. Asking instead would spend somebody's
    attention to arrive at a change nobody reviewed.

    Everything else a shell writes is allowed, and that is the common case by a
    wide margin. A create replaces nothing, an untracked path has nothing a
    reviewer would have seen, and a scratch tree is disposable by declaration --
    so ``python train.py > log.txt`` and ``cmd >> run.log`` never reach the
    refusal, and the conjunction that does is narrow on purpose.

    **A reviewed write allows here and is decided elsewhere.** The refusal is
    about bypassing the content gates, so a route that reaches them has nothing
    for this row to say: an ``Edit`` contributes ``allow`` and the note, size
    and anti-pattern gates reach their own verdict beside it. Since the join
    takes the strongest, the size budget's deferral still decides a long edit
    and the note gate's refusal still decides a deleted marker -- which is what
    keeps this row from quietly meaning "do not edit this file".
    """

    kind = "writes_path"
    reach: Reach = "mount"
    scopes = ["scratch", "production", "protected", "outside", "unbounded"]
    """Which tree the path belongs to, which is most of what the answer turns on.

    ``production`` is reviewable source. ``scratch`` is disposable by
    declaration. ``protected`` is a path a rule holds an approval question
    against by name. ``outside`` is anywhere the checkout does not cover.

    ``unbounded`` is the reading for a write whose target is not in the
    command. A compiler emits where a configuration file says, so no word of
    ``tsc`` names what it is about to replace and none of the four trees above
    can be the answer -- not because the path is unknown to everybody, but
    because it is unknowable to this gate, which decides before anything ran.
    A target spelled with an expansion is the same case: ``> a$X`` names a
    word, and the file it lands on is decided by a run this gate precedes.

    There is no ``secret`` here, though :class:`ReadsPath` has one. Reading key
    material is the disclosure and no boundary puts it back; writing over it
    destroys a local file like any other, which the trees above already answer.
    """

    writes = ["create", "overwrite", "append", "delete"]
    """What the write does to what was already there.

    The axis that keeps output redirection out of the way of source editing. A
    ``create`` replaces nothing and so has no prior content for a gate to read;
    ``overwrite`` and ``append`` both change what a reviewer would have seen.
    """

    reviewable = True
    """The one member that reads the route, because it is the one that refuses.

    ``reviewed`` says the route this write takes has gates that read what it
    wrote. An `Edit` reaches the note gate, the size budget and the
    anti-pattern audit before it lands; a shell write reaches the same gates
    afterwards, against the file itself, because its content is produced by
    running. A route with neither is what the refusal below is about.

    Declared rather than measured. Which gates a spelling passes through is
    fixed by the spelling, so it is known where the rule is written and not
    at the path -- and an axis only a code path could supply is one a
    *declared* row could never state, and without it `git apply` could not
    say that its result is read.
    """

    def default_reach(self, scope: str) -> Reach:
        """A protected path is one of the settings this policy runs from.

        Every other scope is a path, and where the write lands is what its
        targets say.
        """
        return "lup" if scope == "protected" else self.reach

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        if row["scope"] == "protected":
            return "ask"
        # Every relaxation below reads a path: which tree it is in, whether it
        # is already there, whether Git holds it. An unbounded write offers
        # none of them, and containment does not stand in -- the checkout is
        # inside the boundary too, so a confined session emits the same
        # unreviewed output at the same paths nothing named.
        if row["scope"] == "unbounded":
            return "ask"
        if row["scope"] == "scratch":
            return "allow"
        # Beyond the checkout the question is whose the path is, which a
        # placement cannot say: a container holds a directory the host never
        # lent and none it did. So the write asks, and the reach settles it
        # where the host measured every path it names as the container's own.
        if row["scope"] == "outside":
            return "ask"
        if row["write"] == "create" or not evidence.existing:
            return "allow"
        if row["reviewed"]:
            return "allow"
        if row["scope"] == "production" and evidence.tracked:
            return "deny"
        return "allow"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """Review where somebody is asked to look, loss where they weigh one."""
        if row["scope"] == "protected":
            return "quality_review"
        return "unrecovered_local_mutation"


class DestroysUncaptured(Effect):
    """Removing what no capture holds, which is the one loss nothing undoes.

    The snapshot deliberately does not capture ignored content: on this
    checkout that came to 592 MB against a 21 MB object store, so capturing it
    would write twenty-eight times the repository's history before every
    mutating command. ``git clean -fdx`` is therefore the command whose whole
    purpose is destroying what the safety net cannot restore, and it keeps its
    question at every placement.

    The scope says which of those a loss is, and it is the checkpoint column
    read as behaviour rather than as a requirement. A loss a boundary-wide
    capture holds is discharged by that capture actually existing; one nothing
    would hold -- a mode bit, an owner, anything the object store does not
    record -- keeps its question however many snapshots were taken, because
    none of them is holding the thing being changed.
    """

    kind = "destroys_uncaptured"
    reach: Reach = "mount"
    scopes = ["targeted", "boundary_wide", "unrecoverable"]
    """The checkpoint vocabulary, read here as what a loss is rather than as
    what a command must arrange before it runs."""

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        if row["scope"] == "unrecoverable":
            return "ask"
        return "allow" if evidence.captured else "ask"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """A loss, and the one kind no capture is holding."""
        return "unrecovered_local_mutation"


class Fetches(Effect):
    """Reaching a host over the network.

    A declared scope is this repository saying in advance that it talks to
    something, which is the review. An undeclared host asks rather than
    refusing: refusing taught the agent to work around the wall, and the
    question is the cheaper signal for what is usually a typo or a genuinely
    new source worth a person's glance.
    """

    kind = "fetches"
    scopes = ["declared", "undeclared"]
    """Whether the host appears in the scopes this repository declared."""

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "allow" if row["scope"] == "declared" else "ask"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """Something beyond this machine, which is what a host is."""
        return "external_consequence"


class ReadsEnvironment(Effect):
    """Reading machine state that is not a path and is not another host.

    What is running, which images exist, what the daemon reports about itself.
    :class:`MutatesEnvironment` already argues that this state is none of the
    things the other rows answer for; reading it is the same state, and the
    reading half needs its own word for exactly the reason the changing half
    does.

    Reaching for :class:`ReadsPath` instead, at ``outside`` scope, takes the
    nearest available word and the wrong one. That scope makes containment
    decide -- a read outside the checkout is confined by a boundary or it is
    not -- and containment has nothing to say about a socket query. So
    `docker ps` would be allowed by the verdict column while its own
    declaration derived a question, on the strength of a path it never
    touches.

    Allows at every placement, and the split is the same one the container
    surface already draws: a verb either reports on containers, images,
    volumes and the daemon, or changes one of them. Only the second is the
    moment worth seeing.
    """

    kind = "reads_environment"
    reach: Reach = "container"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "allow"


class MutatesEnvironment(Effect):
    """Changing machine state that is neither the checkout nor another host.

    A container started, an image removed, a volume written, a daemon
    reconfigured. None of it is a path this policy resolves, none of it is
    beyond this machine, and no snapshot holds any of it -- so the three rows
    that would otherwise answer all answer wrongly: it is not a write, not a
    publication, and calling it a loss would be false for half the verbs that
    reach it.

    Asks, because what makes it worth a word is not danger but scope. An agent
    reaching for the container runtime has stepped outside the work the
    checkout describes, and that is the moment worth seeing.
    """

    kind = "mutates_environment"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "ask"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """State outside the checkout that outlives the command."""
        return "sensitive_access"


class MutatesRepository(Effect):
    """Changing repository state in a way the repository itself can restore.

    Staging, committing, recording a note. These write, so calling them reads
    would be false, and they destroy nothing a later command cannot reach, so
    calling them losses would be false too -- the object store keeps what they
    replaced, and reaching it is an ordinary operation rather than a recovery.

    Declared rather than left silent. A rule that states no effect and a rule
    that states this one both allow, and only one of them has been thought
    about; a table where those look alike cannot be audited for the rules
    nobody finished.
    """

    kind = "mutates_repository"
    reach: Reach = "container"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "allow"


class ExternalMutation(Effect):
    """A change beyond this machine that no ordinary follow-up puts back.

    The home for every external class that is not simply offering work: a
    review approving in somebody's name, a deployment, a charge, a change to a
    repository's own security. Each asks, and the scope carries which one so a
    reader is told what they are approving rather than that something external
    happened.

    Kept as one member with a scope rather than as five classes, because the
    verdict is the same for all of them and the difference is what to say. Five
    classes would be five places to change the day that stops being true, and
    would read as five decisions when only one is made.
    """

    kind = "external_mutation"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "ask"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """Beyond this machine, whichever of the classes reached it."""
        return "external_consequence"


class Publishes(Effect):
    """Putting a branch or a request where other people can see it.

    Allowed, and deliberately. Pushing a branch and opening a request are how
    work is offered for review rather than how it takes effect -- both are
    reversible by a normal follow-up, and both are everyday operations whose
    interruption buys nothing. What lands it is :class:`Integrates`.
    """

    kind = "publishes"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "allow"


class Integrates(Effect):
    """Merging work into a branch other people build on.

    Allowed, like the push before it. A push offers and a merge lands, and
    landing is the last step of the workflow the session was set to finish
    rather than a decision taken away from it: the review the merge concludes
    happened on the request, and what the merge put on the branch a revert
    takes off again. Kept apart from :class:`Publishes` because it is a
    different act, and an audit reading which rows integrate should find them.
    """

    kind = "integrates"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "allow"


class ReachesHost(Effect):
    """Opening a session on another machine.

    Asks wherever it runs. The sandbox confines this process, not the host at
    the far end, and an agent that has decided it needs a shell somewhere else
    has left the work this repository describes.
    """

    kind = "reaches_host"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "ask"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """Another machine, which is as beyond this one as anything gets."""
        return "external_consequence"


class InstallsDependency(Effect):
    """Bringing third-party code into the tree for the first time.

    The sharpest question in the table, and the one asked for a reason nothing
    else here shares. Every other row guards what the agent does; this guards
    what the agent *trusts*. A package index is a supply chain, a compromised
    release is indistinguishable from a good one at the moment of install, and
    the window between publication and discovery is exactly when an eager agent
    reaches for a name it read somewhere.

    Arrival is the moment, so it is the moment that asks -- by whichever
    route. A package manager's own verb and an edit to the manifest that
    declares it are two; a clone, a release asset and a workflow artifact are
    three more, and they differ from the queries beside them in the table by
    where the bytes end up rather than by how far they travelled. What a query
    fetches is read once by the agent; what these fetch stays on the disk,
    where a build or an import reaches it after nobody is watching.

    ``scope`` names which route arrived, because that is what a person needs
    in order to weigh the question -- a lockfile sync and a stranger's gist
    are the same verdict and nothing like the same decision.
    """

    kind = "installs_dependency"
    reach: Reach = "dependency"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "ask"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """Trust, which is the axis nothing else in the table measures."""
        return "untrusted_dependency"


class MaterializesLockfile(Effect):
    """Turning a lockfile into code on disk, where the lockfile may move first.

    Asks alongside the install it completes: an install whose lockfile is not
    frozen resolves anew wherever the manifest moved, and what the index
    serves under a name today is not what was reviewed when the name was
    declared. The frozen spelling is the de-escalation ``frozen_flags``
    names -- it fetches nothing the lock does not pin by integrity hash,
    which is the restore `uv run` performs before running anything, unasked.
    """

    kind = "materializes_lockfile"
    reach: Reach = "dependency"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "ask"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """The same trust the install asked about, arriving by the other route."""
        return "untrusted_dependency"


class RunsDeclaredTarget(Effect):
    """Running something this repository declares as its own.

    Allowed without qualification. The development tooling is the everyday work
    of the project, already reviewed as source and already covered by the rest
    of this table through the effects it has -- a command that writes gets its
    answer from :class:`WritesPath` regardless of which entry point reached it.
    """

    kind = "runs_declared_target"
    reach: Reach = "container"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "allow"


class RunsUndeclaredProgram(Effect):
    """Running a program no rule in the vocabulary describes.

    Asks, and this is the row that makes the declared surface mean something.
    Not because an unknown binary is likely to be harmful, but because reaching
    for one is the observable form of the agent leaving the work -- the moment
    worth a person seeing, and usually the moment the agent notices it did not
    need the thing at all.
    """

    kind = "runs_undeclared_program"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "ask"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """Whether this is the right thing to be doing, which is a review."""
        return "quality_review"


class UnclassifiedOperation(Effect):
    """An operation that fell off the end of a surface this table enumerated.

    ``gh pr something-new``, a git subcommand nobody classified, a verb the
    container runtime grew last release. Refused rather than asked, and the
    difference from :class:`RunsUndeclaredProgram` -- which meets the same
    "nothing describes this" and asks -- is what was enumerated.

    A declared command's operation table is a *finished* list. Somebody read
    what the command does and wrote down each verb with its answer, so a verb
    missing from it is the enumeration saying no rather than the enumeration
    never having been attempted. An unknown program was never enumerated at
    all, and a sandbox confines whatever it turns out to do, which is why that
    one can ask and settle where this one cannot: what falls off ``gh`` or
    ``git`` reaches a remote the boundary does not cover, so containment has
    nothing to offer it.

    The refusal is also the cheaper half of the trade this table makes
    everywhere. Denying costs the agent one turn and tells it to name the verb
    it wanted; the verb is then classified once, in the table, where the next
    session inherits the judgement. Asking would spend a person's attention on
    the same question every time and leave the table no better informed.

    ``scope`` names the surface that ran out, so the refusal can say which
    enumeration to extend rather than that something was unrecognized.
    """

    kind = "unclassified_operation"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "deny"


class EscapesContainment(Effect):
    """Running outside the boundary the launch measured.

    Asks, because the placement is the thing being changed. Every other row in
    this table reads containment as an input; this one removes it, so no later
    row's answer means what it said.
    """

    kind = "escapes_containment"
    reach: Reach = "container"

    def verdict(
        self, row: EffectRow, evidence: EffectEvidence, placement: SandboxPlacement
    ) -> DecisionEffect:
        return "ask"

    def purpose(self, row: EffectRow) -> ReviewPurpose | None:
        """Changing the boundary every other row reads as its input."""
        return "policy_override"


EFFECT_MEMBERS: list[Effect] = [
    ChangesNothing(),
    ReadsPath(),
    WritesPath(),
    DestroysUncaptured(),
    Fetches(),
    ReadsEnvironment(),
    MutatesEnvironment(),
    MutatesRepository(),
    ExternalMutation(),
    Publishes(),
    Integrates(),
    ReachesHost(),
    InstallsDependency(),
    MaterializesLockfile(),
    RunsDeclaredTarget(),
    RunsUndeclaredProgram(),
    UnclassifiedOperation(),
    EscapesContainment(),
]
"""Every effect a rule may declare.

A list rather than a mapping so the members keep their declaration order,
which is the order the reference table renders and the order a reader meets
them in. :func:`member_for` is the only lookup, and an unknown kind is a
declaration error rather than a silent allow.
"""

STRENGTH: list[DecisionEffect] = ["allow", "defer", "ask", "deny"]
"""The four effects weakest first, which is what makes the join a maximum.

The same order the segment join already uses. Spelled once here so a command
carrying several effects and a command spelled as several segments are combined
by one rule rather than by two that agree until somebody edits one.
"""


def member_for(kind: str, members: list[Effect] = EFFECT_MEMBERS) -> Effect:
    """The member answering for one declared kind.

    Raises rather than defaulting. A kind nothing answers for is a rule that
    was written and never reached, and the permissive reading of that -- allow,
    because no member objected -- is the one failure this model exists to make
    impossible.
    """
    for member in members:
        if member.kind == kind:
            return member
    raise ValueError(f"no effect member answers for {kind!r}")


def declare(
    kind: str,
    scope: str = "",
    write: str = "",
    reviewed: bool = False,
    reason: str = "",
    reach: str = "",
    members: list[Effect] = EFFECT_MEMBERS,
) -> EffectRow:
    """One effect as a rule states it, with every axis checked at declaration.

    Checked here rather than at the verdict because a table of several hundred
    rules is written once and read on every command: a misspelled kind that
    only failed when something matched the rule would sit in the table until
    the day it mattered, and the shape of that failure is a permission nobody
    declared.

    The scope and the write are checked on the same reasoning, and against the
    member rather than against one shared enum, because they are not one
    vocabulary: a scope names a tree to :class:`WritesPath`, a checkpoint to
    :class:`DestroysUncaptured`, and a container noun to
    :class:`MutatesEnvironment`. A single ``Literal`` spanning all of them
    would admit ``declare("writes_path", scope="unrecoverable")`` -- a word
    taken from the right enum, read by the wrong member, and falling past
    every branch that member has to the permissive end of it.

    The two axes differ in what an empty vocabulary means, because they differ
    in what a member does with a word it does not read. A scope is also a
    label -- the container noun, the external class -- so a member declaring
    no scopes accepts any and the row keeps it for the reason to quote. No
    member labels with a write, so declaring none means the axis is not
    offered, and stating one is a mistake with nothing to catch it later.

    ``members`` travels for the same reason it travels through :func:`deciding`
    -- the two have to check and decide against one table, or a kind accepted
    here raises where it is read.

    **What an adopting project can actually extend, though, is the data and
    not the table.** A compiled runtime ships this module byte for byte, so a
    member class written outside it does not exist where the verdict is
    reached; a project that declared one would pass here and raise inside a
    hook. What crosses that boundary is a row. A project wanting a question
    for a concern of its own states it as the scope and reason of the member
    whose verdict it wants -- ``declare("mutates_environment", scope="paid
    agent session", reason=…)`` asks, in the project's own words -- and a
    concern that genuinely is a new *kind* of thing belongs in this list,
    where every runtime compiling it gets the judgement.

    ``reach`` is where the harm lands, defaulted from the member and stated
    by a row whose effect lands somewhere its kind does not: an exported
    variable and a lent credential agent are both a changed environment, and
    only one of them dies with the container. Checked against the closed
    vocabulary, because a misspelled reach would otherwise be a question that
    relaxes nowhere, silently -- or, spelled as the narrow word, everywhere.
    """
    member = member_for(kind, members)
    if reach and reach not in REACHES:
        raise ValueError(f"{kind!r} reaches no {reach!r} — a reach is one of {REACHES}")
    if member.scopes and scope not in member.scopes:
        raise ValueError(
            f"{kind!r} reads no scope {scope!r} — it answers for {member.scopes}"
        )
    if write and write not in member.writes:
        raise ValueError(
            f"{kind!r} reads no write {write!r} — it answers for {member.writes}"
        )
    if reviewed and not member.reviewable:
        raise ValueError(f"{kind!r} reads no route — only a write is reviewed")
    return EffectRow(
        kind=kind,
        scope=scope,
        write=write,
        reviewed=reviewed,
        reason=reason,
        reach=reach or member.default_reach(scope),
    )


def joined_reach(rows: list[EffectRow], order: list[Reach] = REACHES) -> Reach | None:
    """Where the harm of every declared effect together lands: the widest of them.

    ``None`` where nothing was declared, which is not the narrowest answer but
    no answer: a verdict whose effects nobody stated keeps its question
    wherever the session runs. A word outside the vocabulary reads the same
    way, since the compiled table is data and a word it carries unchecked
    placed nothing.
    """
    stated = [row["reach"] for row in rows]
    if not stated or any(word not in order for word in stated):
        return None
    return max((word for word in order if word in stated), key=order.index)


def question_reach(
    rows: list[EffectRow],
    evidence: EffectEvidence | None = None,
    placement: SandboxPlacement = "ambient",
    members: list[Effect] = EFFECT_MEMBERS,
) -> Reach | None:
    """Where the harm a rule's question is about lands: the effects that raised it.

    A clone declares the fetch that reaches a declared host, the write that
    creates a directory, and the arrival of somebody's code -- and only the
    last is what anybody is asked about. So the reach is joined over the
    effects whose own answer is not a permission, and over all of them where
    none of them objects, which is the question a caller escalated by some
    other route and has to be answered for the whole of what was declared.
    """
    measured = EffectEvidence() if evidence is None else evidence
    asking = [
        row
        for row in rows
        if member_for(row["kind"], members).verdict(row, measured, placement) != "allow"
    ]
    return joined_reach(asking or rows)


def external_effects(effect_class: str) -> list[EffectRow]:
    """What a declared external class already says an operation does.

    Derived rather than restated. The class column says what an operation
    does beyond this machine, which a verdict beside it would say again in the
    other vocabulary -- so reading the effects off it is one judgement read
    once instead of two kept in step.

    It is also what keeps a table of a hundred remote operations from being a
    hundred chances to transcribe one wrong. A rule that states its own effects
    overrides this; a rule that states neither reaches the local table above.

    Only ``compensable`` publishes freely, and the distinction is the class's
    own: it means the remote state can be put back by an ordinary follow-up,
    which is true of a pull request and false of a release. A published
    artifact may already have been downloaded by the time anybody reconsiders,
    so ``publication`` asks alongside the classes that never claimed otherwise.
    A merge is not ``execution``: it states that it integrates, on its own row.
    """
    match effect_class:
        case "":
            return []
        case "compensable":
            return [declare("publishes", scope=effect_class)]
        case _:
            return [declare("external_mutation", scope=effect_class)]


class Answer:
    """One declared effect and what it answered, kept together.

    A pair rather than two returns, because every later reading of a verdict
    -- its purpose, its reason, the rule an audit records -- has to come from
    the row that actually decided rather than from the set it was strongest in.
    """

    def __init__(self, row: EffectRow, effect: DecisionEffect) -> None:
        self.row: EffectRow = row
        self.effect: DecisionEffect = effect


def deciding(
    rows: list[EffectRow],
    evidence: EffectEvidence,
    placement: SandboxPlacement = "ambient",
    strength: list[DecisionEffect] = STRENGTH,
    members: list[Effect] = EFFECT_MEMBERS,
) -> Answer | None:
    """The effect whose answer the whole operation takes, or nothing declared.

    The strongest wins, which is the rule the segment join already uses: an
    operation that both reads a credential and writes scratch is a credential
    read, and nothing about the harmless half weakens it.

    ``members`` travels rather than being reached for, so a project adding a
    kind of effect this library never named gets it *decided* as well as
    declared. Defaulting to the offered table and taking a caller's is the
    same seam every other table here keeps.
    """
    answers = [
        Answer(row, member_for(row["kind"], members).verdict(row, evidence, placement))
        for row in rows
    ]
    if not answers:
        return None
    return max(answers, key=lambda answer: strength.index(answer.effect))


def verdict_for(
    rows: list[EffectRow],
    evidence: EffectEvidence,
    placement: SandboxPlacement = "ambient",
    strength: list[DecisionEffect] = STRENGTH,
    members: list[Effect] = EFFECT_MEMBERS,
) -> DecisionEffect:
    """What a set of declared effects earns together.

    The strongest answer wins, which is the same rule the segment join uses:
    an operation that both reads a credential and writes scratch is a
    credential read, and nothing about the harmless half weakens it.

    An operation declaring no effect at all allows. That is not a fallback for
    the unclassified -- a command no rule describes carries
    :class:`RunsUndeclaredProgram` and asks -- but the reading of a rule that
    positively says this does nothing worth guarding.
    """
    answered = deciding(rows, evidence, placement, strength, members)
    return answered.effect if answered is not None else "allow"


def declared_verdict(
    rows: list[EffectRow],
    refuses: str,
    evidence: EffectEvidence,
    placement: SandboxPlacement = "ambient",
    strength: list[DecisionEffect] = STRENGTH,
    members: list[Effect] = EFFECT_MEMBERS,
) -> DecisionEffect:
    """What a whole rule earns: what it does, unless the spelling is refused.

    One line, and it exists so that there is one of it. The refusal is not an
    effect and cannot be folded into the table above -- an operation that
    reaches the same end by a route this project prefers does exactly what the
    refused spelling does -- so something outside the table has to combine
    them, and a rule this small is precisely the kind that gets rewritten at
    each call site until two of them disagree.

    A refusal wins over every effect rather than joining them at their
    strongest. It is not a stronger reading of what the operation does; it is
    the project declining to reach that end this way, which no evidence about
    paths or placement bears on.
    """
    if refuses:
        return "deny"
    return verdict_for(rows, evidence, placement, strength, members)


def purpose_of(
    rows: list[EffectRow],
    evidence: EffectEvidence,
    placement: SandboxPlacement = "ambient",
    strength: list[DecisionEffect] = STRENGTH,
    members: list[Effect] = EFFECT_MEMBERS,
) -> ReviewPurpose | None:
    """Which kind of decision the effect that decided is about, whatever it said.

    Read off the same row the verdict came from rather than off the set. An
    operation that reads a credential and writes a log would otherwise be able
    to report the credential's verdict beside the log's purpose, which is a
    queue entry naming the wrong decision -- worse than none, because a
    reviewer triages on it.

    Ungated, because a caller that reached its own verdict has already settled
    whether anybody is being asked and needs only to know what about. A shell
    row escalated by a flag guard, or de-escalated by a read verb, is exactly
    that caller: gating here as well would answer nothing for the questions
    this table did not raise itself.
    """
    answered = deciding(rows, evidence, placement, strength, members)
    if answered is None:
        return None
    return member_for(answered.row["kind"], members).purpose(answered.row)


def purpose_for(
    rows: list[EffectRow],
    evidence: EffectEvidence,
    placement: SandboxPlacement = "ambient",
    strength: list[DecisionEffect] = STRENGTH,
    members: list[Effect] = EFFECT_MEMBERS,
) -> ReviewPurpose | None:
    """The same, for a caller taking this table's own verdict as well.

    Only an ask carries one. A permission interrupts nobody, and a refusal is
    not a decision anybody is being asked to make.
    """
    answered = deciding(rows, evidence, placement, strength, members)
    if answered is None or answered.effect != "ask":
        return None
    return purpose_of(rows, evidence, placement, strength, members)
