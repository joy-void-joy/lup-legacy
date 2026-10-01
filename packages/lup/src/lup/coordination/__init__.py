"""Addressable agents: one held session each, reachable while they work.

An agent a caller opens, drives for one turn and closes cannot be talked to,
because there is nothing to talk to between the call and the result. This
package is the other shape — an actor holds its session across turns, takes
mail mid-turn through a hook it never chooses to check, and asks questions
that settle without stalling whoever asked.

Nothing here knows what the actors are for. The resolver names its own kinds
and carries its own question type over the same mechanism; a research session
names different ones. A vocabulary borrowed from one consumer hides that:
the layer is written against the mechanism, and no consumer is what makes
it work.

**Coordination rather than orchestration**, and the distinction is the reason
this is its own package. Orchestration is one process deciding what several
agents do — the spawner, the background pipeline, the subagent it dispatches
and collects. Coordination is several agents that already exist finding each
other: a roster somebody joins rather than is spawned into, mail that outlives
the process that sent it, a lock one peer takes that another has to notice.
Everything here folds shared state off disk instead of remembering it, which
is what lets a peer answer to a process that did not create it — a property of
coordination, which spawning is one user of rather than the owner of.
"""
