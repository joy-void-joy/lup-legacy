"""The code-intelligence tools against the real language server.

A tool that is merely registered is worth nothing: the failure this guards is
a server that starts and answers every question with silence, which an agent
reads as "this symbol has no definition and no uses". So these drive the real
`pyright-langserver` over this repository and assert it resolved something
specific, rather than asserting the tools exist.
"""

import json
from pathlib import Path

import pytest

from lup.tools.lsp.tools import (
    DocumentInput,
    PositionInput,
    RenameInput,
    create_codeintel_tools,
)
from lup.harness.codescan.common import PythonSource
from lup.harness.codescan.antipatterns import PYTHON_ANTI_PATTERNS
from lup.harness.codescan.resolution import refute
from lup.channels.models import publish_atomic
from lup.workspace.edition import Edition
from lup.workspace.paths import project_root
from lup.devtools.dev.pyright_oracle import PyrightOracle, langserver_path

pytestmark = pytest.mark.skipif(
    langserver_path() is None, reason="pyright-langserver is not installed"
)

TARGET = "packages/lup/src/lup/tools/lsp/client.py"


def tools_by_name(edition: Path | None = None):
    server = langserver_path()
    assert server is not None
    return {
        tool.name: tool
        for tool in create_codeintel_tools(server, project_root(), edition=edition)
    }


async def call(name: str, params, edition: Path | None = None):
    handler = tools_by_name(edition)[name].handler
    response = await handler(params.model_dump())
    assert "content" in response, response
    block = response["content"][0]
    assert block["type"] == "text"
    # A tool error carries prose where an answer carries JSON, so decoding it
    # fails somewhere unrelated to what went wrong. The message is the finding.
    assert "is_error" not in response, block["text"]
    return json.loads(block["text"])


@pytest.fixture
def unpublished(tmp_path: Path) -> Path:
    """An edition location nothing has published to.

    A relative path resolves against wherever editing was last published, and
    that record is keyed to the repository rather than to a checkout — every
    worktree of it reads and writes the same file. So a test measuring a line
    in its own tree and then asking for it by a relative path is asking about
    whichever tree somebody else is editing, and the two are the same tree
    only while nobody else is working.

    Reading no record is what these tests mean: resolve against the checkout
    under test. The one test that is about following a published edition
    publishes one and passes it.
    """
    return tmp_path / "edition.json"


@pytest.mark.asyncio
async def test_a_definition_resolves_through_the_import_that_names_it(
    unpublished: Path,
) -> None:
    """`utf16_column` is called in `position_in`; the definition is above it."""
    lines = (project_root() / TARGET).read_text(encoding="utf-8").splitlines()
    line = next(
        number
        for number, text in enumerate(lines, start=1)
        if "utf16_column(row, column)" in text
    )
    column = lines[line - 1].index("utf16_column")

    result = await call(
        "find_definition",
        PositionInput(path=TARGET, line=line, column=column),
        edition=unpublished,
    )

    assert result["sites"], "the server resolved no definition at all"
    assert any(site["path"].endswith("lsp/client.py") for site in result["sites"])


@pytest.mark.asyncio
async def test_references_find_a_use_the_declaration_line_does_not_show(
    unpublished: Path,
) -> None:
    lines = (project_root() / TARGET).read_text(encoding="utf-8").splitlines()
    line = next(
        number
        for number, text in enumerate(lines, start=1)
        if text.startswith("def utf16_column")
    )
    column = lines[line - 1].index("utf16_column")

    result = await call(
        "find_references",
        PositionInput(path=TARGET, line=line, column=column),
        edition=unpublished,
    )

    assert result["sites"], "the server found no uses of a symbol that has one"


@pytest.mark.asyncio
async def test_listing_symbols_names_the_session_class(unpublished: Path) -> None:
    result = await call("list_symbols", DocumentInput(path=TARGET), edition=unpublished)

    names = {symbol["name"] for symbol in result["symbols"]}
    assert "LspSession" in names
    assert "request" in names, "nested members are flattened out of their class"


@pytest.mark.asyncio
async def test_hover_reports_a_resolved_type(unpublished: Path) -> None:
    lines = (project_root() / TARGET).read_text(encoding="utf-8").splitlines()
    line = next(
        number
        for number, text in enumerate(lines, start=1)
        if text.startswith("def utf16_column")
    )
    column = lines[line - 1].index("utf16_column")

    result = await call(
        "hover",
        PositionInput(path=TARGET, line=line, column=column),
        edition=unpublished,
    )

    assert "utf16_column" in result["text"]


