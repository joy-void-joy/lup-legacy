"""The decisions every runtime reaches identically, over the shared kernel.

:mod:`lup.policy.dispatcher` compiles this half into every generated script
beside the host half and one runtime's own words, so a decision written here
is the decision every runtime makes. What belongs here is everything
downstream of a payload: the declarations a tool is judged against, and the
host-resolved facts the kernel needs to judge it. What does not belong is
anything a runtime spells for itself — the payload shape those values are read
out of, the root it installs trusted packages beneath, and the envelope a
verdict is returned in.

The split is drawn there because the arguments a kernel call carries are
exactly what drifted before. Each runtime passed its own set, nothing compared
them, and a fact one of them stopped passing was a rule that silently stopped
applying — with no failure anywhere, because a permission that never happens
looks like a permission that was granted. One call site cannot disagree with
itself.

The imports below resolve against the generated runtime this is compiled
beside, which is why this file is type-checked against that tree rather than
against the workspace.
"""

import json
import shlex
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
import policy_data as identity_policy

from host import (
    document_at,
    document_digest,
    patched_documents,
    resolved_path,
    routed_edit_response,
    sed_output,
    approval_fingerprint,
    contained,
    defers_unjudged,
    note_asked,
    policy_snapshot_digest,
    review_answers,
    review_answers_home,
    routing_policy_identity,
    delivers,
    host_held_ports,
    measured_boundary,
    measured_landings,
    unleased_write_targets,
    readonly_write_targets,
    script_run_nudge,
    directory_write_targets,
    empty_directory_targets,
    existing_write_targets,
    foreign_repository,
    granted_allowances,
    ignored_write_targets,
    managed_script_roots,
    outside_this_project,
    this_checkout_path,
    patch_write_targets,
    peer_store,
    close_claim_window,
    declared_identity,
    open_claim_window,
    recoverable_write_targets,
    resolved_write_targets,
    sibling_worktrees,
    walked_withheld,
    record_deferral,
    record_question,
    review_hook_call,
    waiting_edits,
    committed_text,
    resolved_refutations,
    tracked_write_targets,
    text_at,
    undo_snapshot,
    worktree_path,
    worktree_root,
    referral_noted,
    file_diagnostics,
    swept_files,
)
from kernel.decision import KernelDecision
from kernel.rows import PostToolReport

# A line of its own: a dispatcher's bundle drops an import line whose text is
# already in it, and a line naming both would redefine `KernelDecision` there.
from kernel.decision import FileReviewRow, captured_edit_decision
from kernel.documents import (
    FollowedDocument,
    FollowedReading,
    document_operation,
    file_steps,
    followed_documents,
    judged_documents,
    rewrite_reading,
    shown_documents,
)
from kernel.policy_protocol import read_response, routing_failure
from coordination import store
from coordination.runtime import stdin_runtime
from kernel.edit import (
    awaits_resolution,
    decide_edit,
    relocated_edit_text,
    relocated_suppressions,
)
from kernel.effects import STRENGTH
from kernel.fetch import decide_fetch, loopback_port
from kernel.peers import (
    decide_foreign_claim,
    decide_peer_listing,
    decide_peer_send,
    peer_listing_context,
    settled_with_claim,
)
from kernel.lex import (
    authored_writes,
    python_script_targets,
    shell_flag_write_targets,
    shell_patch_operands,
    shell_path_verb_targets,
    shell_sed_rewrites,
    shell_write_targets,
    shell_written_targets,
)
from kernel.rows import (
    DisplacedTargetRow,
    ResolutionRow,
    RewrittenDocumentRow,
    WithheldWalkRow,
    landing_rows,
)
from kernel.review import Reviewed
from kernel.spawns import decide_spawn, spawn_name
from kernel.words import INTERPRETERS
from kernel.roles import displaced_targets, sibling_scratch_rows, unscratched
from kernel.shell import decide_shell, sandbox_excluded, shell_posture_targets
from kernel.tools import decide_tool
from kernel.walks import excluded_name, shell_walked_roots, skipped_file
from kernel.withheld import (
    carries_withheld_name,
    withheld_edit,
    withheld_names,
    withheld_row,
)
from policy_data import (
    ACCEPTANCE_GUARD,
    ALLOWANCE_GRANTS_ENV,
    ALLOWED_FETCH_SCOPES,
    ANTI_PATTERN_ROWS,
    DASHBOARD_URL_ENV,
    DIAGNOSTICS_COMMAND,
    REPAIR_COMMAND,
    RESOLUTION_COMMAND,
    DENIED_FETCH_SCOPES,
    EDIT_RULES,
    IMPORT_BOUNDARIES,
    KNOWN_ALLOWANCES,
    MAXIMUM_ADDED_LINES,
    PATH_ROLES,
    PATH_RULES,
    PEER_POLICY,
    POLICY_ROOT_ENV,
    RECOVERABLE_TARGET_LIMIT,
    REVIEW_ANSWERS_ENV,
    REFUSED_PATHS,
    REFUSED_TOOLS,
    RUNNER_TARGET_TABLES,
    RUNNER_TARGETS,
    SANDBOX_EXCLUDED_COMMANDS,
    SECRET_VARIABLES,
    SHELL_RULES,
    SPAWN_NAMES,
    UNSCOPED_FETCH,
)


