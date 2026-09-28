# Generated from lup.policy.assets.policy_evaluator by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
from kernel.policy_protocol import decision_wire, read_edit_request
from policy_data import AUTONOMOUS_AGENT_IDENTITIES
import csv
import fcntl
import os
import signal
from hashlib import sha256
import time
from datetime import UTC, datetime, timedelta

# lup: ignore[subprocess] — `sh` is third-party and this half is compiled into a bare script that has no virtual environment to resolve it from
import subprocess
from typing import Literal
from urllib.parse import urlsplit
import shlex
import policy_data as identity_policy
from kernel.decision import KernelDecision
from kernel.policy_protocol import read_response, routing_failure
from coordination import store
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
    RewriteReading,
    RewrittenDocumentRow,
    UnproducedDocumentRow,
    landing_rows,
    unproduced_cause,
)
from kernel.spawns import decide_spawn
from kernel.words import INTERPRETERS
from kernel.roles import displaced_targets
from kernel.shell import decide_shell, sandbox_excluded, shell_posture_targets
from kernel.tools import decide_tool
from kernel.withheld import withheld_edit
from policy_data import (
    ACCEPTANCE_GUARD,
    ALLOWANCE_GRANTS_ENV,
    ALLOWED_FETCH_SCOPES,
    ANTI_PATTERN_ROWS,
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
    named `<cwd>/~/.cache/uv`, so the one grant every toolchain needs was
    refused as outside the boundary it was declared into, while `touch` on the
    same path, which no reader resolved, went through.
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
        return f"{path} is outside this launch's writable boundary"
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
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> list[list[str]]:
    """Where each target lands, as this launch's lease and this mount table say.

    Asked by a caller that already knows the session runs in a container:
    the question these answer is asked only there. A mount table nobody
    can read places every target on the host, which keeps every question.
    """
    try:
        lent = lent_mount_points(mountinfo.read_text())
    except OSError:
        return [[target, "host"] for target in targets]
    shared = host_shared_roots(measured, lent, str(Path.home()))
    return landed_targets(targets, shared, root)


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


def opened_deadline(seconds: float, grace: float = 2.0) -> str:
    """Give this hook, and every process it starts, one deadline: returns the one before.

    Each runtime lets a call through once its policy hook runs past its
    timeout, so a hook that is still waiting when that comes has answered
    nothing, and nothing is the one answer a gate may not give. Everything a
    verdict may wait on -- a language server, a destination's evaluator, Git,
    `sed` -- shares the deadline set here, where the hook starts, and asks
    :func:`hook_seconds_left` rather than spending a timeout of its own; one
    cut short reads as the failure it already answers, so the verdict is
    reached in time.

    What no step bounds -- a file lock another writer holds, a read that
    never returns, the classifier itself -- is bounded by an alarm ``grace``
    seconds past the deadline. It raises where the hook is, and the
    dispatcher answers that as it answers every call it could not judge: it
    refuses. The grace is what separates the two: a step cut short at the
    deadline still has time to become the verdict it reads as.

    Kept in the environment, on the monotonic clock every process on the
    machine shares, so a process this hook starts inherits the deadline and
    never extends it: one already set by a parent stands where it is sooner.
    """
    environ = os.environ  # lup: ignore[os-environ]
    previous = environ["LUP_HOOK_DEADLINE"] if "LUP_HOOK_DEADLINE" in environ else ""
    deadline = time.monotonic() + seconds
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
    # catches this one, so it reaches the refusal of a call it could not judge.
    def overran(_number, _frame):
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
        raise RuntimeError("this hook reached its deadline before a verdict")

    signal.signal(signal.SIGALRM, overran)
    signal.setitimer(signal.ITIMER_REAL, max(held - time.monotonic(), 0.0) + grace)
    return previous


def closed_deadline(previous: str) -> None:
    """Disarm :func:`opened_deadline`'s alarm and put back the deadline it replaced."""
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    environ = os.environ  # lup: ignore[os-environ]
    if previous:
        environ["LUP_HOOK_DEADLINE"] = previous
        return
    environ.pop("LUP_HOOK_DEADLINE", None)


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
        "protocol": 1,
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
    return {
        name: [item for item in value if isinstance(item, str)]
        for name, value in loaded.items()
        if isinstance(name, str) and isinstance(value, list)
    }


def contained(measured: dict[str, list[str]]) -> bool:
    """Whether this session runs inside the boundary its profile promised.

    A different question from :func:`sandbox_active`, and the two are not
    interchangeable: the native sandbox confines one call at a time and can
    be told to leave some alone, where a container confines the process and
    was never asked.

    Read from what the launch *measured* rather than from a variable. The
    variable was ``LUP_CONTAINED``, a constant an image bakes, and a constant
    answers yes for any container built from that image, for a bare ``run``
    holding none of the lease, and -- since a launcher forwards its own
    environment -- for an uncontained session started from a shell that
    happened to export it. That last one is not hypothetical: it is a session
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


def review_records(path: Path) -> list[dict]:
    """Read complete object records, preserving malformed bytes as inert evidence."""
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return []

    def complete():
        with stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
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


def append_review_record(path: Path, encoded: str) -> None:
    """Frame an append without repairing an incomplete record into authority.

    Every writer holds the same file lock. An unterminated tail is retained
    and marked invalid before the next record, even if its partial write
    happened to end after a syntactically complete JSON object.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.write(b" [incomplete review record]\n")
        stream.write(encoded.encode("utf-8") + b"\n")
        stream.flush()


def native_review_records(path: Path) -> dict[str, dict]:
    """Read only native receipts with the fields used by the hermetic boundary."""

    def valid():
        for entry in review_records(path):
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
) -> dict[Literal["state", "id", "reason"], str]:
    """Park a call or spend its explicit, single-use reviewer answer.

    A declared successor stage may spend one further claim for that same
    identified invocation. The immutable primary claim proves which stage
    consumed the answer; neither a dispatched log row nor observed execution
    alone establishes that authority.
    """
    if not session:
        return {
            "state": "unavailable",
            "id": "",
            "reason": "the hook carries no session_id",
        }
    payload = json.loads(arguments)
    expected = (
        json.loads(execution_payload) if execution_payload is not None else payload
    )
    before = json.loads(preconditions)
    material = json.dumps(
        [
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
            {path: str(Path(path).resolve()) for path in before},
        ],
        sort_keys=True,
    )
    fingerprint = sha256(material.encode()).hexdigest()
    log = root / ".lup/questions.jsonl"

    entries = native_review_records(log)
    matches = [
        entry for entry in entries.values() if entry["fingerprint"] == fingerprint
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
        claim = root / ".lup/review-claims" / entry["id"]
        with claim.open(encoding="utf-8") as handle:
            consumed = json.load(handle)
        expected = {
            "fingerprint": fingerprint,
            "execution_id": execution_id,
            "stage": predecessor,
        }
        if consumed == expected:
            successor = (
                root
                / ".lup/review-stage-claims"
                / entry["id"]
                / sha256(stage.encode()).hexdigest()
            )
            successor.parent.mkdir(parents=True, exist_ok=True)
            try:
                with successor.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(expected, sort_keys=True))
            except FileExistsError:
                pass
            else:
                return {"state": "approved", "id": entry["id"], "reason": ""}
    entry = matches[-1] if matches else None
    if entry is not None and entry["state"] == "approved":
        match entry:
            case {
                "answer": {
                    "approved": True,
                    "principal": str() as principal,
                    "receipt": "recorded",
                }
            } if principal and principal not in (
                session,
                entry["operation"]["requester"],
            ):
                pass
            case _:
                raise ValueError(
                    "hook approval has no recorded independent affirmative answer"
                )
        claim = root / ".lup/review-claims" / entry["id"]
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
            entry["state"] = "dispatched"
            entry["execution_id"] = execution_id
            append_review_record(log, json.dumps(entry, sort_keys=True))
            return {"state": "approved", "id": entry["id"], "reason": ""}
    if entry is not None and entry["state"] in ("pending", "rejected"):
        answer = entry["answer"] if "answer" in entry else None
        note = answer["note"] if answer and "note" in answer else ""
        return {
            "state": entry["state"],
            "id": entry["id"],
            "reason": note or entry["reason"],
        }
    identifier = os.urandom(16).hex()
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
        "resumption": "native_retry",
        "operation": {
            "id": identifier,
            "session": session,
            "requester": session,
            "tool": tool,
            "payload": payload,
            "cwd": str(root),
            "worktree": str(root),
        },
    }
    append_review_record(log, json.dumps(entry, sort_keys=True))
    return {"state": "pending", "id": identifier, "reason": reason}


