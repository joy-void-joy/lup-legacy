<!-- Generated from lup.devtools.dev.commands by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Command reference

Every command `lup-devtools` serves, walked from the composed CLI at generation time. A command reaches this page by existing, so nothing is left out for want of being remembered — including the ones a session rarely runs directly, which are exactly the ones a hand-written list loses first.

Run any of them with `uv run lup-devtools <command>`, and add `--help` for its arguments and options: the summary here is the first line of each command's own documentation, not a substitute for reading it.

## `agent`

| Command | What it does |
| --- | --- |
| `agent inspect` | Inspect the full agent configuration: tools, schemas, prompt, subagents. |
| `agent capabilities` | Show the backend capability matrix (the parity contract, generated). |
| `agent repl` | Interactive REPL — continuous session with the agent via the SDK. |

## `conversation`

| Command | What it does |
| --- | --- |
| `conversation chatgpt` | Retain ChatGPT conversations and their downloadable attachments. |
| `conversation claude` | Retain Claude conversations and their API-provided attachments. |
| `conversation setup chatgpt` | Open a browser to authenticate ChatGPT conversation access. |
| `conversation setup claude` | Open a browser to authenticate Claude conversation access. |

## `coordination`

| Command | What it does |
| --- | --- |
| `coordination roster` | List every session working in this repository, and the recent departures. |
| `coordination join` | Put a session on the roster and print the id it answers to. |
| `coordination describe` | Record what one session is doing, for whoever reads the roster next. |
| `coordination rename` | Rename one session, leaving the old name resolving to it. |
| `coordination leave` | Record that a session has stopped, so nothing addresses it again. |
| `coordination sweep` | Retire every session whose pulse has stopped, so nothing addresses it again. |
| `coordination send` | Send one message to a peer, and say what will carry it there. |
| `coordination notice` | State something true for every session, now and for whoever starts next. |
| `coordination notices` | Everything standing over this repository, with the id that takes one down. |
| `coordination unnotice` | Take one standing fact down, so no later prompt reads it. |
| `coordination mailbox` | Read what is queued for one session, consuming it only when asked. |
| `coordination holdings` | List what each live session in this repository is holding. |
| `coordination lock` | Take everything beneath a prefix, before having touched any of it. |
| `coordination release` | Give a prefix back, refusing where this session does not hold it. |
| `coordination watch` | Stream what changes: who arrives and leaves, what they are on, what reaches them. |

## `dashboard`

| Command | What it does |
| --- | --- |
| `dashboard serve` | Serve the dashboard in this terminal, over the selected repositories, until Ctrl+C. |
| `dashboard open` | Open the dashboard the running sessions hold, in this machine&#x27;s browser. |
| `dashboard status` | Say whether the dashboard runs, where, for how many sessions, and what waits. |
| `dashboard line` | Print what a session&#x27;s status line shows: the reviews waiting, and where. |
| `dashboard reopen` | Whether a review parking while no tab is open reopens the page in the browser. |
| `dashboard stop` | Stop the running dashboard now; the next session&#x27;s launch starts it again. |

## `dev`

