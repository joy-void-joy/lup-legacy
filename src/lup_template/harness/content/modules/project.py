"""What this repository is, and what it expects of a session working in it.

The module holding this repository's own framing: the document's opening, how
work moves through it, how its code is written, what its tooling is, how to
report, and where to read further. Every section here is prose lup's library
had no standing to write, because each is a judgement this project made about
itself rather than a rule the framework enforces.

It sits **first** in the composed roster, and that placement is load-bearing
rather than ceremonial. Guidance renders as the chapter spine crossed with the
roster, so a module's position is where its prose lands inside whichever
chapter each section named — and this repository's framing is what opens a
chapter, with the library's general statement of the same subject following.

A project adopting this scaffold rewrites these sections rather than declining
them: the seat is the point, not the words in it — which is why the spec is
essential. So are its two pages: the decisions behind this project's tooling,
and `docs/template.md`, the guide to the application this project *is*, drawn
from its own checkout and read by every component guide beside it.
"""

import lup_template.harness.content.guidance as guidance
from lup.harness.content.application import ApplicationLayout
from lup.harness.content.docs.catalog import page
from lup.harness.modules import Module
from lup_template.harness.content.docs import decisions, template
from lup_template.harness.content.modules.specs import PROJECT


def module(layout: ApplicationLayout) -> Module:
    """This repository's own framing as one value, against its own layout.

    The layout is here for the pages rather than for the prose: a page declares
    where it is written, and where this project's pages are written is inside
    this project's package — which only the project knows the name of.
    """
    return Module(
        spec=PROJECT,
        guidance=[
            guidance.HEADER,
            guidance.DEVELOPMENT_WORKFLOW,
            guidance.code_conventions(layout),
            guidance.tooling(layout),
            guidance.PROCESS_AND_COMMUNICATION,
            guidance.REPORTING_FRICTION,
            guidance.EXTERNAL_RESOURCES,
        ],
        documents=[
            page(
                "decisions",
                "dev-tooling-decisions.md",
                lambda _: decisions.DOCUMENT,
                layout.docs(),
            ),
            page(
                "template",
                "template.md",
                lambda context: template.document(context.root, context.subapps),
                layout.docs(),
            ),
        ],
    )
