"""Assembly for generated dispatchers: kernel source plus policy data rows.

Generated native plugins must decide without lup installed, so the adapters'
hook renderers call this module to read :mod:`lup.policy.kernel` verbatim and
to erase validated application inputs — hook URL scopes, protected roots, the
shell vocabulary, the canonical anti-pattern set — into primitive rows
rendered as one generated data file per plugin. Each row list is declared
against the shipped ``kernel.rows`` shapes, so a generated runtime type-checks
as one unit against the kernel beside it. No decision logic lives here; the
kernel decides.
"""

import ast
import importlib.util
import json
import urllib.parse
from collections.abc import Iterator
from functools import cache
from importlib.machinery import ModuleSpec
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from lup.harness.codescan.antipatterns import RuleSet
from lup.harness.models import SubagentCleanup
from lup.formats.banner import REGENERATE_COMMAND, GeneratedBanner
from lup.policy.grants import ALLOWANCE_GRANTS_ENV, known_allowances
from lup.policy.identity import (
    AGENT_IDENTITY_ENV,
    DASHBOARD_URL_ENV,
    POLICY_ROOT_ENV,
    REVIEW_ANSWERS_ENV,
)
import lup.policy.kernel as kernel
from lup.policy.kernel.imports import import_references
from lup.policy.kernel.typescript import TYPESCRIPT_SUFFIXES
from lup.policy.kernel.effects import EffectRow, effect_row_values
from lup.policy.kernel.semantics import UnjudgedAmbient
from lup.policy.kernel.rows import (
    AcceptanceGuardRow,
    AntiPatternRow,
    EditRuleRow,
    ImportBoundaryRow,
    PathRoleRow,
    PathRuleRow,
    PeerPolicyRow,
    RefusedPathRow,
    RefusedToolRow,
    RunnerTargetRow,
    ShellRuleRow,
    SpawnNameRow,
    VerificationRow,
    UrlScopeRow,
    runner_target_values,
    shell_row_values,
)
from lup.policy.edit_rules import EditRule, erase_edit_rules
from lup.policy.imports import ImportBoundary
from lup.policy.peer_policy import PeerPolicy, erase_peer_policy
from lup.policy.refused_paths import RefusedPaths
from lup.policy.refused_tools import RefusedTool, erase_refused_tools
from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    erase_runner_targets,
    erase_shell_rules,
    runner_target_tables,
)
from lup.policy.rules import (
    antipattern_row,
    human_owned_path_rule,
    invariant_path_rules,
    path_rule_row,
    protected_root_rule,
)
from lup.types import JsonValue


class KernelModule(BaseModel, frozen=True):
    """One hermetic kernel source file copied into a generated runtime."""

    name: str
    source: str

    def origin(self) -> str:
        """The canonical module this copy is byte-identical to.

        Carried so a generated tree can say where each kernel file came from
        even though the copy prints nothing: a banner inside it would break
        the diff that proves the copy faithful, which is a reason not to
        render the provenance rather than not to have it.
        """
        return f"{kernel.__name__}.{Path(self.name).stem}"


