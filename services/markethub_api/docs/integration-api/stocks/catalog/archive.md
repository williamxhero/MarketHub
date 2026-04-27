# /api/stocks/catalog/archive

`GET` 返回指定交易日的股票归档清单。

## 查询参数

- `trade_date`（类型：`str`）：归档交易日，格式 `YYYY-MM-DD`。
- `code`（类型：`str`）：股票代码。
- `name`（类型：`str`）：股票简称关键字。
- `industry`（类型：`str`）：所属行业筛选。
- `area`（类型：`str`）：所属地域筛选。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。

## 返回类型

顶层返回 `list[StockArchiveItem]`。

## 返回字段

- `trade_date`（`str`）：交易日期。
- `code`（`str`）：股票代码。
- `name`（`str`）：名称。
- `exchange`（`str`）：归档时点对应的交易所。
- `market`（`str`）：归档时点对应的所属市场板块。
- `list_status`（`str`）：归档时点对应的上市状态。
- `industry`（`str`）：所属行业。
- `area`（`str`）：所属地域。
