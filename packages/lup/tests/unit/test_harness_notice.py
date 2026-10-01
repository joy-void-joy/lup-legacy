"""What a launch line looks like, and what decides it.

A session's opening is a block of thirty lines in one weight, and the sentence
that decides whether the session can work sits somewhere inside it. These hold
the two properties that make the block readable: urgency travels with the
sentence rather than being chosen at the print, and the colour is the
terminal's alone -- a redirected launch records the same words with nothing in
them.
"""

from lup.harness.browser import BrowserBridge
from lup.harness.credential import (
    ForgeToken,
    GitAccess,
    GitIdentity,
    InheritedSigning,
    NoCredential,
)
from lup.harness.egress import SessionEgress
from lup.harness.notice import Banner, Ink, Notice, Palette
from lup.harness.requirements import LostCapability, RefusedLaunch, Requirement, Run

OPERATOR = GitIdentity(name="An Operator", email="operator@example.test")
TOKEN = ForgeToken(variable="LUP_GIT_TOKEN")


def probe(refuses: bool) -> Requirement:
    """A requirement whose absence either stops a launch or merely costs it."""
    return Requirement(
        capability="probe",
        purpose="something worth having",
        exercise=Run(command=["definitely-not-a-program-on-this-host"]),
        absence=(
            RefusedLaunch(because="nothing works without it")
            if refuses
            else LostCapability(capability="one convenience")
        ),
    )


def test_a_refusal_and_a_cost_do_not_read_alike() -> None:
    """They were the same sentence on screen, which is the whole complaint.

    A capability whose absence stops the launch and one whose absence costs a
    convenience are different news, and a reader skimming an opening block has
    only the weight of the line to tell them apart.
    """
    stopping = probe(refuses=True).check({}).notices()[0]
    costing = probe(refuses=False).check({}).notices()[0]

    assert stopping.urgency == "refusal"
    assert costing.urgency == "warning"


def test_a_working_requirement_is_the_thing_the_others_stand_out_from() -> None:
    """Colouring only the alarms leaves them nothing to be an exception to.

    Which is an argument about a block somebody asked for. A launch on its
    way past reads :meth:`alarms` instead, where what the one warning stands
    out from is the band heading above it rather than a roster of greens that
    grows every time somebody declares another requirement.
    """
    working = Requirement(
        capability="probe",
        purpose="something worth having",
        exercise=Run(command=["echo", "ok"]),
        absence=LostCapability(capability="one convenience"),
    )

    assert working.check({}).notices()[0].urgency == "ready"
    assert working.check({}).alarms() == []


def test_a_cause_stays_subordinate_to_the_verdict_it_explains() -> None:
    """Read at full weight, a three-line finding becomes three findings."""
    notices = probe(refuses=True).check({}).notices()

    assert notices[0].indent == 0
    assert all(item.indent == 1 for item in notices[1:])


def test_the_reason_a_thing_was_needed_is_never_itself_alarming() -> None:
    """It is context for the line above, and competes with it when painted."""
    assert probe(refuses=True).check({}).notices()[-1].urgency == "detail"


def test_an_ink_says_only_what_it_asks_for() -> None:
    """`False` is the turn-it-off instruction, not the absence of one.

    Passing it emitted two redundant resets on every plain line, which
    renders identically and doubles the noise in anything that reads the
    escapes back.
    """
    painted = Ink(colour="green").paint("working")

    assert painted.count("\x1b[") == 2
    assert "\x1b[32m" in painted


def test_the_warning_colour_is_one_no_ansi_name_offers() -> None:
    """Orange is the shade a warning wants, and it is a 256-colour index."""
    assert "\x1b[38;5;208m" in Palette().warning.paint("no token")


def test_a_palette_is_a_default_rather_than_a_fixture() -> None:
    """Someone else's terminal is not this code's to decide."""
    theirs = Palette(warning=Ink(colour="yellow"))

    assert "\x1b[33m" in Notice(text="careful", urgency="warning").painted(theirs)


