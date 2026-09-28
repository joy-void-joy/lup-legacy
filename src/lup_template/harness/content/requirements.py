"""The external programs this repository needs, and what going without costs.

Mechanism and batteries both come from the library: `lup.harness.requirements`
says what a requirement *is*, `lup.harness.toolchain` offers one constructor
per program lup has an opinion about, and what is *this project's* is the
composition below -- which constructors it takes, what it passes them, and
anything lup never heard of.

Two axes are worth reading together, because either alone misreports. *Where*
says who is expected to have it: a container runtime is the host's and must
never be the image's, a TypeScript toolchain is the image's and the host has
no reason to carry one. *Absence* says what going without costs, down to a
grade only worth saying to somebody setting a machine up. Between them, a
laptop with no bun and no clipboard is told nothing at all at launch, which
is correct -- neither is a fault of that machine.
"""

from functools import cache

from lup.harness.egress import SessionEgress
from lup.harness.requirements import Manifest
from lup.harness.toolchain import (
    agent_session_requirement,
    bun_requirement,
    checkpoint_store_requirement,
    clipboard_requirement,
    container_requirement,
    endpoint_reachable_requirement,
    git_requirement,
    github_requirement,
    host_placement_requirement,
    inside_placement_requirement,
    metadata_refused_requirement,
    proxy_reachable_requirement,
    proxy_tunnels_requirement,
    read_only_binds_requirement,
    question_relay_requirement,
    reaped_orphans_requirement,
    same_path_mount_requirement,
    sandbox_requirement,
    shell_vocabulary_requirement,
    terminal_handoff_requirement,
    typescript_requirement,
    uv_requirement,
)
from lup.policy.survey import allowed_programs
from lup.policy.vocabulary import default_vocabulary
from lup.providers.claude.confinement import CLAUDE_CONFINEMENT
from lup_template.harness.content.image import agent_image
from lup_template.harness.content.shell_vocabulary import SHELL_RULES


@cache
def carried_vocabulary(
    unpackaged: tuple[str, ...] = ("man",),
) -> tuple[str, ...]:
    """The programs this project's policy declares safe *and* expects to find.

    Derived from the table rather than listed beside it, which is the whole
    point: a word added to the vocabulary joins this probe by being added,
    and a word this environment stops carrying is reported by the next
    session instead of by whoever it lies to first.

    *unpackaged* is the subtraction, and it is a judgement rather than an
    oversight, so it is small and each member earns its place. ``man`` is the
    only one: the image's base strips ``/usr/share/man``, so installing
    ``man-db`` there would put a ``man`` on ``PATH`` that finds nothing --
    installed without working, which is the failure this whole module is
    built to refuse to report as health. A host that has one is simply not
    measured here; a session that wants a manual page has the web.

    Answered once per process: deriving it judges every word of the table
    through the shell policy, which made it most of what building the harness
    cost, and both inputs are declarations that do not move while it runs.
    """
    return tuple(
        name
        for name in allowed_programs(SHELL_RULES.over(default_vocabulary()))
        if name not in unpackaged
    )


