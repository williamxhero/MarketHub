from __future__ import annotations

from datetime import datetime
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

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
from quotemux import QuoteMuxSettings, StockQuotesRequest
from quotemux.infra.cache import store as cache_store
from quotemux.capabilities import list_public_api_bindings
from quotemux.config_runtime.runtime import reset_config_runtime_cache
from quotemux.runtime_core.registry import get_default_source_registry
from quotemux.models import BoardMoneyFlowItem, BoardQuoteItem, IndexMemberItem, IndexQuoteItem, NewsEventItem, NewsEventQueryResult, NewsEventSourceItem, StockBasicInfo, StockDailyBasicItem, StockDailyMarketValueItem, StockMoneyFlowItem, StockQuoteCodeSummary, StockQuoteItem, StockQuotesMeta, StockQuotesQueryResult
from quotemux.sources.datalake import source as datalake
from quotemux.sources.datalake import news as datalake_news
from quotemux.source_packages.registry import refresh_default_source_package_registry
from quotemux_packages.tushare import stocks as tushare_stocks
import quotemux.boards as qm_boards
import quotemux.indexes as qm_indexes
import quotemux.markets as qm_markets
import quotemux.stocks as qm_stocks
from services import boards
from services import indexes
from services import markets
from services import news
from services import stocks
from quotemux.models import TradingCalendarItem


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
    assert 'datalake_db_pool' in payload
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


