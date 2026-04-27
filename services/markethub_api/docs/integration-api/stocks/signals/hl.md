# /api/stocks/{code}/signals/hl

`GET` 返回单只股票的新高新低信号。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[HLSignalItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `first_extreme`（`str`）：首次触发的新高或新低类型。
- `high_time`（`str`）：触发新高的时间。
- `low_time`（`str`）：触发新低的时间。
- `signal`（`str`）：新高新低信号类型。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
