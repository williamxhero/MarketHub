from __future__ import annotations

import ast
import inspect
import sys
import types
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

from fastapi.params import Query


ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = ROOT / "services" / "markethub_api"
SRC_ROOT = SERVICE_ROOT / "src"
DOCS_ROOT = SERVICE_ROOT / "docs" / "integration-api"


if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from runtime_paths import configure_python_path


configure_python_path()

import quotemux.models as quotemux_models
from docs_all import SUMMARY_BY_API_PATH


ROUTER_NAMES = ["stocks", "boards", "indexes", "markets", "rankings"]
ROUTER_MODULES = {name: __import__(f"routers.{name}", fromlist=["*"]) for name in ROUTER_NAMES}
SERVICE_MODULES = {name: __import__(f"services.{name}", fromlist=["*"]) for name in ROUTER_NAMES}


SUMMARYS = dict(SUMMARY_BY_API_PATH)
SUMMARYS.update(
    {
        "/api/health": "返回服务健康状态。",
        "/api/stocks/quotes": "返回单只或多只股票的行情序列。",
        "/api/stocks/catalog": "返回股票基础清单。",
        "/api/stocks/catalog/archive": "返回指定交易日的股票归档清单。",
        "/api/stocks/{code}/profile/basic": "返回单只股票的基础资料。",
        "/api/stocks/{code}/profile": "返回单只股票的公司概况。",
        "/api/stocks/{code}/profile/name-history": "返回单只股票的名称变更记录。",
        "/api/stocks/{code}/profile/managers": "返回单只股票的管理层名单。",
        "/api/stocks/{code}/profile/management-rewards": "返回单只股票的高管薪酬记录。",
        "/api/stocks/{code}/signals/hl": "返回单只股票的新高新低信号。",
        "/api/stocks/{code}/signals/nine-turn": "返回单只股票的神奇九转信号。",
        "/api/stocks/{code}/factors/adj": "返回单只股票的复权因子序列。",
        "/api/stocks/{code}/factors/technical": "返回单只股票的技术指标序列。",
        "/api/stocks/{code}/indicators/money-flow": "返回单只股票的资金流指标。",
        "/api/stocks/indicators/ah-comparisons": "返回 AH 股比价数据。",
        "/api/stocks/indicators/daily-basic": "返回股票日频基础指标。",
        "/api/stocks/indicators/daily-valuation": "返回股票日频估值指标。",
        "/api/stocks/indicators/daily-market-value": "返回股票日频市值指标。",
        "/api/stocks/indicators/risk-flags": "返回股票风险标识记录。",
        "/api/stocks/{code}/indicators/premarket": "返回单只股票的盘前指标数据。",
        "/api/stocks/{code}/indicators/chip-distribution": "返回单只股票的筹码分布数据。",
        "/api/stocks/{code}/indicators/chip-performance": "返回单只股票的筹码盈亏分布数据。",
        "/api/stocks/finance/statements": "返回股票财务报表数据。",
        "/api/stocks/finance/indicators": "返回股票财务指标数据。",
        "/api/stocks/{code}/finance/audits": "返回单只股票的审计意见记录。",
        "/api/stocks/{code}/finance/disclosure-dates": "返回单只股票的财报披露日期记录。",
        "/api/stocks/{code}/finance/express": "返回单只股票的业绩快报记录。",
        "/api/stocks/{code}/finance/forecasts": "返回单只股票的业绩预告记录。",
        "/api/stocks/{code}/finance/main-business": "返回单只股票的主营业务构成。",
        "/api/stocks/{code}/corporate-actions/dividends": "返回单只股票的分红送转记录。",
        "/api/stocks/{code}/corporate-actions/repurchases": "返回单只股票的回购记录。",
        "/api/stocks/{code}/corporate-actions/rights-issues": "返回单只股票的配股记录。",
        "/api/stocks/{code}/corporate-actions/share-changes": "返回单只股票的股本变动记录。",
        "/api/stocks/{code}/corporate-actions/unlock-schedules": "返回单只股票的限售解禁安排。",
        "/api/stocks/{code}/ownership/ccass-holdings": "返回单只股票的中央结算持股汇总。",
        "/api/stocks/{code}/ownership/ccass-holding-details": "返回单只股票的中央结算持股明细。",
        "/api/stocks/{code}/ownership/hk-connect-holdings": "返回单只股票的沪深港通持股数据。",
        "/api/stocks/{code}/ownership/pledges/stats": "返回单只股票的股权质押统计。",
        "/api/stocks/{code}/ownership/pledges/details": "返回单只股票的股权质押明细。",
        "/api/stocks/{code}/ownership/shareholders/count": "返回单只股票的股东户数记录。",
        "/api/stocks/{code}/ownership/shareholders/changes": "返回单只股票的股东户数变动记录。",
        "/api/stocks/{code}/ownership/shareholders/top10": "返回单只股票的前十大股东。",
        "/api/stocks/{code}/ownership/shareholders/top10-float": "返回单只股票的前十大流通股东。",
        "/api/stocks/{code}/research/reports": "返回单只股票的研报记录。",
        "/api/stocks/{code}/research/surveys": "返回单只股票的调研记录。",
        "/api/stocks/reference/bse-code-mappings": "返回北交所证券代码映射关系。",
        "/api/stocks/reference/hk-connect-targets": "返回沪深港通标的范围。",
        "/api/stocks/{code}/quotes/auctions": "返回单只股票的竞价行情数据。",
        "/api/boards/quotes": "返回单个或多个板块的行情序列。",
        "/api/boards/catalog": "返回板块目录清单。",
        "/api/boards/{board_code}/profile": "返回单个板块的基础资料。",
        "/api/boards/{board_code}/members": "返回单个板块在指定交易日的成分列表。",
        "/api/boards/{board_code}/members/history": "返回单个板块的成分变动历史。",
        "/api/boards/{board_code}/indicators/money-flow": "返回单个板块的资金流指标。",
        "/api/boards/reference/categories": "返回板块分类目录。",
        "/api/indexes/catalog": "返回指数目录清单。",
        "/api/indexes/{index_code}/profile": "返回单个指数的基础资料。",
        "/api/indexes/quotes": "返回单个或多个指数的行情序列。",
        "/api/indexes/{index_code}/members": "返回单个指数的成分列表。",
        "/api/markets/calendar/trading": "返回交易日历列表。",
        "/api/markets/calendar/trading/previous": "返回给定日期之前的最近若干个交易日。",
        "/api/markets/calendar/trading/next": "返回给定日期之后的最近若干个交易日。",
        "/api/markets/calendar/trading/yearly": "返回指定年份区间的交易日历汇总。",
        "/api/markets/indicators/main-capital-flow": "返回市场主力资金流指标。",
        "/api/markets/connect/capital-flow": "返回沪深港通资金流向数据。",
        "/api/markets/connect/quotas": "返回沪深港通额度使用情况。",
        "/api/markets/connect/active-top10": "返回沪深港通活跃成交前十明细。",
        "/api/markets/events/block-trades": "返回市场大宗交易明细。",
        "/api/markets/participants/dragon-tiger": "返回龙虎榜成交明细。",
        "/api/markets/participants/dragon-tiger/institutions": "返回龙虎榜机构席位明细。",
        "/api/markets/participants/hot-money": "返回游资营业部榜单。",
        "/api/markets/participants/hot-money/details": "返回游资营业部交易明细。",
        "/api/markets/trading/open-auctions": "返回市场开盘竞价汇总。",
        "/api/markets/trading/sessions": "返回交易时段定义。",
        "/api/rankings/research/reports": "返回研报热度排行。",
        "/api/rankings/research/broker-monthly-picks": "返回券商月度金股排行。",
    }
)

