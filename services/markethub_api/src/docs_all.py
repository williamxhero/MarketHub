from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from core.config import INTEGRATION_DOCS_ROOT
from docs_paths import to_public_doc_path


API_TITLE_RE = re.compile(r"^#\s+(?P<api_path>/api/\S+)\s*$", re.MULTILINE)
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
METHOD_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+(?P<summary>.+)$")
PLACEHOLDER_SUMMARY_RE = re.compile(r"^(GET\s+)?returns data for this endpoint\.?$", re.IGNORECASE)
GROUP_ORDER = ["root", "docs", "stocks", "boards", "indexes", "markets", "rankings", "system"]
GROUP_TITLE = {
    "root": "总入口",
    "docs": "文档服务",
    "stocks": "股票",
    "boards": "板块",
    "indexes": "指数",
    "markets": "市场",
    "rankings": "排行",
    "system": "系统",
}
IGNORED_DOC_PATHS = {"system/transitional-items.md"}
SUMMARY_BY_API_PATH = {
    "/api/markets/calendar/trading/next": "返回给定日期之后的最近一个交易日。",
    "/api/markets/calendar/trading/previous": "返回给定日期之前的最近一个交易日。",
    "/api/markets/calendar/trading/yearly": "返回指定年份的交易日历汇总。",
    "/api/markets/connect/active-top10": "返回沪深港通活跃成交前十明细。",
    "/api/markets/connect/capital-flow": "返回沪深港通资金流向数据。",
    "/api/markets/connect/quotas": "返回沪深港通额度使用情况。",
    "/api/markets/events/block-trades": "返回市场大宗交易明细。",
    "/api/markets/indicators/main-capital-flow": "返回市场主力资金流指标。",
    "/api/markets/participants/dragon-tiger": "返回龙虎榜成交明细。",
    "/api/markets/participants/dragon-tiger-institutions": "返回龙虎榜机构席位明细。",
    "/api/markets/participants/dragon-tiger/institutions": "返回龙虎榜机构席位明细。",
    "/api/markets/participants/hot-money": "返回游资营业部榜单。",
    "/api/markets/participants/hot-money-details": "返回游资营业部交易明细。",
    "/api/markets/participants/hot-money/details": "返回游资营业部交易明细。",
    "/api/markets/trading/open-auctions": "返回市场开盘竞价汇总。",
    "/api/markets/trading/sessions": "返回交易时段定义。",
    "/api/stocks/{code}/corporate-actions/dividends": "返回单只股票的分红送转记录。",
    "/api/stocks/{code}/corporate-actions/repurchases": "返回单只股票的回购记录。",
    "/api/stocks/{code}/corporate-actions/rights-issues": "返回单只股票的配股记录。",
    "/api/stocks/{code}/corporate-actions/share-changes": "返回单只股票的股本变动记录。",
    "/api/stocks/{code}/corporate-actions/unlock-schedules": "返回单只股票的限售解禁安排。",
    "/api/stocks/{code}/factors/technical": "返回单只股票的技术指标序列。",
    "/api/stocks/{code}/finance/audits": "返回单只股票的审计意见记录。",
    "/api/stocks/{code}/finance/disclosure-dates": "返回单只股票的财报披露日期记录。",
    "/api/stocks/{code}/finance/express": "返回单只股票的业绩快报记录。",
    "/api/stocks/{code}/finance/forecasts": "返回单只股票的业绩预告记录。",
    "/api/stocks/{code}/finance/main-business": "返回单只股票的主营业务构成。",
    "/api/stocks/finance/indicators": "返回股票财务指标数据。",
    "/api/stocks/finance/statements": "返回股票财务报表数据。",
    "/api/stocks/indicators/ah-comparisons": "返回 AH 股比价数据。",
    "/api/stocks/{code}/indicators/chip-distribution": "返回单只股票的筹码分布数据。",
    "/api/stocks/{code}/indicators/chip-performance": "返回单只股票的筹码盈亏分布数据。",
    "/api/stocks/indicators/daily-basic": "返回股票日频基础指标。",
    "/api/stocks/quotes/daily-snapshot": "返回指定交易日的全市场股票日线快照。",
    "/api/stocks/quotes/daily-local-window": "返回指定日期区间内的全市场股票日线。",
    "/api/boards/indicators/money-flow": "返回指定交易日的全市场板块资金流快照。",
    "/api/stocks/indicators/daily-market-value": "返回股票日频市值指标。",
    "/api/stocks/indicators/daily-valuation": "返回股票日频估值指标。",
    "/api/stocks/{code}/indicators/premarket": "返回单只股票的盘前指标数据。",
    "/api/stocks/indicators/risk-flags": "返回股票风险标识记录。",
    "/api/stocks/{code}/ownership/ccass-holding-details": "返回单只股票的中央结算持股明细。",
    "/api/stocks/{code}/ownership/ccass-holdings": "返回单只股票的中央结算持股汇总。",
    "/api/stocks/{code}/ownership/hk-connect-holdings": "返回单只股票的沪深港通持股数据。",
    "/api/stocks/{code}/ownership/pledges/details": "返回单只股票的股权质押明细。",
    "/api/stocks/{code}/ownership/pledges/stats": "返回单只股票的股权质押统计。",
    "/api/stocks/{code}/ownership/shareholders/changes": "返回单只股票的股东增减持记录。",
    "/api/stocks/{code}/ownership/shareholders/count": "返回单只股票的股东户数记录。",
    "/api/stocks/{code}/ownership/shareholders/top10-float": "返回单只股票的前十大流通股东。",
    "/api/stocks/{code}/ownership/shareholders/top10": "返回单只股票的前十大股东。",
    "/api/stocks/{code}/profile/management-rewards": "返回单只股票的高管薪酬记录。",
    "/api/stocks/{code}/profile/managers": "返回单只股票的管理层名单。",
    "/api/stocks/{code}/quotes/auctions": "返回单只股票的竞价行情数据。",
    "/api/stocks/reference/bse-code-mappings": "返回北交所证券代码映射关系。",
    "/api/stocks/reference/hk-connect-targets": "返回沪深港通标的范围。",
    "/api/stocks/{code}/research/reports": "返回单只股票的研报记录。",
    "/api/stocks/{code}/research/surveys": "返回单只股票的调研记录。",
    "/api/stocks/{code}/signals/hl": "返回单只股票的新高新低信号。",
    "/api/stocks/{code}/signals/nine-turn": "返回单只股票的神奇九转信号。",
}


