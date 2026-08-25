from __future__ import annotations

from pathlib import Path
import sys

import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from services import dataset_versions


CAPABILITY = "futures.quotes.back_adjusted_continuous.1m"
VERSION = "mhd-v1-futures-current"


def _online(monkeypatch: pytest.MonkeyPatch, status: str = "online") -> None:
    monkeypatch.setattr(dataset_versions, "current_dataset_version", lambda dataset_id: VERSION if dataset_id == "future_bar_1m" else "")
    monkeypatch.setattr(
        dataset_versions,
        "current_dataset_publications",
        lambda versions: {"future_bar_1m": {"dataset_version": versions["future_bar_1m"], "status": status}},
    )


def test_require_current_returns_the_online_registry_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _online(monkeypatch)

    assert dataset_versions.require_current(CAPABILITY, VERSION) == VERSION


@pytest.mark.parametrize("expected", ("", "mhd-v1-stale"))
def test_require_current_rejects_missing_or_stale_version(monkeypatch: pytest.MonkeyPatch, expected: str) -> None:
    _online(monkeypatch)

    with pytest.raises(RuntimeError, match="expected immutable|stale immutable"):
        dataset_versions.require_current(CAPABILITY, expected)


def test_require_current_rejects_unknown_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    _online(monkeypatch)

    with pytest.raises(RuntimeError, match="no MarketHub immutable dataset registry"):
        dataset_versions.require_current("stocks.quotes.intraday", VERSION)


@pytest.mark.parametrize("status", ("ready", "not_ready", "failed", ""))
def test_require_current_fails_closed_until_the_dataset_is_online(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    _online(monkeypatch, status)

    with pytest.raises(RuntimeError, match="not online"):
        dataset_versions.require_current(CAPABILITY, VERSION)
