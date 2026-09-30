# lup: ignore[own-model-dispatch]
# The Codex*Operation models mirror Codex app-server and hook payloads — the
# apply_patch file-change list, Bash, web_fetch, web_search — so the arms of
# CodexEventDecoder.decode narrow a vendor payload rather than dispatch on a
# union of ours. Answering `decode` from each mirror would pull the neutral
# lup.policy vocabulary back across the boundary this adapter exists to hold,
# and would make the vendor's tool roster, not ours, decide when a variant is
# added.
"""Codex-private native event parsing."""

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
from lup.policy.native import NativeEventDecoder
from lup.types import JsonObject


class CodexFileChange(BaseModel, frozen=True):
    path: Path
    before: str | None = None
    after: str | None = None


class CodexFileChangeOperation(BaseModel, frozen=True):
    type: Literal["file_change"] = "file_change"
    changes: list[CodexFileChange] = Field(min_length=1)


class CodexShellOperation(BaseModel, frozen=True):
    type: Literal["shell"] = "shell"
    command: str
    cwd: Path | None = None


class CodexFetchOperation(BaseModel, frozen=True):
    type: Literal["fetch"] = "fetch"
    url: str


class CodexSearchOperation(BaseModel, frozen=True):
    type: Literal["search"] = "search"
    query: str


class CodexUnknownOperation(BaseModel, frozen=True):
    type: Literal["unknown"] = "unknown"
    name: str
    input: JsonObject = {}


type CodexOperation = (
    CodexFileChangeOperation
    | CodexShellOperation
    | CodexFetchOperation
    | CodexSearchOperation
    | CodexUnknownOperation
)


class CodexBeforeToolEvent(BaseModel, frozen=True):
    operation: CodexOperation


class CodexHookPayload(BaseModel, frozen=True):
    """Validated external hook input before operation-specific parsing."""

    tool_name: str
    tool_input: JsonObject = {}


def parse_codex_before_tool(payload: CodexHookPayload) -> CodexBeforeToolEvent:
    """Decode stable Codex hook fields; opaque patches remain conservative."""
    match payload.tool_name, payload.tool_input:
        case "Bash", {"command": str(command)}:
            operation: CodexOperation = CodexShellOperation(command=command)
        case "web_fetch", {"url": str(url)}:
            operation = CodexFetchOperation(url=url)
        case "web_search", {"query": str(query)}:
            operation = CodexSearchOperation(query=query)
        case _:
            operation = CodexUnknownOperation(
                name=payload.tool_name, input=payload.tool_input
            )
    return CodexBeforeToolEvent(operation=operation)


class CodexEventDecoder(NativeEventDecoder[CodexBeforeToolEvent]):
    """Decode Codex app-server/hook operations into shared semantic events."""

    def decode(self, event: CodexBeforeToolEvent) -> BeforeTool:
        operation = event.operation
        match operation:
            case CodexFileChangeOperation(changes=changes):
                tool = EditBatch(
                    changes=[
                        EditChange(
                            path=change.path,
                            before=change.before,
                            after=change.after,
                        )
                        for change in changes
                    ]
                )
                name = "apply_patch"
            case CodexShellOperation(command=command, cwd=cwd):
                tool = ShellCommand(command=command, cwd=cwd)
                name = "Bash"
            case CodexFetchOperation(url=url):
                name = "web_fetch"
                try:
                    tool = FetchUrl(url=AnyHttpUrl(url))
                except ValidationError:
                    identity = ToolIdentity(original_name=name)
                    return BeforeTool(
                        tool=UnknownTool(identity=identity, input={"url": url}),
                        identity=identity,
                    )
            case CodexSearchOperation(query=query):
                tool = SearchWeb(query=query)
                name = "web_search"
            case CodexUnknownOperation(name=name, input=input_data):
                identity = ToolIdentity(original_name=name)
                return BeforeTool(
                    tool=UnknownTool(identity=identity, input=input_data),
                    identity=identity,
                )
        identity = ToolIdentity(original_name=name)
        return BeforeTool(tool=tool, identity=identity)
