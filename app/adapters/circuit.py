"""Per-adapter circuit breaker.

One feed timing out must not keep being called, and must not take the others
down with it. The breaker is therefore owned by the adapter, not global.

States:
  closed     calls go through
  open       calls are refused until the recovery window elapses
  half_open  a single probe is allowed; success closes, failure re-opens
"""

from __future__ import annotations

import threading
import time
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_seconds: float = 30.0,
        clock=time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if recovery_seconds < 0:
            raise ValueError("recovery_seconds must be >= 0")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_inflight = False
        # Called from async code without awaiting while held, so a thread lock
        # is enough and does not bind the breaker to one event loop.
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        """Current state. Reading it does not open a probe window."""
        with self._lock:
            return self._state

    def allow(self) -> bool:
        """True when this call may hit the upstream."""
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN:
                return False
            if self._state is CircuitState.HALF_OPEN:
                if self._probe_inflight:
                    return False
                self._probe_inflight = True
                return True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._probe_inflight = False
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Count one failed *call* (after retries), not each attempt."""
        with self._lock:
            self._probe_inflight = False
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()

    def _maybe_half_open(self) -> None:
        """Caller holds the lock."""
        if self._state is not CircuitState.OPEN:
            return
        if self._clock() - self._opened_at >= self.recovery_seconds:
            self._state = CircuitState.HALF_OPEN
            self._probe_inflight = False
