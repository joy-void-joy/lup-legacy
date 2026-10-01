"""Requirements lup offers, at defaults a project is expected to overrule.

The same split the shell vocabulary makes. :mod:`lup.harness.requirements` is
the mechanism -- what a requirement is, how it is exercised, what absence
costs -- and this is the batteries: one constructor per external program lup
has an opinion about, each parameterised at the points where the opinion is
really a guess.

Every guess is a parameter with a default rather than a value written into
the body, because the same program is needed differently by different
projects. What a container image installs to satisfy a toolchain depends on
that image's base; which group grants a daemon socket depends on how the
daemon was installed; whether a capability is wanted on the host, in the
image, or both is the composing project's answer and not this module's. A
project takes the constructors it wants, passes what it differs on, and
writes its own :class:`~lup.harness.requirements.Requirement` for anything
lup never heard of -- which is the whole of the extension story, exactly as
it is for shell rules.
"""

from pathlib import Path

from lup.devtools.clipboard import clipboard_probes
from lup.harness.devices import Device
from lup.workspace.checkout_state import CheckoutState
from lup.harness.image import ContainerEngine, Docker, detected_client
from lup.harness.requirements import (
    Advisory,
    AnyOf,
    EnvironmentRedirect,
    Exercise,
    HostFacts,
    LostCapability,
    Manifest,
    MisleadingAbsence,
    MountProbe,
    BindProbe,
    Package,
    SENTINEL_VARIABLE,
    RefusedLaunch,
    Requirement,
    Run,
    SentinelProbe,
    Side,
    SupplementaryGroup,
    VocabularyProbe,
)


def uv_requirement(
    where: Side = "both",
    install: list[Package] = [],
) -> Requirement:
    """The package manager every command in a lup project is invoked through.

    The one requirement offered here that refuses rather than degrades. Its
    absence is not a smaller session, it is a session where every command
    fails in the vocabulary of whatever was being attempted -- which is the
    shape the whole manifest exists to prevent, and so worth one refusal.

    ``install`` is empty because the images lup builds start from a base that
    ships uv. A project whose base does not says so here.
    """
    return Requirement(
        capability="uv",
        purpose="every command in this project, which is invoked through it",
        where=where,
        exercise=Run(command=["uv", "--version"]),
        absence=RefusedLaunch(
            because="Project commands require a working uv installation."
        ),
        recovery="Install uv or fix the uv error above, then rerun the launcher.",
        install=install,
    )


def for_host(
    manifest: Manifest,
    engine: ContainerEngine,
    checkout: Path | None = None,
    inside_sentinel: str = "",
    host_sentinel: str = "",
) -> Manifest:
    """This manifest with every client-carried exercise pointed at ``engine``.

    The split this exists for: *which capability is required* is a
    declaration, hashed into the ownership digest and identical on every
    machine, while *which program carries the exercise out* is a fact about
    the machine in front of you. Folding the second into the first reported
    generated artifacts as stale for having a different container client --
    measured moving twice on one machine, minutes apart, when a stale podman
    pid file was cleaned up between the runs.

    It matters which client, which is why this exists at all rather than the
    declaration simply being right: ``DOCKER_HOST`` pointing a genuine Docker
    CLI at a podman socket leaves that CLI answering first, and
    :func:`~lup.harness.image.detected_client` refuses it -- podman needs
    ``--userns=keep-id`` and a Docker client rejects the flag before the
    daemon sees it -- while the podman CLI beside it is what a session opens
    through. Exercising the refused one verifies a boundary no session uses.

    ``checkout`` is the second such fact and arrives the same way. A probe
    aimed at where this tree actually sits cannot have that path in the
    declaration: measured, the ownership digest moved between two worktrees
    of one commit, so every checkout but the one that generated last read its
    own committed tree as stale -- for having been checked out somewhere
    else. What is declared is the shape; this is where a machine answers it.

    The sentinels are the third, and the only one that is a fact about this
    *launch* rather than about this machine. They arrive here for the same
    reason and are empty by default, which leaves a placement probe reporting
    that nothing aimed it -- the honest answer for a caller that resolved a
    manifest without opening a session.
    """
    facts = HostFacts(
        client=engine.binary,
        checkout=checkout or Path(),
        inside_sentinel=inside_sentinel,
        host_sentinel=host_sentinel,
    )
    return Manifest(
        requirements=[
            requirement.model_copy(
                update={"exercise": resolved_exercise(requirement, facts)}
            )
            for requirement in manifest.requirements
        ]
    )


def resolved_exercise(requirement: Requirement, facts: HostFacts) -> Exercise:
    """One requirement's exercise with everything this machine has to supply.

    Two resolutions rather than one, because they answer different questions.
    ``by_client`` is the *requirement's* claim that the client carries the
    exercise out, and only some requirements make it; :meth:`Exercise.given`
    is each exercise shape answering for whatever else it could not name, and
    every shape answers it -- with "nothing" where a command is already
    portable.
    """
    exercise = (
        requirement.exercise.pointed_at(facts.client)
        if requirement.by_client
        else requirement.exercise
    )
    return exercise.given(facts)


def container_client(fallback: str = "docker") -> ContainerEngine:
    """Which client a container exercise should run through — the launcher's.

    Asked rather than spelled, because the two answers come apart on an
    ordinary host: ``DOCKER_HOST`` pointing a genuine Docker CLI at a podman
    socket leaves that CLI answering first, and
    :func:`~lup.harness.image.detected_client` refuses it as undrivable --
    podman needs ``--userns=keep-id`` and a Docker client rejects the flag
    before the daemon sees it -- while the podman CLI beside it is what a
    session actually opens through. A manifest exercising the refused client
    verifies a boundary no session uses: it can pass where the launch fails
    and fail where the launch would have worked, which is the one thing a
    declare-and-verify manifest must not do.

    The engine rather than the binary, because the client decides more than
    which program is spawned: what the daemon behind it is *asked* is spelled
    per engine too, and `podman info --format '{{.ServerVersion}}'` fails with
    *can't evaluate field ServerVersion* -- which a preflight reports as the
    runtime being unavailable on a host where it is running fine.

    ``fallback`` is what a host with no client at all gets. A name rather than
    an absence, so the exercise still fails in that program's own words --
    ``docker: not found`` says more than an empty command could.
    """
    found = detected_client()
    return found.engine() if found is not None else Docker(binary=fallback)


