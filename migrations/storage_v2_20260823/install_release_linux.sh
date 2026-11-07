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
candidate_root="$app_root/releases/.incoming-${release_name}.$$"
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
release_manifest_path="$status_root/release-$release_name.manifest.json"
previous_current=""
service_stopped=0
current_switched=0
rollback_authorized=0
env_existed=0
env_backup="$status_root/env-$release_name.before"
unit_path="/etc/systemd/system/$service_name.service"
unit_backup="$status_root/unit-$release_name.before"
candidate_manifest_path="$candidate_root/release-manifest.json"
maintenance_lock_path="${MARKETHUB_STOCK_BAR_MAINTENANCE_LOCK:-$runtime_root/audit/stock-bar-maintenance.lock}"

# systemd resolves WorkingDirectory as the service user.  Grant traversal only
# to that user; never make a release searchable by all
# users.  setfacl is required when an ancestor is not owned by the service
# account, because chmod u+X would grant the wrong principal.
ensure_service_path_traversable() {
  local path="$1"
  while [[ "$path" != "/" && "$path" != "." ]]; do
    [[ -d "$path" ]] || { echo "service path is not a directory: $path" >&2; return 1; }
    if command -v setfacl >/dev/null 2>&1; then
      setfacl -m "u:${service_user}:x" "$path"
    else
      local owner group
      owner="$(stat -c '%U' "$path")"
      group="$(stat -c '%G' "$path")"
      if [[ "$owner" == "$service_user" ]]; then
        chmod u+X "$path"
      elif [[ "$group" == "$service_group" ]]; then
        chmod g+X "$path"
      else
        echo "setfacl is required to grant service traversal: $path" >&2
        return 1
      fi
    fi
    path="$(dirname "$path")"
  done
}

sha256_file() { sha256sum "$1" | awk '{print $1}'; }

write_release_manifest() {
  local source_manifest="$1"
  local archive_hash="$2"
  local file_inventory_hash="$3"
  python3 - "$source_manifest" "$candidate_manifest_path" "$archive_hash" "$file_inventory_hash" \
    "$archive" "$release_name" "$app_root" "$runtime_root" "$env_path" "$service_name" "$health_url" <<'PY'
import hashlib
import json
import pathlib
import sys

source_path, output_path, archive_hash, inventory_hash, archive, release_name, app_root, runtime_root, env_path, service_name, health_url = sys.argv[1:]
source = json.loads(pathlib.Path(source_path).read_text(encoding="utf-8"))
required = ("migration_id", "package_version", "target_storage_version")
if any(not str(source.get(key, "")) for key in required):
    raise SystemExit("release manifest is missing a version contract")
release_inputs_path = pathlib.Path(source_path).parents[3] / "release-inputs.json"
release_inputs = {}
release_inputs_present = release_inputs_path.is_file()
if release_inputs_present:
    release_inputs = json.loads(release_inputs_path.read_text(encoding="utf-8"))
    if not isinstance(release_inputs, dict):
        raise SystemExit("release-inputs.json must contain an object")
candidate_root = pathlib.Path(source_path).parents[3]
def tree_hash(path):
    if not path.is_dir():
        raise SystemExit(f"release input tree is missing: {path}")
    rows = []
    for item in sorted(path.rglob("*")):
        if item.is_file() and item.name != "release-manifest.json":
            rows.append(f"{item.relative_to(path).as_posix()}\0{hashlib.sha256(item.read_bytes()).hexdigest()}\n".encode())
    return hashlib.sha256(b"".join(rows)).hexdigest()
expected_commit_inputs = {
    "market_hub_tree_sha256": tree_hash(candidate_root / "MarketHub"),
    "quote_mux_tree_sha256": tree_hash(candidate_root / "QuoteMux"),
    "quote_mux_packages_tree_sha256": tree_hash(candidate_root / "QuoteMux_Packages"),
}
if release_inputs_present:
    required_commit_inputs = ("market_hub_commit", "quote_mux_commit", "quote_mux_packages_commit")
    if any(not isinstance(release_inputs.get(key), str) or not release_inputs[key] for key in required_commit_inputs):
        raise SystemExit("release-inputs.json is missing a required commit input")
    if any(len(release_inputs[key]) != 40 or any(char not in "0123456789abcdefABCDEF" for char in release_inputs[key]) for key in required_commit_inputs):
        raise SystemExit("release-inputs.json contains an invalid commit input")
    commit_inputs = {key: release_inputs[key] for key in required_commit_inputs}
else:
    commit_inputs = expected_commit_inputs
actual_archive_hash = hashlib.sha256(pathlib.Path(archive).read_bytes()).hexdigest()
if actual_archive_hash != archive_hash:
    raise SystemExit("archive hash changed while writing release manifest")
actual_inventory_hash = tree_hash(candidate_root)
if actual_inventory_hash != inventory_hash:
    raise SystemExit("candidate inventory changed while writing release manifest")
payload = {
    "release_name": release_name,
    "release_version": str(source["package_version"]),
    "target_storage_version": str(source["target_storage_version"]),
    "migration_id": str(source["migration_id"]),
    "archive": archive,
    "archive_sha256": archive_hash,
    "source_manifest_sha256": hashlib.sha256(pathlib.Path(source_path).read_bytes()).hexdigest(),
    "candidate_file_inventory_sha256": inventory_hash,
    "commit_inputs": commit_inputs,
    "tree_inputs": expected_commit_inputs,
    "task_inputs": {
        "app_root": app_root, "runtime_root": runtime_root, "env_path": env_path,
        "service_name": service_name, "health_url": health_url,
    },
    "release_inputs": release_inputs,
}
pathlib.Path(output_path).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY
}

