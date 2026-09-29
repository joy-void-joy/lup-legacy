"""Boundary scan behavior: what breaches, what is sanctioned, what escapes.

The scan (:mod:`lup.harness.codescan.boundaries`) guards two directions. Inward, it is
the regression guard that keeps per-engine adapter imports from creeping
outside ``lup.providers``, and the live tree is pinned at zero breaches.
Outward, the placement rule judges whether a library data table reaches its
adopters as an overridable default; the live tree still carries known
violations, so those tests work from fixtures rather than pinning a count.
The shell vocabulary is the exception: it has moved out of the library, and
two tests hold it there against the live tree.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

import lup.devtools.dev.boundaries as boundary_scan
from lup.devtools.project import DevProject
from lup.harness.codescan.common import ApplicationRoots, RuleSelection
from lup.policy.imports import ImportBoundary
from lup.policy.models import EditBatch, EditChange
from lup.policy.rules import EditPolicy

from lup.harness.codescan.boundaries import (
    RuleId,
    audit_boundaries,
    audit_constant_declarations,
    audit_kernel_imports,
    audit_library_defaults,
    declared_defaults,
    audit_path_boundaries,
    constant_declarations,
    default_position_names,
    find_boundary_breaches,
    find_library_default_breaches,
    find_native_spelling_breaches,
    generated_tree_paths,
    library_placement_path_is_audited,
    native_spelling_path_is_sanctioned,
    path_is_sanctioned,
)

from lup_template.harness.catalog import (
    NATIVE_RUNTIMES,
    application_roots,
    dev_project,
)
from lup.harness.codescan.common import PythonSource, module_name
from lup.harness.codescan.project import RuleFinding
from lup.policy.kernel.roles import path_role
from lup.devtools.dev.boundaries import (
    TrackedSource,
    library_sources,
    overridable_names,
    scan_boundaries,
    scan_library_placement,
    tracked_python_sources,
)

BREACHING = "from lup.providers.claude.runtime import ClaudeSessionFactory\n"


def test_per_engine_imports_breach() -> None:
    text = (
        "import lup.providers.codex.runtime\n"
        "from lup.providers.codex.native import CodexEventDecoder\n"
        "from lup.providers.claude.theme import ClaudeTheme\n"
        "from lup.providers.claude.harness import ClaudeSkillRenderer\n"
    )
    breaches = find_boundary_breaches(text)
    assert [breach.line for breach in breaches] == [1, 2, 3, 4]
    assert breaches[0].module == "lup.providers.codex.runtime"


def test_seam_surface_does_not_breach() -> None:
    text = (
        "from lup.sessions.capabilities import Client\n"
        "from lup.sessions.query import query\n"
        "from lup.harness.contracts import ArtifactRenderer\n"
        "from lup.policy.contracts import NativeEventDecoder\n"
        "from lup.providers.harness import compile_codex\n"
    )
    assert not find_boundary_breaches(text)


@pytest.mark.parametrize(
    "source",
    [
        "from lup.providers import codex\n",
        "from lup.providers import claude as provider\n",
        "from lup import providers\nfrom lup.providers import codex\n",
        "import claude_agent_sdk as sdk\n",
        "from anthropic import AsyncAnthropic\n",
        "from openai import AsyncOpenAI\n",
    ],
)
def test_native_dependencies_cannot_bypass_the_adapter_boundary(source: str) -> None:
    assert find_boundary_breaches(source)


def test_canonical_vocabulary_and_provider_mentions_remain_valid() -> None:
    source = (
        '"""Compare Claude and Codex through Lup contracts."""\n'
        'tools = ["Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch"]\n'
        'runtime = "codex"\n'
        'description = "Claude and Codex support different native facilities"\n'
    )
    assert audit_boundaries(source) == []


def test_a_tool_group_is_named_rather_than_rebuilt() -> None:
    """The wiring two downstream repositories hand-ported, refused at the import.

    A project reaching for the constructor is a project holding its own copy
    of what a group is made of — which is how a companion goes unstarted and a
    signature that gained parameters lands half-way.
    """
    reaching = "from lup.coordination.peer_tools import create_peer_tools\n"

    findings = audit_path_boundaries(
        Path("src/lup_template/agent/toolsets.py"), reaching, application_roots()
    )

    assert [(item.rule_id, item.module) for item in findings] == [
        (RuleId.ASSEMBLY, "lup.coordination.peer_tools")
    ]


def test_the_assembly_is_what_may_call_a_group_constructor() -> None:
    """One owner, because one module turns a declaration into a session's tools."""
    reaching = "from lup.ledger.tools import create_ledger_tools\n"

    assert (
        audit_path_boundaries(
            Path("packages/lup/src/lup/tools/toolsets.py"),
            reaching,
            application_roots(),
        )
        == []
    )


