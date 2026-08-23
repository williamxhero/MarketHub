#!/usr/bin/env bash
set -Eeuo pipefail

# MarketHub 存储治理只处理可重建的发布产物，不触碰数据库、行情事实表和缓存正文。
MARKETHUB_ROOT="${MARKETHUB_ROOT:-/data/MarketHub2}"
RUNTIME_ROOT="${MARKETHUB_RUNTIME_ROOT:-/data/markethub}"
SERVICE_NAME="${MARKETHUB_SERVICE_NAME:-markethub-api}"
KEEP_RELEASES="${MARKETHUB_STORAGE_KEEP_RELEASES:-5}"
INBOX_RETENTION_DAYS="${MARKETHUB_STORAGE_INBOX_RETENTION_DAYS:-14}"
DRY_RUN=0

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
elif [[ $# -gt 0 ]]; then
    echo "用法: $0 [--dry-run]" >&2
    exit 2
fi

if ! [[ "$KEEP_RELEASES" =~ ^[1-9][0-9]*$ ]]; then
    echo "MARKETHUB_STORAGE_KEEP_RELEASES 必须是正整数" >&2
    exit 2
fi

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

remove_path() {
    local path="$1"
    case "$path" in
        "$MARKETHUB_ROOT"/releases/*|"$RUNTIME_ROOT"/package_venvs/*|"$MARKETHUB_ROOT"/inbox/*) ;;
        *)
            echo "拒绝删除非治理路径: $path" >&2
            exit 20
            ;;
    esac
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "计划删除 $path"
    else
        log "删除 $path"
        rm -rf -- "$path"
    fi
}

current_release="$(readlink -f "$MARKETHUB_ROOT/current")"
[[ -n "$current_release" && -d "$current_release" ]] || {
    echo "无法解析当前 MarketHub 发布目录" >&2
    exit 10
}

mapfile -t newest_releases < <(
    find "$MARKETHUB_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
        | sort -nr \
        | head -n "$KEEP_RELEASES" \
        | cut -d' ' -f2-
)

while IFS= read -r -d '' release; do
    resolved="$(readlink -f "$release")"
    keep=0
    [[ "$resolved" == "$current_release" ]] && keep=1
    for newest in "${newest_releases[@]}"; do
        [[ "$resolved" == "$newest" ]] && keep=1
    done
    [[ "$keep" -eq 1 ]] || remove_path "$resolved"
done < <(find "$MARKETHUB_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -print0)

current_venv="$(systemctl show "$SERVICE_NAME.service" -p Environment --value \
    | tr ' ' '\n' \
    | sed -n 's/^QUOTEMUX_PACKAGE_VENV_ROOT=//p' \
    | head -n 1)"
[[ -n "$current_venv" && -d "$current_venv" ]] || {
    echo "无法解析当前 QuoteMux provider 环境目录" >&2
    exit 11
}
package_venv_root="$RUNTIME_ROOT/package_venvs"
current_venv="$(readlink -f "$current_venv")"
[[ "$(dirname "$current_venv")" == "$(readlink -f "$package_venv_root")" ]] || {
    echo "当前 QuoteMux provider 环境不是 package_venvs 的直接子目录，拒绝治理" >&2
    exit 12
}

for venv in "$package_venv_root"/*; do
    [[ -d "$venv" ]] || continue
    [[ "$(readlink -f "$venv")" == "$current_venv" ]] || remove_path "$venv"
done

while IFS= read -r -d '' archive; do
    remove_path "$archive"
done < <(
    find "$MARKETHUB_ROOT/inbox" -mindepth 1 -maxdepth 1 -type f \
        \( -name '*.tgz' -o -name '*.tar.gz' -o -name '*.zip' \) \
        -mtime "+$INBOX_RETENTION_DAYS" -print0
)

log "治理完成；保留发布数=$KEEP_RELEASES，收件箱保留天数=$INBOX_RETENTION_DAYS，dry_run=$DRY_RUN"
df -h "$MARKETHUB_ROOT"
