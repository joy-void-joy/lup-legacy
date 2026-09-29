"""Inside the container `outer` measures, a question whose harm stays there is settled.

The launch records what it measured about the session's boundary, and a
session inside a container it measured placing work there runs behind walls
no rule has to stand in for: a killed process, an exported variable, a
package in the image, a clone into a directory the container owns are gone
when it is. So every declared effect says where its harm lands, and one
settlement row allows a judged ask or refusal whose every objecting part
stays inside. What reaches a dependency, a lent credential, lup's own
machinery, a mount the host lent, or anything outside or after the container
keeps its question.

Every changed row is driven the way a session drives it -- each runtime's
generated dispatcher on the payload its harness sends, with no ledger (none),
the runtime's own sandbox (inner), and a measured container (outer) -- and
beside it `dev policy`'s reading of the same placement, which has to agree.
"""

import json
import os
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.devtools.dev.policy_explain import session_placement, verdict_for
from lup.policy.assets.host import (
    container_owned,
    host_held_ports,
    host_shared_roots,
    landed_targets,
    lent_mount_points,
)
from lup.policy.kernel.commands import unread_programs
from lup.policy.kernel.decision import KernelDecision, carrying_readings
from lup.policy.kernel.effects import declare
from lup.policy.kernel.fetch import decide_fetch, loopback_port
from lup.policy.kernel.rows import TargetLandingRow, UrlScopeRow, landing_rows
from lup.policy.kernel.settlement import SettlementFacts, settle
from lup.policy.kernel.shell import decide_shell, shell_posture_targets
from lup.policy.shell_rules import erase_shell_rules
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.native import codex_denial
from tests.unit.repos import initialized_repo

type Runtime = Literal["claude", "codex"]
type Posture = Literal["none", "inner", "outer"]

DISPATCHERS: dict[Runtime, Path] = {
    "claude": Path(".claude/plugins/lup/hooks/scripts/policy.py"),
    "codex": Path(".codex/plugins/lup/hooks/scripts/policy.py"),
}

ROWS = erase_shell_rules(declared_hook_set().resolved_shell_rules())

SETTLED_INSIDE = [
    pytest.param("kill 1234", "ask", id="kill"),
    pytest.param("pkill pytest", "ask", id="pkill"),
    pytest.param("make", "ask", id="make"),
    pytest.param("apt install jq", "ask", id="apt"),
    pytest.param("pacman -S jq", "ask", id="pacman"),
    pytest.param("crontab -l", "ask", id="crontab"),
    pytest.param("export PYTHONPATH=src", "deny", id="export"),
    pytest.param("unset FOO", "deny", id="unset"),
    pytest.param("PYTHONPATH=src uv run pytest -q", "ask", id="pythonpath"),
    pytest.param("chmod +x tmp/x.sh", "ask", id="chmod-checkout"),
    pytest.param("git -c core.pager=less log", "ask", id="git-c-pager"),
    pytest.param("git rebase main -x 'make test'", "ask", id="rebase-exec"),
    pytest.param("sort --compress-program=gzip README.md", "ask", id="sort-program"),
    pytest.param("git grep foo -O", "ask", id="git-grep-pager"),
    pytest.param("codex exec hi", "ask", id="codex-exec"),
    pytest.param("rm -rf /opt/outer-probe", "ask", id="rm-private"),
    pytest.param("ls > /opt/outer-probe.txt", "ask", id="redirect-private"),
    pytest.param(
        "git clone https://github.com/o/r /opt/outer-clone", "ask", id="clone-private"
    ),
    pytest.param("gh repo clone o/r /opt/outer-clone", "ask", id="gh-clone-private"),
    pytest.param(
        "gh release download v1 -D /opt/outer-assets", "ask", id="download-private"
    ),
    pytest.param(
        "uv run lup-devtools harness claude --sandbox none", "ask", id="child-none"
    ),
    pytest.param(
        "uv run lup-devtools harness codex --mount /srv/data", "ask", id="child-mount"
    ),
    # Every command the unread word could make stays inside, so the container
    # settles it exactly as it settles the literal.
    pytest.param("make $TARGET", "ask", id="make-unread-target"),
    pytest.param(
        "$CMD -c core.pager=less push origin feat", "ask", id="unread-command-pager"
    ),
    pytest.param(
        "uv run lup-devtools harness claude --sandbox $X",
        "deny",
        id="child-unread-sandbox",
    ),
]
"""Rows whose harm stays in the container: the host answer, and allow under outer."""