def test_application_composition_uses_adapters_instead_of_sdks() -> None:
    path = Path("src/lup_template/agent/core.py")
    roots = application_roots()
    assert audit_path_boundaries(path, BREACHING, roots) == []
    findings = audit_path_boundaries(path, "import claude_agent_sdk\n", roots)
    assert [(item.rule_id, item.module) for item in findings] == [
        ("seam-boundary", "claude_agent_sdk")
    ]


@pytest.mark.parametrize("retired", [False, True])
def test_custom_import_ownership_and_retirement_reach_the_repository_scan(
    monkeypatch: pytest.MonkeyPatch, retired: bool
) -> None:
    source = TrackedSource(
        path=Path("src/worker.py"), rel="src/worker.py", text="import vendor.native\n"
    )
    boundary = ImportBoundary(
        modules=["vendor.native"],
        owners=["src/bootstrap.py"],
        rule_id="seam-boundary",
        message="Use the application contract",
    )
    project = DevProject(
        package="adopter",
        import_boundaries=[boundary],
        rules=RuleSelection(retired=["seam-boundary"] if retired else []),
    )
    monkeypatch.setattr(
        boundary_scan, "tracked_python_sources", lambda _project: [source]
    )
    findings = boundary_scan.scan_boundaries(project)
    assert bool(findings) is not retired
    policy = EditPolicy(
        protected=[], import_boundaries=project.resolved_import_boundaries()
    )
    decision = policy.decide(
        EditBatch(
            changes=[
                EditChange(
                    path=source.path,
                    before="",
                    after=source.text,
                )
            ]
        )
    )
    assert decision.effect == ("allow" if retired else "deny")


@pytest.mark.parametrize(
    "modules,owners",
    [
        ([], []),
        ([""], []),
        (["openai as sdk"], []),
        (["openai; import os"], []),
        (["openai"], ["../outside/"]),
        (["openai"], ["/absolute/"]),
    ],
)
def test_malformed_import_ownership_cannot_silently_disable_a_guard(
    modules: list[str], owners: list[str]
) -> None:
    with pytest.raises(ValidationError):
        ImportBoundary(
            modules=modules,
            owners=owners,
            rule_id="seam-boundary",
            message="Use an adapter",
        )


def test_inline_ignore_excepts_the_line() -> None:
    ignored = BREACHING.rstrip() + "  # lup: ignore[seam-boundary]\n"
    assert not find_boundary_breaches(ignored)

    bare = BREACHING.rstrip() + "  # lup: ignore\n"
    assert not find_boundary_breaches(bare)

    other_rule = BREACHING.rstrip() + "  # lup: ignore[cast]\n"
    assert len(find_boundary_breaches(other_rule)) == 1


def test_file_level_ignore_excepts_the_file() -> None:
    typed = "# lup: ignore[seam-boundary]\n" + BREACHING
    assert not find_boundary_breaches(typed)

    bare = "# lup: ignore\n" + BREACHING
    assert not find_boundary_breaches(bare)

    other_rule = "# lup: ignore[cast]\n" + BREACHING
    assert len(find_boundary_breaches(other_rule)) == 1


def test_boundary_suppressions_are_typed_and_audited() -> None:
    bare = BREACHING.rstrip() + "  # lup: ignore\n"
    assert [(item.kind, item.rule_id) for item in audit_boundaries(bare)] == [
        ("untyped", "seam-boundary")
    ]

    spurious = "value = 1  # lup: ignore[seam-boundary]\n"
    assert [(item.kind, item.rule_id) for item in audit_boundaries(spurious)] == [
        ("spurious", "seam-boundary")
    ]