PARAM_OVERRIDES: dict[tuple[str, str], str] = {
    ("/api/stocks/quotes", "code"): "单个股票代码；与 `codes` 至少传一个。",
    ("/api/stocks/quotes", "codes"): "多个股票代码，逗号分隔；与 `code` 至少传一个。",
    ("/api/stocks/quotes", "freq"): "行情频率，可选 `tick`、`1m`、`5m`、`15m`、`30m`、`60m`、`1d`、`1w`、`1mo`。",
    ("/api/stocks/catalog", "market"): "兼容参数，当前实现保留该入参但不参与筛选。",
    ("/api/stocks/catalog", "is_hs"): "兼容参数，当前实现保留该入参但不参与筛选。",
    ("/api/stocks/catalog", "name"): "股票简称关键字。",
    ("/api/stocks/catalog/archive", "trade_date"): "归档交易日，格式 `YYYY-MM-DD`。",
    ("/api/stocks/catalog/archive", "name"): "股票简称关键字。",
    ("/api/stocks/catalog/archive", "industry"): "所属行业筛选。",
    ("/api/stocks/catalog/archive", "area"): "所属地域筛选。",
    ("/api/stocks/{code}/signals/nine-turn", "freq"): "神奇九转计算周期。",
    ("/api/stocks/{code}/indicators/money-flow", "view"): "资金流视图，可选 `summary`、`trend`、`breakdown`。",
    ("/api/stocks/finance/statements", "code"): "单个股票代码；与 `codes` 至少传一个。",
    ("/api/stocks/finance/statements", "codes"): "多个股票代码，逗号分隔；与 `code` 至少传一个。",
    ("/api/stocks/finance/statements", "report_type"): "报表类型，可选 `income_statement`、`balance_sheet`、`cash_flow_statement`。",
    ("/api/stocks/finance/indicators", "code"): "单个股票代码；与 `codes` 至少传一个。",
    ("/api/stocks/finance/indicators", "codes"): "多个股票代码，逗号分隔；与 `code` 至少传一个。",
    ("/api/stocks/{code}/finance/main-business", "classification"): "主营业务分类口径，默认 `industry`。",
    ("/api/stocks/{code}/ownership/pledges/details", "status"): "质押状态筛选。",
    ("/api/stocks/reference/bse-code-mappings", "old_code"): "旧证券代码筛选。",
    ("/api/stocks/reference/bse-code-mappings", "new_code"): "新证券代码筛选。",
    ("/api/stocks/reference/bse-code-mappings", "status"): "代码映射状态筛选。",
    ("/api/stocks/reference/hk-connect-targets", "direction"): "互联互通方向筛选，如 `north` 或 `south`。",
    ("/api/stocks/reference/hk-connect-targets", "status"): "标的状态筛选。",
    ("/api/stocks/{code}/quotes/auctions", "session"): "竞价时段。",
    ("/api/boards/quotes", "board_code"): "单个板块代码；与 `board_codes` 至少传一个。",
    ("/api/boards/quotes", "board_codes"): "多个板块代码，逗号分隔；与 `board_code` 至少传一个。",
    ("/api/boards/quotes", "freq"): "行情频率，可选 `1m`、`5m`、`15m`、`30m`、`60m`、`1d`、`1w`、`1mo`。",
    ("/api/boards/{board_code}/indicators/money-flow", "scope"): "资金流统计口径，可选 `board`、`industry`。",
    ("/api/boards/reference/categories", "level"): "分类层级筛选，从 `1` 开始。",
    ("/api/indexes/quotes", "index_code"): "单个指数代码；与 `index_codes` 至少传一个。",
    ("/api/indexes/quotes", "index_codes"): "多个指数代码，逗号分隔；与 `index_code` 至少传一个。",
    ("/api/indexes/quotes", "freq"): "行情频率，可选 `1d`、`1w`、`1mo`。",
    ("/api/markets/calendar/trading/previous", "trade_date"): "参考交易日，返回该日期之前的开市日，格式 `YYYY-MM-DD`。",
    ("/api/markets/calendar/trading/next", "trade_date"): "参考交易日，返回该日期之后的开市日，格式 `YYYY-MM-DD`。",
    ("/api/markets/connect/quotas", "type"): "互联互通额度类型筛选，如沪股通、深股通或港股通。",
    ("/api/markets/connect/active-top10", "type"): "互联互通市场类型筛选，如沪股通、深股通或港股通。",
    ("/api/markets/events/block-trades", "code"): "股票代码筛选。",
    ("/api/markets/participants/dragon-tiger", "code"): "股票代码筛选。",
    ("/api/markets/participants/dragon-tiger/institutions", "code"): "股票代码筛选。",
    ("/api/markets/participants/hot-money", "name"): "游资或营业部名称关键字。",
    ("/api/markets/participants/hot-money", "tag"): "游资标签筛选。",
    ("/api/markets/participants/hot-money/details", "name"): "游资或营业部名称筛选。",
    ("/api/markets/trading/open-auctions", "codes"): "股票代码列表，逗号分隔。",
    ("/api/markets/trading/open-auctions", "instrument_type"): "标的类型，当前实现仅按股票口径返回。",
    ("/api/markets/trading/sessions", "codes"): "股票代码列表，逗号分隔；不传时返回默认交易时段定义。",
    ("/api/rankings/research/broker-monthly-picks", "trade_month"): "月份筛选，格式 `YYYY-MM`。",
}

