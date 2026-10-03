"""Where editing is happening, published by one process and read by another.

The writer is the hermetic hook runtime, which may not import the module that
owns the record. Nothing passes between them: each resolves the location out
of the repository they are both in, and each spells the record's shape for
itself — so these pin both ends against each other, since no type checker
spans that boundary.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from lup.devtools.launcher import ENVIRONMENT_VARIABLE
from lup.execution.shell import git
from lup.policy.assets.host import (
    declared_program,
    file_diagnostics,
    publish_edition,
    swept_files,
    shared_git_directory,
    worktree_root,
)
from lup.workspace.edition import Edition, edition_path, read_edition


def checkout(root: Path) -> Path:
    """A main checkout: `.git` is the git directory itself."""
    git("init", "-q", "-b", "main", str(root))
    return root


def linked(main: Path, root: Path) -> Path:
    """A worktree of *main*: `.git` is a file naming the main git directory."""
    git(
        "-C", str(main), "worktree", "add", "-q", "--orphan", "-b", root.name, str(root)
    )
    return root


def edited(root: Path, name: str = "module.py") -> Path:
    file = root / name
    file.write_text("x = 1\n", encoding="utf-8")
    return file


def test_a_file_belongs_to_the_checkout_that_holds_it(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = work / "src" / "module.py"
    file.parent.mkdir(parents=True)
    file.write_text("x = 1\n", encoding="utf-8")

    assert worktree_root(str(file)) == str(work)


def test_a_checkout_root_answers_for_itself(tmp_path: Path) -> None:
    """Codex hands over a directory, and the walk has to admit one.

    With only the parents as candidates, a directory that is already a
    checkout would be answered for by whatever encloses it — or by nothing.
    """
    work = checkout(tmp_path / "repo")

    assert worktree_root(str(work)) == str(work)


def test_a_path_in_no_checkout_names_none(tmp_path: Path) -> None:
    loose = tmp_path / "loose.py"
    loose.write_text("x = 1\n", encoding="utf-8")

    assert worktree_root(str(loose)) == ""


def test_lup_metadata_is_not_a_git_marker(tmp_path: Path) -> None:
    (tmp_path / ".git" / "lup").mkdir(parents=True)
    assert worktree_root(str(tmp_path)) == ""


def test_a_relative_path_names_none() -> None:
    """The hook is promised no working directory to resolve one against."""
    assert worktree_root("src/module.py") == ""


def test_a_worktree_resolves_to_the_directory_it_shares(tmp_path: Path) -> None:
    """The whole point: two checkouts, one place both of them can name.

    The hook runs wherever the edit landed and the reader wherever its
    server was launched. Neither can see the other's directory, so a record
    written beside the edit would be looked for somewhere it never was.
    """
    main = checkout(tmp_path / "repo")
    worktree = linked(main, tmp_path / "feature")

    assert shared_git_directory(str(edited(worktree))) == str(main / ".git")
    assert shared_git_directory(str(edited(main))) == str(main / ".git")


def test_a_repository_kept_beside_its_worktrees_resolves_too(tmp_path: Path) -> None:
    """A git directory need not sit inside a checkout, and this one does not.

    Taking the checkout above `worktrees/` assumes the standard layout. Where
    the repository is kept beside its worktrees instead, that path is not a
    checkout at all — it is whatever encloses the repository, and the record
    lands outside it. The directory they share is one level lower and is
    there in both layouts.
    """
    bare = tmp_path / "repo.git"
    git("init", "-q", "--bare", "-b", "main", str(bare))
    work = tmp_path / "tree" / "feature"
    git(
        "-C", str(bare), "worktree", "add", "-q", "--orphan", "-b", "feature", str(work)
    )

    assert shared_git_directory(str(edited(work))) == str(bare)


def test_the_hook_and_the_library_name_one_location(tmp_path: Path) -> None:
    """Neither half can import the other, so the paths are pinned equal."""
    main = checkout(tmp_path / "repo")
    worktree = linked(main, tmp_path / "feature")
    file = edited(worktree)

    publish_edition(str(file), str(main))

    assert edition_path(worktree).is_file()
    assert edition_path(worktree) == edition_path(main)


def test_what_the_hook_writes_is_what_the_library_reads(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = edited(work)

    publish_edition(str(file), str(work))

    assert read_edition(edition_path(work)) == Edition(workspace=work, file=file)


def test_an_edit_in_a_worktree_publishes_that_worktree(tmp_path: Path) -> None:
    """The record names where editing happened, not where it was recorded."""
    main = checkout(tmp_path / "repo")
    worktree = linked(main, tmp_path / "feature")

    publish_edition(str(edited(worktree)), str(main))

    published = read_edition(edition_path(main))
    assert published is not None and published.workspace == worktree


def test_a_later_edit_replaces_an_earlier_one(tmp_path: Path) -> None:
    main = checkout(tmp_path / "repo")
    worktree = linked(main, tmp_path / "feature")

    publish_edition(str(edited(main)), str(main))
    publish_edition(str(edited(worktree)), str(main))

    published = read_edition(edition_path(main))
    assert published is not None and published.workspace == worktree


def test_a_path_in_no_repository_publishes_nothing(tmp_path: Path) -> None:
    loose = tmp_path / "loose.py"
    loose.write_text("x = 1\n", encoding="utf-8")

    publish_edition(str(loose), str(tmp_path))

    assert list(tmp_path.glob("**/edition.json")) == []


def test_an_edit_in_a_repository_nested_in_the_session_s_publishes_nothing(
    tmp_path: Path,
) -> None:
    """Another repository's git directory is not this session's to keep state in.

    The reader looks in the session's own repository, so a record written
    into the nested one's is read by nobody and outlives the file it names.
    """
    session = checkout(tmp_path / "repo")
    nested = checkout(session / "works")

    publish_edition(str(edited(nested)), str(session))

    assert not (nested / ".git" / "lup").exists()
    assert read_edition(edition_path(session)) is None


def test_an_unwritable_destination_does_not_raise(tmp_path: Path) -> None:
    """A verdict must never turn on this, and neither must an edit.

    The permission path converts anything raised into a conservative ask.
    Publishing runs after the tool, where no verdict is left to corrupt, but
    a failure that propagated would still reach that handler and answer for
    the edit with a filesystem error.
    """
    work = checkout(tmp_path / "repo")
    (work / ".lup").write_text("occupied\n", encoding="utf-8")

    publish_edition(str(edited(work)), str(work))


def checker(root: Path, payload: str) -> list[str]:
    """A stand-in type checker emitting *payload*, so the parsing is the subject."""
    script = root / "fake-checker"
    script.write_text(f"#!/bin/sh\ncat <<'JSON'\n{payload}\nJSON\n", encoding="utf-8")
    script.chmod(0o755)
    return ["fake-checker"]


def test_the_checkout_answers_for_a_declared_program_first(tmp_path: Path) -> None:
    """A sibling worktree holds the same relative path with different contents.

    Preferring the checkout is what makes the verdict the edited tree's
    rather than whichever environment the session was launched from, and it
    holds whether the project spelled a path or a name.
    """
    work = checkout(tmp_path / "repo")
    (work / "bin").mkdir()
    program = work / "bin" / "checker"
    program.write_text("#!/bin/sh\n", encoding="utf-8")

    assert declared_program(str(work), "bin/checker") == str(program)


def test_a_bare_name_the_checkout_lacks_is_left_to_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not every project keeps its toolchain beside its code.

    conda, pyenv, a system install, an environment `UV_PROJECT_ENVIRONMENT`
    put elsewhere — none are reachable by a path relative to the checkout,
    and refusing them made this gate unavailable rather than configurable.
    """
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    work = checkout(tmp_path / "repo")

    assert declared_program(str(work), "pyright") == "pyright"


