import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import BaseModel, Field
from starlette.routing import Match

from app.telemetry import (
    logger,
    order_lookup_duration,
    order_lookup_requests,
    setup_telemetry,
    tracer,
)


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at.replace(day=placed_at.day + 2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_telemetry()
    init_db()
    yield


app = FastAPI(title="Order Tracker", lifespan=lifespan)

ORDER_LOOKUP_ROUTE = "/api/orders/{order_id}"


def is_order_lookup(scope) -> bool:
    for route in app.router.routes:
        if getattr(route, "path", None) == ORDER_LOOKUP_ROUTE:
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return True
    return False


@app.middleware("http")
async def order_lookup_telemetry(request: Request, call_next):
    """Emit a server span plus request count/duration metrics for order lookups.

    Runs outside FastAPI's exception handling, so it sees the final status code:
    404 from HTTPException, and 500 for unhandled errors (which are re-raised).
    """
    if not is_order_lookup(request.scope):
        return await call_next(request)

    method = request.method
    status_code = 500
    error_type = None
    start = time.perf_counter()
    with tracer.start_as_current_span(
        f"{method} {ORDER_LOOKUP_ROUTE}",
        kind=SpanKind.SERVER,
        attributes={
            "http.request.method": method,
            "http.route": ORDER_LOOKUP_ROUTE,
            "url.path": request.url.path,
        },
    ) as span:
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            error_type = type(exc).__qualname__
            raise
        finally:
            attributes = {
                "http.request.method": method,
                "http.route": ORDER_LOOKUP_ROUTE,
                "http.response.status_code": status_code,
            }
            if status_code >= 500:
                attributes["error.type"] = error_type or str(status_code)
                span.set_status(StatusCode.ERROR)
            span.set_attributes(attributes)
            order_lookup_requests.add(1, attributes)
            order_lookup_duration.record(time.perf_counter() - start, attributes)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


def fetch_order_row(order_id: str):
    with connect() as db:
        return db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def load_order(order_id: str):
    row = fetch_order_row(order_id)
    if row is None:
        raise HTTPException(404, "Order not found")
    return order_detail(row)


@app.get(ORDER_LOOKUP_ROUTE)
def get_order(order_id: str):
    trace.get_current_span().set_attribute("order.id", order_id)
    with tracer.start_as_current_span(
        "SELECT orders",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system.name": "sqlite",
            "db.operation.name": "SELECT",
            "db.collection.name": "orders",
        },
    ):
        row = fetch_order_row(order_id)
    if row is None:
        logger.warning("Order not found", extra={"order.id": order_id})
        raise HTTPException(404, "Order not found")
    try:
        order = order_detail(row)
    except Exception:
        logger.exception(
            "Failed to build order details",
            extra={"order.id": order_id, "order.priority": row["priority"]},
        )
        raise
    logger.info(
        "Order lookup succeeded",
        extra={"order.id": order_id, "order.priority": order["priority"],
               "order.status": order["status"]},
    )
    return order


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return load_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return load_order(order_id)
