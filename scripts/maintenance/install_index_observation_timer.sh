#!/usr/bin/env bash
set -euo pipefail

release_root="$(readlink -f "${MARKETHUB_RELEASE_ROOT:-/data/MarketHub2/current}")"
env_path="${MARKETHUB_ENV_PATH:-/data/markethub/env/markethub.env}"
python_path="${MARKETHUB_PYTHON:-/data/markethub/.venv/bin/python}"
service_path="/etc/systemd/system/markethub-index-observation.service"
timer_path="/etc/systemd/system/markethub-index-observation.timer"
script_path="$release_root/MarketHub/scripts/maintenance/capture_index_observation.py"

[[ "$release_root" == /data/MarketHub2/releases/* ]] || { echo "unexpected release root: $release_root" >&2; exit 1; }
[[ -f "$env_path" ]] || { echo "missing env: $env_path" >&2; exit 1; }
[[ -f "$script_path" ]] || { echo "missing observer: $script_path" >&2; exit 1; }

service_tmp="$(mktemp)"
timer_tmp="$(mktemp)"
trap 'rm -f -- "$service_tmp" "$timer_tmp"' EXIT

printf '%s\n' \
    '[Unit]' \
    'Description=MarketHub daily PostgreSQL index observation' \
    'After=postgresql@16-main.service' \
    'Requires=postgresql@16-main.service' \
    '' \
    '[Service]' \
    'Type=oneshot' \
    'User=yosef' \
    'Group=yosef' \
    "EnvironmentFile=$env_path" \
    "ExecStart=$python_path $script_path --output-root /data/markethub/observability/indexes" \
    'NoNewPrivileges=true' \
    'PrivateTmp=true' \
    >"$service_tmp"

printf '%s\n' \
    '[Unit]' \
    'Description=Run MarketHub index observation after each trading-day window' \
    '' \
    '[Timer]' \
    'OnCalendar=*-*-* 21:30:00 Asia/Shanghai' \
    'Persistent=true' \
    'RandomizedDelaySec=120' \
    'Unit=markethub-index-observation.service' \
    '' \
    '[Install]' \
    'WantedBy=timers.target' \
    >"$timer_tmp"

sudo -n install -m 0644 "$service_tmp" "$service_path"
sudo -n install -m 0644 "$timer_tmp" "$timer_path"
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now markethub-index-observation.timer
sudo -n systemctl start markethub-index-observation.service
systemctl is-active markethub-index-observation.timer
systemctl list-timers markethub-index-observation.timer --no-pager
