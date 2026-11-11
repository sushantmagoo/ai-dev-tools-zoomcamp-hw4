import json
import shlex
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from responder import app as responder_app
from responder.context import Collector

TRACE_ID = "4104a1fe91a9843b233010e4e51d77dc"

# Stand-in for the coding assistant: records its prompt and working directory.
FAKE_AGENT = (
    f"{shlex.quote(sys.executable)} -c "
    + shlex.quote("import json, os, sys; print(json.dumps({'cwd': os.getcwd(), 'prompt': sys.stdin.read()}))")
)


def fake_backends(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/query":
        return httpx.Response(200, json={"data": {"result": [{
            "metric": {"http_route": "/api/orders/{order_id}", "http_response_status_code": "500",
                       "error_type": "ValueError"},
            "value": [0, "3"],
        }]}})
    if path == "/loki/api/v1/query_range":
        return httpx.Response(200, json={"data": {"result": [{
            "stream": {"severity_text": "ERROR", "trace_id": TRACE_ID, "span_id": "abc",
                       "order_id": "express-1002", "exception_type": "ValueError",
                       "exception_stacktrace": "Traceback ...\nValueError: day is out of range for month"},
            "values": [["1794368416479895921", "Failed to build order details"]],
        }]}})
    if path == "/api/search":
        return httpx.Response(200, json={"traces": [{"traceID": TRACE_ID}]})
    if path == f"/api/v2/traces/{TRACE_ID}":
        return httpx.Response(200, json={"trace": {"resourceSpans": [{"scopeSpans": [{"spans": [{
            "name": "GET /api/orders/{order_id}", "spanId": "abc",
            "startTimeUnixNano": "1000000", "endTimeUnixNano": "4000000",
            "status": {"code": "STATUS_CODE_ERROR"},
            "attributes": [{"key": "http.response.status_code", "value": {"intValue": "500"}}],
            "events": [{"name": "exception", "attributes": [
                {"key": "exception.type", "value": {"stringValue": "ValueError"}},
                {"key": "exception.message", "value": {"stringValue": "day is out of range for month"}},
            ]}],
        }]}]}]}})
    return httpx.Response(404)


def test_trace_ids_from_tempo_search_are_zero_padded():
    from responder.context import normalize_trace_id
    assert normalize_trace_id("9ed8e287dbb1b626696002300fe6a34") == "09ed8e287dbb1b626696002300fe6a34"


@pytest.fixture
def responder(tmp_path, monkeypatch):
    monkeypatch.setenv("RESPONDER_INCIDENTS_DIR", str(tmp_path / "incidents"))
    monkeypatch.setenv("RESPONDER_REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESPONDER_AGENT_CMD", FAKE_AGENT)
    with TestClient(responder_app.app) as client:
        state = responder_app.app.state
        state.collector = Collector(state.settings, client=httpx.Client(
            transport=httpx.MockTransport(fake_backends)))
        yield client, state


def post(client, *alerts):
    return client.post("/alerts", json={"status": "firing", "alerts": list(alerts)})


FIRING = {
    "status": "firing",
    "fingerprint": "f00dfeed12345678",
    "startsAt": "2026-11-11T05:39:20Z",
    "labels": {"alertname": "Order lookup 5xx responses", "http_route": "/api/orders/{order_id}"},
    "annotations": {"summary": "5xx responses", "endpoint": "GET /api/orders/{order_id}",
                    "time_window": "5m", "dashboard_url": "http://127.0.0.1:3000/d/order-tracker-lookups"},
}


def test_firing_alert_collects_context_and_starts_agent(responder, tmp_path):
    client, state = responder
    response = post(client, FIRING)
    assert response.status_code == 202
    assert response.json()["results"][0]["action"] == "incident_created"
    state.runner.queue.join()

    [incident] = (tmp_path / "incidents").iterdir()
    context = json.loads((incident / "context.json").read_text())
    assert context["endpoint"] == {"method": "GET", "route": "/api/orders/{order_id}",
                                   "description": "GET /api/orders/{order_id}"}
    assert context["logs"]["data"]["entries"][0]["trace_id"] == TRACE_ID
    assert (incident / "traces" / f"{TRACE_ID}.json").exists()

    summary = (incident / "CONTEXT.md").read_text()
    assert "GET /api/orders/{order_id}" in summary
    assert "day is out of range for month" in summary
    assert "http://127.0.0.1:3000/d/order-tracker-lookups" in summary

    status = json.loads((incident / "status.json").read_text())
    assert status["agent"] == "completed"
    output = json.loads((incident / "agent" / "output.jsonl").read_text())
    assert output["cwd"] == str(tmp_path)
    assert str(incident) in output["prompt"]
    assert "regression test" in output["prompt"]


def test_test_alert_tells_agent_not_to_change_code(responder, tmp_path):
    client, state = responder
    post(client, {"status": "firing", "labels": {"alertname": "ResponderTest", "test": "true"},
                  "annotations": {"summary": "Test notification; no incident to fix"}})
    state.runner.queue.join()
    [incident] = (tmp_path / "incidents").iterdir()
    assert json.loads((incident / "status.json").read_text())["test"] is True
    prompt = (incident / "agent" / "prompt.md").read_text()
    assert "Do not change any code" in prompt
    assert "not specified in the alert" in (incident / "CONTEXT.md").read_text()


def test_repeated_alert_is_deduplicated_until_resolved(responder, tmp_path):
    client, state = responder
    assert post(client, FIRING).json()["results"][0]["action"] == "incident_created"
    assert post(client, FIRING).json()["results"][0]["action"] == "duplicate"
    resolved = post(client, {**FIRING, "status": "resolved"}).json()["results"][0]
    assert resolved["action"] == "resolved"
    state.runner.queue.join()
    [incident] = (tmp_path / "incidents").iterdir()
    events = (incident / "events.jsonl").read_text().splitlines()
    assert [json.loads(e)["status"] for e in events] == ["firing", "resolved"]
    assert post(client, FIRING).json()["results"][0]["action"] == "incident_created"
    state.runner.queue.join()


def test_backend_failures_are_recorded_not_fatal(responder, tmp_path):
    client, state = responder
    state.collector = Collector(state.settings, client=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    post(client, FIRING)
    state.runner.queue.join()
    [incident] = (tmp_path / "incidents").iterdir()
    context = json.loads((incident / "context.json").read_text())
    assert context["logs"]["ok"] is False and "503" in context["logs"]["error"]
    assert json.loads((incident / "status.json").read_text())["agent"] == "completed"


def test_rejects_payload_without_alerts(responder):
    client, _ = responder
    assert client.post("/alerts", json={"foo": 1}).status_code == 422
    assert client.post("/alerts", content="not json",
                       headers={"Content-Type": "application/json"}).status_code == 400
