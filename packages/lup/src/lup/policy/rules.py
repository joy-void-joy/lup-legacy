"""Library-side validated policies that erase into kernel rows to decide.

Each policy (shell, fetch, edit) validates its configuration as pydantic
surfaces — URL scopes, path rules, the canonical anti-pattern set — then
flattens them into primitive rows and delegates every verdict to
:mod:`lup.policy.kernel`. :mod:`lup.policy.bundle` performs the same erasure
at generation time, which is how this layer and the generated dispatchers
stay decision-identical; the shared fixture suite asserts exactly that.
"""

import json
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, Field

from lup.harness.codescan.antipatterns import RuleSet, patterns_for_suffix
from lup.harness.codescan.common import AntiPattern
from lup.policy.contracts import DecisionPolicy
from lup.policy.grants import LeaseGrants
from lup.policy.identity import AGENT_IDENTITY_ENV
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.policy_protocol import read_response, routing_failure
from lup.policy.kernel.edit import (
    decide_edit,
    path_rule_matches as kernel_path_rule_matches,
)
from lup.policy.kernel.fetch import decide_fetch, loopback_port
from lup.policy.kernel.semantics import UnjudgedAmbient
from lup.policy.assets.host import (
    declared_identity,
    routed_edit_response,
    directory_write_targets,
    empty_directory_targets,
    foreign_repository,
    host_held_ports,
    ignored_write_targets,
    measured_boundary,
    measured_landings,
    outside_this_project,
    readonly_write_targets,
    recoverable_write_targets,
    resolved_write_targets,
    rewritten_text,
    sibling_worktrees,
    walked_withheld,
    text_at,
    this_checkout_path,
    tracked_write_targets,
    unleased_write_targets,
    peer_store,
)
from lup.coordination.bare.store import claim_holders
from lup.policy.kernel.effects import STRENGTH
from lup.policy.kernel.lex import (
    authored_writes,
    command_segments,
    parse_shell,
    named_write_verdict,
    shell_flag_write_targets,
    shell_path_verb_targets,
    shell_sed_rewrites,
    shell_write_targets,
    shell_written_targets,
)
from lup.policy.kernel.peers import decide_foreign_claim, settled_with_claim
from lup.policy.kernel.roles import displaced_targets, sibling_scratch_rows
from lup.policy.kernel.rows import (
    AcceptanceGuardRow,
    AntiPatternRow,
    DisplacedTargetRow,
    PathRoleRow,
    PathRuleKind,
    PathRuleRow,
    PeerPolicyRow,
    RewriteReading,
    RewrittenDocumentRow,
    UnproducedDocumentRow,
    UrlScopeRow,
    WithheldWalkRow,
    landing_rows,
    unproduced_cause,
)
from lup.policy.kernel.shell import (
    decide_shell,
    decide_shell_segment,
    shell_context,
    shell_posture_targets,
)
from lup.policy.kernel.walks import shell_walked_roots
from lup.policy.kernel.withheld import (
    carries_withheld_name,
    withheld_edit,
    withheld_names,
    withheld_row,
)
from lup.policy.edit_rules import EditRule, erase_edit_rules
from lup.policy.imports import ImportBoundary
from lup.policy.refused_paths import RefusedPaths
from lup.policy.assets.host import worktree_path, worktree_root
from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    erase_runner_targets,
    runner_target_tables,
    erase_shell_rules,
)
from lup.policy.models import (
    Decision,
    EditBatch,
    EditChange,
    FetchUrl,
    ShellCommand,
    UrlPathPrefix,
)


def pydantic_decision(decision: KernelDecision) -> Decision:
    """Restore the validated public decision at the kernel boundary.

    The correspondence itself lives on :meth:`Decision.of`, beside its
    inverse, so the two spellings of a verdict cannot be related differently
    in two places. This name stays because it is what the policy classes here
    read as, at the point where a kernel call returns.
    """
    return Decision.of(decision)


class UrlScope(BaseModel, frozen=True):
    """One normalized scheme/host/port and path-prefix rule."""

    origin: AnyHttpUrl
    path_prefix: UrlPathPrefix = "/"
    reason: str = ""
    include_subdomains: bool = False
    any_port: bool = False


