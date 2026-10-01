"""What the capture must tell apart: a capability that moved and one that went.

Both leave the old import path unresolvable, which is exactly why a reviewer
cannot separate them by reading a diff — so these pin the separation itself
rather than any particular walk. The walk is checked too, but for the one
property that decides what the capture is worth: a name it never recorded is a
name nothing will notice the loss of.
"""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev import preservation
from lup.devtools.dev.boundaries import TrackedSource
from lup.devtools.dev.preservation import (
    Capability,
    SurfaceCapture,
    ModuleSurface,
    compare,
    surfaces,
)
from lup.devtools.project import DevProject
from lup.execution.shell import git
from tests.unit.test_ledger_placement import committed, repository


def capture(*modules: ModuleSurface) -> SurfaceCapture:
    return SurfaceCapture(revision="0" * 40, roots=["lup"], modules=list(modules))


def source(text: str, path: str) -> TrackedSource:
    return TrackedSource(rel=path, path=Path(path), text=text)


def test_a_name_no_module_declares_any_more_has_disappeared() -> None:
    """The failure the whole fixture exists to find."""
    divergence = compare(
        capture(ModuleSurface(module="lup.jobs.runtime", declares=["JobSpec"])),
        capture(ModuleSurface(module="lup.jobs.runtime", declares=[])),
    )

    assert [row.identity for row in divergence.disappeared] == ["JobSpec"]
    assert not divergence.intact()


def test_a_name_declared_somewhere_else_has_moved_and_does_not_fail() -> None:
    """A reorganisation is moves; reading one as a loss would make this noise."""
    divergence = compare(
        capture(ModuleSurface(module="lup.jobs.runtime", declares=["JobSpec"])),
        capture(ModuleSurface(module="lup.orchestration.jobs", declares=["JobSpec"])),
    )

    assert divergence.disappeared == []
    assert divergence.intact()
    assert [row.homes for row in divergence.relocated] == [["lup.orchestration.jobs"]]


def test_a_shared_name_casts_no_vote_in_the_migration_map() -> None:
    """``logger`` is declared forty-six times, so where it went is ambiguous."""
    before = capture(
        ModuleSurface(module="lup.jobs.runtime", declares=["logger"]),
        ModuleSurface(module="lup.client", declares=["logger"]),
    )
    after = capture(
        ModuleSurface(module="lup.orchestration.jobs", declares=["logger"]),
        ModuleSurface(module="lup.client", declares=["logger"]),
    )

    divergence = compare(before, after)

    assert divergence.disappeared == []
    assert [row.homes for row in divergence.relocated] == [
        ["lup.client", "lup.orchestration.jobs"]
    ]
    assert divergence.module_moves() == {}


def test_the_migration_map_is_the_module_pairs_the_moves_imply() -> None:
    """The same difference that proved nothing was lost repoints an importer."""
    before = capture(
        ModuleSurface(module="lup.jobs.runtime", declares=["JobSpec", "JobStore"])
    )
    after = capture(
        ModuleSurface(module="lup.orchestration.jobs", declares=["JobSpec", "JobStore"])
    )

    assert compare(before, after).module_moves() == {
        "lup.jobs.runtime": "lup.orchestration.jobs"
    }


def test_a_renamed_command_is_a_disappearance_at_the_function_declaring_it() -> None:
    """A command is walked through its own declaration rather than separately.

    Nothing imports a command by the words a reader types, and a revision's
    command list could only be read by importing it — so what stands for one
    here is the function the decorator wraps, which a rename takes with it.
    """
    divergence = compare(
        capture(ModuleSurface(module="lup.devtools.dev.app", declares=["check_cmd"])),
        capture(ModuleSurface(module="lup.devtools.dev.app", declares=["verify_cmd"])),
    )

    assert [row.identity for row in divergence.disappeared] == ["check_cmd"]
    assert [row.identity for row in divergence.arrived] == ["verify_cmd"]