| Command | What it does |
| --- | --- |
| `dev pending` | Report the real pending changes, excluding sandbox-masked device paths. |
| `dev check` | Run ruff format, ruff check, pyright, and pytest. Read-only by default. |
| `dev test` | Run named tests in the suite that installs each, one run per suite. |
| `dev comments` | List unresolved `# lup:` feedback comments, or act on specific ones. |
| `dev todos` | List `# lup: template:` markers — a scaffold&#x27;s open decisions. |
| `dev seams` | Show what this project settled about itself, or settle one of them. |
| `dev refutations` | Resolve one file&#x27;s proposed content and report what it refutes. |
| `dev directives` | Measure every `# lup: ignore` against the canonical inline placement. |
| `dev report-friction` | File or correct workflow friction, on the tracker that owns the fix. |
| `dev undo` | List, take, expire, or repair recoverable snapshots of this tree. |
| `dev history` | Trace a symbol through every branch, past this tree&#x27;s own snapshots. |
| `dev issues` | List the open issues a resolver run would take as evidence. |
| `dev rules` | Print the Lup rule and typed-suppression reference. |
| `dev models` | Read each runtime&#x27;s model lineup from its CLI, and compile its types. |
| `dev settings` | Read the settings keys Claude Code takes from its CLI, and compile their types. |
| `dev modules` | Report which modules this project takes, and what each one&#x27;s prose costs. |
| `dev reach` | Report how this repository&#x27;s work reaches a project built on it. |
| `dev guidance` | Report what each section of the always-loaded guidance costs. |
| `dev relocate` | Move a module and repoint every import of it. |
| `dev update` | Move the library, the native trees, and the copied half to one commit. |
| `dev release` | Cut a release or a candidate of one, or promote the newest candidate. |
| `dev edit-prepare` | Audit proposed edits and prepare one patch without writing their targets. |
| `dev policy` | Show what the declared permission policy decides about an input, and why. |
| `dev vocabulary` | Show every shell form the declared vocabulary judges, and how. |
| `dev env status` | Where this project&#x27;s environment is, and who is installed in it. |
| `dev env sync` | Install this project&#x27;s dependencies into its own environment. |
| `dev plugin name` | Name this repo&#x27;s plugin marketplace uniquely (the plugin entry is kept). |
| `dev tracker comment` | Say something on an issue, here or on a declared tracker. |
| `dev tracker close` | Close an issue, here or on a declared tracker. |
| `dev tracker reopen` | Reopen an issue, here or on a declared tracker. |
| `dev tracker list` | Which repositories this project may reach, and what each is for. |
| `dev migrate map` | Print the relocation that repoints an importer across a range. |
| `dev migrate pyright-environment` | Retire unchanged scaffold Pyright environment defaults. |
| `dev migrate pending` | What a project standing at that commit still owes, beyond the map. |
| `dev migrate check` | Refuse a capability that went with no migration speaking for it. |
| `dev library status` | Report where the lup library is resolved from. |
| `dev library release` | Ask the package index whether a release exists, and which mode that settles. |
| `dev library use` | Resolve lup from the package index, or from the vendored copy. |
| `dev library git` | Resolve lup from its repository, for use before a release is published. |
| `dev scaffold compile` | Materialize upstream&#x27;s copied half at one commit, under this name. |
| `dev scaffold fit` | Measure which upstream commit this project&#x27;s copied half corresponds to. |
| `dev scaffold adopt` | Root the scaffold branch, once, at the commit this project came from. |
| `dev model-config census` | Enumerate every `model_config` declaration by right-hand-side shape. |
| `dev model-config aliases` | List every shared configuration alias, and who imports each one. |
| `dev model-config convert` | Rewrite every assigned `model_config` as class keywords, in place. |
| `dev model-config declared` | Record every class&#x27;s declared configuration, without importing it. |
| `dev model-config declared-at` | Record what every class declared as of a git revision. |
| `dev model-config snapshot` | Record the configuration pydantic resolved onto every model. |
| `dev model-config snapshot-at` | Record the configuration pydantic resolved at a git revision. |
| `dev model-config compare` | Diff two snapshots; exit non-zero when any model&#x27;s config moved. |
| `dev hooks classify` | Say what the policy decides about one shell command, and why. |
| `dev hooks classify-fetch` | Say whether a URL is inside this project&#x27;s declared fetch scopes. |
| `dev hooks sweep` | Classify a list of commands at once, and exit non-zero if any is not allowed. |
| `dev hooks roots` | List the path roles and protected roots the declaration carries. |
| `dev hooks learn` | Review the commands the policy declined to interrupt about. |
| `dev hooks approvals` | List execution observations, including unverified historical approvals. |
| `dev hooks forget` | Retire an execution observation without changing authorization. |
| `dev py info` | Inspect a Python object — adapts to modules, classes, functions, values. |
| `dev py source` | View source code for a Python object, or a package file tree with --tree. |
| `dev py imports` | Show what a module imports, or what imports it (--reverse). |
| `dev py layers` | Show how a package&#x27;s top-level entries import each other, and which pairs close. |
| `dev py text` | Search literal source text within explicitly selected Python paths. |
| `dev py search` | Search project source and installed package exports by name. |
| `dev report` | Everything left to implement, in one place |
| `dev usage claude` | Show live Claude Code usage with pacing bars (Anthropic OAuth). |
| `dev usage codex` | Show live Codex usage with pacing bars (ChatGPT plan). |
| `dev init rename-package` | Rename the lup Python package to a project-specific name. |
| `dev init drop-examples` | Remove the scaffold&#x27;s demonstrations of itself, which no adopter wants. |
| `dev init upstream` | Point the lup registration at the template this repository was generated from. |
| `dev init base` | Name the commit of lup this repository was stamped from: the base. |