def container_requirement(
    where: Side = "host",
    install: list[Package] = [],
    socket_variable: str = "DOCKER_HOST",
    socket_group: str = "docker",
    lost: str = "container sessions and parallel resolver workers",
    client: str = "docker",
) -> Requirement:
    """A reachable container daemon, exercised by asking it its own version.

    Asking the daemon rather than asking PATH for a client. The two answers
    came apart for an entire evening once: a profile exported a socket
    variable pointing at a runtime that was not installed, so every client
    redirected to a path that could not exist and reported that it could not
    reach the daemon -- which reads as a stopped service and sends the reader
    to restart the wrong thing.

    ``where`` defaults to the host and ``install`` to nothing, and both
    defaults are load-bearing rather than merely conservative: a container
    holding a daemon socket can start a sibling with the whole host
    bind-mounted, which is a total escape. A project that means to put a
    runtime inside its image is overruling a security property and should
    have to say so.

    Container creation and expression evaluation are exercised separately by
    ``sandbox_requirement`` at setup. This cheap daemon check runs at launch;
    its success alone establishes nothing about the sandbox's Python REPL.
    """
    return Requirement(
        capability="container runtime",
        by_client=True,
        purpose="the sandbox code evaluation runs in, and multi-worker resolve",
        where=where,
        # Bare `info` rather than a formatted field, because the report is
        # shaped per engine and the query is not: `podman info --format
        # '{{.ServerVersion}}'` answers *can't evaluate field ServerVersion*,
        # which a preflight reports as the runtime being unavailable on a
        # machine where it is running fine. Both engines fail `info` when the
        # daemon is unreachable, which is the whole of what this asks.
        exercise=Run(command=[client, "info"]),
        absence=LostCapability(capability=lost),
        recovery="Check that Docker or Podman is installed and its service is reachable.",
        diagnoses=[
            EnvironmentRedirect(variable=socket_variable),
            SupplementaryGroup(group=socket_group),
        ],
        install=install,
    )


def sandbox_requirement(image: str = "") -> Requirement:
    """A setup exercise of container creation and the sandbox's Python REPL."""
    return Requirement(
        capability="Python sandbox",
        purpose="evaluating Python through the sandbox execution transport",
        checked="setup",
        exercise=Run(
            command=[
                "uv",
                "run",
                "lup-devtools",
                "harness",
                "sandbox-check",
                *(["--image", image] if image else []),
            ]
        ),
        absence=LostCapability(capability="sandbox Python execution"),
        recovery=(
            "Rerun `uv run lup-devtools harness sandbox-check` after repairing the "
            "reported startup or evaluation failure. A reachable daemon still needs "
            "permission to create containers and an image with a working Python. "
            "The probe disables network access and installs no packages."
        ),
        diagnoses=[
            EnvironmentRedirect(variable="DOCKER_HOST"),
            SupplementaryGroup(group="docker"),
        ],
    )


def same_path_mount_requirement(
    where: Side = "host",
    install: list[Package] = [],
    image: str = "docker.io/library/busybox:latest",
    witness: str = "pyproject.toml",
) -> Requirement:
    """Whether this host can bind-mount a directory at its own absolute path.

    The prerequisite the worktree rail rests on, and one that has already been
    found false. A linked worktree's `.git` is a file holding an *absolute*
    `gitdir:` pointer, so a container that mounted the tree anywhere else
    would hold a checkout pointing at a path that does not exist there --
    same-path mounting is forced rather than preferred, and where it does not
    work the rail does not work.

    How this is asked matters more than that it is asked, and the cheap way
    gets it wrong in the direction that manufactures findings. Asking
    ``test -d`` about the mounted directory answers *false* on rootless
    podman for every worktree this rail leases -- which reads exactly like an
    absent mount, and is not one. Reading a file through the same mount, in
    the same container, succeeds: the mount is present and `stat` on the
    mount point is simply not answerable under that user-namespace mapping.
    A presence check answers a different question than the one asked, and its
    wrong answer is shaped like a real defect.

    So the exercise reads a file across the boundary. That cannot succeed
    unless the mount both happened and carried content, and it cannot fail
    for a reason that has nothing to do with mounting.

    Which directory it is aimed at is not written here either. Spelling the
    checkout puts an absolute host path into a declaration the ownership
    digest hashes, and the digest then moves between two worktrees
    of one commit -- so every checkout but the last one to generate reads its
    own committed tree as stale, for a fact about where somebody put it.
    :class:`MountProbe` declares the shape
    and :func:`for_host` aims it, which is the same split the container
    client already goes through.

    ``witness`` names a file the probed directory is known to hold. A probe
    whose witness is absent answers about the witness rather than the mount.

    Checked at setup rather than at every launch, because it starts a
    container. That is a statement about the probe's cost and not about the
    requirement's importance -- which is why the two are separate fields.
    """
    return Requirement(
        capability="same-path bind mounts",
        purpose="the worktree rail, which confines a worker by mounting",
        where=where,
        checked="setup",
        exercise=MountProbe(image=image, witness=witness),
        absence=LostCapability(capability="mounting worktrees in worker containers"),
        recovery="Check the mount error above and the container service's access to the checkout.",
        install=install,
    )


