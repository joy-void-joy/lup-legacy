"""What the session's network boundary must keep true.

The container is the filesystem boundary; what a test here protects is the
network half, without which the mount table says one thing and the network
says nothing at all. Each assertion below corresponds to a way that gap can
silently open: a session attached to
no network, a proxy the session cannot resolve, a component routed around the
boundary because nobody told it the boundary was there.
"""

from pathlib import Path

import pytest
import sh

import lup.launch.container as contained
from lup.launch.refusal import LaunchRefused
from lup.harness.egress import AllowedHost, PROXY_LABEL, SessionEgress, Unproxied
from lup.harness.image import Docker, Image
from lup.harness.requirements import Manifest
from lup.sandbox.egress import EgressPolicy
from lup_template.harness.content.image import agent_image


def test_a_session_is_attached_to_the_internal_network_by_name() -> None:
    """Run arguments always name a network, and the internal one by name.

    A container started without ``--network`` gets the engine's default
    bridge, which reaches the operator's LAN and every port on their
    localhost. Nothing about that failure is visible -- the session works
    perfectly, with no boundary.
    """
    arguments = Image().run_arguments(Path("/repo/tree/feat"), 1000, 1000, Docker())
    assert "--network" in arguments
    assert arguments[arguments.index("--network") + 1] == "lup-egress-net-feat"


def test_a_declaration_that_turns_the_boundary_off_says_so_in_the_argv() -> None:
    """An absent flag and a deliberate choice must not look the same.

    ``bridge`` is spelled out rather than left to the engine's default, so
    the difference between "this project chose no egress boundary" and "the
    launcher forgot one" is readable in the command that was run.
    """
    image = Image(egress=SessionEgress(mode="bridge"))
    arguments = image.run_arguments(Path("/repo/tree/feat"), 1000, 1000, Docker())
    assert arguments[arguments.index("--network") + 1] == "bridge"


def test_the_widest_posture_is_the_one_a_loopback_flow_finishes_under() -> None:
    """Sharing the namespace is what makes `127.0.0.1` mean one interface.

    A sign-in redirects the operator's browser at a loopback port the session
    is listening on, and whether that browser reaches it is this field's
    answer and nothing else's. `bridge` is not enough and reads as though it
    were, which is why the mode and the question asked of it are pinned
    together rather than each on its own.
    """
    image = Image(egress=SessionEgress(mode="host"))
    arguments = image.run_arguments(Path("/repo/tree/feat"), 1000, 1000, Docker())

    assert arguments[arguments.index("--network") + 1] == "host"
    assert SessionEgress(mode="host").shares_host_loopback()
    assert not SessionEgress(mode="bridge").shares_host_loopback()
    assert not SessionEgress().shares_host_loopback()


def test_this_repository_runs_its_sessions_on_the_host_network() -> None:
    """The trade this repository makes, pinned where the argv it produces is.

    The filter admits every public destination anyway, so what it costs is
    ssh and a sign-in that cannot finish, and what it buys is a denial of
    this machine's own network. Named in a test so that reversing it is a
    decision somebody makes rather than a default that drifted home.
    """
    assert agent_image().egress.mode == "host"


def test_the_session_is_pointed_at_the_proxy_it_is_the_only_way_out_through() -> None:
    """An internal network with unset proxy variables is a session with no network.

    Both the boundary and the route through it come from one declaration, so
    a session cannot be attached to the internal network without also being
    told how to reach the proxy that is its only member with a way out.

    By address rather than by name. Addressing it by a DNS alias puts a
    resolver on the internal network, and an internal network's resolver
    refuses every public name authoritatively — so it stands in front of the
    one server the proxy could resolve through, and nothing in the session
    reaches anything.
    """
    environment = SessionEgress().environment("10.89.0.29")
    assert environment["HTTPS_PROXY"] == "http://10.89.0.29:3128"
    assert environment["https_proxy"] == environment["HTTPS_PROXY"]
    # Written rather than emptied: emptying keeps the host's exemptions out
    # and takes the session's own loopback with them, so an agent that starts
    # a dev server and curls it is answered by the proxy.
    assert environment["NO_PROXY"] == "localhost,127.0.0.1,::1"