## `feedback`

| Command | What it does |
| --- | --- |
| `feedback status` | Show feedback status: version, data, analysis state, and stats. |
| `feedback collect` | Collect feedback metrics from sessions. |
| `feedback costs` | Per-backend cost/token rollup from session JSONs (any backend). |
| `feedback tools` | Show tool usage aggregates. |
| `feedback errors` | Show sessions with high error rates from structured metrics. |
| `feedback trends` | Show metric trends over time. |
| `feedback history` | Show previous feedback collection runs. |
| `feedback mark` | Mark sessions as analyzed in the feedback loop. |
| `feedback unmark` | Remove analysis marks from sessions. |
| `feedback prompt-health` | Analyze the agent prompt for size and patch accumulation. |
| `feedback unanalyzed` | List unanalyzed session IDs, one per line. |
| `feedback analyze` | Produce a structured JSON analysis report (tools, errors, gaps). |
| `feedback commit` | Commit all uncommitted session result files, one commit per session. |

## `git`

| Command | What it does |
| --- | --- |
| `git branches` | Analyze branch containment, PR status, and worktree info. |
| `git base-branch` | Detect the base branch for the current (or specified) branch. |
| `git freshness` | Report how far this checkout sits behind its own remote and its base. |
| `git pr-body` | Generate a PR body (summary, commits, test plan) from branch commits. |
| `git survey` | Full branch inventory: containment, PRs, unique commits, diff sizes. |
| `git preview` | Say what landing each branch would change, conflict on, and share. |
| `git settle` | Regenerate over the merge commit HEAD just became, and fold it in. |
| `git merge-driver` | Register the ownership-manifest merge driver `.gitattributes` names. |
| `git delete` | Delete a branch and its worktree, and origin&#x27;s copy if it is spent. |
| `git retire` | Retire a branch through a pull request, so its commits outlive it. |
| `git worktree create` | Create or re-attach a git worktree. |
| `git worktree list` | List all git worktrees with branch and status info. |
| `git worktree remove` | Remove a git worktree. |
| `git worktree adopt-records` | Move lup&#x27;s `branch.*.lup-*` config keys into the shared `lup/` directory. |
| `git pr status` | Fetch PR review status, checks, and comments for a branch. |
| `git pr merge` | Merge a PR and pull changes into the integration branch. |
| `git pr prepare` | Merge an explicit local base, regenerate every harness, and commit. |
| `git pr sync-base` | Sync the base branch and merge it into the current feature branch. |
| `git pr push` | Push the current branch and report any existing PR. |
| `git pr create` | Create a new PR. |
| `git pr update` | Update a PR body. |
| `git conflict list` | Show conflicted files with scope classification (in-scope vs out-of-scope). |
| `git conflict status` | Detect conflict state, list files, and show both sides&#x27; history. |
| `git conflict audit` | Post-resolution deletion audit: check for accidentally dropped code. |
| `git conflict union` | Settle a conflict where both sides inserted at one place, keeping both. |
| `git conflict complete` | Finalize the merge/rebase/cherry-pick after all conflicts are resolved. |
| `git hooks install` | Install every git hook this repository declares. |
| `git hooks status` | Report what this clone refuses, at every moment a hook sits at. |
| `git hooks uninstall` | Remove them, leaving hooks written elsewhere alone. |

## `harness`

