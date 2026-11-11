"""Install in-memory OpenTelemetry providers before the app starts.

setup_telemetry() skips providers that are already installed, so tests capture
signals in memory instead of printing them to the console.
"""

import pytest
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

span_exporter = InMemorySpanExporter()
log_exporter = InMemoryLogRecordExporter()
metric_reader = InMemoryMetricReader(
    preferred_temporality={
        Counter: AggregationTemporality.DELTA,
        Histogram: AggregationTemporality.DELTA,
    }
)

_tracer_provider = TracerProvider()
_tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
trace.set_tracer_provider(_tracer_provider)
metrics.set_meter_provider(MeterProvider(metric_readers=[metric_reader]))
_logger_provider = LoggerProvider()
_logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
set_logger_provider(_logger_provider)


class Telemetry:
    def spans(self):
        return span_exporter.get_finished_spans()

    def logs(self):
        return [data.log_record for data in log_exporter.get_finished_logs()]

    def metrics(self):
        """Collect once and return {metric name: [data points]} since the last collect."""
        data = metric_reader.get_metrics_data()
        points = {}
        for resource_metrics in data.resource_metrics if data else []:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    points.setdefault(metric.name, []).extend(metric.data.data_points)
        return points


@pytest.fixture
def telemetry():
    span_exporter.clear()
    log_exporter.clear()
    metric_reader.get_metrics_data()  # drain: delta temporality resets counters
    return Telemetry()