GUARDED = [
    pytest.param("git -c credential.helper=x fetch", "ask", id="git-c-credential"),
    pytest.param("git -c core.sshCommand=x fetch", "ask", id="git-c-ssh"),
    pytest.param("git clone https://github.com/o/r", "ask", id="clone-into-checkout"),
    pytest.param("yay -S foo", "ask", id="aur"),
    pytest.param("npm install left-pad", "ask", id="npm"),
    pytest.param("pip install httpx", "deny", id="pip"),
    pytest.param("cat .env.local", "deny", id="env-local-read"),
    pytest.param("grep KEY .env.production.local", "deny", id="env-mode-local-read"),
    pytest.param("sudo ls", "ask", id="sudo"),
    pytest.param("su -c id root", "ask", id="su"),
    pytest.param("setpriv --reuid=0 --regid=0 --clear-groups id", "ask", id="setpriv"),
    pytest.param("capsh --user=root -- -c id", "ask", id="capsh"),
    pytest.param("unshare -r id", "ask", id="unshare"),
    pytest.param("nsenter -t 1 -m id", "ask", id="nsenter"),
    pytest.param("capsh --print", "allow", id="capsh-report"),
    pytest.param(
        "uv run lup-devtools dev seams --retire dict-get", "ask", id="seams-retire"
    ),
    pytest.param("uv run lup-devtools dev seams --retire-all", "ask", id="seams-all"),
    pytest.param(
        "uv run lup-devtools dev seams --disown README.md", "ask", id="seams-disown"
    ),
    pytest.param("uv run lup-devtools dev seams --keep dict-get", "allow", id="keep"),
    # gh's flag grammar takes a short flag's value attached, so these are
    # the remote branch deletion `-X DELETE` spells apart.
    pytest.param(
        "gh api -XDELETE repos/{owner}/{repo}/git/refs/heads/x",
        "ask",
        id="gh-api-attached-method",
    ),
    pytest.param(
        "gh api -X=DELETE repos/{owner}/{repo}/git/refs/heads/x",
        "ask",
        id="gh-api-attached-equals",
    ),
    pytest.param("gh api -XGET repos/{owner}/{repo}", "allow", id="gh-api-read"),
    # Code another tool runs later, with none of this policy in front of it.
    pytest.param("echo '{}' > .vscode/tasks.json", "ask", id="vscode-task"),
    pytest.param("cp README.md .pre-commit-config.yaml", "ask", id="pre-commit"),
    # Inline code leaves nothing behind to review whatever its arguments turn
    # out to be, so an argument nobody can read does not hand it to a wall.
    pytest.param(
        "files=$(git ls-files) && perl -pi -e 's/a/b/' $files",
        "deny",
        id="perl-inline-bound-operands",
    ),
    pytest.param(
        "perl -pi -e 's/a/b/' $(git ls-files)",
        "deny",
        id="perl-inline-substituted-operands",
    ),
    pytest.param("x=$(ls) && perl -e 'print 1' $x", "deny", id="perl-inline-bound"),
    pytest.param(
        "x=$(ls) && python -c 'print(1)' $x", "deny", id="python-inline-bound"
    ),
    pytest.param("python3 -c 'print(1)' $(ls)", "deny", id="python-inline-substituted"),
    pytest.param(
        "x=$(ls) && node -e 'console.log(1)' $x", "deny", id="node-inline-bound"
    ),
    pytest.param(
        "node -e 'console.log(1)' $(ls)", "deny", id="node-inline-substituted"
    ),
    # An interpreter build arrives from an index and runs everything after it.
    pytest.param("uv python install 3.13", "ask", id="uv-python-install"),
    pytest.param("uv python pin 3.13", "ask", id="uv-python-pin"),
    pytest.param("uv python list", "allow", id="uv-python-list"),
    # An option gh api's screen cannot read could be a method or a body, and
    # either lands on the remote whatever holds the process.
    pytest.param(
        "gh api -iXDELETE repos/{owner}/{repo}/git/refs/heads/x",
        "deny",
        id="gh-api-unread-cluster",
    ),
    pytest.param(
        "install -m644 README.md .lup/preflight/n.json", "ask", id="install-ledger"
    ),
    pytest.param("ssh host.example ls", "ask", id="ssh"),
    pytest.param("export GH_TOKEN=x", "deny", id="export-token"),
    pytest.param("ss -K", "ask", id="ss-kill"),
    pytest.param("PYTHONPATH=x git push --delete origin b", "ask", id="binding-push"),
    pytest.param("kill 1 && git push --delete origin b", "ask", id="kill-then-delete"),
    pytest.param("eval ls", "deny", id="eval"),
    pytest.param("codex l$OP", "ask", id="unread-verb-could-be-login"),
    pytest.param("codex $OP hi", "deny", id="unread-verb-strictest-held-inside"),
    pytest.param(
        "git -c core.pager=less push --force origin feat", "ask", id="pager-force"
    ),
    pytest.param(
        "git -c core.pager=less push --delete origin b", "ask", id="pager-delete"
    ),
    pytest.param(
        "git -c core.pager=less $OP origin --delete b", "deny", id="pager-unread"
    ),
    # An unread argument could be a guarded flag or operand, and a push or a
    # merge it could make lands on the remote whatever holds the process.
    pytest.param("git push $X origin feat", "deny", id="unread-push-flag"),
    pytest.param(
        "git push --force-with-lease origin $X", "deny", id="unread-lease-refspec"
    ),
    pytest.param("git push origin $X", "deny", id="unread-push-refspec"),
    pytest.param("gh pr merge $X", "deny", id="unread-merge-flag"),
    pytest.param("ls && git push $X origin feat", "deny", id="read-then-unread-push"),
    pytest.param("make x && git push $X origin feat", "ask", id="ask-beside-unread"),
    # A substitution result stands where the same words do.
    pytest.param("git push $(cat f) origin feat", "deny", id="substituted-push-word"),
    pytest.param("gh pr merge $(cat f)", "deny", id="substituted-merge-word"),
    # An unread command word could be each program whose verbs follow it.
    pytest.param("$CMD push --force origin feat", "ask", id="unread-command-force"),
    # Found past the program's own globals, which its reading judges too.
    pytest.param(
        "$CMD -c core.sshCommand=x push --force origin feat",
        "ask",
        id="unread-command-behind-guarded-global",
    ),
    pytest.param(
        "$CMD -C /tmp push --force origin feat",
        "ask",
        id="unread-command-behind-value-global",
    ),
    pytest.param(
        "$(which git) push --force origin feat", "ask", id="substituted-command-force"
    ),
]
"""Rows whose harm reaches past the container: the same answer on every posture."""

