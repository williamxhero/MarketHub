# /api/stocks/reference/bse-code-mappings

`GET` 返回北交所证券代码映射关系。

## 查询参数

- `old_code`（类型：`str`）：旧证券代码筛选。
- `new_code`（类型：`str`）：新证券代码筛选。
- `status`（类型：`str`）：代码映射状态筛选。

## 返回类型

顶层返回 `list[BSECodeMappingItem]`。

## 返回字段

- `old_code`（`str`）：旧代码。
- `new_code`（`str`）：新代码。
- `effective_date`（`str`）：生效日期。
- `status`（`str`）：代码映射状态，如生效或停用。
