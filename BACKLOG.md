# Backlog

This backlog is the implementation source of truth for `macro-loader`.

The repository loads reusable daily market-state inputs from open/public sources, preserves source history, performs strict incremental updates during normal execution, and publishes deterministic immutable Gold feature snapshots through a Bronze -> Silver -> Gold architecture.

Last reviewed: 2026-09-23

## Current repository and production status

`main` is at commit `c1aee46b40c02b4d13f6a9747dead33a25b9ff70` as of
2026-09-23. Canonical raw Gold is `schema_version = 7` and
`feature_version = 6`; the PostgreSQL macro-feature view contract is
`MACRO_FEATURE_VIEW_VERSION = 2`. The legacy Fed-policy provider, snapshot store,
and four-feature Python module still exist, but the active `run-daily` -> raw Gold ->
PostgreSQL feature path does not currently refresh or publish Fed-policy data. The delivery
program below replaces that incomplete/dead wiring with a causal, reproducible ZQ-settlement
reconstruction that supports a one-time historical backfill from 2010 and strict EOD delta-only
updates thereafter.

## Active Delivery Program — Fed Policy EOD Reconstruction

This delivery wave makes the Fed-policy expectation family a first-class causal input to the
versioned PostgreSQL macro feature library. The required feature set is exactly:

```text
fed_next_expected_move_bp
fed_path_slope_m3_bp
fed_next_uncertainty_bp
fed_repricing_5obs_bp
```

The historical request boundary is `2010-01-01`; the first expected eligible U.S. futures
trading day is `2010-01-04`, subject to source verification. Historical backfill is an explicit
operator action and may scan history. Normal EOD execution is strict delta-only, uses a bounded
overlap for recent revisions, and must never silently switch to a full-history reconcile.

The required production path must be operable without CME DataMine or another paid market-data
entitlement. Public/free-source gaps are measured and fail the corresponding historical
acceptance stage rather than being filled, synthesized, or hidden. Optional external sources may
be used for independent QA only if they are not required to make production or acceptance pass.

`timestamp_m1` remains observation-day identity rather than availability time. Every Fed-policy
raw/derived record therefore also carries explicit point-in-time availability, and a historical
model may consume a row only after all inputs used by that row were actually available. In
particular, the implementation must not use an EFFR observation before its publication time or a
future/emergency FOMC meeting before that meeting was publicly known.

The active dependency chain is:

