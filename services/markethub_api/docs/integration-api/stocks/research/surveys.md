# /api/stocks/{code}/research/surveys

`GET` 返回单只股票的调研记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `survey_date`（类型：`str`）：调研日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[SurveyItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `survey_date`（`str`）：调研日期。
- `org_name`（`str`）：调研机构名称。
- `survey_method`（`str`）：调研方式。
- `topic`（`str`）：调研主题。
- `announcement_date`（`str`）：调研结果公告日期。
