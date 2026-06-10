from __future__ import annotations

from datetime import datetime
import inspect
import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from runtime_paths import configure_python_path


configure_python_path()

from app import app
from docs_all import collect_all_doc_items
from docs_paths import to_public_doc_path
from fastapi import HTTPException
from quotemux.infra.cache import store as cache_store
from quotemux.capabilities import list_public_api_bindings
from quotemux.config_runtime.runtime import reset_config_runtime_cache
from quotemux.runtime_core.registry import get_default_source_registry
from quotemux.models import BoardMoneyFlowItem, BoardQuoteItem, IndexQuoteItem, NewsEventItem, NewsEventQueryResult, NewsEventSourceItem, StockBasicInfo, StockQuoteCodeSummary, StockQuoteItem, StockQuotesMeta, StockQuotesQueryResult
from quotemux.source_packages.registry import refresh_default_source_package_registry
from quotemux.store.postgres import CacheScope, _coverage_mode_for_capability, _payload_matches_scope, _request_scope_fields_for_capability
from quotemux_packages.tushare import stocks as tushare_stocks
from services import boards
from services import indexes
from services import news
from services import stocks


client = TestClient(app)
DOCS_ROOT = SERVICE_ROOT / 'docs' / 'integration-api'
PLACEHOLDER_TEXTS = (
    'returns data for this endpoint',
    'Returns the standard MarketHub payload for this API route.',
    '返回该接口的标准 MarketHub 响应数据。',
)


@pytest.fixture(autouse=True)
def isolate_quotemux_runtime(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("QUOTEMUX_RUNTIME_ROOT", str(tmp_path / "quotemux-runtime"))
    reset_config_runtime_cache()
    refresh_default_source_package_registry()
    get_default_source_registry.cache_clear()
    yield
    reset_config_runtime_cache()
    refresh_default_source_package_registry()
    get_default_source_registry.cache_clear()


def extract_section_lines(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    header = f'## {title}'
    collected: list[str] = []
    in_section = False
    for raw_line in lines:
        line = raw_line.rstrip()
        if line == header:
            in_section = True
            continue
        if in_section and line.startswith('## '):
            break
        if in_section:
            collected.append(line)
    return collected


def test_health_endpoint() -> None:
    response = client.get('/api/health')
    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'ok'
    assert payload['updated_at'] != '2026-03-24 00:00:00'
    datetime.strptime(payload['updated_at'], '%Y-%m-%d %H:%M:%S')


def test_public_api_routes_are_all_bound_to_capabilities() -> None:
    ignored_paths = {
        "/api/console/config",
        "/api/diagnostics/connections",
        "/api/health",
        "/api/openapi",
        "/api/openapi.json",
    }
    route_paths: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = tuple(sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}))
        if path.startswith("/api/") and not path.startswith("/api/admin") and path not in ignored_paths and methods != ():
            route_paths.append(path)

    binding_paths = [item.api_path for item in list_public_api_bindings()]

    assert len(route_paths) == 79
    assert len(binding_paths) == 79
    assert sorted(route_paths) == sorted(binding_paths)


def test_docs_root_endpoint() -> None:
    response = client.get('/docs')
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == ''
    assert isinstance(payload['content'], str)


def test_doc_view_search_page() -> None:
    response = client.get('/doc-view/search')
    assert response.status_code == 200
    assert 'text/html' in response.headers['content-type']


def test_doc_view_root_converts_absolute_code_urls() -> None:
    response = client.get('/doc-view/')
    assert response.status_code == 200
    html = response.text
    assert 'href="/api/health"' in html
    assert '<code>/api/health</code></a>' in html


def test_openapi_endpoint() -> None:
    response = client.get('/api/openapi.json')
    assert response.status_code == 200
    payload = response.json()
    assert payload['openapi'].startswith('3.')


def test_stock_quotes_limit_has_no_artificial_upper_bound(monkeypatch) -> None:
    monkeypatch.setattr(stocks, 'get_quotes', lambda *args: [])

    response = client.get('/api/stocks/quotes', params={'codes': '603158', 'limit': 50000})

    assert response.status_code == 200
    assert response.json() == []


