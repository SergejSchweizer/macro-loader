# Backlog

This backlog is the implementation source of truth for `macro-loader`.

The repository loads reusable daily market-state inputs from open/public sources, preserves source history, performs strict incremental updates during normal execution, and publishes deterministic immutable Gold feature snapshots through a Bronze -> Silver -> Gold architecture.

Last reviewed: 2026-09-19

## Current repository and production status

`main` is aligned with `origin/main` as of 2026-09-19. PR-40 through PR-61 and the
subsequent feature and PostgreSQL hardening PRs are merged into `main`; entries below
remain the historical delivery record. The Sunday runner has completed successfully
through Gold publication and PostgreSQL synchronization. The verified serving table has
16,775 rows, with 20, 20, 20, and 15 non-null values in the four FedWatch columns.
Production FedWatch acquisition is restricted to the Chinese CME page
(`https://www.cmegroup.cn/fed-watch/`) and downloads every available meeting export.
Unavailable ECB responses remain NULL rather than failing the complete batch. Current
Gold versions are `schema_version = 6` and `feature_version = 5`.

## Active Delivery Program — Materialized Macro Feature Library

The next delivery wave keeps the complete 13-series raw history and turns
`macro_loader.macro_features` into the authoritative, broad, causal feature library consumed
by downstream model repositories. `macro-loader` owns reproducible feature construction;
`regime-engine` owns model-specific feature selection, train-fold scaling, HMM fitting, state
mapping, and portfolio/regime decisions. The materialized view is intentionally wider than any
single HMM observation set.

No PR in this program may delete retained Bronze/Silver history merely because a feature is not
selected by `regime-engine`. The current raw serving table `macro_loader.macro_raw` remains a
rebuildable PostgreSQL replica of canonical raw Gold levels. The feature library must preserve the
existing causal transformations from `application/volatility_features.py`,
`application/macro_features.py`, `application/momentum_features.py`, and
`application/return_features.py` until PostgreSQL parity is proven and the duplicate Python
runtime implementations can be retired safely.

## PR-80: Freeze Materialized Feature-Library Ownership And Delivery Contract

PR name: `feature-library-backlog-program`
Status: Merged
Updated: 2026-09-19
PR: #81
Git branch: `pr-80/feature-library-backlog-program`
Git status: `merged`
Agent lane: Architecture/backlog contract; one agent only
Depends on: none
Commit: `docs(pr-80): define materialized feature library delivery program`
Design patterns: Specification/Policy Object, Materialized View, Ports and Adapters.

Description:
- R1: Define the durable ownership boundary: Bronze/Silver/Gold and `macro_raw` preserve canonical raw source levels; `macro_features` constructs the reusable causal feature library; `regime-engine` alone selects the subset used by each HMM and fits any train-only scaler.
- R2: Freeze all 13 currently registered source series as retained raw inputs for this program; specifically, do not remove VIX6M, VIX1Y, or any already-retained historical Bronze/Silver data merely because they may be redundant for one HMM.
- R3: Define the feature-library families that must survive the migration: raw levels; 1/5/20-valid-observation deltas where currently specified; 60-observation z-scores; existing VIX term-structure ratios/spreads; US 10Y-minus-2Y; positive momentum autocorrelations at (1,60), (5,60), and (20,120); and geometric-return features at 10/25/60/120/240 observations.
- R4: Define the atomic delivery/QA dependency chain PR-81 through PR-90, require exact Rn-to-An acceptance mapping, and require every implementation PR to update README/ARCHITECTURE sidecars when its public contract changes.

