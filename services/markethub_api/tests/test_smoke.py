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
from quotemux.query_engine import CapabilityQuerySpec, execute_capability_query
from quotemux.runtime_core.executor import ProviderStep
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
            "/api/diagnostics/fact-ref",
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
    assert "/api/stocks/quotes" in route_paths
    assert "/api/stocks/quotes/query" not in route_paths
    assert "/api/stocks/quotes/query" not in binding_paths
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
    monkeypatch.setattr(
        stocks,
        'get_quotes_query_result',
        lambda *args: StockQuotesQueryResult(
            items=[],
            meta=StockQuotesMeta(total_rows=0, returned_rows=0, complete=True, truncated=False, codes=[]),
        ),
    )

    response = client.get('/api/stocks/quotes', params={'codes': '603158', 'limit': 50000})

    assert response.status_code == 200
    assert response.json()['items'] == []


def test_connection_diagnostics_endpoint() -> None:
    with TestClient(app) as started_client:
        response = started_client.get('/api/diagnostics/connections')
    assert response.status_code == 200
    payload = response.json()
    assert 'provider_runtime' in payload
    assert 'store_db_pool' in payload
    assert 'data_thread_pool' in payload
    assert 'quote_thread_pool' in payload
    assert 'sync_thread_pool' in payload
    assert 'providers' in payload['provider_runtime']
    assert payload['data_thread_pool']['total_tokens'] == 64
    assert payload['quote_thread_pool']['total_tokens'] == 6
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
        lambda trade_date, limit, offset, skip_suspended, skip_st: [
            StockQuoteItem(code='600000', trade_time='2025-01-02', freq='1d', open=10.0, high=10.5, low=9.8, close=10.2, pre_close=10.0, change=0.2, pct_chg=2.0, volume=12345.0, amount=67890.0, adjust='none', is_suspended=False, is_st=False)
        ],
    )
    response = client.get('/api/stocks/quotes/daily-snapshot', params={'trade_date': '2025-01-02'})
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]['code'] == '600000'
    assert payload[0]['freq'] == '1d'
    assert payload[0]['pre_close'] == 10.0


