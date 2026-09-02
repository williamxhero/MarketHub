from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
import subprocess
import sys
import threading
from typing import Protocol
from zoneinfo import ZoneInfo

from quotemux.infra.db.client import query_dataframe
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


class LiveClockUnhealthy(LiveBarUnavailable):
    """The host clock cannot safely label and persist a current Bar."""


class LiveClockHealth(Protocol):
    def assert_healthy(self) -> None: ...


class EnvironmentClockHealth:
    """Consume NTP skew measured by the service supervisor without trusting host timezone."""

    def assert_healthy(self) -> None:
        try:
            skew_seconds = abs(float(os.getenv("MHK_LIVE_CLOCK_SKEW_SECONDS", "0")))
            tolerance_seconds = float(os.getenv("MHK_LIVE_CLOCK_SKEW_TOLERANCE_SECONDS", "2"))
        except ValueError as exc:
            raise LiveClockUnhealthy("clock health configuration is invalid") from exc
        if tolerance_seconds < 0 or skew_seconds > tolerance_seconds:
            raise LiveClockUnhealthy(f"clock skew {skew_seconds:.3f}s exceeds tolerance {tolerance_seconds:.3f}s")


@dataclass(frozen=True)
class CurrentBarSession:
    market_status: str
    active: bool


class CurrentBarSessionResolver(Protocol):
    def resolve(self, effective_now: datetime) -> CurrentBarSession: ...


class FinalizedCurrentBarReader(Protocol):
    def get_latest_finalized(self, request: CurrentBarRequest, market_status: str) -> CurrentStockQuotesQueryResult: ...


class ChinaStockSessionResolver:
    """Resolve China A-share continuous-trading eligibility from the durable calendar."""

    def resolve(self, effective_now: datetime) -> CurrentBarSession:
        local_now = effective_now.astimezone(SHANGHAI)
        calendar = query_dataframe(
            "select is_open from ref.trade_calendar where exchange in ('SSE', 'SHSE') and trade_date=%s::date limit 1",
            (local_now.date().isoformat(),),
        )
        if calendar.empty:
            raise LiveBarUnavailable("trading calendar is unavailable for the requested date")
        if not bool(calendar.iloc[0]["is_open"]):
            return CurrentBarSession(market_status="closed", active=False)
        clock = local_now.timetz().replace(tzinfo=None)
        if (clock.hour, clock.minute) < (9, 30):
            return CurrentBarSession(market_status="preopen", active=False)
        if (clock.hour, clock.minute) < (11, 30):
            return CurrentBarSession(market_status="trading", active=True)
        if (clock.hour, clock.minute) < (13, 0):
            return CurrentBarSession(market_status="recess", active=False)
        if (clock.hour, clock.minute) < (15, 0):
            return CurrentBarSession(market_status="trading", active=True)
        return CurrentBarSession(market_status="closed", active=False)


