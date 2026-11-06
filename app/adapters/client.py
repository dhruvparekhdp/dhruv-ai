"""Base HTTP client shared by every external integration.

Guarantees:
  * credential comes from a secret source and is never placed in a log line
  * connect and read timeouts are bounded
  * GET is retried a bounded number of times with jitter; other methods are not
    (a retried POST can double-apply a side effect, including uploading a file twice)
  * each adapter has its own circuit breaker
  * every request carries X-Correlation-ID
  * the body is accepted only when it matches the envelope schema
  * data_as_of older than this adapter's threshold is stale even when the
    engine says it is fresh
  * timeout, 5xx, and an open circuit return `unavailable` and do not raise
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping

import httpx

from app.adapters.circuit import CircuitBreaker
from app.adapters.envelope import SchemaCheck, parse_timestamp, validate_envelope
from app.adapters.models import AdapterResponse, Outcome
from app.adapters.secrets import EnvSecretSource, SecretSource

log = logging.getLogger("jarvis.adapters")

CORRELATION_HEADER = "X-Correlation-ID"

Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdapterClient:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        stale_after: timedelta,
        auth_env: str = "",
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        connect_timeout: float = 3.0,
        read_timeout: float = 10.0,
        max_retries: int = 2,
        retry_base_seconds: float = 0.05,
        retry_cap_seconds: float = 2.0,
        failure_threshold: int = 3,
        recovery_seconds: float = 30.0,
        schema: SchemaCheck | None = None,
        secrets: SecretSource | None = None,
        sleeper: Sleeper | None = None,
        rng: random.Random | None = None,
        now: Clock | None = None,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        if not name or not name.strip():
            raise ValueError("adapter name is required")
        if stale_after.total_seconds() < 0:
            raise ValueError("stale_after must be >= 0")
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.name = name.strip()
        self.base_url = base_url.rstrip("/")
        self.stale_after = stale_after
        self.auth_env = auth_env
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.retry_cap_seconds = retry_cap_seconds
        self.schema = schema
        self._secrets = secrets or EnvSecretSource()
        self._sleeper = sleeper
        self._rng = rng or random.Random()
        self._now = now or _utcnow
        self.circuit = circuit or CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_seconds=recovery_seconds,
        )
        self._last: AdapterResponse | None = None

    async def get(self, path: str, **kwargs: Any) -> AdapterResponse:
        return await self.request("GET", path, **kwargs)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        files: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> AdapterResponse:
        """Perform one logical call. Does not raise for upstream failures.

        `files` is a multipart body (httpx's shape). It is still one attempt
        when the method is not GET. The file bytes are not logged.
        """
        method = method.upper()
        correlation_id = correlation_id or str(uuid.uuid4())
        try:
            result = await self._request(
                method, path, params=params, json_body=json_body, files=files,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - agents must not see this
            # Class name only. str(exc) from httpx can include request headers.
            log.error(
                "adapter %s failed closed (%s) correlation=%s",
                self.name, exc.__class__.__name__, correlation_id,
            )
            result = self._result(
                outcome=Outcome.UNAVAILABLE,
                correlation_id=correlation_id,
                reason="internal",
                detail="adapter call failed before a response was produced",
            )
        self._last = result
        log.info(
            "adapter %s %s %s -> %s reason=%s http=%s correlation=%s",
            self.name, method, path, result.outcome.value, result.reason or "-",
            result.http_status if result.http_status is not None else "-",
            correlation_id,
        )
        return result

    def health(self) -> dict[str, Any]:
        """Last outcome and circuit state. Does not call the upstream."""
        last = self._last
        return {
            "name": self.name,
            "circuit": self.circuit.state.value,
            "last_outcome": last.outcome.value if last else None,
            "last_reason": last.reason if last else "",
            "stale": last.stale if last else None,
            "data_as_of": last.data_as_of if last else None,
            "checked_at": last.checked_at if last else None,
            "correlation_id": last.correlation_id if last else None,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json_body: Any,
        files: Mapping[str, Any] | None,
        correlation_id: str,
    ) -> AdapterResponse:
        # Credential check happens before the breaker so a missing secret does
        # not consume the single half-open probe.
        headers = {
            CORRELATION_HEADER: correlation_id,
            "Accept": "application/json",
        }
        if self.auth_env:
            secret = self._secrets.get(self.auth_env)
            if not secret:
                log.warning(
                    "adapter %s has no credential in %s", self.name, self.auth_env
                )
                return self._result(
                    outcome=Outcome.UNAVAILABLE,
                    correlation_id=correlation_id,
                    reason="missing_credential",
                    detail=f"credential {self.auth_env} is not set",
                )
            token = f"{self.auth_scheme} {secret}".strip() if self.auth_scheme else secret
            headers[self.auth_header] = token

        if not self.circuit.allow():
            return self._result(
                outcome=Outcome.UNAVAILABLE,
                correlation_id=correlation_id,
                reason="circuit_open",
                detail=f"{self.name} circuit is open",
            )

        attempts = 1 + (self.max_retries if method == "GET" else 0)
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        failure: tuple[str, str, int | None] | None = None

        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout) as http:
            for attempt in range(attempts):
                got = await self._attempt(
                    http, method, path, params=params, json_body=json_body,
                    files=files, headers=headers, correlation_id=correlation_id,
                )
                if isinstance(got, AdapterResponse):
                    return got
                failure = got
                reason = failure[0]
                # Only GET is retried, and only when the upstream did not answer
                # or answered 5xx. Retrying a POST can apply it twice.
                retryable = method == "GET" and reason in {"timeout", "http_5xx", "network"}
                if retryable and attempt + 1 < attempts:
                    await self._sleep(self._jitter(attempt))
                    continue
                break

        assert failure is not None
        reason, detail, status = failure
        if reason in {"timeout", "http_5xx", "network"}:
            self.circuit.record_failure()
        else:
            # 3xx/4xx still prove the upstream is reachable.
            self.circuit.record_success()
        return self._result(
            outcome=Outcome.UNAVAILABLE,
            correlation_id=correlation_id,
            reason=reason,
            detail=detail,
            http_status=status,
        )

    async def _attempt(
        self,
        http: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json_body: Any,
        files: Mapping[str, Any] | None,
        headers: dict[str, str],
        correlation_id: str,
    ) -> AdapterResponse | tuple[str, str, int | None]:
        """Return a response, or a failure tuple the caller may retry."""
        request_kwargs: dict[str, Any] = {"headers": headers}
        if params is not None:
            request_kwargs["params"] = dict(params)
        if files is not None:
            # Multipart sets its own Content-Type. A JSON body would replace
            # the file, so the two are not combined.
            request_kwargs["files"] = dict(files)
        elif json_body is not None:
            request_kwargs["json"] = json_body
        try:
            response = await http.request(method, path, **request_kwargs)
        except httpx.TimeoutException:
            return ("timeout", f"{self.name} timed out", None)
        except httpx.HTTPError:
            return ("network", f"{self.name} could not be reached", None)

        status = response.status_code
        if status >= 500:
            return ("http_5xx", f"{self.name} returned HTTP {status}", status)
        if status >= 400:
            return ("http_4xx", f"{self.name} returned HTTP {status}", status)
        if status >= 300:
            return ("http_3xx", f"{self.name} returned HTTP {status}", status)

        try:
            payload = response.json()
        except ValueError:
            self.circuit.record_success()
            return self._result(
                outcome=Outcome.INVALID,
                correlation_id=correlation_id,
                reason="invalid_schema",
                detail="response was not JSON",
                http_status=status,
            )

        problem = validate_envelope(payload)
        if problem is None and self.schema is not None:
            try:
                problem = self.schema(payload)
            except Exception:  # noqa: BLE001 - a schema bug must not reach agents
                problem = "schema check failed"
        if problem is not None:
            self.circuit.record_success()
            return self._result(
                outcome=Outcome.INVALID,
                correlation_id=correlation_id,
                reason="invalid_schema",
                detail=problem,
                http_status=status,
            )

        self.circuit.record_success()
        return self._interpret(payload, correlation_id, status)

    def _interpret(self, payload: Mapping[str, Any], correlation_id: str, status: int) -> AdapterResponse:
        as_of_raw = payload["data_as_of"]
        as_of = parse_timestamp(as_of_raw)
        assert as_of is not None  # validate_envelope already checked
        age = self._now() - as_of
        # Age wins over the engine. A feed that stamps stale:false on hour-old
        # data is still stale, otherwise agents act on a cache.
        engine_says_stale = payload.get("stale") is True
        is_stale = engine_says_stale or age > self.stale_after
        return self._result(
            outcome=Outcome.STALE if is_stale else Outcome.OK,
            correlation_id=correlation_id,
            data=payload["data"],
            data_as_of=as_of.isoformat(),
            stale=is_stale,
            reason="stale_data" if is_stale else "",
            detail="data_as_of is older than the adapter threshold" if is_stale and not engine_says_stale else "",
            http_status=status,
        )

    def _jitter(self, attempt: int) -> float:
        # Full jitter. Equal delays make every replica retry in lockstep.
        ceiling = min(self.retry_cap_seconds, self.retry_base_seconds * (2 ** attempt))
        if ceiling <= 0:
            return 0.0
        return self._rng.uniform(0, ceiling)

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._sleeper is not None:
            await self._sleeper(seconds)
            return
        import asyncio

        await asyncio.sleep(seconds)

    def _result(
        self,
        *,
        outcome: Outcome,
        correlation_id: str,
        data: Any = None,
        data_as_of: str | None = None,
        stale: bool = False,
        reason: str = "",
        detail: str = "",
        http_status: int | None = None,
    ) -> AdapterResponse:
        return AdapterResponse(
            adapter=self.name,
            outcome=outcome,
            correlation_id=correlation_id,
            data=data,
            data_as_of=data_as_of,
            stale=stale,
            reason=reason,
            detail=detail,
            http_status=http_status,
            checked_at=self._now().isoformat(),
        )