def test_stock_quotes_endpoint_forwards_skip_filters(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get_quotes_query_result(*args):
        captured["args"] = args
        return StockQuotesQueryResult(
            items=[StockQuoteItem(code="600000", trade_time="2025-01-02", freq="1d", close=10.0, adjust="none", is_suspended=False, is_st=True)],
            meta=StockQuotesMeta(total_rows=1, returned_rows=1, complete=True, truncated=False, codes=[]),
        )

    monkeypatch.setattr(stocks, "get_quotes_query_result", fake_get_quotes_query_result)
    response = client.get("/api/stocks/quotes", params={"code": "600000", "skip_suspended": "false", "skip_st": "true", "fill_missing": "true", "fields": "code,is_suspended,is_st"})

    assert response.status_code == 200
    assert captured["args"][11] is False
    assert captured["args"][12] is True
    assert captured["args"][13] is True
    assert response.json()["items"] == [{"code": "600000", "is_suspended": False, "is_st": True}]
    assert response.json()["meta"]["complete"] is True


def test_stock_quotes_query_old_endpoint_not_found() -> None:
    response = client.get("/api/stocks/quotes/query", params={"code": "600000"})

    assert response.status_code == 404


def test_stock_daily_snapshot_endpoint_forwards_skip_filters(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get_market_daily_snapshot(*args):
        captured["args"] = args
        return []

    monkeypatch.setattr(stocks, "get_market_daily_snapshot", fake_get_market_daily_snapshot)
    response = client.get("/api/stocks/quotes/daily-snapshot", params={"trade_date": "2025-01-02", "skip_suspended": "false", "skip_st": "true"})

    assert response.status_code == 200
    assert captured["args"] == ("2025-01-02", 10000, 0, False, True)


def test_stock_daily_local_window_endpoint_forwards_skip_filters(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get_market_daily_local_window(*args):
        captured["args"] = args
        return []

    monkeypatch.setattr(stocks, "get_market_daily_local_window", fake_get_market_daily_local_window)
    response = client.get("/api/stocks/quotes/daily-local-window", params={"start_date": "2025-01-01", "end_date": "2025-01-02", "skip_suspended": "false", "skip_st": "true"})

    assert response.status_code == 200
    assert captured["args"] == ("2025-01-01", "2025-01-02", 50000, 0, False, True)


def test_stock_daily_local_window_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        stocks,
        'get_market_daily_local_window',
        lambda start_date, end_date, limit, offset, skip_suspended, skip_st: [
            StockQuoteItem(code='600000', trade_time='2025-01-02', freq='1d', close=10.2, pre_close=10.0, change=0.2, pct_chg=2.0, adjust='none', is_suspended=False, is_st=False)
        ],
    )
    response = client.get('/api/stocks/quotes/daily-local-window', params={'start_date': '2025-01-01', 'end_date': '2025-01-02'})
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]['code'] == '600000'
    assert payload[0]['trade_time'] == '2025-01-02'
    assert payload[0]['freq'] == '1d'



def test_stock_daily_window_old_endpoint_not_found() -> None:
    response = client.get('/api/stocks/quotes/daily-window', params={'start_date': '2025-01-01', 'end_date': '2025-01-02'})
    assert response.status_code == 404
def test_stock_catalog_endpoint_defaults_to_full_market_limit(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get_catalog(codes: str, name: str, exchange: str, list_status: str, include_delisted: bool, limit: int, offset: int) -> list[StockBasicInfo]:
        captured['limit'] = limit
        captured['offset'] = offset
        return [StockBasicInfo(code='600000', name='娴嬭瘯鑲′唤', exchange='SSE', market='娌競涓绘澘', list_status='listed', list_date='1999-11-10', delist_date='')]

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

    assert fields == ('codes', 'name', 'exchange', 'list_status', 'include_delisted')
    assert _coverage_mode_for_capability('stocks.catalog') == 'snapshot'
    assert _payload_matches_scope({'code': '600000', 'name': '测试股份'}, scope)
    assert not _payload_matches_scope({'code': '000001', 'name': '测试股份'}, scope)


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
    assert payload['title'].startswith('MarketHub')
    assert '/api/*' in payload['content']
    assert '/docs/*' in payload['content']



def test_doc_endpoint_contains_typed_response_and_field_docs() -> None:
    response = client.get('/docs/stocks/finance/express')
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'stocks/finance/express'
    assert payload['title'] == '/api/stocks/{code}/finance/express'
    assert '`GET`' in payload['content']
    assert 'list[ExpressItem]' in payload['content']
    assert 'announce_date' in payload['content']
    assert all(text not in payload['content'] for text in PLACEHOLDER_TEXTS)



def test_daily_snapshot_doc_endpoint_contains_formal_snapshot_entry() -> None:
    response = client.get('/docs/stocks/quotes-daily-snapshot')
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'stocks/quotes-daily-snapshot'
    assert payload['title'] == '/api/stocks/quotes/daily-snapshot'
    assert 'list[StockQuoteItem]' in payload['content']
    assert 'fact.stock_daily_1d' in payload['content']



def test_daily_local_window_doc_endpoint_contains_local_fact_table_entry() -> None:
    response = client.get('/docs/stocks/quotes-daily-local-window')
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'stocks/quotes-daily-local-window'
    assert payload['title'] == '/api/stocks/quotes/daily-local-window'
    assert 'fact.stock_daily_1d' in payload['content']
    assert '/api/stocks/quotes' in payload['content']
    assert 'trade_time, code' in payload['content']
    assert all(text not in payload['content'] for text in PLACEHOLDER_TEXTS)



def test_daily_window_old_doc_endpoint_not_found() -> None:
    response = client.get('/docs/stocks/quotes-daily-window')
    assert response.status_code == 404



def test_quotes_doc_mentions_daily_filter_flags() -> None:
    response = client.get('/docs/stocks/quotes')
    assert response.status_code == 200
    payload = response.json()
    assert 'skip_suspended' in payload['content']
    assert 'skip_st' in payload['content']
    assert 'fill_missing' in payload['content']



def test_quotes_doc_endpoint_mentions_fact_stock_daily_1d_and_bjse() -> None:
    response = client.get('/docs/stocks/quotes')
    assert response.status_code == 200
    payload = response.json()
    assert 'fact.stock_daily_1d' in payload['content']
    assert 'BJSE' in payload['content']
    assert 'is_suspended=true' in payload['content']



def test_provider_coverage_doc_mentions_bjse_daily_table_for_stock_quotes() -> None:
    response = client.get('/docs/system/provider-coverage')
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'system/provider-coverage'
    assert '/api/stocks/quotes/daily-local-window' in payload['content']
    assert 'fact.stock_daily_1d' in payload['content']
    assert 'is_suspended' in payload['content']
    assert 'is_st' in payload['content']



def test_board_money_flow_daily_snapshot_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        boards,
        'get_market_money_flow',
        lambda trade_date, scope, limit, offset: [
            BoardMoneyFlowItem(board_code='BK001', trade_date='2025-01-02', scope=scope, net_inflow=1.0)
        ],
    )
    response = client.get('/api/boards/indicators/money-flow', params={'trade_date': '2025-01-02'})
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]['board_code'] == 'BK001'
    assert payload[0]['trade_date'] == '2025-01-02'
    assert payload[0]['scope'] == 'board'



