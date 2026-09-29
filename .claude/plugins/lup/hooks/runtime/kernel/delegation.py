"""What a delegated agent verifies, and what it leaves to whoever lands its work.

A delegated agent inherits the repository's guidance and reads, correctly,
that the full gate is what has to be green. Nothing in that guidance tells it
that *it* is not the one to run it — so several agents dispatched to implement
several pieces each start the whole suite, in the one working tree they share.

Three things go wrong at once, and only the first is cost. Each run spreads
itself over every core, so four of them are four times the processes and
several times the wall clock. Each reads a tree the others are still editing,
so a green answers about a state that never existed and a red cannot be
attributed to whoever caused it. And the gate is the slowest thing in the
repository, so the redundancy is paid at its full price.

The tiers are not a weakening. A delegated agent gets the two checks a scope
narrows *exactly* — the scoped lint and type pass, which answers about the
files it is handed, and the tests it names — which is seconds rather than
minutes and is a true answer about its own change. What it does not get is
the whole suite, which answers about the tree rather than the change, and is
owed once by whoever lands the work, over a tree that has stopped moving.

The commit follows the tree rather than the tier. A builder dispatched into a
worktree of its own is the only writer there, so its commit is attributable
and is its to make; one working in a checkout it shares would commit whatever
the others left half-edited beside its change, so its caller commits. The
notice names both rather than assuming one, because a sentence that fits one
of them contradicts every brief written for the other — and a subagent told
two things by its brief and its start follows the brief and discounts the
start.
"""

# lup: solved: an adopter that renames its devtools CLI gets lup's spellings
# here, because the host half this notice is joined in ships verbatim and
# carries no injected project values. Wiring it would mean compiling the
# subagent asset the way the policy dispatcher is compiled, which is a change
# to how that asset is built rather than to what it says.


def verification_notice(scoped: str, tests: str) -> str:
    """What a delegated agent is told, at the moment it is dispatched.

    ``scoped`` and ``tests`` are how the project spells the scoped check and
    its test runner, read out of the plugin's own policy data, since the
    notice ships into projects that named their devtools CLI for themselves.

    Said at the start rather than refused at the call, because by the time a
    refusal lands the agent has already planned around running the gate and
    has to plan again. This is the cheaper half of the same judgement, and the
    one that changes what gets planned.

    Phrased as what to do rather than what not to: an agent told only that
    something is refused reaches for the nearest substitute, and the nearest
    substitute for the gate is the gate under another name.
    """
    return (
        f"Run {scoped} and {tests} over what your change reaches; leave the"
        " full gate to whoever lands it. In a worktree of your own, commit"
        " your work; in a checkout you share, leave the commit to your caller."
    )
