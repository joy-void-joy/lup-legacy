#!/usr/bin/env python3
# lup: ignore[argparse]
# A standalone container program. Its only dependency is the standard library.
"""Apply a launch's settings seed to a config home without undoing a running session.

A repository's config volume is shared by every session opened in it, and each
launch seeds it with the person's settings. Replacing the files outright
would undo whatever an earlier session, still running, changed in them since
its own launch — its ``/theme`` gone before it could flow back. So each seed
is merged three ways, key by key, leaf by leaf through nested objects:

``base``
    What the previous launch seeded, recorded beside the home in
    ``.lup-seed/`` when it was applied.
``ours``
    What the home holds now.
``theirs``
    What the person's settings say now: this launch's seed.

A key only the home changed keeps the home's value; a key only the settings
changed takes theirs; where both changed to different values the settings
win, and the key is named. With no record yet, the seed simply replaces.

The same functions settle the seed on the host before the container starts,
so the launch can say what it overrode and knows what it applied.
"""

import argparse
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

# lup: ignore[constant-declaration] — where a home keeps what it was last seeded
# with, a name the host reading it back and this program both have to use
RECORD = ".lup-seed"

# lup: ignore[constant-declaration] — the lock trust-seed.py takes in the same
# home before it amends the trust document, a name both programs have to use
LOCK = ".lup-trust.lock"


class Absent:
    """A key or file that is not there, told apart from one holding ``null``."""

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT = Absent()

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None
"""A value a settings file holds."""

type Seeded = Json | Absent
"""A value, or the absence of one."""


class Merged:
    """One value merged three ways, and every key both sides changed differently."""

    def __init__(self, value: Seeded, conflicts: list[str]) -> None:
        self.value = value
        self.conflicts = conflicts


def entry(side: Seeded, key: str) -> Seeded:
    """One key of an object side, absent where the side is no object or lacks it."""
    if isinstance(side, dict) and key in side:
        return side[key]
    return ABSENT


def present(merged: dict[str, Merged]) -> dict[str, Json]:
    """The merged keys that hold a value, without the ones merged away."""
    return {
        key: value
        for key, held in merged.items()
        if not isinstance(value := held.value, Absent)
    }


def three_way(
    base: Seeded, ours: Seeded, theirs: Seeded, path: list[str] | None = None
) -> Merged:
    """Merge one value three ways, descending into objects only where both changed them.

    Where one side left the value as the last launch had it, the other side's
    value stands whole — removal included. Where both changed an object, it
    is merged key by key; where both changed anything else differently, the
    person's settings win and the key is named.
    """
    where = path or []
    if ours == base:
        return Merged(theirs, [])
    if theirs in (base, ours):
        return Merged(ours, [])
    if isinstance(ours, dict) and isinstance(theirs, dict):
        keys = dict.fromkeys(
            [*ours, *theirs, *(base if isinstance(base, dict) else ours)]
        )
        inner = {
            key: three_way(
                entry(base, key), entry(ours, key), entry(theirs, key), [*where, key]
            )
            for key in keys
        }
        return Merged(
            present(inner),
            [conflict for held in inner.values() for conflict in held.conflicts],
        )
    return Merged(theirs, [".".join(where)])


def loaded(text: str | None) -> Seeded:
    """A file's content as a value: its JSON object, its raw text, or absent."""
    if text is None:
        return ABSENT
    try:
        decoded: Json = json.loads(text)
    except ValueError:
        return text
    return decoded if isinstance(decoded, dict) else text


def dumped(value: Seeded) -> str | None:
    """A value as the file that holds it, or ``None`` where it is absent."""
    if isinstance(value, Absent):
        return None
    if isinstance(value, dict):
        return json.dumps(value, indent=2) + "\n"
    return str(value)


class SeedFile:
    """One file by its name relative to a seed or a home, and its text.

    ``None`` stands for a file that is to be removed.
    """

    def __init__(self, name: str, text: str | None) -> None:
        self.name = name
        self.text = text


def text_of(files: list[SeedFile], name: str) -> str | None:
    """The text of the named file, ``None`` where there is none."""
    return next((held.text for held in files if held.name == name), None)


class Settled:
    """What one seeded file comes to in a home, and the keys the seed overrode there."""

    def __init__(self, file: SeedFile, conflicts: list[str]) -> None:
        self.file = file
        self.conflicts = conflicts


def replaced(seed: list[SeedFile], home: list[SeedFile], name: str) -> Settled:
    """One file the seed owns whole, merged three ways."""
    recorded = text_of(home, f"{RECORD}/managed") is not None
    ours = loaded(text_of(home, name))
    merged = three_way(
        loaded(text_of(home, f"{RECORD}/replace/{name}")) if recorded else ours,
        ours,
        loaded(text_of(seed, f"replace/{name}")),
    )
    return Settled(
        SeedFile(name, dumped(merged.value)),
        [f"{name} {key}".strip() for key in merged.conflicts],
    )