def test_datalake_stock_quotes_1d_reads_fact_stock_daily_1d_and_supports_bjse(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_load_stock_daily_frame(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
        captured['codes'] = codes
        captured['start_date'] = start_date
        captured['end_date'] = end_date
        return pd.DataFrame(
            [
                {
                    'code': '430001',
                    'trade_time': '2026-04-03',
                    'open': 10.0,
                    'high': 12.0,
                    'low': 9.5,
                    'close': 11.0,
                    'volume': 12340,
                    'amount': 56700.0,
                    'adj_factor': 1.0,
                    'labi_buy': pd.NA,
                    'labi_sell': pd.NA,
                    'mism_buy': pd.NA,
                    'mism_sell': pd.NA,
                }
            ]
        )

    monkeypatch.setattr(datalake, 'load_stock_daily_frame', fake_load_stock_daily_frame)
    monkeypatch.setattr(datalake, 'load_stock_intraday_frame', lambda *args, **kwargs: pytest.fail('1d 行情不应读取分钟表'))

    items = datalake.get_stock_quotes(['BJSE.430001'], '1d', '', '2026-04-03', '2026-04-03', '', '', None, 'none')

    assert captured == {'codes': ['430001'], 'start_date': '2026-04-03', 'end_date': '2026-04-03'}
    assert len(items) == 1
    assert items[0].code == '430001'
    assert items[0].trade_time == '2026-04-03'
    assert items[0].close == 11.0
    assert items[0].volume == 12340.0


def test_datalake_stock_daily_snapshot_reads_fact_stock_daily_1d_and_supports_bjse(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_load_stock_daily_snapshot_frame(trade_date: str, limit: int, offset: int) -> pd.DataFrame:
        captured['trade_date'] = trade_date
        captured['limit'] = limit
        captured['offset'] = offset
        return pd.DataFrame(
            [
                {
                    'code': '430001',
                    'trade_time': '2026-04-03',
                    'open': 10.0,
                    'high': 12.0,
                    'low': 9.5,
                    'close': 11.0,
                    'pre_close': 10.5,
                    'volume': 12340,
                    'amount': 56700.0,
                }
            ]
        )

    monkeypatch.setattr(datalake, 'load_stock_daily_snapshot_frame', fake_load_stock_daily_snapshot_frame)

    items = datalake.get_stock_daily_snapshot('2026-04-03', 500, 20)

    assert captured == {'trade_date': '2026-04-03', 'limit': 500, 'offset': 20}
    assert len(items) == 1
    assert items[0].code == '430001'
    assert items[0].trade_time == '2026-04-03'
    assert items[0].pre_close == 10.5
    assert round(items[0].pct_chg or 0.0, 6) == round((11.0 - 10.5) / 10.5 * 100, 6)


def test_datalake_stock_daily_snapshot_full_reads_fact_stock_daily_1d(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_load_stock_daily_snapshot_full_frame(trade_date: str) -> pd.DataFrame:
        captured['trade_date'] = trade_date
        return pd.DataFrame(
            [
                {
                    'code': '600000',
                    'trade_time': '2026-04-03',
                    'open': 10.0,
                    'high': 12.0,
                    'low': 9.5,
                    'close': 11.0,
                    'pre_close': 10.5,
                    'volume': 12340,
                    'amount': 56700.0,
                }
            ]
        )

    monkeypatch.setattr(datalake, 'load_stock_daily_snapshot_full_frame', fake_load_stock_daily_snapshot_full_frame)

    items = datalake.get_stock_daily_snapshot_full('2026-04-03')

    assert captured == {'trade_date': '2026-04-03'}
    assert len(items) == 1
    assert items[0].code == '600000'
    assert items[0].trade_time == '2026-04-03'


def test_datalake_adj_factor_endpoint_repairs_single_day_gap(monkeypatch) -> None:
    monkeypatch.setattr(
        datalake,
        'load_stock_daily_frame',
        lambda codes, start_date, end_date: pd.DataFrame(
            [
                {'code': '600601', 'trade_time': '2026-02-26', 'close': 11.93, 'adj_factor': 6471.278},
                {'code': '600601', 'trade_time': '2026-02-27', 'close': 11.35, 'adj_factor': None},
                {'code': '600601', 'trade_time': '2026-03-02', 'close': 11.08, 'adj_factor': 6471.278},
            ]
        ),
    )

    items = datalake.get_adj_factors('600601', '2026-02-26', '2026-03-02', '')

    assert [(item.trade_date, item.adj_factor) for item in items] == [
        ('20260226', 6471.278),
        ('20260227', 6471.278),
        ('20260302', 6471.278),
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
    assert '`static_core -> Tushare -> efinance -> mootdx -> akshare`' in payload['content']
    assert '`OpenTDX -> efinance -> mootdx -> akshare`' in payload['content']
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


def test_daily_basic_service_supports_market_trade_date(monkeypatch) -> None:
    monkeypatch.setattr(
        qm_stocks._tushare_provider,
        'get_stock_daily_basic',
        lambda code, codes, trade_date, start_date, end_date: [
            StockDailyBasicItem(code='600000', trade_date='2025-01-02', turnover_rate=0.26, total_share=2935217.7607, float_share=2935217.7607, volume_ratio=1.16, pe=8.10, pb=0.46)
        ],
    )

    items = stocks.get_daily_basic('', '', '2025-01-02', '', '')

    assert len(items) == 1
    assert items[0].code == '600000'
    assert items[0].trade_date == '2025-01-02'
    assert items[0].turnover_rate == 0.26
    assert items[0].volume_ratio == 1.16
    assert items[0].pe == 8.10
    assert items[0].pb == 0.46


def test_daily_indicator_service_rejects_market_range_without_codes() -> None:
    with pytest.raises(HTTPException):
        stocks.get_daily_basic('', '', '', '2025-01-01', '2025-03-31')


def test_daily_indicator_service_rejects_large_code_batch() -> None:
    codes = ','.join(f'600{index:03d}' for index in range(201))
    with pytest.raises(HTTPException):
        stocks.get_daily_basic('', codes, '2025-01-02', '', '')


def test_stock_quotes_auto_fill_missing_even_when_fill_missing_is_false(monkeypatch) -> None:
    ts_calls: list[tuple[list[str], str, str]] = []
    ef_calls: list[tuple[list[str], str, str]] = []

    monkeypatch.setattr(
        qm_stocks._datalake,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: [
            StockQuoteItem(code='600000', trade_time='2025-03-03', freq='1d', close=10.0, adjust='none')
        ],
    )
    monkeypatch.setattr(
        qm_stocks._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date='2025-03-03', is_open=True),
            TradingCalendarItem(exchange='SSE', trade_date='2025-03-04', is_open=True),
        ],
    )
    monkeypatch.setattr(
        qm_stocks._tushare_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: ts_calls.append((codes, start_date, end_date)) or [
            StockQuoteItem(code='600000', trade_time='2025-03-04', freq='1d', close=10.5, adjust='none')
        ],
    )
    monkeypatch.setattr(
        qm_stocks._efinance_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: ef_calls.append((codes, start_date, end_date)) or [],
    )

    items = stocks.get_quotes('600000', '', '1d', '', '2025-03-03', '2025-03-04', '', '', None, 'none', 200, True, False)

    assert [item.trade_time for item in items] == ['2025-03-03', '2025-03-04']
    assert ts_calls == [(['600000'], '2025-03-04', '2025-03-04')]
    assert ef_calls == [(['600000'], '2025-03-04', '2025-03-04')]


def test_stock_quotes_query_marks_single_intraday_range_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(
        qm_stocks._datalake,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: [
            StockQuoteItem(code='300001', trade_time='2026-05-14 15:00:00', freq='30m', close=10.0, adjust='none')
        ],
    )
    monkeypatch.setattr(
        qm_stocks._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date='2026-05-14', is_open=True),
            TradingCalendarItem(exchange='SSE', trade_date='2026-05-15', is_open=True),
        ],
    )
    monkeypatch.setattr(qm_stocks._opentdx_provider, 'get_stock_quotes', lambda *args, **kwargs: [])
    monkeypatch.setattr(qm_stocks._efinance_provider, 'get_stock_quotes', lambda *args, **kwargs: [])
    monkeypatch.setattr(qm_stocks._mootdx_provider, 'get_stock_quotes', lambda *args, **kwargs: [])
    monkeypatch.setattr(qm_stocks._akshare_provider, 'get_stock_quotes', lambda *args, **kwargs: [])

    result = qm_stocks.QuoteMuxStocks(QuoteMuxSettings()).get_quotes_query_result(
        StockQuotesRequest(codes=['300001'], freq='30m', start_date='2026-03-16', end_date='2026-05-15', limit=5000)
    )

    assert result.meta.complete is False
    assert result.meta.codes[0].code == '300001'
    assert result.meta.codes[0].last_trade_time == '2026-05-14 15:00:00'
    assert result.meta.codes[0].missing_trade_dates == ['2026-05-15']


def test_stock_quotes_query_reports_batch_code_ranges(monkeypatch) -> None:
    codes = [f'30000{index}' for index in range(1, 6)]
    monkeypatch.setattr(
        qm_stocks._datalake,
        'get_stock_quotes',
        lambda request_codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: [
            StockQuoteItem(code=code, trade_time=trade_time, freq='30m', close=10.0, adjust='none')
            for code in request_codes
            for trade_time in ['2026-05-14 15:00:00', '2026-05-15 15:00:00']
        ],
    )
    monkeypatch.setattr(
        qm_stocks._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date='2026-05-14', is_open=True),
            TradingCalendarItem(exchange='SSE', trade_date='2026-05-15', is_open=True),
        ],
    )
    monkeypatch.setattr(qm_stocks._opentdx_provider, 'get_stock_quotes', lambda *args, **kwargs: pytest.fail('数据完整时不应补源'))

    result = qm_stocks.QuoteMuxStocks(QuoteMuxSettings()).get_quotes_query_result(
        StockQuotesRequest(codes=codes, freq='30m', start_date='2026-03-16', end_date='2026-05-15')
    )

    assert result.meta.complete is True
    assert result.meta.total_rows == 10
    assert [(item.code, item.row_count, item.first_trade_time, item.last_trade_time, item.complete) for item in result.meta.codes] == [
        (code, 2, '2026-05-14 15:00:00', '2026-05-15 15:00:00', True)
        for code in codes
    ]


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


