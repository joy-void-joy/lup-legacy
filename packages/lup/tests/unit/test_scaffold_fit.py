"""Which upstream commit a project's copied half corresponds to, measured.

The base adoption is given decides every merge after it, and two wrong
answers look like success. The commit the pin already resolves to leaves the
first merge nothing between base and target, so it reports three zeros while
every change nobody hand-ported stays untaken. A commit far behind the copy
re-offers a year of already-applied changes. What is tested here is that
neither passes in silence, that the reading identifies the right commit as a
peak, and that a project whose copy matches nothing well can still adopt by
restating what the measurement read.

The upstream repository is built rather than mocked, because the reading is
of git's own bytes: two commits that changed the copied half and one that did
not, which is what tells the sampled range from the whole history.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
import typer

import lup.devtools.dev.update as update
from lup.devtools.dev.scaffold import branch_head, merged_at
from lup.devtools.dev.scaffold_fit import (
    candidates,
    measured,
    restated,
    neighbourhood,
    strided,
    surveyed,
)
from lup.devtools.utils import short_sha
from lup.diagnostics import Refusal
from lup.execution.shell import git
from lup.policy.kernel.diagnostic import rendered
from tests.unit.test_ledger_placement import committed
from tests.unit.test_scaffold import (
    PACKAGE,
    SOURCE,
    adopter_from,
    upstream_at_base,
    wrote,
)


def upstream_moved_on(upstream: Path) -> str:
    """Upstream past the stamped commit: two files changed, one added."""
    wrote(
        upstream,
        "src/lup_template/serve.py",
        "from lup_template.tools import all\nserve = True\n",
    )
    wrote(upstream, "src/lup_template/tools.py", "all = []\npulse = True\n")
    wrote(upstream, "src/lup_template/kinds.py", "kinds = []\n")
    committed(upstream, "the pulse, and a new module")
    return git.out("-C", str(upstream), "rev-parse", "HEAD")


def upstream_revised_its_own_docs(upstream: Path) -> str:
    """A commit touching nothing the scaffold is compiled from."""
    wrote(upstream, "docs/upstream.md", "upstream's own, revised\n")
    committed(upstream, "upstream's own docs")
    return git.out("-C", str(upstream), "rev-parse", "HEAD")


def revised(upstream: Path, index: int) -> str:
    """One commit revising one of three modules in turn, and the sha it made."""
    turning = ["serve.py", "tools.py", "catalog.py"]
    name = turning[index % len(turning)]
    wrote(upstream, f"src/lup_template/{name}", f"revision = {index}\n")
    committed(upstream, f"revision {index} of {name}")
    return git.out("-C", str(upstream), "rev-parse", "HEAD")


def upstream_rotating(upstream: Path, rounds: int) -> list[str]:
    """A history whose reading is a tent, oldest first.

    Each commit moves one file of three, so a copy stamped from the middle of
    it reads every file identical there and one fewer at each commit either
    side — which is the shape a strided search is entitled to assume.
    """
    return [revised(upstream, index) for index in range(rounds)]


def pinned_at(root: Path, commit: str) -> None:
    """A lock resolving the library at one commit, as uv writes one."""
    wrote(
        root,
        "uv.lock",
        'version = 1\n\n[[package]]\nname = "lup-agents"\nversion = "0.3.0"\n'
        f'source = {{ git = "https://example.test/lup?branch=dev#{commit}" }}\n',
    )


def adopting(monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
    """Point adoption at this upstream, without a tracked registration.

    `upstream_checkout` is where the command materializes and fetches the
    clone `sync.json` names; the fixture repository is that clone here.
    """

    def checkout(project: str, report: Callable[[str], None]) -> Path:
        return upstream

    monkeypatch.setattr(update, "upstream_checkout", checkout)


def test_the_reading_counts_what_this_checkout_carries_of_one_commit(
    tmp_path: Path,
) -> None:
    """Byte for byte, which is what says whether a merge would have work to do."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    head = upstream_moved_on(upstream)

    stamped = measured(adopter, upstream, SOURCE, PACKAGE, base)
    later = measured(adopter, upstream, SOURCE, PACKAGE, head)

    # Everything the scaffold holds at the stamped commit is here byte
    # for byte; the later one compiles a module this copy never had and
    # edits others, so it is wider and less of it matches.
    assert stamped.identical == stamped.compiled == stamped.carried
    assert later.compiled > stamped.compiled
    assert later.carried == stamped.carried
    assert later.identical < later.compiled
    assert later.share() < stamped.share()
    assert later.subject == "the pulse, and a new module"


