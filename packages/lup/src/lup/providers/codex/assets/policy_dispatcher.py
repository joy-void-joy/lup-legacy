"""Codex's half of the compiled hook dispatcher.

:mod:`lup.policy.dispatcher` compiles this module together with the shared
host half into the plugin's `hooks/scripts/policy.py`, so this is not itself
a script. It holds only what Codex spells for itself: the environment naming
the home it installs trusted packages beneath, relativization against the
worktree rather than the launch directory, the tools it routes, the patch
envelope it decodes into per-file edits, and the fail-closed exit it takes
where the command-hook boundary offers no way to ask.

A call none of those three decode is put to the refusal table before it earns
the unclassified ask, so this file never names a tool it has no semantics for.
Which names a runtime offers is that runtime's own fact; which of them a
project has decided against is the application's — so the tools a refusal adds
to the routed set come from the declaration rather than from a list here.

The imports below resolve against the generated runtime the compiled script
sits beside, which is why this file is type-checked against that tree rather
than against the workspace.
"""

import json
import os
import sys
from hashlib import sha256
from pathlib import Path

# The hook is launched as a bare script, promised no cwd, PYTHONPATH, or
# interpreter environment, and `runtime/` is a plain sibling directory holding
# the kernel package and this plugin's policy data rather than an installed
# distribution. Naming it as a search path is what lets the imports below
# resolve, for the interpreter and for a type checker alike.
sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
from codex_patch import patched_files, patched_paths
from decisions import (
    edit_claim_decision,
    held_refusal,
    unconfined_by_declaration,
    bash_decision,
    edit_decision,
    family_hold_report,
    claim_window_closed,
    claim_window_opened,
    fetch_decision,
    merged,
    named_claim_recorded,
    referred_once,
    refused_tool_decision,
    review_policy_identity,
    reviewed_decision,
    reviewed_writes,
    session_contained,
    shell_preimages,
    spawn_decision,
    spawn_named,
    written_review,
)
from host import (
    approval_fingerprint,
    approval_subject,
    boundary_account,
    closed_deadline,
    declared_identity,
    hook_started,
    note_ran,
    observe_hook_call,
    opened_deadline,
    publish_edition,
    read_document,
    record_hook_evidence,
    sandbox_active,
    unjudged_reason,
    ran_out,
    words_before,
)
from kernel.rows import PostToolReport
from kernel.review import Said
from kernel.decision import KernelDecision
from kernel.decision import unjudged_recovery
from kernel.diagnostic import Step, devtools, spelled, stated, step
from kernel.review import literal_input
from kernel.shell import auto_escape_matches
from caller_payload import caller_of, spoken, transcript_of
from policy_data import AUTO_ESCAPE_PREFIXES
from policy_data import AGENT_IDENTITY_ENV, AUTONOMOUS_AGENT_IDENTITIES
from policy_data import HOOK_DEADLINE_SECONDS

# Read by the entry point the compiler writes, which hands it to the warden.
from policy_data import HOOK_ANSWER_SECONDS


def hook_environment():
    """The native environment passed to this bare hook process."""
    # lup: ignore[os-environ] — bare hooks have no settings package
    return os.environ


def plugin_data_root():
    """The plugin-owned writable directory Codex gives hook processes."""
    environ = hook_environment()
    root = environ["PLUGIN_DATA"] if "PLUGIN_DATA" in environ else ""
    return Path(root) if root else None


def managed_root():
    """The home Codex installs and trusts packages beneath."""
    environ = hook_environment()
    return Path(environ["CODEX_HOME"]) if "CODEX_HOME" in environ else None


def spent_escape(tool_input):
    """Whether this call requested Codex's per-command sandbox escape."""
    return (
        "sandbox_permissions" in tool_input
        and tool_input["sandbox_permissions"] == "require_escalated"
    )


def joined(decisions):
    """Join one envelope's files: deny beats ask beats defer beats allow."""
    evidence = tuple(row for decision in decisions for row in decision.file_reviews)
    for effect in ("deny", "ask", "defer"):
        for decision in decisions:
            if decision.effect == effect:
                return decision.revised(file_reviews=evidence)
    return KernelDecision(
        "allow", "every patched file is declared safe", file_reviews=evidence
    )


def patch_changes(command, cwd):
    """Resolve both the preimage and policy path against the tool's directory."""
    root = cwd or Path.cwd()

    def document(path):
        return read_document(str(root / path))

    changes = patched_files(command, document)
    for change in changes:
        change.path = str(root / change.path)
    return changes