readback_release_manifest() {
  python3 - "$candidate_manifest_path" "$release_name" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("release_name") != sys.argv[2]:
    raise SystemExit("release manifest release name readback mismatch")
for key in ("release_version", "target_storage_version", "migration_id", "archive_sha256", "source_manifest_sha256", "candidate_file_inventory_sha256"):
    if not isinstance(payload.get(key), str) or not payload[key]:
        raise SystemExit(f"release manifest missing {key}")
commit_inputs = payload.get("commit_inputs")
if not isinstance(commit_inputs, dict):
    raise SystemExit("release manifest commit inputs readback is incomplete")
commit_keys = set(commit_inputs)
if commit_keys == {"market_hub_commit", "quote_mux_commit", "quote_mux_packages_commit"}:
    if any(not isinstance(commit_inputs[key], str) or len(commit_inputs[key]) != 40 or any(char not in "0123456789abcdefABCDEF" for char in commit_inputs[key]) for key in commit_keys):
        raise SystemExit("release manifest commit inputs readback is invalid")
elif commit_keys == {"market_hub_tree_sha256", "quote_mux_tree_sha256", "quote_mux_packages_tree_sha256"}:
    if any(not isinstance(commit_inputs[key], str) or not commit_inputs[key] for key in commit_keys):
        raise SystemExit("release manifest tree inputs readback is incomplete")
else:
    raise SystemExit("release manifest commit input schema is incomplete")
inputs = payload.get("task_inputs")
if not isinstance(inputs, dict) or any(not isinstance(inputs.get(key), str) or not inputs[key] for key in ("app_root", "runtime_root", "env_path", "service_name", "health_url")):
    raise SystemExit("release task inputs readback is incomplete")
print(payload["release_version"])
PY
}

inventory_hash() {
  python3 - "$1" <<'PY'
import hashlib
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
rows = []
for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != "release-manifest.json"):
    rows.append(f"{path.relative_to(root).as_posix()}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
print(hashlib.sha256(b"".join(rows)).hexdigest())
PY
}

health_readback() {
  local expected_release="${1:-}"
  local payload
  payload="$(curl -fsS --retry 30 --retry-delay 2 --retry-connrefused "$health_url")" || return 1
  printf '%s' "$payload" | python3 -c '
import json, sys
value = json.load(sys.stdin)
if not isinstance(value, dict) or value.get("status") != "ok":
    raise SystemExit("health response status is not ok")
expected = sys.argv[1]
if expected and str(value.get("version", value.get("release", ""))) != expected:
    raise SystemExit("health release version readback mismatch")
' "$expected_release"
}