def manifest(boundary: SessionEgress | None = None) -> Manifest:
    """This repository's requirements, host side and image side.

    ``boundary`` is the network posture the image half is asked about, and it
    defaults to the one this repository declares. A parameter because the
    entries below divide on it and a caller that cannot vary it cannot ask
    what the other posture would produce -- which is how the whole at-launch
    set went empty under a change to one field, with every test still green.

    Nothing here names a path, a container client, or an image tag, and that
    is load-bearing rather than tidy. This manifest sits inside the `Harness`
    the ownership digest hashes, so a host fact written into it moves that
    digest per machine: measured, two worktrees of one commit hashing
    differently, which made every checkout but the last one to generate read
    its own committed tree as stale. The shapes are declared here and
    `lup.harness.toolchain.for_host` aims them at what this machine answered.

    Ordered by how early a session notices an absence, not by importance, and
    deliberately short. A first draft also declared ripgrep, and exercising it
    refuted the declaration twice over: this project never invokes `rg` --
    only the policy vocabulary judges it, which is a rule about what an
    *agent* may run -- and on the machine that raised the finding `rg` was a
    shell function rather than an executable, so `command -v` would have
    called it present while nothing spawned could reach it. A manifest that
    invents prerequisites refuses machines that were fine, which is this
    module's own failure pointed the other way.

    The image half is exercised inside the container a session opens, which
    `harness requirements --inside` is for. Three of its entries take a
    filtered boundary component by component -- the proxy being reachable, a
    request reaching the world through it, the metadata endpoint still being
    refused -- and each is one a contained session finds broken while no
    preflight is in a position to see it, an image half declared and never
    run. They are asked only where that boundary is declared, since each
    names the proxy in the exercise itself.

    The others hold whatever the network is. One is the operator's terminal
    having arrived. The last is about the container rather than the boundary,
    and is here for the same reason as the rest: a session collapsing finds
    it, not anything asking. A container with no reaper at PID 1 keeps every
    orphan it ever made, so the process bound is reached by a session that
    leaked -- and what announces that is an unrelated suite failing to start
    threads. Declared beside them because the cure and the check belong to the
    same argv, and a flag nothing measures is one that comes off in a refactor
    and is missed hours later by somebody bisecting their own change.
    """
    egress = agent_image().egress if boundary is None else boundary
    return Manifest(
        requirements=[
            # Every default taken as offered. Where this repository has an
            # opinion it is in what it *adds*: the JavaScript toolchain, which
            # `default_manifest` deliberately omits because most projects on lup
            # have none.
            git_requirement(),
            uv_requirement(),
            container_requirement(),
            sandbox_requirement(),
            same_path_mount_requirement(),
            github_requirement(),
            clipboard_requirement(),
            # What the policy already promised an agent it could run. Placed
            # among the defaults because it is one, and early because a
            # session that cannot compare two files finds out by being told
            # the wrong thing rather than by being stopped.
            shell_vocabulary_requirement(vocabulary=list(carried_vocabulary())),
            # What the placement vocabulary rests on, asked rather than
            # assumed. The relay and the store are the checkout's, so the host
            # roster answers for both however the session opens; the mount
            # they sit behind is `inside placement`'s to prove.
            question_relay_requirement(),
            checkpoint_store_requirement(),
            host_placement_requirement(),
            # The image half, exercised behind the argv a session opens with.
            # Placement leads it because everything after is a fact about
            # somewhere, and this is what establishes where: a session whose
            # boundary did not stand still reaches its proxy and still places
            # every operation by a wall that is not there.
            inside_placement_requirement(),
            # What the shared git `config` and `hooks/` rest on: the mount
            # table, read inside, rather than the lease that asked for it.
            read_only_binds_requirement(),
            # Ordered as a session meets them: the proxy has to be reachable
            # before it can tunnel, the tunnel has to stand before a turn can
            # run, and the terminal is what the operator sees either way.
            #
            # The three that are about the proxy are asked only where there is
            # one. Each names it in the exercise itself -- one curls
            # `$HTTPS_PROXY`, one reads the variables pointing at it, one wants
            # the 403 its denial rules answer with -- so under a posture with
            # no proxy all three fail, and two of them refuse the launch for
            # the absence of a component this project declared it would not
            # have. The end-to-end question they carry between them outlives
            # the proxy, so it is asked either way and only the vocabulary of
            # the refusal changes: dropping it would leave a launch with no
            # image entry marked always at all, and a session opening with
            # nothing exercised behind the argv it opens with.
            *(
                [
                    proxy_reachable_requirement(),
                    proxy_tunnels_requirement(),
                    metadata_refused_requirement(),
                ]
                if egress.filtered()
                else [endpoint_reachable_requirement()]
            ),
            terminal_handoff_requirement(),
            reaped_orphans_requirement(),
            bun_requirement(),
            typescript_requirement(),
            # Opened the way a launch opens one, which for this runtime means
            # its own sandbox stood down: the container is the boundary, and
            # a probe spelling anything else answers about a session nobody
            # runs.
            agent_session_requirement(arguments=CLAUDE_CONFINEMENT.off),
        ],
    )
