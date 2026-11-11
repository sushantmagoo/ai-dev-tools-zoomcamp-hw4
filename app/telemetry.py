"""OpenTelemetry setup for the order tracker.

When OTEL_EXPORTER_OTLP_ENDPOINT is set (as in compose.yaml), traces, metrics
and logs are sent over OTLP/HTTP to the OpenTelemetry Collector. Otherwise they
are printed to the console (stdout), which is handy when running without the
observability stack. Set OTEL_CONSOLE_EXPORT=true to print them in both cases.
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger_provider, set_logger_provider
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "order-tracker")
USE_OTLP = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
USE_CONSOLE = not USE_OTLP or os.getenv("OTEL_CONSOLE_EXPORT", "").lower() == "true"
SCOPE = "order_tracker"

logger = logging.getLogger(SCOPE)

# Instruments are created from the global API up front. They are proxies until
# setup_telemetry() installs real providers, so importing this module is cheap.
tracer = trace.get_tracer(SCOPE)
meter = metrics.get_meter(SCOPE)

order_lookup_requests = meter.create_counter(
    "order_lookup.requests",
    unit="{request}",
    description="Order lookup requests by route and HTTP status code",
)
order_lookup_duration = meter.create_histogram(
    "order_lookup.duration",
    unit="s",
    description="Order lookup request duration by route and HTTP status code",
    # Seconds-scale buckets (the SDK default buckets assume milliseconds).
    explicit_bucket_boundaries_advisory=[
        0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10,
    ],
)


def _exporters():
    """Return (span, metric, log) exporter lists based on the environment."""
    spans, metrics_, logs = [], [], []
    if USE_OTLP:
        # Endpoint, headers and timeouts come from the standard OTEL_EXPORTER_OTLP_* env vars.
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        spans.append(OTLPSpanExporter())
        metrics_.append(OTLPMetricExporter())
        logs.append(OTLPLogExporter())
    if USE_CONSOLE:
        spans.append(ConsoleSpanExporter())
        metrics_.append(ConsoleMetricExporter())
        logs.append(ConsoleLogRecordExporter())
    return spans, metrics_, logs


def setup_telemetry() -> None:
    """Install SDK providers with OTLP and/or console exporters, unless SDK providers already exist.

    Skipping when providers are already installed keeps this idempotent and lets
    tests install in-memory exporters before the app starts.
    """
    if (
        isinstance(trace.get_tracer_provider(), TracerProvider)
        and isinstance(metrics.get_meter_provider(), MeterProvider)
        and isinstance(get_logger_provider(), LoggerProvider)
    ):
        _attach_log_handler()
        return

    resource = Resource.create({"service.name": SERVICE_NAME})
    span_exporters, metric_exporters, log_exporters = _exporters()

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        tracer_provider = TracerProvider(resource=resource)
        for exporter in span_exporters:
            tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(tracer_provider)

    if not isinstance(metrics.get_meter_provider(), MeterProvider):
        # Export interval honours OTEL_METRIC_EXPORT_INTERVAL (ms, default 60000).
        readers = [PeriodicExportingMetricReader(exporter) for exporter in metric_exporters]
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    if not isinstance(get_logger_provider(), LoggerProvider):
        logger_provider = LoggerProvider(resource=resource)
        for exporter in log_exporters:
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
        set_logger_provider(logger_provider)

    _attach_log_handler()


def _attach_log_handler() -> None:
    if not any(isinstance(h, LoggingHandler) for h in logger.handlers):
        logger.addHandler(LoggingHandler(logger_provider=get_logger_provider()))
    logger.setLevel(logging.INFO)