def granted_device_requirement(
    device: Device,
    image: str = "docker.io/library/busybox:latest",
) -> Requirement:
    """A device this machine grants its sessions, exercised by handing it to a container.

    Built from the machine's own grant rather than declared in a manifest,
    and that placement is the whole of it: a manifest is committed and shared
    by every machine and every downstream user of the repository, and which
    GPU one of them holds is that machine's alone. `sync grant` builds this
    as it writes the grant, and `harness requirements` builds one per grant
    it finds, so a committed roster never names a vendor's device.

    The exercise starts a throwaway container with the device and nothing
    else. That is vendor-neutral proof of the two halves that pass alone and
    fail together: a spec can be registered on a host whose engine does not
    honour the registry, and an engine can honour it on a host with no spec
    for the name. Whether the driver inside answers a vendor's tool is the
    project's to ask in its own words, once the device is there.

    Checked at setup rather than every launch because it starts a container:
    a launch reads the registry itself and withholds a device it cannot
    grant, so what this adds is worth one container start when a grant is
    made and not before every session. Carried by the client for the reason
    the container requirement is: which engine starts the probe is a fact
    about the machine.
    """
    return Requirement(
        capability=f"device {device.name}",
        by_client=True,
        purpose="what sessions on this machine were granted, held to the registry they read",
        where="host",
        checked="setup",
        exercise=Run(
            command=["docker", "run", "--rm", *device.arguments(), image, "true"]
        ),
        absence=LostCapability(
            capability=f"{device.name} inside contained sessions and workers"
        ),
        recovery=(
            "Register the device with its vendor's toolkit -- for NVIDIA, "
            "`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` -- "
            "and check the engine reads the registry: Docker does from 28.3, "
            'an older daemon needs `"features": {"cdi": true}`, podman always has.'
        ),
    )


def git_requirement(
    where: Side = "host",
    install: list[Package] = [Package(name="git")],
) -> Requirement:
    """Git itself, which every other capability here assumes and none declared.

    The one external program nothing works without was the one with no
    declaration: `uv`, the container runtime, `gh` and the clipboard were all
    exercised, while the program that answers where the checkout is, which
    worktree holds the lease, and who is committing was simply assumed. What
    that bought is a machine missing git failing at whichever git call ran
    first, in that call's own vocabulary, several steps from the one thing to
    install.

    Exercised inside a repository rather than by ``--version``, for the reason
    `gh` is exercised as authenticated: a git that runs while standing outside
    a worktree answers every version query and refuses every question a
    session actually asks it.

    Host-side, because the image declares its own -- git is in the base
    package list :class:`~lup.harness.image.Image` renders, so a requirement
    installing it again would rebuild every image to add a package already
    there.

    Absence costs rather than refuses, which is the grade this module reserves
    for what would be *quietly* weaker. Nothing about a missing git is quiet:
    the operator gets this line at the top of the scrollback naming what to
    install, and every route below it fails loudly rather than proceeding
    against a boundary that is not standing.
    """
    return Requirement(
        capability="git",
        purpose="the checkouts, worktree leases and commit attribution a session needs",
        where=where,
        exercise=Run(command=["git", "rev-parse", "--git-dir"]),
        absence=LostCapability(capability="Git access"),
        recovery="Check that git is installed and this directory is a Git checkout.",
        install=install,
    )


def github_requirement(
    where: Side = "both",
    install: list[Package] = [Package(name="github-cli")],
) -> Requirement:
    """The GitHub CLI, exercised as *authenticated* rather than as installed.

    `gh --version` passes on a machine that has never logged in, and every
    command that matters then fails one at a time with an authentication
    error several steps from the setup that would fix it.
    """
    return Requirement(
        capability="gh",
        purpose="pull requests, issues, and the friction reports the loop files",
        where=where,
        exercise=Run(command=["gh", "auth", "status"]),
        absence=LostCapability(capability="GitHub access for pull requests and issues"),
        recovery="Check the gh authentication error above.",
        recovery_by_location={
            "host": "Run `gh auth login` on the host, then rerun the launcher.",
            "image": "Check the GitHub credentials passed to the container by the launcher.",
        },
        install=install,
    )


def bubblewrap_requirement(
    where: Side = "host",
    install: list[Package] = [Package(name="bubblewrap")],
) -> Requirement:
    """The unprivileged confinement a runtime's own Linux sandbox is built on.

    Exercised rather than looked up on PATH, because the two answers differ
    exactly where it matters: a confinement binary that is installed and
    cannot start a namespace on this kernel is present and useless, and a
    launcher that vouched for it on presence alone would tell every
    dispatcher downstream to relax into a boundary that is not there.
    """
    return Requirement(
        capability="bwrap",
        purpose="the OS boundary an uncontained session's runtime confines with",
        where=where,
        exercise=Run(command=["bwrap", "--version"]),
        absence=LostCapability(capability="OS confinement"),
        recovery="Install bubblewrap and check the bwrap error above.",
        install=install,
    )


def socat_requirement(
    where: Side = "host",
    install: list[Package] = [Package(name="socat")],
) -> Requirement:
    """The relay that carries a sandboxed command's traffic to its proxy.

    ``-V`` rather than ``--version``, which socat does not have: asked the
    long way it prints ``E unknown option "--version"`` and exits 1. A prober
    that spelled one flag for every program it checked read that as a broken
    socat on every host in the world, and the OS boundary was reported
    unavailable on machines where it was installed and working -- which is
    the whole argument for a probe travelling with the program it probes
    rather than with the code that calls for it.
    """
    return Requirement(
        capability="socat",
        purpose="the OS boundary an uncontained session's runtime confines with",
        where=where,
        exercise=Run(command=["socat", "-V"]),
        absence=LostCapability(capability="OS confinement"),
        install=install,
    )


