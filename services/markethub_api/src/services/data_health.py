from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.config import DATA_ROOT
from quotemux.capabilities.inventory import CapabilityDefinition, list_capability_definitions
from quotemux.infra.db.availability import get_fact_ref_availability
from quotemux.infra.db.client import is_db_available, query_dataframe


KNOWN_OBJECT_NAMES = (
    "fact.stock_daily_1d",
    "fact.stock_bar_1m",
    "fact.stock_bar_30m",
    "fact.index_bar_1d",
    "fact.board_daily_1d",
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
    "fact.board_daily_1d": ("board_code", "trade_date"),
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
    "fact.board_daily_1d": "board_daily_1d_pkey",
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
    "fact.board_daily_1d": "trade_date",
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
    return _read_latest_payload()


def run_data_health_check() -> dict[str, object]:
    payload = _compute_data_health()
    _write_latest_payload(payload)
    return payload


def _compute_data_health() -> dict[str, object]:
    profiles = _load_profiles()
    definitions = list_capability_definitions()
    check_cache: dict[str, object] = {}
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


def _read_latest_payload() -> dict[str, object]:
    latest_path = _latest_payload_path()
    if not latest_path.is_file():
        return _empty_latest_payload()
    try:
        with latest_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return _empty_latest_payload()
    if not isinstance(payload, dict):
        return _empty_latest_payload()
    return payload


def _write_latest_payload(payload: dict[str, object]) -> None:
    latest_path = _latest_payload_path()
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = latest_path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    tmp_path.replace(latest_path)


def _latest_payload_path() -> Path:
    return _data_health_root() / "latest.json"


def _data_health_root() -> Path:
    root_text = os.getenv("MARKETHUB_DATA_HEALTH_ROOT", "")
    if root_text != "":
        return Path(root_text)
    runtime_root_text = os.getenv("MARKETHUB_RUNTIME_ROOT", "")
    if runtime_root_text != "":
        return Path(runtime_root_text) / "data-health"
    return DATA_ROOT / "data-health"


def _empty_latest_payload() -> dict[str, object]:
    return {
        "status": "unknown",
        "checked_at": "",
        "summary": {"status": "unknown", "total": 0, "healthy": 0, "warning": 0, "unhealthy": 0},
        "dependencies": {},
        "groups": [],
        "capabilities": [],
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
    check_cache: dict[str, object],
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
    check_cache: dict[str, object],
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
    check_cache: dict[str, object],
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
    if check_type == "recent_minute_session_complete":
        check_id = str(check.get("check_id", ""))
        expected_minutes = int(check.get("expected_minutes", 241) or 241)
        lookback_days = int(check.get("lookback_days", 10) or 10)
        min_row_ratio = float(check.get("min_row_ratio", 0.95) or 0.95)
        return [_cached_check(check_cache, f"recent_minute_session_complete:{check_id}:{object_name}:{expected_minutes}:{lookback_days}:{min_row_ratio}", lambda: _recent_minute_session_check(check_id, str(check.get("title", "")), object_name, expected_minutes, lookback_days, min_row_ratio, objects, db_available, calendar))]
    if check_type == "calendar_continuity_90d":
        return [_calendar_continuity_check(calendar, db_available)]
    if check_type == "long_empty":
        check_id = str(check.get("check_id", ""))
        return [_cached_check(check_cache, f"long_empty:{check_id}", lambda: _long_empty_check(check_id, str(check.get("title", "")), _string_list(check.get("required_objects", [])), objects, db_available, calendar))]
    if check_type == "market_data_contract":
        metric_names = _string_list(check.get("metrics", []))
        index_code = str(check.get("index_code", "000001"))
        required_objects = _string_list(check.get("required_objects", []))
        return _market_data_contract_checks(metric_names, index_code, required_objects, objects, db_available, calendar, check_cache)
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


def _recent_minute_session_check(
    check_id: str,
    title: str,
    object_name: str,
    expected_minutes: int,
    lookback_days: int,
    min_row_ratio: float,
    objects: dict[str, dict[str, object]],
    db_available: bool,
    calendar: dict[str, object],
) -> CheckSpec:
    if check_id == "":
        check_id = f"recent_minute_session_complete:{object_name}"
    if title == "":
        title = f"{object_name} 最近交易日分钟完整性"
    if not db_available:
        return CheckSpec(check_id, title, "unknown", "未执行", DB_UNAVAILABLE_ERROR)
    if str(calendar.get("status", "unhealthy")) != "healthy":
        return CheckSpec(check_id, title, "unknown", "未执行", "交易日历不可用，无法判断分钟完整性")
    if not _object_exists(object_name, objects):
        return CheckSpec(check_id, title, "unknown", "未执行", f"缺少依赖表 {object_name}")
    if expected_minutes <= 0 or lookback_days <= 0:
        return CheckSpec(check_id, title, "unknown", "未执行", "分钟完整性检查配置非法")
    frame = query_dataframe(
        f"""
        with clock as (
            select
                (now() at time zone 'Asia/Shanghai')::date as today,
                (now() at time zone 'Asia/Shanghai')::time as now_time
        ),
        expected_days as (
            select ref.trade_calendar.trade_date
            from ref.trade_calendar
            cross join clock
            where ref.trade_calendar.is_open
              and ref.trade_calendar.trade_date >= clock.today - %s::int
              and (
                  ref.trade_calendar.trade_date < clock.today
                  or (ref.trade_calendar.trade_date = clock.today and clock.now_time >= time '15:10')
              )
        ),
        expected_minutes as (
            select expected_days.trade_date, expected_days.trade_date + minute_series.minute_time as bar_time
            from expected_days
            cross join (
                select time '09:31' + minute_offset * interval '1 minute' as minute_time
                from generate_series(0, 119) as minute_offset
                union all
                select time '13:01' + minute_offset * interval '1 minute' as minute_time
                from generate_series(0, 119) as minute_offset
            ) minute_series
        ),
        minute_counts as (
            select
                expected_minutes.trade_date as trade_date,
                expected_minutes.bar_time::time as minute_time,
                count(bars.*)::int as row_count
            from expected_minutes
            left join {object_name} bars
              on bars.{OBJECT_TIME_COLUMNS[object_name]} = expected_minutes.bar_time
            group by 1, 2
        ),
        day_stats as (
            select
                trade_date,
                count(*) filter (where row_count > 0)::int as minute_count,
                sum(row_count)::bigint as total_rows,
                min(minute_time) filter (where row_count > 0)::text as first_minute,
                max(minute_time) filter (where row_count > 0)::text as last_minute,
                min(row_count) filter (where row_count > 0)::int as min_rows_per_minute,
                max(row_count)::int as max_rows_per_minute
            from minute_counts
            group by trade_date
        )
        select
            expected_days.trade_date::text as trade_date,
            coalesce(day_stats.minute_count, 0)::int as minute_count,
            coalesce(day_stats.total_rows, 0)::bigint as total_rows,
            coalesce(day_stats.first_minute, '') as first_minute,
            coalesce(day_stats.last_minute, '') as last_minute,
            coalesce(day_stats.min_rows_per_minute, 0)::int as min_rows_per_minute,
            coalesce(day_stats.max_rows_per_minute, 0)::int as max_rows_per_minute
        from expected_days
        left join day_stats on day_stats.trade_date = expected_days.trade_date
        order by expected_days.trade_date desc
        """,
        (lookback_days,),
    )
    if frame.empty:
        return CheckSpec(check_id, title, "unknown", "未执行", "最近窗口没有已完成交易日")
    issues: list[str] = []
    for _, row in frame.iterrows():
        trade_date = str(row.get("trade_date", ""))
        minute_count = int(row.get("minute_count", 0) or 0)
        min_rows = int(row.get("min_rows_per_minute", 0) or 0)
        max_rows = int(row.get("max_rows_per_minute", 0) or 0)
        if minute_count <= 0:
            issues.append(f"{trade_date} 无 1m 数据")
            continue
        first_minute = _minute_text(str(row.get("first_minute", "")))
        last_minute = _minute_text(str(row.get("last_minute", "")))
        day_issues: list[str] = []
        if minute_count < expected_minutes:
            day_issues.append(f"分钟数 {minute_count}/{expected_minutes}")
        if first_minute != "09:31" or last_minute != "15:00":
            day_issues.append(f"首尾分钟 {first_minute}-{last_minute}")
        if max_rows > 0 and min_rows < int(max_rows * min_row_ratio):
            day_issues.append(f"每分钟股票数 {min_rows}-{max_rows}")
        if day_issues != []:
            issues.append(f"{trade_date} " + "，".join(day_issues))
    if issues != []:
        return CheckSpec(check_id, title, "warning", "警告", "；".join(issues[:8]))
    return CheckSpec(check_id, title, "healthy", f"最近 {len(frame.index)} 个已完成交易日完整")


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


MARKET_DATA_CONTRACT_REQUIRED_OBJECTS = (
    "fact.index_bar_1d",
    "fact.concept_daily_1d",
    "fact.stock_daily_1d",
    "fact.board_daily_1d",
    "ref.index",
    "ref.stock",
    "ref.concept",
    "ref.concept_stock_membership",
)

MARKET_DATA_CONTRACT_EXPECTATIONS: dict[str, tuple[int, str, str]] = {
    "first_stage_index_trade_date_count": (6, "eq", "第一阶段核心指数最近 6 根日线"),
    "first_stage_index_core_null_count": (0, "eq", "第一阶段核心指数核心字段非空"),
    "first_stage_index_invalid_volume_amount_count": (0, "eq", "第一阶段核心指数成交量和成交额为正"),
    "first_stage_ref_index_missing_count": (0, "eq", "第一阶段核心指数存在目录元数据"),
    "first_stage_ref_index_bad_name_count": (0, "eq", "第一阶段核心指数名称有效"),
    "first_stage_concept_trade_date_count": (3, "eq", "第一阶段概念最近 3 个交易日"),
    "first_stage_incomplete_concept_count": (0, "eq", "第一阶段概念日线覆盖完整"),
    "first_stage_concept_core_invalid_count": (0, "eq", "第一阶段概念涨跌幅和成交额有效"),
    "second_stage_trade_date_count": (8, "eq", "第二阶段概念最近 8 个交易日"),
    "second_stage_incomplete_concept_count": (0, "eq", "第二阶段概念日线覆盖完整"),
    "second_stage_null_pct_chg_count": (0, "eq", "第二阶段概念涨跌幅非空"),
    "second_stage_invalid_amount_count": (0, "eq", "第二阶段概念成交额为正"),
    "second_stage_missing_member_stock_count": (0, "eq", "第二阶段概念成分匹配当日个股日线"),
    "second_stage_missing_member_pre_close_count": (0, "eq", "第二阶段概念成分前收完整"),
    "second_stage_invalid_market_code_count": (0, "eq", "第二阶段概念成分市场和代码前缀一致"),
    "second_stage_amount_mismatch_count": (0, "eq", "第二阶段概念成交额可由成分股重算一致"),
    "second_stage_pct_chg_mismatch_count": (0, "eq", "第二阶段概念涨跌幅可由成分股重算一致"),
    "global_latest_date_mismatch_count": (0, "eq", "核心事实表最新交易日一致"),
    "global_duplicate_concept_key_count": (0, "eq", "概念日线主键不重复"),
    "global_duplicate_stock_key_count": (0, "eq", "个股日线主键不重复"),
    "global_duplicate_board_key_count": (0, "eq", "板块日线主键不重复"),
    "global_duplicate_index_key_count": (0, "eq", "指数日线主键不重复"),
    "global_board_daily_core_null_count": (0, "eq", "板块日线当日核心字段非空"),
    "global_stock_daily_core_null_count": (0, "eq", "个股日线当日核心字段非空"),
    "global_stock_daily_without_ref_count": (0, "eq", "当日个股日线匹配股票目录"),
    "global_active_stock_without_daily_count": (0, "eq", "有效股票有当日日线"),
    "global_missing_board_type_column_count": (0, "eq", "股票目录提供 board_type 字段"),
    "global_blank_board_type_count": (0, "eq", "股票目录 board_type 非空"),
    "global_board_type_listing_board_mismatch_count": (0, "eq", "股票目录 board_type 与 listing_board 一致"),
    "global_index_daily_without_ref_count": (0, "eq", "当日指数日线匹配指数目录"),
    "global_ref_index_bad_name_count": (0, "eq", "指数目录名称有效"),
}


def _market_data_contract_checks(
    metric_names: list[str],
    index_code: str,
    required_objects: list[str],
    objects: dict[str, dict[str, object]],
    db_available: bool,
    calendar: dict[str, object],
    check_cache: dict[str, object],
) -> list[CheckSpec]:
    if metric_names == []:
        metric_names = list(MARKET_DATA_CONTRACT_EXPECTATIONS.keys())
    required = required_objects or list(MARKET_DATA_CONTRACT_REQUIRED_OBJECTS)
    unavailable_check = _market_data_contract_unavailable(metric_names, required, objects, db_available, calendar)
    if unavailable_check is not None:
        return unavailable_check
    metrics = _cached_market_data_contract_metrics(check_cache, index_code)
    if metrics == {}:
        return [_unknown_market_data_contract_check(metric_name, "市场数据契约查询无结果") for metric_name in metric_names]
    return [_market_data_contract_check(metric_name, metrics) for metric_name in metric_names]


def _market_data_contract_unavailable(metric_names: list[str], required_objects: list[str], objects: dict[str, dict[str, object]], db_available: bool, calendar: dict[str, object]) -> list[CheckSpec] | None:
    if not db_available:
        return [_unknown_market_data_contract_check(metric_name, DB_UNAVAILABLE_ERROR) for metric_name in metric_names]
    if str(calendar.get("status", "unhealthy")) != "healthy":
        return [_unknown_market_data_contract_check(metric_name, "交易日历不可用，无法执行市场数据契约检查") for metric_name in metric_names]
    missing = [object_name for object_name in required_objects if not _object_exists(object_name, objects)]
    if missing != []:
        return [_unknown_market_data_contract_check(metric_name, "缺少依赖表 " + ", ".join(missing)) for metric_name in metric_names]
    return None


def _cached_market_data_contract_metrics(check_cache: dict[str, object], index_code: str) -> dict[str, int]:
    key = f"market_data_contract_metrics:{index_code}"
    cached = check_cache.get(key)
    if isinstance(cached, dict):
        return {str(metric_name): int(value or 0) for metric_name, value in cached.items()}
    metrics = _query_market_data_contract_metrics(index_code)
    check_cache[key] = metrics
    return metrics


def _market_data_contract_check(metric_name: str, metrics: dict[str, int]) -> CheckSpec:
    expected, operator, title = MARKET_DATA_CONTRACT_EXPECTATIONS.get(metric_name, (0, "eq", metric_name))
    actual = int(metrics.get(metric_name, 0) or 0)
    passed = actual >= expected if operator == "gte" else actual == expected
    check_id = f"market_data_contract:{metric_name}"
    if passed:
        return CheckSpec(check_id, title, "healthy", f"{actual}")
    operator_text = ">=" if operator == "gte" else "="
    return CheckSpec(check_id, title, "unhealthy", "异常", f"实际 {actual}，期望 {operator_text} {expected}")


def _unknown_market_data_contract_check(metric_name: str, error_text: str) -> CheckSpec:
    title = MARKET_DATA_CONTRACT_EXPECTATIONS.get(metric_name, (0, "eq", metric_name))[2]
    return CheckSpec(f"market_data_contract:{metric_name}", title, "unknown", "未执行", error_text)


def _query_market_data_contract_metrics(index_code: str) -> dict[str, int]:
    frame = query_dataframe(
        """
        with target as (
            select least(
                (select max(trade_date) from fact.index_bar_1d where index_code = %s),
                (select max(trade_date) from fact.concept_daily_1d),
                (select max(trade_date) from fact.stock_daily_1d),
                (select max(trade_date) from fact.board_daily_1d)
            ) as trade_date,
            %s::varchar as index_code
        ), recent_index_bars as (
            select index_rows.*
            from fact.index_bar_1d index_rows
            cross join target
            where index_rows.index_code = target.index_code
              and index_rows.trade_date <= target.trade_date
            order by index_rows.trade_date desc
            limit 6
        ), first_stage_trade_dates as (
            select distinct concept_rows.trade_date
            from fact.concept_daily_1d concept_rows
            cross join target
            where concept_rows.trade_date <= target.trade_date
            order by concept_rows.trade_date desc
            limit 3
        ), trade_dates as (
            select distinct concept_rows.trade_date
            from fact.concept_daily_1d concept_rows
            cross join target
            where concept_rows.trade_date <= target.trade_date
            order by concept_rows.trade_date desc
            limit 8
        ), active_members as (
            select distinct membership.concept_id, membership.stock_market, membership.stock_code
            from ref.concept_stock_membership membership
            cross join target
            where membership.valid_from <= target.trade_date
              and (membership.valid_to is null or membership.valid_to >= target.trade_date)
        ), member_counts as (
            select concept_id, count(*) as member_count
            from active_members
            group by concept_id
        ), eligible_concepts as (
            select concept_rows.concept_id, concept_ref.name, member_counts.member_count
            from fact.concept_daily_1d concept_rows
            cross join target
            join ref.concept concept_ref on concept_ref.concept_id = concept_rows.concept_id
            join member_counts on member_counts.concept_id = concept_rows.concept_id
            where concept_rows.trade_date = target.trade_date
              and member_counts.member_count <= 500
              and concept_ref.name is not null
              and concept_ref.name <> ''
        ), concept_coverage as (
            select eligible_concepts.concept_id, count(concept_rows.trade_date) as bar_count
            from eligible_concepts
            cross join trade_dates
            left join fact.concept_daily_1d concept_rows
              on concept_rows.concept_id = eligible_concepts.concept_id
             and concept_rows.trade_date = trade_dates.trade_date
            group by eligible_concepts.concept_id
        ), first_stage_concept_coverage as (
            select eligible_concepts.concept_id, count(concept_rows.trade_date) as bar_count
            from eligible_concepts
            cross join first_stage_trade_dates
            left join fact.concept_daily_1d concept_rows
              on concept_rows.concept_id = eligible_concepts.concept_id
             and concept_rows.trade_date = first_stage_trade_dates.trade_date
            group by eligible_concepts.concept_id
        ), current_members as (
            select membership.concept_id, membership.stock_market, membership.stock_code
            from active_members membership
            join eligible_concepts on eligible_concepts.concept_id = membership.concept_id
            cross join target
            join ref.stock stock_ref
              on stock_ref.market = membership.stock_market
             and stock_ref.code = membership.stock_code
             and stock_ref.listed_date <= target.trade_date
             and (stock_ref.delisted_date is null or stock_ref.delisted_date >= target.trade_date)
        ), stock_history as (
            select
                stock_rows.market,
                stock_rows.code,
                stock_rows.trade_date,
                stock_rows.close,
                stock_rows.pct_chg,
                stock_rows.amount,
                stock_rows.is_st,
                stock_rows.is_suspended,
                lag(stock_rows.close) over(partition by stock_rows.market, stock_rows.code order by stock_rows.trade_date) as pre_close
            from fact.stock_daily_1d stock_rows
            cross join target
            where stock_rows.trade_date between target.trade_date - interval '20 days' and target.trade_date
        ), current_stock as (
            select stock_history.*
            from stock_history
            cross join target
            where stock_history.trade_date = target.trade_date
        ), expected_concept as (
            select current_members.concept_id,
                   sum(current_stock.amount) filter (
                       where coalesce(current_stock.is_suspended, false) = false
                         and coalesce(current_stock.is_st, false) = false
                         and current_stock.close is not null
                         and current_stock.pre_close is not null
                         and current_stock.amount > 0
                   ) as amount,
                   sum(current_stock.pct_chg * current_stock.amount) filter (
                       where coalesce(current_stock.is_suspended, false) = false
                         and coalesce(current_stock.is_st, false) = false
                         and current_stock.pct_chg is not null
                         and current_stock.amount > 0
                   ) / nullif(sum(current_stock.amount) filter (
                       where coalesce(current_stock.is_suspended, false) = false
                         and coalesce(current_stock.is_st, false) = false
                         and current_stock.pct_chg is not null
                         and current_stock.amount > 0
                   ), 0) as pct_chg
            from current_members
            left join current_stock
              on current_stock.market = current_members.stock_market
             and current_stock.code = current_members.stock_code
            group by current_members.concept_id
        ), current_concept as (
            select concept_rows.concept_id, concept_rows.amount, concept_rows.pct_chg
            from fact.concept_daily_1d concept_rows
            cross join target
            join eligible_concepts on eligible_concepts.concept_id = concept_rows.concept_id
            where concept_rows.trade_date = target.trade_date
        ), latest_dates as (
            select 'fact.index_bar_1d' as table_name, max(index_rows.trade_date) as trade_date
            from fact.index_bar_1d index_rows
            cross join target
            where index_rows.trade_date <= target.trade_date
            union all
            select 'fact.concept_daily_1d', max(concept_rows.trade_date)
            from fact.concept_daily_1d concept_rows
            cross join target
            where concept_rows.trade_date <= target.trade_date
            union all
            select 'fact.stock_daily_1d', max(stock_rows.trade_date)
            from fact.stock_daily_1d stock_rows
            cross join target
            where stock_rows.trade_date <= target.trade_date
            union all
            select 'fact.board_daily_1d', max(board_rows.trade_date)
            from fact.board_daily_1d board_rows
            cross join target
            where board_rows.trade_date <= target.trade_date
        )
        select
            (select count(*) from recent_index_bars)::int as first_stage_index_trade_date_count,
            (select count(*) from recent_index_bars where open is null or high is null or low is null or close is null or pre_close is null or volume is null or amount is null or pct_chg is null)::int as first_stage_index_core_null_count,
            (select count(*) from recent_index_bars where volume <= 0 or amount <= 0)::int as first_stage_index_invalid_volume_amount_count,
            (select count(*) from target where not exists (select 1 from ref.index index_ref where index_ref.index_code = target.index_code))::int as first_stage_ref_index_missing_count,
            (select count(*) from target join ref.index index_ref on index_ref.index_code = target.index_code where index_ref.index_name is null or index_ref.index_name = '' or index_ref.index_name like '%%?%%')::int as first_stage_ref_index_bad_name_count,
            (select count(*) from first_stage_trade_dates)::int as first_stage_concept_trade_date_count,
            (select count(*) from first_stage_concept_coverage where bar_count <> 3)::int as first_stage_incomplete_concept_count,
            (select count(*) from fact.concept_daily_1d concept_rows join eligible_concepts on eligible_concepts.concept_id = concept_rows.concept_id join first_stage_trade_dates on first_stage_trade_dates.trade_date = concept_rows.trade_date where concept_rows.pct_chg is null or concept_rows.amount is null or concept_rows.amount <= 0)::int as first_stage_concept_core_invalid_count,
            (select count(*) from trade_dates)::int as second_stage_trade_date_count,
            (select count(*) from concept_coverage where bar_count <> 8)::int as second_stage_incomplete_concept_count,
            (select count(*) from fact.concept_daily_1d concept_rows join eligible_concepts on eligible_concepts.concept_id = concept_rows.concept_id join trade_dates on trade_dates.trade_date = concept_rows.trade_date where concept_rows.pct_chg is null)::int as second_stage_null_pct_chg_count,
            (select count(*) from fact.concept_daily_1d concept_rows join eligible_concepts on eligible_concepts.concept_id = concept_rows.concept_id join trade_dates on trade_dates.trade_date = concept_rows.trade_date where concept_rows.amount is null or concept_rows.amount <= 0)::int as second_stage_invalid_amount_count,
            (select count(*) from current_members left join current_stock on current_stock.market = current_members.stock_market and current_stock.code = current_members.stock_code where current_stock.code is null)::int as second_stage_missing_member_stock_count,
            (select count(*) from current_members join current_stock on current_stock.market = current_members.stock_market and current_stock.code = current_members.stock_code where current_stock.pre_close is null)::int as second_stage_missing_member_pre_close_count,
            (select count(*) from current_members where not (
                (stock_market = 'SHSE' and (left(stock_code, 1) in ('5', '6') or left(stock_code, 3) = '900'))
                or (stock_market = 'BJSE' and (left(stock_code, 1) in ('4', '8') or left(stock_code, 3) = '920'))
                or (stock_market = 'SZSE' and left(stock_code, 1) not in ('4', '5', '6', '8', '9'))
            ))::int as second_stage_invalid_market_code_count,
            (select count(*) from current_concept join expected_concept using(concept_id) where expected_concept.amount is null or abs(current_concept.amount - expected_concept.amount) > greatest(1.0, abs(expected_concept.amount) * 0.000001))::int as second_stage_amount_mismatch_count,
            (select count(*) from current_concept join expected_concept using(concept_id) where expected_concept.pct_chg is null or abs(current_concept.pct_chg - expected_concept.pct_chg) > 0.001)::int as second_stage_pct_chg_mismatch_count,
            (select case when count(distinct trade_date) = 1 then 0 else 1 end from latest_dates)::int as global_latest_date_mismatch_count,
            (select count(*) from (select concept_id, trade_date, count(*) from fact.concept_daily_1d group by concept_id, trade_date having count(*) > 1) duplicated)::int as global_duplicate_concept_key_count,
            (select count(*) from (select market, code, trade_date, count(*) from fact.stock_daily_1d group by market, code, trade_date having count(*) > 1) duplicated)::int as global_duplicate_stock_key_count,
            (select count(*) from (select board_code, trade_date, count(*) from fact.board_daily_1d group by board_code, trade_date having count(*) > 1) duplicated)::int as global_duplicate_board_key_count,
            (select count(*) from (select index_code, trade_date, count(*) from fact.index_bar_1d group by index_code, trade_date having count(*) > 1) duplicated)::int as global_duplicate_index_key_count,
            (select count(*) from fact.board_daily_1d board_rows cross join target where board_rows.trade_date = target.trade_date and (board_rows.open is null or board_rows.high is null or board_rows.low is null or board_rows.close is null or board_rows.pre_close is null or board_rows.change is null or board_rows.pct_chg is null or board_rows.amount is null or board_rows.volume is null))::int as global_board_daily_core_null_count,
            (select count(*) from fact.stock_daily_1d stock_rows cross join target where stock_rows.trade_date = target.trade_date and (stock_rows.close is null or stock_rows.pre_close is null or stock_rows.pct_chg is null or stock_rows.amount is null))::int as global_stock_daily_core_null_count,
            (select count(*) from fact.stock_daily_1d stock_rows cross join target where stock_rows.trade_date = target.trade_date and not exists (select 1 from ref.stock stock_ref where stock_ref.market = stock_rows.market and stock_ref.code = stock_rows.code))::int as global_stock_daily_without_ref_count,
            (select count(*) from ref.stock stock_ref cross join target where stock_ref.listed_date <= target.trade_date and (stock_ref.delisted_date is null or stock_ref.delisted_date >= target.trade_date) and not exists (select 1 from fact.stock_daily_1d stock_rows where stock_rows.market = stock_ref.market and stock_rows.code = stock_ref.code and stock_rows.trade_date = target.trade_date))::int as global_active_stock_without_daily_count,
            (select case when exists (select 1 from information_schema.columns where table_schema = 'ref' and table_name = 'stock' and column_name = 'board_type') then 0 else 1 end)::int as global_missing_board_type_column_count,
            (select count(*) from ref.stock stock_ref where coalesce(to_jsonb(stock_ref)->>'board_type', '') = '')::int as global_blank_board_type_count,
            (select count(*) from ref.stock stock_ref where coalesce(to_jsonb(stock_ref)->>'board_type', '') <> coalesce(stock_ref.listing_board, ''))::int as global_board_type_listing_board_mismatch_count,
            (select count(*) from fact.index_bar_1d index_rows cross join target where index_rows.trade_date = target.trade_date and not exists (select 1 from ref.index index_ref where index_ref.index_code = index_rows.index_code))::int as global_index_daily_without_ref_count,
            (select count(*) from ref.index index_ref where index_ref.index_name is null or index_ref.index_name = '' or index_ref.index_name like '%%?%%')::int as global_ref_index_bad_name_count
        """,
        (index_code, index_code),
    )
    if frame.empty:
        return {}
    row = frame.iloc[0]
    return {metric_name: int(row.get(metric_name, 0) or 0) for metric_name in MARKET_DATA_CONTRACT_EXPECTATIONS}


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


def _minute_text(value: str) -> str:
    return value[:5] if len(value) >= 5 else value


def _cached_check(cache: dict[str, object], key: str, factory: Callable[[], CheckSpec]) -> CheckSpec:
    cached = cache.get(key)
    if cached is not None:
        if not isinstance(cached, CheckSpec):
            raise TypeError(f"缓存项 {key} 不是 CheckSpec")
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