| Command | What it does |
| --- | --- |
| `harness generate` | Deterministically generate owned native artifacts without launching. |
| `harness policy-refresh` | Accept changed destination policy from an independent operator terminal. |
| `harness check` | Read-only ownership and generated-artifact drift check for CI. |
| `harness reconcile` | Classify local differences without rewriting canonical Python source. |
| `harness apply-reconciliation` | Apply a stale-base-checked source patch, then regenerate every target. |
| `harness propose-reconciliation` | Persist a source patch for separate review and stale-base-checked apply. |
| `harness doctor` | Report installed native runtime evidence without updating either CLI. |
| `harness requirements` | Check dependencies on the host, or in the session container with --inside. |
| `harness binds` | Check, inside a session, that every read-only bind it launched with holds. |
| `harness sandbox-check` | Evaluate arithmetic in a disposable Python sandbox without network access. |
| `harness image` | Render the container image this project&#x27;s sessions run in. |
| `harness clean` | List everything lup keeps for sessions, and what nothing points at. |
| `harness egress` | Report or remove the network boundary this project&#x27;s sessions run behind. |
| `harness claude` | Generate/reconcile Claude artifacts and launch the verified plugin. |
| `harness codex` | Generate/reconcile Codex artifacts and launch without updating the CLI. |
| `harness profile list` | Show every profile, local and global, and which one a launch selects. |
| `harness profile add` | Register a runtime configuration home under a name, in this checkout. |
| `harness profile use` | Select the profile a launch uses when none is named, in this checkout. |
| `harness profile remove` | Forget a profile in this checkout, leaving its configuration home on disk. |
| `harness profile migrate` | Move this checkout&#x27;s profiles, and the old ~/.lup registry&#x27;s, to global. |
| `harness codex-plugin install` | Install the declared plugin and verify native discovery in the selected home. |
| `harness codex-home migrate` | Move each worktree&#x27;s Codex home out of the checkout, into lup&#x27;s state. |

## `ledger`

| Command | What it does |
| --- | --- |
| `ledger types` | List the node and edge types this project declares, with their fields. |
| `ledger record` | Record one node of any declared kind; the kind validates the fields. |
| `ledger relate` | Draw one edge of any declared kind between two nodes. |
| `ledger amend` | Record a node again with some fields changed; nothing is overwritten. |
| `ledger cite` | Check every cite in one document against where its node stands now. |
| `ledger writeup` | Generate the documents this project declares over its ledger. |
| `ledger list` | Show every node in this repository, with its standing read now. |
| `ledger show` | Print one node in full, with its edges and what it attaches. |
| `ledger delegate` | Record one task, hand it to a peer if there is one, and say what happened. |
| `ledger handoff` | Move a body of work to a peer, and say exactly what crossed. |
| `ledger brief` | Write one handoff out for whoever is going to read it. |
| `ledger mine` | Print one holder&#x27;s outstanding tasks, grouped by what they cost. |
| `ledger done` | Mark one piece of work finished, by recording it again as done. |
| `ledger migrate` | Copy a kind&#x27;s records into the journal its placement now declares. |
| `ledger index-notes` | Record the runs already under notes/ that the log points at nothing of. |
| `ledger snapshot` | Commit the local half to a branch of its own, for a record worth keeping. |
| `ledger explore` | Open the log in a browser, or write it as one self-contained page. |

## `resolve`

