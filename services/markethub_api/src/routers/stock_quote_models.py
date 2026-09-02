from __future__ import annotations

from typing import Literal

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from quotemux.models import StockQuoteItem, StockQuotesMeta


class StockQuotesQueryPayload(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "codes": ["600000", "000001", "920000"],
                    "freq": "1m",
                    "trade_date": "2026-07-15",
                    "adjust": "none",
                    "meta_detail": "summary",
                    "data_version": "mhf-v1-from-api-health",
                }
            ]
        }
    )

    codes: list[str] = Field(min_length=1, description="股票代码列表，推荐每批 100 至 200 只。")
    freq: str = Field(default="1d", description="行情频率，例如 1m 或 1d。")
    datetime: Literal["", "now"] = Field(default="", description="动态时间锚点；首期仅支持 now。")
    trade_date: str = Field(default="", description="单个交易日，格式 YYYY-MM-DD。")
    start_date: str = Field(default="", description="起始交易日，格式 YYYY-MM-DD。")
    end_date: str = Field(default="", description="结束交易日，格式 YYYY-MM-DD。")
    start_time: str = Field(default="", description="起始时间，可传完整日期时间。")
    end_time: str = Field(default="", description="结束时间，可传完整日期时间。")
    count: int | None = Field(default=None, ge=1, description="每只股票保留最近若干条记录。")
    adjust: str = Field(default="none", description="复权方式。")
    limit: int | None = Field(
        default=None,
        ge=1,
        description="整个响应的硬裁剪上限，不是分页大小；不传时返回请求范围内的全部结果。",
    )
    skip_suspended: bool = Field(default=True, description="日线查询时过滤停牌行。")
    skip_st: bool = Field(default=False, description="日线查询时按整只股票过滤 ST。")
    fill_missing: bool = Field(default=False, description="是否返回日线缺口生成的停牌占位行。")
    meta_detail: Literal["summary", "full"] = Field(
        default="summary",
        description="summary 只返回缺失数量；full 额外展开 missing_trade_times。",
    )
    data_version: str = Field(default="", description="兼容字段：/api/health 返回的全局市场事实版本。")
    dataset_version: str = Field(default="", description="推荐字段：对应频率的数据集版本；1m 使用 stock_bar_1m。")

    @model_validator(mode="after")
    def validate_current_mode(self) -> "StockQuotesQueryPayload":
        if self.datetime != "now":
            return self
        if self.freq != "1m":
            raise ValueError("datetime=now 当前仅支持 freq=1m")
        if self.adjust != "none":
            raise ValueError("datetime=now 当前仅支持 adjust=none")
        if any((self.trade_date, self.start_date, self.end_date, self.start_time, self.end_time)):
            raise ValueError("datetime=now 不能与交易日期或时间范围参数组合")
        if self.count is None:
            self.count = 1
        elif self.count != 1:
            raise ValueError("datetime=now 当前仅支持 count=1")
        return self


class StockQuotesVersionedMeta(StockQuotesMeta):
    dataset_version: str = Field(default="", description="实际固定的目标数据集版本。")


class StockQuotesVersionedQueryResult(BaseModel):
    items: list[StockQuoteItem] = Field(default_factory=list)
    meta: StockQuotesVersionedMeta


class CurrentStockQuoteItem(StockQuoteItem):
    interval_start: str
    interval_end: str
    is_final: bool
    observed_at: str
    last_trade_at: str | None = None
    provider: str
    source_semantics: Literal["native", "derived"]
    observation_version: str
    freshness_ms: int
    degraded: bool = False
    market_status: str


class CurrentStockQuotesMeta(StockQuotesVersionedMeta):
    effective_now: str
    historical_dataset_version: str = ""


class CurrentStockQuotesQueryResult(BaseModel):
    items: list[CurrentStockQuoteItem] = Field(default_factory=list)
    meta: CurrentStockQuotesMeta
    errors: list[dict[str, object]] = Field(default_factory=list)
    diagnostics: list[dict[str, object]] = Field(default_factory=list)


class StockDailyWindowQueryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_version: str = Field(default="", description="兼容字段：/api/health 返回的全局市场事实版本。")
    dataset_version: str = Field(default="", description="推荐字段：/api/health.dataset_versions.stock_daily_1d。")
    freq: Literal["1d"] = Field(default="1d", description="固定为 1d。")
    universe: Literal["codes", "all_a"] = Field(description="精确代码集合或正式全 A universe。")
    codes: list[str] = Field(default_factory=list, description="universe=codes 时提交六位裸代码集合；不设代码数量上限。")
    start_date: str = Field(description="起始交易日，YYYY-MM-DD，闭区间。")
    end_date: str = Field(description="结束交易日，YYYY-MM-DD，闭区间。")
    page_size: int = Field(default=50000, ge=1, le=100000, description="交付分页大小；不是结果裁剪上限。")
    cursor: str | None = Field(default=None, description="上一页返回的 opaque continuation cursor。")
    meta_detail: Literal["summary", "full"] = Field(default="full", description="full 保留逐代码 coverage；summary 仅返回汇总。")

    @field_validator("codes")
    @classmethod
    def validate_codes(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            code = value.strip()
            if len(code) != 6 or not code.isdigit():
                raise ValueError("codes 只接受六位裸股票代码")
            if code not in seen:
                normalized.append(code)
                seen.add(code)
        return normalized

    @model_validator(mode="after")
    def validate_contract(self) -> "StockDailyWindowQueryPayload":
        try:
            start = date.fromisoformat(self.start_date)
            end = date.fromisoformat(self.end_date)
        except ValueError as exc:
            raise ValueError("start_date/end_date 必须是 YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("start_date 不能晚于 end_date")
        if self.universe == "codes" and not self.codes:
            raise ValueError("universe=codes 时 codes 不能为空")
        if self.universe == "all_a" and self.codes:
            raise ValueError("universe=all_a 时 codes 必须为空")
        return self


class StockDailyWindowCoverage(BaseModel):
    code: str
    expected_rows: int
    actual_rows: int
    missing_rows: int
    missing_trade_dates: list[str] = Field(default_factory=list)
    complete: bool


class StockDailyWindowMeta(BaseModel):
    data_version: str
    dataset_version: str = ""
    total_rows: int
    returned_rows: int
    complete: bool
    truncated: Literal[False] = False
    page_complete: bool
    request_complete: bool
    delivery_complete: bool
    next_cursor: str | None = None
    universe_kind: Literal["codes", "all_a"]
    universe_size: int
    page_size: int
    coverage: list[StockDailyWindowCoverage] = Field(default_factory=list)


class StockDailyWindowQueryResponse(BaseModel):
    items: list[StockQuoteItem] = Field(default_factory=list)
    meta: StockDailyWindowMeta
