"""The orthogonal facts a settled verdict composes, each named separately.

One enum answering "how dangerous is this" collapses questions that have
different answers and different answerers. A checkpoint does not consent to a
release; an approval does not build a host channel; running inside the
boundary does not discharge a code review; a familiar destination does not
make a merge ordinary. So each fact is its own small vocabulary here, and a
verdict composes them.

The provider-facing effect stays exactly four — allow, ask, deny, defer, in
:mod:`lup.policy.kernel.decision` — because that is what a native runtime can
be told. Everything in this module is what Lup knows *about* one of those
four, and none of it is a fifth.
"""

from typing import Literal

type ReviewerRequirement = Literal["human_only", "supervisor_allowed"]
"""Who may answer one question, which is never the agent that asked it.

``human_only`` is the default and what silence means: publication, spending,
credential exposure, and repository security changes are answerable by a
person and by nobody the person delegated to. ``supervisor_allowed`` is a
rule deliberately opting a question into the supervisor chain, for review
that is about the work rather than about consequence a supervisor cannot
carry — a quality checkpoint over a full file rewrite is the shape.

Eligibility is granted by the relay from authenticated session relationships.
It is never inferred from whether an answering command happens to be visible
in a tool list, which would make the reviewer whoever the agent could reach.
"""

type ReviewPurpose = Literal[
    "quality_review",
    "sensitive_access",
    "external_consequence",
    "policy_override",
    "unrecovered_local_mutation",
    "untrusted_dependency",
]
"""What a question is *for*, so the queue can be read without the rule ids.

The reason text says what happened; this says which kind of decision the
person is being asked to make, which is what lets a reviewer triage a queue
and an audit count interruptions by kind. It never replaces the rule id or
the complete reason — a purpose is a category, and two rules sharing one are
still two rules.

``untrusted_dependency`` is the one that is not about what the agent does but
about what it *trusts*. Taking a package is the moment third-party code enters
a tree, a compromised release is indistinguishable from a good one at that
moment, and the window between publication and discovery is exactly when an
eager agent reaches for a name it read somewhere. Counted apart from
``external_consequence`` because the direction is inward: nothing leaves this
machine, and the exposure outlives the command by however long the dependency
stays.
"""

type AbstentionPurpose = Literal["provider_native", "boundary_settle"]
"""Why a rule declined to finalize, which decides what happens next.

``provider_native`` is a deliberate handoff: this policy has looked and has
decided the provider's own mode should answer. Edit size is the archetype —
a large ordinary edit is exactly what a native auto-accept mode exists for,
and Lup interposing would be replacing a decision an operator already made.
It is the only abstention that reaches ``defer``.

``boundary_settle`` is the classifier lacking a final judgement while the
*boundary* still has facts about it. Contained, the operation's effects are
confined and it runs inside; ambient, the profile's declared unjudged-ambient
policy answers. Sharing one word with the deliberate handoff would let a
parser gap silently inherit provider auto-mode, which is the opposite of what
a gap means.
"""

type UnjudgedAmbient = Literal["ask", "defer"]
"""What a profile does with a legible operation nothing judged, uncontained.

``ask`` is the default: work nobody classified stays visible, and the person
answering sees exactly the text that will run. ``defer`` is a profile
deliberately handing the long tail to provider-native judgement for a
seamless posture. Either way it is a declaration being read, never a fifth
effect and never an accident.
"""

type CheckpointEvidence = Literal["absent", "complete", "failed"]
"""What was actually measured about a capture, as against what was required.

``absent`` is nothing measured yet, which is every operation during
preliminary settlement and every operation whose requirement is
``unrecoverable``. ``complete`` is capture proven: coverage, restore, and
metadata, not the mere existence of a snapshot reference. ``failed`` is the
capture attempted and measured short.

The distinction between ``absent`` and ``failed`` is load-bearing. Nothing
measured leaves an operation's review standing, which is the same outcome a
failure produces — but a failure is a fact worth a person seeing, and an
operation that never needed a capture is not a failure of anything.
"""


type Visibility = Literal["quiet", "notice"]
"""Whether an operation that did not interrupt should still be seen.

``notice`` surfaces in the live activity view without stopping execution — a
recovery-backed deletion, a refused credential read, the first unusual system
inspection of a run. ``quiet`` records normally.

Presentation and audit metadata, never authority. A notice that no UI renders
cannot change an effect, a placement, or who may review; and nothing waits on
one being seen, because a delivery gate is exactly what would make missing
rendering into missing permission.
"""

