#!/usr/bin/env python3
# Generated from lup.policy.assets.host and lup.providers.claude.assets.policy_dispatcher by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

"""Claude Code hook dispatcher over the canonical semantic kernel.

Runs as a bare script beside its own runtime directory, reaching only
the standard library and the kernel copied beside it.
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
from kernel.rows import PostToolReport
from kernel.review import Said
from kernel.decision import KernelDecision, sandbox_escaped
from kernel.decision import unjudged_recovery
from kernel.diagnostic import Step, devtools, spelled, stated, step
from caller_payload import caller_of, spoken, transcript_of
from policy_data import (
    AGENT_IDENTITY_ENV,
    AUTONOMOUS_AGENT_IDENTITIES,
    # Read by the entry point the compiler writes, which hands it to the warden.
    HOOK_ANSWER_SECONDS,
    HOOK_DEADLINE_SECONDS,
)
import ast
import csv
import fcntl
import select
import signal
from hashlib import sha256
import tempfile
import time
from datetime import UTC, datetime, timedelta

# lup: ignore[subprocess] — `sh` is third-party and this half is compiled into a bare script that has no virtual environment to resolve it from
import subprocess
from collections.abc import Callable, Iterator
from typing import BinaryIO, Literal
from urllib.parse import urlsplit
import policy_data as identity_policy
from kernel.diagnostic import diagnostic, rendered as rendered_said
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
from kernel.spawns import decide_spawn, spawn_name, spawn_notice
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
    GENERATED_PLUGIN_ROOTS,
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


def policy_snapshot_files(directory: Path) -> list[Path]:
    """The executable source of one hermetic destination evaluator."""
    evaluator = directory / "scripts" / "policy_evaluator.py"
    runtime = directory / "runtime"
    if not evaluator.is_file() or not (runtime / "policy_data.py").is_file():
        raise ValueError(f"Generated policy evaluator is missing beneath {directory}")
    entries = [
        directory,
        directory / "scripts",
        evaluator,
        runtime,
        *runtime.rglob("*"),
    ]
    if any(item.is_symlink() for item in entries):
        raise ValueError(f"Generated policy evaluator contains a symlink: {directory}")
    if any(
        item.is_file()
        and item.suffix != ".py"
        and item.name != "evidence.json"
        and not (item.suffix == ".pyc" and item.parent.name == "__pycache__")
        for item in entries
    ):
        raise ValueError(
            f"Generated policy evaluator contains unsupported code: {directory}"
        )
    return sorted([evaluator, *runtime.rglob("*.py")])


def policy_snapshot_digest(directory: Path) -> str:
    """Bind the evaluator and every imported runtime source to accepted bytes."""
    rows = [
        [
            str(item.relative_to(directory).as_posix()),
            sha256(item.read_bytes()).hexdigest(),
        ]
        for item in policy_snapshot_files(directory)
    ]
    return sha256(json.dumps(rows, separators=(",", ":")).encode("utf-8")).hexdigest()


def granted_root(scope: str) -> Path:
    """One root a launch recorded, as the absolute directory it names here.

    A lease's own roots are recorded resolved, and a declared sandbox grant is
    recorded as it was written -- `~/.cache/uv` -- because it answers for
    whichever home reads it. Resolving that spelling without expanding it
    would name `<cwd>/~/.cache/uv`, refusing the one grant every toolchain
    needs as outside the boundary it is declared into, while `touch` on the
    same path, which no reader resolves, goes through.
    """
    return Path(scope).expanduser().resolve()


def execution_write_refusal(path_text: str, root: Path | None) -> str:
    """Keep the caller's measured mounts in force independently of policy ownership."""
    boundary = measured_boundary(root)
    writable = boundary["writable_roots"] if "writable_roots" in boundary else []
    readonly = boundary["read_only_roots"] if "read_only_roots" in boundary else []
    if not writable and not readonly:
        return ""
    path = ((root or Path.cwd()) / path_text).resolve()
    matches = [
        (len(granted.parts), allowed)
        for scopes, allowed in ((writable, True), (readonly, False))
        for scope in scopes
        for granted in [granted_root(scope)]
        if path.is_relative_to(granted)
    ]
    # A path under no root the lease names, inside a container this launch
    # measured, is the container's own unless the host lent it some other way:
    # nothing a write there lands on outlives the container or reaches the host.
    if not matches and container_owned(path, boundary):
        return ""
    if not matches or not min(
        allowed for depth, allowed in matches if depth == max(row[0] for row in matches)
    ):
        return "it is outside this launch's writable boundary"
    return ""


def container_owned(
    path: Path,
    measured: dict[str, list[str]],
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> bool:
    """Whether one resolved path is the measured container's own.

    Only inside a container the launch measured placing its work there, and
    only where this process's mount table can be read: a table nobody can read
    vouches for nothing, and the answer that keeps a refusal is no.
    """
    if not contained(measured) or not delivers(measured, "inside_placement"):
        return False
    try:
        lent = lent_mount_points(mountinfo.read_text())
    except OSError:
        return False
    shared = host_shared_roots(measured, lent, str(Path.home()))
    return not any(path.is_relative_to(scope) for scope in shared)


def measured_landings(
    targets: list[str],
    measured: dict[str, list[str]],
    root: Path | None = None,
    siblings: list[str] | None = None,
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> list[list[str]]:
    """Where each target lands, as this launch's lease and this mount table say.

    Asked by a caller that already knows the session runs in a container:
    the question these answer is asked only there. A mount table nobody
    can read places every target on the host, which keeps every question.
    ``siblings`` are the repository's other checkouts, as
    :func:`sibling_worktrees` names them.
    """
    try:
        lent = lent_mount_points(mountinfo.read_text())
    except OSError:
        return [[target, "host"] for target in targets]
    shared = host_shared_roots(measured, lent, str(Path.home()))
    return landed_targets(targets, shared, root, siblings)


def destination_policy_binding(path_text: str, root: Path | None) -> str:
    """Find one positively authorized repository binding, never infer one from a parent."""
    path = ((root or Path.cwd()) / path_text).resolve()
    owner = worktree_root(str(path))
    repository = shared_git_directory(str(path))
    boundary = measured_boundary(root)
    rows = (
        boundary["destination_policies"] if "destination_policies" in boundary else []
    )
    decoded = [json.loads(encoded) for encoded in rows]
    for row in decoded:
        if (
            not isinstance(row, dict)
            or "checkout" not in row
            or not isinstance(row["checkout"], str)
        ):
            raise ValueError("malformed destination checkout identity")
        checkout = Path(row["checkout"])
        if not checkout.is_absolute() or str(checkout.resolve()) != str(checkout):
            raise ValueError("destination checkout identity must be canonical")
    checkouts = [
        Path(row["checkout"])
        for row in decoded
        if path.is_relative_to(Path(row["checkout"]))
    ]
    expected = max(checkouts, key=lambda checkout: len(checkout.parts), default=None)
    if expected is not None and str(expected) != owner:
        raise ValueError("destination checkout identity changed or is missing")
    if (
        sum(
            isinstance(row, dict) and "checkout" in row and row["checkout"] == owner
            for row in decoded
        )
        > 1
    ):
        raise ValueError("ambiguous destination policy bindings")
    for encoded, row in zip(rows, decoded, strict=True):
        if not isinstance(row, dict):
            raise ValueError("malformed destination policy binding")
        if "checkout" not in row or "repository" not in row:
            raise ValueError("incomplete destination policy binding")
        if row["checkout"] != owner:
            continue
        if row["repository"] != repository:
            raise ValueError("destination repository identity changed")
        if root is not None and owner == worktree_root(str(root)):
            return ""
        if type(row["protocol"]) is not int or row["protocol"] != 1:
            raise ValueError("unsupported destination policy protocol")
        if "writable_roots" not in boundary or not boundary["writable_roots"]:
            raise ValueError("destination authority has no measured writable boundary")
        for name in ("writable_roots", "read_only_roots"):
            if not isinstance(row[name], list) or not all(
                isinstance(item, str) for item in row[name]
            ):
                raise ValueError(f"malformed destination {name}")
            if any(
                not Path(item).is_absolute() or str(Path(item).resolve()) != item
                for item in row[name]
            ):
                raise ValueError(
                    f"destination {name} must contain canonical absolute paths"
                )
        scopes = [
            (len(Path(scope).parts), allowed)
            for name, allowed in (("writable_roots", True), ("read_only_roots", False))
            for scope in row[name]
            if path.is_relative_to(Path(scope).resolve())
        ]
        if not scopes or not min(
            allowed
            for depth, allowed in scopes
            if depth == max(item[0] for item in scopes)
        ):
            raise ValueError(f"{path} has no explicit writable destination grant")
        for name in ("source", "snapshot", "digest", "error"):
            if not isinstance(row[name], str):
                raise ValueError(f"malformed destination policy {name}")
        if row["error"]:
            raise ValueError(row["error"])
        for name in ("source", "snapshot"):
            if policy_snapshot_digest(Path(row[name])) != row["digest"]:
                raise ValueError(
                    f"destination policy {name} changed; refresh its accepted snapshot"
                )
        return encoded
    return ""


def hook_started() -> float:
    """When this hook began, on the monotonic clock: where the guard stamped it, else now.

    A runtime counts its timeout from the moment it starts the hook, and by
    the time the dispatcher's first line runs the interpreter has started,
    compiled the script and imported the kernel -- time the runtime counts
    and a deadline opened from inside would not. So the guard stamps the
    wall clock as it starts, in whole seconds since that is what every
    `date` prints, and every deadline here counts from the stamp: never
    later than the hook began, and at most a second earlier. Without a
    stamp, as when something other than the guard starts the dispatcher,
    the hook began now.
    """
    environ = os.environ  # lup: ignore[os-environ]
    stamp = environ["LUP_HOOK_STARTED"] if "LUP_HOOK_STARTED" in environ else ""
    now = time.monotonic()
    try:
        elapsed = time.time() - float(stamp) if stamp else 0.0
    except ValueError:
        elapsed = 0.0
    return now - max(elapsed, 0.0)


def opened_deadline(seconds: float, grace: float = 2.0) -> str:
    """Give this hook, and every process it starts, one deadline: returns the one before.

    Each runtime lets a call through once its policy hook runs past its
    timeout, so a hook that is still waiting when that comes has answered
    nothing, and nothing is the one answer a gate may not give. Everything a
    verdict may wait on -- a language server, a destination's evaluator, Git,
    `sed` -- shares the deadline set here, counted from when the hook
    started, and asks :func:`hook_seconds_left` rather than spending a
    timeout of its own; one cut short reads as the failure it already
    answers, so the verdict is reached in time.

    What no step bounds -- a file lock another writer holds, a read that
    never returns, the classifier itself -- is bounded by an alarm ``grace``
    seconds past the deadline. It raises where the hook is, and the
    dispatcher answers that as it answers every call it could not judge: it
    refuses. The grace is what separates the two: a step cut short at the
    deadline still has time to become the verdict it reads as. What an
    alarm cannot reach either, :func:`answered_in_time` answers for.

    Kept in the environment, on the monotonic clock every process on the
    machine shares, so a process this hook starts inherits the deadline and
    never extends it: one already set by a parent stands where it is sooner.
    """
    environ = os.environ  # lup: ignore[os-environ]
    previous = environ["LUP_HOOK_DEADLINE"] if "LUP_HOOK_DEADLINE" in environ else ""
    deadline = hook_started() + seconds
    try:
        inherited = float(previous) if previous else deadline
    except ValueError:
        inherited = deadline
    held = min(deadline, inherited)
    environ["LUP_HOOK_DEADLINE"] = repr(held)

    # A RuntimeError rather than a TimeoutError, which is an OSError: steps on
    # the way to a verdict answer an OSError as the failure it reads as and
    # carry on, and an alarm one of them swallowed would leave the hook
    # running with nothing left to stop it. Nothing before the dispatcher
    # catches this one, so it reaches the refusal of a call it could not judge,
    # which :func:`deadline_passed` then names as what it was.
    def overran(_number, _frame):
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
        raise RuntimeError("this hook reached its deadline before a verdict")

    signal.signal(signal.SIGALRM, overran)
    signal.setitimer(signal.ITIMER_REAL, max(held - time.monotonic(), 0.0) + grace)
    return previous


def answered_in_time(
    seconds: float,
    judged: Callable[[bytes], None],
    unanswered: Callable[[bytes, BaseException | None], None],
) -> None:
    """Answer within ``seconds`` of the hook starting, whatever the judgement does.

    A runtime lets the call through once its policy hook overruns its
    timeout, and reads nothing the hook said until its process has ended --
    so the answer has to be written, and this process gone, before then.
    The deadline :func:`opened_deadline` sets, and the alarm past it, bound
    every wait that can be interrupted. This bounds the rest: the judgement
    runs in a child process, and this one only waits on it. Past ``seconds``
    it stops the child and writes ``unanswered`` itself, so a judgement
    stuck where no alarm reaches -- a read the kernel will not interrupt,
    native code that never returns to the interpreter, the verdict being
    written after its alarm was disarmed -- still meets a refusal in time.

    ``judged`` reads the hook's input and answers as the dispatcher always
    has: on standard output and error and in its exit status, carried out
    whole once it ends, and none of it if it does not end in time -- half a
    verdict is not one. The child holds neither of the runtime's streams,
    so nothing it leaves running keeps the runtime waiting. A child a signal
    ended reached no verdict, and is refused as one that could not judge;
    one that exited keeps the status the guard around this script reads.

    ``unanswered`` takes the input and what failed -- None where the time
    ran out -- and writes this runtime's refusal on the channel the input's
    event reads, in the words :func:`unjudged_reason` and
    :func:`unjudged_recovery` give it. The input is whatever arrived, which
    is nothing where it did not all arrive in time.
    """
    limit = hook_started() + seconds

    def arriving() -> Iterator[bytes]:
        """The hook's input as it arrives; a TimeoutError where it stops arriving in time."""
        while (left := limit - time.monotonic()) > 0 and select.select(
            [0], [], [], left
        )[0]:
            chunk = os.read(0, 65536)
            if not chunk:
                return
            yield chunk
        raise TimeoutError("the hook's input did not arrive in time")

    def status_of(given: bytes) -> int:
        """Judge here, in the child, and say how it ended as an exit status would."""
        try:
            judged(given)
        except SystemExit as stop:
            if isinstance(stop.code, str):
                print(stop.code, file=sys.stderr)
            return stop.code if isinstance(stop.code, int) else int(bool(stop.code))
        # The interpreter's own report of what escaped, which a child that
        # leaves through os._exit has to give itself: the traceback on stderr
        # and the status the guard reads as a crash.
        except Exception as escaped:
            sys.excepthook(type(escaped), escaped, escaped.__traceback__)
            return 1
        return 0

    def streamed(stream: int) -> Iterator[bytes]:
        """What one of the child's streams holds now, without waiting for more."""
        os.set_blocking(stream, False)
        try:
            yield from iter(lambda: os.read(stream, 65536), b"")
        except BlockingIOError:
            return

    try:
        given = b"".join(arriving())
    except TimeoutError:
        unanswered(b"", None)
        return
    sys.stdout.flush()
    sys.stderr.flush()
    said_out, said_in = os.pipe()
    told_out, told_in = os.pipe()
    ended, ending = os.pipe()
    try:
        child = os.fork()
    except OSError as error:
        unanswered(given, error)
        return
    if child == 0:
        for unused in (said_out, told_out, ended):
            os.close(unused)
        # Its input stays the runtime's wire, all read already, since which
        # runtime a hook answers for is read off whose wire that is.
        for source, target in ((said_in, 1), (told_in, 2)):
            if source != target:
                os.dup2(source, target)
                os.close(source)
        # Whatever escapes the judgement, the child leaves here: it is a
        # copy of this process, and returning would run this one's code.
        status = 1
        try:
            status = status_of(given)
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os._exit(status)
    for unused in (said_in, told_in, ending):
        os.close(unused)
    kept: dict[int, list[bytes]] = {said_out: [], told_out: []}

    def ended_with() -> int | None:
        """The child's exit status, its streams read meanwhile; None past the limit.

        The streams are read as they fill, since a child writing more than a
        pipe holds waits for a reader. Its end is the one pipe nothing it
        starts inherits closing, rather than the end of its output, which a
        process it left running could hold open long after it answered.
        """
        watched = [said_out, told_out, ended]
        while ended in watched:
            left = limit - time.monotonic()
            ready = select.select(watched, [], [], left)[0] if left > 0 else []
            if not ready:
                return None
            for stream in ready:
                chunk = os.read(stream, 65536)
                if not chunk:
                    watched.remove(stream)
                    continue
                # Only the two streams carry anything: the child never writes
                # to the pipe whose closing says it ended.
                kept[stream].append(chunk)
        while (left := limit - time.monotonic()) > 0:
            reaped, status = os.waitpid(child, os.WNOHANG)
            if reaped:
                return os.waitstatus_to_exitcode(status)
            time.sleep(min(left, 0.01))
        return None

    status = ended_with()
    if status is None or status < 0:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        killed = None if status is None else RuntimeError(f"ended by signal {-status}")
        unanswered(given, killed)
        return
    for stream, chunks in kept.items():
        chunks.extend(streamed(stream))
    sys.stdout.buffer.write(b"".join(kept[said_out]))
    sys.stderr.buffer.write(b"".join(kept[told_out]))
    sys.stdout.flush()
    sys.stderr.flush()
    if status:
        raise SystemExit(status)


def refuse_unanswered(question: str) -> None:
    """End a hook's judgement at a question it could not get answered in time.

    "No answer" is not "no". A question cut short by the deadline -- is this
    path tracked, which repository holds it, what does this patch touch --
    read as its negative answer lets a write through as untracked, as this
    project's, as touching nothing, where the answer it never got would have
    asked. Reading every such question strictly one by one is a second
    policy beside the first; ending the judgement is the one strict reading
    that needs no second. So under a hook's deadline this moves the deadline
    to now, which every later step reads as no time left and the refusal
    names, and raises where the question was asked. Outside a hook -- a
    review waiter, the dashboard -- nothing is being judged, and it returns
    for the caller's own answer to a question nobody could answer.
    """
    environ = os.environ  # lup: ignore[os-environ]
    if "LUP_HOOK_DEADLINE" not in environ:
        return
    environ["LUP_HOOK_DEADLINE"] = repr(time.monotonic())
    raise RuntimeError(f"{question} did not answer before the hook's deadline")


def closed_deadline(previous: str) -> None:
    """Disarm :func:`opened_deadline`'s alarm and put back the deadline it replaced."""
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    environ = os.environ  # lup: ignore[os-environ]
    if previous:
        environ["LUP_HOOK_DEADLINE"] = previous
        return
    environ.pop("LUP_HOOK_DEADLINE", None)


def deadline_passed() -> bool:
    """Whether the hook's deadline has come, so a failure now is the deadline's.

    Read off the clock rather than off the error: the alarm fires past the
    deadline, and a step cut short at it fails however it fails, so a
    verdict that did not arrive before the deadline is named as having met
    it. Outside a hook there is no deadline to have passed.
    """
    return hook_seconds_left(float("inf")) <= 0.0


def unjudged_reason(error: BaseException | None, read: bool) -> str:
    """Why a call went unjudged, named by what failed rather than by one guess.

    Every failure is refused alike -- the call went unjudged, and that is
    the whole of what the verdict can say -- but the reason is what somebody
    reads to fix it, and each cause has a different fix: a judgement that
    ran out of time (``error`` None, or anything failing once the deadline
    has passed), a payload that is not one (``read`` false), and a failure
    judging a payload that was.
    """
    if ran_out(error):
        return "the policy could not judge this call in time, so it is refused unjudged"
    if not read:
        return f"the hook input is malformed, so the call is refused unjudged: {error}"
    return (
        "the policy failed on this call, so it is refused unjudged"
        f" (`{type(error).__name__}: {error}`)"
    )


def ran_out(error: BaseException | None) -> bool:
    """Whether a call went unjudged for want of time rather than for a failure.

    ``error`` None is a judgement still running when the hook had to answer;
    anything failing once the deadline has passed failed for the same reason.
    """
    return error is None or deadline_passed()


def hook_seconds_left(ceiling: float) -> float:
    """How long a step may still take: ``ceiling``, or less where the hook's deadline is nearer.

    Outside a hook no deadline is set and the step keeps its own ceiling.
    """
    environ = os.environ  # lup: ignore[os-environ]
    if "LUP_HOOK_DEADLINE" not in environ:
        return ceiling
    try:
        deadline = float(environ["LUP_HOOK_DEADLINE"])
    except ValueError:
        return ceiling
    return max(0.0, min(ceiling, deadline - time.monotonic()))


def destination_evaluation(binding: str, request: str, timeout: float = 15) -> str:
    """Run accepted bytes in isolation; return only the evaluator's protocol reply."""
    row = json.loads(binding)
    if not isinstance(row, dict) or not isinstance(row["snapshot"], str):
        raise ValueError("malformed destination evaluator binding")
    snapshot = Path(row["snapshot"])
    if policy_snapshot_digest(snapshot) != row["digest"]:
        raise ValueError("accepted destination policy snapshot changed")
    allowed = hook_seconds_left(timeout)
    if allowed <= 0:
        raise ValueError(
            "this hook has no time left to run the destination's accepted policy"
        )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-X",
            "pycache_prefix=/dev/null/lup-policy",
            str(snapshot / "scripts" / "policy_evaluator.py"),
        ],
        input=request,
        text=True,
        capture_output=True,
        timeout=allowed,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"destination policy evaluator failed: {result.stderr or result.stdout}"
        )
    if (
        policy_snapshot_digest(snapshot) != row["digest"]
        or policy_snapshot_digest(Path(row["source"])) != row["digest"]
    ):
        raise ValueError("destination policy changed during evaluation")
    return result.stdout


