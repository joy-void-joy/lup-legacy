# lup: ignore[import-re, re-call, string-replace]
# Renaming IS text surgery over source and config files — every .replace
# rewrites a known literal in place and the import-statement rewriter is a
# regex by nature, so those rules are opted out file-wide.
"""Package renaming for downstream project initialization.

Renames the ``lup_template`` Python package to a project-specific name,
updating imports, dotted string anchors (``resources.files`` and
``mock.patch`` targets, entry-point strings), entry points, and CLI
references, then reports surviving references for manual triage.
Framework vocabulary (``lup_tool``, ``lup-devtools``, ``.lup/``, etc.)
stays unchanged.

Examples::

    $ uv run lup-devtools dev init rename-package myproject
    $ uv run lup-devtools dev init rename-package myproject --dry-run
"""

import re
import shutil
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
import tomlkit
from tomlkit.container import Container
from tomlkit.items import Comment
import typer
from pydantic import BaseModel

from lup.workspace.paths import project_root
from lup.formats.toml import edited_manifest
from lup.devtools.dev.documented import generated_files
from lup.devtools.dev.library import VENDORED_ROOT
from lup.devtools.dev.plugin import set_marketplace_name
from lup.devtools.dev.scaffold import ScaffoldFile, ScaffoldSource
from lup.devtools.dev.tracked import tracked_files
from lup.diagnostics import refuse
from lup_template.harness.catalog import declared_plugin
from lup.execution.shell import git

PACKAGE_IMPORT_RE = re.compile(
    r"""
    (?<![.\w])          # not preceded by dot or word char
    (?:from|import)     # keyword
    \s+
    lup_template        # the package name
    (?=\.|\.|\s|$)      # followed by dot, whitespace, or end
    """,
    re.VERBOSE,
)

PACKAGE_STRING_ANCHOR_RE = re.compile(
    r"""
    ["']                # opening quote
    lup_template        # the package name
    (?=\.)              # dotted module path only — bare literals stay vocabulary
    """,
    re.VERBOSE,
)

# lup: ignore[constant-declaration] — each entry is a name lup itself publishes,
# so what marks a line as framework is lup's vocabulary rather than a preference
FRAMEWORK_MARKERS = {
    "lup_tool",
    "LupMcpTool",
    "lup-tools",
    "lup-devtools",
    "lup-sandbox",
    "lup-mcp",
    "lup@local",
    "lup-template",
    "plugins/lup",
    "plugins/cache/local/lup",
}


def is_framework_reference(line: str) -> bool:
    """Check if a line's ``lup_template`` usage is framework vocabulary, not a package import."""
    return any(marker in line for marker in FRAMEWORK_MARKERS)


def is_renamer_module(path: Path) -> bool:
    """Check if ``path`` is the renamer module — its literals are rename vocabulary."""
    return path.as_posix().endswith("devtools/dev/init.py")


INITIALIZATION_FILES = [
    "devtools/dev/init.py",
    "devtools/dev/app.py",
    "harness/content/skills/init.passage.md",
    "harness/content/skills/install.passage.md",
    "harness/content/modules/examples.py",
    "tests/unit/test_devtools_drop_examples.py",
]
"""Where what initialization removes is the subject, for a caller that does not
say: the commands removing it, the skill running them, the install guidance
leaving the same trees behind, the module declaring the demonstrations, and the
tests driving their removal. Each names a removed path because that is what it
is about, so a scan reporting lines still naming one skips these for the same
reason the rename skips the renamer: their literals are the subject rather
than a reference the command left dangling."""


def about_initialization(path: Path, files: list[str] = INITIALIZATION_FILES) -> bool:
    """Whether ``path`` is one of the files whose subject is what initialization removes."""
    spelled = path.as_posix()
    return any(spelled.endswith(file) for file in files)


def rename_match(matched: str, new_name: str) -> str:
    """Rewrite the package name inside one matched piece of source text."""
    return matched.replace("lup_template", new_name, 1)


