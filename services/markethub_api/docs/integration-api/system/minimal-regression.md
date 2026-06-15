# 最小回归清单

每次恢复、发布或部署前，至少验证：

- `/api/health`
- `/api/openapi.json`
- `/docs`
- `/doc-view/`
- `/docs/all`
- `/doc-view/all`
- `/docs/system/admin-warmups`
- `/docs/sync-workflow`
- `/docs/system/runbook`
- `/docs/indexes`
- `/docs/indexes/members`
- `/docs/boards/quotes`
- `/docs/markets/calendar/trading`
- `/docs/stocks/profile/basic`
- `services/markethub_api/tests/test_smoke.py`

如果接入了真实数据库和真实 provider，还应补充验证：

- `/api/stocks/catalog`
- `/api/stocks/{code}/profile/basic`
- `/api/stocks/quotes`
- `/api/boards/catalog`
- `/api/boards/quotes`
- `/api/boards/{board_code}/members`
- `/api/indexes/catalog`
- `/api/indexes/quotes`
- `/api/indexes/{index_code}/members`
- `/api/markets/calendar/trading`

## 当前特别关注

- 指数接口已经纳入最小回归范围。
- `members` 需要验证请求日和实际返回权重日是否符合预期。