def test_board_money_flow_daily_snapshot_doc_endpoint() -> None:
    response = client.get('/docs/boards/indicators/money-flow')
    assert response.status_code == 200
    payload = response.json()
    assert payload['title'] == '/api/boards/{board_code}/indicators/money-flow'
    assert 'list[BoardMoneyFlowItem]' in payload['content']
    assert 'board_code' in payload['content']
    assert all(text not in payload['content'] for text in PLACEHOLDER_TEXTS)



def test_doc_view_endpoint_contains_typed_response_and_field_docs() -> None:
    response = client.get('/doc-view/stocks/finance/express')
    assert response.status_code == 200
    html = response.text
    assert '/api/stocks/{code}/finance/express' in html
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
        assert chr(0) not in payload['content'], endpoint



def test_all_api_docs_are_fully_described() -> None:
    for path in sorted(DOCS_ROOT.rglob('*.md')):
        text = path.read_text(encoding='utf-8').lstrip('﻿')
        if not text.startswith('# /api/'):
            continue
        assert all(item not in text for item in PLACEHOLDER_TEXTS), path
        assert '## ' in text, path
        public_path = to_public_doc_path(path.relative_to(DOCS_ROOT).as_posix())
        endpoint = '/docs' if public_path == '' else f'/docs/{public_path}'
        response = client.get(endpoint)
        assert response.status_code == 200, path



def test_docs_all_endpoint_covers_all_docs() -> None:
    response = client.get('/docs/all')
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'all'
    assert 'GET /api/stocks/quotes/daily-local-window' in payload['content']
    assert '/docs/stocks/quotes-daily-window' not in payload['content']

    items = collect_all_doc_items()
    doc_paths = [item.doc_path for item in items]
    api_paths = [item.api_path for item in items if item.api_path != '']
    assert 'stocks/quotes-daily-local-window' in doc_paths
    assert 'stocks/quotes-daily-window' not in doc_paths
    assert '/api/stocks/quotes/daily-local-window' in api_paths
    assert '/api/stocks/quotes/daily-window' not in api_paths



def test_public_api_bindings_use_daily_local_window_path() -> None:
    binding_paths = [item.api_path for item in list_public_api_bindings()]
    assert '/api/stocks/quotes/daily-local-window' in binding_paths
    assert '/api/stocks/quotes/daily-window' not in binding_paths



