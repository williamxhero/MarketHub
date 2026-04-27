# /api/markets/trading/sessions

`GET` 返回交易时段定义。

## 查询参数

- `codes`（类型：`str`）：股票代码列表，逗号分隔；不传时返回默认交易时段定义。

## 返回类型

顶层返回 `list[TradingSessionItem]`。

## 返回字段

- `code`（`str`）：证券代码。
- `session_name`（`str`）：交易时段名称，如集合竞价、连续竞价。
- `start_time`（`str`）：开始时间。
- `end_time`（`str`）：结束时间。
- `timezone`（`str`）：时区标识。