def installed(environment: Path, name: str) -> Path:
    """*name* as an environment's own scripts directory installs it."""
    scripts = environment / Path(sys.executable).parent.name
    scripts.mkdir(parents=True)
    program = scripts / name
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    return program


def test_a_bare_name_is_asked_of_the_checkout_environment_before_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declaration names the program; resolving it is what finds a copy.

    A project installs its checker into its own environment, so that is where
    a bare name is answered — before `PATH`, which would let whichever
    environment the session was launched from answer for this checkout.
    """
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    work = checkout(tmp_path / "repo")
    program = installed(work / ".venv", "pyright")

    assert declared_program(str(work), "pyright") == str(program)


def test_an_interpreter_outside_the_conventional_directory_still_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook runs under whichever `python3` the runtime found.

    One installed in `sbin` names a scripts directory no environment has, so
    a declared program resolved from it would be a bare name sent to `PATH` —
    where a project's own toolchain is exactly what is not installed. That
    leaves both the checker and the repair sweep silent on a machine holding
    both, which is the failure this gate exists to prevent.
    """
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/sbin/python3")
    work = checkout(tmp_path / "repo")
    scripts = work / ".venv" / "bin"
    scripts.mkdir(parents=True)
    program = scripts / "pyright"
    program.write_text("#!/bin/sh\n", encoding="utf-8")

    assert declared_program(str(work), "pyright") == str(program)