def bash_decision(
    command: str,
    managed_root: Path | None,
    sandboxed: bool,
    interactive: bool,
    escapable: bool,
    cwd: Path | None,
    relayed: bool = False,
    autonomous: bool = False,
    agent_identity: str = "",
    park: bool = True,
) -> KernelDecision:
    """Judge one shell command against the declared vocabulary.

    The kernel reads no filesystem, so every fact about the paths this command
    would touch is resolved here and passed as data: which of the paths it
    would write already exist, which operands Git could restore, and which
    are directories.

    Existence and recoverability both cover redirection targets and path-verb
    operands alike, because the questions they ask are the same ones —
    whether writing here brings something into being or replaces it, and what
    replacing it would cost. Resolving them for only one of the two writing
    forms is what left ``rm f`` granted while ``echo x > f`` asked about the
    same clean, tracked file.

    ``cwd`` is where the calling session is, which the command's relative
    operands resolve against. It is a parameter rather than a read of this
    process, because a hook is promised nothing about where it runs, and
    resolving a target against the wrong tree answers a different question.

    ``escapable`` is the one thing here a runtime answers rather than the host:
    whether it can put a single call outside its own sandbox. It arrives as an
    argument for the same reason the rest does — a fact one dispatcher stopped
    passing is a rule that silently stopped applying.

    ``autonomous`` is the identity the edit gates read, carried here because a
    command that writes its own content reaches those gates. It is the same
    answer the dispatcher hands ``edit_decision``, passed rather than derived
    from ``relayed``: a session holding a mailbox and a session implementing
    against fixed acceptance tests are different facts that happen to coincide
    on one runtime.
    """
    # Read once and passed to each fact that needs it, rather than re-read per
    # question: the ledger is one measurement of one launch, and a second read
    # partway through a verdict could answer from a file the first did not see.
    boundary = measured_boundary(cwd)
    inside = contained(boundary)
    acted_on = shell_path_verb_targets(command, SHELL_RULES)
    # The third way a command names a file it writes, after a redirection and
    # a path verb's operand. It joins the two relaxing facts below and not the
    # lease's list, because it gathers every write flag the executable has a
    # row for rather than only the row that matches -- which is safe for a
    # fact consulted about a path and not for a list that asks about one.
    flagged = shell_flag_write_targets(command, SHELL_RULES)
    # Before the verdict rather than after it, because the verdict reads it:
    # an approval question exists where a loss is permanent, and a tree the
    # object store already holds has no permanent loss to ask about. Ordered
    # the other way the relaxation would be judging a snapshot that did not
    # exist yet, and a refused command is snapshotted too -- one ref for a
    # state the tree was already in, which dedup collapses.
    reference = undo_snapshot(cwd, command)
    # What the line leaves in every file it writes, step by step, and the
    # edit gates' verdict on each file its own bytes or a rewrite reach. Both
    # halves of the rewrite reading come off it -- the documents a rewrite
    # leaves, and why the rest left none -- so a target reaches exactly one.
    followed = followed_reading(command, cwd or Path.cwd())
    judged = judged_verdicts(followed, cwd or Path.cwd(), autonomous, agent_identity)
    reading = rewrite_reading(
        followed,
        lambda target, document: rewritten_row(
            target, document, judged[document["path"]], cwd or Path.cwd()
        ),
    )
    # Another checkout of this repository keeps this one's scratch, reached by
    # the absolute path a session spells it with -- so Git is asked for the
    # checkouts only where the command names such a path at all.
    siblings = (
        sibling_worktrees(cwd)
        if any(
            target.startswith("/")
            for target in [*shell_write_targets(command), *acted_on, *flagged]
        )
        else []
    )
    verdict = decide_shell(
        command,
        SHELL_RULES,
        ALLOWED_FETCH_SCOPES,
        DENIED_FETCH_SCOPES,
        sandboxed=sandboxed,
        excluded_commands=SANDBOX_EXCLUDED_COMMANDS,
        trusted_script_roots=managed_script_roots(managed_root),
        path_roles=[*PATH_ROLES, *sibling_scratch_rows(siblings, PATH_ROLES)],
        path_rules=PATH_RULES,
        existing_targets=existing_write_targets(
            [*shell_write_targets(command), *acted_on, *flagged], cwd
        ),
        tracked_targets=tracked_write_targets(
            [*shell_write_targets(command), *acted_on, *flagged], cwd
        ),
        recoverable_targets=recoverable_write_targets(
            [*shell_write_targets(command), *acted_on], cwd
        ),
        directory_targets=directory_write_targets(acted_on, cwd),
        empty_directories=empty_directory_targets(acted_on, cwd),
        recoverable_target_limit=RECOVERABLE_TARGET_LIMIT,
        runner_targets=RUNNER_TARGETS,
        target_tables=RUNNER_TARGET_TABLES,
        # The edit gates, over the files a rewrite in place would replace.
        # An in-place rewrite is an edit spelled as a command, so it meets the
        # rules an edit meets rather than a recoverability grant that answers
        # whether it could be undone -- a different question, and not the one
        # the anti-pattern table, the review-note gate and the size gate ask.
        antipattern_rows=ANTI_PATTERN_ROWS,
        edit_rules=EDIT_RULES,
        import_boundaries=IMPORT_BOUNDARIES,
        acceptance_guard=ACCEPTANCE_GUARD,
        maximum_added_lines=MAXIMUM_ADDED_LINES,
        autonomous=autonomous,
        allowances=granted_allowances(ALLOWANCE_GRANTS_ENV, KNOWN_ALLOWANCES),
        rewritten_documents=reading["documents"],
        unproduced_documents=reading["unproduced"],
        interactive=interactive,
        # A reviewed worker is non-interactive and not therefore alone: it
        # holds a mailbox reaching the human supervising its run, and a
        # refusal that named no route sent it to queue a blocking question
        # instead.
        relayed=relayed,
        # What `outside` means is the launcher's host, and a runtime's own
        # per-call escape only reaches it where there is no container in
        # between. Uncontained, that escape genuinely is the way out of the
        # only boundary there is; contained, it lands in the container and a
        # placement settled on it would send an operation somewhere nothing
        # can carry it. So the runtime still answers for its escape and the
        # measurement answers for whether that escape reaches the host.
        escapable=(escapable and not inside) or delivers(boundary, "host_executor"),
        # Read here rather than passed by each dispatcher, unlike `escapable`
        # above: whether this process sits inside the boundary its profile
        # promised is a fact about the host with no runtime variation to it,
        # so neither dispatcher is given the chance to forget it.
        contained=inside,
        # Where this checkout sits, so an absolute spelling of a path inside
        # it is read back to the form the declared roles are anchored at. The
        # host's to supply for the reason it resolves symlinks: a fact about
        # this machine, which the kernel holds none of.
        checkout_root=str(cwd or Path.cwd()),
        # The other half of that pair, and the reason the first one alone
        # settles nothing: a container is a promise about where an operation
        # lands, and `bounded()` counts it only where the launch measured that
        # the promise holds. Passed from the same ledger read, so the two
        # cannot describe different launches.
        inside_placement=delivers(boundary, "inside_placement"),
        # The profile's own answer for the long tail, which only an
        # uncontained session ever reaches: contained, the row above settles
        # the same operation first.
        unjudged_ambient="defer" if defers_unjudged(boundary) else "ask",
        # What `curl` and `wget` answer for an origin no scope names: the
        # project's fetch declaration, or the posture above where it made
        # none -- the same answer `fetch_decision` gives `WebFetch`.
        unscoped_fetch=UNSCOPED_FETCH,
        # What no word may name and no builtin may print, as the project
        # declared them: the same rows the canonical policy is handed.
        refused_paths=REFUSED_PATHS,
        # And what a recursive reader would walk into beneath a root it names,
        # which only the filesystem can say.
        withheld_walks=[
            WithheldWalkRow(root=walk["path"], found=found)
            for walk in shell_walked_roots(command, SHELL_RULES)
            for names in [withheld_names(REFUSED_PATHS)]
            for found in [
                walked_withheld(
                    walk["path"],
                    walk["hidden"],
                    lambda name: carries_withheld_name(name, names),
                    lambda path: withheld_row(path, REFUSED_PATHS) is not None,
                    lambda name: excluded_name(name, walk["excluded"]),
                    lambda name: skipped_file(name, walk),
                    cwd,
                )
            ]
            if found
        ],
        secret_variables=SECRET_VARIABLES,
        # Resolved against what this launch mounted writable, so a write into a
        # worktree cut after the container started reaches a reviewer instead of
        # the writable base no overlay covers. Every target the lease leaves
        # uncovered is listed, the scratchpad and `/tmp` included: which of
        # those roots are the launch's own is the settlement row's to say, so
        # the canonical policy hands the row the same list from the same call.
        unleased_targets=unleased_write_targets(
            [*shell_write_targets(command), *acted_on], boundary, cwd
        ),
        # The read-only holes of the same lease: a repository's shared config
        # and hooks, which the container binds read-only and a host posture
        # holds only here. Every spelling of a write, so a `cp` or `ln` into
        # one meets what a redirection's content already met -- and only the
        # writes, since this refuses: a copy's source is read, not written.
        readonly_targets=readonly_write_targets(
            [
                *shell_write_targets(command),
                *shell_written_targets(command, SHELL_RULES),
            ],
            boundary,
            cwd,
        ),
        # Where a target really lands, for the grants above that read a role
        # off its spelling. The host resolves the links because the kernel
        # reads no filesystem, and the kernel says whether the landing changes
        # what the path is, because the host holds no role table.
        displaced_targets=displaced_targets(
            [
                DisplacedTargetRow(path=path, lands=lands)
                for path, lands in resolved_write_targets(
                    [*shell_write_targets(command), *acted_on, *flagged], cwd
                ).items()
            ],
            PATH_ROLES,
        ),
        # Where each path the command changes lands, for the one row that
        # reads it: inside a measured container, a question whose harm stays
        # there is settled without anybody.
        landings=(
            landing_rows(
                measured_landings(
                    shell_posture_targets(command, SHELL_RULES), boundary, cwd
                )
            )
            if inside and delivers(boundary, "inside_placement")
            else []
        ),
        # Read only where the line names this machine's loopback at all, since
        # it walks every process this container can see.
        host_ports=(
            held_loopback_ports(boundary)
            if any(host in command for host in ("localhost", "127.", "::1"))
            else []
        ),
        # A snapshot proves a capture only of what it took, and it takes
        # nothing Git ignores: one ignored target outside declared scratch,
        # which needs no capture, leaves the loss uncaptured.
        recovered=bool(reference)
        and not ignored_write_targets(
            unscratched(
                [
                    *shell_write_targets(command),
                    *shell_written_targets(command, SHELL_RULES),
                ],
                PATH_ROLES,
                str(cwd or Path.cwd()),
            ),
            cwd,
        ),
    )
    # The gates an edit is judged by, over the writes this command carries the
    # content of. Joined here rather than inside the classifier because they
    # read the filesystem -- the file about to be replaced, so a note removed
    # or an anti-pattern introduced is seen against what is actually there --
    # and the kernel reads nothing. Strongest wins, the rule every other join
    # in this policy uses.
    authored = authored_review(followed, judged)
    if authored is not None and STRENGTH.index(authored.effect) > STRENGTH.index(
        verdict.effect
    ):
        verdict = authored
    # What the command changes, file by file, as the verdict judged it: the
    # record a question keeps for the operator to read.
    verdict = verdict.revised(
        file_reviews=reviewed_rows(
            followed,
            judged,
            verdict.effect == "ask",
            cwd or Path.cwd(),
            autonomous,
            agent_identity,
        ),
        unpreviewed=tuple(followed["unpreviewed"]),
    )
    if verdict.effect == "ask":
        note_asked(cwd, approval_fingerprint("shell", command, cwd), "shell", command)
    # In-process callers park here; native dispatchers park the complete tool
    # payload with their file preconditions at their own decoding boundary.
    if verdict.effect == "ask" and park:
        record_question(
            cwd,
            command,
            verdict.reason,
            verdict.rule,
            verdict.purpose or "",
            verdict.reviewer,
            verdict.escalated,
            verdict.sandbox,
        )
    if verdict.effect == "deny":
        return verdict
    # The log half of allow-and-log. A deferral is this policy declining to
    # interrupt, which is the one verdict that reaches nobody: the runtime's
    # own gate decides and the reason goes to no human. Written down here or
    # it is not written down anywhere.
    if verdict.effect == "defer":
        record_deferral(cwd, command, verdict.reason, verdict.checkpoint != "nothing")
    if verdict.effect != "allow":
        return verdict
    nudge = script_run_nudge(python_script_targets(command, INTERPRETERS), cwd)
    if not nudge:
        return verdict
    return verdict.revised(reason=verdict.reason + nudge)