def bun_requirement(
    where: Side = "image",
    install: list[Package] = [Package(name="bun")],
) -> Requirement:
    """The JavaScript runtime and package manager, wanted inside the image.

    ``where`` defaults to the image because a host that will only ever run
    this toolchain inside a container is not a host with a problem, and
    exercising it there would report one. A project doing JavaScript work
    directly on the host passes ``both``.
    """
    return Requirement(
        capability="bun",
        purpose="running and bundling this project's TypeScript",
        where=where,
        exercise=Run(command=["bun", "--version"]),
        absence=LostCapability(capability="the JavaScript toolchain"),
        install=install,
    )


def typescript_requirement(
    where: Side = "image",
    install: list[Package] = [Package(name="typescript", manager="bun")],
) -> Requirement:
    """The TypeScript compiler, reached through the package runner.

    Through `bunx` rather than as a global binary: a project's compiler
    version is a property of that project, and a globally installed `tsc`
    checks against whatever version somebody last installed anywhere.
    """
    return Requirement(
        capability="typescript",
        purpose="type-checking this project's TypeScript before it runs",
        where=where,
        exercise=Run(command=["bunx", "tsc", "--version"], expect="Version"),
        absence=LostCapability(capability="type-checking TypeScript"),
        install=install,
    )


def clipboard_requirement(
    where: Side = "host",
    install: list[Package] = [],
) -> Requirement:
    """Any one of the clipboard clients a desktop might have, as an advisory.

    Advisory because a machine without one still works: what is lost is
    copying a printed command by hand, and the clipboard bridge a contained
    session pastes through, which answers out of this same backend. Neither
    stops a session, so this is said once to whoever is setting a machine up
    and stays silent at every launch, where it would only teach people to
    skip the line above it. The bridge says its own absence at the launch it
    is absent from, which is the moment that one matters.

    The spellings come off :func:`~lup.devtools.clipboard.clipboard_probes`
    rather than being listed here, because which one a machine has is a fact
    about its desktop rather than about any project -- and because a list
    written twice comes apart. It already had: this named four backends
    including Wayland while the code that reached for a clipboard tried four
    that did not, so a Wayland machine was told it had a clipboard and then
    silently failed to use it.

    Each probe is a *read*. A write would destroy whatever the operator had
    on their clipboard to establish something they never asked about, and a
    ``--version`` proves nothing: `wl-copy --version` succeeds with no
    compositor running and `xclip -version` succeeds with no `DISPLAY`, which
    is exactly the machine where pasting does nothing.
    """
    return Requirement(
        capability="clipboard",
        purpose=(
            "handing a printed command straight to the shell that runs it, "
            "and answering the clipboard bridge a contained session pastes "
            "through"
        ),
        where=where,
        checked="setup",
        exercise=AnyOf(
            alternatives=[Run(command=probe) for probe in clipboard_probes()]
        ),
        absence=Advisory(
            improves="copying a command, and pasting into a contained session"
        ),
        install=install,
    )


def agent_session_requirement(
    where: Side = "image",
    install: list[Package] = [],
    runtime: str = "claude",
    arguments: list[str] = [],
) -> Requirement:
    """Whether an agent session actually runs inside the image, not merely opens.

    The question the whole contained-agent architecture rests on, and one that
    every cheaper probe answers wrongly. ``claude --version`` passes in an
    image where no session can authenticate; a config home that accepts a
    ``mkdir`` proves the filesystem and nothing about whether the runtime will
    use it; and ``claude plugin validate`` was measured reporting a plugin
    path *missing* through a bind mount that was demonstrably there -- the
    same rootless-podman misreport ``same_path_mount_requirement`` exists to
    route around.

    So the exercise runs a real turn and requires its answer back. That cannot
    pass without the image, the mount, the relocated config home, the
    credential store, the egress proxy and the model endpoint all working
    together, and it cannot fail for a reason unrelated to any of them.

    The list in that sentence is what a declaration can only *claim*. Spelled
    out as its own ``run``, this exercise would carry no mount, no config
    home, no credential and no network -- so it would start a bare container
    on the engine's default bridge, where the proxy this architecture routes
    through does not stand between anything. It can pass on a host whose
    sessions cannot open, which is the one thing a declare-and-verify manifest
    must not do, and the contained session then meets a DNS failure that no
    preflight is in a position to see.

    Declared image-side, which is what makes it true: an image-side exercise
    is carried out behind the argv a session opens with, so every part of the
    boundary the sentence above names is in the path. Nothing here spells a
    client or a tag, for the same reason -- both are host facts that would
    sit in a declaration the ownership digest hashes.

    Its absence refuses rather than degrades, because an architecture whose
    sessions do not run is not a degraded architecture.

    ``arguments`` is what the launch says on the command line and this must
    say too, held by the caller because the words are one runtime's own and
    this module stays provider-neutral. It exists because the sentence above
    was still not true without it: an exercise carrying the mounts, the
    config home and the network, and *not* carrying the flag that stands the
    runtime's own sandbox down, opened a session with its settings still
    saying the sandbox was on -- so it refused for a confinement that cannot
    start in an unprivileged container and that no launch has ever asked
    for. A probe answering about a session nobody opens is the one failure
    this declaration exists to prevent, and it had found a third way to do
    it.
    """
    return Requirement(
        capability="contained agent session",
        purpose="running agents, workers and reviewers inside the boundary",
        where=where,
        checked="setup",
        exercise=Run(
            command=[runtime, *arguments, "-p", "Reply with exactly: SESSION_OK"],
            expect="SESSION_OK",
        ),
        absence=RefusedLaunch(
            because="The agent did not complete the test turn inside the container."
        ),
        recovery="Check the runtime error above, including sign-in and network access.",
        install=install,
    )