def patch_decision(command, cwd, autonomous, caller, session=""):
    """Judge every decoded path, including the source of a move and peer claims."""
    return joined(
        [
            edit_claim_decision(
                referred_once(
                    edit_decision(
                        change.path,
                        change.before,
                        change.after,
                        change.path_exists,
                        autonomous,
                        change.operation(),
                        cwd,
                    ),
                    change.path,
                    cwd,
                    session,
                ),
                change.path,
                cwd,
                caller,
            )
            for change in patch_changes(command, cwd)
        ]
    )


def dispatch(payload, permission_request=False):
    name = payload["tool_name"]
    tool_input = payload["tool_input"]
    # Where this session is rooted, which is what says whether a patched file
    # belongs to the repository being worked on or to somebody else's. Read
    # once, because the shell path and the patch path ask the same question of
    # it and a second read is a second place it can be forgotten.
    session_directory = Path(payload["cwd"]) if "cwd" in payload else None
    # Whether this session is a reviewed worker, which decides two unrelated
    # things: how a patch is judged, and whether a refusal has a route to
    # name. Read once at the top rather than inside either branch, since
    # both branches need it.
    agent_type = payload["agent_type"] if "agent_type" in payload else ""
    autonomous = (
        agent_type in AUTONOMOUS_AGENT_IDENTITIES
        or declared_identity(AGENT_IDENTITY_ENV) in AUTONOMOUS_AGENT_IDENTITIES
    )
    # Which conversation of this session made the call, so what it holds and
    # what it is asked about are its own roster row's — a subagent's where
    # one called — read the way the caller hook reads it for the tool server.
    caller = caller_of(payload)
    session = payload["session_id"] if "session_id" in payload else ""
    if name == "Bash":
        envelope = literal_input(tool_input["command"], "apply_patch")
        if envelope is not None:
            return patch_decision(
                envelope, session_directory, autonomous, caller, session
            )
        requested_escape = spent_escape(tool_input)
        # The snapshot a comparison afterwards is read against, taken only on
        # the event that runs immediately before the call: a permission
        # request runs before the prompt, and a window opened there would span
        # however long somebody took to answer it.
        if not permission_request:
            claim_window_opened(session_directory, caller)
        escaped = requested_escape or auto_escape_matches(
            tool_input["command"], AUTO_ESCAPE_PREFIXES
        )
        decision = bash_decision(
            tool_input["command"],
            managed_root(),
            False if permission_request else sandbox_active(),
            interactive=True,
            park=False,
            # A native prefix rule can auto-escape one simple command; an explicit
            # request is the other supported route. Both are checked against
            # semantic placement before the hook lets the native boundary act.
            escapable=escaped,
            cwd=session_directory,
            # A reviewed worker's session has nobody at a keyboard and is not
            # therefore alone: the run it belongs to carries a mailbox that
            # reaches whoever is supervising it.
            relayed=autonomous,
            # The same identity the edit branches are given, because a command
            # carrying its own content reaches the same gates.
            autonomous=autonomous,
        )
        # PreToolUse can neither see nor place every native escape. Let Codex's
        # sandbox run a confined call or raise the PermissionRequest where this
        # same policy can judge the requested escape instead of preempting it.
        if not permission_request and decision.capability == "host_executor":
            return KernelDecision("defer", decision.reason)
        if (
            requested_escape
            and decision.effect == "allow"
            and decision.sandbox != "outside"
            and not unconfined_by_declaration(tool_input["command"])
        ):
            return KernelDecision(
                "deny",
                f"call requested outside but policy places it {decision.sandbox}; remove sandbox_permissions and retry",
            )
        return decision
    if name == "web_fetch":
        # The same directory the shell branch reads its boundary from: the
        # profile's answer for what nothing classified is one declaration,
        # not one per surface.
        return fetch_decision(tool_input["url"], session_directory)
    if name == "apply_patch":
        return patch_decision(
            tool_input["command"], session_directory, autonomous, caller, session
        )
    if name == "collaborationspawn_agent":
        # Measured on 0.155.1 and 0.158.0: the spawn carries `task_name` and
        # `message`, and the hook names the tool this way. No description to
        # read a name out of, so a spawn with no task name is refused where
        # Claude's half would name it, and one misspelled goes out normalized.
        return spawn_decision(
            tool_input["task_name"] if "task_name" in tool_input else "",
            "",
            [value for value in tool_input.values() if isinstance(value, str)],
            "task_name",
        )
    # Asked of whatever reached here rather than of a listed few, exactly as
    # the Claude half asks it: which tools are worth refusing is the
    # declaration's answer, and a runtime that shipped the table without
    # consulting it would read as a refusal in force while the call went
    # through. The branches above keep their calls, which have semantics.
    refused = refused_tool_decision(
        name, [value for value in tool_input.values() if isinstance(value, str)]
    )
    if refused is not None:
        return refused
    return KernelDecision("ask", f"unknown tool {name!r} is not covered by policy")