Acceptance:
- A1 (verifies R1): BACKLOG explicitly states that `macro-loader` generates the broad feature library while `regime-engine` performs model-specific feature selection/scaling; neither responsibility can be reasonably read as belonging to both repositories.
- A2 (verifies R2): the planned program contains no deletion of the 13 canonical raw series or retained source history and explicitly preserves VIX6M/VIX1Y as available inputs.
- A3 (verifies R3): every currently supported transformation family is named with its exact causal horizons/semantics and is assigned to the materialized-view migration before any duplicate Python computation is removed.
- A4 (verifies R4): PR-81 through PR-90 each declare branch, Git status, dependency, commit, design pattern, matched Description/Acceptance counts, and dedicated QA PRs include both a full-run and cron-run acceptance stage.

## PR-81: Define Executable Macro Feature Catalog And View Version

PR name: `macro-feature-catalog-contract`
Status: Merged
Updated: 2026-09-19
PR: #82
Git branch: `pr-81/macro-feature-catalog-contract`
Git status: `merged`
Agent lane: Feature contract; one agent only
Depends on: PR-80
Commit: `feat(pr-81): define macro feature catalog contract`
Design patterns: Specification/Policy Object, Value Object.

Description:
- R1: Add one source-controlled executable catalog for the materialized-view contract containing the explicit ordered raw source groups, exact ordered output columns, feature-family membership, and per-feature semantic metadata; it must not discover features by scanning for arbitrary `*_level` columns at runtime.
- R2: Make all 13 raw levels first-class columns in `macro_loader.macro_features` in addition to the derived feature families, so a downstream consumer can use levels and transformations through one stable view without joining `macro_raw`.
- R3: Introduce a source-controlled `MACRO_FEATURE_VIEW_VERSION` and deterministic contract fingerprint derived from ordered output names plus formula parameters; this version/fingerprint is independent of raw Gold schema/feature versions.
- R4: Add unit tests proving deterministic ordering, no duplicate names, complete coverage of all 13 source levels, exact family membership, stable fingerprinting, and rejection of accidental wildcard/dynamic-column expansion.

Acceptance:
- A1 (verifies R1): the catalog enumerates every permitted output/family explicitly, adding an unrelated future `foo_level` raw column cannot change the feature view without a source-code contract change, and no production catalog builder uses information-schema wildcard discovery.
- A2 (verifies R2): the expected view schema contains `timestamp_m1`, all 13 `<series>_level` columns exactly once, and the complete derived library in deterministic order.
- A3 (verifies R3): identical contracts reproduce the same version/fingerprint; any formula parameter, column name, order, or family change changes the fingerprint and requires an explicit version update.
- A4 (verifies R4): focused tests fail on duplicate/missing/reordered columns, missing source coverage, hidden wildcard expansion, and stale expected fingerprints.

## PR-82: Make Macro Features A Versioned PostgreSQL Materialized View

PR name: `versioned-macro-features-view`
Status: Merged
Updated: 2026-09-19
PR: #83
Git branch: `pr-82/versioned-macro-features-view`
Git status: `merged`
Agent lane: PostgreSQL feature materialization; one agent only
Depends on: PR-81
Commit: `feat(pr-82): materialize canonical macro feature library`
Design patterns: Materialized View, Versioned Migration, Specification/Policy Object.

Description:
- R1: Create `macro_loader.macro_features` through the ordered/idempotent `postgres-migrate` path, not an operator-run side script, while preserving all historical migration-ledger versions already applied in production.
- R2: Materialize the complete legacy feature semantics from the four Python feature modules: source levels; supported 1/5/20-observation deltas; 60-observation population z-scores; existing VIX ratios/spreads; US 10Y-minus-2Y; positive momentum autocorrelations at (1,60), (5,60), (20,120); and 10/25/60/120/240-observation geometric-return features.
- R3: Compute rolling/lagged features over all available `macro_raw` history first and only then expose rows from `2010-01-01` onward, so pre-2010 observations may supply causal warm-up without appearing as consumer rows.
- R4: Preserve observation-count semantics and causality exactly: lag/rolling windows count prior valid observations of the relevant source, no forward/back fill or interpolation is introduced, cross-series ratios/spreads require the same `timestamp_m1`, invalid denominators/non-positive return transitions remain NULL, negative momentum autocorrelation is clipped to zero, and no NaN/infinity reaches the view.
- R5: Set deterministic owner/grants for the materialized view and make a clean database plus an already-migrated production-shaped database converge to the same exact view definition and column contract.