```text
PR-97 contract
  |
  +--> PR-98 ZQ store --> PR-99 CME ZQ acquisition ---------+
  |                                                          |
  +--> PR-100 point-in-time Fed references ------------------+--> PR-101 probability engine
                                                                  |
                                                                  v
                                                          PR-102 four features
                                                            |             \
                                                            v              v
                                                     PR-103 PostgreSQL   PR-106 differential QA
                                                            |              |
                                                            v              |
                                                     PR-104 EOD app         |
                                                            |              |
                                                            v              |
                                                     PR-105 cron            |
                                                            \             /
                                                             v           v
                                                          PR-107 real PostgreSQL QA
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

## PR-97: Freeze Fed Policy EOD Contract And Delivery Program

PR name: `fed-policy-eod-contract`
Status: Ready
Updated: 2026-09-23
PR: #97
Git branch: `pr-97/fed-policy-eod-contract`
Git status: `pushed-ci-green`
Agent lane: Architecture/backlog contract; one agent only
Depends on: none
Commit: `test(pr-97): cover catalog validation guard`
Design patterns: Specification/Policy Object, Ports and Adapters.

Description:
- R1: Freeze the exact four-feature semantic contract: `fed_next_expected_move_bp = E[R1] - R0`; `fed_path_slope_m3_bp = E[R3] - E[R1]`; `fed_next_uncertainty_bp` is the probability-weighted population standard deviation of the next-meeting move distribution; and `fed_repricing_5obs_bp = fed_next_expected_move_bp(t) - fed_next_expected_move_bp(t-5 valid observations)`.
- R2: Freeze point-in-time semantics: `timestamp_m1` is observation-day identity; derived-row `available_at_utc` is not fabricated and must be at least the latest availability of every input used; same-day unpublished EFFR and not-yet-announced scheduled/emergency meetings are forbidden.
- R3: Freeze source/coverage policy: requested history begins at 2010-01-01, the first expected eligible trading day is 2010-01-04 subject to source proof, no fill/interpolation/synthetic FedWatch probabilities are allowed, and the required runtime/acceptance path may not depend on paid CME DataMine or a paid FedWatch API.
- R4: Define PR-98 through PR-110 as small dependency-ordered implementation/QA scopes, require one-to-one Rn/An acceptance mapping, and require README/ARCHITECTURE/BACKLOG sidecar updates whenever a public contract changes.

Acceptance:
- A1 (verifies R1): BACKLOG contains the four names, units, and exact formulas above; `fed_path_slope_m3_bp` cannot be interpreted as a sum of cumulative meeting moves and the old `fed_m3_expected_move_bp` semantic is not part of the new contract.
- A2 (verifies R2): the contract explicitly distinguishes observation identity from availability and states causal rules for EFFR publication and FOMC schedule knowledge, including emergency meetings.
- A3 (verifies R3): the contract names the 2010 boundary, prohibits fabricated gap filling, and makes free/public production data a hard acceptance condition rather than an undocumented preference.
- A4 (verifies R4): PR-98 through PR-110 each declare complete metadata, exact R/A counts, dependencies, and dedicated formula, PostgreSQL, full-history, and cron QA stages.

## PR-98: Add Canonical ZQ Settlement-Curve Store

PR name: `fed-policy-zq-settlement-store`
Status: Ready
Updated: 2026-09-23
PR: not opened
Git branch: `pr-98/fed-policy-zq-settlement-store`
Git status: `pushed-ci-green`
Agent lane: Fed-funds-futures persistence; one agent only
Depends on: PR-97
Commit: `feat(pr-98): persist canonical zq settlement curves`
Design patterns: Repository, Value Object, Unit of Work.

Description:
- R1: Add a canonical Polars/Parquet store for individual CME 30-Day Federal Funds futures (`ZQ`) final settlements with at least `observation_date`, `contract_month`, `settlement_price`, `source_id`, `source_url`, `fetched_at_utc`, and `available_at_utc`; natural key is `(observation_date, contract_month)`.
- R2: Store individual monthly contracts only; a continuous/front contract, stitched series, OHLC close, or intraday last trade must never substitute for the official final settlement curve used by the probability engine.
- R3: Implement deterministic idempotent upsert/revision handling so equal-key final-settlement revisions replace the prior value, unchanged replay is a no-op, and only affected physical partitions are rewritten.
- R4: Add schema, duplicate-key, invalid-price, timezone, deterministic-sort, revision, and unchanged-replay unit tests; raw-source omission must never delete older retained history implicitly.

Acceptance:
- A1 (verifies R1): repository round-trip returns the exact declared schema/types/order and one unique row per observation-date/contract-month key with source and availability provenance intact.
- A2 (verifies R2): tests reject continuous symbols and non-final/close-only records, while a multi-contract same-day curve is preserved without collapsing contract months.
- A3 (verifies R3): insert/revision/no-op fixtures produce exact mutation counts and rewrite only partitions containing changed natural keys.
- A4 (verifies R4): all listed validation cases fail closed, deterministic replay is byte-stable where expected, and an omitted upstream contract/date does not erase durable history.

## PR-99: Acquire Historical And Delta ZQ Settlements From Public CME

PR name: `fed-policy-zq-cme-provider`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-99/fed-policy-zq-cme-provider`
Git status: `not-started (branch absent)`
Agent lane: CME public-source adapter; one agent only
Depends on: PR-98
Commit: `feat(pr-99): ingest public cme zq settlements`
Design patterns: Adapter, Ports and Adapters, Strategy.

Description:
- R1: Implement a dedicated CME public-settlement adapter that requests date-specific ZQ final settlements, normalizes all required individual contract months, and records the source/availability metadata required by PR-98; no paid CME FedWatch/DataMine endpoint may be required.
- R2: Support explicit bootstrap/reconcile from requested start `2010-01-01` through the requested end, preserve every legitimately exposed historical curve, and emit an exact coverage/gap report rather than inventing observations when the public source does not expose a date.
- R3: Implement normal update from authoritative durable max observation date with the repository-wide seven-calendar-day overlap; normal update must issue only bounded recent requests and must never fall back to full-history reconcile.
- R4: Fail closed on malformed/non-final settlements, ambiguous contract months, HTML/bot/error payloads, and impossible date reversals; transient source failure must leave durable settlement history unchanged.
- R5: Add hermetic parser/provider fixtures plus an opt-in network smoke that probes both a recent settlement date and the oldest required 2010 boundary; the network smoke may report unavailable coverage but may not fabricate PASS.

