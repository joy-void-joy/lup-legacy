"""A repository nested under this checkout's scratch is scratch, on every surface.

A probe kit is a throwaway project under `tmp/` given its own `git init`, so a
runtime launched inside it takes the kit as its project root rather than this
repository. That `.git` is also what the foreign-repository referral reads,
and it made every file in the kit "a different repository": each edit, each
redirect into it and each command's output landing there put a question to
the operator about a file nothing reviews — on both runtimes, and in the
preview that answers for them.

Every surface is driven here the way a session drives it — each runtime's
generated dispatcher run on the payload its harness sends, and `dev policy`'s
own reading — against one layout holding the kit and every repository that
keeps its question: one nested in the checkout outside any scratch root, one
beside the checkout, the same one reached through a `refs/` link, and a kit
under a sibling worktree's scratch.

The kit's own plugin is the same case one refusal later. A probe kit carries
a hand-written `.claude/plugins/` or `.codex/plugins/` of its own, and the
refusal meant for this project's compiled trees read the path alone, so the
probe could not be built. Scratch here holds no generated tree, which the last
case pins against the recipes themselves; every tree outside it keeps the
refusal, including this checkout's own reached through a link planted in
scratch.
"""

import json
import os
import sys
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.devtools.dev.policy_explain import verdict_for
from lup.harness.enforcement import declared_role_rows
from lup.policy.assets.host import this_checkout_path
from lup.policy.kernel.roles import declared_scratch
from lup.policy.models import EditBatch, EditChange, ShellCommand
from lup.policy.rules import ShellPolicy
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from lup_template.harness.composition import TARGETS
from tests.unit.native import claude_effect, codex_denial
from tests.unit.repos import commit_file, initialized_repo

type Runtime = Literal["claude", "codex"]

DISPATCHERS: dict[Runtime, Path] = {
    "claude": Path(".claude/plugins/lup/hooks/scripts/policy.py"),
    "codex": Path(".codex/plugins/lup/hooks/scripts/policy.py"),
}

PREIMAGE = "value = 1"

REFUSED = "from typing import Any"
"""A line production refuses, so an allow is scratch answering, not a pass."""

KIT = "checkout/tmp/kit/probe.py"
"""The file this is about: inside a probe kit, under the checkout's `tmp/`."""

KEEPS_ITS_QUESTION = [
    pytest.param("checkout/vendor/lib/probe.py", id="nested-outside-scratch"),
    pytest.param("elsewhere/src/probe.py", id="beside-the-checkout"),
    pytest.param("elsewhere/tmp/probe.py", id="another-repositorys-own-tmp"),
    pytest.param("checkout/refs/elsewhere/src/probe.py", id="refs-link"),
    pytest.param("checkout/refs/elsewhere/tmp/probe.py", id="refs-link-into-tmp"),
    pytest.param("sibling/tmp/kit/probe.py", id="sibling-worktree-scratch"),
]
"""Every repository the checkout's scratch does not hold, spelled from the base.

`elsewhere/tmp/` is the one a precedence read off the wrong spelling would
open: against its own checkout it reads `tmp/probe.py`, which is exactly how
this repository declares scratch.
"""

KIT_PLUGINS = [
    pytest.param("checkout/tmp/kit/.claude/plugins/p/x.md", id="claude-plugin"),
    pytest.param("checkout/tmp/kit/.codex/plugins/p/x.md", id="codex-plugin"),
]
"""A probe kit's own hand-written plugin, under each runtime's plugin root."""