def dashboard_held() -> bool:
    """Whether this session's launch holds a dashboard, where a reviewer reads what parks."""
    return bool(declared_identity(DASHBOARD_URL_ENV))


def shell_preimages(command: str, cwd: Path) -> dict[Path, str | None]:
    """What each file one command would write holds now, keyed where it resolves.

    Every spelling of a write the policy reads -- a redirection, a path verb's
    operand, a write flag, an authored document, a sed rewrite, a copy -- and
    every file a step lands the text of -- a copy's source, a patch -- so the
    review binds to the files as they stood, and a change to any of them
    since makes the same command a fresh question. A directory operand has no
    document to bind, so it is bound by the command that names it alone, and
    a file that does not read as text is bound the same way: the question
    names it as a file no document shows.
    """
    paths = [
        *shell_write_targets(command),
        *shell_path_verb_targets(command, SHELL_RULES),
        *shell_flag_write_targets(command, SHELL_RULES),
        *(write["path"] for write in authored_writes(command)),
        *(
            path
            for rewrite in shell_sed_rewrites(command, SHELL_RULES)
            for path in rewrite["targets"]
        ),
        *(
            path
            for taken in file_steps(command, SHELL_RULES)
            for path in (taken["path"], taken["source"])
            if path
        ),
    ]

    return {
        (cwd / path).resolve(): text_at(cwd, path)
        for path in dict.fromkeys(paths)
        if not (cwd / path).exists() or text_at(cwd, path) is not None
    }


def review_policy_identity(cwd: Path, script: Path) -> str:
    """Which policy judged a parked call: the routed one, and this compiled script's own.

    Part of what an approval binds to, so a regeneration between asking and
    answering makes the same call a fresh question rather than spending an
    answer given under other rules.
    """
    return json.dumps(
        [
            routing_policy_identity(cwd),
            policy_snapshot_digest(script.parents[1]),
            sha256(script.read_bytes()).hexdigest(),
        ]
    )


