"""OpenTelemetry setup and low-cardinality dependency instrumentation."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode, Tracer

from app.config import config

_initialized = False
_meter: Meter | None = None
_tracer: Tracer | None = None
_meter_provider: MeterProvider | None = None
_trace_provider: TracerProvider | None = None
_dependency_calls: Any = None
_dependency_duration: Any = None
_retry_attempts: Any = None
_circuit_state_changes: Any = None
_circuit_rejections: Any = None


@dataclass
class DependencyObservation:
    """Mutable result fields attached to one dependency operation."""

    status: str = "success"
    retry_count: int = 0


def configure_observability(app: Any | None = None) -> None:
    """Configure OTLP traces/metrics and framework instrumentation once."""

    global _initialized, _meter, _meter_provider, _trace_provider, _tracer
    if _initialized:
        return
    _initialized = True

    resource = Resource.create(
        {
            "service.name": config.otel_service_name,
            "service.version": config.app_version,
            "deployment.environment": config.otel_environment,
        }
    )
    trace_provider = TracerProvider(resource=resource)
    metric_readers = []

    if config.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = config.otel_exporter_otlp_endpoint.rstrip("/")
        trace_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=f"{endpoint}/v1/traces",
                    timeout=config.otel_export_timeout_seconds,
                )
            )
        )
        metric_readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=f"{endpoint}/v1/metrics",
                    timeout=config.otel_export_timeout_seconds,
                ),
                export_interval_millis=config.otel_metric_export_interval_seconds * 1000,
            )
        )

    trace.set_tracer_provider(trace_provider)
    meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
    metrics.set_meter_provider(meter_provider)
    _trace_provider = trace_provider
    _meter_provider = meter_provider
    _tracer = trace.get_tracer("autooncall")
    _meter = metrics.get_meter("autooncall")
    _create_instruments()

    if app is not None and config.otel_instrument_fastapi:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls=config.otel_excluded_urls)
        HTTPXClientInstrumentor().instrument()
    logger.info(
        "OpenTelemetry configured: exporter={}, service={}",
        "otlp_http" if config.otel_exporter_otlp_endpoint else "in_process",
        config.otel_service_name,
    )


def _create_instruments() -> None:
    global _circuit_rejections
    global _dependency_calls, _dependency_duration, _retry_attempts, _circuit_state_changes
    if _meter is None:
        return
    _dependency_calls = _meter.create_counter(
        "autooncall.dependency.calls",
        description="External dependency call outcomes",
    )
    _dependency_duration = _meter.create_histogram(
        "autooncall.dependency.duration",
        unit="ms",
        description="External dependency call duration",
    )
    _retry_attempts = _meter.create_counter(
        "autooncall.dependency.retries",
        description="Retry attempts after the initial dependency call",
    )
    _circuit_state_changes = _meter.create_counter(
        "autooncall.circuit_breaker.transitions",
        description="Circuit breaker state transitions",
    )
    _circuit_rejections = _meter.create_counter(
        "autooncall.circuit_breaker.rejections",
        description="Dependency calls rejected while a circuit is open",
    )


@contextmanager
def dependency_operation(
    dependency: str,
    operation: str,
    *,
    attributes: dict[str, str | int | float | bool] | None = None,
) -> Iterator[DependencyObservation]:
    """Create one span and metric observation without high-cardinality labels."""

    if not _initialized:
        configure_observability()
    labels: dict[str, str | int | float | bool] = {
        "dependency.name": dependency,
        "dependency.operation": operation,
    }
    labels.update(attributes or {})
    observation = DependencyObservation()
    started = time.perf_counter()
    tracer = _tracer or trace.get_tracer("autooncall")
    with tracer.start_as_current_span(f"{dependency}.{operation}", attributes=labels) as span:
        try:
            yield observation
        except BaseException as exc:
            if observation.status == "success":
                observation.status = "error"
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            metric_labels = {
                "dependency.name": dependency,
                "dependency.operation": operation,
                "status": observation.status,
            }
            span.set_attribute("autooncall.retry_count", observation.retry_count)
            span.set_attribute("autooncall.duration_ms", duration_ms)
            if _dependency_calls is not None:
                _dependency_calls.add(1, metric_labels)
            if _dependency_duration is not None:
                _dependency_duration.record(duration_ms, metric_labels)
            if _retry_attempts is not None and observation.retry_count:
                _retry_attempts.add(observation.retry_count, metric_labels)
            if _circuit_rejections is not None and observation.status == "circuit_open":
                _circuit_rejections.add(
                    1,
                    {
                        "dependency.name": dependency,
                        "dependency.operation": operation,
                    },
                )


def record_circuit_transition(dependency: str, old_state: str, new_state: str) -> None:
    """Record a low-cardinality circuit breaker transition."""

    if not _initialized:
        configure_observability()
    if _circuit_state_changes is not None:
        _circuit_state_changes.add(
            1,
            {
                "dependency.name": dependency,
                "circuit.old_state": old_state,
                "circuit.new_state": new_state,
            },
        )


def observability_status() -> dict[str, Any]:
    """Return non-secret runtime telemetry configuration for health diagnostics."""

    return {
        "enabled": True,
        "service_name": config.otel_service_name,
        "exporter": "otlp_http" if config.otel_exporter_otlp_endpoint else "in_process",
        "fastapi_instrumented": config.otel_instrument_fastapi,
    }


def shutdown_observability() -> None:
    """Flush and stop configured telemetry providers."""

    if _meter_provider is not None:
        _meter_provider.shutdown()
    if _trace_provider is not None:
        _trace_provider.shutdown()
