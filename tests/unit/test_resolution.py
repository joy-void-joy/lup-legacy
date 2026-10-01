"""Resolution decides a rule's own sites by what its subjects are declared as.

`lup.harness.codescan.resolution` measures the sites a rule's matcher already chose
against the family that rule declares, so these exercise the three answers an
oracle can give — in the family, outside it, and nothing shown — plus the
hook's unchanged line rule and the dead directives the refinement exposes.

The oracle here is a table. What a real one has to do to fill that table in —
which protocol request, read out of which file — is
`tests/integration/test_codeintel.py`, against the language server itself,
because a fake that answers whatever the engine hoped for cannot tell anyone
whether pyright answers that way.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.harness.codescan.antipatterns import (
    MAPPING_FAMILY,
    PYTHON_ANTI_PATTERNS,
    TEXT_FAMILY,
    audit_text,
)
from lup.harness.codescan.common import AntiPattern, PythonSource, RuleExample
from lup.harness.codescan.oracle import (
    ClassDeclaration,
    Declaration,
    FunctionDeclaration,
    SourceBuffer,
    SymbolQuery,
    TypeOracle,
    UnknownDeclaration,
)
from lup.harness.codescan.resolution import refute, resolved_sites
from lup.policy.bundle import bundled_antipattern_rows
from lup.policy.kernel.edit import antipattern_decision
from lup.policy.kernel.rows import AntiPatternRow

MAPPING = ClassDeclaration(
    name="dict", bases=["MutableMapping"], path=Path("builtins.pyi"), line=1282
)
"""What a `.get` on a mapping resolves to: the class typeshed declares it on."""

CLIENT = ClassDeclaration(
    name="Client", bases=["BaseClient"], path=Path("httpx/_client.py"), line=594
)
"""The same spelling on an HTTP client, which is not this rule's subject."""

ROW = ClassDeclaration(name="Row", bases=["TypedDict"], path=Path("sample.py"), line=3)
"""A `TypedDict`, reached through the receiver because its `get` is synthesized."""

TEXT = ClassDeclaration(
    name="str", bases=["Sequence"], path=Path("builtins.pyi"), line=487
)
"""What a `.replace` on a string resolves to."""

FRAME = ClassDeclaration(
    name="DataFrame", bases=["NDFrame"], path=Path("frame.py"), line=12
)
"""A two-argument `replace` on something that is not text.

Arity is what the hook tells the file rename by, and it says nothing here:
this spells the rule's shape exactly and substitutes no text at all.
"""

MODULE_FUNCTION = FunctionDeclaration(name="get", path=Path("httpx/_api.py"), line=174)
"""What a module-qualified `httpx.get` resolves to: a `def` inside no class."""

NOTHING = UnknownDeclaration(reason="the checker inferred no type")
"""What a subject nobody can show anything about resolves to."""


class TableOracle(TypeOracle):
    """Answers declaration queries from a table keyed by the queried line."""

    def __init__(self, answers: dict[int, Declaration]) -> None:
        self.answers = answers
        self.asked: list[SymbolQuery] = []
        self.held: list[SourceBuffer] = []

    def declarations(
        self,
        queries: list[SymbolQuery],
        buffers: list[SourceBuffer] | None = None,
    ) -> list[Declaration]:
        self.asked.extend(queries)
        self.held.extend(buffers or [])
        return [
            self.answers[query.member.line]
            if query.member.line in self.answers
            else NOTHING
            for query in queries
        ]


def source(text: str, name: str = "sample.py") -> PythonSource:
    return PythonSource(path=Path(name), module="sample", text=text)


def test_mapping_receiver_keeps_its_finding() -> None:
    """A `.get` declared on `dict` is the schema-hiding access the rule means."""
    text = "value = payload.get('name')\n"
    assert refute([source(text)], TableOracle({1: MAPPING}), PYTHON_ANTI_PATTERNS) == {}
    findings = audit_text(text, PYTHON_ANTI_PATTERNS)
    assert [finding.rule_id for finding in findings] == ["dict-get"]