def routing_policy_identity(root: Path | None) -> str:
    """Bind an approval to both accepted policies and the bytes currently present."""
    boundary = measured_boundary(root)
    rows = (
        boundary["destination_policies"] if "destination_policies" in boundary else []
    )
    identities = list(rows)
    for encoded in rows:
        try:
            row = json.loads(encoded)
            if not isinstance(row, dict):
                raise ValueError("malformed destination policy binding")
            for name in ("source", "snapshot"):
                identities.append(policy_snapshot_digest(Path(row[name])))
        except (OSError, ValueError, KeyError, TypeError) as error:
            identities.append(f"unavailable: {error}")
    return sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()


def routed_edit_response(
    path_text: str,
    before: str | None,
    after: str | None,
    path_exists: bool,
    autonomous: bool,
    operation: str,
    root: Path | None,
    agent_identity: str = "",
) -> str | None:
    """Resolve caller authority and run an accepted foreign owner, without native effects."""
    path = str(((root or Path.cwd()) / path_text).resolve())
    refusal = execution_write_refusal(path, root)
    if refusal:
        raise ValueError(refusal)
    if root is None:
        return None
    binding = destination_policy_binding(path, root)
    if not binding:
        return None
    row = json.loads(binding)
    request = {
        "protocol": 2,
        "path": path,
        "before": before,
        "after": after,
        "path_exists": path_exists,
        "autonomous": autonomous,
        "agent_identity": agent_identity,
        "operation": operation,
        "cwd": str(root or Path.cwd()),
        "owner": row["checkout"],
    }
    try:
        return destination_evaluation(binding, json.dumps(request))
    except subprocess.TimeoutExpired as error:
        raise ValueError("destination policy evaluator timed out") from error


def sandbox_active() -> bool:
    """Whether the launcher confined this session to an OS sandbox."""
    environ = os.environ  # lup: ignore[os-environ]
    return "LUP_SANDBOX_ACTIVE" in environ and environ["LUP_SANDBOX_ACTIVE"] == "1"


def measured_boundary(
    root: Path | None, ledger: str = ".lup/preflight"
) -> dict[str, list[str]]:
    """What the launch that opened this session measured about its boundary.

    Read back from the ledger that launch wrote, and only from the one it
    named: ``LUP_BOUNDARY_NONCE`` says which file this session is entitled to
    believe, and ``LUP_BOUNDARY_ROOT`` pins the launch checkout even when a
    tool or preflight command runs elsewhere. A ledger left by some other launch
    is a measurement of some other session, and reading it would be the same
    class of wrong answer as a ledger at a constant path, arrived at from the
    other direction.

    Absent, unnamed, or unparseable all come back empty, and every caller
    reads empty as "no boundary was measured" -- the fail-closed answer and
    the honest one. A session whose launcher wrote no ledger gets exactly
    what a session whose boundary failed to stand gets.

    Containment is measured here rather than taken from the row. Every
    contained launch holds its ledger's directory read-only in the container,
    so a ledger claiming a container that this process does not read
    through such a mount was written by something other than that launch --
    a script in a session on the host, where the directory is writable, is
    the case this answers. Its claim is dropped and the rest of what it says
    stands, which is what any uncontained ledger already is: the classifier's
    to guard, since nothing holds it.
    """
    environ = os.environ  # lup: ignore[os-environ]
    nonce = environ["LUP_BOUNDARY_NONCE"] if "LUP_BOUNDARY_NONCE" in environ else ""
    if "LUP_BOUNDARY_ROOT" in environ:
        root = Path(environ["LUP_BOUNDARY_ROOT"])
        if not root.is_absolute() or str(root.resolve()) != str(root):
            return {}
    if root is None or not nonce or Path(nonce).name != nonce or nonce in {".", ".."}:
        return {}
    try:
        raw = (root / ledger / f"{nonce}.json").read_text()
    except OSError:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    measured = {
        name: [item for item in value if isinstance(item, str)]
        for name, value in loaded.items()
        if isinstance(name, str) and isinstance(value, list)
    }
    if contained(measured) and not record_held((root / ledger).resolve()):
        return {name: value for name, value in measured.items() if name != "contained"}
    return measured


def record_held(
    directory: Path, mountinfo: Path = Path("/proc/self/mountinfo")
) -> bool:
    """Whether this process reads ``directory`` through a read-only mount of it.

    Asked of this process's own mount table, which nothing a session runs
    can change: a session on the host cannot mount, and one in a container
    cannot unmount what the engine bound, nor move the directory holding it
    while its parents are pinned. A table nobody can read vouches for
    nothing, and the answer that keeps the claim out is no.
    """
    try:
        table = mountinfo.read_text()
    except OSError:
        return False
    return str(directory) in read_only_mount_points(table)


def read_only_mount_points(mountinfo: str) -> list[str]:
    """Every mount point here mounted read-only, from a ``mountinfo`` table.

    The record's fields are named where they are read: proc(5) fixes the
    fifth as the mount point and the sixth as its own options, which say
    ``ro`` for a read-only mount whatever the filesystem beneath it allows.
    A record too short to carry them is not a mount and is skipped.
    """

    def held(record: list[str]) -> list[str]:
        match record:
            case [_mount_id, _parent_id, _device, _root, mount_point, options, *_] if (
                "ro" in next(csv.reader([options]))
            ):
                return [unescaped_mount_point(mount_point)]
            case _:
                return []

    return [
        point
        for record in csv.reader(mountinfo.splitlines(), delimiter=" ")
        for point in held(record)
    ]


def contained(measured: dict[str, list[str]]) -> bool:
    """Whether this session runs inside the boundary its profile promised.

    A different question from :func:`sandbox_active`, and the two are not
    interchangeable: the native sandbox confines one call at a time and can
    be told to leave some alone, where a container confines the process and
    was never asked.

    Read from what the launch *measured* rather than from a variable. A
    variable is a constant an image bakes, and a constant answers yes for any
    container built from that image, for a bare ``run`` holding none of the
    lease, and -- since a launcher forwards its own environment -- for an
    uncontained session started from a shell that exports it: a session
    reporting a boundary with no container under it, placing every operation
    by a wall that is not there.
    """
    return "yes" in (measured["contained"] if "contained" in measured else [])


def delivers(measured: dict[str, list[str]], capability: str) -> bool:
    """Whether the launch observed one capability this session depends on."""
    return capability in (measured["delivered"] if "delivered" in measured else [])


def launched(measured: dict[str, list[str]]) -> list[str]:
    """The invocation the launch that opened this session recorded for itself.

    What lets a session spell its own reopening — a mount registered
    mid-session takes effect only at the next launch, and this record is the
    only thing that remembers which launch that is. Empty wherever nothing
    was recorded (an older launcher, or no measurement at all), and every
    caller reads empty as "no reopening can be spelled".
    """
    return measured["launch"] if "launch" in measured else []


def defers_unjudged(measured: dict[str, list[str]]) -> bool:
    """Whether this profile hands legible work nothing judged to the runtime.

    A bool rather than the policy's own word for it, because this half may
    reach nothing but a pinned standard library and cannot name the literal
    the kernel takes. The caller spells the vocabulary; what crosses here is
    the fact.

    False wherever nothing was measured, which is both the declared default
    and the visible answer. A session that could not read its own profile is
    not one that should infer a seamless posture from the silence.
    """
    declared = measured["unjudged_ambient"] if "unjudged_ambient" in measured else []
    return bool(declared) and declared[0] == "defer"


def append_hook_evidence(path: Path, encoded: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(encoded + "\n")


def fetch_origin(payload: dict) -> str:
    """The scheme, host and port a fetch names, with the path left behind.

    A journal that omits tool input entirely leaves a refused fetch
    unattributable: the record says a URL was outside the declared scopes,
    and which URL has to be inferred from whatever the session did next.
    The origin closes that without reopening what the omission protects. It
    is the coarse half of a URL and the one a scope is written against,
    while the path and the query are where a token, a document id or a
    search phrase ride -- so those are read to parse the origin out and are
    never written. Userinfo goes the same way: the hostname and port come
    from the parse rather than the netloc, which would carry a credential
    spelled into the URL.

    Empty for a tool whose input names no URL, which is what keeps the
    omission whole everywhere but the fetch surface, and empty for a URL no
    scope could have matched either -- an unparseable one reaches its
    verdict on being unparseable, not on an origin.
    """
    tool_input = payload["tool_input"] if "tool_input" in payload else {}
    named = isinstance(tool_input, dict) and "url" in tool_input
    url = tool_input["url"] if named else ""
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if not parsed.scheme or hostname is None:
        return ""
    return f"{parsed.scheme}://{hostname}" + (f":{port}" if port else "")


def record_hook_evidence(
    data_root: Path | None,
    payload: dict,
    phase: str,
    outcome: str | None = None,
    detail: str | None = None,
) -> None:
    """Append hook metadata, keeping of a tool's input only a fetch origin.

    Input and output stay out of a record because they carry commands,
    patches and credentials. A fetch's origin is the one part that does not:
    it is what the verdict was reached against, and :func:`fetch_origin`
    bounds it to the scheme, host and port.
    """
    if data_root is None:
        return
    record = {
        "schema_version": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "phase": phase,
    }
    fields = ("session_id", "turn_id", "tool_name", "tool_use_id")
    record["event_name"] = (
        payload["hook_event_name"]
        if "hook_event_name" in payload and isinstance(payload["hook_event_name"], str)
        else None
    )
    record.update(
        {
            field: payload[field]
            for field in fields
            if field in payload and isinstance(payload[field], str)
        }
    )
    origin = fetch_origin(payload)
    record.update({"fetch_origin": origin} if origin else {})
    record.update({"outcome": outcome} if outcome is not None else {})
    record.update({"detail": detail} if detail is not None else {})
    encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
    try:
        append_hook_evidence(data_root / "hook-events.jsonl", encoded)
    except OSError as error:
        print(f"lup: could not record hook evidence: {error}", file=sys.stderr)


def script_run_nudge(
    scripts: list[str],
    root: Path | None,
    after: int = 5,
    every: int = 10,
    ledger: str = ".lup/script-runs.json",
) -> str:
    """Count each script's runs and say when one has stopped being a one-off.

    The ladder allows a scratch script because computing something once does
    not earn a command. Nothing in that argument survives the fifth run: by
    then the thing is a tool, and a tool nobody can invoke by name is one the
    next session rewrites from scratch. This is what notices, because the
    agent doing the rewriting has no memory of the previous four.

    Advice rather than a gate. It rides along with a verdict that already
    allowed the command, so a genuine repeat is a sentence to read and not a
    wall -- the only form this can take without punishing the case it exists
    to improve.

    Said once at ``after`` and then only every ``every`` runs, because the
    two ways to get this wrong are opposite and both fatal to it. On every
    run it becomes noise attached to a command that worked, which is read
    once and skipped forever after. Once and never again, and a session that
    was mid-thought when it arrived never hears it a second time, however
    many more times it runs the thing.

    A ledger that cannot be read or written yields no nudge. A counter is not
    worth failing a command over, and a read-only checkout is an ordinary
    place to be running.
    """
    if root is None or not scripts:
        return ""
    path = root / ledger
    try:
        raw = path.read_text() if path.exists() else ""
    except OSError:
        return ""
    try:
        loaded = json.loads(raw) if raw else None
    except ValueError:
        loaded = None
    try:
        counts = loaded if isinstance(loaded, dict) else {}
        seen = {
            script: (
                counts[script]
                if script in counts and isinstance(counts[script], int)
                else 0
            )
            for script in dict.fromkeys(scripts)
        }
        bumped = {
            script: before + scripts.count(script) for script, before in seen.items()
        }
        earned = [
            script
            for script, total in bumped.items()
            if any(
                seen[script] < point <= total
                for point in range(after, total + 1, every)
            )
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**counts, **bumped}, indent=2, sort_keys=True))
    except OSError:
        return ""
    if not earned:
        return ""
    counted = ", ".join(f"{script} ({bumped[script]}x)" for script in earned)
    return (
        f" — {counted}: more than a one-off by now, so consider making it a"
        " `lup-devtools` command, which lands in the diff and can be run"
        " again by name"
    )


def noted_once(
    root: Path,
    conversation: str,
    subject: str,
    ledger: str = ".lup/notices.json",
    kept_days: int = 7,
) -> bool:
    """Whether this conversation was already told about *subject*, noting it if not.

    What a notice says once is true for the rest of the conversation and news
    only the first time: another repository's referral, the habit of naming a
    spawn. Kept per conversation under the checkout, for *kept_days*, so the
    ledger holds what a live conversation could still be told and nothing
    older. A ledger that cannot be read or written answers no, which errs
    toward saying a notice again rather than never.
    """
    path = root / ledger
    now = datetime.now(UTC)

    def recent(entry: dict) -> bool:
        if "subjects" not in entry:
            return False
        try:
            stamped = datetime.fromisoformat(str(entry["at"]))
        except (KeyError, ValueError):
            return False
        return now - stamped < timedelta(days=kept_days)

    try:
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        loaded = {}
    held = loaded if isinstance(loaded, dict) else {}
    kept = {
        name: entry
        for name, entry in held.items()
        if isinstance(entry, dict) and recent(entry)
    }
    seen = kept[conversation]["subjects"] if conversation in kept else []
    if subject in seen:
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    **kept,
                    conversation: {
                        "at": now.isoformat(),
                        "subjects": [*seen, subject],
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except OSError:
        return False
    return False


def stream_records(stream: BinaryIO) -> list[dict]:
    """Every complete object record in an open file, read from its start.

    A line with no terminator is a write still under way, or one that never
    finished, and a line that is not a JSON object is inert evidence: neither
    is a record.
    """
    stream.seek(0)

    def complete() -> Iterator[dict]:
        for line in stream:
            if not line.endswith(b"\n"):
                continue
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict):
                yield entry

    return list(complete())


def review_records(path: Path) -> list[dict]:
    """Read complete object records, preserving malformed bytes as inert evidence."""
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return []
    with stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        return stream_records(stream)


def framed(stream: BinaryIO, lines: list[str]) -> None:
    """Append *lines* to a file its writers' lock is held on, repairing no incomplete record into authority.

    An unterminated tail is retained and marked invalid before the first of
    them, even if its partial write happened to end after a syntactically
    complete JSON object. The lines are written in one call, so a reader
    never finds half of them.
    """
    stream.seek(0, os.SEEK_END)
    if stream.tell():
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) != b"\n":
            stream.write(b" [incomplete review record]\n")
    stream.write(b"".join(line.encode("utf-8") + b"\n" for line in lines))
    stream.flush()


def append_review_record(path: Path, encoded: str) -> None:
    """Frame an append under the one lock every writer of *path* holds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        framed(stream, [encoded])


def relay_header() -> dict:
    """The line a relay's log opens with: its records name the documents they bind by digest.

    A log without it keeps a full copy of a question at every transition, and
    is rewritten into this shape the first time it is opened
    (:func:`migrate_relay`), after which nothing reads the older shape.
    """
    return {"relay": 2}


def relay_lock(log: Path) -> Path:
    """The lock a relay's transactions hold, and the rewriting of its log beside them."""
    located = log.resolve()
    return located.with_name(f"{located.name}.lock")


def relay_blobs(log: Path) -> Path:
    """Where a relay keeps every document its records name, once each, under its digest."""
    return log.parent / "reviews" / "blobs"


def transition_fields() -> tuple[str, ...]:
    """What a transition may move on a question: never what it asks, nor its answer."""
    return ("state", "outcome", "completed", "expires", "moved", "execution_id")


def blob_digest(stored: dict | None) -> str:
    """The digest a stored-document reference names, or "" where it names none.

    Sixty-four lowercase hex digits and nothing else, so a reference can
    never name a path beside the store.
    """
    match stored:
        case {"sha256": str() as digest} if len(digest) == 64 and all(
            character in "0123456789abcdef" for character in digest
        ):
            return digest
    return ""


def stored_blob(blobs: Path, text: str) -> dict:
    """Keep one document in a relay's store, once, and name it by the digest of its UTF-8 text.

    Written beside its name and moved into place, so a reader never meets a
    half-written document under a digest it does not hash to; one the store
    already keeps is not written again.
    """
    encoded = text.encode()
    digest = sha256(encoded).hexdigest()
    target = blobs / digest
    if not target.is_file():
        blobs.mkdir(parents=True, exist_ok=True)
        staged = blobs / f".{digest}.{os.urandom(8).hex()}"
        try:
            with staged.open("xb") as stream:
                stream.write(encoded)
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
    return {"sha256": digest}


def blob_text(blobs: Path, stored: dict | None) -> str:
    """The document a record names by digest, read back whole.

    Refused with ValueError where the store does not keep it, or keeps
    something that no longer hashes to its name: what a record binds by
    digest is that document or nothing.
    """
    digest = blob_digest(stored)
    if not digest:
        raise ValueError(f"{stored!r} names no document")
    try:
        encoded = (blobs / digest).read_bytes()
    except OSError as error:
        raise ValueError(f"document {digest} is not kept: {error}") from error
    if sha256(encoded).hexdigest() != digest:
        raise ValueError(f"document {digest} no longer hashes to its name")
    return encoded.decode()


def document_name(text: str) -> dict:
    """The name a record keeps for one document in place of it: the digest of its UTF-8 text."""
    return {"sha256": sha256(text.encode()).hexdigest()}


def kept_apart(text: str) -> bool:
    """Whether one string of a call is kept in the relay's store rather than in its record.

    A kibibyte of UTF-8 or more -- a document a call writes, a patch, a long
    script. A shorter one costs the record less than the name it would keep
    in its place, and a reader of the record less than the file it would
    open for it.
    """
    return len(text.encode()) >= 1024


def call_strings(
    value: dict | list | str | int | float | bool | None, at: list
) -> list[list]:
    """Where each string in *value* long enough to keep apart stands, as a path of keys and indices after *at*."""
    match value:
        case str() if kept_apart(value):
            return [at]
        case dict():
            return [
                found
                for key, item in value.items()
                for found in call_strings(item, [*at, key])
            ]
        case list():
            return [
                found
                for index, item in enumerate(value)
                for found in call_strings(item, [*at, index])
            ]
    return []


def stored_paths(entry: dict) -> list[list]:
    """Where a recorded question keeps a name in place of a string of its call.

    Only paths into the call -- the operation's payload, or the payload a
    native retry runs -- each a list of keys and indices; a record naming
    anywhere else names nothing.
    """
    listed = entry["stored"] if "stored" in entry else []

    def into_call(at: list) -> bool:
        match at:
            case ["operation", "payload", *rest] | ["execution_payload", *rest]:
                return all(isinstance(part, str) or type(part) is int for part in rest)
        return False

    return (
        [at for at in listed if isinstance(at, list) and into_call(at)]
        if isinstance(listed, list)
        else []
    )


