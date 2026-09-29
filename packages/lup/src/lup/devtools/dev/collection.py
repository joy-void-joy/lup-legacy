"""Which files a pytest suite collects, read the way pytest reads its configuration.

The gate runs a suite, and the policy judges the files that suite collects as
tests: written whole without a question, held still by an acceptance guard.
Both have to name the same files, so the role is derived from the suite's own
configuration rather than written beside it. A pytest root that derived
nothing left a nested project's `tests/` judged as production source — every
test longer than three lines went to the operator as a size question.

Only the suite's own directory is read. Pytest walks up from where it was
invoked for a configuration, but a `testpaths` it finds above that directory
applies only when pytest is invoked from the directory declaring it, which a
suite run from its own root never is; what is collected then is whatever the
default file patterns match under that root, which is what an unconfigured
directory answers here too.
"""

import configparser
import shlex
from pathlib import Path

from pydantic import BaseModel, field_validator

from lup.types import JsonValue
from lup.workspace.paths import manifest_table


def ini_section(path: Path, section: str) -> dict[str, JsonValue] | None:
    """One section of an INI file, or None where the file does not hold it.

    A file that does not parse holds nothing, for the reason
    :func:`~lup.workspace.paths.manifest_table` gives: the role table is
    compiled whenever the toolchain starts, including while a merge holds
    this file open, and a decode error escaping here would take the whole
    toolchain down at the moment it is needed to repair the conflict.
    """
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return None
    if not parser.has_section(section):
        return None
    return dict(parser.items(section))


def toml_settings(path: Path) -> dict[str, JsonValue] | None:
    """Pytest's settings in a TOML file, or None where it holds none.

    ``pytest.toml`` answers whatever it holds, the way ``pytest.ini`` does;
    a manifest answers only where it carries ``[tool.pytest]`` — pytest's
    native table — or the INI-mode ``[tool.pytest.ini_options]``. One that
    does not parse holds nothing, as :func:`ini_section` says why.
    """
    document = manifest_table(path)
    match (path.name, document):
        case (_, None):
            return None
        case ("pytest.toml" | ".pytest.toml", {"pytest": dict(native)}):
            return native
        case ("pytest.toml" | ".pytest.toml", _):
            return {}
        case (_, {"tool": {"pytest": {"ini_options": dict(options)}}}):
            return options
        case (_, {"tool": {"pytest": dict(native)}}) if native:
            return native
        case _:
            return None


def configured(path: Path) -> dict[str, JsonValue] | None:
    """What one candidate file configures, or None where pytest would pass it by."""
    match path.name:
        case "pytest.toml" | ".pytest.toml" | "pyproject.toml":
            return toml_settings(path)
        case "pytest.ini" | ".pytest.ini":
            return ini_section(path, "pytest") or {}
        case "tox.ini":
            return ini_section(path, "pytest")
        case _:
            return ini_section(path, "tool:pytest")


class PytestCollection(BaseModel, frozen=True, extra="ignore"):
    """The two settings that decide which files one pytest run collects.

    Validated from whatever table configures pytest, so every other setting
    in it passes by unread.
    """

    testpaths: list[str] = []
    """Where collection starts, relative to the suite's directory."""

    python_files: list[str] = ["test_*.py", "*_test.py"]
    """The file patterns collection takes, pytest's own default where unset."""

    @field_validator("testpaths", "python_files", mode="before")
    @classmethod
    def as_arguments(cls, value: JsonValue) -> JsonValue | list[str]:
        """An INI value, or a string in the INI-mode table, split as pytest splits it.

        Both settings are pytest ``args``: whitespace separates entries and
        quoting keeps one together. A TOML array is already the list.
        """
        match value:
            case str(text):
                # lup: ignore[string-split] — shlex is the parser pytest itself
                # reads an `args` setting with
                return shlex.split(text)
            case _:
                return value

    @classmethod
    def read(
        cls,
        directory: Path,
        names: tuple[str, ...] = (
            "pytest.toml",
            ".pytest.toml",
            "pytest.ini",
            ".pytest.ini",
            "pyproject.toml",
            "tox.ini",
            "setup.cfg",
        ),
    ) -> "PytestCollection":
        """The collection settings of the suite rooted at *directory*.

        *names* is pytest's own order of precedence, and the first file that
        configures pytest is the only one read: `pytest.ini` answers even
        where it says nothing, so a table in the manifest beside it is not
        pytest's configuration at all.
        """
        settings = next(
            (
                found
                for name in names
                if (directory / name).is_file()
                and (found := configured(directory / name)) is not None
            ),
            {},
        )
        return cls.model_validate(settings)

    def patterns(self, base: Path) -> list[Path]:
        """The role patterns naming what this suite collects, under *base*.

        Each ``testpaths`` entry whole, conftests and helpers included, because
        pytest imports all of it to run the suite. Without one, the files the
        patterns match from the directory down, and the conftests that
        configure them.
        """
        if self.testpaths:
            return [base / path for path in self.testpaths]
        return [base / "**" / name for name in [*self.python_files, "conftest.py"]]
