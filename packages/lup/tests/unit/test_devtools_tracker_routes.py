"""Which repository a devtools forge command reaches, and which it refuses.

The command runs inside an allowed `uv run lup-devtools` invocation, so
nothing downstream of it can ask: whatever it reaches, it reaches
unsupervised. That makes the reachable set the whole of the boundary, and it
is deliberately two things — the repository `origin` names, and the trackers
this project wrote down. Everything else is refused, with the `gh` line that
reaches it under the permission policy, where somebody is asked.

The refusal's route is tested as hard as the refusal. A denial whose escape
hatch has gone stale is worse than one with none: it sends a caller to a
command that no longer does what it says.
"""

import shlex

import pytest

from lup.devtools.dev.issues import (
    TrackerRoutes,
    TrackerVerb,
    disabled_issues_advice,
    issue_arguments,
)
from lup.devtools.project import Tracker
from lup.devtools.utils import names_same_repository, repository_reference

UPSTREAM = Tracker(
    repository="upstream/framework",
    what="the framework this project is built on",
    components=["lup"],
)


def routes(own: str = "acme/widget") -> TrackerRoutes:
    """A downstream checkout that declares its framework's tracker."""
    return TrackerRoutes(own=own, declared=[UPSTREAM])


@pytest.mark.parametrize(
    "spelling",
    [
        "acme/widget",
        "acme/widget.git",
        "github.com/acme/widget",
        "https://github.com/acme/widget",
        "https://github.com/acme/widget.git",
        "git@github.com:acme/widget.git",
        "Acme/Widget",
    ],
)
def test_this_checkout_is_reachable_however_it_is_written(spelling: str) -> None:
    """One repository, every spelling, one answer.

    A caller writes what they have — a slug from `gh`, a URL from a browser,
    a remote out of `git config`. Reading only one of those would refuse the
    repository the command is standing in, which is the answer nobody wants
    to argue with.

    Resolved to the declared spelling rather than the typed one, so what
    reaches `gh` is the pair it takes.
    """
    assert routes().chosen(["issue", "list"], named=spelling) == "acme/widget"


@pytest.mark.parametrize(
    "spelling",
    [
        "upstream/framework",
        "upstream/framework.git",
        "github.com/upstream/framework",
        "https://github.com/upstream/framework",
    ],
)
def test_a_declared_tracker_is_reachable_by_name(spelling: str) -> None:
    """What a project wrote down is what it may reach beyond itself.

    Declared rather than discovered, and the discovery not taken is worth
    naming: a remote called `upstream` means the framework in one checkout
    and a colleague's fork in the next, so a command routed by one would
    reach two repositories under one spelling.
    """
    assert routes().chosen(["issue", "list"], named=spelling) == "upstream/framework"


@pytest.mark.parametrize(
    "elsewhere",
    [
        "someone-else/widget",
        "acme/other",
        "github.com/decoy/acme/widget",
        "acme/widget/../../someone-else/widget",
        "upstream/framework-plugin",
    ],
)
def test_every_other_repository_is_refused(elsewhere: str) -> None:
    """The reachable set is closed, and a near miss is not a member.

    Extra path segments resolve to the pair they end with rather than to one
    they merely contain, so a crafted value cannot wear a declared
    repository's name in the middle of itself. `lup-plugin` is not `lup`.
    """
    with pytest.raises(RuntimeError):
        routes().chosen(["issue", "list"], named=elsewhere)


def test_the_host_discriminates_exactly_when_the_declaration_names_one() -> None:
    """Whether two forges are told apart is the declaring project's call.

    A tracker written `owner/name` means the repository that pair names, and
    a caller who volunteers the host is naming the same thing more
    precisely; refusing them is the defect this surface exists to remove.
    A tracker written `host/owner/name` meant to tell two forges apart, and
    an enterprise host carrying the same owner and name is then what it is —
    a different repository — and is refused.

    `origin` answers for this checkout as a pair, because the host a remote
    carries is not reliably one: an SSH alias puts a name there that only
    the caller's ssh config can resolve, and that is the checkout shape this
    tooling exists to keep working. So the discrimination is available
    between declared trackers, where somebody wrote the host down on
    purpose.
    """
    single = TrackerRoutes(declared=[Tracker(repository="acme/widget", what="both")])
    enterprise = TrackerRoutes(
        declared=[Tracker(repository="ghe.example.com/acme/widget", what="internal")]
    )

    assert single.chosen(["issue", "list"], named="github.com/acme/widget") == (
        "acme/widget"
    )
    assert single.chosen(["issue", "list"], named="ghe.example.com/acme/widget") == (
        "acme/widget"
    )
    assert (
        enterprise.chosen(["issue", "list"], named="ghe.example.com/acme/widget")
        == "ghe.example.com/acme/widget"
    )
    with pytest.raises(RuntimeError):
        enterprise.chosen(["issue", "list"], named="github.com/acme/widget")


