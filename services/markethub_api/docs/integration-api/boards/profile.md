# /api/boards/{board_code}/profile

`GET` 返回单个板块的基础资料。

## 路径参数

- `board_code`（类型：`str`）：板块代码。

## 返回类型

顶层返回 `BoardCatalogItem`；查不到对应记录时返回空对象 `{}`。

## 返回字段

- `board_code`（`str`）：板块代码。
- `board_name`（`str`）：板块名称。
- `category`（`str`）：分类。
- `market`（`str`）：板块所属市场，默认 A 股口径。
- `status`（`str`）：板块状态。

## 补充说明

- 查不到对应记录时返回空对象 `{}`。
