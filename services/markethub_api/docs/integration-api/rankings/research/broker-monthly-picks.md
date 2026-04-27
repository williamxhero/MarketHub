# /api/rankings/research/broker-monthly-picks

`GET` 返回券商月度金股排行。

## 查询参数

- `trade_month`（类型：`str`）：月份筛选，格式 `YYYY-MM`。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。

## 返回类型

顶层返回 `list[RankingBrokerPickItem]`。

## 返回字段

- `trade_month`（`str`）：月份，格式 `YYYY-MM`。
- `code`（`str`）：股票代码。
- `name`（`str`）：股票简称。
- `institution`（`str`）：券商机构名称。
- `rank`（`int | None`）：排名。
- `recommend_count`（`int | None`）：被推荐次数。
- `rating`（`str`）：评级。
