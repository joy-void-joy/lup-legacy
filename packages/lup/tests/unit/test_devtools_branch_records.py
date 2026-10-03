"""Behavior tests for where lup keeps what it knows about a branch.

The base a worktree was cut from and the commit it was reserved at live in
``<common>/lup/``, beside the edition record, and not in the repository's
shared ``config``, where ``core.hooksPath``, ``alias.*``,
``credential.helper`` and ``merge.*.driver`` name programs git runs on the
host and nothing in git reads a lup key.

What has to hold: a record written in one worktree answers from every other,
a fact written later does not erase one written earlier, a key a clone's
config carries answers nothing until it is adopted into the record, and a
branch recorded nowhere is still a branch nobody recorded.
"""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev import records


def git_in(work: Path) -> sh.Command:
    """A git bound to one checkout, with identity per call and no hooks.

    Identity never reaches `git config`: a persisted setting lands in the
    shared configuration, which is the file this whole module is about.
    """
    return sh.Command("git").bake(
        "-C",
        str(work),
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.email=records@example.test",
        "-c",
        "user.name=Records Test",
        _tty_out=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout with one commit, which is enough to carry branches."""
    work = tmp_path / "repo"
    work.mkdir(parents=True)
    git = git_in(work)
    git("init", "-b", "main")
    git("commit", "--allow-empty", "-m", "base")
    return work


def test_a_record_survives_a_round_trip(repo: Path) -> None:
    """What was written is what is read, from the same checkout."""
    records.remember(
        "topic", records.BranchRecord(base="main", base_commit="abc123"), repo
    )

    assert records.recorded_base("topic", repo) == "main"
    assert records.recorded_reservation("topic", repo) == "abc123"


def test_a_later_fact_keeps_the_ones_written_before_it(repo: Path) -> None:
    """Three commands own three facts, and none of them reads the others back.

    The base is written at creation, the upstream at the first push. Writing
    the second as a whole record would drop the first, and the branch would
    report itself as never cut from anything.
    """
    records.remember("topic", records.BranchRecord(base="main"), repo)

    records.remember("topic", records.BranchRecord(upstream="origin/topic"), repo)

    assert records.recorded_base("topic", repo) == "main"
    assert records.recorded_upstream("topic", repo) == "origin/topic"


def test_a_landing_keeps_the_facts_written_before_it(repo: Path) -> None:
    """Where the ref stood as it went, beside what it was cut from."""
    records.remember("topic", records.BranchRecord(base="main"), repo)

    records.record_landing("topic", "dev", repo)

    assert records.read_record("topic", repo).landed_in == "dev"
    assert records.recorded_base("topic", repo) == "main"


def test_a_name_deleted_holding_work_stops_claiming_an_earlier_landing(
    repo: Path,
) -> None:
    """The one write here that has to be able to state the empty answer.

    A landing is a verdict a deletion reaches in full, and "it held commits
    the integration branch lacked" is spelled blank. Folded in the way every
    other fact is, that blank would read as "not mine to say" and leave a
    second branch of a reused name claiming the first one's landing.
    """
    records.record_landing("topic", "dev", repo)

    records.record_landing("topic", "", repo)

    assert records.read_record("topic", repo).landed_in == ""


def test_a_record_written_in_one_worktree_reads_from_a_sibling(
    repo: Path, tmp_path: Path
) -> None:
    """The whole reason the shared git directory is the record's home.

    Detection runs wherever somebody happens to be standing, and the base of
    a branch is the same fact from every checkout of the repository. A
    per-worktree home — which is what ``git config --worktree`` would have
    given — answers only where it was written, and a sibling asking the same
    question gets the answer for a branch nobody recorded.
    """
    sibling = tmp_path / "tree" / "topic"
    git_in(repo)("worktree", "add", str(sibling), "-b", "topic")

    records.remember("topic", records.BranchRecord(base="main"), repo)

    assert records.recorded_base("topic", sibling) == "main"


def test_a_branch_recorded_nowhere_says_nothing(repo: Path) -> None:
    """The case that must not change: no record is still no record."""
    assert records.recorded_base("topic", repo) == ""
    assert records.recorded_reservation("topic", repo) == ""
    assert records.recorded_upstream("topic", repo) == ""


def test_a_fact_held_only_in_the_config_answers_nothing(repo: Path) -> None:
    """Reads answer from the record alone, whatever the config carries.

    A key in the shared config is something to adopt, not a second place to
    read: a read that fell back to it would keep the config a file lup's
    bookkeeping needs, which is what the record exists to end.
    """
    git_in(repo)("config", "branch.topic.lup-base", "main")
    git_in(repo)("config", "branch.topic.lup-base-commit", "abc123")

    records.remember("topic", records.BranchRecord(upstream="origin/topic"), repo)

    assert records.recorded_base("topic", repo) == ""
    assert records.recorded_reservation("topic", repo) == ""


def test_adopting_moves_a_config_record_and_drops_the_key(repo: Path) -> None:
    """The shared config ends up holding nothing, and the record everything."""
    git_in(repo)("config", "branch.topic.lup-base", "main")
    git_in(repo)("config", "branch.topic.lup-base-commit", "abc123")

    moved = list(records.adopt_config_records(repo))

    assert len(moved) == 2
    assert records.recorded_base("topic", repo) == "main"
    assert records.recorded_reservation("topic", repo) == "abc123"
    assert records.config_keys(repo) == []


def test_adopting_again_moves_nothing(repo: Path) -> None:
    """Idempotent, because it is offered as a step somebody may repeat.

    A second run finds no keys and says so by yielding nothing, and the
    records the first run wrote are left exactly as they were rather than
    overwritten with the blanks a missing key would produce.
    """
    git_in(repo)("config", "branch.topic.lup-base", "main")
    list(records.adopt_config_records(repo))

    assert list(records.adopt_config_records(repo)) == []
    assert records.recorded_base("topic", repo) == "main"


def test_a_branch_name_holding_a_dot_recovers_from_its_key(repo: Path) -> None:
    """The key is three parts and the middle one is not restricted.

    Cutting the key at a dot would name the branch ``feat`` and record its
    base under a branch that does not exist, leaving the real one reading as
    never recorded.
    """
    git_in(repo)("config", "branch.feat.v2.lup-base", "main")

    list(records.adopt_config_records(repo))

    assert records.recorded_base("feat.v2", repo) == "main"


def test_a_branch_left_in_the_config_is_named_as_awaiting_adoption(
    repo: Path,
) -> None:
    """The move reports itself, because nothing else about the clone would.

    No command fails while the keys sit there — a branch whose base nobody
    reads detects one from the topology instead — so the unfinished move has
    to be asked for rather than waited for.
    """
    git_in(repo)("config", "branch.topic.lup-base", "main")

    assert records.branches_awaiting_adoption(repo) == ["topic"]


def test_one_branch_holding_both_keys_is_named_once(repo: Path) -> None:
    """A count of keys would say two branches are left where one is."""
    git_in(repo)("config", "branch.topic.lup-base", "main")
    git_in(repo)("config", "branch.topic.lup-base-commit", "abc123")
    git_in(repo)("config", "branch.other.lup-base", "main")

    assert records.branches_awaiting_adoption(repo) == ["topic", "other"]


def test_a_clone_that_adopted_its_records_awaits_nothing(repo: Path) -> None:
    """How the report goes quiet: the measurement itself empties out."""
    git_in(repo)("config", "branch.topic.lup-base", "main")
    list(records.adopt_config_records(repo))

    assert records.branches_awaiting_adoption(repo) == []


def test_a_clone_that_never_carried_the_keys_awaits_nothing(repo: Path) -> None:
    """A record written where records belong is not something to move."""
    records.remember("topic", records.BranchRecord(base="main"), repo)

    assert records.branches_awaiting_adoption(repo) == []


def test_an_unreadable_record_reads_as_no_record(repo: Path) -> None:
    """A blank answer is an ordinary state, so a corrupt file is one too."""
    path = records.record_path("topic", repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a record", encoding="utf-8")

    assert records.recorded_base("topic", repo) == ""