def observe_hook_call(
    root: Path, session: str, tool: str, arguments: dict, execution_id: str
) -> list[str]:
    """Reconcile execution with its receipt without inferring authorization."""
    log = root / ".lup/questions.jsonl"
    if not session or not log.exists():
        return []
    entries = native_review_records(log)

    def same_call(entry):
        expected = entry.get("execution_payload")
        return entry["operation"]["tool"] == tool and arguments == (
            entry["operation"]["payload"] if expected is None else expected
        )

    matches = [
        entry
        for entry in entries.values()
        if entry["operation"]["session"] == session
        and entry["operation"]["cwd"] == str(root)
        and (
            "execution_id" in entry and entry["execution_id"] == execution_id
            if execution_id
            else entry["operation"]["tool"] == tool
            and (entry["operation"]["payload"] == arguments or same_call(entry))
        )
        and entry["state"]
        in ("pending", "approved", "rejected", "dispatched", "prompted")
    ]
    if not matches:
        return []
    entry = matches[-1]
    matches_call = same_call(entry)
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
    entry["state"] = "completed" if authorized else "in_doubt"
    entry["completed"] = datetime.now(UTC).isoformat()
    entry["outcome"] = (
        "Native execution observed; effect success is not verified."
        if authorized
        else f"Native execution observed {problem}."
    )
    append_review_record(log, json.dumps(entry, sort_keys=True))
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

    Append-only, because the failure this survives is a crash between
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
    entry = json.dumps(
        {
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
        },
        sort_keys=True,
    )
    path = root / relay
    try:
        if any(
            "id" in entry and entry["id"] == identifier
            for entry in review_records(path)
        ):
            return identifier
        append_review_record(path, entry)
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
    command has run. The later event was proposed and refuted: a runtime
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
    path = root / corpus
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
    return root / ".lup/hooks/approvals.jsonl"


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
        # Git not answering in the time this hook has left reads as Git
        # failing, which every caller already answers.
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
    """This path as the session's own checkout spells it, or "" outside it.

    :func:`worktree_path` anchors a path at the checkout nearest the file,
    which is the right anchor for every rule but one. A repository nested
    inside this checkout -- a probe kit given its own ``git init`` under
    ``tmp/``, so a runtime launched there takes it as the project root -- has
    a ``.git`` nearer the file than this checkout's, so the file arrives
    spelled against the kit and claims none of the roles this checkout
    declares for the tree around it. This is the other anchor, and the kernel
    reads it for that one question.

    Resolved before it is compared, so a link is judged where it lands: a
    ``refs/`` entry pointing at another project is outside this checkout
    however it is spelled. A relative path is anchored on the session's own
    directory, where the tool carrying it resolves it, and a session in no
    checkout holds nothing, which the empty answer says.
    """
    if root is None:
        return ""
    checkout = worktree_root(str(root))
    if not checkout:
        return ""
    resolved = (root / path_text).resolve()
    if not resolved.is_relative_to(checkout):
        return ""
    return resolved.relative_to(checkout).as_posix()