@dataclass(frozen=True, slots=True)
class AllDocItem:
    doc_path: str
    title: str
    group: str
    summary: str
    api_path: str
    method: str


def build_all_doc_payload(with_links: bool) -> dict[str, str]:
    items = collect_all_doc_items()
    lines = [
        "# all",
        "",
        "这里汇总当前 `services/markethub_api/docs/integration-api` 下的全部文档。",
        "",
        "设计目标是让 `/docs/all` 和 `/doc-view/all` 可以一路打开全部文档，不遗漏分组 README、系统说明和接口说明。",
        "",
        "当前范围只覆盖 A 股股票市场基础数据，不包含 ETF、LOF、债券、可转债、tick 和因子。",
        "",
        f"当前共收录 {len(items)} 篇文档。",
        "",
    ]
    for group in GROUP_ORDER:
        group_items = [item for item in items if item.group == group]
        if group_items == []:
            continue
        lines.append(f"## {group} / {GROUP_TITLE[group]}")
        lines.append("")
        for item in group_items:
            label = build_item_label(item)
            if with_links:
                doc_href = "/doc-view" if item.doc_path == "" else f"/doc-view/{item.doc_path}"
                lines.append(f'- <a href="{doc_href}">{label}</a> {item.summary}')
            else:
                lines.append(f"- {label} {item.summary}")
        lines.append("")
    return {
        "path": "all",
        "title": "all",
        "content": "\n".join(lines).rstrip() + "\n",
    }


def collect_all_doc_items() -> list[AllDocItem]:
    items: list[AllDocItem] = []
    for path in sorted(INTEGRATION_DOCS_ROOT.rglob("*.md")):
        raw_relative_path = path.relative_to(INTEGRATION_DOCS_ROOT).as_posix()
        if raw_relative_path in IGNORED_DOC_PATHS:
            continue
        items.append(normalize_item_summary(parse_all_doc_item(path)))
    return sorted(items, key=lambda item: (GROUP_ORDER.index(item.group), item.doc_path, item.api_path, item.title))


def parse_all_doc_item(path: Path) -> AllDocItem:
    text = path.read_text(encoding="utf-8").lstrip("\ufeff")
    raw_relative_path = path.relative_to(INTEGRATION_DOCS_ROOT).as_posix()
    doc_path = to_public_doc_path(raw_relative_path)
    title = extract_title(text, path)
    api_path = extract_api_path(text)
    method, summary = extract_method_and_summary(text)
    return AllDocItem(
        doc_path=doc_path,
        title=title,
        group=resolve_group(raw_relative_path),
        summary=summary,
        api_path=api_path,
        method=method,
    )


def resolve_group(raw_relative_path: str) -> str:
    if raw_relative_path == "README.md":
        return "root"
    group = raw_relative_path.split("/", 1)[0]
    if group in GROUP_ORDER:
        return group
    return "system"


def extract_title(text: str, path: Path) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def extract_api_path(text: str) -> str:
    title_match = API_TITLE_RE.search(text)
    if title_match is None:
        return ""
    return title_match.group("api_path")


def build_item_label(item: AllDocItem) -> str:
    if item.api_path:
        return f"{item.method} {item.api_path}"
    if item.doc_path == "":
        return "/docs"
    return f"/docs/{item.doc_path}"


def normalize_item_summary(item: AllDocItem) -> AllDocItem:
    if item.api_path == "":
        return item
    if PLACEHOLDER_SUMMARY_RE.match(item.summary) is None:
        return item
    summary = SUMMARY_BY_API_PATH.get(item.api_path, "返回该接口对应的数据。")
    return AllDocItem(
        doc_path=item.doc_path,
        title=item.title,
        group=item.group,
        summary=summary,
        api_path=item.api_path,
        method=item.method,
    )


def extract_method_and_summary(text: str) -> tuple[str, str]:
    seen_title = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "":
            continue
        if line.startswith("# "):
            seen_title = True
            continue
        if not seen_title:
            continue
        if line.startswith("## "):
            break
        plain_line = INLINE_CODE_RE.sub(lambda match: match.group(1), line)
        method_match = METHOD_RE.match(plain_line)
        if method_match is not None:
            return method_match.group(1), method_match.group("summary").strip()
        return "GET", plain_line
    return "GET", "文档说明。"