def merged_into(seed: list[SeedFile], home: list[SeedFile], name: str) -> Settled:
    """One document the seed owns only some keys of, those keys merged three ways."""
    recorded = text_of(home, f"{RECORD}/managed") is not None
    theirs = loaded(text_of(seed, f"merge/{name}"))
    document = loaded(text_of(home, name))
    whole = document if isinstance(document, dict) else {}
    before = loaded(text_of(home, f"{RECORD}/merge/{name}")) if recorded else ABSENT
    owned = [
        *(theirs if isinstance(theirs, dict) else {}),
        *(before if isinstance(before, dict) else {}),
    ]
    ours: dict[str, Json] = {key: whole[key] for key in owned if key in whole}
    merged = three_way(before if recorded else ours, ours, theirs)
    kept = {key: held for key, held in whole.items() if key not in owned}
    applied = merged.value if isinstance(merged.value, dict) else {}
    return Settled(
        SeedFile(name, dumped({**kept, **applied})),
        [f"{name} {key}" for key in merged.conflicts],
    )


def merged_names(seed: list[SeedFile]) -> list[str]:
    """The documents a seed merges keys into."""
    return [
        held.name.removeprefix("merge/")
        for held in seed
        if held.name.startswith("merge/")
    ]


def settle(
    seed: list[SeedFile], home: list[SeedFile], managed: list[str]
) -> list[Settled]:
    """Merge a seed into a home, both given as files by their relative name.

    ``seed`` holds ``replace/<name>`` and ``merge/<name>`` files; ``home``
    holds the home's own files and its record under :data:`RECORD`.
    ``managed`` names the files the seed owns whole; every ``merge/`` file is
    a document the seed owns only the keys of.
    """
    return [
        *(replaced(seed, home, name) for name in managed),
        *(merged_into(seed, home, name) for name in merged_names(seed)),
    ]


def read_tree(directory: Path) -> list[SeedFile]:
    """Every file under a directory, by its relative name."""
    if not directory.is_dir():
        return []
    return [
        SeedFile(str(path.relative_to(directory)), path.read_text(encoding="utf-8"))
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


def write(path: Path, text: str | None) -> None:
    """Write a file atomically, or remove it where the text is ``None``.

    Staged in a temporary file of this writer's own rather than under a fixed
    name: containers start on one home at once, and a staging name they
    shared is truncated by the next writer while the first is still filling
    it, whose rename then publishes a document whose front is NUL bytes.
    """
    if text is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(prefix=f"{path.name}.lup-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(staged).replace(path)
    finally:
        Path(staged).unlink(missing_ok=True)


def apply(seed_dir: Path, home: Path) -> list[str]:
    """Merge the seed at ``seed_dir`` into ``home`` and record it, answering conflicts.

    Under the lock trust-seed.py takes in the same home, since both read a
    document there, merge into it and write it back: interleaved by two
    containers starting at once, whichever renames second drops what the
    other added. A file the merge leaves as the home holds it is not written
    at all: a session running in the home saves the same document holding no
    lock of ours, and a start that rewrote it anyway would drop whatever that
    session saved between the read and the rename -- at every start, rather
    than only at one whose seed changed something.
    """
    managed_file = seed_dir / "managed"
    if not managed_file.is_file():
        return []
    home.mkdir(parents=True, exist_ok=True)
    with (home / LOCK).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        managed = managed_file.read_text(encoding="utf-8").split()
        seed = read_tree(seed_dir)
        record = read_tree(home / RECORD)
        held = [
            *(
                SeedFile(name, (home / name).read_text(encoding="utf-8"))
                for name in [*managed, *merged_names(seed)]
                if (home / name).is_file()
            ),
            *(SeedFile(f"{RECORD}/{item.name}", item.text) for item in record),
        ]
        settled = settle(seed, held, managed)
        for outcome in settled:
            if loaded(outcome.file.text) != loaded(text_of(held, outcome.file.name)):
                write(home / outcome.file.name, outcome.file.text)
        seeded = [item.name for item in seed]
        for stale in record:
            if stale.name not in seeded:
                write(home / RECORD / stale.name, None)
        for item in seed:
            write(home / RECORD / item.name, item.text)
        return [conflict for outcome in settled for conflict in outcome.conflicts]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed", type=Path)
    parser.add_argument("home", type=Path)
    arguments = parser.parse_args()
    for conflict in apply(arguments.seed, arguments.home):
        print(
            f"lup: {conflict} changed in a running session and in your settings; "
            "your settings win",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
