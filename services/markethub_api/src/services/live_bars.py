from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from routers.stock_quote_models import CurrentStockQuotesQueryResult
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


_GATEWAY: CurrentBarGateway = _UnavailableCurrentBarGateway()


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
