#!/usr/bin/env bash
set -Eeuo pipefail

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
service_user="${MARKETHUB_SERVICE_USER:-will}"
postgres_major="${MARKETHUB_POSTGRES_MAJOR:-18}"
postgres_data="${MARKETHUB_POSTGRES_DATA:-/data/postgresql/$postgres_major/main}"

[[ "$(id -u)" -eq 0 ]] || { echo "本脚本必须由 root 执行" >&2; exit 3; }
test -f "$archive"
id "$service_user" >/dev/null
test -x "/usr/lib/postgresql/$postgres_major/bin/initdb"
test -f "/usr/share/postgresql/$postgres_major/extension/timescaledb.control"

if [[ ! -f "$postgres_data/PG_VERSION" ]]; then
  install -d -o postgres -g postgres -m 0700 "$postgres_data"
  runuser -u postgres -- "/usr/lib/postgresql/$postgres_major/bin/initdb" \
    -D "$postgres_data" --auth-local=peer --auth-host=scram-sha-256
fi

pg_ctlcluster "$postgres_major" main start || {
  journalctl -u "postgresql@$postgres_major-main.service" -n 100 --no-pager >&2 || true
  exit 10
}

install -d -o "$service_user" -g "$service_user" "$app_root/releases" "$runtime_root" "$(dirname "$env_path")"
rm -rf -- "$release_root"
mkdir -p "$release_root"
tar --no-same-owner -xzf "$archive" -C "$release_root"
chown -R "$service_user:$service_user" "$release_root" "$runtime_root"

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
QUOTEMUX_PACKAGE_VENV_ROOT=$runtime_root/package_venvs/$release_name
QUOTEMUX_ALLOW_LOCAL_PACKAGE_REPO=true
ENV
  chmod 0600 "$env_path"
  chown "$service_user:$service_user" "$env_path"
fi

systemctl stop "$service_name.service" >/dev/null 2>&1 || true
trap 'systemctl restart "$service_name.service" >/dev/null 2>&1 || true' EXIT

python3 -m venv "$runtime_root/.venv"
"$runtime_root/.venv/bin/python" -m pip install --upgrade pip
"$runtime_root/.venv/bin/python" -m pip install -e "$release_root/QuoteMux"
export QUOTEMUX_PACKAGE_REPO_SPEC="$release_root/QuoteMux_Packages"
"$runtime_root/.venv/bin/python" -m pip install -r "$release_root/MarketHub/requirements.txt"
set -a
. "$env_path"
set +a
export QUOTEMUX_PACKAGE_REPO_SPEC="$release_root/QuoteMux_Packages"
export QUOTEMUX_PACKAGE_VENV_ROOT="$runtime_root/package_venvs/$release_name"
export PYTHONPATH="$release_root/QuoteMux/src:$release_root/MarketHub/services/markethub_api/src"
mkdir -p "$runtime_root"/{runtime,cache_payloads,package_venvs,store,logs,data-update,migrations,scripts} "$app_root/exports"
"$runtime_root/.venv/bin/python" "$release_root/MarketHub/scripts/deploy/install_all_packages.py"
"$runtime_root/.venv/bin/python" "$release_root/MarketHub/scripts/deploy/bootstrap_database.py"
chown -R "$service_user:$service_user" "$release_root" "$runtime_root" "$app_root/exports"

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
Group=$service_user
WorkingDirectory=$app_root/current/MarketHub/services/markethub_api
EnvironmentFile=$env_path
Environment=MARKETHUB_RELEASE=$release_name
Environment=PYTHONPATH=$app_root/current/QuoteMux/src:$app_root/current/MarketHub/services/markethub_api/src
Environment=QUOTEMUX_PACKAGE_REPO_SPEC=$app_root/current/QuoteMux_Packages
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
trap - EXIT
curl -fsS --retry 30 --retry-delay 2 --retry-connrefused "$health_url"
rm -f -- "$archive"
echo "本机发布与迁移完成: $release_name"