def named_input(payload):
    """The spawn's arguments under the name it goes out with, or ``None`` to send it as written.

    The one call this half rewrites. Codex takes `updatedInput` only beside
    `permissionDecision: "allow"`, replacing a tool's whole arguments object —
    its hook documentation says so, and 0.158.0 measured it: a spawn the model
    named `probe-child`, rewritten to `hooked_child` this way, went out as
    `/root/hooked_child`, while an MCP call's rewrite with no decision was
    dropped and the call ran as written. A spawn raises no approval
    of its own there, so the allow that carries the name settles nothing the
    deferral it spells would have left to anybody.
    """
    if payload["tool_name"] != "collaborationspawn_agent":
        return None
    tool_input = payload["tool_input"]
    given = tool_input["task_name"] if "task_name" in tool_input else ""
    named = spawn_named(given, "")
    return None if named in ("", given) else {**tool_input, "task_name": named}


def waiting(command, payload):
    """How the conversation that asked hears the operator's answer, in its shell tool's words.

    The session's own thread holds no waiter. Where no `review wait` holds
    the review, the operator's answer goes to its mailbox and is queued into
    its thread with `codex queue`, which starts a turn in an idle thread --
    measured on 0.158.0, a browser decision reaches a second turn that way
    -- and the `review wait` it runs then carries the call out at once.

    A subagent's last message is its report, and nothing wakes it after: a
    command it leaves running outlives the report and wakes nobody, measured
    on 0.155.1 and again on 0.158.0, and a message to it waits for its next
    tool call. So it holds the waiter in its shell tool and reads it before
    it reports, starting it again without telling anybody where it ends
    with the review still waiting: a waiter ending is no news.
    """
    if "agent_id" in payload:
        return (
            step(
                "carry on with other work, and hold this in your shell tool,"
                " reading its output before you report: once a subagent reports,"
                " nothing wakes it, and a command it left running wakes nobody",
                command,
            ),
            step(
                "if it ends with the review still waiting, start it again"
                " quietly, reporting that to nobody"
            ),
        )
    return (
        step(
            "carry on with other work, or end your turn: the operator's answer is"
            " queued into this thread, which starts a turn, so don't start a"
            " waiter"
        ),
        step("once it wakes you, carry the call out at once", command),
    )


def account(payload) -> list[Said]:
    """What the agent said this call is for, each with where it was found.

    The ``justification`` Codex's shell tool carries beside a request to run
    outside its sandbox, which the model writes to say why, and the words the
    agent wrote since it last heard anything, read back off the rollout of
    the conversation making the call. Codex's tools carry no note saying
    what a command does, as Claude Code's ``description`` does, so a call
    staying inside its sandbox is accounted for by the words alone. Either is
    left out where it says nothing.
    """
    tool_input = payload["tool_input"]
    justified = tool_input["justification"] if "justification" in tool_input else ""
    transcript = transcript_of(payload)
    preceding = words_before(transcript, spoken) if transcript is not None else ""
    return [
        *(
            [Said(source="justification", text=justified)]
            if isinstance(justified, str) and justified.strip()
            else []
        ),
        *([Said(source="preceding", text=preceding)] if preceding.strip() else []),
    ]


