# Stock catalog recovery v1

This migration owns the first production activation of the authoritative stock
catalog. It is fail-closed and has four operator-visible gates:

1. `capture_yosef_authority.ps1` streams `capture_authority.py` to the already
   configured Tushare package venv. The remote side performs only database/API
   reads and writes the JSON bundle to stdout; the controller persists it
   locally.
2. `release_migration.py preflight` validates the bundle hash, all three
   required authority shard keys and their direct-provider receipts, the
   captured database fingerprint, latest completed trading day, current health
   token, and exact release inputs. Exit code `20` means NO-GO and performs no
   mutation.

The listed shard must contain rows. Pending and delisted are status sets and may
legitimately be empty only when their direct Tushare calls completed with the
expected schema and the bundle carries matching row-count, payload, and
normalized hashes. Missing keys, malformed or stale receipts, schema drift, and
failed provider calls remain NO-GO conditions.

Only canonical stock identities whose provider `symbol` is six ASCII digits
and whose `ts_code` has the same symbol plus `.SH`, `.SZ`, or `.BJ` enter the
catalog. Provider-only synthetic or malformed identifiers are not rewritten;
they are excluded with a reason, count, and hash in each shard receipt, and
preflight requires provider rows to equal accepted plus rejected rows.
3. `release_migration.py rehearse` is accepted only when the target database
   name contains `spec3`, `rehearsal`, or `test`. It expands the schema,
   freezes/reconciles the authority input, repeats the same run key, publishes,
   and verifies the isolated result.
4. Production `apply` is invoked only by `scripts/local/deploy_yosef_server.ps1`
   after the controlled service stop and requires the literal controller token
   `SPEC-3-CONTROLLER-APPLY`. `verify` runs read-only after the new release is
   healthy.

Before apply begins, ordinary deployment rollback keeps the prior release. Once
apply begins, the deploy trap never restarts a legacy release that could serve
reconciled names under an old dirty data version. A failure leaves MarketHub
stopped for inspected forward recovery. After success, rollback eligibility is
limited to retained mappings whose mapping and catalog statuses are both
`healthy`; known dirty versions are permanently quarantined.