def test_connection_diagnostics_endpoint() -> None:
    with TestClient(app) as started_client:
        response = started_client.get('/api/diagnostics/connections')
    assert response.status_code == 200
    payload = response.json()
    assert 'provider_runtime' in payload
    assert 'store_db_pool' in payload
    assert 'data_thread_pool' in payload
    assert 'sync_thread_pool' in payload
    assert 'providers' in payload['provider_runtime']
    assert payload['data_thread_pool']['total_tokens'] == 64
    assert payload['sync_thread_pool']['total_tokens'] == 100


def test_critical_entry_routes_are_async() -> None:
    critical_paths = {
        '/',
        '/favicon.ico',
        '/api/health',
        '/api/diagnostics/connections',
        '/docs',
        '/docs/{doc_path:path}',
        '/docs/search',
        '/doc-view',
        '/doc-view/{doc_path:path}',
        '/doc-view/search',
    }
    endpoints = {
        route.path: route.endpoint
        for route in app.router.routes
        if hasattr(route, 'path') and route.path in critical_paths
    }
    assert set(endpoints) == critical_paths
    for endpoint in endpoints.values():
        assert inspect.iscoroutinefunction(endpoint)


def test_public_api_routes_are_async_except_internal_openapi_and_reindex() -> None:
    excluded_paths = {'/api/openapi.json', '/api/admin/docs/reindex'}
    api_routes = {
        route.path: route.endpoint
        for route in app.router.routes
        if hasattr(route, 'path') and route.path.startswith('/api/') and route.path not in excluded_paths
    }
    assert '/api/stocks/catalog' in api_routes
    assert '/api/indexes/quotes' in api_routes
    assert '/api/markets/calendar/trading' in api_routes
    assert '/api/markets/events/news' in api_routes
    assert '/api/rankings/research/reports' in api_routes
    for endpoint in api_routes.values():
        assert inspect.iscoroutinefunction(endpoint)


def test_stock_daily_snapshot_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        stocks,
        'get_market_daily_snapshot',
        lambda trade_date, limit, offset: [
            StockQuoteItem(code='600000', trade_time='2025-01-02', freq='1d', open=10.0, high=10.5, low=9.8, close=10.2, pre_close=10.0, change=0.2, pct_chg=2.0, volume=12345.0, amount=67890.0, adjust='none')
        ],
    )
    response = client.get('/api/stocks/quotes/daily-snapshot', params={'trade_date': '2025-01-02'})
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]['code'] == '600000'
    assert payload[0]['freq'] == '1d'
    assert payload[0]['pre_close'] == 10.0


