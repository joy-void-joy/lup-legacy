"""Behavioral contract of the resolver entry: headless answers and note intake."""

import inspect
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner
import lup.devtools.harness.resolve as resolve_entry
from lup.resolver.admissions import AdmissionMailbox, AdmissionStatus

from lup.harness.codescan.markers import NoteKind
from lup.channels.models import utc_now
from lup.coordination.mailbox import (
    AnswerDoor,
    AnswerOffer,
    MailboxConflictError,
    RecordedAnswer,
)
from lup.coordination.questions import QuestionAnswer
from lup.resolver.mailbox import QuestionMailbox
from lup.resolver.core import planned_evidence
from lup.resolver.models import (
    AcceptanceCriterion,
    AdmissionRequest,
    Concern,
    ConcernInventory,
    InventoryPlanner,
    MaterialQuestion,
    PlannedConcern,
    QuestionBatch,
    ResolveInventory,
    ResolvePhase,
    ResolveRequest,
    ResolveState,
    SourceSnapshot,
)
from lup.resolver.state import ResolverStateRepository
from lup.harness.models import ResolveSpec, SkillInvocation
from lup.devtools.dev.comments import FoundComment
from lup.harness.ownership import (
    OWNERSHIP_FILENAME,
    GeneratedArtifacts,
    OwnedArtifact,
    OwnershipManifest,
)
from lup.devtools.harness.resolve import (
    AUTH_PROBE_SECONDS,
    HOST_BACKOFF_SECONDS,
    HOST_RETRIES,
    AdmissionFlags,
    DetachedRun,
    NoteTargetRef,
    SupervisorSpawn,
    admission_notes,
    admission_request,
    chosen_run,
    inert_offers,
    missing_run_refusal,
    offer_flag_answers,
    parse_answer_flags,
    parse_note_targets,
    resolver_intake,
    run_owned,
    scanned_intake,
    seed_request,
)
from typer.core import TyperGroup
from typer.main import get_group

from lup_template.devtools.main import app
from tests.unit.repos import commit_file, initialized_repo