def reviewed_decision(
    decision: KernelDecision,
    cwd: Path,
    session: str,
    tool: str,
    arguments: dict,
    preconditions: dict[Path, str | None],
    waiting: Callable[[str], str],
    execution_id: str = "",
    stage: str = "",
    predecessor: str = "",
    execution_payload: dict | None = None,
    policy_identity: str = "",
    provider: str = "",
    agent: str = "",
    account: list[dict[str, str]] | None = None,
) -> Reviewed:
    """Park one ask for the operator, or spend the single-use answer they recorded.

    The call is refused while it waits, with a recovery written for the agent:
    it is queued rather than refused, the call is not to be reshaped, and
    ``waiting`` spells, in the runtime's own words, how to start `review wait`
    on it, which carries the approved call out and reports the result.

    Every file the verdict records a document for is bound as it stands, the
    preimage its row's ``before_sha256`` names: the operator reads each diff
    against it, and a change to it since makes the same call a fresh question.

    *agent* is the runtime's id for the subagent that asked, blank for the
    session's own conversation, and *account* what it said the call is for,
    as its runtime's half read it off the call and its transcript. Where it
    said nothing there, what its roster row says it is on stands in, named
    as that. A conversation with two or more edits waiting is told how to
    put them to the operator as one.
    """
    bound = {
        **{
            Path(row["path"]): text_at(cwd, row["path"])
            for row in decision.file_reviews
        },
        **preconditions,
    }
    directory = peer_directory(cwd)
    member = answering_member(directory)
    told = account or roster_doing(directory, member, agent)
    result = review_hook_call(
        cwd,
        session,
        tool,
        json.dumps(arguments, sort_keys=True),
        json.dumps(
            {str(path): before for path, before in bound.items()},
            sort_keys=True,
        ),
        decision.reason,
        decision.rule,
        decision.purpose or "",
        decision.reviewer,
        execution_id,
        stage,
        predecessor,
        json.dumps(execution_payload, sort_keys=True)
        if execution_payload is not None
        else None,
        policy_identity,
        json.dumps(decision.file_reviews, sort_keys=True),
        answers=str(
            review_answers(
                cwd / ".lup/questions.jsonl", review_answers_home(REVIEW_ANSWERS_ENV)
            )
        ),
        member=member,
        placement=decision.sandbox,
        provider=provider,
        unpreviewed=json.dumps(decision.unpreviewed, sort_keys=True),
        segments=json.dumps(decision.segments, sort_keys=True),
        agent=agent,
        account=json.dumps(told, sort_keys=True),
    )
    identifier = result["id"]
    if result["state"] == "approved":
        return {"decision": decision.revised(effect="allow"), "notice": ""}
    if result["state"] == "rejected":
        note = f": {result['reason']}" if result["reason"] else ""
        return {
            "decision": decision.revised(
                effect="deny",
                recovery=(
                    f"The operator declined review {identifier}{note}. Don't "
                    "retry this call as it stands: change course, or ask the "
                    "user."
                ),
            ),
            "notice": f"Lup review {identifier} was declined; the agent is told.",
        }
    if not identifier:
        unavailable = f"Review queue unavailable: {result['reason']}. Run this operation from an operator terminal."
        return {
            "decision": decision.revised(effect="deny", recovery=unavailable),
            "notice": unavailable,
        }
    project = declared_identity(POLICY_ROOT_ENV)
    prefix = [
        "uv",
        "run",
        # The checkout holding the review queue, so the line runs from
        # anywhere; the project, where declared, selects the application's CLI.
        "--directory",
        str(cwd),
        *(["--project", project] if project else []),
        "lup-devtools",
        "review",
    ]
    approve = shlex.join([*prefix, "approve", identifier, "--as", "operator"])
    decline = shlex.join([*prefix, "decline", identifier, "--as", "operator"])
    dashboard = declared_identity(DASHBOARD_URL_ENV)
    where = (
        f"on the dashboard, {dashboard}"
        if dashboard
        else f"from a terminal outside the session: `{approve}` or `{decline}`"
    )
    waiting_here = waiting_edits(cwd, session, agent)
    together = (
        f" {waiting_here} of your edits now wait on the operator one review at "
        "a time. Where changes belong together, write each file as it should "
        "end up under one directory in tmp/, mirroring the checkout, and run "
        f"`{shlex.join([*prefix, 'propose'])} <directory> --why '<what they "
        "are for>'`: the operator reads them as one review and answers all "
        "of them at once."
        if waiting_here >= 2
        else ""
    )
    return {
        "decision": decision.revised(
            effect="deny",
            recovery=(
                f"Queued for the operator as review {identifier} — not refused. "
                "Don't change the command; carry on with other work. "
                + waiting(shlex.join([*prefix, "wait", identifier]))
                + f" The operator answers it {where}."
                + together
            ),
        ),
        "notice": f"Lup review {identifier} is waiting for you {where}.",
    }


def roster_doing(
    directory: Path | None, member: str, agent: str
) -> list[dict[str, str]]:
    """What the asking conversation last told the roster it is on, where it said anything.

    Its own row: the subagent's where one asked, else the session's. Nothing
    where no roster is kept here or the row says nothing.
    """
    if directory is None or not member:
        return []
    row = store.member_of(
        directory,
        store.subagent_actor(member, agent) if agent else store.session_actor(member),
    )
    doing = (
        store.text(row["description"])
        if row is not None and "description" in row
        else ""
    )
    return [{"source": "doing", "text": doing}] if doing.strip() else []


def session_contained(cwd: Path | None) -> bool:
    """Whether this session sits inside the container its launch measured.

    The fact a renderer hands ``KernelDecision.placed`` beside its own
    ``escapable``: a runtime's per-call escape reaches the host only where no
    container is between, and the question an approved crossing asks has to
    say which of the two it buys. Read here, from the same ledger the verdict
    read, so neither dispatcher spells the measurement for itself — the same
    reason ``bash_decision`` reads ``contained`` rather than being passed it.
    """
    return contained(measured_boundary(cwd))


def unconfined_by_declaration(command: str) -> bool:
    """Whether the boundary declaration takes this command out of isolation.

    A command excluded from the boundary runs unconfined because the profile
    said so, which is a grant a native escape request is spending rather than
    circumventing. Read here, beside every other reading of the same table, so
    a runtime cannot answer it differently from the classifier.
    """
    return sandbox_excluded(command, SANDBOX_EXCLUDED_COMMANDS)


def fetch_decision(url: str, root: Path | None = None) -> KernelDecision:
    """Judge one outbound fetch against the declared scopes.

    An origin no scope names answers what the project declared for it, and
    where it declared nothing, the profile's posture read from the same
    ledger the shell family reads it from, so one declaration answers on
    every surface. Read here rather than passed, because this entry point is
    what a dispatcher calls and a dispatcher holds nothing but the call.
    """
    boundary = measured_boundary(root)
    port = loopback_port(url)
    verdict = decide_fetch(
        url,
        ALLOWED_FETCH_SCOPES,
        DENIED_FETCH_SCOPES,
        UNSCOPED_FETCH or ("defer" if defers_unjudged(boundary) else "ask"),
        host_listener=port is not None and port in held_loopback_ports(boundary),
    )
    if verdict.effect != "ask":
        return verdict
    note_asked(root, approval_fingerprint("fetch", url, root), "fetch", url)
    return verdict