def test_native_wire_spellings_breach_but_semantic_names_do_not() -> None:
    text = (
        'method = "thread" + "/start"\n'
        'config_home = "CODEX_HOME"\n'
        'semantic = "Bash Edit Fetch Search"\n'
    )

    breaches = find_native_spelling_breaches(text)

    assert [(item.line, item.module) for item in breaches] == [
        (1, "thread/start"),
        (2, "CODEX_HOME"),
    ]


def test_native_spelling_suppression_is_audited() -> None:
    text = 'method = "turn/start"  # lup: ignore[native-spelling]\n'
    assert find_native_spelling_breaches(text) == []
    assert audit_boundaries(text) == []

    spurious = 'method = "portable"  # lup: ignore[native-spelling]\n'
    findings = audit_boundaries(spurious)
    assert [(item.kind, item.rule_id) for item in findings] == [
        ("spurious", "native-spelling")
    ]


def test_sanctioned_paths() -> None:
    roots = application_roots()
    assert path_is_sanctioned(Path("packages/lup/src/lup/providers/claude/runtime.py"))
    assert path_is_sanctioned(Path("tests/unit/test_adapter_transforms.py"), roots)
    assert path_is_sanctioned(Path("src/lup_template/agent/core.py"), roots)
    assert not path_is_sanctioned(
        Path("packages/lup/src/lup/orchestration/subagents.py"), roots
    )


def test_an_application_that_says_nothing_sanctions_nothing_of_its_own() -> None:
    """The library guards its own package and can name no adopter's."""
    assert not path_is_sanctioned(Path("src/lup_template/agent/core.py"))
    assert path_is_sanctioned(Path("packages/lup/src/lup/providers/codex/harness.py"))


def test_a_generated_tree_is_sanctioned_by_the_runtime_that_spells_it() -> None:
    """Asked of the runtimes, so a location they learn sanctions its own tree."""
    roots = application_roots()
    for spelled in generated_tree_paths(NATIVE_RUNTIMES, ["lup"]):
        assert path_is_sanctioned(
            Path(spelled) / "any.py", roots
        ) or path_is_sanctioned(Path(spelled), roots)


def test_portable_content_is_scanned_for_native_spellings() -> None:
    path = Path("src/lup_template/harness/content/skills/example.py")
    text = (
        "from lup.providers.claude.runtime import ClaudeSessionFactory\n"
        'method = "turn/start"\n'
    )
    roots = application_roots()

    assert path_is_sanctioned(path, roots)
    assert not native_spelling_path_is_sanctioned(path, roots)
    findings = audit_path_boundaries(path, text, roots)
    assert [(item.rule_id, item.line) for item in findings] == [("native-spelling", 2)]


def test_policy_kernel_imports_are_pinned_to_hermetic_stdlib() -> None:
    clean = "import ast\nimport urllib.parse\n"
    breach = "import ast\nfrom pydantic import BaseModel\n"

    assert audit_kernel_imports(clean) == []
    findings = audit_kernel_imports(breach)
    assert [(item.kind, item.module) for item in findings] == [("missing", "pydantic")]


def test_live_tree_has_zero_breaches() -> None:
    assert scan_boundaries(dev_project()) == []


TABLE = 'READ_ONLY_COMMANDS = ("ls", "cat", "grep")\n'
NOTHING_OVERRIDABLE = ()
"""A library where no caller can replace anything."""

SAMPLE = "lup.sample"
"""The library module these tables are judged as written in."""


def test_a_library_table_no_caller_can_replace_breaches() -> None:
    breaches = find_library_default_breaches(TABLE, NOTHING_OVERRIDABLE, SAMPLE)

    assert [(item.line, item.module) for item in breaches] == [
        (1, "READ_ONLY_COMMANDS")
    ]


def test_only_declared_multi_entry_tables_are_judged() -> None:
    text = (
        'PREAMBLE = "one long prompt contract"\n'
        'SINGLETON = ("only",)\n'
        "DERIVED = [name.upper() for name in OTHER]\n"
        "lowercase = (1, 2)\n"
    )

    assert find_library_default_breaches(text, NOTHING_OVERRIDABLE, SAMPLE) == []