def test_an_undeclared_project_reaches_only_the_checkout_it_is_in() -> None:
    """Declaring nothing is a real answer, and it is the shipped one.

    A project whose defects are all its own reaches its own tracker, which is
    what `origin` already said. Nothing is granted by omission.
    """
    alone = TrackerRoutes(own="acme/widget")

    assert alone.chosen(["issue", "list"]) == "acme/widget"
    assert alone.chosen(["issue", "list"], named="acme/widget") == "acme/widget"
    with pytest.raises(RuntimeError) as refused:
        alone.chosen(["issue", "list"], named="upstream/framework")
    assert "declares no other tracker" in str(refused.value)


def test_an_unreadable_origin_grants_nothing() -> None:
    """No repository is not every repository.

    A checkout whose origin names nothing readable has no repository of its
    own to reach, and the declared list is still exactly what it was.
    """
    homeless = TrackerRoutes(own="", declared=[UPSTREAM])

    assert homeless.chosen(["issue", "list"]) == ""
    assert homeless.chosen(["issue", "list"], named="upstream/framework") == (
        "upstream/framework"
    )
    with pytest.raises(RuntimeError):
        homeless.chosen(["issue", "list"], named="acme/widget")


@pytest.mark.parametrize(
    ("component", "expected"),
    [
        ("lup", "upstream/framework"),
        ("lup/policy", "upstream/framework"),
        ("lup.resolver.state", "upstream/framework"),
        ("lup-devtools", "upstream/framework"),
        ("LUP/Sandbox", "upstream/framework"),
        ("lup/sandbox, lup/devtools", "upstream/framework"),
        ("lupine/thing", "acme/widget"),
        ("aib.devtools.trace", "acme/widget"),
        ("", "acme/widget"),
    ],
)
def test_a_report_goes_to_whoever_owns_the_component_it_names(
    component: str, expected: str
) -> None:
    """Routed by what the defect is in, not by where the session was standing.

    Measured downstream: four friction reports, every one of them a defect in
    the framework, all four filed against the consuming repository — where
    the resolver's intake then takes them as evidence for a run that cannot
    reach the code. The components named in those reports are the rows above.

    A prefix claims what continues it at a word boundary, so `lupine` is
    somebody else's and a component nobody claims stays where it was found.
    """
    assert routes().chosen(["issue", "create"], component=component) == expected


def test_a_new_report_a_declared_tracker_claims_is_named_rather_than_filed() -> None:
    """Routing says where a report belongs; the policy decides who is asked.

    The policy reads the words, and an unnamed report reads as one filed on
    this checkout's repository. One whose component a declared tracker claims
    would reach that project's watchers with nobody asked, so it stops,
    carrying the same invocation with `--repo` naming the tracker -- the
    spelling the policy asks about. A component nobody claims has nothing to
    name, and neither does a tracker that is this checkout written another way.
    """
    given = ["uv", "run", "lup-devtools", "dev", "report-friction"]
    given += ["--summary", "it broke", "--component", "lup/policy"]

    message = routes().claimed_elsewhere("lup/policy", given)

    assert shlex.join([*given, "--repo", "upstream/framework"]) in message
    assert "acme/widget" in message
    assert routes().claimed_elsewhere("aib.devtools", given) == ""
    itself = Tracker(repository="github.com/acme/widget", what="us", components=["lup"])
    same = TrackerRoutes(own="acme/widget", declared=[itself])
    assert same.claimed_elsewhere("lup/policy", given) == ""
    # No repository of its own is not this checkout: the tracker is named.
    homeless = TrackerRoutes(own="", declared=[UPSTREAM])
    assert "--repo upstream/framework" in homeless.claimed_elsewhere("lup", given)


def test_an_explicit_repository_outranks_the_component_routing() -> None:
    """Someone who says where it goes has answered the question routing asks."""
    assert (
        routes().chosen(
            ["issue", "create"], named="acme/widget", component="lup/policy"
        )
        == "acme/widget"
    )


def test_the_refusal_carries_the_command_that_does_reach_it() -> None:
    """The route out is the point, so it is spelled rather than described.

    What this command declines is reaching another repository unsupervised.
    `gh` reaches it under the permission policy, which asks — so the refusal
    is a redirection, and one that said "use gh instead" would leave somebody
    rebuilding an invocation they had already written correctly.

    Built from the same words the operation would have run, so the printed
    route cannot drift from the performed one.
    """
    route = issue_arguments("comment", 12, "please look")
    with pytest.raises(RuntimeError) as refused:
        routes().chosen(route, named="someone-else/widget")
    message = str(refused.value)

    assert "gh issue comment 12 --body 'please look' --repo someone-else/widget" in (
        message
    )
    # The reachable set is read out, because "where may this go?" is a
    # question with an answer and being told no is a poor way to learn it.
    assert "acme/widget" in message
    assert "upstream/framework — the framework this project is built on" in message


