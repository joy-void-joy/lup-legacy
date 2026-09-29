"""The table that turns what an operation does into what it earns.

Pinned here rather than through a command, because the whole point of the
model is that the verdict stops depending on the spelling. A test that reached
this through `echo x > f` would be testing the lexer's route to the row; what
needs holding is the row.
"""

from lup.policy.kernel.decision import DecisionEffect, SandboxPlacement
from lup.policy.kernel.effects import (
    EFFECT_MEMBERS,
    STRENGTH,
    Effect,
    EffectEvidence,
    EffectRow,
    deciding,
    declare,
    member_for,
    purpose_for,
    purpose_of,
    verdict_for,
)

import pytest


def effect(
    kind: str, scope: str = "", write: str = "", reviewed: bool = False
) -> EffectRow:
    """One declared effect, with the axes a row leaves unconstrained empty."""
    return EffectRow(
        kind=kind,
        scope=scope,
        write=write,
        reviewed=reviewed,
        reason="probe",
        reach="host_later",
    )


def test_every_member_answers_for_a_distinct_kind() -> None:
    """A kind two members claim is a verdict decided by list order."""
    kinds = [member.kind for member in EFFECT_MEMBERS]
    assert len(kinds) == len(set(kinds))
    assert "" not in kinds


def test_an_undeclared_kind_raises_rather_than_allowing() -> None:
    """The permissive reading of "nobody answered" is the failure to avoid."""
    with pytest.raises(ValueError, match="no effect member answers"):
        member_for("writes_pathh")


def test_the_strongest_effect_decides_a_command_carrying_several() -> None:
    """A harmless half never weakens the half that asks."""
    rows = [effect("reads_path", "secret"), effect("writes_path", "scratch")]
    assert verdict_for(rows, EffectEvidence(contained=True), "inside") == "ask"


def test_declaring_no_effect_allows() -> None:
    """A rule that positively says this does nothing worth guarding."""
    assert verdict_for([], EffectEvidence()) == "allow"


class TestReadsPath:
    """Free inside the checkout, a question outside it, never free for a secret."""

    def test_reading_the_project_allows_at_any_placement(self) -> None:
        rows = [effect("reads_path", "project")]
        assert verdict_for(rows, EffectEvidence(), "inside") == "allow"
        assert verdict_for(rows, EffectEvidence(), "outside") == "allow"

    def test_reading_outside_the_checkout_allows_only_under_containment(self) -> None:
        """The /etc/shadow case: confined it costs nothing, loose it is a wander."""
        rows = [effect("reads_path", "outside")]
        assert verdict_for(rows, EffectEvidence(contained=True), "inside") == "allow"
        assert verdict_for(rows, EffectEvidence(), "outside") == "ask"

    def test_reading_a_credential_asks_even_inside(self) -> None:
        """Containment does not help: the read into context is the disclosure."""
        rows = [effect("reads_path", "secret")]
        assert verdict_for(rows, EffectEvidence(contained=True), "inside") == "ask"


class TestWritesPath:
    """The row every spelling of a write arrives at."""

    def test_redirecting_output_to_a_new_file_allows(self) -> None:
        """`python train.py > log.txt` — the commonest shell write there is."""
        rows = [effect("writes_path", "production", "overwrite")]
        assert verdict_for(rows, EffectEvidence(existing=False), "inside") == "allow"

    def test_appending_to_an_untracked_log_allows(self) -> None:
        """`cmd >> run.log` — the log exists, but nothing reviews it."""
        rows = [effect("writes_path", "production", "append")]
        evidence = EffectEvidence(existing=True, tracked=False)
        assert verdict_for(rows, evidence, "inside") == "allow"

    def test_writing_scratch_allows_whatever_is_there(self) -> None:
        rows = [effect("writes_path", "scratch", "overwrite")]
        evidence = EffectEvidence(existing=True, tracked=True)
        assert verdict_for(rows, evidence, "inside") == "allow"

    def test_replacing_tracked_source_is_refused_rather_than_asked(self) -> None:
        """Refused so it costs nobody a question and reaches the gates instead."""
        rows = [effect("writes_path", "production", "overwrite")]
        evidence = EffectEvidence(existing=True, tracked=True)
        assert verdict_for(rows, evidence, "inside") == "deny"

    def test_appending_to_tracked_source_is_refused_too(self) -> None:
        """Appending changes what a reviewer would have seen, so it is an edit."""
        rows = [effect("writes_path", "production", "append")]
        evidence = EffectEvidence(existing=True, tracked=True)
        assert verdict_for(rows, evidence, "inside") == "deny"

    def test_a_protected_path_asks_rather_than_refusing(self) -> None:
        """Here the point is review rather than recovery, so a person decides."""
        rows = [effect("writes_path", "protected", "overwrite")]
        evidence = EffectEvidence(existing=True, tracked=True)
        assert verdict_for(rows, evidence, "inside") == "ask"

    def test_writing_outside_the_checkout_asks_whatever_the_placement(self) -> None:
        """Whose the path is decides it, which a placement cannot say.

        A container holds a directory the host never lent and none it did, so
        the row asks and the reach it states is settled where the host measured
        every named path as the container's own.
        """
        rows = [declare("writes_path", scope="outside", write="overwrite")]
        placements: list[SandboxPlacement] = ["inside", "ambient", "outside"]
        assert {verdict_for(rows, EffectEvidence(), where) for where in placements} == {
            "ask"
        }
        assert rows[0]["reach"] == "mount"

    def test_a_reviewed_write_is_never_refused_by_this_row(self) -> None:
        """The refusal is about bypassing the gates, so a route to them is not it.

        The regression this holds is total: were the review axis dropped, an
        `Edit` to any tracked source file would be denied, which is every edit
        anybody makes here.
        """
        rows = [effect("writes_path", "production", "overwrite", reviewed=True)]
        evidence = EffectEvidence(existing=True, tracked=True)
        assert verdict_for(rows, evidence, "inside") == "allow"

    def test_a_reviewed_write_leaves_the_gates_free_to_decide(self) -> None:
        """Contributing allow is what lets the size budget still defer a long edit.

        The join takes the strongest, so an edit gate reaching `defer` for a
        change block over budget outranks this row rather than being outranked
        by it -- which is the behaviour an editing session actually depends on.
        """
        rows = [effect("writes_path", "production", "overwrite", reviewed=True)]
        evidence = EffectEvidence(existing=True, tracked=True)
        answered = deciding(rows, evidence, "inside")
        assert answered is not None
        assert STRENGTH.index(answered.effect) < STRENGTH.index("defer")