def test_arrival_is_reported_without_failing_the_run() -> None:
    """What the range added is worth seeing; it is not what the gate is for."""
    divergence = compare(
        capture(ModuleSurface(module="lup.client", declares=["Client"])),
        capture(ModuleSurface(module="lup.client", declares=["Client", "Session"])),
    )

    assert [row.identity for row in divergence.arrived] == ["Session"]
    assert divergence.intact()


def test_a_method_is_its_own_capability() -> None:
    """Qualifying by scope is what keeps one class's loss from another's cover."""
    walked = list(
        surfaces(
            [
                source(
                    "class Client:\n"
                    "    def close(self) -> None: ...\n"
                    "class Session:\n"
                    "    def close(self) -> None: ...\n",
                    "packages/lup/src/lup/client.py",
                )
            ],
            {"lup"},
        )
    )

    assert walked[0].declares == [
        "Client",
        "Client.close",
        "Session",
        "Session.close",
    ]


def test_a_module_no_root_can_import_is_not_a_surface() -> None:
    """A generated tree is derived; counting it would report every regeneration."""
    walked = list(
        surfaces(
            [
                source("MARKER = 1\n", ".claude/plugins/lup/hooks/runtime/kernel.py"),
                source("MARKER = 1\n", "packages/lup/src/lup/client.py"),
            ],
            {"lup"},
        )
    )

    assert [one.module for one in walked] == ["lup.client"]


