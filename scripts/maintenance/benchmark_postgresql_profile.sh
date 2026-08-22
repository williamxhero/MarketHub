#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
profile_or_backup="${2:-}"
env_path="${MARKETHUB_ENV_PATH:-/data/markethub/env/markethub.env}"
backup_root="${MARKETHUB_PG_PROFILE_BACKUP_ROOT:-/data/markethub/backups/postgresql-profiles}"
cluster_service="${MARKETHUB_PG_CLUSTER_SERVICE:-postgresql@16-main.service}"
api_service="${MARKETHUB_API_SERVICE:-markethub-api.service}"
health_url="${MARKETHUB_HEALTH_URL:-http://127.0.0.1:8803/api/health}"

usage() {
    echo "usage: $0 apply A|B | verify A|B | rollback BACKUP_DIR | show" >&2
    exit 2
}

[[ "$action" == "apply" || "$action" == "verify" || "$action" == "rollback" || "$action" == "show" ]] || usage
[[ -f "$env_path" ]] || { echo "missing environment file: $env_path" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
. "$env_path"
set +a

admin_psql() {
    sudo -n -u postgres psql -X -v ON_ERROR_STOP=1 "$@"
}

config_file="$(admin_psql -d postgres -Atqc 'show config_file')"
data_directory="$(admin_psql -d postgres -Atqc 'show data_directory')"
auto_config="$data_directory/postgresql.auto.conf"

wait_postgres() {
    for _ in $(seq 1 60); do
        if pg_isready -q -h "$MARKETHUB_DB_HOST" -p "$MARKETHUB_DB_PORT" -d "$MARKETHUB_DB_NAME" -U "$MARKETHUB_DB_USER"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_api() {
    for _ in $(seq 1 60); do
        if curl -fsS "$health_url" >/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

show_settings() {
    admin_psql -d "$MARKETHUB_DB_NAME" -At <<'SQL'
select name || '=' || setting || coalesce(unit, '')
from pg_settings
where name in (
    'shared_buffers',
    'effective_cache_size',
    'work_mem',
    'maintenance_work_mem',
    'random_page_cost',
    'effective_io_concurrency'
)
order by name;
SQL
}

expected_settings() {
    case "$1" in
        A)
            printf '%s\n' \
                'effective_cache_size=39321608kB' \
                'effective_io_concurrency=128' \
                'maintenance_work_mem=524288kB' \
                'random_page_cost=1.5' \
                'shared_buffers=10485768kB' \
                'work_mem=16384kB'
            ;;
        B)
            printf '%s\n' \
                'effective_cache_size=47185928kB' \
                'effective_io_concurrency=256' \
                'maintenance_work_mem=1048576kB' \
                'random_page_cost=1.1' \
                'shared_buffers=15728648kB' \
                'work_mem=16384kB'
            ;;
        *) usage ;;
    esac
}

verify_profile() {
    local profile="$1"
    local actual expected
    actual="$(show_settings)"
    expected="$(expected_settings "$profile")"
    [[ "$actual" == "$expected" ]] || {
        echo "profile $profile mismatch" >&2
        diff -u <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") || true
        return 1
    }
    systemctl is-active "$cluster_service"
    systemctl is-active "$api_service"
    curl -fsS "$health_url" >/dev/null
    echo "profile=$profile"
    printf '%s\n' "$actual"
}

restore_backup() {
    local source_dir="$1"
    sudo -n -u postgres bash -c 'cd "$1" && sha256sum -c SHA256SUMS' _ "$source_dir"
    sudo -n install -m 0644 -o postgres -g postgres "$source_dir/postgresql.conf" "$config_file"
    if sudo -n -u postgres test -f "$source_dir/postgresql.auto.conf.absent"; then
        sudo -n rm -f -- "$auto_config"
    else
        sudo -n install -m 0600 -o postgres -g postgres "$source_dir/postgresql.auto.conf" "$auto_config"
    fi
}

restart_stack() {
    sudo -n systemctl restart "$cluster_service"
    wait_postgres
    sudo -n systemctl restart "$api_service"
    wait_api
}

if [[ "$action" == "show" ]]; then
    show_settings
    exit 0
fi

if [[ "$action" == "verify" ]]; then
    verify_profile "$profile_or_backup"
    exit 0
fi

if [[ "$action" == "apply" ]]; then
    profile="$profile_or_backup"
    [[ "$profile" == "A" || "$profile" == "B" ]] || usage
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_dir="$backup_root/${timestamp}-${profile}"
    sudo -n install -d -m 0750 -o postgres -g postgres "$backup_dir"
    sudo -n cp --preserve=mode,ownership,timestamps "$config_file" "$backup_dir/postgresql.conf"
    if [[ -f "$auto_config" ]]; then
        sudo -n cp --preserve=mode,ownership,timestamps "$auto_config" "$backup_dir/postgresql.auto.conf"
    else
        sudo -n -u postgres touch "$backup_dir/postgresql.auto.conf.absent"
    fi
    show_settings | sudo -n -u postgres tee "$backup_dir/pg_settings.before.txt" >/dev/null
    sudo -n -u postgres bash -c '
        cd "$1"
        find . -maxdepth 1 -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS
    ' _ "$backup_dir"

    apply_failed() {
        local exit_code=$?
        trap - ERR
        echo "profile apply failed; restoring $backup_dir" >&2
        restore_backup "$backup_dir"
        restart_stack || true
        exit "$exit_code"
    }
    trap apply_failed ERR

    if [[ "$profile" == "A" ]]; then
        admin_psql -d postgres <<'SQL'
alter system set shared_buffers = '8GB';
alter system set effective_cache_size = '30GB';
alter system set work_mem = '16MB';
alter system set maintenance_work_mem = '512MB';
alter system set random_page_cost = '1.5';
alter system set effective_io_concurrency = '128';
SQL
    else
        admin_psql -d postgres <<'SQL'
alter system set shared_buffers = '12GB';
alter system set effective_cache_size = '36GB';
alter system set work_mem = '16MB';
alter system set maintenance_work_mem = '1GB';
alter system set random_page_cost = '1.1';
alter system set effective_io_concurrency = '256';
SQL
    fi
    sudo -n -u postgres /usr/lib/postgresql/16/bin/postgres -D "$data_directory" -C shared_buffers -c "config_file=$config_file" >/dev/null
    restart_stack
    verify_profile "$profile"
    trap - ERR
    echo "backup_dir=$backup_dir"
    exit 0
fi

[[ -n "$profile_or_backup" ]] || usage
resolved_backup="$(realpath -e "$profile_or_backup")"
resolved_root="$(realpath -e "$backup_root")"
[[ "$resolved_backup" == "$resolved_root"/* ]] || {
    echo "rollback path is outside backup root: $resolved_backup" >&2
    exit 1
}
restore_backup "$resolved_backup"
restart_stack
show_settings
echo "restored_backup=$resolved_backup"