def test_node_is_told_to_read_the_proxy_variables_it_otherwise_ignores() -> None:
    """The CLI this harness launches is a Node program, and Node opts in.

    Measured against Node's own release notes: from 22.21 and 24.5,
    ``NODE_USE_ENV_PROXY`` routes ``fetch``, ``node:http`` and ``node:https``
    through the proxy variables, and without it they are ignored entirely. On
    an internal network that means the session's own API calls go to a
    network with no gateway and wait -- the boundary breaking the one thing
    the session exists to do, with a timeout for a diagnostic.
    """
    assert SessionEgress().environment("10.89.0.29")["NODE_USE_ENV_PROXY"] == "1"


def test_an_unfiltered_declaration_sets_no_proxy_variables() -> None:
    """A proxy variable pointing at a proxy nobody started is worse than none."""
    assert SessionEgress(mode="bridge").environment() == {}


def test_the_proxy_reaches_the_outside_and_the_session_reaches_the_proxy() -> None:
    """The two halves that make one process the only bridge.

    The proxy starts on the engine's ordinary network -- that is the half
    with a route out -- and is joined to the internal network afterwards.
    Either half alone is a boundary with nothing behind it.

    No alias on the join. An alias is a DNS record, this network has no
    resolver, and podman records one on a network that cannot serve it — its
    own manual says so — so a launch connects without complaint and the name
    never works. A flag accepted and ignored is worse than one refused.
    """
    egress = SessionEgress()
    started = egress.proxy_arguments("feat", Path("/repo/tmp/egress.conf"))
    assert started[started.index("--network") + 1] == "bridge"
    connected = egress.connect_arguments("feat")
    assert connected == [
        "network",
        "connect",
        "lup-egress-net-feat",
        "lup-egress-feat",
    ]


def test_the_network_the_proxy_joins_is_the_one_the_session_attaches_to() -> None:
    """One string, asked for twice. Two spellings is a session with no route."""
    egress = SessionEgress()
    attached = egress.attachment_arguments("feat")[1]
    assert attached in egress.connect_arguments("feat")
    assert attached in egress.network_arguments("feat")


def test_the_network_is_created_internal() -> None:
    """The whole posture rests on this flag.

    Without ``--internal`` the engine gives the network a gateway, and the
    session has a route out that merely happens not to be the proxy -- a
    boundary that is asked rather than enforced.
    """
    assert "--internal" in SessionEgress().network_arguments("feat")


