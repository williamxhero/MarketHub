from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from quotemux.capabilities.inventory import CapabilityDefinition, list_capability_definitions
from quotemux.infra.db.availability import get_fact_ref_availability
from quotemux.infra.db.client import is_db_available, query_dataframe


KNOWN_OBJECT_NAMES = (
    "fact.stock_daily_1d",
    "fact.stock_bar_1m",
    "fact.stock_bar_30m",
    "fact.index_bar_1d",
    "fact.concept_daily_1d",
    "ref.trade_calendar",
    "ref.stock",
    "ref.stock_name_history",
    "ref.concept",
    "ref.concept_stock_membership",
    "ref.index",
)

STATUS_ORDER = {"healthy": 0, "not_applicable": 0, "unknown": 1, "warning": 2, "unhealthy": 3}
SUMMARY_STATUSES = ("healthy", "warning", "unhealthy")
DB_UNAVAILABLE_ERROR = "无法连接本地数据库"

OBJECT_PRIMARY_KEYS = {
    "fact.stock_daily_1d": ("market", "code", "trade_date"),
    "fact.stock_bar_1m": ("market", "code", "bar_time"),
    "fact.stock_bar_30m": ("market", "code", "bar_time"),
    "fact.index_bar_1d": ("index_code", "trade_date"),
    "fact.concept_daily_1d": ("concept_id", "trade_date"),
    "ref.trade_calendar": ("exchange", "trade_date"),
    "ref.stock": ("market", "code"),
    "ref.stock_name_history": ("market", "code", "valid_from"),
    "ref.concept": ("concept_id",),
    "ref.concept_stock_membership": ("concept_id", "stock_market", "stock_code", "valid_from"),
    "ref.index": ("index_code",),
}

OBJECT_PRIMARY_KEY_INDEXES = {
    "fact.stock_daily_1d": "stock_daily_1d_pkey",
    "fact.stock_bar_1m": "stock_bar_1m_pkey",
    "fact.stock_bar_30m": "stock_bar_30m_pkey",
    "fact.index_bar_1d": "index_bar_1d_pkey",
    "fact.concept_daily_1d": "concept_daily_1d_pkey",
    "ref.trade_calendar": "trade_calendar_pkey",
    "ref.stock": "stock_pkey",
    "ref.stock_name_history": "stock_name_history_pkey",
    "ref.concept": "concept_pkey",
    "ref.concept_stock_membership": "concept_stock_membership_pkey",
    "ref.index": "index_pkey",
}

OBJECT_TIME_COLUMNS = {
    "fact.stock_daily_1d": "trade_date",
    "fact.stock_bar_1m": "bar_time",
    "fact.stock_bar_30m": "bar_time",
    "fact.index_bar_1d": "trade_date",
    "fact.concept_daily_1d": "trade_date",
}

@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    title: str
    status: str
    result_text: str
    error_text: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "check_id": self.check_id,
            "title": self.title,
            "status": self.status,
            "result_text": self.result_text,
            "error_text": self.error_text,
        }


def get_data_health() -> dict[str, object]:
    profiles = _load_profiles()
    definitions = list_capability_definitions()
    check_cache: dict[str, CheckSpec] = {}
    db_available = is_db_available()
    fact_ref = get_fact_ref_availability() if db_available else _empty_fact_ref_availability()
    fact_ref["status"] = _normalize_status(str(fact_ref.get("status", "warning")))
    objects = _objects_by_name(fact_ref)
    calendar = _check_trade_calendar(objects, db_available)
    capabilities = [_build_capability_health(definition, profiles, objects, db_available, calendar, check_cache) for definition in definitions]
    groups = _build_group_health(capabilities)
    summary = _build_summary(capabilities)
    status = _worst_status([str(summary["status"]), str(calendar["status"]), str(fact_ref.get("status", "warning"))])
    return {
        "status": status,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "dependencies": {
            "database": {"status": "healthy" if db_available else "unhealthy", "available": db_available},
            "fact_ref": fact_ref,
            "trade_calendar": calendar,
        },
        "groups": groups,
        "capabilities": capabilities,
    }