def test_every_admitted_default_spelling_clears_a_table() -> None:
    reached = {
        "SIGNATURE": "def build(rules: list[str] = SIGNATURE) -> None: ...\n",
        "FIELD": "class Set(BaseModel):\n    rules: list[str] = Field(default=FIELD)\n",
        "FACTORY": (
            "class Set(BaseModel):\n"
            "    rules: list[str] = Field(default_factory=lambda: FACTORY)\n"
        ),
        "SENTINEL": "def build(rules=None):\n    return SENTINEL if rules is None else rules\n",
        "FALLBACK": "def build(rules=None):\n    return rules or FALLBACK\n",
    }

    for name, consumer in reached.items():
        declaration = f'{name} = ("a", "b")\n'
        overridable = declared_defaults(
            PythonSource(path=Path("lup/sample.py"), module=SAMPLE, text=consumer)
        )

        assert name in default_position_names(consumer), name
        assert f"{SAMPLE}.{name}" in overridable, name
        assert find_library_default_breaches(declaration, overridable, SAMPLE) == [], (
            name
        )


def test_a_fallback_after_what_a_caller_supplies_reaches_through_its_attribute() -> (
    None
):
    consumer = "class Set:\n    def build(self):\n        return self.rules or FIELD\n"

    assert "FIELD" in default_position_names(consumer)


def test_a_fallback_after_what_no_caller_supplies_replaces_nothing() -> None:
    """`something or TABLE` is a default only where a caller hands `something` in.

    Any operand at all used to count, so a local, a module global or a call
    beside a constant exempted it from both rules that ask for a default.
    """
    unreached = {
        "LOCAL": "def build():\n    rules = load()\n    return rules or LOCAL\n",
        "GLOBAL": "CHOSEN = configured or GLOBAL\n",
        "CALL": "def build(rules=None):\n    return load(rules) or CALL\n",
        "SENTINEL": (
            "def build():\n    rules = load()\n"
            "    return SENTINEL if rules is None else rules\n"
        ),
    }

    for name, consumer in unreached.items():
        assert name not in default_position_names(consumer), name


# A directive inside the first ten lines governs the whole file, so proximity
# is only observable below that window.
BELOW_FILE_WINDOW = "\n" * 10


def test_a_directive_heading_a_multi_line_table_suppresses_it() -> None:
    heading = BELOW_FILE_WINDOW + "# lup: ignore[library-default] — canonical\n" + TABLE
    assert find_library_default_breaches(heading, NOTHING_OVERRIDABLE, SAMPLE) == []

    spread = (
        BELOW_FILE_WINDOW + "# lup: ignore[library-default] — canonical\n"
        "READ_ONLY_COMMANDS = (\n"
        '    "ls",\n'
        '    "cat",\n'
        ")\n"
    )
    assert find_library_default_breaches(spread, NOTHING_OVERRIDABLE, SAMPLE) == []
    assert audit_library_defaults(spread, NOTHING_OVERRIDABLE, SAMPLE) == []


def test_a_directive_two_lines_above_a_table_stays_spurious() -> None:
    detached = (
        BELOW_FILE_WINDOW + "# lup: ignore[library-default] — canonical\n\n" + TABLE
    )
    findings = audit_library_defaults(detached, NOTHING_OVERRIDABLE, SAMPLE)

    assert sorted(item.kind for item in findings) == ["missing", "spurious"]


SHELL_VOCABULARY = Path("src/lup_template/harness/content/shell_vocabulary.py")
"""Where this project's shell command tables live, outside the library."""