# The three erasures to the kernel's primitive rows read alike on purpose, and
# `antipattern_row` erases a model another module declares, so it could not be
# a method even if these two were.
def url_scope_row(scope: UrlScope) -> UrlScopeRow:
    """Erase a validated URL scope into the kernel's primitive row."""
    parsed = urlsplit(str(scope.origin))
    if parsed.hostname is None:
        raise ValueError("validated URL scope has no hostname")
    return UrlScopeRow(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=parsed.port,
        path_prefix=scope.path_prefix,
        reason=scope.reason,
        include_subdomains=scope.include_subdomains,
        any_port=scope.any_port,
    )


class FetchPolicy(DecisionPolicy[FetchUrl]):
    """Evaluate deny scopes before allow scopes, and let the project answer the rest.

    ``unscoped`` is what an origin no scope names answers, and the shell
    policy's downloader screen is handed the same value, so one
    declaration answers on every surface rather than on one.

    ``contained`` says the session sits in a container sharing the host's
    network, where a loopback port is the session's own only if one of the
    container's processes holds its listener -- the dispatcher's reading,
    taken from this machine's socket tables.
    """

    def __init__(
        self,
        allowed: list[UrlScope],
        denied: list[UrlScope],
        unscoped: UnjudgedAmbient = "ask",
        contained: bool = False,
    ) -> None:
        self.allowed = list(allowed)
        self.denied = list(denied)
        self.unscoped: UnjudgedAmbient = unscoped
        self.contained = contained

    def decide(self, event: FetchUrl) -> Decision:
        port = loopback_port(str(event.url))
        return pydantic_decision(
            decide_fetch(
                str(event.url),
                [url_scope_row(scope) for scope in self.allowed],
                [url_scope_row(scope) for scope in self.denied],
                self.unscoped,
                host_listener=self.contained
                and port is not None
                and port in host_held_ports(Path("/proc")),
            )
        )


class ShellSegment(BaseModel, frozen=True):
    """One parsed command segment with its ordered shell words."""

    words: list[str] = Field(min_length=1)


def parse_shell_segments(command: str) -> list[ShellSegment] | None:
    """The segments a gate deciding on argv alone may read, or ``None``.

    What the resolver worker's shell gate takes, which is the one reading of
    a command line that judges the words rather than the effects: it admits
    `git status` and refuses `git commit`, and it has no facts about the
    filesystem to decide a write with.

    ``None`` for a line the kernel would not read as plain segments: one that
    does not parse, runs nothing, or carries a redirection it would stop when
    judged with no facts about the filesystem. A consumer reading only argv
    would otherwise wave through `echo x > .git/HEAD` as an `echo`.
    """
    tree = parse_shell(command)
    if isinstance(tree, KernelDecision) or named_write_verdict(tree) is not None:
        return None
    segments = command_segments(tree)
    return [ShellSegment(words=words) for words in segments] if segments else None


