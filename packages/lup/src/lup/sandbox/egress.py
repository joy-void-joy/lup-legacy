"""What a filtered sandbox may reach, and the proxy configuration it compiles to.

The sandbox in ``filtered`` mode has no route out of its own network; one
proxy is the only process bridged to both sides, so this declaration is the
entire boundary. It is a declaration rather than a configuration file because
the posture is a judgement its caller owns: an agent that installs arbitrary
packages and reads arbitrary documentation needs the open internet, while a
sandbox that only ever reaches one index should refuse everything else. Baked
in either direction, the other consumer has to fork it.

Two orderings in the rendered rules are load-bearing rather than stylistic.
The destination denies sit above any allow, because a permitted hostname can
resolve to a private address and an allowlist alone would wave it through.
And ports are allowlisted while destinations are not, because the ports a
sandbox legitimately needs are enumerable and its destinations are not.
"""

from collections.abc import Iterator, Sequence

from pydantic import BaseModel, Field

# The IANA special-purpose address registry (RFC 6890 and the RFCs it
# collects), denied wholesale rather than curated: a published registry is a
# rule a reviewer can check, where a hand-picked subset invites the question
# of what was left out. Roughly half of these are unreachable in practice and
# cost nothing to keep; the ones carrying real weight are noted.
IANA_SPECIAL_PURPOSE: tuple[str, ...] = (
    "0.0.0.0/8",  # RFC 1122 "this" network
    "10.0.0.0/8",  # RFC 1918 private — the operator's own LAN
    "100.64.0.0/10",  # RFC 6598 CGNAT — some hosts put their LAN here
    "127.0.0.0/8",  # RFC 1122 loopback
    "169.254.0.0/16",  # RFC 3927 link-local — cloud metadata, credentials
    "172.16.0.0/12",  # RFC 1918 private — contains Docker's default bridge
    "192.0.0.0/24",  # RFC 6890 IETF protocol assignments
    "192.0.2.0/24",  # RFC 5737 TEST-NET-1
    "192.168.0.0/16",  # RFC 1918 private — the operator's own LAN
    "198.18.0.0/15",  # RFC 2544 benchmarking
    "198.51.100.0/24",  # RFC 5737 TEST-NET-2
    "203.0.113.0/24",  # RFC 5737 TEST-NET-3
    "224.0.0.0/4",  # RFC 5771 multicast
    "240.0.0.0/4",  # RFC 1112 reserved
    "::/128",  # RFC 4291 unspecified
    "::1/128",  # RFC 4291 loopback
    "2001:db8::/32",  # RFC 3849 documentation
    "fc00::/7",  # RFC 4193 unique-local — the v6 RFC 1918
    "fe80::/10",  # RFC 4291 link-local — the v6 169.254
)

# Belt-and-braces only. Every cloud's metadata endpoint answers on an address
# inside 169.254.0.0/16 (Alibaba's inside 100.64.0.0/10), which the
# destination rules already refuse — and a caller asking numerically walks
# past any name list. These are listed so a denial reads intelligibly in the
# proxy log, not because they are what holds.
CLOUD_METADATA_HOSTS: tuple[str, ...] = (
    "metadata.google.internal",
    "metadata.goog",
    "instance-data.ec2.internal",
    "metadata.azure.com",
)

# The names a host answers to for itself, which no address range expresses.
#
# Dotted forms only, and that is load-bearing rather than terse. A leading dot
# in `dstdomain` matches the apex as well as every subdomain, so `.localhost`
# already covers the bare name -- and squid treats the redundancy as fatal:
#
#   ERROR: '.localhost' is a subdomain of 'localhost'
#   FATAL: Bungled /etc/squid/squid.conf line 6
#
# Squid exits on that line, the proxy container removes itself, and every
# reader downstream sees a proxy name that does not resolve: a contained
# session with no way out.
#
# Squid's own advice there is to remove `.localhost`, and following it would
# be wrong -- that drops subdomain coverage and keeps only the apex, which is
# the narrower half. The entry to remove is the bare one.
LOCAL_NAMES: tuple[str, ...] = (".localhost", ".local")


