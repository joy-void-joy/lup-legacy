"""A relocated notes root must not be a one-way door.

``configure(notes_dir=...)`` exists so part of a session writes into a
directory of its own — an adopter gives its nested agents a notes root inside
an ignored run directory, so their records never reach the tracked tree. Every
path derived from ``notes_path()`` moves with it, which is right for a writer
and wrong for a reader: what other sessions recorded is still where it always
was, and a lookup that follows the override alone searches inside this session
for something kept outside it.

Two things close that. The root's own notes tree stays nameable however the
override moved the process's, and the session-directory walk takes the trees
to search rather than assuming the one it resolved.
"""

from pathlib import Path

from lup.workspace.history import iter_session_dirs, version_dirs
from lup.workspace.paths import checkout_notes_path, configure, notes_path

VERSION = "0.1.0"
SESSION = "20260827_071803_340126_claude_150e8bdf"


def session_dir(notes: Path, session: str = SESSION, version: str = VERSION) -> Path:
    """One session directory, where the trace layout puts it."""
    made = notes / "traces" / version / "sessions" / session
    made.mkdir(parents=True, exist_ok=True)
    return made


def relocate(root: Path) -> Path:
    """Point this process at a notes root inside one session's own record."""
    nested = root / "notes" / "runs" / SESSION / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    configure(notes_dir=nested)
    return nested


class TestCheckoutNotesPath:
    def test_it_answers_the_root_default_with_no_override(
        self, tmp_lup_project: Path
    ) -> None:
        assert checkout_notes_path() == notes_path() == tmp_lup_project / "notes"

    def test_an_override_moves_the_writer_and_not_the_checkout(
        self, tmp_lup_project: Path
    ) -> None:
        """The whole point: the tree an override replaced stays nameable."""
        nested = relocate(tmp_lup_project)

        assert notes_path() == nested
        assert checkout_notes_path() == tmp_lup_project / "notes"

    def test_a_new_root_moves_both(self, tmp_lup_project: Path) -> None:
        """Naming a root is a different statement from naming a notes dir."""
        elsewhere = tmp_lup_project / "elsewhere"
        configure(root=elsewhere)
        try:
            assert checkout_notes_path() == elsewhere / "notes"
            assert notes_path() == elsewhere / "notes"
        finally:
            configure(root=tmp_lup_project)


class TestSessionDirsAcrossRoots:
    def test_the_resolved_root_stays_the_default(self, tmp_lup_project: Path) -> None:
        wanted = session_dir(tmp_lup_project / "notes")

        assert list(iter_session_dirs(session_id=SESSION)) == [wanted]

    def test_a_relocated_process_finds_nothing_without_being_told(
        self, tmp_lup_project: Path
    ) -> None:
        """A walk following the override alone searches inside this session."""
        wanted = session_dir(tmp_lup_project / "notes")
        relocate(tmp_lup_project)

        assert list(iter_session_dirs(session_id=SESSION)) == []
        assert wanted.is_dir()

    def test_naming_the_checkout_tree_finds_it_again(
        self, tmp_lup_project: Path
    ) -> None:
        wanted = session_dir(tmp_lup_project / "notes")
        relocate(tmp_lup_project)

        found = iter_session_dirs(
            session_id=SESSION, roots=[checkout_notes_path() / "traces"]
        )

        assert list(found) == [wanted]

    def test_roots_replace_the_default_rather_than_extending_it(
        self, tmp_lup_project: Path
    ) -> None:
        """A caller able to add a root but never decline one has no choice at all."""
        session_dir(tmp_lup_project / "notes")
        other = tmp_lup_project / "other"
        wanted = session_dir(other)

        assert list(
            iter_session_dirs(session_id=SESSION, roots=[other / "traces"])
        ) == [wanted]

    def test_one_directory_under_two_roots_is_yielded_once(
        self, tmp_lup_project: Path
    ) -> None:
        """Repetition would read as ambiguity to a caller counting the answers."""
        wanted = session_dir(tmp_lup_project / "notes")
        traces = tmp_lup_project / "notes" / "traces"

        found = iter_session_dirs(session_id=SESSION, roots=[traces, traces])

        assert list(found) == [wanted]

    def test_a_root_that_is_not_there_is_skipped(self, tmp_lup_project: Path) -> None:
        wanted = session_dir(tmp_lup_project / "notes")

        found = iter_session_dirs(
            session_id=SESSION,
            roots=[tmp_lup_project / "gone", tmp_lup_project / "notes" / "traces"],
        )

        assert list(found) == [wanted]

    def test_a_named_version_honours_the_roots_too(self, tmp_lup_project: Path) -> None:
        other = tmp_lup_project / "other"
        wanted = session_dir(other)
        relocate(tmp_lup_project)

        found = iter_session_dirs(
            session_id=SESSION, version=VERSION, roots=[other / "traces"]
        )

        assert list(found) == [wanted]

    def test_every_session_under_several_roots_comes_back(
        self, tmp_lup_project: Path
    ) -> None:
        here = session_dir(tmp_lup_project / "notes", session="here")
        there = session_dir(tmp_lup_project / "other", session="there")

        found = iter_session_dirs(
            roots=[
                tmp_lup_project / "notes" / "traces",
                tmp_lup_project / "other" / "traces",
            ]
        )

        assert sorted(found) == sorted([here, there])


class TestVersionDirs:
    def test_it_reads_the_resolved_root_by_default(self, tmp_lup_project: Path) -> None:
        session_dir(tmp_lup_project / "notes")

        assert version_dirs() == [tmp_lup_project / "notes" / "traces" / VERSION]

    def test_it_reads_the_roots_it_is_given(self, tmp_lup_project: Path) -> None:
        session_dir(tmp_lup_project / "other", version="0.2.0")

        found = version_dirs([tmp_lup_project / "other" / "traces"])

        assert found == [tmp_lup_project / "other" / "traces" / "0.2.0"]

    def test_hidden_directories_are_not_versions(self, tmp_lup_project: Path) -> None:
        session_dir(tmp_lup_project / "notes")
        (tmp_lup_project / "notes" / "traces" / ".cache").mkdir()

        assert version_dirs() == [tmp_lup_project / "notes" / "traces" / VERSION]
