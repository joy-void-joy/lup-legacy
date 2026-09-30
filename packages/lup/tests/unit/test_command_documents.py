"""A command line is read as the shell runs it: each write applied to what came before.

The readers a verdict is built from each answer one question about a whole
line against the files as they stood before it ran. A line that writes one
file twice, or writes a file and then rewrites it, needs the second write read
against what the first left -- and a reviewer shown the change needs one
document per file, before the line and after it. These pin both halves: the
walk that names each step from the words, and the fold that applies them
through the host's own readers.
"""

from functools import partial
from pathlib import Path

import pytest

from lup.policy.assets.host import (
    document_at,
    patched_documents,
    resolved_path,
    sed_output,
)
from lup.policy.kernel.documents import (
    FileStep,
    FollowedReading,
    file_steps,
    followed_documents,
    judged_documents,
    shown_documents,
)
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

ROWS = erase_shell_rules(default_vocabulary())


def steps(command: str) -> list[tuple[str, str, str]]:
    """What the walk names, as (action, path, source) triples."""
    return [
        (step["action"], step["path"], step["source"])
        for step in file_steps(command, ROWS)
    ]


def followed(command: str, root: Path) -> FollowedReading:
    """The line folded over *root* with the readers a dispatcher hands the kernel."""
    return followed_documents(
        file_steps(command, ROWS),
        partial(document_at, root),
        partial(resolved_path, root),
        sed_output,
        partial(patched_documents, root),
    )


def documents(command: str, root: Path) -> dict[str, tuple[str | None, str | None]]:
    """Each file a reviewer is shown, by name under *root*: (before, after)."""
    return {
        str(Path(document["path"]).relative_to(root.resolve())): (
            document["before"],
            document["after"],
        )
        for document in shown_documents(followed(command, root))
    }


def test_sed_names_every_file_and_every_expression() -> None:
    (step, second) = file_steps(
        "sed -i -e 's/a/b/' -e 's/c/d/' first.md second.md", ROWS
    )
    assert (step["action"], step["path"]) == ("rewrite", "first.md")
    assert step["scripts"] == ["s/a/b/", "s/c/d/"]
    assert (second["action"], second["path"]) == ("rewrite", "second.md")


def test_a_cd_places_what_follows_it() -> None:
    assert steps("cd pkg && sed -i 's/x/y/' f.py") == [("rewrite", "pkg/f.py", "")]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat > n.py <<'EOF'\nx = 1\nEOF", [("author", "n.py", "")]),
        (
            "printf 'x\\n' > f && echo y >> f",
            [("author", "f", ""), ("append", "f", "")],
        ),
        ("echo x | tee out.txt", [("author", "out.txt", "")]),
        ("cp a.txt b.txt", [("copy", "b.txt", "a.txt")]),
        ("cp a.txt b.txt dir", [("copy", "dir", "a.txt"), ("copy", "dir", "b.txt")]),
        ("mv a.txt b.txt", [("move", "b.txt", "a.txt")]),
        ("install -m 644 a.txt b.txt", [("copy", "b.txt", "a.txt")]),
        ("install -D -m 0755 a.sh bin/a", [("copy", "bin/a", "a.sh")]),
        ("rm -f a.txt b.txt", [("remove", "a.txt", ""), ("remove", "b.txt", "")]),
        ("truncate -s 0 log.txt", [("empty", "log.txt", "")]),
        ("> log.txt", [("empty", "log.txt", "")]),
        (
            "sed -i.bak 's/a/b/' f.txt",
            [("copy", "f.txt.bak", "f.txt"), ("rewrite", "f.txt", "")],
        ),
        ("git apply tmp/p.diff", [("patch", "", "tmp/p.diff")]),
        ("patch -p1 < tmp/p.diff", [("patch", "", "tmp/p.diff")]),
    ],
)
def test_each_write_the_words_state_is_named_in_order(
    command: str, expected: list[tuple[str, str, str]]
) -> None:
    assert steps(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "uv run lup-devtools dev render > docs/api.md",
            [("unknown", "docs/api.md", ""), ("run", "", "")],
        ),
        ("sort -o out.txt in.txt", [("unknown", "out.txt", "")]),
        ("make build | tee build.log", [("run", "", ""), ("unknown", "build.log", "")]),
        ("ruff format f.py", [("run", "", "")]),
        ("python3 tmp/script.py", [("run", "", "")]),
        (
            "for f in a b; do echo x >> $f; done",
            [("run", "", ""), ("unknown", "a", ""), ("unknown", "b", "")],
        ),
        ("sed -i 's/a/b/' $TARGET", [("run", "", "")]),
        # A glob or a brace expansion names whatever it matches as it runs.
        ("rm *.txt", [("run", "", "")]),
        ("sed -i 's/a/b/' *.py", [("run", "", "")]),
        ("cp a{b,c} d", [("run", "", "")]),
        ("cat > *.py <<'EOF'\nx = 1\nEOF", [("run", "", "")]),
        ("rm '*.txt'", [("remove", "*.txt", "")]),
        # Without a strip count `patch` reads names by rules `git apply` does not share.
        ("patch < p.diff", [("run", "", "")]),
        # Index-only, three-way and filtered applies are what only running shows.
        ("git apply --cached p.diff", [("run", "", "")]),
    ],
)
def test_a_result_only_running_produces_is_named_as_that(
    command: str, expected: list[tuple[str, str, str]]
) -> None:
    assert steps(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status && git diff",
        "grep -rn x src",
        "cd docs",
        "sed 's/a/b/' f",
        "git apply --check p.diff",
        "git apply --stat p.diff",
    ],
)
def test_a_line_that_only_reads_names_nothing(command: str) -> None:
    assert steps(command) == []