def proxy_reachable_requirement(
    where: Side = "image",
    install: list[Package] = [],
) -> Requirement:
    """Whether the session can open a connection to the proxy it was given.

    The first component of a filtered egress, and the one whose failure is
    least recognisable. A session on an internal network reaches the world
    only through the proxy, so a proxy it cannot open a socket to means every
    request fails before anything is sent.

    Asked of ``$HTTPS_PROXY`` rather than of a spelled address, and that
    keeps two things true at once. The address is assigned when the proxy
    joins the network, so a declaration naming one would be a fact about a
    machine sitting in something the ownership digest hashes. And what the
    session was *pointed at* is the right subject anyway: a probe that
    reached the proxy by some other route would verify a path no session
    takes -- the mistake the contained-session exercise is declared
    image-side to avoid.

    Squid answers a direct request with a status of its own, so any HTTP code
    proves the socket opened. Being refused is a correct answer here.

    Asking instead whether a DNS alias resolves is a question a network can
    answer yes while its resolver refuses every public name the proxy needs
    -- measured, and the reason no alias stands in for this.

    ``at_launch`` because it is one of two places in the image roster where a
    container start is worth paying for on the way in. A session that opens
    without this cannot do anything, and it does not fail on the way in: it
    opens, looks entirely healthy, and blames the operator's network for
    every request afterwards.
    """
    return Requirement(
        capability="session reaches its proxy",
        at_launch=True,
        purpose="reaching anything at all from inside a filtered session",
        where=where,
        exercise=Run(
            command=[
                "sh",
                "-c",
                "curl --silent --show-error --max-time 15 --output /dev/null "
                '--write-out "%{http_code}" "$HTTPS_PROXY"',
            ]
        ),
        absence=RefusedLaunch(
            because="The container cannot connect to its configured network proxy."
        ),
        recovery=(
            "Run `uv run lup-devtools harness egress` to inspect the proxy and network. "
            "Check the connection error above before changing their configuration."
        ),
        install=install,
    )


def proxy_tunnels_requirement(
    where: Side = "image",
    install: list[Package] = [],
    destination: str = "https://api.anthropic.com/",
) -> Requirement:
    """Whether a request actually reaches the public internet through the proxy.

    The second component, and the end-to-end one. Reaching the proxy proves a
    socket; this proves the tunnel -- that the proxy accepted a ``CONNECT``,
    that it could resolve the destination on its own side, and that its
    bridged network really does reach out.

    Through ``curl``'s reading of the proxy variables rather than an explicit
    ``-x``, deliberately. Those variables are what every other client in the
    session reads, so a probe that bypassed them would prove the proxy works
    while saying nothing about whether anything is pointed at it -- which is
    exactly the half that was broken.

    The destination is the API the session exists to reach. A generic
    connectivity host would answer a question nobody has: an allowlist that
    admits the wider internet and not this one is a working boundary and a
    session that cannot do anything.

    ``at_launch`` for the reason the reachability probe above is, and the two
    are separate because they fail separately and send a reader to different
    places: a proxy that cannot be reached is its attachment to this network,
    and one that is reached and carries nothing is the proxy itself, or what
    it was configured to allow.
    """
    return Requirement(
        capability="egress proxy tunnels out",
        at_launch=True,
        purpose="every model call, package install and documentation fetch",
        where=where,
        # `-o /dev/null` with the code written out: any HTTP status proves the
        # tunnel stood up, and the body does not. An unauthenticated 401 from
        # the API is a complete success for this question, so matching on
        # content here would refuse a working boundary.
        exercise=Run(
            command=[
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                destination,
            ]
        ),
        absence=RefusedLaunch(
            because=f"The container could not reach {destination} through the proxy."
        ),
        recovery=(
            "Run `uv run lup-devtools harness egress` and inspect `tmp/egress.conf` "
            "for the proxy or connection error above."
        ),
        install=install,
    )


def endpoint_reachable_requirement(
    where: Side = "image",
    install: list[Package] = [],
    destination: str = "https://api.anthropic.com/",
) -> Requirement:
    """Whether a session with nothing in front of it reaches the model at all.

    The same end-to-end question :func:`proxy_tunnels_requirement` asks,
    carried by the posture with nothing in the middle to blame. Both exist
    because the question outlives the boundary and the *vocabulary of the
    answer* does not: a reader sent to `tmp/egress.conf` and `harness egress
    --down` about a session running on the host's own network is being sent
    to a rendered policy governing nothing, which costs a refusal the only
    thing it is for.

    ``at_launch``, and that is worth stating here rather than inheriting.
    Every other image entry marked always is about the proxy, so under a
    posture with none this is not one check among several -- it is the only
    one, and leaving it out does not soften the launch's verification but
    removes it, which is the state §6 exists to make impossible. What it
    catches is the same failure either way: a session that cannot reach the
    model opens cleanly, looks entirely healthy, and reports the operator's
    own internet as down.

    No ``expect``, for the reason the tunnel probe has none. Any HTTP status
    proves the request arrived, and an unauthenticated 401 from the API is a
    complete success for this question.
    """
    return Requirement(
        capability="session reaches the model endpoint",
        at_launch=True,
        purpose="every model call, package install and documentation fetch",
        where=where,
        exercise=Run(
            command=[
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                destination,
            ]
        ),
        absence=RefusedLaunch(
            because=f"The container could not reach the model endpoint at {destination}."
        ),
        recovery=(
            "Check the connection error above and compare access from the host. "
            "This check alone does not identify whether DNS, routing, TLS, "
            "or the endpoint caused the failure."
        ),
        install=install,
    )


