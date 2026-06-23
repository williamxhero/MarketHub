from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import rankings


router = APIRouter()


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


@router.get("/api/rankings/research/reports", summary='返回研报热度排行', description='`GET` 返回研报热度排行。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n\n## 返回类型\n\n顶层返回 `list[RankingResearchReportItem]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：股票简称。\n- `institution`（`str`）：发布研报的机构。\n- `rating`（`str`）：评级。\n- `target_price`（`float | None`）：目标价。\n- `title`（`str`）：研报标题。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_ranking_research_reports(trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), limit: int = Query(200, ge=1, le=5000, description='返回记录上限。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, rankings.get_research_reports, (trade_date, start_date, end_date, limit))


@router.get("/api/rankings/research/broker-monthly-picks", summary='返回券商月度金股排行', description='`GET` 返回券商月度金股排行。\n\n## 查询参数\n\n- `trade_month`（类型：`str`）：月份筛选，格式 `YYYY-MM`。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n\n## 返回类型\n\n顶层返回 `list[RankingBrokerPickItem]`。\n\n## 返回字段\n\n- `trade_month`（`str`）：月份，格式 `YYYY-MM`。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：股票简称。\n- `institution`（`str`）：券商机构名称。\n- `rank`（`int | None`）：排名。\n- `recommend_count`（`int | None`）：被推荐次数。\n- `rating`（`str`）：评级。')
async def api_ranking_broker_monthly_picks(trade_month: str = Query("", description='月份，格式 `YYYY-MM`。'), limit: int = Query(200, ge=1, le=5000, description='返回记录上限。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, rankings.get_broker_monthly_picks, (trade_month, limit))