def test_non_mapping_receiver_is_refuted_with_evidence() -> None:
    """A `.get` declared on an HTTP client is not what the rule is about."""
    text = "response = client.get('https://example.com')\n"
    refutations = refute([source(text)], TableOracle({1: CLIENT}), PYTHON_ANTI_PATTERNS)
    refuted = refutations["sample.py"]
    assert [row.rule_id for row in refuted] == ["dict-get"]
    assert refuted[0].line == 1
    assert refuted[0].subject == "client"
    assert "`Client`" in refuted[0].evidence
    assert "outside the mapping family" in refuted[0].evidence
    assert refuted[0].settled
    assert audit_text(text, PYTHON_ANTI_PATTERNS, refuted) == []


def test_a_typed_dict_receiver_is_refuted_as_the_modelling_it_is() -> None:
    """`.get` on a `TypedDict` is how an optional key is read out of one.

    The rule asks for exactly this modelling, so a site that reached it has
    already done what the denial wanted. It is refuted on its own class
    rather than on a member the checker could not find: a `TypedDict`'s `get`
    is synthesized and declared nowhere, so refuted on the member the finding
    would be dropped for a reason that has nothing to do with the rule.
    """
    text = "name = row.get('name')\n"
    refuted = refute([source(text)], TableOracle({1: ROW}), PYTHON_ANTI_PATTERNS)
    assert "`Row`" in refuted["sample.py"][0].evidence


def test_a_subject_shown_to_descend_from_the_family_keeps_its_finding() -> None:
    """Membership follows the whole chain, not the one link below the class.

    A project's `Middle(Base(dict))` is a mapping, and reading only what its
    own declaration lists as bases called it a stranger — which refuted the
    exact access the rule exists for.
    """
    text = "value = holder.get('name')\n"
    deep = ClassDeclaration(
        name="Middle",
        bases=["Base", "dict", "MutableMapping"],
        path=Path("m.py"),
        line=5,
    )
    assert refute([source(text)], TableOracle({1: deep}), PYTHON_ANTI_PATTERNS) == {}


def test_text_receiver_keeps_its_replace_finding() -> None:
    """A `.replace` declared on `str` is the string surgery the rule means."""
    text = "name = source.replace('.py', '.pyi')\n"
    assert refute([source(text)], TableOracle({1: TEXT}), PYTHON_ANTI_PATTERNS) == {}
    findings = audit_text(text, PYTHON_ANTI_PATTERNS)
    assert [finding.rule_id for finding in findings] == ["string-replace"]


def test_a_two_argument_replace_on_a_non_text_receiver_is_refuted() -> None:
    """What arity cannot reach, the receiver's own declaration does.

    A bound `Path.replace` takes only the destination, so the hook tells that
    rename from string surgery without types. A dataframe filling missing
    values takes two arguments and spells this rule's shape exactly — and
    substitutes no text, which only the declaration says.
    """
    text = "frame = frame.replace(missing, default)\n"
    refuted = refute([source(text)], TableOracle({1: FRAME}), PYTHON_ANTI_PATTERNS)[
        "sample.py"
    ]
    assert [row.rule_id for row in refuted] == ["string-replace"]
    assert refuted[0].subject == "frame"
    assert "`DataFrame`" in refuted[0].evidence
    assert "outside the text family" in refuted[0].evidence
    assert audit_text(text, PYTHON_ANTI_PATTERNS, refuted) == []


def test_a_module_qualified_call_never_becomes_a_question() -> None:
    """`httpx.get` is settled by the tree, so no checker is asked about it.

    The rule's own matcher rules a module receiver out, and resolution reads
    that matcher — so the site the gate never flags is not one a language
    server spends a session on, and cannot come back as a refutation of a
    finding nobody made.
    """
    text = "import httpx\nresponse = httpx.get('https://example.com')\n"
    assert resolved_sites([source(text)], PYTHON_ANTI_PATTERNS) == []
    assert refute([source(text)], TableOracle({}), PYTHON_ANTI_PATTERNS) == {}


