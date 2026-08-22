#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
restore_sql="${2:-}"
evidence_gate="${MARKETHUB_INDEX_EVIDENCE_GATE:-}"
env_path="${MARKETHUB_ENV_PATH:-/data/markethub/env/markethub.env}"
observation_root="${MARKETHUB_INDEX_OBSERVATION_ROOT:-/data/markethub/observability/indexes}"
backup_root="${MARKETHUB_INDEX_BACKUP_ROOT:-/data/markethub/backups/postgresql-indexes}"
candidate="fact.stock_daily_1d_code_trade_idx"
expected_definition="CREATE INDEX stock_daily_1d_code_trade_idx ON fact.stock_daily_1d USING btree (code, trade_date)"

usage() {
    echo "usage: $0 verify | drop | restore ROLLBACK_SQL" >&2
    exit 2
}

[[ "$action" == "verify" || "$action" == "drop" || "$action" == "restore" ]] || usage
[[ -f "$env_path" ]] || { echo "missing environment file: $env_path" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$env_path"
set +a
export PGPASSWORD="$MARKETHUB_DB_PASSWORD"

psql_cmd=(psql -X -v ON_ERROR_STOP=1 -h "$MARKETHUB_DB_HOST" -p "$MARKETHUB_DB_PORT" -U "$MARKETHUB_DB_USER" -d "$MARKETHUB_DB_NAME")

if [[ "$action" == "restore" ]]; then
    [[ -n "$restore_sql" ]] || usage
    resolved_sql="$(realpath -e "$restore_sql")"
    resolved_root="$(realpath -e "$backup_root")"
    [[ "$resolved_sql" == "$resolved_root"/*/rollback.sql ]] || { echo "rollback SQL outside backup root: $resolved_sql" >&2; exit 1; }
    backup_dir="$(dirname "$resolved_sql")"
    (cd "$backup_dir" && sha256sum -c SHA256SUMS)
    "${psql_cmd[@]}" -f "$resolved_sql"
    echo "restored=$candidate"
    exit 0
fi

definition="$("${psql_cmd[@]}" -Atqc "select pg_get_indexdef('$candidate'::regclass)")"
[[ "$definition" == "$expected_definition" ]] || {
    echo "candidate definition changed: $definition" >&2
    exit 1
}
duplicate_count="$("${psql_cmd[@]}" -Atqc "
select count(*) from pg_index a join pg_index b on a.indrelid=b.indrelid and a.indexrelid<>b.indexrelid
where a.indexrelid='$candidate'::regclass
  and a.indkey=b.indkey and a.indclass=b.indclass and a.indcollation=b.indcollation and a.indoption=b.indoption
  and a.indexprs is not distinct from b.indexprs and a.indpred is not distinct from b.indpred
  and b.indexrelid in ('fact.stock_daily_1d_code_date_idx'::regclass,'fact.stock_daily_1d_code_date_uniq'::regclass)")"
[[ "$duplicate_count" == "2" ]] || { echo "candidate is no longer exactly duplicated twice" >&2; exit 1; }

trading_days="$(/data/markethub/.venv/bin/python - "$observation_root" <<'PY'
import glob, json, os, sys
days = set()
for path in glob.glob(os.path.join(sys.argv[1], "index-observation-*.json")):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("is_current_trading_day") and payload.get("after_trading_window"):
        days.add(str(payload["shanghai_date"]))
print(len(days))
PY
)"

if [[ "$action" == "verify" ]]; then
    echo "candidate=$candidate"
    echo "exact_duplicate_count=$duplicate_count"
    echo "observed_trading_days=$trading_days"
    exit 0
fi

if [[ "$action" == "drop" ]]; then
    [[ "$evidence_gate" == "accelerated-approved" ]] || {
        echo "drop requires MARKETHUB_INDEX_EVIDENCE_GATE=accelerated-approved" >&2
        exit 1
    }
    dependency_count="$("${psql_cmd[@]}" -Atqc "
select count(*) from pg_depend
where objid='$candidate'::regclass
  and deptype not in ('a','i')")"
    [[ "$dependency_count" == "0" ]] || { echo "candidate has non-automatic dependencies: $dependency_count" >&2; exit 1; }
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_dir="$backup_root/$timestamp"
    install -d -m 0750 "$backup_dir"
    rollback_file="$backup_dir/rollback.sql"
    printf '%s\n' 'CREATE INDEX CONCURRENTLY stock_daily_1d_code_trade_idx ON fact.stock_daily_1d USING btree (code, trade_date);' >"$rollback_file"
    sha256sum "$rollback_file" >"$backup_dir/SHA256SUMS"
    printf '%s\n' "candidate=$candidate" "definition=$definition" "exact_duplicate_count=$duplicate_count" \
        "dependency_count=$dependency_count" "observed_trading_days=$trading_days" \
        "evidence_gate=$evidence_gate" >"$backup_dir/pre-drop-evidence.txt"
    sha256sum "$backup_dir/pre-drop-evidence.txt" >>"$backup_dir/SHA256SUMS"
    "${psql_cmd[@]}" -c 'DROP INDEX CONCURRENTLY fact.stock_daily_1d_code_trade_idx'
    echo "rollback_sql=$rollback_file"
    exit 0
fi