capture_owner_quiescence_gate() {
  local state
  state="$(systemctl is-active "$service_name.service" 2>/dev/null || true)"
  local owner_path="${MARKETHUB_OWNER_STATE_PATH:-$runtime_root/audit/pending-due-owner}"
  if [[ "$state" != "active" ]]; then
    [[ "$state" == "inactive" && -z "$previous_current" ]] || {
      echo "unable to establish service state before cutover: $state" >&2
      return 1
    }
    [[ ! -e "$owner_path" ]] || { echo "active owner marker exists: $owner_path" >&2; return 1; }
    printf '{"service_state":"%s","capture_state":"inactive","owner_state":"inactive","quiescent":true}\n' "$state" >"$status_root/safety-gate-$release_name.json"
    return 0
  fi
  local api_base running
  api_base="${health_url%/api/health}"
  health_readback "" || { echo "active service health cannot be read before cutover" >&2; return 1; }
  running="$(curl -fsS --max-time 10 "$api_base/api/admin/capture-runs?status=running&limit=500")" || {
    echo "active capture state cannot be read before cutover" >&2
    return 1
  }
  RUNNING_PAYLOAD="$running" python3 - <<'PY'
import json
import os
value = json.loads(os.environ["RUNNING_PAYLOAD"])
if not isinstance(value, list):
    raise SystemExit("capture-runs response is not a list")
if value:
    raise SystemExit("active captures are still running")
PY
  owner_path="${MARKETHUB_OWNER_STATE_PATH:-$runtime_root/audit/pending-due-owner}"
  [[ ! -e "$owner_path" ]] || { echo "active owner marker exists: $owner_path" >&2; return 1; }
  printf '{"service_state":"active","capture_state":"empty","owner_state":"empty","quiescent":true}\n' >"$status_root/safety-gate-$release_name.json"
}

verify_saved_gate() {
  python3 - "$status_root/safety-gate-$release_name.json" <<'PY'
import json
import pathlib
import sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if value.get("quiescent") is not True or value.get("capture_state") not in {"empty", "inactive"}:
    raise SystemExit("saved safety gate is not quiescent")
PY
}

rollback_quiescence_gate() {
  local state owner_path
  state="$(systemctl is-active "$service_name.service" 2>/dev/null || true)"
  [[ "$state" == "inactive" || "$state" == "failed" ]] || {
    echo "service is not stopped for rollback: $state" >&2
    return 1
  }
  owner_path="${MARKETHUB_OWNER_STATE_PATH:-$runtime_root/audit/pending-due-owner}"
  [[ ! -e "$owner_path" ]] || {
    echo "active owner marker exists during rollback: $owner_path" >&2
    return 1
  }
}

[[ "$(id -u)" -eq 0 ]] || { echo "本脚本必须由 root 执行" >&2; exit 3; }
test -f "$archive"
id "$service_user" >/dev/null
service_group="$(id -gn "$service_user")"
test -x "/usr/lib/postgresql/$postgres_major/bin/initdb"
test -f "/usr/share/postgresql/$postgres_major/extension/timescaledb.control"
mkdir -p "$status_root"
printf 'running\n' >"$status_file"
previous_current="$(readlink -f "$app_root/current" 2>/dev/null || true)"
if [[ -e "$release_root" || -L "$release_root" ]]; then
  echo "refusing to replace an existing release: $release_root" >&2
  exit 6
fi
if [[ -f "$env_path" ]]; then
  env_existed=1
  cp -p -- "$env_path" "$env_backup"
fi
if [[ -f "$unit_path" ]]; then
  cp -p -- "$unit_path" "$unit_backup"
fi

