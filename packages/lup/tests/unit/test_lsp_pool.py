"""Language servers kept warm for a page: what they read, what they never run, and how long they are waited on."""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from lup.tools.lsp.pool import Document, ServerPool
from lup.tools.lsp.servers import (
    BasedPyright,
    LanguageServer,
    Launch,
    TypeScriptNative,
    Unavailable,
    site_paths,
)
from lup.types import JsonObject, JsonValue, StringMap

# A language server that answers `initialize` and `shutdown` and nothing else,
# writing every message it receives, and every answer to a request of its
# own, to the log it is handed. After `initialized` it asks for the `python`
# section of its configuration, as a server pulling its settings does.
SILENT_SERVER = r"""
import json, os, sys

log = open(sys.argv[1], "a", encoding="utf-8")
log.write(json.dumps({"started": os.getpid()}) + "\n")
log.flush()
reader = sys.stdin.buffer


def send(body):
    payload = json.dumps(body).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(payload) + payload)
    sys.stdout.buffer.flush()


while True:
    length = 0
    while True:
        line = reader.readline()
        if not line:
            sys.exit(0)
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode().partition(":")
        if name.lower() == "content-length":
            length = int(value)
    message = json.loads(reader.read(length))
    log.write(json.dumps(message) + "\n")
    log.flush()
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {"hoverProvider": True}}})
    elif method == "initialized":
        folder = message.get("params", {})
        send({"jsonrpc": "2.0", "id": "asked", "method": "workspace/configuration",
              "params": {"items": [{"scopeUri": os.environ["SILENT_SCOPE"], "section": "python"}]}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        sys.exit(0)
"""


class Silent(LanguageServer, frozen=True):
    """A server that never answers a question, run from a script the test writes."""

    name: str = "silent"
    languages: StringMap = {".py": "python"}
    script: Path
    log: Path

    def launch(self) -> Launch:
        return Launch(
            command=Path(sys.executable), arguments=[str(self.script), str(self.log)]
        )

    def settings(self, checkout: Path) -> JsonObject:
        return {"python": {"pythonPath": f"{checkout.name}-interpreter"}}


class Folder(BaseModel):
    uri: str


class FolderEvent(BaseModel):
    added: list[Folder] = []


class Change(BaseModel):
    text: str


class Params(BaseModel):
    """The parts of a message's params these tests read."""

    root: str = Field(default="", alias="rootUri")
    process: int | None = Field(default=None, alias="processId")
    id: int | str | None = None
    event: FolderEvent | None = None
    document: JsonObject = Field(default={}, alias="textDocument")
    changes: list[Change] = Field(default=[], alias="contentChanges")


class Message(BaseModel):
    """One line of the server's log: its start, or a message it received."""

    started: int | None = None
    method: str = ""
    id: int | str | None = None
    params: Params = Params()
    result: JsonValue = None


def logged(log: Path) -> list[Message]:
    return [
        Message.model_validate_json(line)
        for line in log.read_text(encoding="utf-8").splitlines()
    ]


def silent(tmp_path: Path) -> Silent:
    script = tmp_path / "silent_server.py"
    script.write_text(SILENT_SERVER, encoding="utf-8")
    return Silent(script=script, log=tmp_path / "server.log")


def document(
    path: Path, checkout: Path, repository: Path, text: str = "x = 1\n"
) -> Document:
    return Document(path=path, checkout=checkout, repository=repository, text=text)


def test_an_environment_s_packages_are_read_off_its_directory_and_no_line_of_code_is_run(
    tmp_path: Path,
) -> None:
    environment = tmp_path / ".venv"
    site = environment / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    editable = tmp_path / "source"
    editable.mkdir()
    marker = tmp_path / "ran"
    (site / "editable.pth").write_text(
        f"# a comment\n{editable}\nimport os; open({str(marker)!r}, 'w')\n../missing\n",
        encoding="utf-8",
    )

    assert site_paths(environment) == [str(site), str(editable)]
    assert not marker.exists()
    assert site_paths(tmp_path / "nowhere") == []


def test_basedpyright_is_handed_the_dashboard_s_own_interpreter_and_the_checkout_s_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", ".venv")
    site = tmp_path / ".venv" / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    interpreter = tmp_path / "own" / "bin" / "python"

    settings = BasedPyright(interpreter=interpreter).settings(tmp_path)

    assert settings == {
        "python": {"pythonPath": str(interpreter)},
        "basedpyright": {
            "analysis": {
                "extraPaths": [str(site)],
                "autoSearchPaths": True,
                "diagnosticMode": "openFilesOnly",
            }
        },
    }