| Command | What it does |
| --- | --- |
| `resolve` | Drive a resolver run, and watch or answer it |
| `resolve status` | Say whether a run is alive, where it stands, and what it last did. |
| `resolve cost` | Report journal timing, unresolved intervals, actor turns, failures, and idle gaps. |
| `resolve recover-integration` | Reconcile integration explicitly, retaining answers and completed concern work. |
| `resolve supervise` | Answer any run under ``.lup/resolve``, live or parked. |
| `resolve questions` | List a run&#x27;s questions and what each one has been answered. |
| `resolve answer` | Offer an answer to one or more of a run&#x27;s questions. |
| `resolve actors` | List every actor this run has recorded, and what each has not read yet. |
| `resolve rebind-actor` | Retire one binding so resume opens a fresh conversation with current schemas. |
| `resolve say` | Tell one actor something. It reads this and keeps going. |
| `resolve accept` | Accept one concern over one failing verification, on the human&#x27;s word. |
| `resolve retire` | Retire one concern whose work was settled somewhere other than this run. |
| `resolve redirect` | Stop an actor and put it on something else. |
| `resolve park` | Ask every open wait in this run to give up now. |
| `resolve drain` | Ask a busy run to finish what is in flight and stop, resumably. |
| `resolve refresh` | Bring a run&#x27;s base, and the leases holding work, up to its branch. |
| `resolve intake` | Print what a run started now would plan from, without starting one. |
| `resolve admissions` | Inspect accepted evidence and its pending, applied, or rejected result. |
| `resolve serve-tools` | Serve one worker&#x27;s question tools over stdio, for out-of-process runtimes. |
| `resolve branch` | Create + switch to the resolve/&lt;id&gt; branch (a resolve editor&#x27;s first step). |
| `resolve review` | Render a resolve manifest and its branch diffs into one static HTML review. |
| `resolve summary` | Print per-concern verdicts from a resolve manifest. |

## `review`

| Command | What it does |
| --- | --- |
| `review list` | List the reviews parked in this checkout, and what each is waiting on. |
| `review show` | Show one review whole, including the operation it would resume. |
| `review approve` | Approve one review, optionally with a note for the agent. |
| `review decline` | Decline one review, optionally saying what to do instead. |
| `review cancel` | Withdraw a review nobody needs answered any more. |
| `review wait` | Wait on this session&#x27;s reviews, carrying out each one the operator approves. |

## `run`

| Command | What it does |
| --- | --- |
| `run monitor` | Follow a background run: its landed units, their statuses, its heartbeat. |
| `run report` | Say how far into its own work a unit has got, from any language. |

## `setup`

| Command | What it does |
| --- | --- |
| `setup` | Interactive setup wizard, and its page |
| `setup status` | Show current integration status. |
| `setup slack` | Set up Slack tokens. |
| `setup google` | Set up Google OAuth. |
| `setup notion` | Set up Notion integration. |
| `setup api-key` | Set up Example API key. |
| `setup codex` | Set Codex/OpenAI per-MTok pricing (enables budget caps). |
| `setup timezone` | Set timezone. |
| `setup secret` | Set a key in this project&#x27;s host store, which only host companions are handed. |
| `setup profile list` | Show every profile, local and global, and which one a launch selects. |
| `setup profile add` | Register a runtime configuration home under a name, in this checkout. |
| `setup profile use` | Select the profile a launch uses when none is named, in this checkout. |
| `setup profile remove` | Forget a profile in this checkout, leaving its configuration home on disk. |
| `setup profile migrate` | Move this checkout&#x27;s profiles, and the old ~/.lup registry&#x27;s, to global. |

## `sync`

| Command | What it does |
| --- | --- |
| `sync upstream` | Print a measured upstream defect, or list the ones declared. |
| `sync usage` | Show what each tracked project imports from a package, name by name. |
| `sync status` | Show tracked projects and their sync status (read-only). |
| `sync fetch` | Clone missing repos and fetch cached ones (network + writes). |
| `sync log` | List commits to review: everything upstream added since the last sync. |
| `sync diff` | Show full diff for a specific commit. |
| `sync mark-synced` | Share the reviewed checkpoint across this repository&#x27;s worktrees. |
| `sync setup` | Set the local path for a project (writes to sync.json.local). |
| `sync remote` | Record how this machine reaches a repository (writes to sync.json.local). |
| `sync grant` | Grant sessions on this machine a host device (writes to sync.json.local). |
| `sync revoke` | Take a device back from sessions on this machine (writes to sync.json.local). |

## `tools`

| Command | What it does |
| --- | --- |
| `tools serve` | Serve one hosted tool server over MCP stdio, for the session it names. |

## `trace`

| Command | What it does |
| --- | --- |
| `trace show` | Show trace for a session. |
| `trace search` | Search traces for a pattern. |
| `trace list` | List available traces. |
| `trace errors` | Show sessions with errors found in trace files. |
| `trace capabilities` | Extract capability requests from traces. |
| `trace archive` | Copy a worktree&#x27;s session records into the archive beside the repository. |

## `version`

| Command | What it does |
| --- | --- |
| `version` | Agent version, changelog, and bump |
| `version changelog` | Show changes since a version tag, classified by type. |
| `version bump` | Bump agent version, record the release, and create a git tag. |