def test_doc_view_all_endpoint_links_to_all_doc_types() -> None:
    response = client.get('/doc-view/all')
    assert response.status_code == 200
    html = response.text
    assert '/doc-view/docs' in html
    assert '/doc-view/system/provider-coverage' in html
    assert '/doc-view/indexes/members' in html
    assert '/doc-view/stocks/quotes-daily-local-window' in html
    assert '/doc-view/stocks/quotes-daily-window' not in html



def test_docs_search_can_find_doc_paths() -> None:
    response = client.get('/docs/search', params={'q': 'provider-coverage', 'limit': 5})
    assert response.status_code == 200
    items = response.json()['items']
    assert items[0]['path'] == 'system/provider-coverage'

    response = client.get('/docs/search', params={'q': 'search-docs', 'limit': 5})
    assert response.status_code == 200
    items = response.json()['items']
    assert items[0]['path'] == 'search-docs'



def test_docs_search_and_all_only_expose_daily_local_window() -> None:
    response = client.get('/docs/search', params={'q': 'daily-local-window', 'limit': 20})
    assert response.status_code == 200
    items = response.json()['items']
    paths = [item['path'] for item in items]
    assert 'stocks/quotes-daily-local-window' in paths
    assert 'stocks/quotes-daily-window' not in paths

    response = client.get('/docs/all')
    assert response.status_code == 200
    assert 'GET /api/stocks/quotes/daily-local-window' in response.json()['content']
    assert '/docs/stocks/quotes-daily-window' not in response.json()['content']


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
    assert payload['title'] == 'indexes'
    assert '/docs/indexes/quotes' in payload['content']



def test_daily_indicator_service_rejects_market_range_without_codes() -> None:
    with pytest.raises(HTTPException):
        stocks.get_daily_basic('', '', '', '2025-01-01', '2025-03-31')



def test_daily_indicator_service_rejects_large_code_batch() -> None:
    codes = ','.join(f'600{index:03d}' for index in range(201))
    with pytest.raises(HTTPException):
        stocks.get_daily_basic('', codes, '2025-01-02', '', '')



def test_stock_quotes_endpoint_filters_item_fields(monkeypatch) -> None:
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

    response = client.get('/api/stocks/quotes', params={'code': '600000', 'fields': 'code,close'})

    assert response.status_code == 200
    payload = response.json()
    assert payload['items'] == [{'code': '600000', 'close': 10.0}]
    assert payload['meta']['codes'][0]['last_trade_time'] == '2026-05-15'



def test_quotes_doc_mentions_automatic_gap_fill() -> None:
    response = client.get('/docs/stocks/quotes')
    assert response.status_code == 200
    payload = response.json()
    assert 'stocks.quotes.daily' in payload['content']
    assert 'Capability Matrix' in payload['content']
    assert 'fill_missing=true' in payload['content']
    assert 'is_suspended=true' in payload['content']



