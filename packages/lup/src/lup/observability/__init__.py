"""What happened, recorded so that a later reader can answer for it.

One subject rather than four top-level entries answering the same reader
question. The ordered record file every durable log appends to; the lossless
hash-chained audit stream a session writes as it runs, checked as a launch
closes and by `trace verify`; the compact markdown trace and its sidecar a
later reader skims to find a session worth opening; the console display; the
per-tool metrics, which every tool-serving process keeps a snapshot of under
its session, held to a retention window and read by `tools metrics`; the
replay divergence check; the per-turn cost arithmetic; and the account-level
metered usage. What separates them is what each is kept *for* — evidence,
navigation, or a bill — and never the mechanism, which they share.

**What the chain proves.** Each audit record carries its place and the digest
of the record before it, so an edit, removal, insertion or reordering anywhere
but the very end breaks the chain at that record, and a reader knows which
records are still evidence. It cannot see a tail cut off cleanly or a whole
chain rewritten: the digest a ledger session record pins when the launch
closes is what answers for those.
"""