@pytest.mark.parametrize(
    ("verb", "note", "expected"),
    [
        ("comment", "hello", ["issue", "comment", "7", "--body", "hello"]),
        ("close", "done", ["issue", "close", "7", "--comment", "done"]),
        ("close", "", ["issue", "close", "7"]),
        ("reopen", "back", ["issue", "reopen", "7", "--comment", "back"]),
        ("reopen", "", ["issue", "reopen", "7"]),
    ],
)
def test_each_verb_is_spelled_as_gh_spells_it(
    verb: TrackerVerb, note: str, expected: list[str]
) -> None:
    """A note is the whole point of a comment and optional on the other two."""
    assert issue_arguments(verb, 7, note) == expected


def test_a_tracker_that_takes_no_issues_names_the_ones_that_do() -> None:
    """Measured downstream: the report was lost and the recovery was manual.

    The route existed the whole time. Named rather than taken, because
    re-filing somebody's report on another project's tracker unasked moves it
    out of sight of everyone watching the first one.
    """
    spoken = "the 'acme/widget' repository has disabled issues"

    advice = disabled_issues_advice(spoken, routes())

    assert "upstream/framework" in advice
    assert "--repo" in advice
    # Every other failure keeps the words gh gave it and gains nothing.
    assert disabled_issues_advice("could not resolve to a Repository", routes()) == ""
    # And a project with nowhere else to go is not offered a route it lacks.
    assert disabled_issues_advice(spoken, TrackerRoutes(own="acme/widget")) == ""


@pytest.mark.parametrize(
    ("value", "reference"),
    [
        ("owner/name", "owner/name"),
        ("owner/name.git", "owner/name"),
        ("github.com/owner/name", "github.com/owner/name"),
        ("https://github.com/owner/name.git", "github.com/owner/name"),
        ("https://forge.example:8443/owner/name.git", "forge.example:8443/owner/name"),
        ("forge.example:8443/owner/name", "forge.example:8443/owner/name"),
        (
            "ssh://git@forge.example:2222/owner/name.git",
            "forge.example:2222/owner/name",
        ),
        ("git@github.com:owner/name.git", "github.com/owner/name"),
        ("ssh://git@github.com/owner/name.git", "github.com/owner/name"),
        ("alias:owner/name.git", "alias/owner/name"),
        ("name", ""),
    ],
)
def test_one_reading_for_every_shape_a_repository_is_written_in(
    value: str, reference: str
) -> None:
    """The pair, and the host where one is written at all.

    The scp-like form is why this is parsed here rather than left to `gh`: a
    remote written through an SSH alias names no host `gh` recognizes, and
    every inferred query against it fails as though the repository were
    unreachable.
    """
    assert repository_reference(value) == reference


def test_a_host_is_compared_when_both_sides_name_one() -> None:
    """The host decision, stated on its own.

    Compared when both references carry one, ignored when either does not.
    A caller who volunteers `github.com/owner/name` for a project that
    declared `owner/name` is naming the same repository more precisely, and
    refusing them would be the very defect this surface removes; a project
    that wrote the host meant to tell two forges apart and gets that.

    Well-formed first: `[host/]owner/name` and no longer, so a value with a
    declared pair buried in the middle of it is refused rather than read
    from the end.
    """
    assert names_same_repository("owner/name", "github.com/owner/name")
    assert names_same_repository("github.com/owner/name", "owner/name")
    assert names_same_repository("github.com/owner/name", "github.com/owner/name")
    assert not names_same_repository(
        "ghe.example.com/owner/name", "github.com/owner/name"
    )
    assert not names_same_repository("github.com/decoy/owner/name", "owner/name")
    assert not names_same_repository("owner/name/../../other/name", "owner/name")
    assert not names_same_repository("", "owner/name")
    assert not names_same_repository("owner/name", "")
    assert names_same_repository(
        "https://forge.example:8443/owner/name", "forge.example:8443/owner/name"
    )
    assert not names_same_repository(
        "https://one.example:8443/owner/name", "https://two.example:8443/owner/name"
    )
    for remote in (
        "git@forge.example:decoy/acme/widget.git",
        "forge.example:decoy/acme/widget.git",
        "git@forge.example:1234/acme/widget.git",
    ):
        assert not names_same_repository(remote, "acme/widget")


@pytest.mark.parametrize(
    "malformed",
    [
        "github.com/decoy/acme/widget",
        "acme/widget/../../someone-else/widget",
        "https://github.com/acme/widget/issues/12",
    ],
)
def test_a_reference_deeper_than_a_repository_is_not_one(malformed: str) -> None:
    """`[host/]owner/name` and no longer, which is what gh's --repo takes.

    Read from the end instead, a value carrying a reachable pair somewhere
    inside it would pass for that pair. Refused as malformed rather than
    trimmed, which is also what gh does with it.
    """
    with pytest.raises(RuntimeError):
        routes().chosen(["issue", "list"], named=malformed)
