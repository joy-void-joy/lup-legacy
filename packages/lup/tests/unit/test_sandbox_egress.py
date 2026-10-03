"""The filtered sandbox's egress policy and the proxy rules it compiles to."""

from lup.sandbox.egress import EgressPolicy


def rule_order(rendered: str) -> list[str]:
    return [line for line in rendered.splitlines() if line.startswith("http_access")]


def test_the_permissive_posture_allows_what_it_has_not_denied() -> None:
    rendered = EgressPolicy().render()

    assert rule_order(rendered)[-1] == "http_access allow all"


def test_naming_domains_flips_the_posture_to_refuse_by_default() -> None:
    rendered = EgressPolicy(allowed_domains=[".pypi.org"]).render()

    assert "acl allowed_domains dstdomain .pypi.org" in rendered
    assert rule_order(rendered)[-2:] == [
        "http_access allow allowed_domains",
        "http_access deny all",
    ]


def test_destination_denials_precede_any_allow() -> None:
    # Load-bearing rather than cosmetic: a permitted hostname can resolve to a
    # private address, and an allowlist consulted first would wave it through.
    order = rule_order(EgressPolicy(allowed_domains=[".pypi.org"]).render())

    assert order.index("http_access deny forbidden_destinations") < order.index(
        "http_access allow allowed_domains"
    )


def test_cloud_metadata_is_refused_by_address_not_only_by_name() -> None:
    # The address rule is what holds, because a caller asking numerically
    # never consults a name list.
    rendered = EgressPolicy().render()

    assert "acl forbidden_destinations dst 169.254.0.0/16" in rendered
    assert "metadata.google.internal" in rendered


def test_the_ipv6_analogues_are_refused_too() -> None:
    rendered = EgressPolicy().render()

    for private in ("fc00::/7", "fe80::/10", "::1/128"):
        assert f"acl forbidden_destinations dst {private}" in rendered


def test_ports_are_allowlisted_and_tunnels_narrower_still() -> None:
    rendered = EgressPolicy().render()

    assert "http_access deny !Safe_ports" in rendered
    assert "http_access deny CONNECT !SSL_ports" in rendered
    assert "acl SSL_ports port 443" in rendered
    assert "acl Safe_ports port 22" not in rendered


def test_a_caller_may_narrow_the_reachable_ports() -> None:
    rendered = EgressPolicy(allowed_ports=(443,), tunnel_ports=(443,)).render()

    assert "acl Safe_ports port 80" not in rendered
    assert "acl Safe_ports port 443" in rendered


def test_nothing_is_cached_between_runs() -> None:
    assert "cache deny all" in EgressPolicy().render()


def test_the_default_names_hold_no_pair_squid_would_refuse() -> None:
    """A line that would stop the proxy, refused as a standing assertion.

    Measured: with both `localhost` and `.localhost` in `LOCAL_NAMES`, squid
    answers `FATAL: Bungled ... line 6` and exits, the proxy container removes
    itself, and what is left reads as a proxy name that will not resolve. A
    fatal configuration error is not something a boundary can carry, because
    the boundary is simply not there afterwards.
    """
    rendered = EgressPolicy().render()
    (names,) = [
        line for line in rendered.splitlines() if line.startswith("acl forbidden_names")
    ]
    entries = names.split()[3:]

    assert entries
    assert not [
        inner
        for outer in entries
        for inner in entries
        if inner != outer
        and outer.startswith(".")
        and inner.endswith(outer.lstrip("."))
    ]


def test_an_apex_beside_its_dotted_form_is_reduced_to_the_dotted_one() -> None:
    """Reduced rather than refused, because the dotted form loses nothing.

    A leading dot matches the apex too, so the shorter list denies exactly
    what the longer one meant. Squid's own advice on this error is to remove
    the dotted entry, which would keep the apex and drop every subdomain —
    the narrower half, and the wrong one.
    """
    policy = EgressPolicy(denied_local_names=("localhost", ".localhost", ".local"))

    assert policy.distinct(policy.denied_names) == [
        "metadata.google.internal",
        "metadata.goog",
        "instance-data.ec2.internal",
        "metadata.azure.com",
        ".localhost",
        ".local",
    ]


def test_a_subdomain_beside_its_parent_is_reduced_to_the_parent() -> None:
    """The same rule reaching the case an adopter is likelier to write."""
    policy = EgressPolicy()

    assert policy.distinct([".example.com", "www.example.com", "other.net"]) == [
        ".example.com",
        "other.net",
    ]


def test_names_that_only_look_alike_are_both_kept() -> None:
    """`.local` does not cover `.localhost`, however the strings end.

    A suffix test alone would drop one of these, which would quietly stop
    denying a name the declaration asked to deny — the failure direction that
    matters, since nothing downstream would report it.
    """
    assert EgressPolicy().distinct([".local", ".localhost"]) == [".local", ".localhost"]


def test_an_admitted_pair_is_reduced_too() -> None:
    """Squid does not care which list a redundancy came from.

    Widening the boundary in a hurry is exactly when somebody writes both an
    apex and its dotted form, and a fatal parse there takes the whole proxy
    down rather than the one entry.
    """
    rendered = EgressPolicy(allowed_domains=["pypi.org", ".pypi.org"]).render()
    (admitted,) = [
        line for line in rendered.splitlines() if line.startswith("acl allowed_domains")
    ]

    assert admitted == "acl allowed_domains dstdomain .pypi.org"


def test_the_icmp_helper_is_off_because_the_proxy_has_no_net_raw() -> None:
    """The other way this configuration stopped the proxy before it served.

    The helper needs NET_RAW, which the proxy deliberately lacks — it is the
    one process bridged out of the session's network and is hardened for it.
    Left on it dies at startup on `Unable to open any ICMP sockets`, which
    reads as squid failing rather than as a helper measuring round-trip time
    between cache peers, of which there are none here.
    """
    assert "pinger_enable off" in EgressPolicy().render()
