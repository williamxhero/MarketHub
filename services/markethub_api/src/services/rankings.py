from __future__ import annotations

from core.config import DEFAULT_LIMIT
from quotemux import QuoteMux
from quotemux.models import RankingBrokerPickItem, RankingResearchReportItem


_QUOTEMUX = QuoteMux()


def get_research_reports(trade_date: str, start_date: str, end_date: str, limit: int) -> list[RankingResearchReportItem]:
    return _QUOTEMUX.rankings.get_research_reports(trade_date, start_date, end_date, limit or DEFAULT_LIMIT)


def get_broker_monthly_picks(trade_month: str, limit: int) -> list[RankingBrokerPickItem]:
    return _QUOTEMUX.rankings.get_broker_monthly_picks(trade_month, limit or DEFAULT_LIMIT)
