"""Feedback aggregation: session results into domain metrics and cost rollups.

This is a TEMPLATE script. Run ``/lup:init`` to customize it for your domain.
"""

from collections import Counter, defaultdict

from lup.channels.models import utc_now
from lup.types import Usage
from lup.devtools.feedback.models import (
    BackendCostRow,
    FeedbackMetrics,
    LoadedSession,
    SessionResult,
    empty_cost_row,
)


# lup: template: aggregate the outcome fields your domain scores on.
def compute_metrics(results: list[SessionResult]) -> FeedbackMetrics:
    """Compute aggregate metrics from session results.

    Examples: a mean Brier score, win rate, or rating average.

    Sessions are counted per backend (``sessions_by_sdk``) so mixed
    Claude/Codex collections never pool silently into one trend.
    """
    sessions_with_outcomes = sum(1 for r in results if r.outcome is not None)
    by_sdk = Counter(r.agent_sdk or "unknown" for r in results)

    return FeedbackMetrics(
        collection_timestamp=utc_now().isoformat(),
        total_sessions=len(results),
        sessions_with_outcomes=sessions_with_outcomes,
        sessions_by_sdk=dict(by_sdk),
        results=results,
    )


def rollup_costs(
    sessions: list[LoadedSession],
) -> dict[str, BackendCostRow]:
    """Group session cost and token totals by ``agent_sdk``.

    Sessions without a cost (codex/openai runs without
    ``CODEX_USD_PER_MTOK_*`` rates) count into ``without_cost`` so a
    missing-rates gap stays visible instead of reading as free.
    """
    rows: defaultdict[str, BackendCostRow] = defaultdict(empty_cost_row)
    for s in sessions:
        row = rows[s.agent_sdk or "unknown"]
        row["sessions"] += 1
        if s.cost_usd:
            row["cost_usd"] += s.cost_usd
        else:
            row["without_cost"] += 1
        usage = s.token_usage or Usage()
        row["input_tokens"] += usage.input_tokens
        row["output_tokens"] += usage.output_tokens
        row["cache_read_input_tokens"] += usage.cache_read_input_tokens
    return dict(rows)