def test_stock_quotes_daily_range_does_not_fill_weekend_gaps(monkeypatch) -> None:
    ef_calls: list[tuple[list[str], str, str]] = []
    ts_calls: list[tuple[list[str], str, str]] = []

    monkeypatch.setattr(
        qm_stocks._datalake,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: [
            StockQuoteItem(code='000032', trade_time=trade_day, freq='1d', close=10.0, adjust='none')
            for trade_day in ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
        ],
    )
    monkeypatch.setattr(
        qm_stocks._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date=trade_day, is_open=True)
            for trade_day in ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
        ],
    )
    monkeypatch.setattr(
        qm_stocks._efinance_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: ef_calls.append((codes, start_date, end_date)) or [],
    )
    monkeypatch.setattr(
        qm_stocks._tushare_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: ts_calls.append((codes, start_date, end_date)) or [],
    )

    items = stocks.get_quotes('000032', '', '1d', '', '2026-04-07', '2026-04-14', '', '', None, 'none', 5000, True, False)

    assert [item.trade_time for item in items] == ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
    assert ef_calls == []
    assert ts_calls == []


def test_market_daily_snapshot_service_auto_fills_missing_from_ts_then_ef_after_datalake(monkeypatch) -> None:
    ts_calls: list[list[str]] = []
    ef_calls: list[list[str]] = []

    monkeypatch.setattr(
        qm_stocks._datalake,
        'get_stock_daily_snapshot_full',
        lambda trade_date: [
            StockQuoteItem(code='600000', trade_time='2025-03-03', freq='1d', close=10.0, adjust='none')
        ],
    )
    monkeypatch.setattr(qm_stocks._datalake_ref, 'get_stock_active_codes', lambda trade_date: ['430001', '600000', '830001'])
    monkeypatch.setattr(
        qm_stocks._tushare_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: ts_calls.append(codes) or [
            StockQuoteItem(code='430001', trade_time='2025-03-03', freq='1d', close=9.0, adjust='none'),
        ],
    )
    monkeypatch.setattr(
        qm_stocks._efinance_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: ef_calls.append(codes) or [
            StockQuoteItem(code='830001', trade_time='2025-03-03', freq='1d', close=8.0, adjust='none'),
            StockQuoteItem(code='600000', trade_time='2025-03-03', freq='1d', close=11.0, adjust='none'),
        ],
    )

    items = stocks.get_market_daily_snapshot('2025-03-03', 10, 0)

    assert [(item.code, item.close) for item in items] == [('430001', 9.0), ('600000', 10.0), ('830001', 8.0)]
    assert ts_calls == [['430001', '830001']]
    assert ef_calls == [['430001', '830001']]