def publish_edition(path_text: str) -> None:
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

    The rename is the whole guarantee, and the temporary is dot-prefixed, for
    the reasons ``lup.channels.models.write_atomic`` gives. This cannot call
    that one: it is compiled into a bare script with no ``lup`` to import.
    """
    root = worktree_root(path_text)
    shared = shared_git_directory(path_text)
    if not root or not shared:
        return
    destination = Path(shared) / "lup" / "edition.json"
    record = json.dumps(
        {"workspace": root, "file": str(Path(path_text).resolve())}, indent=2
    )
    temporary_path = destination.with_name(f".{destination.name}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(record + "\n", encoding="utf-8")
        temporary_path.replace(destination)
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

    Accepting only the first is what made this gate unavailable rather than
    configurable. A declared program it could not resolve produced no
    diagnostics and said nothing about why, so a project outside one layout
    did not get a weaker check — it got silence indistinguishable from a
    clean file, on every edit.

    A bare name is asked of the checkout's own environment before ``PATH``,
    because that is where a project's toolchain is installed and asking is
    what keeps the declaration from naming a layout. Spelling the path in
    would answer only for the layout it spelled: ``.venv`` is `uv`'s default
    and nothing else's, so a project that redirected it, or that installs
    through conda or pyenv, resolved to nothing and was gated in silence.
    The scripts directory comes from the running interpreter — ``bin`` on
    POSIX, ``Scripts`` on Windows — because that is a property of how Python
    is installed rather than of any project, and reading it is what keeps
    this from being a second layout assumption behind the one it replaces.
    It is read as a candidate rather than as the answer: a hook runs under
    whichever ``python3`` the runtime found, and one installed in ``sbin``
    names a directory no environment has, which resolved every declared
    program to a bare name and left the gate silent on a machine where it
    was installed all along. The conventional pair follows it, so the
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


def file_diagnostics(
    path_text: str,
    command: list[str],
    suffixes: tuple[str, ...] = (".py", ".pyi"),
    timeout_seconds: float = 20.0,
) -> list[str]:
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
    """
    if not command or Path(path_text).suffix.lower() not in suffixes:
        return []
    if conflicted(path_text):
        return []
    root = worktree_root(path_text)
    if not root:
        return []
    located = declared_program(root, command[0])
    if not located:
        return []
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
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return []
    return [
        f"{Path(edited).relative_to(Path(root).resolve())}:"
        f"{item['range']['start']['line'] + 1}: {item['severity']}: {item['message']}"
        for item in reported
        if item["file"] == edited and item["severity"] != "information"
    ]