Acceptance:
- A1 (verifies R1): a clean `postgres-migrate` creates the real populated-view definition without running `scripts/create_macro_features_view.sql`; rerunning migration is idempotent and existing ledger rows are never renumbered or rewritten.
- A2 (verifies R2): exact schema assertions prove every legacy feature family/formula currently produced by the four Python modules is present in the materialized view together with all 13 raw levels.
- A3 (verifies R3): a fixture with sufficient pre-2010 history produces non-null eligible rolling features on early-2010 rows while no output row predates 2010-01-01.
- A4 (verifies R4): hand-calculable gap/null/zero/negative/cross-series fixtures match the causal valid-observation formulas exactly and prove absence of fill, future leakage, NaN, and infinity.
- A5 (verifies R5): clean-create and in-place-upgrade PostgreSQL integration tests yield byte-for-byte equivalent normalized view definitions, exact ordered columns/types, expected ownership, and least-privilege SELECT grants.

## PR-83: Add Regime-Oriented VIX Curve And Dollar Change Features

PR name: `regime-oriented-derived-features`
Status: Merged
Updated: 2026-09-19
PR: #84
Git branch: `pr-83/regime-oriented-derived-features`
Git status: `merged`
Agent lane: Feature formulas; one agent only
Depends on: PR-82
Commit: `feat(pr-83): add regime oriented derived features`
Design patterns: Specification/Policy Object, Pure Transformation.

Description:
- R1: Add `vix9d_vix3m_log_ratio = ln(vix9d_level / vix3m_level)` using same-timestamp positive observations only; retain all existing VIX term-structure ratios/spreads rather than replacing them.
- R2: Add `usd_broad_log_return_20obs = ln(usd_broad_level(t) / usd_broad_level(t-20 valid observations))` using source-valid-observation lag semantics rather than calendar-day lagging.
- R3: Append both features to the explicit catalog/view schema, advance only the materialized-view contract version/fingerprint required by PR-81, and leave raw Gold/`macro_raw` schema and all source histories unchanged.
- R4: Add hand-calculable SQL/unit integration fixtures for exact values, warm-up NULLs, non-positive-input NULLs, missing-date observation counting, and deterministic column order/fingerprint changes.

Acceptance:
- A1 (verifies R1): exact fixtures prove the log-ratio formula, same-timestamp requirement, positivity guard, retained legacy term-structure columns, and NULL rather than invalid numeric output.
- A2 (verifies R2): an irregular-date fixture proves the 20th prior valid USD observation is used exactly and differs from a 20-calendar-day implementation.
- A3 (verifies R3): raw Gold and `macro_raw` column contracts/digests are unchanged while the materialized-view version/fingerprint advances exactly once and exposes both new columns.
- A4 (verifies R4): focused tests fail on off-by-one lags, calendar lagging, reordered columns, stale fingerprints, non-positive log inputs, or any NaN/infinity.

## PR-84: Refresh Macro Features Exactly Once Per Successful Raw Sync

PR name: `macro-feature-refresh-uow`
Status: Merged
Updated: 2026-09-19
PR: #85
Git branch: `pr-84/macro-feature-refresh-uow`
Git status: `merged`
Agent lane: PostgreSQL synchronization lifecycle; one agent only
Depends on: PR-82, PR-83
Commit: `refactor(pr-84): refresh macro features in sync transaction`
Design patterns: Unit of Work, Materialized View, Repository.

