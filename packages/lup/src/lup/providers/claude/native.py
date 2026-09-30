# lup: ignore[own-model-dispatch]
# The Claude*Operation models mirror Claude Code's PreToolUse tool_input shapes
# — Edit, Write, Bash, WebFetch, WebSearch — so the arms of
# ClaudeEventDecoder.decode narrow a vendor payload rather than dispatch on a
# union of ours. Answering `decode` from each mirror would pull the neutral
# lup.policy vocabulary back across the boundary this adapter exists to hold,
# and would make the vendor's tool roster, not ours, decide when a variant is
# added.
"""Claude-private native event parsing and the sandbox rewrite a verdict places."""

from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, ValidationError

from lup.policy.models import (
    BeforeTool,
    EditBatch,
    EditChange,
    FetchUrl,
    SearchWeb,
    ShellCommand,
    ToolIdentity,
    UnknownTool,
)
from lup.policy.kernel.decision import (
    SandboxPlacement,
    sandbox_escaped,
)
from lup.policy.native import NativeEventDecoder
from lup.types import JsonObject


class ClaudeEditOperation(BaseModel, frozen=True):
    type: Literal["edit"] = "edit"
    path: Path
    before: str
    after: str


class ClaudeWriteOperation(BaseModel, frozen=True):
    type: Literal["write"] = "write"
    path: Path
    content: str


class ClaudeEditBatchOperation(BaseModel, frozen=True):
    type: Literal["edit_batch"] = "edit_batch"
    changes: list[EditChange] = Field(min_length=1)


class ClaudeShellOperation(BaseModel, frozen=True):
    type: Literal["shell"] = "shell"
    command: str
    cwd: Path | None = None
    unsandboxed: bool = False


class ClaudeFetchOperation(BaseModel, frozen=True):
    type: Literal["fetch"] = "fetch"
    url: str


class ClaudeSearchOperation(BaseModel, frozen=True):
    type: Literal["search"] = "search"
    query: str


class ClaudeUnknownOperation(BaseModel, frozen=True):
    type: Literal["unknown"] = "unknown"
    name: str
    input: JsonObject = {}


type ClaudeOperation = (
    ClaudeEditOperation
    | ClaudeWriteOperation
    | ClaudeEditBatchOperation
    | ClaudeShellOperation
    | ClaudeFetchOperation
    | ClaudeSearchOperation
    | ClaudeUnknownOperation
)


class ClaudeBeforeToolEvent(BaseModel, frozen=True):
    operation: ClaudeOperation


class ClaudeHookPayload(BaseModel, frozen=True):
    """Validated external hook input before operation-specific parsing."""

    tool_name: str
    tool_input: JsonObject = {}
    cwd: Path | None = None
    """Where the calling session is, which a shell command's operands resolve
    against. The hook itself does not always run there."""


def parse_claude_before_tool(payload: ClaudeHookPayload) -> ClaudeBeforeToolEvent:
    """Decode Claude names and payload fields at the adapter boundary."""
    match payload.tool_name, payload.tool_input:
        case "Edit", {
            "file_path": str(path),
            "old_string": str(before),
            "new_string": str(after),
        }:
            operation: ClaudeOperation = ClaudeEditOperation(
                path=Path(path), before=before, after=after
            )
        case "Write", {"file_path": str(path), "content": str(content)}:
            operation = ClaudeWriteOperation(path=Path(path), content=content)
        case "Bash", {"command": str(command), "dangerouslyDisableSandbox": True}:
            operation = ClaudeShellOperation(
                command=command, cwd=payload.cwd, unsandboxed=True
            )
        case "Bash", {"command": str(command)}:
            operation = ClaudeShellOperation(command=command, cwd=payload.cwd)
        case "WebFetch", {"url": str(url)}:
            operation = ClaudeFetchOperation(url=url)
        case "WebSearch", {"query": str(query)}:
            operation = ClaudeSearchOperation(query=query)
        case _:
            operation = ClaudeUnknownOperation(
                name=payload.tool_name, input=payload.tool_input
            )
    return ClaudeBeforeToolEvent(operation=operation)


class ClaudeEventDecoder(NativeEventDecoder[ClaudeBeforeToolEvent]):
    """Decode validated Claude operations into the shared vocabulary."""

    def decode(self, event: ClaudeBeforeToolEvent) -> BeforeTool:
        operation = event.operation
        match operation:
            case ClaudeEditOperation(path=path, before=before, after=after):
                tool = EditBatch(
                    changes=[EditChange(path=path, before=before, after=after)]
                )
                name = "Edit"
            case ClaudeWriteOperation(path=path, content=content):
                # Whether this replaces a file or makes one is the difference
                # between an overwrite and a creation, and only the tree can
                # answer it. Naming it here is what lets `as_documents` fetch
                # the preimage an overwrite carries no copy of — without which
                # the marker gate compares a file's notes against nothing,
                # finds none of them missing, and admits a write that erased
                # every one. The generated dispatchers read that document
                # themselves; nothing else on this path does.
                tool = EditBatch(
                    changes=[
                        EditChange(
                            path=path,
                            after=content,
                            operation="overwrite" if path.exists() else "create",
                        )
                    ]
                )
                name = "Write"
            case ClaudeEditBatchOperation(changes=changes):
                tool = EditBatch(changes=changes)
                name = "Edit"
            case ClaudeShellOperation(
                command=command, cwd=cwd, unsandboxed=unsandboxed
            ):
                tool = ShellCommand(command=command, cwd=cwd, unsandboxed=unsandboxed)
                name = "Bash"
            case ClaudeFetchOperation(url=url):
                name = "WebFetch"
                try:
                    tool = FetchUrl(url=AnyHttpUrl(url))
                except ValidationError:
                    identity = ToolIdentity(original_name=name)
                    return BeforeTool(
                        tool=UnknownTool(identity=identity, input={"url": url}),
                        identity=identity,
                    )
            case ClaudeSearchOperation(query=query):
                tool = SearchWeb(query=query)
                name = "WebSearch"
            case ClaudeUnknownOperation(name=name, input=input_data):
                identity = ToolIdentity(original_name=name)
                return BeforeTool(
                    tool=UnknownTool(identity=identity, input=input_data),
                    identity=identity,
                )
        identity = ToolIdentity(original_name=name)
        return BeforeTool(tool=tool, identity=identity)


def claude_sandbox_input(
    tool_input: JsonObject | None, sandbox: SandboxPlacement
) -> JsonObject | None:
    """The call's own arguments, rewritten to run where the verdict placed it.

    Claude Code's one spelling of the sandbox axis, and the only place it is
    written. The rewrite replaces the arguments outright rather than merging
    into them, which is why the whole input is carried through; an unplaced
    verdict rewrites nothing at all. The rewrite channel is what makes an
    unprompted placement reachable: it arrives whole and the sandbox is chosen
    from it, read out of the shipped binary at version 2.1.228, as the
    compiled dispatcher in ``assets/policy_dispatcher.py`` records in full.

    Which placements leave is :func:`~lup.policy.kernel.decision.sandbox_escaped`
    and not this function, because the compiled dispatcher renders the same
    rewrite and cannot import this one. What stays here is the field name,
    which is Claude Code's own and reaches no other runtime.
    """
    if tool_input is None or sandbox == "ambient":
        return None
    return {**tool_input, "dangerouslyDisableSandbox": sandbox_escaped(sandbox)}
