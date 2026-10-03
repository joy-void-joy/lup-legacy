"""Claude Code's half of the compiled hook dispatcher.

:mod:`lup.policy.dispatcher` compiles this module together with the shared
host half into the plugin's `hooks/scripts/policy.py`, so this is not itself
a script. It holds only what Claude Code spells for itself: the environment
naming the root it installs trusted packages beneath, relativization against
the launch directory, the tools it routes, and the conservative ask it
returns through its own decision channel for input nothing can decide from.

A call none of those four decode is put to the refusal table before it earns
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
from pathlib import Path

# The hook is launched as a bare script, promised no cwd, PYTHONPATH, or
# interpreter environment, and `runtime/` is a plain sibling directory holding
# the kernel package and this plugin's policy data rather than an installed
# distribution. Naming it as a search path is what lets the imports below
# resolve, for the interpreter and for a type checker alike.
sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
from decisions import (
    bash_decision,
    dashboard_held,
    edit_decision,
    claim_window_closed,
    claim_window_opened,
    edit_claim_decision,
    family_hold_report,
    fetch_decision,
    merged,
    named_claim_recorded,
    peer_listing_attachment,
    peer_listing_decision,
    peer_send_decision,
    placed_document,
    placed_edit_text,
    referred_once,
    refused_tool_decision,
    review_policy_identity,
    reviewed_decision,
    reviewed_writes,
    session_contained,
    shell_preimages,
    spawn_decision,
    spawn_named,
    spawn_notice_report,
    written_review,
)
from host import (
    approval_fingerprint,
    approval_subject,
    boundary_account,
    closed_deadline,
    declared_identity,
    note_ran,
    observe_hook_call,
    opened_deadline,
    publish_edition,
    read_document,
    record_hook_evidence,
    sandbox_active,
    unjudged_reason,
    unjudged_recovery,
    words_before,
)
from kernel.rows import PostToolReport
from kernel.review import Said
from kernel.decision import KernelDecision, sandbox_escaped
from caller_payload import caller_of, spoken, transcript_of
from policy_data import (
    AGENT_IDENTITY_ENV,
    AUTONOMOUS_AGENT_IDENTITIES,
    # Read by the entry point the compiler writes, which hands it to the warden.
    HOOK_ANSWER_SECONDS,
    HOOK_DEADLINE_SECONDS,
)


def announced(effect, tool_name, reason, dialogs=("Edit", "Write")):
    """What a verdict has to say that its own prompt will not carry, or "".

    *dialogs* names the calls whose approval prompt is this runtime's own
    dialog. Claude Code renders a hook's ``permissionDecisionReason`` in the
    prompt it raises for a shell command, and drops it in the one it raises
    for a file write — that dialog shows the path, a preview and the two
    answers, and takes nothing from a hook. Measured against 2.1.237, in both
    directions.

    Silent wherever the prompt already speaks. A shell command's prompt shows
    the reason, so repeating it would say everything twice; a reason of one
    line names a category and adds nothing to a dialog already showing the
    file and its content. What is left is the case this exists for — a verdict
    that enumerated something the approver cannot otherwise see, about a call
    whose prompt drops it.

    ``systemMessage`` is the one field this runtime displays to a person from
    every hook, and it arrives with the tool call rather than with the prompt,
    so this informs rather than gates. That is the whole of what is reachable:
    the reason is dropped here, and ``PermissionRequest`` — the event that runs
    before the prompt — did not fire for a hook's ask in a plain `-p` run on
    2.1.283 and fired only under `--permission-prompts none`, so no field of
    it is one this prompt is known to carry.

    The colour is spelled here rather than in the kernel because it is this
    terminal's alphabet. The kernel states the sites; a runtime that shows them
    some other way is showing the same verdict.
    """
    lines = reason.splitlines()
    if effect != "ask" or tool_name not in dialogs or len(lines) < 2:
        return ""
    return "\n".join([lines[0], *(f"\033[33m{site}\033[0m" for site in lines[1:])])


def plugin_data_root():
    """The plugin-owned writable directory Claude Code gives hook processes."""
    environ = os.environ  # lup: ignore[os-environ]
    root = environ["CLAUDE_PLUGIN_DATA"] if "CLAUDE_PLUGIN_DATA" in environ else ""
    return Path(root) if root else None


def managed_root():
    """The root Claude Code installs and trusts packages beneath."""
    environ = os.environ  # lup: ignore[os-environ]
    if "CLAUDE_CONFIG_DIR" in environ:
        return Path(environ["CLAUDE_CONFIG_DIR"])
    if "HOME" in environ:
        return Path(environ["HOME"]) / ".claude"
    return None


def edit_documents(path, old_text, new_text, replace_all):
    """Build the before and after documents one Edit call would produce.

    A `replace_all` edit rewrites every occurrence, so requiring exactly one
    would reject the tool's own semantics — and a rejection here is not a
    judgment: it reaches the agent as an approval prompt that no rule
    produced, which leaves a whole class of edit ungoverned.
    """
    current = Path(path).read_text(encoding="utf-8")
    occurrences = current.count(old_text)
    if occurrences == 0:
        raise ValueError("Edit preimage does not occur in the file")
    if replace_all:
        # Reproducing the Edit tool's own splice: source text has no parser
        # here, and the preimage is a literal the caller already chose.
        spliced = current.replace(old_text, new_text)  # lup: ignore[string-replace]
        return current, spliced
    if occurrences != 1:
        raise ValueError("Edit preimage must occur exactly once")
    position = current.find(old_text)
    updated = current[:position] + new_text + current[position + len(old_text) :]
    return current, updated


def spent_escape(tool_input):
    """Whether the call as written already asked to run outside the sandbox.

    A fact about where this call would land, never a request Lup honours:
    asking for the launcher's host is `# lup: escalate[sandbox]: <why>`, which
    is reviewed. What this answers is narrower and only ever tightens — a call
    carrying the flag is not confined by the native sandbox, so unjudged work
    in it has no boundary to be carried by and re-enters the stricter lattice.
    """
    return (
        "dangerouslyDisableSandbox" in tool_input
        and tool_input["dangerouslyDisableSandbox"] is True
    )


def placed_input(payload):
    """The tool arguments that send the same call out in the shape the policy keeps.

    The correcting route rather than the refusing one: where a directive was
    written somewhere the placement policy does not keep it, or a spawn went
    out with no name or one no runtime here would take, the call goes out
    rewritten instead of coming back as a complaint, so nobody weighs a reason
    against a column count, or guesses at an argument the schema they read
    does not list. For a directive this is what `ruff --add-noqa` does for its
    own, moved to the gate that already reads the edit.

    ``None`` says place nothing. An edit can only rewrite the text it supplies,
    so a move reaching outside that text declines rather than guesses — and a
    `replace_all` edit has no single span to read a move back out of. A spawn
    whose name goes out as given, or that no name can be read for, is left
    to its verdict.
    """
    name = payload["tool_name"]
    tool_input = payload["tool_input"]
    if name == "Agent":
        given = tool_input["name"] if "name" in tool_input else ""
        named = spawn_named(
            given, tool_input["description"] if "description" in tool_input else ""
        )
        return None if named in ("", given) else {**tool_input, "name": named}
    if name not in ("Edit", "Write"):
        return None
    path = tool_input["file_path"]
    if Path(path).suffix.lower() not in (".py", ".pyi"):
        return None
    if name == "Write":
        content = tool_input["content"]
        revised = placed_document(path, content)
        return None if revised == content else {**tool_input, "content": revised}
    if "replace_all" in tool_input and tool_input["replace_all"] is True:
        return None
    before, after = edit_documents(
        path, tool_input["old_string"], tool_input["new_string"], False
    )
    start = before.find(tool_input["old_string"])
    revised = placed_edit_text(
        path, after, start, start + len(tool_input["new_string"])
    )
    return None if revised is None else {**tool_input, "new_string": revised}


def session_root(payload):
    """Where this session is rooted, which its relative operands resolve against.

    A hook is promised nothing about where it runs, so the payload's own answer
    is the only one there is. Read through here by both halves of an answer —
    the verdict and whatever rides beside it — because a second spelling of it
    is a second place it can be forgotten.
    """
    return Path(payload["cwd"]) if "cwd" in payload else None


def parks():
    """Whether an ask is parked for the operator rather than put to a prompt.

    Wherever the launch holds a dashboard, a reviewer reads what parks there,
    for this session and every subagent and `-p` run inside it alike. Where
    none is held, nobody reads a parked question until they run a terminal
    command for it, so every ask -- a person's included -- is this runtime's
    own prompt, where the person already is. Measured on 2.1.283 in an
    interactive auto-mode session: a hook's ask raises the prompt, and the
    call has not run a minute later with nobody answering.
    """
    return dashboard_held()


def waiting(command, payload):
    """How the conversation that asked hears the operator's answer, in its tool's words.

    The session's own conversation holds no waiter. Where no `review wait`
    holds the review, the operator's answer goes to its mailbox and wakes it
    through its wake socket -- an idle interactive session took a turn on
    it, measured live -- and the `review wait` it runs then carries the call
    out at once. A waiter held instead ended at the tool's limit every two
    hours and woke the session for nothing.

    A subagent is woken by nothing but its own background work: a message to
    it waits for its next tool call. So it holds the waiter, started with
    `run_in_background` and the longest timeout the tool takes -- two hours,
    the Bash tool's own schema on 2.1.285 -- and told to end itself a minute
    sooner, saying the review still waits, rather than leave a bare timeout.
    It starts it again without telling anybody: a waiter ending is no news.

    A `-p` run's background commands end with it, about five seconds after
    its final result, and nothing wakes a run that ended --
    `CLAUDE_CODE_ENTRYPOINT` is `sdk-cli` there, measured on 2.1.283 -- so it
    waits in the foreground once nothing else is left, under the ten minutes
    the tool takes there.
    """
    if "agent_id" in payload:
        return (
            f"Carry on with other work, and hold `{command} --timeout 7140` in the "
            "background (run_in_background, with the longest timeout the tool "
            "takes, 7200000 ms): nothing else wakes a subagent, and it wakes you "
            "with the result. If it ends with the review still waiting, start it "
            "again quietly, reporting that to nobody."
        )
    if declared_identity("CLAUDE_CODE_ENTRYPOINT") == "cli":
        return (
            "Carry on with other work, or end your turn: the operator's answer "
            f"wakes this session, and `{command}` then carries the call out at "
            "once. Don't start a waiter."
        )
    return (
        "Carry on with other work. This run ends with its last turn and nothing "
        "wakes it after, so once nothing else is left, run "
        f"`{command} --timeout 540` in the foreground (the longest timeout the "
        "tool takes there, 600000 ms), again each time it ends still waiting."
    )


def preimages(payload, cwd):
    """What each file the call would change holds now, keyed where it resolves."""
    tool_input = payload["tool_input"]
    match payload["tool_name"]:
        case "Edit" | "Write":
            named = Path(tool_input["file_path"])
            target = named if named.is_absolute() else cwd / named
            return {
                target.resolve(): (
                    target.read_text(encoding="utf-8", newline="")
                    if target.is_file()
                    else None
                )
            }
        case "Bash":
            return shell_preimages(tool_input["command"], cwd)
        case _:
            return {}


def account(payload) -> list[Said]:
    """What the agent said this call is for, each with where it was found.

    The ``description`` Claude Code's Bash tool carries beside a command,
    which the model writes to say what the command does, and the words the
    agent wrote since it last heard anything, read back off the transcript
    of the conversation making the call. Either is left out where it says
    nothing.
    """
    tool_input = payload["tool_input"]
    described = tool_input["description"] if "description" in tool_input else ""
    transcript = transcript_of(payload)
    preceding = words_before(transcript, spoken) if transcript is not None else ""
    return [
        *(
            [Said(source="description", text=described)]
            if isinstance(described, str) and described.strip()
            else []
        ),
        *([Said(source="preceding", text=preceding)] if preceding.strip() else []),
    ]


def queued_review(payload, decision, placed):
    """Park one ask in the review queue, or spend the operator's recorded answer.

    The preimages are the files the call would change as they stand now, and
    the exact input it would run with is the one placed, so what the operator
    approves is what `review wait` or a retry carries out.
    """
    cwd = session_root(payload) or Path.cwd()
    return reviewed_decision(
        decision,
        cwd,
        payload["session_id"] if "session_id" in payload else "",
        payload["tool_name"],
        payload["tool_input"],
        preimages(payload, cwd),
        lambda command: waiting(command, payload),
        payload["tool_use_id"] if "tool_use_id" in payload else "",
        execution_payload=placed,
        policy_identity=review_policy_identity(cwd, Path(__file__)),
        provider="claude",
        agent=payload["agent_id"] if "agent_id" in payload else "",
        account=account(payload),
    )


def dispatch(payload):
    name = payload["tool_name"]
    tool_input = payload["tool_input"]
    # Where this session is rooted, which is what says whether an edited file
    # belongs to the repository being worked on or to somebody else's. Read
    # once, because the shell path and the edit path ask the same question of
    # it and a second read is a second place it can be forgotten.
    session_directory = session_root(payload)
    agent_identity = (
        payload["agent_type"] if "agent_type" in payload else ""
    ) or declared_identity(AGENT_IDENTITY_ENV)
    autonomous = agent_identity in AUTONOMOUS_AGENT_IDENTITIES
    # Which conversation of this session made the call, so what it holds and
    # what it is asked about are its own roster row's — a subagent's where
    # one called — read the way the caller hook reads it for the tool server.
    caller = caller_of(payload)
    session = payload["session_id"] if "session_id" in payload else ""
    if name == "Bash":
        unsandboxed = spent_escape(tool_input)
        # A command names no file it will write, so what it changed can only
        # be read afterwards against what stood here before it ran.
        claim_window_opened(session_directory, caller)
        return bash_decision(
            tool_input["command"],
            managed_root(),
            sandbox_active() and not unsandboxed,
            interactive=True,
            # A call's sandbox is an argument of the call here, so a verdict
            # that has to leave the sandbox is carried out rather than refused.
            escapable=True,
            cwd=session_directory,
            # A reviewed worker's session has nobody at a keyboard and is not
            # therefore alone: the run it belongs to carries a mailbox that
            # reaches whoever is supervising it.
            relayed=autonomous,
            # The same identity the edit branches below are given, because a
            # command carrying its own content reaches the same gates.
            autonomous=autonomous,
            park=False,
            agent_identity=agent_identity,
        )
    if name == "WebFetch":
        # The same directory the shell branch reads its boundary from: the
        # profile's answer for what nothing classified is one declaration,
        # not one per surface.
        return fetch_decision(tool_input["url"], session_directory)
    if name == "Edit":
        path = tool_input["file_path"]
        before, after = edit_documents(
            path,
            tool_input["old_string"],
            tool_input["new_string"],
            "replace_all" in tool_input and tool_input["replace_all"] is True,
        )
        return edit_claim_decision(
            referred_once(
                edit_decision(
                    path,
                    before,
                    after,
                    Path(path).exists(),
                    autonomous,
                    "modify",
                    session_directory,
                    agent_identity=agent_identity,
                ),
                path,
                session_directory,
                session,
            ),
            path,
            session_directory,
            caller,
        )
    if name == "Write":
        path = tool_input["file_path"]
        exists = Path(path).exists()
        return edit_claim_decision(
            referred_once(
                edit_decision(
                    path,
                    read_document(path),
                    tool_input["content"],
                    exists,
                    autonomous,
                    "overwrite" if exists else "create",
                    session_directory,
                    agent_identity=agent_identity,
                ),
                path,
                session_directory,
                session,
            ),
            path,
            session_directory,
            caller,
        )
    if name == "SendMessage":
        # Every string the call carries rather than a named field, the reading
        # the refusal table already takes: which key this runtime spells a
        # recipient in is its own business, and the roster answers for all of
        # them. A target nobody on it answers to passes through untouched, and
        # so does this session or one of its own subagents: a subagent
        # reporting to `main` or the session steering its subagent never
        # leaves the process, so there is no record another worktree misses.
        return peer_send_decision(
            [value for value in tool_input.values() if isinstance(value, str)],
            session_directory,
        )
    if name == "ListAgents":
        # Nothing to permit and nothing to refuse: it answers for a population
        # wider than one repository, so the roster rides alongside as context
        # rather than as a verdict that could take the answer away.
        return peer_listing_decision()
    if name == "Agent":
        # A spawn is judged by the one thing that makes its subagent legible
        # and addressable: the name it goes out under, read out of the
        # description every spawn here carries where none was given.
        return spawn_decision(
            tool_input["name"] if "name" in tool_input else "",
            tool_input["description"] if "description" in tool_input else "",
            [value for value in tool_input.values() if isinstance(value, str)],
            "name",
        )
    # Asked of whatever reached here rather than of a listed few: which tools
    # are worth refusing is the declaration's answer, and naming any of them
    # here would be this file holding a second, narrower copy of it. The
    # branches above keep their calls, which have semantics to be judged by.
    refused = refused_tool_decision(
        name, [value for value in tool_input.values() if isinstance(value, str)]
    )
    if refused is not None:
        return refused
    return KernelDecision("ask", "tool is not classified")


def attachment(name, cwd):
    """What one call carries back beside its verdict, or nothing to carry.

    Only the listing has any. It answers for a wider population than this
    repository's roster, and the roster is exactly what a reader needs beside
    it to tell the two apart — every other call has no second population to be
    confused with, so nothing is folded for it and nothing is paid.
    """
    if name != "ListAgents":
        return ""
    return peer_listing_attachment(cwd)


def rendered(decision, payload, placed, attached):
    """Answer one call on the permission channel, and place it on the other.

    Claude Code takes a call's sandbox as an argument of the call rather than
    as part of the verdict, so a placed decision goes out as the permission
    decision plus a rewrite of the arguments — which is what makes an
    unprompted placement reachable at all. Three things in the runtime make
    that rewrite carry the flag rather than swallow it, each read out of the
    shipped Claude Code binary at version 2.1.228 — the baseline to re-check
    against, rather than a conclusion to remember: the PreToolUse hook
    schema types `updatedInput` as an open record of arbitrary keys;
    `dangerouslyDisableSandbox` is a declared field of the shell tool's own
    input schema, so it is not an unknown key for the schema validation a
    returned `updatedInput` has to pass; and the one per-tool key filter
    applied to it before execution is keyed by a table naming a different
    tool, so for the shell tool the object arrives whole and the sandbox is
    chosen from it. What remains outside this file's reach is the session
    itself: a host that forbids unsandboxed commands ignores the flag, and
    the call runs confined with the verdict unchanged. A contained launch
    never arms the per-call sandbox at all, so there the flag lifts nothing
    and the call runs in the container's own mount namespace — which is why
    the sentence an approved crossing adds to its question is chosen by the
    placement the launch measured, handed to the kernel beside `escapable`,
    and never by this runtime.

    The rewrite replaces the arguments rather than merging into them, so the
    whole input is carried through. A deferral places no sandbox, which is
    also why nothing here reads a payload a deferral may not have parsed.

    ``placed`` carries the other rewrite this channel can hand back: the same
    edit with its suppression directives at their canonical placement, or the
    same spawn under the name it goes out with. It rides along with the
    verdict rather than replacing it — placing a directive or a name settles
    how the call is spelled and says nothing about whether it may happen, so
    an ask still asks, over the placed text, which is what the approver should
    be reading. A deferral carries it too, as ``updatedInput`` with no
    ``permissionDecision``, which Claude Code takes as the arguments alone and
    leaves the permission where it was: read out of 2.1.283, whose hook result
    yields a bare rewrite exactly when no behaviour was given, validates it
    against the tool's full input schema, and is measured recording an
    unnamed spawn rewritten this way under the name the hook gave it. A denied
    call runs nothing, so there is nothing to place. The rewrites never
    contend for the one ``updatedInput`` field: a directive is placed only in
    an ``Edit`` or ``Write``, a name only in an ``Agent``, and the sandbox
    argument belongs only to ``Bash``.

    The rewrite is how a verdict places a call, and it is the whole of what
    this field does: whether a placement leaves the boundary is the kernel's
    `sandbox_escaped` rather than a condition spelled here, because the
    in-process seam fills the same field and two conditions written twice are
    two conditions that can be written differently. A call the agent wrote the
    field on itself is overwritten rather than read: asking for the host is a
    reviewed request spelled `# lup: escalate[sandbox]:`, and a native flag the
    agent set for itself is not that request.
    """
    settled = decision.placed(
        escapable=True, contained=session_contained(session_root(payload))
    )
    # The prompt is the approver's, so a question's recovery rides beside it
    # as the agent's context; a refusal reaches only the agent and says both.
    beside = settled.recovery if settled.effect == "ask" else ""
    context = "\n\n".join(text for text in (attached, beside) if text)

    def carried(result):
        """The same answer, with whatever context rides beside the verdict.

        Beside rather than inside, and it survives a deferral: the whole point
        of attaching a roster to a listing is that the listing goes ahead, so
        the one answer that says "this runtime decides" is the one that most
        needs to carry it. An empty attachment adds no key, so a call with
        nothing to say returns exactly what it returned before.
        """
        if not context:
            return result
        specific = (
            result["hookSpecificOutput"]
            if "hookSpecificOutput" in result
            else {"hookEventName": "PreToolUse"}
        )
        return {
            **result,
            "hookSpecificOutput": {**specific, "additionalContext": context},
        }

    if settled.effect == "defer":
        return carried(
            {}
            if placed is None
            else {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": placed,
                }
            }
        )
    answer = {
        "hookEventName": "PreToolUse",
        "permissionDecision": settled.effect,
        "permissionDecisionReason": (
            settled.addressed() if settled.effect == "deny" else settled.reason
        ),
    }

    def surfaced(result):
        """The same verdict, with what this runtime will not show it said."""
        message = announced(settled.effect, payload["tool_name"], settled.reason)
        return carried({**result, "systemMessage": message} if message else result)

    if placed is not None and settled.effect != "deny":
        return surfaced({"hookSpecificOutput": {**answer, "updatedInput": placed}})
    if settled.sandbox == "ambient" or payload["tool_name"] != "Bash":
        return surfaced({"hookSpecificOutput": answer})
    return surfaced(
        {
            "hookSpecificOutput": {
                **answer,
                "updatedInput": {
                    **payload["tool_input"],
                    "dangerouslyDisableSandbox": sandbox_escaped(settled.sandbox),
                },
            }
        }
    )


def remembered_run(payload):
    """Record what executed; a native execution event conveys no authority."""
    name = payload["tool_name"] if "tool_name" in payload else ""
    tool_input = payload["tool_input"] if "tool_input" in payload else {}
    subject = approval_subject(name, tool_input)
    if subject is None:
        return
    root = session_root(payload)
    note_ran(root, approval_fingerprint(subject["kind"], subject["text"], root))


def observe(payload):
    """Record where a write landed and read it, deciding nothing.

    Claude Code names the file the same way for both editing tools, so the
    one key is the whole reading of an edit. A payload without it is a call
    this event is registered for and has nothing to say about, which is not a
    failure — the matcher is narrow, but the runtime owns it, and a tool that
    stops carrying a path should cost a recorded edition rather than an error.

    A shell command is the other shape, and it names no file: what it wrote
    is whatever running it produced. That is exactly what no gate could read
    before the fact, so the gates an edit passes on the way in are put to a
    command's result on the way out — which is what lets the verdict before
    it ran answer from the path alone and stay generous about the content.

    A spawn is the third, and leaves a name behind rather than a write: the
    call arrives as it ran, its `PreToolUse` rewrite included (measured on
    2.1.285), so a spawn that went out under its description's name is one
    whose caller chose none, and is told once to choose its own.
    """
    tool_input = payload["tool_input"] if "tool_input" in payload else {}
    if "tool_name" in payload and payload["tool_name"] == "Agent":
        return spawn_notice_report(
            tool_input["name"] if "name" in tool_input else "",
            tool_input["description"] if "description" in tool_input else "",
            "name",
            session_root(payload),
            payload["session_id"] if "session_id" in payload else "",
            caller_of(payload),
        )
    path = tool_input["file_path"] if "file_path" in tool_input else ""
    if path:
        publish_edition(path, str(session_root(payload) or ""))
        # The tier that needs no comparison: the call said which file, so the
        # claim it leaves is one another session can act on unqualified.
        named_claim_recorded(path, session_root(payload), caller_of(payload))
        return merged(
            [
                family_hold_report([path], session_root(payload), caller_of(payload)),
                reviewed_writes([path], session_root(payload)),
            ]
        )
    command = tool_input["command"] if "command" in tool_input else ""
    if not command:
        return PostToolReport(blocking=[], context=[])
    # What the command changed, read against the snapshot its own PreToolUse
    # took, and contested where another session had a window open across it.
    changed = claim_window_closed(session_root(payload), caller_of(payload))
    return merged(
        [
            family_hold_report(changed, session_root(payload), caller_of(payload)),
            written_review(
                command,
                session_root(payload) or Path.cwd(),
                changed,
                payload["session_id"] if "session_id" in payload else "",
            ),
            # What the boundary refused, named as the boundary rather than
            # left as an errno the agent would debug as a broken disk.
            PostToolReport(
                blocking=[],
                context=boundary_account(
                    payload["tool_response"] if "tool_response" in payload else "",
                    session_root(payload),
                ),
            ),
        ]
    )


def post_tool_answer(report):
    """One post-tool report, in the two channels this runtime reads it by.

    What a gate still refuses goes as ``decision: "block"``, whose reason
    Claude Code shows the agent as feedback to act on before it moves on.
    What is only worth knowing goes as ``additionalContext``, which the
    runtime wraps as a system reminder beside the result and does not label
    as an error: https://code.claude.com/docs/en/hooks, "PostToolUse
    decision control". One channel for both labelled a name another edit
    was about to supply, and a justified suppression, as a blocking error.
    """
    answer = (
        {"decision": "block", "reason": "\n".join(report["blocking"])}
        if report["blocking"]
        else {}
    )
    if not report["context"]:
        return answer
    return {
        **answer,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(report["context"]),
        },
    }


def unjudged_answer(event, error, read):
    """The answer to a call nobody judged, on the channel its event reads.

    ``error`` is what failed, or None where the judgement was still running
    when the hook had to answer. A refusal says what to do next, as every
    refusal this runtime shows does; a post-tool check has nothing left to
    refuse and says only that it did not finish.
    """
    if event == "PostToolUse":
        failure = error if error is not None else "it did not finish in time"
        return {"decision": "block", "reason": f"Lup post-tool check failed: {failure}"}
    refusal = KernelDecision(
        "deny", unjudged_reason(error, read), recovery=unjudged_recovery(error)
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": refusal.addressed(),
        }
    }


def unanswered(given, error):
    """Refuse a call the judgement did not answer in time, as its event reads it."""
    try:
        payload = json.loads(given)
    except ValueError:
        payload = {}
    named = isinstance(payload, dict) and "hook_event_name" in payload
    json.dump(
        unjudged_answer(payload["hook_event_name"] if named else "", error, True),
        sys.stdout,
    )


def judged(given):
    """Judge one hook input and answer it, the whole of what this hook decides."""
    previous = opened_deadline(HOOK_DEADLINE_SECONDS)
    payload = {}
    event = ""
    placed = None
    attached = ""
    notice = ""
    read = False
    try:
        payload = json.loads(given)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        read = True
        record_hook_evidence(plugin_data_root(), payload, "started")
        event = payload["hook_event_name"] if "hook_event_name" in payload else ""
        # Watching and deciding are separate events, and this one returns
        # before a verdict exists: the tool has already run, so there is
        # nothing left to permit, and the conservative ask below would be an
        # approval prompt for work already done.
        if event == "PostToolUse":
            remembered_run(payload)
            observed = observe_hook_call(
                session_root(payload) or Path.cwd(),
                payload["session_id"] if "session_id" in payload else "",
                payload["tool_name"],
                payload["tool_input"],
                payload["tool_use_id"] if "tool_use_id" in payload else "",
            )
            report = merged(
                [PostToolReport(blocking=observed, context=[]), observe(payload)]
            )
            # Structured feedback reaches the agent beside the completed tool.
            # A file diagnostic is a successful check, so it exits normally.
            detail = "\n".join([*report["blocking"], *report["context"]])
            record_hook_evidence(
                plugin_data_root(), payload, "completed", "observed", detail or None
            )
            json.dump(post_tool_answer(report), sys.stdout)
            return
        decision = dispatch(payload)
        placed = placed_input(payload)
        attached = attachment(payload["tool_name"], session_root(payload))
        # A parked ask is refused while it waits, telling the agent it is
        # queued rather than refused and how to wait on it; an ask that does
        # not park is rendered as this runtime's own prompt, where the person
        # already is. It is parked as it would be rendered -- every reason
        # it joined, and what its placement crosses -- since that is the
        # question the operator answers.
        if decision.effect == "ask" and parks():
            asked = decision.placed(
                escapable=True, contained=session_contained(session_root(payload))
            )
            reviewed = queued_review(payload, asked, placed)
            decision = reviewed["decision"]
            notice = reviewed["notice"]
    # Every way this can fail means one thing — the call went unjudged — and
    # one answer is right for all of them. Naming the exceptions instead
    # would let a plain unreadable file escape, and the traceback exit reaches
    # PreToolUse as a non-blocking error, so the call would proceed ungoverned.
    # Nothing is swallowed: the reason carries whatever went wrong, and an
    # interrupt still passes through as the BaseException it is.
    except Exception as error:
        record_hook_evidence(
            plugin_data_root(),
            payload if isinstance(payload, dict) else {},
            "failed",
            "error",
            f"{type(error).__name__}: {error}",
        )
        json.dump(unjudged_answer(event, error, read), sys.stdout)
        return
    finally:
        closed_deadline(previous)
    answer = rendered(decision, payload, placed, attached)
    # The person is told a review is waiting on them in the one field this
    # runtime shows a person from every hook; the agent reads the reason.
    json.dump({**answer, "systemMessage": notice} if notice else answer, sys.stdout)
    detail = (
        decision.reason
        if decision.effect == "deny" and payload["tool_name"] != "WebFetch"
        else None
    )
    record_hook_evidence(
        plugin_data_root(), payload, "completed", decision.effect, detail
    )
