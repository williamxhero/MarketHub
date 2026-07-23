from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
                }
            ]
        }
    )

    codes: list[str] = Field(min_length=1, description="股票代码列表，推荐每批 100 至 200 只。")
    freq: str = Field(default="1d", description="行情频率，例如 1m 或 1d。")
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