def test_indentation_survives_a_terminal_that_takes_no_colour() -> None:
    """The shape of the block is structure, not styling."""
    assert Notice(text="cause", indent=2).painted().endswith("cause\x1b[0m")
    assert Notice(text="cause", indent=2).painted().startswith("        ")


def test_a_launch_with_nothing_wrong_says_nothing_in_the_warning_colour() -> None:
    """Every posture here is doing exactly what it was declared to do.

    Filtered and deliberately unrestricted networks, a bridged browser, and a
    session reaching its forge on a token while signing nothing are declared
    security postures working as configured. None is an action item.
    Painting them orange teaches a reader that the opening block is
    orange whatever happens -- which is the same as having no warning
    colour, paid for at the one launch where something is actually wrong.
    """
    healthy = [
        *SessionEgress().notice("feat"),
        *SessionEgress(mode="host").notice("feat"),
        *BrowserBridge().notice(serving=True),
        *GitAccess().notice(TOKEN, OPERATOR),
    ]

    assert healthy
    assert [item.text for item in healthy if item.urgency == "warning"] == []


def test_the_postures_that_really_do_fail_keep_the_colour() -> None:
    """The other half, without which the one above passes by painting nothing.

    Each of these ends in a failure somewhere that names neither the cause nor
    the remedy: a session that can read a public repository and push to nothing,
    a commit refused for an identity assembled from a hostname, and `gpg: signing
    failed` in the middle of a commit whose key never crossed the boundary.

    A session with no credential at all belongs in this half rather than the
    other one. Placing it among the healthy postures reasons that plenty of
    work never touches a remote, which is true and is not the test:
    `Action required` is for a blocker *or a degradation*, and a session that
    can read and cannot push is the second one.
    """
    loud = [
        *GitAccess().notice(
            NoCredential(variable="LUP_GIT_TOKEN", host="github.com"), OPERATOR
        ),
        *GitAccess().notice(TOKEN, None),
        *GitAccess(signing=InheritedSigning()).notice(TOKEN, OPERATOR),
    ]

    assert sum(item.urgency == "warning" for item in loud) == 3


def test_a_band_keeps_a_subordinate_line_with_the_one_it_is_subordinate_to() -> None:
    """Sorting by urgency alone would file a remedy away from its problem.

    The remediation under an unavailable credential is `detail`, and `detail`
    is not a band -- so a banner that grouped by urgency would drop it under
    no heading, several lines below the sentence it explains.
    """
    banner = Banner()
    banner.add(SessionEgress().notice("feat"))
    banner.add(
        GitAccess().notice(
            NoCredential(
                variable="LUP_GIT_TOKEN",
                host="github.com",
                declined=["no ssh agent holding an identity answers"],
            ),
            OPERATOR,
        )
    )

    action = next(band for band in banner.bands if band.heading == "Action required")
    carried = [item.urgency for item in banner.under(action)]

    assert carried == ["warning", "detail", "detail"]


def test_a_band_with_nothing_to_say_prints_no_heading_at_all() -> None:
    """Which is what makes the shape itself informative.

    A launch showing no `Action required` has nothing requiring action,
    rather than a heading standing over a blank.
    """
    banner = Banner()
    banner.add(GitAccess().notice(TOKEN, OPERATOR))

    action = next(band for band in banner.bands if band.heading == "Action required")

    assert banner.under(action) == []
    assert banner.bands[2].heading == "Session access — informational"
    assert [item.text for item in banner.under(banner.bands[2])] == [
        "Forge access: this session may use the token in LUP_GIT_TOKEN.",
        "Commit signing: off — agent commits do not use your signing identity.",
    ]


def test_nothing_added_to_a_banner_is_ever_dropped() -> None:
    """A band list that forgot an urgency would swallow every line carrying it.

    Which is the failure this cannot have: the launch would read as healthy
    for exactly the reason it was not.
    """
    banner = Banner(bands=[])
    banner.add([Notice(text="unclaimed", urgency="boundary")])

    assert [item.text for item in banner.unbanded()] == ["unclaimed"]