def metadata_refused_requirement(
    where: Side = "image",
    install: list[Package] = [],
    endpoint: str = "http://169.254.169.254/",
    status: str = "403",
) -> Requirement:
    """Whether the boundary still refuses what it exists to refuse.

    The third component, and the only one that fails *closed*: the two above
    ask whether the session can reach the world, and this asks whether it
    still cannot reach the places a compromised one would head for. Both
    questions have to be live, because the natural repair for the first --
    widen, bridge, turn the filtering off -- passes it by removing the
    boundary, and nothing else here would notice.

    The cloud metadata endpoint stands for the whole denied set. It is the
    destination with the highest payoff and the lowest effort: an unauthenticated
    HTTP GET that hands out instance credentials, on an address every cloud
    answers on. If this one is refused, the private-range rules the proxy
    compiles are in force.

    Matching a *status* rather than the proxy's error page. The status is
    protocol; the page is a build's English, and a probe that read it would
    start failing on a proxy upgrade that changed nothing about the boundary.
    """
    return Requirement(
        capability="metadata endpoint refused",
        purpose="the half of the boundary that keeps a session out of the host's cloud",
        where=where,
        checked="setup",
        exercise=Run(
            command=[
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                endpoint,
            ],
            expect=status,
        ),
        absence=RefusedLaunch(
            because="Blocking access to cloud metadata could not be verified."
        ),
        recovery=(
            f"Check the error above and the metadata deny rules in `tmp/egress.conf`. "
            f"The request to {endpoint} must return HTTP {status}."
        ),
        install=install,
    )


def terminal_handoff_requirement(
    where: Side = "image",
    install: list[Package] = [],
) -> Requirement:
    """Whether the operator's terminal actually arrived inside the container.

    Three facts in one exercise because they fail together and are fixed
    together: they are the same handoff, and splitting them would start three
    containers to answer one question.

    Each of the three was measured absent on the first contained session.
    ``COLORTERM`` unset is 24-bit colour collapsing to sixteen, with the
    engine's own placeholder ``TERM`` making a truecolour terminal
    indistinguishable from a teletype. An ``EDITOR`` naming nothing runnable
    is the open-in-editor binding doing nothing when pressed -- and an
    ``EDITOR`` that is merely *unset* is the same silence, which is why this
    checks that it runs rather than that it is written. A ``LANG`` naming a
    locale nobody generated is glibc falling back to ASCII, one warning per
    program.

    The exercise prints what it found before it checks it, so a failure
    reports the values rather than only the verdict. Which of the three is
    wrong is the whole of what a reader needs, and re-running by hand inside
    a container is not a step anybody should have to take to learn it.
    """
    return Requirement(
        capability="terminal handoff",
        purpose="colour, the open-in-editor binding, and UTF-8 output",
        where=where,
        checked="setup",
        exercise=Run(
            command=[
                "sh",
                "-c",
                'printf "TERM=%s COLORTERM=%s EDITOR=%s LANG=%s\n" '
                '"$TERM" "$COLORTERM" "$EDITOR" "$LANG"; '
                '[ -n "$COLORTERM" ] || { '
                'echo "COLORTERM is unset; true-color support could not be verified" >&2; '
                "exit 1; }; "
                'command -v "$EDITOR" >/dev/null || { '
                'echo "EDITOR=$EDITOR is not an available command" >&2; '
                "exit 1; }; "
                'LC_ALL="$LANG" locale >/dev/null 2>&1 || { '
                'echo "LANG=$LANG could not be loaded" >&2; '
                "exit 1; }",
            ]
        ),
        absence=Advisory(
            improves="colour depth, the open-in-editor binding, and UTF-8 output"
        ),
        install=install,
    )


def reaped_orphans_requirement(
    where: Side = "image",
    install: list[Package] = [],
    settle: int = 3,
) -> Requirement:
    """Whether anything at PID 1 reaps the children a session abandons.

    The bound this checks is not the one that gets hit. `Image.pids_limit`
    caps a session at 4,096 processes so that a runaway fork loop is refused
    rather than taking the host down, and a session doing ordinary work never
    approaches it -- unless nothing drains what accumulates. PID 1 in a
    container started without a reaper is the agent runtime itself, which
    waits on the children it means to wait on and no others, so every orphan
    is reparented to it and stays a zombie until the container ends. The table
    then fills monotonically over hours, and the limit is reached by a session
    that leaked rather than by one doing too much.

    Worth a probe rather than trust because of how the failure presents. It is
    not "out of processes": it is `RuntimeError: can't start new thread` and
    `/bin/bash: fork: retry: Resource temporarily unavailable` spread across a
    suite, and a pre-commit hook dying of signal 6. Measured once as 94
    failures and 151 errors, read as a change having broken the tests, and
    bisected for a while before anybody counted zombies. A defect whose whole
    signature is *other things appearing broken* is one no reader diagnoses
    twice, so it is measured on the way in instead.

    Behavioural rather than a look at what PID 1 is. Reading `/proc/1/comm`
    would name the reaper on the engines that install one and prove nothing
    about whether it reaps -- and the property wanted here is the reaping. So
    the exercise abandons a child on purpose: an inner shell starts one and
    exits while it still runs, which is what reparents it, and the count is
    taken after it has had time to exit under whatever inherited it.

    ``settle`` is the only guess, and it is a guess about scheduling rather
    than about reaping: the orphan has to have exited before anything is
    counted, or a live child reads as a reaped one and this passes on an
    engine that reaps nothing.
    """
    return Requirement(
        capability="abandoned children are reaped",
        purpose="a long session reaching its process bound only by really using it",
        where=where,
        checked="setup",
        exercise=Run(
            command=[
                "sh",
                "-c",
                # The inner shell exits while its child still runs, which is
                # what reparents the child to PID 1; `settle` outlasts the
                # child so what is counted is a process that has exited.
                'sh -c "sleep 1 & exit 0"; '
                f"sleep {settle}; "
                'zombies=$(ps -eo stat= | grep -c "^Z"); '
                "reaper=$(cat /proc/1/comm); "
                'printf "pid1=%s zombies=%s\n" "$reaper" "$zombies"; '
                '[ "$zombies" = "0" ] || { '
                'echo "Found $zombies zombie process(es) after the cleanup test; '
                'PID 1 is $reaper" >&2; '
                "exit 1; }",
            ]
        ),
        absence=LostCapability(
            capability="automatic cleanup of exited child processes"
        ),
        recovery="Check that the container starts with `--init` to reap exited children.",
        install=install,
    )


