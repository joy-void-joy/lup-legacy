"""What each launched Claude session spent, request by request, as its own telemetry reports it.

Every Claude session a launch holding the dashboard opens exports
OpenTelemetry to the dashboard's telemetry port. Of what it exports, two
signals together answer "which agent spent what", measured on Claude Code
2.1.285: each request's ``api_request`` log event carries its cost, its token
counts and its id, and the ``claude_code.llm_request`` span of the same
request carries the id of the subagent that made it (none for the session's
own conversation). The metrics name only whether a request was the
session's or *a* subagent's, never which, so they are not read.

The two arrive on separate exports, in either order. A request is charged
once its span has said whose it was, or once ``grace`` has passed without
one — a session exporting no traces is charged as its own conversation.

The port answers loopback alone and only a request bearing the telemetry
token every launch hands its session: a capability to report spend and
nothing else, and never the page's.
"""

import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import BaseModel, Field, ValidationError
from pydantic.alias_generators import to_camel

from lup.types import EnvVars

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — Claude Code's own event and span names,
# fixed by the runtime that emits them
REQUEST_EVENT = "api_request"
REQUEST_SPAN = "claude_code.llm_request"


class Wire(
    BaseModel,
    frozen=True,
    extra="ignore",
    alias_generator=to_camel,
    populate_by_name=True,
):
    """OTLP's JSON encoding: camelCase on the wire, everything unread ignored."""


class AnyValue(Wire, frozen=True):
    """One attribute's value, in whichever of OTLP's scalar slots it arrived."""

    string_value: str | None = None
    int_value: int | None = None
    double_value: float | None = None
    bool_value: bool | None = None

    def text(self) -> str:
        """The value as text, empty where it carries none."""
        for each in (self.string_value, self.int_value, self.double_value):
            if each is not None:
                return str(each)
        return ""

    def number(self) -> float:
        """The value as a number, zero where it carries none or text that reads as none."""
        if self.double_value is not None:
            return self.double_value
        if self.int_value is not None:
            return float(self.int_value)
        try:
            return float(self.string_value or 0)
        except ValueError:
            return 0.0


class KeyValue(Wire, frozen=True):
    key: str
    value: AnyValue = AnyValue()


class Attributed(Wire, frozen=True):
    """Anything carrying OTLP attributes, read by name."""

    attributes: list[KeyValue] = []

    def value(self, key: str) -> AnyValue:
        return next(
            (each.value for each in self.attributes if each.key == key), AnyValue()
        )


class LogRecord(Attributed, frozen=True):
    time_unix_nano: int = 0
    observed_time_unix_nano: int = 0


class ScopeLogs(Wire, frozen=True):
    log_records: list[LogRecord] = []


class ResourceLogs(Wire, frozen=True):
    scope_logs: list[ScopeLogs] = []


class LogsExport(Wire, frozen=True):
    resource_logs: list[ResourceLogs] = []


class Span(Attributed, frozen=True):
    name: str = ""
    start_time_unix_nano: int = 0


class ScopeSpans(Wire, frozen=True):
    spans: list[Span] = []


class ResourceSpans(Wire, frozen=True):
    scope_spans: list[ScopeSpans] = []


class TracesExport(Wire, frozen=True):
    resource_spans: list[ResourceSpans] = []


class RequestSpend(BaseModel, frozen=True):
    """One request a session made, what it cost and moved, and whose it was where known."""

    session: str
    """The runtime's id for the conversation that made it (``session.id``)."""

    request: str
    at: float
    """When it was made, in seconds since the epoch."""

    usd: float = 0.0
    tokens: int = 0
    agent: str = ""
    """The runtime's id for the subagent that made it; empty for the session's own."""


def spends(export: LogsExport) -> list[RequestSpend]:
    """Each request an export of log events reports, by its ``api_request`` event."""
    return [
        RequestSpend(
            session=record.value("session.id").text(),
            request=record.value("request_id").text(),
            at=(record.time_unix_nano or record.observed_time_unix_nano) / 1e9,
            usd=record.value("cost_usd").number(),
            tokens=int(
                sum(
                    record.value(name).number()
                    for name in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "cache_creation_tokens",
                    )
                )
            ),
        )
        for resource in export.resource_logs
        for scope in resource.scope_logs
        for record in scope.log_records
        if record.value("event.name").text() == REQUEST_EVENT
        and record.value("request_id").text()
    ]