def repaired_directives(
    path_text: str,
    command: list[str],
    suffixes: tuple[str, ...] = (".py", ".pyi"),
    timeout_seconds: float = 30.0,
) -> list[str]:
    """Take the dead `# lup: ignore` directives out of one written file.

    A directive guarding nothing is the one audit finding whose fix is not a
    judgement — there is a single correct edit and this is it — so the gate
    ahead of the write neither refuses it nor spends an approval naming it,
    and it goes afterwards instead. That pairing is what lets the prompt leave
    it unmentioned: unlisted and removed is one behaviour, while unlisted and
    kept would be a directive nobody ever reads.

    Reported back rather than done in silence. The agent wrote the directive
    believing it did something, and a line that disappears without a word is
    one it writes again on the next file.

    Anything that goes wrong is nothing repaired, exactly as an unreadable
    checker is no diagnostics: this runs after the tool, so the alternative
    to saying nothing is failing a write that has already happened.
    """
    if not command or Path(path_text).suffix.lower() not in suffixes:
        return []
    if conflicted(path_text):
        return []
    root = worktree_root(path_text)
    if not root:
        return []
    located = declared_program(root, command[0])
    if not located:
        return []
    # Named the way the sweep names its own files, which is how the request
    # and the report come back in one spelling. It is also the only spelling
    # every sweep must understand: a project declares its own program here,
    # and one that selects by repository-relative prefix is the shape this
    # can count on rather than one it would have to assume.
    try:
        named = str(Path(path_text).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return []
    try:
        finished = subprocess.run(
            [located, *command[1:], "--path", named],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=hook_seconds_left(timeout_seconds),
            check=False,
        )
        reported = json.loads(finished.stdout)["repaired"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return []
    return [
        f"line {item['line']}: removed `# lup: ignore"
        + (f"[{item['rule_id']}]" if item["rule_id"] else "")
        + "` — it guarded no rule, so it silenced nothing"
        for item in reported
    ]


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

    Git missing, the path outside a repository, a malformed pathspec, a
    non-zero exit, and no answer inside ``timeout_seconds`` or the hook's
    deadline, whichever is nearer, all collapse to None, so a caller reading
    this as evidence that something is safe to destroy treats an unanswerable
    question as a no.

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
            # Bounded by what the hook has left: an answer that does not come
            # in time is the unanswerable question this already reads as no.
            timeout=hook_seconds_left(timeout_seconds),
        )
    except (OSError, subprocess.TimeoutExpired):
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
    oversight: on the checkout this was built in, ignored-but-precious content
    came to 592 MB against a 21 MB object store, so capturing it would write
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
        return (root / target).read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def rewritten_text(
    scripts: list[str], target: str, root: Path, timeout_seconds: float = 25.0
) -> dict[Literal["text", "cause"], str | None]:
    """What one file would hold after these scripts, without touching the file.

    sed is run over the file and its output captured, which is the same
    computation ``-i`` performs and none of the writing: ``-i`` is exactly
    "run the script, then replace the file with the result", so dropping it
    leaves the result on standard output and the file as it was. A command
    still about to be refused has therefore changed nothing by being judged.

    Both keys always stand and exactly one is filled: ``text`` is the
    after-document, ``cause`` is what stopped one being produced.

    A cause rather than a bare absence, because each of the four sends the
    writer somewhere different: nothing stands at the path, something stands
    there that a rewrite cannot replace, sed would not run the script, or what
    came back is not text this can read. One sentence covering all four told a
    writer who typed a wrong path the same thing it told one who aimed ``-i``
    at a directory, and offered a recovery that fitted neither.

    The cause crosses as the word this half read rather than as the
    classifier's own literal, which this half may not name -- the arrangement
    a checker's verdicts already cross by, and the kernel narrows it.

    The scripts are passed as ``-e`` expressions and the file as an operand
    after ``--``, so a filename beginning with a dash stays a filename and a
    script is never re-read as one.
    """
    landed = root / target
    if not landed.exists():
        return {"text": None, "cause": "missing"}
    if not landed.is_file():
        return {"text": None, "cause": "irregular"}
    expressions = [word for script in scripts for word in ("-e", script)]
    try:
        finished = subprocess.run(
            ["sed", *expressions, "--", str(landed)],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=hook_seconds_left(timeout_seconds),
        )
    except UnicodeDecodeError:
        return {"text": None, "cause": "unreadable"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"text": None, "cause": "refused"}
    if finished.returncode:
        return {"text": None, "cause": "refused"}
    return {"text": finished.stdout, "cause": None}


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
    volume, the credential seed, the peer inbox. A filesystem mounted whole --
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
                # The table escapes a space, a tab, a newline and a backslash
                # as three octal digits and leaves every other byte as UTF-8.
                raw = mount_point.encode("utf-8").decode("unicode_escape")
                return [raw.encode("latin-1").decode("utf-8", "replace")]
            case _:
                return []

    return [
        point
        for record in csv.reader(mountinfo.splitlines(), delimiter=" ")
        for point in lent(record)
    ]


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
    targets: list[str], shared: list[str], root: Path | None = None
) -> list[list[str]]:
    """Place each target: this checkout, somewhere else the host shares, or the container.

    Pairs of ``[path, landing]``, one per target, in the order given. Resolved
    against ``root``, the session's own checkout, so a link is followed to
    where it lands. A target no one can read -- an expansion, a substitution,
    a directory a `cd` left unknown -- lands ``host``: nothing here can vouch
    for where it goes, and that is the answer that keeps a question.
    """
    where = Path.cwd() if root is None else root
    checkout = where.resolve()

    def landing(target: str) -> str:
        if "$" in target or "`" in target:
            return "host"
        resolved = (where / target).resolve()
        if resolved.is_relative_to(checkout):
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
    # Both halves of one reading: the documents a rewrite would leave, and why
    # the rest left none. Computed together so a target reaches exactly one.
    reading = rewritten_documents(
        command, cwd or Path.cwd(), autonomous, agent_identity
    )
    verdict = decide_shell(
        command,
        SHELL_RULES,
        ALLOWED_FETCH_SCOPES,
        DENIED_FETCH_SCOPES,
        sandboxed=sandboxed,
        excluded_commands=SANDBOX_EXCLUDED_COMMANDS,
        trusted_script_roots=managed_script_roots(managed_root),
        path_roles=PATH_ROLES,
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
        # nothing Git ignores: one ignored target leaves the loss uncaptured.
        recovered=bool(reference)
        and not ignored_write_targets(
            [
                *shell_write_targets(command),
                *shell_written_targets(command, SHELL_RULES),
            ],
            cwd,
        ),
    )
    # The gates an edit is judged by, over the writes this command carries the
    # content of. Joined here rather than inside the classifier because they
    # read the filesystem -- the file about to be replaced, so a note removed
    # or an anti-pattern introduced is seen against what is actually there --
    # and the kernel reads nothing. Strongest wins, the rule every other join
    # in this policy uses.
    authored = authored_review(command, cwd or Path.cwd(), autonomous, agent_identity)
    if authored is not None and STRENGTH.index(authored.effect) > STRENGTH.index(
        verdict.effect
    ):
        verdict = authored
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
    return KernelDecision(
        verdict.effect,
        verdict.reason + nudge,
        verdict.sandbox,
        verdict.escalated,
        checkpoint=verdict.checkpoint,
    )


