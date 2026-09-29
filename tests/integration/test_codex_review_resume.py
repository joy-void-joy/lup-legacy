"""A live Codex session resumes an exact patch after an operator queue answer."""

import os
import shutil
from pathlib import Path

import pytest
import sh

from lup.devtools.review.app import answer
from lup.policy.relay import QuestionRelay
from lup.providers.codex.harness_runtime import CodexPluginInstaller, PluginCacheConfig
from lup.providers.codex.subagents import CodexModelTiers
from lup_template.harness.catalog import portable_harness

pytestmark = pytest.mark.integration


def test_codex_document_replacement_resumes_after_queue_approval(
    tmp_path: Path,
) -> None:
    root = Path.cwd()
    home = tmp_path / "codex-home"
    home.mkdir()
    source_home = (
        Path(os.environ["CODEX_HOME"])
        if "CODEX_HOME" in os.environ
        else Path.home() / ".codex"
    )
    auth = source_home / "auth.json"
    if not auth.exists():
        pytest.skip("native Codex credentials are unavailable")
    shutil.copy(auth, home / "auth.json")
    plugin = portable_harness().plugins[0]
    CodexPluginInstaller(
        PluginCacheConfig(codex_home=home, marketplace=plugin.marketplace)
    ).ensure(
        root / ".codex/plugins" / plugin.name,
        root,
    )
    repository = tmp_path / "document-review"
    repository.mkdir()
    sh.Command("git")("init", "--initial-branch=main", _cwd=str(repository))
    target = repository / "DESIGN.md"
    target.write_text("# Previous design\n", encoding="utf-8")
    envelope = "*** Begin Patch\n*** Add File: DESIGN.md\n+# Agreed design\n+\n+All decisions.\n*** End Patch"
    environment = {**os.environ, "CODEX_HOME": str(home)}
    codex = sh.Command("codex")
    instruction = (
        "Call apply_patch exactly once with the following patch, verbatim. "
        "Do not call any other tools and do not work around a hook refusal. "
        "After the tool returns, report its result and stop.\n" + envelope
    )
    codex(
        "exec",
        "--enable",
        "hooks",
        "--dangerously-bypass-hook-trust",
        "--sandbox",
        "workspace-write",
        "--model",
        CodexModelTiers().strongest,
        "--cd",
        str(repository),
        instruction,
        _env=environment,
        _timeout=180,
    )
    assert target.read_text() == "# Previous design\n"
    store = QuestionRelay(repository / ".lup/questions.jsonl")
    (question,) = store.pending()
    assert question.operation.tool == "apply_patch"
    assert question.preconditions == {target: "# Previous design\n"}
    answer(
        repository, question.id, "operator", True, "Approve this fixture replacement"
    )
    codex(
        "exec",
        "resume",
        "--last",
        "--enable",
        "hooks",
        "--dangerously-bypass-hook-trust",
        instruction,
        _env=environment,
        _cwd=str(repository),
        _timeout=180,
    )
    assert target.read_text() == "# Agreed design\n\nAll decisions.\n"
    consumed = store.find(question.id)
    assert consumed is not None and consumed.state == "dispatched"
