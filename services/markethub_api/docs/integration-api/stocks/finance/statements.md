# /api/stocks/finance/statements

`GET` 返回股票财务报表数据。

## 查询参数

- `code`（类型：`str`）：单个股票代码；与 `codes` 至少传一个。
- `codes`（类型：`str`）：多个股票代码，逗号分隔；与 `code` 至少传一个。
- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。
- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。
- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。
- `report_type`（类型：`str`；默认：`income_statement`）：报表类型，可选 `income_statement`、`balance_sheet`、`cash_flow_statement`。

## 返回类型

顶层返回 `list[StockFinancialStatementItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `report_period`（`str`）：报告期。
- `report_type`（`str`）：报告类型。
- `announce_date`（`str`）：公告日期。
- `revenue`（`float | None`）：营业收入。
- `operating_profit`（`float | None`）：营业利润。
- `total_profit`（`float | None`）：利润总额。
- `net_profit`（`float | None`）：净利润。
- `total_assets`（`float | None`）：总资产。
- `total_liabilities`（`float | None`）：总负债。
- `equity`（`float | None`）：权益规模。

## 补充说明

- `code` 与 `codes` 至少需要传一个。
- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。
