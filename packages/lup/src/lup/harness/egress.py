"""The network an agent session reaches through, and the one process bridged out.

A container bought without this is half a boundary. The filesystem is scoped
by the mount table and the network is not scoped at all: an ordinary bridge
network reaches the operator's LAN, every port on their localhost, and the
cloud metadata endpoint that hands out credentials to whoever asks. Those are
the three destinations a compromised session would want, and a container that
protects the home directory while leaving them open protects the wrong half.

So the session's network is ``internal`` -- Docker gives it no gateway, so
there is no route out to decline rather than a route the container is asked
not to take -- and one proxy is the only member of both it and the outside.
:class:`~lup.sandbox.egress.EgressPolicy` is what that proxy enforces, and it
is the same declaration the code-execution sandbox already used, pointed at
the session container that replaced it.

**Why the default names no domains.** An allowlist is a deployment fact with
a different answer per adopter: this project reaches PyPI and GitHub, the next
needs crates.io, an npm registry, a distro mirror, or a corporate proxy. A
list baked here would hand each of them a timeout -- the exact vocabulary this
design forbids -- for a destination nobody could have known to name. What the
default *does* refuse is the part that is the same everywhere: the private
ranges, the metadata hosts, and the names a host answers to for itself. A
project whose destinations really are enumerable names them and gets
refuse-by-default.

**What does not honour the variables.** A proxy is a convention, not a
kernel rule, and a component that ignores ``HTTPS_PROXY`` on an internal
network does not fail politely -- it hangs until something times out. That is
knowable in advance rather than discoverable at runtime, so it is declared
here and said at launch. The one that matters most is ssh: it reads none of
these variables, and git over ``git@host:`` is therefore unreachable from a
filtered session. That is not a gap to close later. It is the measurement
§9.2's HTTPS credential exists because of.
"""

from collections.abc import Iterator
from ipaddress import ip_address
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from lup.harness.notice import Notice
from lup.sandbox.egress import EgressPolicy
from lup.sandbox.models import NetworkMode
from lup.types import EnvVars

# lup: ignore[constant-declaration] — an identity this repository defines, not
# a judgement: the writer and the reader have to name the same label key or
# neither works, exactly as the image's own declaration label does
PROXY_LABEL = "lup.egress"
"""The label carrying the digest of the declaration a proxy was started from."""

DEFAULT_PROXY_IMAGE = "ubuntu/squid:6.6-24.04_edge"
"""The image the filtered network is bridged through, pinned rather than latest.

Same image the code-execution sandbox reached for, and pinned for the reason
every other version in this harness is: a proxy that silently became a
different build is a boundary that silently changed what it lets through.
"""


class Unproxied(BaseModel, frozen=True):
    """A component that ignores the proxy variables, named rather than discovered.

    Declared because the failure has no vocabulary of its own. A component
    that honours ``HTTPS_PROXY`` and is refused gets a 403 the proxy wrote and
    :mod:`lup.sandbox.attribution` can explain; a component that ignores it
    resolves the name itself, on a network whose resolver was deliberately
    taken away, and fails in DNS's vocabulary instead -- naming neither the
    proxy nor the boundary. Nothing afterwards can attribute ``Temporary
    failure in name resolution`` to a decision somebody made on purpose, so
    the only place this can be said is before it happens.
    """

    component: str = Field(description="What ignores the variables")
    reason: str = Field(description="Why it does, in its own terms")
    consequence: str = Field(description="What is unreachable as a result")

    def sentence(self) -> str:
        """One line naming this component and what it costs, for the launch.

        Unindented: subordination is the notice's to express, so a line that
        carried its own two spaces would be indented twice over.
        """
        return f"{self.component}: {self.reason} — {self.consequence}"


SSH_IGNORES_THE_PROXY = Unproxied(
    component="ssh",
    reason=(
        "reads none of the proxy variables and resolves the host itself, "
        "which this network has no resolver to answer"
    ),
    consequence=(
        "a direct `ssh`, `scp` or `rsync -e ssh` fails at name resolution in "
        "milliseconds; a git remote spelled for ssh is rewritten to HTTPS at "
        "launch, which is what the credential design assumes"
    ),
)
"""The measurement the credential design rests on, kept where a launch says it."""


