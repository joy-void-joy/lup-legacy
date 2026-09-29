"""Typer commands for Python source, symbol, and import inspection."""

import inspect
from collections import defaultdict
from pathlib import Path
from types import ModuleType
from typing import Annotated

import typer

from lup.workspace.paths import find_nearest_pyproject
from lup.devtools.py.common import (
    fail,
    fail_unresolved,
    find_module_path,
    resolve_object,
)
from lup.devtools.py.imports import (
    ImportEntry,
    collect_imports_from_source,
    find_reverse_imports,
    format_import_entry,
)
from lup.devtools.py.layers import Layers, Move, layers
from lup.devtools.py.info import (
    show_callable_info,
    show_class,
    show_module,
    show_value_info,
)
from lup.devtools.py.search import (
    get_top_level_packages,
    scan_project_symbols,
    scan_module_symbols,
)
from lup.devtools.py.source import format_tree
from lup.devtools.py.text import source_text_matches
from lup.devtools.subapps import subapp

app = typer.Typer(no_args_is_help=True)
SUBAPP = subapp("py", "Python source and module inspection", app)


@app.command("info")
def info_cmd(
    path: Annotated[
        str,
        typer.Argument(
            help="Dotted path: module, module.Class, module.func, module.CONST"
        ),
    ],
    schema: Annotated[
        bool,
        typer.Option("--schema", help="Show JSON schema (Pydantic models)"),
    ] = False,
    private: Annotated[
        bool,
        typer.Option("--private", "-p", help="Include private members"),
    ] = False,
) -> None:
    """Inspect a Python object — adapts to modules, classes, functions, values."""
    try:
        resolved = resolve_object(path)
    except ValueError as e:
        fail(str(e))
    obj, name = resolved.value, resolved.leaf_name

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"  {path}")
    typer.echo(f"{'=' * 60}")

    match obj:
        case ModuleType():
            show_module(obj, path, private)
        case type():
            show_class(obj, schema, private)
        case _:
            if callable(obj):
                show_callable_info(obj, name)
            else:
                show_value_info(obj)

    typer.echo()


@app.command("source")
def source_cmd(
    path: Annotated[
        str,
        typer.Argument(help="Dotted path: module, module.Class, or module.func"),
    ],
    tree: Annotated[
        bool,
        typer.Option("--tree", "-t", help="Show package file tree instead of source"),
    ] = False,
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", help="Number of lines to show (default all)"),
    ] = 0,
    start: Annotated[
        int,
        typer.Option(
            "--start",
            "-s",
            help="Starting line (file line for modules, offset within the object)",
        ),
    ] = 1,
) -> None:
    """View source code for a Python object, or a package file tree with --tree."""
    if tree:
        file_path = find_module_path(path)
        if file_path is None:
            fail(f"Could not find module '{path}'")
        if file_path.name == "__init__.py":
            package_root = file_path.parent
        else:
            typer.echo(str(file_path))
            return
        typer.echo(f"{package_root.name}/")
        for line in format_tree(package_root):
            typer.echo(line)
        return

    file_path = find_module_path(path)
    try:
        obj = None if file_path is not None else resolve_object(path).value
    except ValueError as e:
        fail_unresolved(path, str(e))

    if file_path is not None or inspect.ismodule(obj):
        file_path = file_path or find_module_path(path)
        if file_path is None or not file_path.exists():
            fail(f"No source file for '{path}'")
        typer.echo(f"# {file_path}")
        source_lines = file_path.read_text().splitlines()
        start_idx = max(0, start - 1)
        if lines > 0:
            selected = source_lines[start_idx : start_idx + lines]
        else:
            selected = source_lines[start_idx:]
        typer.echo(f"# Lines {start_idx + 1}–{start_idx + len(selected)}")
        typer.echo()
        for i, line in enumerate(selected, start=start_idx + 1):
            typer.echo(f"{i:4d}  {line}")
        return

    if not callable(obj):
        fail(f"Cannot get source for '{path}': {type(obj).__name__} has no source")
    try:
        source = inspect.getsource(obj)
        source_file = inspect.getfile(obj)
        _, start_lineno = inspect.getsourcelines(obj)
        typer.echo(f"# {source_file}:{start_lineno}")
    except (TypeError, OSError) as e:
        fail(f"Cannot get source for '{path}': {e}")

    obj_lines = source.splitlines()
    start_idx = max(0, start - 1)
    if lines > 0:
        selected = obj_lines[start_idx : start_idx + lines]
    else:
        selected = obj_lines[start_idx:]
    typer.echo(f"# {len(obj_lines)} lines")
    if len(selected) < len(obj_lines):
        first = start_lineno + start_idx
        last = first + len(selected) - 1
        typer.echo(f"# Showing lines {first}–{last} (--lines 0 for all)")
    typer.echo()
    for i, line in enumerate(selected, start=start_lineno + start_idx):
        typer.echo(f"{i:4d}  {line}")


