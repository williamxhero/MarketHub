# /api/boards/catalog

`GET` 返回板块目录清单。

## 查询参数

- `category`（类型：`str`）：分类筛选。
- `market`（类型：`str`）：市场筛选。
- `status`（类型：`str`）：状态筛选。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。

## 返回类型

顶层返回 `list[BoardCatalogItem]`。

## 返回字段

- `board_code`（`str`）：板块代码。
- `board_name`（`str`）：板块名称。
- `category`（`str`）：分类。
- `market`（`str`）：板块所属市场，默认 A 股口径。
- `status`（`str`）：板块状态。