GENERIC_PARAM_DESC: dict[str, str] = {
    "code": "股票代码。",
    "codes": "多个股票代码，逗号分隔。",
    "board_code": "板块代码。",
    "board_codes": "多个板块代码，逗号分隔。",
    "index_code": "指数代码。",
    "index_codes": "多个指数代码，逗号分隔。",
    "trade_date": "交易日期，格式 `YYYY-MM-DD`。",
    "start_date": "起始日期，格式 `YYYY-MM-DD`。",
    "end_date": "结束日期，格式 `YYYY-MM-DD`。",
    "start_time": "起始时间，分钟级行情可传完整时间字符串。",
    "end_time": "结束时间，分钟级行情可传完整时间字符串。",
    "count": "每个代码返回的最近记录条数。",
    "adjust": "复权方式。",
    "fields": "按逗号指定返回字段，不传返回全部字段。",
    "limit": "返回记录上限。",
    "offset": "结果偏移量，从 `0` 开始。",
    "name": "名称关键字。",
    "market": "市场筛选。",
    "exchange": "交易所标识。",
    "list_status": "上市状态筛选。",
    "is_hs": "沪深港通标识筛选。",
    "include_delisted": "是否包含已退市标的。",
    "report_period": "单个报告期，格式 `YYYY-MM-DD`。",
    "start_period": "报告期起始日期，格式 `YYYY-MM-DD`。",
    "end_period": "报告期结束日期，格式 `YYYY-MM-DD`。",
    "report_type": "报表类型。",
    "classification": "分类口径。",
    "view": "数据视图。",
    "flag_type": "风险标识类型筛选。",
    "status": "状态筛选。",
    "unlock_date": "解禁日期，格式 `YYYY-MM-DD`。",
    "effective_date": "生效日期，格式 `YYYY-MM-DD`。",
    "report_date": "研报日期，格式 `YYYY-MM-DD`。",
    "survey_date": "调研日期，格式 `YYYY-MM-DD`。",
    "industry": "行业筛选。",
    "area": "地域筛选。",
    "category": "分类筛选。",
    "publisher": "编制方筛选。",
    "parent_code": "父级分类代码。",
    "level": "层级筛选。",
    "is_open": "是否只返回开市日。",
    "n": "返回记录数量。",
    "start_year": "起始年份。",
    "end_year": "结束年份。",
    "type": "类型筛选。",
    "direction": "方向筛选。",
    "tag": "标签筛选。",
    "session": "时段筛选。",
    "trade_month": "月份筛选。",
    "skip_suspended": "是否跳过停牌补齐逻辑。",
    "fill_missing": "是否在本地数据不足时补拉外部数据。",
    "instrument_type": "标的类型。",
}

