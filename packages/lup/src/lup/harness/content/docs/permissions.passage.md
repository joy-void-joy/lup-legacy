# Permission Policy

How the generated hooks decide allow, ask, defer, or deny, and the two
markers that change a decision. The guidance carries the rule; this page
carries the mechanism a denial sends you to.

## Sources of truth

Permissions come from the canonical semantic policies in `lup.policy` and the
application-owned `HookSet` in `harness/catalog.py`. Harness
generation compiles one hermetic dispatcher and runtime for each native
plugin. Never edit generated dispatcher or runtime files — change the
canonical source and regenerate.

## Shell classification

The policy classifies each shell command against
`lup.policy.vocabulary.default_vocabulary` as
`harness/content/shell_vocabulary.py` selects it, every URL scope,
and each edit in a batch. `lup.policy.shell_rules` owns the shape that table
takes and its erasure into the rows the kernel reads, never the words; the
project states only where it differs from what the library offers — a
downstream toolchain to add, a command it judges differently, one it drops —
so declaring `lake` costs one entry rather than a copy of every command the
library already judged. The shell
lattice reserves ask for judged risk; unjudged work denies, hinting the
escalation recipe. Under a launcher-verified OS sandbox
(`LUP_SANDBOX_ACTIVE`), unjudged work defers to that boundary, and a
`dangerouslyDisableSandbox` escape re-enters the deny lattice; the sandbox
block derives from the same `HookSet` declaration. A command the sandbox's
`excluded_commands` takes out of isolation re-enters it too, without the
escape: the boundary was told to leave that command alone, so there is
nothing for unjudged work to defer to. That sandbox is the one `--sandbox
inner` establishes on the host, and a launch naming no `--sandbox` falls back
to it, with a warning, where no Docker or Podman client is found;
[harness.md](harness.md) carries the three postures.

Segments join deny > ask > defer > allow — unjudged rides into a judged
prompt, a judged deny wins the batch. Between two deferrals the one leaving
the most to settle speaks for the line: an unread segment before an
unlisted one, and either before a handoff to the runtime, so a deferred
`curl` never carries an unread command beside it to the runtime's own
mode. Malformed input fails conservatively.

### What a rule states, and what it earns

**A rule says what an operation does. It never says what that earns.** The
lattice is not keyed on how a command is *spelled*: a rule naming an
executable and stating a verdict beside it gives two commands with one effect
different answers whenever two people write the two rules. `effects`
is the declaration instead — a list from the closed table in
`policy/kernel/effects.py`, each member deciding its own verdict from the
scope it was given, what the host measured, and where the session sits — and
`declared_verdict` derives the answer wherever it is used. Two spellings of
one effect cannot diverge, because there is one row for the effect and every
spelling reaches it.

`ShellCommandRule.effects` and `RunnerTargetRule.effects` are **required**. A
declaration stating none derives an allow, so an omission would be a grant
nobody wrote down rather than a gap a reader sees; a command that genuinely
does nothing this table guards says `changes_nothing`, which exists to be
sayable.

Two things a rule states that are not effects:

- `refuses` — where the agent goes instead, when this project declines the
  *spelling*. Set, the row denies whatever its effects would have earned, and
  the text is the whole of what the agent is told, so it names the route
  rather than the objection: `uv add` for `pip install`, writing the command
  out for `eval`. A refusal is about the route, and the route is not what an
  operation does — which is why it sits outside the effects instead of being
  spelled as one.
- `sandbox` — where an invocation has to run, whatever it earns. The axis
  below.

#### Where the harm lands, and what the container settles

Every effect also carries a `reach`: where the harm its question guards
against would land. The member states a default and a row overrides it where
its effect lands somewhere its kind does not.

| Reach | The harm lands | Inside the measured container |
|---|---|---|
| `container` | in processes, variables, packages and programs the session runs | settled, unless a path it names is one the host lent from outside this checkout |
| `mount` | on the paths the operation names | settled where every named path is the container's own |
| `dependency` | in code arriving from an index, which a later build runs | keeps its question |
| `credential` | on a secret the launch lent, or the identity a later command acts as | keeps its question |
| `lup` | on this policy's own settings and machinery | keeps its question |
| `host_later` | beyond the container or after it: a remote, another machine, a cache the host reads | keeps its question |

