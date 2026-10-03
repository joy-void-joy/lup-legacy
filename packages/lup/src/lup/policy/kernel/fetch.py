"""URL scope matching for the fetch policy."""

import ipaddress
import urllib.parse

from .decision import KernelDecision
from .diagnostic import devtools, step
from .rows import UrlScopeRow
from .semantics import UnjudgedAmbient


def host_matches_scope(hostname: str, expected_host: str, subdomains: bool) -> bool:
    """Match a host exactly, or beneath a scope that opted into subdomains.

    The leading dot is required so a scope for ``githubusercontent.com``
    covers ``raw.githubusercontent.com`` without also covering a
    lookalike registration like ``evilgithubusercontent.com``.
    """
    return hostname == expected_host or (
        subdomains and hostname.endswith(f".{expected_host}")
    )


def url_matches_scope(
    scheme: str,
    hostname: str,
    port: int | None,
    path: str,
    scope: UrlScopeRow,
) -> bool:
    """Compare parsed URL components with one primitive scope row."""
    return (
        scheme == scope["scheme"]
        and host_matches_scope(hostname, scope["host"], scope["include_subdomains"])
        and (scope["any_port"] or port == scope["port"])
        and path.startswith(scope["path_prefix"])
    )


# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
SCOPES_HINT = (
    step(
        "see which scopes are declared, and why",
        devtools("dev", "policy", "--kind", "fetch", "<url>"),
    ),
)
"""Where the agent reads the scope table, which the question itself never carries.

The reason once listed every declared scope beside the URL -- twenty-five of
them, four hundred characters -- so the one word that decided the question,
the host, was the hardest to find in it. A scope table is reference: it does
not change from one question to the next, and a line repeated on every
occurrence is read by nobody. It is pulled from the command instead."""


def scope_text(scope: UrlScopeRow) -> str:
    """One declared scope, spelled the way a URL it would admit is spelled.

    Rendered where the scope table is read on purpose -- ``dev policy`` -- so
    the reader sees each scope the way a URL it admits is written, rather than
    as the row's fields.
    """
    host = ("*." if scope["include_subdomains"] else "") + scope["host"]
    port = "" if scope["any_port"] or scope["port"] is None else f":{scope['port']}"
    return f"{scope['scheme']}://{host}{port}{scope['path_prefix']}"


def ipv4_number(hostname: str) -> int | None:
    """The IPv4 address a host names as a number, however it spells one.

    The URL standard's IPv4 host grammar, which curl and every browser read
    by: one to four dot-separated parts, each decimal, `0x` hex or `0`-led
    octal, the last filling every byte the others left, and one trailing dot.
    `169.254.169.254`, `2852039166`, `0xa9fea9fe` and `0251.0376.0251.0376`
    reach one address, so they are read as one. ``None`` for a name.
    """

    def part_number(piece: str) -> int | None:
        """One part's value, in the base its prefix says, or ``None``."""
        match (piece[:2].lower(), piece[:1], len(piece) > 1):
            case ("0x", _, _):
                digits, base = piece[2:] or "0", 16
            case (_, "0", True):
                digits, base = piece[1:], 8
            case _:
                digits, base = piece, 10
        allowed = "0123456789abcdef"[:base]
        if not digits or any(character not in allowed for character in digits.lower()):
            return None
        return int(digits, base)

    # lup: ignore[string-split] — the URL standard's IPv4 host grammar, which no stdlib parser reads
    parts = [part_number(piece) for piece in hostname.removesuffix(".").split(".")]
    numbers = [number for number in parts if number is not None]
    if not 1 <= len(parts) <= 4 or len(numbers) != len(parts):
        return None
    *leading, last = numbers
    if any(number > 255 for number in leading) or last >= 256 ** (5 - len(numbers)):
        return None
    shifted = [number << (8 * (3 - index)) for index, number in enumerate(leading)]
    return sum(shifted) + last


