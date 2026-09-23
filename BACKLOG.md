# Backlog

This backlog is the implementation source of truth for `macro-loader`.

The repository loads reusable daily market-state inputs from open/public sources, preserves source history, performs strict incremental updates during normal execution, and publishes deterministic immutable Gold feature snapshots through a Bronze -> Silver -> Gold architecture.

Last reviewed: 2026-09-23

## Current repository and production status

As of 2026-09-23, PR-97 through PR-105 are merged: the repository has the causal
ZQ-based Fed-policy reconstruction, the four canonical Fed-policy features, PostgreSQL
Fed synchronization, EOD orchestration, and the installed daily cron path. The remaining
serving-layout gap is structural: the four Fed origins are synchronized to the private
Fed-policy relation and `macro_features` currently joins that relation directly, while
`macro_raw` still contains only the pre-existing market-source columns.

The delivery wave below changes the serving lineage to one consistent path:

```text
canonical Fed EOD features
        -> canonical Gold/origin serving contract
        -> macro_loader.macro_raw
        -> macro_loader.macro_features
```

The target historical exposure boundary for both PostgreSQL relations is
`2010-01-01` through the latest completed EOD source date. Values may remain NULL only
where point-in-time source support is genuinely unavailable or a transformation has not
completed its causal warm-up; every such gap must be measured and explained by QA rather
than filled, interpolated, carried, or synthesized.

## New Delivery PRs — Fed Origins In PostgreSQL Serving

## PR-114: Extend Canonical Origin And Macro Raw Schema With Four Fed Features

PR name: `fed-policy-macro-raw-contract`
Status: Merged
Updated: 2026-09-23
PR: #117
Git branch: `pr-114/fed-policy-macro-raw-contract`
Git status: `merged`
Agent lane: Serving schema/migration; one agent only
Depends on: PR-113
Commit: `feat(pr-114): add fed policy origins to macro raw contract`
Design patterns: Versioned Migration, Specification/Policy Object, Single Source of Truth.

Description:
- R1: Extend the canonical origin-serving schema and `macro_loader.macro_raw` with exactly four nullable DOUBLE PRECISION columns: `fed_next_expected_move_bp`, `fed_path_slope_m3_bp`, `fed_next_uncertainty_bp`, and `fed_repricing_5obs_bp`; preserve the existing timestamp primary key and all pre-existing source columns/order semantics.
- R2: Make the four Fed columns part of the versioned source-controlled raw/origin contract used by PostgreSQL conformance, row digests, sync planning, and downstream catalog construction; advance the appropriate schema/version/fingerprint exactly once.
- R3: Preserve the architectural invariant that `macro_raw` is rebuildable from canonical local origin data: the migration must define how canonical Fed daily output is joined by `timestamp_m1` into the origin frame before PostgreSQL synchronization rather than writing ad-hoc SQL-only values.
- R4: Set the serving exposure lower boundary to `2010-01-01` without fabricating a Fed value on that date; pre-first-eligible and genuinely unavailable point-in-time observations remain NULL and are auditable.
- R5: Add clean-create and in-place-upgrade tests proving exact ordered schema/types/nullability, owner/grants, digest/version behavior, idempotent migration rerun, and preservation of unrelated tables/schemas.

Acceptance:
- A1 (verifies R1): independent PostgreSQL/schema inspection shows exactly the four Fed columns once in `macro_raw`, with DOUBLE PRECISION NULL types and no loss/reordering of existing columns.
- A2 (verifies R2): conformance/digest planners include all four columns; the declared schema/version/fingerprint changes once and stale pre-migration contracts fail closed.
- A3 (verifies R3): fixed canonical input rebuilds the same `macro_raw` Fed values without direct operator SQL and repository search finds one defined origin assembly path.
- A4 (verifies R4): rows from 2010-01-01 onward are permitted, but fixtures prove missing/holiday/pre-availability Fed observations remain NULL rather than filled or synthesized.
- A5 (verifies R5): clean bootstrap and production-shaped upgrade converge to the same exact contract, second migration is a no-op, and unrelated objects remain unchanged.

## PR-115: Backfill And Incrementally Populate Macro Raw Fed Origins

PR name: `fed-policy-macro-raw-population`
Status: Merged
Updated: 2026-09-23
PR: #118
Git branch: `pr-115/fed-policy-macro-raw-population`
Git status: merged
Agent lane: Historical/delta synchronization; one agent only
Depends on: PR-114
Commit: `feat(pr-115): populate macro raw fed policy origins`
Design patterns: Unit of Work, Repository, Reconciliation, Strategy.

Description:
- R1: Add an explicit one-time reconcile/backfill path that merges the canonical four-feature Fed daily dataset into canonical origin rows and synchronizes `macro_raw` for every serving timestamp from 2010-01-01 through the latest completed EOD source date.
- R2: For every timestamp where a canonical Fed value exists, persist exact value parity in the corresponding `macro_raw` column; where point-in-time source support is genuinely absent, persist NULL and record/report the gap rather than fill, interpolate, forward-carry, or synthesize.
- R3: Extend normal EOD/delta synchronization so new/revised canonical Fed rows update only the bounded affected serving rows plus required overlap/revision scope; daily execution must never trigger the full 2010 backfill automatically.
- R4: Reconcile inserts/updates/deletes, row hashes, sync state, source bounds, and canonical data SHA deterministically; immediate unchanged replay produces zero semantic mutations.
- R5: Preserve transactional atomicity: failure before commit leaves the previously committed `macro_raw`, row hashes, sync state, and downstream refresh eligibility unchanged.
- R6: Add hermetic integration fixtures spanning 2010 start, sparse Fed dates, a historical revision, a multi-day missed run, and unchanged replay with exact mutation counts.