The settlement row `contained-judgement` reads it. Only where the launch
measured a container placing its work inside itself — the `outer` posture,
not the runtime's own sandbox — a judged ask or refusal whose every
objecting part stays inside is settled as a permission: `kill`, `make`, an
exported `PYTHONPATH`, `git -c core.pager=…`, `git rebase -x`, a system
package install, a nested `codex` session, a clone into a directory the
container owns, and a child launched with `--sandbox none` or `--mount`.
A setting is judged beside the verb it rides on, as a binding is: `git -c
core.pager=… push --force` keeps the push's question, and a clone behind it
still has its landing placed. What names a place — a redirection, a path verb's operand, a write flag, the
landing a row declares (`git clone <repo> <dir>`, `gh release download -D`) —
is placed by the host against the lease and its own mount table: in this
checkout, somewhere else the host lent, or the container's own. A path
nobody can read, and a mount table nobody can read, land on the host. A
reach nobody stated — any verdict reached by code rather than a declared
effect — keeps its question everywhere, which is why `sudo` (whose payload
is not judged), `ss -K` (the host's network), and a `git -c` setting that
hands over a credential or retargets a remote do. Every other tool that runs
a command as another identity or with other capabilities — `su`, `runuser`,
`setpriv`, `capsh`, `pkexec`, `setcap` and their kin — is the same escalation
and keeps the same question, and so do `unshare`, `nsenter` and `chroot`,
which choose the namespaces a command runs behind; `capsh --print` and
`setpriv --dump` alone report the present process and read.

A decision escalation keeps the question the agent asked for, and a hard
prohibition, a missing channel and a read-only hole are settled before the
row is read. A part of the line that deferred is answered by neither this row
nor a capture, but settled on its own: `kill 1234 && curl <unlisted>` still
hands the fetch to the runtime. The same measured container makes a loopback
fetch ask when a process the container cannot see holds the port: sharing the
host's network means sharing its loopback, and the scope declared for this
machine's own development servers would otherwise admit the operator's. It
asks even where `unscoped_fetch` defers, since the runtime sees a loopback
address and not whose service answers on it — and however the rest of the
command reads: `curl -s -o /dev/null -w '%{http_code}' <url>` asks as `curl
-s <url> | grep x` does, and so does a `curl` carrying an option nobody
classified, since where the request goes is what the container cannot hold.

And the columns that say what a *word* adds or removes, each answering one
question the row alone cannot:

| column | what it states |
|---|---|
| `ask_flags` | the spellings that escalate this row |
| `flag_effects` | what the escalation is *about* — `git reset --hard` discards working-tree content, which the bare verb never did |
| `write_flags` | options whose value is a path this command writes, so the path is resolved and judged by the write row every other spelling reaches |
| `allow_flags`, `read_verbs`, `frozen_flags`, `write_markers`, `bare_reads`, `guarded_keys` | the de-escalations: a pure read-only form, a verb that pins the query action, a flag that pins a dependency restore to what its lockfile already declares (`bun install --frozen-lockfile`, and `uv sync --frozen` or `--locked` by the same judgement), a marker whose absence means it only reads, the argument-less form, a setting that redirects neither execution nor the repository this checkout talks to |
| `read_operands`, `read_options` | the reading form a guarded subject still has, told by its count of operands and the options it may carry: `git config core.hooksPath` looks the key up and `git config core.hooksPath /x` sets it, so the first reads; an option off the list — `--unset`, `--add` — keeps the question |
| `setting_flags`, `guarded_settings` | the same absence test about a global that carries a setting — `git -c color.ui=false` turns off colour, `git -c core.pager=x` runs a program and `git -c remote.origin.url=x` aims the next push somewhere else, and only the last two are worth interrupting about; the question a guarded one raises never stands in for the subcommand behind it, so `git -c core.pager=x checkout main` is refused as `git checkout main` is |
| `ask_refspecs` | the effects an operand's *grammar* carries, for a push that spells a delete twice — `--delete main` and `:main` |
| `force_flags`, `lease_flags`, `protected_refs` | a forced update judged by what it can discard: `--force-with-lease` onto a named feature branch allows, because it replaces only what this checkout last saw; `--force`, a refspec's leading `+`, a lease onto a protected branch (`main`, `dev`), or a lease naming no branch asks |
| `value_flags` | on a subcommand, the options whose value is the next word, so `git push -o ci.skip origin` is not read as a push to `ci.skip` |
| `ask_destinations` | the forms of repository named inline that the first non-flag operand may carry — a URL or a path reaches one the remote table never heard of, where a bare remote name is one somebody approved putting there |

A rule declaring `reviewed` on a write says the route it takes has gates that
read what it wrote. It is declared rather than measured: which gates a
spelling passes through is fixed by the spelling, so it is known where the
rule is written and not at the path. That axis is what keeps the write row's
refusal aimed at *bypassing the content gates* rather than at editing a file.

What the classified verdict then becomes is a second, ordered pass, declared
as an order rather than written as a branch. `policy/kernel/settlement.py`
holds one row per rule, read the way `.gitignore` reads patterns: every row is
offered the running verdict, a row that rewrites hands its result to the next,
and the first row that settles ends the pass. So a statement about precedence
— *a stated reason never leaves a refusal standing*, *a judged deny is not
rescued by a boundary*, *a question nobody can answer is no judgment* — is one
row that says it, and changing the policy is moving, adding, or dropping one.
The rows, in order, each stating its own claim:


<!-- passage: placement -->

**Placement** is the second axis, and the boundary it names is the profile's
own — whatever delivers containment — never a provider's per-call sandbox,
which is one adapter's mechanism for spelling `inside`. Three values:
`inside` runs within the containment boundary whatever mode the session is
in, `ambient` runs wherever the session already lives, and `outside` runs on
the launcher's host through the trusted host executor. An allow placed
outside runs there unprompted; an ask placed outside says so in the question
it asks; a deny short-circuits the axis, and so does a defer, which hands the
whole decision over rather than half of it. Confinement wins a join, so one
segment that must stay inside keeps the whole line inside, and a runtime with
no channel renders the plain effect rather than an intent it would drop.

The offered table declares no placement at all. What `git`, `gh` and a
session-opening toolchain need is a boundary that grants a route to the
remote, the repository's own locks, and the runtime's configuration home —
which is a fact about the profile, declared with the boundary and *measured*
at launch, where a profile that cannot meet it says so once. Declared as a
placement it was unmeasurable: the profile that grants those and the profile
that does not both read as `outside`, and the second finds out at its first
shell call, on an error that reads like a broken repository.

**Checkpoint** is the third axis, and the one that makes the effect a
function of the session rather than of the command. The vocabulary guards
*the direction that removes something no second attempt restores*, so each
rule names the capture that would cover its loss:

| value | what the capture holds | when a rule declares it |
|---|---|---|
| `targeted` | exactly the paths the operation names | every path resolves statically — `rm build/out`, `git restore`, a redirect into a named file |
| `boundary_wide` | every precious writable root | a glob or a directory walk prevents an exact footprint, so the wider capture is what the opacity costs |
| `unrecoverable` | nothing reaches it | a remote ref, a published artifact, an issue somebody read, a command whose argument is another command |

`unrecoverable` is the default and the whole safety of the axis: a rule
nobody annotated keeps asking. `git clean -fdx` carries it deliberately
rather than by omission — it destroys ignored files, which is exactly what a
capture leaves out.

**A declared value is a claim about a path, so it is checked against the
paths.** A row carries one value for every path it might touch, which stops
being true the moment an operand leaves the checkout: the capture these name
is a snapshot of the checkout, and a loss beyond it is held by nothing. So a
redirection reads its scope off the target, and a path verb reads its
strongest operand — only the ones it *writes*, since a source `cp` merely
reads is an ordinary read however far out it sits. Without that, `rm
/etc/hosts` settles as "the affected paths are captured and restorable",
which is a sentence about a file no snapshot has ever seen. A guarded flag
that sends the write elsewhere says so in its own effects, and its question
takes the checkpoint they imply: `git apply --unsafe-paths` and
`--build-fake-ancestor` write outside the checkout, so no capture settles
them.

A written path carrying an expansion (a variable, a tilde or a substitution)
fails the same check for a different reason: it names a word, and only the
run says where that word lands. So it is read as the `unbounded` write a
compiler's configured output is, whatever spells it: a redirection, a write
flag's value, or a copy, move, `tee`, delete or link operand. It asks at
every placement. No capture settles it, and a path nothing stands at yet does
not make it a create: `sort -o a$X f` is not a new file called `a$X`. A
declared scratch root reached through the variable that names it, like
`$TMPDIR/out.txt`, is still scratch.

**Indirection is judged as the path it reaches, or as a path only the run
knows.** A variable the line settles is resolved before any rule reads a
word: an assignment standing first in its chain holds for everything after
it, and one reached through nothing but `&&` holds for the rest of that
chain, so `cd w && F=<protected path> && sed -i … $F` asks exactly as the
same `sed` naming the path does. Where the line cannot settle the value — a
`read`, a substitution, an assignment an `||` or a branch may skip, a loop
over a glob or over more words than it reads, `find`'s `{}`, a path named
from a directory a `cd` may or may not have reached — the word is left as
spelled and the command is judged with it standing there, so a write
through it asks as `sed -i 1d $F` does for a name the line never assigned,
at every placement. A word that could also become a flag still puts its
command on a floor (*an opaquely bound variable could become a guarded
flag*, *a command substitution result could become a guarded flag*), but a
floor never stands in for what the command asks as spelled: a boundary
settling it confines the call, not the checkout the call writes in, and a
protected file there was rewritten unasked. `eval`, `source` and an
interpreter's inline code stay refused, and `xargs` keeps its own question.
What `uv run` hands an interpreter is read as `uv run` reads it, so
`uv run python s.py $T` gets the same floor as `uv run bash s.sh $T`, while
`uv run python -c … $T` and `uv run python $T` stay refused.

Where the capture was actually *taken*, `RecoveredLoss` settles the question
as a **permission**. Not a deferral: deferring would make the outcome depend
on which mode the session happened to be started in, for a fact that has
nothing to do with the session's mode. This policy has positively established
that the loss it was protecting against did not happen, so it authorizes.

Taken, and not merely requested. A snapshot reference is not recovery —
coverage, restoration, metadata, completion and post-state are the guarantee
— so the row reads *measured* evidence and distinguishes three answers:
nothing required, capture proven, and capture attempted and short. The third
keeps the question and says which it was, because "nobody captured this" and
"the capture did not work" are different things to tell somebody. Coverage is
read per path: the snapshot takes what `git add -A` would, so a target Git
ignores — `.lup/`, a gitignored cache nobody declared scratch — is one no
capture holds, and a command removing or replacing one keeps its question
however many snapshots exist.

It discharges local loss and nothing travelling beside it. An operation that
also rewrites a production file, touches a protected path, reads a credential
or reaches a remote keeps its question in full, which the row reads over the
findings that composed the verdict rather than over their join — a join
reports the strongest effect and says nothing about how many reasons reached
it. And a `# lup: escalate[decision]:` marker keeps its question either way:
the agent asked to be judged, and evidence does not overrule the request.

A delete of a protected path is asked before any of this is read, because a
capture answers what the loss costs and a protected file is protected by
whose it is. `rm` and `git rm` — `--cached` included, since the next commit
deletes the file from the project — ask when any operand is the protected
path, a directory holding it (`rm -r .`), or a glob that could expand to it
(`rm *.md`), whatever the other operands are. A dry run deletes nothing.
The operands are found past git's own globals, the way the row walk finds
the subcommand, and placed from the directory a `cd` or `git -C` left: `git
--no-pager rm README.md`, `git -c color.ui=false rm README.md` and `cd docs
&& git rm ../README.md` ask as `git rm README.md` does, a restore of a
protected file asks by the same spellings, and a patch handed to `git
--no-pager apply` is read afterwards like any other. Only the readings that
*grant* — a restore from a named ref, a restore of paths with nothing
pending — read the subcommand where it is written, since a global they did
not model could change what they cover, and missing one costs a question.

`--work-tree` moves the pathspecs where git stands outside the tree: `git
--work-tree=/home/x restore .bashrc` is read as a restore of
`/home/x/.bashrc`, a `checkout <ref> -- <path>` likewise, while a flag's own
file stays where git stands, and a tree that holds where git stands — `.`,
`..` — moves nothing. `--git-dir` names a repository and moves nothing. A
grant resting on this checkout's history — a checkout or restore from a
named ref — holds only for paths this checkout answers for, so a placed path
elsewhere meets the row's question and the write scope's reading of where
it lands. A project that redirects `checkout` to the newer verbs grants no
checkout at all: `git checkout <ref> -- <path>` is refused however the path
is spelled — `.`, `./x`, `x` or absolute — and the refusal names the `git
restore --source=<ref> -- <path>` that does the same. And where git works in a tree outside the checkout, by
`--work-tree` or `-C`, a loss a capture would have settled keeps its
question, because the capture is of this checkout: `git -C ../other reset
--hard` and `git --work-tree=/srv/wt reset --hard` ask where `git reset
--hard` is settled. The tree is named to the container question too, so a
tree the host lent keeps the question there.

Placing a file on a protected path is asked the same way, whether or not
anything stood there: a path created is written as surely as one replaced,
and no capture answers whose it is. Every path a command writes — a `cp`,
`install`, `mv` or `ln` destination, a `touch`, a `dd of=`, an archive's
target — asks when a protected rule matches it; a source `mv` takes away is
read as a delete is, and a source landing under a directory destination is
read at the name it lands at, so `cp -r /tmp/.claude .` asks though no word
spells `.claude`. Reading a protected file, or copying one out, stays
ordinary. `lup.policy.kernel.words.PATH_VERBS` declares each of these verbs
once: the flags that leave its operands meaning what they read, which of
them it writes, and whether an empty destination is created from the rest.
`rsync` and `scp` are read from the same table and ask anyway, since they can
reach another machine, so no scratch grant reaches them; what the table
gives them is the reason — a local destination under a protected root asks
as that root, not as a remote sync.

**And it is written down**, which is what makes the relaxation honest rather
than merely quieter. The lattice asks about everything unjudged for an
*observability* reason, and a deferral is the one verdict that reaches nobody
— the runtime's own gate decides and the reason goes to no human. So every
deferral appends to `.lup/hooks/learned.jsonl`, one line per distinct command,
and `uv run lup-devtools dev hooks learn` reads it back as two lists:

- **gaps** — commands nobody has judged, which a boundary carried rather than
  a rule. Each is a candidate for a row in the shell vocabulary, and this list
  is the reason the corpus exists.
- **settled** — commands a rule judged and the boundary answered for. The
  audit trail: read it to check the relaxation is letting through what you
  meant.

Nothing writes a rule automatically, and the refusal is the design. From one
deferred `ruff check .`, a row of `ruff` → allow permits `ruff format --write`
forever and a row of `ruff check` → allow permits `ruff check --fix`; the same
mechanism over `rm tmp/scratch` → `rm` → allow permits `rm -rf`. What separates
the safe generalisation from the catastrophic one is exactly the judgement a
person is there to make.

Recorded when the verdict is reached rather than after the command has run. The
later event was the first proposal — learning from what a human approved — and
it cannot carry that: a runtime offers both *yes* and *yes, don't ask again*
and the event cannot tell them apart, and a human may answer by editing the
command, so it fires for something other than what was judged. None of that
touches a deferral, which is nobody's approval and is exactly known here.

`uv add`, `sync`, `lock`, `remove`, `cache` and `run` are parsed in the
kernel, against the lockfile and the runner targets. The rest of uv is the
vocabulary's `uv_rules`, walked from the command as spelled so the verb is
found past uv's global options, before it or between its words:
`uv pip`, `uv tool` and `uvx` install into an environment the lockfile does
not describe or fetch and run a package nobody declared, and `uv python`
fetches an interpreter build or pins which one runs the project, so they ask
with the dependency effect at every placement, and `uv publish` asks as the
external mutation an upload is. Their listing verbs (`uv pip list`, `show`,
`freeze`, `check`, `tree`; `uv tool list`, `dir`; `uv python list`, `find`,
`dir`) read, and a verb none names falls to the question. A global option uv does not document leaves
the verb unread, and is refused; `uv --version` and `uv -V` name no verb and
change nothing, so they read. The tool `uvx` or `uv tool run` runs is the
first operand past their own options, read by the grammar `uvx --help`
lists: an interpreter there is refused as a bare one is, however the options
before it are spelled (`uvx --from foo python -c 1`), and an option the
grammar does not list could take the next word, so it leaves the tool unread
and is refused too.

`uv run <target>` is parsed rather than matched against that table, so its
targets carry a table of their own — and they carry it in the same vocabulary:
each declares its `effects`, its `refuses`, its placement, and its reason.
Blessing a toolchain is the common case and it is one word,
`runs_declared_target`. A project that means to stop a target — one that
spends money, runs for an hour, or publishes something — refuses it there.
A target is one program however a session reaches it: `pytest` found on the
path and this checkout's `.venv/bin/pytest` run what `uv run pytest` runs,
so each is judged by that one row rather than a row per spelling — `ruff
format .` allows as `uv run ruff format .` does. A file that only shares the
name (`tmp/pytest`, another project's `.venv/bin/pytest`) is some other
program, and a command the vocabulary states a row for answers by that row,
which is where `lup-devtools` declines its bare spelling for the environment
`uv run` guarantees.
A program the shell vocabulary judges is judged as itself: `uv run` puts this
project's environment on the path and nothing more, so `uv run pip install x`
and `uv -q run pip install x` are refused as `pip install x` is, whatever uv
options surround them, and `uv run git status` reads as `git status` —
standing in the directory `--directory` names, where one does. Leaving a
program off both tables is not the same answer: it reaches no judgment,
which denies unsandboxed and defers under the boundary, where the policy has
stated nothing and the runtime's own permissions decide.

That table also answers `uv run -m <root>.<module>`, on the root segment, and
one criterion settles every `uv run` form: an invocation is refused when it
leaves no reviewable artifact behind. `-c` leaves nothing to read and an
interpreter handed nothing runs no program at all, so those keep the refusal.
A path is judged as that path, spelled plainly, after `-s`, or after
`--script`. A module is judged by whether the project declares the root it
lives under, because a module is as openable, diffable and re-runnable as the
file it lives in — so one declaration admits every entry point beneath a root,
and an undeclared root is refused with the declaration to extend named. An
`-m` a declared target owns stays that target's: `uv run pytest -m slow`
selects a marker expression, not a module.

The same criterion answers an interpreter run directly. `bash`, `sh`, `zsh`,
`node`, `bun` and `deno run` run a named script file, and `lup.policy.kernel.programs`
reads each one's own grammar to find it: an option's value is never taken for
the script, a cluster carries every letter in it, and an option the grammar
does not list leaves the script unread and refuses. Inline code (`-c`, `-e`,
`-p`, `--eval`, `--print`, `deno eval`, a `data:` import), a program read from
stdin (`-s`, `-`, a pipe, a redirect, a heredoc, `/dev/stdin`), one fetched
from a URL or package specifier, and an interpreter handed nothing stay
refused, as do `eval`, `source` and `.`; the inline refusals hold where the
vocabulary names the interpreter too, so `bun --eval` is refused beside the
`bun` subcommands its row declares. Python runs through `uv run python
<script>`, in this project's environment, so `python <script>` keeps its
refusal and names that route, and `uv run <interpreter>` reads the same
grammar. Asking an interpreter what it is runs no program at all, so its
version or usage alone is a read on every interpreter, Python's bare spelling
included: `python3 --version`, `uv run python -V`, `node -v`, `deno
--version`. Each grammar lists its own spellings, since a letter one tool
spends on help another spends on something else (`bash -h` hashes commands),
and a program beside the question is read as though it were absent:
`python3 -V tmp/x.py` and `bash --version -c ls` keep their refusals. The
`--help` that lets any other command show its usage is answered by whichever
program reads it, so one handed to the program an interpreter runs is that
program's argument: `bash -c ls --help` runs `ls`, `echo ls | bash -h` runs
its input, and each keeps its refusal however it is carried (`env`, `uv run`,
`xargs`, `find -exec`). The refusal is decided by the words up to the code, so an argument
after it that nobody can read — a variable a substitution bound, a `$(...)`
result — does not hand it to a boundary: `perl -pi -e … $files`, `python -c
… $x` and `node -e … $(ls)` are refused on every posture as their spelled
forms are. A program nobody can read is judged as the strictest one it could
be: `bash $x` runs whatever `$x` holds, and that could as well be `-c` and
code split out of the word, or `-` and its input, as a script file — so
`bash $x`, `bash $(ls)`, `x=$(ls) && node $x` and `uv run python $x` are
refused on every posture, where an unread argument after a named script
(`bash tmp/x.sh $x`) keeps the reading an unread argument gets.

A target may also carry subcommands, because a toolchain reached through
`uv run` is one target and many commands — a devtools CLI that mostly reads
a repository may have one verb beneath it that opens a paid agent session,
and without this the choice is blessing that verb or refusing the toolchain.
The shape and the walk are the command table's own, so a target with verbs
is judged exactly as the command spelled directly would be, with the target's
own effects as the default beneath them. One statement serves both halves:
while the runner row stated a verdict of its own, a target could bless itself
and refuse its own verbs with nothing noticing.

`uv run lup-devtools dev migrate pyright-environment` rewrites the protected
`pyproject.toml` configuration and requires review. Its literal `--dry-run`
form only reports the proposed change and is admitted as a read-only probe.

Every axis cascades down a table's nesting, and absence means one thing
everywhere: a subcommand or operation omitting `effects`, `refuses` or
`sandbox` inherits the level above it, and one stating any of them overrides
what it inherited in either direction — widening a restrictive parent is as
ordinary as narrowing a permissive one. So `git` says once where its
subcommands run, and each of them says only what differs; a toolchain refused
at the command keeps its one documented entry point by clearing `refuses` on
the subcommand that has one.

`$(...)` classifies recursively — the inner command joins the batch and its
opaque result rides only argument-safe commands, and anywhere else the
command is judged with the result standing where it is spelled, so a write
through it asks; command position, deep nesting, and backticks stay
conservative. File writes (redirection, `rm`)
auto-allow only into a repo `tmp/` — the one at the top or any a package
opened beside itself, in this checkout or in another worktree of the same
repository reached by its absolute path, where every write and every delete
meet the one rule — and the machine's temporary root, the session
scratchpad (`$TMPDIR`, `/tmp/claude-*`) with the rest of `/tmp` around it,
which no review pass reads and no capture holds (reassigning `TMPDIR` asks,
and a suffix climbing clear of `/tmp` leaves the grant behind); discards and
fd dups strip. A repository made in this checkout's declared scratch is as
disposable as the scratch holding it, so `git init tmp/p` — after a `cd` or
`git -C`, with a separate git dir there too — is a scratch write, its
directories resolved by the host as any write target is. Every other `git
init` stays unclassified: one naming no directory makes the repository
wherever git stands, a `--git-dir` moves it, and `--template` copies a
directory in.
Loops, conditionals, case
arms, subshells, and brace groups classify recursively over frozen bindings —
literal assignments instantiate, and opaque ones (`read`, globs, a
substitution) put each command referencing them that is not argument-safe
on a floor, beside whatever it asks as spelled. A construct the walk does not
read as a structure — a `select`, an arithmetic command, one nested past the
depth the walk opens — keeps its floor too, and every command inside it and
after it is judged all the same: what runs after it is still what runs. A
`for` loop over at most sixteen literal words is read
once per word in the binding pass every reader of the line shares, so a
redirection, a `tee` or a `cd` in its body names the path each pass reaches:
`for f in tmp/a tmp/b; do echo x > $f; done` writes two scratch files rather
than a path only the run knows. A body that assigns the loop's own name is
not read that way, since a later reference is then some other value
(`f=README.md; rm $f`), and its references gate as an opaque list's do.
Where a `cd` leaves the shell is followed the way the shell follows it:
through `&&`, `||` and `!`, and into the `if` branch its condition chose. A
`cd` that fails leaves the shell where it stood, so `cd a || rm x` removes
the `x` beside it. Past a `;` a `cd` naming its directory is taken to have
succeeded — `cd /abs/wt && make; date > tmp/log` writes the log in that tree
— which leaves one case open, recorded where the assumption is made: a `cd`
into a directory that is not there runs what follows where the shell stood.
Where the line itself may have skipped or undone a move — a `cd` after a
command that may have failed, a chain routing `||` to either side of its
`cd`, a loop whose next pass starts wherever the last one left — no
directory is named, and a relative path written there is a path only the run
knows, which asks. A pipeline's commands and a backgrounded list run in
processes of their own, so a `cd` among them moves nothing after them;
`command cd` and `builtin cd` move the shell as `cd` does, and `time cd` may
run in a child, so where it lands is not named.
`find -exec` payloads and wrappers (`env`, `time`,
`timeout`, `nice`, `stdbuf`, `setsid`, `nohup`, `exec`, `command`) recurse;
a payload is judged with each `{}` standing for a path only the run names,
beneath each starting point, so `find packages -exec rm {} +` asks as a
deletion of files nobody listed, and `-execdir` runs its payload in a
directory the run chooses, from which every path it names is read;
each wrapper's options are read by the grammar its `--help` lists, clusters
included, and one the grammar does not list leaves the command unread and
refuses. A wrapper option that acts on its own keeps the wrapper as the
command: `time -o <file>` writes a file no redirection names, so it asks
beside whatever it times, and `env -C <dir>` judges its command in that
directory, as `cd <dir> && <command>` is judged. So does an `xargs` payload, found past xargs's options by the
grammar `xargs --help` lists (an option it does not list could take the next
word, so it refuses), and because xargs appends operands read
from its input, the payload keeps its verdict only where those cannot
matter: a refusal stands, and an allow stands where the deciding row only
reads (`xargs grep`, `xargs cat`). Anything else asks at a checkpoint no
capture settles — `echo README.md | xargs rm` names its target nowhere a
rule can read. `sed`/`awk` pass read-only screens, quoted-delimiter heredocs are
literal data, and `curl` and `wget` are read the way the next section says.

`gh api` is screened by its method and body the way a download is. gh hands
a flag written before its subcommand to whichever subcommand it reaches, and
one written there without `=` takes the next word as its value, so `gh -t
status api -X DELETE` is `gh api` rather than the `gh status` it spells. A
flag before gh's subcommand, or before the operation of a subcommand that
has operations, is refused rather than modelled; the same command with its
flags after the operation is judged by its row. A short flag's value is read
where gh reads it, attached as well as apart, so `-XDELETE` and `-X=DELETE`
are the method `-X DELETE` is; an option the screen does not read could be a
method or a body, so it is refused on every posture, a measured container
included, since what it could send lands on the remote.

Quoting is kept past the parse. A `$` inside single quotes, or escaped as
`\$`, is a dollar sign rather than an expansion, so `rg '$x' src`, `git config
user.name '$me'` and `sort -o 'a$b' f` are read by the characters they spell.
`"$x"` still expands. `$'…'` and `$"…"` are quoting the shell rewrites, and a
brace expansion or a `~` after `=` makes more of a word than its text, so each
of those keeps the reading an unresolved expansion gets. The edit gates read
a path the same way: a native edit's path is never expanded by any shell, and
a command's write reaches them only once its target was read as a literal, so
`echo x > 'tmp/a$b'` and an `Edit` of `tmp/a$b` are both a scratch file.

### A download

`curl` and `wget` are read through each tool's own option grammar, in
`lup.policy.kernel.downloads`, into three answers joined strongest first.
Every URL is the fetch policy's: a refused scope denies, a declared one
allows, and an origin outside every scope answers `unscoped_fetch`. A
request body — curl's `-d`/`--data*`, `--json`, `-F`/`--form*`, `-T`, wget's
`--post-data`, `--post-file`, `--body-data`, `--body-file` — or a method
beyond `GET` and `HEAD`, attached (`-XPOST`) or apart, asks: it can change
state on the far end. Every file the response lands at is a write to that
path, judged by the tool's row in `downloader_rules` the way `sort -o` and a
redirection are: `-o`/`-O FILE`, the URL's own name that `curl -O` and a
plain `wget` take (in `wget -P`'s directory), and a log or header file. So a
download into scratch or a new file is ordinary, and one over a protected,
human-authored or tracked file asks. A redirect `-L` follows is not
re-judged; the network boundary answers for where it is sent. curl's
`-w`/`--write-out` prints a format once the transfer ends, which is how a
probe asks whether a service answered, and it reads; a format naming a
file — one it writes, `%output{…}`, or one it is read from, `@file` — leaves
the invocation unread. An option neither grammar lists — a config file, a
cookie jar, a recursive crawl, a server-chosen name — leaves the invocation
unread.

### A path no command may name

`HookSet.refused_paths` declares paths no word of any shell command may
name, each with the reason and the route to take instead, and
`lup.policy.kernel.withheld` reads every operand of every command — a value
attached after `=` or `:` included, socat's trailing `,options` read off —
and every redirection target against them. A
match denies whichever verb it sits under, because which operands a program
reads is that program's grammar: `cat`, `head`, `less`, `grep -r`, `base64`,
`xxd`, a `cp` source, a `tar` or `zip` member, a script's argument, a `cd`
into the directory, and `cat < key` are one refusal. A pattern spelled from
the root (`/proc/*/environ`) names that place; one spelled from `~/` or `**/`
names whatever path its trailing names end, since the kernel knows no home —
`~/.ssh/id_rsa`, `$HOME/.ssh/id_rsa` and `/home/u/.ssh/id_rsa` are one file.
A glob reaches what it could expand to, except a dot-named file an unspelled
dot skips, and a run of names that is all glob reaches a home's file only
where the word spells the home: `~/.*` is refused, `ls -d .*` in the checkout
is not. `exempt` passes a word only when all it could name is exempt:
`~/.ssh/*.pub` reads, `~/.ssh/*` does not. A program or a pattern a command
is handed is text it runs or matches, never a file it opens, so it names no
path whatever words it holds: a sed script, a grep or rg pattern (an `-e`
value, or the first operand where none was given) and an awk program are
left out, read by each tool's grammar — `sed 's/.*/takeToken/' f` and `grep
-rn '.*/token' src` read — while a file of patterns (`grep -f`, `awk -f`) and
every other operand are read as ever. The file tools meet the same
declaration at the path they resolve, before any destination policy is
consulted: an `Edit`, a `Write` or an `apply_patch` of a withheld path is
refused on both runtimes, since authoring a login file is naming it as surely
as `cp` is, and what a command may name — the exemptions — an edit may write.

The library's default is `credential_files()`: everything in `~/.ssh` but
the public keys, `known_hosts`, `config` and `authorized_keys`, `~/.gnupg`,
and the token files of AWS, netrc, git's credential store, gh, docker and
PyPI, plus a process's environment file and, in any directory, the
gitignored `.env.local` and `.env.<mode>.local` a project's settings keep
its API keys in — `cat .env.local` is refused where `cat .env` and
`cat .env.example` read. This project adds each runtime's
own login through `ProviderLogin.withheld_logins()`: the default home's file
and the file inside every profile's home.

A recursive read names no withheld path and reads every one beneath its
root, so it is judged by what lies there. `lup.policy.kernel.walks` reads
which words a command walks, by each utility's own grammar: `grep -r`, `rg`,
the members of an archive being written (`tar c`, `zip -r`), the sources of a
recursive `cp`, `rsync` or `scp`, and both sides of `diff -r`. The host walks
each root as the reader would — from the home for `~` and `$HOME`, skipping
dot names where `rg` does — and a root holding a withheld path refuses the
command as naming it would: `grep -r password ~`, `tar czf out.tgz ~` and
`grep -r token .` over a checkout that keeps a runtime's login are refused,
while `ls ~`, `grep x ~/notes.txt`, `rg password ~` and `grep -rn x src`
read. grep's `--exclude-dir` and `--exclude` are walks leaving that
directory or file out, so a refused search of a checkout names the two that
read the same tree — `rg token .`, which skips hidden and ignored paths, and
`grep -r --exclude-dir=.lup token .` — in the command's own words, and
both are allowed. A walk the hook's deadline cut short is refused too, since a root
nobody finished walking is not one known to hold nothing.

A listing prints names, and a name is not the secret, until something reads
each name it printed. A `find` handing `{}` to an `-exec`, `-execdir`, `-ok`
or `-okdir` payload names every file it meets to that payload, and a line
that hands names read from its input to a program — `xargs` with a payload,
a `while read` loop — reads what its listings (`find`, `ls -R`, `du`,
`tree`, `fd`, `rg --files`) yield; each is read as the walk it feeds, over
the whole line, since a pipe, a file and a loop all carry the names. So
`find ~ | xargs cat`, `find ~ -exec cat {} +` and `find ~ | while read f; do
cat "$f"; done` are refused as `grep -r x ~` is, while `find ~ -name x` and
`find src | xargs cat` read. A `find` whose expression is a plain AND of
`-name` and `-iname` tests yields only names they admit, so `find . -name
'*.py' | xargs grep x` walks past a login it never hands on.

`HookSet.secret_variables` names the variables no command may print, matched
without case: `printenv NAME`, `echo`/`printf`/`print` of an expansion, and a
here-string carrying one to any command are refused. A length or an
alternative (`${X:+set}`) prints nothing of the value and stays allowed, as
does `[ -n "$X" ]`; a value copied into another name before it is printed is
printed under that name. `set` alone joins `env` and `printenv` as a dump, and
`gh auth token` is refused by its row while `gh auth status --show-token`
asks. All of these deny rather than ask, for the reason the dump does: a
printed secret lands in a transcript that outlives the turn.

### A word nobody can read

A verb or a flag built by an expansion — `$OP`, a `$(...)` result, `set$X`,
`--ret$X` — names no row as spelled, and the command chooses at run time
which one it is. So it is judged as every command it could stand for, and
takes the strictest of their verdicts where that is stricter than the
spelling earns as written. A verb word could be any row its legible part
begins, among those the words before it still leave: `uv run lup-devtools
sync $OP lup /x --mount rw` asks as `sync setup` does, and `review
$(echo approve) <id>` is refused as the operator-only verb it could be. A
word whose legible part begins a guarded flag is read as that flag:
`--ret$X` asks as `--retire` does, and `git -$X status` as the `-c` global.
The reason names the word and the command it was read as, in one line. The
verdict carries the reach of every reading that objects, so a measured
container settles it only where each would stay inside: `codex l$OP` keeps
its question there, since it could be `codex login`. No capture retires it:
`$X rm tmp/x` asks though the `git rm` it could be is restorable.

What the legible part rules out is not read in — `sync st$X` can only be
`sync status` — and a program guarding nothing answers an unread word as it
always did. A word opening on its expansion in an argument's place keeps
the abstention above: it could as well be the path or the ref beside it.
The abstention carries the reach of every guarded flag or operand the word
could be, so a measured container settles it only where each would stay
inside: `rg $X foo` and a child launch's `--sandbox $X` run there, while
`git push $X origin feat` and `gh pr merge $X` are refused as they are
without one, since each could force, delete or merge on the remote.
A `$(...)` result standing in an argument's place carries the same.

A command word nobody can read — `$CMD`, a `$(...)` result — could be any
program, so the words after it decide: it is read as each program one of
whose verbs they name, and the abstention it earns as written is the floor.
The verb is found past that program's own globals, and the reading judges
them too. `$CMD push --force origin feat` asks as `git push --force` does, on
every posture, and so does `$CMD -c core.sshCommand=x push --force origin
feat`. `$CMD push origin feat` could be a push that asks nothing, so it
keeps the abstention: refused with no boundary, and run inside one. Words
naming no program's verb — `$EDITOR file`, `"$PYTHON" x.py` — read nothing in.

### A write that carries its own content

A redirection is answered by its path, and the reason is that a command
produces its output by running: before the fact there is nothing for the
content gates to read. What the path settles is who gets asked. Into scratch
it is the ordinary work it was. Beyond the checkout it asks whose the path is,
and a measured container settles that only where the host placed every path
the write names as the container's own — a directory the host lent, a sibling
worktree bound in, is the host's the moment the bytes land. Into this
repository's own tree it **asks**, because the same bytes arriving through an `Edit` or an
`echo` would have been read by the content gates and these never will be. So
`dev render > docs/api.md` puts one question, and its recovery names the two
ways past it: redirect into a scratch path and move the result in once it has
been read, or carry the content in the command.

`tee f` is the same write as `> f`, and it gets the same judgement from the
same code: `| tee` and `>` into one path reach one verdict in every
placement, and `tee -a` is judged as `>>` is. Each is read at the file it
reaches from the directory a `cd` left, so `cd tests && date > ../README.md`
is a write to the human-owned README, and a `cd` nothing can read leaves a
target only the run can name, which asks. A `tee` handed its operands by `find -exec` or
`xargs` is not one of these: it writes files no word of the command
names, so it keeps its own question.

Asking rather than refusing is the whole concession to the premise. Nothing
can read this write in advance, and refusing on that ground would refuse the
only writes for which that is unavoidable — so a human is asked instead, and
`git apply`'s unreviewed route keeps being the one that denies. Measured
before this: `date +%s >> <a tracked module>` appended to reviewed source,
allowed and unprompted, while `echo x > <the same path>` was read by the
content gates — one file, one write, two answers, decided by which spelling
carried its bytes.

That premise is false for `cat > f <<'EOF'` and `echo x > f`, where the bytes
are sitting in the command. Those go to the same gates an `Edit` goes to —
the anti-pattern audit, the review-note gate, the size budget, the full-write
gate — with the file about to be replaced as the preimage, and the strongest
verdict wins. So a heredoc that drops a `# lup:` note is denied exactly as
the edit would be, and one that replaces a tracked module asks; a write into
scratch stays the ordinary work it was.

Two shapes are read and no others: `cat` handed nothing but a
quoted-delimiter heredoc, and `echo` handed literal words. `printf` is
absent because its first argument is a format, and an unquoted heredoc
because the shell substitutes into the body — a reading that was wrong would
put a document in front of the gates that the command never writes, which is
worse than putting nothing there. Everything unread keeps the answer it had.

A copy over a file carries its content too, from a file rather than from the
command: `cp new.py src/app.py` leaves in `src/app.py` what `new.py` holds,
and the host reads both before the copy runs. So a `cp` whose every
destination already stands, and whose every source reads as text, is judged
as the edit it makes of each — the gates an `Edit` of that file meets,
reading the real difference between what stood there and what lands: a small
change allows, a larger one is handed to the runtime's own mode as a large
`Edit` is, a dropped `# lup:` note is denied, a protected path asks. It is the
same answer wherever the file is — this checkout, or a sibling worktree
reached by its absolute path, which no capture of this checkout holds — and
no question about losing what the copy replaces is put, since an `Edit` of
the same file is not asked one either. A copy that brings a file into being,
one carrying a flag the reading does not model (`-r`, `--backup`), and one
whose source is not text keep the verb's own row and its grants.

Codex's native prefix evaluator deliberately leaves an assignment-bearing
script opaque. Its permission-request hook still passes literal assignments
through this same classifier, so `ENV_VAR=constant git status` is approved
without a prompt. Security-sensitive assignments preserve the prompt, and a
malformed assignment is refused as an unknown command.

### What an install trusts

Every manifest and lockfile — `pyproject.toml`, `package.json`, `uv.lock`,
`poetry.lock`, `package-lock.json`, `bun.lock`, `bun.lockb`,
`pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock` — is a protected root in whichever
package holds it, declared as `**/<name>` from
`lup.policy.rules.dependency_declarations()`, and so is `.github`, because CI
runs with the repository's secrets on every push — and `.vscode`,
`.devcontainer` and `.pre-commit-config.yaml` for the same reason by other
hands: an editor's tasks, a container recipe and the hooks `git commit` runs
are code another tool runs later, with none of this policy in front of it.
(`.envrc` is already under the `.env` rule.) An edit of one, or any
shell write of one, asks whoever makes it, a self-reviewing identity
included, exactly as the root manifest does. The commands that write them
for a reason — `uv lock`, `uv add`, `bun install` — are judged by the
dependency rows instead, which read what they fetch rather than the path.

A root spelled from anywhere names a file by what it is, and a declared
scratch root holds only disposable copies of it: a project scaffolded under
`tmp/` carries its own `pyproject.toml`, and nothing installs from there. So
every `**/<name>` rule gives way to the scratch root holding the path, by
every writing route and the edit gate alike — `echo x > tmp/p/pyproject.toml`,
`cp`, `mv`, `rm` and an edit of it are scratch writes — while a rule naming the
scratch root itself holds, and the manifest in any package keeps its question.

### A write to what compiles the policy

The policy package, the scan rules and the declarations selecting them are
protected edit roots, and so is what compiles them into the hooks: an edit
there and a regeneration change what judges the session as an edit of the
policy would. `lup.policy.bundle.compilation_sources()` reads that list off
the compilation's imports — each runtime's hook renderer and the
composition an in-process session judges by, followed into every module
that itself imports from `lup.policy`, so `harness/models.py`,
`harness/enforcement.py` and each runtime's `harness.py` — and adds the
`assets/` each renderer ships verbatim as its dispatcher. Derived rather
than listed, a module the compilation comes to import is covered the day it
does, and one it reaches only for skills, prompts or launches stays ordinary
source.

### A write that widens a later launch

`sync.json` and `sync.json.local` are protected edit roots because a
registration there decides what a session may mount, at which mode, and
cloned from which repository. A command writing the same key without the
question would be the confined session choosing what confines it by another
spelling, so each writer asks what an edit of the file asks, and its reason
names what widens. `lup.policy.vocabulary.devtools_rules` declares them beside
the operator-only review verbs:

- `sync setup`, with or without `--mount`: the path it registers is what the
  registration's mount opens, and the lup entry every scaffold ships already
  carries one.
- `sync remote`: the repository a registration clones and mounts.
- `sync grant`: a host device every later launch on this machine is handed.
- `dev init upstream`: the URL of the committed lup registration.
- `dev library git --url`: the pin the lup registration follows.
- `harness claude` or `harness codex` with `--mount`, `--mount-ro` or
  `--device`: the one session a launch made from inside a session opens.

What only reads or keeps books is the ordinary work it was: `sync status`,
`fetch`, `log`, `diff` and `mark-synced`; `sync revoke`, which only narrows;
`dev library git` without `--url`, which keeps the repository the pin names;
`dev library use`, which returns the registration to the URL the registry
files declare; and every dry run, which writes nothing — `--dry-run` or `-n`
on the two writers taking one, `--generate-only` on a launcher. An operation
row states its dry-run spelling in `probe_flags`, as a subcommand row does.

## Fetch scopes

One declared origin table — the `HookSet`'s `allowed_fetch` in
`harness/catalog.py` — feeds `WebFetch` and the downloader screen. A
scope may opt into its subdomains, which also contributes the `*.host`
wildcard to the OS sandbox network allowlist, so both boundaries admit the
same set. Declare any origin an agent should be able to read as a fetch
scope; reserve the sandbox's `extra_domains` for hosts that need egress
without being readable sources.

An origin outside every scope answers `HookSet.unscoped_fetch`, by every
route that reads one: `WebFetch`, `curl` and `wget` alike. `ask` puts it to
a reviewer, `defer` hands it to the runtime's own permission system — a
Claude hook returns no decision and Codex's exits clean on both judging
events, so the runtime asks or allows by its own rules and nothing here
turns the handoff into an allow. Unset, it follows `unjudged_ambient`.
This repository declares `defer`, and keeps `unjudged_ambient` at `ask`:
reading an unlisted origin is the runtime's question, while a command
nothing classified stays visible. A denied scope is refused under either.

A cloud metadata service is refused before any scope is read, by every route
and on every placement, and escalation does not move it: what it answers is
the credentials of the machine it serves, which no container puts back.
`lup.policy.kernel.fetch.metadata_address` names the IPv4 link-local block
(every provider's IPv4 service, and ECS's task endpoint), `fd00:ec2::254`
and `metadata.google.internal`, and reads an address as the address it is:
`2852039166`, `0xa9.0xfe.0xa9.0xfe` and `[::ffff:169.254.169.254]` are one
address to curl and to this. A LAN address is an ordinary unlisted origin and
keeps answering `unscoped_fetch`; a name that only resolves to a metadata
address is the network's to stop.

Egress the proxy cannot carry at all — SSH
under a git remote, a daemon socket — is not a scope question: the sandbox's
only lever there is `excluded_commands`, which drops the command out of
isolation rather than widening anything.


<!-- passage: reaching-another-session -->
## Reaching another session

Two native calls address the population [coordination.md](coordination.md)
describes, and the policy answers both from the roster rather than from a
table. A send is denied when any string it carries names a live member of this
repository's roster, with `coordination_send` named as the surface reaching the
same peer and recording what it carried; every other target — a subagent this
session started, a teammate, a session in another repository — passes through
untouched. A listing is never refused: it answers for a wider, account-scoped
population a repository-scoped roster cannot hold, so the verdict is a deferral
and this repository's roster is attached beside the answer instead.

This is the one decision whose inputs are not declarative. The roster is live,
so the dispatcher's host half folds it — `peer_send_decision` and
`peer_listing_attachment` in `lup.policy.assets.decisions`, over the store's
own reader — and hands the kernel the spellings it found, exactly
the way an edit rule marked `resolution: required` is answered from a resolver
the dispatcher ran. The kernel still reads no filesystem and still decides from
its inputs alone. What is *declared* is only where to look: `HookSet.peer_policy`,
built by `lup.coordination.policy.peer_policy` from the store's own layout,
so renaming the coordination directory moves the compiled hook with it. A
project declaring none has both calls left entirely to the runtime's own
permissions, which is what a repository whose sessions never coordinate should
pay for them.

The shell has a third way to reach a peer: its wake socket, which the launcher
binds under the image's `WakeSockets.directory` and which takes a raw frame and
starts that session's turn with nothing on the roster.
`lup.coordination.policy.wake_socket_refusal` withholds that directory as a
`refused_paths` row built from the image's own declaration, so every spelling
of a connection the kernel reads — a socat `UNIX-CONNECT`/`UNIX-CLIENT`/
`UNIX-SENDTO`/`ABSTRACT-*` address, `nc -U`, `ncat -U`, `curl
--unix-socket`, a redirection — is refused with `coordination_send` named.

A deliberate send to a peer is not walled off. The `# lup: escalate[decision]:`
marker in any of the call's own inputs turns the refusal into the approval
question the sender asked for, carrying their stated reason — the valve every
refusal has.

<!-- passage: forge-credentials -->
## Forge credentials

A contained session reaches its forge on something the operator lent it,
selected at launch from a ladder ordered by what each rung leaves behind: a
forwarded ssh agent, then an ephemeral copy of the host's usable ssh keys,
then a token over HTTPS, then nothing and public reads. `GitAccess.source`
pins one rung; `auto` walks them and takes the first that is *verified*
usable — an agent holding an identity, a key that opens with no passphrase,
a `known_hosts` entry that lets a non-interactive ssh verify the forge. A
pinned rung that turns out unusable degrades to public reads with the reason
said, rather than refusing a launch over a preference.

Both ssh rungs are gated on the egress carrying ssh at all. ssh reads none of
the proxy variables, so under `filtered` a forwarded socket would be a
credential the session holds and cannot use — which reads as ready and is
not. The selected rung also decides which way remotes are rewritten: toward
`https://host/` for a token, toward `git@host:` for a key or an agent, so one
session speaks one transport rather than half its remotes working.

**What the credential-path denials do and do not buy.** The key and login
files are declared once, in `HookSet.refused_paths`, and every reader is
refused them from that declaration: the shell's `refused_paths` screen above,
which both runtimes' dispatchers run, and on Claude the `Read` deny rules the
in-process file tools obey — Read, Grep and Glob are not routed through the
policy hook — which Claude Code merges into its sandbox's read restrictions
for sandboxed commands too. A pattern from a home is spelled `~/`, one from
the root `//`, and one from anywhere `//**/`. Two differences are the
runtimes' own. A `Read` deny carves no exemption out of a home or a root, so
the file tools are refused the public keys and `known_hosts` the shell may
read. And Claude's sandbox on Linux expands a pattern against what exists
when a command is wrapped, skipping one whose first name is a wildcard, so
`//**/…` holds for the file tools and not for a sandboxed command there.
Codex has no file-reading tool — every read it makes is a command — and no
read restriction in its sandbox, so the screen is the whole of it. That
stops an agent *reading* key material and is worth keeping. It is not
isolation from `ssh` and `git` *using* it: `ssh git@github.com` contains no
credential path, and ssh reads the key or the agent socket itself, and
neither layer is a syscall boundary. An operator granting an ssh rung is
granting the contained session the use of that identity, and this is the
honest description of that grant rather than a claim of a stronger boundary.

Nothing lent is written where it could outlive the session: the ephemeral
home is made under the system temporary directory at mode `0700`, holds
copies at `0600`, is mounted read-only, and is removed when the launcher
exits. The host's own `~/.ssh/config` is never copied — the configuration is
compiled from what was actually lent, so it cannot name an `IdentityFile`,
an `Include` or a `Match exec` that does not exist inside. Host-key
verification is left at ssh's default: `known_hosts` is carried in, and a
forge it cannot verify is a reason to decline the rung rather than to accept
an unknown host.

Nothing lent reaches the argv that starts the container either. The token
crosses as a bare `-e NAME`, which both engines read as "take this one from
my own environment", and the forge client's own variable is derived inside
the image from it — a value in argv is a value in `ps` for every process on
the host, for as long as the session runs.

Signing is a separate claim from authorship and stays off by default: an
agent commit is not a human vouching for it, and signing it with the
operator's key would make the signature assert something untrue. What that
costs is a branch protection rule requiring signed commits, which fails on
agent branches as a visible check at push time rather than as `gpg: signing
failed` mid-commit.

## Edit decisions

Edits in an explicitly mounted destination repository use that checkout's
generated policy. The caller keeps its session identity, measured execution
boundary, approval channel, and whole-shell restrictions. A writable parent
directory alone grants no policy authority over unrelated nested repositories.
Read-only mounts, including nested read-only paths, still withhold writes;
canonical Git common-directory identities distinguish unrelated repositories
and bind linked worktrees, separate Git directories, and submodules correctly.
Autonomous edit grants require the same agent identity to be authorized by
both policies. Claude's native agent type is carried to the owner; other calls
use the declared session identity.

At launch, `.lup/preflight/<nonce>.json` records exact destination grants and
the digest of the generated evaluator and runtime source accepted for each.
`LUP_BOUNDARY_ROOT` pins that ledger to the launch checkout when a command
changes its working directory; changing directories never changes its grants.
The evaluator runs from `.lup/policy-snapshots/<digest>`, with both source and
snapshot checked before execution. Missing, malformed, incompatible, changed,
or failing evaluators block the edit with a recovery diagnostic. Both native
runtimes share this routing and aggregate every file, including both sides of
a move; a denial wins over an approval request.

Destination evaluation never starts an external resolver from the checkout.
Rules requiring unavailable resolution retain their conservative review verdict.

After regenerating a destination policy, an independent operator can accept
its bytes without restarting the session. From the caller checkout, run
`uv run lup-devtools harness policy-refresh --nonce <nonce> --repository <checkout>`.
The same command can accept a newly created worktree only beneath an original
explicit writable bare-repository mount, with the same Git common directory
and a writable measured boundary. A registration the per-user cache
materialized is mounted as one: the launch mounts its whole bare clone, so a
worktree cut in it afterwards is accepted this way. A registration naming a
checkout this machine keeps mounts that working tree alone, and a worktree
beside it needs a launch granting it. The refresh never discovers unrelated
nested repositories or extends the launch's filesystem grants. The requester
cannot run this operator action, and the authority ledger and accepted
snapshots are protected edit paths. These records prevent accidental
inheritance and stale policy execution; they are mutable local bookkeeping,
not authentication against a hostile process with the same filesystem
authority.

A container holds its checkout's launch record read-only — `.lup/preflight`,
`.lup/policy-snapshots` and the mount table `.lup/boundary.json` — and pins
every directory between a hold and the writable mount enclosing it, `.lup`
and `.git` among them, so the record can be neither rewritten nor moved out
from under its hold. A ledger's claim to a container is believed only where
the dispatcher reads it through that read-only mount, which it measures from
its own mount table: the same ledger written by a script in a host session,
where the directory is writable, loses the claim and is answered as
uncontained. Only the session's own checkout is held; a sibling's record
stays writable, since a mount point inside a sibling would stop `git worktree
remove` of it from inside.

What the launcher keeps for itself is mounted by no launch: a mount at,
above, or inside `$XDG_STATE_HOME/lup` (the store of trusted repositories),
`$XDG_CONFIG_HOME/lup` (each profile's account and credentials) or
`~/.cache/lup/codex-revisions` (the hooks a contained Codex session runs)
refuses the launch, naming the mount and the variable that moves the
directory out of it.

Every mounted repository's shared `config` and `hooks/` are held read-only,
because git runs on the host what they name. For a linked worktree the
container binds the shared git directory itself read-only and every directory
under it but `hooks/` and `modules/` writable back over it. A file bind would
not hold: git rewrites `config` by renaming a lockfile over it, and the kernel
detaches a file mount renamed over from the host, so one host-side `push -u`
left every running container's `config` writable. A launch readies the
directory first, making the directories git creates on first use and moving
`packed-refs` into a writable `lup-refs/` behind a symlink, since every ref
deletion rewrites it. A plain checkout keeps its per-worktree state at the
top of that directory, so it keeps the writable share with `config` and
`hooks/` bound read-only inside it. The ledger records the read-only
directory, or the two files, as read-only roots, which is what holds them on
a host posture. The file tools
meet them through the edit gates. Every spelling of a shell write meets them
through the `read-only-write` settlement — a redirection, `tee`, `sed -i`,
`cp`, `mv`, `ln` — which refuses rather than asks, as the bind does, and reads
only the operands a verb writes, so copying a hook *out* stays a read. A git
config write naming a program keeps its own question. The runtime's own
sandbox holds them for what no rule reads: an inner Claude launch passes them
as `sandbox.filesystem.denyWrite`, which holds inside a wider `allowWrite`.
An inner Codex launch cannot say that — `sandbox_workspace_write.writable_roots`
takes no read-only region inside a root, and Codex protects only a root's
`.git`, which a bare repository does not have — so it admits a mounted bare
clone's worktrees rather than its git directory, and a worktree cut after
the launch is Codex's to write from the next one.

A repository kept inside the checkout is held the same way once it is
declared: `OuterContainer(nested_repositories=[NestedRepository(path=...)])`
binds its `config` and `hooks/` read-only as a plain checkout's are, and pins
its `.git` and the directories above it, so neither can be moved out from
under the hold. Each launch verifies its pointers on the host and refuses a
planted `commondir`, and refuses one whose `.git` is a pointer rather than a
directory of its own. `create=True` initializes an absent one on the host, so
no session writes its configuration first; an absent one not marked so is
said and held by nothing. Declared rather than found, since a scan would read
a tree the session writes.

Git's own pointers are held the same way without a bind. A linked worktree's
`.git` file names its entry under the shared directory, and the entry's
`commondir` and `gitdir` name the shared directory and the way back; host git
follows them to the config it reads, so rewriting one hands the operator's
next git command a config the session built. They cannot be mounted
read-only, because `git worktree remove` unlinks exactly these files, so they
are recognized by what they are rather than measured: the file tools on both
runtimes are refused them, and so is every shell spelling of a write through
the `read-only-write` settlement — a redirection, `tee`, a write flag, `cp`,
`mv`, `ln`, `sed -i` — along with `config.worktree` and renaming or
recreating an entry of `worktrees/` itself, whether the path is spelled or
reached through a link. The refs — `HEAD`, every `*_HEAD`, `packed-refs`,
`refs/` — are refused to the file tools and to a command carrying the bytes
it writes, which meets the edit gates, and keep the question a write to the
repository earns otherwise. `git worktree add`, `move`, `remove` and `prune`,
which write all of them, keep their own verdicts. `git worktree lock` is
undone by the unlock beside it, and allows; `git worktree unlock` asks on
every placement, since it releases a hold another session may own and
nothing here puts that hold back for its owner. `git archive` reads the tree
it archives, and the file `-o` lands is judged where it lands; `--remote`
and `--exec`, which fetch from another repository or run the program that
serves it, ask.

A file another repository holds, with no destination grant, meets a referral
in place of the gates below: the edit **asks**, and the reason says that
repository's conventions are its own, so the way through is never to restyle
its code into this one's. The exception is this checkout's own scratch. A
repository nested under a root declared scratch here — a probe kit given its
own `git init` under `tmp/` — is judged as the scratch around it, so an edit,
a redirect and a command's output landing there are allowed like any other
scratch file, on both runtimes and after the fact alike. The claim is read off
the path as this checkout spells it, never as the nested repository does, so
a kit under a sibling worktree's `tmp/`, a `refs/` link landing in another
project, and another repository's own `tmp/` all keep the referral.

The refusal to write a generated plugin tree by hand stops at the same line,
and for a reason of its own: nothing this project generates lands in its
scratch, which a test walking both recipes pins. So a kit's own hand-written
`.claude/plugins/` or `.codex/plugins/` there is written like any other
scratch file — by an edit, a redirect or a path verb. This checkout's compiled
trees stay refused, and so does every tree its scratch does not hold: a
sibling worktree's, another repository's, one under the machine's temporary
root, and this checkout's own reached through a link planted in scratch, which
the host resolves and the shell refuses once it has seen the link move the
write.

Edit decisions cover protected paths, marker changes, size, the canonical
anti-pattern audit, and declared import ownership. A human-owned file
surfaces every change to it, edit or shell write, as an approval its author
answers. An edit over the size gate alone is deferred — the hook
emits no decision, so auto-accept applies while hard gates stay explicit.

A deferral is never a question lup puts, and nothing parks one, whichever
spelling carried the change — an `Edit`, a `cp` over a file, a heredoc, a
`sed -i`, or a line whose strongest part is the handoff. On Claude Code the
hook returns no permission decision, and the runtime's own mode answers: auto
mode's classifier, or the prompt in the terminal. On Codex the pre-tool hook
lets the call through to Codex's own approval policy, and its
permission-request hook returns nothing, which leaves Codex's own prompt. A
`# lup: escalate[decision]:` line over a deferred command changes nothing
either: the handoff stands, and the agent is told the reason was not needed.

Size is counted in *real* changed lines per change block, and an edit of
three or fewer auto-allows. Imports, comments, whitespace, blank lines,
docstrings, string literals, type annotations, and TypedDict/BaseModel bodies
are not real lines. Pure deletions and single-line `replace_all` renames
auto-allow outright; a multi-line `replace_all` falls through to the size
gate, and a full-file write asks for everything but a package marker — an
`__init__.py` arriving empty or holding nothing but its docstring, where the
question the gate exists to raise has no content to answer it. One carrying
anything else is the module it became, and asks. The anti-pattern audit runs
before any auto-allow, so keeping an edit small cannot outrun it.

`HookSet.import_boundaries` carries the same `seam-boundary` ownership that
the repository audit reads. Concrete adapter imports belong in providers or
declared composition roots; provider SDK imports belong in implementations
and explicitly named fixtures, not application composition roots. The shared
AST scanner understands direct, parent-package, wildcard, and relative
imports, including multiline statements. Provider names in prose and canonical
tool grants such as `Read` and `WebSearch` remain valid: vocabulary is not a
dependency. Computed import names and arbitrary executed code are outside this
static guard, not claims of runtime isolation.

An unsuppressed dependency breach denies before size allowances or a batch's
approval request can admit it. A typed suppression uses the existing reviewed
suppression allowance; removing one exposes the import again. Retiring
`seam-boundary` through `HookSet.rules` retires the hook and audit together.
Both native dispatchers carry the same scanner and ownership rows.

Every verdict above is what the kernel reaches when a project says nothing,
and every one of them is nameable. `HookSet.edit_rules` is a `Selection` of
`EditRule`, each naming the axes an edit has — the gate it speaks about, the
file suffixes, the path roles, and whether the change creates, overwrites,
modifies, or deletes — plus the effect it hands that class and the size
threshold it counts by. The two move independently: a rule may widen how much
counts as small for one suffix without restating who decides when it trips.

Overlapping rules resolve **last-match-wins**, the way `.gitignore` reads, so
a project writes the broad statement first and carves its exceptions after
it. A repository whose conventions are Python conventions says so in two
entries — the content gates allow, then `.py`/`.pyi` ask — and leaves prose,
data, and other toolchains to be reviewed in the diff rather than at the
hook. Most-specific-wins was rejected: it makes a table's meaning depend on a
specificity ordering nobody wrote down, and has no answer at all for two
rules of equal reach.

The gate ids include the two that deny removing review feedback. A gate a
project cannot reach is one whose rightness this library asserted on that
project's behalf, and an escape hatch stated in a declaration somebody
reviews is better than the fork it would otherwise take. Moving one gate
moves nothing adjacent: softening `feedback-removed` leaves `claim-removed`
denying, because the two are about different things.

A project that declares an **acceptance guard** adds one gate ahead of all
of those, over every root it gave the `test` role. An ordinary session is
asked before it edits a test, because a test that encodes the wrong
behaviour has to be fixable by someone who can weigh that; a session
declared autonomous is refused, because for it these tests are the
specification it is implementing against, and rewriting a specification to
match an implementation is the failure the guard exists to catch. It answers
before the gates below rather than through them — including pure deletion,
which would otherwise wave through removing the test outright, and the
protected-path rules, whose autonomous release must not survive a refusal
aimed at exactly that caller. This is the one place autonomy costs a caller
more rather than less. Declaring no guard leaves tests judged by the
ordinary lattice, which is right for a project that does not implement
against fixed acceptance tests.

The
resolver's worker receives only its declared autonomous edit exceptions,
which are the size and whole-file gates: it writes a large edit or a new
file without a question because it reviews its own. Protected roots,
temporary paths, human-owned files like `README.md`, marker changes, and
anti-pattern violations retain their guardrails in every mode. A protected
root is where a session's own boundary is declared — its settings, its
launch registry, its measured preflight, its review queue, the policy, and
the selection of which scan and shell rules apply — so an edit there asks a
person whoever is making it, including an identity trusted to review its own
work. A command writing one asks the same: `dev seams --retire`,
`--retire-all` and `--disown` rewrite the rule selection and the human-owned
files, while `--keep` and `--own` only narrow and `dev seams` alone reads.

A few of those guardrails open only for a gate a human granted — creating a
devtools module, adding an anti-pattern suppression. What a lease holds is
written in one document per lease, and every judge reads it at the moment it
judges: the canonical policy in the composing process and the deployed
dispatcher in the session's own. The session environment names that document
and never carries its contents, so a gate granted while the session runs
reaches it and one taken back stops applying, with no restart either way. A
narrowed document parks the run rather than silently reducing what a worker
may do. Nothing else grants: a name outside the declared vocabulary is
dropped, and an unreadable document is no grant at all.

Autonomy follows the identity a launcher declares for the session it starts,
carried in the environment and matched against the resolver's own
`worker_identity`, so it reaches a top-level worker session on either runtime
rather than only a natively dispatched subagent. A session that is not
autonomous declares the empty identity rather than staying silent: runtimes
merge a session's environment over the launching process's, so silence would
inherit whatever the operator had exported. A hook script is spawned by the
runtime with the runtime's environment, so an agent exporting the variable
inside a shell tool call never reaches the dispatcher that judges it.

## Where a native ask is put

A policy ask is parked for the operator where somebody reads what parks, and
is otherwise put to the runtime's own prompt. Parking writes the call to the
review queue — `.lup/questions.jsonl` at the top of the checkout the
session's launch opened (`LUP_BOUNDARY_ROOT`), or of the checkout the call
runs in for an unlaunched session, never a subdirectory's — and refuses it
while it waits; the answer releases it once. The call keeps the directory it
runs in and the checkout it changes, and every review command the refusal
names runs in the queue's checkout, with its code (`host.review_home`). Codex parks every ask, having no ask
effect at its pre-tool boundary, at both pre-tool and permission-request
events, and is answered on the dashboard or from the terminal. Claude parks
every ask where the session's launch holds a dashboard (the launch hands the
session `LUP_DASHBOARD_URL`, which the hook reads), for the session, its
subagents and its `-p` runs alike. Where no dashboard is held, every Claude
ask — a `human_only` one included — is its native permission request, where
the person already is.

Measured on Claude Code 2.1.283, in an interactive session driven in auto
mode: a hook's `ask` raised the permission prompt, and the call had not run
a minute later with nobody answering; a hook's `deny` held in a `-p` run
under both the default and the auto mode. What a mode does with a prompt is
the vendor's and has moved between releases: on 2.1.263 an auto-mode
classifier let a hook's ask for a ref deletion run with no prompt shown.
No field in the hook payload separates a prompt somebody saw from one a mode
settled; a parked question is answered only by a recorded answer. Observed
execution is evidence of neither: it records that a call ran and confers no
authority over the next.

The refusal is written for the agent, because a refusal normally means
"change course" and an agent reading this one that way reshapes the call and
spends the review. It reads "Queued for the operator as review `<id>` — not
refused. Don't change the command.", then how the conversation that asked
hears the answer, in its runtime's words. A session's own conversation is
told: "Carry on with other work, or end your turn: the operator's answer
wakes this session, and `uv run --directory <session checkout> lup-devtools
review wait <id>` then carries the call out at once. Don't start a
waiter." — the answer goes to its mailbox and its wake, the socket on
Claude and `codex queue` on Codex, and a waiter it held instead ended at the
tool's limit every two hours and woke it for nothing. A subagent, carrying
`agent_id` in its payload, is woken by nothing but its own work: on Claude it
is told to hold `review wait <id> --timeout 7140` in the background
(run_in_background, with the longest timeout the tool takes, 7200000 ms),
whose own timeout ends it a minute before the tool would, saying the review
still waits; on Codex, whose subagent's last message is its report, to hold
it in its shell tool and read it before it reports. Either starts it again
quietly, reporting that to nobody. A `-p` run, whose background commands
end with it and which nothing wakes after, is told to run it in the
foreground once nothing else is left, with `--timeout 540` under the tool's
ten-minute foreground limit. Beside it, the operator is shown
where the review waits — on
the dashboard, or the terminal commands that answer it — as `systemMessage`
on both runtimes. Codex delivers the refusal as a structured `deny`
carrying that line, so its app-server raises an operator-visible warning in
`hook/completed`; `codex exec --json` omits those hook events, and its agent
still receives the same refusal. Neither surface turns a policy question
into an implicit approval.

{{ dashboard }}The terminal answers every review: run `uv run --directory <checkout>
lup-devtools review show <id>` over the indicated checkout, then `review
approve <id> --as operator` or `review decline <id> --as operator` the same
way, outside the agent session. The session that asked hears of it as it
hears of an answer on the dashboard, by its waiter or its mailbox and wake,
and the command says which. Review answers are declared
`operator_only` in the shell vocabulary, and the verbs refuse a caller inside
a launched session themselves; an escalation cannot grant the requester
authority to answer itself.

An answer is written where no session writes: into lup's own state on the
host, `$XDG_STATE_HOME/lup/reviews`, one directory per repository. Every
launch lends its session that repository's directory read-only, mounted in a
container at the path the host has it and named by `LUP_REVIEW_ANSWERS`, so a
hook and `review wait` read the answer and nothing in the session can write
one; a launch refuses a writable mount of it, and any mount of the rest of
lup's state. The question stays in the checkout, which the session writes:
`.lup/questions.jsonl`, the store beside it under `.lup/reviews`, and the
claims under `.lup/review-claims` and `.lup/review-stage-claims` that spend an
answer once, are protected roots. The hooks and `review wait` write them from
their own processes, so a session's own write — a row appended, a copy over
the file, a claim retired — asks. The library writes them, so the library
protects them: `lup.policy.rules.invariant_path_rules` holds them — with the
launch ledger under `.lup/preflight`, the policy snapshots, `.env` and a new
devtools module — on both enforcement paths whatever a project declares, and
no adopter has to know to list them. A record there claiming an answer is
ignored, and a parked record whose fields no longer hash to its fingerprint
— one rewritten to show another call — can be neither answered nor spent.
Nested command paths are declared with `ShellOperationRule.parents`, and the
deepest matching path decides.

The relay keeps each question once. Its log opens with a line naming its
shape, `{"relay": 2}`, and holds a record per question as it was parked, then
a transition per state it moves to — dispatched, completed, stale, expired —
naming the question, the fields that moved and when, never a copy of the
question. Every document a question binds — the preimage of each file it
records, the document each file verdict judged, and each string of its call a
kibibyte or longer, such as a written file's content, a proposal's files or a
long command — is kept once in `.lup/reviews/blobs/<sha256>`, written beside
its name and moved into place, and the record names it by that digest; for a
call's string the record also lists where in the call it stood. A native
retry's payload that repeats the call's is recorded as null, which every
reader takes for the call's own payload. The fingerprint still binds each
document whole: a record is read back through the documents its digests name,
a document that no longer hashes to its name reads as missing, and a record
that cannot be read back whole can be neither answered nor spent. Whoever
reads the relay first under its lock — a hook, `review list`, `review wait`,
the dashboard — rewrites a log still kept the older way, a full copy per
transition, into this shape, and nothing reads the older one after; a writer
that waited on a log rewritten meanwhile writes to the new one. A reader
that stays open, as the dashboard and a waiter do, reads only what was
appended since it last read. Settled reviews past the retention window
(`[tool.lup] review-retention-days` in the checkout's `pyproject.toml`, seven
days unless set) leave the log for `.lup/reviews/archive.jsonl`, and the
documents no remaining question names leave the store with them.

An approval is spent once. `review wait`, started in the session's own shell,
carries the call out there: an approved edit writes the after-document the
operator saw, only where every file still stands as the review recorded it,
and reports a conflict otherwise; an approved command runs in the directory
recorded with it, in a fresh shell, so nothing the session did to its own
shell since reaches it. It carries out only a review this session asked —
by its runtime's session id or its launch's roster member — and it takes the
same exclusive claim a retried call would. A call a shell cannot carry out,
one placed outside the session's sandbox or a tool that is not a write or a
command, is left to one exact retry in the same session and directory: the
hook re-runs policy, compares the payload, captured file preimages, resolved
paths, originating dispatcher and policy bytes, and accepted destination
policy bindings, then claims the approval exclusively before allowing
execution. A changed file, payload or policy requires another review. The
receipt binds both the original request and the exact approved runtime input
rewrite; observing a different executed input marks the receipt `in_doubt`.
Declining leaves the operation stopped and delivers the operator's note.
A crash after claiming approval does not make it reusable. Native sandbox
restrictions still apply; queue approval does not change execution placement.
Post-tool evidence marks a dispatched review completed, without claiming that
the operation's effects succeeded. Execution against an unresolved review is
recorded as `in_doubt` and diagnosed. A missing matching event leaves dispatch
unresolved; it never makes the answer reusable.

A Codex pre-tool receipt can advance once to the permission event when both
events carry the same nonempty native invocation ID and exact operation.
The handoff takes a separate exclusive claim, so repeated permission events
cannot reuse it. The native permission-event contract does not promise that
ID; when absent, the pre-tool receipt cannot establish the handoff. The call
stays blocked pending fresh review or execution from an operator terminal.

Native patches are decoded into file transitions before review. A standalone
shell `apply_patch` with a single-quoted argument or a quoted heredoc reaches
the same edit gates. Relative paths resolve against the hook payload's
working directory. Add-file operations replacing existing files are judged
as overwrites. Compound shell commands are never reduced to only their patch.

A shell command is read the way the shell runs it: segment by segment, each
write applied to what the writes before it left, through a `cd` and across
`&&`, `;` and pipes (`lup.policy.kernel.documents`). An in-place `sed`, with
every expression and every file it names, runs sandboxed over the text the
line has left there -- any encoding sed reads, the file never touched -- under
the `C.UTF-8` locale, since no runtime hands a hook its shell's environment
and a locale decides what `.`, a class and a range match; `cp`,
`mv` and `install` land their source's text, a move's source gone; `rm`
removes; a heredoc, `echo`, `printf` or `tee` lands the bytes the command
carries, `>>` after what stands; `patch -pN` and `git apply` are applied by
Git to a copy of the files they touch. The edit gates judge each file as the
edit the whole line makes of it, so a second rewrite of a file is read
against what the first left, and several appends meet the size gate as the one
change they add up to. A rewrite whose document nobody could produce -- over a
file an earlier step writes by running, say -- asks, except in scratch, where
no gate reads the content and only a rule protecting the path is put.

Where somebody is asked, the question keeps that reading as its per-file
record: each file the line changes, in the order it writes them, with the
verdict the edit gates gave it and the document it would hold, beside the
preimage its digest names. The dashboard and `review show` read the record;
nothing is re-derived or run where a review is read, so the diff is what was
judged. A file the policy allows on its own -- scratch, a test, a data file
-- carries `allow`, which leaves it out of the default view and in the full
one. A step whose result exists only once it runs -- a program's output
redirected into a file, `sort -o`, a formatter, a script, a loop, a word
the shell expands into other words -- is listed as that, with the files it
leaves so, and never run to find out; a file that does not read as text is
named rather than shown. A reader writes nothing only running would show: a
command whose row only reads, printing to a stream -- `pyright` reporting
what it found, `2>&1` beside it -- is no such step, unless it spells a flag
its row guards, as `pyright --createstub` does. Every file a step lands the text of, and every
file a row shows, contributes its preimage, so a change to any of them
before the call is carried out makes it a fresh question.

The question keeps each command of the line too, with the verdict it reached
on its own -- effect, reason and rule -- in the order they run. The line's
verdict is joined from them and its reason speaks for all of them; these
rows say which command asked what, so a line that asks twice is put to the
reviewer as two questions, and the commands allowed on their own are listed
apart. They are bound into the fingerprint with the rest of what a reviewer
reads, and cross no owner's wire: they are the caller's reading of the line.

Beside them the question keeps who asked and what it said the call is for,
bound into nothing: they name the asker and its claim, never what an approval
releases. `agent` is the runtime's own id for the subagent that asked, blank
for the session's own conversation, which is how the operator's words reach
the conversation handling the call and a copy the session it runs in.
`account` is the asker's own words, each with where they were found: the
note the tool call carries -- Claude Code's `Bash` `description`, Codex's
shell `justification` for running outside its sandbox; Codex's tools carry
no note saying what a command does -- and what the agent wrote since it last
heard anything, read back from the end of the transcript of the conversation
making the call: a Claude subagent's own `subagents/agent-<agent_id>.jsonl`
beside its session's, a Codex subagent's own rollout. Claude Code writes a
call's own record before its `PreToolUse` hook runs, measured on 2.1.283;
Codex's rollout is read so that it makes no difference whether the call's own
line is written yet. Where the agent said nothing, what its roster row says
it is on stands in, as `doing`. Recorded once, when the call first parks, and
kept whole.

A conversation with two or more edits waiting on the operator is told, in the
refusal, how to put the next ones as one: each file as it should end up,
under one directory in the `tmp/` of the checkout they change, mirroring
that checkout, and `review propose` over its absolute path, run in the
session's queue's checkout, which parks every file as one review. It is told too that `--why`
and each file's note are written in plain words, and that
`review propose --help` shows how.


## Two markers change a decision

A denial that admits one spells it; this is what each one does. Escalation
is one-off: a wall met again means widening the protected declaration, and a
command is tried inside the boundary before it asks for the host.

- The escalation marker, as the leading comment line of a shell command,
  names which axis it asks to move — `lup: escalate[decision]: <why>` for a
  reviewer over a verdict a rule reached alone, `lup: escalate[sandbox]:
  <why>` for the launcher's host, and `lup: escalate[decision,sandbox]:
  <why>` for both. Two different requests were sharing one spelling, and they
  promote a verdict differently: decision escalation turns an overrideable
  refusal or an abstention into a question at the placement it already had,
  while sandbox escalation moves the placement and is *always* reviewed —
  what the person is being asked is not "may this run" but "may this run
  *there*", which has an answer of its own. Composed, the combined form is
  the only route from an overrideable refusal to the host, because the
  decision half has already made it a question by the time the placement
  moves.

  The question says which *there* it buys, from the placement the launch
  measured rather than from the marker: on a host, that the call leaves for
  the host, outside the only boundary there is; inside a container, that it
  runs with the per-call sandbox off and still inside the container's
  mounts, where a path mounted read-only stays read-only. A contained launch
  never arms the per-call sandbox, so the escape lifts no mount, and a
  write the mount table refuses fails approved exactly as it fails unmarked
  — the exact command is then the user's to run from a host terminal.

  A marker naming no kind, `lup: escalate: <why>`, is refused with the
  spellings that name one: the kind is the request, and the refusal is
  where an agent stuck inside the boundary learns the sandbox half exists.

  A reason is mandatory in every spelling: the whole content of the request
  is what it says to whoever answers, and a request that says nothing asks
  them to approve a rule id. Three refusals it does not reach, each being a
  statement that the operation cannot happen rather than that nobody
  approved it: a marker stating no reason, a policy invariant, and an
  operation whose placement no channel can carry — where no approval creates
  the channel, so no reviewer is shown the question.
- The typed suppression marker, `lup: ignore[<rule-id>]` as a comment on the
  offending line, silences exactly the anti-pattern it names and no other, so
  the site still trips every rule it left unnamed. [contributing.md](contributing.md)
  carries the scoping — where the marker must sit, comma-separated ids, the
  flagged bare form, and the file-wide placement.

Each rule id is shown in the deny message that cites it, and indexed in
[rules.md](rules.md).

## What a question says

A verdict carries two texts for two readers, and the contract is that
neither borrows from the other. The `reason` is read by whoever approves,
who answers yes or no and can act on nothing else, so it is one sentence of
at most two hundred characters that leads with the operands the decision
turns on — the packages a `--with` installs, the path a write lands on, the
host a fetch reaches — and states the one fact that stopped it. A compound
command that trips several rules lists each survivor after the first, one
per line, because the answer is one decision over the whole operation. The
`recovery` is read by the agent, on a refusal or a question nobody can be
shown, and says what to change; it is where every instruction goes, and an
instruction found in a reason is a defect
`packages/lup/tests/unit/test_reason_voice.py` refuses. Neither carries
reference. A scope table, a rule index, the marker grammar: each is the same
on every occurrence and read on none, so a question names where it is
pulled from — `dev policy`, this page — rather than repeating it.

## Execution does not grant authority

`.lup/hooks/approvals.jsonl` contains execution observations. Historical records
labelled `approved` carry no explicit reusable grant and are never read as
authority. `dev hooks approvals` shows these observations with that limitation;
`dev hooks forget <prefix or exact call>` retires an observation without
changing authorization. Explicit native review answers remain single-use in
`review`; an execution event cannot turn one into a permanent grant.

The `uv` command reader resolves global options before the subcommand, including
`--directory`, so relocating an operator-only queue command cannot make it an
unclassified command that a contained session runs freely.

## Asking before spending a turn on it

A denial is the ordinary way to learn a verdict, and it costs a turn. These
commands answer the same question up front, against the declared policy
rather than a reading of this page:

```bash
uv run lup-devtools dev policy '<the command as you would run it>'
uv run lup-devtools dev policy --kind fetch '<the URL>'
uv run lup-devtools dev policy --kind edit '<the path>'
uv run lup-devtools dev policy --kind edit-batch proposed-edits.json
uv run lup-devtools dev vocabulary --provenance
uv run lup-devtools dev hooks sweep
```

`dev policy` prints the decision and the sentence explaining it — the same
sentence the hook would have shown — for a shell command, and takes the same
lattice through the same segments, so a pipeline or a `$(...)` answers as it
actually would. It reads what the hook reads about the host: the launch's
lease from the ledger, so a write the launch did not mount writable asks here
as it asks there, and the roster, so an edit of a file another live session
holds asks too. Its edit gates hold the rules the plugin was compiled with,
the project's retirements and additions included. It assumes a capture holds
what a write would replace, since only a running session takes one, names
every path it assumed that of, and it answers with the selection the
repository declares where a launch relaxed the rules for one session. It
reads the command from where the session's commands start — the checkout the
launch names, else this directory, or the one `--from` gives — rather than
from wherever it was itself run: `cd <worktree> && dev policy 'cp x
<worktree>/y'` is asked about the copy the session's shell will make, into a
sibling worktree no capture of the session's checkout holds, and it says
which directory it read from when that is not its own. With `--kind fetch` it reads a URL against the declared
scopes and lists every one of them beneath the verdict, which is where the
question a fetch outside them raises sends its reader. `dev vocabulary` prints every shell form the vocabulary
judges and where each rule came from, which is the one to reach for when the
question is "what *would* be allowed here" rather than "is this".

An edit path is a path-only preview over unchanged content. Its output labels
the proposed content and operation as unavailable; it is not approval of an
unspecified edit. For a concrete verdict, `edit-batch` reads a JSON
`EditBatch`: `{"changes": [{"path": "src/example.py", "before": "old\n",
"after": "new\n", "operation": "modify"}]}`. Paths resolve against the
calling checkout, and every preimage must match disk. Creations use null
preimages and deletions null postimages. Both forms use the hook's destination
authority and accepted policy snapshots, retaining the caller's write boundary.

`dev edit-prepare proposed-edits.json --output tmp/prepared.patch` audits the
complete proposed documents before a write is attempted. It collects every
source-rule finding together, including project-wide rules and canonical type
resolution, and reports the declared edit gates. The command writes only a
fresh patch artifact beneath a declared scratch root; it never edits the
targets, submits a review, or grants approval. Submit the emitted patch once
through the native edit tool. An `ask` in this preview means review may be
required when submitted; it does not mean a question has already been queued.

Use `--suppressions exceptions.json` to request specific Python exceptions in
the candidate, as a JSON list:

```json
[{"path": "src/example.py", "line": 8, "rule_id": "import-re",
  "reason": "This module implements the grammar itself."}]
```

Line numbers refer to the proposed document before insertion. Each request
must name a proven, suppressible missing directive and state a reason; strong,
refuted, unresolved and nonmatching sites are refused. The helper merges typed
directives, keeps their reasons, verifies that Python semantics are unchanged,
and audits the resulting candidate again. Other source families are audited
but their exception comments remain explicit edits in the proposal. Missing
type evidence is reported, never converted into an automatic suppression.

The emitted native patch is decoded again and compared with every intended
before/after document and operation. Content that the native patch grammar
cannot preserve exactly, including an empty creation or a missing final
newline, is refused with an explanation. Stale preimages, duplicate targets,
foreign paths and contradictory operations are refused before preparation.
`--json` exposes the complete candidate, findings and readings for tools.

A native hook reporting a pending review has already submitted the request.
Wait for that answer, then retry the exact call. Adding an escalation or
changing the payload creates a different review; an approved request cannot
acquire additional code under its existing receipt. Reviews also bind to the
policy that judged the call: regenerating that policy before retrying can
require a fresh review even when the proposal and its preimages are unchanged.

`hooks sweep` classifies a whole list at once and exits non-zero if any line
is not a plain allow. With no file it reads the everyday corpus this project
declared in `HookSet.everyday_commands` — the commands an ordinary session
runs, which this table must keep allowing — and `dev check` runs the same
sweep, so a rule that tightened something it did not mean to fails at the
gate rather than in somebody's session. That is the one measurement of the
vocabulary that reads a *tightening*: the recorded questions list what a
session was interrupted about, and a verdict census lists what each row
earns, so both go on agreeing when a de-escalation quietly stops firing.

The corpus is swept once per posture a session runs in — interactive, worker,
inner, inner worker, outer, and outer worker — because a verdict is only ever
reached for somebody, and a rule that starts asking where nobody is there to
answer stops a session rather than interrupting one. `--autonomous`,
`--headless`, `--trapped` (the runtime's own sandbox) and `--outer` (the
container) name one posture and ask about that one instead. Pass a file for
a question this project has not settled — a candidate corpus, or the commands
a recorded session was actually stopped for — asked from the posture those
same flags name.

`dev policy` answers as the session running it: the runtime's sandbox from
the launcher's variable, and from the ledger its dispatcher reads, whether a
container stands around it and places work inside, which posture answers
legible work nothing judged, and whether a host executor carries what has to
run outside. A runtime's own per-call escape is an argument of one call and
is not read. `--placement` names one of the placements the launcher spells —
`none`, `inner` or `outer` — and answers for it instead: a named placement
decides both walls itself rather than inheriting the ledger of the session
asking, so a reading taken inside a container still says what a host session
is told. An `outer` reading measures what only a container can: which paths
this machine's mount table lends from elsewhere, and which loopback ports a
process out of sight holds. The everyday sweep names its postures the same
way, so a corpus reads alike from any session.

Only what must keep allowing belongs in that corpus. A command that asks
today is either a defect to fix or a question somebody meant, and neither is
settled by adding it to a list that asserts allow.

## Hook execution evidence

After a write, the hooks sweep the written file with the whole-tree rule check
scoped to it (`repair_command`), then type-check it (`diagnostics_command`).
The sweep runs every rule over every span, project rules included, so what
the gate ahead of the write cannot see is reported per write rather than
first met at the end. It removes dead directives and says so; where the
policy the session loaded would refuse taking a directive out, which happens
when the sources moved since launch, it puts the file back and says the two
disagree.

What reaches the agent comes in two parts. What a gate still refuses is
*blocking*. Claude gets it as
[structured post-edit feedback](https://code.claude.com/docs/en/hooks#posttooluse-decision-control)
(exit 0 with `decision: "block"` and a `reason`), and Codex through stderr
and exit 2. What is only worth knowing is *context*: a removed directive, a
name used before a later edit supplies it (`reportUndefinedVariable`, an
unresolved import, an unknown symbol on an import line), what a question
would have asked about a shell write, or another repository's referral.
Claude and Codex both get it as `hookSpecificOutput.additionalContext`. When
something also blocks, Codex adds it after the refusal. Nothing here undoes
an edit or reports a crashed hook. Another repository's referral is said in
full once per repository per session.

A shell command is reviewed by what it changed, not by what its words name:
the claim window's before-and-after comparison gives every file that moved
from the commit, so a script's writes get the same rule scan as a redirect's.
Only a file the words name meets the path gates as well: a generator rewrites
its own trees, which those gates refuse editing by hand. Codex's patch parser reads every touched path without replaying the old file
contents, so both runtimes run the same sweep and type check after an edit,
including moves.

Both plugins register a short command invoking the generated
`hooks/scripts/policy.sh`. That guard runs `policy.py`, preserves its output
and deliberate refusals, and refuses if the dispatcher cannot start or crashes.
Missing Python calls for installing Python or fixing PATH. Missing or broken
generated files call for `uv run lup-devtools harness generate all` from a
terminal outside the affected session. If a merge left conflict markers in
generated files, settle or abort that merge before regenerating; do not repair
the generated dispatcher by hand. Recovery instructions live in the guard,
so the native runtime does not echo them with every diagnostic.

A runtime lets a call through once its policy hook runs past its timeout —
Claude Code continues through its own permission flow, Codex records the
hook as failed and runs the tool — so a hook still waiting then has answered
nothing. `HookSet.policy_timeout` is declared once: the hooks file each
runtime reads carries it, and the dispatcher opens one deadline five seconds
short of it as it starts. The language server an anti-pattern rule consults,
a destination's accepted evaluator, and every Git and `sed` call take what is
left rather than a timeout of their own, and one cut short reads as the
failure it already answers — no checker looked, so the gate asks; Git could
not say, so no capture is claimed. What nothing can hand a timeout to — a
review-queue lock another writer holds, a read that never returns, the
classifier itself — is stopped by an alarm two seconds past the deadline,
and the dispatcher refuses the call as one it could not judge. Every such
refusal says which cause it was, since each has a different fix: the
deadline reached, input that is not a hook payload at all, or a failure
judging one that is (`host.unjudged_reason`, on both runtimes). A process the
hook starts inherits the deadline and cannot extend it. `dev policy` opens
the same deadline for each reading it takes, from the same declaration
(`lup.policy.bundle.hook_deadline`), so a reading that would wait past it is
refused as the hook it previews would be rather than holding the command.

Plugin hooks receive a writable data directory: `PLUGIN_DATA` under Codex and
`CLAUDE_PLUGIN_DATA` under Claude Code. Each dispatcher appends
`hook-events.jsonl` there as it runs: a `started` record after input parsing,
then `completed` with the final policy outcome or `failed` with the exact
dispatcher exception. Records carry the event, session, turn, tool, tool-use
id, and UTC timestamp. They deliberately omit tool input and output, which may
contain commands, patches, or credentials — with one exception. A call whose
input names a URL also records `fetch_origin`: the scheme, host, and port of
that URL, and nothing else. That is the coarse half a scope is written
against and the half the verdict turned on, so without it a refusal says a
URL was outside the declared scopes without saying which origin asked, and
the host has to be inferred from what the session did next. The path and
query stay omitted because they are where a document id, a search phrase, or
a token spelled into the URL ride; userinfo goes with them, since the host is
read from the parse rather than from the authority that would carry it.

This journal distinguishes failures whose UI is otherwise identical. A
`failed` record is a dispatcher failure; `completed` with `deny` is an
intentional policy refusal; `started` without a terminal record is an
interrupted dispatcher. If the native runtime reports a hook event but no
correlated `started` record exists, the plugin command never began, so the
investigation belongs at its trust, hook-definition, or process-launch
boundary rather than in policy logic. An unwritable journal reports its own
diagnostic but does not change the decision the hook reached.

## How one decision reaches two runtimes

The generated plugins enforce permissions without importing lup, yet decide
identically to the library.

1. **Canonical sources** — the `HookSet` in `harness/catalog.py`
   (protected edit roots, allowed fetch scopes, policy ids, and the shell and
   edit selections), the anti-pattern rule set in `lup.harness.codescan.antipatterns`,
   and the offered shell vocabulary in `lup.policy.vocabulary`. Each selection
   is resolved by `HookSet.resolved_shell_rules` and
   `HookSet.resolved_edit_rules` and nowhere else: a second place that knew
   which defaults a selection layers over is how a session comes to decide
   differently from the plugin its own declaration generated.
2. **Library layer** — `lup.policy.rules` validates those inputs as Pydantic
   surfaces and erases them into primitive rows; `lup.policy.kernel` — the
   hermetic, stdlib-only decision core — interprets those rows to reach every
   shell, fetch, and edit verdict; `lup.policy.chain` composes policies
   deny-before-ask; the adapters' `native` modules decode wire payloads into
   `lup.policy.models` events and render decisions back.
3. **Assembly** — `lup.policy.bundle` reads the kernel source verbatim and
   renders the erased rows as data files; the adapter hook renderers emit
   `hooks/hooks.json`, the guard `hooks/scripts/policy.sh`, the dispatcher
   `hooks/scripts/policy.py`, and
   `hooks/runtime/{kernel.py,policy_data.py}` into each plugin tree.
4. **Equivalence** — the shared fixture suite runs the same cases through the
   library policies and the assembled runtime and requires identical verdicts.

Every rule id a denial cites is indexed in [rules.md](rules.md).
[harness.md](harness.md) covers changing the declarations above, and
[platform-differentiation.md](platform-differentiation.md) records where the
two dispatchers deliberately differ.