class TestExternalEffects:
    """Offering and landing work allow; trusting a stranger asks loudest."""

    def test_publishing_and_integrating_allow(self) -> None:
        """A push offers and a merge lands: the two ends of one workflow."""
        assert verdict_for([effect("publishes")], EffectEvidence()) == "allow"
        assert verdict_for([effect("integrates")], EffectEvidence()) == "allow"

    def test_a_declared_host_allows_and_an_undeclared_one_asks(self) -> None:
        """Asks rather than refuses: a wall only teaches a way around it."""
        assert verdict_for([effect("fetches", "declared")], EffectEvidence()) == "allow"
        assert verdict_for([effect("fetches", "undeclared")], EffectEvidence()) == "ask"

    def test_reaching_another_host_asks_at_every_placement(self) -> None:
        """The sandbox confines this process, not the machine at the far end."""
        assert (
            verdict_for([effect("reaches_host")], EffectEvidence(), "inside") == "ask"
        )

    def test_taking_a_dependency_asks_by_either_route(self) -> None:
        """The supply chain is what this guards, and the install is the moment."""
        assert verdict_for([effect("installs_dependency")], EffectEvidence()) == "ask"
        assert verdict_for([effect("materializes_lockfile")], EffectEvidence()) == "ask"


class TestExecution:
    """The declared surface, and what makes leaving it visible."""

    def test_a_declared_target_allows(self) -> None:
        """Everyday work, already reviewed as source."""
        rows = [effect("runs_declared_target")]
        assert verdict_for(rows, EffectEvidence(), "inside") == "allow"

    def test_an_undeclared_program_asks(self) -> None:
        """Not because it is dangerous, but because reaching for it is the tell."""
        rows = [effect("runs_undeclared_program")]
        assert verdict_for(rows, EffectEvidence(), "inside") == "ask"

    def test_destroying_uncaptured_work_asks_until_a_capture_holds_it(self) -> None:
        """Ignored content is outside the snapshot by design, so nothing undoes it."""
        rows = [effect("destroys_uncaptured")]
        assert verdict_for(rows, EffectEvidence(captured=False)) == "ask"
        assert verdict_for(rows, EffectEvidence(captured=True)) == "allow"


class TestPurpose:
    """Which kind of decision a question is, read off the row that decided it."""

    def test_the_purpose_comes_from_the_effect_that_decided(self) -> None:
        """Not from the set: the log's purpose must not ride the secret's verdict."""
        rows = [effect("reads_path", "secret"), effect("writes_path", "scratch")]
        evidence = EffectEvidence(contained=True)
        assert purpose_for(rows, evidence, "inside") == "sensitive_access"

    def test_taking_a_dependency_is_counted_as_its_own_kind(self) -> None:
        """Trust is inward, so it is not the same queue entry as a publication."""
        rows = [effect("installs_dependency")]
        assert purpose_for(rows, EffectEvidence()) == "untrusted_dependency"

    def test_only_an_ask_carries_a_purpose(self) -> None:
        """A permission interrupts nobody and a refusal asks nobody to decide."""
        assert purpose_for([effect("publishes")], EffectEvidence()) is None
        rows = [effect("writes_path", "production", "overwrite")]
        evidence = EffectEvidence(existing=True, tracked=True)
        assert verdict_for(rows, evidence, "inside") == "deny"
        assert purpose_for(rows, evidence, "inside") is None

    def test_a_caller_with_its_own_verdict_still_learns_what_it_is_about(self) -> None:
        """The ungated reading, for the questions this table did not raise.

        A shell row escalated by a flag guard is asked about something, and
        what it is asked about is what the row does. Gating on this table's
        own verdict would answer nothing for exactly those.
        """
        rows = [effect("writes_path", "production", "overwrite", reviewed=True)]
        evidence = EffectEvidence(existing=True, tracked=True)
        assert verdict_for(rows, evidence, "inside") == "allow"
        assert purpose_for(rows, evidence, "inside") is None
        assert purpose_of(rows, evidence, "inside") == "unrecovered_local_mutation"