def held_loopback_ports(
    boundary: dict[str, list[str]], proc: Path = Path("/proc")
) -> list[int]:
    """The loopback ports this session's container does not own, where that matters.

    Only inside a container the launch measured placing its work there: a
    container sharing the host's network shares its loopback, and a port is
    this session's own only where one of the container's processes holds the
    listener -- the rest are the operator's services on the same address.
    Elsewhere nothing is withheld, since the scopes were declared for the
    machine the session runs on. A socket table nobody can read withholds
    every port.
    """
    if not contained(boundary) or not delivers(boundary, "inside_placement"):
        return []
    return host_held_ports(proc)


def refused_tool_decision(name: str, values: list[str]) -> KernelDecision | None:
    """Judge one native call against the calls this project refuses outright.

    ``None`` leaves the routing runtime's own answer for a tool no refusal
    mentions, because the table says what a project decided against and never
    what it approved — an unmentioned tool is still unclassified.
    """
    return decide_tool(name, values, REFUSED_TOOLS)


def peer_directory(cwd: Path | None) -> Path | None:
    """Where this repository's sessions meet, or nothing where none do.

    The one thing the shipped fold cannot answer for itself: which repository
    this call is in, and where beneath its shared git directory this project
    put its store. Everything inside that directory the fold knows, because
    it is the same fold the store's own library reads it with.
    """
    if PEER_POLICY is None:
        return None
    return peer_store(cwd, PEER_POLICY["store"])


def answering_member(directory: Path | None) -> str:
    """The member this hook's runtime is on the roster, blank where nothing launched it.

    The launcher's id where this runtime is the session it was minted for;
    where it inherited that id from the session's shell — a `claude -p` or a
    pipeline run there — the member it is instead, so what it changes and
    what it is asked about are its own. The runtime is the process feeding
    this hook's input, which is the runtime that fired it.
    """
    launched = (
        declared_identity(PEER_POLICY["member_env"]) if PEER_POLICY is not None else ""
    )
    if directory is None:
        return launched
    return store.own_member(directory, launched, stdin_runtime())


def peer_send_decision(values: list[str], cwd: Path | None) -> KernelDecision:
    """Judge one native send against who this repository's roster holds.

    The kernel reads no filesystem, so the roster is folded here and passed as
    the spellings it currently answers to. Every string the call carries is
    offered rather than a named field, because which field a runtime spells a
    recipient in is that runtime's business and this half answers for all of
    them.

    This session and its own subagents are left out of the spellings: a send
    between two of them never leaves the process, so it leaves nothing any
    other worktree could have read — a subagent reporting to the session that
    dispatched it, or the session steering one of its own.
    """
    directory = peer_directory(cwd)
    if PEER_POLICY is None or directory is None:
        return decide_peer_send(values, [], PEER_POLICY)
    return decide_peer_send(
        values,
        store.addresses(directory, beside=answering_member(directory)),
        PEER_POLICY,
    )


def peer_listing_decision() -> KernelDecision:
    """Judge one native listing of who this session can reach, which defers."""
    return decide_peer_listing(PEER_POLICY)


def spawn_decision(
    name: str, description: str, values: list[str], field: str
) -> KernelDecision:
    """Judge one native spawn by the name it goes out under, against what this project declared.

    ``name`` is the runtime's own field for it and ``description`` the text a
    name is read from where none was given, each read by the host half that
    knows which key that is — a runtime whose spawn carries no description
    passes ``""``. ``field`` is the name's key, so the refusal can name the
    argument; every string the call carries rides beside them so an
    escalation marker in any of them is found.
    """
    return decide_spawn(name, description, values, SPAWN_NAMES, field)


def spawn_named(name: str, description: str) -> str:
    """The name this project sends a spawn out under, the one the verdict judged.

    What a host half writes back into the call where it differs from what
    was given, so the rewrite and the verdict cannot come to disagree.
    """
    return spawn_name(name, description, SPAWN_NAMES)


def peer_listing_attachment(cwd: Path | None) -> str:
    """This repository's roster, as a listing carries it, or nothing to carry.

    Beside the verdict rather than inside it. A deferral says the runtime
    decides this call, and what the roster has to add is context a reader
    acts on rather than a condition of the call happening — folding it into
    a reason would make it visible only where something refused.
    """
    directory = peer_directory(cwd)
    if directory is None:
        return ""
    return peer_listing_context(
        store.listing_lines(directory),
        PEER_POLICY,
    )


def placed_document(path_text: str, after: str) -> str:
    """One file's text with every suppression at its canonical placement.

    Only Python has a placement to settle here: the policy is written in terms
    of a comment the formatter cannot wrap, and the tokenizer that says where
    a comment really opens is Python's.
    """
    if Path(path_text).suffix.lower() not in (".py", ".pyi"):
        return after
    return relocated_suppressions(after)


def placed_edit_text(path_text: str, after: str, start: int, end: int) -> str | None:
    """The replacement for an edit's own span, or ``None`` to place nothing."""
    if Path(path_text).suffix.lower() not in (".py", ".pyi"):
        return None
    return relocated_edit_text(after, start, end)


def resolution_of(
    reply: dict[str, dict[str, list[int]]] | None,
) -> ResolutionRow | None:
    """The kernel's row for what a checker answered, or None where none did.

    The host half returns the checker's two verdict maps as the primitives it
    read off the wire, because it may name no kernel type; this is where they
    become the row the kernel reads, and the only place the two are joined.
    """
    if reply is None:
        return None
    return ResolutionRow(refuted=reply["refuted"], unresolved=reply["unresolved"])


def followed_reading(command: str, cwd: Path) -> FollowedReading:
    """What this command line leaves in each file it writes, worked out step by step.

    Each write applied to what the writes before it left -- a rewrite run
    over the text, a patch applied to a copy, a copy or a move landing its
    source's text -- and nothing run that only running could show. Read once
    per verdict: the edit gates judge its documents, and a question records
    them as what the operator is shown.
    """
    return followed_documents(
        file_steps(command, SHELL_RULES),
        lambda target: document_at(cwd, target),
        lambda target: resolved_path(cwd, target),
        sed_output,
        lambda patch, options, program, directory, current: patched_documents(
            cwd, patch, options, program, directory, current
        ),
    )


def judged_verdicts(
    reading: FollowedReading, cwd: Path, autonomous: bool, agent_identity: str
) -> dict[str, KernelDecision]:
    """What the edit gates say about each file a command's own bytes or a rewrite reach.

    Judged as the edit the whole line makes of the file, so a second rewrite
    or an append is read against what came before it in the same line, and
    many small writes to one file meet the size gate as the one change they
    add up to.
    """
    return {
        document["path"]: document_verdict(document, cwd, autonomous, agent_identity)
        for document in judged_documents(reading)
    }