GENERATED_ELSEWHERE = [
    pytest.param("checkout/.claude/plugins/lup/x.md", id="this-checkouts-claude-tree"),
    pytest.param("checkout/.codex/plugins/lup/x.md", id="this-checkouts-codex-tree"),
    pytest.param(
        "checkout/tmp/linked/.claude/plugins/lup/x.md", id="linked-from-scratch"
    ),
    pytest.param("elsewhere/.claude/plugins/p/x.md", id="another-repositorys-tree"),
    pytest.param("elsewhere/tmp/.claude/plugins/p/x.md", id="another-repositorys-tmp"),
    pytest.param("sibling/tmp/kit/.claude/plugins/p/x.md", id="sibling-scratch"),
]
"""Every plugin tree the checkout's own scratch does not hold, spelled from the base.

`checkout/tmp/linked/.claude` is a link into this checkout's real `.claude`:
spelled under scratch, landing in the generated tree, which is the spelling
the exception must not be read off.
"""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """The checkout a session works in, and every repository around it."""
    hooks = tmp_path / "no-hooks"
    checkout = tmp_path / "checkout"
    git = initialized_repo(checkout, hooks)
    commit_file(git, checkout, "README.md", "checkout\n", "chore: base")
    git("worktree", "add", "-b", "sibling", str(tmp_path / "sibling"))
    for nested in (
        "checkout/tmp/kit",
        "checkout/vendor/lib",
        "elsewhere",
        "sibling/tmp/kit",
    ):
        initialized_repo(tmp_path / nested, hooks)
    (checkout / "refs").mkdir()
    (checkout / "refs" / "elsewhere").symlink_to(tmp_path / "elsewhere")
    for held in (
        KIT,
        "checkout/vendor/lib/probe.py",
        "elsewhere/src/probe.py",
        "elsewhere/tmp/probe.py",
        "sibling/tmp/kit/probe.py",
        "checkout/tmp/kit/.claude/plugins/p/x.md",
        "checkout/tmp/kit/.codex/plugins/p/x.md",
        "checkout/.claude/plugins/lup/x.md",
        "checkout/.codex/plugins/lup/x.md",
        "elsewhere/.claude/plugins/p/x.md",
        "elsewhere/tmp/.claude/plugins/p/x.md",
        "sibling/tmp/kit/.claude/plugins/p/x.md",
    ):
        (tmp_path / held).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / held).write_text(f"{PREIMAGE}\n", encoding="utf-8")
    (checkout / "tmp" / "linked").mkdir()
    (checkout / "tmp" / "linked" / ".claude").symlink_to(checkout / ".claude")
    return tmp_path


def dispatched(runtime: Runtime, payload: JsonObject, base: Path) -> sh.RunningCommand:
    """One runtime's generated dispatcher, run on a payload as its harness runs it."""
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(DISPATCHERS[runtime].resolve()),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={**os.environ, "PLUGIN_DATA": str(base / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    return result


def verdict(runtime: Runtime, payload: JsonObject, base: Path) -> tuple[str, str]:
    """The effect a session meets before the call runs, and the reason it reads.

    Codex has no ask at this boundary: it parks the question as a review and
    answers with a structured refusal naming who can release it, which is the
    same question put where Codex can carry one. Only exit 2 is a refusal.
    """
    result = dispatched(runtime, payload, base)
    if runtime == "codex":
        if result.exit_code == 2:
            return "deny", result.stderr.decode()
        if not result.stdout:
            return "allow", ""
        return "ask", codex_denial(result)
    answered = json.loads(str(result))
    specific = answered["hookSpecificOutput"]
    return claude_effect(answered), str(
        specific["permissionDecisionReason"]
        if "permissionDecisionReason" in specific
        else ""
    )


def session(base: Path) -> str:
    """Where every call here is made from: the checkout holding the kit."""
    return str(base / "checkout")


def edit(runtime: Runtime, target: Path, base: Path) -> JsonObject:
    """One edit of *target* that production refuses, as each runtime carries it."""
    call: JsonObject = (
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": PREIMAGE,
                "new_string": REFUSED,
            },
        }
        if runtime == "claude"
        else {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": (
                    f"*** Begin Patch\n*** Update File: {target}\n"
                    f"@@\n-{PREIMAGE}\n+{REFUSED}\n*** End Patch"
                )
            },
        }
    )
    return {
        "session_id": "kit-probe",
        "hook_event_name": "PreToolUse",
        "cwd": session(base),
        **call,
    }


def created(runtime: Runtime, target: Path, base: Path) -> JsonObject:
    """One new file at *target*, whole, as each runtime carries a creation.

    Production asks about any file arriving whole, so an allow here is the
    scratch role answering for the file rather than the size of the change.
    """
    lines = [REFUSED, "value: Any = 1"]
    call: JsonObject = (
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "\n".join(lines)},
        }
        if runtime == "claude"
        else {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "\n".join(
                    [
                        "*** Begin Patch",
                        f"*** Add File: {target}",
                        *(f"+{line}" for line in lines),
                        "*** End Patch",
                    ]
                )
            },
        }
    )
    return {
        "session_id": "kit-probe",
        "hook_event_name": "PreToolUse",
        "cwd": session(base),
        **call,
    }