UNREAD_COMMAND_AS_BEFORE = [
    pytest.param("$EDITOR file", id="editor"),
    pytest.param('"$PYTHON" x.py', id="interpreter"),
    pytest.param("$PAGER out.txt", id="pager"),
    # Each could be a program nothing here asks about, so nothing moves.
    pytest.param("$CMD push origin feat", id="could-be-plain-push"),
    pytest.param("$CMD pr merge 12", id="could-be-plain-merge"),
]
"""Command words nobody can read and no reading objects to: refused with no
boundary, and confined by either."""

UNREAD_BEYOND_THE_CONTAINER = [
    pytest.param("$CMD push $X origin feat", id="could-be-unread-push"),
]
"""A reading reaching past the container: refused there as with no boundary.

The runtime's own sandbox confines a command it cannot name, as it always has,
so the inner posture is not asserted here."""

CHILD_INNER = "uv run lup-devtools harness claude --sandbox inner"
"""A child as confined as its parent, which no posture asks about."""

UNGUARDED_UNREAD = "ls $X"
"""An unread word under a command that guards nothing, which no posture asks about."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A checkout with a measured container around it, named by no nonce yet.

    The ledger lends the checkout and a sibling project, which is what a
    launch's lease names; the sibling is what a path the host lent from
    outside this checkout is measured against.
    """
    work = tmp_path / "checkout"
    initialized_repo(work, tmp_path / "no-hooks")
    (work / "README.md").write_text("probe\n", encoding="utf-8")
    ledger = work / ".lup" / "preflight" / "launch.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "contained": ["yes"],
                "delivered": ["inside_placement", "question_relay"],
                "blocked": ["host_executor"],
                "writable_roots": [str(work), str(tmp_path / "sibling")],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("LUP_BOUNDARY_ROOT", raising=False)
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    monkeypatch.delenv("LUP_SANDBOX_ACTIVE", raising=False)
    yield work


def posture_environment(posture: Posture) -> dict[str, str]:
    """The environment a launch of one posture hands the session it opens."""
    held = {
        name: value
        for name, value in os.environ.items()
        if name not in ("LUP_BOUNDARY_ROOT", "LUP_BOUNDARY_NONCE", "LUP_SANDBOX_ACTIVE")
    }
    match posture:
        case "inner":
            return {**held, "LUP_SANDBOX_ACTIVE": "1"}
        case "outer":
            return {**held, "LUP_BOUNDARY_NONCE": "launch"}
        case "none":
            return held


def met(runtime: Runtime, posture: Posture, command: str, checkout: Path) -> str:
    """The effect a session of one posture meets before the call runs.

    Codex has no ask at this boundary: it parks the question as a review and
    answers with a structured refusal, which is the same question put where
    Codex can carry one. Only exit 2 is a refusal.
    """
    payload: JsonObject = {
        "session_id": "outer-probe",
        "hook_event_name": "PreToolUse",
        "cwd": str(checkout),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(DISPATCHERS[runtime].resolve()),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={
            **posture_environment(posture),
            "PLUGIN_DATA": str(checkout.parent / "plugin-data"),
        },
    )
    assert isinstance(result, sh.RunningCommand)
    if runtime == "codex":
        if result.exit_code == 2:
            return "deny"
        if not result.stdout:
            return "allow"
        codex_denial(result)
        return "ask"
    specific = json.loads(str(result))["hookSpecificOutput"]
    return str(specific["permissionDecision"])


def previewed(
    command: str, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """What `dev policy` answers for each placement, read beside the ledger."""
    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "launch")
    monkeypatch.chdir(checkout)
    verdict = verdict_for(command, "shell", False, checkout, declared_hook_set())
    return {reading.placement: reading.effect for reading in verdict.readings}