def test_stock_catalog_endpoint_defaults_to_full_market_limit(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get_catalog(codes: str, name: str, exchange: str, list_status: str, include_delisted: bool, limit: int, offset: int) -> list[StockBasicInfo]:
        captured['limit'] = limit
        captured['offset'] = offset
        return [StockBasicInfo(code='600000', name='浦发银行', exchange='SSE', market='主板', list_status='listed', list_date='1999-11-10', delist_date='')]

    monkeypatch.setattr(stocks, 'get_catalog', fake_get_catalog)
    response = client.get('/api/stocks/catalog')

    assert response.status_code == 200
    assert captured['limit'] == 5000
    assert captured['offset'] == 0


def test_stock_catalog_cache_scope_uses_catalog_query_fields() -> None:
    fields = _request_scope_fields_for_capability('stocks.catalog')
    scope = CacheScope(
        scope_identity='',
        criteria={'codes': '600000', 'name': '', 'exchange': '', 'list_status': '', 'include_delisted': True, 'limit': 5, 'offset': 0},
        time_start=datetime(2026, 1, 1),
        time_end=datetime(2026, 1, 1),
    )

    assert fields == ('codes', 'name', 'exchange', 'list_status', 'include_delisted', 'limit', 'offset')
    assert _coverage_mode_for_capability('stocks.catalog') == 'snapshot'
    assert _payload_matches_scope({'code': '600000', 'name': '浦发银行'}, scope)
    assert not _payload_matches_scope({'code': '000001', 'name': '平安银行'}, scope)


def _stock_30m_rows(code: str, trade_dates: list[str]) -> list[dict[str, object]]:
    bar_times = ['09:30:00', '10:00:00', '10:30:00', '11:00:00', '11:30:00', '13:00:00', '13:30:00', '14:00:00', '14:30:00', '15:00:00']
    return [
        {'code': code, 'trade_time': f'{trade_date} {bar_time}', 'open': 10.0, 'high': 10.2, 'low': 9.8, 'close': 10.1, 'volume': 100, 'amount': 1000.0}
        for trade_date in trade_dates
        for bar_time in bar_times
    ]


def test_read_cache_frame_returns_empty_for_broken_parquet(tmp_path) -> None:
    path = tmp_path / 'broken.parquet'
    path.write_bytes(b'broken parquet')

    frame = cache_store.read_cache_frame(path)

    assert frame.empty


def test_daily_basic_market_cache_uses_trade_date_partition(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_build_cache_path(provider: str, namespace: list[str], identity: dict[str, str], file_name: str = 'data') -> Path:
        captured['provider'] = provider
        captured['namespace'] = namespace
        captured['identity'] = identity
        return tmp_path / 'data.parquet'

    monkeypatch.setattr(tushare_stocks, 'build_cache_path', fake_build_cache_path)
    monkeypatch.setattr(tushare_stocks, 'read_cache_frame', lambda path: pd.DataFrame())
    monkeypatch.setattr(
        tushare_stocks,
        '_fetch_daily_basic_market_frame',
        lambda start_value, end_value: pd.DataFrame(
            [{'code': '600000', 'trade_date': '2025-01-02', 'turnover_rate': 1.2, 'volume_ratio': 0.8, 'pe': 10.0, 'pb': 1.0, 'ps': 2.0, 'pcf': 3.0, 'dv_ratio': 0.5, 'total_share': 100.0, 'float_share': 80.0, 'total_mv': 1000.0, 'circ_mv': 800.0}]
        ),
    )
    monkeypatch.setattr(tushare_stocks, 'write_cache_frame', lambda path, df: captured.setdefault('written', len(df)))

    frame = tushare_stocks._build_daily_market_frames('2025-01-02')

    assert captured['provider'] == 'tushare'
    assert captured['namespace'] == ['stocks', 'indicators', 'daily-basic', 'market']
    assert captured['identity'] == {'trade_date': '20250102'}
    assert len(frame) == 1


def test_docs_search_endpoint() -> None:
    response = client.get('/docs/search', params={'q': 'stocks'})
    assert response.status_code == 200
    payload = response.json()
    assert 'items' in payload


def test_system_runbook_doc_endpoint() -> None:
    response = client.get('/docs/system/runbook')
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'system/runbook'
    assert '运行说明' in payload['title']


def test_doc_endpoint_contains_typed_response_and_field_docs() -> None:
    response = client.get('/docs/stocks/finance/express')
    assert response.status_code == 200
    payload = response.json()
    assert '`GET` 返回单只股票的业绩快报记录。' in payload['content']
    assert '顶层返回 `list[ExpressItem]`。' in payload['content']
    assert '- `announce_date`（`str`）：公告日期。' in payload['content']
    assert '## 查询参数' in payload['content']
    assert all(text not in payload['content'] for text in PLACEHOLDER_TEXTS)


def test_daily_snapshot_doc_endpoint_contains_formal_snapshot_entry() -> None:
    response = client.get('/docs/stocks/quotes-daily-snapshot')
    assert response.status_code == 200
    payload = response.json()
    assert '`GET` 返回指定交易日的全市场股票日线快照。' in payload['content']
    assert '不需要传 `code` 或 `codes`。' in payload['content']
    assert '`fact.stock_daily_1d`' in payload['content']
    assert '`BJSE`' in payload['content']
    assert '`stocks.quotes.daily_snapshot`' in payload['content']


def test_quotes_doc_endpoint_mentions_fact_stock_daily_1d_and_bjse() -> None:
    response = client.get('/docs/stocks/quotes')
    assert response.status_code == 200
    payload = response.json()
    assert '`fact.stock_daily_1d`' in payload['content']
    assert '`BJSE`' in payload['content']


def test_provider_coverage_doc_mentions_bjse_daily_table_for_stock_quotes() -> None:
    response = client.get('/docs/system/provider-coverage')
    assert response.status_code == 200
    payload = response.json()
    assert '盘点时间：2026-04-23' in payload['content']
    assert '`stocks.quotes.daily` Store' in payload['content']
    assert '`/api/stocks/quotes/daily-snapshot`' in payload['content']
    assert '截至 2026-04-22，MarketHub 旧行情主链已完全移除。' in payload['content']
    assert 'Capability Matrix 勾选的源会并发参与' in payload['content']
    assert '`merge_strategy` 合并' in payload['content']
    assert '正式源 / 常规后备 / 昂贵兜底' in payload['content']


def test_provider_coverage_doc_mentions_news_event_view() -> None:
    response = client.get('/docs/system/provider-coverage')
    assert response.status_code == 200
    payload = response.json()
    assert '`/api/markets/events/news`' in payload['content']
    assert '`news_store`' in payload['content']
    assert '`fact.news_event_agent_view`' in payload['content']


def test_board_money_flow_daily_snapshot_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        boards,
        'get_market_money_flow',
        lambda trade_date, scope, limit, offset: [
            BoardMoneyFlowItem(board_code='885338', trade_date='2025-01-02', scope='board', inflow=10.0, outflow=8.0, net_inflow=2.0)
        ],
    )
    response = client.get('/api/boards/indicators/money-flow', params={'trade_date': '2025-01-02'})
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]['board_code'] == '885338'
    assert payload[0]['trade_date'] == '2025-01-02'
    assert payload[0]['net_inflow'] == 2.0