def test_the_search_peaks_at_the_commit_the_copy_was_stamped_from(
    tmp_path: Path,
) -> None:
    """The peak is the answer, and the table is what shows it is a peak."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    head = upstream_moved_on(upstream)
    outside = upstream_revised_its_own_docs(upstream)

    survey = surveyed(adopter, upstream, SOURCE, PACKAGE, outside)
    peak = survey.best()

    assert survey.reachable == 2
    assert [one.commit for one in survey.readings] == [head, base]
    assert peak is not None
    assert peak.commit == base
    assert f"* {short_sha(base)}" in survey.table()


def test_only_the_commits_that_changed_the_copied_half_are_candidates(
    tmp_path: Path,
) -> None:
    """A commit touching nothing under the roots compiles its parent's bytes."""
    upstream, base = upstream_at_base(tmp_path)
    head = upstream_moved_on(upstream)
    outside = upstream_revised_its_own_docs(upstream)

    assert candidates(upstream, SOURCE, outside) == [head, base]


def test_a_round_spreads_its_measurements_over_the_whole_range() -> None:
    """The range is the history; the depth is how finely one round reads it.

    Both ends are kept whatever the depth, because a peak at either end is
    what says the answer may lie outside the range — and the stretch the next
    round takes is bounded by the samples either side of the one that read
    highest, which is where a commit-level peak has to be.
    """
    window = [f"c{index}" for index in range(10)]
    spread = strided(window, 4)

    assert spread == ["c0", "c3", "c6", "c9"]
    assert strided(window, 20) == window
    assert neighbourhood(window, spread, "c3") == window[:7]
    assert neighbourhood(window, spread, "c0") == window[:4]
    assert neighbourhood(window, spread, "c9") == window[6:]


def test_the_search_closes_on_the_peak_without_measuring_the_whole_range(
    tmp_path: Path,
) -> None:
    """What makes the whole history affordable: rounds, not a window.

    Twelve revisions deep, measured four at a time — the peak is the commit
    the copy was stamped from, and the range is not compiled whole.
    """
    upstream, base = upstream_at_base(tmp_path)
    history = upstream_rotating(upstream, 12)
    adopter = adopter_from(tmp_path, upstream, history[5])

    survey = surveyed(adopter, upstream, SOURCE, PACKAGE, history[-1], 4)
    peak = survey.best()

    assert survey.reachable == len(history) + 1
    assert peak is not None
    assert peak.commit == history[5]
    assert len(survey.readings) < survey.reachable
    assert survey.readings[0].commit == history[-1]
    assert survey.readings[-1].commit == base


def test_a_base_equal_to_the_resolved_pin_is_refused_before_anything_is_rooted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent no-op, caught: the first merge would have nothing to carry."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    head = upstream_moved_on(upstream)
    pinned_at(adopter, head)
    adopting(monkeypatch, upstream)

    with pytest.raises(Refusal) as refusal:
        update.adopted(adopter, SOURCE, PACKAGE, head, print)

    said = rendered(refusal.value.said)
    assert "is the commit the library pin already resolves to" in said
    assert "0 fast-forwarded, 0 merged clean, 0 conflicted" in said
    stamped = measured(adopter, upstream, SOURCE, PACKAGE, base)
    assert f"{short_sha(base)} (the scaffold) reads {stamped.spelled()}" in said
    anyway = refusal.value.said["steps"][-1]["run"]
    assert anyway[-2:] == restated(measured(adopter, upstream, SOURCE, PACKAGE, head))
    assert branch_head(adopter, SOURCE.branch) == ""


def test_a_base_the_copy_matches_is_accepted_and_becomes_the_merge_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The right answer costs one compile and says what it read."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    head = upstream_moved_on(upstream)
    pinned_at(adopter, head)
    adopting(monkeypatch, upstream)
    said: list[str] = []  # lup: ignore[empty-collection] — collected by callback

    update.adopted(adopter, SOURCE, PACKAGE, base, said.append)

    assert merged_at(adopter, SOURCE.branch) == base
    stamped = measured(adopter, upstream, SOURCE, PACKAGE, base)
    assert (
        f"scaffold({short_sha(base)}) against this checkout: {stamped.spelled()}"
        in said[0]
    )


