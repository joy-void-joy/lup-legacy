"""What a project's declared edit table does to the gates the kernel decides.

Two things are pinned here, and the first matters more than the second: an
empty table decides exactly what the kernel decided before a table existed. A
seam that quietly moved a verdict for projects that never declared one would
be a worse defect than the rigidity it was built to fix.

The second is the resolution rule — last match wins, `.gitignore`-style — held
against the shapes a real declaration takes: a broad statement narrowed by an
exception, a threshold moved without touching who decides, and a gate a
project has no business softening but is nonetheless able to reach.
"""

from lup.policy.edit_rules import EditRule, erase_edit_rules
from lup.policy.kernel.edit import decide_edit
from lup.policy.kernel.rows import EditRuleRow, PathRoleRow, PathRuleRow

ROLES: list[PathRoleRow] = [{"root": "src", "role": "production"}]

NOTED = "# lup: this note names work somebody owes\n"


def verdict(
    path: str,
    before: str | None,
    after: str | None,
    rules: list[EditRule] | None = None,
    operation: str = "modify",
    path_exists: bool = True,
) -> str:
    """What the kernel decides about one change under one declared table."""
    return decide_edit(
        path,
        before,
        after,
        path_exists=path_exists,
        path_rules=[],
        antipattern_rows=[],
        path_roles=ROLES,
        suffix=path[path.rfind(".") :],
        operation=operation,
        edit_rules=erase_edit_rules(rules or []),
    ).effect


# ---------------------------------------------------------------------------
# An undeclared project sees the lattice it always saw
# ---------------------------------------------------------------------------


def test_an_empty_table_still_asks_about_a_whole_file_write() -> None:
    assert verdict("src/a.py", None, "x = 1\n", operation="create") == "ask"


def test_an_empty_table_still_defers_a_large_edit() -> None:
    grown = "".join(f"line {number}\n" for number in range(40))
    assert verdict("src/a.py", "line 0\n", grown) == "defer"


def test_an_empty_table_still_allows_a_small_edit() -> None:
    assert verdict("src/a.py", "x = 1\n", "x = 2\n") == "allow"


def test_an_empty_table_still_denies_removing_feedback() -> None:
    assert verdict("src/a.py", NOTED + "x = 1\n", "x = 1\n") == "deny"


# ---------------------------------------------------------------------------
# Last match wins
# ---------------------------------------------------------------------------

BROAD_THEN_NARROW = [
    EditRule(
        name="content-gates-stop-at-prose",
        gates=["full-write", "size"],
        effect="allow",
        reason="reviewed in the diff, not at the hook",
    ),
    EditRule(
        name="python-source-is-still-read",
        gates=["full-write", "size"],
        suffixes=[".py", ".pyi"],
        effect="ask",
        reason="full-file writes require approval",
    ),
]


def test_a_broad_rule_frees_the_files_no_exception_names() -> None:
    assert verdict("src/notes.md", None, "# title\n", BROAD_THEN_NARROW, "create") == (
        "allow"
    )
    assert (
        verdict(
            "src/Thing.lean",
            None,
            "theorem t : True := trivial\n",
            BROAD_THEN_NARROW,
            "overwrite",
        )
        == "allow"
    )


def test_the_later_exception_wins_over_the_broad_rule() -> None:
    """The whole point of last-match: the narrowing is written after."""
    assert verdict("src/a.py", None, "x = 1\n", BROAD_THEN_NARROW, "create") == "ask"


def test_order_is_the_semantics_so_reversing_it_reverses_the_verdict() -> None:
    assert (
        verdict(
            "src/a.py", None, "x = 1\n", list(reversed(BROAD_THEN_NARROW)), "create"
        )
        == "allow"
    )


def test_a_rule_constrains_only_the_axes_it_names() -> None:
    """An unnamed axis matches every value of it, rather than none."""
    everything = [EditRule(name="nothing-is-reviewed-here", effect="allow")]
    grown = "".join(f"line {number}\n" for number in range(40))

    assert verdict("src/a.py", None, "x = 1\n", everything, "create") == "allow"
    assert verdict("src/a.py", "line 0\n", grown, everything) == "allow"