def test_board_money_flow_daily_snapshot_doc_endpoint() -> None:
    response = client.get('/docs/boards/indicators/money-flow-daily-snapshot')
    assert response.status_code == 200
    payload = response.json()
    assert '`GET` 返回指定交易日的全市场板块资金流快照。' in payload['content']
    assert '不需要传 `board_code`。' in payload['content']


def test_doc_view_endpoint_contains_typed_response_and_field_docs() -> None:
    response = client.get('/doc-view/stocks/finance/express')
    assert response.status_code == 200
    html = response.text
    assert '返回单只股票的业绩快报记录。' in html
    assert 'list[ExpressItem]' in html
    assert 'announce_date' in html
    assert all(text not in html for text in PLACEHOLDER_TEXTS)


def test_all_docs_files_are_readable() -> None:
    for path in sorted(DOCS_ROOT.rglob('*.md')):
        relative = path.relative_to(DOCS_ROOT).as_posix()
        public_path = to_public_doc_path(relative)
        endpoint = '/docs' if public_path == '' else f'/docs/{public_path}'
        response = client.get(endpoint)
        assert response.status_code == 200, endpoint
        payload = response.json()
        assert payload['title'] != '', endpoint
        assert payload['content'] != '', endpoint
        assert '\x00' not in payload['content'], endpoint


def test_all_api_docs_are_fully_described() -> None:
    for path in sorted(DOCS_ROOT.rglob('*.md')):
        text = path.read_text(encoding='utf-8').lstrip('\ufeff')
        if not text.startswith('# /api/'):
            continue
        assert all(item not in text for item in PLACEHOLDER_TEXTS), path
        assert '## 返回类型' in text, path
        assert '## 返回字段' in text, path
        query_lines = extract_section_lines(text, '查询参数')
        for line in query_lines:
            if line.startswith('- '):
                assert '）：' in line, (path, line)
        path_lines = extract_section_lines(text, '路径参数')
        for line in path_lines:
            if line.startswith('- '):
                assert '）：' in line, (path, line)
        field_lines = extract_section_lines(text, '返回字段')
        assert field_lines != [], path
        for line in field_lines:
            if line.startswith('- '):
                assert '）：' in line, (path, line)


def test_docs_all_endpoint_covers_all_docs() -> None:
    response = client.get('/docs/all')
    assert response.status_code == 200
    payload = response.json()
    items = collect_all_doc_items()
    assert f'当前共收录 {len(items)} 篇文档。' in payload['content']
    assert '/docs/docs' in payload['content']
    assert '/docs/system/provider-gap-candidates' in payload['content']
    assert 'GET /api/indexes/{index_code}/members' in payload['content']
    assert 'GET /api/stocks/{code}/corporate-actions/dividends 返回单只股票的分红送转记录。' in payload['content']
    assert 'GET /api/boards/indicators/money-flow 返回指定交易日的全市场板块资金流快照。' in payload['content']
    assert all(text not in payload['content'] for text in PLACEHOLDER_TEXTS)