Acceptance:
- A1 (verifies R1): an explicit backfill fixture exposes `macro_raw` rows from 2010-01-01 through the requested completed EOD date and processes the full canonical Fed history only under reconcile/backfill mode.
- A2 (verifies R2): all non-NULL Fed origin values in `macro_raw` equal canonical local Fed values exactly; every NULL is attributable to an explicit source/availability gap and no fill/carry occurs.
- A3 (verifies R3): normal EOD after a complete backfill requests/processes only bounded recent delta/revision scope and repository traces prove it never scans 2010-present automatically.
- A4 (verifies R4): insert/update/delete/no-op cases reconcile consumer rows, hashes/state/source bounds exactly and unchanged replay reports zero semantic mutations.
- A5 (verifies R5): injected failure at each mutation/verification boundary preserves the prior committed serving state and cannot expose partial Fed columns.
- A6 (verifies R6): all listed historical/delta scenarios have deterministic expected row/value/mutation counts.

## PR-116: Verify Macro Raw Fed History And Data Quality

PR name: `fed-policy-macro-raw-quality-qa`
Status: Merged
Updated: 2026-09-23
PR: #119
Git branch: `pr-116/fed-policy-macro-raw-quality-qa`
Git status: merged
Agent lane: Real PostgreSQL/history data-quality QA; one agent only
Depends on: PR-115
Commit: `test(pr-116): verify macro raw fed policy quality`
Design patterns: End-to-End Test, Reconciliation, Differential Testing, Fail-Closed Verification.

Description:
- R1: Run real-PostgreSQL clean/upgrade QA and query `macro_raw` from 2010-01-01 through the latest completed EOD, proving exact four-column schema, timestamp uniqueness/order, finite-or-NULL values, owner/grants, versions, hashes, and source-state conformance.
- R2: Differentially compare every non-NULL `macro_raw` Fed origin against the canonical local `fed_policy_features_daily` value on the same `timestamp_m1`; no tolerance is allowed beyond representation-equivalent Float64 equality unless a separately documented serialization tolerance is required.
- R3: Produce exact per-feature coverage statistics: first/last non-NULL date, non-NULL count, NULL count, longest gap, yearly coverage, and gap reasons; verify the requested lower boundary 2010-01-01 and fail on unexplained missing periods.
- R4: Add semantic/data-quality checks for finite values, non-negative uncertainty, expected 25bp-grid-compatible policy distributions upstream, plausible but non-clipping signed move/slope/repricing values, absence of impossible spikes caused by unit errors, and exact point-in-time availability/no-look-ahead spot checks across 2010, 2015, 2020, 2022, and a recent period.
- R5: Verify historical revision propagation, immediate no-op replay, and tamper detection for one Fed value, one hash/state row, and one schema/grant property; all drift must fail closed and repair through the documented sync/migrate path.
- R6: Emit deterministic sanitized `artifacts/acceptance/fed-policy-macro-raw-quality-v1.json` with schema/version, date bounds, per-feature coverage/gaps, canonical parity, quality checks, mutation/replay evidence, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): independent real-PostgreSQL inspection passes every schema/temporal/finite/grant/version/hash/state assertion from 2010-01-01 through current completed EOD.
- A2 (verifies R2): 100% of non-NULL `macro_raw` Fed values match canonical local values under the declared equality rule and any mismatch prevents PASS.
- A3 (verifies R3): the artifact contains complete per-feature coverage/gap metrics and every material gap has a documented causal/source reason; unexplained gaps fail.
- A4 (verifies R4): all semantic quality and point-in-time spot checks pass without clipping legitimate signed values or accepting unit/look-ahead errors.
- A5 (verifies R5): revision/no-op/tamper scenarios show exact expected behavior, zero mutations on unchanged replay, and fail-closed drift detection/recovery.
- A6 (verifies R6): the sanitized artifact is deterministic and cannot report PASS unless A1-A5 all pass.

## PR-117: Build Fed Materialized Features Exclusively From Macro Raw

PR name: `fed-policy-macro-features-from-raw`
Status: Merged
Updated: 2026-09-23
PR: #120
Git branch: `pr-117/fed-policy-macro-features-from-raw`
Git status: merged
Agent lane: Materialized-view transformations; one agent only
Depends on: PR-116
Commit: `feat(pr-117): derive fed policy features from macro raw`
Design patterns: Materialized View, Specification/Policy Object, Pure Transformation.