def shell(event: str, command: str, base: Path) -> JsonObject:
    """One shell call at *event*, as both runtimes carry it."""
    return {
        "session_id": "kit-probe",
        "hook_event_name": event,
        "cwd": session(base),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def reported(runtime: Runtime, command: str, base: Path) -> str:
    """What one runtime's review of a command that already ran tells the agent.

    Both channels: what still refuses, and what is only worth knowing.
    """
    result = dispatched(runtime, shell("PostToolUse", command, base), base)
    if runtime == "codex" and result.exit_code == 2:
        return result.stderr.decode()
    printed = str(result).strip()
    answered = json.loads(printed) if printed else {}
    specific = (
        answered["hookSpecificOutput"] if "hookSpecificOutput" in answered else {}
    )
    return "\n".join(
        [
            *([str(answered["reason"])] if "reason" in answered else []),
            *(
                [str(specific["additionalContext"])]
                if "additionalContext" in specific
                else []
            ),
        ]
    )


def test_an_edit_inside_a_kit_under_scratch_is_scratch(
    runtime: Runtime, base: Path
) -> None:
    assert verdict(runtime, edit(runtime, base / KIT, base), base)[0] == "allow"


def test_a_file_created_inside_a_kit_under_scratch_is_scratch(
    runtime: Runtime, base: Path
) -> None:
    inside = created(runtime, base / "checkout/tmp/kit/hooks/record.py", base)
    beside = created(runtime, base / "elsewhere/new.py", base)

    assert verdict(runtime, inside, base)[0] == "allow"
    effect, reason = verdict(runtime, beside, base)
    assert effect == "ask"
    assert "different repository" in reason


@pytest.mark.parametrize("spelled", KEEPS_ITS_QUESTION)
def test_a_repository_this_checkouts_scratch_does_not_hold_keeps_its_question(
    runtime: Runtime, base: Path, spelled: str
) -> None:
    effect, reason = verdict(runtime, edit(runtime, base / spelled, base), base)

    assert effect == "ask"
    assert "different repository" in reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("echo hello > tmp/kit/run.log", id="redirect"),
        pytest.param("sed -i 's/1/2/' tmp/kit/probe.py", id="rewrite-in-place"),
    ],
)
def test_a_shell_write_into_the_kit_is_scratch(
    runtime: Runtime, base: Path, command: str
) -> None:
    assert verdict(runtime, shell("PreToolUse", command, base), base)[0] == "allow"


@pytest.mark.parametrize(
    ("command", "asked"),
    [
        pytest.param(
            "echo hello > refs/elsewhere/tmp/run.log",
            "different repository",
            id="through-refs",
        ),
        # Creating a file outside the checkout is asked about before the
        # content gates are, and that question is the one this keeps.
        pytest.param(
            "echo hello > ../elsewhere/src/run.log", "an outside path", id="beside"
        ),
    ],
)
def test_a_shell_write_into_another_repository_keeps_its_question(
    runtime: Runtime, base: Path, command: str, asked: str
) -> None:
    effect, reason = verdict(runtime, shell("PreToolUse", command, base), base)

    assert effect == "ask"
    assert asked in reason


@pytest.mark.parametrize(
    ("written", "effect"),
    [
        pytest.param("date {into}tmp/kit/run.log", "allow", id="own-scratch"),
        pytest.param(
            "date {into}{base}/checkout/tmp/kit/run.log", "allow", id="own-absolute"
        ),
        pytest.param(
            "date {into}{base}/sibling/tmp/kit/run.log", None, id="sibling-scratch"
        ),
        pytest.param("date {into}../elsewhere/src/run.log", "ask", id="beside"),
        pytest.param("cd tmp && date {into}../README.md", "ask", id="after-cd"),
        pytest.param('cd "$D" && date {into}run.log', None, id="after-unread-cd"),
    ],
)
def test_a_redirect_and_a_tee_into_one_file_get_one_verdict(
    runtime: Runtime, base: Path, written: str, effect: str | None
) -> None:
    """`> f` and `| tee f` land the same bytes at the same path.

    Measured before this, from a session in one checkout writing into a
    sibling worktree's scratch: the redirection was allowed inside the
    sandbox and `tee` asked in both placements, and `tee` into this
    checkout's own scratch spelled absolutely asked where the redirection
    allowed. The tee's row asked about every tee and was relaxed only by
    grants that read a relative spelling. The redirection, for its part, was
    judged as spelled after a `cd`: `cd "$D" && date > run.log` created a
    file at the top of the checkout, wherever `$D` was. Both are now judged
    by one reading of one path.

    Two effects are not pinned here. This layout sits wherever the test
    runner makes its temporary directory, and under the machine's temporary
    root every path is scratch; and an unread `cd` is the runtime's to
    answer. What holds everywhere is that the two spellings agree, and
    `test_semantic_policy.py` pins both answers on paths no temporary root
    holds.
    """
    answers = {
        spelling: verdict(
            runtime,
            shell("PreToolUse", written.format(into=into, base=base), base),
            base,
        )[0]
        for spelling, into in (("redirect", "> "), ("tee", "| tee "))
    }

    assert answers["tee"] == answers["redirect"]
    if effect is not None:
        assert answers["redirect"] == effect


