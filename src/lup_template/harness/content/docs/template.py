# lup: ignore[constant-declaration]
# The two constants here are this page's own prose, split so the layout diagram
# can be drawn between them from a checkout the caller supplies. Prose is not a
# judgement a caller could override with a different value; a project wanting
# different words composes a different document.
"""Guide to ``src/lup_template``, the application built on the library."""

from pathlib import Path

import lup.harness.models as models

from lup.harness.content.tree import annotated_tree
from lup.devtools.subapps import SubAppSpec, subapp_bullets
from lup_template.harness.content.catalog import LAYOUT


HEAD_PARTS: list[models.PromptPart] = [
    models.TextPart(
        text=r"""# The application template

`src/lup_template` is the half you fork. It is a complete working agent — an
`lup` application, a development CLI, and the harness declarations that
generate the native plugin trees — arranged so that adapting it to a new
domain is a series of small, obvious edits rather than a rewrite.

Three packages, split by what changes and how often.

| Package | Owns | Changes |
| --- | --- | --- |
| `agent/` | Reasoning. Prompts, output schema, tools, subagents, tool policy. No I/O. | Every time the domain does |
| `environment/` | I/O. The CLI that starts a run, the session lifecycle, what happens to a result. | When the surface changes |
| `devtools/` | Development. What this project adds to the inherited `lup-devtools` CLI, and the typed harness declarations. | When the workflow changes |

Keeping the agent free of I/O is what makes it improvable: the self-improvement
loop reads traces and changes prompts, tools, and models, and it never has to
reason about where a session came from.

```
"""
    ),
]
"""Everything before the layout diagram, which is drawn rather than declared."""

CLI_PARTS: list[models.PromptPart] = [
    models.TextPart(
        text=r"""
```

Nothing above is written down. The structure is walked from the checkout when
this page is generated, and each caption is the module's own docstring — a
package's from its `__init__.py`. So a module that is renamed, moved, or
re-described changes this page by being edited, and one that is deleted leaves
it by being deleted. A module with no docstring simply has no caption, which
is the only nudge this page gives about writing one.

## `agent/` — what you change first

| Module | What it holds | Adapt it by |
| --- | --- | --- |
| `models.py` | `AgentOutput` and `Factor`: the structured result a turn must submit. | Replacing the fields with your domain's result. The prompt's output section is generated from this schema, so it cannot drift. |
| `prompts.py` | The system prompt composed from named sections. | Editing `PURPOSE` and `GUIDELINES`. Leave `output_format()` alone — it reads the schema. |
| `toolsets.py` | Which MCP tool groups a session carries, as one declaration: lup's own named, this domain's own built. | Writing a group's builder and naming it in `declared_tool_groups()` — server registration, the names a subprocess backend serves and the servers a runtime starts are all read off that list. |
| `tools/` | The tool implementations. `example.py` is placeholder search/fetch/read/glob; `reflect.py`, `realtime.py`, and `nested.py` are working patterns. | Replacing `example.py` with your domain's tools. |
| `subagents.py` | Portable `SubagentSpec` declarations: capabilities, exact tool grants, model tiers. | Adding specs to `ALL_SPECS`. |
| `tool_policy.py` | Which tools are available given the configuration — a missing API key bans its tools rather than failing at call time. | Adding an exclusion for each new conditional dependency. |
| `config.py` | Pydantic settings from `.env` and `.env.local`: model, budget, turn cap, sandbox, paths. | Adding settings, never reading the environment directly elsewhere. |
| `core.py` | `provider_factory()` — the **one** place a concrete adapter is named. | Rarely. Everything downstream takes the portable `Agent` it returns. |

That last row is the load-bearing one. `seam-boundary` permits a concrete
adapter import in `agent/core.py` and a short list of other composition roots,
and rejects it everywhere else — so provider choice cannot spread by
accident.

## `environment/` — where a run begins

`environment/cli/__main__.py` is the `lup` entry point, with `run` and `loop`
commands. A run opens a session through the agent's factory, executes the
task, and disposes of the result. The template's disposal is a git commit of
the session directory, which suits a batch domain and is the first thing an
interactive domain deletes.

The boundary is deliberate: outside events arrive here, and only here.

## Configuration

`.env` holds the template's committed defaults; `.env.local` holds secrets
and personal overrides and is gitignored. `.env.local` wins where both
declare a value. `ANTHROPIC_API_KEY` is read straight from the environment by
the SDK; everything else is loaded through pydantic-settings in
`agent/config.py`, which is the only module that reads the environment.

```bash
# .env.local — secrets and overrides

# AGENT_MODEL=claude-opus-5
# AGENT_MAX_BUDGET_USD=5.00
# AGENT_MAX_TURNS=50
# AGENT_REASONING_EFFORT=high   # low through ultra, on both backends
# CODEX_EFFORT=xhigh            # overrides AGENT_REASONING_EFFORT on Codex/OpenAI
# AGENT_SANDBOX_ENABLED=false   # run without Docker (disables code execution tools)
# AGENT_NOTES_PATH=./notes      # relocate session data
# AGENT_LOGS_PATH=./logs        # relocate trace logs
```

With `AGENT_REASONING_EFFORT` and `CODEX_EFFORT` both unset, a session takes
the model's default effort: `xhigh` clamped to the model's catalog row — the
first of `xhigh`, `high`, `medium`, `low` the row takes, none for a model
whose row lists no effort, and `xhigh` for a model with no row. The agent's
`resolved_effort()`, beside `model_id()`, answers which one a session gets,
and an effort named that the model lacks is refused where the agent is
declared.

## `devtools/` — the development CLI

`lup-devtools` is the second entry point, and most of it is not here. The
workflow sub-apps live in `lup.devtools` and are *inherited*: an upgrade
brings their improvements without a merge, which is the point — they are
development tooling, not this domain, and a fork of them goes stale the day
it is taken. Which of them this project serves is not written down anywhere: a
sub-app is one surface of a subject, so the roster follows the modules this
project adopted and is derived beside them in `harness/content/catalog.py`.
`devtools/subapps.py` declares the one thing that cannot be derived — what a
sub-app of this project's own is called — and `devtools/main.py` is where each
name meets the app answering to it.

That is also where `usage` is decided, twice over: whether to serve it, and
which backends' accounts it reads. The display, the pacing bars, and the
snapshot live in `lup.observability.usage`; each adapter contributes a reader that turns
its own account call into the one report shape, and the roster composes the
display around the readers it names.

"""
    ),
]
"""Everything between the diagram and the CLI roster, which is composed."""