Acceptance:
- A1 (verifies R1): fixture payloads produce exact ZQ monthly final-settlement rows and code/reference search proves no paid CME API/DataMine credential or browser FedWatch export is required by this provider.
- A2 (verifies R2): a historical fixture produces a deterministic min/max/count/gap report beginning at the requested 2010 boundary, and omitted dates remain explicit gaps rather than forward-filled curves.
- A3 (verifies R3): with durable max date D, update requests exactly `D-7 calendar days .. today`; a fixture with history back to 2010 proves the provider does not re-request that history during normal update.
- A4 (verifies R4): each malformed/error fixture yields non-success with zero durable mutations and no partial replacement of a previously valid curve.
- A5 (verifies R5): required CI is fully offline; the marked network smoke records actual source capability separately and cannot turn missing 2010 public coverage into a green historical acceptance claim.

## PR-100: Add Point-In-Time Fed Reference Inputs

PR name: `fed-policy-point-in-time-references`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-100/fed-policy-point-in-time-references`
Git status: `not-started (branch absent)`
Agent lane: Fed reference-data causality; one agent only
Depends on: PR-97
Commit: `feat(pr-100): persist point in time fed references`
Design patterns: Repository, Adapter, Specification/Policy Object.

Description:
- R1: Persist EFFR as a point-in-time reference with activity date, value, source, and actual/derived publication availability; historical feature construction must select only the latest EFFR publication known by the feature cutoff, never a value published later.
- R2: Persist an FOMC meeting-schedule vintage ledger with decision date, meeting type, source, and first-known/announced availability so scheduled changes and emergency meetings become eligible only after public announcement.
- R3: Persist the effective Federal Funds target-range history needed to express meeting outcomes relative to the current policy baseline, with exact effective/known-at semantics and no future-effective target leaking backward.
- R4: Add bounded incremental refresh plus explicit historical reconcile for all three reference families; source revisions are retained deterministically and provenance remains auditable.
- R5: Add causal fixtures covering ordinary scheduled meetings, a schedule revision, a weekend/holiday EFFR publication boundary, and the 2020 emergency-meeting case.

Acceptance:
- A1 (verifies R1): an observation cutoff before an EFFR publication cannot see that EFFR value, while the same query after publication can; no same-day historical table lookup bypasses publication time.
- A2 (verifies R2): a future scheduled/emergency meeting is absent before its recorded first-known time and present afterward; tests fail if the final ex-post calendar is treated as if it had always been known.
- A3 (verifies R3): hand-calculable target-range transitions return the exact contemporaneous baseline and never apply a later decision before its effective/known time.
- A4 (verifies R4): repeated incremental refresh is idempotent, historical reconcile is explicit, and every retained row has deterministic source/availability lineage.
- A5 (verifies R5): all listed edge fixtures pass causally and a deliberately future-leaking implementation makes the suite fail.

## PR-101: Implement Versioned CME FedWatch Probability Reconstruction

PR name: `fed-policy-probability-engine`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-101/fed-policy-probability-engine`
Git status: `not-started (branch absent)`
Agent lane: Probability reconstruction; one agent only
Depends on: PR-99, PR-100
Commit: `feat(pr-101): reconstruct cme fedwatch probabilities`
Design patterns: Pure Transformation, Specification/Policy Object, Value Object.

Description:
- R1: Replace the legacy simplified per-meeting `_outcomes` approximation with one pure, network-free, source-controlled probability engine implementing the documented CME FedWatch construction from the contemporaneous ZQ settlement curve and point-in-time Fed references.
- R2: Produce normalized outcome distributions for each known future meeting needed through at least the third meeting, retaining `observation_date`, `meeting_date`, target/move bucket, probability, `available_at_utc`, and an explicit methodology version.
- R3: Enforce probability mass, finite-value, quarter-point-grid/bucket, month-day weighting, meeting-month boundary, and ordering invariants; impossible/insufficient curves return explicit unavailable results rather than guessed probabilities.
- R4: Add deterministic worked-example tests for no-change, one-step, multi-step, late-month meeting, multiple consecutive meetings, zero-lower-bound-era, and hiking-cycle cases, plus methodology-version fingerprint tests.

