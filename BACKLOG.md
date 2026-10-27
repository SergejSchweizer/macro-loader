# Backlog

This backlog records the active GOALS.md implementation sequence, durable repository
contracts, and compact historical context for `macro-loader`.

The repository loads reusable daily market-state inputs from open/public sources, preserves source history, performs strict incremental updates during normal execution, and publishes deterministic immutable Gold feature snapshots through a Bronze -> Silver -> Gold architecture.

Last reviewed: 2026-10-27

## GOALS.md Implementation Wave

### PR-200: Retire Wide Feature Views And Publish Long View As `macro_features`

PR name: `rename-long-feature-materialized-view`
Status: PR #200 open — local quality gate and disposable PostgreSQL acceptance passed; production deployment pending
Updated: 2026-10-27
Git branch: `pr-200/rename-long-feature-materialized-view`
PR: [#200](https://github.com/SergejSchweizer/macro-loader/pull/200) (open)
Git status: pushed-local-checks-passed
Agent lane: make the raw-derived long transformation materialized view the sole public feature view
Depends on: PR-199 (merged to `origin/main`)
Commit: `f66df93 feat(pr-200): rename long feature view and retire legacy views`; `2baa229 test(pr-200): validate renamed view calculations`; `8a2e7d2 docs(pr-200): record pull request and local acceptance`
Design patterns: Versioned Migration, Materialized View, Catalog Conformance.

Description:
- R1: Rename the populated `macro_loader.macro_feature_values` materialized view to
  `macro_loader.macro_features`; preserve its rows and four-column long-form contract,
  generated availability, indexes, ownership, and consumer/sync SELECT access.
- R2: Retire the superseded wide public views `macro_loader.macro_features` and
  `macro_loader.macro_features_extended` before the rename, plus the private
  `macro_feature_raw_projection` and `macro_feature_transform_<series>` materialized
  views used only by that wide-view chain. Preserve all `macro_raw_<feature>` tables,
  canonical Gold sync tables/state, and the independent raw-derived long-view calculation.
- R3: Update the refresh routine, runtime schema verifier, role provisioner, migrations,
  README, architecture, and tests so only the long-form `macro_loader.macro_features`
  transformation view remains; reject any retired view objects after migration.
- R4: Apply as a new idempotent migration, avoiding any long-view recomputation during
  rename. Confirm existing rows/indexes survive, then run a zero-change sync replay and
  independent production conformance after local QA.

Acceptance:
- A1 disposable PostgreSQL upgrade verifies migration 36 removes precisely the retired
  wide/private view chain, renames the populated long materialized view in place, keeps
  its data, columns, indexes, owner, grants, and valid refresh routine, and is replay-safe.
- A2 runtime and independent verifiers confirm `macro_features` has exactly
  `timestamp_m1`, `available_at_utc`, `feature_name`, `value DOUBLE PRECISION`, and no
  obsolete wide, extended, raw-projection, or per-series transform materialized views
  remain in `macro_loader`.
- A3 focused mathematical/source-parity tests and `make quality-gate` pass locally. No
  GitHub Actions or remote quality gates.
- A4 production migration preserves all existing long-view rows, removes only the
  explicitly retired materialized views, passes independent conformance, and a canonical
  `postgres-sync` replay reports zero inserted/updated/deleted Gold rows.

Local A1/A3 evidence on 2026-10-27: a populated disposable PostgreSQL view retained its
OID and rows through migration 36, all simulated legacy views were removed, refresh replay
preserved the values, and an upgrade from simulated schema 23 completed. A separate
hand-calculated return/mean/volatility parity test passed against the long view.
`make quality-gate` passed (518 unit tests, 37 offline integration tests, 89.56% coverage);
no GitHub Actions were used. Production migration and replay remain pending.

### PR-199: Publish Long Transformations As A Raw-Derived Materialized View

PR name: `long-feature-values-materialized-view`
Status: Merged — production migration 35 and long-view population completed; PR-200 adds the requested sole-view rename/removal
Updated: 2026-10-27
Git branch: `pr-199/long-feature-materialized-view`
PR: [#199](https://github.com/SergejSchweizer/macro-loader/pull/199) (merged)
Git status: merged
Agent lane: publish the 1,720 typed transformations directly from the core raw feature tables
Depends on: PR-198 (merged to `origin/main`)
Commit: `9342d28 feat(pr-199): replace long table with raw-derived materialized view`
Design patterns: Materialized View, Registry-driven Projection, Catalog Conformance.

Description:
- R1: Replace `macro_loader.macro_feature_values` base table with a materialized view that
  computes all primary and extended catalog transformations directly from the 25
  `macro_raw_<feature>` tables; do not source either existing public feature view.
- R2: Preserve the four-column long contract, UTC next-trading-day availability, null/warm-up
  omission, numeric values, and boolean `1.0`/`0.0` encoding. Add a unique index on
  `(timestamp_m1, feature_name)` and a feature-first lookup index.
- R3: Refresh the materialized view after source sync and update migration, role provisioning,
  schema conformance, independent verification, and operator documentation. Refresh is a full
  recomputation; document and test its timeout behavior.

Acceptance:
- A1 verifies the materialized-view SQL reads the registered raw feature tables and neither
  existing public feature view, and exposes exactly the declared four columns.
- A2 disposable PostgreSQL tests verify all catalog names/types, availability values, NULL and
  warm-up omission, boolean encoding, unique key, replay, and raw-source insert/update/delete
  refresh behavior.
- A3 schema migration, role provisioning, runtime preflight, and independent conformance all
  recognize the relation as a materialized view; `make quality-gate` passes locally. No GitHub
  Actions.
- A4 run the authorized production migration/refresh only after local QA, then independently
  verify catalog coverage, counts, sampled values, and a zero-change sync replay. If the full
  raw-source refresh exceeds the configured bound, keep production unchanged and report that
  the selected materialized-view design needs a separate runtime-boundary decision.

Production migration 35 completed on 2026-10-27. Read-only production checks found
11,794,988 unique rows across 1,720 features (1962-01-02 through 2026-10-27), no
NaN/Infinity, correct indexes/grants, and 200 sampled values/availability timestamps
matching the existing feature views. The production CLI conformance run and zero-change
replay are still being verified; PR-200 owns the subsequent view retirement and rename.

### PR-198: Materialize The Long-Table Source Once Per Refresh

PR name: `materialize-feature-value-source-once`
Status: Merged — code merged; production population was not completed and is superseded by PR-199
Updated: 2026-10-27
Git branch: `pr-198/materialize-feature-value-source-once`
PR: [#198](https://github.com/SergejSchweizer/macro-loader/pull/198) (merged)
Git status: merged
Agent lane: keep migration 34 within the production statement-timeout boundary
Depends on: PR-197 (merged to `origin/main`)
Commit: `742cc34 fix(pr-198): stage long feature source once`; squash merge `0334a7b`
Design patterns: Unit of Work, Materialized Staging Relation.

Description:
- R1: Compute the direct-raw-source JSONB projection once into a transaction-scoped
  temporary staging table, validate that staged projection, then populate the durable
  long table from it. Preserve rollback safety and the existing numeric/boolean contract.
- R2: Prevent the prior duplicate evaluation of the full 1,720-feature projection, which
  caused migration 34 to exceed the 15-minute production statement timeout. Keep the
  potentially expensive population in separately retryable migration 35.
- R3: Document the production timeout/rollback result and the exact retry behavior.

Acceptance:
- A1 verifies source SQL is materialized once per refresh, validation reads only staging,
  and publication remains atomic with JSONB type checks and boolean `1.0`/`0.0` conversion.
- A2 executes migration/refresh in disposable PostgreSQL and verifies data, replay,
  rollback on invalid type, and no duplicate `(timestamp_m1, feature_name)` keys.
- A3 passes local `make quality-gate`; no GitHub Actions.
- A4 production attempt exceeded the configured 15-minute timeout and rolled back atomically;
  PR-199 supersedes this storage design before another production attempt.

### PR-197: Validate Raw JSON Types Before Long-Table Casting

PR name: `fix-long-feature-values-migration`
Status: Merged — local gate passed; production retry timed out and rolled back
Updated: 2026-10-27
Git branch: `pr-197/fix-long-feature-values-migration`
PR: [#197](https://github.com/SergejSchweizer/macro-loader/pull/197) (merged)
Git status: merged
Agent lane: correct migration 34 raw-value type validation before production rollout
Depends on: PR-196 (merged to `origin/main`)
Commit: `33ab753 fix(pr-197): validate raw JSON types before casting`; squash merge `e5b6a91`
Design patterns: Versioned Migration, independent source validation.

Description:
- R1: Keep migration 34's invalid-source validation on raw JSONB values; only the
  population projection may cast supported numeric/boolean values to `DOUBLE PRECISION`.
- R2: Add a regression assertion that protects the distinction between raw validation
  and typed population SQL. Production's failed v34 attempt rolled back atomically, so
  production remains on v33 until this correction is merged.

Acceptance:
- A1 verifies raw JSONB comparisons are used for type/null validation and the insert
  projection remains `DOUBLE PRECISION`, including booleans encoded as `1.0`/`0.0`.
- A2 executes migration 34's refresh function on a disposable PostgreSQL fixture and
  verifies populated rows and boolean encodings.
- A3 passes local `make quality-gate`; no GitHub Actions.
- A4 production migration attempt timed out and rolled back atomically; superseded by
  PR-198's split schema/population migrations and repeat production acceptance.

### PR-196: Publish PostgreSQL Transformations In A Long Feature-Values Table

PR name: `macro-feature-values-long-table`
Status: Merged — implementation merged; production migration pending optimized backfill
Updated: 2026-10-27
Git branch: `pr-196/macro-feature-values-long-table`
PR: [#196](https://github.com/SergejSchweizer/macro-loader/pull/196) (merged)
Git status: merged
Agent lane: expose the complete PostgreSQL transformation catalog in a row-oriented relation
Depends on: PR-195 (merged to `origin/main`)
Commit: `c477c4a` (squash merge); original commits: `feat(pr-196): store long-form values
  as doubles`; `refactor(pr-196): derive long feature values directly from raw tables`
Design patterns: Repository, Unit of Work, explicit feature-catalog projection.

Description:
- R1: Add `macro_loader.macro_feature_values` with one sparse row per non-null
  transformation output and observation timestamp; include generated `available_at_utc`,
  `feature_name`, and `value DOUBLE PRECISION`; boolean Donchian flags map to `1.0`/`0.0`.
- R2: Publish every current primary and extended catalog transformation, excluding raw
  OHLC passthrough columns; absent rows represent the existing SQL NULL/warm-up semantics.
- R3: Calculate directly from the 25 `macro_raw_<feature>` tables, independently of both
  public materialized views; rebuild transactionally with raw table changes. Use a primary
  key on `(timestamp_m1, feature_name)`, a feature-first lookup index, owner-only writes,
  and read access for consumer and sync roles.
- R4: Include the table in migrations, schema conformance, independent QA, operator docs,
  and populate it in the authorized production database.

Acceptance:
- A1 upgrades a populated v33 database through migration 34 without changing the two
  existing materialized-view contracts; fresh installs also produce the table.
- A2 verifies exact feature-name/type coverage against both catalogs, numeric and
  boolean-to-`1.0`/`0.0` encoding, omission of SQL NULLs, generated UTC availability,
  unique key, and grants.
- A3 proves the generated source SQL reads raw feature tables and references neither public
  view; insert, update, deletion, and replay leave the long table equal to the catalogued
  raw-derived calculations. Local `make quality-gate` passes. No GitHub Actions.
- A4 migrates and populates production only after local QA; independent read-only checks
  reconcile row counts and sampled values to both views, including boolean outputs and
  NULL/warm-up absence, then confirm the cron sync refreshes the table.

### PR-195: Run Daily 09:00 PostgreSQL Delta Sync With 30-Observation Coverage

PR name: `daily-09-cron-30obs`
Status: Merged — daily cron and live acceptance passed
Updated: 2026-10-27
PR: [#195](https://github.com/SergejSchweizer/macro-loader/pull/195) (merged)
Git branch: `pr-195/daily-09-cron-30obs`
Git status: merged
Agent lane: align installed daily ingestion and PostgreSQL synchronization with GOALS.md
Depends on: PR-194 (merged to `origin/main`)
Commit: `ed5fae4 fix(pr-195): run daily PostgreSQL feature sync`
Design patterns: Command, bounded delta policy, acceptance verifier.

Description:
- R1: Replace the weekly Sunday schedule with one daily 09:00 `Europe/Vienna` job and
  use a bounded 60-calendar-day source overlap so daily series cover at least 30 recent
  weekday EOD observations without invoking full-history reconciliation.
- R2: Rename the runner to reflect daily execution and retain logging, locking,
  configuration validation, core parallelism, and Gold-before-PostgreSQL ordering.
- R3: Make cron acceptance distinguish successful wrapper exits from a true no-op sync;
  require zero inserted/updated/deleted rows on explicit replay and independent
  `postgres-verify`.
- R4: Verify the installed daily schedule and document measured deployment state.

Acceptance:
- A1 verifies exactly one cron entry at `0 9 * * *` under `CRON_TZ=Europe/Vienna` and
  the daily wrapper invokes `run-daily` with `--overlap-days 60` before Gold sync.
- A2 verifies wrapper locking, runtime config validation, `--cores` forwarding, failure
  stop behavior, and that GitHub Actions remain absent/unused.
- A3 proves the acceptance report only passes after both daily wrapper runs, a parsed
  zero-change PostgreSQL replay, and independent conformance verification pass.
- A4 passed 2026-10-27: local quality gate passed; 25/25 raw tables had 37–41 observations
  in 2026-08-29–2026-10-27; both views had 16,805 rows through 2026-10-27 and availability
  through 2026-10-28; the installed daily crontab was verified. Both wrapper runs passed,
  replay was 0/0/0, and independent `postgres-verify` passed. No GitHub Actions.

### PR-194: Reconcile Gold Across Consecutive Schema-Only Versions

PR name: `allow-schema-version-sync-jumps`
Status: Merged — production Gold sync and independent verification complete
Updated: 2026-10-27
PR: [#194](https://github.com/SergejSchweizer/macro-loader/pull/194) (merged)
Git branch: `pr-194/allow-schema-version-sync-jumps`
Git status: merged
Agent lane: reconcile the current canonical Gold build into the newly cut-over serving tables
Depends on: PR-193 (merged to `origin/main`)
Commit: `9a4f28c fix(pr-194): coalesce schema-only Gold sync upgrades` (squash merge)
Design patterns: Explicit Compatibility Policy, Unit of Work.

Description:
- R1: Allow an accumulated sync to advance across skipped schema-only versions 7 through
  11 when feature version 6 is unchanged; keep feature-version changes, backwards moves,
  and out-of-range versions fail-closed.
- R2: Treat these supported jumps as a schema upgrade so canonical source rows and
  digests are reconciled transactionally without resetting PostgreSQL sync state.
- R3: Document the compatibility rule and production cutover/sync status.

Acceptance:
- A1 verifies 7/6→11/6 and supported intermediate schema-only jumps are accepted and
  planned as schema upgrades; feature-version changes, backwards versions, and versions
  outside the schema-7-to-11 interval are rejected before any repository mutation.
- A2 verifies an existing target with stale row digests reconciles all current Gold rows,
  advances its checkpoint only after successful atomic apply, and remains retryable after
  failure.
- A3 passes local `make quality-gate`; PostgreSQL disposable integration tests pass when
  configured. No GitHub Actions.
- A4 passed 2026-10-27: production sync advanced state from schema 7 / feature 6 to
  schema 11 / feature 6; the consumer and digest index both contain 16,804 rows at the
  time of this PR. The independent verifier and zero-change replay passed, and all 25
  raw tables were populated before PR-195 widened the scheduled overlap.

### PR-193: Archive Orphaned Retired Fed Table Before Schema Conformance

PR name: `archive-orphaned-retired-fed-table`
Status: Merged — production schema/view cutover and canonical Gold sync complete
Updated: 2026-10-27
PR: [#193](https://github.com/SergejSchweizer/macro-loader/pull/193) (merged)
Git branch: `pr-193/archive-orphaned-retired-fed-table`
Git status: merged
Agent lane: repair the production migration ledger/object drift without discarding retired data
Depends on: PR-192 (merged to `origin/main`)
Commit: `521c843 fix(pr-193): archive orphaned retired fed table` (squash merge)
Design patterns: Versioned Migration, Idempotent Archive.

Description:
- R1: Add migration 33 that replays the data-preserving Fed archive/drop prefix after
  feature-table and view cutover, for installations whose ledger says Fed retirement is
  complete but whose active `fed_policy_features_daily` relation remains.
- R2: Do not rebuild or alter the published feature views in this cleanup migration.
- R3: Document the recovery behavior and retain the final owned-table conformance check.

Acceptance:
- A1 creates a disposable v23 schema, simulates a missing `macro_loader_retired` schema
  plus an orphaned populated `macro_loader_sync.fed_policy_features_daily`, and verifies
  the normal migrator archives every legacy value, removes the active relation, creates
  all 25 feature tables, removes the wide `macro_raw`, publishes both conforming views,
  and records migration 33. Replaying the migrator preserves the archive and passes
  conformance.
- A2 verifies unit contracts for migration 33, archive-before-drop ordering, and no view
  rebuild in the cleanup migration.
- A3 passes `make quality-gate` and the targeted disposable PostgreSQL upgrade locally;
  no GitHub Actions.
- A4 passed 2026-10-27: production migration 33 is applied; all 25 raw feature tables
  exist; the wide and orphaned retired tables are absent. PR-194 subsequently synced
  canonical Gold and independently verified both views.

### PR-192: Defer Expensive View Rebuilds Until Feature-Table Cutover

PR name: `defer-view-refresh-until-cutover`
Status: Merged — production cutover pending
Updated: 2026-10-27
PR: [#192](https://github.com/SergejSchweizer/macro-loader/pull/192) (merged)
Git branch: `pr-192/defer-view-refresh-until-cutover`
Git status: merged
Agent lane: complete the production cutover within a bounded migration window
Depends on: PR-191 (merged to `origin/main`)
Commit: `357f292 fix(pr-192): defer expensive views until feature cutover` (squash merge)
Design patterns: Versioned Migration, Bounded Execution Policy.

Description:
- R1: During schema upgrade, preserve the currently committed view while deferring
  intermediate migration 24–30 view rebuild/drop/comment/grant statements; the atomic
  feature-table cutover publishes the new view once.
- R2: Give admin-only full-history DDL a bounded 15-minute statement timeout and apply
  the same bounded timeout only to the runtime dependency-ordered full-history view
  refresh; all other runtime statements retain the 5-minute bound.
- R3: Document the atomic publication and distinct bounded execution policies.

Acceptance:
- A1 verifies only view-publication SQL in versions 24–30 is deferred and version 31+
  publication is retained; non-view schema/data migrations still run.
- A2 verifies admin/runtime timeout bounds independently.
- A3 passes local `make quality-gate` and the disposable v23-to-current PostgreSQL upgrade.
- A4 requires the authorized production migration, all per-feature tables, both populated
  views, full data sync, replay, and cron acceptance before live completion.

### PR-191: Restore Missing Legacy Availability Before View Rebuild

PR name: `repair-legacy-availability-migration`
Status: Merged
Updated: 2026-10-27
PR: [#191](https://github.com/SergejSchweizer/macro-loader/pull/191) (merged)
Git branch: `pr-191/repair-legacy-availability-migration`
Git status: merged
Agent lane: make the production v23 schema upgrade tolerate ledger/schema drift
Depends on: PR-190 (merged to `origin/main`)
Commit: `b3a778d fix(pr-191): restore legacy availability before view migration` (squash merge)
Design patterns: Versioned Migration, Upgrade Integration Test.

Description:
- R1: Ensure migration 24 creates generated `available_at_utc` and nullable OHLC
  source columns before rebuilding `macro_features`; production v23 is missing both
  groups despite migration history through version 23.
- R2: Preserve the requested end state of one `macro_raw_<feature>` table per registered
  feature, populated before the legacy wide relation is removed.

Acceptance:
- A1 verifies availability and OHLC additions precede the view rebuild.
- A2 upgrades a disposable v23 database with both column groups absent through the
  normal migrator and verifies the full per-feature table set and retired wide table.
- A3 passes local `make quality-gate`; no GitHub Actions.
- A4 resumes production migration, sync, verification, replay, and cron acceptance only
  after A1-A3 pass; all requested feature tables must be present and populated.

### PR-190: Repair OHLC Migration Ordering For Legacy PostgreSQL Schemas

PR name: `repair-legacy-ohlc-migration`
Status: Merged
Updated: 2026-10-27
PR: [#190](https://github.com/SergejSchweizer/macro-loader/pull/190) (merged)
Git branch: `pr-190/repair-legacy-ohlc-migration`
Git status: merged
Agent lane: safely upgrade production v23 schemas before feature-table cutover
Depends on: PR-189 (merged to `origin/main`)
Commit: `ca4e965 fix(pr-190): repair legacy OHLC migration ordering` (squash merge)
Design patterns: Versioned Migration, Upgrade Integration Test.

Description:
- R1: Add missing OHLC columns to the legacy wide relation before migration 24 rebuilds
  `macro_features`, so existing v23 installations can reach the later cutover migration.
- R2: Keep the final target unchanged: every registered raw feature is stored in its own
  `macro_loader.macro_raw_<feature>` table; the wide `macro_raw` relation is only a
  migration source and must be dropped after the feature tables are populated.

Acceptance:
- A1 verifies migration 24 adds every required nullable OHLC field before rebuilding
  the public view.
- A2 upgrades a disposable v23 database with all OHLC columns absent through the regular
  migrator and verifies all per-feature raw tables exist and the wide relation is gone.
- A3 passes `make quality-gate` locally; no GitHub Actions.
- A4 resumes the explicitly authorized live run and verifies all feature tables, both
  views, full data sync, replay, and cron acceptance against production before reporting
  deployment complete.

### PR-189: Bootstrap Empty Series In The Full-Live Acceptance Runner

PR name: `bootstrap-empty-series-in-live-acceptance`
Status: Merged
Updated: 2026-10-27
PR: [#189](https://github.com/SergejSchweizer/macro-loader/pull/189) (merged)
Git branch: `pr-189/bootstrap-empty-series-in-live-acceptance`
Git status: merged
Agent lane: choose lifecycle-correct history operation per series
Depends on: PR-188 (merged to `origin/main`)
Commit: `91e62b1 fix(pr-189): bootstrap empty series in acceptance runs`
Design patterns: Repository, Command Planning Policy.

Description:
- R1: Inspect durable Bronze bounds for every registered series; use `bootstrap` when
  empty and explicit full-history `reconcile` when history exists.
- R2: Keep the full-run migration/sync/verification stages and first-failure report
  semantics unchanged.

Acceptance:
- A1 verifies an empty series gets `bootstrap`, a populated series gets `reconcile`, and
  every registered series appears once in the acceptance plan.
- A2 verifies local quality gate. No GitHub Actions.
- A3 allows resuming full live ingestion; production PostgreSQL migration/sync/replay still
  require valid current database credentials and may not be reported passed before their
  full execution and verification.

### PR-188: Skip Empty Official STOXX Values During Full-History Reconciliation

PR name: `skip-empty-stoxx-source-values`
Status: Merged
Updated: 2026-10-27
PR: [#188](https://github.com/SergejSchweizer/macro-loader/pull/188) (merged)
Git branch: `pr-188/skip-empty-stoxx-source-values`
Git status: merged
Agent lane: ingest documented missing STOXX values without fabricating observations
Depends on: PR-187 (merged to `origin/main`)
Commit: `b3a42cc fix(pr-188): omit blank STOXX source values` (GitHub squash merge)
Design patterns: Adapter, Source Contract Test.

Description:
- R1: Treat blank official STOXX `Indexvalue` cells as absent source observations;
  continue rejecting malformed non-empty values and invalid dates.
- R2: Document source-missing behavior and verify the fix against the live full-history
  file before resuming authorized full-run reconciliation.

Acceptance:
- A1 verifies the historical blank value in official `h_v6i1.txt` is omitted, numeric
  observations are retained, malformed non-empty values remain errors, and unit tests pass.
- A2 verifies docs and the local quality gate. No GitHub Actions.
- A3 verifies all registered source reconciliations, Silver/Gold publication, and all
  PostgreSQL migration/sync/replay checks against disposable or production systems only
  under explicit authorization; production PostgreSQL currently requires credential
  repair before those stages can complete.

### PR-187: Record Availability QA Merge And Verify GOALS Wave Closure

PR name: `record-availability-qa-merge`
Status: Merged
Updated: 2026-10-27
PR: [#187](https://github.com/SergejSchweizer/macro-loader/pull/187) (merged)
Git branch: `pr-187/record-availability-qa-merge`
Git status: merged
Agent lane: keep backlog merge metadata synchronized and audit the requested local scope
Depends on: PR-186 (merged to `origin/main`)
Commit: `158e6e6 docs(pr-187): record availability QA merge and audit goals` (GitHub squash merge)
Design patterns: Architectural baseline only.

Description:
- R1: Record PR-186's confirmed squash merge in this backlog and update current delivery
  metadata. Do not alter implementation behavior or assert production state.
- R2: Audit the tracked implementation against the ignored GOALS.md: migrations 31/32
  provide per-series raw tables, availability, feature-table projection, and the extended
  transformation view; migration/runtime contracts retire Fed-policy ingestion; registry
  feature definitions and PostgreSQL projection cover the specified causal transforms;
  README/ARCHITECTURE document schemas, formulas, windows, and source-availability limits;
  `--cores` defaults to six and computation uses vectorized Polars. Local quality-gate and
  disposable PostgreSQL tests are evidence; production population/cron state remains
  unverified and outside this PR.

Acceptance:
- A1 verifies R1 against GitHub's merged PR #186 and the fetched `origin/main` history.
- A2 verifies GOALS.md implementation scope against migration/catalog code, README,
  ARCHITECTURE, and local quality-gate evidence. No production DB, cron, or GitHub Actions.


### PR-184: Extend PostgreSQL Transformations To All Registered Positive-Level Series

PR name: `extend-transformations-to-registered-series`
Status: Merged
Updated: 2026-10-27
PR: [#184](https://github.com/SergejSchweizer/macro-loader/pull/184) (merged)
Git branch: `pr-184/extend-transformations-to-registered-series`
Git status: merged
Agent lane: complete point-in-time feature projection for every applicable registered source
Depends on: PR-183 (merged to `origin/main`)
Commit: `9d82a67 feat(pr-184): extend transformations to registered series` (GitHub squash merge)
Design patterns: Registry-driven Projection, Materialized View, Versioned Migration, Independent Reference.

Description:
- R1: Add the existing catalog-defined causal price/momentum transformations for the 12
  registered positive-level series not currently projected: `vstoxx1m`, `vstoxx2m`,
  `vstoxx3m`, `ciss_money`, `ciss_bonds`, `ciss_equity`, `ciss_financials`,
  `euro_hy_yield`, `euro_hy_ytw`, `euro_hy_total_return`, `us_3m`, and `us_5y`. Keep
  signed `estr` excluded from positive-level transforms and retain all 25 raw tables.
- R2: Preserve the existing 954-column `macro_features` contract. The full catalog would
  contain 1,746 columns and exceed PostgreSQL's per-relation limit, so publish the new
  series' 792 applicable catalog outputs in `macro_features_extended`, keyed by the same
  `timestamp_m1` and carrying the same `available_at_utc`. Do not remove existing feature
  families to evade the limit.
- R3: Add migration 32 with a private shared raw-key projection and private
  materialized transformations for the 12 added series, then assemble the companion view
  from those relations. Leave the existing `macro_features` materialized view untouched,
  create/grant the companion view, and refresh the core view, raw projection, added-series
  transforms, and companion view in dependency order. Bind both public column/type
  contracts and fingerprints to the registry catalog.
- R4: Document the 24-source two-view consumer contract, the PostgreSQL column-limit
  rationale, formulas, and null/warm-up behavior. No production migration or cron run.

Acceptance:
- A1 verifies R1: catalog coverage matches exactly the 12 listed added series; every
  applicable existing transformation family is generated for each, `estr` receives no
  positive-level output, and all 25 raw tables remain unchanged.
- A2 verifies R2: the existing `macro_features` ordered names/types/fingerprint remain
  unchanged; `macro_features_extended` has exactly 794 columns (timestamp, availability,
  and 792 feature outputs), no duplicate names, and stays below PostgreSQL's 1,600-column
  relation limit.
- A3 verifies R3: migration 32 is transactional and replay-safe; private materializations
  are not granted to consumer roles; both public views refresh together; the existing
  `macro_features` relation, public schema, and fingerprint remain unchanged; role grants
  are correct; and the conformance verifier rejects missing, extra, reordered, or
  wrong-typed extended columns. Disposable PostgreSQL upgrade/clean-create tests run when
  `POSTGRES_TEST_DSN` is configured.
- A4 verifies causal/warm-up/null behavior with hand-calculable references and
  truncation/invalid-input checks; README and ARCHITECTURE match the executable contract;
  local `make quality-gate` passes. No production DB, cron, GitHub Actions, or remote gate.

### PR-185: Bound PostgreSQL Feature-View Refresh Compilation And Restore Migration QA

PR name: `bound-postgres-feature-view-refresh`
Status: Merged
Updated: 2026-10-27
PR: [#185](https://github.com/SergejSchweizer/macro-loader/pull/185) (merged)
Git branch: `pr-185/bound-postgres-feature-view-refresh`
Git status: merged
Agent lane: bounded PostgreSQL materialized-view execution and end-to-end migration regression
Depends on: PR-184 (merged to `origin/main`)
Commit: `36f2e6d fix(pr-185): bound PostgreSQL feature-view refresh` (GitHub squash merge)
Design patterns: Adapter Configuration, Bounded Execution Policy, Independent Reference.

Description:
- R1: Disable PostgreSQL JIT for loader-controlled migration/sync sessions and the
  explicit materialized-view refresh routine. The feature-view query has recursive EMA
  and Wilder-RSI CTEs; PostgreSQL estimates an enormous plan cost and JIT compilation
  dominates the actual refresh. Keep connection, lock, and statement timeout bounds.
- R2: Repair the migration-25 integration fixture so it builds its historical v14 view
  from the legacy wide raw table and its frozen legacy column contract, rather than
  accidentally using the current feature-table projection.
- R3: Preserve public names, ordered schemas, formulas, availability semantics, and
  existing `macro_features` relation identity. Do not change production data, cron, or
  run GitHub Actions.

Acceptance:
- A1 verifies R1: session configuration and the security-definer refresh function
  disable JIT; the read-only PostgreSQL conformance inspector detects drift; a disposable
  single-row complete refresh stays within the configured 300-second statement bound.
- A2 verifies R2: clean-create and migration-25-to-current PostgreSQL tests pass, including
  historical value backfill, generated `available_at_utc`, all 25 feature-table schemas,
  removal of wide `macro_raw`, and both public view contracts.
- A3 verifies R3: core-view relation identity and both ordered catalogs/fingerprints stay
  unchanged; README/ARCHITECTURE document the bounded execution setting; local
  `make quality-gate` passes. No production DB, cron, GitHub Actions, or remote gate.

### PR-186: Use The Managed Refresh Path In Availability Integration QA

PR name: `use-explicit-postgres-refresh-in-availability-qa`
Status: Merged
Updated: 2026-10-27
PR: [#186](https://github.com/SergejSchweizer/macro-loader/pull/186) (merged)
Git branch: `pr-186/use-explicit-postgres-refresh-in-availability-qa`
Git status: merged
Agent lane: validate generated availability through the published PostgreSQL refresh boundary
Depends on: PR-185 (merged to `origin/main`)
Commit: `462bb94 test(pr-186): use managed refresh in availability QA` (GitHub squash merge)
Design patterns: Command Boundary, Integration Contract Test.

Description:
- R1: Update the generated `available_at_utc` PostgreSQL integration test to invoke
  `macro_loader.refresh_macro_features_explicit()` instead of directly refreshing only
  the core materialized view. This exercises the supported dependency-ordered refresh,
  including the companion view and its JIT execution setting.
- R2: Preserve assertions for weekday/weekend observation timestamps, raw-table generated
  availability, and the matching `macro_features` availability values. No production
  data, cron, or GitHub Actions.

Acceptance:
- A1 verifies R1/R2: the disposable PostgreSQL availability test passes using the
  explicit refresh function and checks all five weekday/weekend cases in raw and public
  view values.
- A2 verifies the local `make quality-gate`; no production database, cron, GitHub Actions,
  or remote quality gate is used.

### PR-183: Update PostgreSQL EMA/MACD Parity QA For Feature Tables

PR name: `update-feature-table-parity-qa`
Status: Merged
Updated: 2026-10-27
PR: [#183](https://github.com/SergejSchweizer/macro-loader/pull/183) (merged)
Git branch: `pr-183/update-feature-table-parity-qa`
Git status: merged
Agent lane: PostgreSQL serving transformation parity QA after raw-table cutover
Depends on: PR-182 (merged to `origin/main`)
Commit: `716b33d test(pr-183): update ema macd parity for feature tables` (GitHub squash merge)
Design patterns: Independent Reference, Migration/Schema Contract Testing.

Description:
- R1: Update the EMA PostgreSQL migration and parity tests to operate on the current
  feature-specific raw tables, cover all current eligible price series, and validate the
  latest view contract after the historical migration-22 upgrade and clean-create paths.
- R2: Update the MACD PostgreSQL migration and parity tests to operate on the current
  feature-specific raw tables, cover all current eligible price series, and validate the
  latest view contract after the historical migration-23 upgrade and clean-create paths.
- R3: Remove current-schema QA reads/writes against retired wide `macro_raw` while
  retaining wide-table fixtures only where a test intentionally constructs a pre-cutover
  migration baseline.

Acceptance:
- A1 verifies R1: independent EMA reference outputs, reset/crossover/causality cases,
  current table layout, current view version/fingerprint, and migration upgrade/replay pass
  against a disposable PostgreSQL database.
- A2 verifies R2: independent MACD reference outputs across all eligible series, no helper
  leakage, current table layout, and migration upgrade/replay pass against disposable
  PostgreSQL.
- A3 verifies R3 by source-level assertions/review and local `make quality-gate`; no
  production database, cron job, or GitHub Actions workflow is used.

### PR-182: Provision PostgreSQL Access For Feature-Specific Raw Tables

PR name: `provision-feature-specific-postgres-grants`
Status: Merged
Updated: 2026-10-27
PR: [#182](https://github.com/SergejSchweizer/macro-loader/pull/182) (merged)
Git branch: `pr-182/provision-feature-specific-postgres-grants`
Git status: merged
Agent lane: PostgreSQL least-privilege role provisioning after raw-table cutover
Depends on: PR-181 (merged to `origin/main`)
Commit: `4a428cb fix(pr-182): grant roles on feature raw tables` (GitHub squash merge)
Design patterns: Registry-driven Configuration, Least-Privilege Policy.

Description:
- R1: Generate table ownership and grants from the authoritative
  `POSTGRES_RAW_TABLES` registry: `macro-loader` receives SELECT only, while
  `macro-loader-sync` receives the DML required for transactional Gold sync. Retain
  the existing sync-support-table grants and the separate `macro_features` view grant.
- R2: Remove the retired wide `macro_loader.macro_raw` relation from the provisioner's
  current ownership/grant contract; do not create, grant, or require it.
- R3: Document the runtime-reader, sync-writer, and owner role boundaries against the
  25 feature-specific tables.

Acceptance:
- A1 verifies R1/R2: unit tests prove all 25 registered feature tables are owned and
  granted correctly, no wide `macro_raw` table is configured, and unrelated privileges
  remain least-privilege.
- A2 verifies R1: disposable PostgreSQL QA proves reader SELECT-only and sync DML access
  for every feature table, plus the existing sync support/view permissions.
- A3 verifies R3 and local `make quality-gate`; no production role or database is changed.

### PR-176: Add OHLC Stochastic And Donchian Features

PR name: `add-stochastic-donchian-features`
Status: Merged
Updated: 2026-10-27
PR: [#176](https://github.com/SergejSchweizer/macro-loader/pull/176) (merged)
Git branch: `pr-176/add-stochastic-donchian-features`
Git status: merged
Agent lane: PostgreSQL OHLC feature projection and independent mathematical/schema QA
Depends on: PR-175 (merged to `origin/main`)
Commit: `c3da4cf feat(pr-176): add OHLC stochastic and Donchian features` (GitHub squash merge)
Design patterns: Registry-driven Projection, Pure Transformation, Materialized View.

Description:
- R1: Add the six stochastic columns and two Donchian columns for each registered
  native-OHLC source (`vix`, `vix9d`, `vix3m`, `vix6m`, `vix1y`, `move`) to the
  ordered, closed-world `macro_features` projection. Do not add helper/intermediate
  fields or put derived values in `macro_raw` or canonical Gold.
- R2: Compute stochastic `%K` over complete finite source bars at windows 5 and 14;
  compute `%D` as a three-observation mean; expose the `%K5 - %D5` spread and the
  three-observation `%K5` slope. Null incomplete/invalid/zero-range results. Donchian
  compares the current high/low strictly against the prior 20 valid source bars,
  excluding the current bar, and is NULL until that complete prior window exists.
- R3: Preserve observation-based causality across sparse dates, nullable BOOLEAN types
  for Donchian, deterministic feature order, and the PostgreSQL migration/version/hash
  contract.
- R4: Add independent hand-calculable QA and PostgreSQL clean-create/upgrade parity
  tests; do not write to production PostgreSQL, run cron, or invoke GitHub Actions.
- R5: Document formulas, source-observation windows, null/warm-up behavior, and the
  serving-only scope in README and ARCHITECTURE.

Acceptance:
- A1 verifies R1: exactly eight columns per OHLC series, in catalog order, with no
  outputs for scalar series and no helper fields in any public relation.
- A2 verifies R2: reference fixtures cover each formula, zero-range and invalid bars,
  complete-window warm-up, comparison strictness, and NULL propagation.
- A3 verifies R3: sparse-date and truncation/causality tests pass; PostgreSQL schema
  checks verify ordered names/types, view contract version/fingerprint, and migration
  replay on a disposable database.
- A4 verifies R4/R5: PostgreSQL integration QA is run when `POSTGRES_TEST_DSN` is
  available; local `make quality-gate` passes; docs match implementation. No production
  database writes, cron execution, GitHub Actions, or remote quality gates.

### PR-177: Add VSTOXX 1M/2M/3M Source Series

PR name: `add-vstoxx-term-structure-series`
Status: Merged
Updated: 2026-10-27
PR: [#177](https://github.com/SergejSchweizer/macro-loader/pull/177) (merged)
Git branch: `pr-177/add-vstoxx-term-structure-series`
Git status: merged
Agent lane: STOXX source adapter and provider-level QA
Depends on: PR-176
Commit: `5f03547 feat(pr-177): add VSTOXX 1M/2M/3M source series` (GitHub squash merge)
Design patterns: Adapter, Registry/Factory, Pure Parser.

Description:
- R1: Register `vstoxx1m`, `vstoxx2m`, and `vstoxx3m` as daily STOXX scalar series
  using symbols `V6I1`, `V6I2`, and `V6I3`, respectively. Parse `Date;Symbol;Indexvalue`
  source rows through the existing STOXX adapter boundary, preserving provider identity,
  source URL, observation date, and fetched time.
- R2: Support maximum exposed history on bootstrap and exact bounded logical filtering
  on normal updates. The official history files were checked at `h_v6i1.txt`,
  `h_v6i2.txt`, and `h_v6i3.txt`; each returned daily observations beginning 1999-01-04
  and latest through 2026-10-26 at verification time. Do not claim earlier history.
- R3: Add deterministic offline parser/request tests and source metadata for all three
  contracts; do not introduce credentials, fallback sources, or fabricated dates.

Acceptance:
- A1 verifies R1: registry contracts and parsed canonical frames identify the exact
  symbols/series and contain finite scalar `value` observations with the correct source
  metadata.
- A2 verifies R2: bootstrap preserves the exposed full file, bounded updates return only
  the exact requested interval, and rows outside that interval cannot alter persistence.
- A3 verifies R3: provider tests are network-free, source catalog tests assert daily
  frequency and start-date evidence, and local quality-gate passes.

### PR-178: Add Daily ECB CISS Component Series

PR name: `add-ecb-ciss-components`
Status: Merged
Updated: 2026-10-27
PR: [#178](https://github.com/SergejSchweizer/macro-loader/pull/178) (merged)
Git branch: `pr-178/add-ecb-ciss-components`
Git status: merged
Agent lane: ECB series registration and independent provider QA
Depends on: PR-177
Commit: `56d5c04 feat(pr-178): add daily ecb ciss component series` (GitHub squash merge)
Design patterns: Adapter, Registry/Factory, Pure Parser.

Description:
- R1: Register daily Euro-area series `ciss_money`, `ciss_bonds`, `ciss_equity`, and
  `ciss_financials` from ECB New-CISS component-contribution keys
  `CISS.D.U2.Z0Z.4F.EC.SS_MMN.CON`, `...SS_BMN.CON`, `...SS_EMN.CON`, and `...SS_FIN.CON`.
  Keep the documented payload semantics as contributions (not mislabeled standalone
  subindex levels); the downstream raw scalar column remains `indexvalue`.
- R2: Preserve daily observations from the source's first available date (verified as
  1999-01-01 in the ECB catalog) through the latest source date, with no forward-fill or
  fabricated values; normal updates use the established bounded date-range policy.
- R3: Update registry/source documentation and add offline parser, request-bound, missing
  value, duplicate-date, and provider-routing tests.
- R4: Publish the four new Gold scalar columns under Gold schema version 9 and migrate
  existing PostgreSQL installations additively for all seven source columns introduced
  by PR-177 and PR-178; never rewrite prior migration entries or discard retained rows.

Acceptance:
- A1 verifies R1: all four exact ECB series keys map to distinct canonical IDs and scalar
  values; metadata identifies their contribution semantics and daily frequency.
- A2 verifies R2: bootstrap retains all source observations and sparse/missing dates stay
  absent; updates pass exact bounds to the ECB adapter.
- A3 verifies R3: deterministic offline tests pass and README source metadata is updated
  with the verified start period and current public-source limits.
- A4 verifies R4: the 8→9 sync-state upgrade is explicitly allowed, migration 28 adds
  the seven nullable scalar columns without dropping data, clean and upgraded physical
  schemas match the declared column contract, and the full local quality gate passes.

### PR-179: Add Euro High-Yield Yield And Total-Return Series

PR name: `add-euro-high-yield-series`
Status: Merged
Updated: 2026-10-27
PR: [#179](https://github.com/SergejSchweizer/macro-loader/pull/179) (merged)
Git branch: `pr-179/add-euro-high-yield-series`
Git status: merged
Agent lane: FRED credit-series registration and source-history QA
Depends on: PR-178
Commit: `8119b01 feat(pr-179): add euro high-yield series` (GitHub squash merge)
Design patterns: Adapter, Registry/Factory, Pure Parser.

Description:
- R1: Add daily FRED scalar series `euro_hy_yield` (`BAMLHE00EHYIEY`),
  `euro_hy_ytw` (`BAMLHE00EHYISYTW`), and `euro_hy_total_return`
  (`BAMLHE00EHYITRIV`), preserving their published units and distinct meanings.
- R1: Reuse the already registered `euro_hy_oas` observations from the existing wide
  `macro_raw` relation; do not download or independently recalculate OAS a second time.
- R2: Bootstrap the full history currently exposed by FRED; at verification time all
  three began 2023-10-27 because ICE BofA series on FRED are limited to a rolling three
  years. Preserve any older already-durable local observations during bounded updates;
  never infer deletion from source truncation.
- R3: Document the FRED identifiers, daily frequency, units, source restrictions, and
  available date range; add offline parsing and exact delta-bound tests.
- R4: Add the new source columns to Gold schema version 10 and append a PostgreSQL
  additive migration 29 for those columns; preserve migration 28 and the retained
  `euro_hy_oas` history.

Acceptance:
- A1 verifies R1: each FRED ID has its own daily scalar contract and values are not
  mixed across yield, yield-to-worst, and total-return index units; OAS continues to
  come from its existing `macro_raw` series with no duplicate FRED registration.
- A2 verifies R2: bootstrap and update tests prove exposed-history and bounded-delta
  behavior without deleting retained older rows.
- A3 verifies R3: local provider/catalog tests and README metadata describe the current
  2023-10-27 source limit without promising full history.
- A4 verifies R4: sync state accepts schema upgrade 9→10, migration 29 adds only the three
  new nullable scalar columns and preserves prior rows, and local quality-gate passes.

### PR-180: Add U.S. Three-Month And Five-Year Treasury Series

PR name: `add-us-treasury-3m-5y-series`
Status: Merged
Updated: 2026-10-27
PR: [#180](https://github.com/SergejSchweizer/macro-loader/pull/180) (merged)
Git branch: `pr-180/add-us-treasury-3m-5y-series`
Git status: merged
Agent lane: FRED Treasury registration and provider QA
Depends on: PR-179
Commit: `40cbed7 feat(pr-180): add treasury 3m and 5y series` (GitHub squash merge)
Design patterns: Registry/Factory, Pure Parser.

Description:
- R1: Add daily scalar FRED series `us_3m` (`DGS3MO`) and `us_5y` (`DGS5`) in percent
  units, keeping the existing `us_2y` only once and retaining `us_10y` unchanged.
- R2: Bootstrap maximum exposed history: `DGS3MO` from 1981-09-01 through 2026-10-22
  and `DGS5` from 1962-01-02 through 2026-10-23 as checked on 2026-10-27. Existing
  FRED bounded-update and durable-history retention rules continue to apply.
- R3: Update source metadata and offline registry/request-bound tests.
- R4: Add the two new Gold scalar columns under schema version 11 and append PostgreSQL
  migration 30; preserve migration 29 and all previously retained rows.

Acceptance:
- A1 verifies R1: exact IDs, daily frequency, percent units, and no duplicate `us_2y`
  registration.
- A2 verifies R2: documented and tested earliest available dates; no source omission
  deletes durable history.
- A3 verifies R3: local quality-gate passes with no network tests in required checks.
- A4 verifies R4: schema upgrade 10→11 is allowed, migration 30 adds exactly two nullable
  scalar columns without destructive changes, and the local quality gate passes.

### PR-181: Cut Over To Per-Feature PostgreSQL Raw Tables

PR name: `publish-feature-specific-raw-tables`
Status: Merged
Updated: 2026-10-27
PR: [#181](https://github.com/SergejSchweizer/macro-loader/pull/181) (merged)
Git branch: `pr-181/publish-feature-specific-raw-tables`
Git status: merged
Agent lane: PostgreSQL raw-table projection, migration, backfill, and schema QA
Depends on: PR-180
Commit: `7780cb3 feat(pr-181): split postgres raw data by feature` (GitHub squash merge)
Design patterns: Registry-driven Projection, Repository, Unit of Work, Versioned Migration.

Description:
- R1: Create and populate `macro_loader.macro_raw_<feature>` tables for each unique
  requested raw feature after source registration. The requested universe is
  `vix`, `vix9d`, `vix3m`, `vix6m`, `vix1y`, `vstoxx`, `vstoxx1m`, `vstoxx2m`,
  `vstoxx3m`, `move`, `ciss`, `ciss_money`, `ciss_bonds`, `ciss_equity`, `ciss_financials`, `estr`,
  `euro_hy_oas`, `euro_hy_yield`, `euro_hy_ytw`, `euro_hy_total_return`, `us_3m`,
  `us_2y`, `us_5y`, `us_10y`, and `usd_broad`; the repeated `us_2y` is intentionally
  deduplicated per the user's clarification. Retain the already registered `vstoxx`
  series in its own table as well; do not discard its current raw history as an implicit
  side effect of adding the three term-structure series.
- R2: Backfill each feature table from the existing wide `macro_raw` relation exactly
  once, including every existing Euro-HY observation (`euro_hy_oas` today). Thereafter,
  write source/Gold syncs directly to the feature-specific tables; do not continue
  storing new values in the wide relation.
- R3: Each table has the same primary temporal type and `available_at_utc` calculation:
  `timestamp_m1 TIMESTAMPTZ(6)` plus generated `available_at_utc TIMESTAMPTZ(6)`; the
  six OHLC features have nullable `open/high/low/close DOUBLE PRECISION`, and every
  scalar feature has nullable `indexvalue DOUBLE PRECISION`. Maintain the tables
  transactionally on every source/Gold sync, revision, deletion, and migration replay;
  `macro_features` must be sourced from these feature tables.
- R4: In the same cutover migration, remove the wide `macro_raw` relation after
  backfill, switch all readers/writers to the feature tables, and ensure no runtime path
  recreates or writes to it.
- R5: README lists every feature's source/provider identifier, columns, frequency,
  earliest available date, current history limitation, and a concise data description.

Acceptance:
- A1 verifies R1: exactly one relation per requested unique feature and retained
  registered series exists in `macro_loader` (25 tables for the catalog in this wave),
  and no duplicate table is created for `us_2y`.
- A2 verifies R2: historical rows, including existing Euro-HY observations, are
  backfilled exactly and only once; row counts and values match the pre-migration source.
- A3 verifies R3: disposable PostgreSQL clean-create/upgrade, insert, revise, delete,
  replay, timestamp-type, availability, column/type/order, and feature-table parity tests
  pass; transaction failures leave all feature tables consistent.
- A4 verifies R4: after migration `macro_raw` is absent, feature-table writes and view
  refresh work, and static/runtime checks find no path that writes or recreates it.
- A5 verifies R5: README metadata matches the registered/provider contracts and
  observed source-date limits; local `make quality-gate` passes.

## Current repository and production status

PR-176 through PR-191 are merged; PR-192 is in progress. No production migration or
installed-crontab change is asserted here. Historical Fed-policy acceptance artifacts
remain audit records; the independent FRED provider remains active for its ordinary macro
series.

The active serving lineage is:

```text
canonical market and macro source levels
        -> canonical Gold/source serving contract
        -> macro_loader.macro_raw_<feature> tables
        -> macro_loader.macro_features
```

The PostgreSQL serving relations expose the complete available feature-table history
through the latest completed EOD source date. The retired wide `macro_raw` relation is
used only as a one-time migration source and is absent after PR-181. Values may remain NULL only where
point-in-time source support is genuinely unavailable or a transformation has not
completed its causal warm-up; every such gap must be measured and explained by QA rather
than filled, interpolated, carried, or synthesized.

## Transformation scope (summary)

This wave adopts the calculation semantics from the current
[`xetra-loader` feature catalog](https://github.com/SergejSchweizer/xetra-loader/blob/main/src/xetra_loader/features/catalog.py)
and its PostgreSQL view implementation. The source-specific `adjusted_close` concept is
mapped to each eligible positive `macro_raw` level series; no synthetic adjusted-close
column is introduced.

Market-wide `breadth`, cross-instrument `dispersion`, and volume transformations are
explicitly **out of scope** per the user's scope decision. The repository has no instrument
cross-section (`isin`, venue, instrument, trade-date) or applicable `volume_level` source.
Do not synthesize these inputs from heterogeneous macro indicators or index quote fields.

The XETRA-compatible geometric-return columns retain the `_pct` name but store decimal
fractions (`0.05` means 5%), exactly as the XETRA SQL example computes them. Average
returns mean the average one-observation **log returns**, not average simple returns.

The final replacement set per eligible source level is exactly 26 columns:

```text
log_return:       1, 3, 5, 10, 20 observations
return_geom_pct:  5, 10, 20 observations; decimal fractions despite the _pct suffix
return_mean:      5, 10, 20 observations; mean of one-observation log returns
volatility:       5, 10, 20, 40 observations; sample standard deviation of log returns
sma_ratio:        5/20, 10/20, 10/40
wilder_rsi:       7, 14 observations
roc:              3, 5, 10, 20 observations
drawdown:         20, 60 observations
```

The existing geometric-return implementation is replaced. The `10obs` column name is
retained with the new XETRA-compatible 10-observation semantics; the obsolete
`25/60/120/240obs` columns and their old values are removed. No breadth, dispersion, or
volume column is added.

## Delivery Policy

These rules apply to the active GOALS implementation wave above. The compact archive at
the end is historical only.

- One `PR-XX` entry equals one logical implementation pull request.
- Keep each PR small and atomic: one infrastructure boundary, provider family,
  transformation boundary, publication concern, or operational concern.
- Every PR has `Status`, `Updated`, `PR`, `Git branch`, `Git status`, `Agent lane`, `Depends on`, `Commit`, and `Design patterns`.
- Delivery statuses: `Planned`, `In Progress`, `Blocked`, `Ready`, `Merged`.
- Git statuses: `not-started (branch absent)`, `active-clean`, `active-dirty: <paths>`, `pushed-local-checks-passed`, `merged`.
- Every `Description` requirement `R<n>` has exactly one matching `Acceptance` item `A<n>`. Counts must match.
- Implement only the selected PR. Do not pull future-PR scope forward.
- Run checks locally with `make quality-gate`; it includes lint, type, unit, offline integration, and coverage at the 85% threshold configured in `pyproject.toml`.
- Do not add, trigger, rerun, or rely on GitHub Actions or remote quality gates. Network tests are not part of the local quality gate.
- Production dataframe operations are Polars-first; no production pandas dependency.
- Runtime `lake/` is ignored by Git.
- `README.md`, `ARCHITECTURE.md`, `AGENTS.md`, and this backlog must not intentionally contradict one another.

## Git Workflow Contract

The active PR specs are listed at the beginning of this backlog.

Every implementation PR uses:

```text
Git branch: pr-XX/<kebab-case-description>
Commit:     type(pr-XX): <lowercase imperative description>
```

Allowed Conventional Commit types:

```text
feat fix docs test refactor perf build ci chore
```

Rules:

- Branch and commit scope contain the same `pr-XX` as the backlog entry.
- Every non-generated commit subject uses Conventional Commit format exactly `type(pr-XX): <description>` with an allowed type and the same PR identifier as the branch.
- Branch from dependency-complete `main` only after every `Depends on` PR is merged.
- Before every commit, verify the active branch is exactly the `Git branch` declared by the backlog PR.
- Before push: `make quality-gate` passes locally and the worktree is clean.
- After push: use `pushed-local-checks-passed` only when the branch was pushed after the local gate passed; this is not a remote status check.
- Do not configure or use GitHub Actions, remote quality gates, or auto-merge.
- After merge: update backlog status/PR link/Git status in the next documentation-maintenance change; do not keep an implementation task alive to start another PR.
- No force-push on shared branches, branch reuse, or guessed semantic conflict resolution.

## Local Quality Gate

Run only the repository's local gate:

```text
make quality-gate
```

The gate runs lint, type, unit, offline integration, and coverage; the production line
coverage threshold is 85%, configured in `pyproject.toml`. Do not add GitHub Actions or
remote gates, and do not describe local results as remote GitHub checks.

## Mandatory Design Patterns

Use patterns whenever they materially reduce coupling, clarify lifecycle/ownership, improve substitution in tests, or protect transaction boundaries. Do not introduce a pattern only to satisfy a label; prefer the simplest implementation that satisfies the contract. Prefer composition/`typing.Protocol` over inheritance.

Every PR declares `Design patterns:` explicitly. If no additional pattern is justified beyond the repository architecture, use `Architectural baseline only` rather than inventing one.

- **Ports and Adapters / Hexagonal Architecture** — `application` owns contracts/use cases; `ingestion` implements provider/filesystem adapters.
- **Adapter** — CBOE/STOXX/Yahoo/ECB/FRED and physical persistence implementations.
- **Strategy** — retry policy, update/reconcile planning policy, consumer resolution policy.
- **Registry/Factory** — canonical series/provider adapter routing; orchestration must not use provider `if/elif` ladders.
- **Repository** — Bronze, Silver, state, run manifest, inventory, Gold build and Gold catalog persistence.
- **Unit of Work** — one-series Bronze durability boundary and Gold catalog promotion boundary.
- **State Machine** — Gold publication `building -> complete|failed`; catalog alone owns publication status.
- **Materialized View** — root `manifest.json` and `feature_profile.png` are rebuildable views of authoritative `manifest.parquet`.
- **Mark-and-Sweep** — retention tombstones a build in the catalog before physical deletion.
- **Command** — CLI adapters parse/call/render; no provider/persistence business logic.
- **Dependency Injection** — clock, sleeper, HTTP client, repositories, provider registry, source-control identity, and policies are injected.
- **Specification/Policy Object** — governance validators encode repository/backlog invariants as executable rules rather than prose-only conventions.

## Initial Series Catalog

| Canonical ID | Provider | Source | Shape | Capability | Bootstrap |
|---|---|---|---|---|---|
| `vix` | CBOE | `VIX_History.csv` | `ohlc` | `full_file` | maximum exposed history |
| `vix9d` | CBOE | `VIX9D_History.csv` | `ohlc` | `full_file` | maximum exposed history |
| `vix3m` | CBOE | `VIX3M_History.csv` | `ohlc` | `full_file` | maximum exposed history when available |
| `vix6m` | CBOE | `VIX6M_History.csv` | `ohlc` | `full_file` | maximum exposed history when available |
| `vix1y` | CBOE | `VIX1Y_History.csv` | `ohlc` | `full_file` | maximum exposed history when available |
| `vstoxx` | STOXX | `V2TX` | `scalar` | `full_file` | maximum exposed history |
| `vstoxx1m` | STOXX | `V6I1` | `scalar` | `full_file` | maximum exposed history |
| `vstoxx2m` | STOXX | `V6I2` | `scalar` | `full_file` | maximum exposed history |
| `vstoxx3m` | STOXX | `V6I3` | `scalar` | `full_file` | maximum exposed history |
| `move` | Yahoo Finance | `^MOVE` | `ohlc` | `date_range` | maximum available history |
| `ciss` | ECB | `CISS.D.U2.Z0Z.4F.EC.SS_CIN.IDX` | `scalar` | `date_range` | maximum exposed history |
| `ciss_money` | ECB | `CISS.D.U2.Z0Z.4F.EC.SS_MMN.CON` | `scalar` | `date_range` | maximum exposed history |
| `ciss_bonds` | ECB | `CISS.D.U2.Z0Z.4F.EC.SS_BMN.CON` | `scalar` | `date_range` | maximum exposed history |
| `ciss_equity` | ECB | `CISS.D.U2.Z0Z.4F.EC.SS_EMN.CON` | `scalar` | `date_range` | maximum exposed history |
| `ciss_financials` | ECB | `CISS.D.U2.Z0Z.4F.EC.SS_FIN.CON` | `scalar` | `date_range` | maximum exposed history |
| `estr` | ECB | `EST.B.EU000A2X2A25.WT` | `scalar` | `date_range` | maximum exposed history |
| `euro_hy_oas` | FRED | `BAMLHE00EHYIOAS` | `scalar` | `date_range` | maximum currently exposed history; preserve older local history |
| `euro_hy_yield` | FRED | `BAMLHE00EHYIEY` | `scalar` | `date_range` | rolling three years since 2026-04; preserve older local history |
| `euro_hy_ytw` | FRED | `BAMLHE00EHYISYTW` | `scalar` | `date_range` | rolling three years since 2026-04; preserve older local history |
| `euro_hy_total_return` | FRED | `BAMLHE00EHYITRIV` | `scalar` | `date_range` | rolling three years since 2026-04; preserve older local history |
| `us_2y` | FRED | `DGS2` | `scalar` | `date_range` | maximum exposed history |
| `us_10y` | FRED | `DGS10` | `scalar` | `date_range` | maximum exposed history |
| `usd_broad` | FRED | `DTWEXBGS` | `scalar` | `date_range` | maximum exposed history |
| `us_3m` | FRED | `DGS3MO` | `scalar` | `date_range` | 1981-09-01 through 2026-10-22 when checked; maximum history |
| `us_5y` | FRED | `DGS5` | `scalar` | `date_range` | 1962-01-02 through 2026-10-23 when checked; maximum history |

Additional source series in this implementation wave are limited to those explicitly
listed in PR-177 through PR-180; no implicit provider fallback is allowed.

## Medallion Storage Contract
The storage contract below remains active.
```text
lake/
  bronze/
    provider=<provider>/series=<series_id>/year=<YYYY>/month=<MM>/data.parquet
  silver/
    series=<series_id>/year=<YYYY>/month=<MM>/data.parquet
  gold/
    dataset=macro_features_daily/
      versions/build_id=<YYYYMMDDTHHMMSSZ>/
        data.parquet
        manifest.json
        feature_profile.png
      manifest.parquet
      manifest.json
      feature_profile.png
  state/
    ingestion_state.parquet
  manifests/
    ingestion_runs.parquet
    dataset_inventory.parquet
```

### Bronze common schema

```text
series_id: String
provider: String
observation_date: Date
fetched_at_utc: Datetime(time_zone="UTC")
source_id: String
source_url: String
```

Payload is exactly OHLC (`open/high/low/close`) or scalar (`value`). Natural key `(provider, series_id, observation_date)`.

### Silver schema

```text
observation_date: Date
series_id: String
value: Float64
open: Float64 nullable
high: Float64 nullable
low: Float64 nullable
close: Float64 nullable
unit: String
provider: String
source_id: String
fetched_at_utc: Datetime(time_zone="UTC")
```

Natural key `(series_id, observation_date)`. OHLC uses `value=close`; scalar uses `value` and null OHLC.

### Gold timestamp

```text
timestamp_m1: Datetime(time_unit="us", time_zone="UTC")
```

First column, unique, strictly increasing, UTC midnight. It is observation-day identity, **not** provider publication/availability time. Gold contains no `observation_date`.

### Gold feature math

```text
delta_Nobs(t) = x(t) - x(previous Nth valid observation)
```

`zscore_60obs` uses last 60 valid observations including current and `ddof=0`; null before 60 observations or at zero variance. Cross-series features require same timestamp. No forward/back fill, interpolation, centered window, future data, or implicit as-of carry. Final Gold normalizes NaN to null and rejects infinity.

Each canonical source series also exposes positive momentum autocorrelation at `(lag, window)` pairs `(1, 60)`, `(5, 60)`, and `(20, 120)`, calculated from causal one-observation source-unit changes. Negative correlations are clipped to zero and unavailable warm-up windows remain null.

Each canonical source series also exposes rolling geometric-mean simple returns over 10, 25, 60, 120, and 240 observations, expressed as percentages and left null for invalid/non-positive level transitions or incomplete windows.

### Gold semantic versions

```text
schema_version  = 6
feature_version = 5
```

Schema version changes for column name/order/type changes; feature version changes for formula/parameter semantics without schema change. Runtime never auto-increments.

### Gold catalog schema

Authoritative `lake/gold/dataset=macro_features_daily/manifest.parquet` fields:

```text
dataset_id
build_id
status                  # building | complete | failed
current
started_at_utc
completed_at_utc
schema_version
feature_version
min_timestamp
max_timestamp
row_count
data_path
build_manifest_path
plot_path
pruned_at_utc
```

Root JSON/PNG are materialized views, not authority.

## Strict Delta Update Contract
The update contract below remains active.
The ingestion modes are explicit:

```text
bootstrap
update
reconcile
```

### Bootstrap

If authoritative Bronze contains no observation for the selected series, request maximum public history exposed by the configured provider.

### Normal `update` / `run-daily`

If Bronze exists, determine the delta window from **the newest durable Bronze observation**, never from the oldest retained observation:

```text
latest_stored_date = max(Bronze.observation_date)
request_start      = latest_stored_date - overlap_days
request_end        = injected_today
```

Default `overlap_days = 7` calendar days. The overlap exists only to catch recent equal-key revisions.

Mandatory invariants:

1. `latest_stored_date` is derived from authoritative Bronze (state may cache it but must not override Bronze truth).
2. Normal `update` and `run-daily` never choose `min(Bronze.observation_date)` as request start.
3. Normal `update` and `run-daily` never automatically switch to full-history `reconcile`.
4. If `request_end < latest_stored_date`, fail rather than fabricate a reverse/empty history state.
5. For `date_range` providers, send the exact bounded interval `[request_start, request_end]`; do not silently broaden it.
6. For `full_file` providers, a complete remote object may have to be downloaded because the upstream source has no bounded-history capability, but before logical diff/persistence filter accepted observations to `[request_start, request_end]` during normal update.
7. A normal update must rewrite only monthly partitions containing inserted/revised rows inside the logical delta window.
8. Provider rows outside the requested delta scope must not expand normal update semantics: bounded-provider out-of-window data is a contract error; full-file out-of-window data is ignored for the normal diff.
9. Source omission/shortening never deletes older retained history.

Canonical proof case required in planner/orchestration/provider integration tests:

```text
Bronze min date       = 2000-01-03
Bronze latest date    = 2026-08-18
injected today        = 2026-08-19
overlap_days          = 7
expected request      = 2026-08-11 .. 2026-08-19
forbidden request     = 2000-01-03 .. 2026-08-19
```

### Explicit `reconcile`

`reconcile` is a separate operator-requested command. It may request maximum currently exposed history to detect revisions older than the overlap window. It is **never invoked automatically by `run-daily`**. Operators may schedule it separately if desired.

A shorter/omitted response still never implies deletion. Explicit deletion semantics require a future source-mutation contract.

## Obsolete PR Archive

Superseded or completed PR records from earlier delivery waves are condensed here for
historical reference only. The active GOALS implementation PRs are listed at the top.

- **PR-00–PR-30: Repository, ingestion, Gold, and operations foundation** — Obsolete (historical delivery range)
- **PR-31–PR-61: PostgreSQL serving and operational hardening** — Obsolete (historical delivery range)
- **PR-62–PR-105: Feature transformations, Fed-policy work, and acceptance** — Obsolete (historical delivery range)

- **PR-175: Add OHLC Bar Range, Intraday Return, And Overnight Gap Features** — Obsolete
- **PR-173: Add Relative MACD Trend Features** — Obsolete
- **PR-174: Preserve OHLC Inputs Through Gold And PostgreSQL** — Obsolete
- **PR-172: Add EMA-Level Trend Features** — Obsolete
- **PR-171: Add Level-Only Composite And Statistical Features** — Obsolete
- **PR-170: Complete Scalar-Level Transformation Windows** — Obsolete
- **PR-169: Run The Daily Pipeline With A Bounded Core Budget** — Obsolete
- **PR-168: Retire Fed Policy Features And Acquisition** — Obsolete
- **PR-167: Add Next-Business-Day Availability To PostgreSQL Serving** — Obsolete
- **PR-165: Reconcile Fed Gaps And Publish Full Raw History** — Obsolete
- **PR-160: Follow Source Redirects In Cron HTTP Transport** — Obsolete
- **PR-159: Make PostgreSQL Verification Match Market Gold Artifacts** — Obsolete
- **PR-158: Scope Gold Summaries To Market-Owned Rows** — Obsolete
- **PR-157: Bound The Production Feature Refresh Timeout** — Obsolete
- **PR-156: Ignore Fed-Only Rows During Market Gold Reconciliation** — Obsolete
- **PR-155: Decouple Market Gold And Fed Policy PostgreSQL Ownership** — Obsolete
- **PR-146: Record Audit Findings And Remediation Plan** — Obsolete
- **PR-147: Unify Fed Feature Column Contract End To End** — Obsolete
- **PR-148: Correct FedWatch Economic Baseline And CME Reconstruction** — Obsolete
- **PR-149: Define Signed-Series Eligibility For XETRA Feature Families** — Obsolete
- **PR-150: Make RSI And Momentum Numerically Stable And NULL-Correct** — Obsolete
- **PR-151: Reconcile Executable Versions And Documentation Contracts** — Obsolete
- **PR-152: Independent End-To-End Mathematical And Serving Acceptance** — Obsolete
- **PR-145: Repair Installed Cron Runtime Path And Fail-Closed Configuration** — Obsolete
- **PR-129: Initial path rename attempt** — Obsolete
- **PR-130: Project path rename** — Obsolete
- **PR-131: Cron runtime executable preflight** — Obsolete
- **PR-141: Finalize Merged Backlog Status And Branch Cleanup** — Obsolete
- **PR-140: Consolidate Final XETRA Delivery Backlog Status** — Obsolete
- **PR-132: Implement XETRA-Compatible Returns And Volatility** — Obsolete
- **PR-133: Implement XETRA-Compatible Trend And Momentum Features** — Obsolete
- **PR-134: Independent Mathematical QA For Returns And Volatility** — Obsolete
- **PR-135: Independent Mathematical QA For Trend And Momentum** — Obsolete
- **PR-136: Verify Complete View Contract And Legacy Removal** — Obsolete
- **PR-137: Historical Data Quality, Revision, Replay, And Refresh QA** — Obsolete
- **PR-138: Complete Pipeline And PostgreSQL Serving Acceptance** — Obsolete
- **PR-139: Installed Cron And Failure-Recovery Acceptance** — Obsolete
- **PR-114: Extend Canonical Origin And Macro Raw Schema With Four Fed Features** — Obsolete
- **PR-115: Backfill And Incrementally Populate Macro Raw Fed Origins** — Obsolete
- **PR-116: Verify Macro Raw Fed History And Data Quality** — Obsolete
- **PR-117: Build Fed Materialized Features Exclusively From Macro Raw** — Obsolete
- **PR-118: Verify Macro Features Fed Transformations And Historical Quality** — Obsolete
- **PR-113: Reframe Fed Policy Serving Through Macro Raw** — Obsolete
- **PR-106: Differentially Validate Reconstruction Against Official CME FedWatch** — Obsolete
- **PR-107: Run Real-PostgreSQL Fed Policy Integration QA** — Obsolete
- **PR-108: Execute Full Fed Policy History Acceptance From 2010** — Obsolete
- **PR-109: Execute Installed Daily Cron Acceptance** — Obsolete
- **PR-110: Retire Legacy FedWatch Runtime And Finalize Documentation** — Obsolete
