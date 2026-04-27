# /api/stocks/{code}/research/reports

`GET` 返回单只股票的研报记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `report_date`（类型：`str`）：研报日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[ResearchReportItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `report_date`（`str`）：研报日期。
- `institution`（`str`）：发布研报的机构。
- `analyst`（`str`）：分析师。
- `rating`（`str`）：评级。
- `target_price`（`float | None`）：目标价。
- `title`（`str`）：研报标题。