Acceptance:
- A1 (verifies R1): production probability calculation has exactly one authoritative algorithm and repository search finds no active call path using the old simplified `_outcomes` implementation as the production oracle.
- A2 (verifies R2): fixtures yield one deterministic normalized probability tree per observation/meeting with exact availability and methodology metadata and enough future meetings to construct M3 slope when source inputs permit.
- A3 (verifies R3): every valid meeting distribution sums to one within the declared tolerance, invalid curves fail closed, and edge fixtures prove month-weighting/boundary logic rather than a calendar-insensitive shortcut.
- A4 (verifies R4): all worked examples match hand-calculated expected probabilities and any formula/version change breaks the pinned expected fingerprint/tests.

## PR-102: Build The Four Canonical Fed Policy Features

PR name: `fed-policy-four-feature-builder`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-102/fed-policy-four-feature-builder`
Git status: `not-started (branch absent)`
Agent lane: Fed feature transformation; one agent only
Depends on: PR-101
Commit: `feat(pr-102): build canonical fed policy features`
Design patterns: Pure Transformation, Value Object, Repository.

Description:
- R1: Build exactly `fed_next_expected_move_bp`, `fed_path_slope_m3_bp`, `fed_next_uncertainty_bp`, and `fed_repricing_5obs_bp` from PR-101 probability snapshots using the PR-97 formulas and basis-point units.
- R2: Define M3 path slope as expected target/move at the third future meeting minus expected target/move at the next meeting; do not sum already-cumulative meeting moves and do not reintroduce `fed_m3_expected_move_bp`.
- R3: Compute repricing against the fifth previous valid feature observation, not five calendar days; a revised source date must deterministically recompute every current/future row whose five-observation dependency includes the revision.
- R4: Publish a canonical local `fed_policy_features_daily` dataset with unique increasing `timestamp_m1`, the four nullable Float64 features, explicit `available_at_utc`/lineage sidecar metadata, no fill/interpolation/carry, no NaN/infinity, and deterministic rebuild semantics.
- R5: Add hand-calculable unit/integration fixtures for mean, population uncertainty, path slope, first-five-observation warm-up, gaps, revisions, missing third meeting, and exact availability propagation.

Acceptance:
- A1 (verifies R1): exact toy probability trees reproduce all four hand-calculated feature values and the output contract contains no fifth unapproved numeric Fed feature.
- A2 (verifies R2): a three-meeting fixture with cumulative expected moves of +25/+50/+50 bp yields path slope +25 bp from meeting 1 to meeting 3 rather than +125 bp or another cumulative-sum artifact.
- A3 (verifies R3): irregular-date fixtures use the fifth prior valid observation exactly, and revising one observation updates precisely the directly affected feature row(s) and downstream five-observation dependency row(s).
- A4 (verifies R4): canonical output is unique/sorted/finite-or-NULL, contains no implicit fill, preserves explicit availability lineage, and deterministic rebuild reproduces the same semantic dataset.
- A5 (verifies R5): every listed edge case has a focused assertion and a deliberately calendar-lagged, future-leaking, or cumulative-sum implementation fails tests.

## PR-103: Integrate Fed Policy Features Into PostgreSQL Macro Library

PR name: `fed-policy-postgres-integration`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-103/fed-policy-postgres-integration`
Git status: `not-started (branch absent)`
Agent lane: PostgreSQL feature integration; one agent only
Depends on: PR-102
Commit: `feat(pr-103): publish fed policy features to postgres`
Design patterns: Versioned Migration, Materialized View, Unit of Work, Repository.

Description:
- R1: Add a versioned migration and repository for a private synchronized Fed-policy daily source relation outside the downstream feature-discovery schema (for example `macro_loader_sync.fed_policy_features_daily`) containing `timestamp_m1` plus exactly the four Float64 features and required sync lineage.
- R2: Add deterministic incremental PostgreSQL synchronization keyed by `timestamp_m1`, with exact insert/update/delete planning against canonical local Fed-policy output, digest/state reconciliation, idempotent replay, and fail-closed schema/version checks.
- R3: Extend the explicit `macro_features` catalog/materialized-view contract to LEFT JOIN the four Fed-policy columns by `timestamp_m1`, advance only the appropriate feature-view version/fingerprint, and keep canonical raw Gold/`macro_raw` unchanged.
- R4: Refresh `macro_loader.macro_features` exactly once inside the locked transaction when Fed-policy sync has a semantic mutation and zero times on a true no-op; refresh or verification failure must roll back Fed-policy rows/state and preserve the last committed view.
- R5: Add clean-create/in-place-upgrade integration tests for exact schema/order/types, owner/grants, version/fingerprint, join semantics, missing-date NULL behavior, mutation/no-op counts, rollback, and unrelated-schema preservation.

