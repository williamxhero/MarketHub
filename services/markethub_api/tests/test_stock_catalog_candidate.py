from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import sys

import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from services.stock_catalog_candidate import CatalogCandidateRejected, CatalogSourceEvidence, build_stock_catalog_candidate


def _evidence(**changes: object) -> CatalogSourceEvidence:
    payload: dict[str, object] = {
        "input_id": "a" * 64,
        "content_sha256": "b" * 64,
        "provider": "tushare",
        "source_refreshed_at": datetime(2026, 9, 10, 9, tzinfo=timezone.utc),
        "fresh_through": date(2026, 9, 9),
        "provisional_count": 1,
        "conflict_count": 0,
    }
    payload.update(changes)
    return CatalogSourceEvidence(**payload)


def _row(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "identity_status": "authoritative",
        "identity_source": "tushare_catalog",
        "market": "SZSE",
        "code": "301699",
        "name": "  回归样例  ",
        "listing_board": "创业板",
        "listed_date": "2026-09-08",
        "delisted_date": "",
        "industry": "",
        "area": "",
    }
    payload.update(changes)
    return payload


def test_candidate_is_authoritative_only_normalized_stable_and_content_addressed() -> None:
    candidate = build_stock_catalog_candidate(
        [
            _row(code="920268", market="BJSE", name="北交样例", listing_board="北交所"),
            _row(identity_status="provisional", code="000001", name="不应发布"),
            _row(),
        ],
        _evidence(),
        expected_fresh_through=date(2026, 9, 9),
    )

    assert [item.code for item in candidate.items] == ["301699", "920268"]
    assert candidate.items[0].name == "回归样例"
    assert candidate.provisional_count == 1
    assert candidate.version.startswith("mhc-v1-")

    replay = build_stock_catalog_candidate(
        list(reversed([_row(), _row(code="920268", market="BJSE", name="北交样例", listing_board="北交所")])),
        _evidence(source_refreshed_at=datetime(2026, 9, 10, 11, tzinfo=timezone.utc)),
        expected_fresh_through=date(2026, 9, 9),
    )
    changed = build_stock_catalog_candidate(
        [_row(industry="软件服务"), _row(code="920268", market="BJSE", name="北交样例", listing_board="北交所")],
        _evidence(),
        expected_fresh_through=date(2026, 9, 9),
    )

    assert replay.version == candidate.version
    assert changed.version != candidate.version


@pytest.mark.parametrize("invalid_name", ("", "\u2003\t", None, 7))
def test_candidate_rejects_every_invalid_authoritative_name(invalid_name: object) -> None:
    with pytest.raises(CatalogCandidateRejected, match="name"):
        build_stock_catalog_candidate([_row(name=invalid_name)], _evidence(), expected_fresh_through=date(2026, 9, 9))


def test_candidate_fails_closed_for_stale_or_conflicted_authority_input() -> None:
    with pytest.raises(CatalogCandidateRejected, match="fresh"):
        build_stock_catalog_candidate([_row()], _evidence(fresh_through=date(2026, 9, 8)), expected_fresh_through=date(2026, 9, 9))
    with pytest.raises(CatalogCandidateRejected, match="conflict"):
        build_stock_catalog_candidate([_row()], _evidence(conflict_count=1), expected_fresh_through=date(2026, 9, 9))