@pytest.mark.asyncio
async def test_a_rename_is_planned_and_never_written(unpublished: Path) -> None:
    target = project_root() / TARGET
    before = target.read_text(encoding="utf-8")
    lines = before.splitlines()
    line = next(
        number
        for number, text in enumerate(lines, start=1)
        if text.startswith("def utf16_column")
    )
    column = lines[line - 1].index("utf16_column")

    result = await call(
        "rename_symbol",
        RenameInput(path=TARGET, line=line, column=column, new_name="utf16_offset"),
        edition=unpublished,
    )

    assert result["files"], "a symbol with uses planned no edits"
    assert target.read_text(encoding="utf-8") == before, "the plan wrote to disk"


@pytest.mark.asyncio
async def test_a_file_in_another_checkout_resolves_against_its_own(
    tmp_path: Path,
) -> None:
    """The server is started once; the work happens somewhere else.

    A worktree is a second checkout of the same repository, which is where
    this project asks that every change be made, and a server rooted on the
    launch directory resolves the same module names against different
    source. The failure is silent — a well-formed answer about the wrong
    tree — so this pins the import, which only resolves if the workspace
    followed the file.
    """
    elsewhere = tmp_path / "other-checkout"
    elsewhere.mkdir()
    (elsewhere / "pyproject.toml").write_text("[project]\nname = 'other'\n")
    (elsewhere / "helpers.py").write_text("def only_here() -> int:\n    return 1\n")
    caller = elsewhere / "caller.py"
    caller.write_text("from helpers import only_here\n\nonly_here()\n")

    result = await call(
        "find_definition",
        PositionInput(path=str(caller), line=3, column=0),
    )

    assert result["sites"], "the import resolved against the wrong checkout"
    assert result["sites"][0]["path"].endswith("helpers.py")


@pytest.mark.asyncio
async def test_a_relative_path_follows_where_editing_is_happening(
    tmp_path: Path,
) -> None:
    """The half deriving the workspace from an absolute path could not reach.

    A relative path is relative to the working directory of whoever asked,
    and this server is a separate long-lived process whose own was fixed at
    launch. Joining it to the launch checkout is a guess, and it is wrong
    silently whenever work has moved, because both trees hold that path.

    The permission hook publishes where editing is happening on every edit,
    which is the fact the guess was standing in for.
    """
    elsewhere = tmp_path / "other-checkout"
    (elsewhere / ".git").mkdir(parents=True)
    (elsewhere / "pyproject.toml").write_text("[project]\nname = 'other'\n")
    (elsewhere / "helpers.py").write_text("def only_here() -> int:\n    return 1\n")
    caller = elsewhere / "caller.py"
    caller.write_text("from helpers import only_here\n\nonly_here()\n")

    published = tmp_path / "edition.json"
    publish_atomic(published, Edition(workspace=elsewhere, file=caller))

    result = await call(
        "find_definition",
        PositionInput(path="caller.py", line=3, column=0),
        edition=published,
    )

    assert result["sites"], "the relative path resolved against the launch checkout"
    assert result["sites"][0]["path"].endswith("helpers.py")


RESOLVED_SUBJECTS = '''from typing import TypedDict


class Row(TypedDict):
    name: str


class Base(dict[str, int]):
    """A mapping the family names only through what it inherits."""


class Middle(Base):
    """Declares the member itself, so resolving it lands here, not on `dict`."""

    def get(self, key: str, default: int | None = None) -> int | None:
        return default


class Client:
    def get(self, url: str) -> str:
        return url


def mapping(payload: dict[str, str]) -> str | None:
    return payload.get("name")


def typed_dict(row: Row) -> str | None:
    return row.get("name")


def descended(holder: Middle) -> int | None:
    return holder.get("count")


def client(session: Client) -> str:
    return session.get("url")


def keywords(**kwargs: str) -> str | None:
    return kwargs.get("name")


def untyped(anything):
    return anything.get("name")
'''
"""One file holding every shape resolution has a different answer for.

Every receiver is named once, so a test names the code it is about rather
than a line number that moves whenever this grows.

Every key is a literal, because the receiver is what each of these is about.
A key computed at runtime is settled by the tree and never becomes a site, so
a fixture spelling one would ask the checker nothing at all.
"""


