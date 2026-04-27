# /api/stocks/{code}/factors/adj

`GET` 返回单只股票的复权因子序列。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `base_date`（类型：`str`）：参数说明见接口上下文。

## 返回类型

顶层返回 `list[AdjFactorItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `adj_factor`（`float | None`）：复权因子。
