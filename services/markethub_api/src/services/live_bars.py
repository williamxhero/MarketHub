from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from concurrent.futures import Future
import json
import os
import subprocess
import sys
import threading
from typing import Protocol
from zoneinfo import ZoneInfo

from routers.stock_quote_models import CurrentStockQuoteItem, CurrentStockQuotesMeta, CurrentStockQuotesQueryResult
from services.common import require_adjust, require_codes, require_quote_freq


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class CurrentBarRequest:
    codes: tuple[str, ...]
    freq: str
    count: int
    adjust: str
    effective_now: datetime


class LiveBarUnavailable(RuntimeError):
    """The public API could not reach its internal live-ingest worker."""


class CurrentBarGateway(Protocol):
    def get_current_quotes(self, request: CurrentBarRequest) -> CurrentStockQuotesQueryResult: ...


class _UnavailableCurrentBarGateway:
    def get_current_quotes(self, request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
        raise LiveBarUnavailable("实时行情写穿 worker 尚未就绪")


class QuoteMuxWorkerGateway:
    """Invoke the separately deployed QuoteMux live-ingest command worker."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str, str, str, str], CurrentStockQuotesQueryResult] = {}
        self._cache_index: dict[tuple[str, str, str, str], tuple[str, str, str, str, str, str]] = {}
        self._inflight: dict[tuple[str, str, str, str], Future[CurrentStockQuotesQueryResult]] = {}
        self._lock = threading.Lock()

    def get_current_quotes(self, request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
        if len(request.codes) != 1:
            return self._refresh_worker(request)
        base_key = self._base_key(request)
        cached = self._cached(base_key, request)
        if cached is not None:
            result, age_ms = cached
            if age_ms <= 60_000:
                return self._with_freshness(result, request, age_ms, degraded=False)
        try:
            refreshed = self._singleflight_refresh(base_key, request)
        except LiveBarUnavailable:
            if cached is not None and cached[1] <= 300_000:
                return self._with_freshness(cached[0], request, cached[1], degraded=True)
            raise
        refreshed_age_ms = self._result_age_ms(refreshed, request)
        if refreshed_age_ms > 300_000:
            raise LiveBarUnavailable("live-ingest worker returned an observation older than 300 seconds")
        self._remember(base_key, refreshed)
        return self._with_freshness(refreshed, request, refreshed_age_ms, degraded=False)

    @staticmethod
    def _target_interval(request: CurrentBarRequest) -> datetime:
        return request.effective_now.astimezone(SHANGHAI).replace(second=0, microsecond=0)

    def _base_key(self, request: CurrentBarRequest) -> tuple[str, str, str, str]:
        return (request.codes[0], request.freq, self._target_interval(request).isoformat(), request.adjust)

    def _cached(self, base_key: tuple[str, str, str, str], request: CurrentBarRequest) -> tuple[CurrentStockQuotesQueryResult, int] | None:
        with self._lock:
            identity = self._cache_index.get(base_key)
            result = self._cache.get(identity) if identity is not None else None
        if result is None or len(result.items) != 1:
            return None
        item = result.items[0]
        try:
            observed_at = datetime.fromisoformat(item.observed_at).astimezone(SHANGHAI)
            target = datetime.fromisoformat(base_key[2]).astimezone(SHANGHAI)
        except ValueError:
            return None
        if item.interval_start != base_key[2] or observed_at > target + timedelta(minutes=1):
            return None
        # The request's effective time is the only clock exposed to this
        # boundary, so freshness remains deterministic under test and retries.
        age_ms = max(0, int((request.effective_now.astimezone(SHANGHAI) - observed_at).total_seconds() * 1_000))
        return result, age_ms

    @staticmethod
    def _result_age_ms(result: CurrentStockQuotesQueryResult, request: CurrentBarRequest) -> int:
        try:
            observed_at = datetime.fromisoformat(result.items[0].observed_at).astimezone(SHANGHAI)
        except (IndexError, ValueError) as exc:
            raise LiveBarUnavailable(f"live-ingest worker returned invalid observed_at: {exc}") from exc
        return max(0, int((request.effective_now.astimezone(SHANGHAI) - observed_at).total_seconds() * 1_000))

    def _singleflight_refresh(self, base_key: tuple[str, str, str, str], request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
        with self._lock:
            future = self._inflight.get(base_key)
            owner = future is None
            if future is None:
                future = Future()
                self._inflight[base_key] = future
        if not owner:
            try:
                return future.result(timeout=8)
            except Exception as exc:
                raise LiveBarUnavailable(f"live-ingest concurrent refresh failed: {exc}") from exc
        try:
            result = self._refresh_worker(request)
            future.set_result(result)
            return result
        except Exception as exc:
            future.set_exception(exc)
            if isinstance(exc, LiveBarUnavailable):
                raise
            raise LiveBarUnavailable(str(exc)) from exc
        finally:
            with self._lock:
                self._inflight.pop(base_key, None)

    def _remember(self, base_key: tuple[str, str, str, str], result: CurrentStockQuotesQueryResult) -> None:
        item = result.items[0]
        identity = (*base_key, item.provider, item.observation_version)
        with self._lock:
            previous = self._cache_index.get(base_key)
            if previous is not None and previous != identity:
                self._cache.pop(previous, None)
            self._cache[identity] = result
            self._cache_index[base_key] = identity

    @staticmethod
    def _with_freshness(result: CurrentStockQuotesQueryResult, request: CurrentBarRequest, age_ms: int, *, degraded: bool) -> CurrentStockQuotesQueryResult:
        return CurrentStockQuotesQueryResult(
            items=[item.model_copy(update={"freshness_ms": age_ms, "degraded": degraded}) for item in result.items],
            meta=result.meta.model_copy(update={"effective_now": request.effective_now.isoformat()}),
            errors=result.errors,
            diagnostics=result.diagnostics,
        )

    def _refresh_worker(self, request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
        payload = json.dumps(
            {"codes": list(request.codes), "effective_now": request.effective_now.isoformat()},
            ensure_ascii=False,
        )
        executable = os.getenv("MHK_LIVE_INGEST_PYTHON", sys.executable)
        try:
            completed = subprocess.run(
                [executable, "-m", "quotemux.live_bars_worker"],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LiveBarUnavailable(f"live-ingest worker unavailable: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-1200:]
            raise LiveBarUnavailable(f"live-ingest worker failed: {detail or completed.returncode}")
        try:
            result = json.loads(completed.stdout)
            raw_items = result.get("items", [])
            raw_errors = result.get("errors", [])
            raw_diagnostics = result.get("diagnostics", [])
            if not isinstance(raw_items, list) or not isinstance(raw_errors, list) or not isinstance(raw_diagnostics, list):
                raise ValueError("worker response has invalid items/errors/diagnostics")
            items = [CurrentStockQuoteItem.model_validate(item) for item in raw_items]
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise LiveBarUnavailable(f"live-ingest worker returned invalid response: {exc}") from exc
        if not items:
            detail = str(raw_errors[0].get("message", "no current Bar committed")) if raw_errors and isinstance(raw_errors[0], dict) else "no current Bar committed"
            raise LiveBarUnavailable(detail)
        target = self._target_interval(request).isoformat()
        if any(item.interval_start != target for item in items):
            raise LiveBarUnavailable("live-ingest worker returned a non-target interval")
        return CurrentStockQuotesQueryResult(
            items=items,
            meta=CurrentStockQuotesMeta(
                total_rows=len(items), returned_rows=len(items), complete=not raw_errors, truncated=False,
                effective_now=request.effective_now.isoformat(), historical_dataset_version="",
            ),
            errors=[dict(item) for item in raw_errors if isinstance(item, dict)],
            diagnostics=[dict(item) for item in raw_diagnostics if isinstance(item, dict)],
        )


_GATEWAY: CurrentBarGateway = QuoteMuxWorkerGateway()


def build_current_bar_request(
    *,
    code: str,
    codes: str,
    freq: str,
    count: int | None,
    adjust: str,
    trade_date: str,
    start_date: str,
    end_date: str,
    start_time: str,
    end_time: str,
    effective_now: datetime | None = None,
) -> CurrentBarRequest:
    if any((trade_date, start_date, end_date, start_time, end_time)):
        raise ValueError("datetime=now 不能与交易日期或时间范围参数组合")
    normalized_freq = require_quote_freq(freq)
    if normalized_freq != "1m":
        raise ValueError("datetime=now 当前仅支持 freq=1m")
    normalized_adjust = require_adjust(adjust)
    if normalized_adjust != "none":
        raise ValueError("datetime=now 当前仅支持 adjust=none")
    if count not in {None, 1}:
        raise ValueError("datetime=now 当前仅支持 count=1")
    return CurrentBarRequest(
        codes=tuple(require_codes(code, codes)),
        freq=normalized_freq,
        count=count or 1,
        adjust=normalized_adjust,
        effective_now=(effective_now or datetime.now(tz=SHANGHAI)).astimezone(SHANGHAI),
    )


def get_current_quotes(request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
    """Delegate current-Bar retrieval to the internal live-ingest boundary."""
    return _GATEWAY.get_current_quotes(request)
