"""What `dev check --antipatterns` prints, and in which order.

The listing exists to be acted on. Printed ahead of the findings that fail
the run, hundreds of refuted and unresolved lines leave an agent tidying it
filtering them out with `grep -v` before it can start.
"""

from lup.devtools.dev.antipatterns import (
    ADVISORY_KINDS,
    AntiPatternScan,
    FoundAntiPattern,
    FoundRefutation,
    report_lines,
)


def swept() -> AntiPatternScan:
    """One advisory finding, two blocking ones, and a refutation of each kind."""
    return AntiPatternScan(
        findings=[
            FoundAntiPattern(
                file="a.py",
                kind="untyped",
                line=1,
                text="x = 1  # lup: ignore",
                message="bare directive",
            ),
            FoundAntiPattern(
                file="b.py",
                kind="missing",
                line=2,
                text="def f(x: Any): ...",
                message="Any is banned",
                rule_id="any-type",
            ),
            FoundAntiPattern(
                file="c.py",
                kind="spurious",
                line=3,
                text="# lup: ignore[cast]",
                message="guards nothing",
                rule_id="cast",
            ),
        ],
        refuted=[
            FoundRefutation(
                file="d.py",
                rule_id="dict-get",
                line=4,
                subject="client.get",
                evidence="Client is not a mapping",
                settled=True,
            ),
            FoundRefutation(
                file="e.py",
                rule_id="dict-get",
                line=5,
                subject="thing.get",
                evidence="no declaration resolved",
                settled=False,
            ),
        ],
    )


def test_the_blocking_findings_come_before_everything_else() -> None:
    lines = report_lines(swept(), ADVISORY_KINDS, refutations=False)

    listed = [line for line in lines if line.startswith(("a.py", "b.py", "c.py"))]
    assert [line.split(":")[0] for line in listed] == ["b.py", "c.py", "a.py"]
    assert lines[0].startswith("b.py:2 [missing]")


def test_refutations_are_counted_rather_than_listed_by_default() -> None:
    lines = report_lines(swept(), ADVISORY_KINDS, refutations=False)

    assert not any("[refuted" in line or "[unresolved" in line for line in lines)
    assert any("2 refuted or unresolved" in line for line in lines)


def test_the_flag_lists_each_refutation_after_the_findings() -> None:
    lines = report_lines(swept(), ADVISORY_KINDS, refutations=True)

    refuted = [index for index, line in enumerate(lines) if "[refuted" in line]
    unresolved = [index for index, line in enumerate(lines) if "[unresolved" in line]
    finding = max(index for index, line in enumerate(lines) if line.startswith("a.py"))
    assert refuted and unresolved
    assert min(refuted + unresolved) > finding
