"""A write whose target arrives through indirection is judged as the path it names.

`cd w && F=<protected path> && sed -i … $F` was allowed with no question: the
binding pass bound an assignment only where it stood first in its chain, so
`$F` stayed unread, and a command referencing a name the line bound to
something unread was answered by a floor a boundary settles -- the runtime's
own sandbox, which confines the call and not the checkout it writes in -- in
place of what the command asks as spelled. The same floor answered a
substitution's output, a loop over a glob, `while read`, `find -exec`'s `{}`,
and every command after a construct the walk stopped at, and the placing pass
read a `cd` as having succeeded wherever it ran: `cd a || rm x` and
`cd missing; rm x` removed `x` where the shell already stood, judged as `a/x`.

So a variable the line settles is resolved and judged as its literal
spelling, a `cd` is followed through `&&`, `||`, `!` and an `if` the way the
shell follows it, and what nothing on the line can read -- a `read`, a
substitution, a glob, find's `{}`, a directory a `cd` may or may not have
reached -- is judged as the command it is spelled as, which asks wherever the
same command naming a protected path would.

Driven the way a session drives it: each runtime's generated dispatcher on
the payload its harness sends, under no boundary, the runtime's own sandbox
and a measured container, and beside it `dev policy`'s reading of the same
placements, which has to agree.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.repos import initialized_repo
from tests.unit.test_outer_posture_policy import Posture, Runtime, met, previewed

POSTURES: tuple[Posture, ...] = ("none", "inner", "outer")

PROTECTED = "packages/lup/src/lup/policy/kernel/shell.py"
"""A path under a protected root, which every write to asks about."""

RESOLVED = [
    pytest.param("cd {checkout} && F={protected} && sed -i 1d $F", id="issue-529"),
    pytest.param("true && F={protected} && sed -i 1d $F", id="chain"),
    pytest.param("F={protected}; sed -i 1d $F", id="standing"),
    pytest.param("cd {checkout} && F={protected} && echo x > $F", id="redirect"),
    pytest.param("cd {checkout} && F={protected} && echo x | tee $F", id="tee"),
    pytest.param("cd {checkout} && F={protected} && cp tmp/x.txt $F", id="cp"),
    pytest.param("cd {checkout} && F={protected} && mv tmp/x.txt $F", id="mv"),
    pytest.param(
        "cd {checkout} && F={protected} && install tmp/x.txt $F", id="install"
    ),
    pytest.param("cd {checkout} && F={protected} && rm $F", id="rm"),
    pytest.param(
        "cd packages && F=lup/src/lup/policy/kernel/shell.py && rm $F", id="cd-chain"
    ),
    pytest.param("for F in {protected}; do sed -i 1d $F; done", id="literal-loop"),
    pytest.param("cd tmp || sed -i 1d {protected}", id="failed-cd"),
    pytest.param(
        "if cd packages; then rm lup/src/lup/policy/kernel/shell.py; fi", id="if-cd"
    ),
    pytest.param(
        "command cd packages && rm lup/src/lup/policy/kernel/shell.py", id="command-cd"
    ),
    pytest.param("read F <<< x; git status $F; rm {protected}", id="after-a-gate"),
    pytest.param("(( 1 )); rm {protected}", id="after-arithmetic"),
    pytest.param(
        "for a in 1; do for b in 1; do for c in 1; do rm {protected}; done; done; done",
        id="nested-past-the-walk",
    ),
    pytest.param("select x in a; do :; done; rm {protected}", id="after-select"),
]
"""A protected path the line itself resolves, each spelling its own way there."""

UNRESOLVED = [
    pytest.param("read F <<< {protected}; sed -i 1d $F", id="read"),
    pytest.param("read F <<< {protected}; rm $F", id="read-rm"),
    pytest.param("read F <<< {protected}; cp tmp/x.txt $F", id="read-cp"),
    pytest.param("F=$(echo {protected}); sed -i 1d $F", id="substituted-binding"),
    pytest.param("sed -i 1d $(echo {protected})", id="substitution"),
    pytest.param("rm $(echo {protected})", id="substitution-rm"),
    pytest.param("true || F={protected} && sed -i 1d $F", id="skippable-binding"),
    pytest.param("F=x.bak; sed -i 1d ${{F%.bak}}", id="operator"),
    pytest.param(
        "for F in packages/lup/src/lup/policy/kernel/*.py; do sed -i 1d $F; done",
        id="glob-loop",
    ),
    pytest.param("while read F; do rm $F; done < tmp/x.txt", id="while-read"),
    pytest.param("find packages -name '*.py' -exec rm {{}} +", id="find-exec"),
    pytest.param(
        "find packages -name '*.py' -exec sed -i 1d {{}} +", id="find-exec-sed"
    ),
    pytest.param(
        "cd packages; rm lup/src/lup/policy/kernel/shell.py", id="cd-may-fail"
    ),
    pytest.param(
        "cd packages && true; rm lup/src/lup/policy/kernel/shell.py",
        id="past-the-chain",
    ),
    pytest.param("cd tmp && cd - && rm {protected}", id="cd-back"),
    pytest.param("function f {{ rm {protected}; }}; f", id="function-body"),
    pytest.param("cd $X && echo x > {protected}", id="unread-cd-redirect"),
    pytest.param("export F={protected}; sed -i 1d $F", id="export"),
    pytest.param("eval 'sed -i 1d {protected}'", id="eval"),
    pytest.param("echo {protected} | xargs rm", id="xargs"),
    pytest.param("bash -c 'sed -i 1d {protected}'", id="bash-c"),
]
"""A write whose target nothing on the line can read, where it may be protected."""

AS_WRITTEN = [
    pytest.param(
        "cd {checkout} && F=src/app.py && sed -i 1d $F",
        "cd {checkout} && sed -i 1d src/app.py",
        id="ordinary-chain",
    ),
    pytest.param(
        "cd {checkout} && F=tmp/x.txt && rm $F",
        "cd {checkout} && rm tmp/x.txt",
        id="scratch-chain",
    ),
    pytest.param(
        "true && F=tmp/x.txt && echo x > $F",
        "true && echo x > tmp/x.txt",
        id="scratch-redirect",
    ),
    pytest.param(
        "F=src/app.py; sed -i 1d $F", "sed -i 1d src/app.py", id="ordinary-standing"
    ),
    pytest.param(
        "for F in tmp/a.txt tmp/b.txt; do rm $F; done",
        "rm tmp/a.txt; rm tmp/b.txt",
        id="scratch-loop",
    ),
    pytest.param("cd tmp || rm src/app.py", "rm src/app.py", id="failed-cd-ordinary"),
    pytest.param("if cd tmp; then rm x.txt; fi", "rm tmp/x.txt", id="if-cd-scratch"),
    pytest.param("cd tmp && rm x.txt", "rm tmp/x.txt", id="cd-scratch"),
]
"""An ordinary or scratch path through the same shapes, and the literal it resolves to."""

READS = [
    pytest.param("read F <<< {protected}; cat $F", id="read-cat"),
    pytest.param(
        "for F in packages/lup/src/lup/policy/kernel/*.py; do cat $F; done",
        id="glob-cat",
    ),
    pytest.param("cd $X && cat {protected}", id="unread-cd-cat"),
    pytest.param("find packages -name '*.py' -exec grep -l x {{}} +", id="find-grep"),
]
"""What only reads through an unread word, which nothing asks about."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_record_held: None
) -> Iterator[Path]:
    """A checkout holding a protected file, an ordinary one and scratch, in a container.

    The ledger is the one a measured container leaves, so the `outer` posture
    answers as a contained launch would; the other two read no containment.
    """
    work = tmp_path / "checkout"
    initialized_repo(work, tmp_path / "no-hooks")
    for name in (PROTECTED, "src/app.py", "tmp/x.txt", "tmp/a.txt", "tmp/b.txt"):
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text("one\ntwo\n", encoding="utf-8")
    ledger = work / ".lup" / "preflight" / "launch.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "contained": ["yes"],
                "delivered": ["inside_placement", "question_relay"],
                "blocked": ["host_executor"],
                "writable_roots": [str(work)],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("LUP_BOUNDARY_ROOT", raising=False)
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    monkeypatch.delenv("LUP_SANDBOX_ACTIVE", raising=False)
    yield work


