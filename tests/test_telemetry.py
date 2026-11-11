import pytest
from fastapi.testclient import TestClient
from opentelemetry.trace import SpanKind, StatusCode

from app import main

ROUTE = "/api/orders/{order_id}"


@pytest.fixture
def client(tmp_path, monkeypatch, telemetry):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


def request_counts(points):
    return {
        (p.attributes["http.route"], p.attributes["http.response.status_code"]): p.value
        for p in points.get("order_lookup.requests", [])
    }


def test_successful_lookup_emits_span_metric_and_log(client, telemetry):
    assert client.get("/api/orders/standard-1001").status_code == 200

    points = telemetry.metrics()
    assert request_counts(points) == {(ROUTE, 200): 1}
    [duration] = points["order_lookup.duration"]
    assert duration.count == 1

    spans = {span.name: span for span in telemetry.spans()}
    server = spans[f"GET {ROUTE}"]
    assert server.kind == SpanKind.SERVER
    assert server.attributes["http.route"] == ROUTE
    assert server.attributes["http.response.status_code"] == 200
    assert server.attributes["order.id"] == "standard-1001"
    assert spans["SELECT orders"].parent.span_id == server.context.span_id

    [log] = [r for r in telemetry.logs() if r.body == "Order lookup succeeded"]
    assert log.attributes["order.id"] == "standard-1001"
    assert log.trace_id == server.context.trace_id


def test_missing_order_is_counted_as_404(client, telemetry):
    assert client.get("/api/orders/missing").status_code == 404

    assert request_counts(telemetry.metrics()) == {(ROUTE, 404): 1}
    [server] = [s for s in telemetry.spans() if s.kind == SpanKind.SERVER]
    assert server.status.status_code != StatusCode.ERROR
    assert any(r.body == "Order not found" for r in telemetry.logs())


def test_unhandled_error_is_counted_as_500(client, telemetry, monkeypatch):
    def broken(_row):
        raise ValueError("boom")

    monkeypatch.setattr(main, "order_detail", broken)
    assert client.get("/api/orders/standard-1001").status_code == 500

    [point] = telemetry.metrics()["order_lookup.requests"]
    assert point.attributes["http.response.status_code"] == 500
    assert point.attributes["error.type"] == "ValueError"
    [server] = [s for s in telemetry.spans() if s.kind == SpanKind.SERVER]
    assert server.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in server.events)
    [log] = [r for r in telemetry.logs() if r.body == "Failed to build order details"]
    assert log.attributes["exception.type"] == "ValueError"


def test_other_routes_are_not_instrumented(client, telemetry):
    client.get("/api/orders")
    client.get("/healthz")
    assert telemetry.spans() == ()
    assert "order_lookup.requests" not in telemetry.metrics()