def test_an_operation_axis_tells_creating_from_overwriting() -> None:
    creation_only = [
        EditRule(
            name="new-files-are-free",
            gates=["full-write"],
            operations=["create"],
            effect="allow",
            reason="a file that did not exist has nothing to lose",
        )
    ]

    assert verdict("src/a.py", None, "x = 1\n", creation_only, "create") == "allow"
    assert verdict("src/a.py", None, "x = 1\n", creation_only, "overwrite") == "ask"


# ---------------------------------------------------------------------------
# The threshold moves on its own axis
# ---------------------------------------------------------------------------


def test_a_threshold_moves_without_restating_who_decides() -> None:
    generous = [
        EditRule(name="prose-grows-freely", suffixes=[".md"], maximum_added_lines=100)
    ]
    grown = "".join(f"line {number}\n" for number in range(40))

    assert verdict("src/notes.md", "line 0\n", grown, generous) == "allow"
    assert verdict("src/notes.md", "line 0\n", grown) == "defer"


def test_a_rule_that_decides_nothing_cannot_shadow_one_that_does() -> None:
    """A threshold-only rule is not a match for the verdict lookup behind it."""
    table = [
        EditRule(name="python-writes-are-fine", gates=["full-write"], effect="allow"),
        EditRule(name="just-a-threshold", maximum_added_lines=100),
    ]

    assert verdict("src/a.py", None, "x = 1\n", table, "create") == "allow"


def test_erasure_drops_a_rule_that_states_neither() -> None:
    silent = EditRule(name="says-nothing", suffixes=[".py"])

    assert erase_edit_rules([silent]) == []


# ---------------------------------------------------------------------------
# Every gate is reachable, including the ones a project should not move
# ---------------------------------------------------------------------------


def test_a_project_can_reach_even_the_feedback_gate() -> None:
    """Reachable on purpose: an escape hatch in a reviewed file beats a fork.

    That it *can* be moved is the design; that moving it is visible in a
    declaration somebody reads is what makes the design defensible.
    """
    softened = [
        EditRule(
            name="feedback-deletion-is-ours-to-judge",
            gates=["feedback-removed"],
            effect="ask",
            reason="this project reviews note deletion by hand",
        )
    ]

    assert verdict("src/a.py", NOTED + "x = 1\n", "x = 1\n") == "deny"
    assert verdict("src/a.py", NOTED + "x = 1\n", "x = 1\n", softened) == "ask"


def test_moving_one_gate_leaves_its_neighbours_alone() -> None:
    """The gate ids are separate because the verdicts are about different things."""
    softened = [
        EditRule(name="only-this-one", gates=["feedback-removed"], effect="ask")
    ]
    claimed = "# lup: solved: the note this answers\n"

    assert verdict("src/a.py", claimed + "x = 1\n", "x = 1\n", softened) == "deny"


def test_an_unknown_effect_in_a_row_falls_back_to_the_kernel() -> None:
    """A hand-written row cannot vote itself a verdict the kernel does not have."""
    row: EditRuleRow = {
        "name": "malformed",
        "gates": [],
        "suffixes": [],
        "roles": [],
        "operations": [],
        "effect": "encourage",
        "maximum_added_lines": None,
        "reason": "",
    }

    assert (
        decide_edit(
            "src/a.py",
            None,
            "x = 1\n",
            path_exists=False,
            path_rules=[],
            antipattern_rows=[],
            path_roles=ROLES,
            suffix=".py",
            operation="create",
            edit_rules=[row],
        ).effect
        == "ask"
    )


def test_a_note_moved_to_another_declaration_is_not_a_deletion() -> None:
    """Relocation is the fourth honest way a note leaves a line.

    Survival is asked of the note's words across the whole revision rather
    than of the line it sat on, so moving one to the declaration it actually
    concerns is not the act this gate refuses. It bites hardest in a merge,
    where both sides add at one spot and a note routinely lands against the
    wrong declaration -- a gate that read the move as a deletion would leave
    it stranded there permanently.
    """
    before = NOTED + "first = 1\nsecond = 2\n"
    moved = "first = 1\n" + NOTED + "second = 2\n"

    assert verdict("src/a.py", before, moved) == "allow"