type RefusalCause = Literal[
    "deliberate",
    "reviewability",
    "unreadable",
    "unlisted",
    "capability",
]
"""Why a refusal was reached, which is what says whether it can be answered.

``deliberate`` is a rule having decided against the operation. ``reviewability``
is a shape nobody could review — inline code, an opaque downloaded script.
``unreadable`` is the classifier declining to read: an unresolved expansion,
a substitution it cannot see into. ``unlisted`` is the vocabulary naming
nothing and no boundary fact settling it. ``capability`` is the runtime
unable to deliver a guarantee the operation requires.

Only ``capability`` is unanswerable by anybody. The rest differ in whether
*escalation* reaches them, which each rule states for itself.
"""

type Capability = Literal[
    "host_executor",
    "checkpoint_store",
    "question_relay",
    "inside_placement",
]
"""One runtime guarantee an operation may require and a profile may lack.

``host_executor`` is the launcher-owned channel that runs an approved
operation outside the containment boundary. ``checkpoint_store`` is where
pre-state and post-state live, without which recovery cannot discharge
anything. ``question_relay`` is the durable record every final ask is written
to. ``inside_placement`` is a provider adapter's ability to hold an operation
inside the boundary regardless of the session's own mode.

A missing one of these is a *capability-blocked refusal*, not an ask: no
reviewer can approve a channel into existence, and offering the question
would spend a person's attention on a decision that changes nothing.
"""

type EffectClass = Literal[
    "compensable",
    "attestation",
    "execution",
    "publication",
    "deployment",
    "spending",
    "repository_security",
    "opaque",
]
"""What an operation does to state or people outside this machine.

``compensable`` means the remote state can be restored by a normal follow-up
operation — a pull request reopened, an issue's title put back. It does not
claim the notification anybody received can be unsent, which is why it is a
classification of the state and not of the observation.

``attestation`` is a claim made in somebody's name: an approving review, a
request-changes review. ``execution`` is something running as a result:
a dispatched or rerun workflow, an issue deleted or transferred. A merge is
not one: it states that it integrates, which is its own effect. ``publication``,
``deployment``, and ``spending`` are what they say. ``repository_security``
covers settings, rules, visibility, secrets, variables, and environments.
``opaque`` is an external mutation whose effect the classifier cannot name,
which is the one that must never read as compensable by default.
"""

type Reach = Literal[
    "container",
    "mount",
    "dependency",
    "credential",
    "lup",
    "host_later",
]
"""Where the harm an operation's question guards against would land.

The one fact that decides what a question is worth inside a container
measured around the session. The walls stand whatever the operation does,
so what remains to ask about is whatever the walls do not hold:

* ``container`` — the harm stays inside: a process killed, a variable
  exported, a package installed into the image, a program a flag names. It
  goes when the container does.
* ``mount`` — the harm lands on the paths the operation names, which may be
  the container's own or a directory the host lent it. Only a target that
  lands on nothing the host shares keeps the harm inside.
* ``dependency`` — code arriving from a package index, which a later build or
  import reaches after nobody is watching, in the container or out of it.
* ``credential`` — a secret the launch lent the session, or a variable that
  swaps the identity a later command acts as.
* ``lup`` — this policy's own machinery: its settings, its ledger, its gates.
* ``host_later`` — beyond the container or after it: another machine, a
  remote, a published artifact, a cache the host reads again.

Ordered narrowest first, and only the first two ever relax: the join of an
operation's reaches is the widest, so one guarded effect keeps the whole
operation's question.
"""

REACHES: list[Reach] = [
    "container",
    "mount",
    "dependency",
    "credential",
    "lup",
    "host_later",
]
"""Every reach, narrowest first, which is what makes a join a maximum."""

type PathRuleKind = Literal[
    "exact",
    "subtree",
    "name_prefix",
    "new_subtree",
    "contains_part",
    "new_devtools",
]
"""How a protected-path rule matches a path.

``exact`` names one file, ``subtree`` a root and everything under it,
``new_subtree`` only what does not stand there yet, ``contains_part`` a
directory or file by name wherever it sits, ``name_prefix`` a file name's
opening (`.env`), and ``new_devtools`` a devtools module not written yet.
"""