def test_a_redirected_environment_is_where_a_bare_name_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.venv` is uv's default, not the answer, and the gate must not assume it.

    `UV_PROJECT_ENVIRONMENT` is how one environment gets shared across
    worktrees, kept off a slow filesystem, or put where a container expects
    it. A declaration spelling `.venv/bin/pyright` would resolve to nothing
    here and report no diagnostics, on every edit, without saying why.
    """
    shared = tmp_path / "shared"
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, str(shared))
    work = checkout(tmp_path / "repo")
    program = installed(shared, "pyright")

    assert declared_program(str(work), "pyright") == str(program)


def test_a_declared_path_that_resolves_to_nothing_stays_nothing(
    tmp_path: Path,
) -> None:
    """A project that named a location meant that location.

    Falling back to `PATH` here would run some other copy of the program in
    silence, which is the substitution the checkout-first order exists to
    prevent.
    """
    work = checkout(tmp_path / "repo")

    assert declared_program(str(work), "bin/absent") == ""


def report(file: Path, severity: str = "error", line: int = 0) -> str:
    return json.dumps(
        {
            "generalDiagnostics": [
                {
                    "file": str(file),
                    "severity": severity,
                    "range": {"start": {"line": line}},
                    "message": "something is wrong",
                }
            ]
        }
    )


def test_a_diagnostic_for_the_edited_file_is_reported(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = checker(work, report(file))

    assert file_diagnostics(str(file), command)["blocking"] == [
        "module.py:1: error: something is wrong",
    ]


def sweep(root: Path, payload: str) -> list[str]:
    """A stand-in repair sweep emitting *payload*, recording how it was called."""
    script = root / "fake-sweep"
    script.write_text(
        f'#!/bin/sh\necho "$@" > "{root / "sweep-arguments"}"\ncat <<\'JSON\'\n'
        f"{payload}\nJSON\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return ["fake-sweep", "--fix"]


def removals(file: str, line: int = 3, rule_id: str = "") -> str:
    return json.dumps({"repaired": [{"file": file, "line": line, "rule_id": rule_id}]})


def test_a_removed_directive_is_reported_back(tmp_path: Path) -> None:
    """A directive the sweep deletes is one the agent wrote on purpose.

    Removing it in silence teaches nothing, and the same directive is written
    again on the next file — so the one channel this event has carries what
    went and why it silenced nothing.
    """
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = sweep(work, removals("module.py"))

    assert swept_files([str(file)], command)[str(file)]["repaired"] == [
        "line 3: removed `# lup: ignore` — it guarded no rule, so it silenced nothing"
    ]


def test_a_removed_directive_names_the_rule_it_claimed(tmp_path: Path) -> None:
    """What a typed directive said it silenced is the whole of why it is gone."""
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = sweep(work, removals("module.py", rule_id="any-type"))

    assert swept_files([str(file)], command)[str(file)]["repaired"] == [
        "line 3: removed `# lup: ignore[any-type]` — it guarded no rule, so it"
        " silenced nothing"
    ]


def test_the_sweep_is_given_the_file_the_way_it_names_its_own(
    tmp_path: Path,
) -> None:
    """The sweep is asked in the spelling it answers in.

    Which is also the only spelling a project's own declared sweep must
    understand: selecting by repository-relative prefix is the shape this can
    count on, where anything else is a shape it would have to assume.
    """
    work = checkout(tmp_path / "repo")
    (work / "package").mkdir()
    file = edited(work, "package/module.py")
    command = sweep(work, removals("package/module.py"))
    swept_files([str(file)], command)

    recorded = (work / "sweep-arguments").read_text(encoding="utf-8")

    assert "--path package/module.py" in recorded
    assert str(work) not in recorded


def test_a_file_outside_the_checkout_is_not_swept(tmp_path: Path) -> None:
    """Nothing anchors it, so nothing about it can be asked of the sweep."""
    checkout(tmp_path / "repo")
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    assert swept_files([str(outside)], ["fake-sweep"]) == {}


