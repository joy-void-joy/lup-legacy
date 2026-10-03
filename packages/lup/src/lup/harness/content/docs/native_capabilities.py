"""The evidence ledger of accepted native contracts.

Every version this page names, every schema digest it publishes, and every
fixture it cites is read from :mod:`lup.harness.evidence` rather than
spelled here. That module is what the doctor compares an installed CLI
against, so a refreshed probe moves the warning and the page together instead
of leaving the page asserting a version nothing was probed on, and a fixture
that moves fails generation instead of being published as a dead citation.
"""

from pathlib import Path

import lup.harness.models as models
from lup.harness.evidence import (
    SCHEMA_COMMAND,
    SCHEMA_DIGESTS,
    accepted,
    cited_fixture,
)
from lup.formats.markdown import CodeCell


def cited_library_fixture(library: Path | None, path: str) -> str:
    """One of lup's own fixture citations, checked where the tree holds it."""
    return path if library is None else cited_fixture(library, path)


LIBRARY_RUNTIME_FIXTURES = "packages/lup/tests/unit/test_adapter_runtime.py"
"""Where the adapter-runtime fixtures live in lup's repository.

A default rather than a constant, for the reason :data:`LIBRARY_DOCS_ROOT` is
one: a project that vendors lup elsewhere cites the copy it actually has.
"""

LIBRARY_DISPATCHER_FIXTURES = "tests/unit/test_harness_compilation.py"
"""Where the compiled-dispatcher fixtures live in lup's repository.

Lup's rather than the reading project's, even though the path is repository-
relative and a project has a `tests/unit/` of its own. What these pin is the
dispatcher lup compiles, which a downstream inherits whole — so the evidence
for it sits where the compilation does, and a project that never wrote such a
fixture would otherwise cite a file it does not have.
"""

LIBRARY_PUBLICATION_FIXTURES = (
    "packages/lup/tests/unit/test_codex_plugin_publication.py"
)
"""Where the Codex plugin publication fixtures live in lup's repository."""

LIBRARY_AUTHENTICATION_FIXTURES = "packages/lup/tests/unit/test_codex_launch_auth.py"
"""Where the Codex launch authentication fixtures live in lup's repository."""

LIBRARY_EXEC_FIXTURES = "tests/integration/test_codex_exec_governance.py"
"""Where the non-interactive Codex governance probe lives in lup's repository.

Lup's for the reason the dispatcher fixtures are: what it settles is whether
the tree lup generates governs a `codex exec`, which a downstream inherits
whole rather than re-establishes.
"""


def document(
    library: Path | None,
    runtime_fixtures_at: str = LIBRARY_RUNTIME_FIXTURES,
    dispatcher_fixtures_at: str = LIBRARY_DISPATCHER_FIXTURES,
    exec_fixtures_at: str = LIBRARY_EXEC_FIXTURES,
    publication_fixtures_at: str = LIBRARY_PUBLICATION_FIXTURES,
    authentication_fixtures_at: str = LIBRARY_AUTHENTICATION_FIXTURES,
) -> models.PromptDocument:
    """This page, with every version read from the ledger the doctor uses.

    ``library`` is the tree holding lup's own suite: its own checkout where
    the page is generated from lup, and ``None`` for a project that took lup
    as a distribution, where the fixtures are simply not present — a built
    distribution ships the code they pin and none of them.

    Both citations still name where the evidence is, which is a fact about
    lup's repository and true read from anywhere. Existence is checked only
    against a tree that could hold them, so the check keeps failing loudly
    where a fixture could actually move, and no downstream is asked to prove
    a path its dependency never gave it.
    """
    claude = accepted("claude-cli")
    sdk = accepted("claude-agent-sdk")
    codex = accepted("codex-cli")
    claude_cli = claude.version
    claude_sdk = sdk.version
    codex_cli = codex.version
    runtime_fixtures = cited_library_fixture(library, runtime_fixtures_at)
    dispatcher_fixtures = cited_library_fixture(library, dispatcher_fixtures_at)
    exec_fixtures = cited_library_fixture(library, exec_fixtures_at)
    publication_fixture = cited_library_fixture(library, publication_fixtures_at)
    authentication_fixture = cited_library_fixture(library, authentication_fixtures_at)
    return models.PromptDocument(
        source=__name__,
        parts=[
            models.Passage(
                module=__name__,
                values={
                    "table": models.MarkdownTable(
                        headers=["Schema", "SHA-256"],
                        rows=[
                            [CodeCell(text=digest.path), CodeCell(text=digest.sha256)]
                            for digest in SCHEMA_DIGESTS
                        ],
                    ),
                    "claude_cli": models.plain(claude_cli),
                    "refreshed": models.plain(claude.refreshed),
                    "claude_sdk": models.plain(claude_sdk),
                    "refreshed_2": models.plain(sdk.refreshed),
                    "codex_cli": models.plain(codex_cli),
                    "refreshed_3": models.plain(codex.refreshed),
                    "runtime_fixtures": models.code(runtime_fixtures),
                    "dispatcher_fixtures": models.code(dispatcher_fixtures),
                    "exec_fixtures": models.code(exec_fixtures),
                    "schema_command_spelled_temporary_directory": models.plain(
                        SCHEMA_COMMAND.spelled("<temporary-directory>")
                    ),
                    "publication_fixture": models.code(publication_fixture),
                    "authentication_fixture": models.code(authentication_fixture),
                    # Where the report to a runtime's vendor is drafted is the
                    # upstream module's page.
                    "upstream_report": models.WhereTaken(
                        module="upstream",
                        parts=[
                            models.TextPart(
                                text="; `docs/upstream-reports.md` carries the "
                                "report to send"
                            )
                        ],
                    ),
                },
            ),
        ],
    )