class RequestAgent(BaseModel, frozen=True):
    """Whose one request was, as its span says."""

    request: str
    agent: str = ""


def agents(export: TracesExport) -> list[RequestAgent]:
    """Each request an export of spans says the maker of, by its ``llm_request`` span."""
    return [
        RequestAgent(
            request=span.value("request_id").text(),
            agent=span.value("agent_id").text(),
        )
        for resource in export.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
        if span.name == REQUEST_SPAN and span.value("request_id").text()
    ]


class TelemetryJoin:
    """Requests waiting for their span to say whose they were, and the ones settled.

    Shared between the receiver's threads and the governor's, so every
    method holds one lock. A span whose request never reported is let go once
    more than ``kept`` of them wait.
    """

    def __init__(self, grace: float = 30.0, kept: int = 4096) -> None:
        self.grace = grace
        self.kept = kept
        self.lock = threading.Lock()
        self.waiting: dict[str, RequestSpend] = {}
        self.makers: dict[str, RequestAgent] = {}

    def spent(self, received: list[RequestSpend]) -> None:
        with self.lock:
            self.waiting.update({each.request: each for each in received})

    def made(self, received: list[RequestAgent]) -> None:
        with self.lock:
            self.makers.update({each.request: each for each in received})

    def settled(self, now: float) -> list[RequestSpend]:
        """Every request whose maker is known, or whose span did not come within the grace."""
        with self.lock:
            ready = [
                each.model_copy(
                    update={
                        "agent": self.makers[each.request].agent
                        if each.request in self.makers
                        else ""
                    }
                )
                for each in self.waiting.values()
                if each.request in self.makers or now - each.at >= self.grace
            ]
            for each in ready:
                self.waiting.pop(each.request, None)
                self.makers.pop(each.request, None)
            if len(self.makers) > self.kept:
                self.makers = {
                    request: agent
                    for request, agent in self.makers.items()
                    if request in self.waiting
                }
            return ready


class TelemetryReceiver:
    """An OTLP/HTTP JSON endpoint on loopback, feeding what it receives into a join.

    Answers ``POST /v1/logs`` and ``/v1/traces``, and ``/v1/metrics`` with
    nothing read, since the person's own exporters may send them; refuses a
    request without the token, and anything else.
    """

    def __init__(
        self,
        port: int,
        token: str,
        join: TelemetryJoin,
        host: str = "127.0.0.1",
    ) -> None:
        self.join = join
        self.token = token
        accepted: dict[str, Callable[[bytes], None]] = {
            "/v1/logs": self.logs,
            "/v1/traces": self.traces,
            "/v1/metrics": lambda body: None,
        }
        expected = f"Bearer {token}"

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.headers.get("Authorization", "") != expected:
                    self.send_error(401, "the telemetry token is missing or wrong")
                    return
                if self.path not in accepted:
                    self.send_error(404, "this endpoint takes OTLP logs and traces")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                try:
                    accepted[self.path](body)
                except ValidationError as unreadable:
                    logger.warning(
                        "unreadable OTLP export on %s: %s", self.path, unreadable
                    )
                    self.send_error(400, "not an OTLP JSON export")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
                del code, size

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="lup-telemetry", daemon=True
        )

    def logs(self, body: bytes) -> None:
        self.join.spent(spends(LogsExport.model_validate_json(body)))

    def traces(self, body: bytes) -> None:
        self.join.made(agents(TracesExport.model_validate_json(body)))

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> "TelemetryReceiver":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TelemetryEnvironment(BaseModel, frozen=True):
    """The variables that point a Claude session's telemetry at the dashboard."""

    port: int = Field(gt=0)
    token: str

    def variables(self) -> EnvVars:
        """Logs and traces over OTLP/HTTP JSON to the telemetry port, bearing the token."""
        return {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{self.port}",
            "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {self.token}",
        }


def telemetry_variables() -> list[str]:
    """Every variable a session's telemetry is pointed with, by name.

    A person exporting any of them already sends telemetry somewhere of
    their own, which a launch leaves as it is.
    """
    return list(TelemetryEnvironment(port=1, token="").variables())