Acceptance:
- A1 (verifies R1): PostgreSQL introspection shows the private source relation with the exact contract and confirms it is not accidentally exposed as an independent downstream feature relation.
- A2 (verifies R2): insert/update/delete/no-op fixtures reconcile source, consumer rows, hashes/state, and deterministic mutation counts exactly; incompatible schema/version fails before mutation.
- A3 (verifies R3): `macro_features` exposes the four columns exactly once in deterministic order, raw Gold/`macro_raw` hashes are unchanged, missing Fed dates remain NULL, and the view version/fingerprint advances exactly as specified.
- A4 (verifies R4): many Fed row mutations cause one view refresh, unchanged replay causes zero, and injected refresh/verification failure leaves the pre-transaction PostgreSQL state byte/semantically unchanged.
- A5 (verifies R5): clean and upgraded real-PostgreSQL fixtures converge to the same normalized contract/grants/definition and no unrelated object changes.

## PR-104: Add Fed Policy Bootstrap Reconcile And EOD Update Commands

PR name: `fed-policy-eod-orchestration`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-104/fed-policy-eod-orchestration`
Git status: `not-started (branch absent)`
Agent lane: Application orchestration; one agent only
Depends on: PR-103
Commit: `feat(pr-104): orchestrate fed policy eod updates`
Design patterns: Command, Strategy, Unit of Work, Dependency Injection.

Description:
- R1: Add explicit Fed-policy `bootstrap`, `update`, and `reconcile` application/CLI modes plus a `run-fed-policy-eod` command; bootstrap/reconcile may build requested history, while normal EOD execution must select `update` only.
- R2: Make `update` derive its provider request window from the newest durable ZQ settlement with the seven-calendar-day overlap and refresh only point-in-time reference/input ranges needed for that delta; it must never auto-reconcile history.
- R3: Recompute probability/features from the earliest changed observation through the minimum causal downstream range required to keep the five-valid-observation repricing dependency correct, without re-downloading unchanged 2010-present ZQ history.
- R4: Publish local canonical Fed-policy output atomically before PostgreSQL synchronization is attempted; provider/transform/publication failure must leave the previous local canonical dataset/selectable state intact.
- R5: Add orchestration tests for first bootstrap, bounded daily delta, recent revision, missed several-day run, weekend/holiday no-data run, unchanged replay, and provider/transformation failure.

Acceptance:
- A1 (verifies R1): CLI help/contracts expose the three explicit modes and `run-fed-policy-eod`; code search proves the daily command cannot choose reconcile implicitly.
- A2 (verifies R2): a 2010..2026 durable-history fixture requests only the seven-day recent overlap during update and never issues a full-history provider call.
- A3 (verifies R3): a recent revision recomputes all and only the causal feature rows needed for direct values plus five-observation repricing consequences, while unrelated historical raw partitions remain untouched.
- A4 (verifies R4): injected provider/build/publish failures preserve the previously selectable local dataset and do not start PostgreSQL mutation.
- A5 (verifies R5): every listed operational scenario has deterministic mutation/request counts and exact expected terminal state.

## PR-105: Install Daily EOD Fed Policy Cron Chain

PR name: `fed-policy-daily-cron`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-105/fed-policy-daily-cron`
Git status: `not-started (branch absent)`
Agent lane: Operations/crontab; one agent only
Depends on: PR-104
Commit: `feat(pr-105): install fed policy daily cron chain`
Design patterns: Command, Single-Instance Lock, Fail-Closed Verification.

Description:
- R1: Add a source-controlled daily crontab entry and wrapper that runs after the prior U.S. futures EOD settlement publication with an explicit scheduler timezone (default operational target `Europe/Vienna`, daily at 10:15 local time unless deployment configuration intentionally overrides it), deterministic cwd/Git identity, lock, environment export, and log path.
- R2: Make the wrapper execute exactly `run-fed-policy-eod -> fed-policy-sync-postgres -> postgres-verify` on success; it must not run migrations, historical reconcile, browser FedWatch scraping, or the weekly full macro reconcile path.
- R3: Treat weekends/holidays/no-new-settlement days as successful semantic no-ops, while the seven-day overlap automatically recovers missed daily executions without a special operator path.
- R4: Enforce runtime-vs-admin PostgreSQL role separation, single-instance exclusion, bounded source failure behavior, non-zero exit propagation, and no secret/DSN logging.
- R5: Add hermetic shell/command-trace tests for exact stage order, lock contention, cwd/timezone/logging, no-op behavior, missed-run recovery, and early-stage/postgres-stage failure propagation.