def standing_at(
    tree: dict | list, at: list
) -> dict | list | str | int | float | bool | None:
    """What stands at the path *at* inside *tree*; ValueError where nothing does."""
    found = tree
    for part in at:
        match found:
            case dict() if isinstance(part, str) and part in found:
                found = found[part]
            case list() if type(part) is int and 0 <= part < len(found):
                found = found[part]
            case _:
                raise ValueError(f"nothing stands at {at!r}")
    return found


def replaced_at(
    entry: dict, paths: list[list], change: Callable[[str | dict], str | dict]
) -> dict:
    """A copy of *entry* with what stands at each of *paths* replaced by what *change* makes of it.

    Raises ValueError where nothing stands at one, or where what does is
    neither a string nor a name. A record is JSON, so it is copied as JSON.
    """
    copied = json.loads(json.dumps(entry))
    for at in paths:
        if not at:
            raise ValueError("an empty path names the whole question")
        *above, last = at
        parent = standing_at(copied, above)
        current = standing_at(copied, at)
        if not isinstance(parent, dict | list) or not isinstance(current, str | dict):
            raise ValueError(f"{at!r} names neither a string nor a document")
        parent[last] = change(current)
    return copied


def stored_form(entry: dict, keep: Callable[[str], dict]) -> dict:
    """One question as its record keeps it, every document it carries named by what *keep* makes of it.

    The documents: the preimage of each file it binds, the document each
    file verdict judged, and each string of its call a kibibyte or longer
    (:func:`kept_apart`) -- in the operation's payload, and in the payload a
    native retry runs, where one differs from the other. A retry's payload
    repeating the operation's is recorded as null, which every reader of a
    question takes for the operation's payload; the call's strings are
    named where they stood, and the paths to them listed under ``stored``.

    *keep* writes a document into the store and names it where a question is
    parked (:func:`stored_blob`), or only names it (:func:`document_name`)
    where the record is worked out to compare with one.
    """
    preconditions = entry["preconditions"] if "preconditions" in entry else None
    rows = entry["file_reviews"] if "file_reviews" in entry else None
    operation = entry["operation"] if "operation" in entry else None
    payload = (
        operation["payload"]
        if isinstance(operation, dict) and "payload" in operation
        else None
    )
    expected = entry["execution_payload"] if "execution_payload" in entry else None
    repeated = expected is not None and expected == payload
    documented = {
        **entry,
        **(
            {
                "preconditions": {
                    path: keep(text) if isinstance(text, str) else None
                    for path, text in preconditions.items()
                }
            }
            if isinstance(preconditions, dict)
            else {}
        ),
        **(
            {
                "file_reviews": [
                    {
                        **row,
                        "after": keep(row["after"])
                        if isinstance(row["after"], str)
                        else None,
                    }
                    if isinstance(row, dict) and "after" in row
                    else row
                    for row in rows
                ]
            }
            if isinstance(rows, list)
            else {}
        ),
        **({"execution_payload": None} if repeated else {}),
    }
    paths = [
        *(
            call_strings(payload, ["operation", "payload"])
            if isinstance(payload, dict)
            else []
        ),
        *(
            call_strings(expected, ["execution_payload"])
            if isinstance(expected, dict) and not repeated
            else []
        ),
    ]
    if not paths:
        return documented
    return {
        **replaced_at(
            documented,
            paths,
            lambda text: keep(text) if isinstance(text, str) else text,
        ),
        "stored": paths,
    }


def resolved_call(entry: dict, blobs: Path) -> dict:
    """One recorded question with each string of its call it names read back whole; its documents stay named.

    Raises ValueError where one is missing or altered.
    """
    paths = stored_paths(entry)
    unlisted = {field: value for field, value in entry.items() if field != "stored"}
    if not paths:
        return unlisted
    return replaced_at(
        unlisted,
        paths,
        lambda named: blob_text(blobs, named) if isinstance(named, dict) else named,
    )


def resolved_entry(entry: dict, blobs: Path) -> dict:
    """One question as it was asked: every document it names by digest read back whole.

    Raises ValueError where one is missing or altered, since a question that
    cannot be read back whole cannot be shown, or checked, for what it binds.
    """
    called = resolved_call(entry, blobs)
    preconditions = called["preconditions"] if "preconditions" in called else None
    rows = called["file_reviews"] if "file_reviews" in called else None
    return {
        **called,
        **(
            {
                "preconditions": {
                    path: blob_text(blobs, stored) if stored is not None else None
                    for path, stored in preconditions.items()
                }
            }
            if isinstance(preconditions, dict)
            else {}
        ),
        **(
            {
                "file_reviews": [
                    {**row, "after": blob_text(blobs, row["after"])}
                    if isinstance(row, dict)
                    and "after" in row
                    and row["after"] is not None
                    else row
                    for row in rows
                ]
            }
            if isinstance(rows, list)
            else {}
        ),
    }


def named_blobs(entry: dict) -> list[str]:
    """Every document one recorded question names by digest, the strings of its call among them."""
    preconditions = entry["preconditions"] if "preconditions" in entry else None
    rows = entry["file_reviews"] if "file_reviews" in entry else None

    def called(at: list) -> dict | list | str | int | float | bool | None:
        try:
            return standing_at(entry, at)
        except ValueError:
            return None

    return [
        digest
        for stored in [
            *(preconditions.values() if isinstance(preconditions, dict) else []),
            *(
                row["after"]
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict) and "after" in row
            ),
            *(called(at) for at in stored_paths(entry)),
        ]
        if (digest := blob_digest(stored if isinstance(stored, dict) else None))
    ]


def fold_relay(folded: dict[str, dict], records: list[dict]) -> None:
    """Move each question in *folded* by the records that follow.

    A parked record adds a question, or replaces one parked earlier under its
    id; a transition moves, on a question already parked, the fields
    :func:`transition_fields` names, and says when. Neither may claim an
    answer -- the answer is the host's, so a record saying `approved` or
    `rejected` is passed over -- and anything else a relay holds is not a
    question's. A question that moves is a new dict, so a reader keeping
    what it made of the old one can tell.
    """
    for record in records:
        match record:
            case {
                "parked": {"id": str() as identifier, "state": str() as state} as entry
            } if state not in ("approved", "rejected"):
                folded[identifier] = dict(entry)
            case {
                "transition": {"id": str() as identifier, "at": str() as at} as step
            } if identifier in folded and (
                "state" not in step or step["state"] not in ("approved", "rejected")
            ):
                folded[identifier] = {
                    **folded[identifier],
                    **{
                        field: step[field]
                        for field in transition_fields()
                        if field in step
                    },
                    "changed": at,
                }


def relay_current(log: Path) -> bool:
    """Whether a relay's log is kept in the shape this code reads, or holds nothing to rewrite yet."""
    try:
        with log.open("rb") as stream:
            first = stream.readline()
    except FileNotFoundError:
        return True
    if not first:
        return True
    try:
        return json.loads(first) == relay_header()
    except (ValueError, UnicodeDecodeError):
        return False


def opened_relay(log: Path) -> BinaryIO:
    """A relay's log held exclusively, as the file its path names once the lock is taken.

    A log is rewritten by moving a new file over it, so a writer that waited
    for the lock on the file its path named before holds one nothing reads
    any more: it opens the path again rather than write there.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(64):
        stream = log.open("a+b")
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            named = log.stat().st_ino
        except FileNotFoundError:
            named = -1
        if named == os.fstat(stream.fileno()).st_ino:
            return stream
        stream.close()
    raise OSError(f"{log} was replaced each time it was opened")


def rewrite_relay(log: Path, records: list[dict]) -> None:
    """Replace a relay's log whole: the new file moved over the old, so a crash leaves one or the other.

    Called with the old one held, so nothing lands in it after its records
    were read; a writer that waited on it opens the new one instead
    (:func:`opened_relay`). The new file keeps the old one's mode.
    """
    staged = log.with_name(f".{log.name}.{os.urandom(8).hex()}")
    try:
        with staged.open("xb") as stream:
            stream.write(
                b"".join(
                    json.dumps(record, sort_keys=True).encode() + b"\n"
                    for record in records
                )
            )
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(log.stat().st_mode & 0o7777)
        staged.replace(log)
    finally:
        staged.unlink(missing_ok=True)


def settled_time(entry: dict) -> str | None:
    """When a question kept the older way came to the state its last copy records."""
    match entry:
        case {"state": "expired", "expires": str() as at}:
            return at
        case {"completed": str() as at}:
            return at
        case {"created": str() as at}:
            return at
    return None


def migrated_relay(records: list[dict], blobs: Path) -> list[dict]:
    """A relay kept as a full copy per transition, as records naming documents by digest.

    Each question's copies collapse into the last, which is the state it came
    to, with when it came to it beside it and its documents kept once in the
    store; replies stay as they are, and a record already in the digest shape
    follows them. A copy claiming an answer is passed over, and so is anything
    that is not a question or a reply.
    """

    def copied(record: dict) -> str:
        """The question a record is a full copy of, or "" where it is none -- or claims an answer."""
        match record:
            case {
                "id": str() as identifier,
                "state": str() as state,
                "operation": dict(),
            } if state not in ("approved", "rejected"):
                return identifier
        return ""

    latest = {
        identifier: record for record in records if (identifier := copied(record))
    }
    return [
        relay_header(),
        *(
            {
                "parked": {
                    **stored_form(entry, lambda text: stored_blob(blobs, text)),
                    "changed": settled_time(entry),
                }
            }
            for entry in latest.values()
        ),
        *(record for record in records if "reply" in record and "question" in record),
        *(record for record in records if "parked" in record or "transition" in record),
    ]


def migrate_relay(log: Path) -> None:
    """Rewrite a relay kept as full copies into the shape it is read in, once.

    Under the relay's transaction lock and its log's own, so no transition
    and no append lands midway: whoever takes them first rewrites it, and the
    rest find it rewritten.
    """
    lock = relay_lock(log)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with opened_relay(log) as stream:
            if relay_current(log):
                return
            rewrite_relay(log, migrated_relay(stream_records(stream), relay_blobs(log)))


def relay_records(log: Path) -> list[dict]:
    """Every record a relay's log holds after its heading line, an older log rewritten first."""
    if not relay_current(log):
        migrate_relay(log)
    records = review_records(log)
    return records[1:] if records and records[0] == relay_header() else []


def append_relay_records(log: Path, records: Callable[[], list[dict]]) -> None:
    """Append what *records* builds to a relay's log, built while its lock is held.

    Built under the lock because a parked question keeps its documents in the
    store as it is recorded, and the store is swept under the same lock: a
    document written for a question about to be recorded is never taken for
    one nobody names. A new log opens with its heading line; one an older
    writer started meanwhile is rewritten first, so nothing lands beneath a
    shape it is not read in.
    """
    for _attempt in range(8):
        if not relay_current(log):
            migrate_relay(log)
        with opened_relay(log) as stream:
            size = stream.seek(0, os.SEEK_END)
            if size and not relay_current(log):
                continue
            heading = [relay_header()] if not size else []
            framed(
                stream,
                [
                    json.dumps(record, sort_keys=True)
                    for record in [*heading, *records()]
                ],
            )
            return
    raise OSError(f"{log} kept being started in an older shape")


def park_relay_entry(log: Path, entry: dict) -> None:
    """Park one question as it was asked: its documents kept in the store, its record naming them."""
    append_relay_records(
        log,
        lambda: [
            {
                "parked": stored_form(
                    entry, lambda text: stored_blob(relay_blobs(log), text)
                )
            }
        ],
    )


def transition_relay_entry(log: Path, identifier: str, fields: dict) -> None:
    """Record one question's move to another state, and when, beside the question it moves."""
    step = {"id": identifier, "at": datetime.now(UTC).isoformat(), **fields}
    append_relay_records(log, lambda: [{"transition": step}])


def native_review_records(path: Path) -> dict[str, dict]:
    """Read only native receipts with the fields used by the hermetic boundary.

    A record claiming an answer is skipped: the checkout's relay is the
    session's to write, so an answer found there is one the session could
    have written, and the answer is read from the host instead.
    """
    folded: dict[str, dict] = {}
    fold_relay(folded, relay_records(path))

    def valid():
        for entry in folded.values():
            match entry:
                case {
                    "id": str(),
                    "fingerprint": str(),
                    "state": str(),
                    "reason": str(),
                    "resumption": "native_retry",
                    "operation": {
                        "session": str(),
                        "requester": str(),
                        "cwd": str(),
                        "tool": str(),
                        "payload": dict(),
                    },
                }:
                    yield entry

    return {entry["id"]: entry for entry in valid()}


def review_answers_home(variable: str) -> Path:
    """Where the operator's answers to parked reviews are kept: the host's, per person.

    The directory a launch names in *variable*, which is how a contained
    session finds the host's store at the path the host has it, mounted
    read-only; otherwise `$XDG_STATE_HOME/lup/reviews`, or
    `~/.local/state/lup/reviews` where that is unset or relative.
    """
    declared = declared_identity(variable)
    if declared:
        return Path(declared)
    state = declared_identity("XDG_STATE_HOME")
    base = (
        Path(state)
        if state and Path(state).is_absolute()
        else Path.home() / ".local" / "state"
    )
    return base / "lup" / "reviews"


def review_answers(relay: Path, home: Path) -> Path:
    """The file the operator's answers to one relay's reviews are kept in.

    One directory per repository, named for its shared git directory, so a
    launch lends a session its own repository's answers and no other's; one
    file per relay inside it, so each checkout reads only its own.
    """
    located = relay.resolve()
    shared = shared_git_directory(str(located.parent))
    repository = sha256((shared or str(located.parent)).encode()).hexdigest()[:16]
    checkout = sha256(str(located).encode()).hexdigest()[:16]
    return home / repository / f"{checkout}.jsonl"


def recorded_answers(path: Path) -> dict[str, dict]:
    """The first answer the operator recorded for each review, by its id.

    The first rather than the last: a review is answered once, so an answer
    after it is one the relay refused to record, and nothing later replaces it.
    """

    def valid():
        for entry in review_records(path):
            match entry:
                case {
                    "question": str(),
                    "fingerprint": str(),
                    "answer": {
                        "approved": bool(),
                        "principal": str(),
                        "receipt": str(),
                    },
                }:
                    yield entry

    return {entry["question"]: entry for entry in reversed(list(valid()))}


def review_fingerprint(
    session: str,
    root: str,
    tool: str,
    payload: dict,
    before: dict,
    reason: str,
    rule: str,
    purpose: str,
    reviewer: str,
    expected: dict,
    policy_identity: str,
    resolved: dict,
    bound: dict,
) -> str:
    """The digest one parked call is approved under: everything the approver reads.

    *bound* is what else the approver reads, each part under the name the
    record keeps it by, in the order its scheme lists them
    (:func:`bound_parts`): ``file_reviews``, each file's verdict with the
    document it judged, and ``unpreviewed``, the steps no document states --
    the two halves of what a command changes -- and ``segments``, each
    command of the line with the verdict it reached.
    """
    material = json.dumps(
        [
            session,
            root,
            tool,
            payload,
            before,
            reason,
            rule,
            purpose,
            reviewer,
            expected,
            policy_identity,
            resolved,
            *bound.values(),
        ],
        sort_keys=True,
    )
    return sha256(material.encode()).hexdigest()


def bound_parts(
    entry: dict,
    known: tuple[str, ...] = ("file_reviews", "unpreviewed", "segments"),
) -> dict | None:
    """What else a parked record's fingerprint binds, by name in its scheme's order.

    A record keeps its scheme -- the parts it bound, in order -- so a reader
    on other code tells a record it cannot check from one that changed:
    ``None`` where the scheme names a part this code does not know. A record
    keeping no scheme, or a null one, binds the parts it carries; one it
    holds as null, which a relay writes for a part it never had, it does not.
    """
    scheme = (
        entry["scheme"]
        if "scheme" in entry and entry["scheme"] is not None
        else [name for name in known if name in entry and entry[name] is not None]
    )
    if not isinstance(scheme, list) or not all(
        isinstance(name, str) and name in known for name in scheme
    ):
        return None
    return {name: entry[name] if name in entry else None for name in scheme}


def recorded_fingerprint(entry: dict) -> str:
    """The digest a parked record's own fields hash to, or "" where it lacks one.

    What binds the record an approver reads to the call its fingerprint names:
    a record whose fields hash to another digest shows one call and carries
    another's authority, and nothing may answer or spend it. A retry's
    payload recorded as null is the operation's own, as it was hashed.

    Hashed from what the record holds, so a field a later model adds never
    enters a record parked before it. A hook checks a record it would spend
    with this, and every reader checks a record it shows with this too.
    """
    match entry:
        case {
            "operation": {
                "session": str() as session,
                "cwd": str() as root,
                "tool": str() as tool,
                "payload": dict() as payload,
            },
            "preconditions": dict() as before,
            "reason": str() as reason,
            "rule": str() as rule,
            "requirement": str() as reviewer,
            "execution_payload": dict() | None as expected,
            "policy_identity": str() as policy_identity,
            "resolved": dict() as resolved,
        }:
            purpose = entry["purpose"] if "purpose" in entry else None
            bound = bound_parts(entry)
            if bound is None:
                return ""
            return review_fingerprint(
                session,
                root,
                tool,
                payload,
                before,
                reason,
                rule,
                purpose if isinstance(purpose, str) else "",
                reviewer,
                expected if expected is not None else payload,
                policy_identity,
                resolved,
                bound,
            )
    return ""