ROUTE_NOTES: dict[str, list[str]] = {
    "/api/stocks/catalog": ["`market` 和 `is_hs` 当前只是兼容入参，不参与实际筛选。"],
    "/api/markets/calendar/trading/previous": ["结果按日期升序返回，最多返回 `n` 个开市日。"],
    "/api/markets/calendar/trading/next": ["结果按日期升序返回，最多返回 `n` 个开市日。"],
    "/api/markets/trading/open-auctions": ["`instrument_type` 当前不会改变返回口径，接口始终按股票竞价数据返回。"],
}

MANUAL_ROUTE_SPECS: dict[str, dict[str, object]] = {
    "/api/health": {
        "params": [],
        "container": "object",
        "model_name": "HealthPayload",
        "fields": [
            ("service", "str", "服务标识。"),
            ("status", "str", "健康状态；正常情况下为 `ok`。"),
            ("version", "str", "当前服务版本。"),
            ("updated_at", "str", "健康检查文案中的更新时间。"),
        ],
    }
}

FIELD_EXACT: dict[str, str] = {
    "code": "股票代码。",
    "trade_time": "时间点；日频返回交易日，分钟级返回具体时间。",
    "freq": "数据频率。",
    "open": "开盘价。",
    "high": "最高价。",
    "low": "最低价。",
    "close": "收盘价。",
    "pre_close": "前收盘价。",
    "change": "涨跌额。",
    "pct_chg": "涨跌幅，单位 %。",
    "volume": "成交量。",
    "amount": "成交额。",
    "adjust": "复权方式。",
    "report_period": "报告期。",
    "announce_date": "公告日期。",
    "revenue": "营业收入。",
    "operating_profit": "营业利润。",
    "total_profit": "利润总额。",
    "net_profit": "净利润。",
    "total_assets": "总资产。",
    "total_liabilities": "总负债。",
    "equity": "权益规模。",
    "adj_factor": "复权因子。",
    "trade_date": "交易日期。",
    "main_inflow": "主力流入金额。",
    "main_outflow": "主力流出金额。",
    "net_inflow": "净流入金额。",
    "board_code": "板块代码。",
    "board_name": "板块名称。",
    "rank": "排名。",
    "turnover_rate": "换手率，单位 %。",
    "net_amount": "净买入金额。",
    "inflow": "流入金额。",
    "outflow": "流出金额。",
    "name": "名称。",
    "exchange": "交易所标识。",
    "market": "市场标识。",
    "list_status": "上市状态。",
    "list_date": "上市日期。",
    "delist_date": "退市日期；未退市时为空字符串。",
    "industry": "所属行业。",
    "area": "所属地域。",
    "ann_date": "公告日期。",
    "first_extreme": "首次触发的新高或新低类型。",
    "high_time": "触发新高的时间。",
    "low_time": "触发新低的时间。",
    "signal": "信号类型。",
    "category": "分类。",
    "status": "状态标识。",
    "weight": "权重。",
    "join_date": "纳入日期。",
    "effective_date": "生效日期。",
    "action": "变动动作。",
    "parent_code": "父级分类代码。",
    "level": "层级。",
    "sort_order": "排序值。",
    "category_code": "分类代码。",
    "category_name": "分类名称。",
    "is_open": "是否为开市日。",
    "buy_amount": "买入金额。",
    "sell_amount": "卖出金额。",
    "quota_total": "总额度。",
    "quota_balance": "剩余额度。",
    "quota_used": "已使用额度。",
    "buyer": "买方营业部或席位名称。",
    "seller": "卖方营业部或席位名称。",
    "reason": "原因说明。",
    "institution_count": "机构席位数量。",
    "alias": "别名。",
    "tag": "标签。",
    "style": "风格标签。",
    "stock_name": "股票名称。",
    "session_name": "交易时段名称。",
    "start_time": "开始时间。",
    "end_time": "结束时间。",
    "timezone": "时区标识。",
    "auction_time": "竞价时间。",
    "price": "价格。",
    "institution": "机构名称。",
    "rating": "评级。",
    "target_price": "目标价。",
    "title": "标题。",
    "company_name": "公司简称。",
    "full_name": "公司全称。",
    "chairman": "董事长。",
    "manager": "总经理或经营负责人。",
    "website": "公司网站。",
    "employee_count": "员工人数。",
    "main_business": "主营业务描述。",
    "office": "办公地址。",
    "gender": "性别。",
    "education": "学历。",
    "begin_date": "开始日期。",
    "reward_amount": "薪酬金额。",
    "hold_amount": "持股数量。",
    "setup_index": "九转 setup 序号。",
    "countdown_index": "九转 countdown 序号。",
    "view": "返回视图标识。",
    "h_code": "对应 H 股代码。",
    "a_close": "A 股收盘价。",
    "h_close": "H 股收盘价。",
    "premium_ratio": "A/H 溢价率，单位 %。",
    "volume_ratio": "量比。",
    "pe": "市盈率。",
    "pb": "市净率。",
    "ps": "市销率。",
    "pcf": "市现率。",
    "dv_ratio": "股息率，单位 %。",
    "total_share": "总股本。",
    "float_share": "流通股本。",
    "total_mv": "总市值。",
    "float_mv": "流通市值。",
    "free_mv": "自由流通市值。",
    "flag_type": "风险标识类型。",
    "limit_up": "涨停价。",
    "limit_down": "跌停价。",
    "chip_ratio": "筹码占比，单位 %。",
    "profit_ratio": "获利盘占比，单位 %。",
    "avg_cost": "平均成本。",
    "cost_70": "70% 成本位。",
    "cost_90": "90% 成本位。",
    "ma5": "5 日均线。",
    "ma10": "10 日均线。",
    "ma20": "20 日均线。",
    "ma60": "60 日均线。",
    "ema12": "12 日 EMA。",
    "ema26": "26 日 EMA。",
    "dif": "MACD 的 DIF 值。",
    "dea": "MACD 的 DEA 值。",
    "macd": "MACD 柱值。",
    "rsi6": "6 日 RSI。",
    "rsi12": "12 日 RSI。",
    "rsi24": "24 日 RSI。",
    "kdj_k": "KDJ 的 K 值。",
    "kdj_d": "KDJ 的 D 值。",
    "kdj_j": "KDJ 的 J 值。",
    "boll_upper": "布林带上轨。",
    "boll_mid": "布林带中轨。",
    "boll_lower": "布林带下轨。",
    "audit_result": "审计意见结论。",
    "auditor": "审计机构。",
    "sign_accountant": "签字会计师。",
    "plan_date": "计划披露日期。",
    "actual_date": "实际披露日期。",
    "change_reason": "变更原因。",
    "eps": "每股收益。",
    "roe": "净资产收益率，单位 %。",
    "roa": "总资产收益率，单位 %。",
    "gross_margin": "毛利率，单位 %。",
    "net_margin": "净利率，单位 %。",
    "asset_turnover": "总资产周转率。",
    "current_ratio": "流动比率。",
    "debt_to_asset": "资产负债率，单位 %。",
    "forecast_type": "业绩预告类型。",
    "forecast_summary": "业绩预告摘要。",
    "net_profit_min": "净利润下限。",
    "net_profit_max": "净利润上限。",
    "pct_chg_min": "业绩变动幅度下限，单位 %。",
    "pct_chg_max": "业绩变动幅度上限，单位 %。",
    "classification": "分类口径。",
    "segment_name": "分部名称。",
    "cost": "成本。",
    "profit": "利润。",
    "revenue_ratio": "收入占比，单位 %。",
    "record_date": "股权登记日。",
    "ex_date": "除权除息日。",
    "pay_date": "派息日期。",
    "cash_dividend_per_share": "每股现金分红。",
    "stock_dividend_per_share": "每股送股。",
    "capital_reserve_per_share": "每股转增资本公积。",
    "progress": "进度状态。",
    "repurchase_volume": "回购数量。",
    "repurchase_amount": "回购金额。",
    "highest_price": "最高回购价。",
    "lowest_price": "最低回购价。",
    "rights_ratio": "配股比例。",
    "rights_price": "配股价格。",
    "change_date": "股本变动日期。",
    "restricted_share": "限售股本。",
    "unlock_date": "解禁日期。",
    "holder_type": "持有人类型。",
    "unlock_volume": "解禁数量。",
    "unlock_ratio": "解禁比例，单位 %。",
    "share_type": "股份类型。",
    "participant_count": "参与者数量。",
    "holding_volume": "持有数量。",
    "holding_ratio": "持有占比，单位 %。",
    "participant_id": "参与者编号。",
    "participant_name": "参与者名称。",
    "change_volume": "变动数量。",
    "pledge_volume": "质押数量。",
    "pledge_ratio": "质押比例，单位 %。",
    "unrestricted_pledge_volume": "无限售股质押数量。",
    "holder_count": "股东户数。",
    "avg_holding": "户均持股数量。",
    "change_count": "较上一期变动的户数。",
    "shareholder_name": "股东名称。",
    "report_date": "研报日期。",
    "analyst": "分析师。",
    "org_name": "调研机构名称。",
    "survey_date": "调研日期。",
    "survey_method": "调研方式。",
    "topic": "调研主题。",
    "announcement_date": "公告日期。",
    "direction": "方向。",
    "old_code": "旧代码。",
    "new_code": "新代码。",
    "index_code": "指数代码。",
    "index_name": "指数名称。",
    "publisher": "编制方。",
    "trade_month": "月份，格式 `YYYY-MM`。",
    "recommend_count": "被推荐次数。",
}