def test_trading_calendar_doc_mentions_ts_gap_fill() -> None:
    response = client.get('/docs/markets/calendar/trading')
    assert response.status_code == 200
    payload = response.json()
    assert 'markets.calendar.trading' in payload['content']
    assert 'Capability Matrix' in payload['content']
    assert 'AKShare emergency' in payload['content']



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
                    session_tag='post',
                    event_type='notice',
                    title='Test event',
                    summary='Summary text',
                    content_text='',
                    importance_score=4,
                    sentiment='neutral',
                    source_name='Test source',
                    primary_detail_url='https://example.com/detail',
                    related_stock_codes=['600000'],
                    related_stock_names=['PFYH'],
                    related_board_codes=['BK001'],
                    related_board_names=['BANK'],
                    topic_tags=['BANK'],
                    mentioned_stock_codes=['600000'],
                    mentioned_stock_names=['PFYH'],
                    mentioned_board_names=['BANK'],
                    sources=[
                        NewsEventSourceItem(
                            source_table='cninfo_disclosure_item',
                            source_record_id='1001',
                            source_name='Test source',
                            source_type='notice',
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
                    session_tag='post',
                    event_type='notice',
                    title='Test event',
                    summary='Summary text',
                    content_text='Full text',
                    importance_score=4,
                    sentiment='neutral',
                    source_name='Test source',
                    primary_detail_url='https://example.com/detail',
                    related_stock_codes=['600000'],
                    related_stock_names=['PFYH'],
                    related_board_codes=['BK001'],
                    related_board_names=['BANK'],
                    topic_tags=['BANK'],
                    mentioned_stock_codes=['600000'],
                    mentioned_stock_names=['PFYH'],
                    mentioned_board_names=['BANK'],
                    sources=[
                        NewsEventSourceItem(
                            source_table='cninfo_disclosure_item',
                            source_record_id='1001',
                            source_name='Test source',
                            source_type='notice',
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
        params={'trade_date': '2025-01-02', 'include_sources': 'true', 'include_content_text': 'true'},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['events'][0]['content_text'] == 'Full text'
    assert payload['events'][0]['sources'][0]['source_table'] == 'cninfo_disclosure_item'



def test_market_news_doc_endpoint() -> None:
    response = client.get('/docs/markets/events/news')
    assert response.status_code == 200
    payload = response.json()
    assert payload['title'] == '/api/markets/events/news'
    assert 'NewsEventQueryResult' in payload['content']
    assert 'announcement_time' in payload['content']
    assert 'crawl_time' in payload['content']
    assert 'published_at' in payload['content']



def test_fact_ref_complete_hit_does_not_write_cache(monkeypatch) -> None:
    written_items: list[StockQuoteItem] = []

    def fail_store_write(*args, **kwargs):
        raise AssertionError('fact_ref should not write cache on complete hit')

    monkeypatch.setattr('quotemux.query_engine.store_result', fail_store_write)
    items, report = execute_capability_query(
        CapabilityQuerySpec(
            capability_id='stocks.quotes.daily',
            store_identity={'codes': ['000001'], 'freq': '1d'},
            model_type=StockQuoteItem,
            key_fields=('code', 'trade_time', 'freq'),
            sort_fields=('code', 'trade_time'),
            request_builder=lambda current_items: [],
            provider_steps=(ProviderStep(name='provider', fetcher=lambda: []),),
            source_order=('provider',),
            base_items=[StockQuoteItem(code='000001', trade_time='2026-06-09', freq='1d', close=10.0)],
            base_source_name='fact.stock_daily_1d',
            fact_ref_writer=lambda provider_items: written_items.extend(provider_items) is None,
        )
    )

    assert [item.code for item in items] == ['000001']
    assert written_items == []
    assert report.source_hit_counts['fact.stock_daily_1d'] == 1



def test_fact_ref_gap_fill_writes_fact_ref_not_cache(monkeypatch) -> None:
    written_items: list[StockQuoteItem] = []

    def fail_store_write(*args, **kwargs):
        raise AssertionError('fact_ref gap fill should not write cache')

    monkeypatch.setattr('quotemux.query_engine.store_result', fail_store_write)
    items, _ = execute_capability_query(
        CapabilityQuerySpec(
            capability_id='stocks.quotes.daily',
            store_identity={'codes': ['000001'], 'freq': '1d'},
            model_type=StockQuoteItem,
            key_fields=('code', 'trade_time', 'freq'),
            sort_fields=('code', 'trade_time'),
            request_builder=lambda current_items: [] if len(current_items) >= 2 else [('000001', '2026-06-10', '2026-06-10')],
            provider_steps=(ProviderStep(name='provider', fetcher=lambda code, start_date, end_date: [StockQuoteItem(code=code, trade_time=start_date, freq='1d', close=11.0)]),),
            source_order=('provider',),
            base_items=[StockQuoteItem(code='000001', trade_time='2026-06-09', freq='1d', close=10.0)],
            base_source_name='fact.stock_daily_1d',
            fact_ref_writer=lambda provider_items: written_items.extend(provider_items) is None,
        )
    )

    assert [item.trade_time for item in items] == ['2026-06-09', '2026-06-10']
    assert [item.trade_time for item in written_items] == ['2026-06-10']
