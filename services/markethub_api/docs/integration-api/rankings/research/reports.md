# /api/rankings/research/reports

`GET` 返回研报热度排行。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。

## 返回类型

顶层返回 `list[RankingResearchReportItem]`。

## 返回字段

- `trade_date`（`str`）：交易日期。
- `code`（`str`）：股票代码。
- `name`（`str`）：股票简称。
- `institution`（`str`）：发布研报的机构。
- `rating`（`str`）：评级。
- `target_price`（`float | None`）：目标价。
- `title`（`str`）：研报标题。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