class TestDeclaring:
    """What a rule may state, checked where it is written rather than read.

    The check exists because every member's verdict ends in a fall-through:
    a word none of its branches match earns the mildest answer on the list.
    That makes a misspelled scope a permission nobody declared, which is the
    same failure `member_for` refuses for a misspelled kind.
    """

    def test_a_scope_the_deciding_member_does_not_read_is_refused(self) -> None:
        """The failure this exists for: it would have fallen through to allow."""
        with pytest.raises(ValueError, match="reads no scope"):
            declare("writes_path", scope="project")

    def test_a_word_from_the_wrong_members_vocabulary_is_still_refused(self) -> None:
        """Scope is per member, so a real word read by the wrong one is a typo."""
        with pytest.raises(ValueError, match="reads no scope"):
            declare("writes_path", scope="unrecoverable")

    def test_a_member_reading_no_scope_takes_any_label(self) -> None:
        """The container noun and the external class are quoted, never matched."""
        assert declare("mutates_environment", scope="volume")["scope"] == "volume"

    def test_a_write_stated_where_no_member_reads_one_is_refused(self) -> None:
        """Unlike a scope, no member carries a write it does not decide on."""
        with pytest.raises(ValueError, match="reads no write"):
            declare("reads_path", scope="project", write="create")

    def test_a_project_table_checks_and_decides_the_same_kinds(self) -> None:
        """The two have to read one table, or a kind accepted here raises there.

        A project's own member is only reachable where this module is the one
        compiled, so what this holds is the seam rather than the deployment:
        `declare` and `deciding` take the same list, and a kind in it is
        declarable and decidable together.
        """

        class Charges(Effect):
            kind = "charges"

            def verdict(
                self,
                row: EffectRow,
                evidence: EffectEvidence,
                placement: SandboxPlacement,
            ) -> DecisionEffect:
                return "ask"

        members = [*EFFECT_MEMBERS, Charges()]
        row = declare("charges", reason="this spends money", members=members)

        assert (
            verdict_for([row], EffectEvidence(), "inside", STRENGTH, members) == "ask"
        )
        with pytest.raises(ValueError, match="no effect member answers"):
            declare("charges")

    def test_a_concern_of_its_own_needs_no_member_to_be_stated(self) -> None:
        """What actually crosses into a compiled runtime is a row, not a class.

        So the reachable extension is the scope and the reason of the member
        whose verdict a project wants, which asks in the project's own words.
        """
        row = declare(
            "mutates_environment",
            scope="paid agent session",
            reason="this opens an agent that spends money",
        )

        assert verdict_for([row], EffectEvidence(), "inside") == "ask"
        assert row["reason"] == "this opens an agent that spends money"

    def test_a_route_stated_where_no_member_reads_one_is_refused(self) -> None:
        """A claim that the gates see this, made to a verdict that never asks."""
        with pytest.raises(ValueError, match="reads no route"):
            declare("mutates_repository", reviewed=True)

    def test_every_scope_a_member_declares_is_one_it_answers_for(self) -> None:
        """A vocabulary listing a word no branch reads would document a lie."""
        for member in EFFECT_MEMBERS:
            for scope in member.scopes:
                assert declare(member.kind, scope=scope)["scope"] == scope


class TestUnclassifiedOperation:
    """Falling off the end of a table somebody finished enumerating."""

    def test_an_operation_no_row_classified_is_refused(self) -> None:
        """Refused rather than asked: the fix is one line in the table."""
        rows = [effect("unclassified_operation", "gh pr")]
        assert verdict_for(rows, EffectEvidence(contained=True), "inside") == "deny"

    def test_containment_does_not_soften_it_the_way_it_softens_a_program(
        self,
    ) -> None:
        """The distinction the two members exist to keep.

        An unknown program is confined by the boundary and can settle; what
        falls off `gh` reaches a remote the boundary does not cover, so the
        same placement buys it nothing.
        """
        evidence = EffectEvidence(contained=True)
        assert verdict_for([effect("runs_undeclared_program")], evidence, "inside") == (
            "ask"
        )
        assert verdict_for([effect("unclassified_operation")], evidence, "inside") == (
            "deny"
        )

    def test_a_refusal_asks_nobody_so_it_carries_no_purpose(self) -> None:
        rows = [effect("unclassified_operation", "git")]
        assert purpose_for(rows, EffectEvidence()) is None
