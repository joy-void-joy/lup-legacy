# lup: ignore[string-split]
# Python's dotted module names and repository paths are the syntax judged here.
"""Static import ownership, without provider names or imported project code."""

import ast
import posixpath
from collections.abc import Iterator
from typing import TypedDict

from .rows import AntiPatternRow, ImportBoundaryRow


class ImportReference(TypedDict):
    line: int
    end_line: int
    module: str


class ImportViolation(ImportReference):
    rule_id: str
    message: str


class ResolvedImportRule(TypedDict):
    row: AntiPatternRow
    spans: dict[int, int]


def owns_path(path: str, roots: list[str]) -> bool:
    """Match normalized repository-relative paths, never substring lookalikes."""
    normalized = posixpath.normpath(path)
    return any(
        (not posixpath.isabs(normalized) and not normalized.startswith("../"))
        if root == "./"
        else normalized.startswith(root)
        if root.endswith("/")
        else normalized == root
        for root in roots
    )


def package_name(path: str, source_roots: list[str]) -> str:
    """Resolve one source file's package through the declared source roots."""
    normalized = posixpath.normpath(path)
    for root in sorted(source_roots, key=len, reverse=True):
        folder = posixpath.normpath(root)
        prefix = "" if folder == "." else posixpath.join(folder, "")
        if normalized.startswith(prefix):
            relative = normalized.removeprefix(prefix)
            return ".".join(posixpath.dirname(relative).split("/"))
    return ""


def import_references(tree: ast.AST, package: str) -> Iterator[ImportReference]:
    """Read imports, including parent-package aliases and relative imports.

    These are syntax dependencies, not arbitrary Python execution: computed
    import names and code passed to exec remain a runtime's responsibility.
    """
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                for name in names:
                    yield ImportReference(
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        module=name.name,
                    )
            case ast.ImportFrom(module=module, names=names, level=level):
                base = module or ""
                if level:
                    parts = package.split(".") if package else []
                    if len(parts) < level:
                        continue
                    base = ".".join(
                        [*parts[: len(parts) - level + 1], *([base] if base else [])]
                    )
                yield ImportReference(
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    module=base,
                )
                for name in names:
                    yield ImportReference(
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        module=f"{base}.{name.name}",
                    )


def import_violations(
    path: str, text: str, boundaries: list[ImportBoundaryRow]
) -> list[ImportViolation]:
    """Every disallowed static dependency, before suppression handling."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    # lup: ignore[empty-collection] — indexed fold retains the first reference per module and statement
    findings: dict[int, dict[str, dict[str, ImportViolation]]] = {}
    for boundary in boundaries:
        if owns_path(path, boundary["owners"]):
            continue
        for reference in import_references(
            tree, package_name(path, boundary["source_roots"])
        ):
            module = reference["module"]
            for prefix in boundary["modules"]:
                if not (
                    module == prefix
                    or module.startswith(prefix + ".")
                    or (
                        module.endswith(".*")
                        and prefix.startswith(module.removesuffix("*"))
                    )
                ):
                    continue
                by_rule = findings.setdefault(reference["line"], {})
                by_module = by_rule.setdefault(boundary["rule_id"], {})
                by_module.setdefault(
                    prefix,
                    ImportViolation(
                        **reference,
                        rule_id=boundary["rule_id"],
                        message=boundary["message"],
                    ),
                )
    return [
        finding
        for by_rule in findings.values()
        for by_module in by_rule.values()
        for finding in by_module.values()
    ]


def resolved_import_rules(
    path: str, text: str, boundaries: list[ImportBoundaryRow]
) -> list[ResolvedImportRule]:
    """Feed the shared suppression/approval gate with AST-resolved line sets."""
    violations = import_violations(path, text, boundaries)
    messages = {boundary["rule_id"]: boundary["message"] for boundary in boundaries}
    return [
        ResolvedImportRule(
            row=AntiPatternRow(
                id=rule_id,
                pattern=r"(?!)",
                message=message,
                remedy="",
                context="code",
                matcher="",
                strength="soft",
                resolution="",
            ),
            spans={
                violation["line"]: violation["end_line"]
                for violation in violations
                if violation["rule_id"] == rule_id
            },
        )
        for rule_id, message in messages.items()
    ]