def test_a_route_decorator_never_becomes_a_question() -> None:
    """`@app.get("/runs")` names a route, which the tree settles without types."""
    text = '@app.get("/runs")\ndef runs() -> list[Run]: ...\n'
    assert resolved_sites([source(text)], PYTHON_ANTI_PATTERNS) == []


def test_a_declaration_that_is_no_class_puts_nothing_in_the_family() -> None:
    """A function is not a class, so it cannot be what a family is joined by."""
    text = "value = payload.get('name')\n"
    refuted = refute(
        [source(text)], TableOracle({1: MODULE_FUNCTION}), PYTHON_ANTI_PATTERNS
    )
    assert [row.rule_id for row in refuted["sample.py"]] == ["dict-get"]
    assert "the module-level `get`" in refuted["sample.py"][0].evidence


def test_an_unresolved_receiver_is_refuted_rather_than_denied() -> None:
    """Nothing shown means nothing established, which is not a mapping.

    Denying here would read no declaration as "not refuted", and "not
    refuted" as "confirmed mapping" -- refusing an unannotated parameter, a
    `json.loads` result, and an object out of a package with no stubs, with a
    typed directive the only way past each.
    """
    text = "value = whatever.get('name')\n"
    refuted = refute([source(text)], TableOracle({}), PYTHON_ANTI_PATTERNS)
    assert [row.evidence for row in refuted["sample.py"]] == [
        "the checker inferred no type for `whatever`, so nothing puts it in "
        "the mapping family"
    ]
    assert not refuted["sample.py"][0].settled


def test_an_unresolved_receiver_leaves_its_directive_standing() -> None:
    """Refuted without being settled: no directive demanded, none called dead.

    The loop this prevents, in one file: a hook whose checker types nothing
    for the receiver reports the marker spurious and deletes it; the sweep,
    resolving the receiver into the family, reports the line missing the
    marker; a session restoring it meets the hook again. What a checker
    failed to learn is no evidence against the marker, so the unsettled
    refutation drops the demand and leaves the directive exactly as written.
    """
    marked = 'value = whatever.get("name")  # lup: ignore[dict-get] — open map\n'
    bare = 'value = whatever.get("name")\n'
    refuted = refute([source(marked)], TableOracle({}), PYTHON_ANTI_PATTERNS)[
        "sample.py"
    ]

    assert audit_text(marked, PYTHON_ANTI_PATTERNS, refuted) == []
    assert audit_text(bare, PYTHON_ANTI_PATTERNS, refuted) == []


def test_an_unresolved_receiver_keeps_a_file_level_opt_out_live() -> None:
    """The whole-file directive stands on an unsettled hit as an inline one does."""
    text = (
        '# lup: ignore[dict-get] — open maps throughout\nvalue = whatever.get("name")\n'
    )
    refuted = refute([source(text)], TableOracle({}), PYTHON_ANTI_PATTERNS)["sample.py"]

    assert audit_text(text, PYTHON_ANTI_PATTERNS, refuted) == []


class ColumnOracle(TypeOracle):
    """Answers by the member's column, for a line carrying several sites."""

    def __init__(self, answers: dict[int, Declaration]) -> None:
        self.answers = answers

    def declarations(
        self,
        queries: list[SymbolQuery],
        buffers: list[SourceBuffer] | None = None,
    ) -> list[Declaration]:
        return [
            self.answers[query.member.column]
            if query.member.column in self.answers
            else NOTHING
            for query in queries
        ]


def test_one_unresolved_site_keeps_the_whole_line_open() -> None:
    """A line is settled only when every site on it is.

    `client` resolves outside the family and `whatever` resolves to nothing:
    the directive guarding the line may be guarding exactly the second site,
    so the line comes back unsettled and the marker stands.
    """
    text = 'value = client.get("url") or whatever.get("name")\n'
    client_member = text.index(".get(") + 1
    refuted = refute(
        [source(text)], ColumnOracle({client_member: CLIENT}), PYTHON_ANTI_PATTERNS
    )["sample.py"]

    assert [row.settled for row in refuted] == [False, False]
    marked = text.rstrip("\n") + "  # lup: ignore[dict-get] — open map\n"
    assert audit_text(marked, PYTHON_ANTI_PATTERNS, refuted) == []


