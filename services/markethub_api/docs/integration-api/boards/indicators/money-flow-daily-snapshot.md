# /api/boards/indicators/money-flow

`GET` 返回指定交易日的全市场板块资金流快照。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `scope`（类型：`str`；默认：`board`）：资金流统计口径，可选 `board`、`industry`。
- `fields`（类型：`str`）：可选返回字段列表，逗号分隔。
- `limit`（类型：`int`；默认：`10000`；最小值：`1`；最大值：`10000`）：单次返回的最大记录数。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量。

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

- 这个入口用于“按交易日读取全市场板块资金流快照”，不需要传 `board_code`。
- 当前由 `static_core` 承担本地快照能力，后续可通过 Capability Matrix 接入补源。
- 如需分页读取，可配合 `limit` 和 `offset` 使用。