Description:
- R1: Remove the direct private Fed-policy relation join from the `macro_features` materialized-view query and source all four canonical Fed origins exclusively from `macro_raw`; the private Fed relation may remain as synchronization/audit staging but is not a feature-view input.
- R2: Expose the four canonical Fed origins unchanged in `macro_features` for every serving timestamp from 2010-01-01 through latest completed EOD, preserving NULLs exactly from `macro_raw`.
- R3: For each Fed origin add exactly `delta_1obs`, `delta_5obs`, `delta_20obs`, and `zscore_60obs` using source-valid-observation causal semantics, population stddev, full warm-up, and no fill/interpolation/carry.
- R4: For each Fed origin add exactly `momentum_autocorr_1_60obs`, `momentum_autocorr_5_60obs`, and `momentum_autocorr_20_120obs` from one-valid-observation changes using the same positive-clipping/undefined-NULL rules as existing momentum origins.
- R5: Do not add Fed `*_log_level`, shifted-log, ratio-return, or geometric-return columns because the canonical bp origins can be negative or zero; retain exactly 4 origin + 28 derived = 32 Fed columns.
- R6: Update the closed-world feature catalog, materialized-view version/fingerprint, normalized definition markers, conformance checks, and migration/rebuild path atomically; preserve all non-Fed feature formulas/order and one-refresh-per-semantic-sync behavior.

Acceptance:
- A1 (verifies R1): normalized SQL/code search contains no feature-view join to the private Fed table and every Fed expression is rooted in `macro_raw`.
- A2 (verifies R2): every canonical Fed value/NULL in `macro_features` equals the same-timestamp `macro_raw` origin exactly from 2010-01-01 onward.
- A3 (verifies R3): all 16 delta/z-score columns pass hand-calculable irregular-date/warm-up/zero-variance fixtures with exact valid-observation semantics.
- A4 (verifies R4): all 12 momentum-autocorrelation columns pass exact 60/120-window, lag, negative-correlation clipping, undefined-window, and sparse-calendar fixtures.
- A5 (verifies R5): schema/catalog/code search proves exactly 32 Fed columns and zero logarithmic/geometric/shifted-log Fed columns; negative/zero/sign-crossing origin fixtures remain valid.
- A6 (verifies R6): view version/fingerprint advance exactly once, non-Fed outputs are unchanged on fixed inputs, clean/upgrade definitions converge, one mutation causes one refresh, and no-op causes zero.

## PR-118: Verify Macro Features Fed Transformations And Historical Quality

PR name: `fed-policy-macro-features-quality-qa`
Status: In Progress
Updated: 2026-09-23
PR: #121
Git branch: `pr-118/fed-policy-macro-features-quality-qa`
Git status: active-dirty: tests/integration/test_postgres_real.py
Agent lane: Independent materialized-view QA; one agent only
Depends on: PR-117
Commit: `test(pr-118): verify fed policy materialized feature quality`
Design patterns: Differential Testing, Golden Master, End-to-End Test, Fail-Closed Verification.

Description:
- R1: Build an independent reference calculator from the four `macro_raw` Fed origin columns, without importing/calling the production SQL-expression builder, and reproduce all 28 derived Fed columns over the full available history.
- R2: Compare real PostgreSQL `macro_features` against `macro_raw` and the independent reference from 2010-01-01 through latest completed EOD: exact origin parity, exact schema/order/types, one declared numeric tolerance for derived floating arithmetic, timestamp uniqueness, finite-or-NULL values, version/fingerprint, owner/grants, and normalized definition.
- R3: Verify valid-observation semantics and data quality for sparse calendars, NULL gaps, 1/5/20 lags, z-score warm-up/zero variance, 60/120 momentum windows, negative correlation, sign changes, and absence of fill/interpolation/carry.
- R4: Produce per-column coverage statistics for all 32 Fed columns, including first/last non-NULL date, yearly counts, warm-up-derived NULLs versus upstream-origin NULLs, longest unexplained gap, and distribution summaries; unexplained gaps or premature non-NULL warm-up values fail.
- R5: Verify revision propagation and refresh lifecycle end to end: one historical `macro_raw` origin revision changes exactly the mathematically affected derived windows after one refresh; immediate unchanged replay yields zero source mutations and zero refreshes.
- R6: Emit deterministic sanitized `artifacts/acceptance/fed-policy-macro-features-quality-v1.json` covering all 32 columns, formulas, coverage/gaps, max numeric errors, version/fingerprint, refresh/replay evidence, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): all 28 derived columns are independently recomputed from `macro_raw`; missing/extra/renamed columns or an altered production formula fails QA.
- A2 (verifies R2): the real materialized view matches all four origins exactly and all 28 derived values within the single declared tolerance over the full 2010-current history while schema/version/grants/definition are exact.
- A3 (verifies R3): every listed lag/window/warm-up/gap/signed-value case has exact assertions and proves no calendar-day, carry, logarithmic, or geometric-return shortcut.
- A4 (verifies R4): the artifact contains complete coverage/quality metrics for all 32 columns; all NULLs are attributable to upstream absence or causal warm-up and unexplained anomalies prevent PASS.
- A5 (verifies R5): revision/no-op tests prove exact affected windows, exactly one refresh on semantic mutation, zero refresh on unchanged replay, and stable fingerprint.
- A6 (verifies R6): the sanitized artifact is deterministic and PASS is impossible unless A1-A5 all pass.

