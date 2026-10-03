# The Account a Session Runs As

A profile is a configuration home with its own saved login. Which one a
session uses is settled when the process starts, from the environment variable
naming that home — so selecting a profile and moving a running session onto
one are two different operations, and this skill keeps them apart rather than
letting one read as the other.

## Input

**Arguments**: {{ arguments }}

The first token is the verb and the second is a profile name:

- **`list`**, or nothing — every profile, which one this session is on, and
  whether each holds a login.
- **`use <name>`** — select the profile a later launch takes by default.
  Changes nothing about the session you are in.
- **`switch <name>`** — move *this* session onto that profile now.

Where the verb is absent, {{ ask }}. Where a verb names no profile, run `list`
first and put the roster in front of the person rather than guessing which of
their accounts they meant.

## Reading the roster

```bash
uv run lup-devtools harness profile list
```

It marks the selected profile, gives each one's configuration home, and says
which hold a login. Report the home as well as the name: two profiles differ
by nothing a person can see except where their credentials live.

Each profile is `local`, kept in this checkout, or `global`, shared by every
checkout the person opens. A name resolves to the local one first, and the
roster says where a global profile is shadowed by a local one of the same
name; report which of the two a name would open.

Which profile *this* session is on is a separate question, and the roster does
not answer it — the roster is about what a launch would select, and a running
session was launched with whatever it was launched with. Read the environment
variable naming the configuration home, and say which profile's home it is, or
that it is one no profile claims.

## Selecting for a later launch

```bash
uv run lup-devtools harness profile use <name>
```

That selects it for this checkout, overriding the global selection here. Add
`--global` only when the person asked for the selection every checkout takes
without one of its own; it can name only a global profile, and a checkout's
own selection still answers inside that checkout, as the command says.

Then say plainly that the running session is unaffected and the next one will
take it. A person who asked to "switch" and was given this has been answered
with something else, so name the difference rather than letting the success
line imply the switch happened.

## Switching the running session

The session reads where its configuration home is once, at startup, and the
variable naming it cannot be changed from inside. What can change is the
login that home holds — and where the runtime reads its login file again at
every request, a session whose home is handed another account's login runs as
that account from its next request, with nothing typed into it.

Where it can move depends on where the session runs. `LUP_CONTAINED=1` says
it runs in its repository's container, on the volume every contained session
of that repository and runtime shares:

```bash
uv run lup-devtools harness profile switch <name>
```

That hands the profile's login to the volume, so it moves this session *and
every other contained one of the same runtime in the repository*; say so
before running it. It is put to the operator before it runs, since moving
sessions onto another account is theirs to approve. A session of a runtime
that rereads its login runs on the new account from its next request; one that
keeps the login it started with stays on it until opened again, and the
command prints the one that reopens it on the profile (`--runtime` names
which runtime's sessions move).

A session on the host runs in its profile's own home, and nothing rewrites
that: another account's login written there would overwrite the profile's
own. The command answers a host session with the command that opens it again
on the profile; give the person that, after selecting the profile as above.

**Report what the command printed for each session, separately** — which run
on the new account from their next request and which need opening again —
rather than one line saying the switch happened. Where it is declined or
fails, fall back to selecting the profile and printing the exact command that
starts a new session on it: a relaunch is a real answer to "switch me to this
account"; a claimed switch that did not happen is not.

## Afterwards

Report which account the session is on now, measured rather than assumed:
for a contained session, what the volume holds, which the switch printed; for
any other, the home its variable names. The switch either moved it or it did
not, and the only honest report is the one that looked.
