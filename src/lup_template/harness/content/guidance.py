"""Canonical repository guidance.

The portable conventions are composed from ``lup.harness.content
.conventions`` rather than restated here, so this document holds only what is
true of *this* repository: its two-package layout, its tooling paths, and the
placement rule that follows from having both halves in one tree.

What earns space here is what an agent needs before it knows to look: a norm
no gate fires on, or a mechanism it must recognise the first time one stops
it. Anything a denial names at the moment it matters is left to the denial
and to the generated reference behind it — a second copy in always-loaded
prose can only fall behind the registry that actually runs, and is redundant
for as long as it agrees.

One named section per stretch, each declaring the chapter of the spine it
renders into. The document is held to a byte ceiling it sits close to, so a
section is also the unit somebody condensing works in: what ``dev guidance``
reports against, and what a project retires or rewrites by name.
"""

import lup.harness.models as models
from lup.harness.content.application import ApplicationLayout

HEADER = models.GuidanceSection(
    id="header",
    chapter="orientation",
    parts=[
        models.Passage(module=__name__, name="header"),
    ],
)

CHANGING_THE_POLICY = models.GuidanceSection(
    id="changing-the-policy",
    chapter="gates",
    parts=[
        models.Passage(
            module=__name__,
            name="changing-the-policy",
            values={
                "hooks_skill": models.SkillInvocation(plugin="lup", skill="hooks"),
                "project_settings": models.NativePath(location="project_settings"),
            },
        ),
    ],
)

MARKER_VOCABULARY = models.GuidanceSection(
    id="marker-vocabulary",
    chapter="gates",
    parts=[
        models.Passage(
            module=__name__,
            name="marker-vocabulary",
            values={
                # The resolver answers `template:` markers; a project that
                # declined it is not pointed at a skill it cannot run.
                "resolve_pointer": models.WhereTaken(
                    module="resolver",
                    parts=[
                        models.TextPart(text=" (`"),
                        models.SkillInvocation(plugin="lup", skill="resolve"),
                        models.TextPart(text="`)"),
                    ],
                ),
            },
        ),
    ],
)

DEFERRED_WORK = models.GuidanceSection(
    id="deferred-work",
    chapter="gates",
    parts=[
        models.Passage(module=__name__, name="deferred-work"),
    ],
)

DEVELOPMENT_WORKFLOW = models.GuidanceSection(
    id="development-workflow",
    chapter="workflow",
    parts=[
        models.Passage(module=__name__, name="development-workflow"),
    ],
)

COMMIT_TYPE_POINTER = models.GuidanceSection(
    id="commit-type-pointer",
    chapter="workflow",
    parts=[
        models.Passage(module=__name__, name="commit-type-pointer"),
    ],
)


def code_conventions(layout: ApplicationLayout) -> models.GuidanceSection:
    """How this repository's code is written, naming the application where it sits."""
    return models.GuidanceSection(
        id="code-conventions",
        chapter="code",
        parts=[
            models.Passage(
                module=__name__,
                name="code-conventions",
                values={"application": models.code(layout.directory())},
            ),
        ],
    )


def tooling(layout: ApplicationLayout) -> models.GuidanceSection:
    """The development CLI and the tools beside it, naming the half that composes it."""
    return models.GuidanceSection(
        id="tooling",
        chapter="tooling",
        parts=[
            models.Passage(
                module=__name__,
                name="tooling",
                values={"application": models.code(layout.directory())},
            ),
        ],
    )


def configuration(layout: ApplicationLayout) -> models.GuidanceSection:
    """Where settings load from, naming the one module that reads the environment."""
    return models.GuidanceSection(
        id="configuration",
        chapter="tooling",
        parts=[
            models.Passage(
                module=__name__,
                name="configuration",
                values={"config_py": models.code(layout.path("agent", "config.py"))},
            ),
        ],
    )


KEEPING_IN_STEP = models.GuidanceSection(
    id="keeping-in-step",
    chapter="tooling",
    parts=[
        models.Passage(
            module=__name__,
            name="keeping-in-step",
            values={
                "update_skill": models.SkillInvocation(plugin="lup", skill="update"),
                "upstream_skill": models.SkillInvocation(
                    plugin="lup", skill="upstream"
                ),
            },
        ),
    ],
)

PROCESS_AND_COMMUNICATION = models.GuidanceSection(
    id="process-and-communication",
    chapter="process",
    parts=[
        models.Passage(module=__name__, name="process-and-communication"),
    ],
)

REPORTING_FRICTION = models.GuidanceSection(
    id="reporting-friction",
    chapter="process",
    parts=[
        models.Passage(module=__name__, name="reporting-friction"),
    ],
)

EXTERNAL_RESOURCES = models.GuidanceSection(
    id="external-resources",
    chapter="meta",
    parts=[
        models.Passage(
            module=__name__,
            name="external-resources",
            values={
                "runtime_docs": models.RuntimeDocs(),
            },
        ),
    ],
)

SELF_IMPROVEMENT = models.GuidanceSection(
    id="self-improvement",
    chapter="meta",
    parts=[
        models.Passage(module=__name__, name="self-improvement"),
    ],
)


def document(sections: list[models.GuidanceSection]) -> models.PromptDocument:
    """The composed sections as one document.

    There is no order here to read. Reading order is the chapter spine crossed
    with the module roster, resolved where the modules are — so this takes the
    sequence rather than declaring it, and a subject that grew a paragraph
    places it by naming a chapter instead of by being spliced into a list
    somebody else maintains.

    What the hand-written list cost was not effort but ownership: sections
    from two packages sat in one sequence no module could claim a stretch of,
    so declining a subject left its prose behind and adding one meant editing
    a file in the other half.
    """
    return models.PromptDocument(source=__name__, parts=models.sectioned(sections))
