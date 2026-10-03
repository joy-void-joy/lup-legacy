"""A relocation carries the module's own file, not only its importers.

The command is named for moving a module, and one that only repointed imports
would report success over a tree where nothing resolves. The type check would
catch that — somewhere else, one step later, naming an unresolved import
rather than the command that had just produced it.
"""

from pathlib import Path

from lup.devtools.dev.relocate import Relocation, carry_module, occupied


def module_at(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("value = 1\n", encoding="utf-8")
    return path


def test_the_file_lands_where_its_new_name_spells(tmp_path: Path) -> None:
    source = module_at(tmp_path, "pkg", "old_home")

    carried = carry_module(
        [tmp_path], Relocation(old=["pkg", "old_home"], new=["pkg", "new_home"])
    )

    assert carried is not None
    assert carried.new == tmp_path / "pkg" / "new_home.py"
    assert not source.exists()
    assert carried.new.read_text(encoding="utf-8") == "value = 1\n"


def test_a_module_may_change_depth(tmp_path: Path) -> None:
    """The ordinary relocation: a flat module moving under a subpackage."""
    module_at(tmp_path, "pkg", "flat")

    carried = carry_module(
        [tmp_path], Relocation(old=["pkg", "flat"], new=["pkg", "nested", "flat"])
    )

    assert carried is not None
    assert carried.new == tmp_path / "pkg" / "nested" / "flat.py"


def test_a_source_that_is_not_there_moves_nothing(tmp_path: Path) -> None:
    """The caller who moved the file first and is repointing imports after.

    Refusing that would punish the tidier sequence for arriving in the other
    order, so it is quiet rather than an error.
    """
    assert (
        carry_module([tmp_path], Relocation(old=["pkg", "gone"], new=["pkg", "new"]))
        is None
    )


def test_an_occupied_destination_is_left_alone(tmp_path: Path) -> None:
    """Overwriting one module with another is not a relocation."""
    module_at(tmp_path, "pkg", "old_home")
    existing = module_at(tmp_path, "pkg", "new_home")
    existing.write_text("standing = True\n", encoding="utf-8")

    carried = carry_module(
        [tmp_path], Relocation(old=["pkg", "old_home"], new=["pkg", "new_home"])
    )

    assert carried is None
    assert existing.read_text(encoding="utf-8") == "standing = True\n"
    assert (tmp_path / "pkg" / "old_home.py").exists()


def test_an_occupied_destination_is_named_before_anything_moves(
    tmp_path: Path,
) -> None:
    """The relocation that cannot land is found while the tree is still whole.

    Carrying nothing and rewriting every importer anyway would aim them all at
    the module already standing at the new name, and report that as a
    relocation. Asked first, it is refused before either half runs.
    """
    source = module_at(tmp_path, "pkg", "old_home")
    existing = module_at(tmp_path, "pkg", "new_home")
    free = Relocation(old=["pkg", "old_home"], new=["pkg", "elsewhere"])
    taken = Relocation(old=["pkg", "old_home"], new=["pkg", "new_home"])
    already_moved = Relocation(old=["pkg", "gone"], new=["pkg", "new_home"])

    assert occupied([tmp_path], [free, already_moved]) == []
    [refused] = occupied([tmp_path], [free, taken])
    assert (refused.old, refused.new) == (source, existing)


def test_a_module_crossing_packages_lands_in_the_root_that_owns_the_name(
    tmp_path: Path,
) -> None:
    """The move a two-package repository makes: an application's becomes the library's.

    Resolving the destination against the root the file came from wrote a
    second `lup/` tree beside the application, and every import repointed at
    the library then resolved to nothing. The distribution root is a root of
    its own so a sweep finds every importer, and it holds a directory named
    for the package that no import resolves against — so the destination is
    the root holding the name as a package, not the one spelling it.
    """
    application = tmp_path / "src"
    distribution = tmp_path / "packages"
    library = distribution / "lup" / "src"
    module_at(application, "app_package", "library")
    module_at(library, "lup", "devtools", "placed")
    (library / "lup" / "__init__.py").write_text("", encoding="utf-8")

    carried = carry_module(
        [application, distribution, library],
        Relocation(
            old=["app_package", "library"], new=["lup", "devtools", "dev", "library"]
        ),
    )

    assert carried is not None
    assert carried.new == library / "lup" / "devtools" / "dev" / "library.py"
    assert not (application / "lup").exists()


def test_the_first_root_holding_the_module_is_the_one_that_moves(
    tmp_path: Path,
) -> None:
    """Roots are searched, not assumed: the two halves are separate trees."""
    first = tmp_path / "library"
    second = tmp_path / "application"
    first.mkdir()
    second.mkdir()
    module_at(second, "pkg", "shared")

    carried = carry_module(
        [first, second], Relocation(old=["pkg", "shared"], new=["pkg", "moved"])
    )

    assert carried is not None
    assert carried.new == second / "pkg" / "moved.py"