class ShellPolicy(DecisionPolicy[ShellCommand]):
    """Delegate shell classification to the shared hermetic kernel.

    The vocabulary is the caller's: ``rules`` is the whole table this project
    judges, not an extension of one the library chose. URL scopes feed the
    kernel's downloader screen, so shell reads and WebFetch consult one declared
    origin table, and ``unscoped_fetch`` is what an origin outside it answers
    -- the value :class:`FetchPolicy` is handed.
    """

    def __init__(
        self,
        rules: list[ShellCommandRule],
        allowed_urls: list[UrlScope] | None = None,
        denied_urls: list[UrlScope] | None = None,
        sandbox_active: bool = False,
        sandbox_excluded_commands: list[str] | None = None,
        escapable: bool = False,
        recovered: bool = False,
        contained: bool = False,
        inside_placement: bool = False,
        trusted_script_roots: list[str] | None = None,
        interactive: bool = True,
        path_roles: list[PathRoleRow] | None = None,
        path_rules: list["PathRule"] | None = None,
        recoverable_target_limit: int = 5,
        runner_targets: list[RunnerTargetRule] | None = None,
        relayed: bool = False,
        authored: "EditPolicy | None" = None,
        unscoped_fetch: UnjudgedAmbient | None = None,
        refused_paths: list[RefusedPaths] | None = None,
        secret_variables: list[str] | None = None,
        unjudged_ambient: UnjudgedAmbient = "ask",
    ) -> None:
        self.unscoped_fetch: UnjudgedAmbient | None = unscoped_fetch
        # What legible work nothing judged answers in an uncontained session:
        # a launched session's measured posture where a caller read one --
        # `dev policy` reading its own session -- and the question otherwise.
        self.unjudged_ambient: UnjudgedAmbient = unjudged_ambient
        self.refused_paths = [paths.erased() for paths in refused_paths or []]
        self.secret_variables = secret_variables or []
        self.authored = authored
        """The edit policy a write carrying its own content is put to.

        Narrowed to the concrete policy rather than the family it implements,
        because a command that rewrites a file in place is judged by the
        declarations rather than by the verdict: the kernel puts the document
        the rewrite would produce through the same gates, and it is handed the
        rows to do it with. Reading them off this instance is what keeps the
        promise below true for that route as well.

        The same instance the composition gives the edit family, because it is
        the same judgement: one table saying what may be written, reached by
        whichever call carries the bytes. A second instance here would be a
        second table to keep in step, and the one that fell behind would read
        as a decision.

        ``None`` leaves a redirection judged by its path alone, which is what
        every shell write was judged by before -- correct for output produced
        by running, and a hole for output the command is holding.
        """
        self.path_rules = [path_rule_row(rule) for rule in path_rules or []]
        self.path_roles = path_roles or []
        self.recoverable_target_limit = recoverable_target_limit
        self.runner_targets = erase_runner_targets(runner_targets or [])
        self.target_tables = runner_target_tables(runner_targets or [])
        self.escapable = escapable
        # A fact about the session rather than about any command, and not
        # discoverable from here: whether the undo layer holds this tree. A
        # caller that knows says so, and one that does not gets the strict
        # answer -- the one that relaxes nothing.
        self.recovered = recovered
        # The container's half of the same question, and measured elsewhere for
        # the same reason. They are stored apart and joined by
        # `SettlementFacts.bounded`, so a composition that knows only one of
        # them says only that rather than deciding what confinement means.
        self.contained = contained
        self.inside_placement = inside_placement
        self.rules = erase_shell_rules(rules)
        self.allowed_scopes = [url_scope_row(scope) for scope in allowed_urls or []]
        self.denied_scopes = [url_scope_row(scope) for scope in denied_urls or []]
        self.sandbox_active = sandbox_active
        self.sandbox_excluded_commands = sandbox_excluded_commands or []
        self.trusted_script_roots = trusted_script_roots or []
        self.interactive = interactive
        self.relayed = relayed

    def authored_verdict(self, event: ShellCommand) -> Decision | None:
        """What the edit gates say about the writes this command carries.

        A redirection is judged by its path because a command produces its
        output by running and nothing could read it first. `cat > f <<'EOF'`
        and `echo x > f` carry the bytes instead, so the gates an `Edit` is
        put to -- the anti-pattern audit, the review-note gate, the size
        budget -- can read exactly what would land, at the moment that still
        changes the answer. Measured before this: a heredoc replaced a tracked
        library module with one line, allowed and unprompted.

        The preimage is read here rather than left to
        :meth:`~lup.policy.models.EditChange.as_documents`, which would resolve
        it against this process's directory. The command's targets are relative
        to the session's, and those are the same directory only by luck.

        ``None`` where nothing was read: a command whose output is produced by
        running, or a file this process cannot open. Neither is a refusal --
        the reading is what a relaxation needs, not a gate of its own.
        """
        if self.authored is None:
            return None
        root = event.cwd or Path.cwd()
        changes = [
            EditChange(
                path=Path(write["path"]),
                before=before,
                after=(before or "") + write["content"]
                if write["append"]
                else write["content"],
                operation="modify"
                if write["append"]
                else "overwrite"
                if existing
                else "create",
            )
            for write in authored_writes(event.command)
            for existing in [(root / write["path"]).is_file()]
            for before in [text_at(root, write["path"]) if existing else None]
        ]
        if not changes:
            return None
        # The edit gates alone, as the dispatchers' authored review reads them:
        # a peer's claim is asked about by an edit tool, while a command's
        # writes are attributed to it after it runs.
        authored = self.authored
        return joined([authored.decide_change(change, root) for change in changes])

    def rewritten_documents(self, event: ShellCommand) -> RewriteReading:
        """What every in-place rewrite in this command would leave behind.

        Resolved here and judged in the kernel, which is the arrangement that
        makes ``dev policy`` and the generated dispatcher answer alike: both
        halves resolve the same documents on their own side of the boundary
        and hand them to one classifier, rather than each joining an edit
        verdict onto a shell verdict in its own words.

        A file this could not produce yields the reason it could not, and the
        classifier says which reason rather than that something went unproduced.

        ``resolution`` is left unanswered here for the reason
        :meth:`EditPolicy.decide_change` leaves it unanswered: resolving a
        finding needs a language server, which is a cost the generated
        dispatcher pays where it can and an in-process reading does not.
        """
        root = event.cwd or Path.cwd()
        # First naming wins, and the fold is written backwards to get it: one
        # file named by two rewrites is one file, and re-running the second
        # script over it would answer about a document the first already
        # replaced.
        scripts_for = {
            target: rewrite["scripts"]
            for rewrite in reversed(shell_sed_rewrites(event.command, self.rules))
            for target in reversed(rewrite["targets"])
        }

        # lup: ignore[empty-collection] — one reading feeding two collections:
        # each target reaches exactly one of them and which it is costs a sed
        # run, so a comprehension per list would run every script twice
        documents: list[RewrittenDocumentRow] = []
        unproduced: list[UnproducedDocumentRow] = []  # lup: ignore[empty-collection]
        for target, scripts in scripts_for.items():
            attempt = rewritten_text(scripts, target, root)
            after = attempt["text"]
            if after is None:
                unproduced.append(
                    UnproducedDocumentRow(
                        target=target, cause=unproduced_cause(attempt["cause"])
                    )
                )
                continue
            before = text_at(root, target)
            if before is None:
                unproduced.append(
                    UnproducedDocumentRow(target=target, cause="unreadable")
                )
                continue
            document = RewrittenDocumentRow(
                target=target,
                path=worktree_path(str((root / target).resolve())),
                before=before,
                after=after,
                foreign=foreign_repository(target, root),
                outside_project=outside_this_project(target, root),
                checkout_path=this_checkout_path(target, root),
                resolution=None,
            )
            if self.authored is not None:
                document["decision"] = self.authored.decide_change(
                    EditChange(path=Path(target), before=before, after=after), root
                ).as_kernel()
            documents.append(document)
        return RewriteReading(documents=documents, unproduced=unproduced)

    def rewrite_antipatterns(
        self, rows: list[RewrittenDocumentRow]
    ) -> dict[str, list[AntiPatternRow]]:
        """The anti-pattern table for exactly the suffixes a rewrite touches.

        Compiled per classification rather than held, because the rules are
        selected by file suffix and a command that rewrites nothing needs none
        of them compiled at all. Drawn from the edit policy's own set, so a
        rule the project retired is retired for a rewrite as for an edit.
        """
        rules = None if self.authored is None else self.authored.rules
        return {
            suffix: []
            if (patterns := patterns_for_suffix(suffix, rules)) is None
            else [antipattern_row(rule) for rule in patterns]
            for suffix in {Path(row["path"]).suffix.lower() for row in rows}
        }

    def decide(self, event: ShellCommand) -> Decision:
        root = event.cwd or Path.cwd()
        # One measurement of one launch, read once for every fact drawn from it.
        # lup: solved: the dispatchers also draw the unjudged posture and the
        # host-executor channel from this ledger; a composition takes neither,
        # because the ledger describes the launched session while a nested
        # agent composed here answers through its own runtime's permission
        # flow. Decide whether `dev policy` and the sweep read both, with
        # nested agents left as they are.
        boundary = measured_boundary(root)
        acted_on = shell_path_verb_targets(event.command, self.rules)
        flagged = shell_flag_write_targets(event.command, self.rules)
        # The edit gates, over the files a rewrite in place would replace. Off
        # the one edit policy this composition holds rather than a second set
        # declared here: a rewrite is an edit spelled as a command, and two
        # tables would be two answers to what may be written.
        rewritten = self.rewritten_documents(event)
        edits = self.authored
        # Another checkout of this repository keeps this one's scratch, as
        # the dispatchers read it: asked of Git only where the command names
        # an absolute path at all.
        siblings = (
            sibling_worktrees(root)
            if any(
                target.startswith("/")
                for target in [*shell_write_targets(event.command), *acted_on, *flagged]
            )
            else []
        )
        verdict = pydantic_decision(
            decide_shell(
                event.command,
                self.rules,
                self.allowed_scopes,
                self.denied_scopes,
                sandboxed=self.sandbox_active and not event.unsandboxed,
                excluded_commands=self.sandbox_excluded_commands,
                trusted_script_roots=self.trusted_script_roots,
                path_roles=[
                    *self.path_roles,
                    *sibling_scratch_rows(siblings, self.path_roles),
                ],
                path_rules=self.path_rules,
                interactive=self.interactive,
                existing_targets=[
                    target
                    for target in [
                        *shell_write_targets(event.command),
                        *acted_on,
                        *flagged,
                    ]
                    if (root / target).exists()
                ],
                tracked_targets=tracked_write_targets(
                    [*shell_write_targets(event.command), *acted_on, *flagged], root
                ),
                recoverable_targets=recoverable_write_targets(
                    [*shell_write_targets(event.command), *acted_on], root
                ),
                displaced_targets=displaced_targets(
                    [
                        DisplacedTargetRow(path=path, lands=lands)
                        for path, lands in resolved_write_targets(
                            [
                                *shell_write_targets(event.command),
                                *acted_on,
                                *flagged,
                            ],
                            root,
                        ).items()
                    ],
                    self.path_roles,
                ),
                directory_targets=directory_write_targets(acted_on, root),
                empty_directories=empty_directory_targets(acted_on, root),
                # The launch's lease and its read-only holes, read off the
                # ledger the native dispatchers read, so a write the launch
                # did not mount is asked about and a `cp` into a hole is
                # refused here exactly as a session meets either.
                unleased_targets=unleased_write_targets(
                    [*shell_write_targets(event.command), *acted_on], boundary, root
                ),
                readonly_targets=readonly_write_targets(
                    [
                        *shell_write_targets(event.command),
                        *shell_written_targets(event.command, self.rules),
                    ],
                    boundary,
                    root,
                ),
                recoverable_target_limit=self.recoverable_target_limit,
                runner_targets=self.runner_targets,
                target_tables=self.target_tables,
                rewritten_documents=rewritten["documents"],
                unproduced_documents=rewritten["unproduced"],
                antipattern_rows=self.rewrite_antipatterns(rewritten["documents"]),
                edit_rules=[] if edits is None else edits.edit_rules,
                import_boundaries=[] if edits is None else edits.import_boundaries,
                acceptance_guard=None if edits is None else edits.acceptance_guard,
                maximum_added_lines=3 if edits is None else edits.maximum_added_lines,
                autonomous=edits is not None and edits.autonomous,
                allowances=[] if edits is None else edits.grants.granted(),
                escapable=self.escapable,
                unjudged_ambient=self.unjudged_ambient,
                # The capture this session holds takes nothing Git ignores, so
                # one ignored target leaves the loss uncaptured.
                recovered=self.recovered
                and not ignored_write_targets(
                    [
                        *shell_write_targets(event.command),
                        *shell_written_targets(event.command, self.rules),
                    ],
                    root,
                ),
                contained=self.contained,
                # The same root the write readings above resolve against, so
                # an absolute path inside the checkout reaches the declared
                # roles that are anchored at its top.
                checkout_root=str(root),
                inside_placement=self.inside_placement,
                relayed=self.relayed,
                unscoped_fetch=self.unscoped_fetch,
                refused_paths=self.refused_paths,
                # What a recursive reader would walk into beneath a root it
                # names, walked here as the dispatchers walk it.
                withheld_walks=[
                    WithheldWalkRow(root=walk["path"], found=found)
                    for walk in shell_walked_roots(event.command, self.rules)
                    for names in [withheld_names(self.refused_paths)]
                    for found in [
                        walked_withheld(
                            walk["path"],
                            walk["hidden"],
                            lambda name: carries_withheld_name(name, names),
                            lambda path: (
                                withheld_row(path, self.refused_paths) is not None
                            ),
                            root,
                        )
                    ]
                    if found
                ],
                secret_variables=self.secret_variables,
                # The same two measurements a dispatcher takes inside a
                # container, taken wherever this composition was told it is
                # in one -- so a reading of that posture from a host checkout
                # places each path by this machine's own mount table.
                landings=(
                    landing_rows(
                        measured_landings(
                            shell_posture_targets(event.command, self.rules),
                            boundary,
                            root,
                        )
                    )
                    if self.contained and self.inside_placement
                    else []
                ),
                host_ports=(
                    host_held_ports(Path("/proc"))
                    if self.contained and self.inside_placement
                    else []
                ),
            )
        )
        # Strongest wins, the rule every other join in this policy uses. The
        # shell verdict is kept where it is at least as strong, because it
        # carries the placement and the checkpoint that an edit verdict has
        # nothing to say about.
        carried = self.authored_verdict(event)
        if carried is None or STRENGTH.index(carried.effect) <= STRENGTH.index(
            verdict.effect
        ):
            return verdict
        return carried

    def decide_segment(self, segment: ShellSegment) -> Decision:
        return pydantic_decision(
            decide_shell_segment(
                segment.words,
                shell_context(
                    self.rules,
                    self.allowed_scopes,
                    self.denied_scopes,
                    self.trusted_script_roots,
                    self.path_roles,
                    self.path_rules,
                    refused_paths=self.refused_paths,
                    secret_variables=self.secret_variables,
                ),
            )
        )


