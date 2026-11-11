# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Observability

`docker compose up --build -d --wait` also starts a local observability stack. The app sends traces, metrics and logs over OTLP to the OpenTelemetry Collector, which forwards them:

| Signal | Backend | URL |
| --- | --- | --- |
| Dashboards | Grafana | <http://127.0.0.1:3000> (no login) |
| Metrics | Prometheus | <http://127.0.0.1:9090> |
| Logs | Loki | <http://127.0.0.1:3100> |
| Traces | Tempo | <http://127.0.0.1:3200> |

The **Order Tracker - Order Lookups** dashboard (Grafana's home page) shows request counts, 4xx/5xx errors, error rate, latency, and warning/error logs for `GET /api/orders/{id}`. In a log line's details, the `trace_id` link opens the trace in Tempo.

Telemetry for order lookups:

- Metrics: `order_lookup_requests_total` and `order_lookup_duration_seconds`, labelled with `http_route`, `http_response_status_code`, `http_request_method`, and `error_type` for 5xx responses.
- Traces: a server span per lookup with a child span for the SQLite query.
- Logs: lookup success, not found and failure messages, correlated with the trace.

The **Order lookup 5xx responses** alert (Grafana → Alerting → Alert rules) fires when any lookup returns a 5xx in the last 5 minutes, after a 1 minute pending period. Its annotations include the endpoint, the time window and a dashboard link. Lookups with no 5xx evaluate to 0 (Normal), and no data at all is treated as Normal. No contact point is configured yet, so check the alert's state in Grafana.

Configuration lives in `observability/`. To also print telemetry to `docker compose logs app`, start with `OTEL_CONSOLE_EXPORT=true`. When the app runs outside Compose without `OTEL_EXPORTER_OTLP_ENDPOINT`, telemetry goes to the console only. Ports can be changed with `GRAFANA_PORT`, `PROMETHEUS_PORT`, `LOKI_PORT`, `TEMPO_PORT`, `OTEL_GRPC_PORT` and `OTEL_HTTP_PORT`.

Grafana has anonymous admin access for local use only; all ports bind to `127.0.0.1`.