Description:
- R1: Remove the statement-level `macro_raw` refresh trigger and its trigger function so row-by-row INSERT/UPDATE/DELETE execution can never cause repeated full materialized-view refreshes during one sync.
- R2: Refresh `macro_loader.macro_features` exactly once inside the locked PostgreSQL sync Unit of Work after all planned raw-row/digest mutations and before final success commit when and only when the delta contains at least one insert, update, or delete.
- R3: On a true zero-mutation replay, do not refresh the materialized view and do not change raw rows, digests, sync-state semantics, or view contents.
- R4: Treat materialized-view refresh failure as sync failure: roll back raw-row mutations, digest changes, sync-state advancement, and view replacement atomically; preserve the previously committed serving/view state.
- R5: Add repository/integration traces proving lock -> read target -> plan -> raw mutation -> digest mutation -> single view refresh -> verification/state -> commit ordering and proving no hidden refresh path remains.

Acceptance:
- A1 (verifies R1): schema introspection proves no refresh trigger/function remains attached to `macro_raw`, and DML statement count cannot multiply refresh executions.
- A2 (verifies R2): mutation fixtures with many inserted/updated/deleted rows observe exactly one materialized-view refresh before commit under the existing advisory lock.
- A3 (verifies R3): an unchanged sync reports zero mutations, performs zero refreshes, preserves all row/view fingerprints, and remains idempotent.
- A4 (verifies R4): injected refresh failure leaves consumer rows, hashes, sync state, and visible materialized-view contents identical to the pre-transaction state.
- A5 (verifies R5): deterministic event/SQL traces match the required order and a repository-wide test/code search finds no second runtime refresh trigger or operator-only refresh dependency.

## PR-85: Enforce Macro Feature View Schema And Migration Conformance

PR name: `macro-feature-view-conformance`
Status: Merged
Updated: 2026-09-19
PR: #86
Git branch: `pr-85/macro-feature-view-conformance`
Git status: `merged`
Agent lane: PostgreSQL conformance; one agent only
Depends on: PR-84
Commit: `feat(pr-85): verify macro feature view conformance`
Design patterns: Fail-Closed Verification, Specification/Policy Object, Versioned Migration.

Description:
- R1: Extend PostgreSQL preflight/conformance verification to inspect `macro_loader.macro_features` as a first-class owned serving object: materialized-view kind, exact ordered columns/types/nullability, owner, grants, contract version/fingerprint, and normalized definition.
- R2: Replace the current placeholder/empty-view and manual-rebuild serving assumptions with one forward migration that supersedes them without deleting or renumbering historical migration-ledger entries; remove `scripts/create_macro_features_view.sql` after its behavior is fully represented by migrations.
- R3: Fail closed before runtime raw mutation when the view is missing, stale, has an unexpected extra/missing/reordered column, wrong type/owner/grant, wrong version/fingerprint, or a definition not matching the source-controlled contract.
- R4: Add clean-create, production-upgrade, deliberate-drift, and migration-rerun tests using real PostgreSQL; unrelated schemas/materialized views must remain untouched.

Acceptance:
- A1 (verifies R1): independent introspection returns an exact match for view kind/schema/order/types/owner/grants/version/fingerprint/definition and any individual drift prevents PASS.
- A2 (verifies R2): the current production migration ledger upgrades forward without history rewrite, the manual SQL script is removed, and no operational documentation instructs operators to recreate the view manually.
- A3 (verifies R3): each deliberate missing/extra/reordered/type/owner/grant/version/definition drift fixture fails before INSERT/UPDATE/DELETE against `macro_raw`.
- A4 (verifies R4): clean and upgraded databases converge to the same contract, migration rerun is a no-op, and catalog comparison proves no unrelated PostgreSQL object changed.

## PR-86: Prove Python-To-PostgreSQL Feature Formula Parity

PR name: `macro-feature-formula-parity-qa`
Status: In Progress
Updated: 2026-09-19
PR: #87
Git branch: `pr-86/macro-feature-formula-parity-qa`
Git status: `active-clean`
Agent lane: Feature QA; one agent only
Depends on: PR-85
Commit: `test(pr-86): prove macro feature formula parity`
Design patterns: Golden Master, Differential Testing, Test Fixture.

