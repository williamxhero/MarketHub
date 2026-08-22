#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
backup_dir="${2:-}"
env_path="${MARKETHUB_ENV_PATH:-/data/markethub/env/markethub.env}"
backup_root="${MARKETHUB_PG_OBSERVABILITY_BACKUP_ROOT:-/data/markethub/backups/postgresql-observability}"
cluster_service="${MARKETHUB_PG_CLUSTER_SERVICE:-postgresql@16-main.service}"

usage() {
    echo "usage: $0 apply | verify | rollback BACKUP_DIR" >&2
    exit 2
}

[[ "$action" == "apply" || "$action" == "verify" || "$action" == "rollback" ]] || usage
[[ -f "$env_path" ]] || { echo "missing environment file: $env_path" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
. "$env_path"
set +a

admin_psql() {
    sudo -n -u postgres psql -X -v ON_ERROR_STOP=1 "$@"
}

config_file="${MARKETHUB_PG_CONFIG_FILE:-/etc/postgresql/16/main/postgresql.conf}"
data_directory="${MARKETHUB_PG_DATA_DIRECTORY:-/data/postgresql/16/main}"
if admin_psql -d postgres -Atqc 'select 1' >/dev/null 2>&1; then
    config_file="$(admin_psql -d postgres -Atqc 'show config_file')"
    data_directory="$(admin_psql -d postgres -Atqc 'show data_directory')"
fi
auto_config="$data_directory/postgresql.auto.conf"

wait_postgres() {
    for _ in $(seq 1 30); do
        if pg_isready -q \
            -h "$MARKETHUB_DB_HOST" \
            -p "$MARKETHUB_DB_PORT" \
            -d "$MARKETHUB_DB_NAME" \
            -U "$MARKETHUB_DB_USER"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

restore_backup_files() {
    local source_dir="$1"
    sudo -n -u postgres bash -c 'cd "$1" && sha256sum -c SHA256SUMS' _ "$source_dir"
    sudo -n install -m 0644 -o postgres -g postgres "$source_dir/postgresql.conf" "$config_file"
    if sudo -n -u postgres test -f "$source_dir/postgresql.auto.conf.absent"; then
        sudo -n rm -f -- "$auto_config"
    else
        sudo -n install -m 0600 -o postgres -g postgres "$source_dir/postgresql.auto.conf" "$auto_config"
    fi
}

verify_settings() {
    admin_psql -d "$MARKETHUB_DB_NAME" -At <<'SQL'
select name || '=' || setting
from pg_settings
where name in (
    'shared_preload_libraries',
    'track_io_timing',
    'log_temp_files',
    'pg_stat_statements.track',
    'pg_stat_statements.max',
    'pg_stat_statements.save'
)
order by name;
select 'extension=' || extversion from pg_extension where extname = 'pg_stat_statements';
select 'view_readable=' || (count(*) >= 0)::text from pg_stat_statements;
SQL
    systemctl is-active "$cluster_service"
    systemctl is-active markethub-api.service
}

if [[ "$action" == "verify" ]]; then
    verify_settings
    exit 0
fi

if [[ "$action" == "apply" ]]; then
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_dir="$backup_root/$timestamp"
    sudo -n install -d -m 0750 -o postgres -g postgres "$backup_dir"
    sudo -n cp --preserve=mode,ownership,timestamps "$config_file" "$backup_dir/postgresql.conf"
    if [[ -f "$auto_config" ]]; then
        sudo -n cp --preserve=mode,ownership,timestamps "$auto_config" "$backup_dir/postgresql.auto.conf"
    else
        sudo -n -u postgres touch "$backup_dir/postgresql.auto.conf.absent"
    fi
    admin_psql -d postgres -Atqc "select name || '=' || setting from pg_settings order by name" \
        | sudo -n -u postgres tee "$backup_dir/pg_settings.before.txt" >/dev/null
    sudo -n -u postgres bash -c '
        cd "$1"
        find . -maxdepth 1 -type f ! -name SHA256SUMS -print0 \
            | sort -z \
            | xargs -0 sha256sum >SHA256SUMS
    ' _ "$backup_dir"

    apply_failed() {
        local exit_code=$?
        trap - ERR
        echo "apply failed; restoring $backup_dir" >&2
        restore_backup_files "$backup_dir"
        sudo -n systemctl restart "$cluster_service"
        wait_postgres || true
        exit "$exit_code"
    }
    trap apply_failed ERR

    admin_psql -d postgres <<'SQL'
alter system set shared_preload_libraries = 'timescaledb', 'pg_stat_statements';
alter system set track_io_timing = 'on';
alter system set log_temp_files = '65536';
SQL
    parsed_preload="$(sudo -n -u postgres /usr/lib/postgresql/16/bin/postgres \
        -D "$data_directory" \
        -C shared_preload_libraries \
        -c "config_file=$config_file")"
    [[ "$parsed_preload" == "timescaledb, pg_stat_statements" ]]
    sudo -n systemctl restart "$cluster_service"
    wait_postgres

    admin_psql -d postgres <<'SQL'
alter system set pg_stat_statements.track = 'all';
alter system set pg_stat_statements.max = '10000';
alter system set pg_stat_statements.save = 'on';
SQL
    sudo -n systemctl restart "$cluster_service"
    wait_postgres
    admin_psql -d "$MARKETHUB_DB_NAME" -c 'create extension if not exists pg_stat_statements'
    verify_settings
    trap - ERR
    echo "backup_dir=$backup_dir"
    exit 0
fi

[[ -n "$backup_dir" ]] || usage
resolved_backup="$(realpath -e "$backup_dir")"
resolved_root="$(realpath -e "$backup_root")"
[[ "$resolved_backup" == "$resolved_root"/* ]] || {
    echo "rollback path is outside backup root: $resolved_backup" >&2
    exit 1
}
restore_backup_files "$resolved_backup"
sudo -n systemctl restart "$cluster_service"
wait_postgres || { echo "PostgreSQL did not become ready after rollback" >&2; exit 1; }
systemctl is-active "$cluster_service"
systemctl is-active markethub-api.service
