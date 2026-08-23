#!/usr/bin/env bash
set -Eeuo pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_PROGRESS_BAR=off

if [[ $# -ne 7 ]]; then
  echo "用法: $0 <archive> <release-name> <app-root> <runtime-root> <env-path> <service-name> <health-url>" >&2
  exit 2
fi

archive="$1"
release_name="$2"
app_root="$3"
runtime_root="$4"
env_path="$5"
service_name="$6"
health_url="$7"
release_root="$app_root/releases/$release_name"
service_user="${MARKETHUB_SERVICE_USER:-}"
if [[ -z "$service_user" ]]; then
  service_user="$(getent passwd 1000 | cut -d: -f1 | head -n1)"
fi
[[ -n "$service_user" ]] || { echo "无法自动确定服务用户，请设置 MARKETHUB_SERVICE_USER" >&2; exit 4; }
postgres_major="${MARKETHUB_POSTGRES_MAJOR:-}"
if [[ -z "$postgres_major" ]]; then
  postgres_major="$(find /usr/share/postgresql -mindepth 3 -maxdepth 3 -path '*/extension/timescaledb.control' -printf '%h\n' 2>/dev/null | awk -F/ '{print $(NF-1)}' | sort -Vr | head -n1)"
fi
[[ -n "$postgres_major" ]] || { echo "没有发现已安装 TimescaleDB 的 PostgreSQL" >&2; exit 5; }
postgres_cluster="${MARKETHUB_POSTGRES_CLUSTER:-main}"
postgres_data="${MARKETHUB_POSTGRES_DATA:-}"
if [[ -z "$postgres_data" ]] && command -v pg_lsclusters >/dev/null 2>&1; then
  postgres_data="$(pg_lsclusters --no-header 2>/dev/null | awk -v major="$postgres_major" -v cluster="$postgres_cluster" '$1 == major && $2 == cluster {print $6; exit}')"
fi
postgres_data="${postgres_data:-$runtime_root/postgresql/$postgres_major/$postgres_cluster}"
package_venv_root="${MARKETHUB_PACKAGE_VENV_ROOT:-$runtime_root/package_venvs/$release_name}"
status_root="$runtime_root/migrations/markethub-storage-v2-20260823"
status_file="$status_root/install-$release_name.status"

[[ "$(id -u)" -eq 0 ]] || { echo "本脚本必须由 root 执行" >&2; exit 3; }
test -f "$archive"
id "$service_user" >/dev/null
service_group="$(id -gn "$service_user")"
test -x "/usr/lib/postgresql/$postgres_major/bin/initdb"
test -f "/usr/share/postgresql/$postgres_major/extension/timescaledb.control"
mkdir -p "$status_root"
printf 'running\n' >"$status_file"

finish() {
  local exit_code="$?"
  systemctl restart "$service_name.service" >/dev/null 2>&1 || true
  if [[ "$exit_code" -eq 0 ]]; then
    printf 'success\n' >"$status_file"
  else
    printf 'failed:%s\n' "$exit_code" >"$status_file"
  fi
}
trap finish EXIT

if [[ ! -f "$postgres_data/PG_VERSION" ]]; then
  install -d -o postgres -g postgres -m 0700 "$postgres_data"
  runuser -u postgres -- "/usr/lib/postgresql/$postgres_major/bin/initdb" \
    -D "$postgres_data" --auth-local=peer --auth-host=scram-sha-256
fi

pg_ctlcluster "$postgres_major" "$postgres_cluster" start || {
  journalctl -u "postgresql@$postgres_major-$postgres_cluster.service" -n 100 --no-pager >&2 || true
  exit 10
}

install -d -o "$service_user" -g "$service_group" "$app_root/releases" "$runtime_root" "$(dirname "$env_path")"
rm -rf -- "$release_root"
mkdir -p "$release_root"
tar --no-same-owner -xzf "$archive" -C "$release_root"
chown -R "$service_user:$service_group" "$release_root"
chown "$service_user:$service_group" "$runtime_root"

if [[ ! -f "$env_path" ]]; then
  db_password="$(openssl rand -base64 32 | tr -d '=+/\n' | head -c 32)"
  cat >"$env_path" <<ENV
MARKETHUB_HOST=127.0.0.1
MARKETHUB_PORT=8803
MARKETHUB_DB_HOST=127.0.0.1
MARKETHUB_DB_PORT=5432
MARKETHUB_DB_NAME=markethub
MARKETHUB_DB_USER=markethub
MARKETHUB_DB_PASSWORD=$db_password
MARKETHUB_RUNTIME_ROOT=$runtime_root
MARKETHUB_DATA_ROOT=$runtime_root/store
MARKETHUB_LOG_ROOT=$runtime_root/logs
MARKETHUB_DATA_UPDATE_ROOT=$runtime_root/data-update
MARKETHUB_EXPORT_ROOT=$app_root/exports
QUOTEMUX_RUNTIME_ROOT=$runtime_root/runtime
QUOTEMUX_CACHE_PAYLOAD_ROOT=$runtime_root/cache_payloads
QUOTEMUX_PACKAGE_REPO_SPEC=$release_root/QuoteMux_Packages
QUOTEMUX_PACKAGE_VENV_ROOT=$package_venv_root
QUOTEMUX_ALLOW_LOCAL_PACKAGE_REPO=true
ENV
  chmod 0600 "$env_path"
  chown "$service_user:$service_group" "$env_path"
fi

systemctl stop "$service_name.service" >/dev/null 2>&1 || true

python3 -m venv "$runtime_root/.venv"
"$runtime_root/.venv/bin/python" -m pip install --upgrade pip
"$runtime_root/.venv/bin/python" -m pip install -e "$release_root/QuoteMux"
export QUOTEMUX_PACKAGE_REPO_SPEC="$release_root/QuoteMux_Packages"
"$runtime_root/.venv/bin/python" -m pip install -r "$release_root/MarketHub/requirements.txt"
set -a
. "$env_path"
set +a
export QUOTEMUX_PACKAGE_REPO_SPEC="$release_root/QuoteMux_Packages"
export QUOTEMUX_PACKAGE_VENV_ROOT="$package_venv_root"
export MARKETHUB_VENV_ROOT="$runtime_root/.venv"
export PYTHONPATH="$release_root/QuoteMux/src:$release_root/MarketHub/services/markethub_api/src"
install -d -o "$service_user" -g "$service_group" \
  "$runtime_root"/{runtime,cache_payloads,package_venvs,store,logs,data-update,migrations,scripts} \
  "$package_venv_root" "$app_root/exports"
chown -R "$service_user:$service_group" "$release_root" "$runtime_root/.venv"
runuser -u "$service_user" -- env \
  QUOTEMUX_PACKAGE_REPO_SPEC="$QUOTEMUX_PACKAGE_REPO_SPEC" \
  QUOTEMUX_PACKAGE_VENV_ROOT="$QUOTEMUX_PACKAGE_VENV_ROOT" \
  QUOTEMUX_ALLOW_LOCAL_PACKAGE_REPO=true \
  QUOTEMUX_RUNTIME_ROOT="$runtime_root/runtime" \
  MARKETHUB_VENV_ROOT="$MARKETHUB_VENV_ROOT" \
  MARKETHUB_RUNTIME_ROOT="$runtime_root" \
  PYTHONPATH="$PYTHONPATH" \
  "$runtime_root/.venv/bin/python" "$release_root/MarketHub/scripts/deploy/install_all_packages.py"
"$runtime_root/.venv/bin/python" "$release_root/MarketHub/scripts/deploy/bootstrap_database.py"
chown -R "$service_user:$service_group" "$release_root"

ln -sfn "$release_root" "$app_root/current.next"
mv -Tf "$app_root/current.next" "$app_root/current"

cat >/etc/systemd/system/"$service_name.service" <<UNIT
[Unit]
Description=MarketHub API
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=$service_user
Group=$service_group
WorkingDirectory=$app_root/current/MarketHub/services/markethub_api
EnvironmentFile=$env_path
Environment=MARKETHUB_RELEASE=$release_name
Environment=PYTHONPATH=$app_root/current/QuoteMux/src:$app_root/current/MarketHub/services/markethub_api/src
Environment=QUOTEMUX_PACKAGE_REPO_SPEC=$app_root/current/QuoteMux_Packages
Environment=QUOTEMUX_PACKAGE_VENV_ROOT=$package_venv_root
ExecStart=$runtime_root/.venv/bin/python $app_root/current/MarketHub/services/markethub_api/app.py
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30s
MemoryHigh=12G
MemoryMax=18G
MemorySwapMax=2G
OOMPolicy=stop

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$service_name.service"

evidence_root="$runtime_root/migrations/markethub-storage-v2-20260823"
mkdir -p "$evidence_root"
"$runtime_root/.venv/bin/python" "$release_root/MarketHub/migrations/storage_v2_20260823/release_migration.py" \
  --env-file "$env_path" --output "$evidence_root/apply.json" \
  apply --writers-paused
"$runtime_root/.venv/bin/python" "$release_root/MarketHub/migrations/storage_v2_20260823/release_migration.py" \
  --env-file "$env_path" --output "$evidence_root/verify.json" verify

systemctl start "$service_name.service"
curl -fsS --retry 30 --retry-delay 2 --retry-connrefused "$health_url"
rm -f -- "$archive"
echo "本机发布与迁移完成: $release_name"