MOVED_TABLES: list[str] = ["INTEGRATION_BRANCHES"]
"""Only this repository's branch model is left, and that is the point.

This once named six tables, then one. The five word tables reached the
library as parameter defaults on the groups in ``lup.policy.vocabulary``, so
an adopter replaces a vocabulary by calling a group differently instead of
editing lup. The last entry was ``SHELL_RULES`` itself — the composition,
which was a table only because a project had no way to state a difference
without restating everything around it. A ``Selection`` over
``default_vocabulary()`` is that way, so what remains in the file is two
declarations that genuinely have no library form: a rule no other project
has, and ``git`` carrying this repository's two arguments.

One of those arguments is a table, named because the workflow runs on the
same branches: ``INTEGRATION_BRANCHES``, ``main`` and ``dev``. Returned to
the library it would freeze one repository's branches for every adopter,
which the library instead offers as ``git_rule``'s own default, so the rule
naming it is the rule working. It is judged by where it is declared: the
library's default of the same name excuses only the library's constant.

The list is the assertion: the rule still runs against the whole file, and
anything a future composition parks here comes back as a breach beside it.
"""


def test_the_shell_rule_models_declare_no_vocabulary_of_their_own() -> None:
    """The library module keeps the models and the erasure, and no table."""
    breaches = [
        breach
        for breach in scan_library_placement()
        if breach.file == "packages/lup/src/lup/policy/shell_rules.py"
    ]

    assert breaches == []


def test_the_rule_names_every_table_if_the_vocabulary_returns_to_the_library() -> None:
    """Judge the remaining source against the real library, as if it moved back.

    This once named six tables, and the answer was that the vocabulary could
    not move. Five of them since did, as parameter defaults an adopter passes
    over. The composition is what the rule still stops, correctly: it is the
    one thing in the file that a second project with the same intent would
    write differently.
    """
    breaches = find_library_default_breaches(
        SHELL_VOCABULARY.read_text(encoding="utf-8"),
        overridable_names(library_sources()),
        "lup.policy.shell_vocabulary",
    )

    assert [breach.module for breach in breaches] == MOVED_TABLES


APPLICATION = Path("src/lup_template/agent/tools/search.py")
"""A path outside the library, where only the constant rule ever judges."""


def constant_findings(text: str, path: Path = APPLICATION) -> list[RuleFinding]:
    """Judge one module's constants the way the whole-project sweep does."""
    return audit_constant_declarations(
        [PythonSource(path=path, module=path.stem, text=text)]
    )


def test_a_judgement_constant_outside_the_library_is_reported() -> None:
    """The defect the rule exists for: a ceiling with no parameter to replace it."""
    findings = constant_findings(
        "SNIPPET_LENGTH = 500\n\n\ndef show(text: str) -> str:\n"
        "    return text[:SNIPPET_LENGTH]\n"
    )

    assert [item.kind for item in findings] == ["missing"]
    assert [item.line for item in findings] == [1]
    assert "overridable default" in findings[0].message
    assert f"# lup: ignore[{RuleId.CONSTANT_DECLARATION}]" in findings[0].message


def test_a_canonical_constant_is_cleared_by_a_reasoned_suppression() -> None:
    reasoned = (
        BELOW_FILE_WINDOW
        + "# lup: ignore[constant-declaration] — the header the vendor requires\n"
        'ANTHROPIC_BETA = "oauth-2025-04-20"\n'
    )

    assert constant_findings(reasoned) == []


def test_a_reason_may_head_its_declaration_but_never_the_neighbour_above() -> None:
    """A reason worth reading rarely fits on one line; a run of constants shares none."""
    block = (
        BELOW_FILE_WINDOW + "# lup: ignore[constant-declaration] — the two-line\n"
        "# reason a real exception needs\n"
        "FIRST = 500\n"
    )
    assert constant_findings(block) == []

    run = block + "SECOND = 900\n"
    assert [item.line for item in constant_findings(run)] == [14]


def test_a_constant_a_caller_can_replace_is_not_reported() -> None:
    """Both spellings of the remedy clear the rule: a parameter, and a field."""
    parametrized = (
        "TRAILING_DAYS = 7\n\n\ndef days(trailing: int = TRAILING_DAYS) -> int:\n"
        "    return trailing\n"
    )
    field = (
        "SUPERVISED_WAIT = 3600.0\n\n\nclass Spawn(BaseModel):\n"
        "    wait: float = SUPERVISED_WAIT\n"
    )

    assert constant_findings(parametrized) == []
    assert constant_findings(field) == []