def document_verdict(
    document: FollowedDocument, cwd: Path, autonomous: bool, agent_identity: str
) -> KernelDecision:
    """One file's edit verdict, over what stood there and what the line leaves."""
    return edit_decision(
        document["path"],
        document["before"],
        document["after"],
        document["existed"],
        autonomous,
        document_operation(document),
        cwd,
        agent_identity=agent_identity,
    )


def rewritten_row(
    target: str, document: FollowedDocument, decision: KernelDecision, cwd: Path
) -> RewrittenDocumentRow:
    """One rewritten file as the classifier reads it, with the verdict it was given."""
    return RewrittenDocumentRow(
        target=target,
        path=worktree_path(document["path"]),
        before=document["before"],
        after=document["after"],
        operation=document_operation(document),
        foreign=foreign_repository(target, cwd),
        decision=decision,
        outside_project=outside_this_project(target, cwd),
        checkout_path=this_checkout_path(target, cwd),
        resolution=None,
    )


def reviewed_rows(
    reading: FollowedReading,
    judged: dict[str, KernelDecision],
    asked: bool,
    cwd: Path,
    autonomous: bool,
    agent_identity: str,
) -> tuple[FileReviewRow, ...]:
    """Each file a command changes, with its verdict and the document it would hold.

    In the order the line writes them. A file no gate read on its way to the
    verdict -- a copy's destination, a move, a removal, a patch -- is put to
    the edit gates only where somebody is asked, since only a reviewer reads
    it: that is what marks a scratch or test file as one the policy allows
    on its own, so the reviewer's default view can leave it out.
    """
    return tuple(
        row
        for document in shown_documents(reading)
        if document["path"] in judged or asked
        for row in (
            judged[document["path"]]
            if document["path"] in judged
            else document_verdict(document, cwd, autonomous, agent_identity)
        ).file_reviews
    )


def edit_decision(
    path_text: str,
    before: str | None,
    after: str | None,
    path_exists: bool,
    autonomous: bool,
    operation: str = "modify",
    cwd: Path | None = None,
    agent_identity: str = "",
) -> KernelDecision:
    """Route an edit to its authorized owner while retaining the caller's boundary."""
    path = str(((cwd or Path.cwd()) / path_text).resolve())
    # Before any owner is asked: a key or a login is this session's to be
    # kept from, whichever repository's policy the rest of the edit answers to.
    withheld = withheld_edit(path, REFUSED_PATHS)
    if withheld is not None:
        return withheld
    try:
        response = routed_edit_response(
            path,
            before,
            after,
            path_exists,
            autonomous,
            operation,
            cwd,
            agent_identity or declared_identity(identity_policy.AGENT_IDENTITY_ENV),
        )
        if response is not None:
            return captured_edit_decision(
                read_response(json.loads(response)),
                path,
                before_sha256=document_digest(before),
                after_sha256=document_digest(after),
                after=after,
            )
    except (OSError, ValueError, KeyError, TypeError) as error:
        return routing_failure(str(error))
    return captured_edit_decision(
        local_edit_decision(
            path, before, after, path_exists, autonomous, operation, cwd
        ),
        path,
        before_sha256=document_digest(before),
        after_sha256=document_digest(after),
        after=after,
    )


def local_edit_decision(
    path_text: str,
    before: str | None,
    after: str | None,
    path_exists: bool,
    autonomous: bool,
    operation: str = "modify",
    cwd: Path | None = None,
    allowances: list[str] | None = None,
    resolve_external: bool = True,
) -> KernelDecision:
    """Judge one file's before and after against the declared edit policy.

    The path is relativized against the worktree holding it rather than the
    directory the runtime started in, because every repo-relative rule matches
    on that answer and a session may be launched anywhere.

    Two facts about where the file sits are read here rather than in the
    kernel, which sees a path and no filesystem. Another repository's file
    answers to that repository's conventions and gets the referral; a file in
    no repository of ours is not this project's code either, which is all the
    gates about this project's own review notes need to decline it. The file
    as the session's own checkout spells it rides beside them, because a
    repository nested under this checkout's scratch is still this checkout's
    scratch, and only that spelling can show it.

    The gates this lease holds are read here, per call, rather than resolved
    when the session started: a grant is answered by a human while the session
    that asked for it is still running, and one resolved at launch could not
    have carried the answer.

    A checker is started only where its answer decides something. The kernel
    is asked first, from the tree and the tables alone, whether this edit
    trips a rule whose verdict turns on a resolved declaration; almost none
    do, and those are judged for nothing. Only the rest pay for a language
    server, which is the difference between a gate that costs a second per
    edit and one that costs a second on the edits that need it.
    """
    outside_this_repository = foreign_repository(path_text, cwd)
    beyond_this_project = outside_this_project(path_text, cwd)
    suffix = Path(path_text).suffix.lower()
    python_source = suffix in (".py", ".pyi")
    rows = ANTI_PATTERN_ROWS[suffix] if suffix in ANTI_PATTERN_ROWS else []
    # A checker is not started for a file this policy has already decided it
    # has nothing to say about. It would resolve another repository's imports
    # against another repository's environment to answer a rule that will not
    # be applied, and pay a language server's second for the privilege.
    resolution = resolution_of(
        resolved_refutations(path_text, after, RESOLUTION_COMMAND)
        if resolve_external
        and not outside_this_repository
        and after is not None
        and awaits_resolution(before, after, rows, python_source)
        else None
    )
    return decide_edit(
        worktree_path(path_text),
        before,
        after,
        path_exists=path_exists,
        path_rules=PATH_RULES,
        antipattern_rows=rows,
        path_roles=PATH_ROLES,
        maximum_added_lines=MAXIMUM_ADDED_LINES,
        autonomous=autonomous,
        allowances=(
            granted_allowances(ALLOWANCE_GRANTS_ENV, KNOWN_ALLOWANCES)
            if allowances is None
            else allowances
        ),
        python_source=python_source,
        acceptance_guard=ACCEPTANCE_GUARD,
        resolution=resolution,
        suffix=suffix,
        operation=operation,
        edit_rules=EDIT_RULES,
        import_boundaries=IMPORT_BOUNDARIES,
        foreign=outside_this_repository,
        outside_project=beyond_this_project,
        checkout_path=this_checkout_path(path_text, cwd),
        # lup: defer: every caller hands this a path already resolved --
        # `edit_decision` resolves it, and a routed request carries the resolved
        # path -- so this reports nothing and `edit:displaced-path` fires on no
        # runtime; the edit is judged where it lands instead, as the in-process
        # policy judges it. Either drop the gate and this call, or feed it the
        # spelled path without letting its ask preempt a deny the landing earns.
        displaced=next(
            iter(
                displaced_targets(
                    [
                        DisplacedTargetRow(path=path, lands=lands)
                        for path, lands in resolved_write_targets(
                            [path_text], cwd
                        ).items()
                    ],
                    PATH_ROLES,
                )
            ),
            None,
        ),
    )