def _load_profiles() -> dict[str, dict[str, object]]:
    profiles_path = _profiles_path()
    if not profiles_path.is_file():
        return {}
    with profiles_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    raw_profiles = payload.get("capabilities", {})
    if not isinstance(raw_profiles, dict):
        return {}
    return {str(capability_id): profile for capability_id, profile in raw_profiles.items() if isinstance(profile, dict)}


def _profiles_path() -> Path:
    return Path(__file__).resolve().with_name("data_health_checks.json")


def _empty_fact_ref_availability() -> dict[str, object]:
    return {
        "status": "unhealthy",
        "warnings": ["数据库不可用，无法检查本地 fact/ref 表"],
        "objects": [
            {"name": name, "exists": False, "missing_indexes": [], "row_count": 0, "min_value": "", "max_value": ""}
            for name in KNOWN_OBJECT_NAMES
        ],
    }


def _objects_by_name(fact_ref: dict[str, object]) -> dict[str, dict[str, object]]:
    objects: dict[str, dict[str, object]] = {}
    raw_objects = fact_ref.get("objects", [])
    if not isinstance(raw_objects, list):
        return objects
    for item in raw_objects:
        if isinstance(item, dict):
            name = str(item.get("name", ""))
            if name != "":
                objects[name] = item
    return objects


def _check_trade_calendar(objects: dict[str, dict[str, object]], db_available: bool) -> dict[str, object]:
    if not db_available:
        return {"status": "unhealthy", "recent_days": 0, "recent_open_days": 0, "max_trade_date": "", "issues": [DB_UNAVAILABLE_ERROR]}
    calendar_object = objects.get("ref.trade_calendar", {})
    if not bool(calendar_object.get("exists", False)):
        return {"status": "unhealthy", "recent_days": 0, "recent_open_days": 0, "max_trade_date": "", "issues": ["缺少 ref.trade_calendar，无法判断缺失日期是否为交易日"]}
    frame = query_dataframe(
        """
        select
            count(*)::int as recent_days,
            count(*) filter (where is_open)::int as recent_open_days,
            max(trade_date)::text as max_trade_date
        from ref.trade_calendar
        where trade_date >= current_date - interval '90 days'
          and trade_date < current_date
        """
    )
    if frame.empty:
        return {"status": "unhealthy", "recent_days": 0, "recent_open_days": 0, "max_trade_date": "", "issues": ["ref.trade_calendar 最近 90 天没有可读记录"]}
    row = frame.iloc[0]
    recent_days = int(row.get("recent_days", 0) or 0)
    recent_open_days = int(row.get("recent_open_days", 0) or 0)
    max_trade_date = "" if row.get("max_trade_date") is None else str(row.get("max_trade_date"))
    issues: list[str] = []
    if recent_days == 0:
        issues.append("ref.trade_calendar 最近 90 天没有日期记录")
    if recent_open_days == 0:
        issues.append("ref.trade_calendar 最近 90 天没有交易日记录")
    return {"status": "healthy" if issues == [] else "unhealthy", "recent_days": recent_days, "recent_open_days": recent_open_days, "max_trade_date": max_trade_date, "issues": issues}


def _build_capability_health(
    definition: CapabilityDefinition,
    profiles: dict[str, dict[str, object]],
    objects: dict[str, dict[str, object]],
    db_available: bool,
    calendar: dict[str, object],
    check_cache: dict[str, CheckSpec],
) -> dict[str, object]:
    profile = profiles.get(definition.capability_id, {})
    dependencies = _reference_objects(profile)
    checks = _build_checks(profile, objects, db_available, calendar, check_cache)
    status = _status_from_checks(checks)
    issues = [check["error_text"] for check in checks if check["error_text"] != ""]
    return {
        "capability_id": definition.capability_id,
        "group": str(profile.get("group", "未分组")),
        "status": status,
        "logic_scope": str(profile.get("logic_scope", "")),
        "reference_data": str(profile.get("reference_data", "")),
        "rule_summary": str(profile.get("pass_rule", "")),
        "empty_result_rule": str(profile.get("empty_result_rule", "")),
        "unhealthy_rule": str(profile.get("unhealthy_rule", "")),
        "dependencies": dependencies,
        "checks": checks,
        "issues": issues,
        "api_paths": list(definition.api_paths),
        "result_shape": definition.result_shape,
        "store_enabled": definition.store_enabled,
    }