def queued_review(payload, decision):
    """Both judging events require the same explicit review authority.

    Returns the verdict and the line the operator is shown beside a refusal.
    """
    cwd = Path(payload["cwd"]) if "cwd" in payload else Path.cwd()
    tool_input = payload["tool_input"]
    name = payload["tool_name"]
    command = tool_input["command"] if "command" in tool_input else ""
    envelope = (
        command
        if name == "apply_patch"
        else literal_input(command, "apply_patch")
        if name == "Bash"
        else None
    )
    before = {
        **(
            {
                Path(change.path).resolve(): change.before
                for change in patch_changes(envelope, cwd)
            }
            if envelope is not None
            else {}
        ),
        **(shell_preimages(command, cwd) if name == "Bash" else {}),
    }
    reviewed = reviewed_decision(
        decision,
        cwd,
        payload["session_id"] if "session_id" in payload else "",
        name,
        tool_input,
        before,
        lambda command: waiting(command, payload),
        payload["tool_use_id"] if "tool_use_id" in payload else "",
        payload["hook_event_name"] if "hook_event_name" in payload else "",
        "PreToolUse"
        if "hook_event_name" in payload
        and payload["hook_event_name"] == "PermissionRequest"
        else "",
        policy_identity=review_policy_identity(cwd, Path(__file__)),
        provider="codex",
        agent=payload["agent_id"] if "agent_id" in payload else "",
        account=account(payload),
    )
    return reviewed["decision"], reviewed["notice"]


def remembered_run(payload):
    """Record what executed; a native execution event conveys no authority."""
    name = payload["tool_name"] if "tool_name" in payload else ""
    tool_input = payload["tool_input"] if "tool_input" in payload else {}
    subject = approval_subject(name, tool_input)
    if subject is None:
        return
    root = Path(payload["cwd"]) if "cwd" in payload else None
    note_ran(root, approval_fingerprint(subject["kind"], subject["text"], root))


def patch_snapshot(payload):
    """One native call's before-image record, without storing command contents."""
    root = plugin_data_root()
    if root is None or not payload.get("session_id") or not payload.get("tool_use_id"):
        return None
    identity = {
        key: payload.get(key)
        for key in ("session_id", "tool_use_id", "cwd", "tool_input")
    }
    digest = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return root / "patch-snapshots" / f"{digest}.json"


def patch_stamps(payload):
    """Digest named files so a failed or partly applied patch is observed honestly."""
    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command", "")
    name = payload.get("tool_name", "")
    envelope = (
        command
        if name == "apply_patch"
        else literal_input(command, "apply_patch")
        if name == "Bash"
        else None
    )
    if not command or envelope is None:
        return None
    directory = Path(payload.get("cwd") or Path.cwd())
    stamps = {}
    for path in patched_paths(envelope):
        target = directory / path
        try:
            stamps[str(target)] = sha256(target.read_bytes()).hexdigest()
        except FileNotFoundError:
            stamps[str(target)] = None
    return stamps


def remember_patch(payload):
    """Capture the allowed call immediately before native execution."""
    path = patch_snapshot(payload)
    stamps = patch_stamps(payload)
    if path is None or stamps is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(stamps), encoding="utf-8")


def observe(payload):
    """Record each patched path and run the shared post-edit checks."""
    root = payload["cwd"] if "cwd" in payload else ""
    if root:
        publish_edition(root, root)
    tool_input = payload["tool_input"] if "tool_input" in payload else {}
    command = tool_input["command"] if "command" in tool_input else ""
    if not command:
        return PostToolReport(blocking=[], context=[])
    name = payload["tool_name"] if "tool_name" in payload else ""
    envelope = (
        command if name == "apply_patch" else literal_input(command, "apply_patch")
    )
    if envelope is not None:
        directory = Path(root) if root else Path.cwd()
        snapshot = patch_snapshot(payload)
        if snapshot is None or not snapshot.is_file():
            return PostToolReport(
                blocking=[
                    "Patch diagnostics have no matching PreToolUse snapshot; run dev check to verify the result."
                ],
                context=[],
            )
        before = json.loads(snapshot.read_text(encoding="utf-8"))
        snapshot.unlink()
        after = patch_stamps(payload)
        assert after is not None
        changed = [
            target
            for target, stamp in after.items()
            if target in before and before[target] != stamp
        ]
        for target in changed:
            publish_edition(target, str(directory))
            named_claim_recorded(target, directory, caller_of(payload))
        return merged(
            [
                family_hold_report(changed, directory, caller_of(payload)),
                reviewed_writes(changed, directory),
            ]
        )
    # What the command changed, read against the snapshot its own PreToolUse
    # took, and contested where another session had a window open across it.
    changed = claim_window_closed(Path(root) if root else None, caller_of(payload))
    return merged(
        [
            family_hold_report(
                changed, Path(root) if root else None, caller_of(payload)
            ),
            written_review(
                command,
                Path(root) if root else Path.cwd(),
                changed,
                payload["session_id"] if "session_id" in payload else "",
            ),
            PostToolReport(
                blocking=[],
                context=boundary_account(
                    payload["tool_response"] if "tool_response" in payload else "",
                    Path(root) if root else None,
                ),
            ),
        ]
    )


