"""Module naming, which decides whether cross-module lookups find anything.

A wrong name here fails invisibly: the scanner resolves fewer symbols and
reports fewer findings, which reads exactly like a clean repository.
"""

from pathlib import Path

from lup.harness.codescan.common import module_name


def test_a_package_under_a_distribution_directory_resolves_to_the_package() -> None:
    """`packages/lup` is the distribution; the second `lup` is the package.

    Taking the first match named the distribution directory, so every module
    in the library resolved to `lup.src.lup.*` and matched nothing.
    """
    assert module_name(Path("packages/lup/src/lup/resolver/core.py")) == (
        "lup.resolver.core"
    )


def test_an_application_module_resolves_from_the_root_src_introduces() -> None:
    """Whatever the package under `src/` is called, it is the import root.

    Initialization renames the application's package, and a nested project
    publishes one the library was never told about; a name resolved against
    a list of known roots missed both, and read as `src.<package>.*`.
    """
    assert module_name(Path("src/lup_template/devtools/app.py")) == (
        "lup_template.devtools.app"
    )


def test_a_package_named_src_inside_the_import_root_stays_in_the_name() -> None:
    # The first `src` introduces the root; a subpackage that happens to be
    # called `src` is part of the module path under it.
    assert module_name(Path("src/app/src/build.py")) == "app.src.build"


def test_a_package_init_names_the_package_itself() -> None:
    assert module_name(Path("packages/lup/src/lup/channels/__init__.py")) == (
        "lup.channels"
    )


def test_a_path_naming_no_package_root_is_taken_whole() -> None:
    assert module_name(Path("tests/unit/test_thing.py")) == "tests.unit.test_thing"