def test_stock_quotes_intraday_service_degrades_from_opentdx_to_efinance(monkeypatch) -> None:
    op_calls: list[tuple[list[str], str, str]] = []
    ef_calls: list[tuple[list[str], str, str]] = []

    monkeypatch.setattr(qm_stocks._datalake, 'get_stock_quotes', lambda *args, **kwargs: [])
    monkeypatch.setattr(qm_stocks._datalake_ref, 'get_trading_calendar', lambda exchange, start_date, end_date, is_open: [TradingCalendarItem(exchange='SSE', trade_date='2026-04-03', is_open=True)])
    monkeypatch.setattr(
        qm_stocks._opentdx_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: op_calls.append((codes, start_date, end_date)) or [],
    )
    monkeypatch.setattr(
        qm_stocks._efinance_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: ef_calls.append((codes, start_date, end_date)) or [
            StockQuoteItem(code='600000', trade_time='2026-04-03 09:31:00', freq='1m', close=10.0, adjust='none')
        ],
    )
    monkeypatch.setattr(qm_stocks._mootdx_provider, 'get_stock_quotes', lambda *args, **kwargs: [])
    monkeypatch.setattr(qm_stocks._akshare_provider, 'get_stock_quotes', lambda *args, **kwargs: [])

    items = stocks.get_quotes('600000', '', '1m', '2026-04-03', '', '', '', '', 1, 'none', 20, True, False)

    assert [(item.code, item.trade_time) for item in items] == [('600000', '2026-04-03 09:31:00')]
    assert op_calls == [(['600000'], '2026-04-03', '2026-04-03')]
    assert ef_calls == [(['600000'], '2026-04-03', '2026-04-03')]


def test_stock_quotes_intraday_service_uses_datalake_before_provider_fill(monkeypatch) -> None:
    op_calls: list[tuple[list[str], str, str]] = []

    monkeypatch.setattr(
        qm_stocks._datalake,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: [
            StockQuoteItem(code='600000', trade_time='2026-03-31 09:30:00', freq='30m', close=10.0, adjust='none')
        ],
    )
    monkeypatch.setattr(
        qm_stocks._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date='2026-03-31', is_open=True),
            TradingCalendarItem(exchange='SSE', trade_date='2026-04-01', is_open=True),
        ],
    )
    monkeypatch.setattr(
        qm_stocks._opentdx_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: op_calls.append((codes, start_date, end_date)) or [
            StockQuoteItem(code='600000', trade_time='2026-04-01 09:30:00', freq='30m', close=10.5, adjust='none')
        ],
    )
    monkeypatch.setattr(qm_stocks._efinance_provider, 'get_stock_quotes', lambda *args, **kwargs: [])
    monkeypatch.setattr(qm_stocks._mootdx_provider, 'get_stock_quotes', lambda *args, **kwargs: [])
    monkeypatch.setattr(qm_stocks._akshare_provider, 'get_stock_quotes', lambda *args, **kwargs: [])

    items = stocks.get_quotes('600000', '', '30m', '', '2026-03-31', '2026-04-01', '', '', None, 'none', 20, True, False)

    assert [(item.trade_time, item.close) for item in items] == [('2026-03-31 09:30:00', 10.0), ('2026-04-01 09:30:00', 10.5)]
    assert op_calls == [(['600000'], '2026-04-01', '2026-04-01')]


def test_index_members_service_degrades_to_b3_and_only_keeps_member_shape(monkeypatch) -> None:
    ef_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(qm_indexes._tushare_provider, 'get_index_members', lambda index_code, trade_date: [])
    monkeypatch.setattr(
        qm_indexes._efinance_provider,
        'get_index_members',
        lambda index_code, trade_date: ef_calls.append((index_code, trade_date)) or [
            IndexMemberItem(index_code='000001', code='600000', name='旧名称', weight=None, trade_date='2026-04-03')
        ],
    )
    monkeypatch.setattr(qm_indexes._mootdx_provider, 'get_index_members', lambda *args, **kwargs: [])
    monkeypatch.setattr(qm_indexes._akshare_provider, 'get_index_members', lambda *args, **kwargs: [])
    monkeypatch.setattr(qm_indexes._datalake_ref, 'get_stock_names', lambda codes: {'600000': '浦发银行'})

    items = indexes.get_members('000001', '2026-04-03')

    assert [(item.index_code, item.code, item.name, item.weight) for item in items] == [('000001', '600000', '浦发银行', None)]
    assert ef_calls == [('000001', '2026-04-03')]


