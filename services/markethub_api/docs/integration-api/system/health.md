# /api/health

`GET` 返回服务健康状态。

## 返回类型

顶层返回 `HealthPayload`。

## 返回字段

- `service`（`str`）：服务标识。
- `status`（`str`）：健康状态；正常情况下为 `ok`。
- `version`（`str`）：当前服务版本。
- `updated_at`（`str`）：健康检查文案中的更新时间。