class EgressPolicy(BaseModel, frozen=True):
    """What a filtered sandbox may reach.

    Leaving ``allowed_domains`` unset keeps the permissive posture: everything
    not denied is reachable, which is what an agent doing open-ended work
    needs. Naming domains flips it to refuse-by-default, for a sandbox whose
    destinations are known in advance.
    """

    allowed_domains: list[str] | None = Field(
        default=None,
        description=(
            "Domains reachable when set, with everything else refused; unset "
            "permits anything the denials below do not catch"
        ),
    )
    denied_destinations: tuple[str, ...] = IANA_SPECIAL_PURPOSE
    denied_metadata_hosts: tuple[str, ...] = CLOUD_METADATA_HOSTS
    denied_local_names: tuple[str, ...] = LOCAL_NAMES
    allowed_ports: tuple[int, ...] = (80, 443)
    tunnel_ports: tuple[int, ...] = (443,)
    listen_port: int = 3128

    @property
    def denied_names(self) -> tuple[str, ...]:
        """Every hostname this policy refuses, from both of its lists."""
        return self.denied_metadata_hosts + self.denied_local_names

    def distinct(self, names: Sequence[str]) -> list[str]:
        """One ``dstdomain`` list with nothing in it that another entry covers.

        Squid refuses a ``dstdomain`` ACL holding both a name and a dotted
        form that contains it, and refuses it *fatally* -- the proxy does not
        start. So this is not tidying: an adopter who adds ``example.com``
        beside an existing ``.example.com`` would otherwise get a boundary
        that silently is not there, which is the one failure this design
        forbids.

        Dropping the covered entry loses nothing, because a leading dot
        matches the apex too. That is what makes normalising the right answer
        rather than refusing: no declaration here means something the shorter
        list does not.

        Applied to what is admitted as well as to what is denied, since both
        render as ``dstdomain`` and squid does not care which list a
        redundancy came from.
        """

        def covers(outer: str, inner: str) -> bool:
            """Whether *outer* already matches everything *inner* would.

            Only a dotted entry covers anything but itself: ``.local`` covers
            ``local`` and ``foo.local``, while ``local`` covers only itself.
            Nested rather than free-standing because the rule is squid's
            matching semantics and has no meaning away from this call.
            """
            if not outer.startswith("."):
                return False
            return inner == outer[1:] or inner.endswith(outer)

        unique = list(dict.fromkeys(names))
        return [
            name
            for name in unique
            if not any(other != name and covers(other, name) for other in unique)
        ]

    def render(self) -> str:
        """Compile this policy into the proxy configuration enforcing it."""

        def lines() -> Iterator[str]:
            yield f"http_port {self.listen_port}"
            for port in self.allowed_ports:
                yield f"acl Safe_ports port {port}"
            for port in self.tunnel_ports:
                yield f"acl SSL_ports port {port}"
            yield "acl CONNECT method CONNECT"
            forbidden = self.distinct(self.denied_names)
            if forbidden:
                yield f"acl forbidden_names dstdomain {' '.join(forbidden)}"
            for destination in self.denied_destinations:
                yield f"acl forbidden_destinations dst {destination}"
            admitted = self.distinct(self.allowed_domains or [])
            if admitted:
                yield f"acl allowed_domains dstdomain {' '.join(admitted)}"
            # Denials first, so a permitted name that resolves to a private
            # address is still refused.
            if forbidden:
                yield "http_access deny forbidden_names"
            if self.denied_destinations:
                yield "http_access deny forbidden_destinations"
            yield "http_access deny !Safe_ports"
            yield "http_access deny CONNECT !SSL_ports"
            if admitted:
                yield "http_access allow allowed_domains"
                yield "http_access deny all"
            else:
                yield "http_access allow all"
            # One run's fetch is never served to the next as though fresh.
            yield "cache deny all"
            # The ICMP helper needs NET_RAW, which the proxy deliberately does
            # not have -- it is the one process bridged out of the session's
            # network and is hardened accordingly. Left enabled it fails at
            # startup with three lines reading `FATAL: Unable to open any ICMP
            # sockets`, which is a helper dying rather than squid, and which
            # cost a pass being read as the cause of something else. What it
            # measures is round-trip time between cache peers, of which there
            # are none here.
            yield "pinger_enable off"
            yield "access_log stdio:/dev/stdout"
            yield "cache_log /dev/stderr"
            yield "pid_filename /tmp/squid.pid"
            yield "coredump_dir /tmp"

        return "\n".join(lines()) + "\n"
