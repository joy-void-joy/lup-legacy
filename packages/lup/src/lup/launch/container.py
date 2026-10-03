"""Opening a native session inside the container the project declares.

The launcher's other half. :mod:`lup.harness.image` says what a container is
and how it starts; this says how a *launch* reaches one -- which image tag
this checkout answers to, whether it has been built, and what the lease under
the session mounts.

Why the checkout is a mount rather than a copy, and why it is mounted at its
own absolute path: a linked worktree's ``.git`` is a file holding an absolute
``gitdir:`` pointer into the repository's common directory, so a tree that
appeared anywhere else inside the container would be a checkout pointing at a
path that does not exist. :func:`lup.sandbox.rail.same_path` is the rule, and
this module is one of the two callers that has to honour it.

What this deliberately does not do is fall back to an uncontained launch when
the boundary cannot be built. A boundary that quietly is not there is the one
failure mode the design forbids, so a caller that asked to be contained and
cannot be gets a refusal naming what was missing, and the operator decides.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
import json
import os
import shlex
import stat
import sys
import time
from datetime import UTC, datetime
from collections import deque
from contextlib import nullcontext
from ipaddress import IPv4Address
from pathlib import Path

import sh
from lup.execution.shell import git
from lup.policy.identity import POLICY_ROOT_ENV
from pydantic import BaseModel, Field
from rich.console import Console
from rich.live import Live
from rich.text import Text

from lup.launch.preflight import (
    LaunchSentinels,
    ROOT_VARIABLE,
    launch_record,
    mount_table,
)
from lup.harness.credential import committer, fleet_rewrites
from lup.harness.devices import (
    Device,
    DeviceLease,
    lease_devices,
    registered_devices,
)
from lup.formats import digest
from lup.harness.egress import PROXY_LABEL, SessionEgress
from lup.harness.image import (
    ContainerEngine,
    Image,
    MemoryLimit,
    SessionPrivileges,
    SessionStreams,
    detected_client,
)
from lup.harness.notice import Banner, Notice
from lup.harness.releases import resolved_agent_clis
from lup.harness.requirements import Manifest
from lup.coordination.bare.store import STORE_DIR
from lup.workspace.shared_directory import ARCHIVE_DIRECTORY_NAME, SLOT_DIRECTORY
from lup.harness.terminal import host_timezone
from lup.providers.login import ProviderLogin
from lup.providers.runtime_homes import runtime_logins
from lup.launch.superseded import SupersededFile
from lup.launch.environments import (
    HeldEnvironment,
    claimed,
    sweep_environments,
)
from lup.launch.config_volume import (
    HandedLogin,
    Handoff,
    HomeFile,
    HomeHelper,
    HomeSeedPlaces,
    RuntimeVolume,
    VolumeLogins,
    kept_for_superseded,
    settle_handoff,
    settle_home_seed,
    split_config_volumes,
    sweep_superseded,
    swept_superseded_notice,
)
from lup.sandbox.attribution import WRITE_REFUSAL_MARKERS
from lup.launch.pointer_trust import judged_roots, launcher_state_exposure
from lup.sandbox.pointers import pointer_drift, refusal
from lup.launch.refusal import LaunchRefused
from lup.sandbox.rail import (
    AccessibleRoot,
    Lease,
    demoted,
    fleet_lease,
    NestedRepository,
    hold_pruning_across,
    in_repository,
    merged,
    prepared_across,
    repository_layout,
    rooted,
    same_path,
    sibling_worktrees,
    worker_lease,
    working_trees,
)

# lup: ignore[library-default] — a mirror of the directories lup's own modules write under a shared git directory, named from those modules; no adopter chooses it
SHARED_STATE = (STORE_DIR, SLOT_DIRECTORY, ARCHIVE_DIRECTORY_NAME)
"""The directories lup keeps under a shared git directory, each made before a lease.

A session writes each of them from inside -- the coordination store and the
branch records, a gate's admission slots, archived traces -- and each is
created on first use, which a read-only shared directory refuses. Named here
from the modules that own them, so a rename there reaches the launch.
"""


def image_tag(dockerfile: str) -> str:
    """The image a declaration answers to, named for the declaration itself.

    Two worktrees share a layer exactly when they would build the same thing,
    and get separate ones exactly when they would not -- derived rather than
    assumed. The assumption is what the alternatives cost. Naming the image
    after the *worktree* pays a full build per checkout, which is hundreds of
    packages times however many are open. Naming it after the *repository*
    rests on every worktree declaring the same toolchain, which is true right
    up until a branch edits the image declaration -- and then the two rebuild
    over each other on every switch, because :func:`image_matches` compares
    the declaration digest and each finds the other's.

    Twelve hex characters, for the reason a short object hash is enough to
    name a commit: this is a handle for a thing already in the store, not a
    security claim, and the full digest is on the image's own label.
    """
    return f"lup-agent:{digest.text(dockerfile)[:12]}"


def checkout_tag(root: Path) -> str:
    """The readable name this checkout's latest image also answers to.

    A second tag on the same image, moved to whatever this checkout last
    built or reused. It earns its place twice over. An image list showing
    only digests is one nobody can read, and -- the load-bearing half -- the
    preflight has to name the image it probes, while the digest is computed
    *from* the manifest that preflight is part of. A name that does not
    depend on the declaration is the only one that can be stated inside it.

    Moved even when the build is skipped, or a checkout that reused another's
    image would have no tag of its own and its probe would ask after nothing.
    """
    return f"lup-agent:{root.name}"


# lup: solved: a contained session's config home is one volume per repository,
# so a first launch in a new repository opens its container on a fresh
# document. The account, theme, tier and effort now arrive from the person's
# lup config, but every other preference Claude Code keeps in its home —
# editor mode, user settings.json, keybindings, user memory — starts over in
# each repository's volume. Decide whether those travel from the selected
# account's home into the volume at launch, or the volume is kept per person
# rather than per repository, which shares a written hook across projects.
def state_volume_name(root: Path, login: ProviderLogin) -> str:
    """The volume carrying one runtime's container-side config home for this project.

    Per repository, and keyed on the shared git directory because that is the
    only name every worktree of one repository agrees on. ``root.name`` reads
    as the repository right up until the checkout is a linked worktree -- and
    the documented workflow makes one per feature. Keying on it costs a
    config home created empty for every branch: trust re-seeded, the history
    a ``--continue`` reopens gone, and, since a login can be made in here, a
    sign-in per feature.

    Per runtime, so one CLI's sessions never read the other's transcripts or
    credentials (:mod:`lup.launch.config_volume`). Not per person:
    a volume every repository shared would carry one project's transcripts
    and settings into every other. The person's settings reach it by being
    seeded at each launch, and what a session changes goes back to their own
    files when it closes, so the volume holds state rather than decisions.
    """
    return f"lup-{login.state_volume}-{repository_layout(root).name()}"


# lup: ignore[constant-declaration] — an identity this repository defines, not a
# judgement: the writer and the reader have to name the same key or neither works
DECLARATION_LABEL = "lup.declaration"
"""The label carrying the digest of the Dockerfile an image was built from."""


def image_matches(tag: str, dockerfile: str, engine: ContainerEngine) -> bool:
    """Whether this tag exists *and* was built from this declaration.

    Presence alone is the wrong question: an image is built once and the
    declaration goes on changing, so every later edit -- a pinned CLI, a
    package, the entrypoint -- is a change that lands in the repository and
    never in the thing a session actually runs in. Nothing reports it,
    because from the outside a stale image and a current one are one tag.

    The digest is a label rather than a file beside the image, so it travels
    with what it describes and cannot be left behind by a `rmi`. An image
    carrying no label at all says nothing about what built it, and is treated
    as stale: rebuilding costs a build, and trusting it costs a session
    running in something nobody can identify.
    """
    try:
        labelled = sh.Command(engine.binary)(
            "image",
            "inspect",
            "--format",
            f'{{{{index .Config.Labels "{DECLARATION_LABEL}"}}}}',
            tag,
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return str(labelled).strip() == digest.text(dockerfile)


# lup: ignore[constant-declaration] — an identity this repository defines, not a
# judgement: the tag functions above spell it and the sweep below matches on it,
# so a caller free to differ would be a caller whose prune found nothing
IMAGE_PREFIX = "lup-agent:"
"""What every image this project builds is named under.