class PathRule(BaseModel, frozen=True):
    """One semantic protected-path match supplied by a composition root.

    ``kind`` spans the whole primitive vocabulary rather than a subset of it.
    A rule a generated dispatcher can enforce and a composed session cannot
    express is a rule whose reach depends on who launched the run, which is
    the one thing single-sourcing the policy exists to prevent.
    """

    kind: PathRuleKind
    value: str
    reason: str
    recovery: str = ""
    """What the agent does instead of writing here, where there is such a route."""
    allow_autonomous: bool = False

    def matches(self, path: Path) -> bool:
        """Compare a path with this rule through the canonical kernel matcher."""
        return kernel_path_rule_matches(
            path.as_posix(), path.exists(), path_rule_row(self)
        )


def path_rule_row(rule: PathRule) -> PathRuleRow:
    """Erase one validated path rule into the kernel's primitive row."""
    return PathRuleRow(
        kind=rule.kind,
        value=rule.value,
        reason=rule.reason,
        recovery=rule.recovery,
        allow_autonomous=rule.allow_autonomous,
    )


def human_owned_path_rule(path: str) -> PathRule:
    """Declare one human-owned file whose edits always require approval."""
    return PathRule(
        kind="exact",
        value=path,
        reason=f"{path} is human-authored",
        recovery="Propose the exact change and let the user apply it.",
    )


