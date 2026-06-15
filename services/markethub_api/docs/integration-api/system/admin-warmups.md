# Admin Warmups

这里说明 MarketHub 管理后台里“大范围预热”相关的页面入口、接口契约和状态语义。

## 页面入口

- `/admin` 的 `Warmups` 区域负责创建后台预热任务、轮询进度和展示最近历史。
- `/docs/system/capability-update-policy` 说明它和 `run-due-async` 的职责边界。

## 适用场景

- 需要人工选择一批 capability 做集中预热。
- 预热范围较大，不希望把整个过程挂在单个 HTTP 请求里等待。
- 需要在页面里持续查看总进度、逐项结果和失败原因。

## 接口列表

### `POST /api/admin/warmups`

创建一个后台 warmup 任务，并立即交给后台异步执行。

请求体：

```json
{
  "capability_ids": [
    "stocks.quotes.daily",
    "stocks.quotes.daily_snapshot"
  ]
}
```

约束：

- `capability_ids` 必须是显式 capability 列表，不能为空。
- 服务会把派生 capability 归一到它的配置根 capability。
- 同一时刻只允许一个 `queued` 或 `running` 的 warmup 任务；若已有活跃任务，会直接报错。

返回值：

- 返回新建任务的摘要，结构与 `GET /api/admin/warmups/{task_id}` 的顶层字段保持一致。
- 创建成功后，真正执行过程在后台继续进行；调用方不需要保持连接。

### `GET /api/admin/warmups`

返回最近 warmup 任务列表，按创建时间倒序排列。

查询参数：

- `limit`：返回条数，默认 `50`，范围 `1-200`。

返回字段：

- `task_id`：任务 ID。
- `status`：任务状态，取值见下文“任务状态”。
- `created_at`：创建时间。
- `started_at`：开始执行时间；未开始时为空字符串。
- `finished_at`：结束时间；未结束时为空字符串。
- `total_count`：总 capability 数量。
- `finished_count`：已结束条目数。
- `success_count`：成功条目数。
- `failed_count`：失败条目数。
- `skipped_count`：跳过条目数。
- `current_capability_id`：当前正在执行的 capability；若还没开始执行，则指向下一个排队条目。
- `error_message`：任务级错误信息；成功时为空字符串。

### `GET /api/admin/warmups/{task_id}`

返回单个 warmup 任务的摘要和逐项明细。

在顶层摘要字段之外，额外返回：

- `items`：按执行顺序排列的明细列表。

每个 `item` 包含：

- `task_id`：所属任务 ID。
- `position`：在本次任务中的顺序，从 `1` 开始。
- `capability_id`：本条预热对应的 capability。
- `status`：条目状态，取值见下文“条目状态”。
- `capture_run_id`：底层 capture 运行 ID；没有时为 `null`。
- `started_at`：条目开始时间；未开始时为空字符串。
- `finished_at`：条目结束时间；未结束时为空字符串。
- `row_count`：本次 capture 产出的记录数。
- `coverage_count`：本次 capture 覆盖计数。
- `error_message`：条目错误信息；成功或跳过时为空字符串。
- `detail_json`：底层 capture 的附加明细。

## 任务状态

- `queued`：任务已创建，尚未开始。
- `running`：任务正在串行执行各个 capability。
- `success`：全部条目处理完成，且没有失败条目。
- `failed`：至少一个条目失败，任务提前结束。

## 条目状态

- `queued`：条目尚未开始。
- `running`：条目正在执行。
- `success`：条目执行成功。
- `failed`：条目执行失败。
- `skipped`：底层 capture 返回 `skipped`，通常表示当前策略下无需重复写入。

## 执行语义

- warmup 任务在服务进程内串行调用现有 `run_capture()`，不额外引入新的 provider 执行链路。
- 任务和明细会持久化到数据库，页面刷新后仍可继续查看。
- 若中途失败，任务状态会标记为 `failed`，并保留已完成部分的结果。

## 与定时更新的边界

- `POST /api/admin/capture/run-due-async`：按 capture policy 做到期检查，属于日常定时更新入口。
- `POST /api/admin/warmups`：按显式 capability 列表做人工批量预热，属于一次性后台任务入口。

两者都可能调用底层 capture，但触发方式、调度职责和观察方式不同，不应混用。