class PostgresFinalizedCurrentBarReader:
    """Read only canonical final history for non-active current-mode requests."""

    def get_latest_finalized(self, request: CurrentBarRequest, market_status: str) -> CurrentStockQuotesQueryResult:
        frame = query_dataframe(
            """
            select distinct on (bars.code) bars.market, bars.code, bars.bar_time, bars.open, bars.high, bars.low,
                   bars.close, bars.volume, bars.amount
            from fact.stock_bar_1m bars
            where bars.code = any(%s::character(6)[])
              and bars.bar_time < %s::timestamptz
            order by bars.code, bars.bar_time desc
            """,
            (list(request.codes), request.effective_now.astimezone(SHANGHAI)),
        )
        rows = {str(row["code"]).zfill(6): row for _, row in frame.iterrows()}
        items: list[CurrentStockQuoteItem] = []
        errors: list[dict[str, object]] = []
        for code in request.codes:
            row = rows.get(code)
            if row is None:
                errors.append({"code": code, "message": "no finalized eligible 1m Bar in canonical history"})
                continue
            raw_bar_time = row["bar_time"].to_pydatetime()
            interval_start = raw_bar_time.replace(tzinfo=SHANGHAI) if raw_bar_time.tzinfo is None else raw_bar_time.astimezone(SHANGHAI)
            items.append(
                CurrentStockQuoteItem(
                    code=code, trade_time=interval_start.isoformat(), freq="1m",
                    open=float(row["open"]), high=float(row["high"]), low=float(row["low"]), close=float(row["close"]),
                    volume=float(row["volume"]), amount=float(row["amount"]), adjust="none", is_suspended=False, is_st=False,
                    interval_start=interval_start.isoformat(), interval_end=(interval_start + timedelta(minutes=1)).isoformat(),
                    is_final=True, observed_at=interval_start.isoformat(), last_trade_at=interval_start.isoformat(),
                    provider="canonical_history", source_semantics="native", observation_version=f"final:{interval_start.isoformat()}",
                    freshness_ms=0, degraded=False, market_status=market_status,
                )
            )
        if not items:
            detail = str(errors[0]["message"]) if errors else "no finalized current Bar"
            raise LiveBarUnavailable(detail)
        return CurrentStockQuotesQueryResult(
            items=items,
            meta=CurrentStockQuotesMeta(total_rows=len(items), returned_rows=len(items), complete=not errors, truncated=False, effective_now=request.effective_now.isoformat()),
            errors=errors,
        )


class CurrentBarGateway(Protocol):
    def get_current_quotes(self, request: CurrentBarRequest) -> CurrentStockQuotesQueryResult: ...


