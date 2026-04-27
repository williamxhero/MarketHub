# /api/boards/{board_code}/indicators/money-flow

`GET` 返回单个板块的资金流指标。

## 路径参数

- `board_code`（类型：`str`）：板块代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `scope`（类型：`str`；默认：`board`）：资金流统计口径，可选 `board`、`industry`。

## 返回类型

顶层返回 `list[BoardMoneyFlowItem]`。

## 返回字段

- `board_code`（`str`）：板块代码。
- `trade_date`（`str`）：交易日期。
- `scope`（`str`）：统计口径。
- `inflow`（`float | None`）：流入金额。
- `outflow`（`float | None`）：流出金额。
- `net_inflow`（`float | None`）：净流入金额。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 和 `end_date` 用于区间筛选。
- 如果需求是“按 `trade_date` 直接拿全市场板块资金流快照”，请改用 `GET /api/boards/indicators/money-flow`，不要再循环传 `board_code`。
