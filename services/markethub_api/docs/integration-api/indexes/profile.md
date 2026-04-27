# /api/indexes/{index_code}/profile

`GET` 返回单个指数的基础资料。

## 路径参数

- `index_code`（类型：`str`）：指数代码。

## 返回类型

顶层返回 `IndexCatalogItem`；查不到对应记录时返回空对象 `{}`。

## 返回字段

- `index_code`（`str`）：指数代码。
- `index_name`（`str`）：指数名称。
- `category`（`str`）：分类。
- `market`（`str`）：指数覆盖市场。
- `publisher`（`str`）：编制方。
- `list_date`（`str`）：上市日期。
- `status`（`str`）：指数状态。

## 补充说明

- 查不到对应记录时返回空对象 `{}`。