def test_the_checker_leads_the_path_with_its_own_environment(tmp_path: Path) -> None:
    """A checker resolves what it reads against the interpreter on its `PATH`.

    Inheriting the session's leaves a check running in one checkout reading
    another one's installed packages, and reporting every third-party import
    of the edited file unresolvable. Those arrive looking exactly like the
    edit having broken something, and no edit in the checked tree clears one.
    """
    work = checkout(tmp_path / "repo")
    file = edited(work)
    binaries = work / ".venv" / "bin"
    binaries.mkdir(parents=True)
    recorded = work / "seen-path"
    script = binaries / "fake-checker"
    script.write_text(
        f'#!/bin/sh\nprintf "%s" "$PATH" > {recorded}\n'
        "printf '{\"generalDiagnostics\": []}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    assert file_diagnostics(str(file), [".venv/bin/fake-checker"])["blocking"] == []
    assert recorded.read_text(encoding="utf-8").startswith(f"{binaries}{os.pathsep}")


def test_a_file_the_checker_cannot_read_is_not_checked(tmp_path: Path) -> None:
    """A type checker handed a manifest reports the manifest as broken.

    Every line of `pyproject.toml` is a syntax error to a Python parser, so an
    edit to one answered with the checker's whole opinion of the file — dozens
    of errors about lines the edit never touched, none of them true. Output
    that is noise on sight is output a reader learns to scroll past, including
    on the edits where it was worth reading.
    """
    work = checkout(tmp_path / "repo")
    manifest = work / "pyproject.toml"
    manifest.write_text('[project]\nname = "x"\n', encoding="utf-8")
    command = checker(work, report(manifest))

    assert file_diagnostics(str(manifest), command)["blocking"] == []


def test_a_file_holding_a_merge_open_is_not_checked(tmp_path: Path) -> None:
    """Conflict markers are not source, so the checker's verdict is about them.

    The same failure as the manifest above, arriving where it costs most. A
    resolution is edits to a file that does not parse until the last marker
    goes, so every edit until then answers with the checker's opinion of the
    markers — and the reader making those edits is the one who can least
    afford to sort a real finding out of it.
    """
    work = checkout(tmp_path / "repo")
    file = work / "module.py"
    file.write_text(
        "<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> other\n", encoding="utf-8"
    )
    command = checker(work, report(file))

    assert file_diagnostics(str(file), command)["blocking"] == []


def test_a_file_quoting_one_marker_is_still_checked(tmp_path: Path) -> None:
    """A lone marker is reachable in honest text and silences nothing.

    A conflict writes the pair, so requiring both costs nothing it was meant
    to catch — while answering to one would drop the checker for a file whose
    docstring quotes a diff, or for the fixtures of this very rule.
    """
    work = checkout(tmp_path / "repo")
    file = work / "module.py"
    file.write_text('x = """\n<<<<<<< quoted, not conflicted\n"""\n', encoding="utf-8")
    command = checker(work, report(file))

    assert file_diagnostics(str(file), command)["blocking"] == [
        "module.py:1: error: something is wrong",
    ]


def test_the_readable_suffixes_are_the_callers_to_choose(tmp_path: Path) -> None:
    """The checker is the caller's, so what it reads is declared, not assumed."""
    work = checkout(tmp_path / "repo")
    file = edited(work, "module.qs")
    command = checker(work, report(file))

    assert file_diagnostics(str(file), command)["blocking"] == []
    assert file_diagnostics(str(file), command, suffixes=(".qs",))["blocking"] == [
        "module.qs:1: error: something is wrong",
    ]


def test_a_diagnostic_about_another_file_is_not(tmp_path: Path) -> None:
    """The checker resolves what the file imports, so it can see the whole tree.

    Repeating that would answer every edit with the same standing backlog,
    most of it about files this edit never touched.
    """
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = checker(work, report(work / "elsewhere.py"))

    assert file_diagnostics(str(file), command)["blocking"] == []


def test_an_informational_note_is_not_a_diagnostic(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = checker(work, report(file, severity="information"))

    assert file_diagnostics(str(file), command)["blocking"] == []


def test_no_declared_checker_reports_nothing(tmp_path: Path) -> None:
    """Empty declares no checker, rather than guessing at one."""
    work = checkout(tmp_path / "repo")

    assert file_diagnostics(str(edited(work)), [])["blocking"] == []


def test_a_checker_that_is_not_installed_reports_nothing(tmp_path: Path) -> None:
    """A missing checker is not evidence about the edit."""
    work = checkout(tmp_path / "repo")

    assert file_diagnostics(str(edited(work)), ["nowhere/pyright"])["blocking"] == []


def test_a_checker_that_writes_nonsense_reports_nothing(tmp_path: Path) -> None:
    """This runs after the tool, so the alternative to silence is failing an
    edit that already happened."""
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = checker(work, "not json at all")

    assert file_diagnostics(str(file), command)["blocking"] == []


def test_a_corrupt_record_reads_as_none(tmp_path: Path) -> None:
    """This only refines a root the caller already has, so it may decline."""
    destination = tmp_path / "edition.json"
    destination.write_text("{ not json", encoding="utf-8")

    assert read_edition(destination) is None


def test_a_missing_record_reads_as_none(tmp_path: Path) -> None:
    assert read_edition(tmp_path / "nowhere.json") is None


def test_a_record_naming_a_removed_workspace_reads_as_none(tmp_path: Path) -> None:
    """Landing a branch deletes its worktree; the record it published stays.

    A reader that trusts the dead path resolves every relative question
    against it and answers none of them, which is worse than the fallback
    it was refining — that one is rooted somewhere that exists.
    """
    workspace = tmp_path / "gone"
    workspace.mkdir()
    destination = tmp_path / "edition.json"
    destination.write_text(
        Edition(workspace=workspace, file=workspace / "edited.py").model_dump_json(),
        encoding="utf-8",
    )
    assert read_edition(destination) is not None

    workspace.rmdir()

    assert read_edition(destination) is None


def test_the_published_bytes_are_the_declared_fields(tmp_path: Path) -> None:
    """The hook writes JSON by hand; drift here is drift in the contract."""
    work = checkout(tmp_path / "repo")

    publish_edition(str(edited(work)), str(work))

    written = json.loads(edition_path(work).read_text(encoding="utf-8"))
    assert set(written) == set(Edition.model_fields)


def pending_report(file: Path, rule: str, line: int) -> str:
    return json.dumps(
        {
            "generalDiagnostics": [
                {
                    "file": str(file),
                    "severity": "error",
                    "rule": rule,
                    "range": {"start": {"line": line}},
                    "message": "not there yet",
                }
            ]
        }
    )


def test_a_name_used_before_it_is_supplied_is_context(tmp_path: Path) -> None:
    """A change spanning two edits reports its use before its definition.

    Labelled a blocking error, it arrived dozens of times per change while
    four builders worked in parallel, each time about a name the next edit
    wrote. It is said, and said as what it is.
    """
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = checker(work, pending_report(file, "reportUndefinedVariable", 0))

    found = file_diagnostics(str(file), command)

    assert found["blocking"] == []
    assert found["context"][1:] == ["module.py:1: error: not there yet"]


def test_an_unknown_symbol_on_an_import_line_is_context(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = work / "module.py"
    file.write_text("from lup import not_yet\n\nx = not_yet\n", encoding="utf-8")
    command = checker(work, pending_report(file, "reportAttributeAccessIssue", 0))

    assert file_diagnostics(str(file), command)["blocking"] == []


def test_an_unknown_attribute_elsewhere_still_blocks(tmp_path: Path) -> None:
    """The same rule off an import line is a real mistake, not a pending one."""
    work = checkout(tmp_path / "repo")
    file = work / "module.py"
    file.write_text("import os\n\nx = os.nope\n", encoding="utf-8")
    command = checker(work, pending_report(file, "reportAttributeAccessIssue", 2))

    assert file_diagnostics(str(file), command)["blocking"] == [
        "module.py:3: error: not there yet"
    ]


def findings(file: str, line: int = 2, kind: str = "missing") -> str:
    return json.dumps(
        {
            "repaired": [],
            "findings": [
                {
                    "file": file,
                    "line": line,
                    "kind": kind,
                    "rule_id": "native-spelling",
                    "message": "neutral module contains a wire word",
                    "text": "x",
                }
            ],
        }
    )


def test_what_the_sweep_still_refuses_comes_back_with_the_file(tmp_path: Path) -> None:
    """The sweep is the whole-tree check scoped to the file, every rule over
    every span, so a verdict the gate ahead of the write cannot reach is
    reported per write rather than first met at the end."""
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = sweep(work, findings("module.py"))

    swept = swept_files([str(file)], command)[str(file)]

    assert swept["refused"] == [
        {
            "line": 2,
            "rule_id": "native-spelling",
            "kind": "missing",
            "message": "neutral module contains a wire word",
        }
    ]
    assert swept["written"] == "x = 1\n"


def test_an_advisory_finding_is_not_a_refusal(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = sweep(work, findings("module.py", kind="untyped"))

    assert swept_files([str(file)], command)[str(file)]["refused"] == []


def test_every_file_of_a_checkout_is_swept_in_one_run(tmp_path: Path) -> None:
    """Starting the sweep is most of what it costs, so a command that wrote
    several files pays for it once."""
    work = checkout(tmp_path / "repo")
    first, second = edited(work, "first.py"), edited(work, "second.py")
    command = sweep(work, json.dumps({"repaired": []}))

    swept = swept_files([str(first), str(second)], command)

    assert set(swept) == {str(first), str(second)}
    recorded = (work / "sweep-arguments").read_text(encoding="utf-8")
    assert "--path first.py --path second.py" in recorded
