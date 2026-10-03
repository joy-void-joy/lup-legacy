"""The container this repository's agent sessions run in.

Mechanism from the library, composition here, exactly as `requirements.py`
splits them: `lup.harness.image` says what an image declaration *is*, and what
is this project's is which defaults it takes and which it overrules.

It overrules two and fills one. The one it fills is `tooling`: the library
ships it empty because what a project's own work needs inside the image is
the one thing a library cannot guess, and it is the third door a package
reaches an image through — `baseline` being what any shell session needs, and
a `Requirement` being what a declared capability asked for and something
exercises.

It overrules two. The first is the network. The library defaults
to a filtered proxy because an adopter's first session should not reach
whatever is on their network before anybody has decided it may. This repository
has decided. That filter admits every public destination, so it does not
stand between a session and the internet; what it stands between is a session
and this machine's own loopback, and the price is that HTTP is the only
transport crossing it -- which is what rewrites every ssh remote, and what
leaves a sign-in redirecting to a port no browser on this machine can reach.
Sharing the namespace hands both back and gives up the denial of the LAN, which
is a trade about who runs this repository and on what, so it is made here
rather than in a library with no way to know either.

The second is where the forge token comes from. The library reads a variable
and stops, because an adopter's stored login is scoped to whatever they signed
in with and taking it uninvited is not a library's call. This repository is
developed on machines that authenticate `gh` by signing in rather than by
exporting a token, and the cost of leaving it out is not a missing convenience:
`git pr create`, `dev issues` and `dev report-friction` are how this repository
says work is finished and how it records that its own tooling misbehaved, and
inside the boundary none of them reaches the API without it. The
friction-reporting loop in particular cannot report its own breakage.

What that grant adds over what a session already holds is the part worth
naming. These sessions carry a forwarded ssh agent, which can sign anything
that key can sign for every host it reaches -- so repository access is not
what is withheld. What the login adds is the rest of the account's scopes,
`admin:public_key` and `workflow` among them. Exporting a fine-grained token
narrows it again without changing anything here, since the variable is read
first.
"""

from lup.harness.credential import GitAccess
from lup.harness.egress import SessionEgress
from lup.harness.image import Image
from lup.harness.requirements import Package


def agent_image() -> Image:
    """This repository's image: the library's offer, on this machine's network.

    A function rather than the model itself, so a project that later needs to
    resolve something against its own checkout has somewhere to do it without
    changing what the catalog calls.

    Notably *not* a place to enumerate the worktrees under `tree/`. Doing so
    fills `trusted_projects` with every directory name there, some of them
    not checkouts, bakes one host's filesystem layout into the image, and
    rebuilds a layer whenever a worktree comes or goes.
    Trust belongs to the checkout a container is started against, which the
    entrypoint knows and a build cannot.
    """
    return Image(
        egress=SessionEgress(mode="host"),
        forge=GitAccess(token_source="forge-login"),
        # lup: template: what this project's own work needs inside the image,
        # as against what a shell needs to be usable (`baseline`, the
        # library's) and what a declared capability asked for (the manifest's).
        # A domain that reads spreadsheets, renders diagrams or shells out to a
        # converter names it here. Nothing exercises these: an unresolvable
        # name fails the build.
        tooling=[
            # PDFs arrive as attachments and as fetched documents, and reading
            # one means `pdftotext`. Declared rather than requirement-shaped
            # because there is no capability here to exercise -- a session
            # either has the binary or the build failed.
            Package(name="poppler"),
        ],
    )
