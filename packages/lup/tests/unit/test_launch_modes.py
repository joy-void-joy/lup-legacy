"""A mode is a named preset of the project's declaration, selected by `--mode <name>`.

What a kind of session changes is a ``Claude(...)`` and its ``Codex(...)``
variant stating only those fields, laid over the declaration `harness
claude|codex` builds from the project, with the command line laid over both;
what it grants its container is an ``OuterContainer`` between the command line
and the person's config. How much the runtime asks is those declarations' own
permission and approval fields, so a mode switching the asking off, or
swapping in guidance of its own, is refused where no container stands in for
what it takes away.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

import lup.devtools.harness.launch as launch
from lup.devtools.harness.composition import NativeTargets
from lup.diagnostics import Refusal
from lup.harness.models import PromptDocument, TextPart
from lup.launch.companions import CompanionLaunch, Contribution, HostCompanion
from lup.launch.declaration import (
    InnerSandbox,
    LaunchSandbox,
    OuterContainer,
    Recording,
)
from lup.policy.kernel.diagnostic import rendered
from lup.providers.claude import Claude
from lup.providers.codex import Codex
from lup.types import CustomModel
from tests.unit.harness_launch import checkout, composition, profiles


class Preview(HostCompanion, frozen=True):
    """A companion a mode keeps beside its sessions, handing nothing."""

    @contextmanager
    def held(self, launch: CompanionLaunch) -> Iterator[Contribution]:
        del launch
        yield Contribution()


FREE = launch.LaunchMode(
    name="free",
    help="explore without being asked, a later session tidies up",
    claude=Claude(permission_mode="auto", system_prompt="You may explore freely."),
    codex=Codex(approval_policy="on-request", approvals_reviewer="auto_review"),
    container=OuterContainer(sudo=True),
)
RESEARCH = launch.LaunchMode(
    name="research",
    help="a research session, kept apart",
    claude=Claude(
        model="sonnet",
        max_recursive_agent=0,
        record=Recording(root=Path("notes/sessions"), transcript=False),
    ),
    codex=Codex(model=CustomModel(id="gpt-research")),
)
MODES = [FREE, RESEARCH]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(launch, "accessible_roots", lambda *_a, **_k: [])
    monkeypatch.setattr(launch, "granted_devices", lambda *_a, **_k: [])
    return checkout(tmp_path)


def claude(root: Path, **named: object) -> Claude:
    request = launch.LaunchArguments.model_validate(
        {"sandbox": LaunchSandbox.OUTER, **named}
    )
    return launch.claude_declaration(composition(root, "claude"), request, profiles())


def codex(root: Path, **named: object) -> Codex:
    request = launch.LaunchArguments.model_validate(
        {"sandbox": LaunchSandbox.OUTER, **named}
    )
    return launch.codex_declaration(composition(root, "codex"), request, None)


def test_a_mode_is_selected_by_its_name() -> None:
    assert launch.selected_mode(MODES, "free") == FREE
    assert launch.selected_mode(MODES, None) is None


def test_an_undeclared_mode_is_refused_naming_the_declared_ones() -> None:
    with pytest.raises(Refusal) as refused:
        launch.selected_mode(MODES, "syra")

    said = refused.value.said["why"]
    assert "free" in said and "research" in said


def test_a_mode_s_preset_is_laid_over_the_project_s_declaration(root: Path) -> None:
    plain = claude(root)
    free = claude(root, mode=FREE)

    assert free.permission_mode == "auto"
    assert free.system_prompt == "You may explore freely."
    assert free.plugin == plain.plugin
    assert free.tools == plain.tools


def test_a_mode_carries_its_codex_variant(root: Path) -> None:
    free = codex(root, mode=FREE)

    assert free.approval_policy == "on-request"
    assert free.approvals_reviewer == "auto_review"
    assert codex(root, mode=RESEARCH).model == CustomModel(id="gpt-research")


def test_the_command_line_overrules_the_mode(root: Path) -> None:
    assert claude(root, mode=RESEARCH).model == "sonnet"
    assert claude(root, mode=RESEARCH, model="opus").model == "opus"
    assert claude(root, mode=RESEARCH).max_recursive_agent == 0
    assert claude(root, mode=RESEARCH, max_recursive_agent=2).max_recursive_agent == 2


def test_a_mode_moves_its_record_and_keeps_the_ledger_and_its_name(
    root: Path,
) -> None:
    record = claude(root, mode=RESEARCH).record

    assert record is not None
    assert record.root == Path("notes/sessions")
    assert record.transcript is False
    assert record.mode == "research"
    transcribed = claude(root, mode=RESEARCH, transcribe_session=True).record
    assert transcribed is not None and transcribed.transcript is True


def test_a_mode_s_container_lies_between_the_command_line_and_the_person(
    root: Path, tmp_path: Path
) -> None:
    person = tmp_path / "xdg" / "lup" / "config.toml"
    person.parent.mkdir(parents=True, exist_ok=True)
    person.write_text('[container]\nsudo = false\nnetwork = "bridge"\n')

    free = claude(root, mode=FREE).sandbox
    assert isinstance(free, OuterContainer)
    assert free.sudo is True
    assert free.network == "bridge"
    flagged = claude(root, mode=FREE, sudo=False).sandbox
    assert isinstance(flagged, OuterContainer) and flagged.sudo is False


def test_a_mode_leaves_the_wall_to_the_command_line() -> None:
    with pytest.raises(ValidationError, match="container="):
        launch.LaunchMode(
            name="walled",
            help="names its own wall",
            claude=Claude(sandbox=InnerSandbox()),
        )


@pytest.mark.parametrize("posture", [LaunchSandbox.INNER, LaunchSandbox.NONE])
def test_a_mode_switching_the_asking_off_is_refused_on_the_host(
    root: Path, posture: LaunchSandbox
) -> None:
    with pytest.raises(Refusal) as refused:
        claude(root, mode=FREE, sandbox=posture)

    said = rendered(refused.value.said)
    assert "free" in said and "auto" in said and "--sandbox outer" in said
    with pytest.raises(Refusal) as refused:
        codex(root, mode=FREE, sandbox=posture)
    assert "auto_review" in refused.value.said["why"]


def test_a_mode_asking_nothing_a_container_stands_in_for_opens_on_the_host(
    root: Path,
) -> None:
    agent = claude(root, mode=RESEARCH, sandbox=LaunchSandbox.INNER)

    assert isinstance(agent.sandbox, InnerSandbox)
    assert agent.model == "sonnet"


def test_a_mode_s_own_guidance_is_the_container_s_and_refused_on_the_host(
    root: Path,
) -> None:
    guided = launch.LaunchMode(
        name="guided",
        help="reads its own guidance",
        container=OuterContainer(
            guidance=PromptDocument(source=__name__, parts=[TextPart(text="Hi.\n")])
        ),
    )

    held = claude(root, mode=guided).sandbox
    assert isinstance(held, OuterContainer) and held.guidance is not None
    with pytest.raises(Refusal) as refused:
        codex(root, mode=guided, sandbox=LaunchSandbox.INNER)
    assert "guidance" in refused.value.said["why"]


def test_a_mode_s_companions_are_held_beside_the_project_s(root: Path) -> None:
    kept = launch.LaunchMode(
        name="kept",
        help="keeps a preview beside it",
        claude=Claude(companions=[Preview(name="preview")]),
    )

    assert [each.name for each in claude(root, mode=kept).companions] == [
        "preview",
        "review-answers",
    ]


def test_a_mode_compiles_its_own_tree_or_the_project_s() -> None:
    own = NativeTargets(builders={})
    compiled = launch.LaunchMode(name="own", help="its own tree", targets=own)

    assert compiled.targets_at(-1) is own
    assert FREE.targets_at(-1) is None


def test_the_launcher_help_lists_every_mode() -> None:
    said = launch.modes_help(MODES)

    assert "free" in said and FREE.help in said and "research" in said
