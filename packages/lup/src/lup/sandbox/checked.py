"""A process launcher that refuses host git in a worktree whose pointer moved.

The resolver runs git on the host in every worker's tree after the worker's
container has written it -- `add -A`, `commit`, `diff`, `rev-parse`, `reset`,
`worktree remove` -- and a `commit` there runs the hooks, an `add` the
`core.fsmonitor`, of whichever repository the tree's pointer leads git to.
Each request names its program and its directory outright, so which requests
read a pointer is decided from the request rather than guessed from a
command's text: a `git` request is judged at its `cwd` before it runs.

Judged only, never remembered: a worker's repository is the run's own, found
by path from the workspace the run started in or remembered at the launch
that opened it.
"""

from pathlib import Path

from lup.execution.process import ExitStatus, LaunchRequest, ProcessLauncher
from lup.sandbox.known import known_repositories
from lup.sandbox.pointers import refusal, verdict, vouched_from


class RedirectedPointer(RuntimeError):
    """Host git refused where a pointer leads outside what its repository lists."""


class PointerCheckedLauncher(ProcessLauncher):
    """Launch through ``inner``, judging every `git` request's directory first."""

    def __init__(self, inner: ProcessLauncher, workspace: Path) -> None:
        self.inner = inner
        self.workspace = workspace

    def launch(self, request: LaunchRequest) -> ExitStatus:
        if request.arguments[:1] == ["git"]:
            trusted = [*known_repositories(), *vouched_from(self.workspace)]
            if message := refusal(verdict(request.cwd, trusted).drifts):
                raise RedirectedPointer(message)
        return self.inner.launch(request)