FIELD_OVERRIDES: dict[tuple[str, str], str] = {
    ("StockBasicInfo", "market"): "所属市场板块，如主板、创业板或北交所等口径值。",
    ("StockArchiveItem", "market"): "归档时点对应的所属市场板块。",
    ("BoardCatalogItem", "market"): "板块所属市场，默认 A 股口径。",
    ("IndexCatalogItem", "market"): "指数覆盖市场。",
    ("MarketCapitalFlowItem", "market"): "市场范围标识，如沪市、深市或全市场口径。",
    ("ConnectCapitalFlowItem", "market"): "互联互通市场方向，如沪股通、深股通或港股通。",
    ("ConnectQuotaItem", "market"): "互联互通市场方向，如沪股通、深股通或港股通。",
    ("ConnectActiveTop10Item", "market"): "互联互通市场方向，如沪股通、深股通或港股通。",
    ("TradingCalendarItem", "exchange"): "交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。",
    ("StockArchiveItem", "exchange"): "归档时点对应的交易所。",
    ("StockBasicInfo", "exchange"): "交易所。",
    ("StockBasicInfo", "list_status"): "上市状态。",
    ("StockArchiveItem", "list_status"): "归档时点对应的上市状态。",
    ("HotMoneyProfileItem", "name"): "游资或营业部名称。",
    ("HotMoneyDetailItem", "name"): "游资或营业部名称。",
    ("ConnectActiveTop10Item", "name"): "证券简称。",
    ("BlockTradeItem", "name"): "证券简称。",
    ("DragonTigerItem", "name"): "证券简称。",
    ("DragonTigerInstitutionItem", "name"): "证券简称。",
    ("RankingResearchReportItem", "name"): "股票简称。",
    ("RankingBrokerPickItem", "name"): "股票简称。",
    ("StockAHComparisonItem", "name"): "A 股简称。",
    ("ResearchReportItem", "institution"): "发布研报的机构。",
    ("RankingResearchReportItem", "institution"): "发布研报的机构。",
    ("RankingBrokerPickItem", "institution"): "券商机构名称。",
    ("StockManagerItem", "title"): "职务。",
    ("ManagementRewardItem", "title"): "职务。",
    ("ResearchReportItem", "title"): "研报标题。",
    ("RankingResearchReportItem", "title"): "研报标题。",
    ("DragonTigerItem", "reason"): "上榜原因。",
    ("ShareChangeItem", "reason"): "股本变动原因。",
    ("BSECodeMappingItem", "status"): "代码映射状态，如生效或停用。",
    ("HKConnectTargetItem", "status"): "标的状态，如调入、调出或有效状态。",
    ("BoardCatalogItem", "status"): "板块状态。",
    ("IndexCatalogItem", "status"): "指数状态。",
    ("StockRiskFlagItem", "status"): "风险标识状态。",
    ("PledgeDetailItem", "status"): "质押状态。",
    ("HKConnectTargetItem", "direction"): "互联互通方向，如 `north` 或 `south`。",
    ("HLSignalItem", "signal"): "新高新低信号类型。",
    ("NineTurnItem", "signal"): "九转信号类型。",
    ("BoardMemberItem", "name"): "成分股名称。",
    ("BoardMemberHistoryItem", "name"): "成分股名称。",
    ("IndexMemberItem", "name"): "成分股名称。",
    ("BoardMoneyFlowItem", "scope"): "统计口径。",
    ("TradingSessionItem", "session_name"): "交易时段名称，如集合竞价、连续竞价。",
    ("TradingSessionItem", "code"): "证券代码。",
    ("SurveyItem", "announcement_date"): "调研结果公告日期。",
    ("StockProfileItem", "company_name"): "公司简称或工商登记简称。",
    ("StockProfileItem", "manager"): "总经理或经营负责人。",
    ("MainBusinessItem", "classification"): "主营业务分类口径，如行业、地区或产品。",
    ("IndexMemberItem", "trade_date"): "成分权重对应的交易日。",
    ("TradingCalendarItem", "trade_date"): "交易日日期。",
    ("AuctionItem", "session"): "竞价时段，如开盘竞价。",
    ("ShareholderTop10Item", "change_volume"): "相对上一报告期的持股变动数量。",
}

