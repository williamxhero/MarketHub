# /api/stocks/{code}/profile

`GET` 返回单只股票的公司概况。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 返回类型

顶层返回 `StockProfileItem`；查不到对应记录时返回空对象 `{}`。

## 返回字段

- `code`（`str`）：股票代码。
- `company_name`（`str`）：公司简称或工商登记简称。
- `full_name`（`str`）：公司全称。
- `chairman`（`str`）：董事长。
- `manager`（`str`）：总经理或经营负责人。
- `website`（`str`）：公司网站。
- `employee_count`（`int | None`）：员工人数。
- `main_business`（`str`）：主营业务描述。
- `office`（`str`）：办公地址。

## 补充说明

- 查不到对应记录时返回空对象 `{}`。
