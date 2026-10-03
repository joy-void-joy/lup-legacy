#!/usr/bin/env python3
# lup: ignore[argparse, subprocess]
# A standalone container program. Its only dependencies are the standard
# library and the image's jq; provider-owned JSON is transported, not interpreted.
"""Apply a selected host login once per change, retaining private container state."""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def chosen_login(incoming, keys=None):
    """The records of a login that are applied: the named keys, or the whole file."""
    if not keys:
        return incoming
    return {key: incoming[key] for key in keys if key in incoming}


def login_fingerprint(login):
    """What a stamp records of one applied login, so the same login is applied once."""
    return hashlib.sha256(
        json.dumps(login, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def stamp_of(stored):
    """The file beside a stored login recording the fingerprint last applied to it."""
    return stored.with_name(f".lup-seeded-{stored.name}.sha256")


def seed_login(seed, stored, keys=None, renewable=""):
    """Copy a changed login while preserving other records in a shared file."""
    keys = keys or []

    def read(path):
        if not path.exists() or not path.stat().st_size:
            return {}, b""
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError(f"Unreadable credential JSON: {path}") from None
        if not isinstance(data, dict):
            raise ValueError(f"Credential JSON must be an object: {path}")
        return data, raw

    def usable(data, raw):
        if not data:
            return False
        if not renewable:
            return True
        return (
            subprocess.run(
                ["jq", "-e", renewable],
                input=raw,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            ).returncode
            == 0
        )

    def replace(path, raw):
        descriptor, temporary = tempfile.mkstemp(prefix=".lup-login-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            Path(temporary).replace(path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    stored.parent.mkdir(parents=True, exist_ok=True)
    with (stored.parent / ".lup-login-handoff.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        incoming, raw = read(seed)
        login = chosen_login(incoming, keys)
        if not login or not usable(incoming, raw):
            return False
        current, current_raw = read(stored)
        fingerprint = login_fingerprint(login)
        stamp = stamp_of(stored)
        if (
            stamp.exists()
            and stamp.read_text() == fingerprint
            and usable(current, current_raw)
        ):
            return False
        if keys:
            for key in keys:
                if key in incoming:
                    current[key] = incoming[key]
                else:
                    current.pop(key, None)
            raw = (json.dumps(current, indent=2) + "\n").encode("utf-8")
        replace(stored, raw)
        replace(stamp, fingerprint.encode("ascii"))
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed", type=Path)
    parser.add_argument("stored", type=Path)
    parser.add_argument("--keys", type=json.loads, default=[])
    parser.add_argument("--renewable", default="")
    arguments = parser.parse_args()
    try:
        if seed_login(
            arguments.seed, arguments.stored, arguments.keys, arguments.renewable
        ):
            print(
                "lup: selected host login applied; container-only credentials retained.",
                file=sys.stderr,
            )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(
            f"lup: login handoff failed ({type(error).__name__}); no credential content is logged.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
