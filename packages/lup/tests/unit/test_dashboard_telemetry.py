"""A session's telemetry, read the way Claude Code 2.1.285 was measured to export it, and joined per request."""

import json

import httpx


from lup.devtools.dashboard.telemetry import (
    LogsExport,
    TelemetryEnvironment,
    TelemetryJoin,
    TelemetryReceiver,
    TracesExport,
    agents,
    spends,
    telemetry_variables,
)
from lup.types import JsonObject

SESSION = "c9e99ffb-84bd-4722-b2b1-175ff0ab66cf"


def attribute(key: str, value: str | int | float) -> JsonObject:
    match value:
        case str():
            return {"key": key, "value": {"stringValue": value}}
        case int():
            return {"key": key, "value": {"intValue": value}}
        case _:
            return {"key": key, "value": {"doubleValue": value}}


def request_event(request: str, cost: float, at_ns: int) -> JsonObject:
    """An ``api_request`` event as the measured export spelled it, account attributes left out."""
    return {
        "timeUnixNano": str(at_ns),
        "attributes": [
            attribute("event.name", "api_request"),
            attribute("session.id", SESSION),
            attribute("model", "claude-haiku-4-5-20251001"),
            attribute("input_tokens", 10),
            attribute("output_tokens", 255),
            attribute("cache_read_tokens", 13796),
            attribute("cache_creation_tokens", 8636),
            attribute("cost_usd", cost),
            attribute("request_id", request),
            attribute("query_source", "sdk"),
        ],
    }


def logs(*records: JsonObject) -> bytes:
    return json.dumps(
        {"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]}
    ).encode()


def traces(*spans: JsonObject) -> bytes:
    return json.dumps(
        {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}
    ).encode()


def request_span(request: str, agent: str = "") -> JsonObject:
    return {
        "name": "claude_code.llm_request",
        "attributes": [
            attribute("request_id", request),
            attribute("session.id", SESSION),
            *([attribute("agent_id", agent)] if agent else []),
        ],
    }


def test_a_request_event_says_what_it_cost_and_moved() -> None:
    found = spends(
        LogsExport.model_validate_json(
            logs(request_event("req_1", 0.0199366, 2_000_000_000))
        )
    )
    assert [
        (each.session, each.request, each.usd, each.tokens, each.at) for each in found
    ] == [(SESSION, "req_1", 0.0199366, 10 + 255 + 13796 + 8636, 2.0)]


def test_other_events_and_spans_are_passed_over() -> None:
    other: JsonObject = {
        "attributes": [
            attribute("event.name", "tool_result"),
            attribute("request_id", "x"),
        ]
    }
    assert spends(LogsExport.model_validate_json(logs(other))) == []
    tool: JsonObject = {
        "name": "claude_code.tool",
        "attributes": [attribute("request_id", "x")],
    }
    assert agents(TracesExport.model_validate_json(traces(tool))) == []


def test_a_request_is_settled_once_its_span_says_whose_it_was() -> None:
    join = TelemetryJoin(grace=30)
    join.spent(
        spends(LogsExport.model_validate_json(logs(request_event("req_1", 1.0, 10**9))))
    )
    assert join.settled(now=2.0) == []
    join.made(
        agents(
            TracesExport.model_validate_json(
                traces(request_span("req_1", "a9c6e3cf8a6129cf9"))
            )
        )
    )
    settled = join.settled(now=3.0)
    assert [(each.request, each.agent) for each in settled] == [
        ("req_1", "a9c6e3cf8a6129cf9")
    ]
    assert join.settled(now=4.0) == []


def test_a_span_may_come_first_and_a_missing_one_settles_as_the_session_s_own() -> None:
    join = TelemetryJoin(grace=30)
    join.made(agents(TracesExport.model_validate_json(traces(request_span("req_2")))))
    join.spent(
        spends(LogsExport.model_validate_json(logs(request_event("req_2", 1.0, 10**9))))
    )
    assert [each.agent for each in join.settled(now=1.0)] == [""]
    join.spent(
        spends(LogsExport.model_validate_json(logs(request_event("req_3", 1.0, 10**9))))
    )
    assert join.settled(now=20.0) == []
    assert [each.request for each in join.settled(now=31.0)] == ["req_3"]


def test_the_receiver_takes_only_exports_bearing_its_token() -> None:
    join = TelemetryJoin(grace=0)
    receiver = TelemetryReceiver(0, "secret", join).start()
    try:
        url = f"http://127.0.0.1:{receiver.port}"
        body = logs(request_event("req_9", 0.5, 10**9))
        refused = httpx.post(
            f"{url}/v1/logs", content=body, headers={"Content-Type": "application/json"}
        )
        assert refused.status_code == 401
        headers = {"Authorization": "Bearer secret", "Content-Type": "application/json"}
        assert (
            httpx.post(f"{url}/v1/logs", content=body, headers=headers).status_code
            == 200
        )
        assert (
            httpx.post(f"{url}/v1/metrics", content=b"{}", headers=headers).status_code
            == 200
        )
        assert (
            httpx.post(f"{url}/v1/other", content=b"{}", headers=headers).status_code
            == 404
        )
        assert (
            httpx.post(f"{url}/v1/logs", content=b"[1]", headers=headers).status_code
            == 400
        )
    finally:
        receiver.stop()
    assert [each.request for each in join.settled(now=10.0)] == ["req_9"]


def test_a_session_is_pointed_at_the_port_with_logs_and_traces_on() -> None:
    variables = TelemetryEnvironment(port=8776, token="t").variables()
    assert variables["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:8776"
    assert variables["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer t"
    assert (variables["OTEL_LOGS_EXPORTER"], variables["OTEL_TRACES_EXPORTER"]) == (
        "otlp",
        "otlp",
    )
    assert variables["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
    assert sorted(telemetry_variables()) == sorted(variables)
