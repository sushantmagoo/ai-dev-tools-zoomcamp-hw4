"""Collect the evidence needed to understand an alert: endpoint, metrics, logs and traces.

Every backend call is best effort: a failing backend is recorded in the context
as an error instead of stopping the incident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from responder.config import Settings

ERROR_LOG_QUERY = '{{service_name="{service}"}} | severity_text=~"WARN.*|ERROR|CRITICAL"'


def parse_time(value: str | None) -> datetime | None:
    """Parse a Grafana/Alertmanager timestamp; the zero time means 'unset'."""
    if not value or value.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def affected_endpoint(alert: dict) -> dict[str, str | None]:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    route = labels.get("http_route")
    method = labels.get("http_request_method")
    endpoint = annotations.get("endpoint")
    if endpoint and not route:
        parts = endpoint.split(maxsplit=1)
        method, route = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
    return {
        "method": method or ("GET" if route else None),
        "route": route,
        "description": endpoint or (f"GET {route}" if route else None),
    }


def time_window(alert: dict, settings: Settings, now: datetime | None = None) -> dict[str, str]:
    now = now or datetime.now(timezone.utc)
    started = parse_time(alert.get("startsAt")) or now
    ended = parse_time(alert.get("endsAt"))
    start = started - timedelta(minutes=settings.lookback_minutes)
    end = min(ended, now) if ended and ended > start else now
    return {"start": start.isoformat(), "end": end.isoformat(), "alert_started": started.isoformat()}


def normalize_trace_id(trace_id: str) -> str:
    """Tempo search drops leading zeros from trace IDs; restore the 32-hex-digit form."""
    return trace_id.lower().zfill(32)


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1e9)


def _best_effort(fn, *args) -> dict[str, Any]:
    try:
        return {"ok": True, "data": fn(*args)}
    except Exception as exc:  # noqa: BLE001 - record and continue
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


class Collector:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=10)

    # --- Prometheus -------------------------------------------------------
    def metrics(self, window: dict, route: str | None) -> dict:
        start, end = datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"])
        seconds = max(int((end - start).total_seconds()), 60)
        route_filter = f', http_route="{route}"' if route else ""
        base = f'order_lookup_requests_total{{job="{self.settings.service_name}"{route_filter}}}'
        queries = {
            "requests_by_status": f"sum by (http_route, http_response_status_code, error_type) "
                                  f"(increase({base}[{seconds}s]))",
            "error_ratio_5xx": f'(sum(increase({base[:-1]}, http_response_status_code=~"5.."}}[{seconds}s])) '
                               f"or vector(0)) / sum(increase({base}[{seconds}s]))",
        }
        results = {}
        for name, query in queries.items():
            response = self.client.get(
                f"{self.settings.prometheus_url}/api/v1/query",
                params={"query": query, "time": end.timestamp()},
            )
            response.raise_for_status()
            results[name] = {
                "query": query,
                "result": [
                    {"labels": item["metric"], "value": item["value"][1]}
                    for item in response.json()["data"]["result"]
                ],
            }
        return results

    # --- Loki -------------------------------------------------------------
    def logs(self, window: dict) -> dict:
        query = ERROR_LOG_QUERY.format(service=self.settings.service_name)
        response = self.client.get(
            f"{self.settings.loki_url}/loki/api/v1/query_range",
            params={
                "query": query,
                "start": _ns(window["start"]),
                "end": _ns(window["end"]),
                "limit": self.settings.max_logs,
                "direction": "backward",
            },
        )
        response.raise_for_status()
        entries = []
        for stream in response.json()["data"]["result"]:
            labels = stream["stream"]
            for timestamp, line in stream["values"]:
                entries.append({
                    "time": datetime.fromtimestamp(int(timestamp) / 1e9, timezone.utc).isoformat(),
                    "message": line,
                    "severity": labels.get("severity_text"),
                    "trace_id": labels.get("trace_id"),
                    "span_id": labels.get("span_id"),
                    "attributes": {k: v for k, v in labels.items()
                                   if k not in {"severity_text", "trace_id", "span_id"}},
                })
        entries.sort(key=lambda e: e["time"], reverse=True)
        return {"query": query, "entries": entries[: self.settings.max_logs]}

    # --- Tempo ------------------------------------------------------------
    def search_error_traces(self, window: dict, route: str | None) -> list[str]:
        conditions = [f'resource.service.name="{self.settings.service_name}"', "status=error"]
        if route:
            conditions.append(f'span.http.route="{route}"')
        response = self.client.get(
            f"{self.settings.tempo_url}/api/search",
            params={
                "q": "{" + " && ".join(conditions) + "}",
                "start": int(datetime.fromisoformat(window["start"]).timestamp()),
                "end": int(datetime.fromisoformat(window["end"]).timestamp()) + 1,
                "limit": self.settings.max_traces,
            },
        )
        response.raise_for_status()
        return [normalize_trace_id(t["traceID"]) for t in response.json().get("traces", [])]

    def trace(self, trace_id: str) -> dict:
        response = self.client.get(f"{self.settings.tempo_url}/api/v2/traces/{trace_id}")
        response.raise_for_status()
        return response.json()

    # --- All together -----------------------------------------------------
    def collect(self, alert: dict) -> dict:
        endpoint = affected_endpoint(alert)
        window = time_window(alert, self.settings)
        metrics = _best_effort(self.metrics, window, endpoint["route"])
        logs = _best_effort(self.logs, window)

        # Traces: those referenced by error logs first, then Tempo's error search.
        trace_ids: list[str] = []
        for entry in (logs.get("data") or {}).get("entries", []):
            if entry.get("trace_id") and entry["severity"] in {"ERROR", "CRITICAL"}:
                trace_ids.append(normalize_trace_id(entry["trace_id"]))
        search = _best_effort(self.search_error_traces, window, endpoint["route"])
        trace_ids.extend(search.get("data") or [])
        trace_ids = list(dict.fromkeys(trace_ids))[: self.settings.max_traces]

        traces = {}
        for trace_id in trace_ids:
            result = _best_effort(self.trace, trace_id)
            traces[trace_id] = result["data"] if result["ok"] else {"error": result["error"]}

        return {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "endpoint": endpoint,
            "window": window,
            "metrics": metrics,
            "logs": logs,
            "trace_search": search,
            "traces": traces,
        }


def summarize_trace(trace: dict) -> list[dict]:
    """Flatten a Tempo v2 trace into spans with their key attributes and exceptions."""
    spans = []
    batches = (trace.get("trace") or trace).get("resourceSpans") or (trace.get("trace") or trace).get("batches") or []
    for batch in batches:
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                attrs = {a["key"]: _otlp_value(a.get("value", {})) for a in span.get("attributes", [])}
                exceptions = []
                for event in span.get("events", []):
                    if event.get("name") == "exception":
                        ev = {a["key"]: _otlp_value(a.get("value", {})) for a in event.get("attributes", [])}
                        exceptions.append({
                            "type": ev.get("exception.type"),
                            "message": ev.get("exception.message"),
                            "stacktrace": ev.get("exception.stacktrace"),
                        })
                start, end = int(span.get("startTimeUnixNano", 0)), int(span.get("endTimeUnixNano", 0))
                spans.append({
                    "name": span.get("name"),
                    "span_id": span.get("spanId"),
                    "parent_span_id": span.get("parentSpanId"),
                    "status": (span.get("status") or {}).get("code"),
                    "duration_ms": round((end - start) / 1e6, 3) if end else None,
                    "attributes": attrs,
                    "exceptions": exceptions,
                })
    return spans


def _otlp_value(value: dict) -> Any:
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return value