class _UnavailableCurrentBarGateway:
    def get_current_quotes(self, request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
        raise LiveBarUnavailable("实时行情写穿 worker 尚未就绪")


class QuoteMuxWorkerGateway:
    """Invoke the separately deployed QuoteMux live-ingest command worker."""

    def __init__(self, session_resolver: CurrentBarSessionResolver | None = None, finalized_reader: FinalizedCurrentBarReader | None = None, clock_health: LiveClockHealth | None = None) -> None:
        self._cache: dict[tuple[str, str, str, str, str, str], CurrentStockQuotesQueryResult] = {}
        self._cache_index: dict[tuple[str, str, str, str], tuple[str, str, str, str, str, str]] = {}
        self._inflight: dict[tuple[str, str, str, str], Future[CurrentStockQuotesQueryResult]] = {}
        self._lock = threading.Lock()
        self._session_resolver = session_resolver or ChinaStockSessionResolver()
        self._finalized_reader = finalized_reader or PostgresFinalizedCurrentBarReader()
        self._clock_health = clock_health or EnvironmentClockHealth()

    def get_current_quotes(self, request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
        self._clock_health.assert_healthy()
        session = self._session_resolver.resolve(request.effective_now)
        if not session.active:
            return self._finalized_reader.get_latest_finalized(request, session.market_status)
        if len(request.codes) != 1:
            return self._get_active_batch(request)
        return self._get_active_single(request)

    def _get_active_batch(self, request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
        try:
            configured_workers = int(os.getenv("MHK_LIVE_BATCH_MAX_WORKERS", "6"))
        except ValueError:
            configured_workers = 6
        max_workers = max(4, min(8, configured_workers))
        child_results: dict[str, CurrentStockQuotesQueryResult] = {}
        child_errors: dict[str, dict[str, object]] = {}
        with ThreadPoolExecutor(max_workers=min(max_workers, len(request.codes))) as executor:
            futures = {
                code: executor.submit(
                    self._get_active_single,
                    CurrentBarRequest(codes=(code,), freq=request.freq, count=request.count, adjust=request.adjust, effective_now=request.effective_now),
                )
                for code in request.codes
            }
            for code, future in futures.items():
                try:
                    child_results[code] = future.result()
                except LiveBarUnavailable as exc:
                    child_errors[code] = {"code": code, "message": str(exc)}
        items: list[CurrentStockQuoteItem] = []
        errors: list[dict[str, object]] = []
        diagnostics: list[dict[str, object]] = []
        for code in request.codes:
            result = child_results.get(code)
            if result is None:
                errors.append(child_errors[code])
                continue
            items.extend(result.items)
            errors.extend(result.errors)
            diagnostics.extend(result.diagnostics)
        if not items:
            detail = str(errors[0].get("message", "no current Bar committed")) if errors else "no current Bar committed"
            raise LiveBarUnavailable(detail)
        return CurrentStockQuotesQueryResult(
            items=items,
            meta=CurrentStockQuotesMeta(total_rows=len(items), returned_rows=len(items), complete=not errors, truncated=False, effective_now=request.effective_now.isoformat(), historical_dataset_version=""),
            errors=errors,
            diagnostics=diagnostics,
        )

    def _get_active_single(self, request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
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
    normalized_codes = tuple(require_codes(code, codes))
    if len(normalized_codes) > 20:
        raise ValueError("datetime=now accepts at most 20 unique codes")
    allowlist_text = os.getenv("MHK_LIVE_ALLOWED_CODES", "600519,000001")
    allowlist = set(require_codes("", allowlist_text))
    disallowed = [item for item in normalized_codes if item not in allowlist]
    if disallowed:
        raise ValueError(f"datetime=now code is outside the live allowlist: {','.join(disallowed)}")
    return CurrentBarRequest(
        codes=normalized_codes,
        freq=normalized_freq,
        count=count or 1,
        adjust=normalized_adjust,
        effective_now=(effective_now or datetime.now(tz=SHANGHAI)).astimezone(SHANGHAI),
    )


def get_current_quotes(request: CurrentBarRequest) -> CurrentStockQuotesQueryResult:
    """Delegate current-Bar retrieval to the internal live-ingest boundary."""
    return _GATEWAY.get_current_quotes(request)


def get_current_bar_health() -> dict[str, object]:
    """Operational state for mutable current Bars, intentionally separate from historical publication."""
    try:
        EnvironmentClockHealth().assert_healthy()
        clock: dict[str, object] = {"status": "healthy"}
    except LiveClockUnhealthy as exc:
        clock = {"status": "unhealthy", "detail": str(exc)}
    frame = query_dataframe(
        """
        select count(*) filter (where state='staged')::int as staged_count,
               min(interval_start) filter (where state='staged') as oldest_staged_interval,
               max(selected_at) as last_selected_at,
               count(*) filter (where state='failed')::int as failed_count
        from live.stock_bar_selected
        """
    )
    if frame.empty:
        return {
            "status": "unhealthy", "capabilities": ["1m"], "clock": clock,
            "worker": {"status": "unknown"}, "providers": {"primary": "mootdx", "fallback": "opentdx", "validator": "efinance"},
            "finalizer": {"status": "unknown"}, "detail": "live staging state is unavailable",
        }
    row = frame.iloc[0]
    staged_count = int(row.get("staged_count", 0) or 0)
    failed_count = int(row.get("failed_count", 0) or 0)
    status = "unhealthy" if clock["status"] == "unhealthy" else "warning" if staged_count or failed_count else "healthy"
    return {
        "status": status,
        "capabilities": ["1m"],
        "clock": clock,
        "worker": {"status": "configured", "deadline_seconds": 8},
        "providers": {"primary": "mootdx", "fallback": "opentdx", "validator": "efinance"},
        "last_successful_observation": "" if row.get("last_selected_at") is None else str(row["last_selected_at"]),
        "finalizer": {
            "status": "backlog" if staged_count else "ready",
            "staged_count": staged_count,
            "oldest_overdue_interval": "" if row.get("oldest_staged_interval") is None else str(row["oldest_staged_interval"]),
            "failed_count": failed_count,
        },
    }