def post_tool_answer(report):
    """One post-tool report, in the two channels this runtime reads it by.

    What a gate still refuses goes through stderr and exit 2, the channel
    measured carrying post-tool feedback here; what is only worth knowing
    joins it after a blank line. With nothing refused, what is worth
    knowing goes as ``hookSpecificOutput.additionalContext`` on stdout and
    the hook exits normally, which Codex adds as developer context beside
    the result (https://learn.chatgpt.com/docs/hooks, PostToolUse) — so a
    removed directive or a name an edit is about to supply no longer
    replaces the tool's result as if something had failed.
    """
    if report["blocking"]:
        sys.stderr.write(
            "\n\n".join(
                "\n".join(lines)
                for lines in (report["blocking"], report["context"])
                if lines
            )
        )
        raise SystemExit(2)
    if report["context"]:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "\n".join(report["context"]),
                }
            },
            sys.stdout,
        )


def unjudged_detail(event, error, read):
    """What this hook says of a call nobody judged, on stderr or in a denial.

    ``error`` is what failed, or None where the judgement was still running
    when the hook had to answer. A post-tool check has nothing left to
    refuse -- the patch has applied, and retrying it would apply it again --
    so it says only that it did not finish.
    """
    if event == "PostToolUse":
        failure = error if error is not None else "it did not finish in time"
        return f"Lup post-tool check failed: {failure}"
    return KernelDecision(
        "deny", unjudged_reason(error, read), recovery=unjudged_recovery(ran_out(error))
    ).addressed()


def unanswered(given, error):
    """Refuse a call the judgement did not answer in time, as its event reads it.

    A permission request is refused by the decision it carries back, as
    every verdict this hook gives one is; every other event by the reason on
    stderr and the exit status this boundary refuses with. ``error`` is
    what failed, or None where the judgement was still running when the
    hook had to answer.
    """
    try:
        payload = json.loads(given)
    except ValueError:
        payload = {}
    named = isinstance(payload, dict) and "hook_event_name" in payload
    event = payload["hook_event_name"] if named else ""
    detail = unjudged_detail(event, error, True)
    if event == "PermissionRequest":
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": detail},
                }
            },
            sys.stdout,
        )
        return
    sys.stderr.write(detail)
    raise SystemExit(2)


def held(given):
    """Keep a call waiting while a hold covers its caller, before anything judges it.

    Only a call about to run is held: not the event watching one that ran,
    and not the permission request Codex raises for a call already let go
    past this one. True where the call was refused here -- still held at the
    hold's limit -- by the reason on stderr and the exit status this
    boundary refuses with; False where a hold kept it waiting and then let it
    go to be judged; None where nothing held it or no hold reaches its
    event. Input nothing can read is let go to the judgement, which refuses
    it in its own words.

    A hold this cannot read is let go to the judgement too, which meets the
    same failure and refuses in its own words, or judges the call at once.
    The call is not let through unheld for it: the hold hook registered
    beside this one for every tool holds it as well, and refuses wherever it
    cannot tell. What this adds is only that a held call is judged when let
    go, not when it was made.
    """
    try:
        payload = json.loads(given)
    except ValueError:
        return None
    return held_input(payload) if isinstance(payload, dict) else None


def held_input(payload):
    """The hold of one decoded hook input, as :func:`held` answers it."""
    if "hook_event_name" not in payload or payload["hook_event_name"] != "PreToolUse":
        return None
    try:
        refused = held_refusal(
            payload["tool_name"] if "tool_name" in payload else "",
            payload["tool_use_id"] if "tool_use_id" in payload else "",
            Path(payload["cwd"]) if "cwd" in payload else None,
            lambda: caller_of(payload),
            hook_started(),
        )
    except Exception:
        return None
    if refused is None:
        return None
    if not refused:
        return False
    sys.stderr.write(refused)
    raise SystemExit(2)