def test_the_oracle_is_told_the_text_being_audited() -> None:
    """What is resolved has to be what is audited, or the answer is about
    another file that happens to share the path.

    The gap only opens where a caller holds text disk does not — an edit
    judged before it is written — which is the caller whose verdict most
    depends on the resolution being about its own content.
    """
    text = "response = client.get('https://example.com')\n"
    oracle = TableOracle({1: CLIENT})

    refute([source(text)], oracle, PYTHON_ANTI_PATTERNS)

    assert [(held.path.as_posix(), held.text) for held in oracle.held] == [
        ("sample.py", text)
    ]


def test_absent_oracle_degrades_to_the_broad_rule() -> None:
    """With no checker installed nothing is refuted at all."""
    text = "response = client.get('https://example.com')\n"
    assert refute([source(text)], None, PYTHON_ANTI_PATTERNS) == {}
    findings = audit_text(text, PYTHON_ANTI_PATTERNS)
    assert [finding.rule_id for finding in findings] == ["dict-get"]


def test_one_mapping_among_clients_keeps_the_line() -> None:
    """A line refutes only when every site on it does."""
    text = "value = client.get(payload.get('url'))\n"
    oracle = TableOracle({1: MAPPING})
    assert refute([source(text)], oracle, PYTHON_ANTI_PATTERNS) == {}


def test_refuted_line_reports_its_directive_as_spurious() -> None:
    """The reflex suppressions become dead directives on evidence."""
    text = 'response = client.get("url")  # lup: ignore[dict-get]\n'
    refuted = refute([source(text)], TableOracle({1: CLIENT}), PYTHON_ANTI_PATTERNS)[
        "sample.py"
    ]
    findings = audit_text(text, PYTHON_ANTI_PATTERNS, refuted)
    assert [finding.kind for finding in findings] == ["spurious"]
    assert findings[0].rule_id == "dict-get"


def test_unparseable_source_refutes_nothing() -> None:
    """Text no tree can be had from keeps the regex pass's verdicts."""
    text = "response = client.get(\n"
    assert refute([source(text)], TableOracle({}), PYTHON_ANTI_PATTERNS) == {}


def test_the_engine_carries_a_family_it_was_not_written_for() -> None:
    """Nothing in the engine names a family: it reads whichever one it is given.

    The same `.replace` sites, measured against the mapping family instead of
    the text one, refute the string receiver the text family keeps.
    """
    declared = [
        rule.model_copy(update={"family": MAPPING_FAMILY})
        for rule in PYTHON_ANTI_PATTERNS
        if rule.id == "string-replace"
    ]
    text = "name = source.replace('.py', '.pyi')\n"
    refuted = refute([source(text)], TableOracle({1: TEXT}), declared)
    assert [row.rule_id for row in refuted["sample.py"]] == ["string-replace"]
    assert "outside the mapping family" in refuted["sample.py"][0].evidence


def test_a_family_on_a_line_only_matcher_does_not_import() -> None:
    """A family that could never be asked about is refused at the declaration.

    Attached to a rule whose selector records no symbol, it is inert: nothing
    resolves, nothing is refuted, and only the reference page still claims a
    narrowing happens.
    """
    line_only = next(rule for rule in PYTHON_ANTI_PATTERNS if rule.id == "import-re")
    with pytest.raises(ValidationError, match="records no symbol to resolve"):
        AntiPattern(
            id=line_only.id,
            pattern=line_only.pattern,
            examples=line_only.examples,
            message=line_only.message,
            matcher=line_only.matcher,
            family=TEXT_FAMILY,
        )


def test_a_rule_with_no_family_is_never_asked_about() -> None:
    """Most rules turn on no type at all, and cost no checker session."""
    families = {rule.id for rule in PYTHON_ANTI_PATTERNS if rule.family is not None}
    assert families == {"dict-get", "string-replace"}
    assert resolved_sites([source("import re\n")], PYTHON_ANTI_PATTERNS) == []


