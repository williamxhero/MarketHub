"""Pure verifier for complete same-process capture.run_due journal observations.

Not a runtime observer or authority from a persisted receipt. The trusted caller
must collect the complete current-boot, current-process-lifetime journal and
verify live process identity before and after collection; the resulting proof is
same-session only.
"""
from datetime import datetime, timezone
import hashlib
import json
import re


class OwnerJournalRejected(RuntimeError):
    pass


def _decimal_text(value):
    return isinstance(value, str) and value.isascii() and value.isdecimal()


def verify_owner_completion(
    records,
    *,
    pid,
    expected_start_utc,
    expected_boot_id,
    process_start_ticks,
    clock_ticks_per_second,
):
    """Correlate every start and finish by finish elapsed_ms, never by counts alone.

    The journal rows are bound to the live boot and process incarnation by
    requiring the expected boot ID and a monotonic journal timestamp at or after
    the live process ``/proc/<pid>/stat`` start_ticks value.
    """
    try:
        if type(pid) is not int or pid <= 0 or not isinstance(records, list) or not 0 < len(records) <= 1024:
            raise ValueError()
        if not isinstance(expected_start_utc, (list, tuple)) or not expected_start_utc:
            raise ValueError()
        if not isinstance(expected_boot_id, str) or not re.fullmatch(r"[0-9a-f]{32}", expected_boot_id):
            raise ValueError()
        if type(process_start_ticks) is not int or process_start_ticks < 0:
            raise ValueError()
        if type(clock_ticks_per_second) is not int or clock_ticks_per_second <= 0:
            raise ValueError()
        parsed = tuple(datetime.fromisoformat(x.replace("Z", "+00:00")) for x in expected_start_utc)
        if any(x.tzinfo is None or x.utcoffset() is None for x in parsed):
            raise ValueError()
        expected = tuple(int(x.astimezone(timezone.utc).timestamp() * 1_000_000) for x in parsed)
        if len(set(expected)) != len(expected):
            raise ValueError()
        process_start_monotonic_us = process_start_ticks * 1_000_000 // clock_ticks_per_second
        starts = []
        finishes = []
        cursors = set()
        previous_realtime = -1
        for row in records:
            if not isinstance(row, dict):
                raise ValueError()
            cursor = row["__CURSOR"]
            stamp = row["__REALTIME_TIMESTAMP"]
            monotonic = row["__MONOTONIC_TIMESTAMP"]
            message = row["MESSAGE"]
            if (
                not isinstance(cursor, str)
                or not cursor
                or cursor in cursors
                or row["_PID"] != str(pid)
                or row["_SYSTEMD_UNIT"] != "markethub-api.service"
            ):
                raise ValueError()
            if not _decimal_text(stamp) or not _decimal_text(monotonic) or not isinstance(message, str):
                raise ValueError()
            stamp = int(stamp)
            monotonic = int(monotonic)
            if stamp < previous_realtime or monotonic < process_start_monotonic_us:
                raise ValueError()
            previous_realtime = stamp
            cursors.add(cursor)
            boot_id = row["_BOOT_ID"]
            if not isinstance(boot_id, str) or not re.fullmatch(r"[0-9a-f]{32}", boot_id):
                raise ValueError()
            if boot_id != expected_boot_id:
                raise ValueError()
            if re.search(r"(?<!\w)memory_start label=capture\.run_due(?:\s|$)", message):
                starts.append((stamp, cursor))
            elif re.search(r"(?<!\w)memory_finish label=capture\.run_due(?:\s|$)", message):
                values = re.findall(r"(?<!\w)elapsed_ms=([0-9]+)(?=\s|$)", message)
                if len(values) != 1 or len(values[0]) > 15:
                    raise ValueError()
                finishes.append((stamp, cursor, int(values[0])))
            else:
                raise ValueError()
        if len(starts) != len(finishes) or len(starts) != len(expected):
            raise ValueError()
        # Marker timestamps have second precision; never synthesize missing starts.
        if sorted(s // 1_000_000 for s, _ in starts) != sorted(s // 1_000_000 for s in expected):
            raise ValueError()
        unused = set(range(len(starts)))
        pairs = []
        for end, end_cursor, elapsed in finishes:
            matches = [
                i
                for i in unused
                if starts[i][0] <= end and abs((end - starts[i][0]) - elapsed * 1000) <= 2_000_000
            ]
            if len(matches) != 1:
                raise ValueError()
            i = matches[0]
            unused.remove(i)
            pairs.append(
                {
                    "start_cursor": starts[i][1],
                    "finish_cursor": end_cursor,
                    "start_us": starts[i][0],
                    "finish_us": end,
                    "elapsed_ms": elapsed,
                }
            )
        if unused:
            raise ValueError()
        return {
            "pid": pid,
            "boot_id": expected_boot_id,
            "process_start_ticks": process_start_ticks,
            "clock_ticks_per_second": clock_ticks_per_second,
            "process_start_monotonic_us": process_start_monotonic_us,
            "completed_pairs": pairs,
            "journal_sha256": hashlib.sha256(
                json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        raise OwnerJournalRejected("owner journal incomplete, malformed or not bound to the current process") from None
