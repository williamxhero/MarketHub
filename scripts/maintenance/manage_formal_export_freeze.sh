#!/usr/bin/env bash
set -Eeuo pipefail

FREEZE_ROOT="${MARKETHUB_FORMAL_EXPORT_FREEZE_ROOT:-/data/markethub/formal-export-freeze}"
ACTIVE_FILE="$FREEZE_ROOT/ACTIVE"

USER_TIMERS=(
    xdn-task-center-reconcile.timer
    xdn-task-markethub_availability_probe_0300.timer
    xdn-task-markethub_global_data_update.timer
    xdn-task-markethub_global_data_update_0030.timer
    xdn-task-markethub_global_data_update_0400.timer
    xdn-task-markethub_futures_1m_daily.timer
    xdn-task-markethub_storage_governance_weekly.timer
    xdn-task-crawler_provider_daily_close_sync.timer
    xdn-task-crawler_provider_weekly_concept_members_sync.timer
)

USER_SERVICES=(
    xdn-task-center-reconcile.service
    xdn-task-markethub_availability_probe_0300.service
    xdn-task-markethub_global_data_update.service
    xdn-task-markethub_global_data_update_0030.service
    xdn-task-markethub_global_data_update_0400.service
    xdn-task-markethub_futures_1m_daily.service
    xdn-task-markethub_storage_governance_weekly.service
    xdn-task-crawler_provider_daily_close_sync.service
    xdn-task-crawler_provider_weekly_concept_members_sync.service
)

SYSTEM_TIMERS=(
    markethub-stock-backfill-monitor.timer
)

SYSTEM_SERVICES=(
    markethub-stock-1m-annual-import.service
    markethub-stock-5m-backfill.service
    markethub-stock-finance-events-backfill.service
    markethub-stock-history-audit.service
    markethub-stock-industry-membership-backfill.service
    markethub-stock-margin-backfill.service
    markethub-stock-market-indicators-backfill.service
    markethub-stock-money-flow-backfill.service
    markethub-supermind-concept-history-import.service
)

usage() {
    printf 'usage: %s freeze LEASE_ID | reconcile LEASE_ID | restore LEASE_ID | status\n' "$0" >&2
    exit 2
}

unit_property() {
    local scope="$1" unit="$2" property="$3"
    if [ "$scope" = user ]; then
        systemctl --user show "$unit" --property="$property" --value
    else
        systemctl show "$unit" --property="$property" --value
    fi
}

system_systemctl() {
    sudo -n systemctl "$@"
}

require_systemctl_privilege() {
    system_systemctl show --property=Version --value >/dev/null
}

capture_unit_state() {
    local lease_dir="$1" scope="$2" unit="$3"
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$scope" \
        "$unit" \
        "$(unit_property "$scope" "$unit" LoadState)" \
        "$(unit_property "$scope" "$unit" UnitFileState)" \
        "$(unit_property "$scope" "$unit" ActiveState)" \
        >>"$lease_dir/unit-state.tsv"
}

capture_unit_state_once() {
    local lease_dir="$1" scope="$2" unit="$3"
    if grep -Fq "$scope"$'\t'"$unit"$'\t' "$lease_dir/unit-state.tsv"; then
        return
    fi
    capture_unit_state "$lease_dir" "$scope" "$unit"
}

restore_units() {
    local lease_dir="$1" scope unit load_state unit_file_state active_state
    while IFS=$'\t' read -r scope unit load_state unit_file_state active_state; do
        [ "$load_state" = loaded ] || continue
        if [ "$scope" = user ]; then
            if [ "$unit_file_state" = enabled ]; then
                systemctl --user enable "$unit" >/dev/null
            fi
            if [ "$active_state" = active ]; then
                systemctl --user start "$unit"
            fi
        else
            if [ "$unit_file_state" = enabled ]; then
                system_systemctl enable "$unit" >/dev/null
            fi
            if [ "$active_state" = active ]; then
                system_systemctl start "$unit"
            fi
        fi
    done <"$lease_dir/unit-state.tsv"
}