def test_stripping_the_note_off_standing_code_is_still_denied() -> None:
    """The other side of the same measurement, so relocation buys no exemption."""
    before = NOTED + "first = 1\nsecond = 2\n"

    assert verdict("src/a.py", before, "first = 1\nsecond = 2\n") == "deny"


SOLVED = "# lup: solved: this note names work somebody owes\n"
DEFERRED = "# lup: defer[branch:feat-x]: this note names work somebody owes\n"


def test_a_conflict_holding_one_note_in_two_states_resolves_to_either() -> None:
    """A merge can hold the same words as `defer:` on one side and `solved:`
    on the other, and resolving it keeps one spelling of one note.

    Measured before this ruling: both directions were denied — dropping the
    `defer:` spelling read as feedback removed, dropping the `solved:` one as
    a claim retired outside the review pass — so the completing edit of the
    merge could not land at all. Either resolution keeps the words in the
    file: as a claim the verify pass still checks, or as open feedback, which
    is the stronger obligation.
    """
    conflicted = SOLVED + DEFERRED + "first = 1\n"

    assert verdict("src/a.py", conflicted, SOLVED + "first = 1\n") == "allow"
    assert verdict("src/a.py", conflicted, DEFERRED + "first = 1\n") == "allow"


def test_dropping_both_spellings_of_a_note_is_still_denied() -> None:
    """The ruling above spans lifecycle states, never the words themselves."""
    conflicted = SOLVED + DEFERRED + "first = 1\n"

    assert verdict("src/a.py", conflicted, "first = 1\n") == "deny"


def test_converting_a_deferral_to_a_claim_is_the_sanctioned_resolve() -> None:
    before = DEFERRED + "first = 1\n"

    assert verdict("src/a.py", before, SOLVED + "first = 1\n") == "allow"


def test_a_production_full_write_is_a_quality_review_a_supervisor_may_answer() -> None:
    """What is being reviewed is how the code reads, and a supervisor reads code.

    The classification is semantic and independent of which native tool
    produced it — Edit, Write, apply_patch, a shell redirection — because what
    makes it a checkpoint is that a whole file arrives at once, not the name
    of the call that carried it.
    """
    decided = decide_edit(
        "src/app/service.py",
        None,
        "value = 1\n",
        path_exists=False,
        path_rules=[],
        antipattern_rows=[],
        path_roles=[PathRoleRow(root="src", role="production")],
        operation="create",
    )

    assert decided.effect == "ask"
    assert decided.reviewer == "supervisor_allowed"
    assert decided.purpose == "quality_review"
    assert decided.rule == "edit:full-write"


def test_an_edit_that_is_merely_large_is_handed_to_the_provider_deliberately() -> None:
    """The one abstention that survives, and it says so in a field.

    A large ordinary edit is exactly what a native auto-accept mode exists
    for, so interposing would replace a decision an operator already made.
    It reached the same word as a parser gap before, which is what let a gap
    inherit provider auto-mode.
    """
    decided = decide_edit(
        "src/app/service.py",
        "a = 1\n",
        "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\nf = 6\n",
        path_exists=True,
        path_rules=[],
        antipattern_rows=[],
        path_roles=[PathRoleRow(root="src", role="production")],
    )

    assert decided.effect == "defer"
    assert decided.abstention == "provider_native"
    assert decided.rule == "edit:size"


def test_a_protected_path_is_the_owner_s_question_and_not_a_supervisor_s() -> None:
    """A protected path says a person owns this file, which is the whole rule.

    Routing it to a supervisor would answer past exactly the person the
    declaration names.
    """
    decided = decide_edit(
        "docs/owned.md",
        "a\n",
        "b\n",
        path_exists=True,
        path_rules=[
            PathRuleRow(
                kind="exact",
                value="docs/owned.md",
                reason="human-owned",
                recovery=[],
                allow_autonomous=False,
            )
        ],
        antipattern_rows=[],
    )

    assert (decided.effect, decided.reviewer) == ("ask", "human_only")
    assert decided.rule == "edit:protected-path"