## Active Delivery Program — Fed Policy In `macro_raw` And `macro_features`

The four canonical Fed-policy EOD origins are:

```text
fed_next_expected_move_bp
fed_path_slope_m3_bp
fed_next_uncertainty_bp
fed_repricing_5obs_bp
```

Their canonical formulas and point-in-time availability semantics remain those delivered by
PR-97–105. This wave changes only their serving placement and downstream transformations:
the four origins must be first-class columns in `macro_raw`, and `macro_features` must
consume those columns from `macro_raw` rather than directly joining a private Fed table.

Both serving relations expose the requested history from `2010-01-01` through the latest
completed EOD source date. `2010-01-01` is the lower serving boundary; the first actual
eligible Fed observation is source/calendar dependent and must be verified. No QA may turn
a public-source gap into an invented value.

The signed-safe shared transform set for each Fed origin is exactly:

```text
delta_1obs
delta_5obs
delta_20obs
zscore_60obs
momentum_autocorr_1_60obs
momentum_autocorr_5_60obs
momentum_autocorr_20_120obs
```

Thus `macro_features` exposes 4 canonical Fed origins plus 28 derived Fed columns = 32
Fed columns. Natural-log levels, shifted-log workarounds, and geometric/percentage returns
are intentionally excluded because the Fed basis-point origins can be zero or negative.

The active dependency chain is:

```text
PR-114 serving contract/schema
    |
    v
PR-115 backfill + delta population of macro_raw
    |
    v
PR-116 macro_raw data-quality QA
    |
    v
PR-117 macro_features consumes macro_raw + 28 transforms
    |
    v
PR-118 macro_features transform/data-quality QA
    |
    +--------------------------+
    |                          |
    v                          v
PR-106 CME differential QA   PR-107 real PostgreSQL QA
                                  |
                                  v
                          PR-108 history acceptance
                                  |
                                  v
                          PR-109 cron acceptance
                                  |
                                  v
                          PR-110 cleanup/docs
```

## PR-113: Reframe Fed Policy Serving Through Macro Raw

PR name: `fed-policy-transform-backlog`
Status: Merged
Updated: 2026-09-23
PR: #115
Git branch: `pr-113/fed-policy-transform-backlog`
Git status: `merged`
Agent lane: Backlog/governance extension; one agent only
Depends on: none
Commit: `docs(pr-113): define fed macro raw serving program`
Design patterns: Specification/Policy Object.

Description:
- R1: Freeze the serving lineage `canonical Fed EOD -> canonical origin contract -> macro_raw -> macro_features`; the materialized feature view must not bypass `macro_raw` with a direct private-Fed-table join after the migration completes.
- R2: Define PR-114 through PR-118 as atomic schema/population/QA/transformation/QA scopes, each with complete one-to-one Rn/An acceptance lists and a requested historical boundary of 2010-01-01 through the latest completed EOD source date.
- R3: Freeze the `macro_features` Fed contract at four canonical origins plus exactly 28 signed-safe derived columns (7 per origin), with no logarithmic/geometric/shifted-log transformations.
- R4: Gate PR-107, PR-108, PR-109, and PR-110 on the new `macro_raw` and `macro_features` QA evidence so real PostgreSQL, historical, cron, and final-documentation PASS cannot predate the serving migration.

Acceptance:
- A1 (verifies R1): BACKLOG explicitly names `macro_raw` as the sole PostgreSQL origin source used by `macro_features` for the four Fed columns after migration and forbids the previous direct private-table join.
- A2 (verifies R2): PR-114 through PR-118 each have complete metadata, atomic scope, exact R/A counts, and explicit 2010-to-current-EOD acceptance.
- A3 (verifies R3): the contract enumerates the seven allowed transforms per origin and states 4 + 28 = 32 Fed columns with explicit log/geometric exclusions.
- A4 (verifies R4): downstream PR dependencies/acceptance require PR-116 and PR-118 evidence before any full PostgreSQL/history/cron/final PASS.

## PR-106: Differentially Validate Reconstruction Against Official CME FedWatch