def authored_review(
    reading: FollowedReading, judged: dict[str, KernelDecision]
) -> KernelDecision | None:
    """What the edit gates say about a write whose content the command carries.

    :func:`written_review` is the same reading a moment too late. It exists
    because a shell write was answered by its path alone -- the command
    produces its output by running, so before the fact there is nothing to
    read -- and that premise holds for `dev render > docs/api.md` and fails
    for `cat > f <<'EOF'`, where the bytes are in the command. Where they are,
    they go to the same `edit_decision` an `Edit` is put to, at the moment
    that can still change the answer.

    What that closes: a redirection declares its route reviewed, which is what
    lets the write row allow an overwrite of tracked source. For a route
    nothing could read that is the honest trade. For this one it was a hole --
    measured, `cat > packages/lup/src/lup/seams.py <<'EOF'` replaced a tracked
    library module with one line, allowed and unprompted, past the
    anti-pattern audit, the review-note gate and the size budget alike.

    The strongest verdict of the files it could read, or ``None`` where it
    read none. Each is judged as what the whole line leaves there, out of
    ``judged``, so `printf x > f && echo y >> f` is one edit of `f` rather
    than two edits of the file as it stood. An unreadable file leaves the
    write judged as it was rather than refused for being unreadable: the
    reading is a relaxation's precondition, not a gate of its own.
    """
    verdicts = [
        judged[document["path"]]
        for document in reading["documents"]
        if document["authored"] and document["path"] in judged
    ]
    if not verdicts:
        return None
    return max(verdicts, key=lambda verdict: STRENGTH.index(verdict.effect))


def written_review(
    command: str, cwd: Path, changed: list[str] | None = None, session: str = ""
) -> PostToolReport:
    """What the gates say about the files a shell command just wrote.

    The half of an edit's review a shell write cannot reach in advance. An
    `Edit` carries its content, so the note gate, the size budget and the
    anti-pattern audit read it before it lands; `dev render > docs/api.md`
    produces its content by running, so before the fact there is nothing to
    read and the write is answered by its path alone.

    Which is a smaller set than it was, and smaller here rather than only in
    the telling. A command that carries its own bytes is put to the same
    gates *before* it runs by :func:`authored_review`, so what reaches here
    is the output that genuinely did not exist yet -- and a path that reader
    already named is skipped, or an approved write would report its finding
    once on the way in and again on the way out.

    Answering it afterwards is what lets the path answer stay generous. The
    write is allowed on what can be known in advance -- a protected path, a
    generated tree -- and what only the result can settle is settled here,
    against the same `edit_decision` an edit is put to rather than a second
    reading of the same rules.

    It reports and does not undo. The command has run, so a refusal here is
    an account of what landed rather than a verdict on whether it should
    have; the agent is told, in the words the gate would have used, and what
    it does about it is the next turn's business. A refusal is blocking; what
    a question would have asked -- a suppression to approve, another
    repository's file -- is context, since nobody is left to answer it.

    A patch is the third route in, and the one that is read rather than
    resolved: `git apply` replaces tracked content wholesale by a spelling no
    content gate sees, and its targets are inside the file it is handed. Git
    reads them out, and what lands is put to the same gates as the rest --
    which is what lets that row allow instead of refusing an operation with no
    reasonable substitute.

    The words name only some of what a command writes: a script, a generator,
    an interpreter handed a file name none of these readers sees. ``changed``
    is every file the claim window measured moving across the command, among
    those differing from the commit or untracked, so a write no word names is
    reviewed as a redirect is -- and a checkout's own moves, which leave files
    matching the commit, are not. Such a file is put to the rule scan alone,
    the anti-pattern gate's refusal and nothing else: a generator rewrites its
    own trees, which the path gates refuse editing by hand, and read against
    them every regeneration would come back refused. The Python files among
    all of them are then swept as an edit is (:func:`reviewed_writes`), for
    what the rules the edit gate does not run still refuse.
    """
    carried = [write["path"] for write in authored_writes(command)]
    base = cwd.resolve()
    measured = [
        str(Path(path).relative_to(base)) if Path(path).is_relative_to(base) else path
        for path in changed or []
    ]
    named = [
        *shell_write_targets(command),
        *shell_flag_write_targets(command, SHELL_RULES),
        *patch_write_targets(shell_patch_operands(command, SHELL_RULES), cwd),
    ]
    targets = [
        target
        for target in dict.fromkeys([*named, *measured])
        # A write whose bytes were in the command went to these gates before it
        # ran, and reporting it again tells the agent the same thing twice about
        # a write somebody has already answered for.
        if target not in carried and (cwd / target).is_file()
    ]
    verdicts = [
        (target, referred_once(verdict, target, cwd, session))
        for target in targets
        for after in [text_at(cwd, target)]
        if after is not None
        for verdict in [
            edit_decision(
                target,
                committed_text(target, cwd),
                after,
                path_exists=True,
                # There is nobody to ask about a file already written, so the
                # gates are put the question in the form that states what they
                # found rather than the one that offers somebody a choice.
                autonomous=True,
                cwd=cwd,
            )
        ]
        if target in named
        or (verdict.effect == "deny" and verdict.rule == "edit:anti-pattern")
    ]
    # The edit gate answered the line rules against the commit; the sweep adds
    # what only it runs, so the two never name one finding twice.
    answered = [row["id"] for rows in ANTI_PATTERN_ROWS.values() for row in rows]
    return merged(
        [
            PostToolReport(
                blocking=[
                    f"{target}: {verdict.addressed()}"
                    for target, verdict in verdicts
                    if verdict.effect == "deny"
                ],
                context=[
                    f"{target}: {verdict.addressed()}"
                    for target, verdict in verdicts
                    if verdict.effect not in ("allow", "deny")
                    # Said once already: the file is only another
                    # repository's, which the agent was told.
                    and not (
                        verdict.rule == "edit:foreign-repository"
                        and not verdict.recovery
                    )
                ],
            ),
            reviewed_writes(
                [
                    str(cwd / target)
                    for target in targets
                    if not foreign_repository(target, cwd)
                ],
                cwd,
                answered=answered,
                diagnosed=False,
            ),
        ]
    )


def merged(reports: list[PostToolReport]) -> PostToolReport:
    """Several reports about one call, as the one report its runtime delivers."""
    return PostToolReport(
        blocking=[line for report in reports for line in report["blocking"]],
        context=[line for report in reports for line in report["context"]],
    )


