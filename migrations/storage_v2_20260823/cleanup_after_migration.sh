#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" != "--apply" || "${2:-}" != "--confirm-target-version" || -z "${3:-}" ]]; then
  echo "用法: $0 --apply --confirm-target-version <target-storage-version>" >&2
  exit 2
fi

SCRIPT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MARKETHUB_ROOT="${MARKETHUB_ROOT:-/data/MarketHub2}"
RUNTIME_ROOT="${MARKETHUB_RUNTIME_ROOT:-/data/markethub}"
ENV_PATH="${MARKETHUB_ENV_PATH:-$RUNTIME_ROOT/env/markethub.env}"
PYTHON="${MARKETHUB_VENV_ROOT:-$RUNTIME_ROOT/.venv}/bin/python"
EVIDENCE_ROOT="$RUNTIME_ROOT/migrations/markethub-storage-v2-20260823"

test -x "$PYTHON"
test -f "$ENV_PATH"
mkdir -p "$EVIDENCE_ROOT"

"$PYTHON" "$SCRIPT_ROOT/release_migration.py" \
  --env-file "$ENV_PATH" \
  --output "$EVIDENCE_ROOT/verify-before-cleanup.json" \
  verify

"$PYTHON" "$SCRIPT_ROOT/release_migration.py" \
  --env-file "$ENV_PATH" \
  --output "$EVIDENCE_ROOT/legacy-cleanup.json" \
  cleanup-legacy --confirm-target-version "$3"

"$PYTHON" "$SCRIPT_ROOT/release_migration.py" \
  --env-file "$ENV_PATH" \
  --output "$EVIDENCE_ROOT/verify-after-cleanup.json" \
  verify

governance="$MARKETHUB_ROOT/current/MarketHub/scripts/maintenance/storage-governance.sh"
if [[ -x "$governance" ]]; then
  MARKETHUB_ROOT="$MARKETHUB_ROOT" MARKETHUB_RUNTIME_ROOT="$RUNTIME_ROOT" "$governance"
fi

df -h "$MARKETHUB_ROOT"
