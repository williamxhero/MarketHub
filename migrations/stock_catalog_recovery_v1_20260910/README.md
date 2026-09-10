# Stock catalog recovery v1

This migration owns the first production activation of the authoritative stock
catalog. It is fail-closed and has four operator-visible gates:

1. `capture_yosef_authority.ps1` streams `capture_authority.py` to the already
   configured Tushare package venv. The remote side performs only database/API
   reads and writes the JSON bundle to stdout; the controller persists it
   locally.
2. `release_migration.py preflight` validates the bundle hash, all three
   required authority shards, the captured database fingerprint, latest
   completed trading day, current health token, and exact release inputs. Exit
   code `20` means NO-GO and performs no mutation.
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