def test_market_daily_snapshot_service_applies_pagination_after_merge(monkeypatch) -> None:
    monkeypatch.setattr(
        qm_stocks._datalake,
        'get_stock_daily_snapshot_full',
        lambda trade_date: [
            StockQuoteItem(code='600000', trade_time='2025-03-03', freq='1d', close=10.0, adjust='none')
        ],
    )
    monkeypatch.setattr(qm_stocks._datalake_ref, 'get_stock_active_codes', lambda trade_date: ['430001', '600000', '830001'])
    monkeypatch.setattr(qm_stocks._tushare_provider, 'get_stock_quotes', lambda *args, **kwargs: [StockQuoteItem(code='430001', trade_time='2025-03-03', freq='1d', close=9.0, adjust='none')])
    monkeypatch.setattr(
        qm_stocks._efinance_provider,
        'get_stock_quotes',
        lambda codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust: [
            StockQuoteItem(code='830001', trade_time='2025-03-03', freq='1d', close=8.0, adjust='none'),
        ],
    )

    items = stocks.get_market_daily_snapshot('2025-03-03', 1, 1)

    assert len(items) == 1
    assert items[0].code == '600000'


def test_trading_calendar_service_auto_fills_missing_dates_from_ts(monkeypatch) -> None:
    ts_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        qm_markets._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange=exchange, trade_date='2025-03-03', is_open=True)
        ],
    )
    monkeypatch.setattr(
        qm_markets._tushare_provider,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: ts_calls.append((start_date, end_date)) or [
            TradingCalendarItem(exchange=exchange, trade_date='2025-03-04', is_open=True),
            TradingCalendarItem(exchange=exchange, trade_date='2025-03-05', is_open=True),
        ],
    )

    items = markets.get_trading_calendar('SSE', '2025-03-03', '2025-03-05', True)

    assert [item.trade_date for item in items] == ['2025-03-03', '2025-03-04', '2025-03-05']
    assert ts_calls == [('2025-03-04', '2025-03-05')]


def test_trading_calendar_service_uses_akshare_emergency_after_tushare_gap(monkeypatch) -> None:
    ts_calls: list[tuple[str, str]] = []
    ak_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(qm_markets._datalake_ref, 'get_trading_calendar', lambda exchange, start_date, end_date, is_open: [])
    monkeypatch.setattr(
        qm_markets._tushare_provider,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: ts_calls.append((start_date, end_date)) or [],
    )
    monkeypatch.setattr(
        qm_markets._akshare_provider,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: ak_calls.append((start_date, end_date)) or [
            TradingCalendarItem(exchange=exchange, trade_date='2025-03-03', is_open=True),
            TradingCalendarItem(exchange=exchange, trade_date='2025-03-04', is_open=True),
        ],
    )

    items = markets.get_trading_calendar('SSE', '2025-03-03', '2025-03-04', True)

    assert [item.trade_date for item in items] == ['2025-03-03', '2025-03-04']
    assert ts_calls == [('2025-03-03', '2025-03-04')]
    assert ak_calls == [('2025-03-03', '2025-03-04')]


def test_daily_basic_service_remains_tushare_only(monkeypatch) -> None:
    ef_calls: list[str] = []
    moo_calls: list[str] = []
    ak_calls: list[str] = []

    monkeypatch.setattr(
        tushare_stocks,
        'get_stock_daily_basic',
        lambda code, codes, trade_date, start_date, end_date: [
            StockDailyBasicItem(code='600000', trade_date='2025-03-03')
        ],
    )
    monkeypatch.setattr(qm_stocks._efinance_provider, 'get_stock_quotes', lambda *args, **kwargs: ef_calls.append('ef') or [])
    monkeypatch.setattr(qm_stocks._mootdx_provider, 'get_stock_quotes', lambda *args, **kwargs: moo_calls.append('moo') or [])
    monkeypatch.setattr(qm_stocks._akshare_provider, 'get_stock_quotes', lambda *args, **kwargs: ak_calls.append('ak') or [])

    items = stocks.get_daily_basic('600000', '', '2025-03-03', '', '')

    assert [item.code for item in items] == ['600000']
    assert ef_calls == []
    assert moo_calls == []
    assert ak_calls == []