def rename_pattern_in_file(
    path: Path, pattern: re.Pattern[str], new_name: str, dry_run: bool
) -> list[str]:
    """Rename ``lup_template`` occurrences matched by ``pattern`` in a single file.

    Returns a list of change descriptions (empty if no changes); a dry run
    detects and describes without writing.
    """
    text = path.read_text()
    changes: list[str] = []

    def replace_match(m: re.Match[str]) -> str:
        full_match = m.group(0)
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start : line_end if line_end != -1 else len(text)]

        if is_framework_reference(line):
            return full_match

        replaced = rename_match(full_match, new_name)
        changes.append(f"  {path}: {full_match!r} -> {replaced!r}")
        return replaced

    new_text = pattern.sub(replace_match, text)
    if not dry_run and new_text != text:
        path.write_text(new_text)
    return changes


class SpeltPath(BaseModel, frozen=True):
    """One place a manifest spells the package, and what that key configures."""

    spelling: str
    configures: str

    def renamed(self, new_name: str) -> str:
        """The same key under the project's own package name."""
        return self.spelling.replace("lup_template", new_name, 1)


class Replacement(BaseModel, frozen=True):
    """One substitution in a manifest, and the line that reports it."""

    before: str
    after: str
    reported: str


DEFAULT_SPELT_PATHS = [
    SpeltPath(
        spelling="lup_template.devtools.main:app",
        configures="devtools application entry point",
    ),
    SpeltPath(
        spelling="lup_template.devtools.dashboard",
        configures="dashboard package data",
    ),
    SpeltPath(
        spelling="lup_template.harness.content",
        configures="harness content package data",
    ),
]
"""Where *this* scaffold's manifest spells its own package.

A default rather than a constant because the keys are a property of the
manifest being renamed: a project laid out differently passes its own, and a
key added here reaches every caller without one.
"""


def rename_in_pyproject(
    path: Path,
    new_name: str,
    dry_run: bool,
    spelt: list[SpeltPath] | None = None,
) -> list[str]:
    """Update pyproject.toml: package name, CLI entry point, every module path.

    ``spelt`` is every remaining key whose *value* names the package, keyed by
    what it configures rather than by the table it happens to sit in, and
    derived by default rather than hardcoded. The table is the part that does
    not hold still: the devtools entry point sits under
    ``[project.entry-points."lup.devtools"]`` rather than
    ``[project.scripts]``, and a match on a whole line spelled for the wrong
    table succeeds at nothing, silently. A renamed project would then keep an
    entry point naming a package that does not exist — failing later, where
    ``lup-devtools`` refuses two registered applications, far from the
    manifest line that left the second one — and ship none of its package
    data.

    A key that moves tables keeps its value, so the value is what is matched.
    """
    keys = DEFAULT_SPELT_PATHS if spelt is None else spelt
    text = path.read_text()

    def rewritten() -> Iterator[Replacement]:
        """Each substitution this manifest admits, in the order it is applied.

        Yielded rather than accumulated because the caller wants both halves —
        the text to write and the lines to report — and a loop building two
        lists in step is how those come to disagree about what was done.
        """
        yield Replacement(
            before='name = "lup-template"',
            after=f'name = "{new_name}"',
            reported=f"  package name: lup-template -> {new_name}",
        )
        yield Replacement(
            before='lup = "lup_template.environment.cli.__main__:app"',
            after=f'{new_name} = "{new_name}.environment.cli.__main__:app"',
            reported=f"  CLI entry point: lup -> {new_name}",
        )
        for key in keys:
            yield Replacement(
                before=key.spelling,
                after=key.renamed(new_name),
                reported=(
                    f"  {key.configures}: {key.spelling} -> {key.renamed(new_name)}"
                ),
            )

    applied = [step for step in rewritten() if step.before in text]
    new_text = text
    for step in applied:
        new_text = new_text.replace(step.before, step.after, 1)
    if not dry_run and new_text != text:
        path.write_text(new_text)
    return [step.reported for step in applied]