def test_a_module_mid_merge_is_one_surface_however_many_stages_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index holds a conflicted path three times; the walk reads one file.

    Built the way the duplication arises: both sides of a merge edit one
    line, the merge stops, and the working copy is restored from one side
    without being staged — so the file on disk parses clean while the index
    still carries every stage of it.
    """
    work = tmp_path / "repo"
    (work / "src/pkg").mkdir(parents=True)
    module = work / "src/pkg/mod.py"
    build = sh.Command("git").bake(
        "-C",
        str(work),
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.email=preservation@example.test",
        "-c",
        "user.name=Preservation Test",
        _tty_out=False,
    )
    build("init", "-b", "main")
    module.write_text("VALUE = 1\n", encoding="utf-8")
    build("add", "--all")
    build("commit", "-m", "base")
    build("switch", "-c", "theirs")
    module.write_text("VALUE = 2\n", encoding="utf-8")
    build("commit", "-am", "theirs")
    build("switch", "main")
    module.write_text("VALUE = 3\n", encoding="utf-8")
    build("commit", "-am", "ours")
    build("merge", "theirs", _ok_code=[0, 1])
    build("restore", "--ours", "--", "src/pkg/mod.py")
    monkeypatch.chdir(work)
    assert len(git.lines("ls-files", "--unmerged")) == 3

    walked = preservation.surface_now(DevProject(package="pkg"))

    assert [one.module for one in walked.modules] == ["pkg.mod"]
    assert walked.modules[0].declares == ["VALUE"]


def test_every_entry_carries_the_module_that_resolves_it() -> None:
    """The flattening both halves of the comparison run over."""
    entries = list(
        capture(ModuleSurface(module="lup.client", declares=["Client"])).capabilities()
    )

    assert entries == [Capability(identity="Client", location="lup.client")]


def test_a_name_only_its_own_function_can_reach_is_not_a_surface() -> None:
    """A command wired onto an app inside a factory is that factory's own.

    No importer can name it however it is spelled, so its going is not a
    break anybody downstream could have met — and counting it would ask for a
    migration to be declared for a rename nobody outside the file can see.
    """
    walked = list(
        surfaces(
            [
                source(
                    "def create_app():\n"
                    "    @app.command('check')\n"
                    "    def check_cmd() -> None: ...\n"
                    "    return app\n",
                    "src/lup/app.py",
                )
            ],
            {"lup"},
        )
    )

    assert walked[0].declares == ["create_app"]


def test_a_module_that_kept_names_is_not_a_pair_however_many_left() -> None:
    """The map's pair is a whole-module claim, and a split does not support one.

    `dev relocate` respells every import of the old path, so a pair drawn for
    a module still declaring names sends those names to a module that never
    held them — applied silently, and met much later as an unresolved import.
    """
    before = capture(
        ModuleSurface(
            module="lup.harness.codescan.boundaries",
            declares=["ApplicationRoots", "generated_tree_paths"],
        )
    )
    after = capture(
        ModuleSurface(
            module="lup.harness.codescan.boundaries", declares=["generated_tree_paths"]
        ),
        ModuleSurface(
            module="lup.harness.codescan.common", declares=["ApplicationRoots"]
        ),
    )

    divergence = compare(before, after)

    assert divergence.module_moves() == {}
    assert [move.module for move in divergence.unmapped_modules()] == [
        "lup.harness.codescan.boundaries"
    ]
    assert divergence.unmapped_modules()[0].retained == ["generated_tree_paths"]
    assert [
        (destination.module, destination.names)
        for destination in divergence.unmapped_modules()[0].destinations
    ] == [("lup.harness.codescan.common", ["ApplicationRoots"])]


def test_names_that_went_to_two_modules_are_a_split_and_not_one_pair() -> None:
    """A module emptied into two is no more repointable than one that stayed."""
    before = capture(
        ModuleSurface(module="lup.coordination.store", declares=["ROOT", "opened"])
    )
    after = capture(
        ModuleSurface(module="lup.coordination.bare.store", declares=["ROOT"]),
        ModuleSurface(module="lup.coordination.meeting", declares=["opened"]),
    )

    divergence = compare(before, after)

    assert divergence.module_moves() == {}
    assert [
        (destination.module, destination.names)
        for destination in divergence.unmapped_modules()[0].destinations
    ] == [
        ("lup.coordination.bare.store", ["ROOT"]),
        ("lup.coordination.meeting", ["opened"]),
    ]


def test_a_split_spells_what_went_where_and_what_stayed() -> None:
    """What the command prints where it can print no pair: the whole of it.

    A reader told only that the map is shorter learns nothing about the
    imports that will break; one told which names went where repoints them.
    """
    divergence = compare(
        capture(
            ModuleSurface(
                module="lup.coordination.identity",
                declares=["MEMBER_KIND", "session_member_id", "member_ref"],
            )
        ),
        capture(
            ModuleSurface(
                module="lup.coordination.identity",
                declares=["session_member_id", "member_ref"],
            ),
            ModuleSurface(
                module="lup.coordination.bare.store", declares=["MEMBER_KIND"]
            ),
        ),
    )

    assert [move.spelled() for move in divergence.unmapped_modules()] == [
        "lup.coordination.identity: MEMBER_KIND now in lup.coordination.bare.store; "
        "still declares session_member_id, member_ref"
    ]


def test_a_whole_module_move_and_a_split_told_apart_out_of_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both shapes over one range, derived the way the command derives them.

    Read out of two revisions rather than assembled here, because what the
    map is worth rests on the walk agreeing with git about what each revision
    declared.
    """
    root = repository(tmp_path / "upstream")
    (root / "src/lup").mkdir(parents=True)
    (root / "src/lup/shared.py").write_text(
        "class Kept:\n    def here(self) -> None: ...\n\n\ndef taken() -> None: ...\n",
        encoding="utf-8",
    )
    (root / "src/lup/whole.py").write_text(
        "def carried() -> None: ...\n", encoding="utf-8"
    )
    committed(root, "base")
    monkeypatch.chdir(root)
    base = git.out("rev-parse", "HEAD")
    (root / "src/lup/shared.py").write_text(
        "class Kept:\n    def here(self) -> None: ...\n", encoding="utf-8"
    )
    (root / "src/lup/taker.py").write_text(
        "def taken() -> None: ...\n", encoding="utf-8"
    )
    (root / "src/lup/whole.py").unlink()
    (root / "src/lup/moved.py").write_text(
        "def carried() -> None: ...\n", encoding="utf-8"
    )
    committed(root, "head")
    head = git.out("rev-parse", "HEAD")

    project = DevProject(package="lup")
    divergence = compare(
        preservation.surface_at(base, project), preservation.surface_at(head, project)
    )

    assert divergence.module_moves() == {"lup.whole": "lup.moved"}
    assert [move.spelled() for move in divergence.unmapped_modules()] == [
        "lup.shared: taken now in lup.taker; still declares Kept, Kept.here"
    ]


