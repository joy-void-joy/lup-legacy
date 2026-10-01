# Delegate

Hand one piece of work to a peer, recorded where a later session will find it.

## What this is for

Work you have found and are not going to do now. The failure it answers is
quiet: you notice something, mention it in a reply nobody re-reads, and the
session ends. A task in the ledger outlives the session that wrote it, and
`dev ledger mine` puts it in front of whoever holds it.

**Delegating is meant to be one line.** A task needs only a title. Everything
else is worth adding when you know it and worth leaving out when you do not —
a gate that asked for more would make this expensive enough to skip.

## Phase 1: Find out who is here

```sh
uv run lup-devtools coordination roster
```

Read the addresses and what each session says it is doing. The person is
always at `user` and is on the roster like any other member.

**A name nobody answers to parks the task rather than refusing it.** That is
often the right outcome — work is frequently scoped before there is anybody to
do it — but check the roster first so parking is a choice rather than a typo.

## Phase 2: Delegate

```sh
uv run lup-devtools ledger delegate "<what the work is>" \
    --to <peer> --needs <class> --path <path it touches>
```

- `--to` is a roster address, or omitted to park it unheld.
- `--needs` is what class of input the task waits on, from a closed set:
  `judgement`, `identity`, `account`, `payment`, `command`, `review`. It is
  what lets the person's rendering be ordered without anybody writing "most
  urgent first" at the top, and what tells a reader whether a row is a
  decision to make or a command to paste. Leave it out where the task waits on
  nothing but somebody doing it.
- `--path` is repeatable and is taken as a lock on the holder's behalf, so a
  second session knows before it writes rather than after.

A dependency between two tasks is not `--needs`. It is a `blocks` edge, and
the two must not be conflated: one says what kind of input is awaited, the
other says which task comes first.

## Phase 3: Make sure it lands

Read what the command reports back. It says four things and each wants a
different response:

- **`held by <peer>`** — the task went to somebody.
- **`woken`** — that peer has been made to look. Nothing more to do.
- **an instruction naming `SendMessage`** — the peer runs a runtime whose
  sessions no command can speak to, so *you* have to carry it. Send the
  message with your own tool, to the address the instruction names. This is
  the ordinary case for a Claude peer and is not a failure.
- **a note saying it was parked** — nothing holds it, and nobody will be
  told. Say so in your reply, so the person knows it is waiting rather than
  assigned.

Mail is written whether or not anything wakes the peer, so a wake you cannot
make costs latency and never the work.

## What not to do

**Never create a tracking file.** A `TODO.md` or a backlog parks a decision
where no workflow surfaces it again; this skill is where that work goes instead.

**Do not delegate what you can finish now.** A task recorded instead of a
change is a task somebody reads, decides is stale, and deletes.
