# /api/stocks/{code}/ownership/pledges/details

`GET` 返回单只股票的股权质押明细。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `status`（类型：`str`）：质押状态筛选。

## 返回类型

顶层返回 `list[PledgeDetailItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `holder_name`（`str`）：持有人名称。
- `start_date`（`str`）：start日期。
- `end_date`（`str`）：END日期。
- `pledge_volume`（`float | None`）：质押数量。
- `pledge_ratio`（`float | None`）：质押比例，单位 %。
- `status`（`str`）：质押状态。
