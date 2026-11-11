# Incident responder

A small service that receives Grafana alert webhooks on `POST /alerts` (port 8001). For each new firing alert it:

1. Creates an incident folder under `incidents/`.
2. Saves the alert and collects the evidence needed to understand it: the affected endpoint and time window, request and 5xx counts from Prometheus, warning and error logs from Loki (with stack traces), and the related error traces from Tempo.
3. Writes `CONTEXT.md`, a readable summary of all of that.
4. Starts the coding assistant (Claude Code) in headless mode in the repository root. The assistant investigates, fixes the code, adds a regression test, runs the tests and writes `REPORT.md` into the incident folder. It is not allowed to commit, push, reset or check out, or to use Docker, so a person reviews and deploys the change.

Alerts labelled `test="true"` go through the same pipeline, but the assistant is told not to change any code and only to confirm that the pipeline works.

## Run it

Start the app and the observability stack first (`docker compose up --build -d --wait` in the repository root). Then, from this folder:

```bash
uv run uvicorn responder.app:app --host 127.0.0.1 --port 8001
```

The responder runs on the host, not in Docker, because the assistant needs the `claude` CLI and the repository checkout. Grafana reaches it at `http://host.docker.internal:8001/alerts` (see `observability/grafana/provisioning/alerting/incident-responder.yaml`).

Send a test alert:

```bash
curl -X POST http://localhost:8001/alerts \
  -H 'Content-Type: application/json' \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"ResponderTest","test":"true"},"annotations":{"summary":"Test notification; no incident to fix"}}]}'
```

`GET /incidents` lists incidents and their state (`collecting`, `context_collected`, then agent `queued`, `running`, `completed`, `failed`, `timed_out` or `failed_to_start`). Run the tests with `uv run pytest -q`.

## Incident folder

```
incidents/<UTC time>-<alertname>-<fingerprint>/
  alert.json        alert and webhook fields
  context.json      endpoint, window, metrics, logs, trace search results
  traces/<id>.json  full traces from Tempo
  CONTEXT.md        summary for people and the assistant
  events.jsonl      repeat and resolved notifications for this alert
  status.json       incident and assistant state
  agent/            prompt.md, output.jsonl (stream-json), stderr.log
  REPORT.md         written by the assistant
```

## Behaviour

- **Duplicates:** Grafana re-sends firing alerts. A notification for an alert fingerprint that already has an open incident (within `RESPONDER_DEDUP_SECONDS`, default 1 hour) is appended to `events.jsonl` instead of starting a new assistant run. A resolved notification closes the incident, so the next firing starts a new one.
- **One assistant at a time:** runs are queued so two assistants never edit the working tree at once.
- **Best effort context:** if Prometheus, Loki or Tempo is unavailable, the error is recorded and the assistant still starts with whatever was collected.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESPONDER_AGENT_ENABLED` | `true` | Set to `false` to only collect context |
| `RESPONDER_AGENT_CMD` | `claude -p --output-format stream-json --verbose --permission-mode acceptEdits --allowedTools … --disallowedTools …` | Assistant command; the prompt is passed on stdin |
| `RESPONDER_AGENT_TIMEOUT_SECONDS` | `1800` | Kill the assistant after this long |
| `RESPONDER_REPO_DIR` | repository root | Where the assistant works |
| `RESPONDER_INCIDENTS_DIR` | `incidents/` | Where incident folders go |
| `RESPONDER_DEDUP_SECONDS` | `3600` | Duplicate window per alert fingerprint |
| `RESPONDER_LOOKBACK_MINUTES` | `15` | Evidence window before the alert started |
| `PROMETHEUS_URL`, `LOKI_URL`, `TEMPO_URL` | `http://127.0.0.1:9090`, `:3100`, `:3200` | Backends |