An identity rather than a judgement: the tag functions above spell it and the
sweep below has to match them, so a caller free to differ would be a caller
whose prune found nothing.
"""


def superseded_images(engine: ContainerEngine, keep: str) -> list[str]:
    """The digest tags no checkout is pointing at any more.

    Content-addressing is what lets two checkouts share a layer, and the cost
    it comes with is that editing the declaration leaves the old image
    standing rather than replacing it under one name. These are big -- a full
    Arch base and a package set -- so something has to name what is finished.

    A digest tag is finished when no readable checkout tag sits on the same
    image. That is the whole test, and it is deliberately not "older than":
    a checkout nobody has opened for a month still runs the image its tag
    points at, and time says nothing about that.

    ``keep`` is the tag this checkout would build right now, held back
    whether or not anything is tagged onto it yet -- a sweep run between a
    declaration edit and the next launch would otherwise delete the image
    that launch is about to reuse.
    """
    try:
        listed = sh.Command(engine.binary)(
            "images", "--format", "{{.ID}} {{.Repository}}:{{.Tag}}"
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return []
    return finished_tags(str(listed), keep)


def finished_tags(listing: str, keep: str) -> list[str]:
    """Read one engine's image listing down to the tags nothing points at.

    Split from the call that fetches the listing because this is the half
    that decides what gets deleted, and a sweep whose selection cannot be
    exercised without a container runtime is one nobody should be asked to
    trust. The listing is one ``<id> <repository>:<tag>`` per line, which is
    the format the caller asks for.

    A digest tag is recognized by its shape -- the prefix plus twelve hex
    characters -- rather than by parsing, because that is what
    :func:`image_tag` produces and a checkout name of that exact length
    would have to be twelve characters of hex to collide.
    """
    rows = [line.split() for line in listing.splitlines()]
    # Sliced at the prefix rather than split on "/", because the registry a
    # listing prepends is not a path and the tag is not its last segment: the
    # name this project builds under starts where the prefix does, and
    # everything before it is whichever store the image happens to sit in.
    named = [
        (row[0], row[1][row[1].index(IMAGE_PREFIX) :])
        for row in rows
        if len(row) == 2 and IMAGE_PREFIX in row[1]
    ]
    beside = {
        identifier: [tag for other, tag in named if other == identifier]
        for identifier, _ in named
    }
    return sorted(
        tag
        for tags in beside.values()
        for tag in tags
        if tag != keep
        and len(tag) == len(IMAGE_PREFIX) + 12
        and not any(len(other) != len(tag) for other in tags)
    )


def retire_images(tags: list[str], engine: ContainerEngine) -> list[str]:
    """Remove each named image, reporting the ones that actually went.

    One call per tag rather than one call for all of them, because a single
    failure -- an image a stopped container still references -- would take
    the whole sweep down with it and leave the operator no better off.
    """
    gone: list[str] = []
    for tag in tags:
        try:
            sh.Command(engine.binary)("rmi", tag)
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            continue
        gone.append(tag)
    return gone


def finished_containers(engine: ContainerEngine, keep: str) -> list[str]:
    """Every stopped container lup labelled as an egress proxy, but ``keep``.

    A proxy is left standing when it stops, deliberately: its log is the
    only copy of why, and :func:`start_egress` reads it before clearing
    it. That holds for the project being launched, which is ``keep``;
    another project's stopped proxy is one nobody reads until that
    project launches, and a machine with many projects keeps one each.
    """
    try:
        listed = sh.Command(engine.binary)(
            "ps",
            "-a",
            "--filter",
            f"label={PROXY_LABEL}",
            "--filter",
            "status=exited",
            "--format",
            "{{.Names}}",
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return []
    return [name for name in str(listed).split() if name != keep]


def sweep_containers(engine: ContainerEngine, keep: str) -> list[str]:
    """Remove every finished container, answering the ones that went."""
    gone: list[str] = []
    for name in finished_containers(engine, keep):
        try:
            sh.Command(engine.binary)("rm", name)
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            continue
        gone.append(name)
    return gone


def swept_notice(
    images: list[str], environments: list[HeldEnvironment], containers: list[str]
) -> list[Notice]:
    """Say what a launch cleared away, once, where anything went."""
    parts = [
        *([f"images {', '.join(images)}"] if images else []),
        *(
            ["environments of " + ", ".join(str(held.root) for held in environments)]
            if environments
            else []
        ),
        *([f"stopped containers {', '.join(containers)}"] if containers else []),
    ]
    if not parts:
        return []
    return [
        Notice(
            text=(
                "Cleared what nothing points at any more: "
                + "; ".join(parts)
                + ". `harness clean` lists the rest."
            ),
            urgency="detail",
        )
    ]


def name_for_checkout(tag: str, readable: str, engine: ContainerEngine) -> None:
    """Point this checkout's readable name at the image it will actually run.

    Idempotent, and silent about its own failure: the tag is a convenience
    for whoever reads an image list and the name the preflight probes, and
    neither is worth refusing a launch over. The session runs the digest tag
    regardless, which is the one that has to be right.
    """
    try:
        sh.Command(engine.binary)("tag", tag, readable)
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return


def running(name: str, engine: ContainerEngine) -> bool:
    """Whether a container by this name is up, asked of the engine.

    Asked rather than remembered, for the same reason :func:`image_matches`
    is: the operator may have removed it between launches, another checkout
    of the same repository may have brought it up, and a launcher that kept
    its own record of either would be reading a file instead of the truth.
    """
    try:
        state = sh.Command(engine.binary)(
            "inspect", "--format", "{{.State.Running}}", name
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return str(state).strip() == "true"


def host_resolv_conf(path: Path = Path("/etc/resolv.conf")) -> str:
    """What this machine names as its own nameservers, or nothing readable.

    Absence answers empty rather than raising, because a launch is not the
    place to fail over a file: what a missing one costs is that the proxy
    keeps whatever the engine wrote, which :func:`handed_resolvers` says out
    loud rather than leaving to be found later as a boundary carrying nothing.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def handed_resolvers(resolvers: list[str]) -> list[Notice]:
    """What a launch says about the nameservers the proxy is being given.

    Silent when there are some, because a proxy that can resolve is a proxy
    behaving as declared. Loud when there are none, because that is the state
    the whole field exists to escape: the proxy falls back to the engine's own
    resolv.conf, whose first entry on an internal network answers "no such
    name" for every public destination and hides every server after it.
    """
    if resolvers:
        return []
    return [
        Notice(
            text=(
                "No nameserver this host names is reachable from a container "
                "— they are all loopback — so the proxy keeps whatever the "
                "engine gives it."
            ),
            urgency="warning",
        ),
        Notice(
            text=(
                "On an internal network that is the network's own resolver, "
                "which refuses public names authoritatively and hides every "
                "server after it. Declare `resolvers` on the image's egress "
                "if the proxy turns out unable to reach the world."
            ),
            urgency="detail",
            indent=1,
        ),
    ]


def default_route(table: str) -> str:
    """The default route in a kernel routing table, read as the kernel writes it.

    ``/proc/net/route`` rather than ``ip route``, because the proxy image
    carries no ``iproute2`` and a probe that needed one would report a
    missing tool as a missing route -- the same shape of false answer this
    module keeps finding. A file every container has cannot be absent for a
    reason unrelated to the question.

    The destination and gateway are little-endian hexadecimal in the kernel's
    own byte order, which :mod:`ipaddress` and :meth:`int.from_bytes` read
    between them; a default route is the row whose destination is all zeroes.
    """
    for row in table.splitlines()[1:]:
        columns = row.split()
        if len(columns) < 3 or columns[1] != "00000000":
            continue
        gateway = IPv4Address(int.from_bytes(bytes.fromhex(columns[2]), "little"))
        return f"via {gateway} on {columns[0]}"
    return ""


