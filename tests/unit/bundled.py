"""Importing an emitted hook module into the test process, over its own runtime.

Every emitted module puts its plugin's `hooks/runtime` directory first on the
search path and imports what that directory holds -- the kernel, the policy
data, the coordination store -- as top-level modules. One process caches those
names for whichever module imported them first: a Codex dispatcher imported
after the Claude one would run on Claude's policy data, allowing the resolver
worker's namespaced name, which only Claude answers to, on any worker that has
run a Claude dispatcher test first. Loading drops what the runtime provides
before each import, so every emitted module reads its own.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def bundled(name: str, emitted: Path) -> ModuleType:
    """Import the module at *emitted* as *name*, its plugin's runtime read fresh."""
    runtime = emitted.resolve().parents[1] / "runtime"
    provided = [
        entry.stem
        for entry in runtime.iterdir()
        if entry.suffix == ".py" or (entry / "__init__.py").is_file()
    ]
    for held in [
        held
        for held in sys.modules
        if any(held == top or held.startswith(f"{top}.") for top in provided)
    ]:
        del sys.modules[held]
    spec = importlib.util.spec_from_file_location(name, emitted.resolve())
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
