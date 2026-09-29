"""The TypeScript surfaces this library ships, each naming what its page is typed against.

One declaration per surface, read twice: the schema the frontend compiles its
types from is emitted over every model listed here, and the bundles tree is
built for every name. A surface added below reaches both, and a model a page
reads that is not listed fails the build rather than a browser. A project
with a surface of its own lists it beside these in its composition.
"""

from lup.devtools.dashboard.reviews import (
    ReviewAnswer,
    ReviewDecision,
    ReviewSnapshot,
)
from lup.devtools.dashboard.live import ReplyOutcome, ReplyRequest
from lup.devtools.dashboard.panes import SetupPane
from lup.devtools.dashboard.serve import RowRequest, ScopeRequest, StepReply
from lup.devtools.dashboard.stream import StreamFrame
from lup.devtools.dashboard.wizard import StepAnswers, WizardView
from lup.devtools.review.app import ReviewDetail
from lup.devtools.supervisor.projection import (
    ActorIndex,
    AnswerSubmission,
    MessageSubmission,
    ParkSubmission,
    RunIndex,
    SupervisorState,
)
from lup.ledger.views import ExportView, GraphView, KindsView, NodeDetail
from lup.resolver.record import JournalEntry
from lup.web.build import Surface

EXPLORER = Surface(
    name="explorer", models=[GraphView, NodeDetail, KindsView, ExportView]
)
"""The ledger explorer: the graph, one node in full, the kinds, and the export."""

WIZARD = Surface(
    name="wizard",
    models=[WizardView, StepReply, StepAnswers, RowRequest, ScopeRequest],
)
"""The setup wizard: the page as drawn, every reply, and what the page posts."""


SUPERVISOR = Surface(
    name="supervisor",
    models=[
        SupervisorState,
        RunIndex,
        ActorIndex,
        JournalEntry,
        AnswerSubmission,
        MessageSubmission,
        ParkSubmission,
    ],
)
"""The resolver supervisor: one run projected, the rail, the record, and what the page posts."""

DASHBOARD = Surface(
    name="dashboard",
    models=[
        StreamFrame,
        ReviewSnapshot,
        ReviewDetail,
        ReviewAnswer,
        ReviewDecision,
        ReplyRequest,
        ReplyOutcome,
        SetupPane,
    ],
)
"""The operator's dashboard: the live stream, parked reviews, captured changes, exact
decisions, a reply to a session, setup panes."""


# lup: ignore[library-default] — the surfaces this library authors, so the
# table is what it ships rather than a choice made for an adopter
LIBRARY_SURFACES = [EXPLORER, WIZARD, SUPERVISOR, DASHBOARD]
"""Every surface lup builds into its own package data."""