def protected_root_rule(root: str) -> PathRule:
    """One declared protected root, as the path rule it compiles to.

    The one compilation both enforcement paths read: a session this program
    composes and the rows a generated dispatcher carries, so the two cannot
    come to disagree about which paths a root covers.

    A root is anchored at the repository top, and one spelled from anywhere
    (`**/uv.lock`) names the path wherever it sits, the way a path role's
    pattern does: a manifest or a lockfile is what it is in whichever package
    holds it. Scratch matches by path part for the same reason, because a
    scratch directory is reachable at more than one root and the rule is about
    what the directory is, not where it sits.

    No rule here releases an autonomous identity. A protected root is where a
    session's own boundary is declared -- its settings, its launch registry,
    its measured preflight, the policy itself -- and an identity trusted to
    review its own edits is still the confined thing choosing what confines
    it, so the question reaches a person whoever is asking.
    """
    match PurePosixPath(root).parts:
        case ("tmp",):
            return PathRule(
                kind="contains_part",
                value=root,
                reason="scratch path requires approval",
            )
        case ("**", *named) if named:
            return PathRule(
                kind="contains_part",
                value=PurePosixPath(*named).as_posix(),
                reason="protected path requires approval",
            )
    return PathRule(
        kind="subtree",
        value=root,
        reason="protected path requires approval",
    )