PR name: `fed-policy-cme-differential-qa`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-106/fed-policy-cme-differential-qa`
Git status: `not-started (branch absent)`
Agent lane: Formula/differential QA; one agent only
Depends on: PR-102
Commit: `test(pr-106): validate fed policy reconstruction against cme`
Design patterns: Differential Testing, Golden Master, Test Fixture.

Description:
- R1: Capture a small sanitized set of official public CME FedWatch meeting-export fixtures for overlapping dates/meetings and compare reconstructed target/move probability distributions to the official distributions using one source-controlled numeric tolerance and explicit bucket-alignment rules.
- R2: From the same official fixtures, independently calculate/compare all four canonical features, including three-meeting path slope and five-valid-observation repricing, and report exact per-feature error statistics.
- R3: Keep the existing browser/MeetingExport adapter only as a QA acquisition/oracle boundary if still needed; it must not remain a required production/backfill/cron source.
- R4: Add explicit anti-look-ahead differential fixtures proving EFFR publication lag and FOMC schedule/emergency-meeting first-known times change historical eligibility exactly when they should.
- R5: Emit a deterministic sanitized `artifacts/acceptance/fed-policy-cme-parity-v1.json` listing fixture identities/hashes, methodology version, tolerances, compared meetings/features, max errors, causal checks, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): every captured official meeting distribution is matched within the declared tolerance, probability buckets/mass align deterministically, and a deliberately altered reconstruction formula fails.
- A2 (verifies R2): all four feature comparisons are present with finite error metrics and the path-slope/repricing checks use the PR-97 semantics rather than the legacy M3 cumulative sum.
- A3 (verifies R3): repository call-path tests prove browser FedWatch is QA-only and production commands succeed without browser dependencies/profile state.
- A4 (verifies R4): before-publication/before-announcement fixtures exclude unavailable information and become eligible only after the recorded availability boundary.
- A5 (verifies R5): the parity artifact is deterministic/sanitized and can report PASS only if A1-A4 all pass.



## PR-107: Run Real-PostgreSQL Fed Policy Integration QA

PR name: `fed-policy-real-postgres-qa`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-107/fed-policy-real-postgres-qa`
Git status: `not-started (branch absent)`
Agent lane: Real PostgreSQL QA; one agent only
Depends on: PR-106, PR-116, PR-118
Commit: `test(pr-107): validate fed policy postgres integration`
Design patterns: End-to-End Test, Reconciliation, Fail-Closed Verification.

Description:
- R1: Add a real-PostgreSQL suite for clean bootstrap and upgrade from the current production-shaped schema through migrations, Fed-policy sync, materialized-view refresh, independent conformance verification, and migration rerun.
- R2: Verify exact `macro_raw` and public `macro_features` schemas, the four canonical Fed origins plus all 28 PR-117 derived columns (32 Fed columns total), view version/fingerprint, finite-or-NULL values, timestamp uniqueness, ownership/grants, and downstream read-only consumer compatibility.
- R3: Exercise insert, historical revision/update, explicit source-row removal during reconcile, mixed delta, and immediate no-op replay; after each case source/consumer/hash/state/view reconciliation must be exact.
- R4: Inject raw private-table tamper, view-definition/schema drift, owner/grant drift, digest/state drift, and refresh failure; every case must fail closed and preserve/recover through the documented migrate/sync path.
- R5: Emit deterministic PostgreSQL QA evidence with mutation/refresh counts, normalized object definitions, schema/version/fingerprint, conformance result, and PASS|FAIL without credentials.

Acceptance:
- A1 (verifies R1): clean and upgrade paths converge to the same PostgreSQL contract, migration rerun is a no-op, and independent verification passes only after valid Fed synchronization/refresh.
- A2 (verifies R2): exact introspection/query assertions cover every required column/type/grant/version property and a regime-engine-shaped read sees all 32 Fed origin/derived features through the supported macro feature plane.
- A3 (verifies R3): all mutation classes reconcile exactly and the unchanged replay produces zero semantic row mutations and zero materialized-view refreshes.
- A4 (verifies R4): every deliberate drift/failure prevents false success and the documented repair path restores exact conformance without touching unrelated schemas.
- A5 (verifies R5): the QA evidence is deterministic/sanitized and PASS is impossible if any schema/data/refresh/reconciliation assertion fails.

## PR-108: Execute Full Fed Policy History Acceptance From 2010

PR name: `fed-policy-2010-full-history-acceptance`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-108/fed-policy-2010-full-history-acceptance`
Git status: `not-started (branch absent)`
Agent lane: Production-like historical acceptance; one agent only
Depends on: PR-107, PR-116, PR-118
Commit: `test(pr-108): execute fed policy history acceptance`
Design patterns: End-to-End Acceptance, Reconciliation, Fail-Closed Verification.

Description:
- R1: In an explicitly authorized environment, execute the real historical Fed-policy reconcile from requested start `2010-01-01` through the current completed source date using the persistent lake and real free/public provider configuration, then synchronize PostgreSQL and run independent conformance verification.
- R2: Prove source coverage from the first eligible 2010 trading date (expected 2010-01-04, verified rather than assumed) through the present with exact settlement-curve/date counts and an explicit unexplained-gap report; unavailable public history must make this acceptance FAIL rather than create synthetic observations.
- R3: Verify the four canonical Fed-origin histories and all 28 materialized derived Fed histories are unique, ordered, finite-or-NULL, causal, and populated only after their exact valid-observation warm-up; perform exact/independent spot checks across at least 2010, the 2015 liftoff period, 2020 emergency policy, the 2022 hiking cycle, and a recent period.
- R4: Query real PostgreSQL as the downstream consumer and verify `macro_raw` exposes the four Fed origins and `macro_features` exposes those four plus all 28 derived columns from 2010-01-01 through current completed EOD, with exact origin parity, independent derived-value parity, version/fingerprint, and no unintended changes to non-Fed columns.
- R5: Immediately run normal EOD update and PostgreSQL sync unchanged; require bounded recent source requests, zero semantic data mutations, zero view refresh, and identical relevant fingerprints.
- R6: Commit only a deterministic sanitized `artifacts/acceptance/fed-policy-history-v1.json` with source identities, min/max/count/gaps, methodology/version hashes, spot-check evidence, PostgreSQL parity, replay counts, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): every declared real stage executes in order against the intended persistent lake/PostgreSQL endpoint and independent verification runs after sync.
- A2 (verifies R2): the artifact proves the actual first eligible 2010 date and exact continuous/explicit-gap coverage; any unresolved required-history gap prevents PASS and is not filled.
- A3 (verifies R3): all four canonical feature semantics plus every PR-117 derived family pass independent spot checks over the listed policy regimes with correct warm-up/availability and no NaN/infinity/look-ahead.
- A4 (verifies R4): `macro_raw` Fed origins match canonical local output exactly and all 28 `macro_features` derived values match the independent reference within declared tolerances; timestamps/version/fingerprint are exact and non-Fed columns remain unchanged.
- A5 (verifies R5): immediate normal replay is demonstrably delta-bounded and records exactly zero semantic mutations/refreshes with stable fingerprints.
- A6 (verifies R6): the committed artifact is deterministic/sanitized and PASS is impossible unless A1-A5 all pass.

## PR-109: Execute Installed Daily Cron Acceptance

PR name: `fed-policy-cron-acceptance`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-109/fed-policy-cron-acceptance`
Git status: `not-started (branch absent)`
Agent lane: Operational cron acceptance QA; one agent only
Depends on: PR-107, PR-108, PR-116, PR-118
Commit: `test(pr-109): execute fed policy cron acceptance`
Design patterns: End-to-End Acceptance, Command, Single-Instance Lock, Failure Injection.

