#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 3 || "$1" != "--apply" ]]; then
  echo "用法: $0 --apply <postgres-major> <minimum-timescaledb-version>" >&2
  exit 2
fi

postgres_major="$2"
minimum_version="$3"
[[ "$(id -u)" -eq 0 ]] || { echo "前置依赖升级必须由 root 执行" >&2; exit 3; }
[[ "$postgres_major" =~ ^[0-9]+$ ]]
[[ "$minimum_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
command -v apt-get >/dev/null
command -v dpkg >/dev/null

control="/usr/share/postgresql/$postgres_major/extension/timescaledb.control"
installed=""
if [[ -f "$control" ]]; then
  installed="$(sed -n "s/^default_version[[:space:]]*=[[:space:]]*['\"]\([^'\"]*\)['\"].*/\1/p" "$control" | head -n1)"
fi
if [[ -n "$installed" ]] && dpkg --compare-versions "$installed" ge "$minimum_version"; then
  echo "TimescaleDB 前置依赖已满足: $installed"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  "timescaledb-2-loader-postgresql-$postgres_major" \
  "timescaledb-2-postgresql-$postgres_major"

test -f "$control"
installed="$(sed -n "s/^default_version[[:space:]]*=[[:space:]]*['\"]\([^'\"]*\)['\"].*/\1/p" "$control" | head -n1)"
if [[ -z "$installed" ]] || ! dpkg --compare-versions "$installed" ge "$minimum_version"; then
  echo "TimescaleDB 升级后仍不满足最低版本: ${installed:-missing} < $minimum_version" >&2
  exit 10
fi
if command -v pg_lsclusters >/dev/null 2>&1; then
  while read -r major cluster port status rest; do
    if [[ "$major" == "$postgres_major" && "$status" == "online" ]]; then
      pg_ctlcluster "$major" "$cluster" restart
    fi
  done < <(pg_lsclusters --no-header)
fi
echo "TimescaleDB 前置依赖升级完成: $installed"