def dependency_declarations(
    names: tuple[str, ...] = (
        "pyproject.toml",
        "package.json",
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "bun.lock",
        "bun.lockb",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
    ),
) -> list[Path]:
    """The manifests and lockfiles that decide what an install fetches and runs.

    Protected wherever they sit, because a package's own manifest declares
    dependencies and scripts as surely as the root's does, and a lockfile is
    the exact artefact every later `sync` or `install` trusts without reading.
    The commands that write them for a reason -- `uv lock`, `uv add`, `bun
    install` -- are judged by the dependency rows; what this guards is a hand
    writing one directly.

    A default a composition root takes or replaces, as it does the credential
    files: which ecosystems a project uses is the project's call.
    """
    return [Path("**", name) for name in names]


def antipattern_row(rule: AntiPattern) -> AntiPatternRow:
    """Erase one declared rule into the primitive row the kernel matches on.

    The single projection from the declaration to the runtime shape. Both the
    live policy and the bundled hermetic runtime go through it, so a field the
    declaration gains cannot reach one gate and not the other.
    """
    return AntiPatternRow(
        id=rule.id,
        pattern=rule.pattern.pattern,
        message=rule.message,
        context=rule.context,
        matcher="" if rule.matcher is None else rule.matcher.select.__name__,
        strength=rule.strength,
        # Read off the rule's own family: a gate has to know which of its
        # verdicts turn on a resolution it may not have, and the rule that
        # names what it resolves against is the only thing that can say.
        resolution="required" if rule.family is not None else "",
    )