def review_hook_call(
    root: Path,
    session: str,
    tool: str,
    arguments: str,
    preconditions: str,
    reason: str,
    rule: str,
    purpose: str,
    reviewer: str,
    execution_id: str = "",
    stage: str = "",
    predecessor: str = "",
    execution_payload: str | None = None,
    policy_identity: str = "",
    file_reviews: str = "null",
    answers: str = "",
    member: str = "",
    placement: str = "ambient",
    provider: str = "",
    unpreviewed: str = "null",
    segments: str = "null",
    agent: str = "",
    account: str = "[]",
) -> dict[Literal["state", "id", "reason"], str]:
    """Park a call or spend its explicit, single-use reviewer answer.

    The answer is read from *answers*, the host's file for this relay, and
    never from the relay: the relay is the session's to write. A parked
    record is matched only where its own fields hash to the call's
    fingerprint, so a record rewritten to show another call spends nothing.

    *file_reviews* and *unpreviewed* are the verdict's record of what the
    call changes -- each file with the document it would hold, and each step
    no document states -- kept on the question for the reviewer to read, as
    *segments* is its record of what each command of the line decided.

    *agent* is the native subagent that asked, blank for the session's own
    conversation, and *account* what the asker said the call is for, each
    with where it was found. Both are kept on a question when it is first
    parked and bound into nothing: they say who asked and what it claims,
    never what an approval releases.

    A declared successor stage may spend one further claim for that same
    identified invocation. The immutable primary claim proves which stage
    consumed the answer; neither a dispatched log row nor observed execution
    alone establishes that authority.

    *root* is where the call runs, which its record names and its
    fingerprint binds; the relay and the claims that spend an answer are
    kept in :func:`review_home`, so a call made from a subdirectory or a
    sibling worktree is recorded where the session's waiter and dashboard
    read it.
    """
    if not session:
        return {
            "state": "unavailable",
            "id": "",
            "reason": "the hook carries no session_id",
        }
    home = review_home(root)
    payload = json.loads(arguments)
    expected = (
        json.loads(execution_payload) if execution_payload is not None else payload
    )
    before = json.loads(preconditions)
    bound = {
        "file_reviews": json.loads(file_reviews),
        "unpreviewed": json.loads(unpreviewed),
        "segments": json.loads(segments),
    }
    resolved = {path: str(Path(path).resolve()) for path in before}
    fingerprint = review_fingerprint(
        session,
        str(root),
        tool,
        payload,
        before,
        reason,
        rule,
        purpose,
        reviewer,
        expected,
        policy_identity,
        resolved,
        bound,
    )
    log = home / ".lup/questions.jsonl"
    blobs = relay_blobs(log)

    def binds(entry: dict) -> bool:
        """Whether a parked record, its documents read back, hashes to this call's fingerprint."""
        try:
            return recorded_fingerprint(resolved_entry(entry, blobs)) == fingerprint
        except ValueError:
            return False

    entries = native_review_records(log)
    matches = [
        entry
        for entry in entries.values()
        if entry["fingerprint"] == fingerprint and binds(entry)
    ]
    continuations = [
        entry
        for entry in matches
        if stage
        and predecessor
        and isinstance(execution_id, str)
        and execution_id
        and entry["state"] == "dispatched"
        and "execution_id" in entry
        and entry["execution_id"] == execution_id
    ]
    if continuations:
        entry = continuations[-1]
        claim = home / ".lup/review-claims" / entry["id"]
        with claim.open(encoding="utf-8") as handle:
            consumed = json.load(handle)
        spent_by = {
            "fingerprint": fingerprint,
            "execution_id": execution_id,
            "stage": predecessor,
        }
        if consumed == spent_by:
            successor = (
                home
                / ".lup/review-stage-claims"
                / entry["id"]
                / sha256(stage.encode()).hexdigest()
            )
            successor.parent.mkdir(parents=True, exist_ok=True)
            try:
                with successor.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(spent_by, sort_keys=True))
            except FileExistsError:
                pass
            else:
                return {"state": "approved", "id": entry["id"], "reason": ""}
    entry = matches[-1] if matches else None
    granted = recorded_answers(Path(answers)) if answers and entry is not None else {}
    answer = (
        granted[entry["id"]]["answer"]
        if entry is not None
        and entry["state"] == "pending"
        and entry["id"] in granted
        and granted[entry["id"]]["fingerprint"] == fingerprint
        else None
    )
    if entry is not None and answer is not None and not answer["approved"]:
        return {
            "state": "rejected",
            "id": entry["id"],
            "reason": answer["note"] if "note" in answer and answer["note"] else "",
        }
    if entry is not None and answer is not None:
        match answer:
            case {"principal": str() as principal, "receipt": "recorded"} if (
                principal
                and principal not in (session, entry["operation"]["requester"])
            ):
                pass
            case _:
                raise ValueError(
                    "hook approval has no recorded independent affirmative answer"
                )
        claim = home / ".lup/review-claims" / entry["id"]
        claim.parent.mkdir(parents=True, exist_ok=True)
        try:
            with claim.open("x", encoding="utf-8") as handle:
                json.dump(
                    {
                        "fingerprint": fingerprint,
                        "execution_id": execution_id,
                        "stage": stage,
                    },
                    handle,
                    sort_keys=True,
                )
        except FileExistsError:
            entry = None
        else:
            transition_relay_entry(
                log,
                entry["id"],
                {"state": "dispatched", "execution_id": execution_id},
            )
            return {"state": "approved", "id": entry["id"], "reason": ""}
    if entry is not None and entry["state"] == "pending":
        return {"state": "pending", "id": entry["id"], "reason": entry["reason"]}
    identifier = os.urandom(16).hex()
    # The checkout the call changes: the one holding every file it records,
    # which a session editing a sibling worktree does not sit in, and the
    # checkout the call runs in where it records none or files in several.
    changed = {worktree_root(path) for path in resolved.values()} - {""}
    labelled = changed.pop() if len(changed) == 1 else str(checkout_home(root))
    entry = {
        "id": identifier,
        "fingerprint": fingerprint,
        "reason": reason,
        "rule": rule,
        "purpose": purpose or None,
        "requirement": reviewer,
        "eligible": [],
        "chain_resolved": False,
        "state": "pending",
        "execution_id": execution_id,
        "execution_payload": expected,
        "created": datetime.now(UTC).isoformat(),
        "preconditions": before,
        "resolved": resolved,
        "policy_identity": policy_identity,
        **bound,
        "scheme": list(bound),
        "resumption": "native_retry",
        "member": member,
        "agent": agent,
        "account": json.loads(account),
        "operation": {
            "id": identifier,
            "session": session,
            "requester": session,
            "tool": tool,
            "payload": payload,
            "cwd": str(root),
            "worktree": labelled,
            "placement": placement,
            "provider": provider,
        },
    }
    park_relay_entry(log, entry)
    return {"state": "pending", "id": identifier, "reason": reason}


def waiting_edits(root: Path, session: str, agent: str) -> int:
    """How many calls one conversation has waiting on the operator here that change files.

    A call recorded with the files it would change, still waiting, asked by
    *session*'s conversation *agent* -- blank for the session's own. What
    tells a conversation its edits are arriving one review at a time.
    """
    log = review_home(root) / ".lup/questions.jsonl"
    return sum(
        1
        for entry in native_review_records(log).values()
        if entry["state"] == "pending"
        and entry["operation"]["session"] == session
        and (entry["agent"] if "agent" in entry else "") == agent
        and "preconditions" in entry
        and entry["preconditions"]
    )


def records_backwards(path: Path, block: int = 1 << 16) -> Iterator[dict]:
    """A JSON-lines file's records, newest first, read back from its end.

    What an agent said last is at the end of a transcript that runs to
    hundreds of megabytes, so it is read *block* bytes at a time from the
    end and never whole. A line that is not a JSON object -- the one still
    being written among them -- is passed over; a file that cannot be read
    has no records.
    """
    try:
        stream = path.open("rb")
    except OSError:
        return
    with stream:
        size = stream.seek(0, os.SEEK_END)
        carried = b""
        for end in range(size, 0, -block):
            start = max(0, end - block)
            stream.seek(start)
            lines = (stream.read(end - start) + carried).splitlines()
            carried = lines[0] if start else b""
            for line in reversed(lines[1:] if start else lines):
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict):
                    yield record


def words_before(transcript: Path, spoken: Callable[[dict], str | None]) -> str:
    """What an agent said since it last heard anything, read back off its transcript.

    *spoken* reads one record in its runtime's own words: the text the agent
    wrote there, blank where the record holds nothing it said, or ``None``
    where it is something the agent heard -- a person's message, a tool's
    result -- before which nothing it said is about this call. Kept whole,
    in the order it was said.
    """

    def said() -> Iterator[str]:
        for record in records_backwards(transcript):
            words = spoken(record)
            if words is None:
                return
            if words.strip():
                yield words

    return "\n\n".join(reversed(list(said())))


def observe_hook_call(
    root: Path, session: str, tool: str, arguments: dict, execution_id: str
) -> list[str]:
    """Reconcile execution with its receipt without inferring authorization."""
    log = review_home(root) / ".lup/questions.jsonl"
    if not session or not log.exists():
        return []
    entries = native_review_records(log)
    blobs = relay_blobs(log)

    def called(entry: dict) -> dict | None:
        """The entry with the strings of its call read back, or ``None`` where one cannot be."""
        try:
            return resolved_call(entry, blobs)
        except ValueError:
            return None

    def same_call(entry: dict | None) -> bool:
        if entry is None:
            return False
        expected = entry["execution_payload"] if "execution_payload" in entry else None
        return entry["operation"]["tool"] == tool and arguments == (
            entry["operation"]["payload"] if expected is None else expected
        )

    def requested(entry: dict | None) -> bool:
        return entry is not None and (
            entry["operation"]["payload"] == arguments or same_call(entry)
        )

    matches = [
        entry
        for entry in entries.values()
        if entry["operation"]["session"] == session
        and entry["operation"]["cwd"] == str(root)
        and (
            "execution_id" in entry and entry["execution_id"] == execution_id
            if execution_id
            else entry["operation"]["tool"] == tool and requested(called(entry))
        )
        and entry["state"]
        in ("pending", "approved", "rejected", "dispatched", "prompted")
    ]
    if not matches:
        return []
    entry = matches[-1]
    matches_call = same_call(called(entry))
    # `prompted` is the runtime's own dialog answering for a call whose effect
    # a checkpoint restores: authority nobody recorded, so what is checked here
    # is only that what ran is what was shown. A receipt proves who answered;
    # this proves the document and the input did not move underneath them.
    settled = entry["state"] in ("dispatched", "prompted")
    authorized = settled and matches_call
    problem = (
        "with a tool or payload different from the reviewed call"
        if settled and not matches_call
        else "without a consumed approval receipt"
    )
    transition_relay_entry(
        log,
        entry["id"],
        {
            "state": "completed" if authorized else "in_doubt",
            "completed": datetime.now(UTC).isoformat(),
            "outcome": "Native execution observed; effect success is not verified."
            if authorized
            else f"Native execution observed {problem}.",
        },
    )
    if authorized:
        return []
    return [
        f"Lup review {entry['id']}: execution was observed {problem}. "
        f"Inspect {log}; this observation grants no authority."
    ]


def record_question(
    root: Path | None,
    command: str,
    reason: str,
    rule: str,
    purpose: str,
    reviewer: str,
    escalated: str,
    placement: str,
    session: str = "",
    requester: str = "",
    relay: str = ".lup/questions.jsonl",
) -> str:
    """Park one final ask in the durable relay, before anybody is shown it.

    Every route to a question passes through a verdict, and the boundary that
    reaches one is what every provider has in common — so a record written
    here is a record both of them produce, which is what makes the relay one
    authority rather than a store the in-process seam happens to use.

    Primitives rather than a verdict, because this half may reach nothing but
    the pinned standard library and a verdict is the kernel's. The caller
    reads the fields off it.

    Appended, because the failure this survives is a crash between
    recording a question and answering it, and a store rewritten in place has
    a window where the question is neither the old one nor the new one.

    The id is derived from the session and what is being asked, so the same
    question reached twice folds to one record instead of filling a queue
    nobody can then read. What it deliberately does not record is *who
    answered*: on the interactive path that is a receipt inferred from the
    provider's own behaviour, and inventing one here would be writing down a
    decision nobody made.

    Silent about its own failure, for the reason every writer in this module
    is: it runs in front of an operation somebody asked for, and a read-only
    checkout is an ordinary place to be running. What it must not do is turn
    a policy question into a crash.
    """
    if root is None or not command:
        return ""
    identifier = sha256(f"{session}:{command}:{reason}".encode()).hexdigest()[:16]
    entry = {
        "id": identifier,
        "operation": {
            "id": identifier,
            "session": session,
            "requester": requester,
            "tool": "Bash",
            "payload": {"command": command},
            "cwd": str(root),
            "worktree": str(root),
            "placement": placement,
        },
        "fingerprint": identifier,
        "reason": reason,
        "rule": rule,
        "purpose": purpose or None,
        "requirement": reviewer,
        # Empty and *said to be unresolved*, which are different facts: this
        # boundary is hermetic and cannot reach the session's principals, so
        # it has no chain to resolve rather than a chain that resolved to
        # nobody. Written as the second, every question parked here would be
        # answerable by nobody and the queue could only grow.
        "eligible": [],
        "chain_resolved": False,
        "escalation": escalated,
        "state": "pending",
        "created": datetime.now(UTC).isoformat(),
    }
    path = review_home(root) / relay
    try:
        folded: dict[str, dict] = {}
        fold_relay(folded, relay_records(path))
        if identifier in folded:
            return identifier
        park_relay_entry(path, entry)
    except OSError:
        return ""
    return identifier


def record_deferral(
    root: Path | None,
    command: str,
    reason: str,
    judged: bool,
    corpus: str = ".lup/hooks/learned.jsonl",
) -> str:
    """Write down one command this policy declined to interrupt about.

    The other half of allow-and-log, and what makes the relaxation honest.
    The lattice asked about everything unjudged for an *observability*
    reason, and logging serves that without spending anybody's attention --
    but only if something is actually written down, or the relaxation is
    just the asking removed.

    **Two kinds of deferral reach here and they are worth very different
    things**, which is why ``judged`` is recorded rather than inferred later.
    An unjudged one is a gap in the vocabulary: nobody has ever said anything
    about this command, and it is a candidate for a rule. A judged one is the
    relaxation working -- a rule looked, and the boundary answered for the
    loss -- and it is an audit trail rather than a candidate. Collapsing them
    would put `git reset --hard` in the same list as a command nobody has
    classified, and the list is read to find the second.

    **Written at the moment the verdict exists**, rather than after the
    command has run. The later event cannot serve: a runtime
    offers both "yes" and "yes, don't ask again" and the later event cannot
    tell them apart, and a human may answer by *editing* the command, so it
    fires for something other than what was judged. None of that touches a
    deferral, which is nobody's approval and is exactly known here.

    **One line per distinct command.** A session defers the same `grep` fifty
    times, and fifty identical lines is a list nobody reads -- the same
    failure the undo layer's dedup exists to prevent, in the same shape. So a
    command already written down is skipped, and the file is a set: what a
    diff shows is what this session met that no session had met before.

    Silent about its own failure, and for the reason :func:`undo_snapshot`
    is: this runs in front of a command somebody asked for, and a read-only
    checkout is an ordinary place to be running. An empty string says nothing
    was recorded.
    """
    if root is None or not command:
        return ""
    path = checkout_home(root) / corpus
    try:
        seen = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""
    entry = json.dumps(
        {
            "command": command,
            "reason": reason,
            "judged": judged,
            "first_seen": datetime.now(UTC).isoformat(),
        },
        sort_keys=True,
    )
    # Compared on the command alone, because the rest of the row is what this
    # session happened to say about it: the same command reached twice under
    # two reasons is one candidate, and a timestamp differs every time.
    for line in seen.splitlines():
        try:
            held = json.loads(line)
        except ValueError:
            continue
        if isinstance(held, dict) and "command" in held and held["command"] == command:
            return ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as sink:
            sink.write(entry + "\n")
    except OSError:
        return ""
    return entry


def approvals_log(root: Path) -> Path:
    """Where execution observations are retained beside the checkout.

    Append-only, for the reason the question relay is: the failure this
    survives is a crash between two writes, and a file rewritten in place has
    a window where it is neither the old record nor the new one. The latest
    line for a fingerprint is its state, so forgetting is one more line rather
    than an erasure. A function rather than a constant because the compiled
    dispatcher carries this half's functions and nothing beside them.
    """
    return checkout_home(root) / ".lup/hooks/approvals.jsonl"


def approval_fingerprint(kind: str, subject: str, root: Path | None) -> str:
    """One observed subject: what it does, and from where.

    The kind and the text are the whole of what was judged -- a command, a
    URL -- and the checkout it runs from is the third term, because the same
    command means something else in another tree. What is deliberately left
    out is the session: this audit groups repeated subjects and grants no
    authority. Review receipts bind separately to the requesting session.
    """
    material = json.dumps([kind, subject, str(root) if root else ""], sort_keys=True)
    return sha256(material.encode()).hexdigest()


def approval_subject(
    tool: str, tool_input: dict
) -> dict[Literal["kind", "text"], str] | None:
    """What one call did, as the memory keys it, for the tools it remembers.

    A shell command and a fetch, under either runtime's name for them. An
    edit is left out on purpose: its exact call includes the document it
    replaces, which its first application changed, so a repeat is never the
    same call and a memory of it would answer nothing.
    """
    if tool == "Bash" and "command" in tool_input:
        return {"kind": "shell", "text": str(tool_input["command"])}
    if tool in ("WebFetch", "web_fetch") and "url" in tool_input:
        return {"kind": "fetch", "text": str(tool_input["url"])}
    return None


def approval_states(root: Path | None) -> dict[str, dict]:
    """The latest line per fingerprint, which is that call's standing."""
    if root is None:
        return {}
    path = approvals_log(root)

    def entries():
        try:
            lines = (
                path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            )
        except OSError:
            return
        for line in lines:
            try:
                held = json.loads(line)
            except ValueError:
                continue
            if isinstance(held, dict) and "fingerprint" in held and "state" in held:
                yield held

    return {str(held["fingerprint"]): held for held in entries()}


