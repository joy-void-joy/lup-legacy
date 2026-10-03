"""Helpers shared across ``py`` commands — dotted-path resolution, module paths, failure exit."""

import functools
import importlib
import importlib.util
import pkgutil
import sys
import typing
from pathlib import Path

from pydantic import BaseModel

from lup.devtools.py.search import name_candidates
from lup.diagnostics import refuse
from lup.policy.kernel.diagnostic import devtools, step
from lup.workspace.paths import find_nearest_pyproject

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


class ResolvedObject(BaseModel):
    """A resolved dotted path: the live object and the leaf name that reached it."""

    value: object  # lup: ignore[bare-object] — any importable live object
    leaf_name: str


def leaf_name(path: str) -> str:
    """The name a dotted or colon path ends in, resolved or not.

    Spelled once because both readings need it: the resolver reports which
    name it reached, and the failure reports which name it went looking for.
    """
    module_part, _, attr_part = path.partition(":")  # lup: ignore[string-split]
    tail = attr_part or module_part
    # lup's resolve grammar: the leaf is the last dotted segment.
    return tail.rpartition(".")[2]  # lup: ignore[string-split] — dotted-path leaf


def resolve_object(path: str) -> ResolvedObject:
    """Resolve a dotted or colon path to a Python object, returning (object, leaf_name).

    Accepts both ``module.sub.Object`` (dot form) and the entry-point
    ``module.sub:Object.attr`` (colon form) — the two spellings
    ``pkgutil.resolve_name`` parses; it also walks the module-vs-attribute
    boundary itself, so no hand-rolled prefix-import loop is needed.
    """
    try:
        value = pkgutil.resolve_name(path)
    except (ImportError, AttributeError, ValueError) as e:
        raise ValueError(f"Could not resolve '{path}': {e}") from e
    return ResolvedObject(value=value, leaf_name=leaf_name(path))


def find_module_path(module_name: str) -> Path | None:
    """Find the file path for a module."""
    try:
        spec = importlib.util.find_spec(module_name)
        if spec and spec.origin:
            return Path(spec.origin)
    except (ImportError, ModuleNotFoundError, ValueError):
        pass
    try:
        mod = importlib.import_module(module_name)
        if hasattr(mod, "__file__") and mod.__file__:
            return Path(mod.__file__)
    except (ImportError, ModuleNotFoundError):
        pass
    return None


def fail(msg: str) -> typing.NoReturn:
    refuse(msg)


def fail_unresolved(path: str, reason: str) -> typing.NoReturn:
    """Report what a dotted path could not reach, and where its name lives.

    Such a path is two guesses in one string -- which module, and which name
    -- and the refusal alone says only that the pair did not resolve. The
    name is the half the reader is sure of, so it is searched for and every
    module defining it is named beside the refusal. Without that, locating a
    symbol costs one invocation per module guessed at.

    Whether the module resolved and the attribute did not, or nothing
    resolved at all, is not distinguished: the answer a reader needs is the
    same sentence either way.
    """
    name = leaf_name(path)
    found = name_candidates(name, find_nearest_pyproject())
    refuse(
        reason,
        what=path,
        steps=[
            step(
                f"read the {match['kind']} '{name}' where it is defined",
                devtools("dev", "py", "source", match["import_path"]),
            )
            for match in sorted(found, key=lambda match: match["import_path"])
        ],
    )


@functools.cache
def categorize_import(module_name: str) -> str:
    root = module_name.split(".")[0]  # lup: ignore[string-split] — dotted path
    if root in sys.stdlib_module_names:
        return "stdlib"
    path = find_module_path(root)
    if path is None:
        return "third-party"
    if "site-packages" in str(path):
        return "third-party"
    project_root = find_nearest_pyproject()
    if project_root and str(path).startswith(str(project_root)):
        return "project"
    return "third-party"
