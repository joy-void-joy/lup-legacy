"""What this repository grants, refuses, and enables for itself.

The rendering is the library's (:mod:`lup.devtools.harness.settings`), which
derives the marketplace key, the plugin enablement, the served-tool grants,
and the sandbox boundaries from the declaration itself. Named here is only
the half no derivation can reach: which vendor plugins this repository turns
on, and which tools it grants or refuses by judgement.
"""

from lup.devtools.harness.settings import Settings, project_settings as render
from collections.abc import Sequence

from lup.harness.models import Plugin
from lup.mcp import ToolServer
from lup.types import JsonObject

DECLARED = Settings(
    base={
        "coauthorship": True,
        "fileSuggestion": {
            "command": ".claude/plugins/lup/scripts/file_suggest.sh",
            "type": "command",
        },
    },
    # The runtime rolls a session into a subagent-steer arm without
    # notice, and one arm appends a section arguing against delegating
    # at all. Skills here dispatch subagents by design, so the arm that
    # leaves that judgement to this guidance is pinned instead of rolled.
    env={"CLAUDE_CODE_THISTLE_GREBE": "default", "ENABLE_TOOL_SEARCH": "true"},
    official_plugins={
        "agent-sdk-dev@claude-plugins-official": True,
        "claude-md-management@claude-plugins-official": True,
        "github@claude-plugins-official": True,
        # Refused rather than absent: this repository installs
        # `pyright-langserver` for its own audit, which is the condition an
        # editor language server is offered on, so a project that only left
        # this out would be asked again. Two checkers over the same files
        # disagree, and the one wired to the gates is the per-edit check in
        # `lup.policy.assets.host`; the navigation half is already served by
        # the codeintel tools. A vendor server rooted where the session opened
        # also answers about the launch checkout after work moves to a
        # worktree, which the guidance asks for on every change.
        "pyright-lsp@claude-plugins-official": False,
    },
    allowed=[
        "WebSearch",
        "Skill(lup:hooks)",
        # `EnterWorktree` is deliberately absent, and refused outright in the
        # tool table: entering one arms a wall that refuses fourteen ordinary
        # shell words for the rest of the session, and the guidance now sends
        # a session into a worktree by launching it rooted there instead.
        # `ExitWorktree` stays granted for the session that got in anyway —
        # what holds it back is the tool's own refusal on uncommitted files
        # and unmerged commits, which its caller overrides by setting
        # `discard_changes`, so the grant is trust in that refusal rather
        # than a second gate behind it.
        "ExitWorktree",
        "Read(./.claude/settings.json.local*)",
        "Read(./sync.json.local)",
    ],
    # `.env.local` and `.env.<mode>.local` are absent because they are withheld
    # credential paths, whose `Read` rules are compiled from that declaration.
    denied=[
        "Read(./**/secrets*.local)",
        "Read(./**/*.secret.local)",
    ],
)


def project_settings(
    plugin: Plugin | None, servers: Sequence[ToolServer] = ()
) -> JsonObject:
    """Render this repository's settings artifact from its declaration.

    ``servers`` are the tool servers every launched session carries, whose
    tools the settings grant.
    """
    return render(DECLARED, plugin, servers)
