"""The operator's dashboard: one page per person, held by every launch.

Its own module rather than core's, because it is a service every launch
starts on the host and a browser surface the `web` extra serves: a project
that answers its reviews from the terminal declines both at once, and keeps
every `review` verb.
"""

from lup.harness.content.docs import dashboard
from lup.harness.content.docs.catalog import page
from lup.harness.content.modules.specs import DASHBOARD
from lup.harness.models import GuidanceSection, TextPart
from lup.harness.modules import Module


def module() -> Module:
    """The dashboard's guidance and its reference page as one subject."""
    return Module(
        spec=DASHBOARD,
        guidance=[
            GuidanceSection(
                id="dashboard",
                chapter="tooling",
                parts=[
                    TextPart(
                        text=(
                            "## Dashboard\n\n"
                            "Every `harness claude|codex` launch holds the "
                            "operator's dashboard: one page per person over every "
                            "session's parked reviews and each repository's setup, "
                            "stopped with the last session. `dashboard status` "
                            "says where it is; opening, stopping and serving it "
                            "are the operator's. `docs/dashboard.md` describes it.\n\n"
                        )
                    )
                ],
            )
        ],
        documents=[page("dashboard", "dashboard.md", lambda _: dashboard.DOCUMENT)],
    )
