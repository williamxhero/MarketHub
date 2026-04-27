# /api/stocks/{code}/ownership/shareholders/top10-float

`GET` 返回单只股票的前十大流通股东。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。
- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。
- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[ShareholderTop10Item]`。

## 返回字段

- `code`（`str`）：股票代码。
- `report_period`（`str`）：报告期。
- `rank`（`int | None`）：排名。
- `shareholder_name`（`str`）：股东名称。
- `holding_volume`（`float | None`）：持有数量。
- `holding_ratio`（`float | None`）：持有占比，单位 %。
- `change_volume`（`float | None`）：相对上一报告期的持股变动数量。

## 补充说明

- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。