def test_doc_view_all_endpoint_links_to_all_doc_types() -> None:
    response = client.get('/doc-view/all')
    assert response.status_code == 200
    html = response.text
    assert '/doc-view/docs' in html
    assert '/doc-view/system/provider-gap-candidates' in html
    assert '/doc-view/indexes/members' in html


def test_docs_search_can_find_doc_paths() -> None:
    response = client.get('/docs/search', params={'q': 'provider-gap-candidates', 'limit': 5})
    assert response.status_code == 200
    items = response.json()['items']
    assert items[0]['path'] == 'system/provider-gap-candidates'

    response = client.get('/docs/search', params={'q': 'search-docs', 'limit': 5})
    assert response.status_code == 200
    items = response.json()['items']
    assert items[0]['path'] == 'search-docs'


def test_board_quotes_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        boards,
        'get_quotes',
        lambda board_code, board_codes, freq, trade_date, start_date, end_date, start_time, end_time, count, limit: [
            BoardQuoteItem(board_code='885338', trade_time='2026-03-30', freq='1d', close=123.45)
        ],
    )
    response = client.get('/api/boards/quotes', params={'board_code': '885338'})
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]['board_code'] == '885338'
    assert payload[0]['close'] == 123.45


def test_index_quotes_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        indexes,
        'get_quotes',
        lambda index_code, index_codes, freq, trade_date, start_date, end_date, count, limit: [
            IndexQuoteItem(index_code='000001', trade_time='2026-03-30', freq='1d', close=3350.12)
        ],
    )
    response = client.get('/api/indexes/quotes', params={'index_code': '000001'})
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]['index_code'] == '000001'
    assert payload[0]['close'] == 3350.12


def test_indexes_doc_endpoint() -> None:
    response = client.get('/docs/indexes')
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'indexes'
    assert '指数接口' in payload['content']


def test_daily_indicator_service_rejects_market_range_without_codes() -> None:
    with pytest.raises(HTTPException):
        stocks.get_daily_basic('', '', '', '2025-01-01', '2025-03-31')


def test_daily_indicator_service_rejects_large_code_batch() -> None:
    codes = ','.join(f'600{index:03d}' for index in range(201))
    with pytest.raises(HTTPException):
        stocks.get_daily_basic('', codes, '2025-01-02', '', '')


def test_stock_quotes_query_endpoint_filters_item_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        stocks,
        'get_quotes_query_result',
        lambda *args: StockQuotesQueryResult(
            items=[StockQuoteItem(code='600000', trade_time='2026-05-15', freq='1d', close=10.0, volume=100.0)],
            meta=StockQuotesMeta(
                total_rows=1,
                returned_rows=1,
                complete=True,
                truncated=False,
                codes=[
                    StockQuoteCodeSummary(
                        code='600000',
                        row_count=1,
                        first_trade_time='2026-05-15',
                        last_trade_time='2026-05-15',
                        complete=True,
                        truncated=False,
                    )
                ],
            ),
        ),
    )

    response = client.get('/api/stocks/quotes/query', params={'code': '600000', 'fields': 'code,close'})

    assert response.status_code == 200
    payload = response.json()
    assert payload['items'] == [{'code': '600000', 'close': 10.0}]
    assert payload['meta']['codes'][0]['last_trade_time'] == '2026-05-15'


def test_quotes_doc_mentions_automatic_gap_fill() -> None:
    response = client.get('/docs/stocks/quotes')
    assert response.status_code == 200
    payload = response.json()
    assert '`stocks.quotes.daily`' in payload['content']
    assert 'Capability Matrix 勾选的源并发取数' in payload['content']


def test_trading_calendar_doc_mentions_ts_gap_fill() -> None:
    response = client.get('/docs/markets/calendar/trading')
    assert response.status_code == 200
    payload = response.json()
    assert '默认 provider 候选是 `static_core -> Tushare -> AKShare emergency`' in payload['content']
    assert '`trade_cal`' in payload['content']