finish() {
  local exit_code="$?"
  trap - EXIT
  if [[ "$exit_code" -ne 0 ]]; then
    # A failed candidate never deletes the old release.  Restore the old
    # pointer/config only when this invocation actually switched it; if the
    # safety gate was not proven, leave the service stopped for operator review.
    if [[ "$current_switched" -eq 1 && -n "$previous_current" ]]; then
      ln -sfn -- "$previous_current" "$app_root/current.next"
      mv -Tf -- "$app_root/current.next" "$app_root/current"
      current_switched=0
    fi
    if [[ "$env_existed" -eq 1 && -f "$env_backup" ]]; then
      cp -p -- "$env_backup" "$env_path"
    fi
    if [[ -f "$unit_backup" ]]; then
      cp -p -- "$unit_backup" "$unit_path"
    fi
    if [[ "$service_stopped" -eq 1 && "$rollback_authorized" -eq 1 ]]; then
      systemctl daemon-reload >/dev/null 2>&1 || true
      if rollback_quiescence_gate; then
        systemctl start "$service_name.service" >/dev/null 2>&1 || true
        health_readback "$(basename "$previous_current")" >/dev/null 2>&1 || true
      else
        echo "saved safety gate could not be revalidated; service remains stopped" >&2
      fi
    else
      echo "service remains stopped because rollback safety was not proven" >&2
    fi
    rm -rf -- "$candidate_root" 2>/dev/null || true
  fi
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
mkdir -p "$candidate_root"
tar --no-same-owner -xzf "$archive" -C "$candidate_root"
test -f "$candidate_root/MarketHub/migrations/storage_v2_20260823/manifest.json"
find "$candidate_root/MarketHub" -type f -name '*.sh' -exec chmod 0755 {} +
chown -R "$service_user:$service_group" "$candidate_root"
ensure_service_path_traversable "$candidate_root/MarketHub/services/markethub_api"
archive_hash="$(sha256_file "$archive")"
candidate_inventory_hash="$(inventory_hash "$candidate_root")"
write_release_manifest "$candidate_root/MarketHub/migrations/storage_v2_20260823/manifest.json" "$archive_hash" "$candidate_inventory_hash"
release_version="$(readback_release_manifest)"
mkdir -p "$status_root"
cp -p -- "$candidate_manifest_path" "$release_manifest_path"

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
QUOTEMUX_PACKAGE_REPO_SPEC=$candidate_root/QuoteMux_Packages
QUOTEMUX_PACKAGE_VENV_ROOT=$package_venv_root
QUOTEMUX_ALLOW_LOCAL_PACKAGE_REPO=true
ENV
fi

# Existing deployments keep database credentials and site-specific settings,
# but every release-scoped path must move atomically to the release being
# installed.  Otherwise maintenance processes that source this file can
# recreate a retired provider environment even when the systemd unit is new.
python3 "$candidate_root/MarketHub/migrations/storage_v2_20260823/sync_runtime_env.py" \
  --env-file "$env_path" \
  --app-root "$app_root" \
  --runtime-root "$runtime_root" \
  --release-root "$candidate_root" \
  --package-venv-root "$package_venv_root"
chmod 0600 "$env_path"
chown "$service_user:$service_group" "$env_path"

mkdir -p -- "$(dirname "$maintenance_lock_path")"
exec 8>>"$maintenance_lock_path"
flock -x 8 || { echo "unable to acquire stock-bar maintenance writer lock" >&2; exit 75; }
capture_owner_quiescence_gate
rollback_authorized=1
if [[ "$(systemctl is-active "$service_name.service" 2>/dev/null || true)" == "active" ]]; then
  # Re-read the live gate immediately before stopping the service.  The first
  # snapshot may have become stale while the candidate was prepared.
  capture_owner_quiescence_gate
  systemctl stop "$service_name.service" >/dev/null 2>&1 || { echo "service stop failed; old release remains current" >&2; exit 11; }
  service_stopped=1
fi

mv -- "$candidate_root" "$release_root"
candidate_root=""
release_root="$app_root/releases/$release_name"
ensure_service_path_traversable "$release_root/MarketHub/services/markethub_api"
python3 "$release_root/MarketHub/migrations/storage_v2_20260823/sync_runtime_env.py" \
  --env-file "$env_path" \
  --app-root "$app_root" \
  --runtime-root "$runtime_root" \
  --release-root "$release_root" \
  --package-venv-root "$package_venv_root"
chmod 0600 "$env_path"
chown "$service_user:$service_group" "$env_path"

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
export PYTHONPATH="$release_root/MarketHub/services/markethub_api:$release_root/QuoteMux/src:$release_root/MarketHub/services/markethub_api/src"
install -d -o "$service_user" -g "$service_group" \
  "$runtime_root"/{runtime,cache_payloads,package_venvs,store,logs,data-update,migrations,scripts} \
  "$package_venv_root" "$app_root/exports"
chown -R "$service_user:$service_group" "$release_root" "$runtime_root/.venv"
ensure_service_path_traversable "$runtime_root/.venv/bin"
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

verify_saved_gate
ln -sfn "$release_root" "$app_root/current.next"
mv -Tf "$app_root/current.next" "$app_root/current"
current_switched=1

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
Environment=PYTHONPATH=$app_root/current/MarketHub/services/markethub_api:$app_root/current/QuoteMux/src:$app_root/current/MarketHub/services/markethub_api/src
Environment=QUOTEMUX_PACKAGE_REPO_SPEC=$app_root/current/QuoteMux_Packages
Environment=QUOTEMUX_PACKAGE_VENV_ROOT=$package_venv_root
ExecStart=$runtime_root/.venv/bin/python $app_root/current/MarketHub/services/markethub_api/app.py
Restart=always
RestartSec=5
KillSignal=SIGTERM
# Capture and isolated provider workers can still be unwinding after the API
# receives SIGTERM.  Keep enough time for a graceful join; systemd still
# force-kills the complete service cgroup after this bounded deadline.
TimeoutStopSec=180s
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

verify_saved_gate
systemctl start "$service_name.service"
health_readback "$release_name"
rm -f -- "$archive"
echo "本机发布与迁移完成: $release_name"