WORD_LABELS: dict[str, str] = {
    "trade": "交易",
    "date": "日期",
    "time": "时间",
    "report": "报告",
    "period": "期间",
    "amount": "金额",
    "volume": "数量",
    "ratio": "比率",
    "count": "数量",
    "price": "价格",
    "name": "名称",
    "code": "代码",
    "status": "状态",
    "market": "市场",
    "type": "类型",
    "share": "股份",
    "holder": "持有人",
    "holding": "持有",
    "profit": "利润",
    "revenue": "收入",
    "cost": "成本",
    "buy": "买入",
    "sell": "卖出",
    "net": "净",
    "flow": "流",
    "main": "主力",
    "index": "指数",
    "board": "板块",
    "category": "分类",
    "parent": "父级",
    "sort": "排序",
    "change": "变动",
    "reason": "原因",
    "employee": "员工",
    "office": "办公地",
    "website": "网站",
    "direction": "方向",
    "session": "时段",
    "tag": "标签",
    "style": "风格",
    "publisher": "编制方",
    "institution": "机构",
    "analyst": "分析师",
    "survey": "调研",
    "topic": "主题",
    "announcement": "公告",
    "effective": "生效",
    "unlock": "解禁",
    "pledge": "质押",
    "restricted": "限售",
    "float": "流通",
    "free": "自由流通",
    "total": "总",
    "gross": "毛",
    "asset": "资产",
    "debt": "负债",
    "current": "流动",
    "turnover": "周转",
    "premium": "溢价",
    "signal": "信号",
    "weight": "权重",
}


