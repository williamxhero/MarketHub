# /api/markets/events/news

`GET` 返回统一新闻事件流。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`，必填。
- `announcement_date`（类型：`str`）：按公告时间的业务日期筛选，格式 `YYYY-MM-DD`。
- `crawl_date`（类型：`str`）：按爬取时间的采集日期筛选，格式 `YYYY-MM-DD`。
- `stock_code`（类型：`str`）：股票代码筛选，基于 `related_stock_codes` 匹配。
- `event_type`（类型：`str`）：事件类型筛选，例如 `公告`、`个股研报`、`重要新闻`。
- `min_importance_score`（类型：`int`；范围：`1-5`）：重要度下限筛选。
- `sort_by`（类型：`str`；默认：`announcement_time`）：排序时间口径，只支持 `announcement_time` 或 `crawl_time`，均按倒序返回。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回事件上限。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量。
- `include_sources`（类型：`bool`；默认：`false`）：是否回带来源映射。
- `include_content_text`（类型：`bool`；默认：`false`）：是否返回正文纯文本。

## 返回类型

顶层返回 `NewsEventQueryResult`。

## 返回字段

- `events`（`list[NewsEventItem]`）：事件列表。
- `events[].event_id`（`str`）：统一事件主键。
- `events[].trade_date`（`str`）：归属交易日。
- `events[].announcement_time`（`str`）：公告时间，属于业务时间；优先使用源站给出的公告日期或公告发布时间。
- `events[].crawl_time`（`str`）：爬取时间，属于采集时间；只映射系统显式记录的首次抓取时间，不会拿 `created_at`、`processed_at` 这类模糊字段冒充。
- `events[].session_tag`（`str`）：交易时段标签，可能是 `隔夜`、`盘前`、`盘中`、`盘后`。
- `events[].event_type`（`str`）：统一事件类型。
- `events[].title`（`str`）：事件标题。
- `events[].summary`（`str`）：统一摘要。
- `events[].content_text`（`str`）：正文纯文本，仅在 `include_content_text=true` 时返回。
- `events[].importance_score`（`int`）：重要度分值。
- `events[].sentiment`（`str`）：情绪标签。
- `events[].source_name`（`str`）：主来源名称。
- `events[].primary_detail_url`（`str`）：主来源详情链接。
- `events[].related_stock_codes`（`list[str]`）：关联股票代码列表。
- `events[].related_stock_names`（`list[str]`）：关联股票名称列表。
- `events[].related_board_codes`（`list[str]`）：关联板块代码列表。
- `events[].related_board_names`（`list[str]`）：关联板块名称列表。
- `events[].topic_tags`（`list[str]`）：题材标签列表。
- `events[].mentioned_stock_codes`（`list[str]`）：正文提及股票代码列表。
- `events[].mentioned_stock_names`（`list[str]`）：正文提及股票名称列表。
- `events[].mentioned_board_names`（`list[str]`）：正文提及板块名称列表。
- `events[].sources`（`list[NewsEventSourceItem]`）：来源映射列表，仅在 `include_sources=true` 时返回。
- `events[].sources[].source_table`（`str`）：来源表名。
- `events[].sources[].source_record_id`（`str`）：来源记录主键。
- `events[].sources[].source_name`（`str`）：来源名称。
- `events[].sources[].source_type`（`str`）：来源类型。
- `events[].sources[].detail_url`（`str`）：来源详情链接。
- `events[].sources[].announcement_time`（`str`）：来源记录上的公告时间，属于业务时间。
- `events[].sources[].crawl_time`（`str`）：来源记录上的爬取时间，属于采集时间。

## 补充说明

- 正式主查询入口优先读取 `fact.news_event_agent_view`，不会直接把 raw 新闻来源表暴露给用户。
- `include_sources=true` 时，服务会额外读取 `fact.news_event_source`，并按 `event_id` 聚合回每条事件。
- 公告时间通常来自源站；爬取时间来自本系统，两者不是同一件事。
- 在 `cninfo` 这类来源里，公告时间可能晚于爬取时间；这属于正常业务事实，不应视为脏数据。
- 如果要判断“今天抓到了什么”，应使用 `crawl_date` / `crawl_time` 口径。
- 如果要判断“公告业务日期是什么”，应使用 `announcement_date` / `announcement_time` 口径。
- `published_at` 已从对外响应移除，不再作为模糊时间字段保留。
- 默认排序固定为 `announcement_time DESC, importance_score DESC, event_id DESC`；传 `sort_by=crawl_time` 后改为按 `crawl_time DESC` 排序。
- `crawl_time` 只接受底层显式的首次抓取时间字段；如果底层对象没有提供这类字段，响应会返回空字符串，而不是退化成 `created_at` 或 `processed_at`。
- `stock_code` 过滤只基于 `related_stock_codes`，不会回到 raw 表重新拼接。
- 默认轻量返回不包含 `content_text`；只有显式传 `include_content_text=true` 才会回带正文。

## 查询示例

- `GET /api/markets/events/news?trade_date=2026-04-10`
- `GET /api/markets/events/news?trade_date=2026-04-10&announcement_date=2026-04-10&stock_code=600519&min_importance_score=4`
- `GET /api/markets/events/news?trade_date=2026-04-12&crawl_date=2026-04-12&sort_by=crawl_time&event_type=公告&include_sources=true`

## 响应示例一：公告时间晚于爬取时间

```json
{
  "events": [
    {
      "event_id": "evt-cninfo-1001",
      "trade_date": "2026-04-12",
      "announcement_time": "2026-04-13",
      "crawl_time": "2026-04-12 22:41:00",
      "session_tag": "盘后",
      "event_type": "公告",
      "title": "测试公告",
      "summary": "源站公告日期晚于系统抓取时间。",
      "importance_score": 4,
      "sentiment": "中性",
      "source_name": "巨潮资讯",
      "primary_detail_url": "https://example.com/detail",
      "related_stock_codes": ["600000"],
      "related_stock_names": ["浦发银行"],
      "related_board_codes": ["BK001"],
      "related_board_names": ["银行"],
      "topic_tags": ["银行"],
      "mentioned_stock_codes": ["600000"],
      "mentioned_stock_names": ["浦发银行"],
      "mentioned_board_names": ["银行"],
      "sources": [
        {
          "source_table": "cninfo_disclosure_item",
          "source_record_id": "1001",
          "source_name": "巨潮资讯",
          "source_type": "公告",
          "detail_url": "https://example.com/detail",
          "announcement_time": "2026-04-13",
          "crawl_time": "2026-04-12 22:41:00"
        }
      ]
    }
  ]
}
```

## 响应示例二：按爬取时间查询今天抓到的数据

请求：

```text
GET /api/markets/events/news?trade_date=2026-04-12&crawl_date=2026-04-12&sort_by=crawl_time&event_type=公告
```

响应：

```json
{
  "events": [
    {
      "event_id": "evt-cninfo-1001",
      "trade_date": "2026-04-12",
      "announcement_time": "2026-04-13",
      "crawl_time": "2026-04-12 22:41:00",
      "session_tag": "盘后",
      "event_type": "公告",
      "title": "测试公告",
      "summary": "今天抓到的公告。",
      "importance_score": 4,
      "sentiment": "中性",
      "source_name": "巨潮资讯",
      "primary_detail_url": "https://example.com/detail",
      "related_stock_codes": ["600000"],
      "related_stock_names": ["浦发银行"],
      "related_board_codes": [],
      "related_board_names": [],
      "topic_tags": [],
      "mentioned_stock_codes": [],
      "mentioned_stock_names": [],
      "mentioned_board_names": []
    }
  ]
}
```