def test_a_server_missing_from_the_dashboard_s_environment_says_what_would_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(Unavailable, match="`web` extra installs it"):
        BasedPyright(interpreter=tmp_path / "bin" / "python").launch()
    with pytest.raises(Unavailable, match="TypeScript 7 compiler"):
        TypeScriptNative(toolchains=[]).launch()


def test_typescript_is_served_by_the_native_compiler_in_the_dashboard_s_own_toolchain(
    tmp_path: Path,
) -> None:
    native = (
        tmp_path
        / "node_modules"
        / "@typescript"
        / "typescript-linux-x64"
        / "lib"
        / "tsc"
    )
    native.parent.mkdir(parents=True)
    native.write_text("#!/bin/sh\n", encoding="utf-8")
    native.chmod(0o755)

    launch = TypeScriptNative(toolchains=[tmp_path / "node_modules"]).launch()

    assert launch == Launch(command=native, arguments=["--lsp", "--stdio"])


def test_an_older_tsc_on_path_is_passed_over_for_saying_it_serves_no_language_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tsc = tmp_path / "tsc"
    tsc.write_text("#!/bin/sh\necho 'Version 5.9.3'\n", encoding="utf-8")
    tsc.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(Unavailable, match="Version 5.9.3"):
        TypeScriptNative(toolchains=[]).launch()


async def test_a_question_a_server_does_not_answer_in_time_is_withdrawn_and_said_unserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SILENT_SCOPE", tmp_path.as_uri())
    server = silent(tmp_path)
    pool = ServerPool([server])
    try:
        await pool.hover(
            document(tmp_path / "a.py", tmp_path, tmp_path), 1, 0, within=2
        )
        started = time.monotonic()
        hover = await pool.hover(
            document(tmp_path / "a.py", tmp_path, tmp_path), 1, 0, within=0.5
        )
        took = time.monotonic() - started
        for _ in range(50):
            if any(each.method == "$/cancelRequest" for each in logged(server.log)):
                break
            await asyncio.sleep(0.1)
    finally:
        await pool.stop()

    assert not hover.served
    assert hover.server == "silent"
    assert hover.why == "silent did not answer within 0.5 seconds"
    assert took < 3
    messages = logged(server.log)
    hovers = [each.id for each in messages if each.method == "textDocument/hover"]
    cancelled = [
        each.params.id for each in messages if each.method == "$/cancelRequest"
    ]
    assert len(hovers) == 2 and set(cancelled) == set(hovers)


async def test_one_server_per_repository_reads_each_checkout_as_its_own_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    first, second = repository / "tree" / "one", repository / "tree" / "two"
    monkeypatch.setenv("SILENT_SCOPE", first.as_uri())
    server = silent(tmp_path)
    pool = ServerPool([server])
    try:
        await pool.hover(document(first / "a.py", first, repository), 1, 0, within=0.5)
        await pool.hover(
            document(second / "b.py", second, repository), 1, 0, within=0.5
        )
    finally:
        await pool.stop()

    messages = logged(server.log)
    assert len([each for each in messages if each.started is not None]) == 1
    initialize = next(each for each in messages if each.method == "initialize")
    assert initialize.params.root == first.as_uri()
    assert initialize.params.process == os.getpid()
    added = [
        folder.uri
        for each in messages
        if each.method == "workspace/didChangeWorkspaceFolders"
        and each.params.event is not None
        for folder in each.params.event.added
    ]
    assert added == [second.as_uri()]
    answered = next(each for each in messages if each.id == "asked" and not each.method)
    assert answered.result == [{"pythonPath": "one-interpreter"}]
    opened = [
        each.params.document["uri"]
        for each in messages
        if each.method == "textDocument/didOpen"
    ]
    assert opened == [(first / "a.py").as_uri(), (second / "b.py").as_uri()]