CLOSING_PARTS: list[models.PromptPart] = [
    models.Passage(
        module=__name__,
        name="template",
        values={
            "application": models.code(LAYOUT.package),
            "application_2": models.code(LAYOUT.package),
            # The setup page is the setup module's, the pane showing it the
            # dashboard's, and the page it shares its loopback guard with the
            # resolver's: each sentence stays only where its module does.
            "setup_page": models.WhereTaken(
                module="setup",
                parts=[
                    models.Passage(
                        module=__name__,
                        name="setup-page",
                        values={
                            "setup_pane": models.WhereTaken(
                                module="dashboard",
                                parts=[
                                    models.TextPart(
                                        text=", and the dashboard runs it to "
                                        "show it as this repository's setup "
                                        "pane"
                                    )
                                ],
                            ),
                            "shared_with_supervisor": models.WhereTaken(
                                module="resolver",
                                parts=[
                                    models.TextPart(
                                        text=", shared with the\nresolver's "
                                        "supervisor page; see "
                                        "[supervisor.md](supervisor.md)"
                                    )
                                ],
                            ),
                        },
                    )
                ],
            ),
            # The registry is the upstream module's command tree, and the two
            # skills built on it are that module's too.
            "sync_registry": models.WhereTaken(
                module="upstream",
                parts=[
                    models.Passage(
                        module=__name__,
                        name="sync-registry",
                        values={
                            "update_skill": models.SkillInvocation(
                                plugin="lup", skill="update"
                            ),
                            "import_skill": models.SkillInvocation(
                                plugin="lup", skill="import"
                            ),
                            "update_skill_2": models.SkillInvocation(
                                plugin="lup", skill="update"
                            ),
                            # Initialization is template-init's, which a
                            # project keeping upstream may still decline.
                            "init_points": models.WhereTaken(
                                module="template-init",
                                parts=[
                                    models.SkillInvocation(plugin="lup", skill="init"),
                                    models.TextPart(
                                        text=" points it at the repository "
                                        "the project was\nactually generated "
                                        "from when that is a fork "
                                        "(`dev init upstream`).\n"
                                    ),
                                ],
                            ),
                        },
                    )
                ],
            ),
        },
    ),
]
"""Everything after the diagram, from the per-package guide onward."""


def document(root: Path, subapps: list[SubAppSpec]) -> models.PromptDocument:
    """This page, with its layout diagram walked from ``root``.

    Takes the checkout rather than finding one. A document that resolved its
    own root would read the filesystem at import, which makes importing this
    module — and so every CLI command that composes it — fail anywhere but
    inside a lup project.

    Takes the roster for a sharper reason. Which sub-apps a CLI serves follows
    from which modules the project adopted, and this page is declared by one of
    those modules — so reading the roster here would have the composition
    import a page that is part of it. The composition passes it down instead.
    """
    return models.PromptDocument(
        source=__name__,
        parts=[
            *HEAD_PARTS,
            models.TextPart(text=annotated_tree(root, LAYOUT.path())),
            *CLI_PARTS,
            models.TextPart(text=subapp_bullets(subapps)),
            *CLOSING_PARTS,
        ],
    )