def test_a_base_given_short_is_rooted_at_the_commit_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved against upstream, so the recorded base is a commit git can read."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    adopting(monkeypatch, upstream)

    update.adopted(adopter, SOURCE, PACKAGE, short_sha(base), print)

    assert merged_at(adopter, SOURCE.branch) == base


def test_a_base_the_measurement_argues_against_is_refused_and_answerable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project whose copy matches nothing well still has to adopt something."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    head = upstream_moved_on(upstream)
    adopting(monkeypatch, upstream)

    with pytest.raises(Refusal) as refusal:
        update.adopted(adopter, SOURCE, PACKAGE, head, print)

    said = rendered(refusal.value.said)
    assert "fits this checkout poorly" in said
    stamped = measured(adopter, upstream, SOURCE, PACKAGE, base)
    assert f"{short_sha(base)} (the scaffold) reads {stamped.spelled()}" in said
    assert branch_head(adopter, SOURCE.branch) == ""

    reads = measured(adopter, upstream, SOURCE, PACKAGE, head).identical
    update.adopted(adopter, SOURCE, PACKAGE, head, print, reads)

    assert merged_at(adopter, SOURCE.branch) == head


def test_an_accepted_fit_that_misstates_the_reading_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape carries the measurement, so it cannot be typed by habit."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    head = upstream_moved_on(upstream)
    adopting(monkeypatch, upstream)

    reads = measured(adopter, upstream, SOURCE, PACKAGE, head)
    misstated = reads.identical + 1

    with pytest.raises(Refusal) as refusal:
        update.adopted(adopter, SOURCE, PACKAGE, head, print, misstated)

    said = refusal.value.said
    assert said["what"] == f"--accept-fit {misstated}"
    assert said["why"].startswith("is not what this base reads")
    assert said["steps"][-1]["run"][-2:] == restated(reads)
    assert branch_head(adopter, SOURCE.branch) == ""


def test_a_base_naming_no_commit_is_refused_by_the_clone_that_would_compile_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused where the argument is a typo, rather than inside `git archive`."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    adopting(monkeypatch, upstream)

    with pytest.raises(Refusal) as refusal:
        update.adopted(adopter, SOURCE, PACKAGE, "v0.2.0", print)

    assert refusal.value.said["what"] == "v0.2.0"
    assert (
        "names no commit in this project's upstream clone" in refusal.value.said["why"]
    )


def test_an_adoption_over_a_staged_change_is_refused_before_it_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout just renamed holds staged moves, and git would refuse the merge."""
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)
    wrote(adopter, "src/demo/staged.py", "staged = True\n")
    git("-C", str(adopter), "add", "src/demo/staged.py")

    def unreached(project: str, report: Callable[[str], None]) -> Path:
        raise AssertionError("fetched upstream for an adoption that cannot land")

    monkeypatch.setattr(update, "upstream_checkout", unreached)

    with pytest.raises(Refusal) as refusal:
        update.adopted(adopter, SOURCE, PACKAGE, base, print)

    assert "commit them first" in [said["says"] for said in refusal.value.said["steps"]]
    assert branch_head(adopter, SOURCE.branch) == ""


def test_an_update_before_adoption_says_so_and_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a branch there is no base, and compiling one would root it wrong.

    Rooted at the pin, the merge that follows finds no ancestor, and the
    branch it left would refuse the adoption that answers the problem.
    """
    upstream, base = upstream_at_base(tmp_path)
    adopter = adopter_from(tmp_path, upstream, base)

    def unmoved(*words: str, **named: str) -> str:
        raise AssertionError(f"an update before adoption moved a carrier: {words}")

    monkeypatch.setattr(update, "uv", unmoved)
    said: list[str] = []  # lup: ignore[empty-collection] — collected by callback

    with pytest.raises(typer.Exit):
        update.updated(adopter, SOURCE, PACKAGE, "", "lup-agents", said.append)

    assert "dev scaffold adopt --base <commit>" in said[0]
    assert branch_head(adopter, SOURCE.branch) == ""