def test_a_rewrite_reads_non_ascii_text_and_keeps_every_byte(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("the old way — ünïcödé\nkeep\r\n", encoding="utf-8")
    (tmp_path / "second.md").write_text("old — second\n", encoding="utf-8")
    command = "sed -i -e 's/old/new/' -e 's/keep/kept/' notes.md second.md"
    assert documents(command, tmp_path) == {
        "notes.md": (
            "the old way — ünïcödé\nkeep\r\n",
            "the new way — ünïcödé\nkept\r\n",
        ),
        "second.md": ("old — second\n", "new — second\n"),
    }
    assert source.read_bytes() == "the old way — ünïcödé\nkeep\r\n".encode()


def test_a_second_rewrite_reads_what_the_first_left(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("a = 1\n", encoding="utf-8")
    command = "sed -i 's/a/b/' f.py && sed -i 's/b = 1/b = 2/' f.py"
    assert documents(command, tmp_path) == {"f.py": ("a = 1\n", "b = 2\n")}


def test_a_rewrite_reads_the_file_the_line_wrote_before_it(tmp_path: Path) -> None:
    command = "cat > new.py <<'EOF'\nx = 1\nEOF\nsed -i 's/1/2/' new.py"
    reading = followed(command, tmp_path)
    (document,) = reading["documents"]
    assert (document["before"], document["after"]) == (None, "x = 2\n")
    assert document["authored"]
    assert reading["rewrites"] == [
        {"target": "new.py", "path": document["path"], "cause": None}
    ]


def test_copies_moves_and_removals_land_what_they_read(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    (tmp_path / "gone.txt").write_text("bye\n", encoding="utf-8")
    (tmp_path / "dir").mkdir()
    command = (
        "cp a.txt dir && mv b.txt moved.txt && install -m 644 a.txt c.txt"
        " && rm gone.txt"
    )
    assert documents(command, tmp_path) == {
        "dir/a.txt": (None, "alpha\n"),
        "b.txt": ("beta\n", None),
        "moved.txt": (None, "beta\n"),
        "c.txt": (None, "alpha\n"),
        "gone.txt": ("bye\n", None),
    }


def test_a_move_that_may_not_replace_leaves_both_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    assert documents("mv -n a.txt b.txt", tmp_path) == {}
    assert documents("mv -n a.txt c.txt", tmp_path) == {
        "c.txt": (None, "alpha\n"),
        "a.txt": ("alpha\n", None),
    }


def test_several_sources_land_only_in_a_directory(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(f"{name}\n", encoding="utf-8")
    reading = followed("cp a.txt b.txt c.txt", tmp_path)
    assert shown_documents(reading) == []
    assert [row["command"] for row in reading["unpreviewed"]] == [
        "cp a.txt b.txt c.txt"
    ]


def test_a_copy_lands_in_a_directory_the_line_made(tmp_path: Path) -> None:
    """The copy a later rewrite reads is the one an earlier step put there."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 1\n", encoding="utf-8")
    command = (
        "mkdir -p tmp/amend && cp a.py b.py tmp/amend/"
        " && sed -i -e 's/1/2/' tmp/amend/a.py tmp/amend/b.py"
    )
    assert documents(command, tmp_path) == {
        "tmp/amend/a.py": (None, "x = 2\n"),
        "tmp/amend/b.py": (None, "y = 2\n"),
    }
    assert followed(command, tmp_path)["unpreviewed"] == []
    assert not (tmp_path / "tmp").exists()


def test_a_copy_of_nothing_lands_nothing(tmp_path: Path) -> None:
    reading = followed("cp missing.txt b.txt", tmp_path)
    assert reading["documents"] == []
    assert [(row["command"], row["cause"]) for row in reading["unpreviewed"]] == [
        ("cp missing.txt b.txt", "run")
    ]


def test_writes_and_appends_compose_in_order(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    command = (
        "cd sub && printf 'x\\n' > ../f.txt && echo y >> ../f.txt"
        " && echo z | tee -a ../f.txt"
    )
    (tmp_path / "sub").mkdir()
    assert documents(command, tmp_path) == {"f.txt": ("old\n", "x\ny\nz\n")}


def test_a_patch_is_applied_to_a_copy(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("a = 1\nb = 2 — é\n", encoding="utf-8")
    patch = (
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n a = 1\n-b = 2 — é\n"
        "+b = 3 — é\n"
    )
    (tmp_path / "p.diff").write_text(patch, encoding="utf-8")
    for command in ("git apply p.diff", "patch -p1 < p.diff"):
        assert documents(command, tmp_path) == {
            "src/app.py": ("a = 1\nb = 2 — é\n", "a = 1\nb = 3 — é\n")
        }
    assert (tmp_path / "src/app.py").read_text(encoding="utf-8") == "a = 1\nb = 2 — é\n"


def test_a_patch_the_line_wrote_is_applied_to_what_the_line_left(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("a = 1\n", encoding="utf-8")
    command = (
        "sed -i 's/1/2/' app.py && cat > p.diff <<'EOF'\n--- a/app.py\n+++ b/app.py\n"
        "@@ -1 +1 @@\n-a = 2\n+a = 3\nEOF\ngit apply p.diff"
    )
    known = documents(command, tmp_path)
    assert known["app.py"] == ("a = 1\n", "a = 3\n")


def test_a_patch_that_does_not_apply_is_a_result_only_running_shows(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("unrelated\n", encoding="utf-8")
    (tmp_path / "p.diff").write_text(
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n", encoding="utf-8"
    )
    reading = followed("git apply p.diff", tmp_path)
    assert reading["documents"] == []
    assert [row["command"] for row in reading["unpreviewed"]] == ["git apply p.diff"]


def test_what_only_running_produces_is_never_made_up(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    command = "uv run gen > f.py && sed -i 's/1/2/' f.py && ruff format other.py"
    reading = followed(command, tmp_path)
    assert [document["known"] for document in reading["documents"]] == [False]
    assert reading["rewrites"][0]["cause"] == "run"
    assert [(row["command"], row["cause"]) for row in reading["unpreviewed"]] == [
        ("uv run gen > f.py", "run"),
        ("sed -i s/1/2/ f.py", "run"),
        ("ruff format other.py", "run"),
    ]
    assert reading["unpreviewed"][0]["paths"] == [str((tmp_path / "f.py").resolve())]
    assert reading["unpreviewed"][2]["paths"] == []
    assert not (tmp_path / "other.py").exists()


def test_a_later_whole_write_makes_a_run_result_known_again(tmp_path: Path) -> None:
    command = "uv run gen > f.txt && printf 'fixed\\n' > f.txt"
    reading = followed(command, tmp_path)
    (document,) = reading["documents"]
    assert (document["known"], document["after"]) == (True, "fixed\n")
    assert reading["unpreviewed"] == [
        {"command": "uv run gen > f.txt", "paths": [], "cause": "run"}
    ]


def test_the_gates_judge_the_last_document_a_later_run_obscures(
    tmp_path: Path,
) -> None:
    """A write whose file a later step makes unknown is still judged as written."""
    command = "printf 'x = 1  # noqa\\n' > f.py && uv run gen > f.py"
    reading = followed(command, tmp_path)
    (document,) = judged_documents(reading)
    assert (document["known"], document["after"]) == (False, "x = 1  # noqa\n")
    assert shown_documents(reading) == []


def test_a_file_that_is_not_text_is_named_rather_than_shown(tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(b"\x89PNG\x00\xff")
    reading = followed("cp image.png copy.png", tmp_path)
    assert [document["known"] for document in reading["documents"]] == [False]
    assert reading["unpreviewed"] == [
        {
            "command": "cp image.png copy.png",
            "paths": [str((tmp_path / "copy.png").resolve())],
            "cause": "unread",
        }
    ]


def test_removing_a_file_that_is_not_text_is_named_rather_than_shown(
    tmp_path: Path,
) -> None:
    (tmp_path / "image.png").write_bytes(b"\x89PNG\x00\xff")
    reading = followed("rm image.png", tmp_path)
    assert shown_documents(reading) == []
    assert reading["unpreviewed"] == [
        {
            "command": "rm image.png",
            "paths": [str((tmp_path / "image.png").resolve())],
            "cause": "unread",
        }
    ]


def test_every_spelling_of_one_file_is_one_document(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/f.txt").write_text("a\n", encoding="utf-8")
    command = (
        "sed -i 's/a/b/' pkg/f.txt && sed -i 's/b/c/' ./pkg/f.txt"
        " && cd pkg && sed -i 's/c/d/' f.txt"
    )
    reading = followed(command, tmp_path)
    (document,) = reading["documents"]
    assert document["targets"] == ["pkg/f.txt", "./pkg/f.txt"]
    assert document["after"] == "d\n"
    assert [rewrite["target"] for rewrite in reading["rewrites"]] == [
        "pkg/f.txt",
        "./pkg/f.txt",
    ]


def test_a_step_reads_as_it_was_written() -> None:
    """A glob stays a glob and a literal with a space stays one word."""
    walked = file_steps("rm *.txt 'a b.txt' > \"$LOG\"", ROWS)
    assert {step["command"] for step in walked} == {"rm *.txt 'a b.txt' > $LOG"}


def test_steps_carry_the_segment_they_came_from() -> None:
    walked: list[FileStep] = file_steps("cp a b && ls && rm c", ROWS)
    assert [(step["segment"], step["command"]) for step in walked] == [
        (0, "cp a b"),
        (2, "rm c"),
    ]