def host_address(name: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a URL's host spells, as the address it reaches, or ``None``.

    An IPv6 form that carries an IPv4 address -- mapped, or under the NAT64
    prefix a translating network sends on -- reaches that IPv4 address, so
    it is read as it.
    """
    if ":" not in name:
        number = ipv4_number(name)
        return None if number is None else ipaddress.IPv4Address(number)
    try:
        spelled = ipaddress.IPv6Address(name)
    except ValueError:
        return None
    if spelled in ipaddress.IPv6Network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(spelled) & 0xFFFFFFFF)
    return spelled.ipv4_mapped or spelled


def metadata_address(
    hostname: str,
    withheld: tuple[str, ...] = (
        "169.254.0.0/16",
        "fd00:ec2::254/128",
        "metadata.google.internal",
    ),
) -> bool:
    """Whether a host is a cloud metadata service, however its address is spelled.

    A metadata service answers whoever asks with the identity and credentials
    of the machine it serves, so what a fetch of one reads is the host's own
    secret. The IPv4 link-local block holds every provider's IPv4 service
    (and ECS's task endpoint beside it); `fd00:ec2::254` is the IPv6 one and
    `metadata.google.internal` the name one provider resolves. An address is
    compared as the address it is, in any spelling; a name that only resolves
    there (`169.254.169.254.nip.io`) is the network's to stop.
    """
    name = hostname.lower().removesuffix(".")
    address = host_address(name)
    if address is None:
        return name in withheld
    return any(
        address in ipaddress.ip_network(entry) for entry in withheld if "/" in entry
    )


def loopback_port(url: str) -> int | None:
    """The port a URL reaches on this machine's own loopback, or ``None``.

    Loopback is ``localhost`` and names beneath it, the IPv4 loopback block,
    and the IPv6 loopback address. A URL naming no port reaches its scheme's
    default; one whose scheme has none, or that does not parse, is not read as
    reaching a port at all.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return None
    local = (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname == "::1"
        or hostname.startswith("127.")
    )
    if not local:
        return None
    if port is not None:
        return port
    match parsed.scheme:
        case "http":
            return 80
        case "https":
            return 443
        case _:
            return None


def decide_fetch(
    url: str,
    allowed_scopes: list[UrlScopeRow],
    denied_scopes: list[UrlScopeRow],
    unscoped: UnjudgedAmbient = "ask",
    host_listener: bool = False,
) -> KernelDecision:
    """Deny matching scopes first, allow declared scopes, and ask otherwise.

    The last of those is the project's answer rather than this function's.
    ``unscoped`` is what an origin no scope names answers: ``ask`` keeps it
    visible, ``defer`` hands it to provider-native judgement. The caller
    resolves it, from the project's fetch declaration or, where there is
    none, from the posture the shell reads for a command the vocabulary has
    no row for -- so a profile that declared the seamless posture gets it on
    every surface rather than on one.

    Only that answer is taken. The rest of the settlement order is not
    consulted here, and the reason is specific to fetch: the rule that
    settles unjudged work inside a boundary does so because every effect the
    operation can have is confined there, and the effect of a fetch is a
    document entering the agent's context. No filesystem or process boundary
    bounds that. A container is exactly as exposed to what an unlisted origin
    says as a bare host is, so containment is not an argument for reading
    one.

    ``host_listener`` is the host's measurement that a loopback URL reaches a
    port some process outside this session's container listens on. A
    container sharing the host's network shares its loopback, so the scope a
    project declares for its own development servers would also admit the
    operator's services; the declared scopes are not consulted for such a URL,
    and it asks whatever ``unscoped`` says, because provider-native judgement
    sees a loopback address and not whose service answers on it.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        return KernelDecision("ask", f"the URL {url!r} does not parse ({error})")
    if not parsed.scheme or hostname is None:
        missing = "scheme" if not parsed.scheme else "host"
        return KernelDecision("ask", f"the URL {url!r} names no {missing}")
    # Read before any scope, since no declaration makes one safe: what it
    # answers is the machine's own identity, which no boundary puts back.
    if metadata_address(hostname):
        return KernelDecision(
            "deny",
            "is a cloud metadata service, which answers with the credentials of"
            " the machine it serves",
            cause="deliberate",
            hard=True,
            recovery=(step("ask the operator for what you needed from it"),),
            subject=hostname,
        )
    denied = next(
        (
            scope
            for scope in denied_scopes
            if url_matches_scope(parsed.scheme, hostname, port, parsed.path, scope)
        ),
        None,
    )
    if denied is not None:
        return KernelDecision("deny", denied["reason"] or "URL is denied")
    allowed = next(
        (
            scope
            for scope in allowed_scopes
            if url_matches_scope(parsed.scheme, hostname, port, parsed.path, scope)
        ),
        None,
    )
    if host_listener:
        return KernelDecision(
            "ask",
            f"{url} reaches a port a process outside this container listens on",
        )
    if allowed is not None:
        return KernelDecision("allow", allowed["reason"])
    outside = f"{url} is outside every declared fetch scope"
    if unscoped == "defer":
        return KernelDecision("defer", outside, abstention="provider_native")
    return KernelDecision("ask", outside, recovery=SCOPES_HINT)
