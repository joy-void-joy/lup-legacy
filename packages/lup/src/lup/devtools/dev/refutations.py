# lup: ignore[string-split]
# A dotted module name is the language's own structure and has no parser but
# this one: splitting it is how a relative import's package prefix and a
# module's parent are named, exactly as `lup.harness.codescan.project` names them.
"""Remembering what a language server already resolved about a file.

Refuting a rule costs a language-server session, and the gate pays it every
run over files that mostly did not change. What a refutation depends on is
exactly stateable, though: the file's own text, the rules asked of it, the
environment the checker resolves in, and the text of every first-party module
its imports reach — because following imports is how the checker decides what
a receiver's type is declared as.

So an entry is keyed by everything it depends on rather than invalidated by
watching for changes. A stale entry cannot be read as fresh: a changed
dependency digests differently, a different digest is a different key, and a
different key is a miss. Nothing here decides a rule; it decides only whether
the last answer is still about the same code.
"""

import ast
import json
from collections import deque
from pathlib import Path

from pydantic import BaseModel, ValidationError

from lup.formats import digest
from lup.harness.codescan.common import AntiPattern, PythonSource, Refutation
from lup.harness.codescan.oracle import TypeOracle
from lup.harness.codescan.resolution import refute
from lup.policy.kernel.edit import python_nodes, python_tree
from lup.types import StringMap


class FileRefutations(BaseModel, frozen=True):
    """One file's refutations, under the digest of everything they rest on."""

    key: str
    refutations: list[Refutation]


class RefutationStore(BaseModel):
    """Every remembered answer, by the repository-relative path it is about."""

    entries: dict[str, FileRefutations]

    @classmethod
    def read(cls, path: Path) -> "RefutationStore":
        """The store on disk, or an empty one where none can be read.

        A cache that cannot be read is a cache miss, never an error: every way
        this fails — absent, truncated, written by an older shape — leaves the
        sweep asking the checker exactly as it would have asked anyway.
        """
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValidationError, json.JSONDecodeError):
            return cls(entries={})

    def write(self, path: Path) -> None:
        """Replace the store on disk, ignoring a tree that will not take it."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.model_dump_json(), encoding="utf-8")
        except OSError:
            return  # a cache that cannot be written is one that stays cold


def imported_modules(tree: ast.Module, module: str) -> list[str]:
    """Every module one file's imports name, relative ones made absolute."""
    package = module.split(".")
    return [
        named
        for node in python_nodes(tree)
        for named in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [
                ".".join(
                    [
                        *(package[: -node.level] if node.level else []),
                        *([node.module] if node.module is not None else []),
                    ]
                )
            ]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    ]


def first_party_imports(sources: list[PythonSource]) -> dict[str, list[str]]:
    """Which of each module's imports name another module in this sweep.

    A third-party import is left out because its text is not ours to digest.
    The environment fingerprint stands for all of them at once, which is the
    right grain: an upgrade moves every declaration resolved through it.
    """
    known = dict.fromkeys(source.module for source in sources)

    def named(source: PythonSource) -> list[str]:
        tree = python_tree(source.text)
        if tree is None:
            return []
        return sorted(
            {
                reached
                for target in imported_modules(tree, source.module)
                for reached in (target, target.rsplit(".", 1)[0])
                if reached in known and reached != source.module
            }
        )

    return {source.module: named(source) for source in sources}


def import_closure(start: str, imports: dict[str, list[str]]) -> list[str]:
    """Every first-party module reachable from one, itself included.

    Reached rather than imported directly: a receiver declared two modules
    away is resolved through the one in between, so a change there changes the
    answer here. A cycle is followed once — what this returns is the set that
    was reached, not the order it was reached in.
    """
    reached = dict.fromkeys([start])
    pending = deque([start])
    while pending:
        current = pending.popleft()
        for target in imports[current] if current in imports else []:
            if target not in reached:
                reached[target] = None
                pending.append(target)
    return sorted(reached)


def environment_fingerprint(rules: list[AntiPattern], root: Path) -> str:
    """What the checker resolves against, beyond this repository's own text.

    The rule set, because a rule asking a different question of a site gets a
    different answer; and the locked dependency set, because a receiver
    declared in an installed package or a stub is resolved out of one, and an
    upgrade moves every such declaration at once.

    A rule states its whole family here, not just its name. Adding a class to
    one changes which subjects belong without changing anything else about
    the rule, and an entry keyed on the name alone would serve back the
    verdict from before the class was there.
    """
    try:
        locked = (root / "uv.lock").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        locked = ""  # unlocked is a state of its own, and every entry shares it
    return digest.parts(
        [
            *(
                f"{rule.id}:{rule.family.name}:{','.join(rule.family.classes)}"
                for rule in rules
                if rule.family is not None
            ),
            digest.text(locked),
        ]
    )


def entry_keys(sources: list[PythonSource], fingerprint: str) -> StringMap:
    """The digest each source's refutations rest on, by repository path.

    Each module's text is digested once and every closure keyed on those
    digests rather than on the text again. A module near the root of the
    import graph is reached from hundreds of closures, and stating it by its
    text at each one hashed the repository over and over — the same
    dependency, named at the size of a digest instead of the size of a file.
    """
    imports = first_party_imports(sources)
    texts = {source.module: digest.parts([source.text]) for source in sources}
    return {
        source.path.as_posix(): digest.parts(
            [
                fingerprint,
                *(
                    part
                    for module in import_closure(source.module, imports)
                    for part in (module, texts[module] if module in texts else "")
                ),
            ]
        )
        for source in sources
    }


def remembered_refutations(
    sources: list[PythonSource],
    oracle: TypeOracle | None,
    rules: list[AntiPattern],
    store_path: Path,
    root: Path,
) -> dict[str, list[Refutation]]:
    """Refute every source, asking the checker only about what changed.

    Without an oracle nothing resolves and nothing is remembered: recording
    "no refutations" from a run that could not ask would serve that silence
    back to a run that could.
    """
    if oracle is None:
        return {}

    keys = entry_keys(sources, environment_fingerprint(rules, root))
    store = RefutationStore.read(store_path)
    held = {
        path: store.entries[path].refutations
        for path, key in keys.items()
        if path in store.entries and store.entries[path].key == key
    }
    asked = [source for source in sources if source.path.as_posix() not in held]
    resolved = refute(asked, oracle, rules)

    # Every file asked about is recorded, the ones that refuted nothing
    # included: an unrecorded answer is a miss, so a file with no refutations
    # would be asked about again every run for an answer that never changes.
    found = {
        source.path.as_posix(): resolved[source.path.as_posix()]
        if source.path.as_posix() in resolved
        else []
        for source in asked
    }
    RefutationStore(
        entries={
            path: FileRefutations(
                key=key,
                refutations=found[path] if path in found else held[path],
            )
            for path, key in keys.items()
        }
    ).write(store_path)

    return {
        path: refutations
        for path, refutations in {**held, **found}.items()
        if refutations
    }