def _build_checks(
    profile: dict[str, object],
    objects: dict[str, dict[str, object]],
    db_available: bool,
    calendar: dict[str, object],
    check_cache: dict[str, CheckSpec],
) -> list[dict[str, str]]:
    checks: list[CheckSpec] = []
    raw_checks = profile.get("checks", [])
    if not isinstance(raw_checks, list):
        raw_checks = []
    for raw_check in raw_checks:
        if isinstance(raw_check, dict):
            checks.extend(_run_configured_check(raw_check, profile, objects, db_available, calendar, check_cache))
    return _dedupe_checks(checks)


def _run_configured_check(
    check: dict[str, object],
    profile: dict[str, object],
    objects: dict[str, dict[str, object]],
    db_available: bool,
    calendar: dict[str, object],
    check_cache: dict[str, CheckSpec],
) -> list[CheckSpec]:
    check_type = str(check.get("type", ""))
    object_name = str(check.get("object", ""))
    if check_type == "logic_rule_registered":
        return [_logic_rule_check(profile)]
    if check_type == "database_available":
        return [_database_check(db_available)]
    if check_type == "table_exists":
        return [_table_exists_check(object_name, objects, db_available)]
    if check_type == "indexes_complete":
        return [_indexes_complete_check(object_name, objects, db_available)]
    if check_type == "table_non_empty":
        return [_table_non_empty_check(object_name, objects, db_available)]
    if check_type == "trade_calendar_available":
        return [_trade_calendar_available_check(calendar, db_available)]
    if check_type == "non_trading_day_empty_result":
        return [_calendar_rule_check("non_trading_day_empty_result", "非交易日空结果", "健康", calendar, db_available, "交易日历不可用，无法判断非交易日空结果是否健康")]
    if check_type == "trade_day_missing_data_unhealthy":
        return [_calendar_rule_check("trade_day_missing_data_unhealthy", "交易日缺失异常", "正常", calendar, db_available, "交易日历不可用，无法判断交易日缺失")]
    if check_type == "reference_valid":
        check_id = str(check.get("check_id", ""))
        title = _reference_title(check_id, object_name)
        result_text = _reference_result_text(check_id)
        return [_reference_table_check(check_id, title, object_name, result_text, objects, db_available)]
    if check_type == "capability_key_unique_rule":
        return [_capability_key_rule_check(_string_list(check.get("fields", [])))]
    if check_type == "primary_key_duplicate_absent":
        return [_cached_check(check_cache, f"primary_key_duplicate_absent:{object_name}", lambda: _duplicate_check(object_name, objects, db_available))]
    if check_type == "fixed_field":
        check_id = str(check.get("check_id", ""))
        return [_fixed_field_check(check_id, _fixed_field_title(check_id, str(check.get("expected_value", ""))))]
    if check_type == "ohlc_valid":
        return [_cached_check(check_cache, f"ohlc_valid:{object_name}", lambda: _ohlc_check(object_name, objects, db_available))]
    if check_type == "non_negative":
        column = str(check.get("column", ""))
        return [_cached_check(check_cache, f"non_negative:{object_name}:{column}", lambda: _non_negative_number_check(object_name, column, objects, db_available))]
    if check_type == "money_flow_values_valid":
        return [_cached_check(check_cache, f"money_flow_values_valid:{object_name}", lambda: _money_flow_value_check(object_name, objects, db_available))]
    if check_type == "recent_coverage_90d":
        check_id = str(check.get("check_id", ""))
        column = str(check.get("column", ""))
        provider_earliest_date = str(check.get("provider_earliest_date", ""))
        return [_cached_check(check_cache, f"recent_coverage_90d:{check_id}:{object_name}:{column}:{provider_earliest_date}", lambda: _recent_coverage_check(check_id, str(check.get("title", "")), object_name, column, provider_earliest_date, objects, db_available, calendar))]
    if check_type == "calendar_continuity_90d":
        return [_calendar_continuity_check(calendar, db_available)]
    if check_type == "long_empty":
        check_id = str(check.get("check_id", ""))
        return [_cached_check(check_cache, f"long_empty:{check_id}", lambda: _long_empty_check(check_id, str(check.get("title", "")), _string_list(check.get("required_objects", [])), objects, db_available, calendar))]
    return [CheckSpec(f"unknown_check_type:{check_type}", f"未知检查类型 {check_type}", "unknown", "未执行", "检查配置无法识别")]


