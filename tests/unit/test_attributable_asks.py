"""Every question this policy asks can say which rule asked it.

The measurement this exists to make impossible: on the trace corpus before
rule ids, 860 asks carried no recorded reason at all, and native tool
aggregation could only say `Bash` or `Edit` about any of them — so the first
question of any tuning session, "which gate is producing these", had no
answer that did not involve reading the classifier by hand.

Attribution is checked over the *whole declared vocabulary* rather than over a
sample, because a rule that forgets its id is exactly the rule nobody wrote a
case for.
"""

from lup.policy.kernel.edit import decide_edit
from lup.policy.kernel.rows import PathRoleRow, PathRuleRow
from lup.policy.kernel.shell import classify_shell
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.survey import shell_forms
from lup.policy.vocabulary import default_vocabulary
from lup_template.harness.catalog import declared_hook_set


def test_every_shell_verdict_names_the_rule_that_reached_it() -> None:
    """A verdict a reader cannot attribute is one nobody can tune.

    Walked over every form the offered table declares, so a rule added without
    an id fails here rather than in the one session that later needs to know
    which gate produced its interruption.
    """
    rules = default_vocabulary()
    rows = erase_shell_rules(rules)

    unattributed = [
        form
        for form in shell_forms(rules)
        if classify_shell(form, rows).effect in ("allow", "ask")
        and not classify_shell(form, rows).rule
    ]

    assert unattributed == []


def test_every_shell_verdict_names_the_evaluator_that_produced_it() -> None:
    """Which classifier looked, as against which rule it applied.

    Two rules may share an evaluator and one rule never spans two, so an audit
    that counts by rule and one that counts by evaluator are asking different
    questions — and both need an answer that is not the native tool name.
    """
    rules = default_vocabulary()
    rows = erase_shell_rules(rules)
    evaluators = {
        classify_shell(form, rows).evaluator
        for form in shell_forms(rules)
        if classify_shell(form, rows).effect in ("allow", "ask")
    }

    # Two, and the second is the point: `gh api` is parsed rather than matched
    # against a row, so it has no row to take an identity from and names
    # itself instead. A surface that answered with the table's evaluator would
    # be claiming a row decided what a parser decided.
    assert evaluators == {"shell-vocabulary", "gh-api-screen"}
    assert "" not in evaluators


def test_this_project_s_own_table_is_attributable_too() -> None:
    """The library's offered table and the project's composition are two tables.

    A project that replaces a group inherits the ids of what it replaced it
    with, not of what it replaced — so the composed table is walked as well.
    """
    rules = declared_hook_set().resolved_shell_rules()
    rows = erase_shell_rules(rules)

    unattributed = [
        form
        for form in shell_forms(rules)
        if classify_shell(form, rows).effect == "ask"
        and not classify_shell(form, rows).rule
    ]

    assert unattributed == []


def test_every_edit_question_names_its_gate_and_its_purpose() -> None:
    """Native `Edit` and `Write` say nothing about which gate answered.

    Attribution here was the whole of the measurement problem: an edit
    interruption could be size, a full write, a suppression, a protected path,
    or the acceptance guard, and the event carried the same tool name for all
    five.
    """
    production = [PathRoleRow(root="src", role="production")]
    protected = [
        PathRuleRow(
            kind="exact",
            value="docs/owned.md",
            reason="human-owned",
            recovery=[],
            allow_autonomous=False,
            description="docs/owned.md",
        )
    ]

    full_write = decide_edit(
        "src/app/service.py",
        None,
        "value = 1\n",
        path_exists=False,
        path_rules=[],
        antipattern_rows=[],
        path_roles=production,
        operation="create",
    )
    owned = decide_edit(
        "docs/owned.md",
        "a\n",
        "b\n",
        path_exists=True,
        path_rules=protected,
        antipattern_rows=[],
    )

    for decided in (full_write, owned):
        assert decided.effect == "ask"
        assert decided.rule.startswith("edit:")
        assert decided.evaluator == "edit-gate"
        assert decided.purpose is not None
    assert full_write.rule != owned.rule


def test_a_verdict_says_what_kind_of_question_it_is_without_reading_prose() -> None:
    """A reason is prose, and a taxonomy built on prose counts phrasings.

    The purpose is what lets a queue be triaged and an audit counted by kind
    — and it never replaces the rule id, because two rules sharing a purpose
    are still two rules.
    """
    rows = erase_shell_rules(default_vocabulary())

    release = classify_shell("gh release create v1", rows)
    deletion = classify_shell("rm -rf build", rows)

    assert release.purpose == "external_consequence"
    assert deletion.purpose == "unrecovered_local_mutation"