restore_reconcile_schedule() {
    local lease_dir="$1"
    if ! awk -F '\t' '
        $1 == "user" && $2 == "xdn-task-center-reconcile.timer" && $5 == "active" { found = 1 }
        END { exit(found ? 0 : 1) }
    ' "$lease_dir/unit-state.tsv"; then
        return
    fi
    # OnUnitActiveSec can remain elapsed when a previously active timer is
    # stopped for a long freeze and merely started again. Run the authoritative
    # reconciler once, then restart its timer from that fresh activation point.
    systemctl --user start xdn-task-center-reconcile.service
    systemctl --user restart xdn-task-center-reconcile.timer
    test "$(unit_property user xdn-task-center-reconcile.timer ActiveState)" = active
    test -n "$(unit_property user xdn-task-center-reconcile.timer NextElapseUSecMonotonic)"
}

assert_services_idle() {
    local scope="$1"
    shift
    local unit active
    for unit in "$@"; do
        active="$(unit_property "$scope" "$unit" ActiveState)"
        if [ "$active" = active ] || [ "$active" = activating ]; then
            printf 'refusing to interrupt active writer service: scope=%s unit=%s state=%s\n' "$scope" "$unit" "$active" >&2
            return 1
        fi
    done
}

freeze() {
    local lease_id="$1" unit committed=0
    local lease_dir="$FREEZE_ROOT/leases/$lease_id"
    if [ -e "$ACTIVE_FILE" ]; then
        printf 'a formal-export freeze is already active: %s\n' "$(cat "$ACTIVE_FILE")" >&2
        exit 3
    fi
    if [ -e "$lease_dir" ]; then
        printf 'lease already exists: %s\n' "$lease_dir" >&2
        exit 4
    fi

    require_systemctl_privilege
    mkdir -p "$lease_dir"
    : >"$lease_dir/unit-state.tsv"
    for unit in "${USER_TIMERS[@]}" "${USER_SERVICES[@]}"; do
        capture_unit_state "$lease_dir" user "$unit"
    done
    for unit in "${SYSTEM_TIMERS[@]}" "${SYSTEM_SERVICES[@]}"; do
        capture_unit_state "$lease_dir" system "$unit"
    done

    date -Ins >"$lease_dir/requested_at"
    hostname >"$lease_dir/hostname"
    curl --fail --silent --show-error http://127.0.0.1:8803/api/health >"$lease_dir/health-before.json"
    systemctl --user list-timers --all --no-pager >"$lease_dir/user-timers-before.txt"
    systemctl list-timers --all --no-pager >"$lease_dir/system-timers-before.txt"
    ps -eo pid,ppid,lstart,etime,stat,comm,args --sort=start_time >"$lease_dir/processes-before.txt"

    rollback_on_error() {
        local exit_code=$?
        if [ "$committed" -eq 0 ]; then
            restore_units "$lease_dir" || true
            printf 'freeze failed and original unit states were restored; lease=%s exit=%s\n' "$lease_id" "$exit_code" >&2
        fi
        exit "$exit_code"
    }
    trap rollback_on_error ERR INT TERM

    assert_services_idle user "${USER_SERVICES[@]}"
    assert_services_idle system "${SYSTEM_SERVICES[@]}"

    for unit in "${USER_TIMERS[@]}"; do
        systemctl --user disable --now "$unit"
    done
    for unit in "${SYSTEM_TIMERS[@]}"; do
        system_systemctl disable --now "$unit"
    done

    for unit in "${USER_TIMERS[@]}"; do
        test "$(unit_property user "$unit" ActiveState)" != active
    done
    for unit in "${SYSTEM_TIMERS[@]}"; do
        test "$(unit_property system "$unit" ActiveState)" != active
    done

    printf '%s\n' "$lease_id" >"$ACTIVE_FILE"
    date -Ins >"$lease_dir/frozen_at"
    committed=1
    trap - ERR INT TERM
    printf 'formal-export freeze active; lease=%s\n' "$lease_id"
    printf 'drain any already accepted in-process capture before starting the export.\n'
}