Acceptance:
- A1 (verifies R1): the installed/source-controlled cron contract names an explicit timezone and time, invokes the exact wrapper with deterministic cwd/config/lock/log settings, and is late enough for the intended completed EOD source date.
- A2 (verifies R2): command trace contains exactly the declared three-stage chain and contains no migration/reconcile/browser/manual-SQL duplicate path.
- A3 (verifies R3): weekend/holiday fixtures succeed with zero semantic mutations and a multi-day missed-run fixture catches up through bounded delta requests only.
- A4 (verifies R4): overlapping invocation is rejected, runtime role cannot perform admin DDL, failures return non-zero, and logs/artifacts contain no credentials.
- A5 (verifies R5): all operational traces are deterministic and a deliberately reordered/hidden full-history command causes the suite to fail.

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
Depends on: PR-103, PR-106
Commit: `test(pr-107): validate fed policy postgres integration`
Design patterns: End-to-End Test, Reconciliation, Fail-Closed Verification.

Description:
- R1: Add a real-PostgreSQL suite for clean bootstrap and upgrade from the current production-shaped schema through migrations, Fed-policy sync, materialized-view refresh, independent conformance verification, and migration rerun.
- R2: Verify exact private-source and public `macro_features` schemas, ordered four-column exposure, view version/fingerprint, finite-or-NULL values, timestamp uniqueness, ownership/grants, and downstream read-only consumer compatibility.
- R3: Exercise insert, historical revision/update, explicit source-row removal during reconcile, mixed delta, and immediate no-op replay; after each case source/consumer/hash/state/view reconciliation must be exact.
- R4: Inject raw private-table tamper, view-definition/schema drift, owner/grant drift, digest/state drift, and refresh failure; every case must fail closed and preserve/recover through the documented migrate/sync path.
- R5: Emit deterministic PostgreSQL QA evidence with mutation/refresh counts, normalized object definitions, schema/version/fingerprint, conformance result, and PASS|FAIL without credentials.

Acceptance:
- A1 (verifies R1): clean and upgrade paths converge to the same PostgreSQL contract, migration rerun is a no-op, and independent verification passes only after valid Fed synchronization/refresh.
- A2 (verifies R2): exact introspection/query assertions cover every required column/type/grant/version property and a regime-engine-shaped read sees the four features through the supported macro feature plane.
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
Depends on: PR-99, PR-100, PR-102, PR-107
Commit: `test(pr-108): execute fed policy history acceptance`
Design patterns: End-to-End Acceptance, Reconciliation, Fail-Closed Verification.

Description:
- R1: In an explicitly authorized environment, execute the real historical Fed-policy reconcile from requested start `2010-01-01` through the current completed source date using the persistent lake and real free/public provider configuration, then synchronize PostgreSQL and run independent conformance verification.
- R2: Prove source coverage from the first eligible 2010 trading date (expected 2010-01-04, verified rather than assumed) through the present with exact settlement-curve/date counts and an explicit unexplained-gap report; unavailable public history must make this acceptance FAIL rather than create synthetic observations.
- R3: Verify the four-feature history is unique, ordered, finite-or-NULL, causal, and populated whenever sufficient source/three-meeting support exists; perform exact/independent spot checks across at least 2010, the 2015 liftoff period, 2020 emergency policy, the 2022 hiking cycle, and a recent period.
- R4: Query real PostgreSQL as the downstream consumer and verify min/max timestamps, exact four columns in `macro_features`, version/fingerprint, row/value parity with canonical local output, and no unintended changes to raw Gold/`macro_raw`.
- R5: Immediately run normal EOD update and PostgreSQL sync unchanged; require bounded recent source requests, zero semantic data mutations, zero view refresh, and identical relevant fingerprints.
- R6: Commit only a deterministic sanitized `artifacts/acceptance/fed-policy-history-v1.json` with source identities, min/max/count/gaps, methodology/version hashes, spot-check evidence, PostgreSQL parity, replay counts, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): every declared real stage executes in order against the intended persistent lake/PostgreSQL endpoint and independent verification runs after sync.
- A2 (verifies R2): the artifact proves the actual first eligible 2010 date and exact continuous/explicit-gap coverage; any unresolved required-history gap prevents PASS and is not filled.
- A3 (verifies R3): all four feature semantics pass independent spot checks over the listed policy regimes with availability evidence and no NaN/infinity/look-ahead.
- A4 (verifies R4): downstream PostgreSQL values/timestamps/version/fingerprint match canonical local output exactly within declared numeric tolerances while raw Gold/`macro_raw` remain unchanged.
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
Depends on: PR-105, PR-107, PR-108
Commit: `test(pr-109): execute fed policy cron acceptance`
Design patterns: End-to-End Acceptance, Command, Single-Instance Lock, Failure Injection.