@app.command("imports")
def imports_cmd(
    module: Annotated[str, typer.Argument(help="Module to analyze")],
    reverse: Annotated[
        bool,
        typer.Option("--reverse", "-r", help="Find what imports this module"),
    ] = False,
    depth: Annotated[
        int,
        typer.Option("--depth", "-d", help="Transitive import depth"),
    ] = 1,
) -> None:
    """Show what a module imports, or what imports it (--reverse)."""
    if reverse:
        root = find_nearest_pyproject()
        if root is None:
            fail("Could not find project root (no pyproject.toml)")
        results = find_reverse_imports(module, root)
        if not results:
            typer.echo(f"No project files import '{module}'")
            return
        typer.echo(f"Files importing '{module}':\n")
        for hit in results:
            typer.echo(f"  {hit['file']}")
            typer.echo(f"    {hit['import_line']}")
        return

    file_path = find_module_path(module)
    if file_path is None:
        fail(f"Could not find module '{module}'")
    if not file_path.exists():
        fail(f"Module file does not exist: {file_path}")

    seen: set[str] = set()  # lup: ignore[set-shape, empty-collection] — visited
    current_modules = [module]

    for current_depth in range(1, depth + 1):
        next_modules: list[str] = []
        if current_depth > 1:
            typer.echo(f"\n--- Depth {current_depth} ---")

        all_entries: list[ImportEntry] = []
        for mod_name in current_modules:
            mod_path = find_module_path(mod_name)
            if mod_path is None or not mod_path.exists():
                continue
            try:
                source = mod_path.read_text()
            except OSError:
                continue
            all_entries.extend(collect_imports_from_source(source))

        grouped: defaultdict[str, list[ImportEntry]] = defaultdict(list)
        for entry in all_entries:
            if entry["module"] in seen:
                continue
            seen.add(entry["module"])
            grouped[entry["category"]].append(entry)
            next_modules.append(entry["module"])

        for category in ("project", "third-party", "stdlib"):
            entries = grouped[category]
            if not entries:
                continue
            typer.echo(f"\n{category} ({len(entries)}):")
            for entry in sorted(entries, key=lambda e: e["module"]):
                typer.echo(f"  {format_import_entry(entry)}")

        current_modules = next_modules


def show_layers(measured: Layers, sites: bool) -> None:
    """Print each entry's size and reach, then every pair that imports both ways."""
    typer.echo(f"{len(measured.entries)} entries in {measured.package}:\n")
    for entry in measured.entries:
        typer.echo(f"  {entry.name}  ({entry.modules} modules, {entry.lines} lines)")
        typer.echo(f"      imports:     {', '.join(entry.imports) or '-'}")
        typer.echo(f"      imported by: {', '.join(entry.imported_by) or '-'}")
    typer.echo(f"\n{len(measured.two_way)} pair(s) importing each other:")
    for pair in measured.two_way:
        closes = "closes at load" if pair.at_load() else "one side deferred or typing"
        typer.echo(
            f"  {pair.forward.importer} <-> {pair.forward.imported}  ({closes}): "
            f"{pair.forward.spelled()} one way, {pair.back.spelled()} back"
        )
        if not sites:
            continue
        for site in [*pair.forward.sites, *pair.back.sites]:
            typer.echo(
                f"      {site.importer} -> {site.imported}  "
                f"{site.path}:{site.line} ({site.kind})"
            )