def noted_approval(root: Path | None, entry: dict) -> bool:
    """Append one line, silent about a checkout that cannot be written."""
    if root is None:
        return False
    path = approvals_log(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        return False
    return True


def note_asked(root: Path | None, fingerprint: str, kind: str, subject: str) -> None:
    """Record a policy question without granting authority to execute it."""
    latest = approval_states(root)
    if fingerprint in latest and latest[fingerprint]["state"] == "asked":
        return
    noted_approval(
        root,
        {
            "fingerprint": fingerprint,
            "state": "asked",
            "kind": kind,
            "subject": subject,
            "cwd": str(root) if root else "",
            "at": datetime.now(UTC).isoformat(),
        },
    )


def note_ran(root: Path | None, fingerprint: str) -> str:
    """Record execution as an observation, never as reusable authorization."""
    latest = approval_states(root)
    if fingerprint not in latest or latest[fingerprint]["state"] != "asked":
        return ""
    when = datetime.now(UTC).isoformat()
    noted_approval(root, {**latest[fingerprint], "state": "observed", "at": when})
    return when


def forget_approval(root: Path | None, fingerprint: str) -> bool:
    """Retire an execution observation; it grants no authority either way."""
    latest = approval_states(root)
    if fingerprint not in latest or latest[fingerprint]["state"] not in (
        "approved",
        "observed",
    ):
        return False
    return noted_approval(
        root,
        {
            **latest[fingerprint],
            "state": "forgotten",
            "at": datetime.now(UTC).isoformat(),
        },
    )


def managed_script_roots(root: Path | None) -> list[str]:
    """Name the package roots a runtime installed and therefore trusts.

    The workspace-local plugin directory is deliberately not a root: it is
    agent-adjacent and verified only at launch, so an approved write there
    must not grant silent execution rights for the rest of the session.
    """
    if root is None or not root.is_absolute():
        return []
    return [str(root / "skills"), str(root / "plugins" / "cache")]


def worktree_path(path_text: str) -> str:
    """Relativize against the worktree holding the path, not the cwd.

    Every repo-relative rule — human-owned files, protected directories, the
    role a path carries — matches on this answer, so anchoring it anywhere
    but the file's own worktree decides policy by where the runtime happened
    to be launched. A sibling worktree is writable and is not under the
    launch directory; the cwd this script is promised nothing about is not a
    root at all. Both leave the path absolute, and every rule silently misses.
    """
    root = worktree_root(path_text)
    if not root:
        return path_text
    return Path(path_text).resolve().relative_to(root).as_posix()


def is_git_marker(marker: Path) -> bool:
    """Whether *marker* carries Git metadata rather than only its name."""
    return marker.is_file() or (marker.is_dir() and (marker / "HEAD").is_file())


def worktree_root(path_text: str) -> str:
    """The checkout a path belongs to, or "" when it belongs to none.

    The same walk :func:`worktree_path` relativizes against, kept whole
    rather than discarded, because the root is the answer to a second
    question: a language server asked about this file resolves its imports
    against exactly this directory, and resolving them against wherever the
    session was launched reads the same module names out of another tree.
    """
    path = Path(path_text)
    if not path.is_absolute():
        return ""
    resolved = path.resolve()
    # The path itself is a candidate, not only its parents: a file never
    # holds a `.git`, so nothing changes for one, and a directory that is
    # already a checkout root would otherwise be answered for by whatever
    # encloses it — or by nothing at all.
    for root in [resolved, *resolved.parents]:
        if is_git_marker(root / ".git"):
            return str(root)
    return ""


def checkout_home(cwd: Path) -> Path:
    """The top of the checkout *cwd* sits in, where its `.lup` state is kept; *cwd* where it is in none.

    A session's shell moves: a `cd` into `tmp/` leaves every later call
    there, and state written beside the working directory would scatter a
    `.lup` into each directory a call happened to run from, where nothing
    reads it.
    """
    root = worktree_root(str(cwd))
    return Path(root) if root else cwd


def review_home(cwd: Path) -> Path:
    """The checkout a session's reviews are kept in, wherever the call it parks runs.

    The checkout its launch opened, which ``LUP_BOUNDARY_ROOT`` names: the
    relay whose answers the launch lends the session, which the dashboard
    reads with the code the session runs and the session's review commands
    name as ``uv run --directory <it>``. So a call made from a subdirectory,
    a sibling worktree or another repository is recorded there, labelled
    with the checkout it changes. Unlaunched, the checkout holding *cwd*.
    """
    launched = Path(declared_identity("LUP_BOUNDARY_ROOT"))
    if launched.is_absolute() and launched.is_dir():
        return launched
    return checkout_home(cwd)


def sibling_worktrees(root: Path | None = None) -> list[str]:
    """Every other checkout of the repository holding *root*, where each stands.

    Read from `git worktree list --porcelain`, the one place Git states them:
    each entry is a block of lines ending at a blank one, opened by
    `worktree <path>`, and one carrying `bare` is the repository a linked
    layout keeps beside its checkouts, which holds none to write into. The
    checkout *root* sits in is left out -- its own paths are read relative to
    it already. Nothing where Git cannot answer.
    """
    where = Path.cwd() if root is None else root
    here = worktree_root(str(where.resolve()))
    lines = git_answers(["worktree", "list", "--porcelain"], where) or []

    def checkouts():
        """Each entry's path, once its block has said it is not the bare one."""
        tree = ""
        for line in [*lines, ""]:
            if line.startswith("worktree "):
                tree = line.removeprefix("worktree ")
            if line == "bare":
                tree = ""
            if not line and tree:
                yield tree
                tree = ""

    return [tree for tree in checkouts() if str(Path(tree).resolve()) != here]


def walked_withheld(
    walked: str,
    hidden: bool,
    named: Callable[[str], bool],
    withheld: Callable[[str], bool],
    pruned: Callable[[str], bool],
    skipped: Callable[[str], bool],
    root: Path | None = None,
    reserve: float = 1.0,
) -> str:
    """The first withheld path a recursive read of *walked* would reach, or "".

    The kernel reads which words a command walks; what lies beneath each is
    this half's to say, since only a filesystem can. A home is spelled from
    the home -- `~`, `$HOME` -- and every other root from where the command
    stands. A root that is not a directory walks nothing: a file is named, and
    judged as named.

    ``named`` says whether a file or a directory could carry a withheld path
    by its name alone, which is what keeps this from reading every pattern at
    every file: only a file so named, or beneath a directory so named, is put
    to ``withheld``, the kernel's whole reading. ``hidden`` false skips names
    beginning with a dot, as `rg` does unless told otherwise; ``pruned`` is a
    directory and ``skipped`` a file the command told its walk to leave out.

    What was found is returned beneath the root as the command spelled it --
    `~/.codex/auth.json`, `.lup/codex-home/auth.json` -- which is the path the
    refusal names, and whose first name is the directory to leave out.

    Bounded by the hook's deadline less ``reserve``: a walk that has not
    finished by then returns the directory it stopped in, which is withheld
    by no pattern, and the kernel refuses a read nobody finished walking.
    """
    where = Path.cwd() if root is None else root
    home = str(Path.home())
    spelled = next(
        (
            home + walked.removeprefix(variable)
            for variable in ("$HOME", "${HOME}")
            if walked == variable or walked.startswith(f"{variable}/")
        ),
        str(Path(walked).expanduser()),
    )
    if any(mark in spelled for mark in "$*?["):
        return ""
    start = (where / spelled).resolve()
    if not start.is_dir():
        return ""
    for directory, folders, files in start.walk():
        if hook_seconds_left(float("inf")) < reserve:
            return (Path(walked) / directory.relative_to(start)).as_posix()
        folders[:] = [
            name
            for name in folders
            if (hidden or not name.startswith(".")) and not pruned(name)
        ]
        beneath = any(named(part) for part in directory.parts)
        found = next(
            (
                path
                for name in files
                if (hidden or not name.startswith(".")) and not skipped(name)
                if beneath or named(name)
                for path in [directory / name]
                if withheld(str(path))
            ),
            None,
        )
        if found is not None:
            return (Path(walked) / found.relative_to(start)).as_posix()
    return ""


def shared_git_directory(path_text: str) -> str:
    """The one directory every worktree of a repository can name alike.

    The publisher sits in whichever checkout was edited and the reader in
    whichever one its server was launched from, and neither can see the
    other's. What they share is git's own directory: a linked worktree's
    ``.git`` is a file naming its per-worktree directory beneath the common
    one, and a main checkout's ``.git`` is that common directory. So both
    ends resolve to one place without either being told where the other is,
    and without depending on a variable reaching a hook and a server alike.

    The common directory rather than the main checkout, because a checkout
    is not guaranteed to be there: a repository can keep its git directory
    beside its worktrees rather than inside one, and then the path above
    `worktrees/` is not a checkout at all — it is whatever happens to
    enclose the repository, which is nobody's to write into.
    """
    root = worktree_root(path_text)
    if not root:
        return ""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                root,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=hook_seconds_left(5),
            check=False,
        )
    except subprocess.TimeoutExpired:
        # "No repository" read here makes a path in another repository this
        # project's own, so under a hook an unanswered question ends it.
        refuse_unanswered("Git")
        return ""
    return str(Path(result.stdout.strip()).resolve()) if result.returncode == 0 else ""


def boundary_description(
    root: Path | None, ledger: str = ".lup/boundary.json"
) -> dict[str, list[str]]:
    """What the launcher recorded about the boundary this session runs behind.

    The mount table and the egress allowlist are *launch* facts, not
    generation facts: which worktrees exist and which of them this session
    leased is decided when the container starts, long after this dispatcher
    was compiled. So the launcher writes them down and this reads them back,
    the same shape the run ledger already uses.

    An absent file is the ordinary answer rather than a failure: an
    uncontained session has no boundary to describe, and one whose launcher
    predates this has none recorded. Both mean the same thing to every caller
    -- there is nothing here to attribute a failure to -- so both come back
    empty.
    """
    if root is None:
        return {}
    try:
        raw = (root / ledger).read_text()
    except OSError:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        name: [item for item in value if isinstance(item, str)]
        for name, value in loaded.items()
        if isinstance(name, str) and isinstance(value, list)
    }


def unquoted_path(word: str) -> str:
    """One word of a diagnostic, with the punctuation it was quoted in removed.

    An error message is prose with a path in it, and prose has no parser --
    which is why this trims rather than parses. Kept as its own function so
    that reasoning sits beside the one line it excuses.
    """
    # lup: ignore[string-strip] — the quotes a diagnostic wraps a path in are
    # exactly what has to come off, and no parser reads free-form prose
    return word.strip("'\"`:,;()[]<>")


def boundary_refusal(failure: str, described: dict[str, list[str]]) -> str:
    """Name the boundary as the cause of a failure, or say nothing at all.

    A confined command that fails fails in the vocabulary of whatever it was
    doing. A write the mount table refused arrives as ``Read-only file
    system`` and a host the proxy refused arrives as a timeout, and neither
    says "you are confined" -- so an agent reading them debugs the filesystem
    or the network library, for as long as it takes somebody to notice.

    The discipline that makes this worth having is the refusal to guess. A
    marker in the text never suffices on its own, because ``Read-only file
    system`` appears for a genuinely read-only disk too; the claim is made
    only where a *declared* read-only mount covers the path the failure named.
    Everything else says nothing, and silence is the right answer here far
    more often than a claim is. A wrong boundary claim is worse than none: it
    teaches an agent to reach for the host when the bug was in its own code,
    and that lesson outlives the one command it was wrong about.

    The same reading as :mod:`lup.sandbox.attribution`, in the pinned standard
    library, because this one runs inside the compiled dispatcher where that
    module cannot be imported. What it deliberately does not repeat is the
    egress half: a refused host is read out of the proxy's own log rather than
    out of the client's guess about why its connection died, and reaching that
    log means reaching the container runtime, which this half must not do.

    A path under a repository's shared git directory gets the remedy that
    fits it, because the generic one is wrong there: nothing belongs in the
    image declaration, and what git could not write -- `config`, the lock
    `git gc` takes -- is the host's to change from its own terminal. The
    launch records those directories as ``git_shared`` and the writable
    directories bound back inside them as ``writable``.
    """
    markers = described["write_refusals"] if "write_refusals" in described else []
    read_only = described["read_only"] if "read_only" in described else []
    writable = described["writable"] if "writable" in described else []
    shared = described["git_shared"] if "git_shared" in described else []
    if not markers or not read_only:
        return ""
    if not any(marker in failure for marker in markers):
        return ""

    def under(path: str, mount: str) -> bool:
        return path == mount or path.startswith(mount + "/")

    # The deepest mount decides, as the table does: a writable directory
    # bound back inside a read-only one is where a write under it landed,
    # and a refusal there is not this boundary's.
    covering = [
        (candidate, mount)
        for word in failure.split()
        for candidate in [unquoted_path(word)]
        if candidate.startswith("/") and len(candidate) > 1
        for mount in read_only
        if under(candidate, mount)
        and not any(
            under(candidate, opened) and len(opened) > len(mount) for opened in writable
        )
    ]
    if not covering:
        return ""
    candidate, mount = covering[0]
    if any(under(candidate, directory) for directory in shared):
        return (
            f"The boundary refused this, not the filesystem: {mount} is held "
            "read-only because the host runs what a repository's git `config` "
            "and `hooks/` name. Changing git configuration -- a remote, an "
            "upstream, `user.*`, a submodule -- or running `git gc` is for a "
            "host terminal; commits, branches, tags, fetch and `git push origin "
            "<branch>` work here."
        )
    return (
        f"The boundary refused this, not the filesystem: {mount} is "
        "mounted read-only on purpose, so retrying, changing permissions or "
        "creating the parent will not help. Work inside your own tree, or "
        "propose adding the path to the image declaration if it genuinely "
        "belongs in every session."
    )


def boundary_account(
    # lup: ignore[dict-str-payload] — each runtime's own tool-response mapping, whose stream keys differ per runtime
    response: str | dict[str, str | int | float | bool | None],
    root: Path | None,
) -> list[str]:
    """The boundary's sentence for what a finished shell command printed, if any.

    Read after the command rather than before, because a refusal is only
    known once the kernel has made it -- and read whether or not the command
    failed: `git push -u` lands the push, fails to record the upstream in the
    read-only `config`, and exits 0 with the refusal in its output. Each
    runtime hands the output over in its own shape, a string or a mapping of
    streams, so every string in it is read.
    """
    spoken = (
        response
        if isinstance(response, str)
        else "\n".join(value for value in response.values() if isinstance(value, str))
    )
    said = boundary_refusal(spoken, boundary_description(root))
    return [said] if said else []


def outside_this_project(path_text: str, root: Path | None) -> bool:
    """Whether this path sits outside the repository the session is working in.

    Wider than :func:`foreign_repository` by exactly the case that needs no
    second repository to be decidable: a path belonging to no checkout at all.
    Where the session's own repository is known, a file in somebody's home
    directory or under a runtime's scratch root is known not to be in it, and
    is no more this project's code than another checkout's is.

    The undecidable half stays undecidable. A session in no repository and an
    unreadable ``.git`` leave this end blank, and nothing can be established
    to be outside a boundary that could not be read, so both say no.

    A relative path is anchored on the session's own directory before it is
    asked about, which is where the tool that carried it will resolve it. Any
    other reading answers "outside" for the ordinary spelling of a file in
    this repository — the one every gate here exists for — and a ``..`` that
    genuinely climbs out is settled by the same anchoring rather than by a
    separate rule.

    What this settles is deliberately narrow: whose code a file is, and never
    what may be done to it. Only the gates whose subject is this project's own
    conventions read it; everything about the act itself -- a protected path,
    a whole-file write, the size of the change -- is answered without it.
    """
    if root is None:
        return False
    here = shared_git_directory(str(root))
    return bool(here) and here != shared_git_directory(str(root / path_text))


def foreign_repository(path_text: str, root: Path | None) -> bool:
    """Whether this path belongs to a repository other than the session's.

    The discriminator is the *repository*, never the checkout. A sibling
    worktree of this repository is still this repository's code and still
    answers to its conventions, so comparing checkout roots would lift every
    rule the moment work moved one directory sideways -- which is most of how
    this project is worked on. :func:`shared_git_directory` is the answer both
    ends can name alike, whichever worktree either of them is sitting in.

    A path in no repository says no, because the question here is which
    *other* repository owns the file and there is none for the referral to
    name; whether it is this project's code at all is the wider
    :func:`outside_this_project`. A session in no repository and an unreadable
    ``.git`` say no because nothing was established, and the honest reading of
    "cannot tell" is that this project's rules still apply: lifting them on a
    guess would silence the gates on this repository's own files, where
    keeping them costs friction somewhere that is not ours.

    A relative path is anchored on the session's own directory first, as
    :func:`outside_this_project` anchors it: read bare, a relative spelling
    names no checkout at all, and would say no for a file that plainly has one.
    """
    if root is None:
        return False
    return bool(shared_git_directory(str(root / path_text))) and outside_this_project(
        path_text, root
    )


def this_checkout_path(path_text: str, root: Path | None) -> str:
    """This path as this repository's checkout holding it spells it, or "".

    :func:`worktree_path` anchors a path at the checkout nearest the file,
    which is the right anchor for every rule but one. A repository nested
    inside a checkout -- a probe kit given its own ``git init`` under
    ``tmp/``, so a runtime launched there takes it as the project root -- has
    a ``.git`` nearer the file than the checkout's, so the file arrives
    spelled against the kit and claims none of the roles this repository
    declares for the tree around it. This is the other anchor, and the kernel
    reads it for that one question.

    The session's own checkout is asked first, then the other worktrees of
    the same repository (:func:`sibling_worktrees`), the deepest holding the
    file: a session is sent to work in a sibling by absolute path, and a kit
    under that sibling's ``tmp/`` is the same project's scratch.

    Resolved before it is compared, so a link is judged where it lands: a
    ``refs/`` entry pointing at another project is outside every checkout of
    this one however it is spelled. A relative path is anchored on the
    session's own directory, where the tool carrying it resolves it, and a
    session in no checkout holds nothing, which the empty answer says.
    """
    if root is None:
        return ""
    checkout = worktree_root(str(root))
    if not checkout:
        return ""
    resolved = (root / path_text).resolve()
    if resolved.is_relative_to(checkout):
        return resolved.relative_to(checkout).as_posix()
    holding = [
        tree
        for tree in (Path(sibling).resolve() for sibling in sibling_worktrees(root))
        if resolved.is_relative_to(tree)
    ]
    if not holding:
        return ""
    nearest = max(holding, key=lambda tree: len(tree.parts))
    return resolved.relative_to(nearest).as_posix()


