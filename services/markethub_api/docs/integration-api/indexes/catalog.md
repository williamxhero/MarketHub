# /api/indexes/catalog

`GET` 返回指数目录清单。

## 查询参数

- `category`（类型：`str`）：分类筛选。
- `market`（类型：`str`）：市场筛选。
- `publisher`（类型：`str`）：编制方筛选。
- `status`（类型：`str`）：状态筛选。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。

## 返回类型

顶层返回 `list[IndexCatalogItem]`。

## 返回字段

- `index_code`（`str`）：指数代码。
- `index_name`（`str`）：指数名称。
- `category`（`str`）：分类。
- `market`（`str`）：指数覆盖市场。
- `publisher`（`str`）：编制方。
- `list_date`（`str`）：上市日期。
- `status`（`str`）：指数状态。