def proxy_log(name: str, engine: ContainerEngine, lines: int = 400) -> str:
    """What a proxy container said, with its own errors kept above the traffic.

    Both streams, because squid splits them: its access log is configured onto
    stdout and everything about why it would not start, or why one request
    could not be completed, onto stderr. Reading one of the two is reading the
    half that is empty in exactly the case this is for.

    They are separated again on the way out, and that is not cosmetic. A busy
    proxy writes one access line per request and a handful of error lines
    across its whole life, so a plain tail is twenty rows of traffic and none
    of the diagnosis -- five hundred bytes of `TCP_TUNNEL/503` repeating
    while the line saying *why* sits above the window.
    """
    try:
        spoken = sh.Command(engine.binary)(
            "logs", "--tail", str(lines), name, _err_to_out=True
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return ""
    said = str(spoken).strip().splitlines()
    traffic = [line for line in said if "TCP_" in line or "TAG_" in line]
    return "\n".join(
        [*[line for line in said if line not in traffic][-12:], *traffic[-6:]]
    )


def departed(
    egress: SessionEgress, project: str, engine: ContainerEngine
) -> list[Notice]:
    """Account for a proxy this launch is about to replace, then take it away.

    Two things arrive here. One died, and the evidence is reading why: a
    proxy run with ``--rm`` that exits on its configuration removes itself on
    the way out, leaving a launch reporting a boundary that is not there and
    a session reading the absence as a name that does not resolve. The
    other is running and
    was started from a declaration that has since moved, which is the
    counterpart to rebuilding a stale image and is a replacement rather than
    a failure.

    Removal happens here rather than being left to the operator because the
    name is what blocks the restart, and a launch that refused on a name
    collision would be reporting the collision rather than the cause.
    """
    name = egress.proxy_name(project)
    try:
        state = str(
            sh.Command(engine.binary)("inspect", "--format", "{{.State.Status}}", name)
        ).strip()
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return []
    spoken = proxy_log(name, engine)
    sh.Command(engine.binary)("rm", "--force", name, _ok_code=list(range(256)))
    # Two ways to arrive here and they are not the same news. A proxy that
    # died left the last session with no way out and its log says why; one
    # that is running was started from a declaration that has since moved,
    # which is a replacement rather than a failure and has nothing to explain.
    if state == "running":
        return [
            Notice(
                text=(
                    f"{name} was started from an older declaration of this "
                    "boundary, so it is being replaced. A session reaching it "
                    "would have got the policy, the resolvers or the image "
                    "this project no longer declares."
                ),
                urgency="progress",
            )
        ]
    return [
        Notice(
            text=(
                f"The previous {name} was {state} rather than running, so the "
                "last session it was meant to carry had no way out. Replacing "
                "it; what it said before it stopped:"
            ),
            urgency="warning",
        ),
        *[
            Notice(text=line, urgency="detail", indent=1)
            for line in (spoken.splitlines() or ["nothing at all"])
        ],
    ]


def proxy_matches(name: str, declaration: str, engine: ContainerEngine) -> bool:
    """Whether a running proxy was started from the declaration in force now.

    The same question :func:`image_matches` asks of an image, asked of the
    proxy because a change to the declaration reaches one no better. A proxy
    is started once and the declaration goes on moving -- the policy, the
    resolvers, the pinned image -- so every later edit lands in the
    repository and never in the container a session reaches. Unasked, a
    ``--dns`` flag added and a launch run leave the proxy running exactly as
    it was, with the launch reporting the boundary it was supposed to have.

    A proxy carrying no label says nothing about what started it and is
    treated as stale, for the reason an unlabelled image is: replacing it
    costs a second, and trusting it costs a session behind a boundary nobody
    can identify.
    """
    try:
        labelled = sh.Command(engine.binary)(
            "inspect",
            "--format",
            f'{{{{index .Config.Labels "{PROXY_LABEL}"}}}}',
            name,
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return str(labelled).strip() == digest.text(declaration)


def attached(name: str, network: str, engine: ContainerEngine) -> bool:
    """Whether this container is on that network, asked of the engine.

    The third piece of the egress state, and the one a launcher is most
    likely to assume rather than ask. A proxy is *running* and a proxy is
    *reachable from the session's network* are different facts that come
    apart in ordinary ways: the operator removes the network while the proxy
    stays up, or the connect half of a two-command start fails after the run
    half succeeded. Either leaves a proxy answering ``true`` to every
    question a launcher thought to ask, on a network the session is not on.

    What that costs is worth spelling, because it is not a timeout. The
    session resolves ``egress`` through the internal network's resolver,
    which has no record of a container that never joined -- so every request
    fails at DNS, and the runtime reports it as the operator's internet or
    DNS being broken. Nothing in that sentence is true, and nothing in it
    mentions a proxy.
    """
    try:
        found = sh.Command(engine.binary)(
            "inspect",
            "--format",
            "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}",
            name,
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return network in str(found).split()


def proxy_address(egress: SessionEgress, project: str, engine: ContainerEngine) -> str:
    """Where the proxy sits on the session's network, asked after it joined.

    The one fact the session's environment cannot be assembled without, and
    the one nothing could hold in advance: it is assigned when the container
    joins the network. Reaching for a name instead is what put a resolver on
    an internal network and left the proxy unable to resolve anything, so the
    address is asked for here rather than designed around.
    """
    try:
        found = sh.Command(engine.binary)(
            "inspect",
            "--format",
            f'{{{{with index .NetworkSettings.Networks "{egress.network_name(project)}"}}}}'
            "{{.IPAddress}}{{end}}",
            egress.proxy_name(project),
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return ""
    return str(found).strip()


def start_egress(
    egress: SessionEgress, project: str, engine: ContainerEngine, root: Path
) -> str:
    """Bring up the internal network and the proxy bridged out of it.

    Idempotent, and idempotent one piece at a time rather than as a whole:
    the network can outlive a proxy the operator stopped, the proxy can
    outlive the network it was attached to, and a run that treated any pair
    of the three as one fact would leave a session attached to a network with
    nothing on the far side of it -- which is the failure mode the filtered
    posture exists to avoid, arrived at by the launcher itself.

    The attachment is the piece an assumption skips. A check answering as
    soon as the proxy is *running* leaves one that has lost its place on the
    internal network -- or never taken one, the connect half of the start
    having failed after the run half succeeded -- found running and left
    exactly as it is, on every launch afterwards. :func:`attached` is what
    turns that from a permanent state into a repair.

    The rendered configuration is written into the checkout's scratch
    directory rather than piped in, for the reason :func:`build_image` writes
    the Dockerfile there: what the proxy is enforcing should be readable as a
    file rather than reconstructed from a declaration and a memory of which
    version was running.
    """
    if not egress.filtered():
        return ""
    client = sh.Command(engine.binary)
    # Read here rather than declared, for the reason the terminal handoff and
    # the container client are: this is a fact about the machine, and the
    # declaration it would otherwise sit in is hashed into the ownership
    # digest. A launch is the only place that can answer it.
    resolvers = egress.resolvers_for(host_resolv_conf())
    declaration = egress.declaration(resolvers)
    network = egress.network_name(project)
    # The network first, because rebuilding it takes the proxy with it — a
    # network with a container attached refuses to go, and every posture the
    # network carries is one the proxy inherited when it joined.
    if network_present(network, engine) and not network_matches(
        network, declaration, engine
    ):
        Notice(
            text=(
                f"Rebuilding {network} and its proxy because the network "
                "configuration changed."
            ),
            urgency="progress",
        ).say()
        for argv in egress.teardown_arguments(project):
            # Every exit code is acceptable: removing a piece already gone
            # reports an error naming exactly the absence this wanted.
            client(*argv, _ok_code=list(range(256)))
    if not network_present(network, engine):
        client(*egress.network_arguments(project, digest.text(declaration)))
    if running(egress.proxy_name(project), engine) and proxy_matches(
        egress.proxy_name(project), declaration, engine
    ):
        if attached(egress.proxy_name(project), egress.network_name(project), engine):
            return proxy_address(egress, project, engine)
        # Connecting a running container is the repair rather than restarting
        # it, because the proxy is shared: another checkout's session may be
        # reaching the network through this same container right now, and
        # taking it down to fix an attachment it already has would break a
        # session that was working to mend one that was not.
        try:
            client(*egress.connect_arguments(project))
        except sh.ErrorReturnCode as error:
            raise LaunchRefused(
                f"Could not attach proxy {egress.proxy_name(project)} to network "
                f"{egress.network_name(project)}. Launch stopped.\n"
                f"Error: {error.stderr.decode('utf-8', 'replace').strip()}\n"
                "Inspect `uv run lup-devtools harness egress` before retrying."
            ) from error
        return proxy_address(egress, project, engine)
    # A proxy that is here and not being kept is one this has to account for
    # before it goes: dead, in which case its log is the only record of why,
    # or started from a declaration that has since moved. Either way the name
    # is taken, so `run --name` would refuse and the refusal would name the
    # collision rather than the cause.
    for notice in departed(egress, project, engine):
        notice.say()
    scratch = root / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    configuration = scratch / "egress.conf"
    configuration.write_text(egress.enforced().render())
    for notice in handed_resolvers(resolvers):
        notice.say()
    try:
        client(
            *egress.proxy_arguments(
                project, configuration, resolvers, digest.text(declaration)
            )
        )
        client(*egress.connect_arguments(project))
    except sh.ErrorReturnCode as error:
        raise LaunchRefused(
            f"Could not start or connect proxy {egress.proxy_name(project)}. "
            "Launch stopped.\n"
            f"Error: {error.stderr.decode('utf-8', 'replace').strip()}\n"
            f"Proxy configuration: {configuration}\n"
            "Inspect `uv run lup-devtools harness egress` before retrying."
        ) from error
    settled(egress, project, engine, configuration)
    return proxy_address(egress, project, engine)


def settled(
    egress: SessionEgress,
    project: str,
    engine: ContainerEngine,
    configuration: Path,
    grace: float = 2.0,
) -> None:
    """Confirm the proxy is still up a moment after being told to start.

    ``run --detach`` answers whether the container was *created*, which is a
    different question from whether the program in it is still running, and
    the difference is the whole of the hazard. Squid reads its
    configuration at startup and exits on a line it will not accept, and by
    then the launcher has its zero exit code and has moved on to say the
    boundary is up.

    So the start is waited out rather than believed. ``grace`` is the whole
    window and it is slept through rather than polled, because a proxy that
    comes up and dies a second later would satisfy a poll that stopped at the
    first sight of it running. Paid once per proxy rather than once per
    launch: a later session finds it up and returns before reaching here.
    """
    time.sleep(grace)
    name = egress.proxy_name(project)
    if running(name, engine):
        return
    spoken = proxy_log(name, engine)
    raise LaunchRefused(
        f"Proxy {name} exited within {grace:g}s of starting. Launch stopped.\n"
        f"Check its configuration: {configuration}\n"
        "Proxy log:\n" + (spoken or "No log output was available.")
    )


def record_boundary(
    lease: Lease,
    egress: SessionEgress,
    root: Path,
    devices: DeviceLease = DeviceLease(),
    shared: Sequence[Path] = (),
) -> None:
    """Write down what this session is confined by, for the gate that explains it.

    The mount table is a launch fact and the dispatcher that has to explain a
    refusal was compiled long before it. Nothing else bridges that: the
    dispatcher runs as a bare script with no way to ask the container runtime
    anything, and a refusal it cannot attribute reaches the agent as
    ``Read-only file system`` -- which is a broken disk, not a boundary, and
    is debugged as one.

    Written on every contained launch rather than once, because a lease
    changes when a sibling worktree appears or goes, and a stale table would
    attribute a refusal to a mount that is no longer there. An uncontained
    launch writes nothing and the reader treats absence as "no boundary to
    speak of", which is exactly what it is.

    The devices granted are written beside the mounts, and for the other
    reader: a run that computed inside this container has its provenance in
    this file, and a GPU that was withheld is the difference between a
    result and a run that fell back to the host without saying so.

    The writable mounts are written too, because a read-only shared git
    directory has writable directories bound back inside it and the deepest
    mount is the one that refused; ``shared`` names each repository's shared
    git directory, so a refusal under one is explained as git's rather than
    as a path to declare.
    """
    ledger = mount_table(root)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # Written in place, never replaced: a container already running in this
    # checkout holds the file read-only by its inode.
    ledger.write_text(
        json.dumps(
            {
                "read_only": sorted(lease.read_only.values()),
                "writable": sorted(lease.writable.values()),
                "git_shared": sorted(str(path) for path in shared),
                "write_refusals": list(WRITE_REFUSAL_MARKERS),
                "allowed_hosts": sorted(item.host for item in egress.admits),
                "devices": [device.name for device in devices.granted],
            },
            indent=2,
        )
    )


def host_memory() -> int | None:
    """What this host holds, as the kernel counts its pages; nothing where it cannot say."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return None


class HeldMemory(BaseModel, frozen=True):
    """The bytes a container may hold, and the line its launch says them in."""

    limit: int | None = None
    notices: list[Notice] = []


def held_memory(
    declared: MemoryLimit | None,
    engine: ContainerEngine,
    host_total: Callable[[], int | None] = host_memory,
) -> HeldMemory:
    """The memory limit a container runs under, in bytes, and the line saying it.

    A share is resolved against what the engine says it can hand out, and
    only where it says nothing against the host's own total -- the two part
    exactly where a share would go wrong, an engine inside a virtual machine
    handing out the machine's memory rather than the laptop's. A share
    neither can resolve refuses the launch rather than dropping the limit,
    because a declared bound that silently is not there is the one failure a
    boundary may not have.
    """
    if declared is None:
        return HeldMemory()
    total = engine.memory_total() or host_total()
    if total is None and declared.percent is not None:
        raise LaunchRefused(
            f"The memory limit is {declared.percent}% of what the engine can "
            "hand out, and neither the engine nor the host says how much that "
            "is. Launch stopped. Declare an amount instead, such as 12GiB."
        )
    limit = declared.resolved(total or 0)
    return HeldMemory(
        limit=limit,
        notices=[
            Notice(
                text=f"Memory: {declared.described(total or limit)}",
                urgency="boundary",
            )
        ],
    )


def readied_nested(root: Path, declared: Sequence[NestedRepository]) -> list[Notice]:
    """Ready each declared repository inside the checkout for its hold, on the host.

    One marked ``create`` and absent is initialized here, so no session is
    the one that writes its configuration; one absent and unmarked is said
    and held by nothing. Each present one has its pointers verified as every
    worktree's are, so a ``commondir`` a session planted in it is refused
    rather than held in place, and its ``hooks/`` made where ``git init``
    made none, since a bind whose source is missing refuses the container.
    Only a repository keeping its own ``.git`` directory is held: one whose
    ``.git`` is a pointer reads its configuration from wherever that leads.
    """

    def readied(nested: NestedRepository) -> Iterator[Notice]:
        checkout = root / nested.path
        if not (checkout / ".git").exists():
            if not nested.create:
                yield Notice(
                    text=(
                        f"Nested repository {nested.path}: not there, so nothing "
                        "held; a session that makes it writes its configuration "
                        "unheld"
                    ),
                    urgency="boundary",
                )
                return
            checkout.mkdir(parents=True, exist_ok=True)
            git("init", "-q", str(checkout))
            yield Notice(
                text=f"Nested repository {nested.path}: initialized on the host",
                urgency="boundary",
            )
        # Its own directory, by where it is: asking git for the repository
        # would follow the very `commondir` a session could have planted.
        directory = checkout / ".git"
        if not directory.is_dir():
            raise LaunchRefused(
                f"Nested repository {nested.path}: {directory} is not a git "
                "directory of its own, so nothing says where its configuration "
                "is. Declare a repository kept with its own `.git` directory."
            )
        if drifts := pointer_drift(directory):
            raise LaunchRefused(refusal(drifts))
        (directory / "hooks").mkdir(exist_ok=True)

    return [notice for nested in declared for notice in readied(nested)]


def held_lease(
    root: Path,
    lease: Lease,
    nested: Sequence[NestedRepository] = (),
    trees: Sequence[Path] = (),
) -> Lease:
    """What a container at ``root`` mounts: ``lease``, its launch record held, every hold rooted.

    The launch record is :func:`~lup.launch.preflight.launch_record`: what
    the gates believe, written only on the host. It is held only where
    ``lease`` would let it be written, so a hold narrows what a container
    writes and never widens what it reaches -- a lease writing nothing holds
    nothing. Then :func:`~lup.sandbox.rail.rooted` pins the directories
    between each hold and its writable mount, this record's and the git
    configuration's alike, so none can be moved out from under its hold.

    Only this checkout's record. A sibling's is written by the session in it,
    and a mount inside a sibling would make it a checkout nobody can remove
    from inside -- the landing workflow this lease keeps siblings writable
    for.

    ``nested`` are the repositories the wall declares inside the checkout,
    each held as a plain checkout's own is -- ``config`` and ``hooks/`` --
    where it is there to hold; :func:`readied_nested` readies them first.

    ``trees`` are the generated trees the runtime runs from, where the wall
    holds them: each directory whole, so a host regeneration renaming a file
    inside it leaves the hold standing, and each lone file as itself.
    """
    # lup: defer: a sibling worktree's launch record stays writable here, so a
    # contained session can rewrite the ledger a session in that sibling reads
    # through its own hold -- its destination policies and writable roots.
    # Holding it would pin a mount point inside the sibling; closing it needs
    # the record kept outside the checkout, or read against what its launch
    # handed the runtime at start
    anchors = [
        root / repository.path / ".git" / name
        for repository in nested
        for name in ("config", "hooks")
        if (root / repository.path / ".git" / name).exists()
    ]
    held = [
        path
        for path in [*launch_record(root), *anchors, *trees]
        if lease.writable_at(path)
    ]
    return rooted(merged([lease, Lease(read_only=same_path(held))]))


class Spoken(BaseModel, frozen=True):
    """What one command inside a container said, and whether it worked.

    Two fields rather than one because reading the second out of the first is
    what a first draft did and it was wrong within the hour: a lookup that
    failed and a lookup that succeeded were told apart by sniffing the answer
    for a phrase, and the phrase was one of three the failure could produce.
    Whether a command worked is known exactly at the moment it runs, and
    carrying it is cheaper than recovering it.
    """

    worked: bool = False
    text: str = Field(default="", description="Its own words, either way")


class NetworkLeg(BaseModel, frozen=True):
    """One network a container is on, and whether it offers a way off it.

    The gateway is the field this exists for. A proxy is the only process
    meant to be on both sides of the boundary, and "on two networks" says
    nothing about whether either of them routes anywhere -- an ``--internal``
    network is precisely one that does not. A report listing membership
    without it describes a container that looks correctly attached and can
    reach nothing.
    """

    network: str = Field(description="What the network is called")
    address: str = Field(default="", description="The container's address on it")
    gateway: str = Field(
        default="", description="What it routes through there, empty for internal"
    )

    def sentence(self) -> str:
        """This leg as one line, saying plainly when it leads nowhere."""
        through = f"via {self.gateway}" if self.gateway else "no gateway"
        return f"{self.network} at {self.address or 'no address'} — {through}"


class EgressState(BaseModel, frozen=True):
    """What this project's egress infrastructure is actually doing, asked.

    What the *declaration* says -- filtered, through this proxy, these
    denials -- is not evidence that any of it is so, and only the engine can
    say whether it is. A session opening behind a proxy whose name it cannot
    resolve reports every request as the operator's internet being down, and
    a launch printing the declaration alone gives nobody a way to tell.

    Held as a model rather than printed as it is gathered so the verdict can
    be computed from the whole picture. Which of these facts is wrong decides
    where a reader goes next, and no single one of them says on its own.
    """

    network: str = Field(description="The internal network this project declares")
    proxy: str = Field(description="The container bridged out of it")

    network_exists: bool = False
    dns_enabled: bool = False
    proxy_exists: bool = False
    proxy_running: bool = False
    proxy_status: str = Field(
        default="", description="What the engine says about the container's state"
    )
    attached: bool = False
    aliases: list[str] = Field(
        default=[], description="Names the proxy answers to on that network"
    )
    address: str = Field(default="", description="Its address there, if it has one")
    log: str = Field(
        default="",
        description=(
            "What the proxy last said. For a stopped one that is why it "
            "stopped; for a running one it is the access and error lines "
            "behind whatever a session is seeing, which is where a refusal "
            "and a failure to reach an origin are told apart -- squid "
            "answers a denied request and an unreachable one with different "
            "codes and says which in its own log"
        ),
    )
    legs: list[NetworkLeg] = Field(
        default=[],
        description=(
            "Every network the proxy is on, with what each routes through. "
            "The proxy is the one process meant to be on both sides, so this "
            "is where a proxy that joined the session's network and lost its "
            "way out of the other one shows up -- a state in which every "
            "question asked so far answers correctly and nothing works"
        ),
    )
    route: str = Field(
        default="",
        description=(
            "The default route the kernel inside the proxy would actually "
            "use, which is the question a per-network gateway does not "
            "answer. Only one of a container's networks provides it and "
            "netavark installs none for an internal one, so a proxy on two "
            "networks that each *record* a gateway can hold no default route "
            "at all — and every packet it sends to a public address then "
            "fails instantly rather than timing out. Read out of "
            "``/proc/net/route``, which is a file rather than a tool: the "
            "proxy image carries no ``ip``, and a probe needing one would "
            "report a missing tool as a missing route"
        ),
    )
    started_with: str = Field(
        default="",
        description=(
            "The command the engine records this container as having been "
            "created with, read back rather than reconstructed. What a "
            "launcher *asked* for and what a container *has* are two things, "
            "and they part in ordinary ways: a flag added to the arguments is "
            "never applied when a running proxy is reused, and one that is "
            "applied can be undone by a later `network connect`. Printing the "
            "intent shows the intent in both cases"
        ),
    )
    resolver: str = Field(
        default="",
        description=(
            "The nameservers the proxy itself is using. A proxy is the one "
            "process that has to resolve the *destination*, and it does that "
            "on its own network rather than the session's -- so a session "
            "that resolves the proxy perfectly can still be answered 503 by "
            "a proxy that cannot resolve anything"
        ),
    )
    answers_locally: bool = Field(
        default=False,
        description=(
            "Whether the proxy can resolve the alias its own network's "
            "resolver holds. Beside :attr:`reached` this is the pair that "
            "matters: a resolver chain answering internal names and refusing "
            "public ones is being consulted and stopping early, which is a "
            "different fault from one that cannot be reached at all and has "
            "a different repair"
        ),
    )
    reached: bool = Field(
        default=False,
        description=(
            "Whether the proxy turned a public name into an address of its "
            "own. Separate from :meth:`resolvable`, which is the *session's* "
            "question: the two happen on different networks and were "
            "conflated by having only one, so a session resolving `egress` "
            "perfectly and a proxy resolving nothing both read as the same "
            "kind of failure"
        ),
    )
    upstream: str = Field(
        default="",
        description=(
            "What the proxy got when it looked a public name up, or what "
            "went wrong. The question a 503 on CONNECT poses and nothing "
            "else here answers: squid returns it both for a name it could "
            "not resolve and for an origin it could not reach, and the "
            "repairs for those are not the same"
        ),
    )

    def addressable(self) -> bool:
        """Whether a session on this network has a proxy it can send to.

        Every clause here is a candidate that earned its place, and the one
        left out is instructive: whether an *alias* resolves is a question
        that can be answered yes by a network whose resolver then
        refuses every public name the proxy needs. Addressing the proxy
        where it is removes that question rather than answering it.

        A proxy on the engine's bridge and not on this network is invisible
        to a session on it, and one with no address there is the same thing
        said in the engine's own terms.
        """
        return (
            self.network_exists
            and self.proxy_running
            and self.attached
            and bool(self.address)
        )

    def notices(self) -> list[Notice]:
        """This state as a reader needs it: each fact, then what it adds up to.

        Every fact printed rather than only the failing one, because which
        combination holds is what decides where to go next -- a network with
        no DNS is a different repair from a proxy that is not on it, and both
        look identical from inside a session.
        """
        state = self.proxy_status or ("running" if self.proxy_running else "absent")
        return [
            Notice(text=f"network {self.network}", urgency="detail"),
            Notice(
                text=f"exists: {self.network_exists}, dns: {self.dns_enabled}",
                urgency="ready" if self.network_exists else "refusal",
                indent=1,
            ),
            Notice(text=f"proxy {self.proxy}", urgency="detail"),
            *(
                [
                    Notice(
                        text=f"started with: {self.started_with}",
                        urgency="detail",
                        indent=1,
                    )
                ]
                if self.started_with
                else []
            ),
            Notice(
                text=f"state: {state}",
                urgency="ready" if self.proxy_running else "refusal",
                indent=1,
            ),
            Notice(
                text=(
                    f"on this network: {self.attached}"
                    + (f" as {', '.join(self.aliases)}" if self.aliases else "")
                    + (f" at {self.address}" if self.address else "")
                ),
                urgency="ready" if self.attached else "refusal",
                indent=1,
            ),
            *(
                [Notice(text="its networks:", urgency="detail", indent=1)]
                if self.legs
                else []
            ),
            *[
                Notice(
                    text=leg.sentence(),
                    urgency="ready" if leg.gateway else "warning",
                    indent=2,
                )
                for leg in self.legs
            ],
            *(
                [
                    Notice(
                        text=("default route: " + (self.route or "none")),
                        urgency="ready" if self.route else "refusal",
                        indent=1,
                    ),
                    Notice(
                        text=f"DNS resolvers: {self.resolver or 'none reported'}",
                        urgency="detail",
                        indent=1,
                    ),
                    Notice(
                        text=f"public DNS lookup: {self.upstream}",
                        urgency="ready" if self.reached else "refusal",
                        indent=1,
                    ),
                    *(
                        [
                            Notice(
                                text=(
                                    "its own network's names resolve: "
                                    f"{self.answers_locally}"
                                ),
                                urgency="detail",
                                indent=1,
                            )
                        ]
                        # Only where a public name failed. This tells two
                        # failures apart and says nothing otherwise: a proxy
                        # given its own nameservers stops holding the internal
                        # network's, so `False` here is what success looks
                        # like and calling it a fault would be a lie.
                        if not self.reached
                        else []
                    ),
                ]
                if self.proxy_running
                else []
            ),
            *(
                [Notice(text="proxy log:", urgency="detail", indent=1)]
                if self.log
                else []
            ),
            *[
                Notice(text=line, urgency="detail", indent=2)
                for line in self.log.splitlines()
            ],
            *self.verdict(),
        ]

    def routes(self) -> bool:
        """Whether the proxy holds a default route the kernel would use.

        Asked of the routing table rather than of the networks, because those
        two answers can part: both legs can record a gateway while the
        container reaches nothing. Only one of a container's networks provides
        the default route, netavark installs none for an internal one, and
        ``podman inspect`` reports a network's ``.1`` address as its gateway
        either way. Membership says yes, metadata says yes, and every packet
        fails instantly.
        """
        return bool(self.route)

    def shadowed(self) -> list[Notice]:
        """Suggest checking resolver responses when only internal DNS succeeds."""
        if not (self.route and self.answers_locally and self.resolver):
            return []
        return [
            Notice(
                text=(
                    "The proxy resolves internal names but its public DNS "
                    "lookup failed. Test the listed nameservers individually "
                    "to identify which one cannot resolve the public name."
                ),
                urgency="detail",
                indent=1,
            )
        ]

    def verdict(self) -> list[Notice]:
        """What the facts above add up to, in the words a session would use.

        Three answers rather than two, and the third is here because writing
        two reproduced the exact failure this whole report exists to catch: a
        headline saying the boundary was fine, standing over a proxy that
        could not resolve anything. Reaching the proxy and the proxy reaching
        the world are separate legs, they fail separately, and a reader told
        only about the first goes looking in the wrong place.
        """
        recovery = Notice(
            text=(
                "To recreate the proxy and network, run "
                "`uv run lup-devtools harness egress --down`, then rerun the launcher."
            ),
            urgency="detail",
            indent=1,
        )
        if not self.addressable():
            return [
                Notice(
                    text=(
                        f"No running proxy with an address was found on {self.network}. "
                        "Check the network and proxy status above."
                    ),
                    urgency="refusal",
                ),
                recovery,
            ]
        if not self.reached:
            return [
                Notice(
                    text=(
                        f"Proxy {self.proxy} has address {self.address}, "
                        "but its public DNS lookup failed."
                    ),
                    urgency="refusal",
                ),
                *(
                    self.shadowed()
                    or [
                        Notice(
                            text=(
                                "The proxy has no default route. Check its "
                                "external network attachment."
                                if not self.routes()
                                else "Check the proxy's DNS error, resolver "
                                "settings and log above."
                            ),
                            urgency="detail",
                            indent=1,
                        )
                    ]
                ),
            ]
        return [
            Notice(
                text=(
                    f"Proxy address: {self.address}; its public DNS lookup succeeded."
                ),
                urgency="ready",
            )
        ]


def egress_state(
    egress: SessionEgress,
    project: str,
    engine: ContainerEngine,
    resolving: str = "api.anthropic.com",
) -> EgressState:
    """Ask the engine what this project's boundary is, rather than what it declares.

    Every field comes off an inspect rather than off a record this launcher
    kept. A launcher that remembered would be reading a file: the operator may
    have removed either piece between launches, a sibling checkout may have
    brought them up, and the state that mattered here -- an alias recorded on
    a network that cannot serve it -- is one nothing would have thought to
    write down.
    """
    client = sh.Command(engine.binary)
    network = egress.network_name(project)
    proxy = egress.proxy_name(project)

    def asked(*words: str) -> str:
        """One inspect, with absence answering empty rather than raising."""
        try:
            return str(client(*words)).strip()
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            return ""

    dns = asked("network", "inspect", "--format", "{{.DNSEnabled}}", network)
    status = asked("inspect", "--format", "{{.State.Status}}", proxy)
    joined = asked(
        "inspect",
        "--format",
        "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}",
        proxy,
    ).split()
    legs = [
        NetworkLeg(
            network=leg,
            address=asked(
                "inspect",
                "--format",
                f'{{{{with index .NetworkSettings.Networks "{leg}"}}}}'
                "{{.IPAddress}}{{end}}",
                proxy,
            ),
            gateway=asked(
                "inspect",
                "--format",
                f'{{{{with index .NetworkSettings.Networks "{leg}"}}}}'
                "{{.Gateway}}{{end}}",
                proxy,
            ),
        )
        for leg in joined
    ]
    names = asked(
        "inspect",
        "--format",
        f'{{{{with index .NetworkSettings.Networks "{network}"}}}}'
        "{{range .Aliases}}{{.}} {{end}}{{end}}",
        proxy,
    ).split()
    address = asked(
        "inspect",
        "--format",
        f'{{{{with index .NetworkSettings.Networks "{network}"}}}}'
        "{{.IPAddress}}{{end}}",
        proxy,
    )

    def within(*words: str) -> Spoken:
        """One command run inside the proxy, answering in its own words either way.

        Asked of the proxy rather than of the session, because the two
        resolve on different networks and the interesting failure is the
        proxy's: it is the one process that has to turn the *destination*
        into an address, and a session can reach it perfectly while it
        reaches nothing.

        A failure returns what the engine said rather than the empty string
        this module's other probes use. Empty is the right answer for an
        inspect, where absence is the fact being reported; here it would put
        a name that did not resolve and a program the image does not carry
        into one indistinguishable answer.
        """
        if status != "running":
            return Spoken()
        try:
            return Spoken(worked=True, text=str(client("exec", proxy, *words)).strip())
        except sh.CommandNotFound:
            return Spoken(text=f"{engine.binary} is not on PATH")
        except sh.ErrorReturnCode as failure:
            said = failure.stderr.decode("utf-8", "replace").strip()
            return Spoken(
                text=said or f"exited {failure.exit_code} with nothing to say"
            )

    # `getent` exits 2 on a name it cannot find and says nothing, so the
    # question is carried into the answer and the verdict comes off `worked`.
    looked = within("getent", "hosts", resolving)
    # Asked only where the session's network runs a resolver at all, which
    # is off by default and for this exact reason: a resolver there answers
    # names on that network and refuses every other one authoritatively, and
    # glibc stops at the first authoritative answer -- so it stands in front
    # of the one server that could resolve a public name. Where a project
    # turns it back on, this pair is what tells that shadowing apart from a
    # resolver chain that is simply not answering.
    known = (
        within("getent", "hosts", egress.proxy_name(project))
        if egress.resolves_names
        else Spoken()
    )
    routed = default_route(within("cat", "/proc/net/route").text)
    # Podman records it; Docker does not, and an empty answer there is an
    # absence of the field rather than of the container.
    created = asked("inspect", "--format", '{{join .Config.CreateCommand " "}}', proxy)
    nameservers = " ".join(
        line.split()[1]
        for line in within("cat", "/etc/resolv.conf").text.splitlines()
        if line.startswith("nameserver ")
    )
    return EgressState(
        network=network,
        proxy=proxy,
        resolver=nameservers,
        legs=legs,
        route=routed,
        started_with=created,
        reached=looked.worked,
        answers_locally=known.worked,
        upstream=f"{resolving} -> {looked.text or 'no answer'}",
        network_exists=bool(dns),
        dns_enabled=dns == "true",
        proxy_exists=bool(status),
        proxy_running=status == "running",
        proxy_status=status,
        attached=network in joined,
        aliases=names,
        address=address,
        log=proxy_log(proxy, engine),
    )


def report_egress(egress: SessionEgress, root: Path, down: bool) -> None:
    """Say what network boundary this project has, and remove it when asked.

    Removal names each piece separately and tolerates a piece already gone:
    the operator may have stopped the proxy by hand, and a teardown that
    failed on the half already in the state it wanted would leave the other
    half standing while reporting an error.
    """
    project = root.name
    client = detected_client()
    if client is None:
        Notice(
            text="No working Docker or Podman client was found. Network status is unknown.",
            urgency="warning",
        ).say()
        return
    for argv in egress.teardown_arguments(project) if down else []:
        # Every exit code is acceptable here and nowhere else: removing a
        # container that is already gone reports an error naming exactly the
        # absence this call was asked to produce.
        sh.Command(client.binary)(*argv, _ok_code=list(range(256)))
    if down:
        Notice(text="Removed.", urgency="ready").say()
        return
    for notice in egress.notice(project):
        notice.say()
    if not egress.filtered():
        return
    # What is declared, then what is running. The first alone comes apart
    # from the second in the way that matters: the notice above says
    # traffic is filtered through a proxy, which is true, while the session
    # cannot resolve the name it addresses that proxy by.
    for notice in egress_state(egress, project, client.engine()).notices():
        notice.say()


def network_matches(name: str, declaration: str, engine: ContainerEngine) -> bool:
    """Whether this network was created under the declaration in force now.

    The third thing a launch would otherwise reuse whatever the declaration
    says, and the one that would swallow the check on the other two. A
    network is created once and outlives every launch, so the posture it
    was first created under is the posture it keeps -- and the flag that
    stops its resolver shadowing the proxy's never reaches a machine whose
    network already exists.

    An unlabelled network says nothing about the posture it was created under
    and counts as stale, for the reason an unlabelled image and proxy do.
    """
    try:
        labelled = sh.Command(engine.binary)(
            "network",
            "inspect",
            "--format",
            f'{{{{index .Labels "{PROXY_LABEL}"}}}}',
            name,
        )
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return str(labelled).strip() == digest.text(declaration)


def network_present(name: str, engine: ContainerEngine) -> bool:
    """Whether this network exists, asked of the engine rather than guessed."""
    try:
        sh.Command(engine.binary)("network", "inspect", name)
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return False
    return True


def build_image(
    image: Image,
    manifest: Manifest,
    tag: str,
    engine: ContainerEngine,
    root: Path,
    shown: int = 4,
    privileges: SessionPrivileges = SessionPrivileges(),
) -> None:
    """Build this project's image from the declaration, in the open.

    The Dockerfile is written into the checkout's scratch directory rather
    than piped in, so an operator who wants to know what was built can read
    the file the build actually used instead of reconstructing it.

    Every line the build writes is kept, and the terminal shows the last
    ``shown`` of them in place. The distinction matters more than it looks:
    the *file* is complete, so nothing a reader might need was thrown away,
    and the *display* is a window onto it rather than a shortened copy. A
    cold build here is hundreds of package lines, and putting them in the
    scrollback buries whatever the launch said before them -- which is
    exactly the boundary report a session most needs to have read.

    Two lines are printed before anything runs, because a build with a live
    region and no header is a session that appears to have stopped: what is
    being built, and where its full output is being written.
    """
    scratch = root / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    dockerfile = scratch / "agent.Dockerfile"
    rendered = image.dockerfile(manifest, privileges)
    dockerfile.write_text(rendered)
    log = scratch / "agent-build.log"
    argv = [
        "build",
        "-t",
        tag,
        "--label",
        f"{DECLARATION_LABEL}={digest.text(rendered)}",
        "-f",
        str(dockerfile),
        "--build-arg",
        f"UID={root.stat().st_uid}",
        "--build-arg",
        f"GID={root.stat().st_gid}",
        str(scratch),
    ]
    Notice(text=f"Building {tag} from {dockerfile}", urgency="progress").say()
    Notice(text=f"Build log: {log}", urgency="artifact").say()
    console = Console()
    recent: deque[str] = deque(maxlen=shown)
    with log.open("w", encoding="utf-8") as handle:
        # Transient, so the window closes when the build does and the
        # scrollback keeps the two header lines rather than a frozen tail of
        # whatever the last layer happened to say. Off entirely where there
        # is no terminal to redraw -- a CI log is a file, and a file wants
        # every line in order, which is what the handle already gets.
        live = Live(console=console, transient=True) if console.is_terminal else None

        def record(chunk: str) -> None:
            """Keep every line, and show the last few."""
            handle.write(chunk)
            spoken = chunk.rstrip()
            if not spoken or live is None:
                return
            recent.append(spoken)
            live.update(Text("\n".join(recent), style="dim"))

        try:
            with live or nullcontext():
                sh.Command(engine.binary)(
                    *argv, _out=record, _err_to_out=True, _out_bufsize=1
                )
        except sh.ErrorReturnCode as error:
            for line in recent:
                print(line)
            raise LaunchRefused(
                f"Image build failed: {tag} (exit code {error.exit_code}).\n"
                f"Dockerfile: {dockerfile}\nFull build log: {log}\n"
                "Fix the error in the build log, then rerun the launcher."
            ) from error


def fleet_notice(accessible: list[AccessibleRoot]) -> list[Notice]:
    """What this session can reach beyond its own checkout, and in which mode.

    Said because a mount nobody announced is one that gets debugged rather
    than used: an operator who does not know another project is open will
    work around its absence, and one who does not know it is read-only reads
    the refusal as a broken checkout.

    Whole paths rather than the registry's short names. The name is what the
    symlink under `refs/` is called, and the thing a reader has to be able to
    check is which directory on their machine this session can now write.
    """
    if not accessible:
        return []
    return [
        Notice(
            text="Reachable projects: "
            + ", ".join(
                f"{item.path} {'read-write' if item.writable else 'read-only'}"
                for item in sorted(accessible, key=lambda item: item.path)
            ),
            urgency="boundary",
        )
    ]


def pruning_notice(refused: list[Path]) -> list[Notice]:
    """What to say when a repository would not take the prune guard.

    Not a refusal to launch. The mounts are the guard that matters and they
    stand either way; this is the one behind them, and it covers the case
    where they are subtly wrong. Silence would leave a session one `git gc`
    away from removing a worktree's administrative state in a repository
    nobody here owns, with nothing having said so.
    """
    if not refused:
        return []
    return [
        Notice(
            text=", ".join(str(path) for path in refused)
            + " would not take `gc.worktreePruneExpire`, so a `git gc` in this "
            "session could remove the administrative state of a worktree it "
            "cannot see. The mounts still hold; this is the guard behind them.",
            urgency="boundary",
        )
    ]


def preparation_notice(refused: list[str]) -> list[Notice]:
    """What to say when a shared git directory could not be readied for its bind.

    Not a refusal to launch: `config` and `hooks/` are held read-only either
    way. What an unready directory costs is inside the session -- a ref
    deletion with no writable `packed-refs`, or a directory git makes on
    first use that cannot be made under a read-only parent -- and a session
    meeting that with nothing said would read it as a broken repository.
    """
    if not refused:
        return []
    return [
        Notice(
            text="Could not ready the shared git directory for its read-only bind ("
            + "; ".join(refused)
            + "). Deleting a branch or a first-use git directory may fail inside "
            "this session; the next launch tries again.",
            urgency="boundary",
        )
    ]


def held_environments(
    root: Path,
    accessible: list[AccessibleRoot],
    name: str,
    cache: Path | None = None,
) -> dict[Path, Path]:
    """A host directory per project root the session may sync, created here.

    The session's own checkout and every root declared writable -- each
    worktree of one that is a bare repository, which has no tree of its own
    to sync in, so a clone a launch mounts whole holds its environments where
    its projects are rather than in its git directory. A read-only
    root is left out deliberately rather than skipped for tidiness: `uv sync`
    writes, so a root nobody may write is one no environment can be built in,
    and binding a directory inside it would advertise a place to sync that
    refuses the sync when it is tried.

    Created on the host, which is the whole reason the launcher does this
    rather than the declaration: a bind mount arrives owned by whoever owns
    its source, so making these as the operator is what hands the session a
    directory its own uid can write. A source that does not exist is one the
    engine refuses the entire container for, so this runs before any argv is
    assembled rather than lazily beside it.

    The mount *point* is made here too, at ``name`` inside each root, and that
    is the half worth stating. An engine asked to bind onto a path that is not
    there creates it under its own mapping: on rootless podman 6.1.0 with
    ``--userns=keep-id``, the directory left in the checkout afterwards
    belongs to uid 100000 -- `nobody` to the operator, who can then neither
    write it nor sync into it, and whose own `uv` fails on `Permission
    denied` for a path in their own tree. Made by the launcher first, it
    belongs to the operator and stays writable. It outlives the container
    either way; this decides whose it is.

    So a session leaves one empty directory per root behind, at that name.
    This repository gitignores it; a registered project that does not will
    show it as untracked, which is the visible cost of keying the environment
    per project, and cheaper than the sync that would otherwise land in the
    `.venv` the operator is using.
    """

    def prepared(project: Path) -> Path:
        """One root's directory and its mount point, before anything binds them."""
        directory = claimed(project, cache)
        (project / name).mkdir(exist_ok=True)
        return directory

    roots = [
        root,
        *[
            tree
            for item in accessible
            if item.writable
            for tree in working_trees(item.path)
        ],
    ]
    return {project: prepared(project) for project in dict.fromkeys(roots)}


def contained_argv(
    image: Image,
    manifest: Manifest,
    root: Path,
    editor_rendezvous: Path | None,
    credential: HandedLogin | None,
    login: ProviderLogin,
    engine: ContainerEngine | None = None,
    streams: SessionStreams = "terminal",
    banner: Banner | None = None,
    sentinels: LaunchSentinels = LaunchSentinels(),
    inherited_environment: list[str] | None = None,
    accessible: list[AccessibleRoot] = [],
    lease: Lease | None = None,
    devices: list[Device] = [],
    home_seed: HomeSeedPlaces | None = None,
    privileges: SessionPrivileges = SessionPrivileges(),
    nested: Sequence[NestedRepository] = (),
    memory: MemoryLimit | None = None,
    trees: Sequence[Path] = (),
    overlays: Mapping[Path, str] | None = None,
) -> list[str]:
    """The argv that opens a session in this project's container.

    ``credential`` is the host login offered to the repository's config
    volume, and whose it is. Every session running on that volume shares
    the file it lands in, so one whose account differs from the one the
    volume was last handed, while a container holding the volume runs, is
    settled before anything is built: refused, handed anyway, or withheld,
    as :func:`~lup.launch.config_volume.settle_handoff` says. What was handed
    is recorded for the next start to compare against.

    Refuses rather than degrades when no container client answers: a launch
    that asked for the boundary and silently ran without one is exactly the
    failure the boundary exists to make impossible.

    A client that answers and cannot drive the engine behind it is refused
    the same way and in different words, because the two failures send an
    operator to opposite places: one to install a runtime, the other to stop
    pointing the one they have at somebody else's socket.

    ``banner`` collects what this has to say instead of printing it, so a
    caller that has its own lines to add -- whether the runtime checks passed,
    where the transcript went -- says the whole opening once in one order. A
    caller with nothing to add passes nothing and each line is printed as it
    is produced, which is what a probe wants: its notices interleave with the
    build they describe rather than arriving after it.

    ``lease`` is the mount table this container runs under, defaulting to the
    one a session gets over its own repository. A worker passes
    :func:`~lup.sandbox.rail.worker_lease` instead and reaches everything else
    here unchanged -- the same image, egress, credential, identity and
    same-path mounting -- because the only thing that differs between an
    operator's container and a worker's is which checkouts it may write. A
    second builder for that one difference would be a second declaration of
    everything it has in common, free to drift from this one.

    ``devices`` are what this machine grants its sessions and what this
    launch asked for besides, settled by the caller the way ``accessible``
    is: the standing grants come from the same gitignored registry the
    mounts do, so a run's workers hold the devices its session did.

    ``editor_rendezvous`` is where the *host's* editor keeps its session
    lockfiles, and ``None`` asks for no bridge at all -- which is the answer
    for a runtime that declares none, and for an actor, which has nobody at a
    keyboard.

    It is emphatically not derived from the home this launch runs under, and
    that is the distinction the parameter exists to hold. Under ``--profile``
    the launch's home is the profile's, `profiles/<name>/claude-config` in the
    person's lup config home, derived from a name no editor has ever heard of,
    so bridging it would bind a directory nothing writes into: a `--profile`
    session's container would hold the profile's empty `ide` directory at its
    configuration home, and the editor connection would never happen with
    nothing saying why.
    The lockfile is a rendezvous point rather than profile state -- a port and
    a token for one editor window, holding no account and no credential -- so
    which account a session runs under and which editor it talks to are
    independent choices, and only the second decides this.

    ``privileges`` is what the wall grants the session's processes, and
    sudo among them only on a rootless engine: there the container's root is
    an unprivileged user on the host, where on a rootful one it is the
    host's root, held back only by the capabilities dropped -- so that grant
    is refused before anything is built.

    ``nested`` are the repositories the wall declares inside the checkout,
    readied on the host and held as the checkout's own -- see
    :func:`held_lease`.

    ``memory`` is the limit the wall declares, resolved against this
    engine by :func:`held_memory`, ``trees`` the generated trees it holds
    read-only -- see :func:`held_lease` -- and ``overlays`` the files it
    holds read-only over a path in the checkout, keyed by where each is on
    the host.
    """
    said = banner if banner is not None else Banner()
    if engine is not None:
        client = engine
    else:
        found = detected_client()
        if found is None:
            raise LaunchRefused(
                "No working Docker or Podman client was found. Install one to "
                "launch in a container. To run on the host using the runtime's "
                "sandbox, choose `--sandbox inner`."
            )
        if not found.drives_its_server():
            raise LaunchRefused(found.consequence())
        client = found.engine()
    if privileges.sudo:
        if not client.rootless():
            raise LaunchRefused(
                f"sudo was granted, and `{client.binary}` does not run rootless. "
                "Root inside a rootful engine's container is the host's root, "
                "held back only by the capabilities the container drops, so "
                "sudo is granted only on a rootless engine: use rootless Podman "
                "or rootless Docker, or drop sudo from the declaration. Packages "
                "sudo installs vanish with the container either way; declare "
                "them in the image's `tooling` to keep them."
            )
        said.add(
            [
                Notice(
                    text=(
                        f"sudo: the session may become root in its container, "
                        f"which `{client.binary}` runs rootless, so an "
                        "unprivileged user on the host. What it installs vanishes with the "
                        "container; the image's `tooling` keeps a package."
                    ),
                    urgency="boundary",
                )
            ]
        )
    # Before anything is built or started, so a start that would move the
    # sessions running on this repository's volume is refused with nothing of
    # its own left to undo.
    logins = VolumeLogins()
    handoff = (
        settle_handoff(
            credential,
            login,
            state_volume_name(root, login),
            client,
            logins,
            datetime.now(UTC),
        )
        if credential is not None
        else Handoff()
    )
    said.add(handoff.notices)
    bounded = held_memory(memory, client)
    said.add(bounded.notices)
    # Every root this launch mounts, before host git reads any of them -- the
    # lease's own layout questions and the prune guard below both run git
    # there -- and before a broker is started, which a refusal would strand.
    # The lease is settled here for the same reason: a mount over lup's store
    # of trusted repositories refuses the launch before anything starts.
    trust = judged_roots([root, *(item.path for item in accessible)], operator=root)
    for notice in trust.notices:
        print(notice, file=sys.stderr)
    if trust.refusal:
        raise LaunchRefused(trust.refusal)
    # Readied before the lease is read, because the lease binds only the
    # directories that exist: one git or lup makes on first use cannot be
    # made later under the read-only shared directory. A read-only root is
    # not readied -- its whole lease is read-only, and it is not ours to move.
    readied = [root, *(item.path for item in accessible if item.writable)]
    said.add(preparation_notice(prepared_across(readied, SHARED_STATE)))
    said.add(readied_nested(root, nested))
    lease = held_lease(
        root,
        lease if lease is not None else fleet_lease(root, accessible),
        nested,
        trees,
    )
    if exposed := launcher_state_exposure(lease):
        raise LaunchRefused(exposed)
    # Rebound before rendering, so the tag, the build, and the session all
    # read the same resolved copy -- and only they: the declaration the
    # ownership digests hash never carries a resolved version.
    resolution = resolved_agent_clis(image)
    image = resolution.image
    said.add(resolution.said)
    rendered = image.dockerfile(manifest, privileges)
    tag = image_tag(rendered)
    built = not image_matches(tag, rendered, client)
    if built:
        build_image(image, manifest, tag, client, root, privileges=privileges)
    name_for_checkout(tag, checkout_tag(root), client)
    # A build is what leaves an image behind, so it is the moment to sweep:
    # the checkout's own tag has just moved off whatever it ran before.
    retired = retire_images(superseded_images(client, tag), client) if built else []
    # Before anything mounts a runtime's volume: the entrypoint writes a fresh
    # home's document on first start, and a split keeps what a volume already
    # holds, so a probe opened first would leave the old document behind.
    helper = HomeHelper(
        engine=client,
        tag=tag,
        uid=root.stat().st_uid,
        gid=root.stat().st_gid,
        config_home=image.config_home,
    )
    superseded = SupersededFile()
    kept_for = kept_for_superseded()
    now = datetime.now(UTC)
    said.add(
        split_config_volumes(
            root,
            helper,
            [
                RuntimeVolume(login=runtime, volume=state_volume_name(root, runtime))
                for runtime in runtime_logins()
            ],
            superseded,
            kept_for,
            now,
        )
    )
    said.add(
        swept_superseded_notice(
            sweep_superseded(client, superseded, kept_for, now), kept_for
        )
    )
    # After the split, so the seed is settled against the volume a session
    # will open, and before any container starts and applies it.
    if home_seed is not None:
        said.add(settle_home_seed(helper, state_volume_name(root, login), home_seed))
    said.add(
        swept_notice(
            retired,
            sweep_environments([root, *sibling_worktrees(root)]),
            sweep_containers(client, keep=image.egress.proxy_name(root.name)),
        )
    )
    reached_at = start_egress(image.egress, root.name, client, root)
    said.add(image.egress.notice(root.name))
    # Started before the container rather than beside it, because a pipe with
    # no reader blocks its writer: a sign-in that raced the listener would
    # hang on the one step the whole bridge exists to unblock.
    # Started here for the same reason and answering the same way: a broker
    # that cannot listen is a session without a clipboard rather than a launch
    # that fails, and the notice says which happened either way.
    copying = image.clipboard.serve()
    nudging = image.wake_sockets.serve()
    handing = image.browser.serve()
    said.add(image.clipboard.notice(copying is not None))
    said.add(image.wake_sockets.notice(nudging is not None))
    said.add(
        image.browser.notice(handing is not None, image.egress.shares_host_loopback())
    )
    said.add(fleet_notice(accessible))
    said.add(
        pruning_notice(hold_pruning_across([root, *(item.path for item in accessible)]))
    )
    # Resolved on the host against the registry both engines read, before any
    # argv names a device: a name no spec answers refuses the whole container,
    # so a device nobody registered is withheld and said here rather than
    # handed to the engine to fail on.
    granted_devices = lease_devices(devices, registered_devices())
    said.add(granted_devices.notices())
    record_boundary(
        lease,
        image.egress,
        root,
        granted_devices,
        shared=[
            repository_layout(path).common
            for path in [root, *(item.path for item in accessible)]
            if in_repository(path)
        ],
    )
    # Read on the host and passed in, never resolved inside: a rewrite decided
    # in there is a rewrite the confined thing chose for itself, even over a
    # `.git/config` the boundary holds read-only.
    environ = os.environ  # lup: ignore[os-environ]
    # The credential is selected here, on the host, for the third time in this
    # function and for the same reason as the other two: everything it reads --
    # the agent socket, the operator's ssh directory, the token variable -- is
    # the host's, and a container asked to choose its own credential is the
    # confined thing choosing what confines it. The egress is asked first
    # because ssh reads none of the proxy variables, so a filtered session
    # cannot use an ssh credential however good the credential is.
    forge = image.forge.select(dict(environ), image.egress.carries_ssh(), Path.home())
    granted = image.forge.granted(dict(environ))
    rewrites = fleet_rewrites(
        [root, *(item.path for item in accessible)],
        image.forge.host,
        forge.transport(image.forge.ssh_user),
    )
    # Read on the host for the same reason the rewrites are: `.git/config` is
    # writable from inside, so an identity resolved in there would be one the
    # confined thing chose for itself.
    identity = committer(root)
    said.add(image.forge.notice(forge, identity))
    # The operator's terminal, answered here rather than in the declaration
    # the digest hashes. Same rule as the container client and for the same
    # measured reason: a `TERM` folded into the declaration would report the
    # generated trees stale on any machine whose terminal differed. The zone
    # is read beside it and for the same reason, off `/etc/localtime` rather
    # than out of `environ`, which is where a Linux host does not keep it.
    terminal = image.terminal.for_host(environ, host_timezone())
    said.add(terminal.notices())
    if banner is None:
        said.say()
    # The editor's lockfile directory, guaranteed before anything mounts it.
    # Whichever side writes it first creates it, so where no editor has ever
    # run it is simply absent -- and a bind mount whose source
    # does not exist is one the engine refuses the entire container for, which
    # takes the launch and every probe behind the same argv down with it.
    # Here rather than in the image declaration, which assembles argv, touches
    # no disk, and is hashed into the ownership digest.
    if editor_rendezvous is not None:
        editor_rendezvous.mkdir(parents=True, exist_ok=True)
        # Said rather than left implicit: a session that reaches an editor
        # in silence makes a bridge pointed at a directory nothing writes
        # into look exactly like a bridge that works.
        said.add(
            [
                Notice(
                    text=(
                        f"Editor bridge: {editor_rendezvous} → "
                        f"{image.config_home}/{editor_rendezvous.name}"
                    ),
                    urgency="detail",
                )
            ]
        )
    # Recorded once the argv stands, since every argv this returns is run and
    # its entrypoint applies the login the moment the container starts.
    if handoff.record is not None:
        logins.record(handoff.record)
    return image.session_arguments(
        tag=tag,
        checkout=root,
        uid=root.stat().st_uid,
        gid=root.stat().st_gid,
        writable=lease.writable,
        read_only=lease.read_only,
        state_volume=state_volume_name(root, login),
        config_home_env=login.config_home_env,
        credential_file=login.credentials_file,
        credential_renewable=login.renewable,
        credential_fields=login.credential_fields,
        credential=handoff.credential,
        editor_rendezvous=editor_rendezvous,
        engine=client,
        forge=forge,
        granted=granted,
        rewrites=rewrites,
        identity=identity,
        browser_directory=handing,
        clipboard_directory=copying,
        wake_directory=nudging,
        terminal=terminal.environment,
        streams=streams,
        proxy_address=reached_at,
        boundary={**sentinels.within(), ROOT_VARIABLE: str(root.resolve())},
        inherited_environment=inherited_environment,
        environments=held_environments(root, accessible, image.project_environment),
        devices=granted_devices.granted,
        home_seed=home_seed.seed if home_seed is not None else None,
        trust_document=login.trust_document,
        privileges=privileges,
        memory=bounded.limit,
        overlays=overlays,
    )


def read_config_home(
    image: Image, root: Path, login: ProviderLogin, names: list[str]
) -> list[HomeFile]:
    """The named files of one runtime's config volume for this checkout, as they stand.

    Read after a session closes, from the image this checkout last ran and
    as the identity it ran as, with the volume mounted read-only — so a
    read changes nothing it reads. Nothing is read where no client
    answers, or the engine refuses; the caller says the settings stayed.
    """
    found = detected_client()
    if found is None:
        return []
    helper = HomeHelper(
        engine=found.engine(),
        tag=checkout_tag(root),
        uid=root.stat().st_uid,
        gid=root.stat().st_gid,
        config_home=image.config_home,
    )
    try:
        return helper.read(state_volume_name(root, login), names)
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        return []


def engine_absence() -> str | None:
    """Why this host cannot open a worker container, or ``None`` if it can.

    Asked of the client rather than of whether this session is itself
    contained, because the absence is what actually blocks the launch and it
    has more than one cause: a session inside a container has no engine to
    reach, and neither does a host that never installed one. Both need the
    same thing done about them, and naming the engine says so without this
    having to work out which side of a boundary it is standing on.

    Mounting the host's engine socket into a contained session is the obvious
    way to make this answer yes, and it is why the question is asked at all: a
    confined session holding that socket can start a sibling with the whole
    host bound into it, which is a total escape and defeats the containment
    that asked for the worker boundary in the first place. Degrading silently
    to unconfined workers is the other way, and it is worse -- the run looks
    identical and the boundary is simply not there.
    """
    if detected_client() is not None:
        return None
    return (
        "No container client answered, so this run cannot give its workers "
        "their own boundary. A session inside a container has no engine to "
        "reach: start the run from an uncontained session (`harness claude "
        "--sandbox inner`, or a plain shell) so each worker gets a container of "
        "its own. Mounting the engine's socket into a contained session would "
        "let it start a sibling with the whole host bound in, which is why "
        "that is not the answer here."
    )


def wrapper_script(argv: list[str], program: str) -> str:
    """The shell that execs one worker's CLI inside its container.

    ``exec`` rather than a call, so the container replaces this shell instead
    of running under it: what the runtime holds is then the engine's own
    process, and a signal sent to the worker reaches the thing actually running
    rather than a parent that would have to forward it.

    ``"$@"`` is the one thing here that must not be quoted as a unit -- the
    runtime appends its own arguments, and a wrapper folding them into a single
    word would hand the CLI one long argument instead of the flags it was
    given. Everything this writes itself goes through :func:`shlex.quote`,
    which is what keeps a path with a space in it from becoming two arguments.
    """
    quoted = " ".join(shlex.quote(argument) for argument in [*argv, program])
    return (
        "#!/bin/sh\n"
        "# Generated by lup: opens one resolver actor inside its own container.\n"
        f'exec {quoted} "$@"\n'
    )


def written_wrapper(path: Path, argv: list[str], program: str) -> Path:
    """Write one actor's wrapper where its runtime can start it, and mark it runnable.

    Executable because that is what being named as a program means: the runtime
    spawns this path directly rather than handing it to a shell, so a file
    without the bit set fails as a permission error naming a path, which reads
    as a broken install rather than as a file written a moment ago.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(wrapper_script(argv, program))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def contained_cli(
    wrapper: Path,
    image: Image,
    manifest: Manifest,
    root: Path,
    program: str,
    login: ProviderLogin,
    lease: Lease | None = None,
    credential: HandedLogin | None = None,
    editor_rendezvous: Path | None = None,
    sentinels: LaunchSentinels = LaunchSentinels(),
    accessible: list[AccessibleRoot] = [],
    devices: list[Device] = [],
) -> Path:
    """The program a session is started as, so it opens inside a container.

    What an agent declared behind ``sandbox=OuterContainer()`` names as
    its CLI. The seam is the program on both runtimes -- Claude's SDK spawns whatever ``cli_path``
    names and hands it the arguments it would have handed ``claude``, Codex
    takes the same question as ``executable`` -- so a session is contained by
    pointing that field here, and neither adapter learns anything about
    containers.

    ``program`` is required rather than defaulted because a CLI's own name is
    that provider's vocabulary and this builder is neutral between them. The
    caller naming it is already the composition root that picked the adapter.

    ``lease`` defaults to the mounts that confine this session to the tree it
    was given, which is what a session opened by a program rather than by a
    person is almost always for. A session meant to reach every checkout the
    way a launched one does passes :func:`~lup.sandbox.rail.lease_for`
    instead, and one that may write nothing passes the lease it would have
    had through :func:`~lup.sandbox.rail.demoted`.

    The streams are fixed at ``piped`` rather than offered. A session opened
    through the SDK speaks a protocol over stdin and has nobody at a
    terminal, so either other state would leave its runtime talking to a
    stream nothing reads.
    """
    root = root.resolve()
    opening = contained_argv(
        image,
        manifest,
        root,
        editor_rendezvous,
        credential,
        login,
        streams="piped",
        sentinels=sentinels,
        accessible=accessible,
        lease=worker_lease(root) if lease is None else lease,
        devices=devices,
    )
    # Bind recovery to the same source used for home preparation, regardless
    # of the caller's later working directory or inherited policy provenance.
    opening.extend(["env", f"{POLICY_ROOT_ENV}={root}"])
    if login.home_preparation is not None:
        preparation = login.home_preparation.command(root, Path(image.config_home))
        print(str(sh.Command(opening[0])(*opening[1:], *preparation)), end="")
    return written_wrapper(wrapper, opening, program)


def worker_cli(
    wrapper: Path,
    image: Image,
    manifest: Manifest,
    lease_root: Path,
    editor_rendezvous: Path | None,
    credential: HandedLogin | None,
    login: ProviderLogin,
    program: str,
    read_only: bool = False,
    sentinels: LaunchSentinels = LaunchSentinels(),
    accessible: list[AccessibleRoot] = [],
    devices: list[Device] = [],
) -> Path:
    """The program to start one resolver actor as, so it runs in its own container.

    :func:`contained_cli` with the lease an actor gets, which is the whole of
    what this adds. :mod:`lup.sandbox.rail` argues that confinement has to be
    a mount fact rather than a judgement, because deciding from a command's
    text where it will act is undecidable and ``cd ../other && git commit``
    walks past any policy that tries. A table computed when a *session*
    starts covers only the checkouts existing at that instant, which is every
    branch an operator is landing and no worktree a run leases, since those
    are cut afterwards. The concurrency the rail is for is between actors, so
    an actor's boundary is taken here, when its own lease exists.

    ``lease_root`` is both the tree this actor was given and the checkout its
    container opens on, which is not two decisions that happen to agree: every
    path is mounted at its own absolute location because a linked worktree's
    ``.git`` is a file holding an absolute ``gitdir:`` pointer, so the tree an
    actor works in is the only place its container can open it.

    ``read_only`` is what a reviewer sets, and its lease is the worker's with
    nothing writable rather than a table of its own -- built from the same
    call, the two cannot come to hold different ideas of which checkouts exist.
    """
    lease = worker_lease(lease_root)
    return contained_cli(
        wrapper,
        image,
        manifest,
        lease_root,
        program,
        login,
        lease=demoted(lease) if read_only else lease,
        credential=credential,
        editor_rendezvous=editor_rendezvous,
        sentinels=sentinels,
        accessible=accessible,
        devices=devices,
    )


def worker_wrapper_path(run_dir: Path, concern_id: str, actor: str) -> Path:
    """Where one actor's wrapper is written, named for the actor that runs it.

    Under the run directory rather than inside the lease, because the lease is
    a worktree the run deletes when it is done and the wrapper has to outlive
    the turn that spawned it. Named for both the concern and the actor since
    one concern opens more than one -- a worker and the reviewer that judges it
    -- whose leases differ in exactly the way a shared filename would hide.
    """
    return run_dir / "workers" / f"{concern_id}-{actor}.sh"
