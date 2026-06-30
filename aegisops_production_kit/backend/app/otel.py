"""OpenTelemetry wiring — traces + metrics exported to the OTel Collector.

One tracer is shared across the app; the LangGraph nodes/tools open spans under it.
If the collector is unreachable the exporter retries in the background; it never
blocks request handling.
"""

from __future__ import annotations

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .logging_conf import get_logger
from .settings import Settings

log = get_logger(__name__)

_initialised = False


def setup_otel(settings: Settings) -> None:
    """Initialise tracer + meter providers exporting OTLP/gRPC to the collector."""
    global _initialised
    if _initialised:
        return

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "0.1.0",
            "deployment.environment": settings.app_env,
        }
    )
    endpoint = settings.otel_exporter_otlp_endpoint

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint, insecure=True),
        export_interval_millis=15000,
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    _initialised = True
    log.info("otel.initialised", endpoint=endpoint, service=settings.otel_service_name)


def get_tracer(name: str = "aegisops") -> trace.Tracer:
    return trace.get_tracer(name)


def shutdown_otel() -> None:
    """Flush spans/metrics on graceful shutdown."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
    meter_provider = metrics.get_meter_provider()
    if isinstance(meter_provider, MeterProvider):
        meter_provider.shutdown()