def antipattern_rows(
    change: EditChange, rules: RuleSet | None = None
) -> list[AntiPatternRow]:
    """Compile rules selected by one edit path into primitive kernel rows."""
    patterns = patterns_for_suffix(change.path.suffix.lower(), rules)
    if patterns is None:
        return []
    return [antipattern_row(rule) for rule in patterns]


def joined(decisions: list[Decision]) -> Decision:
    """One batch's verdict: deny beats ask beats defer beats allow."""
    for effect in ("deny", "ask", "defer"):
        found = next((item for item in decisions if item.effect == effect), None)
        if found is not None:
            return found
    return Decision(effect="allow", reason="every edit in the batch is safe")


class EditPolicy(DecisionPolicy[EditBatch]):
    """Apply the shared marker, anti-pattern, path, deletion, and size gates.

    ``grants`` is asked what the lease holds per change rather than read into
    a list when the policy is built, which is what lets a gate granted while
    this session runs release the very next edit — and what makes one taken
    back stop releasing it. The generated dispatchers read the same document,
    so a lease has one answer wherever it is asked.

    ``peer_policy`` is where this repository's sessions record what each is
    holding, and a batch is judged together with every claim over its paths,
    read live through the store's own fold as the dispatchers read it. Absent,
    no roster is consulted, which is a project that declared none.

    ``rules`` is the anti-pattern set a project holds itself to, which a
    composition compiles the way generation compiles each plugin's table;
    absent, every rule the library ships applies.
    """

    def __init__(
        self,
        protected: list[PathRule],
        maximum_added_lines: int = 3,
        autonomous: bool = False,
        path_roles: list[PathRoleRow] | None = None,
        grants: LeaseGrants | None = None,
        acceptance_guard: AcceptanceGuardRow | None = None,
        edit_rules: list[EditRule] | None = None,
        import_boundaries: list[ImportBoundary] | None = None,
        peer_policy: PeerPolicyRow | None = None,
        rules: RuleSet | None = None,
        refused_paths: list[RefusedPaths] | None = None,
    ) -> None:
        self.acceptance_guard = acceptance_guard
        # The key and login files every command's words are refused, which a
        # file tool writing one names as surely as `cp` would.
        self.refused_paths = [paths.erased() for paths in refused_paths or []]
        self.path_roles = path_roles or []
        self.grants = LeaseGrants() if grants is None else grants
        self.protected = list(protected)
        self.maximum_added_lines = maximum_added_lines
        self.autonomous = autonomous
        self.peer_policy = peer_policy
        self.rules = RuleSet() if rules is None else rules
        # Erased once here rather than per change: the table is a declaration
        # that does not move while this policy answers, and the generated
        # dispatchers read rows that were erased the same way at generation.
        self.edit_rules = erase_edit_rules(edit_rules or [])
        self.import_boundaries = [
            boundary.erased() for boundary in import_boundaries or []
        ]

    def decide(self, event: EditBatch) -> Decision:
        root = event.cwd or Path.cwd()
        return joined(
            [
                pydantic_decision(
                    settled_with_claim(
                        self.decide_change(change, event.cwd).as_kernel(),
                        self.claim(str(root / change.path), root),
                    )
                )
                for change in event.changes
            ]
        )

    def claim(self, path: str, root: Path) -> KernelDecision | None:
        """Whether a live session other than this one is already in *path*.

        Found by the lookups the dispatchers' `foreign_claim_decision` makes --
        the store beneath the shared git directory, and who holds the path in
        it -- and judged by the kernel function both paths call. The asker is
        whoever this process's environment names, which a session hands every
        process it starts.
        """
        peers = self.peer_policy
        directory = None if peers is None else peer_store(root, peers["store"])
        if peers is None or directory is None:
            return None
        return decide_foreign_claim(
            path,
            claim_holders(directory, path, declared_identity(peers["member_env"])),
            peers,
        )

    def decide_change(self, change: EditChange, cwd: Path | None = None) -> Decision:
        root = cwd or Path.cwd()
        # Resolved before any gate reads a role, as the dispatchers resolve it,
        # so an edit through a link meets the gates of the file it lands on.
        path = str((root / change.path).resolve())
        withheld = withheld_edit(path, self.refused_paths)
        if withheld is not None:
            return pydantic_decision(withheld)
        try:
            response = routed_edit_response(
                path,
                change.before,
                change.after,
                Path(path).exists(),
                self.autonomous,
                change.operation,
                root,
                declared_identity(AGENT_IDENTITY_ENV),
            )
            if response is not None:
                return pydantic_decision(read_response(json.loads(response)))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return pydantic_decision(routing_failure(str(error)))
        suffix = change.path.suffix.lower()
        return pydantic_decision(
            decide_edit(
                Path(path).relative_to(root).as_posix()
                if not worktree_root(path) and Path(path).is_relative_to(root)
                else worktree_path(path),
                change.before,
                change.after,
                path_exists=Path(path).exists(),
                path_rules=[path_rule_row(rule) for rule in self.protected],
                antipattern_rows=antipattern_rows(change, self.rules),
                path_roles=self.path_roles,
                maximum_added_lines=self.maximum_added_lines,
                autonomous=self.autonomous,
                allowances=self.grants.granted(),
                python_source=suffix in (".py", ".pyi"),
                acceptance_guard=self.acceptance_guard,
                suffix=suffix,
                operation=change.operation,
                edit_rules=self.edit_rules,
                import_boundaries=self.import_boundaries,
                foreign=foreign_repository(path, root),
                outside_project=outside_this_project(path, root),
                checkout_path=this_checkout_path(path, root),
            )
        )