def _logic_rule_check(profile: dict[str, object]) -> CheckSpec:
    if profile == {}:
        return CheckSpec("logic_rule_registered", "逻辑健康规则已登记", "unhealthy", "异常", "缺少本条 capability 的逻辑健康规则")
    return CheckSpec("logic_rule_registered", "逻辑健康规则已登记", "healthy", "健康")


def _database_check(db_available: bool) -> CheckSpec:
    if not db_available:
        return CheckSpec("database_available", "数据库可用", "unhealthy", "异常", DB_UNAVAILABLE_ERROR)
    return CheckSpec("database_available", "数据库可用", "healthy", "正常")


def _table_exists_check(object_name: str, objects: dict[str, dict[str, object]], db_available: bool) -> CheckSpec:
    if not db_available:
        return CheckSpec(f"table_exists:{object_name}", f"依赖表 {object_name}", "unhealthy", "异常", DB_UNAVAILABLE_ERROR)
    object_status = objects.get(object_name, {})
    if not bool(object_status.get("exists", False)):
        return CheckSpec(f"table_exists:{object_name}", f"依赖表 {object_name}", "unhealthy", "异常", "表不存在")
    return CheckSpec(f"table_exists:{object_name}", f"依赖表 {object_name}", "healthy", "正常")


def _indexes_complete_check(object_name: str, objects: dict[str, dict[str, object]], db_available: bool) -> CheckSpec:
    if not db_available:
        return CheckSpec(f"indexes_complete:{object_name}", f"依赖表 {object_name} 索引完整", "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    object_status = objects.get(object_name, {})
    if not bool(object_status.get("exists", False)):
        return CheckSpec(f"indexes_complete:{object_name}", f"依赖表 {object_name} 索引完整", "unknown", "未执行", f"缺少依赖表 {object_name}")
    missing_indexes = object_status.get("missing_indexes", [])
    if isinstance(missing_indexes, list) and missing_indexes != []:
        return CheckSpec(f"indexes_complete:{object_name}", f"依赖表 {object_name} 索引完整", "warning", "警告", "缺少索引: " + ", ".join(str(item) for item in missing_indexes))
    return CheckSpec(f"indexes_complete:{object_name}", f"依赖表 {object_name} 索引完整", "healthy", "正常")


def _table_non_empty_check(object_name: str, objects: dict[str, dict[str, object]], db_available: bool) -> CheckSpec:
    if not db_available:
        return CheckSpec(f"table_non_empty:{object_name}", f"依赖表 {object_name} 非空", "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    object_status = objects.get(object_name, {})
    if not bool(object_status.get("exists", False)):
        return CheckSpec(f"table_non_empty:{object_name}", f"依赖表 {object_name} 非空", "unknown", "未执行", f"缺少依赖表 {object_name}")
    row_count = int(object_status.get("row_count", 0) or 0)
    if row_count <= 0:
        return CheckSpec(f"table_non_empty:{object_name}", f"依赖表 {object_name} 非空", "warning", "警告", "表为空")
    return CheckSpec(f"table_non_empty:{object_name}", f"依赖表 {object_name} 非空", "healthy", "正常")


def _trade_calendar_available_check(calendar: dict[str, object], db_available: bool) -> CheckSpec:
    if not db_available:
        return CheckSpec("trade_calendar_available", "交易日历可用", "unhealthy", "异常", DB_UNAVAILABLE_ERROR)
    if str(calendar.get("status", "unhealthy")) != "healthy":
        return CheckSpec("trade_calendar_available", "交易日历可用", "unhealthy", "异常", _join_issues(calendar.get("issues", []), "交易日历不可用"))
    return CheckSpec("trade_calendar_available", "交易日历可用", "healthy", "正常")


def _calendar_rule_check(check_id: str, title: str, success_text: str, calendar: dict[str, object], db_available: bool, unavailable_text: str) -> CheckSpec:
    if not db_available or str(calendar.get("status", "unhealthy")) != "healthy":
        return CheckSpec(check_id, title, "unknown", "未执行", unavailable_text)
    return CheckSpec(check_id, title, "healthy", success_text)


def _reference_table_check(check_id: str, title: str, object_name: str, result_text: str, objects: dict[str, dict[str, object]], db_available: bool) -> CheckSpec:
    if not db_available:
        return CheckSpec(check_id, title, "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    object_status = objects.get(object_name, {})
    if not bool(object_status.get("exists", False)):
        return CheckSpec(check_id, title, "unhealthy", "异常", f"缺少依赖表 {object_name}")
    if int(object_status.get("row_count", 0) or 0) <= 0:
        return CheckSpec(check_id, title, "warning", "警告", f"依赖表 {object_name} 为空")
    return CheckSpec(check_id, title, "healthy", result_text)


def _capability_key_rule_check(key_fields: list[str]) -> CheckSpec:
    if key_fields == []:
        return CheckSpec("capability_key_unique_rule", "capability 主键唯一", "not_applicable", "不适用")
    title = "同一 " + "/".join(key_fields) + " 唯一"
    return CheckSpec("capability_key_unique_rule", title, "healthy", "正常")


def _fixed_field_check(check_id: str, title: str) -> CheckSpec:
    return CheckSpec(check_id, title, "healthy", "正常")


def _duplicate_check(object_name: str, objects: dict[str, dict[str, object]], db_available: bool) -> CheckSpec:
    title = f"{object_name} 主键重复"
    if not db_available:
        return CheckSpec(f"primary_key_duplicate_absent:{object_name}", title, "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    if not _object_exists(object_name, objects):
        return CheckSpec(f"primary_key_duplicate_absent:{object_name}", title, "unknown", "未执行", f"缺少依赖表 {object_name}")
    columns = OBJECT_PRIMARY_KEYS.get(object_name, ())
    if columns == ():
        return CheckSpec(f"primary_key_duplicate_absent:{object_name}", title, "not_applicable", "不适用")
    object_status = objects.get(object_name, {})
    primary_key_index = OBJECT_PRIMARY_KEY_INDEXES.get(object_name, "")
    missing_indexes = object_status.get("missing_indexes", [])
    if primary_key_index != "" and isinstance(missing_indexes, list) and primary_key_index not in missing_indexes:
        return CheckSpec(f"primary_key_duplicate_absent:{object_name}", title, "healthy", "无")
    if primary_key_index != "":
        return CheckSpec(f"primary_key_duplicate_absent:{object_name}", title, "unknown", "未执行", f"缺少主键索引 {primary_key_index}，无法轻量验证主键重复")
    column_sql = ", ".join(columns)
    frame = query_dataframe(f"select count(*)::int as duplicate_group_count from (select {column_sql} from {object_name} group by {column_sql} having count(*) > 1 limit 1) duplicated")
    if frame.empty:
        return CheckSpec(f"primary_key_duplicate_absent:{object_name}", title, "unknown", "未执行", "主键重复检查查询无结果")
    duplicate_group_count = int(frame.iloc[0].get("duplicate_group_count", 0) or 0)
    if duplicate_group_count > 0:
        return CheckSpec(f"primary_key_duplicate_absent:{object_name}", title, "unhealthy", "异常", "存在主键重复记录")
    return CheckSpec(f"primary_key_duplicate_absent:{object_name}", title, "healthy", "无")


def _ohlc_check(object_name: str, objects: dict[str, dict[str, object]], db_available: bool) -> CheckSpec:
    if not db_available:
        return CheckSpec(f"ohlc_valid:{object_name}", f"{object_name} OHLC 合法", "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    if not _object_exists(object_name, objects):
        return CheckSpec(f"ohlc_valid:{object_name}", f"{object_name} OHLC 合法", "unknown", "未执行", f"缺少依赖表 {object_name}")
    window_clause = _recent_window_clause(object_name)
    frame = query_dataframe(
        f"""
        select count(*)::int as invalid_count
        from {object_name}
        where open is not null
          and high is not null
          and low is not null
          and close is not null
          and (low > high or open < low or open > high or close < low or close > high)
          {window_clause}
        """
    )
    return _count_check_result(f"ohlc_valid:{object_name}", f"{object_name} OHLC 合法", frame, "OHLC 关系异常")


def _non_negative_number_check(object_name: str, column: str, objects: dict[str, dict[str, object]], db_available: bool) -> CheckSpec:
    if column == "":
        return CheckSpec(f"non_negative:{object_name}", f"{object_name} 数值合法", "not_applicable", "不适用")
    if not db_available:
        return CheckSpec(f"non_negative:{object_name}:{column}", f"{object_name} {column} 数值合法", "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    if not _object_exists(object_name, objects):
        return CheckSpec(f"non_negative:{object_name}:{column}", f"{object_name} {column} 数值合法", "unknown", "未执行", f"缺少依赖表 {object_name}")
    window_clause = _recent_window_clause(object_name)
    frame = query_dataframe(f"select count(*)::int as invalid_count from {object_name} where {column} is not null and {column} < 0 {window_clause}")
    return _count_check_result(f"non_negative:{object_name}:{column}", f"{object_name} {column} 数值合法", frame, f"{column} 出现负数")


def _money_flow_value_check(object_name: str, objects: dict[str, dict[str, object]], db_available: bool) -> CheckSpec:
    if object_name == "":
        return CheckSpec("money_flow_values_valid", "inflow/outflow/net_inflow 数值合法", "not_applicable", "不适用")
    if not db_available:
        return CheckSpec("money_flow_values_valid", "inflow/outflow/net_inflow 数值合法", "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    if not _object_exists(object_name, objects):
        return CheckSpec("money_flow_values_valid", "inflow/outflow/net_inflow 数值合法", "unknown", "未执行", f"缺少依赖表 {object_name}")
    columns = _table_columns(object_name)
    value_columns = [column for column in ("inflow", "outflow", "net_inflow") if column in columns]
    if value_columns == []:
        return CheckSpec("money_flow_values_valid", "inflow/outflow/net_inflow 数值合法", "unknown", "未执行", "本地依赖表未提供 inflow/outflow/net_inflow 字段，无法执行资金流数值检查")
    clauses = " or ".join(f"{column} is null or {column}::text in ('NaN', 'Infinity', '-Infinity')" for column in value_columns)
    window_clause = _recent_window_clause(object_name)
    frame = query_dataframe(f"select count(*)::int as invalid_count from {object_name} where ({clauses}) {window_clause}")
    return _count_check_result("money_flow_values_valid", "inflow/outflow/net_inflow 数值合法", frame, "资金流字段出现非法数值")


def _recent_coverage_check(check_id: str, title: str, object_name: str, column: str, provider_earliest_date: str, objects: dict[str, dict[str, object]], db_available: bool, calendar: dict[str, object]) -> CheckSpec:
    if title == "":
        title = f"{object_name} 最近 90 天覆盖"
    if not db_available:
        return CheckSpec(check_id, title, "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    if str(calendar.get("status", "unhealthy")) != "healthy":
        return CheckSpec(check_id, title, "unknown", "未执行", "交易日历不可用，无法判断覆盖缺口")
    if not _object_exists(object_name, objects):
        return CheckSpec(check_id, title, "unknown", "未执行", f"缺少依赖表 {object_name}")
    if not _is_iso_date(provider_earliest_date):
        return CheckSpec(check_id, title, "unknown", "未执行", f"provider_earliest_date 配置非法: {provider_earliest_date}")
    frame = query_dataframe(
        f"""
        select count(distinct {column}::date)::int as actual_days
        from {object_name}
        where {column} >= greatest((current_date - interval '90 days')::date, %s::date)
          and {column} < current_date
        """,
        (provider_earliest_date,),
    )
    if frame.empty:
        return CheckSpec(check_id, title, "unknown", "未执行", "覆盖检查查询无结果")
    actual_days = int(frame.iloc[0].get("actual_days", 0) or 0)
    expected_frame = query_dataframe(
        """
        select count(*)::int as expected_days
        from ref.trade_calendar
        where is_open
          and trade_date >= greatest((current_date - interval '90 days')::date, %s::date)
          and trade_date < current_date
        """,
        (provider_earliest_date,),
    )
    if expected_frame.empty:
        return CheckSpec(check_id, title, "unknown", "未执行", "交易日历覆盖分母查询无结果")
    expected_days = int(expected_frame.iloc[0].get("expected_days", 0) or 0)
    if expected_days <= 0:
        return CheckSpec(check_id, title, "unknown", "未执行", "provider 起点后的最近窗口没有交易日记录")
    if actual_days <= 0:
        return CheckSpec(check_id, title, "unhealthy", "异常", f"provider 起点 {provider_earliest_date} 后的最近窗口没有本地覆盖数据")
    if actual_days < expected_days:
        return CheckSpec(check_id, title, "warning", "警告", f"provider 起点 {provider_earliest_date} 后的最近窗口覆盖 {actual_days}/{expected_days} 个交易日")
    return CheckSpec(check_id, title, "healthy", "正常")


def _calendar_continuity_check(calendar: dict[str, object], db_available: bool) -> CheckSpec:
    if not db_available:
        return CheckSpec("calendar_continuity_90d", "交易日历最近 90 天连续覆盖", "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    if str(calendar.get("status", "unhealthy")) != "healthy":
        return CheckSpec("calendar_continuity_90d", "交易日历最近 90 天连续覆盖", "unhealthy", "异常", _join_issues(calendar.get("issues", []), "交易日历不可用"))
    return CheckSpec("calendar_continuity_90d", "交易日历最近 90 天连续覆盖", "healthy", "正常")


def _long_empty_check(check_id: str, title: str, required_objects: list[str], objects: dict[str, dict[str, object]], db_available: bool, calendar: dict[str, object]) -> CheckSpec:
    if title == "":
        title = "长期无数据检查"
    if not db_available:
        return CheckSpec(check_id, title, "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    if str(calendar.get("status", "unhealthy")) != "healthy":
        return CheckSpec(check_id, title, "unknown", "未执行", "交易日历不可用，无法判断长期无数据")
    missing = [object_name for object_name in required_objects if not _object_exists(object_name, objects)]
    if missing != []:
        return CheckSpec(check_id, title, "unknown", "未执行", "缺少依赖表 " + ", ".join(missing))
    return CheckSpec(check_id, title, "healthy", "正常")


def _count_check_result(check_id: str, title: str, frame: object, error_text: str) -> CheckSpec:
    if getattr(frame, "empty", True):
        return CheckSpec(check_id, title, "unknown", "未执行", "检查查询无结果")
    invalid_count = int(frame.iloc[0].get("invalid_count", 0) or 0)
    if invalid_count > 0:
        return CheckSpec(check_id, title, "unhealthy", "异常", f"{error_text}: {invalid_count} 条")
    return CheckSpec(check_id, title, "healthy", "正常")


def _table_columns(object_name: str) -> set[str]:
    schema_name, table_name = object_name.split(".", 1)
    frame = query_dataframe(
        """
        select column_name
        from information_schema.columns
        where table_schema = %s and table_name = %s
        """,
        (schema_name, table_name),
    )
    if frame.empty:
        return set()
    return {str(row["column_name"]) for _, row in frame.iterrows()}


def _recent_window_clause(object_name: str) -> str:
    time_column = OBJECT_TIME_COLUMNS.get(object_name, "")
    if time_column == "":
        return ""
    return f"and {time_column} >= current_date - interval '90 days' and {time_column} < current_date"


def _is_iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _cached_check(cache: dict[str, CheckSpec], key: str, factory: Callable[[], CheckSpec]) -> CheckSpec:
    cached = cache.get(key)
    if cached is not None:
        return cached
    check = factory()
    cache[key] = check
    return check


def _reference_objects(profile: dict[str, object]) -> list[str]:
    return _string_list(profile.get("reference_objects", []))


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item) != ""]


def _reference_title(check_id: str, object_name: str) -> str:
    titles = {
        "stock_reference_valid": "stock 引用有效",
        "stock_lifecycle_valid": "stock 生命周期有效",
        "concept_reference_valid": "concept_id 引用有效",
        "concept_membership_valid": "概念成员有效",
        "index_reference_valid": "index 引用有效",
    }
    return titles.get(check_id, f"{object_name} 引用有效")


def _reference_result_text(check_id: str) -> str:
    if check_id in {"stock_reference_valid", "stock_lifecycle_valid", "concept_reference_valid", "concept_membership_valid", "index_reference_valid"}:
        return "有效"
    return "正常"


def _fixed_field_title(check_id: str, expected_value: str) -> str:
    if check_id == "scope_fixed":
        return "scope 固定为 concept"
    if check_id == "freq_valid":
        return "freq 固定值合法"
    if expected_value != "":
        return f"{check_id} 固定为 {expected_value}"
    return check_id


def _object_exists(object_name: str, objects: dict[str, dict[str, object]]) -> bool:
    return bool(objects.get(object_name, {}).get("exists", False))


def _join_issues(raw_issues: object, default: str) -> str:
    if isinstance(raw_issues, list) and raw_issues != []:
        return "；".join(str(issue) for issue in raw_issues)
    return default


def _dedupe_checks(checks: list[CheckSpec]) -> list[dict[str, str]]:
    deduped: dict[str, CheckSpec] = {}
    for check in checks:
        deduped.setdefault(check.check_id, check)
    return [check.as_dict() for check in deduped.values()]


def _build_group_health(capabilities: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for capability in capabilities:
        group = str(capability.get("group", "未分组"))
        groups.setdefault(group, []).append(capability)
    return [_build_group_item(group, items) for group, items in sorted(groups.items())]


def _build_group_item(group: str, capabilities: list[dict[str, object]]) -> dict[str, object]:
    summary = _build_summary(capabilities)
    return {"group": group, **summary}


def _build_summary(capabilities: list[dict[str, object]]) -> dict[str, object]:
    healthy = _count_status(capabilities, "healthy")
    warning = _count_status(capabilities, "warning")
    unhealthy = _count_status(capabilities, "unhealthy")
    status = "unhealthy" if unhealthy > 0 else "warning" if warning > 0 else "healthy"
    return {"status": status, "total": len(capabilities), "healthy": healthy, "warning": warning, "unhealthy": unhealthy}


def _count_status(capabilities: list[dict[str, object]], status: str) -> int:
    return sum(1 for capability in capabilities if str(capability.get("status", "")) == status)


def _status_from_checks(checks: list[dict[str, str]]) -> str:
    statuses = [str(check.get("status", "unknown")) for check in checks]
    if "unhealthy" in statuses:
        return "unhealthy"
    if "warning" in statuses or "unknown" in statuses:
        return "warning"
    return "healthy"


def _worst_status(statuses: list[str]) -> str:
    return max(statuses, key=lambda status: STATUS_ORDER.get(status, 1))


def _normalize_status(status: str) -> str:
    if status == "ok":
        return "healthy"
    if status in STATUS_ORDER:
        return status
    return "warning"