def clear_scaffold_flag(path: Path, dry_run: bool) -> list[str]:
    """Drop ``[tool.lup] template`` — this repository has adopted the template.

    Adopting is what turns the scaffold's customization markers from inventory
    into decisions this domain has not made yet, so the flag that says "still
    the scaffold" goes when the name does.

    Edited through tomlkit rather than by matching the line, because the key
    carries an explanatory comment and matching text would put a second copy
    of that comment here to drift against the first — a reworded comment would
    silently stop clearing the flag, and the repository that adopted the
    template would never be told what it still owes. The parser sees the key
    whatever the prose around it says, and preserves the rest of the file's
    formatting.

    The comment goes with it. Deleting the key alone leaves the paragraph that
    explains it standing over nothing, which downstream reads as an
    instruction about a setting that is not there.
    """

    def drop_with_preamble(table: Container, name: str) -> None:
        """Remove one key and the standalone comment lines introducing it."""
        body = table.body
        index = next(
            position
            for position, (key, _) in enumerate(body)
            if key is not None and key.key == name
        )
        start = index
        while start and isinstance(body[start - 1][1], Comment):
            start -= 1
        del body[start : index + 1]

    def cleared(document: tomlkit.TOMLDocument) -> list[str]:
        match document:
            case {"tool": {"lup": {"template": _} as lup}}:
                drop_with_preamble(lup.value, "template")
                return ["  scaffold flag: cleared — dev check now lists open decisions"]
            case _:
                return []

    return edited_manifest(path, cleared, write=not dry_run)


def drop_stale_metadata(
    root: Path, dry_run: bool, distribution: str = "lup-template"
) -> list[str]:
    """Remove the editable install's metadata the old distribution name left.

    setuptools writes ``src/<distribution>.egg-info`` beside the package it
    installs in editable mode, and the `uv sync` a rename asks for writes one
    under the new name without removing the old. Both sit on the import path
    the editable install adds, so ``importlib.metadata`` reads two
    distributions each registering the ``lup.devtools`` application, and
    every `lup-devtools` command refuses, naming both, until the stale one
    goes. It is an ignored build product, so nothing tracked goes with it.
    """
    stale = root / "src" / f"{distribution.replace('-', '_')}.egg-info"
    if not stale.is_dir():
        return []
    if not dry_run:
        shutil.rmtree(stale)
    return [f"  {stale.relative_to(root)}: the old name's install metadata, removed"]


def rename_cli_app_name(cli_path: Path, new_name: str, dry_run: bool) -> list[str]:
    """Update the Typer app name in the CLI module."""
    if not cli_path.exists():
        return []

    text = cli_path.read_text()
    old = 'name="lup"'
    if old not in text:
        return []

    if not dry_run:
        cli_path.write_text(text.replace(old, f'name="{new_name}"', 1))
    return [f"  CLI app name: lup -> {new_name}"]


def find_stale_references(root: Path) -> list[str]:
    """List surviving ``lup_template`` occurrences for manual triage.

    Covers reference forms the rewriting passes deliberately leave alone —
    docstring prose, path fragments, generated-content templates — so
    nothing dangles silently after a rename.

    Read over what the checkout holds under ``src/`` and ``tests/``, tracked
    or not yet added, and never what its ignore rules keep out: a scratch
    tree or a runtime's write journal is nobody's to triage, and a line
    reported from one sits in front of the lines somebody has to repair.

    Passages too, which name the application's paths through layout values:
    a literal left in one goes on naming the old package in every page
    generated from it. Not the passages whose subject is initialization,
    which name the template's package on purpose.
    """
    scan_files = [
        path
        for rel in sorted(
            tracked_files(others=True, suffixes=(".py", ".md"), root=root)
        )
        if PurePosixPath(rel).parts[0] in ["src", "tests"]
        and (path := root / rel).is_file()
        and not is_renamer_module(path)
        and (
            path.suffix == ".py"
            or (path.name.endswith(".passage.md") and not about_initialization(path))
        )
    ]
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        scan_files.append(pyproject)
    return [
        f"  {path.relative_to(root)}:{lineno}: {line.strip()}"
        for path in scan_files
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if "lup_template" in line
    ]


SCAFFOLD_DEMONSTRATIONS = [
    Path("examples"),
    Path("tests/unit/test_policy_examples.py"),
    Path("tests/unit/test_examples_use_the_front_door.py"),
    Path("tests/unit/test_examples_documented_commands.py"),
]
"""What the scaffold ships to demonstrate *itself*, for a caller that does not
say. Each of these composes lup's own runtime against lup's own README — a
front door being opened, a wrapper stack, a policy denying the call it declared
— so a domain that adopted the template inherits a directory of demos for a
library it is merely a consumer of, and the test modules driving them. Its own
examples, if it wants any, are about its own subject and share nothing with
these but a directory name. A fork shipping different demonstrations passes
its own list rather than editing this one."""