def test_daily_basic_service_reads_store_before_tushare(monkeypatch) -> None:
    monkeypatch.setattr(
        qm_stocks,
        'load_store_result',
        lambda capability_id, request_identity, model_type: (
            [StockDailyBasicItem(code='600000', trade_date='2025-03-03', pe=12.0)],
            SimpleNamespace(hit=True, partial_hit=False, status='hit'),
        ),
    )
    monkeypatch.setattr(
        qm_stocks._tushare_provider,
        'get_stock_daily_basic',
        lambda code, codes, trade_date, start_date, end_date: pytest.fail('Store 命中后不应调用数据源'),
    )

    items = stocks.get_daily_basic('600000', '', '2025-03-03', '', '')

    assert [(item.code, item.trade_date, item.pe) for item in items] == [('600000', '2025-03-03', 12.0)]


def test_board_money_flow_service_only_fills_missing_date_ranges_from_ts(monkeypatch) -> None:
    ts_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        qm_boards._datalake,
        'get_board_money_flow',
        lambda board_code, trade_date, start_date, end_date, scope: [
            BoardMoneyFlowItem(board_code='885338', trade_date='2025-03-03', scope='board', net_inflow=1.0)
        ],
    )
    monkeypatch.setattr(
        qm_boards._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date='2025-03-03', is_open=True),
            TradingCalendarItem(exchange='SSE', trade_date='2025-03-04', is_open=True),
        ],
    )
    monkeypatch.setattr(
        qm_boards._tushare_provider,
        'get_board_money_flow',
        lambda board_code, trade_date, start_date, end_date, scope: ts_calls.append((start_date, end_date)) or [
            BoardMoneyFlowItem(board_code='885338', trade_date='2025-03-04', scope='board', net_inflow=2.0)
        ],
    )

    items = boards.get_money_flow('885338', '', '2025-03-03', '2025-03-04', 'board')

    assert [(item.trade_date, item.net_inflow) for item in items] == [('2025-03-03', 1.0), ('2025-03-04', 2.0)]
    assert ts_calls == [('2025-03-04', '2025-03-04')]


def test_board_money_flow_service_reads_store_before_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        qm_boards,
        'load_store_result',
        lambda capability_id, request_identity, model_type: (
            [BoardMoneyFlowItem(board_code='885338', trade_date='2025-03-03', scope='board', net_inflow=8.0)],
            SimpleNamespace(hit=True, partial_hit=False, status='hit'),
        ),
    )
    monkeypatch.setattr(
        qm_boards._datalake,
        'get_board_money_flow',
        lambda board_code, trade_date, start_date, end_date, scope: pytest.fail('Store 命中后不应调用 datalake'),
    )

    items = boards.get_money_flow('885338', '2025-03-03', '', '', 'board')

    assert [(item.board_code, item.trade_date, item.net_inflow) for item in items] == [('885338', '2025-03-03', 8.0)]


def test_board_money_flow_service_skips_weekend_gap_fill(monkeypatch) -> None:
    ts_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        qm_boards._datalake,
        'get_board_money_flow',
        lambda board_code, trade_date, start_date, end_date, scope: [
            BoardMoneyFlowItem(board_code='885338', trade_date=trade_day, scope='board', net_inflow=1.0)
            for trade_day in ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
        ],
    )
    monkeypatch.setattr(
        qm_boards._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date=trade_day, is_open=True)
            for trade_day in ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
        ],
    )
    monkeypatch.setattr(
        qm_boards._tushare_provider,
        'get_board_money_flow',
        lambda board_code, trade_date, start_date, end_date, scope: ts_calls.append((start_date, end_date)) or [],
    )

    items = boards.get_money_flow('885338', '', '2026-04-07', '2026-04-14', 'board')

    assert [item.trade_date for item in items] == ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
    assert ts_calls == []


def test_stock_money_flow_service_only_fills_missing_date_ranges_from_ts(monkeypatch) -> None:
    ts_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        qm_stocks._datalake,
        'get_stock_money_flow',
        lambda code, trade_date, start_date, end_date, view: [
            StockMoneyFlowItem(code='600000', trade_date='2025-03-03', view='summary', net_inflow=1.0)
        ],
    )
    monkeypatch.setattr(
        qm_stocks._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date='2025-03-03', is_open=True),
            TradingCalendarItem(exchange='SSE', trade_date='2025-03-04', is_open=True),
        ],
    )
    monkeypatch.setattr(
        qm_stocks._tushare_provider,
        'get_stock_money_flow',
        lambda code, trade_date, start_date, end_date, view: ts_calls.append((start_date, end_date)) or [
            StockMoneyFlowItem(code='600000', trade_date='2025-03-04', view='summary', net_inflow=2.0)
        ],
    )

    items = stocks.get_money_flow('600000', '', '2025-03-03', '2025-03-04', 'summary')

    assert [(item.trade_date, item.net_inflow) for item in items] == [('2025-03-03', 1.0), ('2025-03-04', 2.0)]
    assert ts_calls == [('2025-03-04', '2025-03-04')]