def judged(given):
    """Judge one hook input and answer it, the whole of what this hook decides."""
    previous = opened_deadline(HOOK_DEADLINE_SECONDS)
    payload = {}
    permission_request = False
    review_notice = ""
    event = ""
    read = False
    try:
        payload = json.loads(given)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        read = True
        permission_request = (
            "hook_event_name" in payload
            and payload["hook_event_name"] == "PermissionRequest"
        )
        record_hook_evidence(plugin_data_root(), payload, "started")
        # Watching and deciding are separate events, and this one returns
        # before a verdict exists: the patch has already applied, so there is
        # nothing left to permit, and the fail-closed exit below would refuse
        # a call that already happened.
        event = payload["hook_event_name"] if "hook_event_name" in payload else ""
        if event == "PostToolUse":
            remembered_run(payload)
            observed = observe_hook_call(
                Path(payload["cwd"]) if "cwd" in payload else Path.cwd(),
                payload["session_id"] if "session_id" in payload else "",
                payload["tool_name"],
                payload["tool_input"],
                payload["tool_use_id"] if "tool_use_id" in payload else "",
            )
            report = merged(
                [PostToolReport(blocking=observed, context=[]), observe(payload)]
            )
            detail = "\n".join([*report["blocking"], *report["context"]])
            record_hook_evidence(
                plugin_data_root(), payload, "completed", "observed", detail or None
            )
            post_tool_answer(report)
            return
        decision = dispatch(payload, permission_request)
        # A verdict from here places nothing: this hook answers, and the call
        # runs with the arguments the model wrote, so a placement is degraded
        # to its plain effect rather than carrying an intent no channel here
        # performs. Asking for the launcher's host is a marker a reviewer
        # answers, and it reaches the same relay under every runtime, so
        # nothing about that route depends on this channel existing. The
        # measured placement is handed in all the same, so the kernel seam
        # receives from this dispatcher exactly what it receives from the
        # other: with no channel it settles nothing here, and the moment a
        # channel exists the question already says where the call lands.
        root = Path(payload["cwd"]) if "cwd" in payload else None
        decision = decision.placed(escapable=False, contained=session_contained(root))
        # Native approval mode does not prove who answers. Both judging events
        # require a recorded reviewer answer for this exact pending call, asked
        # as it is rendered: every reason the verdict joined.
        if decision.effect == "ask":
            decision, review_notice = queued_review(payload, decision)
        if not permission_request and decision.effect in ("allow", "defer"):
            remember_patch(payload)
    # Every way this can fail means one thing — the call went unjudged — and
    # one answer is right for all of them. Naming the exceptions instead
    # would let a plain unreadable file escape, and a traceback exit is not the
    # fail-closed exit this boundary takes, so the call would proceed
    # ungoverned.
    # Nothing is swallowed: the reason names which cause it was, carrying
    # whatever went wrong, and an interrupt still passes through as the
    # BaseException it is.
    except Exception as error:
        record_hook_evidence(
            plugin_data_root(),
            payload,
            "failed",
            "error",
            f"{type(error).__name__}: {error}",
        )
        decision = KernelDecision(
            "deny",
            unjudged_reason(error, read),
            recovery=unjudged_recovery(ran_out(error)),
        )
        if not permission_request:
            sys.stderr.write(unjudged_detail(event, error, read))
            raise SystemExit(2) from error
    finally:
        closed_deadline(previous)
    if permission_request and decision.effect != "defer":
        allowed = decision.effect == "allow"
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "allow" if allowed else "deny",
                        **({} if allowed else {"message": decision.addressed()}),
                    },
                }
            },
            sys.stdout,
        )
        record_hook_evidence(
            plugin_data_root(), payload, "completed", "allow" if allowed else "deny"
        )
        return
    if decision.effect in ("allow", "defer"):
        renamed = None if permission_request else named_input(payload)
        if renamed is not None:
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "updatedInput": renamed,
                    }
                },
                sys.stdout,
            )
        record_hook_evidence(plugin_data_root(), payload, "completed", decision.effect)
        return
    # A successful structured denial carries the operator's line beside the
    # agent's reason; exit 2 discards systemMessage. Both routes stop the
    # native tool invocation.
    detail = decision.addressed()
    # The journal is metadata-only: the reason names the refused input, which
    # for a fetch is the full URL, so it stays out of the metadata journal.
    record_hook_evidence(plugin_data_root(), payload, "completed", decision.effect)
    if review_notice:
        json.dump(
            {
                "systemMessage": review_notice,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": detail,
                },
            },
            sys.stdout,
        )
        return
    sys.stderr.write(detail)
    raise SystemExit(2)
