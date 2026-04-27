# /api/stocks/{code}/finance/express

`GET` 返回单只股票的业绩快报记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。
- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。
- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[ExpressItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `report_period`（`str`）：报告期。
- `announce_date`（`str`）：公告日期。
- `revenue`（`float | None`）：营业收入。
- `operating_profit`（`float | None`）：营业利润。
- `total_profit`（`float | None`）：利润总额。
- `net_profit`（`float | None`）：净利润。
- `eps`（`float | None`）：每股收益。
- `roe`（`float | None`）：净资产收益率，单位 %。

## 补充说明

- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。