SKIPPED_TREES = ["fixtures"]
"""Directory names a mention scan never descends into, for a caller that does
not say. A fixture tree is held by git like any other and skipped all the
same, because it says `examples/` on purpose, as the data a test drives. What
the ignore rules keep out — an environment, a dependency tree, a build's
output — needs no entry here: the scan reads only what the checkout holds."""

UPSTREAM_TREES = [VENDORED_ROOT]
"""Trees whose text is upstream's rather than this project's, for a caller that
does not say: the library, where it is vendored beside the application. What
it names is repaired where upstream writes it, and the next update replaces
the copy here whole."""


def drop_scaffold_demonstrations(
    root: Path,
    dry_run: bool,
    demonstrations: list[Path] = SCAFFOLD_DEMONSTRATIONS,
) -> list[str]:
    """Remove the scaffold's demonstrations of itself.

    Deleted through git rather than the filesystem, so the removal is staged
    the way the package rename beside it is and a tracked file that is somehow
    absent fails loudly instead of being passed over.

    The README is not edited. It is human-owned here, and an adopting domain
    rewrites it about its own subject anyway — so a link into a directory that
    is going belongs to that rewrite rather than to a surgery performed behind
    the owner's back. :func:`surviving_mentions` reports it instead.
    """
    present = [path for path in demonstrations if (root / path).exists()]
    if not dry_run:
        for path in present:
            git("rm", "-r", "--quiet", str(path), _cwd=str(root))
    return [f"  {path.as_posix()}: removed" for path in present]


def undeclined_copies(
    removed: list[Path], source: ScaffoldSource, package: str
) -> list[str]:
    """Each removed path the copied half still carries, spelled as a decline is.

    `dev update` merges upstream's copied half, so a file this project deleted
    that the scaffold still compiles comes back as a conflict the first time
    upstream changes it. Declined in the scaffold declaration, it is absent
    from every scaffold commit instead, and nothing is offered back. A path
    outside every copied root — `examples/` itself — is this project's own from
    its first day and has nothing to decline.
    """
    return [
        copied.upstream().as_posix()
        for path in removed
        for root in source.roots
        if PurePosixPath(path).is_relative_to(root.resolved(package))
        and not source.declines(
            copied := ScaffoldFile(
                root=root,
                relative=PurePosixPath(path).relative_to(root.resolved(package)),
            )
        )
    ]


def mention_pattern(path: Path) -> re.Pattern[str]:
    """How a line names this path, in each spelling one can take.

    A file is named by its path and nothing else. A directory is named two
    ways — as a path, with the separator that makes it one, and as the import
    root a ``-m`` invocation spells with a dot. Both carry that separator on
    purpose: the bare name is an ordinary English word, and matching it alone
    would report every sentence that uses the word. The dot form additionally
    requires a name after it, because a sentence ending in "examples." is
    prose about examples rather than a reference to the package.
    """
    name = re.escape(path.as_posix())
    if path.suffix:
        return re.compile(name)
    return re.compile(rf"{name}/|{name}\.(?=[A-Za-z_])")


def surviving_mentions(
    root: Path,
    removed: list[Path],
    skipped_trees: list[str] = SKIPPED_TREES,
    upstream_trees: list[str] = UPSTREAM_TREES,
) -> list[str]:
    """Every line still naming something that was just removed.

    Reported rather than rewritten, for the reason the rename's own stale-
    reference pass reports: what names a deleted directory is prose, a link,
    or a configuration key, and each wants a different repair that only
    whoever owns the file can choose.

    A file inside what was removed is not scanned. It names its own siblings
    constantly and is going with them, so reporting it would bury the handful
    of lines somebody actually has to repair.

    Nor is anything the checkout does not hold. What is scanned is what git
    lists — tracked, or untracked and not ignored — because the trees its
    ignore rules keep out are exactly the ones naming a removed directory
    most often and owned by nobody: a virtual environment's installed
    packages, a frontend's dependencies, whatever a build left behind.

    Nor what the checkout holds on somebody else's behalf. A generated file is
    repaired at its source, which the scan reads where that source is this
    project's, and regenerated after; the library's own tree is upstream's
    prose about upstream's examples. Either would be a line reported to
    somebody who is not the one to repair it.
    """
    patterns = [mention_pattern(path) for path in removed]
    generated = generated_files(root)
    scanned = [
        path
        for rel in sorted(
            tracked_files(others=True, suffixes=(".py", ".md", ".toml"), root=root)
        )
        if (path := root / rel).is_file()
        and rel not in generated
        and not any(PurePosixPath(rel).is_relative_to(tree) for tree in upstream_trees)
        and not any(part in skipped_trees for part in PurePosixPath(rel).parts)
        and not about_initialization(path)
        and not any(path.is_relative_to(root / going) for going in removed)
    ]
    return [
        f"  {path.relative_to(root)}:{number}: {line.strip()}"
        for path in scanned
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if any(pattern.search(line) for pattern in patterns)
    ]