@app.command("layers")
def layers_cmd(
    package: Annotated[
        str, typer.Argument(help="The package to measure, by import name")
    ],
    move: Annotated[
        list[str] | None,
        typer.Option(
            "--move",
            help="Read module.prefix=entry as already moved there (repeatable)",
        ),
    ] = None,
    sites: Annotated[
        bool,
        typer.Option("--sites", help="List every statement behind each two-way pair"),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show how a package's top-level entries import each other, and which pairs close.

    Each import is read with where it runs — when its module loads, when a
    function is called, or only under a type checker — and the kinds are
    counted apart, since whether a deferred import is an edge is the question
    a layering rule has to settle first. ``--move`` measures a relocation
    before it is made, reading a module prefix as belonging to another entry.
    """

    def parsed(spelled: str) -> Move:
        # This CLI's own flag grammar, not structured data with a parser.
        prefix, separator, entry = spelled.partition("=")  # lup: ignore[string-split]
        if not separator or not prefix or not entry:
            fail(f"expected module.prefix=entry; got {spelled!r}")
        return Move(prefix=prefix, entry=entry)

    source = find_module_path(package)
    if source is None or source.name != "__init__.py":
        fail(f"{package!r} is not an importable package")
    measured = layers(source.parent, [parsed(spelled) for spelled in move or []])
    if as_json:
        typer.echo(measured.model_dump_json(indent=2))
        return
    show_layers(measured, sites)


@app.command("text")
def text_cmd(
    pattern: Annotated[str, typer.Argument(help="Literal text to find")],
    paths: Annotated[
        list[Path], typer.Argument(help="Python files or directories to search")
    ],
    ignore_case: Annotated[
        bool,
        typer.Option("--ignore-case", "-i", help="Match without case sensitivity"),
    ] = False,
    prose: Annotated[
        bool,
        typer.Option(
            "--prose",
            help="Search only docstrings and comments, as the prose rules read them",
        ),
    ] = False,
) -> None:
    """Search literal source text within explicitly selected Python paths.

    ``--prose`` narrows the search to what a person reads as a sentence, which
    is how a candidate for a prose rule is found before the rule is written.
    """
    try:
        matches = source_text_matches(pattern, paths, ignore_case, prose)
    except (OSError, UnicodeError, ValueError) as error:
        fail(str(error))
    for match in matches:
        typer.echo(f"{match.path}:{match.line_number}: {match.text}")


@app.command("search")
def search_cmd(
    pattern: Annotated[str, typer.Argument(help="Symbol name to search for")],
    package: Annotated[
        list[str] | None,
        typer.Option(
            "--package", "-P", help="Search only these installed package exports"
        ),
    ] = None,
) -> None:
    """Search project source and installed package exports by name."""
    project_root = None if package else find_nearest_pyproject()
    project_matches = (
        scan_project_symbols(project_root, pattern) if project_root else []
    )
    if package:
        packages = list(package)
    else:
        packages = get_top_level_packages()

    if not package:
        project_scope = "project source and " if project_root else ""
        typer.echo(
            f"Scanning {project_scope}{len(packages)} package exports...", err=True
        )

    package_matches = [
        match for pkg in packages for match in scan_module_symbols(pkg, pattern)
    ]
    all_matches = [*project_matches, *package_matches]
    scanned = len(packages)
    package_scope = f"{scanned} package{'s' if scanned != 1 else ''}"
    scope = f"project source and {package_scope}" if project_root else package_scope

    if not all_matches:
        typer.echo(f"No matches for '{pattern}' in {scope}")
        return

    typer.echo(f"Matches for '{pattern}':\n")
    for match in sorted(all_matches, key=lambda m: m["import_path"]):
        typer.echo(f"  {match['kind']:10s}  {match['import_path']}")

    typer.echo(f"\n({len(all_matches)} matches in {scope})")