def test_a_command_writing_into_the_kit_is_not_reported_afterwards(
    runtime: Runtime, base: Path
) -> None:
    """The output a command leaves in the kit is scratch once it has landed too."""
    (base / "checkout/tmp/kit/run.log").write_text("ran\n", encoding="utf-8")

    assert reported(runtime, "date > tmp/kit/run.log", base) == ""


def test_a_command_writing_into_another_repository_is_still_reported(
    runtime: Runtime, base: Path
) -> None:
    (base / "elsewhere/src/run.log").write_text("ran\n", encoding="utf-8")

    found = reported(runtime, "date > ../elsewhere/src/run.log", base)

    assert "different repository" in found


def test_the_preview_reads_the_kit_as_scratch(base: Path) -> None:
    """`dev policy` answers for both runtimes, so it has to say what they say.

    Asked three ways: the path alone, the concrete change production refuses,
    and the shell forms that write into the kit.
    """
    checkout = base / "checkout"
    change = EditBatch(
        changes=[
            EditChange(
                path=base / KIT,
                before=f"{PREIMAGE}\n",
                after=f"{REFUSED}\n",
            )
        ]
    )
    document = base / "change.json"
    document.write_text(change.model_dump_json(), encoding="utf-8")
    readings = [
        verdict_for(subject, kind, False, checkout, declared_hook_set())
        for subject, kind in (
            (str(base / KIT), "edit"),
            (str(document), "edit-batch"),
            ("echo hello > tmp/kit/run.log", "shell"),
            ("sed -i 's/1/2/' tmp/kit/probe.py", "shell"),
        )
    ]

    assert {reading.effect for read in readings for reading in read.readings} == {
        "allow"
    }


@pytest.mark.parametrize(
    ("target", "effect"),
    [
        pytest.param("tmp/kit/probe.py", "allow", id="kit"),
        pytest.param("../elsewhere/src/probe.py", "ask", id="another-repository"),
    ],
)
def test_a_rewrite_judged_from_its_row_alone_reads_the_same(
    base: Path, target: str, effect: str
) -> None:
    """With no edit policy composed, the classifier judges a rewrite from its row.

    The row has to carry every fact the edit path reads — which repository
    holds the file, and how this checkout spells it — or the verdict would
    turn on which of the two answered. A target spelled relative to the
    session is anchored there before its repository is asked for: read bare,
    it named none, and a rewrite of another repository's file was judged by
    this one's conventions instead of meeting the referral.
    """
    hooks = declared_hook_set()
    classifier = ShellPolicy(
        hooks.resolved_shell_rules(),
        path_roles=declared_role_rows(list(hooks.path_roles)),
    )
    command = f"sed -i 's/{PREIMAGE}/{REFUSED}/' {target}"

    decided = classifier.decide(ShellCommand(command=command, cwd=base / "checkout"))

    assert decided.effect == effect
    if effect == "ask":
        assert "different repository" in decided.reason


@pytest.mark.parametrize("spelled", KEEPS_ITS_QUESTION)
def test_the_preview_keeps_every_other_repositorys_question(
    base: Path, spelled: str
) -> None:
    previewed = verdict_for(
        str(base / spelled), "edit", False, base / "checkout", declared_hook_set()
    )

    assert {reading.effect for reading in previewed.readings} == {"ask"}
    assert all(
        "different repository" in reading.reason for reading in previewed.readings
    )


@pytest.mark.parametrize("spelled", KIT_PLUGINS)
def test_a_kits_own_plugin_tree_under_scratch_is_written_like_scratch(
    runtime: Runtime, base: Path, spelled: str
) -> None:
    """Nothing this project generates lands under `tmp/`, so this is the kit's.

    The refusal exists because a hand edit of a compiled tree is reverted by
    the next generation; a kit's hand-written plugin has no generation to
    revert it, and refusing it left a probe of plugin behaviour unbuildable.
    """
    target = base / spelled
    written = target.relative_to(base / "checkout")
    calls = [
        edit(runtime, target, base),
        created(runtime, target.with_name("y.md"), base),
        shell("PreToolUse", f"echo hello > {written}", base),
        shell("PreToolUse", f"mkdir -p {written.parent / 'q'}", base),
    ]

    assert [verdict(runtime, call, base)[0] for call in calls] == ["allow"] * 4