def test_market_news_events_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        news,
        'get_events',
        lambda trade_date, announcement_date, crawl_date, stock_code, event_type, min_importance_score, sort_by, limit, offset, include_sources, include_content_text: NewsEventQueryResult(
            events=[
                NewsEventItem(
                    event_id='evt-1',
                    trade_date='2025-01-02',
                    announcement_time='2025-01-03',
                    crawl_time='2025-01-02 09:31:00',
                    session_tag='盘中',
                    event_type='公告',
                    title='测试公告',
                    summary='测试摘要',
                    content_text='测试正文',
                    importance_score=4,
                    sentiment='中性',
                    source_name='巨潮资讯',
                    primary_detail_url='https://example.com/detail',
                    related_stock_codes=['600000'],
                    related_stock_names=['浦发银行'],
                    topic_tags=['银行'],
                    mentioned_stock_codes=['600000'],
                    mentioned_stock_names=['浦发银行'],
                    mentioned_board_names=['银行'],
                    sources=[
                        NewsEventSourceItem(
                            source_table='cninfo_disclosure_item',
                            source_record_id='1001',
                            source_name='巨潮资讯',
                            source_type='公告',
                            detail_url='https://example.com/detail',
                            announcement_time='2025-01-03',
                            crawl_time='2025-01-02 09:31:00',
                        )
                    ],
                )
            ]
        ),
    )

    response = client.get('/api/markets/events/news', params={'trade_date': '2025-01-02'})
    assert response.status_code == 200
    payload = response.json()
    assert payload['events'][0]['event_id'] == 'evt-1'
    assert payload['events'][0]['announcement_time'] == '2025-01-03'
    assert payload['events'][0]['crawl_time'] == '2025-01-02 09:31:00'
    assert 'published_at' not in payload['events'][0]
    assert 'sources' not in payload['events'][0]
    assert 'content_text' not in payload['events'][0]


def test_market_news_events_endpoint_can_include_sources_and_content_text(monkeypatch) -> None:
    monkeypatch.setattr(
        news,
        'get_events',
        lambda trade_date, announcement_date, crawl_date, stock_code, event_type, min_importance_score, sort_by, limit, offset, include_sources, include_content_text: NewsEventQueryResult(
            events=[
                NewsEventItem(
                    event_id='evt-1',
                    trade_date='2025-01-02',
                    announcement_time='2025-01-03',
                    crawl_time='2025-01-02 09:31:00',
                    session_tag='盘中',
                    event_type='公告',
                    title='测试公告',
                    summary='测试摘要',
                    content_text='测试正文',
                    importance_score=4,
                    sentiment='中性',
                    source_name='巨潮资讯',
                    primary_detail_url='https://example.com/detail',
                    related_stock_codes=['600000'],
                    related_stock_names=['浦发银行'],
                    sources=[
                        NewsEventSourceItem(
                            source_table='cninfo_disclosure_item',
                            source_record_id='1001',
                            source_name='巨潮资讯',
                            source_type='公告',
                            detail_url='https://example.com/detail',
                            announcement_time='2025-01-03',
                            crawl_time='2025-01-02 09:31:00',
                        )
                    ],
                )
            ]
        ),
    )

    response = client.get(
        '/api/markets/events/news',
        params={'trade_date': '2025-01-02', 'crawl_date': '2025-01-02', 'sort_by': 'crawl_time', 'include_sources': 'true', 'include_content_text': 'true'},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['events'][0]['content_text'] == '测试正文'
    assert payload['events'][0]['crawl_time'] == '2025-01-02 09:31:00'
    assert payload['events'][0]['sources'][0]['source_table'] == 'cninfo_disclosure_item'
    assert payload['events'][0]['sources'][0]['announcement_time'] == '2025-01-03'


def test_market_news_doc_endpoint() -> None:
    response = client.get('/docs/markets/events/news')
    assert response.status_code == 200
    payload = response.json()
    assert '`GET` 返回统一新闻事件流。' in payload['content']
    assert '`fact.news_event_agent_view`' in payload['content']
    assert '`crawl_time`' in payload['content']
    assert '`announcement_time`' in payload['content']
    assert '`published_at` 已从对外响应移除' in payload['content']