def codex_envelope_requirement(
    where: Side = "host",
    install: list[Package] = [],
    runtime: str = "codex",
) -> Requirement:
    """Whether the workspace-write envelope actually refuses a write outside it.

    The counterpart to exercising ``bwrap`` before vouching for it, and it was
    missing: the Claude path probes its confinement tools and the Codex path
    asserted the same flag with nothing run at all. A flag set on an envelope
    nobody tested tells every dispatcher downstream to relax into a boundary
    that may not be there.

    ``codex sandbox`` runs a command under that envelope and no model turn, so
    the boundary a session gets is the boundary this asks about -- the same
    discipline as exercising an image requirement behind the argv a session
    opens with, rather than behind one assembled for the probe.

    Two witnesses, because one cannot tell a boundary from a broken command.
    The outer one is created and removed on the host first, so its absence
    afterwards means the envelope refused rather than that the account could
    never write there. The inner one is written inside the workspace, where
    the envelope permits it, and its presence is what proves the command ran
    at all -- without it a missing runtime, a changed subcommand, or any other
    failure produces no file outside and reads exactly like a boundary
    holding. Measured: spelled with the outer witness alone, this reported a
    working envelope for a runtime that does not exist on this machine.

    Both are computed from the working directory rather than declared, because
    a path in here would sit in what the ownership digest hashes. The outer
    one is the checkout's parent: outside the workspace the envelope roots at,
    and demonstrably writable, since this project cuts its worktrees there.
    """
    return Requirement(
        capability="codex sandbox envelope",
        purpose="confining unjudged shell when the launcher vouches for it",
        where=where,
        exercise=Run(
            command=[
                "sh",
                "-c",
                'i="$PWD/.lup-envelope-ran.$$"; o="$(dirname "$PWD")'
                '/.lup-envelope-probe.$$"; touch "$o" || exit 1; rm -f "$o"; '
                f"{runtime} sandbox -c sandbox_mode='\"workspace-write\"' -- "
                'sh -c "touch $i; touch $o" >/dev/null 2>&1; '
                'if [ ! -e "$i" ]; then printf never-ran; '
                'elif [ -e "$o" ]; then printf breached; '
                'else printf envelope-holds; fi; rm -f "$i" "$o"',
            ],
            expect="envelope-holds",
        ),
        absence=LostCapability(
            capability="verification of the Codex filesystem sandbox"
        ),
        install=install,
    )


def inside_placement_requirement(
    where: Side = "image",
    install: list[Package] = [],
    witness: str = "pyproject.toml",
    variable: str = SENTINEL_VARIABLE,
) -> Requirement:
    """Whether a command run for this session lands inside this session's boundary.

    The claim every placement in the policy is read against, and until this
    ran, the only claim nothing checked. A profile that asks for containment
    and a runtime whose containment did not start read identically from the
    configuration; what separates them is a value only this launch's opening
    argv carries, observed by a command the launch actually ran.

    ``at_launch`` because this is the one whose failure is invisible from
    outside. A session that opens without it looks entirely healthy and
    places every operation by a boundary that is not there -- which is worse
    than a session that refuses, because the operations run anyway and the
    placement in the audit record says they did not.

    The witness rides along for the reason
    :class:`~lup.harness.requirements.SentinelProbe` states: reading a file
    back at its own absolute path is what proves the same-path mount, and
    proving it here costs no container start because this probe already pays
    for one.
    """
    return Requirement(
        capability="inside placement",
        at_launch=True,
        purpose="placing an operation inside the boundary this profile promises",
        where=where,
        exercise=SentinelProbe(variable=variable, side="inside", witness=witness),
        absence=RefusedLaunch(
            because="The check could not verify the container marker and checkout mount."
        ),
        recovery="Check the container or mount error above, then rerun the launcher.",
        install=install,
    )


def read_only_binds_requirement(
    where: Side = "image",
    install: list[Package] = [],
) -> Requirement:
    """Whether every path the lease binds read-only is bound read-only inside.

    The shared git `config` and `hooks/` are what a session must not write,
    because the host runs what they name, and what holds them is the mount
    table rather than the lease that asked for it. Read from the table
    because the two can differ: a read-only *file* bind is detached by the
    kernel the moment the host renames over the file, which is how git
    rewrites `config`, and a boundary reported whole over a file that is
    writable is worse than one reported missing.

    ``at_launch`` for the reason the placement probe is: its failure is
    invisible from outside, and the session would open looking healthy.
    """
    return Requirement(
        capability="read-only binds",
        at_launch=True,
        purpose="holding the shared git config and hooks out of a session's reach",
        where=where,
        exercise=BindProbe(),
        absence=RefusedLaunch(
            because=(
                "A path the lease binds read-only is not read-only inside the "
                "container, so the host's git configuration or hooks would be "
                "writable from the session."
            )
        ),
        recovery="Check the paths named above against the container engine's mounts, then rerun the launcher.",
        install=install,
    )