def test_stock_money_flow_service_skips_weekend_gap_fill(monkeypatch) -> None:
    ts_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        qm_stocks._datalake,
        'get_stock_money_flow',
        lambda code, trade_date, start_date, end_date, view: [
            StockMoneyFlowItem(code='600000', trade_date=trade_day, view='summary', net_inflow=1.0)
            for trade_day in ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
        ],
    )
    monkeypatch.setattr(
        qm_stocks._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date=trade_day, is_open=True)
            for trade_day in ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
        ],
    )
    monkeypatch.setattr(
        qm_stocks._tushare_provider,
        'get_stock_money_flow',
        lambda code, trade_date, start_date, end_date, view: ts_calls.append((start_date, end_date)) or [],
    )

    items = stocks.get_money_flow('600000', '', '2026-04-07', '2026-04-14', 'summary')

    assert [item.trade_date for item in items] == ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
    assert ts_calls == []


def test_index_quotes_service_only_fills_missing_ranges_from_providers(monkeypatch) -> None:
    ts_calls: list[tuple[list[str], str, str]] = []
    ef_calls: list[tuple[list[str], str, str]] = []

    monkeypatch.setattr(
        qm_indexes._datalake,
        'get_index_quotes',
        lambda index_codes, freq, trade_date, start_date, end_date, count: [
            IndexQuoteItem(index_code='000001', trade_time='2025-03-03', freq='1d', close=3300.0)
        ],
    )
    monkeypatch.setattr(
        qm_indexes._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date='2025-03-03', is_open=True),
            TradingCalendarItem(exchange='SSE', trade_date='2025-03-04', is_open=True),
            TradingCalendarItem(exchange='SSE', trade_date='2025-03-05', is_open=True),
        ],
    )
    monkeypatch.setattr(
        qm_indexes._tushare_provider,
        'get_index_quotes',
        lambda index_codes, freq, trade_date, start_date, end_date, count: ts_calls.append((index_codes, start_date, end_date)) or [
            IndexQuoteItem(index_code='000001', trade_time='2025-03-04', freq='1d', close=3310.0)
        ],
    )
    monkeypatch.setattr(
        qm_indexes._efinance_provider,
        'get_index_quotes',
        lambda index_codes, freq, trade_date, start_date, end_date, count: ef_calls.append((index_codes, start_date, end_date)) or [
            IndexQuoteItem(index_code='000001', trade_time='2025-03-05', freq='1d', close=3320.0)
        ],
    )

    items = indexes.get_quotes('000001', '', '1d', '', '2025-03-03', '2025-03-05', None, 20)

    assert [(item.trade_time, item.close) for item in items] == [('2025-03-03', 3300.0), ('2025-03-04', 3310.0), ('2025-03-05', 3320.0)]
    assert ts_calls == [(['000001'], '2025-03-04', '2025-03-05')]
    assert ef_calls == [(['000001'], '2025-03-04', '2025-03-05')]


def test_index_quotes_service_skips_weekend_gap_fill(monkeypatch) -> None:
    ef_calls: list[tuple[list[str], str, str]] = []
    ts_calls: list[tuple[list[str], str, str]] = []

    monkeypatch.setattr(
        qm_indexes._datalake,
        'get_index_quotes',
        lambda index_codes, freq, trade_date, start_date, end_date, count: [
            IndexQuoteItem(index_code='000001', trade_time=trade_day, freq='1d', close=3300.0)
            for trade_day in ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
        ],
    )
    monkeypatch.setattr(
        qm_indexes._datalake_ref,
        'get_trading_calendar',
        lambda exchange, start_date, end_date, is_open: [
            TradingCalendarItem(exchange='SSE', trade_date=trade_day, is_open=True)
            for trade_day in ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
        ],
    )
    monkeypatch.setattr(
        qm_indexes._efinance_provider,
        'get_index_quotes',
        lambda index_codes, freq, trade_date, start_date, end_date, count: ef_calls.append((index_codes, start_date, end_date)) or [],
    )
    monkeypatch.setattr(
        qm_indexes._tushare_provider,
        'get_index_quotes',
        lambda index_codes, freq, trade_date, start_date, end_date, count: ts_calls.append((index_codes, start_date, end_date)) or [],
    )

    items = indexes.get_quotes('000001', '', '1d', '', '2026-04-07', '2026-04-14', None, 20)

    assert [item.trade_time for item in items] == ['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-13', '2026-04-14']
    assert ef_calls == []
    assert ts_calls == []


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


