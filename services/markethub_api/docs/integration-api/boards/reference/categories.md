# /api/boards/reference/categories

`GET` 返回板块分类目录。

## 查询参数

- `parent_code`（类型：`str`）：父级分类代码。
- `level`（类型：`int | None`；允许空值；最小值：`1`）：分类层级筛选，从 `1` 开始。

## 返回类型

顶层返回 `list[BoardCategoryItem]`。

## 返回字段

- `category_code`（`str`）：分类代码。
- `category_name`（`str`）：分类名称。
- `parent_code`（`str`）：父级分类代码。
- `level`（`int | None`）：层级。
- `sort_order`（`int | None`）：排序值。