def reviewed_writes(
    paths: list[str],
    cwd: Path | None,
    answered: list[str] | None = None,
    diagnosed: bool = True,
) -> PostToolReport:
    """What the checks after a write say about the files it wrote.

    The sweep goes first because it rewrites what it repairs, and a type
    check run before it describes lines that have since moved. What it still
    refuses is blocking: it is the whole-tree check scoped to these files,
    every rule over every span, so nothing the gate ahead of the write could
    not see is first met at the end. *answered* names rules another gate
    already reported for this write, and those are left to it.
    """
    swept = swept_files(paths, REPAIR_COMMAND)
    skipped = answered or []

    def refused(path: str, file: dict) -> list[str]:
        shown = worktree_path(path)
        return [
            f"{shown}:{finding['line']}: {finding['message']} "
            f"({finding['kind']}, rule {finding['rule_id']})"
            for finding in file["refused"]
            if finding["rule_id"] not in skipped
        ]

    return merged(
        [
            *(repair_report(path, file, cwd) for path, file in swept.items()),
            PostToolReport(
                blocking=[
                    line for path, file in swept.items() for line in refused(path, file)
                ],
                context=[],
            ),
            *(
                PostToolReport(blocking=found["blocking"], context=found["context"])
                for path in (paths if diagnosed else [])
                for found in [file_diagnostics(path, DIAGNOSTICS_COMMAND)]
            ),
        ]
    )


def repair_report(path: str, file: dict, cwd: Path | None) -> PostToolReport:
    """What the sweep's repair of one file comes to, under this session's policy.

    The sweep judges by the checkout's rules and the gate ahead of the write
    by the policy this session loaded, and the two differ whenever the
    sources moved since the launch -- after a rename, the gate demanded a
    `# lup: ignore[seam-boundary]` the sweep then deleted as dead, and every
    later edit to the file was refused for the missing directive. So the
    repair is put to that policy as an edit: where it would refuse taking a
    directive out, the file goes back to what was written, and the agent is
    told the two disagree rather than meeting the refusal on its next edit.
    Every removal is said either way, because a line that vanishes unsaid is
    one the agent writes again.
    """
    shown = worktree_path(path)
    after = text_at(Path(path).parent, Path(path).name)
    if not file["repaired"] or file["written"] is None or after is None:
        return PostToolReport(
            blocking=[], context=[f"{shown}: {line}" for line in file["repaired"]]
        )
    verdict = local_edit_decision(
        path,
        file["written"],
        after,
        path_exists=True,
        autonomous=True,
        cwd=cwd,
        resolve_external=False,
    )
    if verdict.effect != "deny" or verdict.rule != "edit:anti-pattern":
        return PostToolReport(
            blocking=[], context=[f"{shown}: {line}" for line in file["repaired"]]
        )
    Path(path).write_text(file["written"], encoding="utf-8")
    return PostToolReport(
        blocking=[],
        context=[
            f"{shown}: left as written. The sweep called its directives dead by "
            "this checkout's rules, and the policy this session loaded still "
            "needs one of them; the two agree again once `uv run lup-devtools "
            "harness generate all` runs and the session restarts. What the "
            "loaded policy said about the repair:",
            verdict.reason,
        ],
    )


def referred_once(
    verdict: KernelDecision, path_text: str, cwd: Path | None, session: str
) -> KernelDecision:
    """Another repository's referral, said in full once per repository per session.

    The referral's second sentence -- that the repository's conventions are
    its own and the rule checker is not applying any of them -- is true of
    every file in that repository and news only the first time. Printed on
    every edit it was read about 150 times by one agent, which is the noise
    this project's own "say it once" refuses. So the verdict stands on every
    edit and its recovery goes with the first (:func:`referral_noted`).
    """
    if verdict.rule != "edit:foreign-repository" or not session or cwd is None:
        return verdict
    repository = worktree_root(str((cwd / path_text).resolve())) or path_text
    if referral_noted(cwd, session, repository):
        return verdict.revised(recovery="")
    return verdict


def foreign_claim_decision(
    path_text: str, cwd: Path | None, caller: store.Caller
) -> KernelDecision | None:
    """Whether a live member other than this caller is already in the named file.

    The roster and the claim record are both live, so both are folded here and
    handed over as the names they resolve to — the kernel reads no filesystem
    and decides from what it is given.

    *caller* is the conversation making the call, as its runtime's host half
    read it off the payload: a subagent is judged as its own row, so its
    sibling's claims are asked about and its session's are not.

    The file is named as its checkout spells it, so one edit reads the same
    whichever spelling the call used: named as given, a claim made an absolute
    and a relative spelling of one edit two different answers.
    """
    directory = peer_directory(cwd)
    if PEER_POLICY is None or directory is None:
        return None
    session = answering_member(directory)
    return decide_foreign_claim(
        worktree_path(str(((cwd or Path.cwd()) / path_text).resolve())),
        store.claim_holders(
            directory,
            path_text,
            store.acting_id(session, caller),
            session=session,
        ),
        PEER_POLICY,
    )


def claim_window_opened(cwd: Path | None, caller: store.Caller) -> None:
    """Snapshot the tree before a command whose writes no input names.

    Only a command needs this. Every other writing call says which file it is
    about, and a call that names its own target is attributed from the target
    rather than from a comparison. The window is the calling row's own, so
    two subagents' commands running at once each close their own.
    """
    if PEER_POLICY is None:
        return
    open_claim_window(
        cwd,
        PEER_POLICY["store"],
        PEER_POLICY["windows_dir"],
        store.acting_id(answering_member(peer_directory(cwd)), caller),
    )


def claim_window_closed(cwd: Path | None, caller: store.Caller) -> list[str]:
    """Attribute what a command changed, contested where nothing could tell.

    What changed is returned too, since it is the one account of a command's
    writes that does not depend on the command naming them.
    """
    if PEER_POLICY is None:
        return []
    directory = peer_directory(cwd)
    session = answering_member(directory)
    closed = close_claim_window(
        cwd,
        PEER_POLICY["store"],
        PEER_POLICY["windows_dir"],
        store.acting_id(session, caller),
    )
    if directory is not None:
        store.record_claims(
            directory, store.acting(directory, session, caller), closed["paths"]
        )
    return closed["paths"]


def named_claim_recorded(
    path_text: str, cwd: Path | None, caller: store.Caller
) -> None:
    """Attribute a change to the exact file the call named.

    The tier that needs no comparison: the call said which file, so what this
    leaves on the calling row's own file — the subagent's where one made the
    call, joined on the way — is evidence of the state that row left the path
    in, rather than of what a before-and-after could narrow the writer down to.
    """
    directory = peer_directory(cwd)
    if PEER_POLICY is None or directory is None or not path_text:
        return
    store.record_claims(
        directory,
        store.acting(directory, answering_member(directory), caller),
        [str(Path(path_text).resolve())],
    )


def edit_claim_decision(
    verdict: KernelDecision, path_text: str, cwd: Path | None, caller: store.Caller
) -> KernelDecision:
    """One edit's own verdict, settled together with any claim over its path.

    The join lives here rather than in either runtime, so both reach the same
    answer about the same file: what the content gates decided, and whether
    somebody else is already in it, are two questions and one approval.
    """
    return settled_with_claim(verdict, foreign_claim_decision(path_text, cwd, caller))
