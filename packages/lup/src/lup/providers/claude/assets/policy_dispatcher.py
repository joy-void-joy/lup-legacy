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
    edit_decision,
    claim_window_closed,
    claim_window_opened,
    edit_claim_decision,
    fetch_decision,
    named_claim_recorded,
    peer_listing_attachment,
    peer_listing_decision,
    peer_send_decision,
    placed_document,
    placed_edit_text,
    refused_tool_decision,
    session_contained,
    spawn_decision,
    written_review,
)
from host import (
    approval_fingerprint,
    approval_subject,
    boundary_account,
    closed_deadline,
    declared_identity,
    file_diagnostics,
    note_ran,
    observe_hook_call,
    opened_deadline,
    publish_edition,
    read_document,
    record_hook_evidence,
    repaired_directives,
    sandbox_active,
)
from kernel.decision import KernelDecision, sandbox_escaped
from policy_data import (
    AGENT_IDENTITY_ENV,
    AUTONOMOUS_AGENT_IDENTITIES,
    DIAGNOSTICS_COMMAND,
    HOOK_DEADLINE_SECONDS,
    REPAIR_COMMAND,
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
    before the prompt — belongs to the control protocol rather than to a local
    plugin, and never fires for one.

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
    """The tool arguments that land the same edit with its directives placed.

    The correcting route rather than the refusing one: where a directive was
    written somewhere the placement policy does not keep it, the call goes out
    rewritten instead of coming back as a complaint, so nobody weighs a reason
    against a column count while writing one. This is what `ruff --add-noqa`
    does for its own directives, moved to the gate that already reads the
    edit.

    ``None`` says place nothing. An edit can only rewrite the text it supplies,
    so a move reaching outside that text declines rather than guesses — and a
    `replace_all` edit has no single span to read a move back out of.
    """
    name = payload["tool_name"]
    tool_input = payload["tool_input"]
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
    if name == "Bash":
        unsandboxed = spent_escape(tool_input)
        # A command names no file it will write, so what it changed can only
        # be read afterwards against what stood here before it ran.
        claim_window_opened(session_directory)
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
        )
    if name == "Write":
        path = tool_input["file_path"]
        exists = Path(path).exists()
        return edit_claim_decision(
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
        )
    if name == "SendMessage":
        # Every string the call carries rather than a named field, the reading
        # the refusal table already takes: which key this runtime spells a
        # recipient in is its own business, and the roster answers for all of
        # them. A target nobody on it answers to passes through untouched,
        # which is what leaves subagent continuation and every other session
        # this repository does not hold working untouched.
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
        # and addressable: the name it carries. The runtime validates the
        # spelling; this only insists there is one.
        return spawn_decision(
            tool_input["name"] if "name" in tool_input else "",
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
    whole input is carried through. A deferral is placed nowhere, which is
    also why nothing here reads a payload a deferral may not have parsed.

    ``placed`` carries the other rewrite this channel can hand back: the same
    edit with its suppression directives at their canonical placement. It
    rides along with the verdict rather than replacing it — placing a
    directive settles where it is written and says nothing about whether the
    edit may happen, so an ask still asks, over the placed text, which is what
    the approver should be reading. A denied call runs nothing, so there is
    nothing to place. The two rewrites never contend for the one
    ``updatedInput`` field: a directive is placed only in an ``Edit`` or
    ``Write``, and the sandbox argument belongs only to ``Bash``.

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
        return carried({})
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
    """
    tool_input = payload["tool_input"] if "tool_input" in payload else {}
    path = tool_input["file_path"] if "file_path" in tool_input else ""
    if path:
        publish_edition(path)
        # The tier that needs no comparison: the call said which file, so the
        # claim it leaves is one another session can act on unqualified.
        named_claim_recorded(path, session_root(payload))
        # Repaired before checked, because the repair rewrites the file: run
        # the other way round and the diagnostics describe lines that have
        # already moved. Both reports reach the agent together, which is the
        # one channel this event has.
        return repaired_directives(path, REPAIR_COMMAND) + file_diagnostics(
            path, DIAGNOSTICS_COMMAND
        )
    command = tool_input["command"] if "command" in tool_input else ""
    if not command:
        return []
    # What the command changed, read against the snapshot its own PreToolUse
    # took, and contested where another session had a window open across it.
    claim_window_closed(session_root(payload))
    return [
        *written_review(command, session_root(payload) or Path.cwd()),
        # What the boundary refused, named as the boundary rather than left
        # as an errno the agent would debug as a broken disk.
        *boundary_account(
            payload["tool_response"] if "tool_response" in payload else "",
            session_root(payload),
        ),
    ]


def main():
    # What each runtime gives this hook before it lets the call through,
    # less what starting Python and writing the verdict take: every step a
    # verdict waits on shares it, and anything still waiting past it is
    # refused rather than left for the runtime to wave through.
    previous = opened_deadline(HOOK_DEADLINE_SECONDS)
    payload = {}
    event = ""
    placed = None
    attached = ""
    failed = False
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        record_hook_evidence(plugin_data_root(), payload, "started")
        event = payload["hook_event_name"] if "hook_event_name" in payload else ""
        # Watching and deciding are separate events, and this one returns
        # before a verdict exists: the tool has already run, so there is
        # nothing left to permit, and the conservative ask below would be an
        # approval prompt for work already done.
        if event == "PostToolUse":
            remembered_run(payload)
            found = observe_hook_call(
                session_root(payload) or Path.cwd(),
                payload["session_id"] if "session_id" in payload else "",
                payload["tool_name"],
                payload["tool_input"],
                payload["tool_use_id"] if "tool_use_id" in payload else "",
            ) + observe(payload)
            # Structured feedback reaches the agent beside the completed tool.
            # A file diagnostic is a successful check, so it exits normally.
            if found:
                detail = "\n".join(found)
                record_hook_evidence(
                    plugin_data_root(), payload, "completed", "observed", detail
                )
                json.dump({"decision": "block", "reason": detail}, sys.stdout)
                return
            json.dump({}, sys.stdout)
            record_hook_evidence(plugin_data_root(), payload, "completed", "observed")
            return
        decision = dispatch(payload)
        placed = placed_input(payload)
        attached = attachment(payload["tool_name"], session_root(payload))
        # An ask is rendered here, because this runtime has a channel for a
        # question: the prompt, where the person already is. A recorded
        # receipt is what a runtime with no ask effect falls back to, which
        # is Codex's PreToolUse, and a question put where nobody is standing
        # is read by nobody.
        #
        # The limit this accepts: an autonomy mode can answer the prompt
        # itself, and no field here says whether a person saw one. The verdict
        # reaches whoever the session is answering to, which in that mode is
        # the mode.
    # Every way this can fail means one thing — the call went unjudged — and
    # one answer is right for all of them. Naming the exceptions instead is
    # what let a plain unreadable file escape, and the traceback exit reaches
    # PreToolUse as a non-blocking error, so the call proceeded ungoverned.
    # Nothing is swallowed: the reason carries whatever went wrong, and an
    # interrupt still passes through as the BaseException it is.
    except Exception as error:
        failed = True
        decision = KernelDecision("deny", f"Lup could not judge this call: {error}")
        record_hook_evidence(
            plugin_data_root(),
            payload if isinstance(payload, dict) else {},
            "failed",
            "error",
            f"{type(error).__name__}: {error}",
        )
        if event == "PostToolUse":
            json.dump(
                {"decision": "block", "reason": f"Lup post-tool check failed: {error}"},
                sys.stdout,
            )
            return
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": decision.reason,
                }
            },
            sys.stdout,
        )
        return
    finally:
        closed_deadline(previous)
    json.dump(rendered(decision, payload, placed, attached), sys.stdout)
    if not failed:
        detail = (
            decision.reason
            if decision.effect == "deny" and payload["tool_name"] != "WebFetch"
            else None
        )
        record_hook_evidence(
            plugin_data_root(), payload, "completed", decision.effect, detail
        )