def test_two_modules_sharing_a_word_are_not_one_module_that_moved() -> None:
    """A name resolving elsewhere is not evidence that anything went there.

    ``lup.providers.roster_prompt`` keeps every hook it declares while an
    unrelated module about carrier drift, written beside it, spells two of
    its constants the same way — enough for a vote per name to call the whole
    module moved, and enough to rewrite the imports of everything that
    stayed. What separates coincidence from a move is not how many
    names agree but whether the module still declares any of its own.
    """
    before = capture(
        ModuleSurface(
            module="lup.providers.roster_prompt",
            declares=[
                "RUNTIME_SOURCE",
                "runtime_source",
                "GUARD_SCRIPT",
                "prompt_hook",
            ],
        )
    )
    after = capture(
        ModuleSurface(
            module="lup.providers.roster_prompt",
            declares=["GUARD_SCRIPT", "prompt_hook", "departure_hook"],
        ),
        ModuleSurface(
            module="lup.providers.drift_prompt",
            declares=["RUNTIME_SOURCE", "runtime_source", "GUARD_SCRIPT"],
        ),
    )

    divergence = compare(before, after)

    assert divergence.module_moves() == {}
    assert [move.spelled() for move in divergence.unmapped_modules()] == [
        "lup.providers.roster_prompt: RUNTIME_SOURCE, runtime_source now in "
        "lup.providers.drift_prompt; still declares GUARD_SCRIPT, prompt_hook"
    ]


def test_a_member_moved_up_to_a_base_is_reached_as_before() -> None:
    """A field moved to the parent every wall shares is inherited, not gone."""
    before = capture(
        ModuleSurface(
            module="lup.launch.declaration",
            declares=["Sandbox", "OuterContainer", "OuterContainer.mounts"],
            inherits={"OuterContainer": ["Sandbox"]},
        )
    )
    after = capture(
        ModuleSurface(
            module="lup.launch.declaration",
            declares=["Sandbox", "Sandbox.mounts", "OuterContainer"],
            inherits={"OuterContainer": ["Sandbox"]},
        )
    )

    divergence = compare(before, after)

    assert divergence.disappeared == []
    assert divergence.relocated == []


def test_a_member_its_class_and_every_base_dropped_has_disappeared() -> None:
    before = capture(
        ModuleSurface(
            module="lup.launch.declaration",
            declares=["Sandbox", "OuterContainer", "OuterContainer.mounts"],
            inherits={"OuterContainer": ["Sandbox"]},
        )
    )
    after = capture(
        ModuleSurface(
            module="lup.launch.declaration",
            declares=["Sandbox", "OuterContainer"],
            inherits={"OuterContainer": ["Sandbox"]},
        )
    )

    assert [row.identity for row in compare(before, after).disappeared] == [
        "OuterContainer.mounts"
    ]


def test_the_walk_records_the_bases_a_class_names() -> None:
    walked = list(
        surfaces(
            [
                source(
                    "class Sandbox(BaseModel, ABC):\n    mounts: list = []\n"
                    "class Outer(Sandbox, frozen=True):\n    pass\n"
                    "class Typed(module.Base[int]):\n    pass\n",
                    "lup/walls.py",
                )
            ],
            {"lup"},
        )
    )

    assert walked[0].inherits == {
        "Sandbox": ["BaseModel", "ABC"],
        "Outer": ["Sandbox"],
        "Typed": ["Base"],
    }