def test_the_query_names_both_the_member_and_the_receiver() -> None:
    """The member settles most sites; the receiver answers the ones it cannot."""
    oracle = TableOracle({})
    refute([source("value = payload.get('name')\n")], oracle, PYTHON_ANTI_PATTERNS)
    asked = oracle.asked[0]
    assert (asked.member.line, asked.member.column) == (1, 16)
    assert asked.receiver is not None
    assert (asked.receiver.line, asked.receiver.column) == (1, 14)


def test_a_receiver_ending_in_no_name_carries_no_fallback() -> None:
    """A call result has no position denoting it, so only its member is asked."""
    oracle = TableOracle({})
    refute([source("value = make().get('name')\n")], oracle, PYTHON_ANTI_PATTERNS)
    assert oracle.asked[0].receiver is None


def test_a_family_is_named_where_the_rule_is_declared() -> None:
    """One object holds the shape, the family, and the prose that explains it."""
    declared = {rule.id: rule for rule in PYTHON_ANTI_PATTERNS}
    assert declared["dict-get"].family is MAPPING_FAMILY
    assert declared["string-replace"].family is TEXT_FAMILY
    assert "mapping family" in declared["dict-get"].refinement


def test_the_hook_row_declares_that_its_verdict_needs_a_declaration() -> None:
    """The regex is unchanged; what the gate does when it fires is not.

    A rule resolution sharpens cannot be decided from an edit alone, and the
    row says so rather than leaving the gate to state a verdict the audit
    contradicts. Read off the rule's own family, so a rule that gains or
    loses one cannot disagree with the row describing it.
    """
    rows = [row for row in bundled_antipattern_rows()[".py"] if row["id"] == "dict-get"]
    assert rows == [
        AntiPatternRow(
            id="dict-get",
            pattern=r"\.get\s*\(",
            message=rows[0]["message"],
            context="code",
            matcher="dict_get_sites",
            strength="soft",
            resolution="required",
        )
    ]


def test_an_unresolved_rule_declares_no_resolution() -> None:
    """Only the rules a family sharpens carry the ask-instead-of-deny path.

    Every other rule decides from the text in front of it, so nothing about
    them is pending a checker, and widening the field to all of them would
    turn each into an approval question nobody can answer better than the
    gate already did.
    """
    rows = bundled_antipattern_rows()[".py"]
    resolved = {rule.id for rule in PYTHON_ANTI_PATTERNS if rule.family is not None}
    assert {row["id"] for row in rows if row["resolution"] == "required"} == resolved


def test_the_gate_asks_where_nothing_resolved_the_receiver() -> None:
    """Told nothing, the gate says so instead of stating the audit's opposite.

    This is the deadlock's own shape: the kernel denied `client.get("url")`
    and demanded a directive, the audit resolved `Client`, refuted the
    finding, and reported that directive spurious. No version of the file
    passed both.
    """
    rows = bundled_antipattern_rows()[".py"]

    decision = antipattern_decision(
        None, 'response = client.get("url")\n', rows, python_source=True
    )

    assert decision is not None and decision.effect == "ask"
    assert "could not resolve" in decision.recovery


def test_a_resolved_receiver_is_admitted_without_a_directive() -> None:
    """The answer the audit gives, reaching the gate.

    Refuted here means the same edit needs no marker at all — which is what
    the rule's own text tells an author to write, and what a gate with no
    way to know could not admit.
    """
    rows = bundled_antipattern_rows()[".py"]

    decision = antipattern_decision(
        None,
        'response = client.get("url")\n',
        rows,
        python_source=True,
        resolution={"refuted": {"dict-get": [1]}, "unresolved": {}},
    )

    assert decision is None or decision.effect == "allow"


def test_the_gate_admits_a_line_the_checker_could_type_nothing_for() -> None:
    """Looked at and unresolved is admitted, not asked about and not denied.

    The audit demands no directive on such a line, so a denial here would
    be the split the gate exists to prevent; and the ask is for a checker
    that never answered, which this one did.
    """
    rows = bundled_antipattern_rows()[".py"]

    decision = antipattern_decision(
        None,
        'value = whatever.get("name")\n',
        rows,
        python_source=True,
        resolution={"refuted": {}, "unresolved": {"dict-get": [1]}},
    )

    assert decision is None or decision.effect == "allow"