def spelled(shape: str, checkout: Path) -> str:
    """One shape with the checkout and the protected path written into it."""
    return shape.format(checkout=checkout, protected=PROTECTED)


def answers(
    runtime: Runtime, command: str, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], dict[str, str]]:
    """What each posture's session meets, and what `dev policy` says it meets.

    The preview is read standing in the checkout, as a session would, and
    the test is put back where it stood: each dispatcher is found from there.
    """
    here = Path.cwd()
    session = {
        posture: met(runtime, posture, command, checkout) for posture in POSTURES
    }
    preview = previewed(command, checkout, monkeypatch)
    monkeypatch.chdir(here)
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    return session, preview


@pytest.mark.parametrize("shape", RESOLVED)
def test_a_protected_path_the_line_resolves_asks_on_every_posture(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    session, preview = answers(runtime, spelled(shape, checkout), checkout, monkeypatch)

    assert session == dict.fromkeys(POSTURES, "ask")
    assert preview == dict.fromkeys(POSTURES, "ask")


@pytest.mark.parametrize("shape", UNRESOLVED)
def test_a_write_nothing_on_the_line_can_read_is_never_settled_by_a_boundary(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """At least the question the literal protected path gets, on every posture."""
    session, preview = answers(runtime, spelled(shape, checkout), checkout, monkeypatch)

    assert set(session.values()) <= {"ask", "deny"}
    assert preview == session


@pytest.mark.parametrize(("shape", "literal"), AS_WRITTEN)
def test_an_ordinary_path_through_the_same_shapes_is_judged_as_written(
    runtime: Runtime,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    literal: str,
) -> None:
    indirect = answers(runtime, spelled(shape, checkout), checkout, monkeypatch)

    assert indirect == answers(
        runtime, spelled(literal, checkout), checkout, monkeypatch
    )


def test_scratch_reached_through_a_variable_stays_allowed(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = spelled("cd {checkout} && F=tmp/x.txt && rm $F", checkout)

    session, preview = answers(runtime, command, checkout, monkeypatch)

    assert session == dict.fromkeys(POSTURES, "allow")
    assert preview == dict.fromkeys(POSTURES, "allow")


@pytest.mark.parametrize("shape", READS)
def test_a_read_through_an_unread_word_is_answered_as_before(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    session, preview = answers(runtime, spelled(shape, checkout), checkout, monkeypatch)

    assert session == dict.fromkeys(POSTURES, "allow")
    assert preview == dict.fromkeys(POSTURES, "allow")