def session_previewed(
    command: str, posture: Posture, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """What `dev policy` answers with no placement named, inside one posture."""
    launched = posture_environment(posture)
    for name in ("LUP_SANDBOX_ACTIVE", "LUP_BOUNDARY_NONCE"):
        if name in launched:
            monkeypatch.setenv(name, launched[name])
    monkeypatch.chdir(checkout)
    verdict = verdict_for(
        command,
        "shell",
        False,
        checkout,
        declared_hook_set(),
        [session_placement(checkout)],
    )
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    monkeypatch.delenv("LUP_SANDBOX_ACTIVE", raising=False)
    (reading,) = verdict.readings
    return reading.effect


@pytest.mark.parametrize(("command", "host"), SETTLED_INSIDE)
def test_a_question_whose_harm_stays_inside_is_settled_only_by_the_container(
    runtime: Runtime,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    host: str,
) -> None:
    met_under = {
        posture: met(runtime, posture, command, checkout)
        for posture in ("none", "inner", "outer")
    }
    host_answer = "ask" if runtime == "codex" and host == "ask" else host

    assert met_under == {"none": host_answer, "inner": host_answer, "outer": "allow"}
    assert previewed(command, checkout, monkeypatch) == {
        "none": host,
        "inner": host,
        "outer": "allow",
    }


@pytest.mark.parametrize(("command", "host"), GUARDED)
def test_a_question_whose_harm_reaches_past_the_container_keeps_it(
    runtime: Runtime,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    host: str,
) -> None:
    met_under = {
        posture: met(runtime, posture, command, checkout)
        for posture in ("none", "inner", "outer")
    }

    assert set(met_under.values()) == {host}
    assert set(previewed(command, checkout, monkeypatch).values()) == {host}


REVIEW_QUEUE_WRITES = [
    pytest.param("echo '{}' >> .lup/questions.jsonl", id="append-relay"),
    pytest.param("tee -a .lup/questions.jsonl < README.md", id="tee-relay"),
    pytest.param("cp README.md .lup/questions.jsonl", id="copy-over-relay"),
    pytest.param("touch .lup/review-claims/fresh", id="claim-created"),
    pytest.param("rm .lup/review-claims/spent", id="claim-retired"),
    pytest.param("mkdir -p .lup/review-stage-claims/spent/next", id="stage-claimed"),
]
"""A session writing the review queue its hooks keep, by every route a file takes.

The Codex hook releases a parked call for an approved row whose principal is
neither the session nor its requester, and spends a claim file to release it
once. A session free to append that row, or to retire the claim, answers its
own question."""


@pytest.mark.parametrize("command", REVIEW_QUEUE_WRITES)
def test_a_session_writing_its_own_review_queue_is_asked_on_every_posture(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    lup = checkout / ".lup"
    (lup / "questions.jsonl").write_text("{}\n", encoding="utf-8")
    (lup / "review-claims").mkdir()
    (lup / "review-claims" / "spent").write_text("{}", encoding="utf-8")
    (lup / "review-stage-claims" / "spent").mkdir(parents=True)
    postures: tuple[Posture, ...] = ("none", "inner", "outer")

    assert {met(runtime, posture, command, checkout) for posture in postures} == {"ask"}
    assert set(previewed(command, checkout, monkeypatch).values()) == {"ask"}


@pytest.mark.parametrize(
    ("command", "captured"),
    [
        pytest.param("rm -rf state", False, id="ignored-directory"),
        pytest.param("rm state/run.json", False, id="ignored-file"),
        pytest.param("mv state/run.json tmp/run.json", False, id="ignored-moved-away"),
        pytest.param("rm notes.txt", True, id="untracked-file"),
    ],
)
def test_a_capture_claims_only_what_it_holds(
    runtime: Runtime,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    captured: bool,
) -> None:
    """The undo snapshot takes what Git would take, and Git ignores `state/`.

    So a loss there is one no capture holds, and "captured and restorable" is
    a sentence about a file no snapshot has ever seen. An untracked file Git
    does not ignore is taken, and its loss stays settled.
    """
    (checkout / ".gitignore").write_text("state/\n", encoding="utf-8")
    (checkout / "state").mkdir()
    (checkout / "state" / "run.json").write_text("{}\n", encoding="utf-8")
    (checkout / "notes.txt").write_text("draft\n", encoding="utf-8")
    postures: tuple[Posture, ...] = ("none", "inner", "outer")
    expected = "allow" if captured else "ask"
    # Codex parks a question with the preimage of every file it names, and a
    # directory has none to hold, so it refuses the call it cannot park.
    parked = (
        "deny"
        if runtime == "codex" and expected == "ask" and command.startswith("rm -rf")
        else expected
    )

    assert {met(runtime, posture, command, checkout) for posture in postures} == {
        parked
    }
    assert set(previewed(command, checkout, monkeypatch).values()) == {expected}


def test_a_target_the_host_lent_from_outside_the_checkout_keeps_the_question(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mode bits on a sibling project are that project's, whatever wall stands."""
    command = f"chmod 777 {checkout.parent / 'sibling' / 'x'}"

    assert met(runtime, "outer", command, checkout) == "ask"
    assert previewed(command, checkout, monkeypatch)["outer"] == "ask"


@pytest.mark.parametrize(
    "spelled",
    [
        pytest.param("ls > {tree}/x.txt", id="redirect"),
        pytest.param("date >> {tree}/log.txt", id="append"),
        pytest.param("ls | tee {tree}/x.txt", id="tee"),
    ],
)
def test_a_write_the_host_lent_from_outside_the_checkout_keeps_the_question(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, spelled: str
) -> None:
    """Output landing in a tree the host lent is that tree's, whatever wall stands.

    The container holds a file under a directory the host never lent, and
    nothing under one it did: those bytes are on the host the moment they are
    written. The tree stands outside the temporary root, which is disposable
    wherever it is.
    """
    lent = "/srv/lent-tree"
    ledger = checkout / ".lup" / "preflight" / "launch.json"
    measured = json.loads(ledger.read_text(encoding="utf-8"))
    ledger.write_text(
        json.dumps({**measured, "writable_roots": [*measured["writable_roots"], lent]}),
        encoding="utf-8",
    )
    command = spelled.format(tree=lent)

    assert met(runtime, "outer", command, checkout) == "ask"
    assert previewed(command, checkout, monkeypatch)["outer"] == "ask"


@pytest.mark.parametrize(
    "spelled",
    [
        pytest.param("git --work-tree={tree} reset --hard", id="reset-work-tree"),
        pytest.param("git -C {tree} reset --hard", id="reset-C"),
        pytest.param("git --work-tree={tree} restore README.md", id="restore"),
        pytest.param(
            "git --work-tree {tree} restore --source=HEAD README.md",
            id="restore-source",
        ),
        pytest.param("git --work-tree={tree} clean -fd", id="clean"),
    ],
)
def test_git_moved_into_a_lent_tree_keeps_the_question_there(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, spelled: str
) -> None:
    """A work tree git is pointed at is where its loss lands, not this checkout.

    A capture of the checkout holds none of the tree, and the container holds
    none of what the host lent it, so no wall settles the question. The tree
    stands outside the temporary root, which is disposable wherever it is.
    """
    lent = "/srv/lent-tree"
    ledger = checkout / ".lup" / "preflight" / "launch.json"
    measured = json.loads(ledger.read_text(encoding="utf-8"))
    ledger.write_text(
        json.dumps({**measured, "writable_roots": [*measured["writable_roots"], lent]}),
        encoding="utf-8",
    )
    command = spelled.format(tree=lent)
    postures: tuple[Posture, ...] = ("none", "inner", "outer")

    assert {met(runtime, posture, command, checkout) for posture in postures} == {"ask"}
    assert set(previewed(command, checkout, monkeypatch).values()) == {"ask"}


@pytest.mark.parametrize(
    "spelled",
    [
        pytest.param("git checkout HEAD -- .", id="dot"),
        pytest.param("git checkout HEAD -- ./README.md", id="dot-relative"),
        pytest.param("git checkout HEAD -- README.md", id="relative"),
        pytest.param("git checkout HEAD -- {checkout}", id="absolute-root"),
        pytest.param("git checkout HEAD -- {checkout}/README.md", id="absolute"),
    ],
)
def test_a_checkout_from_a_ref_is_refused_however_its_path_is_spelled(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, spelled: str
) -> None:
    """This project checks out through `git switch` and `git restore`.

    A path spelled from the checkout was granted as a restore from a ref while
    the same path spelled absolutely was refused, so which answer a session
    met turned on how the operand was written. Refused whatever the spelling,
    and pointed at the `git restore --source` that does the same.
    """
    command = spelled.format(checkout=checkout)
    postures: tuple[Posture, ...] = ("none", "inner", "outer")

    assert {met(runtime, posture, command, checkout) for posture in postures} == {
        "deny"
    }
    assert set(previewed(command, checkout, monkeypatch).values()) == {"deny"}


@pytest.mark.parametrize(
    ("command", "standing"),
    [
        pytest.param("git --work-tree=. reset --hard", "git reset --hard", id="reset"),
        pytest.param(
            "git --work-tree=. restore --source=HEAD README.md",
            "git restore --source=HEAD README.md",
            id="restore-source",
        ),
    ],
)
def test_a_work_tree_git_stands_in_moves_nothing(
    runtime: Runtime,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    standing: str,
) -> None:
    postures: tuple[Posture, ...] = ("none", "inner", "outer")
    meets = {posture: met(runtime, posture, standing, checkout) for posture in postures}

    assert {
        posture: met(runtime, posture, command, checkout) for posture in postures
    } == meets
    assert previewed(command, checkout, monkeypatch) == previewed(
        standing, checkout, monkeypatch
    )


def test_a_setting_a_container_holds_leaves_the_verb_its_landing(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pager beside a clone is read past, so the clone's lent target is placed."""
    target = checkout.parent / "sibling" / "r"
    command = f"git -c core.pager=less clone https://github.com/o/r {target}"

    assert met(runtime, "outer", command, checkout) == "ask"
    assert previewed(command, checkout, monkeypatch)["outer"] == "ask"


def test_a_child_as_confined_as_its_parent_asks_on_no_posture(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert {
        met(runtime, posture, CHILD_INNER, checkout)
        for posture in ("none", "inner", "outer")
    } == {"allow"}
    assert set(previewed(CHILD_INNER, checkout, monkeypatch).values()) == {"allow"}


def test_an_unread_word_under_a_command_guarding_nothing_asks_on_no_posture(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert {
        met(runtime, posture, UNGUARDED_UNREAD, checkout)
        for posture in ("none", "inner", "outer")
    } == {"allow"}
    assert set(previewed(UNGUARDED_UNREAD, checkout, monkeypatch).values()) == {"allow"}


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("kill 1234", id="settled-inside"),
        pytest.param("pip install httpx", id="refused"),
        pytest.param("frobnicate", id="unjudged"),
        pytest.param("git status", id="read"),
        pytest.param("uv run pip install httpx", id="uv-run-pip"),
        pytest.param("cat .env.local", id="withheld"),
    ],
)
def test_dev_policy_answers_as_the_session_it_runs_in(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Named no placement, `dev policy` reads the ledger its dispatcher reads."""
    postures: tuple[Posture, ...] = ("none", "inner", "outer")
    # The dispatchers first: a preview stands in the checkout, and the
    # dispatcher scripts are found from the repository.
    meets = {posture: met(runtime, posture, command, checkout) for posture in postures}
    assert {
        posture: session_previewed(command, posture, checkout, monkeypatch)
        for posture in postures
    } == meets


@pytest.mark.parametrize("command", ["cat .env", "cat .env.example"])
def test_a_committed_environment_file_reads_on_every_posture(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Only the gitignored locals hold a key; the committed defaults name none."""
    assert {
        met(runtime, posture, command, checkout)
        for posture in ("none", "inner", "outer")
    } == {"allow"}
    assert set(previewed(command, checkout, monkeypatch).values()) == {"allow"}


@pytest.mark.parametrize(
    ("command", "program"),
    [
        pytest.param("uv run pip install httpx", "pip install httpx", id="pip"),
        pytest.param("uv -q run pip install httpx", "pip install httpx", id="uv-q"),
        pytest.param("uv run python -c 'x'", "python -c 'x'", id="python-inline"),
        pytest.param("uv run git status", "git status", id="git-status"),
        pytest.param("uv run rm -rf /opt/probe", "rm -rf /opt/probe", id="rm"),
        pytest.param(
            "uv --directory /opt run rm -rf probe", "rm -rf /opt/probe", id="moved-rm"
        ),
        pytest.param(
            "uv run git push --force origin feat",
            "git push --force origin feat",
            id="force-push",
        ),
    ],
)
def test_a_program_uv_runs_answers_as_itself(
    runtime: Runtime,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    program: str,
) -> None:
    """`uv run` puts this project's environment on the path and judges nothing."""
    postures: tuple[Posture, ...] = ("none", "inner", "outer")
    assert {
        posture: met(runtime, posture, command, checkout) for posture in postures
    } == {posture: met(runtime, posture, program, checkout) for posture in postures}
    assert previewed(command, checkout, monkeypatch) == previewed(
        program, checkout, monkeypatch
    )


@pytest.mark.parametrize("command", UNREAD_COMMAND_AS_BEFORE)
def test_an_unread_command_word_no_reading_objects_to_is_answered_as_before(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    expected: dict[Posture, str] = {"none": "deny", "inner": "allow", "outer": "allow"}

    assert {
        posture: met(runtime, posture, command, checkout) for posture in expected
    } == expected
    assert previewed(command, checkout, monkeypatch) == expected


@pytest.mark.parametrize("command", UNREAD_BEYOND_THE_CONTAINER)
def test_an_unread_command_word_reaching_past_the_container_is_refused_in_it(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    met_under = {
        posture: met(runtime, posture, command, checkout)
        for posture in ("none", "outer")
    }
    preview = previewed(command, checkout, monkeypatch)

    assert met_under == {"none": "deny", "outer": "deny"}
    assert (preview["none"], preview["outer"]) == ("deny", "deny")


def test_a_loopback_port_this_container_holds_is_its_own(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server the session started, measured by the process that holds it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        command = f"curl -s http://127.0.0.1:{port}/status"

        assert port not in host_held_ports(Path("/proc"))
        assert met(runtime, "outer", command, checkout) == "allow"
        assert previewed(command, checkout, monkeypatch)["outer"] == "allow"


def contained_decision(
    command: str, landings: list[TargetLandingRow], sandboxed: bool = False
) -> KernelDecision:
    """One command in a measured container, with where its paths were measured."""
    return decide_shell(
        command,
        ROWS,
        sandboxed=sandboxed,
        contained=not sandboxed,
        inside_placement=not sandboxed,
        existing_targets=[],
        landings=landings,
    )


def test_the_row_reads_only_the_measured_container() -> None:
    """The runtime's own sandbox leaves the operator's machine reachable."""
    assert contained_decision("kill 1234", []).effect == "allow"
    assert contained_decision("kill 1234", [], sandboxed=True).effect == "ask"
    assert decide_shell("kill 1234", ROWS).effect == "ask"


def test_the_settled_verdict_says_why_and_keeps_its_rule() -> None:
    settled = contained_decision("kill 1234", [])

    assert settled.rule == "shell:kill"
    assert settled.reason.endswith("stays inside this container")
    assert settled.purpose is None


def test_harm_on_paths_settles_only_where_every_path_is_the_containers() -> None:
    private = [TargetLandingRow(path="/opt/x", lands="container")]
    checkout = [TargetLandingRow(path="notes.md", lands="checkout")]

    assert contained_decision("rm -rf /opt/x", private).effect == "allow"
    assert contained_decision("rm -rf /opt/x", [*private, *checkout]).effect == "ask"
    assert contained_decision("rm -rf /opt/x", []).effect == "ask"


def test_harm_inside_is_kept_where_a_named_path_is_lent_from_elsewhere() -> None:
    lent = [TargetLandingRow(path="/srv/other/x", lands="host")]

    assert contained_decision("chmod +x /srv/other/x", lent).effect == "ask"


def test_an_escalation_keeps_the_question_it_asked_for() -> None:
    escalated = contained_decision("# lup: escalate[decision]: checking\nkill 1", [])

    assert escalated.effect == "ask"
    assert escalated.escalated == "checking"


def test_a_verdict_nobody_placed_keeps_its_question() -> None:
    """Code that declared no effect stated no reach, and so relaxes nowhere."""
    judged = KernelDecision("ask", "a judged question with no effect behind it")

    settled = settle(SettlementFacts(judged, contained=True, inside_placement=True))

    assert settled.effect == "ask"


def test_a_verdict_wider_than_its_parts_keeps_its_question() -> None:
    """A reach widened past what the parts name is read, not only the parts."""
    part = KernelDecision(
        "ask", "a pager runs where the session runs", reach="container"
    )
    widened = part.revised(findings=(part, part), reach="credential")

    settled = settle(SettlementFacts(widened, contained=True, inside_placement=True))

    assert settled.effect == "ask"


def test_a_composed_line_carries_every_part_it_objected_to() -> None:
    """A join is itself joined, so each level carries all of what its parts asked."""
    composed = contained_decision("PYTHONPATH=x git push --delete origin b", [])

    assert composed.effect == "ask"
    assert composed.reach is None


def test_an_unread_argument_carries_the_reach_of_every_command_it_could_make() -> None:
    """Settled inside only where every reading's harm would stay there."""
    assert contained_decision("git push $X origin feat", []).effect == "deny"
    assert contained_decision("rg $X foo", []).effect == "allow"
    assert contained_decision("rg $X foo", [], sandboxed=True).effect == "allow"


def test_an_unread_deferral_settles_only_on_a_reach_it_stated_inside() -> None:
    """A reading that stated no reach keeps the refusal; unjudged work does not."""
    unread = KernelDecision(
        "defer", "an argument nobody read", abstention="boundary_settle", unread=True
    )
    unjudged = KernelDecision("defer", "nobody looked", abstention="boundary_settle")
    facts = [
        SettlementFacts(decision, contained=True, inside_placement=True)
        for decision in (unread, unread.revised(reach="container"), unjudged)
    ]

    assert [settle(fact).effect for fact in facts] == ["deny", "allow", "allow"]


def test_a_deferral_carries_its_readings_only_where_one_objects() -> None:
    """A word every reading of which is allowed stays work nobody judged."""
    deferral = KernelDecision("defer", "nobody read it", abstention="boundary_settle")
    allowed = KernelDecision("allow", "reads")
    pushed = KernelDecision("ask", "pushes", reach="host_later")

    assert not carrying_readings(deferral, (allowed,)).unread
    carried = carrying_readings(deferral, (allowed, pushed))
    assert (carried.unread, carried.reach) == (True, "host_later")


def test_an_unread_command_word_could_be_each_program_its_verbs_name() -> None:
    assert "git" in unread_programs(["$CMD", "push", "origin", "feat"], ROWS)
    assert "gh" in unread_programs(["$CMD", "pr", "merge", "12"], ROWS)
    assert unread_programs(["$EDITOR", "file"], ROWS) == []
    assert unread_programs(["git", "push", "origin", "feat"], ROWS) == []


def test_an_unread_command_word_is_found_past_the_programs_own_globals() -> None:
    guarded = ["$CMD", "-c", "core.sshCommand=x", "push", "--force", "origin", "b"]

    assert "git" in unread_programs(guarded, ROWS)
    assert "git" in unread_programs(["$CMD", "-C", "/tmp", "push"], ROWS)


def test_an_unread_command_word_takes_the_strictest_program_it_could_be() -> None:
    forced = contained_decision("$CMD push --force origin feat", [])

    assert forced.effect == "ask"
    assert "`$CMD` could not be read and could be `git`" in forced.reason
    assert contained_decision("$CMD push origin feat", []).effect == "allow"


def test_a_reach_outside_the_vocabulary_is_refused_where_it_is_declared() -> None:
    with pytest.raises(ValueError, match="reaches no 'nowhere'"):
        declare("changes_nothing", reach="nowhere")


def test_each_member_places_its_harm_where_its_kind_lands() -> None:
    assert declare("destroys_uncaptured", scope="targeted")["reach"] == "mount"
    assert declare("reads_path", scope="secret")["reach"] == "credential"
    assert declare("writes_path", scope="protected")["reach"] == "lup"
    assert declare("installs_dependency", scope="x")["reach"] == "dependency"
    assert declare("fetches", scope="declared")["reach"] == "host_later"


MOUNTINFO = "\n".join(
    [
        "1 0 0:1 / / rw - overlay overlay rw",
        "2 1 0:2 /volumes/cache/_data /cache/uv rw - ext4 /dev/sda1 rw",
        "3 1 0:3 / /proc rw - proc proc rw",
        "4 1 0:4 /home/u/My\\040Project /work/My\\040Project rw - ext4 /dev/sda1 rw",
        "5 1 0:5 /lup-wake /tmp/lup-wake rw - tmpfs tmpfs rw",
    ]
)


def test_a_bind_is_lent_and_a_whole_filesystem_is_the_containers() -> None:
    assert lent_mount_points(MOUNTINFO) == [
        "/cache/uv",
        "/work/My Project",
        "/tmp/lup-wake",
    ]


def test_the_lease_expands_home_and_leaves_the_containers_temporary_root() -> None:
    shared = host_shared_roots(
        {"writable_roots": ["/repo", "~/.cache/uv", "/tmp"]}, ["/cfg"], "/home/agent"
    )

    assert shared == ["/repo", "/home/agent/.cache/uv", "/cfg"]


def test_each_target_lands_in_the_checkout_on_a_lent_root_or_in_the_container(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    lent = tmp_path / "lent"

    placed = landed_targets(
        ["notes.md", str(lent / "x"), "/opt/x", "$HOME/x"], [str(lent)], checkout
    )

    assert landing_rows(placed) == [
        TargetLandingRow(path="notes.md", lands="checkout"),
        TargetLandingRow(path=str(lent / "x"), lands="host"),
        TargetLandingRow(path="/opt/x", lands="container"),
        TargetLandingRow(path="$HOME/x", lands="host"),
    ]


def test_a_path_no_root_or_lent_mount_covers_is_the_measured_containers(
    tmp_path: Path,
) -> None:
    """What lifts the lease's refusal of an edit outside it, and only inside."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(MOUNTINFO, encoding="utf-8")
    measured = {
        "contained": ["yes"],
        "delivered": ["inside_placement"],
        "writable_roots": ["/repo"],
    }

    assert container_owned(Path("/opt/notes.md"), measured, mountinfo)
    assert not container_owned(Path("/cache/uv/x"), measured, mountinfo)
    assert not container_owned(Path("/repo/x"), measured, mountinfo)
    assert not container_owned(
        Path("/opt/notes.md"), {**measured, "delivered": []}, mountinfo
    )
    assert not container_owned(Path("/opt/notes.md"), measured, tmp_path / "gone")


def test_a_write_the_lease_does_not_cover_is_refused_unless_the_container_owns_it(
    checkout: Path, tmp_path: Path
) -> None:
    """The edit a session makes outside its lease, through the deployed dispatcher.

    Refused on a host lent from elsewhere -- here the ledger's own read-only
    hole -- and handed to the ordinary edit gates where the path is the
    container's.
    """
    ledger = checkout / ".lup" / "preflight" / "launch.json"
    measured = json.loads(ledger.read_text(encoding="utf-8"))
    ledger.write_text(
        json.dumps({**measured, "read_only_roots": [str(checkout / "held")]}),
        encoding="utf-8",
    )

    def written(path: str) -> str:
        payload: JsonObject = {
            "session_id": "outer-probe",
            "hook_event_name": "PreToolUse",
            "cwd": str(checkout),
            "tool_name": "Write",
            "tool_input": {"file_path": path, "content": "note\n"},
        }
        result = sh.Command(sys.executable)(
            "-I",
            "-S",
            str(DISPATCHERS["claude"].resolve()),
            _in=json.dumps(payload),
            _env={
                **posture_environment("outer"),
                "PLUGIN_DATA": str(tmp_path / "plugin-data"),
            },
        )
        return str(
            json.loads(str(result))["hookSpecificOutput"]["permissionDecisionReason"]
        )

    assert "writable boundary" in written(str(checkout / "held" / "x.md"))
    assert "writable boundary" not in written("/opt/outer-probe/notes.md")


def test_a_malformed_landing_is_read_as_the_host() -> None:
    assert landing_rows([["x", "elsewhere"], ["y"]]) == [
        TargetLandingRow(path="x", lands="host"),
        TargetLandingRow(path="$", lands="host"),
    ]


def test_a_clone_names_where_it_lands_and_an_unreadable_cd_names_nowhere() -> None:
    assert "/opt/r" in shell_posture_targets(
        "git clone https://github.com/o/r /opt/r", ROWS
    )
    assert shell_posture_targets("git clone https://github.com/o/r", ROWS) == ["."]
    assert shell_posture_targets("gh release download v1 --dir=/opt/a", ROWS) == [
        "/opt/a"
    ]
    assert "$" in shell_posture_targets("cd $X && rm -rf a", ROWS)


SOCKET_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when"
    " retrnsmt   uid  timeout inode\n"
)


def listening_record(port: int, inode: str) -> str:
    """One listening record in the socket table's own layout."""
    return (
        f"   0: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000"
        f" 00000000  1000        0 {inode} 1 0000000000000000 100 0 0 10 0\n"
    )


def test_a_listener_no_visible_process_holds_is_the_hosts(tmp_path: Path) -> None:
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(
        SOCKET_HEADER + listening_record(8765, "111") + listening_record(9000, "222"),
        encoding="utf-8",
    )
    descriptors = tmp_path / "42" / "fd"
    descriptors.mkdir(parents=True)
    (descriptors / "3").symlink_to("socket:[111]")

    assert host_held_ports(tmp_path) == [9000]


def test_an_unreadable_socket_table_withholds_every_port(tmp_path: Path) -> None:
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").mkdir()

    assert 8765 in host_held_ports(tmp_path)


LOOPBACK = UrlScopeRow(
    scheme="http",
    host="127.0.0.1",
    port=None,
    any_port=True,
    include_subdomains=False,
    path_prefix="",
    reason="this machine's own pages",
)


def test_a_loopback_port_a_host_process_holds_is_outside_the_declared_scope() -> None:
    url = "http://127.0.0.1:9000/status"

    assert loopback_port(url) == 9000
    assert loopback_port("https://localhost/x") == 443
    assert loopback_port("https://example.org/x") is None
    assert decide_fetch(url, [LOOPBACK], []).effect == "allow"
    assert decide_fetch(url, [LOOPBACK], [], host_listener=True).effect == "ask"
    assert decide_fetch(url, [], [], "defer", host_listener=True).effect == "ask"


def test_a_handoff_to_the_runtime_outlasts_a_question_the_container_settles() -> None:
    """A contained kill beside a fetch no scope names leaves the fetch to the runtime.

    Settled whole, the container's answer to the kill allowed the fetch too,
    which nobody but the runtime was ever to answer.
    """
    for line in (
        "curl https://unlisted.example/x && kill 1234",
        "kill 1234 && curl https://unlisted.example/x",
    ):
        settled = decide_shell(
            line,
            ROWS,
            contained=True,
            inside_placement=True,
            existing_targets=[],
            landings=[],
            unscoped_fetch="defer",
        )

        assert (settled.effect, settled.abstention) == ("defer", "provider_native")
    assert contained_decision("kill 1234", []).effect == "allow"


def test_a_capture_never_retires_a_question_asked_of_a_word_nobody_read() -> None:
    """`$X rm tmp/x.txt` could be the `git rm` a capture restores, or anything else."""

    def captured(line: str) -> str:
        return decide_shell(line, ROWS, existing_targets=[], recovered=True).effect

    assert captured("git rm tmp/x.txt") == "allow"
    assert captured("$X rm tmp/x.txt") == "ask"
