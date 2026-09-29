"""Where a project's own environment is, and how to reach the programs in it.

`uv run lup-devtools ...` is the entry point everywhere it can be used,
because parsing the manifest is how it guarantees the environment it starts
in. Two places cannot use it, and both are about the same failure: the
conflict workflow exists to repair a merge that left `pyproject.toml`
unparseable, so every command that would parse it first is already broken —
and the guidance documenting those commands has to name a spelling that
works there.

What that spelling is differs per project, and the only wrong answer is to
pick one. A repository-relative path is right exactly while the environment
lives inside the checkout, which is what `uv` does by default and what
nothing else does: a conda environment, a pyenv version, a system or
user-level install, and `UV_PROJECT_ENVIRONMENT` all put it somewhere no
path relative to the project can name. Writing one in anyway is not a
default a project can override — it is a workflow that silently does not
exist for anybody outside one layout.

So the project answers instead, and is asked each time rather than described
once. Where its environment is and what is installed there are both facts on
disk, and reading them costs a `stat` — which is less than the guidance it
replaces cost anybody who did not match the layout it assumed.
"""

import os
import sysconfig
from pathlib import Path

from lup.policy.assets.host import project_environment

# lup: ignore[constant-declaration] — uv's own variable name, not a judgement
ENVIRONMENT_VARIABLE = "UV_PROJECT_ENVIRONMENT"
"""What `uv` reads to put a project's environment somewhere other than `.venv`."""

# lup: ignore[constant-declaration] — the directory name uv creates by default
DEFAULT_ENVIRONMENT = ".venv"
"""Where `uv sync` writes an environment when nothing redirects it.

Named for what has to *say* it rather than resolve it: the one spelling the
merge guidance documents, and the tests that redirect it. Resolving is
:func:`~lup.policy.assets.host.project_environment`, which carries both names
as signature defaults because the compiler splicing it into each bare hook
script carries functions alone and would leave a constant beside it behind.
"""

CONSOLE_SCRIPT = "lup-devtools"
"""The entry point this package installs, named in its own packaging metadata."""


def console_script(root: Path, name: str = CONSOLE_SCRIPT) -> Path | None:
    """Where *root*'s own environment installs *name*, if it is there.

    Asked of the project rather than of the running interpreter, because they
    are not always the same project: a notice about one checkout can be
    printed by a program started from another, and the answer has to be about
    the checkout in question.

    The scripts directory comes from Python's virtual-environment installation
    scheme. The caller can live outside a virtual environment, so the directory
    containing its own executable does not describe the project's layout.
    """
    environment = project_environment(root)
    scripts = Path(
        sysconfig.get_path(
            "scripts",
            scheme="venv",
            vars={"base": str(environment), "platbase": str(environment)},
        )
    )
    candidate = scripts / name
    return candidate if candidate.is_file() else None


def project_python(root: Path) -> Path | None:
    """The project's generic interpreter, independent of the caller's version.

    A caller running ``python3.14`` can inspect a project using Python 3.13.
    Virtual environments expose the unversioned interpreter on both platforms;
    reusing the caller's basename would miss that valid project environment.
    """
    return console_script(root, name="python.exe" if os.name == "nt" else "python")


def launcher_invocation(root: Path, name: str = CONSOLE_SCRIPT) -> str:
    """How to spell reaching *name* from *root* when `uv` cannot start.

    Inside the checkout it is named by its path, because a ``PATH`` lookup
    would let a sibling worktree's environment answer for this one — every
    checkout has its own, they are interchangeable to a search path, and the
    wrong one is wrong silently. That hazard is what a path spelling exists
    to avoid, and it exists only where environments are per-checkout.

    Anywhere else, the bare name. A shared environment is reached through
    ``PATH`` by design — activating it is what a conda or pyenv user does
    before running anything — and there is no per-checkout copy for the
    lookup to pick between. An absolute path would be worse than useless: it
    is one machine's, and nothing here knows whether the string is about to
    be printed once or written into something a project shares.
    """
    found = console_script(root, name)
    if found is None:
        return name
    try:
        return found.relative_to(root).as_posix()
    except ValueError:
        return name