Description:
- R1: Execute the exact installed/source-controlled daily cron wrapper under the real scheduler/service-account configuration, working directory, timezone, lock, persistent lake, logs, and PostgreSQL roles; direct Python substitution is forbidden for this acceptance.
- R2: Demonstrate a run with one or more newly available/revised ZQ observations produces the exact four-origin local feature delta, exact PostgreSQL mutations, exactly one materialized-view refresh, and conformant 32-column Fed origin/derived downstream output; an immediate rerun must produce zero semantic mutations and zero refreshes.
- R3: Demonstrate recovery after intentionally skipping multiple scheduled runs and demonstrate weekend/holiday execution; catch-up must use bounded overlap/delta requests only and no-data runs must be successful no-ops.
- R4: Inject CME/source unavailability before local publication and PostgreSQL/refresh failure after local publication separately; the first must prevent PostgreSQL mutation, the second must return non-zero and preserve the last committed PostgreSQL/view state for safe retry.
- R5: Verify lock contention, cwd/Git identity, scheduler timezone/time, log path, exit-code propagation, runtime/admin privilege separation, and secret redaction using the installed execution path.
- R6: Commit only a deterministic sanitized `artifacts/acceptance/fed-policy-cron-v1.json` with run/source dates, request bounds, mutation/refresh counts, failure-injection outcomes, operational assertions, conformance, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): evidence identifies the exact cron wrapper/service identity/config/cwd/timezone/lock/log path used and proves no substitute execution path was used.
- A2 (verifies R2): mutation run records the exact expected origin changes, independently correct affected derived values, and one refresh; immediate rerun records exactly zero semantic changes/refreshes with identical downstream fingerprint.
- A3 (verifies R3): missed-run recovery stays within bounded delta semantics and weekend/holiday invocation succeeds without fabricated observations or hidden reconcile.
- A4 (verifies R4): both injected failures produce non-zero status with the required skip/rollback behavior and no false PASS or partially advanced PostgreSQL/view state.
- A5 (verifies R5): all installed operational/privilege/redaction assertions pass and lock contention prevents concurrent execution.
- A6 (verifies R6): the cron artifact is deterministic/sanitized and PASS is impossible unless A1-A5 all pass.

## PR-110: Retire Legacy FedWatch Runtime And Finalize Documentation

