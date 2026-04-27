# /api/stocks/{code}/finance/main-business

`GET` 返回单只股票的主营业务构成。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。
- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。
- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。
- `classification`（类型：`str`；默认：`industry`）：主营业务分类口径，默认 `industry`。

## 返回类型

顶层返回 `list[MainBusinessItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `report_period`（`str`）：报告期。
- `classification`（`str`）：主营业务分类口径，如行业、地区或产品。
- `segment_name`（`str`）：分部名称。
- `revenue`（`float | None`）：营业收入。
- `cost`（`float | None`）：成本。
- `profit`（`float | None`）：利润。
- `revenue_ratio`（`float | None`）：收入占比，单位 %。

## 补充说明

- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。
