# /api/stocks/finance/indicators

`GET` 返回股票财务指标数据。

## 查询参数

- `code`（类型：`str`）：单个股票代码；与 `codes` 至少传一个。
- `codes`（类型：`str`）：多个股票代码，逗号分隔；与 `code` 至少传一个。
- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。
- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。
- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[StockFinanceIndicatorItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `report_period`（`str`）：报告期。
- `roe`（`float | None`）：净资产收益率，单位 %。
- `roa`（`float | None`）：总资产收益率，单位 %。
- `gross_margin`（`float | None`）：毛利率，单位 %。
- `net_margin`（`float | None`）：净利率，单位 %。
- `asset_turnover`（`float | None`）：总资产周转率。
- `current_ratio`（`float | None`）：流动比率。
- `debt_to_asset`（`float | None`）：资产负债率，单位 %。

## 补充说明

- `code` 与 `codes` 至少需要传一个。
- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。