Description:
- R1: Execute the exact installed/source-controlled daily cron wrapper under the real scheduler/service-account configuration, working directory, timezone, lock, persistent lake, logs, and PostgreSQL roles; direct Python substitution is forbidden for this acceptance.
- R2: Demonstrate a run with one or more newly available/revised ZQ observations produces the exact local feature delta, exact PostgreSQL mutations, exactly one materialized-view refresh, and conformant downstream output; an immediate rerun must produce zero semantic mutations and zero refreshes.
- R3: Demonstrate recovery after intentionally skipping multiple scheduled runs and demonstrate weekend/holiday execution; catch-up must use bounded overlap/delta requests only and no-data runs must be successful no-ops.
- R4: Inject CME/source unavailability before local publication and PostgreSQL/refresh failure after local publication separately; the first must prevent PostgreSQL mutation, the second must return non-zero and preserve the last committed PostgreSQL/view state for safe retry.
- R5: Verify lock contention, cwd/Git identity, scheduler timezone/time, log path, exit-code propagation, runtime/admin privilege separation, and secret redaction using the installed execution path.
- R6: Commit only a deterministic sanitized `artifacts/acceptance/fed-policy-cron-v1.json` with run/source dates, request bounds, mutation/refresh counts, failure-injection outcomes, operational assertions, conformance, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): evidence identifies the exact cron wrapper/service identity/config/cwd/timezone/lock/log path used and proves no substitute execution path was used.
- A2 (verifies R2): mutation run records the exact expected row changes plus one refresh and immediate rerun records exactly zero semantic changes/refreshes with identical downstream fingerprint.
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
Depends on: PR-106, PR-107, PR-108, PR-109
Commit: `refactor(pr-110): retire legacy fedwatch runtime path`
Design patterns: Single Source of Truth, Ports and Adapters.

Description:
- R1: Remove/deactivate legacy production wiring that can source model features directly from browser-only CME FedWatch exports or the simplified legacy `_outcomes` approximation; retain any browser export adapter only under an explicit QA boundary required by PR-106.
- R2: Remove/rename stale `fed_m3_expected_move_bp` contracts/tests/docs and any dead Fed-policy pipeline injection left behind by the pre-PR-97 architecture, without deleting canonical historical snapshots/evidence needed for audit.
- R3: Update README, ARCHITECTURE, BACKLOG status, operational instructions, data-source/availability semantics, semantic/view versions, and cron documentation to the verified post-PR-109 state; do not claim historical/production PASS beyond the committed acceptance artifacts.
- R4: Add repository-wide reference/import/startup checks proving one authoritative Fed-policy runtime path, all required QA paths remain available, no stale browser production dependency survives, and the complete required quality gate remains green.

Acceptance:
- A1 (verifies R1): production call-path/code search finds no browser FedWatch or simplified `_outcomes` feature source, while PR-106 QA fixtures/tools remain explicitly isolated and functional.
- A2 (verifies R2): `fed_m3_expected_move_bp` is absent from the public/runtime contract, no dead injected Fed-policy source remains in daily orchestration, and audit evidence/history is preserved.
- A3 (verifies R3): all four sidecars/ops docs agree on source, formulas, point-in-time availability, 2010 coverage evidence, PostgreSQL publication, daily cron, and current versions with no unverified success claims.
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

The active delivery program now begins with PR-97 above. Completed scopes remain historical context and
must not be reopened or silently rewritten to implement PR-97+ work.