def build_route_specs() -> dict[str, dict[str, object]]:
    specs: dict[str, dict[str, object]] = {}
    for router_name in ROUTER_NAMES:
        module = ROUTER_MODULES[router_name]
        service_module = SERVICE_MODULES[router_name]
        source_path = SRC_ROOT / "routers" / f"{router_name}.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            route_path = ""
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.func.attr == "get":
                    if deco.args and isinstance(deco.args[0], ast.Constant) and isinstance(deco.args[0].value, str):
                        route_path = deco.args[0].value
            if route_path == "":
                continue
            assigned: dict[str, tuple[str, str]] = {}
            service_call: tuple[str, str] | None = None
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    target = stmt.targets[0].id
                    if isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute) and isinstance(stmt.value.func.value, ast.Name):
                        base = stmt.value.func.value.id
                        if base in SERVICE_MODULES:
                            assigned[target] = (base, stmt.value.func.attr)
                if isinstance(stmt, ast.Return):
                    value = stmt.value
                    if isinstance(value, ast.ListComp):
                        iter_value = value.generators[0].iter
                        if isinstance(iter_value, ast.Call) and isinstance(iter_value.func, ast.Attribute) and isinstance(iter_value.func.value, ast.Name):
                            base = iter_value.func.value.id
                            if base in SERVICE_MODULES:
                                service_call = (base, iter_value.func.attr)
                    elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "filter_response_fields":
                        if value.args and isinstance(value.args[0], ast.Name):
                            service_call = assigned.get(value.args[0].id)
                    elif isinstance(value, ast.IfExp) and isinstance(value.test, ast.Compare) and isinstance(value.test.left, ast.Name):
                        service_call = assigned.get(value.test.left.id)
            if service_call is None:
                raise RuntimeError(f"未能识别服务调用: {route_path}")
            service_fn = getattr(service_module, service_call[1])
            globalns = dict(service_fn.__globals__)
            globalns.update(vars(quotemux_models))
            hints = get_type_hints(service_fn, globalns=globalns, localns=globalns)
            return_hint = hints.get("return")
            origin = get_origin(return_hint)
            container = "object"
            model_cls = None
            if origin is list:
                container = "list"
                model_cls = get_args(return_hint)[0]
            else:
                union_args = get_args(return_hint)
                valid_args = [item for item in union_args if item is not type(None)]
                if len(valid_args) == 1:
                    container = "object_or_empty"
                    model_cls = valid_args[0]
                else:
                    model_cls = return_hint
            endpoint_fn = getattr(module, node.name)
            signature = inspect.signature(endpoint_fn)
            params = []
            for name, parameter in signature.parameters.items():
                kind = "path" if "{" + name + "}" in route_path else "query"
                default = parameter.default
                default_value = inspect._empty
                ge = None
                le = None
                if isinstance(default, Query):
                    default_value = default.default
                    for meta in getattr(default, "metadata", []):
                        if hasattr(meta, "ge"):
                            ge = meta.ge
                        if hasattr(meta, "le"):
                            le = meta.le
                elif default is not inspect._empty:
                    default_value = default
                params.append(
                    {
                        "name": name,
                        "kind": kind,
                        "annotation": parameter.annotation,
                        "default": default_value,
                        "ge": ge,
                        "le": le,
                    }
                )
            specs[route_path] = {"params": params, "container": container, "model_cls": model_cls}
    return specs


def type_text(annotation: object) -> str:
    origin = get_origin(annotation)
    if origin in {list, tuple}:
        inner = ", ".join(type_text(item) for item in get_args(annotation))
        return f"{origin.__name__}[{inner}]"
    if origin in {types.UnionType, getattr(__import__("typing"), "Union", object)}:
        return " | ".join(type_text(item) for item in get_args(annotation))
    if annotation is inspect._empty:
        return "object"
    if annotation is None or annotation is type(None):
        return "None"
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def format_default(value: object) -> str:
    if value is inspect._empty or value == "":
        return ""
    if value is None:
        return "允许空值"
    if isinstance(value, bool):
        return f"默认：`{str(value).lower()}`"
    return f"默认：`{value}`"


def format_range(ge: object, le: object) -> str:
    if ge is None and le is None:
        return ""
    if ge is not None and le is not None:
        return f"范围：`{ge}-{le}`"
    if ge is not None:
        return f"最小值：`{ge}`"
    return f"最大值：`{le}`"


def build_param_meta_text(param: dict[str, object]) -> str:
    parts = [f"类型：`{type_text(param['annotation'])}`"]
    default_text = format_default(param["default"])
    range_text = format_range(param["ge"], param["le"])
    if default_text:
        parts.append(default_text)
    if range_text:
        parts.append(range_text)
    return "；".join(parts)


def param_description(api_path: str, param: dict[str, object]) -> str:
    desc = PARAM_OVERRIDES.get((api_path, param["name"]), GENERIC_PARAM_DESC.get(str(param["name"]), "参数说明见接口上下文。"))
    return f"- `{param['name']}`（{build_param_meta_text(param)}）：{desc}"


def token_label(token: str) -> str:
    if token in WORD_LABELS:
        return WORD_LABELS[token]
    if token.startswith("ma") and token[2:].isdigit():
        return f"{token[2:]}日均线"
    if token.startswith("ema") and token[3:].isdigit():
        return f"{token[3:]}日EMA"
    if token.startswith("rsi") and token[3:].isdigit():
        return f"{token[3:]}日RSI"
    if token.isascii() and token.islower() and len(token) <= 4:
        return token.upper()
    return token


