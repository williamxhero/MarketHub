from __future__ import annotations

from core.config import DEFAULT_LIMIT
from quotemux import NextTradingDaysRequest, PreviousTradingDaysRequest, QuoteMux, TradingCalendarRequest, YearlyTradingCalendarRequest
from quotemux.models import AuctionItem, BlockTradeItem, ConnectActiveTop10Item, ConnectCapitalFlowItem, ConnectQuotaItem, DragonTigerInstitutionItem, DragonTigerItem, HotMoneyDetailItem, HotMoneyProfileItem, MarketCapitalFlowItem, TradingCalendarItem, TradingSessionItem
from services.common import ensure_limit, require_exchange


_QUOTEMUX = QuoteMux()


def get_main_capital_flow(trade_date: str, start_date: str, end_date: str) -> list[MarketCapitalFlowItem]:
    return _QUOTEMUX.markets.get_main_capital_flow(trade_date, start_date, end_date)


def get_trading_calendar(exchange: str, start_date: str, end_date: str, is_open: bool | None) -> list[TradingCalendarItem]:
    return _QUOTEMUX.markets.get_trading_calendar(
        TradingCalendarRequest(
            exchange=require_exchange(exchange),
            start_date=start_date,
            end_date=end_date,
            is_open=is_open,
        )
    )


def get_previous_trading_days(exchange: str, trade_date: str, n: int) -> list[TradingCalendarItem]:
    return _QUOTEMUX.markets.get_previous_trading_days(PreviousTradingDaysRequest(exchange=require_exchange(exchange), trade_date=trade_date, n=n))


def get_next_trading_days(exchange: str, trade_date: str, n: int) -> list[TradingCalendarItem]:
    return _QUOTEMUX.markets.get_next_trading_days(NextTradingDaysRequest(exchange=require_exchange(exchange), trade_date=trade_date, n=n))


def get_yearly_trading_calendar(exchange: str, start_year: int, end_year: int) -> list[TradingCalendarItem]:
    return _QUOTEMUX.markets.get_yearly_trading_calendar(YearlyTradingCalendarRequest(exchange=require_exchange(exchange), start_year=start_year, end_year=end_year))


def get_connect_capital_flow(trade_date: str, start_date: str, end_date: str) -> list[ConnectCapitalFlowItem]:
    return _QUOTEMUX.markets.get_connect_capital_flow(trade_date, start_date, end_date)


def get_connect_quotas(trade_date: str, start_date: str, end_date: str, market_type: str) -> list[ConnectQuotaItem]:
    return _QUOTEMUX.markets.get_connect_quotas(trade_date, start_date, end_date, market_type)


def get_connect_active_top10(trade_date: str, start_date: str, end_date: str, market_type: str, limit: int) -> list[ConnectActiveTop10Item]:
    return _QUOTEMUX.markets.get_connect_active_top10(trade_date, start_date, end_date, market_type, ensure_limit(limit or DEFAULT_LIMIT))


def get_block_trades(trade_date: str, start_date: str, end_date: str, code: str, limit: int) -> list[BlockTradeItem]:
    return _QUOTEMUX.markets.get_block_trades(trade_date, start_date, end_date, code, ensure_limit(limit or DEFAULT_LIMIT))


def get_dragon_tiger(trade_date: str, start_date: str, end_date: str, code: str, limit: int) -> list[DragonTigerItem]:
    return _QUOTEMUX.markets.get_dragon_tiger(trade_date, start_date, end_date, code, ensure_limit(limit or DEFAULT_LIMIT))


def get_dragon_tiger_institutions(trade_date: str, start_date: str, end_date: str, code: str, limit: int) -> list[DragonTigerInstitutionItem]:
    return _QUOTEMUX.markets.get_dragon_tiger_institutions(trade_date, start_date, end_date, code, ensure_limit(limit or DEFAULT_LIMIT))


def get_hot_money(name: str, tag: str, limit: int, offset: int) -> list[HotMoneyProfileItem]:
    return _QUOTEMUX.markets.get_hot_money(name, tag, limit or DEFAULT_LIMIT, offset)


def get_hot_money_details(trade_date: str, start_date: str, end_date: str, name: str, limit: int, offset: int) -> list[HotMoneyDetailItem]:
    return _QUOTEMUX.markets.get_hot_money_details(trade_date, start_date, end_date, name, limit or DEFAULT_LIMIT, offset)


def get_open_auctions(codes: str, trade_date: str, instrument_type: str) -> list[AuctionItem]:
    del instrument_type
    return _QUOTEMUX.markets.get_open_auctions(codes, trade_date)


def get_sessions(codes: str) -> list[TradingSessionItem]:
    return _QUOTEMUX.markets.get_sessions(codes)
