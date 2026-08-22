from __future__ import annotations

import json

from quotemux.infra.db import client


def main() -> int:
    batches = list(client.stream_query_batches("select value from generate_series(1,2050) value", batch_size=512))
    sizes = [len(batch) for batch in batches]
    if sizes != [512, 512, 512, 512, 2]:
        raise AssertionError(f"unexpected batch sizes: {sizes}")
    stream = client.stream_query_batches("select value from generate_series(1,100000) value", batch_size=256)
    first = next(stream)
    stream.close()
    metrics = client.get_pool_metrics()
    if len(first) != 256 or metrics["active"] != 0:
        raise AssertionError(f"early-close cleanup failed: first={len(first)} metrics={metrics}")
    print(json.dumps({"batch_sizes": sizes, "early_close_rows": len(first), "pool_metrics": metrics}, sort_keys=True))
    client.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