def test_a_vocabulary_cannot_escape_by_naming_its_own_container() -> None:
    """A bare constructor over literals writes down the same choice a display does."""
    wrapped = (
        'VERBS = dict.fromkeys(["push", "reset"])\n'
        'UNSAFE = set("$*?")\n'
        'READ = resources.files("lup").joinpath("x").read_text("utf-8")\n'
    )

    assert [item.line for item in constant_findings(wrapped)] == [1, 2]


def test_a_value_derived_from_another_name_is_judged_where_it_is_decided() -> None:
    """The choice a derived constant embodies was made by what it derives from."""
    derived = 'ROOT = "packages/lup/"\nKERNEL = f"{ROOT}kernel/"\n'

    assert [item.line for item in constant_findings(derived)] == [1]


def test_a_constant_that_carves_text_is_steered_to_the_parser() -> None:
    """The second defect: the constant exists because nothing parsed the value."""
    findings = constant_findings(
        'UTC_SUFFIX = "Z"\n\n\ndef stamp(raw: str) -> str:\n'
        "    return raw.removesuffix(UTC_SUFFIX)\n"
    )

    assert [item.line for item in findings] == [1]
    assert "parse the value instead" in findings[0].message
    assert "overridable default" not in findings[0].message


def test_a_generated_artifact_is_never_judged_for_a_choice_made_elsewhere() -> None:
    """Its values are compiled, so no fix could survive the next generation."""
    compiled = Path(".claude/plugins/lup/hooks/runtime/policy_data.py")
    source = PythonSource(
        path=compiled,
        module="policy_data",
        text='ALLOWED_HOSTS = ["docs.claude.com", "code.claude.com"]\n',
    )

    assert audit_constant_declarations([source]) != []
    assert (
        audit_constant_declarations([source], ApplicationRoots(generated=[".claude/"]))
        == []
    )


PARTITIONED = (
    'TABLE = ("ls", "cat")\nSCALAR = "one"\nSINGLETON = (1,)\nDERIVED = OTHER\n'
)
"""A vocabulary, a scalar, a one-entry display, and a value naming a name."""


def test_the_two_constant_rules_partition_every_declaration() -> None:
    """cdr-2 in code: one total function hands each declaration to one rule.

    A line both rules could reach would be reported twice and excusable by
    either directive, which is the state this asserts cannot arise. Only the
    library's own vocabulary is ``library-default``'s; every other declaration,
    and every declaration outside the library, is the constant rule's.
    """
    declared = constant_declarations(PARTITIONED)

    assert [constant.name for constant in declared] == ["TABLE", "SCALAR", "SINGLETON"]
    assert [constant.judging_rule(library_module=True) for constant in declared] == [
        RuleId.LIBRARY_DEFAULT,
        RuleId.CONSTANT_DECLARATION,
        RuleId.CONSTANT_DECLARATION,
    ]
    assert [constant.judging_rule(library_module=False) for constant in declared] == [
        RuleId.CONSTANT_DECLARATION
    ] * len(declared)


def test_the_live_tree_leaves_no_constant_unresolved() -> None:
    """cdr-4: every constant the rule trips is parametrized, or reasoned away.

    Production files only, as the sweep that gates them reads: a test declares
    its fixtures and nothing calls them, so the rule has nothing to say there.
    """
    project = dev_project()
    roles = project.path_roles
    findings = audit_constant_declarations(
        [
            PythonSource(
                path=source.path,
                module=module_name(source.path),
                text=source.text,
            )
            for source in tracked_python_sources()
            if path_role(source.rel, roles) == "production"
        ],
        application_roots(),
    )

    assert [f"{item.path}:{item.line} {item.kind}" for item in findings] == []


def test_adapter_packages_are_exempt_from_the_placement_rule() -> None:
    assert library_placement_path_is_audited(
        Path("packages/lup/src/lup/policy/shell_rules.py")
    )
    assert not library_placement_path_is_audited(
        Path("packages/lup/src/lup/providers/codex/harness.py")
    )
    assert not library_placement_path_is_audited(
        Path("src/lup_template/devtools/harness/catalog.py")
    )
