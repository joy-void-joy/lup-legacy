"""Which language server reads each language, and how one is found and started.

One declared place per language: the server that answers for a file is the
first declaration claiming its suffix, and everything about that server — the
executable, the arguments it takes, the `languageId` each suffix opens under,
the settings a checkout hands it — is said once, on its declaration.

**A server only reads.** Whoever starts one runs it with their own
authority — the operator's dashboard runs outside every sandbox — so nothing a
server runs may come from the code it reads. A declaration finds its
executable in the starting process's own environment, never in the checkout
asked about: a checkout's `.venv/bin/<server>` is a script its own
interpreter runs, and that interpreter executes whatever `.pth` file its
site-packages hold, which whoever writes to the checkout chose. The same goes
for an interpreter a Python server would run to find packages: it is handed
the starting process's own, and the checkout's packages arrive as paths read
off its environment's directory, the way `site` reads them, without running
anything there.
"""

import os
import shutil
import sys
from abc import ABC, abstractmethod
from pathlib import Path

import sh
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field

from lup.policy.assets.host import project_environment
from lup.types import JsonObject, StringMap


class Launch(BaseModel, frozen=True):
    """How one server starts: its executable, and the arguments after it."""

    command: Path
    arguments: list[str]


class Unavailable(Exception):
    """Why a server cannot start here, said so a reader knows what would start it."""


class LanguageServer(BaseModel, ABC, frozen=True):
    """One language's server: the files it reads, how it starts, and what a checkout hands it."""

    name: str
    """What the page calls it when it answers, or says why it does not."""

    languages: StringMap
    """The `languageId` each suffix it reads opens under, `.py` as `python`."""

    def language(self, path: Path) -> str:
        """The `languageId` *path* opens under, or nothing where this server reads no such file."""
        suffix = path.suffix.lower()
        return self.languages[suffix] if suffix in self.languages else ""

    @abstractmethod
    def launch(self) -> Launch:
        """How to start it from the starting process's own environment; :class:`Unavailable` where it cannot be."""

    def settings(self, checkout: Path) -> JsonObject:
        """What one checkout's workspace folder is configured with; nothing by default."""
        del checkout
        return {}


def site_paths(environment: Path) -> list[str]:
    """Where an environment's packages are, read off its directory without running it.

    Every ``site-packages`` under its library directories, and the
    directories each ``.pth`` file there names — the lines ``site`` would add
    to ``sys.path``. A line beginning with ``import`` is code ``site`` would
    run, so it is skipped rather than read: that is the line no reader may
    run.
    """
    found = sorted(
        {
            directory
            for pattern in (
                "lib/python3*/site-packages",
                "lib64/python3*/site-packages",
                "Lib/site-packages",
            )
            for directory in environment.glob(pattern)
            if directory.is_dir()
        }
    )
    named = [
        (site / line.strip()).resolve()
        for site in found
        for pth in sorted(site.glob("*.pth"))
        for line in pth.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.startswith(("#", "import ", "import\t"))
    ]
    return list(dict.fromkeys(str(path) for path in [*found, *named] if path.is_dir()))


def own_interpreter() -> Path:
    """The interpreter the starting process runs: the one environment it already trusts."""
    return Path(sys.executable)


class BasedPyright(LanguageServer, frozen=True):
    """Python, through basedpyright: pyright's analysis, with semantic tokens.

    Started from the console script beside the starting process's own
    interpreter. Each checkout is handed that interpreter as its
    ``pythonPath`` — without one, basedpyright runs ``.venv/bin/python``
    in the checkout to list its packages — and the checkout's own packages as
    ``extraPaths``, read off its environment's directory by
    :func:`site_paths`. Extra paths are searched before the interpreter's,
    so a checkout's own copy of a package answers for it, an editable install
    included: that is what makes a worktree's import resolve in that
    worktree.
    """

    name: str = "basedpyright"
    languages: StringMap = {".py": "python", ".pyi": "python"}
    executable: str = "basedpyright-langserver"
    interpreter: Path = Field(default_factory=own_interpreter)

    def launch(self) -> Launch:
        beside = self.interpreter.parent / self.executable
        found = beside if beside.is_file() else shutil.which(self.executable)
        if found is None:
            raise Unavailable(
                f"{self.name} is not installed beside the dashboard's interpreter "
                f"({self.interpreter}); lup's `web` extra installs it"
            )
        return Launch(command=Path(found), arguments=["--stdio"])

    def settings(self, checkout: Path) -> JsonObject:
        return {
            "python": {"pythonPath": str(self.interpreter)},
            "basedpyright": {
                "analysis": {
                    "extraPaths": list(site_paths(project_environment(checkout))),
                    "autoSearchPaths": True,
                    "diagnosticMode": "openFilesOnly",
                }
            },
        }


def own_toolchains() -> list[Path]:
    """The `node_modules` of the web workspace beside lup's own source, where lup runs from one."""
    workspace = Path(__file__).resolve().parents[4] / "web" / "node_modules"
    return [workspace] if workspace.is_dir() else []


def major_version(said: str) -> int:
    """The major version a ``--version`` line names, or 0 where it names none."""
    try:
        return Version(said.strip().removeprefix("Version").strip()).major
    except InvalidVersion:
        return 0


class TypeScriptNative(LanguageServer, frozen=True):
    """TypeScript and JavaScript, through TypeScript 7's own compiler, ``tsc --lsp``.

    TypeScript 7 is the compiler rewritten natively, and it serves the
    language server protocol itself — hover, definitions, references and
    semantic tokens — with no ``tsserver`` and no Node. Its executable is
    looked for in the starting process's own toolchains — by default the web
    workspace beside lup's source — and then on ``PATH``, where a ``tsc``
    older than 7 has no language server and is passed over.
    """

    name: str = "TypeScript"
    languages: StringMap = {
        ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
        ".tsx": "typescriptreact",
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".jsx": "javascriptreact",
    }  # fmt: skip
    toolchains: list[Path] = Field(default_factory=own_toolchains)
    minimum: int = 7
    """The first major version whose ``tsc`` serves the protocol."""

    def launch(self) -> Launch:
        native = [
            executable
            for toolchain in self.toolchains
            for executable in sorted(
                toolchain.glob("@typescript/typescript-*/lib/tsc*")
            )
            if executable.is_file() and os.access(executable, os.X_OK)
        ]
        if native:
            return Launch(command=native[0], arguments=["--lsp", "--stdio"])
        found = shutil.which("tsc")
        if found is None:
            raise Unavailable(
                f"no TypeScript {self.minimum} compiler (`tsc --lsp`) is in the "
                "dashboard's own toolchain or on its PATH, so TypeScript is not served"
            )
        try:
            said = str(sh.Command(found)("--version", _timeout=10))
        except (sh.ErrorReturnCode, sh.TimeoutException, OSError) as failed:
            raise Unavailable(f"`{found} --version` failed: {failed}") from failed
        if major_version(said) < self.minimum:
            raise Unavailable(
                f"the `tsc` on the dashboard's PATH says {said.strip()!r}: its compiler "
                f"serves no language server, TypeScript {self.minimum}'s does"
            )
        return Launch(command=Path(found), arguments=["--lsp", "--stdio"])


def default_servers() -> list[LanguageServer]:
    """The servers a page asks by default: basedpyright for Python, TypeScript 7 for TypeScript and JavaScript."""
    return [BasedPyright(), TypeScriptNative()]