PR name: `fed-policy-runtime-cleanup-docs`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-110/fed-policy-runtime-cleanup-docs`
Git status: `not-started (branch absent)`
Agent lane: Cleanup/documentation; one agent only
Depends on: PR-106, PR-107, PR-108, PR-109, PR-116, PR-118
Commit: `refactor(pr-110): retire legacy fedwatch runtime path`
Design patterns: Single Source of Truth, Ports and Adapters.

Description:
- R1: Remove/deactivate legacy production wiring that can source model features directly from browser-only CME FedWatch exports or the simplified legacy `_outcomes` approximation; retain any browser export adapter only under an explicit QA boundary required by PR-106.
- R2: Remove/rename stale `fed_m3_expected_move_bp` contracts/tests/docs and any dead Fed-policy pipeline injection left behind by the pre-PR-97 architecture, without deleting canonical historical snapshots/evidence needed for audit.
- R3: Update README, ARCHITECTURE, BACKLOG status, operational instructions, data-source/availability semantics, the `macro_raw` four-origin plus `macro_features` 28-derived Fed transformation contract, semantic/view versions, and cron documentation to the verified post-PR-109/PR-118 state; do not claim historical/production PASS beyond the committed acceptance artifacts.
- R4: Add repository-wide reference/import/startup checks proving one authoritative Fed-policy runtime path, all required QA paths remain available, no stale browser production dependency survives, and the complete required quality gate remains green.

Acceptance:
- A1 (verifies R1): production call-path/code search finds no browser FedWatch or simplified `_outcomes` feature source, while PR-106 QA fixtures/tools remain explicitly isolated and functional.
- A2 (verifies R2): `fed_m3_expected_move_bp` is absent from the public/runtime contract, no dead injected Fed-policy source remains in daily orchestration, and audit evidence/history is preserved.
- A3 (verifies R3): all four sidecars/ops docs agree on source, canonical formulas, 28 derived transformation formulas/exclusions, point-in-time availability, 2010 coverage evidence, PostgreSQL publication, daily cron, and current versions with no unverified success claims.
- A4 (verifies R4): reference/import/startup checks and `lint/type/unit/integration/coverage` are green and repository search identifies exactly one production probability/feature pipeline.


## Delivery Policy

- One `PR-XX` entry equals one logical implementation pull request.
- PRs are sized for two weak coding agents working in parallel: one infrastructure boundary, provider family, transformation boundary, publication concern, or operational concern per PR.
- Every PR has `Status`, `Updated`, `PR`, `Git branch`, `Git status`, `Agent lane`, `Depends on`, `Commit`, and `Design patterns`.
- Delivery statuses: `Planned`, `In Progress`, `Blocked`, `Ready`, `Merged`.
- Git statuses: `not-started (branch absent)`, `active-clean`, `active-dirty: <paths>`, `pushed-ci-failing`, `pushed-ci-green`, `merged`.
- Every `Description` requirement `R<n>` has exactly one matching `Acceptance` item `A<n>`. Counts must match.
- Implement only the selected PR. Do not pull future-PR scope forward.
- Required unit/integration tests are offline. Live provider tests use `@pytest.mark.network` and are excluded from required gates.
- Production dataframe operations are Polars-first; no production pandas dependency.
- Runtime `lake/` is ignored by Git.
- `README.md`, `ARCHITECTURE.md`, `AGENTS.md`, and this backlog must not intentionally contradict one another.

## Git Workflow Contract

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
- Before push: required local quality gate passes and `git status --short` is empty.
- A pushed branch with any failing required checks has Git status `pushed-ci-failing`.
- Before `Ready`: remote `lint`, `type`, `unit`, `integration`, `coverage` are green and Git status is `pushed-ci-green`.
- Enable PR auto-merge with squash when the PR is ready; protected `main` ensures merge occurs only after the merge gate passes.
- After merge: update backlog status/PR link/Git status in the next documentation-maintenance change; do not keep an implementation task alive to start another PR.
- No force-push on shared branches, branch reuse, or guessed semantic conflict resolution.

## Push And Merge Quality Gates

Required checks:

```text
lint
type
unit
integration
coverage
```

### Parallel execution

`lint`, `type`, `unit`, and offline `integration` start independently/in parallel. `unit` and `integration` produce separate raw coverage data.

### Coverage

`coverage` depends only on `unit` and `integration`, combines their raw data, and enforces production-code **line coverage >= 90.0%** for:

```text
application/
ingestion/
api/
scripts/
```

Tests, fixtures, generated artifacts, and `lake/` are excluded. `89.99%` fails; `90.00%` passes. Do not exclude production files merely to reach the threshold.

### Triggers

The same gate runs on:

```text
local pre-push
GitHub push
pull_request -> main
merge_group
```

### Target GitHub repository policy

`main` must be ruleset/protection controlled:

- pull request required;
- direct push, force push, and branch deletion blocked;
- required checks: `lint`, `type`, `unit`, `integration`, `coverage`;
- branch up-to-date / merge-queue compatible;
- squash merge only;
- repository auto-merge enabled;
- implementation PRs use auto-merge so GitHub completes them only after all required gates pass;
- head branch deleted after merge.

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
| `move` | Yahoo Finance | `^MOVE` | `ohlc` | `date_range` | maximum available history |
| `ciss` | ECB | `CISS.D.U2.Z0Z.4F.EC.SS_CIN.IDX` | `scalar` | `date_range` | maximum exposed history |
| `estr` | ECB | `EST.B.EU000A2X2A25.WT` | `scalar` | `date_range` | maximum exposed history |
| `euro_hy_oas` | FRED | `BAMLHE00EHYIOAS` | `scalar` | `date_range` | maximum currently exposed history; preserve older local history |
| `us_2y` | FRED | `DGS2` | `scalar` | `date_range` | maximum exposed history |
| `us_10y` | FRED | `DGS10` | `scalar` | `date_range` | maximum exposed history |
| `usd_broad` | FRED | `DTWEXBGS` | `scalar` | `date_range` | maximum exposed history |

No additional MVP series or implicit provider fallback is allowed without a separate PR.

## Medallion Storage Contract

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

## PR Graph

Each PR's `Depends on:` field is authoritative; this diagram is informational only.

```text
PR-01 foundation + quality/Git policy
  |\
  | +--> PR-03 Parquet repositories
  +----> PR-02 registry/path contracts
             |\
             | +--> PR-04 HTTP/provider ports
             +----> PR-05 planner/state
  PR-02 + PR-03 --> PR-11 manifests/inventory

