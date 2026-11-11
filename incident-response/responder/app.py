"""Incident responder: receives Grafana alert webhooks on POST /alerts.

For each new firing alert it creates an incident folder, collects the affected
endpoint, metrics, logs and traces, and starts the coding assistant headlessly.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from responder.agent import AgentRunner
from responder.config import Settings
from responder.context import Collector
from responder.incidents import IncidentStore, is_test_alert, save_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("responder")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    settings.incidents_dir.mkdir(parents=True, exist_ok=True)
    store = IncidentStore(settings.incidents_dir, settings.dedup_seconds)
    app.state.settings = settings
    app.state.store = store
    app.state.collector = Collector(settings)
    app.state.runner = AgentRunner(settings, store)
    log.info("Incidents: %s | repo: %s | agent: %s", settings.incidents_dir, settings.repo_dir,
             settings.agent_command[0] if settings.agent_enabled else "disabled")
    yield


app = FastAPI(title="Incident Responder", lifespan=lifespan)


def handle_incident(incident: Path, alert: dict) -> None:
    """Collect context, then hand the incident to the coding assistant."""
    state = app.state
    try:
        save_context(incident, state.collector.collect(alert))
        state.store.set_status(incident, state="context_collected")
    except Exception as exc:  # noqa: BLE001 - still start the assistant with what we have
        log.exception("Context collection failed for %s", incident.name)
        state.store.set_status(incident, state="context_failed", context_error=str(exc))
    state.runner.submit(incident, test=is_test_alert(alert))


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/alerts", status_code=202)
async def receive_alerts(request: Request, background: BackgroundTasks):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Body must be JSON")
    alerts = payload.get("alerts") if isinstance(payload, dict) else None
    if not isinstance(alerts, list) or not alerts:
        raise HTTPException(422, "Expected a Grafana webhook payload with a non-empty 'alerts' list")

    store: IncidentStore = app.state.store
    results = []
    for alert in alerts:
        if not isinstance(alert, dict):
            raise HTTPException(422, "Each alert must be an object")
        alertname = (alert.get("labels") or {}).get("alertname", "alert")
        status = alert.get("status") or payload.get("status") or "firing"
        alert = {**alert, "status": status}
        if status == "resolved":
            incident = store.close(alert)
            results.append({"alertname": alertname, "status": status, "action": "resolved",
                            "incident": incident.name if incident else None})
            continue
        incident, created = store.open_or_append(alert, payload)
        if created:
            background.add_task(handle_incident, incident, alert)
        log.info("Alert %s (%s): %s", alertname, status, "new incident" if created else "duplicate")
        results.append({"alertname": alertname, "status": status,
                        "action": "incident_created" if created else "duplicate",
                        "incident": incident.name})
    return JSONResponse({"received": len(alerts), "results": results}, status_code=202)


@app.get("/incidents")
def list_incidents():
    root: Path = app.state.settings.incidents_dir
    incidents = []
    for path in sorted(root.iterdir(), reverse=True):
        status_file = path / "status.json"
        if path.is_dir() and status_file.exists():
            incidents.append({"incident": path.name, **json.loads(status_file.read_text())})
    return incidents
