# /api/stocks/{code}/finance/forecasts

`GET` 返回单只股票的业绩预告记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。
- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。
- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[ForecastItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `report_period`（`str`）：报告期。
- `forecast_type`（`str`）：业绩预告类型。
- `forecast_summary`（`str`）：业绩预告摘要。
- `net_profit_min`（`float | None`）：净利润下限。
- `net_profit_max`（`float | None`）：净利润上限。
- `pct_chg_min`（`float | None`）：业绩变动幅度下限，单位 %。
- `pct_chg_max`（`float | None`）：业绩变动幅度上限，单位 %。

## 补充说明

- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。