@pytest.mark.parametrize("spelled", GENERATED_ELSEWHERE)
def test_every_plugin_tree_outside_this_checkouts_scratch_keeps_the_refusal(
    runtime: Runtime, base: Path, spelled: str
) -> None:
    effect, reason = verdict(runtime, edit(runtime, base / spelled, base), base)

    assert effect == "deny"
    assert "generated plugin tree" in reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("echo hello > .claude/plugins/lup/x.md", id="claude-tree"),
        pytest.param("echo hello > .codex/plugins/lup/x.md", id="codex-tree"),
        # Spelled under scratch and landing in the real tree: the host resolves
        # the link, and a spelling it moved is not one the exception trusts.
        pytest.param(
            "echo hello > tmp/linked/.claude/plugins/lup/x.md", id="linked-redirect"
        ),
        pytest.param("mkdir -p tmp/linked/.claude/plugins/lup/extra", id="linked-verb"),
        pytest.param(
            "echo hello > ../elsewhere/tmp/.claude/plugins/p/x.md",
            id="another-repositorys-tmp",
        ),
    ],
)
def test_a_shell_write_into_a_plugin_tree_outside_scratch_keeps_the_refusal(
    runtime: Runtime, base: Path, command: str
) -> None:
    effect, reason = verdict(runtime, shell("PreToolUse", command, base), base)

    assert effect == "deny"
    assert "generated plugin tree" in reason


def test_the_preview_reads_a_kits_plugin_tree_as_scratch_and_ours_as_generated(
    base: Path,
) -> None:
    checkout = base / "checkout"

    def effects(subject: str, kind: str) -> set[str]:
        """Every placement's effect for one subject, as `dev policy` reads it."""
        read = verdict_for(subject, kind, False, checkout, declared_hook_set())
        return {reading.effect for reading in read.readings}

    assert effects(str(base / "checkout/tmp/kit/.claude/plugins/p/x.md"), "edit") == {
        "allow"
    }
    assert effects("echo hello > tmp/kit/.codex/plugins/p/x.md", "shell") == {"allow"}
    assert effects(str(base / "checkout/.claude/plugins/lup/x.md"), "edit") == {"deny"}
    assert effects("echo hello > tmp/linked/.claude/plugins/lup/x.md", "shell") == {
        "deny"
    }


def test_no_tree_this_repository_generates_lands_under_its_scratch() -> None:
    """The exception is only sound while nothing generated can land in scratch.

    Walked from the recipes both runtimes compile rather than from a list, so
    an artifact placed under a scratch root — or a generated root turned into
    a link that resolves into one — fails here, before the exception would let
    a hand edit of it through. Both spellings are asked: the one the recipe
    declares, and the one the file resolves to in this checkout, which is the
    spelling the edit gates actually read.
    """
    root = Path.cwd()
    roles = declared_role_rows(list(declared_hook_set().path_roles))
    landing = [
        spelled
        for build in TARGETS.builders.values()
        for artifact in build(root).recipe.desired.artifacts
        for spelled in (
            artifact.path.as_posix(),
            this_checkout_path(str(root / artifact.path), root),
        )
        if declared_scratch(spelled, roles)
    ]

    assert landing == []


def test_a_repository_made_in_scratch_is_made_where_a_link_lands(
    runtime: Runtime, base: Path
) -> None:
    """`git init` into scratch is a scratch write, and read as one.

    A link planted under `tmp/` moves where the repository is made, so the
    directory it names is resolved by the host as any write target is, and a
    landing outside scratch keeps the question every other write through a
    link meets.
    """
    (base / "checkout/src").mkdir()
    (base / "checkout/tmp/into-src").symlink_to(base / "checkout/src")

    made = verdict(runtime, shell("PreToolUse", "git init tmp/fresh", base), base)
    linked = verdict(
        runtime, shell("PreToolUse", "git init tmp/into-src/fresh", base), base
    )

    assert made[0] == "allow"
    assert linked[0] == "ask"
    assert "symlink" in linked[1]
