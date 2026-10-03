"""One refusal names every violation an edit adds.

A gate answering with the first violation it meets would refuse a whole-file
write of a large module once per violation — `tuple-shape`, then
`subprocess`, `empty-collection`, `os-environ` — each refusal costing a
resend of the whole file.
"""

from lup.policy.bundle import bundled_antipattern_rows
from lup.policy.kernel.edit import antipattern_decision

FOUR_VIOLATIONS = (
    "import os\n"
    "import subprocess\n"
    "\n"
    "\n"
    "def run(rows: list[str]) -> tuple[int, str]:\n"
    "    seen = []\n"
    "    for row in rows:\n"
    "        seen.append(row)\n"
    "    subprocess.run(['true'], check=True, env=os.environ)\n"
    "    return (len(seen), 'a')\n"
)
"""The four rules one module broke in turn, each on a line of its own."""


def test_a_whole_file_is_refused_once_for_all_it_breaks() -> None:
    rows = bundled_antipattern_rows()[".py"]

    decision = antipattern_decision(None, FOUR_VIOLATIONS, rows, python_source=True)

    assert decision is not None
    assert decision.effect == "deny"
    for rule_id in ("tuple-shape", "subprocess", "empty-collection", "os-environ"):
        assert f"(rule {rule_id})" in decision.reason, rule_id


def test_each_violation_carries_its_own_way_through() -> None:
    """A strong rule admits no directive and a soft one names its placement."""
    rows = bundled_antipattern_rows()[".py"]

    decision = antipattern_decision(None, FOUR_VIOLATIONS, rows, python_source=True)

    assert decision is not None
    assert "line 5: no suppression is accepted" in decision.addressed()
    assert "line 9: to keep it, suppress it on line 9" in decision.addressed()


def test_the_violations_are_named_in_the_order_the_file_holds_them() -> None:
    rows = bundled_antipattern_rows()[".py"]

    decision = antipattern_decision(None, FOUR_VIOLATIONS, rows, python_source=True)

    assert decision is not None
    named = [line for line in decision.reason.splitlines() if line.startswith("`line ")]
    numbers = [int(line.split("`")[1].removeprefix("line ")) for line in named]
    assert numbers == sorted(numbers)


def test_one_violation_reads_as_it_always_has() -> None:
    rows = bundled_antipattern_rows()[".py"]

    decision = antipattern_decision(
        None, "import subprocess\n", rows, python_source=True
    )

    assert decision is not None
    assert decision.subject == "line 1"
    assert decision.recovery[0]["says"].startswith("to keep it, suppress it on line 1")


def test_a_question_does_not_hide_a_refusal_below_it() -> None:
    """A resolution-required rule nothing resolved is asked about, not refused.

    Answered first, the question's approval carried the edit through with
    the refusal below it never said: an uncovered violation admitted on an
    approval whose reason named a different line.
    """
    rows = bundled_antipattern_rows()[".py"]
    text = "def f(thing) -> None:\n    thing.get('key')\n    import subprocess\n"

    decision = antipattern_decision(None, text, rows, python_source=True)

    assert decision is not None
    assert decision.effect == "deny"
    assert "(rule subprocess)" in decision.reason
