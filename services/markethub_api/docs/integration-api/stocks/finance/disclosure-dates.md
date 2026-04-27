# /api/stocks/{code}/finance/disclosure-dates

`GET` 返回单只股票的财报披露日期记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。
- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。
- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[DisclosureDateItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `report_period`（`str`）：报告期。
- `plan_date`（`str`）：计划披露日期。
- `actual_date`（`str`）：实际披露日期。
- `change_reason`（`str`）：变更原因。

## 补充说明

- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。