Description:
- R1: Build deterministic fixtures that feed identical source observations to the existing Python feature functions and to real PostgreSQL `macro_features`, then compare every overlapping legacy derived column using exact equality where algebraically exact and one explicitly documented numeric tolerance where database floating arithmetic can differ.
- R2: Cover first-valid-observation and warm-up boundaries, irregular/missing source dates, zero-variance z-scores, ratio denominator guards, non-positive return inputs, negative/undefined autocorrelation, and long-window 120/240-observation boundaries.
- R3: Cover cross-series timestamp alignment explicitly: unmatched dates must not be carried forward and cross-series ratios/spreads are populated only when both required source values exist on the same timestamp.
- R4: Validate the two PR-83 additions independently against hand-calculated expected values rather than treating either Python or SQL as the oracle.
- R5: Emit a deterministic parity report naming every compared feature family/column and fail if any expected legacy feature lacks a PostgreSQL counterpart.

Acceptance:
- A1 (verifies R1): all legacy Python-vs-SQL columns pass the declared exact/tolerance comparison on deterministic fixtures, and a deliberately altered SQL formula makes the suite fail.
- A2 (verifies R2): boundary fixtures exercise every listed warm-up/null/guard case and assert the exact first timestamp at which each affected feature may become non-null.
- A3 (verifies R3): deliberately staggered source calendars prove there is no implicit as-of/forward fill in any cross-series feature.
- A4 (verifies R4): exact manual calculations for VIX9D/VIX3M log-ratio and USD 20-valid-observation log return match PostgreSQL output and fail on off-by-one alternatives.
- A5 (verifies R5): the report covers 100% of catalogued legacy derived columns and the test fails on missing, renamed, silently dropped, or extra unapproved catalog members.

## PR-87: Remove Duplicate Python Feature Computation Legacy

PR name: `remove-duplicate-python-feature-runtime`
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-87/remove-duplicate-python-feature-runtime`
Git status: `not-started (branch absent)`
Agent lane: Feature cleanup; one agent only
Depends on: PR-86
Commit: `refactor(pr-87): remove duplicate python feature runtime`
Design patterns: Single Source of Truth, Specification/Policy Object.

Description:
- R1: After PR-86 parity is green, remove the duplicate numeric runtime implementations in `application/volatility_features.py`, `application/macro_features.py`, `application/momentum_features.py`, and `application/return_features.py`; preserve their feature semantics exclusively in the source-controlled catalog plus PostgreSQL materialized-view definition/tests.
- R2: Remove dead imports/constants/feature-frame assembly code and obsolete Python-only tests that no longer exercise production behavior, replacing them with catalog/MV tests rather than weakening coverage.
- R3: Keep canonical Gold and `macro_raw` raw-level-only, keep all 13 registered source series/history, and prove this cleanup changes no raw data, raw schema, Gold bundle digest for identical inputs, or materialized-view output.
- R4: Remove any now-unused package dependency or helper only when repository-wide import/reference tests prove it has no remaining production/test consumer; do not remove unrelated provider functionality.

Acceptance:
- A1 (verifies R1): the four duplicate computation modules no longer contain executable feature math and repository search identifies exactly one production numerical implementation for the feature library: the versioned PostgreSQL view definition.
- A2 (verifies R2): replacement tests preserve or increase required production coverage, all catalog/MV formula tests remain green, and no deleted test was the sole coverage of still-live behavior.
- A3 (verifies R3): fixed raw-input fixtures produce identical canonical Gold/`macro_raw` rows and identical `macro_features` rows before versus after the cleanup.
- A4 (verifies R4): dependency/import checks prove every removed helper/dependency is unreachable, while all providers, ingestion commands, Gold publication, and PostgreSQL sync commands still import/start successfully.

## PR-88: Run Complete Real-PostgreSQL Feature-Library QA

PR name: `macro-feature-real-postgres-qa`
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-88/macro-feature-real-postgres-qa`
Git status: `not-started (branch absent)`
Agent lane: Integration QA; one agent only
Depends on: PR-87
Commit: `test(pr-88): validate macro feature library on postgres`
Design patterns: End-to-End Test, Fail-Closed Verification, Reconciliation.