@pytest.mark.parametrize("adapter", ["claude", "codex"])
@pytest.mark.parametrize("detach", [False, True])
def test_existing_run_admission_queues_before_launching_or_touching_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str, detach: bool
) -> None:
    monkeypatch.setattr(resolve_entry, "project_root", lambda: tmp_path)
    state_root = tmp_path / ".lup" / "resolve"
    persisted_run(state_root, "live", ResolvePhase.WORKERS)
    repository = ResolverStateRepository(state_root, "live")
    log = repository.root / "detached.log"
    log.write_text("original live evidence\n", encoding="utf-8")
    with repository.exclusive():
        result = CliRunner().invoke(
            app,
            [
                "resolve",
                "--adapter",
                adapter,
                "--run-id",
                "live",
                "--admit",
                "new material evidence",
                *(["--detach"] if detach else []),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "queued" in result.output
    assert "started detached" not in result.output
    assert log.read_text() == "original live evidence\n"
    receipts = AdmissionMailbox(repository.root).receipts()
    assert len(receipts) == 1
    assert receipts[0].status == AdmissionStatus.QUEUED
    assert receipts[0].request.statements == ["new material evidence"]


def material_question(
    identifier: str,
    choices: list[str] | None = None,
    recommendation: str | None = None,
) -> MaterialQuestion:
    return MaterialQuestion(
        id=identifier,
        concern_id="concern-1",
        prompt=f"prompt for {identifier}",
        choices=choices or [],
        recommendation=recommendation,
    )


def question_batch(questions: list[MaterialQuestion]) -> QuestionBatch:
    return QuestionBatch(run_id="run-7", questions=questions)


def persisted_run(state_root: Path, run_id: str, phase: ResolvePhase) -> None:
    """One run on disk, far enough along to have a phase worth reading."""
    repository = ResolverStateRepository(state_root, run_id)
    repository.save(
        ResolveState(
            config_digest="digest",
            run_id=run_id,
            phase=phase,
            source=SourceSnapshot(branch="dev", commit="source"),
            spec=ResolveSpec(
                id="resolve",
                worker_identity="resolver-worker",
                worker_skill=SkillInvocation(plugin="lup", skill="worker"),
                review_skill=SkillInvocation(plugin="lup", skill="review"),
                merge_skill=SkillInvocation(plugin="lup", skill="merge"),
            ),
            concerns=[],
            progress=[],
        )
    )


def test_an_unfinished_run_is_never_left_behind_silently(tmp_path: Path) -> None:
    """A run id defaults to the commit it starts from, so the default moves.

    Two invocations a day apart therefore never mean the same run, and a
    skill that enters the resolver bare starts a second one beside a parked
    one holding every answer already collected — which the skill's own text
    calls the most expensive thing you can do, while the entry made it the
    easy path.
    """
    persisted_run(tmp_path, "resolve-older", ResolvePhase.QUESTIONS)

    with pytest.raises(typer.BadParameter) as refused:
        chosen_run(tmp_path, "resolve-fresh", start_new=False, ending=False)

    assert "resolve-older" in str(refused.value)
    assert "--new" in str(refused.value)


def test_a_finished_or_abandoned_run_does_not_stand_in_the_way(
    tmp_path: Path,
) -> None:
    """Only a run still owed something is a choice; the rest are history."""
    persisted_run(tmp_path, "resolve-done", ResolvePhase.COMPLETE)
    persisted_run(tmp_path, "resolve-dropped", ResolvePhase.ABORTED)

    assert (
        chosen_run(tmp_path, "resolve-fresh", start_new=False, ending=False)
        == "resolve-fresh"
    )


def test_a_failed_run_still_counts_as_unfinished(tmp_path: Path) -> None:
    """A resume re-enters from the phase the failure stopped at.

    That is the whole reason failing is not the end of a run, and this one
    failed twice tonight and carried on both times.
    """
    persisted_run(tmp_path, "resolve-stumbled", ResolvePhase.FAILED)

    with pytest.raises(typer.BadParameter):
        chosen_run(tmp_path, "resolve-fresh", start_new=False, ending=False)


def test_asking_for_a_new_run_is_taken_at_its_word(tmp_path: Path) -> None:
    persisted_run(tmp_path, "resolve-older", ResolvePhase.QUESTIONS)

    assert (
        chosen_run(tmp_path, "resolve-fresh", start_new=True, ending=False)
        == "resolve-fresh"
    )


def test_ending_a_run_is_not_a_choice_between_runs(tmp_path: Path) -> None:
    """`--abort` names what it ends, so nothing here has anything to pick."""
    persisted_run(tmp_path, "resolve-older", ResolvePhase.QUESTIONS)

    assert (
        chosen_run(tmp_path, "resolve-fresh", start_new=False, ending=True)
        == "resolve-fresh"
    )


def test_parse_answer_flags_maps_ids_and_keeps_values_with_equals() -> None:
    parsed = parse_answer_flags(["q-1=yes", "q-2=a=b"])

    assert parsed == {"q-1": "yes", "q-2": "a=b"}


def test_parse_answer_flags_rejects_malformed_and_duplicate_flags() -> None:
    with pytest.raises(typer.BadParameter):
        parse_answer_flags(["missing-separator"])
    with pytest.raises(typer.BadParameter):
        parse_answer_flags(["=value"])
    with pytest.raises(typer.BadParameter):
        parse_answer_flags(["q-1=a", "q-1=b"])


def test_flag_answers_become_offers(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)

    offer_flag_answers(mailbox, "run-7", {"q-1": "yes", "q-2": "free text"})

    assert [(item.question_id, item.value, item.door) for item in mailbox.offers()] == [
        ("q-1", "yes", AnswerDoor.FLAG),
        ("q-2", "free text", AnswerDoor.FLAG),
    ]


def test_a_flag_may_answer_a_question_the_run_has_not_asked_yet(
    tmp_path: Path,
) -> None:
    """Offers precede questions, so a fresh run need not park once first."""
    mailbox = QuestionMailbox(tmp_path)

    offer_flag_answers(mailbox, "run-7", {"q-9": "x"})

    assert mailbox.questions() == []
    assert [item.question_id for item in mailbox.offers()] == ["q-9"]
    assert mailbox.answers() == []


def settle(mailbox: QuestionMailbox, identifier: str, value: str) -> None:
    """Promote one answer, which is the point after which it stops moving."""
    mailbox.record(
        RecordedAnswer(
            run_id="run-7",
            answer=QuestionAnswer(question_id=identifier, value=value),
            door=AnswerDoor.FLAG,
            answered_at=utc_now(),
        )
    )


def test_re_offering_the_settled_value_is_the_no_op_a_rerun_needs(
    tmp_path: Path,
) -> None:
    """The documented rerun recipe re-passes answers a promoter already took."""
    mailbox = QuestionMailbox(tmp_path)
    settle(mailbox, "q-1", "approve")

    offer_flag_answers(mailbox, "run-7", {"q-1": "approve"})

    assert mailbox.offers() == []


def test_correcting_a_settled_answer_is_refused_rather_than_recorded(
    tmp_path: Path,
) -> None:
    """Silence here leased a concern whose design the human had rejected."""
    mailbox = QuestionMailbox(tmp_path)
    settle(mailbox, "q-1", "approve")

    with pytest.raises(typer.BadParameter) as refusal:
        offer_flag_answers(mailbox, "run-7", {"q-1": "defer"})

    assert "'approve'" in str(refusal.value)
    assert "'defer'" in str(refusal.value)
    assert mailbox.offers() == []


def test_every_stale_correction_is_named_by_one_rerun(tmp_path: Path) -> None:
    """Finding the next one only after dropping the last costs a rerun each."""
    mailbox = QuestionMailbox(tmp_path)
    settle(mailbox, "q-1", "approve")
    settle(mailbox, "q-2", "approve")

    with pytest.raises(typer.BadParameter) as refusal:
        offer_flag_answers(mailbox, "run-7", {"q-1": "defer", "q-2": "defer"})

    assert "q-1" in str(refusal.value)
    assert "q-2" in str(refusal.value)


def test_a_still_open_question_keeps_taking_corrections(tmp_path: Path) -> None:
    """Offers stay correctable right up until a promoter takes one."""
    mailbox = QuestionMailbox(tmp_path)

    offer_flag_answers(mailbox, "run-7", {"q-1": "typo"})
    offer_flag_answers(mailbox, "run-7", {"q-1": "meant this"})

    assert [(item.question_id, item.value) for item in mailbox.offers()] == [
        ("q-1", "meant this")
    ]


def test_an_offer_a_promotion_outran_is_named_at_resume(tmp_path: Path) -> None:
    """Nothing under `.lup/resolve` is unlinked, so one says what it found."""
    mailbox = QuestionMailbox(tmp_path)
    mailbox.offer(
        AnswerOffer(
            run_id="run-7",
            question_id="q-1",
            value="defer",
            door=AnswerDoor.FLAG,
            offered_at=utc_now(),
        )
    )
    settle(mailbox, "q-1", "approve")

    reported = inert_offers(mailbox)

    assert len(reported) == 1
    assert "q-1" in reported[0]
    assert "'approve'" in reported[0]


def test_an_offer_awaiting_promotion_is_not_reported_as_stale(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    offer_flag_answers(mailbox, "run-7", {"q-1": "defer"})

    assert inert_offers(mailbox) == []


def test_the_page_and_console_doors_are_refused_the_same_way(tmp_path: Path) -> None:
    """One rule at the point every door writes through, not three."""
    mailbox = QuestionMailbox(tmp_path)
    settle(mailbox, "q-1", "approve")

    for door in (AnswerDoor.PAGE, AnswerDoor.CONSOLE, AnswerDoor.AGENT):
        with pytest.raises(MailboxConflictError):
            mailbox.offer(
                AnswerOffer(
                    run_id="run-7",
                    question_id="q-1",
                    value="defer",
                    door=door,
                    offered_at=utc_now(),
                )
            )


def intake_note(
    kind: NoteKind = NoteKind.note,
    condition: str | None = None,
    file: str = "parked.py",
) -> FoundComment:
    return FoundComment(
        file=file,
        start_line=2,
        end_line=2,
        read_start=1,
        read_end=4,
        text="body",
        kind=kind,
        condition=condition,
        context="",
    )


def test_resolver_intake_excludes_deferred_notes_from_the_inventory() -> None:
    open_note = intake_note()
    parked = intake_note(kind=NoteKind.defer, condition="until v2 lands")
    bare = intake_note(kind=NoteKind.defer)

    intake = resolver_intake([open_note, parked, bare], GeneratedArtifacts(by_path={}))

    assert intake.actionable == [open_note]
    assert intake.generated == []
    assert [note.describe() for note in intake.carried] == [
        "carrying deferred[until v2 lands] parked.py:2-2",
        "carrying deferred parked.py:2-2",
    ]


def test_resolver_intake_leaves_a_note_in_a_generated_artifact_to_its_generator() -> (
    None
):
    own = intake_note(file="src/mine.py")
    theirs = intake_note(file=".claude/plugins/lup/hooks/runtime/kernel/edit.py")
    owned = GeneratedArtifacts(
        by_path={
            theirs.file: OwnedArtifact(
                path=Path(theirs.file),
                category="generated",
                sha256="0" * 64,
                semantic_id="harness.kernel.edit",
            )
        }
    )

    intake = resolver_intake([own, theirs], owned)

    assert intake.actionable == [own]
    assert [note.describe() for note in intake.generated] == [
        "leaving to its generator: harness.kernel.edit owns "
        ".claude/plugins/lup/hooks/runtime/kernel/edit.py:2-2"
    ]


def test_note_targets_parse_a_path_and_a_line() -> None:
    assert parse_note_targets(["src/module.py:42"]) == [
        NoteTargetRef(file=Path("src/module.py"), line=42)
    ]
    with pytest.raises(typer.BadParameter):
        parse_note_targets(["src/module.py"])
    with pytest.raises(typer.BadParameter):
        parse_note_targets([":42"])


def admission_flags(statements: list[str]) -> AdmissionFlags:
    return AdmissionFlags(statements=statements, notes=[], issues=[])


def test_an_invocation_without_admission_evidence_asks_for_nothing() -> None:
    """Every other resolver invocation must stay an ordinary drive."""
    assert admission_request(admission_flags([])) is None


def test_admitted_statements_become_the_evidence_a_run_plans_from() -> None:
    request = admission_request(admission_flags(["the relay must investigate first"]))

    assert request is not None
    assert request.statements == ["the relay must investigate first"]
    assert request.notes == []
    assert request.issues == []


def test_a_detached_relaunch_carries_every_kind_of_evidence_it_was_given() -> None:
    """Forwarding only the answers left what a human named silently behind."""
    flags = AdmissionFlags(
        statements=["the relay must investigate"], notes=["src/m.py:7"], issues=[42]
    )

    assert flags.arguments() == [
        "--admit",
        "the relay must investigate",
        "--admit-note",
        "src/m.py:7",
        "--admit-issue",
        "42",
    ]


def forwarded_run_id(arguments: list[str]) -> str | None:
    """The `--run-id` a relaunched child parses out of this command, if any."""
    if "--run-id" not in arguments:
        return None
    return arguments[arguments.index("--run-id") + 1]


def detached(
    run_id: str | None = None,
    admitted: AdmissionFlags | None = None,
    issues: bool = True,
) -> list[str]:
    """The command a detached launch of one invocation relaunches with."""
    return DetachedRun(
        adapter="claude",
        run_id=run_id,
        answers=[],
        admitted=admitted or admission_flags([]),
        issues=issues,
        wait=0.0,
        host_retries=HOST_RETRIES,
        host_backoff=HOST_BACKOFF_SECONDS,
        supervisor=SupervisorSpawn(),
        adopt_config=False,
        auth_probe_delay=AUTH_PROBE_SECONDS,
        max_parallel_workers=4,
        recheck_standing_per_join=False,
        profile=None,
    ).arguments()


def test_a_detached_seed_does_not_claim_the_run_it_is_about_to_create() -> None:
    """The launcher never reports a run started while the child refuses itself.

    Pinned as a composition rather than at either end: both halves can be
    green on their own, with the failure living only in what one hands the
    other.
    """
    arguments = detached(admitted=admission_flags(["seed a run from these words"]))

    assert "--admit" in arguments
    # Exactly what the child decides, given exactly what the launcher hands it.
    assert missing_run_refusal(forwarded_run_id(arguments), "resolve-derived") is None


def test_a_detached_admission_into_a_run_a_human_named_still_refuses() -> None:
    """The typo guard holds for a detached launch, which forges no run id."""
    arguments = detached("resolve-typo", admission_flags(["widen the run"]))

    assert forwarded_run_id(arguments) == "resolve-typo"
    assert missing_run_refusal(forwarded_run_id(arguments), "resolve-typo") is not None


def test_a_detached_launch_carries_the_evidence_scope_it_was_given() -> None:
    """Dropping it detaches a larger run than was asked for, reported as this one."""
    assert "--no-issues" in detached(issues=False)
    assert "--no-issues" not in detached(issues=True)


DETACH_CARRIES = [
    "adapter",
    "profile",
    "run_id",
    "answer",
    "admit",
    "admit_note",
    "admit_issue",
    "issues",
    "wait",
    "host_retries",
    "host_backoff",
    "supervise",
    "supervise_port",
    "supervise_linger",
    "adopt_config",
    "auth_probe_delay",
    "max_parallel_workers",
    "recheck_standing_per_join",
]
"""Every `resolve` option a detached relaunch reproduces."""

DETACH_DECLINES = {
    "context": "typer's own handle, not an option anybody passes",
    "abort": "refused beside --detach: ending a run takes no turn to outlive",
    "detach": "the flag itself, which a relaunch must not fork on again",
    "start_new": (
        "the parent has already chosen which run this is; re-issuing --new "
        "would have the child derive a second fresh one beside it"
    ),
}
"""Every option a relaunch deliberately does not carry, and why."""


def registered_group(parent: typer.Typer, name: str) -> typer.Typer:
    """The sub-app one typer app registers under a name."""
    for group in parent.registered_groups:
        if group.name == name and group.typer_instance is not None:
            return group.typer_instance
    raise AssertionError(f"no {name!r} group is registered")


def test_every_resolver_option_is_carried_or_declined_by_a_detached_launch() -> None:
    """A flag added later has to be decided rather than silently dropped.

    A silent misfire of this command is one option missing from the relaunch
    — the admitted words, a forged `--run-id`, the evidence scope. So the
    guard belongs on the whole option list rather than on any one option.
    """
    callback = registered_group(app, "resolve").registered_callback

    assert callback is not None and callback.callback is not None
    assert sorted(inspect.signature(callback.callback).parameters) == sorted(
        [*DETACH_CARRIES, *DETACH_DECLINES]
    )


def declared_spellings() -> dict[str, list[str]]:
    """Every option `resolve` declares, and the flags that reach it.

    Read off the command rather than listed beside the renderer. A spelling
    kept by hand can drift from the one the parser accepts — typer decouples
    a flag from its parameter name, so renaming one leaves the other alone —
    and a relaunch naming a flag this command does not declare is a child
    that rejects its own command line where nobody is listening.
    """
    group = get_group(app)
    found = group.commands["resolve"]
    assert isinstance(found, TyperGroup)
    group = found
    return {
        parameter.name: [*parameter.opts, *parameter.secondary_opts]
        for parameter in group.params
        if parameter.name is not None
    }


def test_a_relaunch_renders_a_declared_flag_for_every_option_it_carries() -> None:
    """Every direction, against the spellings the command itself declares.

    Naming an option carried is not carrying it, and rendering a flag is not
    rendering one this command would accept. Neither half is worth much
    alone: the first lets a name outlive the field that fed it, the second
    lets a renamed flag pass every check while the child fails to parse. And
    a declined option is spellable like any other, so it takes a third
    assertion to keep one out of the command a launch hands its child.
    """
    spellings = declared_spellings()
    rendered = [
        part
        for part in DetachedRun(
            adapter="claude",
            run_id="resolve-named",
            answers=["q-1=yes"],
            admitted=AdmissionFlags(statements=["said"], notes=["a.py:1"], issues=[7]),
            issues=False,
            wait=42.0,
            host_retries=3,
            host_backoff=7.5,
            supervisor=SupervisorSpawn(enabled=True, port=9999, linger=True),
            adopt_config=True,
            auth_probe_delay=1.5,
            max_parallel_workers=6,
            recheck_standing_per_join=True,
            profile="work",
        ).arguments()
        if part.startswith("--")
    ]

    unreached = [
        option
        for option in DETACH_CARRIES
        if not any(flag in rendered for flag in spellings[option])
    ]
    assert unreached == []
    declared = [flag for flags in spellings.values() for flag in flags]
    assert [flag for flag in rendered if flag not in declared] == []
    # A declined option is declared too, so being spellable is not enough to
    # keep it out: rendering `--detach` would fork a child that forks again.
    assert [
        flag
        for option in DETACH_DECLINES
        if option in spellings
        for flag in spellings[option]
        if flag in rendered
    ] == []


def test_an_admitted_note_carries_the_text_and_context_the_tree_holds() -> None:
    """An admitted note is the note itself, not a retyped paraphrase."""
    scanned = intake_note()

    notes = admission_notes([NoteTargetRef(file=Path("parked.py"), line=2)], [scanned])

    assert [(note.file, note.line, note.text) for note in notes] == [
        (Path("parked.py"), 2, "body")
    ]


def test_an_admitted_note_target_that_names_no_open_note_is_refused() -> None:
    """A deferred note never reaches the actionable set, so it is refused."""
    with pytest.raises(typer.BadParameter, match="no actionable"):
        admission_notes(
            [NoteTargetRef(file=Path("parked.py"), line=2)],
            resolver_intake(
                [intake_note(kind=NoteKind.defer, condition="until v2 lands")],
                GeneratedArtifacts(by_path={}),
            ).actionable,
        )


OPEN_NOTE = """\
alpha = 1
# lup: close the union
beta = 2
"""

PARKED_NOTE = """\
# lup: defer[until v2 lands]: widen the vocabulary
gamma = 3
"""

GENERATED_NOTE = """\
# lup: the generated copy carries this too
delta = 4
"""

GENERATED_PATH = ".claude/plugins/lup/hooks/runtime/kernel/edit.py"


@pytest.fixture
def intake_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tracked tree holding one note of each kind the resolver partitions."""
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    (work / GENERATED_PATH).parent.mkdir(parents=True)
    (work / ".claude" / OWNERSHIP_FILENAME).write_text(
        OwnershipManifest(
            schema_version=1,
            generator_version="0.0.0",
            source_digest="0" * 64,
            target_requirements=[],
            files=[
                OwnedArtifact(
                    path=Path(GENERATED_PATH),
                    category="generated",
                    sha256="0" * 64,
                    semantic_id="hooks.lup-policy",
                )
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )
    commit_file(git, work, "mine.py", OPEN_NOTE, "chore: open note")
    commit_file(git, work, "parked.py", PARKED_NOTE, "chore: parked note")
    commit_file(git, work, GENERATED_PATH, GENERATED_NOTE, "chore: generated note")
    monkeypatch.chdir(work)
    return work


def test_the_preview_names_every_bucket_at_its_own_file_and_line(
    intake_tree: Path,
) -> None:
    """Reconstructing this by hand took a dozen calls and ended in a guess."""
    assert scanned_intake(intake_tree).describe() == [
        "1 to plan, 1 carried, 1 left to a generator",
        "planning from mine.py:2-2",
        "carrying deferred[until v2 lands] parked.py:1-1",
        f"leaving to its generator: hooks.lup-policy owns {GENERATED_PATH}:1-1",
    ]


def test_the_preview_starts_no_run_and_leases_nothing(
    intake_tree: Path, tmp_path: Path
) -> None:
    """Seeing an inventory commits to nothing — no run, no worktree, no lease."""
    beside = sorted(path.name for path in tmp_path.iterdir())

    scanned_intake(intake_tree).describe()

    assert not (intake_tree / ".lup").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == beside


def seeded(
    comments: list[FoundComment], admission: AdmissionRequest | None
) -> ResolveRequest:
    request = seed_request(
        SourceSnapshot(branch="feature", commit="source-sha"), comments, [], admission
    )
    assert request is not None
    return request


def test_the_preview_lists_exactly_what_a_run_would_plan_from(
    intake_tree: Path,
) -> None:
    """One partitioning read twice: a preview cannot show what a run leaves out."""
    intake = scanned_intake(intake_tree)

    planned = seeded(intake.actionable, None)

    assert [(str(note.file), note.line) for note in planned.notes] == [
        (note.file, note.start_line) for note in intake.actionable
    ]
    assert [line for line in intake.describe() if line.startswith("planning from")] == [
        "planning from mine.py:2-2"
    ]


def test_statements_offered_to_no_named_run_seed_one_rather_than_refusing() -> None:
    """An id nobody named was never a claim that the run exists."""
    assert missing_run_refusal(None, "resolve-abc123def456") is None


def test_admitting_into_a_run_named_explicitly_still_refuses_when_it_is_missing() -> (
    None
):
    """A typo would otherwise start a second run and lease a worktree each."""
    refusal = missing_run_refusal("resolve-typo", "resolve-typo")

    assert refusal is not None
    assert "resolve-typo" in refusal
    # The refusal says statements are usable without a run, rather than
    # leaving a reader to infer that one must pre-exist for them to count.
    assert "Statements seed a run of their own" in refusal
    assert "drop --run-id" in refusal


def test_a_run_is_seeded_from_words_with_no_note_written_into_the_tree() -> None:
    """How a human arrives: the concerns in their own words, nothing on disk."""
    request = seeded([], AdmissionRequest(statements=["the relay must investigate"]))

    assert request.statements == ["the relay must investigate"]
    assert request.notes == []
    assert request.evidence_count() == 1


def test_a_seeded_run_mixes_what_was_said_with_what_the_tree_carries() -> None:
    """Both kinds are positions in one request, so one turn plans them together."""
    request = seeded(
        [intake_note()], AdmissionRequest(statements=["the relay must investigate"])
    )

    assert [(note.file, note.line) for note in request.notes] == [
        (Path("parked.py"), 2)
    ]
    assert request.statements == ["the relay must investigate"]


def test_a_note_named_explicitly_reaches_a_fresh_run_once() -> None:
    """The scan carries it too, so naming one folds it in rather than doubling it."""
    scanned = intake_note()
    named = admission_notes([NoteTargetRef(file=Path("parked.py"), line=2)], [scanned])

    request = seeded([scanned], AdmissionRequest(notes=named))

    assert [(note.file, note.line) for note in request.notes] == [
        (Path("parked.py"), 2)
    ]


def test_an_invocation_carrying_no_evidence_of_any_kind_seeds_nothing() -> None:
    """The one rule that decides whether a fresh run starts, asked once.

    Seeding folds together exactly the evidence a request refuses to exist
    without, so nothing to plan from is answered here rather than restated by
    the caller. A second copy of this in the entry would be the copy that
    drifts: no test can reach it, so dropping a term from it — the term that
    lets words alone start a run — leaves a green suite behind.
    """
    assert (
        seed_request(
            SourceSnapshot(branch="feature", commit="source-sha"), [], [], None
        )
        is None
    )


async def test_a_statement_seeded_run_reaches_a_planned_inventory() -> None:
    """Words alone have to arrive as a concern, not merely as a request.

    Positions are the whole join between what was said and what gets worked
    on: a planner cites indexes and never echoes content, so a statement that
    seeds a run but resolves back as a note — or as nothing — is the failure
    this pins. The tree's note is mixed in because the ordering is what makes
    the citation ambiguous, and a request holding statements alone would pass
    with the arithmetic wrong.
    """
    request = seeded(
        [intake_note()], AdmissionRequest(statements=["the relay must investigate"])
    )

    async def plan(evidence: ResolveRequest) -> ResolveInventory:
        """Stand in for the planning turn, citing every position it was given."""
        inventory = ConcernInventory(
            concerns=[
                PlannedConcern(
                    id="relay-investigation",
                    title="Relay investigation",
                    spec="Investigate what the relay does when nobody is waiting",
                    criteria=[
                        AcceptanceCriterion(id="rel-1", description="it is answered")
                    ],
                    evidence_indexes=list(range(evidence.evidence_count())),
                )
            ]
        )
        return ResolveInventory(
            source=evidence.source,
            concerns=[
                Concern(
                    id=planned.id,
                    title=planned.title,
                    spec=planned.spec,
                    criteria=planned.criteria,
                    notes=cited.notes,
                    evidence=cited.evidence,
                    issues=cited.issues,
                )
                for planned in inventory.concerns
                for cited in [planned_evidence(evidence, planned.evidence_indexes)]
            ],
        )

    planner: InventoryPlanner = plan
    concerns = (await request.inventory(planner)).concerns

    assert [concern.evidence for concern in concerns] == ["the relay must investigate"]
    assert [(note.file, note.line) for note in concerns[0].notes] == [
        (Path("parked.py"), 2)
    ]


def test_a_run_trusts_the_repository_it_was_invoked_against(tmp_path: Path) -> None:
    """The planner reads here, and an untrusted read drops the repo's grants."""
    root = tmp_path / "repo"
    root.mkdir()

    assert run_owned(root, root, tmp_path / "repo-resolve-a-run")


def test_a_run_trusts_the_checkouts_it_made(tmp_path: Path) -> None:
    worktree_root = tmp_path / "repo-resolve-a-run"

    assert run_owned(worktree_root / "a-concern", tmp_path / "repo", worktree_root)


def test_a_run_trusts_nothing_it_merely_opens_a_session_in(tmp_path: Path) -> None:
    """Trust follows the invocation, not wherever a session happens to land."""
    elsewhere = tmp_path / "somebody-elses-checkout"

    assert not run_owned(elsewhere, tmp_path / "repo", tmp_path / "repo-resolve-a-run")