def reviewed_decision(
    decision: KernelDecision,
    cwd: Path,
    session: str,
    tool: str,
    arguments: dict,
    preconditions: dict[Path, str | None],
    execution_id: str = "",
    stage: str = "",
    predecessor: str = "",
    execution_payload: dict | None = None,
    policy_identity: str = "",
) -> KernelDecision:
    """Only an explicit, single-use recorded answer can settle a native ask."""
    result = review_hook_call(
        cwd,
        session,
        tool,
        json.dumps(arguments, sort_keys=True),
        json.dumps(
            {str(path): before for path, before in preconditions.items()},
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
    )
    if result["state"] == "approved":
        return decision.revised(effect="allow")
    if result["state"] == "rejected":
        return decision.revised(
            effect="deny",
            recovery=f"Review {result['id']} was rejected: {result['reason']}. Revise the proposal before retrying.",
        )
    identifier = result["id"]
    if not identifier:
        return decision.revised(
            effect="deny",
            recovery=f"Review queue unavailable: {result['reason']}. Run this operation from an operator terminal.",
        )
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
        "dev",
        "questions",
    ]
    show = shlex.join([*prefix, "show", identifier])
    answer = shlex.join([*prefix, "answer", identifier, "--as", "operator"])
    reject = shlex.join([*prefix, "reject", identifier, "--as", "operator"])
    return decision.revised(
        effect="deny",
        recovery=(
            f"Review {identifier} is {result['state']}. The operator can run "
            f"`{show}`, then `{answer}` or `{reject}`. "
            "After approval, retry this exact tool call; changed file contents require fresh review."
        ),
    )


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


