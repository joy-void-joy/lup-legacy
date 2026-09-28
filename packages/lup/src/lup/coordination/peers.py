# lup: ignore[constant-declaration]
# The names here are the address a person is reached at, which every process
# writing to a roster and every door reading one must spell alike to meet at
# all — an identity of this vocabulary rather than a choice a caller can make.
"""The one member of every roster that is not an agent.

A population of agents that can only talk to each other has nowhere to put
what a person is owed. The answer everything here is arranged around is that
the person is *in* the roster: a member with an address, a mailbox, and a
liveness nobody has to assert, addressed by exactly the verbs that address an
agent.

That is what makes the human channel one mechanism rather than two. A report
to whoever is watching is a message to ``user``; a question is a message to
``user`` carrying a slot id, and the reply settles the slot. A console
displaying "what has been said to you" is reading one member's mailbox. None of
those needs a case in the send path, which is the whole of what the roster
buys: a person is somewhere to send to, not a branch in front of sending.

The peer answers for itself and is never finished, because there is nothing
that could finish it: a person does not stop existing when a run does, and a
roster claiming otherwise would be a roster whose only durable member expires.
Its delivery is the mode that needs nothing running beside it — mail waits in
its mailbox until somebody opens a door onto it, which is the honest description
of how a person reads.
"""

from lup.coordination.refs import ActorRef
from lup.coordination.roster import Delivery, Roster

USER_KIND = "user"
USER_ADDRESS = "user"
"""What a member writes in ``to_actor`` to reach whoever is watching.

The bare kind, because there is one of these per roster. Every other address
carries an id distinguishing it from its siblings, and a peer that has no
siblings would only be spelling the same word twice.
"""

USER_TASK = "the person this cohort answers to"
"""What a listing says the user peer is doing, so a roster reads as a roster.

Every other member's task is what it was asked for. This one's says what the
member *is*, because an operator scanning a listing for the address to send a
report to is looking for exactly that and would otherwise find a blank.
"""


def user_peer() -> ActorRef:
    """The address whoever is watching this cohort is reached at.

    A ref rather than a bare string, so the one thing that is not an agent is
    carried by the type every path already routes: the mail's addressing, the
    roster's fold, and a door's spelling of an address all read it without
    knowing which member they are looking at.
    """
    return ActorRef(kind=USER_KIND, id=USER_ADDRESS)


def join_user(roster: Roster) -> ActorRef:
    """Put the person on this roster, and hand back the address that reaches them.

    Idempotent through the roster's own announce, so every process that opens
    a view onto one cohort may call it: the run that owns the directory, a
    console attaching to it, and a peer that walked in all need the address to
    resolve, and none of them can know whether another already wrote it.

    Joined rather than spawned, because a spawn is a claim about a lifetime
    that this process owns and nothing here owns a person's. The liveness is
    left empty for the same reason it is left empty for a peer that answers
    for itself — the difference is that this one never stops answering.
    """
    peer = user_peer()
    roster.joined(peer, task=USER_TASK, delivery=Delivery.WAITING)
    return peer