def test_the_proxy_is_stripped_of_every_capability_it_does_not_need() -> None:
    """It is the one process with a route out, so it is the one worth hardening."""
    started = SessionEgress().proxy_arguments("feat", Path("/c"))
    assert started[started.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges:true" in started


def test_the_default_policy_refuses_the_destinations_every_adopter_shares() -> None:
    """Naming no domains is not naming no boundary.

    The default is permissive about *public* destinations, because an
    allowlist is a deployment fact with a different answer per adopter and a
    baked one hands the next project a timeout for a registry nobody could
    have known to list. What it still refuses is the part that is the same
    everywhere, and that is what would be lost if "no domains" were read as
    "no policy".
    """
    rendered = SessionEgress().policy.render()
    assert "http_access deny forbidden_destinations" in rendered
    assert "169.254.0.0/16" in rendered
    assert "metadata.google.internal" in rendered
    assert "http_access allow all" in rendered


def test_naming_domains_flips_the_policy_to_refuse_by_default() -> None:
    """A project whose destinations really are enumerable gets the strict posture."""
    egress = SessionEgress(policy=EgressPolicy(allowed_domains=["pypi.org"]))
    rendered = egress.policy.render()
    assert "http_access allow allowed_domains" in rendered
    assert "http_access deny all" in rendered


def test_the_launch_names_what_fails_outside_the_proxys_vocabulary() -> None:
    """The one failure the boundary cannot attribute after the fact.

    A refused request leaves a proxy log line :mod:`lup.sandbox.attribution`
    can read. A component that ignores the variables resolves the name itself
    on a network whose resolver was deliberately taken away, and fails in
    DNS's vocabulary -- naming neither the proxy nor the boundary -- so before
    it happens is the only time it can be said.

    What it must not claim is that such a component hangs. Measured inside a
    session, it does not: the network is created `--disable-dns`, so `ssh`
    fails at name resolution in milliseconds and never opens a socket to wait
    on. A warning about a wait nobody will ever see is a warning that teaches
    the reader to expect the wrong symptom.
    """
    lines = "\n".join(item.text for item in SessionEgress().notice("feat"))
    assert "ssh" in lines
    assert "name resolution" in lines


def test_an_unfiltered_launch_says_what_it_is_leaving_open() -> None:
    """Turning the boundary off is a posture, and a posture is announced."""
    notices = SessionEgress(mode="bridge").notice("feat")
    lines = "\n".join(item.text for item in notices)
    assert {item.urgency for item in notices} == {"boundary"}
    assert "unrestricted" in lines
    assert "metadata" in lines
    host_lines = "\n".join(
        item.text for item in SessionEgress(mode="host").notice("feat")
    )
    assert "your machine's localhost" in host_lines


def test_teardown_removes_the_proxy_before_the_network_holding_it() -> None:
    """Reversed, the engine refuses and names the network rather than the cause."""
    argv = SessionEgress().teardown_arguments("feat")
    assert argv[0][:2] == ["rm", "--force"]
    assert argv[1][:2] == ["network", "rm"]


def test_a_declared_unproxied_component_reaches_the_launch_notice() -> None:
    """The declaration is the only channel, so an addition must surface itself."""
    egress = SessionEgress(
        unproxied=[Unproxied(component="rsync", reason="speaks ssh", consequence="no")]
    )
    assert any("rsync" in item.text for item in egress.notice("feat"))


def test_the_egress_environment_is_not_baked_into_the_image() -> None:
    """A posture in a layer costs a distribution rebuild to change.

    The paths and the project environment really are facts about what was
    built. Which network the session runs on is not, and baking it would mean
    flipping one variable invalidated every layer above the base.
    """
    rendered = Image().dockerfile(Manifest())
    assert "HTTPS_PROXY" not in rendered
    # The control, so this is checking a file the image facts really are in.
    # Quoted, because a baked value with whitespace in it has to be and the
    # renderer does not special-case which ones do.
    assert 'LUP_CONTAINED="1"' in rendered


def test_a_running_proxy_off_the_network_is_repaired_rather_than_believed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running is not the same as reachable.

    A `start_egress` returning as soon as the proxy is *running* would find a
    proxy that has lost its place on the internal network — or never taken
    one, the connect half having failed after the run half succeeded —
    running, and leave it exactly as it is, on that launch and every launch
    afterwards.

    What that costs is why it is worth a test rather than a comment. The
    session addresses its only way out by an alias on that network, so every
    request fails at DNS, and the runtime reports it as the operator's own
    internet or DNS being broken. Nothing in that sentence mentions a proxy.
    """
    issued = engine_answering(
        monkeypatch,
        # The network is there, the proxy is up and carries the declaration in
        # force — and it is on the engine's default bridge and nothing else,
        # which is exactly the state that reads as healthy from every cheaper
        # question a launcher could ask instead.
        networks="bridge ",
    )
    contained.start_egress(SessionEgress(), "feat", Docker(), tmp_path)

    # Among what was issued rather than last: the address a session is
    # pointed at is read back after the join, so the join is no longer the
    # final word.
    assert any(argv[:3] == ["docker", "network", "connect"] for argv in issued)


def test_a_proxy_already_on_the_network_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Idempotent, so a second launch neither restarts nor reconnects anything.

    Reconnecting a container that is already connected is an error on both
    engines, so a repair that did not first ask would turn every launch after
    the first into a refusal.
    """
    issued = engine_answering(monkeypatch, networks="bridge lup-egress-net-feat ")
    contained.start_egress(SessionEgress(), "feat", Docker(), tmp_path)

    assert not any("connect" in argv for argv in issued)
    assert not any("run" in argv for argv in issued)


def test_a_proxy_that_starts_and_stops_is_reported_in_its_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run --detach` answers whether the container was created, nothing more.

    Squid reads its configuration at startup and exits on a line it will not
    accept, and by then the launcher has a zero exit code and has already
    said the boundary is up. Measured: a session opening onto an internal
    network with nothing bridged out of it, and three passes reading that as
    a name that would not resolve.
    """

    def spelled(binary: str):
        def call(*words: str, **_: object) -> str:
            # Not running, and with something to say about why.
            if words[0] == "inspect":
                return "exited"
            if words[0] == "logs":
                return "FATAL: Bungled /etc/squid/squid.conf line 6"
            return ""

        return call

    monkeypatch.setattr(sh, "Command", spelled)
    with pytest.raises(LaunchRefused) as refusal:
        contained.settled(SessionEgress(), "feat", Docker(), Path("egress.conf"), 0)

    assert "Bungled" in str(refusal.value)


def test_a_proxy_that_stayed_up_is_not_complained_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the grace window is that passing it says nothing."""

    def spelled(binary: str):
        def call(*words: str, **_: object) -> str:
            return "true" if words[0] == "inspect" else ""

        return call

    monkeypatch.setattr(sh, "Command", spelled)
    contained.settled(SessionEgress(), "feat", Docker(), Path("egress.conf"), 0)


def test_a_dead_proxy_is_read_before_it_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleared because the name blocks the restart; read because it is the only copy.

    A launch that refused on the name collision would be reporting the corpse
    rather than the death, and one that removed it silently would leave the
    next reader with nothing to read.
    """
    issued: list[list[str]] = []

    def spelled(binary: str):
        def call(*words: str, **_: object) -> str:
            issued.append([binary, *words])
            return {
                "inspect": "exited",
                "logs": "FATAL: Bungled /etc/squid/squid.conf line 6",
            }.get(words[0], "")

        return call

    monkeypatch.setattr(sh, "Command", spelled)
    said = contained.departed(SessionEgress(), "feat", Docker())

    assert any("Bungled" in item.text for item in said)
    assert [argv[1] for argv in issued] == ["inspect", "logs", "rm"]


def test_the_state_asks_the_proxy_what_it_can_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session resolving the proxy says nothing about the proxy resolving.

    The two happen on different networks and only one of them is the
    session's. Measured: `egress proxy resolves: working` beside a CONNECT
    answered `503`, which is squid saying it cannot reach the origin — a
    failure entirely downstream of the alias the session resolves.
    """

    def spelled(binary: str):
        def call(*words: str, **_: object) -> str:
            if words[0] == "exec" and "getent" in words:
                return "160.79.104.10   api.anthropic.com"
            if words[0] == "exec":
                return "nameserver 10.89.0.1\nsearch dns.podman"
            if "DNSEnabled" in words:
                return "true"
            if words[0] == "inspect" and "{{.State.Status}}" in words:
                return "running"
            return "lup-egress-net-feat egress " if words[0] == "inspect" else ""

        return call

    monkeypatch.setattr(sh, "Command", spelled)
    state = contained.egress_state(SessionEgress(), "feat", Docker())

    assert state.reached
    assert state.resolver == "10.89.0.1"
    assert "api.anthropic.com" in state.upstream


def test_a_proxy_that_resolves_nothing_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case a 503 on CONNECT actually poses, told apart from a denial.

    Squid answers a request it refused with 403 and one it could not complete
    with 503, and the second covers both a name it could not resolve and an
    origin it could not reach. Only the proxy itself can say which.
    """

    def spelled(binary: str):
        def call(*words: str, **_: object) -> str:
            if words[0] == "exec" and "getent" in words:
                # getent exits 2 on a name it cannot find and says nothing,
                # which is the case this is about.
                raise sh.ErrorReturnCode_2("getent", b"", b"")
            if words[0] == "exec":
                return "nameserver 10.89.0.1"
            if "DNSEnabled" in words:
                return "true"
            if words[0] == "inspect" and "{{.State.Status}}" in words:
                return "running"
            return ""

        return call

    monkeypatch.setattr(sh, "Command", spelled)
    state = contained.egress_state(SessionEgress(), "feat", Docker())

    assert not state.reached
    assert "api.anthropic.com" in state.upstream


def test_a_reachable_proxy_that_reaches_nothing_is_not_called_working() -> None:
    """The verdict has to cover both legs, which writing two answers does not.

    Reaching the proxy and the proxy reaching the world fail separately. A
    headline reporting only the first says the boundary is fine while the
    proxy resolves nothing — the exact shape of failure this whole report
    exists to catch, reproduced inside it.
    """
    standing = contained.EgressState(
        network="net",
        proxy="proxy",
        network_exists=True,
        dns_enabled=True,
        proxy_exists=True,
        proxy_running=True,
        attached=True,
        address="10.89.0.29",
        reached=False,
    )

    assert standing.addressable()
    assert not any(item.urgency == "ready" for item in standing.verdict())
    assert any("public DNS lookup failed" in item.text for item in standing.verdict())


def test_a_proxy_with_no_gateway_anywhere_is_told_so_plainly() -> None:
    """Membership answers yes and the proxy still reaches nothing.

    An `--internal` network is precisely one with no gateway, so a proxy
    attached to the session's network and to nothing else passes every
    cheaper question and cannot resolve a name — which is the state a reader
    would otherwise have to infer from a resolver list and a 503.
    """
    stranded = working_proxy(route="", reached=False)

    assert not stranded.routes()
    assert any("no default route" in item.text for item in stranded.verdict())


def test_a_recorded_gateway_is_not_taken_for_a_route() -> None:
    """A recorded gateway and a route come apart on a real machine.

    Both of the proxy's legs record a gateway — the internal one reporting its
    own `.1` address, which podman populates whether or not anything routes
    through it — while the container reaches nothing. Only one network
    provides a container's default route and netavark installs none for an
    internal network, so membership says yes, metadata says yes, and every
    packet fails instantly.

    So the verdict asks the routing table. A per-leg gateway stays in the
    report because it is worth seeing; it is not what decides.
    """
    recorded = contained.EgressState(
        network="net",
        proxy="proxy",
        legs=[
            contained.NetworkLeg(
                network="podman", address="10.88.0.4", gateway="10.88.0.1"
            ),
            contained.NetworkLeg(
                network="net", address="10.89.0.29", gateway="10.89.0.1"
            ),
        ],
    )

    assert not recorded.routes()
    assert recorded.model_copy(update={"route": "via 10.88.0.1 on eth1"}).routes()


def test_the_default_route_is_read_in_the_kernel_s_own_byte_order() -> None:
    """`/proc/net/route` is little-endian hex, which is where a reader goes wrong.

    `0100580A` is 10.88.0.1 and not 1.0.88.10, and a route reported backwards
    is worse than none: it looks like an answer.
    """
    table = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth1\t00000000\t0100580A\t0003\t0\t0\t100\t00000000\n"
        "eth0\t0000590A\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
    )

    assert contained.default_route(table) == "via 10.88.0.1 on eth1"


def test_a_table_with_no_default_answers_that_rather_than_guessing() -> None:
    """Two networks and no way off either, which is the state under suspicion.

    Every per-network gateway can be populated while this row is absent:
    only one network provides the default route and netavark installs none
    for an internal one, so the metadata and the table disagree exactly here.
    """
    table = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t0000590A\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
        "eth1\t0000580A\t00000000\t0001\t0\t0\t0\t0000FFFF\n"
    )

    assert contained.default_route(table) == ""


def test_an_unreadable_table_is_not_read_as_a_route() -> None:
    """A proxy that could not be asked is not a proxy that answered `none`.

    Both render the same way here and that is acceptable — the surrounding
    report says whether the container is running — but neither may raise,
    since a diagnostic that dies on a missing file diagnoses nothing.
    """
    assert contained.default_route("") == ""
    assert contained.default_route("Iface\tDestination\tGateway\n") == ""
    assert contained.default_route("garbage\n\n  \n") == ""


def working_proxy(**differing: object) -> "contained.EgressState":
    """A proxy with every question but the named ones answering well.

    Nested defaults rather than a fixture, because what each case is about is
    the one or two fields it overrides — and a reader of the case should not
    have to hold eleven others in mind to see which.
    """
    return contained.EgressState.model_validate(
        {
            "network": "net",
            "proxy": "proxy",
            "network_exists": True,
            "dns_enabled": True,
            "proxy_exists": True,
            "proxy_running": True,
            "attached": True,
            "address": "10.89.0.29",
            "legs": [contained.NetworkLeg(network="net", address="10.89.0.29")],
            "route": "via 10.88.0.1 on eth0",
            "resolver": "10.89.0.1 192.168.0.1",
            "answers_locally": True,
            "reached": True,
            **differing,
        }
    )


def test_internal_dns_success_does_not_identify_the_public_lookup_failure() -> None:
    """A failed public lookup does not establish which resolver caused it."""
    shadowed = working_proxy(reached=False)

    assert any(
        "Test the listed nameservers individually" in item.text
        for item in shadowed.verdict()
    )
    assert not any("authoritatively" in item.text for item in shadowed.verdict())


def test_a_proxy_that_resolves_nothing_at_all_is_not_called_shadowed() -> None:
    """Two of the clauses is a guess, and guesses are what this keeps refuting.

    A chain that answers nothing — not even its own network's names — is a
    different fault with a different repair, and saying "stopping early"
    about it would send a reader to change resolvers that were never
    consulted.
    """
    silent = working_proxy(reached=False, answers_locally=False)

    assert not silent.shadowed()


def test_a_proxy_with_no_route_is_not_called_shadowed_either() -> None:
    """Nor is one that cannot reach any resolver it lists."""
    stranded = working_proxy(reached=False, route="")

    assert not stranded.shadowed()
    assert any("no default route" in i.text for i in stranded.verdict())


def test_squid_s_own_errors_are_kept_above_the_traffic_that_drowns_them() -> None:
    """A busy proxy writes one access line per request and few error lines.

    Measured: twenty rows of `TCP_TUNNEL/503` repeating, with the line saying
    why sitting above the window. A plain tail shows the half that repeats.
    """
    said = "\n".join(
        ["FATAL: something went wrong"]
        + [
            f"175500000{n}.1 0 10.89.0.38 TCP_TUNNEL/503 0 CONNECT h:443"
            for n in range(9)
        ]
    )

    def spelled(binary: str):
        def call(*_words: str, **_kw: object) -> str:
            return said

        return call

    original = sh.Command
    sh.Command = spelled  # pyright: ignore[reportAttributeAccessIssue]
    try:
        kept = contained.proxy_log("proxy", Docker())
    finally:
        sh.Command = original  # pyright: ignore[reportAttributeAccessIssue]

    assert kept.splitlines()[0] == "FATAL: something went wrong"


def test_the_proxy_is_given_the_host_s_own_nameservers() -> None:
    """The repair for a resolver chain that answers "no" and hides the rest.

    A container on an internal network is given that network's resolver
    first, and it answers names on that network and refuses everything else
    authoritatively — correctly, since that is what an internal network is.
    glibc takes the first authoritative answer and stops, so the working
    server listed after it is never consulted.
    """
    handed = SessionEgress().resolvers_for("nameserver 192.168.0.1\n")
    argv = SessionEgress().proxy_arguments("feat", Path("e.conf"), handed)

    assert handed == ["192.168.0.1"]
    assert argv[argv.index("--dns") + 1] == "192.168.0.1"


def test_a_loopback_nameserver_is_not_handed_to_a_container() -> None:
    """`systemd-resolved` names 127.0.0.53, which is a different machine there.

    The case this filter exists for rather than a tidy-up: handing it over
    would replace a resolver that refuses public names with one that cannot
    be reached at all, which is a worse failure and a quieter one.
    """
    assert SessionEgress().resolvers_for("nameserver 127.0.0.53\n") == []
    assert SessionEgress().resolvers_for("nameserver ::1\nnameserver 1.1.1.1") == [
        "1.1.1.1"
    ]


def test_a_host_with_nothing_usable_is_told_rather_than_left_to_find_out() -> None:
    """Falling back to the engine's own resolv.conf is the broken state.

    So it is announced at the launch that chooses it, not discovered later as
    a boundary standing over a proxy that carries nothing.
    """
    assert contained.handed_resolvers(["192.168.0.1"]) == []
    assert any("loopback" in item.text for item in contained.handed_resolvers([]))


def test_a_declared_resolver_overrides_what_the_host_names() -> None:
    """An adopter whose host answers badly says so once, in the declaration."""
    declared = SessionEgress(resolvers=["9.9.9.9"])

    assert declared.resolvers_for("nameserver 192.168.0.1") == ["9.9.9.9"]


def test_no_resolvers_means_no_flag_rather_than_an_empty_one() -> None:
    """An engine handed `--dns` with nothing after it refuses the whole run."""
    assert "--dns" not in SessionEgress().proxy_arguments("feat", Path("e.conf"))


def engine_answering(
    monkeypatch: pytest.MonkeyPatch,
    networks: str,
    resolv: str = "nameserver 192.168.0.1\n",
) -> list[list[str]]:
    """An engine reporting a running proxy started from the declaration in force.

    The label has to be the *current* digest rather than a fixture string,
    because what these cases are about is the launcher reusing a proxy it
    recognises — pinning a literal would make them pass while the comparison
    is broken, which is the failure a declaration digest exists to catch.

    The host's resolv.conf is pinned for the same reason in reverse: it feeds
    the digest, so reading the real one would make the answer depend on the
    machine running the test.
    """
    monkeypatch.setattr(contained, "host_resolv_conf", lambda *_: resolv)
    current = contained.declaration_digest(
        SessionEgress().declaration(SessionEgress().resolvers_for(resolv))
    )
    issued: list[list[str]] = []

    def spelled(binary: str):
        def call(*words: str, **_: object) -> str:
            issued.append([binary, *words])
            if words[0] != "inspect":
                return ""
            if "{{.State.Running}}" in words:
                return "true"
            if PROXY_LABEL in " ".join(words):
                return current
            return networks

        return call

    monkeypatch.setattr(sh, "Command", spelled)
    return issued


def test_a_proxy_from_an_older_declaration_is_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A proxy follows its declaration the way an image does.

    An image is rebuilt when its declaration moves. A proxy reused whatever
    its declaration says would leave a change to it — a `--dns` flag, say —
    landing nowhere: the launch finds the proxy running and leaves it exactly
    as it is, while reporting the boundary it was supposed to have. From
    outside, a stale proxy and a current one are one running name.
    """
    issued = engine_answering(monkeypatch, networks="bridge lup-egress-net-feat ")

    def stale(binary: str):
        def call(*words: str, **_: object) -> str:
            issued.append([binary, *words])
            if words[0] != "inspect":
                return ""
            if "{{.State.Running}}" in words:
                return "true"
            if "{{.State.Status}}" in words:
                return "running"
            if PROXY_LABEL in " ".join(words):
                return "a digest from some earlier declaration"
            return "bridge lup-egress-net-feat "

        return call

    monkeypatch.setattr(sh, "Command", stale)
    contained.start_egress(SessionEgress(), "feat", Docker(), tmp_path)

    assert any(argv[1] == "rm" for argv in issued)
    started = next(argv for argv in issued if argv[1] == "run")
    assert "--dns" in started
    assert started[started.index("--dns") + 1] == "192.168.0.1"


def test_the_declaration_moves_with_what_a_proxy_would_be_started_from() -> None:
    """Policy, resolvers and image all count; the scratch path does not.

    The path is this checkout's `tmp/` and would make two worktrees disagree
    about a proxy they share — the same rule that took the checkout out of
    the ownership digest.
    """
    base = SessionEgress()

    assert base.declaration(["1.1.1.1"]) != base.declaration(["9.9.9.9"])
    assert base.declaration([]) != SessionEgress(
        proxy_image="ubuntu/squid:other"
    ).declaration([])
    admitting = SessionEgress(admits=[AllowedHost(host="pypi.org")])
    assert base.declaration([]) != admitting.declaration([])
    assert base.declaration(["1.1.1.1"]) == SessionEgress().declaration(["1.1.1.1"])


def test_a_network_from_an_older_declaration_is_rebuilt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The third thing a launch could reuse whatever the declaration says, and the widest.

    A network is created once and outlives every launch, so the posture it
    was created under is the posture it keeps. A flag stopping its resolver
    shadowing the proxy's would never reach a machine whose network already
    exists — which would swallow the same change for the other two as well.
    """
    issued: list[list[str]] = []
    # Stateful, because the sequence under test is remove-then-create and a
    # fake that reported the network present throughout would let a launch
    # skip the create and still look right.
    removed: list[bool] = []

    def spelled(binary: str):
        def call(*words: str, **_: object) -> str:
            issued.append([binary, *words])
            if words[:2] == ("network", "rm"):
                removed.append(True)
                return ""
            if words[:2] == ("network", "inspect"):
                if removed:
                    raise sh.ErrorReturnCode_1("network inspect", b"", b"no such")
                return "a digest from some earlier declaration"
            return ""

        return call

    monkeypatch.setattr(contained, "host_resolv_conf", lambda *_: "nameserver 1.1.1.1")
    monkeypatch.setattr(sh, "Command", spelled)
    # The rebuilt proxy never comes up under this fake, so the launch refuses
    # after its grace window. What is under test is everything before that.
    with pytest.raises(LaunchRefused):
        contained.start_egress(SessionEgress(), "feat", Docker(), tmp_path)

    # The proxy goes before the network holding it, then the network is made
    # again carrying the declaration in force.
    assert ["docker", "rm", "--force"] in [argv[:3] for argv in issued]
    assert ["docker", "network", "rm"] in [argv[:3] for argv in issued]
    created = next(
        argv for argv in issued if argv[:3] == ["docker", "network", "create"]
    )
    assert "--disable-dns" in created
    assert created[created.index("--label") + 1].startswith("lup.egress=")


def test_a_network_carrying_the_current_declaration_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rebuilding one every launch would take the proxy with it every time."""
    resolv = "nameserver 192.168.0.1\n"
    monkeypatch.setattr(contained, "host_resolv_conf", lambda *_: resolv)
    current = contained.declaration_digest(
        SessionEgress().declaration(SessionEgress().resolvers_for(resolv))
    )
    issued: list[list[str]] = []

    def spelled(binary: str):
        def call(*words: str, **_: object) -> str:
            issued.append([binary, *words])
            if words[:2] == ("network", "inspect"):
                return current
            if words[0] == "inspect" and "{{.State.Running}}" in words:
                return "true"
            if words[0] == "inspect" and PROXY_LABEL in " ".join(words):
                return current
            if words[0] == "inspect":
                return "bridge lup-egress-net-feat "
            return ""

        return call

    monkeypatch.setattr(sh, "Command", spelled)
    contained.start_egress(SessionEgress(), "feat", Docker(), tmp_path)

    assert not any(argv[:3] == ["docker", "network", "rm"] for argv in issued)
    assert not any(argv[:3] == ["docker", "network", "create"] for argv in issued)