def test_a_directive_on_an_unresolved_line_is_not_refused_as_dead() -> None:
    """The marker the sweep may demand cannot be the marker the gate refuses.

    On a refuted line the directive is dead and refused; on an unresolved
    line it stands, and is asked about like any other declared suppression.
    """
    rows = bundled_antipattern_rows()[".py"]
    marked = 'value = whatever.get("name")  # lup: ignore[dict-get] — open map\n'

    standing = antipattern_decision(
        None,
        marked,
        rows,
        python_source=True,
        resolution={"refuted": {}, "unresolved": {"dict-get": [1]}},
    )
    dead = antipattern_decision(
        None,
        marked,
        rows,
        python_source=True,
        resolution={"refuted": {"dict-get": [1]}, "unresolved": {}},
    )

    assert standing is not None and standing.effect == "ask"
    assert "names dict-get" not in standing.reason
    assert dead is not None and dead.effect == "deny"
    assert "names dict-get" in dead.reason


def test_an_unresolved_verdict_still_denies_a_rule_that_needs_no_checker() -> None:
    """The ask reaches exactly the rules whose verdict was pending, and no others.

    A rule decided from the text in front of it gains nothing from a checker,
    so an absent one must not soften it — otherwise every gate in the table
    degrades together the moment a language server is missing.
    """
    rows = bundled_antipattern_rows()[".py"]

    decision = antipattern_decision(None, "import re\n", rows, python_source=True)

    assert decision is not None and decision.effect == "deny"


def test_a_rule_declaring_a_family_is_the_one_asked_about() -> None:
    """An example the declaration marks refuted is one the engine can reach."""
    refutable = [
        rule
        for rule in PYTHON_ANTI_PATTERNS
        if any(example.verdict == "refuted" for example in rule.examples)
    ]
    assert refutable
    for rule in refutable:
        assert rule.family is not None, rule.id
        assert rule.refinement, rule.id


def test_an_example_the_declaration_calls_refuted_is_one_the_family_refutes() -> None:
    """Each refuted example is a site the engine reaches with a real verdict."""
    for rule in PYTHON_ANTI_PATTERNS:
        for example in rule.examples:
            if example.verdict != "refuted":
                continue
            sites = resolved_sites([source(f"{example.code}\n")], [rule])
            assert sites, (rule.id, example.code)


def test_the_declared_families_are_the_ones_a_rule_can_be_written_against() -> None:
    """A family says what belongs, and nothing about what is spelled how."""
    assert "dict" in MAPPING_FAMILY.classes
    assert "TypedDict" not in MAPPING_FAMILY.classes
    assert MAPPING_FAMILY.name == "mapping"
    assert TEXT_FAMILY.classes == ["str", "bytes", "bytearray", "UserString"]


def test_a_rule_example_still_bounds_every_declaration() -> None:
    """Adding a family does not let a rule ship without both polarities."""
    for rule in PYTHON_ANTI_PATTERNS:
        verdicts = {example.verdict for example in rule.examples}
        assert {"flagged", "cleared"} <= verdicts, rule.id


def test_a_family_can_be_declared_on_a_rule_that_had_none() -> None:
    """Nothing about the engine names which rules resolve."""
    rule = AntiPattern(
        id="sample",
        pattern=PYTHON_ANTI_PATTERNS[0].pattern,
        examples=[
            RuleExample(code="value = payload.get('x')", verdict="flagged"),
            RuleExample(code="value = payload.x", verdict="cleared"),
        ],
        message="sample",
        matcher=next(
            rule.matcher
            for rule in PYTHON_ANTI_PATTERNS
            if rule.id == "dict-get" and rule.matcher is not None
        ),
        family=MAPPING_FAMILY,
    )
    text = "value = payload.get('name')\n"
    refuted = refute([source(text)], TableOracle({1: CLIENT}), [rule])
    assert [row.rule_id for row in refuted["sample.py"]] == ["sample"]