reconcile() {
    local lease_id="$1" active_id unit
    local lease_dir="$FREEZE_ROOT/leases/$lease_id"
    test -f "$ACTIVE_FILE" || { printf 'no active formal-export freeze\n' >&2; exit 5; }
    active_id="$(cat "$ACTIVE_FILE")"
    test "$active_id" = "$lease_id" || { printf 'active lease mismatch: expected=%s actual=%s\n' "$lease_id" "$active_id" >&2; exit 6; }
    test -f "$lease_dir/unit-state.tsv"

    require_systemctl_privilege
    assert_services_idle user "${USER_SERVICES[@]}"
    assert_services_idle system "${SYSTEM_SERVICES[@]}"

    for unit in "${USER_TIMERS[@]}" "${USER_SERVICES[@]}"; do
        capture_unit_state_once "$lease_dir" user "$unit"
    done
    for unit in "${SYSTEM_TIMERS[@]}" "${SYSTEM_SERVICES[@]}"; do
        capture_unit_state_once "$lease_dir" system "$unit"
    done

    for unit in "${USER_TIMERS[@]}"; do
        systemctl --user disable --now "$unit"
    done
    for unit in "${SYSTEM_TIMERS[@]}"; do
        system_systemctl disable --now "$unit"
    done

    for unit in "${USER_TIMERS[@]}"; do
        test "$(unit_property user "$unit" ActiveState)" != active
    done
    for unit in "${SYSTEM_TIMERS[@]}"; do
        test "$(unit_property system "$unit" ActiveState)" != active
    done

    date -Ins >"$lease_dir/reconciled_at"
    printf 'formal-export freeze reconciled; lease=%s\n' "$lease_id"
}

restore() {
    local lease_id="$1" active_id
    local lease_dir="$FREEZE_ROOT/leases/$lease_id"
    test -f "$ACTIVE_FILE" || { printf 'no active formal-export freeze\n' >&2; exit 5; }
    active_id="$(cat "$ACTIVE_FILE")"
    test "$active_id" = "$lease_id" || { printf 'active lease mismatch: expected=%s actual=%s\n' "$lease_id" "$active_id" >&2; exit 6; }
    test -f "$lease_dir/unit-state.tsv"
    require_systemctl_privilege
    restore_units "$lease_dir"
    rm -f "$ACTIVE_FILE"
    restore_reconcile_schedule "$lease_dir"
    date -Ins >"$lease_dir/restored_at"
    printf 'formal-export freeze restored; lease=%s\n' "$lease_id"
}

status() {
    if [ ! -f "$ACTIVE_FILE" ]; then
        printf 'formal-export freeze: inactive\n'
        return
    fi
    local lease_id lease_dir
    lease_id="$(cat "$ACTIVE_FILE")"
    lease_dir="$FREEZE_ROOT/leases/$lease_id"
    printf 'formal-export freeze: active\nlease=%s\n' "$lease_id"
    printf 'frozen_at=%s\n' "$(cat "$lease_dir/frozen_at")"
    curl --fail --silent --show-error http://127.0.0.1:8803/api/health
    printf '\n'
}

command="${1:-}"
case "$command" in
    freeze)
        [ "$#" -eq 2 ] || usage
        freeze "$2"
        ;;
    reconcile)
        [ "$#" -eq 2 ] || usage
        reconcile "$2"
        ;;
    restore)
        [ "$#" -eq 2 ] || usage
        restore "$2"
        ;;
    status)
        [ "$#" -eq 1 ] || usage
        status
        ;;
    *)
        usage
        ;;
esac
