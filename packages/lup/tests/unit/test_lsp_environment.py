"""Fresh language servers resolve the requested checkout's installed packages."""

import json
import os
import sys
import sysconfig
from pathlib import Path
from venv import EnvBuilder

import pytest
from pydantic import BaseModel

from lup.devtools.dev.pyright_oracle import (
    PyrightOracle,
    langserver_path,
    pyright_settings,
)
from lup.devtools.dev import check
from lup.harness.codescan.oracle import ClassDeclaration, SourcePosition, SymbolQuery
from lup.orchestration.reflection import ReviewGate
from lup.tools.lsp.tools import PositionInput, SiteList
from lup.tools.toolsets import SessionNeeds, codeintel_group


class WorkspaceFixture(BaseModel):
    root: Path
    source: Path
    dependency: Path


def installed_package(environment: Path, text: str) -> Path:
    EnvBuilder(with_pip=False).create(environment)
    site = Path(
        sysconfig.get_path(
            "purelib", vars={"base": str(environment), "platbase": str(environment)}
        )
    )
    package = site / "demo_dependency"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text(text, encoding="utf-8")
    return source


def workspace(root: Path, environment: Path) -> WorkspaceFixture:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.pyright]\ninclude = ["sample.py"]\n', encoding="utf-8"
    )
    source = root / "sample.py"
    source.write_text(
        "from demo_dependency import Record\n\n"
        "def read(value: Record) -> str:\n"
        '    return value.get("name")\n',
        encoding="utf-8",
    )
    dependency = installed_package(
        environment,
        "class Record:\n    def get(self, name: str) -> str:\n        return name\n",
    )
    return WorkspaceFixture(root=root, source=source, dependency=dependency)


@pytest.mark.skipif(langserver_path() is None, reason="pyright-langserver is absent")
@pytest.mark.parametrize("environment_kind", ["relative", "absolute", "default"])
@pytest.mark.asyncio
async def test_toolset_resolves_the_requested_workspace_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment_kind: str
) -> None:
    root = tmp_path / "requested"
    match environment_kind:
        case "relative":
            environment = root / ".venv-contained"
            monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", ".venv-contained")
        case "absolute":
            environment = tmp_path / "selected-environment"
            monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(environment))
        case _:
            environment = root / ".venv"
            monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    fixture = workspace(root, environment)
    if environment_kind != "default":
        installed_package(root / ".venv", "class Record(dict[str, str]):\n    pass\n")
    launch = tmp_path / "launch"
    launch.mkdir()
    needs = SessionNeeds(root=launch, session_dir=launch, gate=ReviewGate())
    tool = next(
        tool
        for tool in codeintel_group().tools(needs)
        if tool.name == "find_definition"
    )
    params = PositionInput(path=str(fixture.source), line=1, column=28)

    result = await tool.handler(params.model_dump())

    assert "is_error" not in result, result
    assert "content" in result, result
    block = result["content"][0]
    assert block["type"] == "text"
    sites = SiteList.model_validate_json(block["text"]).sites
    assert sites and {Path(site.path) for site in sites} == {fixture.dependency}


@pytest.mark.skipif(langserver_path() is None, reason="pyright-langserver is absent")
def test_type_oracle_uses_the_same_redirected_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", ".venv-contained")
    fixture = workspace(root, root / ".venv-contained")
    installed_package(root / ".venv", "class Record(dict[str, str]):\n    pass\n")
    server = langserver_path()
    assert server is not None
    member = SourcePosition(path=fixture.source, line=4, column=17)
    receiver = SourcePosition(path=fixture.source, line=4, column=11)

    declarations = PyrightOracle(server, root).declarations(
        [SymbolQuery(member=member, receiver=receiver)]
    )

    assert len(declarations) == 1
    declaration = declarations[0]
    assert isinstance(declaration, ClassDeclaration)
    assert declaration.name == "Record"
    assert declaration.path == fixture.dependency
    assert "dict" not in declaration.bases


def test_missing_project_interpreter_leaves_server_defaults_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "missing-environment")

    assert pyright_settings(tmp_path) == {}


def test_blank_environment_uses_the_same_default_as_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "")
    interpreter = (
        tmp_path
        / ".venv"
        / Path(sys.executable).parent.name
        / ("python.exe" if os.name == "nt" else "python")
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()

    assert pyright_settings(tmp_path) == {"python": {"pythonPath": str(interpreter)}}


def test_versioned_caller_selects_the_projects_generic_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", ".venv-contained")
    environment = tmp_path / ".venv-contained"
    EnvBuilder(with_pip=False).create(environment)
    interpreter = (
        environment
        / Path(sys.executable).parent.name
        / ("python.exe" if os.name == "nt" else "python")
    )
    monkeypatch.setattr(
        sys, "executable", str(Path(sys.executable).with_name("python3.99"))
    )
    assert not interpreter.with_name("python3.99").exists()

    assert pyright_settings(tmp_path) == {"python": {"pythonPath": str(interpreter)}}


@pytest.mark.skipif(langserver_path() is None, reason="pyright-langserver is absent")
@pytest.mark.parametrize(
    "configuration", ["plain", "toml", "json", "inherited", "jsonc"]
)
@pytest.mark.asyncio
async def test_batch_and_language_server_share_pyright_configuration_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: str
) -> None:
    root = tmp_path / "project"
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", ".venv-contained")
    fixture = workspace(root, root / ".venv-contained")
    expected = fixture.dependency
    result_type = "str"
    if configuration != "plain":
        expected = installed_package(
            root / "custom-environments" / "analysis",
            "class Record(dict[str, str]):\n    pass\n",
        )
        result_type = "str | None"
    match configuration:
        case "toml":
            (root / "pyproject.toml").write_text(
                '[tool.pyright]\nvenvPath = "custom-environments"\nvenv = "analysis"\n',
                encoding="utf-8",
            )
        case "json":
            (root / "pyrightconfig.json").write_text(
                json.dumps({"venvPath": "custom-environments", "venv": "analysis"}),
                encoding="utf-8",
            )
        case "inherited":
            base = root / "config" / "base.json"
            base.parent.mkdir()
            base.write_text(
                json.dumps({"venvPath": "../custom-environments", "venv": "analysis"}),
                encoding="utf-8",
            )
            (root / "pyrightconfig.json").write_text(
                json.dumps({"extends": "config/base.json"}), encoding="utf-8"
            )
        case "jsonc":
            (root / "pyrightconfig.json").write_text(
                "// An intentional custom environment.\n"
                '{"venvPath": "custom-environments", "venv": "analysis"}\n',
                encoding="utf-8",
            )
    fixture.source.write_text(
        "from typing import assert_type\n"
        "from demo_dependency import Record\n"
        f'assert_type(Record().get("name"), {result_type})\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check, "project_root", lambda: root)

    report = check.pyright_check([], scope=[str(fixture.source)])
    needs = SessionNeeds(root=root, session_dir=root, gate=ReviewGate())
    tool = next(
        tool
        for tool in codeintel_group().tools(needs)
        if tool.name == "find_definition"
    )
    result = await tool.handler(
        PositionInput(path=str(fixture.source), line=2, column=28).model_dump()
    )

    assert report.passed, report.lines
    assert "is_error" not in result, result
    assert "content" in result, result
    block = result["content"][0]
    assert block["type"] == "text"
    sites = SiteList.model_validate_json(block["text"]).sites
    assert sites and {Path(site.path) for site in sites} == {expected}