def publish_edition(path_text: str, session: str) -> None:
    """Say which checkout an edit landed in, for the servers that would guess.

    A language server and the code-intelligence tools are started once and
    hold the directory the session opened in for the rest of their lives.
    Editing moves — this project asks that it move, into a worktree — and
    nothing about that reaches them, so they answer about the launch tree
    with no sign that they have. The edited file is the one thing that knows
    where editing is happening, and this is the only place that sees it.

    Written after the tool ran, never before: the same call from the
    permission path would put a filesystem failure between an edit and its
    verdict, and this must not be able to decide anything. For the same
    reason an unwritable destination is reported and dropped — a session
    whose diagnostics stay rooted where they were is worth strictly more
    than one that stopped editing over it.

    Only an edit in the *session*'s own repository, whichever of its
    worktrees: the servers read the record there, and a file in another — a
    repository nested in the checkout, or one anywhere else — would have it
    written into that repository's git directory, read by nobody.

    The rename is the whole guarantee, and the temporary is dot-prefixed and
    named for this write alone, for the reasons
    ``lup.channels.models.write_atomic`` gives: two sessions editing at once
    would otherwise truncate each other's staging file. This cannot call that
    one: it is compiled into a bare script with no ``lup`` to import.
    """
    root = worktree_root(path_text)
    shared = shared_git_directory(path_text)
    if not root or not shared or shared != shared_git_directory(session):
        return
    destination = Path(shared) / "lup" / "edition.json"
    record = json.dumps(
        {"workspace": root, "file": str(Path(path_text).resolve())}, indent=2
    )
    temporary_path = destination.with_name(
        f".{destination.name}.{os.urandom(8).hex()}.tmp"
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with temporary_path.open("x", encoding="utf-8") as staged:
                staged.write(record + "\n")
            temporary_path.replace(destination)
        finally:
            temporary_path.unlink(missing_ok=True)
    except OSError as error:
        print(f"lup: could not publish the edition: {error}", file=sys.stderr)


def project_environment(
    root: Path,
    variable: str = "UV_PROJECT_ENVIRONMENT",
    default: str = ".venv",
) -> Path:
    """Where a sync puts *root*'s environment.

    ``.venv`` beside the manifest is `uv`'s default and this project's own
    layout, but it is a default rather than the answer: *variable* redirects
    it, which is how one environment gets shared across worktrees, kept off a
    slow filesystem, or placed where a container expects it. A relative value
    resolves against the project, the way `uv` resolves it, rather than
    against whatever directory a command happened to run in.

    Lives in this half because this is the constrained one: the hook is
    compiled to a bare script that may import nothing but the standard
    library, so logic it needs cannot sit anywhere it would have to import
    from. Everything else reads it from here, which is what keeps one answer
    rather than two that agree until they do not.

    Both names arrive as defaults rather than as module constants because the
    compiler that splices this half into each hook carries functions alone —
    a name declared beside one would be left behind, and the script would
    reference it undefined.
    """
    environ = os.environ  # lup: ignore[os-environ] — uv's own configuration
    declared = (environ[variable] if variable in environ else "").strip()
    if not declared:
        return root / default
    return Path(declared) if Path(declared).is_absolute() else root / declared


def declared_program(root: str, declared: str) -> str:
    """Where a declared program is, or "" when it is not there to run.

    The checkout answers first, whatever the spelling. That is what makes the
    verdict the edited tree's rather than whichever environment the session
    was launched from, and it is the property worth keeping — a sibling
    worktree holds the same relative path with different contents.

    Only where the checkout holds nothing does the spelling decide. A bare
    name goes to the OS to find on ``PATH``, for a project whose toolchain
    lives somewhere else entirely: a conda environment, a pyenv shim, a
    system or user-level install, an environment ``UV_PROJECT_ENVIRONMENT``
    put outside the project. A path that resolved to nothing stays nothing,
    because a project that named a location meant that location.

    Accepting only the first would make this gate unavailable rather than
    configurable. A declared program it cannot resolve produces no
    diagnostics and says nothing about why, so a project outside one layout
    would not get a weaker check — it would get silence indistinguishable
    from a clean file, on every edit.

    A bare name is asked of the checkout's own environment before ``PATH``,
    because that is where a project's toolchain is installed and asking is
    what keeps the declaration from naming a layout. Spelling the path in
    would answer only for the layout it spelled: ``.venv`` is `uv`'s default
    and nothing else's, so a project that redirects it, or that installs
    through conda or pyenv, would resolve to nothing and be gated in silence.
    The scripts directory comes from the running interpreter — ``bin`` on
    POSIX, ``Scripts`` on Windows — because that is a property of how Python
    is installed rather than of any project, and reading it is what keeps
    this from carrying a layout assumption of its own.
    It is read as a candidate rather than as the answer: a hook runs under
    whichever ``python3`` the runtime found, and one installed in ``sbin``
    names a directory no environment has, which alone would resolve every
    declared program to a bare name and leave the gate silent on a machine
    where each one is installed. The conventional pair follows it, so the
    interpreter still decides where it can and never decides alone.
    """
    located = Path(root) / declared
    if located.is_file():
        return str(located)
    if "/" in declared or "\\" in declared:
        return ""
    environment = project_environment(Path(root))
    for scripts in dict.fromkeys([Path(sys.executable).parent.name, "bin", "Scripts"]):
        installed = environment / scripts / declared
        if installed.is_file():
            return str(installed)
    return declared


def conflicted(path_text: str) -> bool:
    """Whether a file is holding a merge open, and so is not source yet.

    Both markers, at the start of a line. A lone `<<<<<<<` is reachable in
    honest text — a diff quoted in a docstring, a fixture about conflicts, this
    very sentence — and answering yes to one would silence the checker for a
    file nothing is wrong with. A conflict always writes the pair, so requiring
    both costs nothing it was meant to catch.

    Unreadable is not conflicted. Whatever the reason, the checker is about to
    meet the same file and is the one that should say so.
    """
    try:
        lines = (
            Path(path_text).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return False
    return any(line.startswith("<<<<<<<") for line in lines) and any(
        line.startswith(">>>>>>>") for line in lines
    )


def import_lines(text: str) -> list[int]:
    """Every line an import statement of this source spans, or none unparsed."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []

    def spanned(node: ast.AST) -> range:
        match node:
            case ast.Import() | ast.ImportFrom():
                return range(node.lineno, (node.end_lineno or node.lineno) + 1)
        return range(0)

    return [line for node in ast.walk(tree) for line in spanned(node)]


def file_diagnostics(
    path_text: str,
    command: list[str],
    suffixes: tuple[str, ...] = (".py", ".pyi"),
    timeout_seconds: float = 20.0,
    pending_rules: tuple[str, ...] = (
        "reportUndefinedVariable",
        "reportMissingImports",
        "reportMissingModuleSource",
    ),
) -> dict[str, list[str]]:
    """Type-check one edited file, in the checkout that actually holds it.

    A language server the runtime starts is rooted once, where the session
    opened, and goes on resolving imports there after work moves to another
    checkout — same module names, different source, diagnostics about a file
    nobody edited. Running the checker per edit has no root to go stale:
    the file names its own checkout, and that is where the check runs.

    The checkout alone does not decide it. A checker finds the interpreter
    whose packages it resolves against by looking down ``PATH``, and a hook
    inherits whichever one the session was launched with, so a check running
    in one tree reads another tree's installed packages and calls every
    third-party import unresolvable. The checker's own directory goes first:
    that is the environment it was installed into, and therefore the one
    belonging to the checkout that holds the file. A checker the checkout
    does not hold has no such directory to prefer, and keeps the ``PATH`` it
    inherited — that is where the OS is about to find it.

    Reported for the edited file alone. The checker resolves whatever the
    file imports, so it can have opinions about the whole tree, and a hook
    that repeated them would answer every edit with the same backlog.

    Anything that goes wrong is no diagnostics. A checker that is missing, or
    slow, or writes something this cannot read, is not evidence about the
    edit — and this runs after the tool, so the alternative to saying
    nothing is failing an edit that already happened.

    *suffixes* is what the checker can read. A type checker handed a manifest,
    a document, or a lockfile parses it as source and reports the whole file
    as broken, so every edit to one answers with a wall of errors about lines
    the edit never touched — which teaches a reader to scroll past the output
    that exists to be read. It is a default rather than a constant because the
    checker is the caller's choice: a project whose checker reads more than
    Python says so instead of editing this.

    A file mid-merge is the same case arriving from the other direction. Its
    conflict markers are not source in any language, so the checker reports the
    file as broken from the first one onward and every line it names is about
    the merge rather than about the edit — during a resolution, which is
    exactly when a reader is editing that file and has the least attention to
    spare for a wall of output that cannot be acted on.

    A name used before it exists is reported as context rather than as a
    refusal: *pending_rules*, and an unknown symbol on an import line. A
    change spanning two edits — the use, then the definition or its import —
    reports it in between, and as a "blocking error" it would arrive dozens of
    times per change wherever several builders work in parallel. What is still
    unresolved when the change settles, `dev check --changed` reports.
    """
    nothing: dict[str, list[str]] = {"blocking": [], "context": []}
    if not command or Path(path_text).suffix.lower() not in suffixes:
        return nothing
    if conflicted(path_text):
        return nothing
    root = worktree_root(path_text)
    if not root:
        return nothing
    located = declared_program(root, command[0])
    if not located:
        return nothing
    edited = str(Path(path_text).resolve())
    environ = os.environ  # lup: ignore[os-environ] — the checker inherits this
    inherited = environ["PATH"] if "PATH" in environ else ""
    # Only a checker the checkout holds has an own directory to put first.
    # A bare name is about to be found on the PATH this inherits, so that
    # PATH is already the environment it belongs to.
    searched = (
        f"{Path(located).parent}{os.pathsep}{inherited}"
        if Path(located).is_absolute()
        else inherited
    )
    try:
        finished = subprocess.run(
            [located, *command[1:], edited],
            capture_output=True,
            text=True,
            cwd=root,
            env={**environ, "PATH": searched},
            timeout=hook_seconds_left(timeout_seconds),
            check=False,
        )
        reported = json.loads(finished.stdout)["generalDiagnostics"]
        imports = import_lines(Path(edited).read_text(encoding="utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return nothing
    shown = Path(edited).relative_to(Path(root).resolve())
    found = [
        item
        for item in reported
        if item["file"] == edited and item["severity"] != "information"
    ]

    def line(item: dict) -> str:
        return (
            f"{shown}:{item['range']['start']['line'] + 1}: "
            f"{item['severity']}: {item['message']}"
        )

    def pending(item: dict) -> bool:
        rule = item["rule"] if "rule" in item else ""
        return rule in pending_rules or (
            rule == "reportAttributeAccessIssue"
            and item["range"]["start"]["line"] + 1 in imports
        )

    awaited = [line(item) for item in found if pending(item)]
    return {
        "blocking": [line(item) for item in found if not pending(item)],
        "context": [
            "Named before it is supplied, which an edit still to come may do; "
            "`uv run lup-devtools dev check --changed` settles it:",
            *awaited,
        ]
        if awaited
        else [],
    }


def swept_files(
    paths: list[str],
    command: list[str],
    suffixes: tuple[str, ...] = (".py", ".pyi"),
    timeout_seconds: float = 30.0,
    refusing: tuple[str, ...] = ("missing", "spurious"),
) -> dict[str, dict]:
    """Sweep the written files: repair dead directives, keep what still refuses.

    A directive guarding nothing is the one audit finding whose fix is not a
    judgement — there is a single correct edit and this is it — so the gate
    ahead of the write neither refuses it nor spends an approval naming it,
    and it goes afterwards instead. That pairing is what lets the prompt leave
    it unmentioned: unlisted and removed is one behaviour, while unlisted and
    kept would be a directive nobody ever reads.

    Reported back rather than done in silence. The agent wrote the directive
    believing it did something, and a line that disappears without a word is
    one it writes again on the next file. What the file held before the sweep
    comes back too, so a caller holding a policy the sweep does not can put
    back a directive only that policy still needs.

    What the sweep still refuses in each file comes back beside it, in the
    kinds *refusing* names. The sweep is the whole-tree check scoped to these
    files — every rule, over every span it reads — so a verdict the gate ahead
    of the write cannot reach, a project rule or a string spanning lines, is
    reported per write rather than first met at the end.

    One run per checkout for all its files, since starting the sweep is most
    of what it costs. Anything that goes wrong is nothing swept, exactly as an
    unreadable checker is no diagnostics: this runs after the tool, so the
    alternative to saying nothing is failing a write that has already happened.
    """
    readable = [
        path
        for path in paths
        if command and Path(path).suffix.lower() in suffixes and not conflicted(path)
    ]
    roots = {path: worktree_root(path) for path in readable}
    by_root = {
        root: [path for path in readable if roots[path] == root]
        for root in dict.fromkeys(roots.values())
        if root
    }

    def swept_in(root: str, held: list[str]) -> dict[str, dict]:
        located = declared_program(root, command[0])
        if not located:
            return {}
        # Named the way the sweep names its own files, which is how the
        # request and the report come back in one spelling. It is also the
        # only spelling every sweep must understand: a project declares its
        # own program here, and one that selects by repository-relative prefix
        # is the shape this can count on rather than one it would have to
        # assume.
        base = Path(root).resolve()
        named = {
            str(Path(path).resolve().relative_to(base)): path
            for path in held
            if Path(path).resolve().is_relative_to(base)
        }
        if not named:
            return {}
        written = {name: text_at(base, name) for name in named}
        try:
            finished = subprocess.run(
                [
                    located,
                    *command[1:],
                    *(word for name in named for word in ("--path", name)),
                ],
                capture_output=True,
                text=True,
                cwd=root,
                timeout=hook_seconds_left(timeout_seconds),
                check=False,
            )
            reported = json.loads(finished.stdout)
            repaired = reported["repaired"]
            findings = reported["findings"] if "findings" in reported else []
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            return {}
        return {
            path: {
                "written": written[name],
                "repaired": [
                    f"line {item['line']}: removed `# lup: ignore"
                    + (f"[{item['rule_id']}]" if item["rule_id"] else "")
                    + "` — it guarded no rule, so it silenced nothing"
                    for item in repaired
                    if item["file"] == name
                ],
                "refused": [
                    {
                        "line": item["line"],
                        "rule_id": item["rule_id"],
                        "kind": item["kind"],
                        "message": item["message"],
                    }
                    for item in findings
                    if item["file"] == name and item["kind"] in refusing
                ],
            }
            for name, path in named.items()
        }

    return {
        path: swept
        for root, held in by_root.items()
        for path, swept in swept_in(root, held).items()
    }


def resolved_refutations(
    path_text: str,
    proposed: str,
    command: list[str],
    timeout_seconds: float = 30.0,
) -> dict[str, dict[str, list[int]]] | None:
    """What a checker refutes in the text about to be written, or None.

    The kernel decides from primitive rows and reads nothing, which is what
    keeps a verdict a pure function of its inputs. Resolving a receiver's
    declaration is not a decision — it is a fact about the machine, the same
    kind this half already resolves — so it is answered here and passed in.

    Two maps come back, each rule id to lines, under the names the kernel's
    resolution row gives them: ``refuted`` for the lines whose receiver
    resolved outside the rule's family, and ``unresolved`` for the lines the
    checker looked at and could type nothing for. They are kept apart because
    the gate treats them oppositely — a directive on a refuted line is dead,
    one on an unresolved line stands — and this half may name no kernel type,
    so the caller builds the row from the primitives.

    Run in the checkout holding the file, like every other checker this half
    starts, and handed the proposed text on stdin: the change is judged before
    it is written, so the copy on disk is the one being replaced. *path_text*
    still names where the content belongs, because imports and the module's
    own name resolve against it and against nothing else.

    None where no answer was had — no declared resolver, none installed, a
    crash, a timeout, output that will not decode, or no time left before the
    hook's deadline for one to answer in — and it has to stay distinct from an
    empty refutation. Empty means a checker looked and refuted nothing, which
    is evidence; None means nothing looked, which is the gate's cue to ask
    rather than refuse. Collapsing the two would turn every unresolvable
    session into a wall of confident denials.
    """
    allowed = hook_seconds_left(timeout_seconds)
    if not command or allowed < 1.0:
        return None
    root = worktree_root(path_text)
    if not root:
        return None
    located = declared_program(root, command[0])
    if not located:
        return None
    try:
        finished = subprocess.run(
            [located, *command[1:], "--path", str(Path(path_text).resolve())],
            capture_output=True,
            text=True,
            input=proposed,
            cwd=root,
            timeout=allowed,
            check=False,
        )
        reported = json.loads(finished.stdout)
        if not reported["resolved"]:
            return None
        return {
            verdict: {rule: list(lines) for rule, lines in reported[verdict].items()}
            for verdict in ("refuted", "unresolved")
        }
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def existing_write_targets(targets: list[str], root: Path | None = None) -> list[str]:
    """Report which of a command's write targets already exist on disk.

    The kernel never reads the filesystem, so it cannot tell creating a file
    from overwriting one. Resolving that here keeps the decision itself a
    pure function of the command text and this list.
    """
    where = Path.cwd() if root is None else root
    return [target for target in targets if (where / target).exists()]


def resolved_write_targets(
    targets: list[str],
    root: Path | None = None,
    # lup: ignore[dict-str-payload] — the keys are the caller's own write
    # targets, an open set, and this half compiles into a bare script that can
    # declare no row to carry them: the kernel's `DisplacedTargetRow` is what
    # the composition root turns these pairs into
) -> dict[str, str]:
    """Report which write targets resolve somewhere other than they spell.

    Every grant in the kernel reads a path lexically — a role names a tree,
    and a spelling sits under it or does not. A symlink is what breaks that
    step, and resolving it is filesystem work, so it happens here and crosses
    as a fact.

    ``lands`` is spelled in the vocabulary the kernel classifies in: relative
    to the checkout when the real path is inside it, absolute otherwise. That
    is what lets the kernel ask its own question — whether the role of where
    this lands is the role its spelling claimed — without resolving anything.

    A word carrying an expansion is skipped, because the path it names at run
    time is not the one standing here. Resolution covers a target that does
    not exist yet: the directories above it are what a symlink would sit in.
    """
    where = Path.cwd() if root is None else root
    # lup: ignore[dict-str-payload] — the write targets the caller named
    reported: dict[str, str] = {}
    for target in targets:
        if "$" in target:
            continue
        try:
            real = (where / target).resolve()
            spelled = (where / target).absolute()
        except OSError:
            continue
        if real == spelled:
            continue
        inside = real.is_relative_to(where)
        reported[target] = str(real.relative_to(where) if inside else real)
    return reported


def git_answers(
    arguments: list[str],
    root: Path,
    # lup: ignore[dict-str-payload] — variable names are an open set the caller
    # supplies, not an enumerable one this signature could name
    overrides: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout_seconds: float = 25.0,
) -> list[str] | None:
    """One Git invocation's lines, or None when Git cannot answer.

    Git missing, the path outside a repository, a malformed pathspec and a
    non-zero exit all collapse to None, so a caller reading this as evidence
    that something is safe to destroy treats an unanswerable question as a
    no. No answer inside ``timeout_seconds`` or the hook's deadline,
    whichever is nearer, is different: under a hook it ends the judgement
    (:func:`refuse_unanswered`), since a caller reading None as "not
    tracked" or "touches nothing" would let through what the answer would
    have asked about.

    ``overrides`` are merged over the inherited environment rather than
    replacing it, because a replacement drops ``PATH`` and ``HOME`` and the
    invocation then fails for a reason that has nothing to do with what it
    was asked. The one caller that passes any is the snapshot, which needs
    ``GIT_INDEX_FILE`` pointed somewhere other than the index a human is in
    the middle of composing.
    """
    environ = os.environ  # lup: ignore[os-environ]
    try:
        finished = subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            input=input_text,
            env={**environ, **overrides} if overrides else None,
            # Bounded by what the hook has left, and never read as a no when
            # the answer does not come in time.
            timeout=hook_seconds_left(timeout_seconds),
        )
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        refuse_unanswered("Git")
        return None
    return finished.stdout.splitlines() if finished.returncode == 0 else None


def undo_namespace() -> str:
    """Where snapshots live: a ref namespace of this project's own.

    Under ``refs/`` rather than in a stash so nothing a human does to their
    stash disturbs them, and outside ``refs/heads`` so no branch listing,
    push, or fetch treats them as work anybody meant to publish.

    A function rather than a constant because this half is spliced into the
    compiled dispatcher one function at a time, and a name beside them is
    dropped on the way in — read by the type checker, absent from the script.
    Being a function is also what makes it importable by the command that
    lists snapshots back, so the writer and the reader cannot end up looking
    in two different places for the same safety net.
    """
    return "refs/lup/undo"


def undo_retention_days() -> int:
    """How long a snapshot is worth keeping, absent a caller's own answer.

    How long ago a mistake is still worth undoing is a judgement about how
    somebody works rather than a fact about git, so it reaches its caller as a
    default they may differ on. Long enough to cover a week of sessions, short
    enough that a snapshot per mutating command does not accumulate without
    bound.

    A function for the reason :func:`undo_namespace` is one: the dispatcher
    splices this half in a function at a time, and the command that expires
    snapshots on request has to read the same number the dispatcher expires
    them by, or the two halves disagree about how long the net holds.
    """
    return 7


def undo_retention_count() -> int:
    """How many snapshots are worth keeping, absent a caller's own answer.

    A second bound because the window answers a different question. "How long
    ago is still worth undoing" is about how somebody works; this is about what
    the net is allowed to cost, and neither answers the other -- a burst
    session reaches a thousand snapshots inside the window, and a quiet
    fortnight holds three that are all past it.

    Measured on this checkout: a working day produces on the order of fifty
    distinct states, so a week inside the window lands near three hundred and
    this bound is the one that usually binds. That is deliberate. What the
    listing is *for* is finding the snapshot from before the thing that went
    wrong, and a reader who cannot see the entries cannot choose one -- so the
    cap is set where the list stays readable rather than where growth would
    become alarming.
    """
    return 200


def undo_expire(
    root: Path | None,
    keep_days: int = 0,
    namespace: str = "",
    now: datetime | None = None,
    keep_most: int = 0,
) -> list[str]:
    """Retire what outlived the window or sits past the cap; report what went.

    Two bounds, and a snapshot need only fail one of them. The window cannot
    hold a burst -- a session that touches a thousand states reaches them all
    inside a week -- and the cap cannot express that a fortnight-old snapshot
    has stopped being worth keeping when only three exist. Read together they
    bound the namespace by age *and* by size, which is what makes the listing
    finite whatever a session does.

    Selection reads the stamp the ref name already carries rather than asking
    git for each commit's date. The name was built to order the listing
    exactly, which makes it fixed-width, zero-padded and UTC -- so comparing
    it against a cutoff spelled the same way is the same judgement one string
    comparison later, and sorting by name is sorting by age. Both bounds come
    off one listing that costs a single call.

    Deleting the ref is the whole of it: the objects it held become
    unreachable and git's own housekeeping reclaims them.

    Silent about its own failure, exactly as its caller is. This runs in front
    of a command somebody asked for, and a repository that will not let a ref
    be deleted is not a reason to stop them.
    """
    if root is None:
        return []
    where = namespace or undo_namespace()
    taken = now or datetime.now(UTC)
    cutoff = taken - timedelta(days=keep_days or undo_retention_days())
    stamped = cutoff.strftime("%Y%m%dT%H%M%S%f")

    def outlived(ref: str) -> bool:
        """Whether this ref's own stamp sorts before the cutoff's."""
        # lup: ignore[string-split] — the ref name is this module's own
        # protocol, spelled `<stamp>-<tree>` by `undo_snapshot` below; git
        # ships no parser for a ref name, and the separator being read here is
        # the one that call chose
        stamp = ref.removeprefix(f"{where}/").split("-")[0]
        return len(stamp) == len(stamped) and stamp < stamped

    listed = sorted(
        git_answers(["for-each-ref", "--format=%(refname)", where], root) or []
    )
    # Taken off the front because the sort is by name and the name leads with
    # the stamp, so the oldest are exactly the ones the cap has no room for.
    limit = keep_most or undo_retention_count()
    surplus = {ref for ref in listed[: max(0, len(listed) - limit)]}
    retired = [ref for ref in listed if outlived(ref) or ref in surplus]
    for ref in retired:
        git_answers(["update-ref", "-d", ref], root)
    return retired


def undo_snapshot(
    root: Path | None,
    reason: str,
    session: str = "default",
    namespace: str = "",
) -> str:
    """Write the working tree into the object store before something destroys it.

    The whole argument for relaxing a permission lattice is that a mistake can
    be undone, so this is what has to exist before that relaxation is honest.
    Tracked content *and* untracked files, through a throwaway index rather
    than through ``git stash create``: measured, ``stash create`` captures only
    tracked files that were modified, so the file written thirty seconds ago
    and not yet added -- precisely what ``rm -rf src/`` destroys -- is absent
    from it exactly when it is reached for.

    Ignored files are not captured, and that is a stated limit rather than an
    oversight: measured on a checkout of this repository, ignored-but-precious
    content comes to 592 MB against a 21 MB object store, so capturing it would write
    twenty-eight times the repository's whole history before every mutating
    command. ``git clean -fdx`` therefore keeps asking, because it is the one
    command whose purpose is destroying what this cannot restore, and a
    credential belongs outside the checkout rather than inside one.

    Silent about its own failure, and deliberately. This runs in front of a
    command somebody asked for; a checkout mid-merge, a locked index, and a
    repository this process cannot write to are all reasons a snapshot cannot
    be taken, and none of them is a reason to stop the command. An empty
    string says no snapshot exists, which is what a caller needs to know and
    all it needs to know.

    In the compiled dispatcher rather than behind ``lup-devtools`` because of
    what it costs. Measured on a 2,634-file checkout of this repository: 81 ms
    for the first snapshot of a session, then a median of 11 ms warm and 14 ms
    with a file changed, against roughly a second of interpreter start for a
    subprocess that would do the same work. A safety net paid for on every
    mutating command has to cost what this costs, or it is the first thing
    somebody turns off -- and the warm figure is the one that matters, because
    the cold index is paid once and reused for the rest of the session.
    """
    if root is None:
        return ""
    directory = git_answers(["rev-parse", "--absolute-git-dir"], root)
    if not directory:
        return ""
    index = Path(f"{directory[0]}/lup-undo-{session}.index")
    # The session's index is written by the first snapshot and reused by every
    # one after it, so its absence is what "nobody has snapshotted in this
    # session yet" looks like -- already on disk, already being stat'd, and
    # true exactly once however long the session runs. Read before the index is
    # created and acted on after the ref is written, so the sweep counts the
    # snapshot it runs beside and the cap means the same number here as it does
    # to the command that offers it.
    cold = not index.exists()
    private = {"GIT_INDEX_FILE": str(index)}
    if git_answers(["add", "-A"], root, private) is None:
        return ""
    tree = git_answers(["write-tree"], root, private)
    if not tree:
        return ""
    commit = git_answers(["commit-tree", tree[0], "-m", f"lup undo: {reason}"], root)
    if not commit:
        return ""
    # The name carries both, and needs both. The stamp orders the listing
    # exactly -- git records a commit's date to the second, so two states
    # reached inside one second would otherwise tie, and a tie in a safety
    # net's "newest" is wrong at the worst possible moment. The tree hash is
    # what makes the state findable again: every earlier ref holding this
    # same tree is retired just below, so the listing carries one entry per
    # distinct state rather than one per command.
    held = tree[0][:12]
    where = namespace or undo_namespace()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    retired = [
        f"delete {stale}"
        for stale in (
            git_answers(["for-each-ref", "--format=%(refname)", where], root) or []
        )
        if stale.endswith(f"-{held}")
    ]
    reference = f"{where}/{stamp}-{held}"
    transaction = "\n".join(
        ["start", f"create {reference} {commit[0]}", *retired, "prepare", "commit", ""]
    )
    if (
        git_answers(
            ["-c", "core.fsync=reference", "update-ref", "--stdin"],
            root,
            input_text=transaction,
        )
        is None
    ):
        return ""
    if cold:
        undo_expire(root, namespace=where)
    return reference


def text_at(root: Path, target: str) -> str | None:
    """What stands at this path, or nothing where it does not read as text.

    One reading for both halves of every gate that judges a write by what it
    replaces. The encoding is named rather than taken from the locale, because
    the halves run in different processes — one inside a session's interpreter,
    one as the bare script a plugin ships — and a locale differing between them
    would make one file a document on one side and an unreadable one on the
    other, which is a disagreement no reader of either could detect.

    Nothing to read and nothing readable are one answer, because both leave the
    caller with no preimage to judge against and neither is a grant.
    """
    try:
        return (root / target).read_text(encoding="utf-8", newline="")
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def sed_output(
    scripts: list[str],
    options: list[str],
    text: str,
    timeout: float = 2.0,
    locale: str = "C.UTF-8",
) -> dict[Literal["text", "cause"], str | None]:
    """What an in-place sed leaves of one document, without touching any file.

    sed is run over the text and its output captured, which is the same
    computation ``-i`` performs and none of the writing: ``-i`` is exactly
    "run the script, then replace the file with the result". Over the text
    rather than the file, because the text is what a command line has left
    there so far, which a second rewrite of the same file reads. And under
    ``--sandbox``, so a script this was never meant to run -- one reading or
    writing another file, or running a command -- is refused by sed itself.

    Both keys always stand and exactly one is filled: ``text`` is the
    after-document, ``cause`` is what stopped one being produced -- ``refused``
    where sed would not run the script, ``unreadable`` where what came back
    is not text. The cause crosses as the word this half read rather than as
    the classifier's own literal, which this half may not name.

    Bounded by ``timeout`` or the hook's deadline, whichever is nearer: an
    answer that does not come in time is a refused rewrite, which the
    classifier asks about. The scripts are passed as ``-e`` expressions, so a
    script is never re-read as an option.

    Run under ``locale`` rather than whichever one started the runtime. What
    `.`, `[[:alpha:]]`, `\\U` and a bracket range match is the locale's to
    say, and the session's shell is where the real `-i` runs -- but no
    runtime hands a hook that shell's environment: Claude Code's payload and
    Codex's carry the call, its directory and its session, and the hook
    process inherits the runtime's own environment, which a profile the shell
    sources can have changed since. So the preview is pinned to the UTF-8
    locale glibc carries without generating one, the same answer on every
    machine and under every runtime, which matches any UTF-8 session for a
    script that leans on neither collation order nor a locale's own
    character classes.
    """
    expressions = [word for script in scripts for word in ("-e", script)]
    try:
        finished = subprocess.run(
            ["sed", "--sandbox", *options, *expressions],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=hook_seconds_left(timeout),
            check=False,
            # lup: ignore[os-environ] — inherited, not read: sed keeps the PATH
            # and everything else it runs with, and only the locale is set
            env={**os.environ, "LC_ALL": locale},
        )
        if finished.returncode:
            return {"text": None, "cause": "refused"}
        return {"text": finished.stdout.decode("utf-8"), "cause": None}
    except UnicodeError:
        return {"text": None, "cause": "unreadable"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"text": None, "cause": "refused"}


def document_at(root: Path, target: str) -> dict[Literal["text", "cause"], str | None]:
    """What stands at one path before a command line writes it, or why no text does.

    The reading a line's fold starts every file from: its text, or
    ``missing`` where nothing stands, ``directory`` or ``irregular`` where
    something that is not a file does, and ``unreadable`` where a file's
    bytes are not text.
    """
    landed = root / target
    if not landed.exists():
        return {"text": None, "cause": "missing"}
    if landed.is_dir():
        return {"text": None, "cause": "directory"}
    if not landed.is_file():
        return {"text": None, "cause": "irregular"}
    text = text_at(root, target)
    return {"text": text, "cause": None if text is not None else "unreadable"}


def resolved_path(root: Path, target: str) -> str:
    """The one name every spelling of a file shares: where it resolves from *root*."""
    return str((root / target).resolve())


def patched_documents(
    root: Path,
    patch: str,
    options: list[str],
    program: str,
    directory: str,
    current: Callable[[str], dict[Literal["text", "cause"], str | None]],
) -> list[dict[Literal["path", "after"], str | None]] | None:
    """What one patch leaves in each file it touches, applied to a copy of them.

    Git reads the patch -- which files it touches, and what applying it
    leaves -- in a scratch directory holding a copy of each of those files as
    ``current`` says the line has left it, so the checkout is never written.
    ``program`` is the command that applies it; `patch` is applied the way
    `git apply` does with the strip count it spelled, which is the only way
    it was stepped.

    ``None`` wherever the copy cannot say what the command would leave: a
    patch Git does not read or apply, a binary or a renaming one, a path
    outside the directory it applies in, a file that does not read as text.
    Each is a result only running shows.
    """
    environ = os.environ  # lup: ignore[os-environ]
    encoded = patch.encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="lup-patch-") as scratch:
        copy = Path(scratch) / "tree"
        copy.mkdir()
        # Nothing above the scratch directory is a repository to it, and no
        # repository a hook inherited is the one it applies to.
        environment = {
            **{
                name: value
                for name, value in environ.items()
                if name not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
            },
            "GIT_CEILING_DIRECTORIES": scratch,
        }

        def applied(arguments: list[str]) -> str | None:
            try:
                finished = subprocess.run(
                    ["git", "-c", "core.quotePath=false", "apply", *arguments, "-"],
                    cwd=str(copy),
                    input=encoded,
                    capture_output=True,
                    env=environment,
                    timeout=hook_seconds_left(5.0),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if finished.returncode:
                return None
            return finished.stdout.decode("utf-8", errors="replace")

        listing = applied(["--numstat", *options])
        if listing is None or program not in ("git", "patch"):
            return None
        rows = list(csv.reader(listing.splitlines(), delimiter="\t"))
        paths = [row[2] for row in rows if len(row) == 3 and row[0] != "-"]
        if len(paths) != len(rows) or any(
            "=>" in path or Path(path).is_absolute() or ".." in Path(path).parts
            for path in paths
        ):
            return None
        standing = {
            path: current(str(Path(directory, path)) if directory else path)
            for path in paths
        }
        # A file the patch touches that stands there and does not read as
        # text is one the copy cannot hold; one that is not there, the patch
        # may create.
        if any(
            reading["text"] is None and reading["cause"] != "missing"
            for reading in standing.values()
        ):
            return None
        for path, reading in standing.items():
            text = reading["text"]
            if text is not None:
                (copy / path).parent.mkdir(parents=True, exist_ok=True)
                (copy / path).write_text(text, encoding="utf-8", newline="")
        if applied(options) is None:
            return None
        return [
            {
                "path": resolved_path(root, str(Path(directory, path))),
                "after": text_at(copy, path) if (copy / path).is_file() else None,
            }
            for path in paths
        ]


def recoverable_write_targets(
    targets: list[str], root: Path | None = None
) -> list[str]:
    """Report which targets Git could restore byte for byte after a delete.

    Recoverable means tracked, carrying no uncommitted change, and a regular
    file: the object store then holds exactly what is on disk, so destroying
    it costs a checkout rather than any information.

    A directory is never reported, however clean everything beneath it is.
    One grant would otherwise cover a tree of unbounded size and depth, and
    the point of resolving this per path is that each grant stays the size of
    the thing named.
    """
    where = Path.cwd() if root is None else root
    return [
        target
        for target in targets
        if (where / target).is_file()
        and git_answers(["ls-files", "--error-unmatch", "--", target], where)
        is not None
        and git_answers(["status", "--porcelain", "--", target], where) == []
    ]


def ignored_write_targets(targets: list[str], root: Path | None = None) -> list[str]:
    """Report which targets Git ignores, which no undo snapshot holds.

    The snapshot takes what `git add -A` would, so an ignored path -- or one
    under an ignored directory -- is outside every capture this session has.
    A loss there is one nothing restores, and "captured and restorable" said
    of it is a sentence about a file no snapshot has ever seen. Git answers
    for the ignore rules it applies rather than a second reading of them
    here; a path Git tracks is never ignored, whatever a pattern says.

    Asked one path at a time, because Git refuses the whole question when one
    path in it lies outside the repository -- and a path out there is no
    capture's to hold anyway, which the write's own scope already says. A path
    Git cannot answer for is not reported, which leaves the capture's own
    evidence to decide: a checkout Git cannot read took no snapshot either.
    """
    where = Path.cwd() if root is None else root
    return [
        target
        for target in targets
        if git_answers(["check-ignore", "-q", "--", target], where) is not None
    ]


def committed_text(path_text: str, root: Path | None = None) -> str | None:
    """What the last commit holds at this path, or None where Git cannot say.

    The "before" half of reviewing a write after it happened. An edit hands
    both halves to the gates because it has both; a shell command that already
    ran leaves only the result on disk, so the prior content has to come from
    somewhere, and the last commit is the reading a reviewer would compare
    against anyway.

    Uncommitted work already in the file is therefore invisible to the
    comparison, which overstates the change rather than understating it. That
    is the right direction for a report: it can name a line the command did
    not write, and cannot miss one it did.
    """
    where = Path.cwd() if root is None else root
    lines = git_answers(["show", f"HEAD:{path_text}"], where)
    return None if lines is None else "\n".join(lines)


def patch_write_targets(patches: list[str], root: Path | None = None) -> list[str]:
    """The paths these patches would write, as Git itself reads them.

    A patch names its targets in a format with exactly one authoritative
    reader, and that reader is already installed: ``--numstat`` parses the
    patch and reports what it touches without applying a byte of it. Reaching
    for it is the alternative to growing a unified-diff parser inside a hook,
    which would be a second reader to keep in step with the one that decides.

    Anything Git declines to read yields nothing. A path that is not a patch,
    a patch against files that are not there, a missing file -- each exits
    non-zero and collapses to ``None``, so a word swept up by mistake
    contributes no target rather than a target nobody writes.

    The report is tab-separated: added lines, deleted lines, path. It is read
    with the reader for that format rather than split, and ``core.quotePath``
    is turned off so a path outside ASCII arrives as itself instead of as an
    escaped rendering no reader here would undo. A path holding a tab or a
    newline is quoted by Git whatever that setting says, and drops out by
    failing to name a file -- which loses a review rather than inventing one.

    A rename reports the destination, which is the file that ends up written
    and so the one worth reading afterwards.
    """
    where = Path.cwd() if root is None else root
    return [
        columns[2]
        for patch in patches
        for report in [
            git_answers(
                ["-c", "core.quotePath=false", "apply", "--numstat", "--", patch], where
            )
        ]
        if report is not None
        for columns in csv.reader(report, delimiter="\t")
        if len(columns) == 3 and columns[2]
    ]


def tracked_write_targets(targets: list[str], root: Path | None = None) -> list[str]:
    """Report which targets Git holds, so a reviewer could diff a change to one.

    The weaker half of :func:`recoverable_write_targets`, and a different
    question rather than a cheaper one. That asks whether the object store
    could put the bytes back, so it wants the path clean as well as tracked;
    this asks whether replacing the content would bypass review, and a file
    carrying uncommitted work is *more* reviewable rather than less.

    Existing on disk is not required either. A tracked path somebody deleted
    is still a path a diff would show.
    """
    where = Path.cwd() if root is None else root
    return [
        target
        for target in targets
        if git_answers(["ls-files", "--error-unmatch", "--", target], where) is not None
    ]


def unleased_write_targets(
    targets: list[str], measured: dict[str, list[str]], root: Path | None = None
) -> list[str]:
    """Report which targets fall outside what this launch mounted writable.

    The lease is a snapshot. A contained launch enumerates its siblings when
    the container starts and punches a read-only overlay over each; a worktree
    cut afterwards gets the writable base with no overlay, and the mount
    namespace is fixed by then, so nothing can be remounted to cover it. The
    judgement is what is left, which is the arrangement the lease already
    relies on elsewhere.

    Nothing is reported where no boundary was measured. A session that could
    not read its own lease knows of no writable roots at all, and reporting
    every target as unleased would put a question in front of every write in
    the checkout -- which teaches nobody anything and buries the real ones.
    """
    leased = measured["writable_roots"] if "writable_roots" in measured else []
    if not leased:
        return []
    where = Path.cwd() if root is None else root
    granted = [granted_root(root_path) for root_path in leased]
    return [
        target
        for target in targets
        for resolved in [(where / target).resolve()]
        if not any(resolved.is_relative_to(root_path) for root_path in granted)
    ]


def lent_mount_points(mountinfo: str) -> list[str]:
    """Every mount point here whose filesystem is a slice the host lent.

    Read off a mount table in the ``/proc/<pid>/mountinfo`` format, one
    space-separated record per mount. A mount whose root inside its own
    filesystem is not that filesystem's top is a bind: a directory, a volume
    or a single file handed in from somewhere else -- the checkout, a cache
    volume, the credential seed, the wake sockets. A filesystem mounted whole --
    the image's own root, ``proc``, a ``tmpfs`` the container made -- is the
    container's. The one whole filesystem a launch lends is a lease root at a
    disk's top, which the lease names.

    The record's fields are named where they are read: proc(5) fixes the
    first five as the mount's id, its parent's, the device, the root within
    that device's filesystem, and the mount point. A record too short to carry
    them is not a mount and is skipped.
    """

    def lent(record: list[str]) -> list[str]:
        match record:
            case [_mount_id, _parent_id, _device, root, mount_point, *_] if root != "/":
                return [unescaped_mount_point(mount_point)]
            case _:
                return []

    return [
        point
        for record in csv.reader(mountinfo.splitlines(), delimiter=" ")
        for point in lent(record)
    ]


def unescaped_mount_point(field: str) -> str:
    """One ``mountinfo`` mount point as the path it names.

    The table escapes a space, a tab, a newline and a backslash as three
    octal digits and leaves every other byte as UTF-8.
    """
    raw = field.encode("utf-8").decode("unicode_escape")
    return raw.encode("latin-1").decode("utf-8", "replace")


def host_shared_roots(
    measured: dict[str, list[str]], mounted: list[str], home: str
) -> list[str]:
    """Every root a path the host can see lies under, as this launch knows them.

    The lease's writable and read-only roots -- a home-relative grant expanded
    against ``home``, the way the host's own tools will read it -- and every
    mount point the mount table says the host lent. The machine's temporary
    root is the one lease entry left out: a container's `/tmp` is its own and
    goes with it, which is what the settlement already reads it as.
    """
    leased = [
        *(measured["writable_roots"] if "writable_roots" in measured else []),
        *(measured["read_only_roots"] if "read_only_roots" in measured else []),
    ]
    expanded = [
        home + scope[1:] if scope == "~" or scope.startswith("~/") else scope
        for scope in leased
    ]
    return [
        *(scope for scope in expanded if Path(scope) != Path("/tmp")),
        *mounted,
    ]


def landed_targets(
    targets: list[str],
    shared: list[str],
    root: Path | None = None,
    siblings: list[str] | None = None,
) -> list[list[str]]:
    """Place each target: this checkout, somewhere else the host shares, or the container.

    Pairs of ``[path, landing]``, one per target, in the order given. Resolved
    against ``root``, the session's own checkout, so a link is followed to
    where it lands. A target no one can read -- an expansion, a substitution,
    a directory a `cd` left unknown -- lands ``host``: nothing here can vouch
    for where it goes, and that is the answer that keeps a question.

    Another worktree of this repository in ``siblings`` is the checkout too:
    the same project on another branch, which a session is sent to work in by
    absolute path, rather than a tree somebody else lent the container.
    """
    where = Path.cwd() if root is None else root
    checkouts = [where.resolve(), *(Path(tree).resolve() for tree in siblings or [])]

    def landing(target: str) -> str:
        if "$" in target or "`" in target:
            return "host"
        resolved = (where / target).resolve()
        if any(resolved.is_relative_to(checkout) for checkout in checkouts):
            return "checkout"
        if any(resolved.is_relative_to(scope) for scope in shared):
            return "host"
        return "container"

    return [[target, landing(target)] for target in targets]


def host_held_ports(proc: Path) -> list[int]:
    """The ports a process this container cannot see is listening on.

    A container sharing the host's network shares its loopback, so a port
    there may be the operator's own service. The socket tables under ``proc``
    list every listener in that network, one space-padded record per socket;
    the descriptors under each visible process name the sockets this
    container's processes hold. A listener whose socket no visible process
    holds is the host's, and its port is one of these.

    Every port where the tables cannot be read: the reading that keeps the
    question.

    A record's fields are named where they are read, in the order proc(5)
    fixes: slot, local address, remote address, state -- ``0A`` is listening
    -- the queues, the timer, retransmits, uid, timeout, and the socket inode.
    """

    # A descriptor can close between listing a process's table and reading
    # one entry -- the listing's own, in this process -- so each is read
    # alone, and one that went takes only itself with it.
    def link(entry: Path) -> list[str]:
        try:
            return [str(entry.readlink())]
        except OSError:
            return []

    def links(descriptors: Path) -> list[str]:
        try:
            entries = list(descriptors.iterdir())
        except OSError:
            return []
        return [target for entry in entries for target in link(entry)]

    try:
        tables = [
            (proc / "net" / name).read_text().splitlines()[1:]
            for name in ("tcp", "tcp6")
            if (proc / "net" / name).exists()
        ]
    except OSError:
        return list(range(1, 65536))
    held = {
        link.removeprefix("socket:[").removesuffix("]")
        for descriptors in proc.glob("[0-9]*/fd")
        for link in links(descriptors)
        if link.startswith("socket:[")
    }

    def unheld(record: list[str]) -> list[int]:
        match record:
            case [
                _slot,
                local,
                _remote,
                "0A",
                _queues,
                _timer,
                _tries,
                _uid,
                _wait,
                inode,
                *_,
            ] if inode not in held:
                # lup: ignore[string-split] — the table's `address:port` field, hex on both sides, which no parser reads
                _address, _, bound = local.rpartition(":")
                return [int(bound, 16)]
            case _:
                return []

    return sorted(
        {
            port
            for table in tables
            for record in csv.reader(
                [line.strip() for line in table], delimiter=" ", skipinitialspace=True
            )
            for port in unheld(record)
        }
    )


def readonly_write_targets(
    targets: list[str], measured: dict[str, list[str]], root: Path | None = None
) -> list[str]:
    """Report which targets land inside a read-only hole this launch measured.

    The other half of :func:`unleased_write_targets`, and the one a path verb
    needs. That reports a target outside every writable root; this reports one
    that sits inside a writable root but under a read-only region punched into
    it -- a mounted bare clone's `config` and `hooks/`, whose contents run on
    the host. The container binds those read-only, so every verb meets an
    EROFS there; on the host nothing did, because a redirection's content
    reached :func:`execution_write_refusal` while a `cp`, `mv` or `ln` that
    places the same bytes reached only the loss gate, which a capture of the
    session's own checkout wrongly discharged.

    The deepest enclosing scope decides, exactly as `execution_write_refusal`
    and the mount table read the same table: a writable subtree inside a
    read-only region stays writable, and a read-only hole inside the writable
    base is what this returns. Nothing is reported where no read-only region
    was measured, for the reason the sibling gives about an unmeasured lease.
    """
    writable = measured["writable_roots"] if "writable_roots" in measured else []
    readonly = measured["read_only_roots"] if "read_only_roots" in measured else []
    if not readonly:
        return []
    where = Path.cwd() if root is None else root
    found: list[str] = []  # lup: ignore[empty-collection] — filtered append below
    for target in targets:
        resolved = (where / target).resolve()
        matches = [
            (len(granted.parts), allowed)
            for scopes, allowed in ((writable, True), (readonly, False))
            for scope in scopes
            for granted in [granted_root(scope)]
            if resolved.is_relative_to(granted)
        ]
        deepest = max((depth for depth, _ in matches), default=0)
        if matches and not any(
            allowed for depth, allowed in matches if depth == deepest
        ):
            found.append(target)
    return found


def empty_directory_targets(targets: list[str], root: Path | None = None) -> list[str]:
    """Report which targets are directories with nothing in them.

    An archive unpacked into one replaces nothing, whatever the archive
    holds — which is the only way to answer that without reading the archive
    itself. A path that is absent, a file, or unreadable is not reported, so
    an unanswerable question reads as "something is already there".
    """
    where = Path.cwd() if root is None else root
    found: list[str] = []
    for target in targets:
        path = where / target
        if not path.is_dir():
            continue
        try:
            if not any(path.iterdir()):
                found.append(target)
        except OSError:
            continue
    return found


def directory_write_targets(targets: list[str], root: Path | None = None) -> list[str]:
    """Report which of a command's targets are directories on disk.

    A refusal that can name this says which way out is open — remove the
    files it holds — rather than leaving the agent to guess why a delete it
    expected to pass did not.
    """
    where = Path.cwd() if root is None else root
    return [target for target in targets if (where / target).is_dir()]


def read_document(path_text: str) -> str | None:
    """Read a path's current text, or None when nothing is there yet."""
    path = Path(path_text)
    return path.read_text(encoding="utf-8") if path.exists() else None


def declared_identity(identity_env: str) -> str:
    """The identity this session's launcher declared, if it declared one.

    A hook payload need not carry an agent identity at all, so the
    environment is the only channel every launcher is guaranteed to have.
    """
    environ = os.environ  # lup: ignore[os-environ]
    return environ[identity_env] if identity_env in environ else ""


def document_allowances(document_text: str, known: list[str]) -> list[str]:
    """Edit gates one document grants, as it reads at this instant.

    Read here rather than carried in, because a grant is answered while the
    session it answers is already running: a list resolved when the process
    started can neither gain the gate a human just approved nor lose one they
    took back. The document is named from outside and its contents are read
    afresh, so the current state governs in both directions.

    Only names in the compiled vocabulary count. The document is a transport,
    not an authority: a name no launcher can legitimately publish — a typo, or
    a gate this policy never grants this way — is dropped rather than
    honoured, so hand-writing one buys nothing.

    Nothing to read is no grant, and so is anything unreadable: a missing
    document, a directory, a half-written replacement, a payload that is not a
    list of names. Every one of them leaves the gate exactly where it was, so
    the failure is a refusal a human can answer rather than a grant nobody
    made.
    """
    if not document_text:
        return []
    try:
        declared = json.loads(Path(document_text).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(declared, list):
        return []
    return [str(name) for name in declared if str(name) in known]


def granted_allowances(grants_env: str, known: list[str]) -> list[str]:
    """Edit gates a human approved for the lease this session is working.

    The environment names the document rather than carrying the grants, so
    what a hook process inherits at launch is a place to look and never an
    answer that has since moved on.
    """
    environ = os.environ  # lup: ignore[os-environ]
    return document_allowances(
        environ[grants_env] if grants_env in environ else "", known
    )


def peer_store(root: Path | None, store: list[str]) -> Path | None:
    """Where this repository's sessions meet, or nothing outside a repository.

    The parts arrive as data rather than spelled here, because the directory
    belongs to whichever module put a roster in it. A dispatcher compiled for
    a project whose sessions never coordinate carries this same code and is
    handed nothing to read, which is the difference between a capability a
    project declined and a path this half decided for it.

    The shared git directory rather than a worktree, so every checkout of one
    clone resolves the same roster and a branch cannot change what a peer
    reads.
    """
    if root is None:
        return None
    shared = shared_git_directory(str(root))
    if not shared:
        return None
    return Path(shared).joinpath(*store)


def writable_snapshot(root: Path) -> dict:
    """What every file Git reports as moved looks like right now, by size and time.

    Two questions asked as two invocations that each answer in one path per
    line, rather than one status report this half would have to take apart:
    what differs from the commit, and what is not tracked at all. Between them
    they cover every path a command could write that anybody could later
    attribute.

    Size and modification time rather than a digest. A digest of every moved
    file before every shell call is paid on the whole working set, where these
    two are a stat each — and what the snapshot is for is telling *which* files
    moved, after which digesting the few that did is cheap.
    """
    moved = git_answers(["diff", "--name-only", "HEAD"], root) or []
    fresh = git_answers(["ls-files", "--others", "--exclude-standard"], root) or []
    return {
        path: [stat.st_size, stat.st_mtime_ns]
        for path in dict.fromkeys([*moved, *fresh])
        for stat in [file_stat(root / path)]
        if stat is not None
    }


def file_stat(path: Path):
    """One file's stat, or nothing where it cannot be read.

    A path Git reported and the filesystem will not stat is a file deleted
    between the two calls, which is a race rather than a failure: it simply
    does not appear in this snapshot, and the comparison treats it the way it
    treats anything else absent from one side.
    """
    try:
        return path.stat()
    except OSError:
        return None


def open_claim_window(
    root: Path | None, store: list[str], windows_dir: str, mine: str
) -> None:
    """Record what this session's tree looked like before a command ran.

    This session's own file and nobody else's reader: what it holds is the
    before half of a comparison only the process that opened it can close. A
    command that names no target leaves this as the one evidence of what it
    wrote, which is what the claim on this member's file is then taken from.
    """
    directory = peer_store(root, store)
    if directory is None or not mine or root is None:
        return
    windows = directory / windows_dir
    try:
        windows.mkdir(parents=True, exist_ok=True)
        (windows / f"{mine}.json").write_text(
            json.dumps(
                {
                    "root": str(root),
                    "at": datetime.now(UTC).timestamp(),
                    "entries": writable_snapshot(root),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        return


def close_claim_window(
    root: Path | None,
    store: list[str],
    windows_dir: str,
    mine: str,
) -> dict:
    """What changed while this session's command ran.

    ``paths`` are the files whose size or modification time differs from the
    snapshot, plus the ones that were not in it at all.

    Nobody else's window is read. A change this comparison cannot attribute is
    one two sessions were both positioned to have made, and each of them
    records a claim on its own file — so the contest is what a reader derives
    from meeting them, rather than a list of suspects one of them guessed at
    while it could not see who wrote.
    """
    empty = {"paths": []}
    directory = peer_store(root, store)
    if directory is None or not mine or root is None:
        return empty
    opened = directory / windows_dir / f"{mine}.json"
    try:
        before = json.loads(opened.read_text(encoding="utf-8"))
        opened.unlink()
    except (OSError, ValueError):
        return empty
    if not isinstance(before, dict) or "entries" not in before:
        return empty
    entries = before["entries"]
    after = writable_snapshot(root)
    return {
        "paths": sorted(
            str(root / path)
            for path, stamp in after.items()
            if path not in entries or entries[path] != stamp
        )
    }


def document_digest(text: str | None) -> str | None:
    """Bind captured attribution to exact UTF-8 content, preserving absence."""
    return sha256(text.encode()).hexdigest() if text is not None else None


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
    forms would leave ``rm f`` granted while ``echo x > f`` asks about the
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
    # Every fact Git answers is asked here, before the edit gates below: a
    # gate may start a type checker that spends what is left of the hook's
    # deadline, and a Git question asked with nothing left reads as no answer
    # -- no other checkout, nothing tracked -- so a heredoc into a sibling
    # worktree's `tmp/` would read as an outside path, and a redirect over
    # tracked source beside it as a file Git never held.
    #
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
    tracked = tracked_write_targets(
        [*shell_write_targets(command), *acted_on, *flagged], cwd
    )
    recoverable = recoverable_write_targets(
        [*shell_write_targets(command), *acted_on], cwd
    )
    # A snapshot proves a capture only of what it took, and it takes nothing
    # Git ignores: one ignored target outside declared scratch, which needs no
    # capture, leaves the loss uncaptured.
    recovered = bool(reference) and not ignored_write_targets(
        unscratched(
            [
                *shell_write_targets(command),
                *shell_written_targets(command, SHELL_RULES),
            ],
            PATH_ROLES,
            str(cwd or Path.cwd()),
        ),
        cwd,
    )
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
        plugin_roots=GENERATED_PLUGIN_ROOTS,
        existing_targets=existing_write_targets(
            [*shell_write_targets(command), *acted_on, *flagged], cwd
        ),
        tracked_targets=tracked,
        recoverable_targets=recoverable,
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
                    shell_posture_targets(command, SHELL_RULES),
                    boundary,
                    cwd,
                    siblings,
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
        recovered=recovered,
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
            stated(verdict.subject, verdict.reason),
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
    waiting: Callable[[list[str]], tuple[Step, ...]],
    execution_id: str = "",
    stage: str = "",
    predecessor: str = "",
    execution_payload: dict | None = None,
    policy_identity: str = "",
    provider: str = "",
    agent: str = "",
    account: list[Said] | None = None,
) -> Reviewed:
    """Park one ask for the operator, or spend the single-use answer they recorded.

    The call is refused while it waits, with a recovery written for the agent:
    it is queued rather than refused, the call is not to be reshaped, and
    ``waiting`` spells, in the runtime's own words, how the conversation that
    asked hears the answer, and when to run the `review wait` that carries
    the approved call out and reports the result. Every review command it
    names runs in :func:`review_home`, with that checkout's code.

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
    home = review_home(cwd)
    result = review_hook_call(
        cwd,
        session,
        tool,
        json.dumps(arguments, sort_keys=True),
        json.dumps(
            {str(path): before for path, before in bound.items()},
            sort_keys=True,
        ),
        stated(decision.subject, decision.reason),
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
                home / ".lup/questions.jsonl", review_answers_home(REVIEW_ANSWERS_ENV)
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
                    step(
                        f"the operator declined review {identifier}{note}: don't"
                        " retry this call as it stands"
                    ),
                    step("change course, or ask the user"),
                ),
            ),
            "notice": f"Lup review {identifier} was declined; the agent is told.",
        }
    if not identifier:
        unavailable = f"the review queue is unavailable: {result['reason']}"
        return {
            "decision": decision.revised(
                effect="deny",
                recovery=(step(f"{unavailable}; run this from an operator terminal"),),
            ),
            "notice": f"Lup: {unavailable}. Run this operation from an operator terminal.",
        }
    project = declared_identity(POLICY_ROOT_ENV)
    prefix = [
        "uv",
        "run",
        # The checkout holding the review queue, so the line runs from
        # anywhere with that checkout's code, which is the code that parked
        # it; a project declared apart from it selects the application's CLI.
        "--directory",
        str(home),
        *(
            ["--project", project]
            if project and Path(project).resolve() != home.resolve()
            else []
        ),
        "lup-devtools",
        "review",
    ]
    dashboard = declared_identity(DASHBOARD_URL_ENV)
    answers = (
        (step(f"the operator answers it on the dashboard, {dashboard}"),)
        if dashboard
        else (
            step(
                "the operator answers it from a terminal outside the session",
                [*prefix, "approve", identifier, "--as", "operator"],
            ),
            step(
                "or declines it", [*prefix, "decline", identifier, "--as", "operator"]
            ),
        )
    )
    waiting_here = waiting_edits(cwd, session, agent)
    together = (
        (
            step(
                f"{waiting_here} of your edits now wait on the operator one review"
                " at a time. Where changes belong together, write each file as it"
                " should end up under one directory in the tmp/ of the checkout"
                " they change, mirroring that checkout, and propose them as one"
                " review, which the operator answers all at once",
                [
                    *prefix,
                    "propose",
                    "<that directory, absolute>",
                    "--why",
                    "<what they change and why>",
                ],
            ),
            step(
                "write --why and each file's note in plain words, as you would"
                " tell a colleague at their desk",
                [*prefix, "propose", "--help"],
            ),
        )
        if waiting_here >= 2
        else ()
    )
    where = (
        f"on the dashboard, {dashboard}"
        if dashboard
        else "from a terminal outside the session"
    )
    return {
        "decision": decision.revised(
            effect="deny",
            queued=identifier,
            recovery=(
                step(
                    f"it waits on the operator as review {identifier}, not"
                    " refused: don't change the command"
                ),
                *waiting([*prefix, "wait", identifier]),
                *answers,
                *together,
            ),
        ),
        "notice": f"Lup review {identifier} is waiting for you {where}.",
    }


def roster_doing(directory: Path | None, member: str, agent: str) -> list[Said]:
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
    return [Said(source="doing", text=doing)] if doing.strip() else []


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
    tool: str, name: str, description: str, values: list[str], field: str
) -> KernelDecision:
    """Judge one native spawn: refused if this project refuses it, else by its name.

    ``tool`` is the runtime's name for the spawn, which the declared
    refusals are matched against. ``name`` is the runtime's own field for the
    subagent's name and ``description`` the text a name is read from where
    none was given, each read by the host half that knows which key that is —
    a runtime whose spawn carries no description passes ``""``. ``field`` is
    the name's key, so the refusal can name the argument, and ``""`` where
    the spawn takes no name; every string the call carries rides beside them
    so an escalation marker in any of them is found.
    """
    return decide_spawn(
        name, description, values, SPAWN_NAMES, field, tool=tool, refused=REFUSED_TOOLS
    )


def spawn_named(name: str, description: str) -> str:
    """The name this project sends a spawn out under, the one the verdict judged.

    What a host half writes back into the call where it differs from what
    was given, so the rewrite and the verdict cannot come to disagree.
    """
    return spawn_name(name, description, SPAWN_NAMES)


def spawn_notice_report(
    name: str,
    description: str,
    field: str,
    cwd: Path | None,
    session: str,
    caller: store.Caller,
) -> PostToolReport:
    """What a finished spawn's caller is told about its name, the first time in its conversation.

    The notice teaches a habit rather than correcting one call, so once is
    what it is worth. Kept per conversation rather than per session, because
    a subagent spawning one of its own never read what its session was told.
    Nothing is noted for a spawn the notice is silent about, so a caller who
    names its spawns never touches the ledger.
    """
    notice = spawn_notice(name, description, SPAWN_NAMES, field)
    said = (
        bool(notice)
        and bool(session)
        and cwd is not None
        and noted_once(cwd, store.acting_id(session, caller), "spawn names")
    )
    return PostToolReport(blocking=[], context=[notice] if notice and not said else [])


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
        return routing_failure(str(error), path)
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
        and awaits_resolution(
            before, after, rows, python_source, worktree_path(path_text), PATH_ROLES
        )
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
        plugin_roots=GENERATED_PLUGIN_ROOTS,
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
    because a shell write is answered by its path alone -- the command
    produces its output by running, so before the fact there is nothing to
    read -- and that premise holds for `dev render > docs/api.md` and fails
    for `cat > f <<'EOF'`, where the bytes are in the command. Where they are,
    they go to the same `edit_decision` an `Edit` is put to, at the moment
    that can still change the answer.

    Why it matters: a redirection declares its route reviewed, which is what
    lets the write row allow an overwrite of tracked source. For a route
    nothing can read that is the honest trade. For this one it would be a
    hole -- `cat > packages/lup/src/lup/seams.py <<'EOF'` replacing a tracked
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

    That set leaves out what a command carries, and leaves it out here rather
    than only in the telling. A command that carries its own bytes is put to the same
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
    sources move after the launch -- after a rename, the gate can demand a
    `# lup: ignore[seam-boundary]` the sweep then deletes as dead, and every
    later edit to the file is refused for the missing directive. So the
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
            f"{shown}: left as written. The sweep called its directives dead by"
            " this checkout's rules, and the policy this session loaded still"
            " needs one of them; the two agree again once"
            f" `{spelled(devtools('harness', 'generate', 'all'))}` runs and the"
            " session restarts. What the loaded policy said about the repair:",
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
    every edit, one agent reads it about 150 times in a session, which is the noise
    this project's own "say it once" refuses. So the verdict stands on every
    edit and its recovery goes with the first (:func:`noted_once`).
    """
    if verdict.rule != "edit:foreign-repository" or not session or cwd is None:
        return verdict
    repository = worktree_root(str((cwd / path_text).resolve())) or path_text
    if noted_once(cwd, session, repository):
        return verdict.revised(recovery=())
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


def family_hold_report(
    paths: list[str], cwd: Path | None, caller: store.Caller
) -> PostToolReport:
    """What the writer is told of its own descendants' holds over the files it wrote.

    A member writes what a subagent it spawned holds without being asked —
    the edit gate let it through — and is told so with the call's result,
    one line a hold: whose it is, and whether that one is still running. The
    same words on both runtimes, in the post-tool context each adds beside
    the result.
    """
    directory = peer_directory(cwd)
    if PEER_POLICY is None or directory is None:
        return PostToolReport(blocking=[], context=[])
    mine = store.acting_id(answering_member(directory), caller)
    return PostToolReport(
        blocking=[],
        context=[
            rendered_said(
                diagnostic(
                    "warning",
                    note,
                    what=worktree_path(str(Path(path).resolve())),
                    steps=(
                        step(
                            "tell it what you wrote, before it writes over your change"
                        ),
                    ),
                )
            )
            for path in paths
            for note in store.family_holds(directory, path, mine)
        ],
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
            step(
                "carry on with other work, and hold this in the background"
                " (run_in_background, with the longest timeout the tool takes,"
                " 7200000 ms): nothing else wakes a subagent, and it wakes you"
                " with the result",
                [*command, "--timeout", "7140"],
            ),
            step(
                "if it ends with the review still waiting, start it again"
                " quietly, reporting that to nobody"
            ),
        )
    if declared_identity("CLAUDE_CODE_ENTRYPOINT") == "cli":
        return (
            step(
                "carry on with other work, or end your turn: the operator's answer"
                " wakes this session, so don't start a waiter"
            ),
            step("once it wakes you, carry the call out at once", command),
        )
    return (
        step(
            "carry on with other work; this run ends with its last turn and"
            " nothing wakes it after, so once nothing else is left, run this in"
            " the foreground (the longest timeout the tool takes there, 600000"
            " ms), again each time it ends still waiting",
            [*command, "--timeout", "540"],
        ),
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
        # A spawn is judged by whether the project refuses spawning, and then
        # by the one thing that makes its subagent legible and addressable:
        # the name it goes out under, read out of the description every spawn
        # here carries where none was given.
        return spawn_decision(
            name,
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
    beside = settled.beside() if settled.effect == "ask" else ""
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
            settled.addressed() if settled.effect == "deny" else settled.headline()
        ),
    }

    def surfaced(result):
        """The same verdict, with what this runtime will not show it said."""
        message = announced(settled.effect, payload["tool_name"], settled.headline())
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
        "deny", unjudged_reason(error, read), recovery=unjudged_recovery(ran_out(error))
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


if __name__ == "__main__":
    answered_in_time(HOOK_ANSWER_SECONDS, judged, unanswered)