async def test_a_document_asked_about_again_with_other_text_is_changed_rather_than_reopened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SILENT_SCOPE", tmp_path.as_uri())
    server = silent(tmp_path)
    pool = ServerPool([server])
    path = tmp_path / "a.py"
    try:
        await pool.hover(
            document(path, tmp_path, tmp_path, "x = 1\n"), 1, 0, within=0.5
        )
        await pool.hover(
            document(path, tmp_path, tmp_path, "x = 1\n"), 1, 0, within=0.5
        )
        await pool.hover(
            document(path, tmp_path, tmp_path, "y = 2\n"), 1, 0, within=0.5
        )
    finally:
        await pool.stop()

    messages = logged(server.log)
    assert (
        len([each for each in messages if each.method == "textDocument/didOpen"]) == 1
    )
    changed = [
        each.params for each in messages if each.method == "textDocument/didChange"
    ]
    assert [
        (each.document, [change.text for change in each.changes]) for each in changed
    ] == [({"uri": path.as_uri(), "version": 2}, ["y = 2\n"])]


async def test_a_server_unasked_for_its_idle_time_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SILENT_SCOPE", tmp_path.as_uri())
    server = silent(tmp_path)
    pool = ServerPool([server], idle=0.3)
    try:
        await pool.hover(
            document(tmp_path / "a.py", tmp_path, tmp_path), 1, 0, within=0.5
        )
        warm = next(iter(pool.warm.values()))
        for _ in range(50):
            if not warm.running():
                break
            await asyncio.sleep(0.1)
        assert not warm.running()
    finally:
        await pool.stop()

    assert any(each.method == "shutdown" for each in logged(server.log))


async def test_a_file_no_server_reads_is_said_unserved(tmp_path: Path) -> None:
    hover = await ServerPool([]).hover(
        document(tmp_path / "notes.txt", tmp_path, tmp_path), 1, 0
    )

    assert hover.served is False
    assert hover.why == "no language server reads .txt files"


def environment_with_a_package(checkout: Path, marker: Path) -> Path:
    """A checkout's `.venv` whose interpreter and `.pth` file each leave *marker* behind if run."""
    bin_directory = checkout / ".venv" / "bin"
    bin_directory.mkdir(parents=True)
    interpreter = bin_directory / "python"
    interpreter.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    interpreter.chmod(0o755)
    site = checkout / ".venv" / "lib" / "python3.14" / "site-packages"
    package = site / "demo_dependency"
    package.mkdir(parents=True)
    (site / "startup.pth").write_text(
        f"import os; open({str(marker)!r}, 'w')\n", encoding="utf-8"
    )
    dependency = package / "__init__.py"
    dependency.write_text(
        'class Record:\n    """A record somebody else keeps."""\n\n    def get(self, name: str) -> str:\n        return name\n',
        encoding="utf-8",
    )
    return dependency


@pytest.mark.skipif(
    not (Path(sys.executable).parent / "basedpyright-langserver").is_file(),
    reason="basedpyright is not installed beside this interpreter",
)
async def test_basedpyright_answers_from_a_checkout_without_running_anything_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", ".venv")
    checkout = tmp_path / "checkout"
    marker = tmp_path / "ran"
    dependency = environment_with_a_package(checkout, marker)
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\n', encoding="utf-8"
    )
    source = checkout / "sample.py"
    text = "from demo_dependency import Record\n\n\ndef read(value: Record) -> str:\n    return value.get('name')\n"
    source.write_text(text, encoding="utf-8")
    pool = ServerPool()
    try:
        on_disk = document(source, checkout, checkout, text)
        hover = await pool.hover(on_disk, 4, 17, within=60)
        definition = await pool.definition(on_disk, 4, 17, within=60)
        references = await pool.references(on_disk, 1, 28, within=60)
        tokens = await pool.tokens(on_disk, within=60)
        proposed = text.replace("def read", "def reread")
        after = await pool.hover(
            document(source, checkout, checkout, proposed), 4, 5, within=60
        )
    finally:
        await pool.stop()

    assert hover.served and "class Record" in hover.markdown
    assert "A record somebody else keeps." in hover.markdown
    assert [(Path(each.path), each.line) for each in definition.locations] == [
        (dependency, 1)
    ]
    assert [each.line for each in references.locations] == [4]
    assert references.locations[0].preview == "def read(value: Record) -> str:"
    assert tokens.served and "class" in tokens.types
    record = next(
        index
        for index in range(0, len(tokens.data), 5)
        if sum(tokens.data[at] for at in range(0, index + 1, 5)) == 3
        and tokens.data[index + 2] == len("Record")
    )
    assert tokens.types[tokens.data[record + 3]] == "class"
    assert after.served and "def reread" in after.markdown
    assert not marker.exists()