def peer_send_decision(values: list[str], cwd: Path | None) -> KernelDecision:
    """Judge one native send against who this repository's roster holds.

    The kernel reads no filesystem, so the roster is folded here and passed as
    the spellings it currently answers to. Every string the call carries is
    offered rather than a named field, because which field a runtime spells a
    recipient in is that runtime's business and this half answers for all of
    them.
    """
    directory = peer_directory(cwd)
    if directory is None:
        return decide_peer_send(values, [], PEER_POLICY)
    return decide_peer_send(values, store.addresses(directory), PEER_POLICY)


def peer_listing_decision() -> KernelDecision:
    """Judge one native listing of who this session can reach, which defers."""
    return decide_peer_listing(PEER_POLICY)


def spawn_decision(name: str, values: list[str], field: str) -> KernelDecision:
    """Judge one native spawn by the name it carries, against what this project declared.

    ``name`` is the runtime's own field for it, read by the host half that
    knows which key that is, and ``field`` is that key, so the refusal can
    name the argument; every string the call carries rides beside them so an
    escalation marker in any of them is found.
    """
    return decide_spawn(name, values, SPAWN_NAMES, field)


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


def rewritten_documents(
    command: str, cwd: Path, autonomous: bool = False, agent_identity: str = ""
) -> RewriteReading:
    """What every in-place rewrite in this command would leave behind.

    The kernel names which files a screened rewrite would replace and this
    produces each one, so the classifier judges a rewrite by the document it
    makes rather than by whether the file could be restored afterwards. The
    two questions are different, and only this one is the question the edit
    gates ask.

    A file that could not be produced yields a reason instead of a document,
    and the classifier says which reason. A rewrite is never granted on a
    reading that failed — which is what makes it safe for a composition to
    reach this late, or not at all.

    Each row is deduplicated by target, because one file named twice is one
    file, and the second reading would run the script over the same bytes to
    reach the same answer.
    """
    # lup: ignore[empty-collection] — one reading feeding two collections: each
    # target reaches exactly one of them and which it is costs a sed run, so a
    # comprehension per list would run every script twice
    rows: list[RewrittenDocumentRow] = []
    unproduced: list[UnproducedDocumentRow] = []  # lup: ignore[empty-collection]
    for rewrite in shell_sed_rewrites(command, SHELL_RULES):
        for target in rewrite["targets"]:
            if any(row["target"] == target for row in rows) or any(
                row["target"] == target for row in unproduced
            ):
                continue
            attempt = rewritten_text(rewrite["scripts"], target, cwd)
            after = attempt["text"]
            if after is None:
                unproduced.append(
                    UnproducedDocumentRow(
                        target=target, cause=unproduced_cause(attempt["cause"])
                    )
                )
                continue
            before = text_at(cwd, target)
            if before is None:
                unproduced.append(
                    UnproducedDocumentRow(target=target, cause="unreadable")
                )
                continue
            path_text = worktree_path(str((cwd / target).resolve()))
            foreign = foreign_repository(target, cwd)
            rows.append(
                RewrittenDocumentRow(
                    target=target,
                    path=path_text,
                    before=before,
                    after=after,
                    foreign=foreign,
                    decision=edit_decision(
                        target,
                        before,
                        after,
                        True,
                        autonomous,
                        cwd=cwd,
                        agent_identity=agent_identity,
                    ),
                    outside_project=outside_this_project(target, cwd),
                    checkout_path=this_checkout_path(target, cwd),
                    resolution=None,
                )
            )
    return RewriteReading(documents=rows, unproduced=unproduced)


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
            return read_response(json.loads(response))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return routing_failure(str(error))
    return local_edit_decision(
        path,
        before,
        after,
        path_exists,
        autonomous,
        operation,
        cwd,
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
    command: str, cwd: Path, autonomous: bool, agent_identity: str = ""
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

    The strongest verdict of the writes it could read, or ``None`` where it
    read none. An unreadable file leaves the write judged as it was rather
    than refused for being unreadable: the reading is a relaxation's
    precondition, not a gate of its own.
    """
    verdicts = [
        edit_decision(
            write["path"],
            before,
            (before or "") + write["content"] if write["append"] else write["content"],
            path_exists=existing,
            autonomous=autonomous,
            operation=(
                "modify" if write["append"] else "overwrite" if existing else "create"
            ),
            cwd=cwd,
            agent_identity=agent_identity,
        )
        for write in authored_writes(command)
        for existing in [(cwd / write["path"]).is_file()]
        for before in [text_at(cwd, write["path"]) if existing else None]
    ]
    stopped = [verdict for verdict in verdicts if verdict.effect != "allow"]
    if not stopped:
        return None
    return max(stopped, key=lambda verdict: STRENGTH.index(verdict.effect))


def written_review(command: str, cwd: Path) -> list[str]:
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
    it does about it is the next turn's business.

    A patch is the third route in, and the one that is read rather than
    resolved: `git apply` replaces tracked content wholesale by a spelling no
    content gate sees, and its targets are inside the file it is handed. Git
    reads them out, and what lands is put to the same gates as the rest --
    which is what lets that row allow instead of refusing an operation with no
    reasonable substitute.
    """
    carried = [write["path"] for write in authored_writes(command)]

    return [
        f"{target}: {verdict.reason}"
        for target in [
            *shell_write_targets(command),
            *shell_flag_write_targets(command, SHELL_RULES),
            *patch_write_targets(shell_patch_operands(command, SHELL_RULES), cwd),
        ]
        # A write whose bytes were in the command went to these gates before it
        # ran, and reporting it again tells the agent the same thing twice about
        # a write somebody has already answered for.
        if target not in carried and (cwd / target).is_file()
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
        if verdict.effect != "allow"
    ]


def foreign_claim_decision(path_text: str, cwd: Path | None) -> KernelDecision | None:
    """Whether a live session other than this one is already in the named file.

    The roster and the claim record are both live, so both are folded here and
    handed over as the names they resolve to — the kernel reads no filesystem
    and decides from what it is given.
    """
    directory = peer_directory(cwd)
    if PEER_POLICY is None or directory is None:
        return None
    return decide_foreign_claim(
        path_text,
        store.claim_holders(
            directory, path_text, declared_identity(PEER_POLICY["member_env"])
        ),
        PEER_POLICY,
    )


def claim_window_opened(cwd: Path | None) -> None:
    """Snapshot the tree before a command whose writes no input names.

    Only a command needs this. Every other writing call says which file it is
    about, and a call that names its own target is attributed from the target
    rather than from a comparison.
    """
    if PEER_POLICY is None:
        return
    open_claim_window(
        cwd,
        PEER_POLICY["store"],
        PEER_POLICY["windows_dir"],
        declared_identity(PEER_POLICY["member_env"]),
    )


def claim_window_closed(cwd: Path | None) -> None:
    """Attribute what a command changed, contested where nothing could tell."""
    if PEER_POLICY is None:
        return
    directory = peer_directory(cwd)
    mine = declared_identity(PEER_POLICY["member_env"])
    closed = close_claim_window(
        cwd, PEER_POLICY["store"], PEER_POLICY["windows_dir"], mine
    )
    if directory is not None:
        store.record_claims(directory, mine, closed["paths"])


def named_claim_recorded(path_text: str, cwd: Path | None) -> None:
    """Attribute a change to the exact file the call named.

    The tier that needs no comparison: the call said which file, so what this
    leaves on the session's own member file is evidence of the state that
    session left the path in, rather than of what a before-and-after could
    narrow the writer down to.
    """
    directory = peer_directory(cwd)
    if PEER_POLICY is None or directory is None or not path_text:
        return
    store.record_claims(
        directory,
        declared_identity(PEER_POLICY["member_env"]),
        [str(Path(path_text).resolve())],
    )


def edit_claim_decision(
    verdict: KernelDecision, path_text: str, cwd: Path | None
) -> KernelDecision:
    """One edit's own verdict, settled together with any claim over its path.

    The join lives here rather than in either runtime, so both reach the same
    answer about the same file: what the content gates decided, and whether
    somebody else is already in it, are two questions and one approval.
    """
    return settled_with_claim(verdict, foreign_claim_decision(path_text, cwd))


def main() -> None:
    """Judge one normalized document under these accepted policy bytes."""
    request = read_edit_request(json.load(sys.stdin))
    path = Path(request["path"])
    owner = Path(request["owner"])
    if not path.is_absolute() or str(owner.resolve()) != worktree_root(str(path)):
        raise ValueError("edit target does not belong to the destination owner")
    autonomous = (
        request["autonomous"]
        and request["agent_identity"] in AUTONOMOUS_AGENT_IDENTITIES
    )
    decision = local_edit_decision(
        str(path),
        request["before"],
        request["after"],
        request["path_exists"],
        autonomous,
        request["operation"],
        owner,
        allowances=[],
        resolve_external=False,
    )
    print(json.dumps({"protocol": 1, "decision": decision_wire(decision)}))


if __name__ == "__main__":
    main()