PR-02 + PR-04 + PR-05
       |      |      |      |      |
     PR-06  PR-07  PR-08  PR-09  PR-10
       \      |      |      |      /
        +-----+------+------+-+----+
                         + PR-03 + PR-11
                                  |
                                PR-12
                               /     \
                            PR-13   PR-14
                           /    \
                        PR-15  PR-16
                           \    /
                            PR-17
                           /     \
                        PR-18   PR-19
                          |
                        PR-20
                           \     /
                            PR-21
                              |
                            PR-22
                              |
                    PR-14 + PR-21 + PR-22
                              |
                            PR-23

PR-24 governance contract is an orthogonal repository-policy sidecar. It may run in parallel with already-active PR-04/PR-05/PR-11 and must merge before starting any new not-yet-active implementation PR.
```

---

## Closed Delivery Summary

This section is the concise historical summary for completed delivery work. Backlog PR identifiers
(`PR-XX`) are the source-of-truth delivery identifiers; GitHub pull-request numbers may differ
because replacement/cleanup PRs were occasionally opened under the same backlog scope.

- PR-00: Architecture/backlog review established strict delta semantics, hexagonal boundaries, Gold publication safety, and the original Git/quality contract before implementation.
- PR-01–05: Repository bootstrap, protected quality gates, canonical registry/lake paths, Polars Parquet IO, shared HTTP/retry ports, and explicit bootstrap/update/reconcile planning/state.
- PR-06–10: Provider adapters for CBOE volatility indices, STOXX VSTOXX, Yahoo MOVE, ECB CISS/ESTR, and FRED rates/credit/USD sources.
- PR-11–14: Operational manifests/inventory, registry-driven Bronze orchestration, canonical Silver, and inventory CLI.
- PR-15–17: Original volatility and macro feature transformations plus canonical Gold assembly/validation.
- PR-18–22: Immutable Gold bundles, catalog resolution, manifests/diagnostic sidecars, publication state machine/materialized root views, and mark-and-sweep retention.
- PR-23–30: Delta-only daily pipeline/CLI, backlog governance, scheduled runner, live-provider repairs, Polars parallelism, Gold mirroring, protected config, and Gold diagnostics.
- PR-31–39: Initial PostgreSQL serving program: contracts, delta planner, adapter, service role/config, full accumulated delta sync, CLI, and Sunday sync chain.
- PR-40–61: Corrective PostgreSQL/operations program covering temporal correctness, repository governance, real-PostgreSQL CI, deterministic cron execution/locking/timezone, provider validation/security, Gold formula/input provenance, bundle verification, transactional locking, schema migrations, admin/runtime separation, least privilege, exact consumer/digest/state reconciliation, bounded waits, documentation accuracy, independent live conformance verification, and authoritative production reconstruction.
- PR-62: One-valid-observation delta features were added across canonical source series.
- PR-63–65: Required offline tests were parallelized, duplicate PR CI triggers removed, and Gold integration rendering accelerated while preserving real renderer coverage.
- PR-66: Official Fed policy expectation work and macro serving-view groundwork were delivered; later raw-serving hardening deliberately kept canonical Gold/`macro_raw` source-level-only.
- PR-67: PostgreSQL conformance/sync hardening moved canonical serving toward raw-only `macro_raw` and added the current manual macro-feature materialized-view maintenance script that PR-82/85 will replace with versioned migrations.
- PR-68: Positive autocorrelation momentum features at (1,60), (5,60), and (20,120) were implemented for canonical series.
- PR-70: Rolling geometric-return features at 10/25/60/120/240 observations were implemented.
- PR-71: Repository/project/CLI/operational naming was completed as `macro-loader`.
- PR-74: PostgreSQL table ownership and the dedicated `macro-loader-sync` writer role were hardened.
- PR-79: Current production-status documentation was reconciled after the latest serving/conformance changes.

- PR-80–81: Materialized-feature-library ownership and executable catalog/version contracts were frozen.
- PR-82–85: The versioned PostgreSQL materialized feature view, regime-oriented additions, single-refresh synchronization, and conformance/migration enforcement were delivered.
- PR-86–88: Python-vs-SQL formula parity was proven, duplicate Python feature runtime was removed, and the feature library was validated on real PostgreSQL.
- PR-89–90: Full-history and installed Sunday-cron acceptance harnesses/artifacts for the materialized macro feature library were delivered.
- PR-96: Raw source exposure in `macro_features` was changed to explicit logarithmic `*_log_level` columns; GitHub PRs #93–#95 were superseded/closed attempts of the same scope and #96 is the merged implementation.
- PR-97–105: Fed-policy EOD contract, ZQ persistence/acquisition, point-in-time Fed references, probability reconstruction, four canonical features, private PostgreSQL synchronization, delta orchestration, and installed daily cron were delivered; the next serving-layout wave moves those origins into `macro_raw` and derives `macro_features` from that table.

The detailed active backlog above contains only open/planned Fed serving work. Completed PR-97–105 are intentionally condensed in this Closed Delivery Summary and must not be reopened or silently rewritten to implement PR-113+ work.