def test_datalake_events_query_reads_agent_view_and_source_table(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_load_news_event_frame(
        trade_date: str,
        announcement_date: str,
        crawl_date: str,
        stock_code: str,
        event_type: str,
        min_importance_score: int | None,
        sort_by: str,
        limit: int,
        offset: int,
        include_content_text: bool,
    ) -> pd.DataFrame:
        captured['trade_date'] = trade_date
        captured['announcement_date'] = announcement_date
        captured['crawl_date'] = crawl_date
        captured['stock_code'] = stock_code
        captured['event_type'] = event_type
        captured['min_importance_score'] = min_importance_score
        captured['sort_by'] = sort_by
        captured['limit'] = limit
        captured['offset'] = offset
        captured['include_content_text'] = include_content_text
        return pd.DataFrame(
            [
                {
                    'event_id': 'evt-1',
                    'trade_date': '2026-04-10',
                    'announcement_time': '2026-04-11',
                    'crawl_time': '2026-04-10 09:31:00',
                    'session_tag': '盘中',
                    'event_type': '公告',
                    'title': '测试公告',
                    'summary': '测试摘要',
                    'content_text': '测试正文',
                    'importance_score': 4,
                    'sentiment': '中性',
                    'source_name': '巨潮资讯',
                    'primary_detail_url': 'https://example.com/detail',
                    'related_stock_codes': ['600000'],
                    'related_stock_names': ['浦发银行'],
                    'related_board_codes': ['BK001'],
                    'related_board_names': ['银行'],
                    'topic_tags': ['银行'],
                    'mentioned_stock_codes': ['600000'],
                    'mentioned_stock_names': ['浦发银行'],
                    'mentioned_board_names': ['银行'],
                }
            ]
        )

    def fake_load_news_event_source_frame(event_ids: list[str]) -> pd.DataFrame:
        captured['event_ids'] = event_ids
        return pd.DataFrame(
            [
                {
                    'event_id': 'evt-1',
                    'source_table': 'cninfo_disclosure_item',
                    'source_record_id': '1001',
                    'source_name': '巨潮资讯',
                    'source_type': '公告',
                    'detail_url': 'https://example.com/detail',
                    'announcement_time': '2026-04-11',
                    'crawl_time': '2026-04-10 09:31:00',
                }
            ]
        )

    monkeypatch.setattr(datalake_news, 'load_news_event_frame', fake_load_news_event_frame)
    monkeypatch.setattr(datalake_news, 'load_news_event_source_frame', fake_load_news_event_source_frame)

    items = datalake_news.get_news_events('2026-04-10', '2026-04-11', '2026-04-10', '600000', '公告', 4, 'crawl_time', 20, 5, True)
    sources_by_event_id = datalake_news.get_news_event_sources(['evt-1'])

    assert captured == {
        'trade_date': '2026-04-10',
        'announcement_date': '2026-04-11',
        'crawl_date': '2026-04-10',
        'stock_code': '600000',
        'event_type': '公告',
        'min_importance_score': 4,
        'sort_by': 'crawl_time',
        'limit': 20,
        'offset': 5,
        'include_content_text': True,
        'event_ids': ['evt-1'],
    }
    assert len(items) == 1
    assert items[0].event_id == 'evt-1'
    assert items[0].announcement_time == '2026-04-11'
    assert items[0].crawl_time == '2026-04-10 09:31:00'
    assert items[0].related_stock_codes == ['600000']
    assert sources_by_event_id['evt-1'][0].source_table == 'cninfo_disclosure_item'
    assert sources_by_event_id['evt-1'][0].crawl_time == '2026-04-10 09:31:00'


def test_market_news_doc_endpoint() -> None:
    response = client.get('/docs/markets/events/news')
    assert response.status_code == 200
    payload = response.json()
    assert '`GET` 返回统一新闻事件流。' in payload['content']
    assert '`fact.news_event_agent_view`' in payload['content']
    assert '`crawl_time`' in payload['content']
    assert '`announcement_time`' in payload['content']
    assert '`published_at` 已从对外响应移除' in payload['content']


def test_daily_market_value_service_uses_tushare(monkeypatch) -> None:
    monkeypatch.setattr(
        qm_stocks._tushare_provider,
        'get_stock_daily_market_value',
        lambda code, codes, trade_date, start_date, end_date: [
            StockDailyMarketValueItem(code='600000', trade_date='2025-01-02', total_mv=11.0, float_mv=7.0, free_mv=None)
        ],
    )

    items = stocks.get_daily_market_value('600000', '', '2025-01-02', '', '')

    assert len(items) == 1
    assert items[0].total_mv == 11.0
    assert items[0].float_mv == 7.0
    assert items[0].free_mv is None