Description:
- R1: Add a real-PostgreSQL integration suite for both clean bootstrap and upgrade from the current pre-program production-shaped schema, including full migration, raw sync, materialized-view refresh, independent conformance verification, and migration rerun.
- R2: Verify exact source/raw/view temporal coverage from 2010 onward, all 13 raw levels in the view, every catalogued derived feature column, deterministic ordering/types, and expected warm-up/null behavior without requiring every feature to be non-null on every date.
- R3: Verify mutation cases for insert, historical revision/update, serving-row delete, and mixed deltas; after each successful sync the view must equal a freshly recomputed expected result and source/consumer/hash/state checks must remain exact.
- R4: Add tamper/drift cases for raw value, view definition/schema, owner/grant, contract version/fingerprint, digest/state, and stale view contents; every case must fail closed and recover through the documented migrate/sync path.
- R5: Verify an immediate unchanged replay yields zero raw mutations, zero view refresh, zero digest/state-semantic changes, and the same view fingerprint.

Acceptance:
- A1 (verifies R1): clean and upgrade paths both pass on real PostgreSQL and converge to the same normalized schema/view definition; a second migration run is a no-op.
- A2 (verifies R2): exact schema/data assertions cover every catalogued column and every date >= 2010 represented by the raw serving plane while correctly allowing causal warm-up NULLs.
- A3 (verifies R3): insert/update/delete/mixed delta fixtures produce mathematically correct refreshed features and exact source-consumer-digest-state reconciliation.
- A4 (verifies R4): every deliberate tamper/drift fixture prevents success/PASS and the documented repair path restores exact conformance without touching unrelated schemas.
- A5 (verifies R5): the unchanged replay reports precisely zero semantic mutations/refreshes and preserves all source/raw/view fingerprints.

## PR-89: Execute Full Historical Pipeline Acceptance Run

PR name: `macro-feature-full-run-acceptance`
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-89/macro-feature-full-run-acceptance`
Git status: `not-started (branch absent)`
Agent lane: Production-like acceptance QA; one agent only
Depends on: PR-88
Commit: `test(pr-89): execute full macro feature acceptance run`
Design patterns: Command, End-to-End Acceptance, Fail-Closed Verification.

Description:
- R1: In an explicitly authorized production-like environment with the persistent lake and real provider/PostgreSQL configuration, execute the complete path: explicit full `reconcile` for all 13 registered series -> full Silver rebuild -> canonical raw Gold publication -> `postgres-migrate` -> complete `gold-sync-postgres` -> materialized-view refresh -> independent `postgres-verify`.
- R2: Prove that every one of the 13 source histories needed by the feature library has retained data covering the requested HMM era from at least 2010-01-01 where the source contract permits, and report exact min/max dates plus missingness without fabricating observations.
- R3: Query `macro_loader.macro_features` exactly as a downstream consumer would and verify exact catalog/version/fingerprint, complete ordered schema, finite-or-NULL values, timestamp uniqueness/order, 2010 lower exposure boundary, and mathematically valid spot checks across calm/stress/rate regimes.
- R4: Immediately rerun `gold-sync-postgres` unchanged and require zero inserts, updates, deletes, digest changes, state-semantic changes, and materialized-view refreshes.
- R5: Commit only a deterministic sanitized `artifacts/acceptance/macro-feature-full-run-v1.json` containing commands/stages, versions/fingerprints, row/date summaries, refresh count, conformance result, and PASS|FAIL; it must contain no credentials, DSNs, or raw provider payloads.

Acceptance:
- A1 (verifies R1): every stage completes in the declared order against the intended endpoint/lake, all 13 series participate in explicit reconcile, and `postgres-verify` returns PASS only after the refreshed view is conformant.
- A2 (verifies R2): the report contains exact per-series min/max/row counts and proves >=2010 coverage where supported; source gaps remain measured gaps rather than filled rows.
- A3 (verifies R3): the downstream SELECT returns one unique ordered row per exposed timestamp, the exact catalogued columns/version/fingerprint, no NaN/infinity, and spot-check formulas match source observations.
- A4 (verifies R4): the immediate replay records exactly zero raw mutations and exactly zero materialized-view refreshes while preserving all fingerprints.
- A5 (verifies R5): the committed artifact is deterministic/sanitized, records PASS only if A1-A4 all pass, and quality gates remain green.

## PR-90: Execute Sunday Cron Chain Feature-Library Acceptance Run

PR name: `macro-feature-cron-acceptance`
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-90/macro-feature-cron-acceptance`
Git status: `not-started (branch absent)`
Agent lane: Operational cron QA; one agent only
Depends on: PR-89
Commit: `test(pr-90): execute macro feature cron acceptance run`
Design patterns: End-to-End Acceptance, Command, Single-Instance Lock, Failure Injection.