def host_placement_requirement(
    where: Side = "host",
    install: list[Package] = [],
    variable: str = SENTINEL_VARIABLE,
) -> Requirement:
    """Whether a command run on the launcher's host observes the host's own value.

    The other half of the pair, and deliberately the cheap one. One variable
    carries both values and the side decides which, so a command that reports
    the inside value ran inside and one that reports the host value ran on the
    host -- and neither can be forged by a variable inherited from whatever
    shell started the launcher, because that shell has neither value.

    What this does *not* prove is that anything can put an operation here on
    purpose. That is the host executor, which is a channel rather than an
    observation, and it is declared absent until one exists. This is the
    observation the channel will have to reproduce.
    """
    return Requirement(
        capability="host placement",
        purpose="telling an operation that reached the host from one that did not",
        where=where,
        exercise=SentinelProbe(variable=variable, side="host"),
        absence=LostCapability(
            capability="telling host execution apart from contained execution"
        ),
        install=install,
    )


def question_relay_requirement(
    where: Side = "host",
    install: list[Package] = [],
    directory: str = CheckoutState(root=Path()).directory().as_posix(),
) -> Requirement:
    """Whether the durable record every final ask is written to accepts a write.

    An ask that cannot be recorded is an ask nobody can answer, and the
    settlement order refuses one of those rather than running it -- so a relay
    that cannot be written is a session where every reviewed operation
    refuses, with a message about the operation rather than about the queue.

    A write and a read-back into the queue's own directory, rather than a
    synthetic question into the queue itself. The failures that matter are
    the directory's -- a read-only checkout, a missing parent, a permission
    the container did not map -- and they are all caught here. Recording a
    fake question would catch the same failures and leave a question in the
    queue that somebody then has to answer, which is a worse probe for being
    a more literal one.
    """
    return Requirement(
        capability="question relay",
        purpose="parking a final ask where a reviewer can answer it",
        where=where,
        exercise=Run(
            command=[
                "sh",
                "-c",
                f"mkdir -p {directory} && f={directory}/preflight-$$.probe && "
                'printf relay-ok > "$f" && cat "$f" && rm -f "$f"',
            ],
            expect="relay-ok",
        ),
        absence=RefusedLaunch(
            because="Approval requests cannot be saved for a reviewer to answer."
        ),
        recovery=f"Check that the launcher can write to `{directory}` in this checkout.",
        install=install,
    )


def preflight_namespace() -> str:
    """Where the checkpoint-store probe writes its ref: a namespace of its own.

    Beside the undo log rather than inside it, so a probe never reads as a
    snapshot, and a function for the reason the undo namespace is one: the
    guard that watches a checkout for refs a suite moved imports it from
    here, so the writer and the reader cannot end up naming two places.
    """
    return "refs/lup/preflight"


def checkpoint_store_requirement(
    where: Side = "host",
    install: list[Package] = [],
    namespace: str = preflight_namespace(),
) -> Requirement:
    """Whether the store a recovery-backed permission rests on accepts a write.

    A destructive operation is permitted where the loss is captured, so the
    capture is the permission. A store that cannot be written turns every one
    of those back into a question, which is the safe direction and a bad
    surprise: the session was opened expecting the other answer.

    Written to a ref of this probe's own under a namespace beside the undo
    log rather than into it, and named for the process so two launches at
    once cannot delete each other's -- a probe that reports the store broken
    because a sibling launch tidied up first is a probe that fails for being
    run twice. Each step's failure fails the exercise, including the delete,
    because a store that accepts a write and refuses a delete is one the
    retention this feeds cannot work in.
    """
    return Requirement(
        capability="checkpoint store",
        purpose="proving a loss was captured before permitting it",
        where=where,
        exercise=Run(
            command=[
                "sh",
                "-c",
                f"r={namespace}/$$ && git update-ref $r HEAD && "
                "git rev-parse --verify --quiet $r > /dev/null && "
                "git update-ref -d $r && printf store-ok",
            ],
            expect="store-ok",
        ),
        absence=LostCapability(
            capability="saving recovery checkpoints before destructive changes"
        ),
        recovery="Check write access to the Git directory and that HEAD resolves to a commit.",
        install=install,
    )


def shell_vocabulary_requirement(
    vocabulary: list[str],
    where: Side = "session",
    at_launch: bool = True,
    install: list[Package] = [
        Package(name="diffutils"),
        Package(name="which"),
        Package(name="tree"),
        Package(name="lsof"),
        Package(name="inetutils"),
        Package(name="go-yq"),
    ],
) -> Requirement:
    """Check the policy's command list where the agent runs.

    ``session`` checks the container for a contained launch and the host
    otherwise. A contained launch does not require these tools on the host.
    ``install`` supplies their image packages; another base can override it.

    The check runs at launch because shell fallbacks can hide command-not-found
    errors. For example, ``cmp -s A B && echo same || echo differs`` prints
    ``differs`` when ``cmp`` is missing, even if the files are identical.
    """
    return Requirement(
        capability="shell commands",
        purpose=(
            "running the shell commands allowed by this project's permission policy"
        ),
        where=where,
        at_launch=at_launch,
        exercise=VocabularyProbe(vocabulary=vocabulary),
        absence=MisleadingAbsence(
            capability="Support for some allowed shell commands",
            mistaken_for=(
                "normal results when shell fallbacks hide a missing-command error"
            ),
        ),
        recovery_by_location={
            "host": "Install the missing commands on the host or add them to PATH.",
            "image": (
                "Check the image's package declarations with "
                "`uv run lup-devtools harness image`. The launcher already builds "
                "changed images; ensure the declared packages provide these commands, "
                "then rerun the launcher."
            ),
        },
        install=install,
    )


def default_manifest() -> Manifest:
    """Every requirement at its offered default -- the batteries-included roster.

    A project with no opinion yet composes this and gets a preflight that
    says true things; one that has an opinion replaces the entries it differs
    on rather than this call. Deliberately carries no JavaScript toolchain:
    most projects on lup have none, and a manifest that invents a
    prerequisite refuses machines that were fine.
    """
    return Manifest(
        requirements=[
            git_requirement(),
            uv_requirement(),
            container_requirement(),
            sandbox_requirement(),
            github_requirement(),
            clipboard_requirement(),
        ]
    )
