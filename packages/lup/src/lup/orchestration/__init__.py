"""Running more than one piece of work, and staying able to speak to it.

A background agent that coalesces wakes into turns; a scheduler and relay for
work that sleeps; the review gates a turn passes through; and spec-driven
delegation for runtimes whose own subagents will not do. The cohort of held
sessions those meet, and the mail that lands in front of each one's next tool
call, are :mod:`lup.coordination`'s.

One subject rather than four top-level entries all plausibly answering
"run work concurrently". What separates them is not the concurrency, which
they share, but who holds the work: this process, another process, or a model
in a session of its own.
"""