Description:
- R1: Execute the exact installed Sunday command path `ops/run-macro-loader-sunday.sh` under the same exported configuration, service-account permissions, working directory, `Europe/Vienna` timezone, lock, persistent lake, logging, and PostgreSQL roles used by cron; do not substitute direct Python calls for the acceptance run.
- R2: Prove the normal cron chain remains `run-daily` delta-only followed by `gold-sync-postgres` only after successful local Gold publication; it must not invoke full-history `reconcile`, manual materialized-view SQL, admin migration, or any second feature builder.
- R3: For a run containing at least one raw mutation, verify exactly one `macro_features` refresh and exact post-run conformance; for an immediate no-change cron-equivalent rerun, verify zero raw mutations and zero materialized-view refreshes.
- R4: Verify single-instance locking, deterministic cwd/Git identity, scheduler timezone, log path, exit-code propagation, runtime-vs-admin PostgreSQL role separation, and that the materialized-view refresh executes under the intended sync writer/ownership contract.
- R5: Inject a pre-sync `run-daily` failure and a materialized-view refresh failure separately; the first must prevent PostgreSQL sync entirely, and the second must make the cron command non-zero while preserving the last committed raw/view/sync state.
- R6: Commit only a deterministic sanitized `artifacts/acceptance/macro-feature-cron-v1.json` with run identifiers, stage/exit results, mutation/refresh counts, lock/timezone/log assertions, conformance result, and PASS|FAIL; no secret/config value beyond non-sensitive endpoint identity may be recorded.

Acceptance:
- A1 (verifies R1): evidence proves the same shell wrapper/service-account/config export/lock/cwd/timezone/logging path as the installed cron entry executed end-to-end.
- A2 (verifies R2): trace contains exactly delta-only `run-daily -> gold-sync-postgres` on success and contains no source reconcile, admin migration, manual feature SQL, or duplicate feature-computation command.
- A3 (verifies R3): the mutation run records exactly one refresh and conformant output; the immediate unchanged rerun records exactly zero mutations and zero refreshes with identical view fingerprint.
- A4 (verifies R4): lock contention prevents overlap, cwd/Git/timezone/log/role assertions all match source-controlled operations contracts, and privilege probes show runtime roles cannot perform admin DDL.
- A5 (verifies R5): both injected failures produce non-zero cron status with the specified skip/rollback behavior and no false PASS or partially advanced serving/view state.
- A6 (verifies R6): the committed cron artifact is deterministic/sanitized and can be marked PASS only when A1-A5 all succeed.

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

The active delivery program now begins with PR-80 above; completed scopes remain historical context and
must not be reopened or silently rewritten to implement PR-80+ work.
