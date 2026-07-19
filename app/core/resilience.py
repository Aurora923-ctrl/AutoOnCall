"""Shared timeout, retry-budget, and circuit-breaker primitives."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from typing import TypeVar

from app.core.observability import dependency_operation, record_circuit_transition

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised when a dependency circuit is open."""


@dataclass
class CircuitBreaker:
    """Small process-local breaker that protects a dependency instance."""

    name: str
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self._failures = 0
        self._opened_at = 0.0
        self._state = "closed"
        self._half_open_probe_active = False
        self._lock = Lock()

    @property
    def state(self) -> str:
        with self._lock:
            self._advance_recovery_state()
            return self._state

    def try_acquire_request(self) -> bool:
        """Allow normal calls and reserve at most one half-open probe."""

        with self._lock:
            self._advance_recovery_state()
            if self._state == "open":
                return False
            if self._state == "half_open":
                if self._half_open_probe_active:
                    return False
                self._half_open_probe_active = True
            return True

    def allow_request(self) -> bool:
        """Return whether a request could proceed without reserving a probe."""

        with self._lock:
            self._advance_recovery_state()
            return self._state == "closed" or (
                self._state == "half_open" and not self._half_open_probe_active
            )

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._half_open_probe_active = False
            if self._state != "closed":
                self._transition("closed")

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._half_open_probe_active = False
            if self._state == "half_open" or self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
                if self._state != "open":
                    self._transition("open")

    def release_request(self) -> None:
        """Release a reserved half-open probe without changing circuit health."""

        with self._lock:
            self._half_open_probe_active = False

    def _advance_recovery_state(self) -> None:
        if (
            self._state == "open"
            and time.monotonic() - self._opened_at >= self.recovery_timeout_seconds
        ):
            self._transition("half_open")

    def _transition(self, new_state: str) -> None:
        old_state = self._state
        self._state = new_state
        record_circuit_transition(self.name, old_state, new_state)


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = Lock()
_sync_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dependency-call")
_sync_call_slots = BoundedSemaphore(8)


def get_circuit_breaker(
    dependency: str,
    *,
    failure_threshold: int,
    recovery_timeout_seconds: float,
) -> CircuitBreaker:
    """Return the shared breaker for one low-cardinality dependency name."""

    with _breakers_lock:
        breaker = _breakers.get(dependency)
        if breaker is None:
            breaker = CircuitBreaker(
                dependency,
                failure_threshold=failure_threshold,
                recovery_timeout_seconds=recovery_timeout_seconds,
            )
            _breakers[dependency] = breaker
        return breaker


async def call_with_resilience(
    dependency: str,
    operation: str,
    call: Callable[[], Awaitable[T]],
    *,
    timeout_seconds: float,
    max_attempts: int,
    retry_delay_seconds: float,
    is_retryable: Callable[[Exception], bool],
    failure_threshold: int,
    recovery_timeout_seconds: float,
) -> T:
    """Execute an async dependency call within one total retry budget."""

    _validate_resilience_policy(timeout_seconds, max_attempts, retry_delay_seconds)
    breaker = get_circuit_breaker(
        dependency,
        failure_threshold=failure_threshold,
        recovery_timeout_seconds=recovery_timeout_seconds,
    )
    if not breaker.try_acquire_request():
        with dependency_operation(dependency, operation) as observation:
            observation.status = "circuit_open"
            raise CircuitOpenError(f"{dependency} circuit is open")

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    with dependency_operation(dependency, operation) as observation:
        for attempt in range(1, max_attempts + 1):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                breaker.record_failure()
                observation.status = "timeout"
                raise TimeoutError(f"{dependency} retry budget exhausted")
            try:
                result = await asyncio.wait_for(call(), timeout=remaining)
                breaker.record_success()
                return result
            except asyncio.CancelledError:
                breaker.release_request()
                raise
            except Exception as exc:
                retryable = is_retryable(exc)
                if attempt >= max_attempts or not retryable:
                    if retryable:
                        breaker.record_failure()
                    else:
                        breaker.record_success()
                    observation.status = "timeout" if isinstance(exc, TimeoutError) else "error"
                    raise
                observation.retry_count += 1
                delay = min(retry_delay_seconds * (2 ** (attempt - 1)), remaining)
                if delay > 0:
                    await asyncio.sleep(delay)
        raise RuntimeError(f"{dependency} call did not complete")


def call_sync_with_resilience(
    dependency: str,
    operation: str,
    call: Callable[[], T],
    *,
    timeout_seconds: float,
    max_attempts: int,
    retry_delay_seconds: float,
    is_retryable: Callable[[Exception], bool],
    failure_threshold: int,
    recovery_timeout_seconds: float,
) -> T:
    """Execute a blocking dependency call with the same total-budget policy."""

    _validate_resilience_policy(timeout_seconds, max_attempts, retry_delay_seconds)
    breaker = get_circuit_breaker(
        dependency,
        failure_threshold=failure_threshold,
        recovery_timeout_seconds=recovery_timeout_seconds,
    )
    if not breaker.try_acquire_request():
        with dependency_operation(dependency, operation) as observation:
            observation.status = "circuit_open"
            raise CircuitOpenError(f"{dependency} circuit is open")

    deadline = time.monotonic() + timeout_seconds
    with dependency_operation(dependency, operation) as observation:
        for attempt in range(1, max_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                breaker.record_failure()
                observation.status = "timeout"
                raise TimeoutError(f"{dependency} retry budget exhausted")
            if not _sync_call_slots.acquire(timeout=remaining):
                breaker.record_failure()
                observation.status = "timeout"
                raise TimeoutError(f"{dependency} worker capacity exhausted")
            try:
                future = _sync_executor.submit(_run_sync_call, call)
            except BaseException:
                _sync_call_slots.release()
                breaker.release_request()
                raise
            try:
                result = future.result(timeout=remaining)
                breaker.record_success()
                return result
            except FutureTimeoutError as exc:
                future.cancel()
                breaker.record_failure()
                observation.status = "timeout"
                raise TimeoutError(f"{dependency} call timed out") from exc
            except Exception as exc:
                error = exc

            retryable = is_retryable(error)
            if attempt >= max_attempts or not retryable:
                if retryable:
                    breaker.record_failure()
                else:
                    breaker.record_success()
                observation.status = "timeout" if isinstance(error, TimeoutError) else "error"
                raise error
            observation.retry_count += 1
            remaining = deadline - time.monotonic()
            delay = min(retry_delay_seconds * (2 ** (attempt - 1)), max(remaining, 0))
            if delay > 0:
                time.sleep(delay)
        raise RuntimeError(f"{dependency} call did not complete")


def _run_sync_call(call: Callable[[], T]) -> T:
    try:
        return call()
    finally:
        _sync_call_slots.release()


def _validate_resilience_policy(
    timeout_seconds: float,
    max_attempts: int,
    retry_delay_seconds: float,
) -> None:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative")
