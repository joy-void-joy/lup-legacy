"""The commands an ordinary session runs, which a rule change must not stop.

Every other measurement of this vocabulary reads one direction. The recorded
asks say which commands a question was raised about, so tightening a rule
never shows up there; the rule census says what each row earns, so a
de-escalation that stopped firing leaves all 411 verdicts identical. Both
would have passed a change that quietly put a question in front of `git
status`, because neither of them ever asks what an ordinary session does.

This is the other direction, and it is the cheap one: a list of commands that
must keep allowing, classified by the same policy a session is judged by, and
read where a change to the table is made rather than in somebody's session
three days later. What it costs is that a rule which genuinely means to
tighten one of these has to say so here — which is the point, since that is
exactly the change worth stating out loud.

**Only what must stay allowed belongs here.** A command that asks today is
either a defect to fix or a question somebody meant, and neither is settled by
adding it to a list that asserts allow. So the families below are deliberately
unremarkable: reading the tree, asking git what happened, running the
checkers, writing into scratch. The interesting commands are absent because
their answers are interesting.
"""

from collections.abc import Sequence

from pydantic import BaseModel


class CommandFamily(BaseModel, frozen=True):
    """A group of everyday commands, and what a session reaches for them for.

    Grouped rather than listed flat because the grouping is what a failure has
    to say. A sweep reporting that `git diff --stat` stopped being allowed
    leaves whoever reads it to reconstruct why that mattered; one reporting it
    under "asking git what happened" states the claim that broke.
    """

    what: str
    commands: list[str]


class SessionShape(BaseModel, frozen=True):
    """One posture a session runs in, and the name a failure reports it by.

    A corpus asserting that these commands allow has to say *for whom*. The
    sweep read one posture — interactive, uncontained, with somebody there to
    answer a question — and the others reach different rows: a worker session
    has nobody to ask, so a rule that starts asking about `git status` stops
    it outright rather than interrupting it, and a contained one answers a
    placement question the first never reaches. A tightening visible only in
    one of them was invisible to every reading this repository takes.

    The cost of the extra postures is that a rule which genuinely means to
    stop an everyday command in one of them has to say so here, which is the
    same trade the corpus itself makes and for the same reason.

    ``trapped`` is the runtime's own sandbox on the operator's machine, which
    `inner` arms; ``contained`` is the container `outer` measures around the
    session. They are separate walls answering separate rows, so a posture
    names which one stands.
    """

    what: str
    autonomous: bool
    interactive: bool
    trapped: bool
    contained: bool = False


SESSION_SHAPES = (
    SessionShape(what="interactive", autonomous=False, interactive=True, trapped=False),
    SessionShape(what="worker", autonomous=True, interactive=False, trapped=False),
    SessionShape(what="inner", autonomous=False, interactive=True, trapped=True),
    SessionShape(what="inner worker", autonomous=True, interactive=False, trapped=True),
    SessionShape(
        what="outer",
        autonomous=False,
        interactive=True,
        trapped=False,
        contained=True,
    ),
    SessionShape(
        what="outer worker",
        autonomous=True,
        interactive=False,
        trapped=False,
        contained=True,
    ),
)
"""The postures a session of this vocabulary runs in, swept one by one.

The axes that change which row answers, crossed rather than sampled: who is
there to answer a question, and which wall -- none, the runtime's sandbox, or
the container -- the session runs behind. Each is cheap to state, and a
tightening that shows only behind one wall is invisible to a sweep of the
others.
"""


READING_THE_CHECKOUT = (
    "ls",
    "ls -la src",
    "cat README.md",
    "head -50 pyproject.toml",
    "tail -20 pyproject.toml",
    "sed -n '1,40p' pyproject.toml",
    "wc -l pyproject.toml",
    "file README.md",
    "stat README.md",
    "realpath README.md",
    "diff -u README.md pyproject.toml",
    "test -f README.md",
)

SEARCHING_IT = (
    "grep -rn 'def main' src",
    "grep -c def README.md",
    "grep -rln 'pydantic' packages",
    "find . -name '*.toml' -maxdepth 2",
    "find src -type d",
    "sort README.md",
    "sort -u README.md",
    "uniq README.md",
    "cut -d= -f1 pyproject.toml",
    "awk 'NR<10' pyproject.toml",
    "comm -13 README.md pyproject.toml",
)

