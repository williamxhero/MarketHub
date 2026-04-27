# /api/stocks/{code}/profile/management-rewards

`GET` 返回单只股票的高管薪酬记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[ManagementRewardItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `ann_date`（`str`）：公告日期。
- `name`（`str`）：名称。
- `title`（`str`）：职务。
- `reward_amount`（`float | None`）：薪酬金额。
- `hold_amount`（`float | None`）：持股数量。