def rename_package(
    new_name: str,
    dry_run: bool,
) -> None:
    """Rename the lup_template Python package to a project-specific name."""
    if not new_name.isidentifier():
        refuse("is not a valid Python identifier", what=new_name)

    if new_name == "lup_template":
        refuse("is the package's current name", what=new_name)

    root = project_root()
    src_dir = root / "src"
    old_pkg = src_dir / "lup_template"
    new_pkg = src_dir / new_name

    if not old_pkg.is_dir():
        refuse("does not exist, so there is no package to rename", what=str(old_pkg))

    if new_pkg.exists():
        refuse("already exists", what=str(new_pkg))

    all_changes: list[str] = []  # lup: ignore[empty-collection] — change log

    python_files = [
        py_file
        for search_dir in [src_dir, root / "tests"]
        if search_dir.is_dir()
        for py_file in search_dir.rglob("*.py")
    ]

    typer.echo("Import renames:" if dry_run else "Renaming imports...")
    for py_file in sorted(python_files):
        all_changes.extend(
            rename_pattern_in_file(py_file, PACKAGE_IMPORT_RE, new_name, dry_run)
        )

    typer.echo("\nString anchors:" if dry_run else "Renaming string anchors...")
    for py_file in sorted(python_files):
        if not is_renamer_module(py_file):
            all_changes.extend(
                rename_pattern_in_file(
                    py_file, PACKAGE_STRING_ANCHOR_RE, new_name, dry_run
                )
            )

    pyproject = root / "pyproject.toml"
    typer.echo("\npyproject.toml:" if dry_run else "Updating pyproject.toml...")
    all_changes.extend(rename_in_pyproject(pyproject, new_name, dry_run))
    all_changes.extend(clear_scaffold_flag(pyproject, dry_run))

    cli_path = old_pkg / "environment" / "cli" / "__main__.py"
    typer.echo("\nCLI app name:" if dry_run else "Updating CLI app name...")
    all_changes.extend(rename_cli_app_name(cli_path, new_name, dry_run))

    typer.echo("\nMarketplace:" if dry_run else "Naming the plugin marketplace...")
    all_changes.extend(
        f"  {c}"
        for c in set_marketplace_name(root, new_name, declared_plugin().name, dry_run)
    )

    typer.echo(
        "\nInstall metadata:" if dry_run else "Removing stale install metadata..."
    )
    all_changes.extend(drop_stale_metadata(root, dry_run))

    if dry_run:
        typer.echo("\nDirectory rename:")
        all_changes.append(f"  src/lup_template/ -> src/{new_name}/")
    else:
        typer.echo("Renaming package directory...")
        git("mv", str(old_pkg), str(new_pkg), _cwd=str(root))
        all_changes.append(f"  src/lup_template/ -> src/{new_name}/")

    typer.echo()
    if dry_run:
        typer.echo(f"Dry run: {len(all_changes)} changes would be made:")
    else:
        typer.echo(f"Done: {len(all_changes)} changes made:")
    for change in all_changes:
        typer.echo(change)

    if not dry_run:
        stale = find_stale_references(root)
        if stale:
            typer.echo(
                f"\nRemaining lup_template references ({len(stale)}) — review manually:"
            )
            for line in stale:
                typer.echo(line)
        typer.echo("\nNext steps:")
        typer.echo("  uv sync")
        typer.echo("  uv run pyright")
        typer.echo("  uv run ruff check .")
        typer.echo("  uv run pytest")
