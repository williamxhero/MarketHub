"""Local production bundle adaptation of the reviewed owner-completion seam.

This module contains no subprocess, network or filesystem IO.  The caller must
supply freshly collected journal/API bytes and the current marker bytes.
"""
from __future__ import annotations

import json
from typing import Any

from spec36_owner_journal import OwnerJournalRejected, verify_owner_completion

CONTRACT = "markethub-adopt-legacy-due-owner-v1"


def normalize_capture_response(payload: object) -> list[dict[str, Any]]:
    """Accept only the actual list contract or an explicit items wrapper."""
    value = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise OwnerJournalRejected("capture response is not a bounded object list")
    return value


def verify_pending_owner(marker_raw: bytes, records: list[dict[str, Any]], *, pid: int,
                         boot_id: str, start_ticks: int, clock_ticks_per_second: int) -> dict[str, Any]:
    try:
        marker = json.loads(marker_raw)
        if not isinstance(marker, dict) or marker.get("contract") != CONTRACT:
            raise ValueError
        expected = marker.get("due_chain_memory_start_utc")
        return verify_owner_completion(records, pid=pid, expected_start_utc=expected,
                                       expected_boot_id=boot_id, process_start_ticks=start_ticks,
                                       clock_ticks_per_second=clock_ticks_per_second)
    except (OwnerJournalRejected, TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise OwnerJournalRejected("owner journal does not prove the current pending owner") from None