def policy_kernel_modules() -> list[KernelModule]:
    """Read the canonical kernel package verbatim for runtime assembly.

    The package's relative imports resolve the same beneath ``runtime/`` as
    they do in lup, so every module ships byte for byte.
    """
    directory = Path(kernel.__file__).parent
    return [
        KernelModule(name=path.name, source=path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.py"))
    ]


@cache
def compilation_sources(
    renderers: tuple[str, ...] = (
        "lup.providers.claude.harness",
        "lup.providers.codex.harness",
    ),
    composers: tuple[str, ...] = ("lup.harness.enforcement",),
    policy: str = "lup.policy",
) -> tuple[PurePosixPath, ...]:
    """Every library source a hook set's compiled policy is made from.

    The policy package is protected as the policy is, and what compiles it
    decides as much: an edit there and a regeneration change the hooks
    judging the session. A hook set is compiled in each runtime's renderer
    and in the composition a session in this process judges by, and the rest
    is read off their imports rather than listed, so a module the compilation
    comes to import is covered the day it does.

    An import is followed only into a module that itself imports from
    ``policy``, since that is the way the compilation reaches the policy:
    following every import takes in the whole library the renderers also
    use for skills, prompts and launches. Each renderer's package ships its
    ``assets/`` into the tree verbatim -- the dispatcher a runtime runs as
    its hook -- so that directory comes too. Paths are relative to the
    library's package directory; the policy package itself is not repeated.
    """
    package = Path(__file__).resolve().parents[1]

    def located(name: str) -> ModuleSpec | None:
        """Where one of the library's modules is defined, if it is one."""
        if name != package.name and not name.startswith(f"{package.name}."):
            return None
        try:
            found = importlib.util.find_spec(name)
        except ModuleNotFoundError:
            # An imported name inside a module rather than a module of its own.
            return None
        return found if found is not None and found.origin else None

    def imported(name: str) -> list[str]:
        """The library modules one module imports from, relative ones resolved."""
        spec = located(name)
        if spec is None or spec.origin is None:
            return []
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
        return list(
            dict.fromkeys(
                reference["module"]
                for reference in import_references(tree, spec.parent or "")
                if located(reference["module"]) is not None
            )
        )

    def reaches_policy(name: str) -> bool:
        return any(
            module == policy or module.startswith(f"{policy}.")
            for module in imported(name)
        )

    reached = [name for name in [*renderers, *composers] if located(name) is not None]
    for name in reached:
        reached.extend(
            module
            for module in imported(name)
            if module not in reached
            and not module.startswith(f"{policy}.")
            and reaches_policy(module)
        )
    origins = [
        Path(spec.origin) for name in reached if (spec := located(name)) and spec.origin
    ]
    assets = [
        Path(spec.origin).parent / "assets"
        for name in renderers
        if (spec := located(name)) and spec.origin
    ]
    return tuple(
        PurePosixPath(path.resolve().relative_to(package).as_posix())
        for path in [
            *origins,
            *(directory for directory in assets if directory.is_dir()),
        ]
    )


def hook_deadline(hook_timeout: int, verdict_reserve: float = 5.0) -> float:
    """How long a verdict may take, given what the runtime gives the hook.

    That timeout less ``verdict_reserve``, the time starting the interpreter
    and writing the verdict take, so everything the verdict waits on has
    ended while the runtime is still listening. Read by each dispatcher, and
    by a reading taken in this process, so `dev policy` is bounded as the
    hook it previews is.
    """
    return hook_timeout - verdict_reserve


def bundled_antipattern_rows(
    rules: RuleSet | None = None,
) -> dict[str, list[AntiPatternRow]]:
    """Compile primitive runtime rows directly from canonical rule objects."""
    declared = rules or RuleSet()
    python_rows = [antipattern_row(rule) for rule in declared.python]
    typescript_rows = [antipattern_row(rule) for rule in declared.typescript]
    return {
        ".py": python_rows,
        ".pyi": python_rows,
        **{suffix: typescript_rows for suffix in TYPESCRIPT_SUFFIXES},
    }


def runtime_url_scope(
    origin: str,
    path_prefix: str,
    reason: str = "",
    include_subdomains: bool = False,
    any_port: bool = False,
) -> UrlScopeRow:
    """Normalize one validated hook scope into a primitive runtime row."""
    parsed = urllib.parse.urlsplit(origin)
    if parsed.hostname is None:
        raise ValueError("validated hook URL scope has no hostname")
    return UrlScopeRow(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=parsed.port,
        path_prefix=path_prefix,
        reason=reason,
        include_subdomains=include_subdomains,
        any_port=any_port,
    )


def runtime_path_rules(
    protected_roots: list[str], human_owned_files: list[str]
) -> list[PathRuleRow]:
    """Compile application roots plus invariant edit guardrails."""
    return [
        *[path_rule_row(protected_root_rule(root)) for root in protected_roots],
        *[path_rule_row(human_owned_path_rule(path)) for path in human_owned_files],
        *[path_rule_row(rule) for rule in invariant_path_rules()],
    ]


def dict_rows_literal(rows: list[list[str]]) -> str:
    """Render already-escaped ``"key": value`` rows in Ruff-stable form."""
    if not rows:
        return "[]"
    blocks = [
        "    {\n" + "".join(f"        {entry},\n" for entry in row) + "    },"
        for row in rows
    ]
    return "[\n" + "\n".join(blocks) + "\n]"


def url_scope_rows_literal(rows: list[UrlScopeRow]) -> str:
    """Render normalized URL scopes as primitive runtime rows."""
    return dict_rows_literal(
        [
            [
                f'"scheme": {json.dumps(row["scheme"])}',
                f'"host": {json.dumps(row["host"])}',
                f'"port": {"None" if row["port"] is None else row["port"]}',
                f'"path_prefix": {json.dumps(row["path_prefix"])}',
                f'"reason": {json.dumps(row["reason"])}',
                f'"include_subdomains": {row["include_subdomains"]}',
                f'"any_port": {row["any_port"]}',
            ]
            for row in rows
        ]
    )


def path_rule_rows_literal(rows: list[PathRuleRow]) -> str:
    """Render protected-path rows as primitive runtime rows."""
    return dict_rows_literal(
        [
            [
                f'"kind": {json.dumps(row["kind"])}',
                f'"value": {json.dumps(row["value"])}',
                f'"reason": {json.dumps(row["reason"])}',
                f'"recovery": {json.dumps(row["recovery"])}',
                f'"allow_autonomous": {row["allow_autonomous"]}',
            ]
            for row in rows
        ]
    )


def python_literal(value: JsonValue) -> str:
    """One primitive as Python source, quoted the way Ruff would quote it.

    ``json.dumps`` alone was the obvious reach and is the wrong language: it
    renders JSON, and what this writes is a Python module. The two agree on
    every string with no quote in it, which is why it worked — until a rule
    message contained a double quote, JSON escaped it, and Ruff wanted the
    single-quoted form instead, failing the format check on a generated file
    nobody had edited and nobody could have fixed.

    So the quote is chosen the way Ruff chooses it: the configured double,
    unless single strictly reduces the escaping. What sits between the quotes
    is JSON's escaping either way, which Ruff leaves alone, so the outermost
    decision is the whole of what this makes.
    """
    if not isinstance(value, str):
        return repr(value)
    rendered = json.dumps(value)
    if value.count("'") >= value.count('"'):
        return rendered
    # Requoting an escaped literal, for which there is no parser: these two
    # substitutions are the escaping itself rather than data being edited.
    inner = rendered[1:-1].replace('\\"', '"')  # lup: ignore[string-replace]
    return "'" + inner.replace("'", "\\'") + "'"  # lup: ignore[string-replace]


def antipattern_rows_literal(rows: dict[str, list[AntiPatternRow]]) -> str:
    """Render suffix-keyed anti-pattern rows in Ruff-stable form."""

    def row_fields(row: AntiPatternRow) -> Iterator[str]:
        """Each field as Python source, refusing a value this cannot render.

        Rendered from the row's own keys rather than a list of them: a field
        added to ``AntiPatternRow`` reaches the hermetic runtime by
        construction, instead of being dropped until someone notices. Reading
        a ``TypedDict`` that way widens every value to ``object``, so what one
        actually holds is narrowed here — and a field that is not a primitive
        fails generation rather than reaching the runtime as its ``repr``.
        """
        for key, value in row.items():
            match value:
                case str() | int() | float() | None:
                    yield f"            {python_literal(key)}: {python_literal(value)},"
                case _:
                    raise TypeError(
                        f"anti-pattern row field {key!r} holds a "
                        f"{type(value).__name__}, which the hermetic runtime "
                        "cannot carry as a primitive"
                    )

    lines = ["{"]
    for suffix, patterns in sorted(rows.items()):
        # An empty list is written closed on one line: opened and closed on
        # two, it is the one shape ruff rewrites, and a generated file that
        # reformats is a drift failure on a file nobody edits. A project that
        # retired the whole anti-pattern family renders every suffix this way.
        if not patterns:
            lines.append(f"    {python_literal(suffix)}: [],")
            continue
        lines.append(f"    {python_literal(suffix)}: [")
        for row in patterns:
            fields = "\n".join(row_fields(row))
            lines.append(f"        {{\n{fields}\n        }},")
        lines.append("    ],")
    lines.append("}")
    return "\n".join(lines)


def path_role_rows_literal(rows: list[PathRoleRow]) -> str:
    """Render declared path roles as primitive runtime rows."""
    return dict_rows_literal(
        [
            [
                f'"root": {json.dumps(row["root"])}',
                f'"role": {json.dumps(row["role"])}',
            ]
            for row in rows
        ]
    )


def acceptance_guard_literal(guard: AcceptanceGuardRow | None) -> str:
    """Render the declared acceptance guard, or the absence of one.

    Spelled here rather than through ``json.dumps`` because the absent case
    is the whole reason this exists: JSON's ``null`` is not a Python name,
    and a data file carrying it fails at import — which in a generated
    dispatcher means every permission decision stops happening at once.
    """
    if guard is None:
        return "None"
    entries = [
        f'"ask_reason": {json.dumps(guard["ask_reason"])}',
        f'"autonomous_reason": {json.dumps(guard["autonomous_reason"])}',
    ]
    return "{\n" + "".join(f"    {entry},\n" for entry in entries) + "}"


def verification_literal(row: VerificationRow) -> str:
    """Render the check spellings the way every other row here is rendered."""
    return (
        "{\n"
        + "".join(f"    {json.dumps(name)}: {json.dumps(row[name])},\n" for name in row)
        + "}"
    )


def verification_row(declared: SubagentCleanup | None) -> VerificationRow:
    """How this project spells its scoped checks, as the shipped notice reads them.

    A project that declined the cleanup declaration still gets spellings,
    because the notice is composed wherever a delegated agent is dispatched
    and the model's own defaults are what an adopter inherits until it says
    otherwise.
    """
    held = declared or SubagentCleanup()
    return VerificationRow(scoped=held.scoped, tests=held.tests)


def spawn_names_literal(row: SpawnNameRow | None) -> str:
    """Render the declared spawn-name requirement, or the absence of one.

    Spelled the way the acceptance guard is, and for the same reason: the
    absent case has to be a Python name, not JSON's ``null``.
    """
    if row is None:
        return "None"
    entries = [
        f'"reason": {json.dumps(row["reason"])}',
        f'"recovery": {json.dumps(row["recovery"])}',
        f'"punctuation": {json.dumps(row["punctuation"])}',
        f'"limit": {json.dumps(row["limit"])}',
    ]
    return "{\n" + "".join(f"    {entry},\n" for entry in entries) + "}"


def peer_policy_literal(redirect: PeerPolicyRow | None) -> str:
    """Render the declared roster the peer calls are judged against, or its absence.

    Spelled here rather than through ``json.dumps`` for the reason the
    acceptance guard is: JSON's ``null`` is not a Python name, and a data file
    carrying one fails at import — which in a generated dispatcher means every
    permission decision stops happening at once.
    """
    if redirect is None:
        return "None"
    entries = [
        f'"store": {json.dumps(redirect["store"])}',
        f'"windows_dir": {json.dumps(redirect["windows_dir"])}',
        f'"member_env": {json.dumps(redirect["member_env"])}',
        f'"send_reason": {json.dumps(redirect["send_reason"])}',
        f'"send_recovery": {json.dumps(redirect["send_recovery"])}',
        f'"listing_note": {json.dumps(redirect["listing_note"])}',
        f'"claim_reason": {json.dumps(redirect["claim_reason"])}',
        f'"claim_recovery": {json.dumps(redirect["claim_recovery"])}',
    ]
    return "{\n" + "".join(f"    {entry},\n" for entry in entries) + "}"


def refused_tool_rows_literal(rows: list[RefusedToolRow]) -> str:
    """Render declared tool refusals as primitive runtime rows."""
    return dict_rows_literal(
        [
            [
                f'"tool": {json.dumps(row["tool"])}',
                f'"specifier": {json.dumps(row["specifier"])}',
                f'"reason": {json.dumps(row["reason"])}',
                f'"recovery": {json.dumps(row["recovery"])}',
            ]
            for row in rows
        ]
    )


def refused_path_rows_literal(rows: list[RefusedPathRow]) -> str:
    """Render declared path refusals as primitive runtime rows."""
    return mapping_rows_literal(
        [
            [
                RenderedField(name="paths", value=row["paths"]),
                RenderedField(name="exempt", value=row["exempt"]),
                RenderedField(name="reason", value=row["reason"]),
                RenderedField(name="recovery", value=row["recovery"]),
            ]
            for row in rows
        ]
    )


def runner_target_rows_literal(rows: list[RunnerTargetRow]) -> str:
    """Render the declared runner targets as primitive runtime rows.

    Walked off :func:`~lup.policy.kernel.rows.runner_target_values` rather
    than spelled here, which is what :func:`mapping_rows_literal` exists for:
    a renderer listing the fields itself is a second enumeration of the row,
    and this row carries a list of mappings, which a renderer written for
    scalars turns into a string.
    """
    return mapping_rows_literal(
        [
            [
                RenderedField(name=name, value=value)
                for name, value in runner_target_values(row).items()
            ]
            for row in rows
        ]
    )


def string_rows_literal(rows: list[str]) -> str:
    """Render a sequence of generated string identities."""
    if not rows:
        return "[]"
    return "[\n" + "".join(f"    {json.dumps(row)},\n" for row in rows) + "]"


def string_matrix_literal(rows: list[list[str]]) -> str:
    """Render a sequence of generated argument vectors."""
    if not rows:
        return "[]"
    return "[\n" + "".join(f"    {json.dumps(row)},\n" for row in rows) + "]"


def literal_element(item: str | EffectRow) -> list[str]:
    """One element of a rendered list, as the lines it occupies.

    A mapping is exploded with a trailing comma rather than inlined, because
    the magic trailing comma is what keeps the shape stable at any length: an
    inline dict is rewrapped the moment a reason grows past the line budget,
    and a generated file that reformats is a drift failure on a file nobody
    edited.

    Every element is rendered, and rendered as the type it is. The shape this
    replaces filtered to strings, which read as a formatting choice and was a
    data loss -- a list of mappings rendered as an empty pair of brackets, so a
    column the rules declared never reached the compiled table at all. Coercing
    each field with ``str`` was the same loss one level further down: it held
    while every axis of a mapping happened to be a string, and rendered the
    first boolean one as ``"False"``, which is a true value in the table the
    dispatcher reads.
    """
    if isinstance(item, dict):
        exploded = ["            {"]
        exploded.extend(
            f"                {json.dumps(name)}: {python_literal(field)},"
            for name, field in effect_row_values(item).items()
        )
        exploded.append("            },")
        return exploded
    return [f"            {python_literal(item)},"]


class RenderedField(BaseModel, frozen=True):
    """One field of one compiled row, on its way to the generated data file.

    The pair a row's ``*_values`` mapping yields, named. Carried rather than
    the mapping itself so two differently-keyed shapes can share one renderer:
    a ``dict`` is invariant in its key, and both of those key theirs on a
    closed ``Literal`` — which is what makes a renamed axis a type error
    instead of a column missing from the table a hook reads.
    """

    name: str
    value: str | bool | int | list[str] | list[EffectRow]


def mapping_rows_literal(rows: list[list[RenderedField]]) -> str:
    """Render rows of named fields as Ruff-stable dict literals.

    A list value is broken across lines and a scalar is not, which is what
    Ruff's own formatter does with these lengths — so the generated file is
    already formatted when it is written and the format check has nothing to
    report on a file nobody edited.
    """
    if not rows:
        return "[]"
    lines = ["["]
    for row in rows:
        lines.append("    {")
        for field in row:
            key = json.dumps(field.name)
            if isinstance(field.value, list):
                if field.value:
                    lines.append(f"        {key}: [")
                    for item in field.value:
                        lines.extend(literal_element(item))
                    lines.append("        ],")
                else:
                    lines.append(f"        {key}: [],")
            else:
                lines.append(f"        {key}: {python_literal(field.value)},")
        lines.append("    },")
    lines.append("]")
    return "\n".join(lines)


def shell_rule_rows_literal(rows: list[ShellRuleRow]) -> str:
    """Render erased shell rules as Ruff-stable dict literals."""
    return mapping_rows_literal(
        [
            [
                RenderedField(name=name, value=value)
                for name, value in shell_row_values(row).items()
            ]
            for row in rows
        ]
    )


def edit_rule_rows_literal(rows: list[EditRuleRow]) -> str:
    """Render erased edit rules as Ruff-stable dict literals, in declared order.

    Order is the semantics for this table — the last matching rule decides —
    so the rows are rendered exactly as they arrive and never sorted into a
    shape that reads more tidily.
    """
    if not rows:
        return "[]"
    lines = ["["]
    for row in rows:
        lines.append("    {")
        lines.append(f'        "name": {json.dumps(row["name"])},')
        for name, values in (
            ("gates", row["gates"]),
            ("suffixes", row["suffixes"]),
            ("roles", row["roles"]),
            ("operations", row["operations"]),
        ):
            if values:
                lines.append(f'        "{name}": [')
                lines.extend(f"            {json.dumps(value)}," for value in values)
                lines.append("        ],")
            else:
                lines.append(f'        "{name}": [],')
        lines.append(f'        "effect": {json.dumps(row["effect"])},')
        maximum = row["maximum_added_lines"]
        lines.append(
            '        "maximum_added_lines": '
            + ("None" if maximum is None else json.dumps(maximum))
            + ","
        )
        lines.append(f'        "reason": {json.dumps(row["reason"])},')
        lines.append("    },")
    lines.append("]")
    return "\n".join(lines)


POLICY_DATA_BANNER = GeneratedBanner(source=__name__, command=REGENERATE_COMMAND)
"""Provenance every adapter's rendered policy-data module opens with."""


def import_boundary_rows_literal(rows: list[ImportBoundaryRow]) -> str:
    """Render the dependency ownership read by both native edit gates."""
    if not rows:
        return "[]"
    lines = ["["]
    for row in rows:
        lines.append("    {")
        for name, values in (
            ("modules", row["modules"]),
            ("owners", row["owners"]),
            ("source_roots", row["source_roots"]),
        ):
            if values:
                lines.append(f'        "{name}": [')
                lines.extend(f"            {json.dumps(value)}," for value in values)
                lines.append("        ],")
            else:
                lines.append(f'        "{name}": [],')
        lines.append(f'        "rule_id": {json.dumps(row["rule_id"])},')
        lines.append(f'        "message": {json.dumps(row["message"])},')
        lines.append("    },")
    lines.append("]")
    return "\n".join(lines)


def render_policy_data(
    *,
    allowed_fetch_scopes: list[UrlScopeRow],
    denied_fetch_scopes: list[UrlScopeRow],
    protected_roots: list[str],
    human_owned_files: list[str],
    autonomous_agent_identities: list[str],
    path_roles: list[PathRoleRow],
    acceptance_guard: AcceptanceGuardRow | None,
    spawn_names: SpawnNameRow | None,
    verification: VerificationRow | None = None,
    shell_rules: list[ShellCommandRule],
    edit_rules: list[EditRule],
    refused_tools: list[RefusedTool],
    peer_policy: PeerPolicy | None,
    recoverable_target_limit: int,
    runner_targets: list[RunnerTargetRule],
    sandbox_excluded_commands: list[str],
    auto_escape_prefixes: list[list[str]],
    diagnostics_command: list[str],
    resolution_command: list[str],
    repair_command: list[str],
    rules: RuleSet | None = None,
    import_boundaries: list[ImportBoundary] | None = None,
    unscoped_fetch: UnjudgedAmbient | None = None,
    refused_paths: list[RefusedPaths] | None = None,
    secret_variables: list[str] | None = None,
    hook_timeout: int = 30,
) -> str:
    """Render one plugin's canonical policy rows without executable logic.

    ``rules`` is the table compiled for the runtime this plugin belongs to, so
    a rule whose message names a native tool ships each tree the words that
    tree can act on. Omitting it renders the runtime-neutral table.

    ``unscoped_fetch`` ships as declared, ``None`` included: an unset
    declaration is answered at runtime by the posture the launch measured,
    which no compiled constant could know.

    ``hook_timeout`` is what the runtime gives the policy hook, the same value
    its hooks file declares, and the hook's deadline is derived from it rather
    than restated beside it, by :func:`hook_deadline`.
    """
    body = "\n\n".join(
        [
            "ALLOWED_FETCH_SCOPES: list[UrlScopeRow] = "
            + url_scope_rows_literal(allowed_fetch_scopes),
            "DENIED_FETCH_SCOPES: list[UrlScopeRow] = "
            + url_scope_rows_literal(denied_fetch_scopes),
            "UNSCOPED_FETCH: UnjudgedAmbient | None = "
            + ("None" if unscoped_fetch is None else json.dumps(unscoped_fetch)),
            "PATH_RULES: list[PathRuleRow] = "
            + path_rule_rows_literal(
                runtime_path_rules(protected_roots, human_owned_files)
            ),
            "ANTI_PATTERN_ROWS: dict[str, list[AntiPatternRow]] = "
            + antipattern_rows_literal(bundled_antipattern_rows(rules)),
            "PATH_ROLES: list[PathRoleRow] = " + path_role_rows_literal(path_roles),
            "ACCEPTANCE_GUARD: AcceptanceGuardRow | None = "
            + acceptance_guard_literal(acceptance_guard),
            "SPAWN_NAMES: SpawnNameRow | None = " + spawn_names_literal(spawn_names),
            "VERIFICATION: VerificationRow = "
            + verification_literal(verification or verification_row(None)),
            "SHELL_RULES: list[ShellRuleRow] = "
            + shell_rule_rows_literal(erase_shell_rules(shell_rules)),
            "EDIT_RULES: list[EditRuleRow] = "
            + edit_rule_rows_literal(erase_edit_rules(edit_rules)),
            "IMPORT_BOUNDARIES: list[ImportBoundaryRow] = "
            + import_boundary_rows_literal(
                [boundary.erased() for boundary in import_boundaries or []]
            ),
            "REFUSED_TOOLS: list[RefusedToolRow] = "
            + refused_tool_rows_literal(erase_refused_tools(refused_tools)),
            "REFUSED_PATHS: list[RefusedPathRow] = "
            + refused_path_rows_literal(
                [paths.erased() for paths in refused_paths or []]
            ),
            "SECRET_VARIABLES: list[str] = "
            + string_rows_literal(secret_variables or []),
            "PEER_POLICY: PeerPolicyRow | None = "
            + peer_policy_literal(erase_peer_policy(peer_policy)),
            "AUTONOMOUS_AGENT_IDENTITIES: list[str] = "
            + string_rows_literal(autonomous_agent_identities),
            "AGENT_IDENTITY_ENV = " + json.dumps(AGENT_IDENTITY_ENV),
            "POLICY_ROOT_ENV = " + json.dumps(POLICY_ROOT_ENV),
            "DASHBOARD_URL_ENV = " + json.dumps(DASHBOARD_URL_ENV),
            "REVIEW_ANSWERS_ENV = " + json.dumps(REVIEW_ANSWERS_ENV),
            "ALLOWANCE_GRANTS_ENV = " + json.dumps(ALLOWANCE_GRANTS_ENV),
            "KNOWN_ALLOWANCES: list[str] = " + string_rows_literal(known_allowances()),
            "MAXIMUM_ADDED_LINES = 3",
            "RECOVERABLE_TARGET_LIMIT = " + json.dumps(recoverable_target_limit),
            "RUNNER_TARGETS: list[RunnerTargetRow] = "
            + runner_target_rows_literal(erase_runner_targets(runner_targets)),
            "RUNNER_TARGET_TABLES: list[ShellRuleRow] = "
            + shell_rule_rows_literal(runner_target_tables(runner_targets)),
            "SANDBOX_EXCLUDED_COMMANDS: list[str] = "
            + string_rows_literal(sandbox_excluded_commands),
            "AUTO_ESCAPE_PREFIXES: list[list[str]] = "
            + string_matrix_literal(auto_escape_prefixes),
            "DIAGNOSTICS_COMMAND: list[str] = "
            + string_rows_literal(diagnostics_command),
            "RESOLUTION_COMMAND: list[str] = "
            + string_rows_literal(resolution_command),
            "REPAIR_COMMAND: list[str] = " + string_rows_literal(repair_command),
            "HOOK_DEADLINE_SECONDS = " + json.dumps(hook_deadline(hook_timeout)),
        ]
    )
    return (
        '"""Generated application-owned policy data."""\n\n'
        "from kernel.rows import (\n"
        "    AcceptanceGuardRow,\n"
        "    AntiPatternRow,\n"
        "    EditRuleRow,\n"
        "    ImportBoundaryRow,\n"
        "    PathRoleRow,\n"
        "    PathRuleRow,\n"
        "    PeerPolicyRow,\n"
        "    RefusedPathRow,\n"
        "    RefusedToolRow,\n"
        "    RunnerTargetRow,\n"
        "    ShellRuleRow,\n"
        "    SpawnNameRow,\n"
        "    UrlScopeRow,\n"
        "    VerificationRow,\n"
        ")\n"
        "from kernel.semantics import UnjudgedAmbient"
        "\n\n\n" + body + "\n"
    )