def resolved(root: Path, text: str) -> dict[str, str]:
    """What the real server makes of each site, keyed by the call it is about.

    The file written to disk holds none of this, so an answer can only come
    from the buffer: the position the server is asked about and the
    declaration it reports back are both inside text that was never saved.
    """
    server = langserver_path()
    assert server is not None
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'other'\n")
    edited = root / "subjects.py"
    edited.write_text("x = 1\n", encoding="utf-8")

    refutations = refute(
        [PythonSource(path=edited, module="subjects", text=text)],
        PyrightOracle(server, root),
        PYTHON_ANTI_PATTERNS,
    )
    lines = text.splitlines()
    found = refutations[edited.as_posix()] if edited.as_posix() in refutations else []
    return {lines[row.line - 1].strip(): row.evidence for row in found}


def test_a_mapping_receiver_keeps_its_finding_against_the_real_server(
    tmp_path: Path,
) -> None:
    """The access the rule exists for survives a checker that can see it."""
    assert 'return payload.get("name")' not in resolved(tmp_path, RESOLVED_SUBJECTS)


def test_a_client_receiver_is_refuted_on_its_own_class(tmp_path: Path) -> None:
    """The same spelling on something that is not a mapping, named as such."""
    evidence = resolved(tmp_path, RESOLVED_SUBJECTS)['return session.get("url")']
    assert "`Client`" in evidence
    assert "outside the mapping family" in evidence


def test_a_typed_dict_is_refuted_on_its_own_class(tmp_path: Path) -> None:
    """Its `get` is synthesized, so only the receiver query reaches the class.

    Asking the member alone answers nothing here, which is the same reply a
    subject the checker cannot type at all gives — so a genuine `TypedDict`
    was dropped with an evidence sentence naming the wrong reason.
    """
    evidence = resolved(tmp_path, RESOLVED_SUBJECTS)['return row.get("name")']
    assert "`Row`" in evidence
    assert "inferred no type" not in evidence


def test_a_subject_descending_from_the_family_keeps_its_finding(
    tmp_path: Path,
) -> None:
    """`Middle(Base(dict))` is a mapping, however many links down it says so."""
    assert 'return holder.get("count")' not in resolved(tmp_path, RESOLVED_SUBJECTS)


def test_keyword_arguments_are_a_mapping_and_keep_their_finding(
    tmp_path: Path,
) -> None:
    """`**kwargs` is a `dict`, and the reference says so.

    Calling the finding refuted for resolving to nothing is the reading to
    refuse: the server resolves it to `dict.get` in typeshed, so the finding
    stands — and a contributor reading that a directive there would be
    reported spurious reads the opposite of what the gate does.
    """
    assert 'return kwargs.get("name")' not in resolved(tmp_path, RESOLVED_SUBJECTS)


def test_an_untyped_subject_is_refuted_for_being_untyped(tmp_path: Path) -> None:
    """Nothing shown is not membership, and the evidence says which happened."""
    evidence = resolved(tmp_path, RESOLVED_SUBJECTS)['return anything.get("name")']
    assert "inferred no type" in evidence


def test_a_buffer_that_disk_does_not_hold_answers_the_same_wherever_it_sits(
    tmp_path: Path,
) -> None:
    """An edit is judged before it is written, so disk is the wrong source.

    Both halves of a resolution have to be told about the buffer: the
    position the server is asked about, and the file the declaration it
    reports back is read out of. Reading that second one from disk answered
    about a file nobody audited — and the line the server reported is a line
    in the buffer, so on disk it landed wherever the two copies had drifted
    to, which flipped a confirmed mapping into "nothing resolved".
    """
    header = "".join(f"# a line disk does not have {n}\n" for n in range(12))

    def verdicts(root: Path, text: str) -> dict[str, str]:
        """Each site's verdict, less where the declaration settling it sits.

        The location is what legitimately differs: a header pushes every
        declaration down, and the evidence cites where each one really is.
        What must not differ is which sites refuted and what they resolved to.
        """
        return {
            call: evidence.partition(" declared at ")[0]
            for call, evidence in resolved(root, text).items()
        }

    assert verdicts(tmp_path / "plain", RESOLVED_SUBJECTS) == verdicts(
        tmp_path / "moved", header + RESOLVED_SUBJECTS
    )


@pytest.mark.asyncio
async def test_a_missing_file_is_an_error_rather_than_an_empty_answer(
    unpublished: Path,
) -> None:
    handler = tools_by_name(unpublished)["find_definition"].handler

    response = await handler(
        PositionInput(path="does/not/exist.py", line=1).model_dump()
    )

    assert "is_error" in response
    assert response["is_error"] is True