ASKING_GIT_WHAT_HAPPENED = (
    "git status",
    "git status --short",
    "git diff",
    "git diff --stat",
    "git diff HEAD --stat",
    "git -c color.ui=false diff",
    "git log --oneline -20",
    "git log --format='%h %s' -5",
    "git log -S 'declare' --oneline",
    "git show HEAD --stat",
    "git show HEAD:README.md",
    "git branch --show-current",
    "git branch -a",
    "git rev-parse HEAD",
    "git rev-parse --show-toplevel",
    "git rev-list --count HEAD",
    "git merge-base main HEAD",
    "git for-each-ref --format='%(refname:short)' refs/heads",
    "git ls-files",
    "git blame README.md",
    "git stash list",
    "git remote -v",
    "git worktree list",
    "git config --get user.name",
    "git describe --tags --always",
)

OFFERING_WORK_FOR_REVIEW = (
    "git add README.md",
    "git commit -m 'docs: a line'",
    "git fetch",
    "git pull --ff-only",
    "git push",
    "gh pr list",
    "gh pr view 1",
    "gh pr checks",
)

RUNNING_THE_CHECKERS = (
    "uv run pytest -q",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run pyright",
    "uv run python tmp/script.py",
    # The same checkers, reached without `uv run`: one program, one verdict.
    "pytest -q",
    "ruff check .",
    ".venv/bin/pytest -q",
    ".venv/bin/pyright",
)

WRITING_WHERE_NOTHING_IS_REVIEWED = (
    "mkdir -p tmp/build",
    "echo hello > tmp/scratch.txt",
    "cat > tmp/notes.txt <<'EOF'\nhello\nEOF",
    "uv run pytest -q > tmp/pytest.log",
    "uv run pytest -q >> tmp/pytest.log",
    "cp README.md tmp/readme-copy.md",
    "mv tmp/readme-copy.md tmp/readme.md",
    "touch tmp/marker",
    "rm tmp/scratch.txt",
    "rm -r tmp/build",
)

SHAPING_OUTPUT = (
    "git log --oneline | head -20",
    "ls src | sort",
    "cat pyproject.toml | wc -l",
    "grep -rn 'import' src | head -20",
    "ls -la | awk '{print $9}'",
    "cat pyproject.toml 2>&1 | head",
    "ls tmp/ 2>/dev/null || true",
    "if [ -f README.md ]; then cat README.md; fi",
    'for name in a b c; do echo "$name"; done',
    'echo "$(git rev-parse HEAD)"',
)

READING_THE_MACHINE = (
    "pwd",
    "cd src",
    "echo hello",
    "true",
    "date",
    "command -v uv",
    "which python",
    "uname -a",
    "df -h .",
    "du -sh tmp",
    "ps aux | head",
    "printenv PATH",
)


def everyday_commands(
    reading: Sequence[str] = READING_THE_CHECKOUT,
    searching: Sequence[str] = SEARCHING_IT,
    git_history: Sequence[str] = ASKING_GIT_WHAT_HAPPENED,
    offering: Sequence[str] = OFFERING_WORK_FOR_REVIEW,
    checkers: Sequence[str] = RUNNING_THE_CHECKERS,
    scratch: Sequence[str] = WRITING_WHERE_NOTHING_IS_REVIEWED,
    shaping: Sequence[str] = SHAPING_OUTPUT,
    machine: Sequence[str] = READING_THE_MACHINE,
    also: Sequence[CommandFamily] = (),
) -> list[CommandFamily]:
    """What the offered vocabulary must keep allowing, by what it is reached for.

    Each family is a parameter, so a project running a different toolchain
    replaces the one family that names it and keeps the seven it shares.
    ``also`` is the other direction, for the families that are nobody else's —
    a project's own toolchain, reached by a name no library rule knows.

    Composed here rather than by a caller spreading this into a list of its
    own, for the reason every other table in a project's vocabulary is: a list
    literal in an application module is a judgement frozen where an adopter
    cannot reach it, and a call is one they pass over.

    ``checkers`` names the three targets ``runner_target_rules`` blesses by
    default, so a project that passed different ones replaces this family
    too — the two are one judgement about which toolchain this is, and a
    corpus asserting `uv run pytest` allows on a project with no pytest is
    asserting something about nothing.
    """
    return [
        CommandFamily(what="reading the checkout", commands=list(reading)),
        CommandFamily(what="searching it", commands=list(searching)),
        CommandFamily(what="asking git what happened", commands=list(git_history)),
        CommandFamily(what="offering work for review", commands=list(offering)),
        CommandFamily(what="running the checkers", commands=list(checkers)),
        CommandFamily(what="writing where nothing is reviewed", commands=list(scratch)),
        CommandFamily(what="shaping output", commands=list(shaping)),
        CommandFamily(what="reading the machine", commands=list(machine)),
        *also,
    ]