class AllowedHost(BaseModel, frozen=True):
    """One destination the boundary admits, and what made it necessary.

    The motivation is the field that matters. A boundary answered by widening
    its own declaration teaches an agent that every wall is answered by
    widening it, and mounts and allowed hosts then accrete with nothing ever
    removing one -- each entry perfectly defensible when written and nobody
    afterwards able to say whether it is still needed. Recording the command
    that motivated it makes the second question answerable: an entry whose
    command nobody runs any more is an entry to take out, and the check that
    reads the proxy's log can say which have not been reached at all.

    A bare hostname still parses, so widening the list in a hurry stays one
    word -- and shows up in the check as an entry that never said why.
    """

    host: str = Field(description="The domain the proxy admits")
    because: str = Field(
        default="",
        description=(
            "The command or capability that needed it. Empty is accepted and "
            "reported, because refusing it would make the honest answer -- "
            "'I do not remember' -- unwritable"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    # lup: ignore[bare-object] — pydantic hands a before-hook whatever the
    # caller wrote, which is the untyped boundary the rule says to narrow at
    def a_bare_name_is_a_host_nobody_said_why_about(cls, value: object) -> object:
        """Accept the shortest spelling, and let the check ask for the rest."""
        return {"host": value} if isinstance(value, str) else value


class SessionEgress(BaseModel, frozen=True):
    """How one agent session reaches the network, declared with what enforces it.

    One model rather than two for the same reason :class:`Image` is one: the
    network name the session attaches to and the network the proxy is
    connected to have to be the same string, and the way that stays true is
    that one declaration answers both.
    """

    mode: NetworkMode = Field(
        default="filtered",
        description=(
            "``filtered`` puts the session on an internal network whose only "
            "way out is the proxy; ``bridge`` gives it the host's ordinary "
            "network and no egress boundary at all; ``host`` gives it the "
            "host's network namespace itself; ``none`` cuts it off entirely, "
            "which no session that installs a dependency survives. Filtered "
            "by default because a container whose network is unscoped "
            "protects the home directory and leaves the LAN, localhost, and "
            "the metadata endpoint open. ``host`` is the widest and the only "
            "one a loopback flow completes under: a callback the session "
            "listens for at `127.0.0.1` is the *host's* `127.0.0.1`, so a "
            "browser on the operator's machine reaches it, which under every "
            "other mode it cannot -- the same sharing that lets a dev server "
            "started inside answer on the host without a published port, and "
            "lets the session reach whatever else is bound there"
        ),
    )
    policy: EgressPolicy = Field(
        default=EgressPolicy(),
        description=(
            "The denial vocabulary and the ports, which are the same for "
            "every adopter. What is *admitted* is not set here -- "
            "`admits` below owns it, and :meth:`enforced` overwrites this "
            "field's allowlist with what that says, so there is no second "
            "way to widen the boundary that records no reason for widening it"
        ),
    )
    admits: list[AllowedHost] = Field(
        default=[],
        description=(
            "Destinations this project reaches, each with what needed it. "
            "Empty keeps the permissive posture: every public destination "
            "reachable, the private ranges and metadata hosts still refused. "
            "That is the default because an allowlist is a deployment fact "
            "with a different answer per adopter, and a baked one hands the "
            "next project a timeout for a registry nobody could have known "
            "to list. Naming any host flips the posture to refuse-by-default"
        ),
    )
    reached_directly: list[str] = Field(
        default=["localhost", "127.0.0.1", "::1"],
        description=(
            "Destinations a session reaches without the proxy, written into "
            "``NO_PROXY`` rather than inherited from the host. The session's "
            "own loopback, and only that: an agent that runs a dev server "
            "and curls it is reaching a port inside its own container, which "
            "no boundary here was ever meant to stand between. Widening this "
            "to a real destination would be widening the boundary, because "
            "an exempt name is one the proxy never sees -- which is why the "
            "default names nothing the container could not already reach"
        ),
    )
    resolvers: list[str] = Field(
        default=[],
        description=(
            "Nameservers the *proxy* is given, overriding whatever the "
            "engine would write into it. Empty asks the host for its own, "
            "which is the right answer almost everywhere and is a fact about "
            "the machine rather than the declaration -- so it is resolved at "
            "launch and passed, never written here.\n\n"
            "This exists because of what an internal network's resolver "
            "correctly does. A container attached to one is given that "
            "network's resolver first, and it answers names on that network "
            "and refuses everything else *authoritatively* -- which is what "
            "an internal network should do. glibc takes the first "
            "authoritative answer and stops, so every server listed after it "
            "is hidden, including the working one. Measured: a proxy with a "
            "default route, `192.168.0.1` third in its list, resolving its "
            "own network's names and answering `503` to every CONNECT.\n\n"
            "Only the proxy is overridden. The session must go on resolving "
            "the proxy's alias through the internal resolver, which is the "
            "one name it needs and the one that resolver holds"
        ),
    )
    proxy_image: str = DEFAULT_PROXY_IMAGE
    resolves_names: bool = Field(
        default=False,
        description=(
            "Whether the session's internal network runs a resolver at all. "
            "Off, and the reason is the whole of why the first contained "
            "sessions could not reach anything.\n\n"
            "A resolver on an internal network correctly refuses every name "
            "outside it, and refuses *authoritatively*. The proxy is on that "
            "network too, glibc stops at the first authoritative answer, and "
            "so the one server that could have resolved a public name was "
            "never consulted -- measured, with the proxy holding a default "
            "route, `192.168.0.1` third in its list, and answering `503` to "
            "every CONNECT. Handing the proxy its own nameservers did not "
            "settle it either, because joining the network rewrites the file "
            "they were written into.\n\n"
            "The resolver was only ever there to turn one name into one "
            "address, so the shorter answer is to do that where the address "
            "is already known. Nothing else on that network has a name worth "
            "resolving, and a network with no resolver has nothing to shadow "
            "with. A project that puts named services on this network turns "
            "it back on and accepts what it does to the proxy"
        ),
    )
    unproxied: list[Unproxied] = Field(
        default=[SSH_IGNORES_THE_PROXY],
        description=(
            "Components known not to honour the proxy variables. Said at "
            "launch, because a component that ignores them hangs rather than "
            "failing and leaves nothing behind to attribute"
        ),
    )

    def filtered(self) -> bool:
        """Whether this declaration puts a proxy between the session and the world."""
        return self.mode == "filtered"

    def carries_ssh(self) -> bool:
        """Whether a transport that ignores the proxy variables can leave at all.

        The measurement this module's own header records, asked as a
        question rather than repeated as a comparison: a proxy is a
        convention and ssh does not follow it, so under ``filtered`` a
        session has no route to port 22 and no resolver to find one with,
        and under ``none`` it has no route to anything. An ssh credential is
        selectable exactly where the answer is yes -- which is not a
        preference about credentials but a fact about the network, and is
        why the credential ladder asks the egress before it probes a key.

        ``bridge`` and ``host`` both carry it, for different reasons that
        make no difference here: one has a gateway of its own and the other
        is the host's own stack.
        """
        return self.mode in {"bridge", "host"}

    def shares_host_loopback(self) -> bool:
        """Whether `127.0.0.1` names the same interface on both sides.

        Asked because a loopback flow either completes or cannot, and the two
        want opposite sentences from anything that narrates one. A sign-in
        redirects the operator's browser to a port the session is listening
        on; under every other mode the container holds its own network
        namespace and that browser reaches its own machine's loopback, where
        nothing is listening, so the flow needs a second half done by hand.
        Sharing the namespace makes the two the same port and the redirect
        lands, which is not a nicety -- it is the difference between an
        instruction the operator must follow and one that would be a lie.

        A question about the posture rather than a member of it, so callers
        ask rather than comparing against a mode name. The comparison spelled
        at each of them is how a fourth mode arrives and two of the three
        keep saying what was true before it.
        """
        return self.mode == "host"

    def joins_no_network(self) -> bool:
        """Whether the session is cut off from every network, its loopback alone left."""
        return self.mode == "none"

    def enforced(self) -> EgressPolicy:
        """The policy the proxy is actually given, allowlist and all.

        One place the two halves are joined, so nothing else has to remember
        that ``admits`` is where hosts are named and ``policy`` is where
        everything else is. A declaration that set ``policy.allowed_domains``
        directly is overwritten rather than merged: two ways to widen the
        boundary is one way too many, and the one that survives is the one
        that records why.
        """
        return self.policy.model_copy(
            update={"allowed_domains": [item.host for item in self.admits] or None}
        )

    def declaration(self, resolvers: list[str]) -> str:
        """Everything about this proxy that a change to should replace it.

        The counterpart to the image's own declaration label, and it exists
        for the same measured reason. An image is rebuilt when what it is
        declared to be moves; a proxy was reused whatever the declaration
        said, so every later edit -- the policy, the resolvers, the image --
        landed in the repository and never in the container a session
        actually reached. Nothing reported it, because from the outside a
        stale proxy and a current one are one running name.

        The rendered policy rather than the path it is written to: the path
        is this checkout's scratch directory and would make two worktrees
        disagree about a proxy they share. Same rule as everywhere else here
        -- what is compared has to be what was declared, never where this
        machine happened to put it.
        """
        return "\n".join(
            [
                self.mode,
                self.proxy_image,
                str(self.resolves_names),
                *resolvers,
                self.enforced().render(),
            ]
        )

    def network_name(self, project: str) -> str:
        """The internal network this project's sessions attach to."""
        return f"lup-egress-net-{project}"

    def proxy_name(self, project: str) -> str:
        """The proxy container bridging that network to the outside."""
        return f"lup-egress-{project}"

    def environment(self, address: str = "") -> EnvVars:
        """The variables pointing a session's toolchain at its only way out.

        Both cases are set because the tools disagree about which they read,
        and ``NO_PROXY`` is *written* rather than inherited, so nothing
        arrives from the host claiming an exemption that the internal network
        could not honour anyway.

        Written rather than emptied, which is the same argument carried one
        step further. Emptying it took the host's exemptions away and the one
        exemption that is always right along with them: a client honours
        these variables for ``localhost`` too, so a session that started its
        own dev server and reached for it sent the request to the proxy --
        measured, ``curl -v http://localhost:3000`` answering ``Uses proxy
        env variable http_proxy`` -- where squid refused it twice over, as a
        denied local name and as a port outside 80 and 443. Nothing was
        protected by that. The session container is on a network with no
        gateway, so it cannot reach the host's loopback whatever the proxy
        says, and the denial only ever stopped it reaching *its own*
        services by name.

        ``address`` is where the proxy sits on the internal network, read
        back after it is connected and passed in. An address rather than a
        name, and the earlier argument for a name -- that the address is not
        known until the network exists -- was simply false: this environment
        is assembled after the proxy has been started and joined, which is
        the point at which the engine can be asked. Believing it cost a
        resolver on the internal network, whose refusal of public names stood
        in front of the one server that could answer them.

        An empty address returns nothing rather than a URL pointing nowhere.
        A session with no proxy variables fails at its first request in the
        client's own words; one pointed at a proxy that is not there fails in
        the proxy's, which is a sentence about a boundary that was never
        built.

        ``NODE_USE_ENV_PROXY`` is here because the CLI this harness launches
        is itself a Node program, and Node honours these variables only when
        asked: from 22.21 and 24.5 the flag routes ``fetch``, ``node:http``
        and ``node:https`` through them, and without it the session's own API
        calls go straight onto a network with no gateway. A filtered session
        would then fail at the one thing it exists to do, slowly, with a
        timeout for a diagnostic.
        """
        if not self.filtered() or not address:
            return {}
        proxy = f"http://{address}:{self.policy.listen_port}"
        direct = ",".join(self.reached_directly)
        return {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "ALL_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "all_proxy": proxy,
            "NO_PROXY": direct,
            "no_proxy": direct,
            "NODE_USE_ENV_PROXY": "1",
        }

    def attachment_arguments(self, project: str) -> list[str]:
        """The run arguments putting a session container on this network.

        ``bridge`` and ``none`` are spelled too rather than left to the
        engine's default, so a declaration that turned the boundary off says
        so in the argv a reader can see instead of by the absence of a flag.
        """
        network = self.network_name(project) if self.filtered() else self.mode
        return ["--network", network]

    def network_arguments(self, project: str, digest: str = "") -> list[str]:
        """The argv creating the internal network, for a caller to run.

        ``--disable-dns`` unless a project asked for names, and that flag is
        the repair rather than a tuning. A resolver on this network is given
        to every container on it, the proxy included, and an internal
        network's resolver refuses public names authoritatively -- so it
        stood in front of the one server that could have answered.

        Labelled with the same declaration digest the proxy carries, because
        a network is reused across launches exactly as a proxy is and would
        otherwise keep whatever posture it was first created under. That is
        not hypothetical: this very flag would never have reached a machine
        whose network already existed, which is the third time in this design
        a fix has landed in the repository and not in the thing a session
        reaches.
        """
        return [
            "network",
            "create",
            "--internal",
            *([] if self.resolves_names else ["--disable-dns"]),
            "--label",
            f"{PROXY_LABEL}={digest}",
            self.network_name(project),
        ]

    def resolvers_for(self, resolv_conf: str) -> list[str]:
        """The nameservers to hand the proxy, given what the host holds.

        The declaration wins where it says anything; otherwise the host's own
        are read out of the text a caller passes -- text rather than a path,
        so the rule is testable without a machine that happens to be
        configured the right way.

        Loopback addresses are dropped, and that is the case this filter
        exists for rather than a tidy-up: a host running `systemd-resolved`
        names `127.0.0.53`, which resolves perfectly there and is a different
        machine's loopback as far as a container is concerned. Handing it
        over would replace a resolver that refuses public names with one that
        cannot be reached at all.
        """
        if self.resolvers:
            return self.resolvers

        def named() -> Iterator[str]:
            for line in resolv_conf.splitlines():
                words = line.split()
                if len(words) < 2 or words[0] != "nameserver":
                    continue
                try:
                    address = ip_address(words[1])
                except ValueError:
                    continue
                if not address.is_loopback:
                    yield words[1]

        return list(dict.fromkeys(named()))

    def proxy_arguments(
        self,
        project: str,
        configuration: Path,
        resolvers: list[str] | None = None,
        digest: str = "",
    ) -> list[str]:
        """The argv starting the proxy, bridged out and stripped of everything else.

        Started on the engine's ordinary network, which is the half that
        reaches the world; :meth:`connect_arguments` adds the other half.
        Every capability but the four it needs to drop privileges and bind
        its port is removed, because this container is the one process with a
        route out of the session's network and is therefore the one worth
        hardening.

        Deliberately **not** ``--rm``, and the absence is the point. A
        detached container that removes itself on exit is one whose logs are
        gone by the time anybody asks, and ``run --detach`` returns zero the
        moment the container is *created* -- so a proxy whose process dies a
        moment later reports a clean start and leaves nothing behind. Measured
        exactly that: a filtered session opened, the launch said the boundary
        was up, the proxy was already gone, and three passes read it as a name
        that would not resolve. A corpse costs nothing and is the only copy of
        why. :func:`~lup.launch.container.start_egress` reads it and
        then clears it, so at most one is ever kept.
        """
        return [
            "run",
            "--detach",
            "--name",
            self.proxy_name(project),
            "--label",
            f"{PROXY_LABEL}={digest}",
            *[word for address in resolvers or [] for word in ("--dns", address)],
            "--network",
            "bridge",
            "-v",
            f"{configuration}:/etc/squid/squid.conf:ro",
            "--memory",
            "256m",
            "--pids-limit",
            "128",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            self.proxy_image,
        ]

    def connect_arguments(self, project: str) -> list[str]:
        """The argv joining the proxy to the internal network.

        No alias. An alias is a DNS record, this network has no resolver, and
        podman records one on a network that cannot serve it -- its own
        manual says so, which is exactly how the first launch connected
        without complaint and the name never worked. A flag that is accepted
        and does nothing is worse than one that is refused.
        """
        return [
            "network",
            "connect",
            self.network_name(project),
            self.proxy_name(project),
        ]

    def teardown_arguments(self, project: str) -> list[list[str]]:
        """Every argv that removes this project's egress infrastructure, in order.

        The proxy first: a network with a container still attached refuses to
        go, and the resulting diagnostic names the network rather than the
        thing holding it.
        """
        return [
            ["rm", "--force", self.proxy_name(project)],
            ["network", "rm", self.network_name(project)],
        ]

    def notice(self, project: str) -> list[Notice]:
        """What a launch says about the network the session is about to get.

        Every line here is one the friction table demands: the posture, what
        the posture refuses, what fails in some other vocabulary than the
        proxy's, and how to take the infrastructure back down. A boundary
        nobody was told about is one whose refusals get debugged as something
        else.

        Each of them is *one* line, and that is the part worth holding. Four
        sentences from the unfiltered posture on why a shared loopback is
        what lets a sign-in redirect land would be true, and rationale,
        and printed at every launch above the sentence that decides whether
        the session can work. What a reader needs here is
        which posture is in force and what it exposes; why it exposes that is
        this module's header and ``docs/permissions.md``.
        """
        if not self.filtered():
            shared = (
                ", and services on your machine's localhost"
                if self.shares_host_loopback()
                else ""
            )
            return [
                Notice(
                    text=(
                        "Network access: unrestricted — this session can reach "
                        "the internet, LAN and cloud metadata"
                        f"{shared}."
                    ),
                    urgency="boundary",
                )
            ]
        scoped = (
            f"only {', '.join(item.host for item in self.admits)}"
            if self.admits
            else "any public destination"
        )
        return [
            Notice(
                text=(
                    f"Network access: filtered through {self.proxy_name(project)} — "
                    f"{scoped}. Private networks, cloud metadata hosts and local "
                    "names are refused."
                ),
                urgency="boundary",
            ),
            *(
                [
                    Notice(
                        text=(
                            "These ignore the proxy and cannot reach anything "
                            "through it:"
                        ),
                        urgency="boundary",
                    )
                ]
                if self.unproxied
                else []
            ),
            *[
                Notice(text=item.sentence(), urgency="boundary", indent=1)
                for item in self.unproxied
            ],
            Notice(
                text=(
                    "It outlives this session so the next one does not pay for "
                    "it again; `harness egress --down` removes it."
                ),
                urgency="detail",
            ),
        ]
