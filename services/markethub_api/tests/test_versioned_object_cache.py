from __future__ import annotations

from pathlib import Path
import sys


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from services import versioned_object_cache


def test_versioned_object_cache_singleflight_and_version_keying() -> None:
    versioned_object_cache.clear()
    builds = 0

    def builder() -> dict[str, object]:
        nonlocal builds
        builds += 1
        return {"value": builds}

    first, first_hit = versioned_object_cache.get_or_build("stock-reference-v1|catalog", builder)
    second, second_hit = versioned_object_cache.get_or_build("stock-reference-v1|catalog", builder)
    third, third_hit = versioned_object_cache.get_or_build("stock-reference-v2|catalog", builder)

    assert first == second == {"value": 1}
    assert third == {"value": 2}
    assert (first_hit, second_hit, third_hit) == (False, True, False)
