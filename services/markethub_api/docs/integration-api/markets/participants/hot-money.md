# /api/markets/participants/hot-money

`GET` 返回游资营业部榜单。

## 查询参数

- `name`（类型：`str`）：游资或营业部名称关键字。
- `tag`（类型：`str`）：游资标签筛选。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。

## 返回类型

顶层返回 `list[HotMoneyProfileItem]`。

## 返回字段

- `name`（`str`）：游资或营业部名称。
- `alias`（`str`）：别名。
- `tag`（`str`）：标签。
- `style`（`str`）：风格标签。