def build_fallback_field_desc(field_name: str) -> str:
    if field_name.endswith("_date"):
        base = field_name.removesuffix("_date")
        return f"{''.join(token_label(item) for item in base.split('_'))}日期。"
    if field_name.endswith("_time"):
        base = field_name.removesuffix("_time")
        return f"{''.join(token_label(item) for item in base.split('_'))}时间。"
    if field_name.endswith("_amount"):
        base = field_name.removesuffix("_amount")
        return f"{''.join(token_label(item) for item in base.split('_'))}金额。"
    if field_name.endswith("_volume"):
        base = field_name.removesuffix("_volume")
        return f"{''.join(token_label(item) for item in base.split('_'))}数量。"
    if field_name.endswith("_ratio"):
        base = field_name.removesuffix("_ratio")
        return f"{''.join(token_label(item) for item in base.split('_'))}比率。"
    if field_name.endswith("_count"):
        base = field_name.removesuffix("_count")
        return f"{''.join(token_label(item) for item in base.split('_'))}数量。"
    return f"{''.join(token_label(item) for item in field_name.split('_'))}。"


def field_description(model_name: str, field_name: str) -> str:
    return FIELD_OVERRIDES.get((model_name, field_name), FIELD_EXACT.get(field_name, build_fallback_field_desc(field_name)))


def build_notes(api_path: str, spec: dict[str, object]) -> list[str]:
    params = {item["name"] for item in spec["params"]}
    notes: list[str] = []
    if {"code", "codes"} <= params:
        notes.append("`code` 与 `codes` 至少需要传一个，`count` 按每个股票分别生效。" if "count" in params else "`code` 与 `codes` 至少需要传一个。")
    if {"board_code", "board_codes"} <= params:
        notes.append("`board_code` 与 `board_codes` 至少需要传一个，`count` 按每个板块分别生效。" if "count" in params else "`board_code` 与 `board_codes` 至少需要传一个。")
    if {"index_code", "index_codes"} <= params:
        notes.append("`index_code` 与 `index_codes` 至少需要传一个，`count` 按每个指数分别生效。" if "count" in params else "`index_code` 与 `index_codes` 至少需要传一个。")
    if "trade_date" in params and {"start_date", "end_date"} & params:
        notes.append("`trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。")
    if "report_period" in params and {"start_period", "end_period"} & params:
        notes.append("`report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。")
    if "fields" in params:
        notes.append("传入 `fields` 后，响应中的每条记录只保留所选字段。")
    if spec["container"] == "object_or_empty":
        notes.append("查不到对应记录时返回空对象 `{}`。")
    notes.extend(ROUTE_NOTES.get(api_path, []))
    return notes


def build_doc(api_path: str, spec: dict[str, object]) -> str:
    lines = [f"# {api_path}", "", f"`GET` {SUMMARYS[api_path]}", ""]
    path_params = [item for item in spec["params"] if item["kind"] == "path"]
    query_params = [item for item in spec["params"] if item["kind"] == "query"]
    if path_params:
        lines.extend(["## 路径参数", ""])
        lines.extend(param_description(api_path, item) for item in path_params)
        lines.append("")
    if query_params:
        lines.extend(["## 查询参数", ""])
        lines.extend(param_description(api_path, item) for item in query_params)
        lines.append("")
    model_cls = spec.get("model_cls")
    model_name = str(spec.get("model_name") or model_cls.__name__)
    lines.extend(["## 返回类型", ""])
    if spec["container"] == "list":
        lines.append(f"顶层返回 `list[{model_name}]`。")
    elif spec["container"] == "object_or_empty":
        lines.append(f"顶层返回 `{model_name}`；查不到对应记录时返回空对象 `{{}}`。")
    else:
        lines.append(f"顶层返回 `{model_name}`。")
    lines.extend(["", "## 返回字段", ""])
    manual_fields = spec.get("fields")
    if manual_fields:
        for field_name, field_type, field_desc in manual_fields:
            lines.append(f"- `{field_name}`（`{field_type}`）：{field_desc}")
    else:
        for field_name, field_info in model_cls.model_fields.items():
            lines.append(f"- `{field_name}`（`{type_text(field_info.annotation)}`）：{field_description(model_name, field_name)}")
    notes = build_notes(api_path, spec)
    if notes:
        lines.extend(["", "## 补充说明", ""])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines).rstrip() + "\n"


def collect_api_doc_files() -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for path in DOCS_ROOT.rglob("*.md"):
        text = path.read_text(encoding="utf-8").lstrip("\ufeff")
        lines = text.splitlines()
        if not lines or not lines[0].startswith("# /api/"):
            continue
        api_path = lines[0][2:].strip()
        result.setdefault(api_path, []).append(path)
    return result


def main() -> None:
    route_specs = build_route_specs()
    route_specs.update(MANUAL_ROUTE_SPECS)
    doc_files_by_api = collect_api_doc_files()
    missing_specs = sorted(set(doc_files_by_api) - set(route_specs))
    if missing_specs:
        raise RuntimeError(f"缺少路由规格: {missing_specs}")
    for api_path, paths in sorted(doc_files_by_api.items()):
        content = build_doc(api_path, route_specs[api_path])
        for path in paths:
            path.write_text(content, encoding="utf-8", newline="\n")
            print(path.relative_to(ROOT).as_posix())


if __name__ == "__main__":
    main()
