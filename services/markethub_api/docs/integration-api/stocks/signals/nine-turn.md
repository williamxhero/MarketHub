# /api/stocks/{code}/signals/nine-turn

`GET` 返回单只股票的神奇九转信号。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `freq`（类型：`str`；默认：`daily`）：神奇九转计算周期。
- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[NineTurnItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_time`（`str`）：时间点；日频返回交易日，分钟级返回具体时间。
- `freq`（`str`）：数据频率。
- `setup_index`（`int | None`）：九转 setup 序号。
- `countdown_index`（`int | None`）：九转 countdown 序号。
- `signal`（`str`）：九转信号类型。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
