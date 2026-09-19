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
Status: In Progress
Updated: 2026-09-19
PR: TBD
Git branch: `pr-80/feature-library-backlog-program`
Git status: `active-clean`
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
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-81/macro-feature-catalog-contract`
Git status: `not-started (branch absent)`
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
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-82/versioned-macro-features-view`
Git status: `not-started (branch absent)`
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
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-83/regime-oriented-derived-features`
Git status: `not-started (branch absent)`
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
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-84/macro-feature-refresh-uow`
Git status: `not-started (branch absent)`
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
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-85/macro-feature-view-conformance`
Git status: `not-started (branch absent)`
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
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `pr-86/macro-feature-formula-parity-qa`
Git status: `not-started (branch absent)`
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

## PR-01: Bootstrap Repository, Quality Gates, And Git Policy

Status: Merged

Updated: 2026-08-19

PR: #2

Git branch: `pr-01/repository-bootstrap-quality-gates`

Git status: `merged`

Agent lane: Foundation; one agent only

Depends on: none

Commit: `chore(pr-01): bootstrap repository and quality gates`

Design patterns: Command, Dependency Injection, Ports and Adapters.

Description:
- R1: Create Python >=3.13 `uv` project; runtime dependencies `polars`, `pyarrow`, `httpx`, `pydantic`, `PyYAML`, `matplotlib`; dev dependencies `pytest`, `pytest-cov`, `coverage`, `ruff`, `mypy`; no production pandas.
- R2: Create importable `application/`, `application/ports/`, `ingestion/`, `api/`, `scripts/`, `tests/unit/`, `tests/integration/`, `tests/fixtures/`; register `integration` and `network` pytest markers; preserve `AGENTS.md` rules.
- R3: Add Make targets `lint`, `type`, `unit`, `integration`, `coverage`, `quality-gate`; first four execute independently in parallel, test targets write separate coverage data, `coverage` combines and `--fail-under=90`.
- R4: Add repository-managed idempotent pre-push hook installer; dirty worktree, any execution failure, or combined coverage `<90.0%` blocks push.
- R5: Add `.github/workflows/quality-gates.yml` for `push`, `pull_request` to `main`, and `merge_group`: four parallel jobs exactly `lint|type|unit|integration`, plus `coverage` with `needs: [unit, integration]` and raw coverage artifact combination.
- R6: Add Conventional Commit/branch validator requiring `type(pr-XX): ...` scope to match `pr-XX/...`; exempt generated merge-group commits only.
- R7: Add idempotent GitHub repository setup script/documentation that configures protected `main`, five required checks, PR-only/no force/no delete, squash-only merging, auto-delete head branches, repository `allow_auto_merge`, and merge-queue compatibility; fail visibly when admin permissions are insufficient.
- R8: Add a documented/helper command for `gh pr merge <PR> --auto --squash` (or equivalent API) so implementation PRs are queued for auto-completion and cannot merge until branch protection requirements are green.
- R9: Add `.gitignore` for virtualenv/caches/coverage/temp outputs and `lake/`; keep README/ARCHITECTURE/AGENTS synchronized with implemented tooling.

Acceptance:
- A1 (verifies R1): dependency resolution succeeds and production dependency inspection finds no pandas.
- A2 (verifies R2): all roots exist/import, markers register, required tests exclude `network`, and AGENTS remains present.
- A3 (verifies R3): four children start independently; separate coverage data combine correctly; `89.99%` fails and `90.00%` passes.
- A4 (verifies R4): hook install is idempotent and each dirty/failure/coverage-negative case blocks push.
- A5 (verifies R5): workflow contract tests prove triggers, no inter-job `needs` among first four, exact coverage dependency/artifacts, and five stable check names.
- A6 (verifies R6): matching examples pass; malformed type/scope/PR mismatch fail deterministically.
- A7 (verifies R7): setup contract contains every required repository setting, is idempotent, and returns non-zero/actionable output when configuration cannot be applied.
- A8 (verifies R8): helper requests auto-squash merge but an integration/mock check proves required checks remain the gating condition.
- A9 (verifies R9): ignored artifacts are untracked and documentation does not contradict implemented tooling.

## PR-02: Define Registry, Paths, And Adapter Registry Contracts

Status: Merged

Updated: 2026-08-19

PR: #3

Git branch: `pr-02/series-registry-lake-contracts`

Git status: `merged`

Agent lane: Agent A

Depends on: PR-01

Commit: `feat(pr-02): define series and lake contracts`

Design patterns: Registry/Factory, Value Object, Dependency Injection.

Description:
- R1: Define immutable typed registry with exactly 13 canonical series and provider/source/unit/shape/frequency/bootstrap/capability metadata.
- R2: Restrict shape to `ohlc|scalar` and capability to `date_range|full_file`; VSTOXX is `scalar/full_file`; validate duplicate/unknown/empty/invalid metadata.
- R3: Define typed path service for every Bronze/Silver monthly file, Gold build/root artifact, state, run manifest, inventory path; no provider-local path literals.
- R4: Define canonical provider enum/identity contracts and an adapter-registry interface used later as Registry/Factory; lookup unknown provider fails explicitly.
- R5: Add exact fixed-path/registry tests for `2026-08-18` and build `20260818T020000Z` plus all invalid registry cases.

Acceptance:
- A1 (verifies R1): exactly 13 complete registry entries exist.
- A2 (verifies R2): only declared enum values pass and VSTOXX is unambiguous.
- A3 (verifies R3): one path service returns every documented exact path.
- A4 (verifies R4): fake adapters can register/resolve by provider without application `if/elif`, unknown provider fails.
- A5 (verifies R5): all fixed and invalid cases pass offline.

## PR-03: Implement Polars Lake Repositories And Atomic IO

Status: Merged

Updated: 2026-08-19

PR: #4

Git branch: `pr-03/polars-parquet-lake-io`

Git status: `merged`

Agent lane: Agent B

Depends on: PR-01

Commit: `feat(pr-03): add polars parquet repositories`

Design patterns: Repository, Adapter, Dependency Injection.

Description:
- R1: Define narrow repository/IO ports and Polars filesystem adapters for deterministic zero/one/multi monthly reads with caller-key ordering and efficient `min/max observation_date` discovery from authoritative Bronze.
- R2: Implement same-directory temp write, flush/fsync where supported, and `os.replace`; stale temp never becomes authoritative.
- R3: Implement pure diff by supplied natural key returning inserts/unchanged/revisions; reject duplicate incoming keys; equal-key new row replaces once.
- R4: Rewrite only months containing inserts/revisions; logical no-op preserves file hashes/mtime and unrelated months.
- R5: Add tests for read modes, authoritative min/max discovery, atomic interruption, stale temp, duplicate keys, revision, no-op, deterministic ordering, and unaffected partitions.

Acceptance:
- A1 (verifies R1): repository test doubles and filesystem adapters satisfy the same contracts, min/max are exact from fixtures, and production code contains no pandas.
- A2 (verifies R2): injected failures preserve prior destination; no stale temp is authoritative.
- A3 (verifies R3): classifications and duplicate rejection are exact/idempotent.
- A4 (verifies R4): no-op and unrelated partitions remain byte/mtime unchanged.
- A5 (verifies R5): all IO/repository cases pass offline.

## PR-04: Add Shared HTTP Port, Retry Strategy, And Provider Protocol

Status: Merged

Updated: 2026-08-19

PR: #5

Git branch: `pr-04/shared-http-provider-port`

Git status: `merged`

Agent lane: Agent B

Depends on: PR-01, PR-02

Commit: `feat(pr-04): add shared http provider port`

Design patterns: Ports and Adapters, Adapter, Strategy, Dependency Injection.

Description:
- R1: Define application-facing HTTP request/response port and `MarketDataProvider` protocol; application imports no `httpx`.
- R2: Provider request contract receives an explicit operation mode and logical date window; `date_range` adapters must honor exact bounds for normal update while `full_file` adapters receive the same logical window for post-fetch filtering.
- R3: Implement one `httpx` adapter with explicit timeouts and injected `RetryPolicy` Strategy: bounded retries for transient transport errors, `429`, `5xx`; no generic retry for other `4xx`.
- R4: Implement bounded exponential backoff with injected sleeper, deterministic tests, and numeric `Retry-After` capped by configured maximum.
- R5: Define typed sanitized provider error with provider/series/source/request category but no API key/auth/full-secret URL; add success/retry/bound-contract/protocol/redaction tests.

Acceptance:
- A1 (verifies R1): fake provider/HTTP implementations substitute without `httpx` in application.
- A2 (verifies R2): mocks prove normal update propagates exact logical bounds to every provider adapter contract.
- A3 (verifies R3): exact retry categories/attempts/timeouts are proven.
- A4 (verifies R4): deterministic delay/cap sequence passes without real sleep.
- A5 (verifies R5): safe context remains, configured secrets never appear, and all stated cases pass offline.

## PR-05: Implement Bootstrap, Strict Delta Update, Explicit Reconcile Planner And State

Status: Merged

Updated: 2026-08-19

PR: #8

Git branch: `pr-05/planner-delta-reconcile-state`

Git status: `merged`

Agent lane: Foundation; first free agent

Depends on: PR-02, PR-03

Commit: `feat(pr-05): add strict delta ingestion planner`

Design patterns: Strategy, Repository, Dependency Injection.

Description:
- R1: Implement pure planner modes `bootstrap|update|reconcile`; `bootstrap` only when no authoritative Bronze observation exists, `update` for normal existing-history execution, and `reconcile` only when explicitly requested by caller/operator.
- R2: For `update`, derive `latest_stored_date=max(authoritative Bronze observation_date)` and compute `request_start=latest_stored_date-overlap_days`, `request_end=injected_today`; default overlap 7; never use Bronze minimum date as update start; reject today earlier than latest stored date and invalid overlap/config.
- R3: Map `update` to exact bounded request for `date_range`; map `update` to `full_file` fetch plus mandatory logical filter window metadata for `full_file`; map explicit `reconcile` to maximum exposed history request. No clock/state condition may auto-promote `update` into `reconcile`.
- R4: Define `ingestion_state.parquet` key `(provider,series_id)` with last success, authoritative last observation cache, last requested start/end, operation mode, fetched/accepted/changed counts, and optional `last_reconcile_utc`; state cache disagreement with authoritative Bronze must fail or be repaired from Bronze, never broaden fetch scope.
- R5: State advances only after caller confirms durable Bronze plus success run manifest; no-op may advance success timestamp but not fabricate observation coverage; reconcile timestamp advances only after explicit successful reconcile.
- R6: All dates/times are injected; add canonical proof fixture `min=2000-01-03,max=2026-08-18,today=2026-08-19,overlap=7 -> 2026-08-11..2026-08-19`, plus an assertion that `2000-01-03..2026-08-19` is never emitted by normal update.

Acceptance:
- A1 (verifies R1): empty Bronze -> bootstrap; existing Bronze -> update; reconcile appears only from explicit reconcile request.
- A2 (verifies R2): exact latest-derived overlap bounds pass; minimum-date start, reverse date, and invalid configuration fail deterministically.
- A3 (verifies R3): `date_range` receives exact delta bounds; `full_file` receives exact logical filter window; normal update never emits maximum-history/reconcile instruction.
- A4 (verifies R4): typed state round-trip/upsert leaves one row/key and stale cache cannot override Bronze max date.
- A5 (verifies R5): failure preserves prior state; no-op/reconcile timestamp semantics are exact.
- A6 (verifies R6): canonical fixture emits only `2026-08-11..2026-08-19`; test spy proves no production planner wall-clock call and no normal full-history plan.

## PR-06: Add CBOE Volatility Provider

Status: Merged

Updated: 2026-08-19

PR: #10

Git branch: `pr-06/cboe-volatility-provider`

Git status: `merged`

Agent lane: Agent A

Depends on: PR-02, PR-04, PR-05, PR-24

Commit: `feat(pr-06): ingest cboe volatility indices`

Design patterns: Adapter, Ports and Adapters, Dependency Injection.

Description:
- R1: Implement one CBOE adapter for registered `vix|vix9d|vix3m|vix6m|vix1y` only through shared provider/HTTP contracts.
- R2: Treat sources as registry-driven `full_file`; unavailable registered source returns typed error, never fallback/synthetic data.
- R3: Parse exact Bronze common+OHLC with Polars; reject invalid/duplicate dates, missing/non-finite close, invalid natural key.
- R4: On normal update, although full file may be downloaded, filter parsed rows to caller's exact logical delta window before returning/diffing; out-of-window rows must not be persisted or cause old-month rewrites. On bootstrap/explicit reconcile, maximum history may be accepted. Shorter response never deletes retained rows.
- R5: Add representative fixtures/tests for all routes, exact delta-window filtering, parsing, invalid/duplicate, revision, shortened response, unavailable source, HTTP propagation.

Acceptance:
- A1 (verifies R1): only five registered canonical IDs resolve to CBOE.
- A2 (verifies R2): requests use registry source/full-file and no fallback path.
- A3 (verifies R3): exact schema and invalid cases pass.
- A4 (verifies R4): normal update accepts only in-window rows and touches only matching affected months; bootstrap/reconcile may accept full range; retained history survives shortening.
- A5 (verifies R5): all scenarios pass offline.

## PR-07: Add STOXX VSTOXX Provider

Status: Merged

Updated: 2026-08-19

PR: #11

Git branch: `pr-07/stoxx-vstoxx-provider`

Git status: `merged`

Agent lane: Agent A

Depends on: PR-02, PR-04, PR-05, PR-24

Commit: `feat(pr-07): ingest vstoxx history`

Design patterns: Adapter, Ports and Adapters, Dependency Injection.

Description:
- R1: Implement only registered `vstoxx/V2TX` through shared ports.
- R2: Parse registry-declared scalar Bronze `value: Float64`; no runtime provider-shape guessing.
- R3: `full_file` source may download complete history, but normal update filters to exact caller delta window before logical diff/persistence; bootstrap/explicit reconcile may accept maximum history; shorter response never deletes older retained data.
- R4: Reject invalid/duplicate dates and missing/non-finite values; propagate safe HTTP/provider errors; revisions replace equal keys once.
- R5: Add representative fixture/tests for bootstrap, explicit reconcile, strict normal delta filtering, stable source identity, invalid/duplicate, revision, shortening, error propagation.

Acceptance:
- A1 (verifies R1): only registered VSTOXX mapping is accepted.
- A2 (verifies R2): exact scalar Bronze schema is produced.
- A3 (verifies R3): normal update accepts only caller-window rows while reconcile/bootstrap can use full history and shortening never truncates retained history.
- A4 (verifies R4): invalid/error/revision cases are deterministic/sanitized.
- A5 (verifies R5): all scenarios pass offline.

## PR-08: Add Yahoo MOVE Provider

Status: Merged

Updated: 2026-08-19

PR: #12

Git branch: `pr-08/yahoo-move-provider`

Git status: `merged`

Agent lane: Agent A

Depends on: PR-02, PR-04, PR-05, PR-24

Commit: `feat(pr-08): ingest move index history`

Design patterns: Adapter, Ports and Adapters, Dependency Injection.

Description:
- R1: Implement isolated Yahoo adapter only for registered `move -> ^MOVE`; no Yahoo-specific behavior outside adapter.
- R2: Bootstrap/explicit reconcile request maximum available history; normal update passes the planner's exact `request_start/request_end` and must not broaden it.
- R3: Normalize only daily OHLC to exact Bronze; reject invalid/duplicate dates and missing/non-finite close; exclude volume/actions; bounded normal response outside requested dates is a contract failure.
- R4: Empty bounded response is valid no-op; shorter explicit reconcile response never truncates retained history; revisions replace once.
- R5: Add canonical delta request test (`2026-08-11..2026-08-19`), maximum-history separation, empty, out-of-window, invalid/duplicate, revision, shortening, schema, and HTTP errors.

Acceptance:
- A1 (verifies R1): only canonical MOVE mapping is accepted and application remains provider-agnostic.
- A2 (verifies R2): normal update makes exactly one bounded request with exact dates and never maximum-history fallback.
- A3 (verifies R3): schema/exclusion/invalid/out-of-window cases pass.
- A4 (verifies R4): no-op/shortening/revision semantics are exact.
- A5 (verifies R5): canonical delta fixture proves no request from historical minimum; all scenarios pass offline.

## PR-09: Add ECB CISS And ESTR Provider

Status: Merged

Updated: 2026-08-19

PR: #13

Git branch: `pr-09/ecb-ciss-estr-provider`

Git status: `merged`

Agent lane: Agent B

Depends on: PR-02, PR-04, PR-05, PR-24

Commit: `feat(pr-09): ingest ecb regime series`

Design patterns: Adapter, Ports and Adapters, Dependency Injection.

Description:
- R1: Implement only registered CISS and ESTR API mappings through shared ports.
- R2: Bootstrap/explicit reconcile request maximum exposed history; normal update maps exact planner bounds to provider `startPeriod/endPeriod` and never omits/broadens them; MVP does not use deletion events until explicit deletion semantics exist.
- R3: Parse exact scalar Bronze; missing/non-numeric/non-finite observations are absent, duplicates invalid; calendar gaps remain gaps; out-of-window bounded rows fail contract.
- R4: Treat provider no-results response for a valid bounded query as empty/no-op where source semantics indicate no matching observations; other typed HTTP errors propagate; revisions replace once.
- R5: Add canonical delta request test, both series, max-history explicit modes, missing/duplicate/gap/out-of-window, empty/no-result mapping, revision, shortening and HTTP errors.

Acceptance:
- A1 (verifies R1): exactly two mappings resolve.
- A2 (verifies R2): normal mode emits exact start/end and no maximum-history request; only bootstrap/reconcile do so.
- A3 (verifies R3): schema/missing/duplicate/gap/out-of-window behavior passes.
- A4 (verifies R4): valid no-result becomes no-op while real failures remain typed; revision is exact.
- A5 (verifies R5): canonical delta fixture proves no historical-minimum fetch and all scenarios pass offline.

## PR-10: Add FRED Rates, Credit, And Dollar Provider

Status: Merged

Updated: 2026-08-19

PR: #14

Git branch: `pr-10/fred-rates-credit-dollar-provider`

Git status: `merged`

Agent lane: Agent B

Depends on: PR-02, PR-04, PR-05, PR-24

Commit: `feat(pr-10): ingest fred regime series`

Design patterns: Adapter, Ports and Adapters, Dependency Injection.

Description:
- R1: Implement exactly registered `DGS2|DGS10|DTWEXBGS|BAMLHE00EHYIOAS` mappings through shared ports.
- R2: Bootstrap/explicit reconcile request maximum currently exposed history; normal update sends exact planner observation bounds and never omits/broadens them or silently falls back to full history.
- R3: Parse exact scalar Bronze; `.`, blank, missing/non-finite values are absent; duplicate dates invalid; out-of-window bounded rows fail contract.
- R4: Shorter reconcile/bootstrap responses never truncate retained history; explicit reconcile may revise historical equal keys, while normal update can revise only keys inside its delta window.
- R5: Inject required FRED API key/config; strip/redact it from persisted `source_url`, registry, logs, errors, repr, and fixtures.
- R6: Add canonical delta request fixture, four series, explicit max-history modes, missing/duplicate/out-of-window, historical revision, shortened HY history, API-key redaction, errors.

Acceptance:
- A1 (verifies R1): exactly four source/canonical mappings resolve.
- A2 (verifies R2): normal mode sends exact delta bounds and never full-history; bootstrap/reconcile remain explicitly separate.
- A3 (verifies R3): schema/missing/duplicate/out-of-window behavior passes.
- A4 (verifies R4): normal revision stays inside delta window and explicit reconcile may update older equal keys without truncation.
- A5 (verifies R5): configured secret is absent from every persisted/diagnostic artifact tested.
- A6 (verifies R6): canonical fixture proves no historical-minimum fetch and all scenarios pass offline.

## PR-11: Add Operational Manifest And Inventory Repositories

Status: Merged

Updated: 2026-08-19

PR: #7

Git branch: `pr-11/bronze-inventory-run-manifests`

Git status: `merged`

Agent lane: Agent B

Depends on: PR-02, PR-03

Commit: `feat(pr-11): add inventory and run manifests`

Design patterns: Repository, Adapter, Dependency Injection.

Description:
- R1: Define authoritative `dataset_inventory.parquet` snapshot fields `series_id,provider,min_observation_date,max_observation_date,row_count,duplicate_key_count,file_count`.
- R2: Define `ingestion_runs.parquet` unique `run_id` fields including provider/series/mode/requested bounds/fetched rows/accepted rows/inserted/revised/written partitions/status/timestamps/sanitized error.
- R3: Implement repository adapters: deterministic snapshot replacement for inventory and unique-run upsert for runs.
- R4: Inventory describes observed stored coverage only; no synthetic market-calendar missing metrics. `max_observation_date` is the authoritative planning reference for normal delta updates; `min_observation_date` must not be used as normal update request start.
- R5: Failed run never contains secrets or claims non-durable inserts/revisions/partitions; add empty/populated/success/failure/idempotency/request-bound tests.

Acceptance:
- A1 (verifies R1): exact inventory schema/values pass.
- A2 (verifies R2): exact run schema/status/request-bound values pass.
- A3 (verifies R3): deterministic round-trip and no duplicate run ID.
- A4 (verifies R4): no expected-calendar metric exists and tests distinguish min from max planning semantics.
- A5 (verifies R5): failure durability/redaction and all stated cases pass.

## PR-12: Add Registry-Driven Bronze Orchestration Unit Of Work

Status: Merged

Updated: 2026-08-19

PR: #15

Git branch: `pr-12/bronze-orchestration`

Git status: `merged`

Agent lane: Integration; one agent only

Depends on: PR-03, PR-05, PR-06, PR-07, PR-08, PR-09, PR-10, PR-11, PR-24

Commit: `feat(pr-12): orchestrate bronze updates`

Design patterns: Registry/Factory, Unit of Work, Repository, Dependency Injection.

Description:
- R1: Application service resolves series contract and provider via injected registries, planner, repositories; no provider implementation/HTTP imports or provider conditional ladder.
- R2: Expose `bootstrap|update|reconcile`; normal `update` reads authoritative Bronze max date and uses PR-05 strict delta plan. `reconcile` occurs only through explicit caller request; orchestration must not select it based on age/timer/state during normal update.
- R3: One selected series is a Unit of Work: fetch/parse -> enforce logical request window -> diff -> durable Bronze -> durable success run -> state advance; failure before commit preserves prior state/authoritative data.
- R4: Multi-series isolation: one failed series does not roll back separately committed series; failed run is recorded safely.
- R5: No-op writes success run with zero changes, rewrites no Bronze file, may advance success time but not observation coverage; normal update writes requested bounds exactly to run/state metadata.
- R6: Add fake-provider/repository tests for registry routing, all modes, canonical delta fixture, assertion that normal update never calls reconcile/max-history strategy, partial failure, barrier failures, restart, no-op, revision, explicit reconcile, multi-series isolation.

Acceptance:
- A1 (verifies R1): provider registry substitution works with no application provider-specific imports/conditionals.
- A2 (verifies R2): existing Bronze normal call always selects strict update; only explicit reconcile call selects reconcile.
- A3 (verifies R3): canonical fixture accepts only `2026-08-11..2026-08-19`; failure injection around each barrier proves commit semantics.
- A4 (verifies R4): one failure coexists with another durable success.
- A5 (verifies R5): file hash/mtime/state/run values prove no-op and exact-bound semantics.
- A6 (verifies R6): spy proves no normal full-history request and all stated scenarios pass offline.

## PR-13: Build Canonical Silver Daily Series

Status: Merged

Updated: 2026-08-19

PR: #16

Git branch: `pr-13/silver-canonical-series`

Git status: `merged`

Agent lane: Agent A

Depends on: PR-12, PR-24

Commit: `feat(pr-13): build canonical silver series`

Design patterns: Repository, Adapter, Dependency Injection.

Description:
- R1: Registry-driven Silver builder/repository produces exact canonical schema from selected retained Bronze.
- R2: OHLC maps `value=close` and preserves OHLC; scalar preserves value and null Float64 OHLC; identity/unit consistent.
- R3: Require unique key, strict per-series date order, finite non-null value, consistent provider/source; never create missing dates.
- R4: Diff and rewrite only affected monthly Silver partitions; identical rebuild physical no-op; unselected series untouched.
- R5: Add OHLC/scalar/schema/dtype/duplicate/non-finite/identity/gap/revision/no-op tests.

Acceptance:
- A1 (verifies R1): exact schema/order/types.
- A2 (verifies R2): both source shapes map exactly.
- A3 (verifies R3): all invalid/gap rules pass.
- A4 (verifies R4): revision touches one month; no-op/unselected unchanged physically.
- A5 (verifies R5): all cases pass offline.

## PR-14: Add Lake Inventory CLI

Status: Merged

Updated: 2026-08-19

PR: #17

Git branch: `pr-14/lake-inventory-cli`

Git status: `merged`

Agent lane: Agent B

Depends on: PR-11, PR-12, PR-24

Commit: `feat(pr-14): add lake inventory cli`

Design patterns: Command, Dependency Injection.

Description:
- R1: Command adapter `inventory` reads inventory repository and renders exactly seven stable fields.
- R2: Repeatable `--series`/`--provider` filters validate registry/provider values; unknown fails; no lake mutation.
- R3: Deterministic `--json` has same logical fields/order/values.
- R4: Empty valid result exits zero; config/read/schema/unknown-filter exits non-zero without mutation.
- R5: Add parser/render/filter/json/empty/corrupt-missing tests.

Acceptance:
- A1 (verifies R1): exact text fields/order.
- A2 (verifies R2): filter/unknown/no-mutation behavior passes.
- A3 (verifies R3): JSON/text logical equivalence passes.
- A4 (verifies R4): exact exit semantics pass.
- A5 (verifies R5): all cases pass offline.

## PR-15: Build Volatility Gold Feature Family

Status: Merged

Updated: 2026-08-19

PR: #18

Git branch: `pr-15/volatility-gold-features`

Git status: `merged`

Agent lane: Agent A

Depends on: PR-13, PR-24

Commit: `feat(pr-15): add volatility regime features`

Design patterns: Strategy for feature policy, Dependency Injection; otherwise functional core.

Description:
- R1: Convert Silver dates to unique/sorted first-column UTC-midnight `timestamp_m1: Datetime(us,UTC)`; remove `observation_date`; union family source dates only.
- R2: For `vix,vix9d,vix3m,vix6m,vix1y,vstoxx,move` output exact `<series>_level`, `_delta_5obs`, `_delta_20obs`, `_zscore_60obs` using global math.
- R3: Output exact `vix9d_vix_ratio,vix_vix3m_ratio,vix3m_minus_vix,vix6m_minus_vix,vix1y_minus_vix`; same timestamp only, null on missing/zero denominator.
- R4: No fill; observation lags count valid observations; 60-value `ddof=0`; insufficient/zero variance null.
- R5: Deterministic schema order: timestamp, each series in registry order with four features, then term features; nullable Float64.
- R6: Add hand-calculable timestamp/delta-gap/zscore/zero-variance/ratio/missing/no-future tests.

Acceptance:
- A1 (verifies R1): exact timestamp contract/no observation_date.
- A2 (verifies R2): all 28 series features/formulas exact.
- A3 (verifies R3): five term features/null rules exact.
- A4 (verifies R4): gap/59th/60th/zero-variance tests exact.
- A5 (verifies R5): full expected schema/order/types exact.
- A6 (verifies R6): all cases pass offline.

## PR-16: Build Macro/Credit/Rates/USD Gold Feature Family

Status: Merged

Updated: 2026-08-19

PR: #19

Git branch: `pr-16/macro-gold-features`

Git status: `merged`

Agent lane: Agent B

Depends on: PR-13, PR-24

Commit: `feat(pr-16): add macro regime features`

Design patterns: Strategy for feature policy, Dependency Injection; otherwise functional core.

Description:
- R1: Convert Silver to same canonical family timestamp contract; union source dates without fill.
- R2: Output CISS and Euro HY `level,delta_5obs,delta_20obs,zscore_60obs`.
- R3: Output `us_2y_level,us_2y_delta_20obs,us_10y_level,us_10y_delta_20obs,us_10y_minus_us_2y`; spread same timestamp only.
- R4: Output `estr_level,estr_delta_20obs,usd_broad_level,usd_broad_delta_20obs`; no carry.
- R5: Deterministic schema order R2->R3->R4 after timestamp; nullable Float64; same history/null rules.
- R6: Add hand-calculable timestamp/delta-gap/zscore/yield-pair/no-fill/zero-variance/no-future tests.

Acceptance:
- A1 (verifies R1): exact timestamp/union/no-fill contract.
- A2 (verifies R2): CISS/HY features exact.
- A3 (verifies R3): rates/spread exact including missing pair.
- A4 (verifies R4): ESTR/USD exact/no carry.
- A5 (verifies R5): full schema/order/types exact.
- A6 (verifies R6): all cases pass offline.

## PR-17: Assemble And Validate Canonical Gold Frame

Status: Merged

Updated: 2026-08-19

PR: #20

Git branch: `pr-17/canonical-gold-frame`

Git status: `merged`

Agent lane: Integration; one agent only

Depends on: PR-15, PR-16, PR-24

Commit: `feat(pr-17): assemble canonical daily gold frame`

Design patterns: Facade, Dependency Injection; functional core for deterministic assembly.

Description:
- R1: Outer-join feature-family frames only on timestamp; one union row, null preservation, no imputation/as-of carry.
- R2: Enforce one source-controlled exact ordered Gold schema: timestamp, PR-15 exact columns, PR-16 exact columns; reject missing/extra/renamed/reordered/wrong type/observation_date.
- R3: Validate non-empty, UTC-midnight exact dtype, unique strictly increasing timestamps; normalize feature NaN to null; reject infinity/non-Float64 feature types.
- R4: Add truncation causality regression: rebuild from Silver cutoff `t` and assert every Gold value `<=t` equals full-build prefix; deliberately leaky transform must be detected.
- R5: Keep assembly pure/storage-neutral: no build ID/files/hash/JSON/plot/catalog/publication imports or side effects.
- R6: Add join/schema/timestamp/NaN/infinity/causality/storage-boundary tests.

Acceptance:
- A1 (verifies R1): exact union/null/no-carry result.
- A2 (verifies R2): expected schema passes and every drift case fails.
- A3 (verifies R3): final frame has no NaN/infinity and invalid timestamps fail.
- A4 (verifies R4): multiple cutoff tests pass and leaky control fails.
- A5 (verifies R5): static/import tests prove no physical dependency.
- A6 (verifies R6): all cases pass offline.

## PR-18: Add Immutable Gold Build Store Repository

Status: Merged

Updated: 2026-08-19

PR: #21

Git branch: `pr-18/versioned-gold-storage`

Git status: `merged`

Agent lane: Agent A

Depends on: PR-17, PR-24

Commit: `feat(pr-18): add immutable gold build store`

Design patterns: Repository, Adapter, Dependency Injection.

Description:
- R1: Build ID from injected UTC second `YYYYMMDDTHHMMSSZ`; reject non-UTC/malformed/reused build directory.
- R2: GoldBuildStore creates directory, writes same-dir temp Parquet, validates readback schema/rows/bounds, fsync where supported, atomically names final; creation-only.
- R3: Return deterministic final-byte SHA-256, row count, min/max timestamp, ordered columns and semantic versions.
- R4: Explicit build reader requires build ID; no latest/glob/mtime selection; validates final Parquet.
- R5: Partial directory/final absence is incomplete and rejected; restart never overwrites/promotes it.
- R6: Add ID/collision/path/readback/hash/interruption/overwrite/partial/coexistence tests.

Acceptance:
- A1 (verifies R1): exact ID/invalid/collision semantics.
- A2 (verifies R2): success/failure atomic creation semantics exact.
- A3 (verifies R3): metadata/hash independently match file.
- A4 (verifies R4): explicit A never resolves B.
- A5 (verifies R5): partial directory remains incomplete/unpromoted.
- A6 (verifies R6): all cases pass offline.

## PR-19: Add Gold Catalog Repository And Resolution Strategies

Status: Merged

Updated: 2026-08-19

PR: #22

Git branch: `pr-19/gold-catalog-contract`

Git status: `merged`

Agent lane: Agent B

Depends on: PR-17, PR-24

Commit: `feat(pr-19): add gold catalog and resolution strategies`

Design patterns: Repository, Strategy, Dependency Injection.

Description:
- R1: Define exact 15-field catalog schema including `pruned_at_utc`; source-controlled semantic versions start `1/1` and never auto-bump.
- R2: GoldCatalogRepository atomically persists deterministic `(started_at_utc,build_id)` ordering; unique ID; statuses only `building|complete|failed`.
- R3: Validate pure logical invariants only: current only complete; at most one current; complete metadata required; pruned row has non-null `pruned_at_utc` and all three artifact paths null; selectable complete is non-pruned with all paths non-null and syntactically inside matching build ID. Do not inspect filesystem existence.
- R4: Define `ResolutionPolicy` Strategy `strict_current` (default) and `latest_compatible`; both filter exact caller-supported schema/feature sets and never select building/failed/pruned.
- R5: `strict_current` fails on incompatible/unselectable current; `latest_compatible` prefers compatible current else newest compatible selectable complete by completed time/build ID; no filesystem query.
- R6: Add schema/version/state/path-shape/pruned/current/policy/fallback/order/no-filesystem tests.

Acceptance:
- A1 (verifies R1): exact schema has 15 fields/1-1 constants and no legacy date fields.
- A2 (verifies R2): deterministic atomic round-trip and invalid duplicate/status fail.
- A3 (verifies R3): every logical/pruned/path-shape invalid case fails while repository performs no existence checks.
- A4 (verifies R4): both strategies substitute through one interface with exact version filtering.
- A5 (verifies R5): strict/fallback/order semantics are exact and filesystem spy is untouched.
- A6 (verifies R6): all cases pass offline.

## PR-20: Generate Immutable Build Manifest And Feature Profile

Status: Merged

Updated: 2026-08-19

PR: #23

Git branch: `pr-20/gold-build-sidecars`

Git status: `merged`

Agent lane: Agent A

Depends on: PR-18, PR-24

Commit: `feat(pr-20): add gold build sidecars`

Design patterns: Builder, Adapter, Dependency Injection.

Description:
- R1: Generate deterministic creation-only build `manifest.json` with exact artifact concepts: dataset/build identity, `artifact_state="built"`, schema/feature versions, build start/completion timestamps, rows, ordered columns, bounds, data path/SHA, feature-set hash, Git commit hash, plot path. Do not use catalog `building|complete|failed` status.
- R2: Compute deterministic feature-set SHA-256 from semantic versions + ordered names/dtypes + documented formula parameters; Git identity is injected and required except explicit deterministic test fallback.
- R3: Generate creation-only deterministic `feature_profile.png` from exact frame; numeric features in canonical order; timestamp excluded; no random sampling/wall-clock labels.
- R4: Validate Parquet/JSON/PNG bundle identity/hash/bounds/path/PNG before publication-ready; existing sidecar target never overwritten.
- R5: JSON/plot failure leaves incomplete immutable attempt and never mutates root catalog/materialized views.
- R6: Add exact JSON bytes/schema/hash/Git/PNG/order/exclusion/overwrite/mismatch/failure tests.

Acceptance:
- A1 (verifies R1): exact required artifact key set/values and no publication-status ambiguity.
- A2 (verifies R2): stable hash; formula/schema/version change changes hash; injected Git preserved.
- A3 (verifies R3): valid deterministic PNG/input order/exclusion.
- A4 (verifies R4): bundle validation and creation-only behavior exact.
- A5 (verifies R5): injected failures leave root state untouched.
- A6 (verifies R6): all cases pass offline.

## PR-21: Publish Gold With State Machine, Unit Of Work, And Materialized Views

Status: Merged

Updated: 2026-08-19

PR: #24

Git branch: `pr-21/gold-publication-state-machine`

Git status: `merged`

Agent lane: Integration; one agent only

Depends on: PR-19, PR-20, PR-24

Commit: `feat(pr-21): publish gold with catalog state machine`

Design patterns: State Machine, Unit of Work, Materialized View, Repository, Dependency Injection.

Description:
- R1: Implement publication State Machine: register `building,current=false`; create/validate immutable bundle; on build failure atomically finalize attempted row `failed,current=false`; previous current never changes.
- R2: Before promotion physically validate candidate via GoldBuildStore: exact schema/version/build ID, Parquet SHA/rows/bounds, build JSON identity/hash/path, valid PNG, containment in expected build directory.
- R3: Promotion Unit of Work atomically replaces authoritative catalog with new `complete,current=true` and old current demoted; this catalog replace is the only publication commit point.
- R4: Implement root `manifest.json` and `feature_profile.png` as `GoldMaterializedViewWriter`: after each successful catalog mutation regenerate from catalog/current bundle; never use them as commit authority or selection input.
- R5: If materialized-view refresh fails after catalog commit, return hard operational error but preserve catalog truth; startup/publication entry reconciles stale/missing root views from catalog/current bundle.
- R6: Recover stale non-current `building` rows to `failed`; never infer completion from filesystem presence. A current `building` row is invalid and stops publication for manual/invariant recovery.
- R7: Add failure-injection/state/promotion/physical-integrity/materialized-view/reconciliation/stale-building/first/subsequent publication tests.

Acceptance:
- A1 (verifies R1): only fully built candidate can continue; build failure leaves old current and failed attempt.
- A2 (verifies R2): every physical metadata/hash/path mismatch prevents promotion.
- A3 (verifies R3): exactly one current after success and event trace proves catalog replacement is commit point.
- A4 (verifies R4): root views exactly derive from catalog/current and are never read by resolver.
- A5 (verifies R5): post-commit view failure reports error, catalog remains new authority, next reconciliation repairs views.
- A6 (verifies R6): stale building is never promoted; invalid current-building stops safely.
- A7 (verifies R7): all stated scenarios pass offline.

## PR-22: Add Safe Gold Retention With Mark-And-Sweep

Status: Merged

Updated: 2026-08-19

PR: #25

Git branch: `pr-22/gold-build-retention`

Git status: `merged`

Agent lane: Foundation; first free agent

Depends on: PR-21, PR-24

Commit: `feat(pr-22): add mark and sweep gold retention`

Design patterns: Mark-and-Sweep, Strategy, Repository, Dependency Injection.

Description:
- R1: Default retain five physical complete builds including current per `(schema_version,feature_version)` pair; deterministic oldest non-current ordering by completed time/build ID.
- R2: Mark phase: validate eligible non-current complete/non-pruned row then atomically set all artifact paths null and `pruned_at_utc=injected_now`; current/building/failed/other-version never marked.
- R3: Sweep phase: only after successful mark delete `data.parquet,manifest.json,feature_profile.png` and empty directory; missing already-marked files are idempotent success.
- R4: Sweep interruption/failure leaves catalog safely unselectable and physical orphan(s); return operational error and retry orphan cleanup later. Never restore selectability automatically.
- R5: Resolver never returns marked row; repeated retention/cleanup idempotent; root materialized views are refreshed if catalog audit representation changes.
- R6: Add default/custom/order/current/version/mark-before-delete/partial-delete/orphan-retry/idempotency/resolver tests.

Acceptance:
- A1 (verifies R1): retained count/order exact per semantic pair.
- A2 (verifies R2): catalog tombstone occurs before any delete and protected rows untouched.
- A3 (verifies R3): valid sweep removes whole bundle; already-marked missing file is safe retry.
- A4 (verifies R4): injected partial delete leaves unselectable catalog plus retryable orphan, never dangling selectable path.
- A5 (verifies R5): policy/resolution/repeated runs/view refresh are exact.
- A6 (verifies R6): all cases pass offline.

## PR-23: Add Delta-Only Daily Medallion Pipeline And Operational CLI

Status: Merged

Updated: 2026-08-19

PR: #26

Git branch: `pr-23/daily-medallion-pipeline`

Git status: `merged`

Agent lane: Integration; one agent only

Depends on: PR-14, PR-21, PR-22, PR-24

Commit: `feat(pr-23): add delta-only daily medallion pipeline`

Design patterns: Command, Facade/Orchestrator, Unit of Work, Dependency Injection.

Description:
- R1: Command adapters `bootstrap,update,reconcile,silver-build,gold-build,inventory,run-daily`; parsing/config/render in `api`, use cases in `application`; Gold publish only PR-21 service.
- R2: On Gold-capable command entry, recover stale building rows and reconcile root materialized views before creating another build; this Gold-view reconciliation is unrelated to market-source full-history `reconcile`.
- R3: `run-daily` behavior is strict: empty selected series -> bootstrap; existing selected series -> **update only** using newest durable Bronze date and overlap through injected today. `run-daily` must never invoke source `reconcile`, maximum-history strategy, or historical-minimum request for an existing series. Then Bronze -> Silver -> full canonical Gold -> immutable bundle -> candidate validation -> atomic catalog promotion -> root view refresh -> retention -> inventory.
- R4: `reconcile` is an explicit separate source command and may request maximum source history; if operators want it periodically, documentation gives a separate scheduler example. It is never hidden inside `run-daily`.
- R5: `--series` restricts Bronze/Silver execution only; Gold always rebuilds full canonical schema from all currently available Silver; unknown series fail.
- R6: Failure semantics: provider failure makes run non-zero but independent committed Bronze series remain; any pre-promotion Silver/Gold failure leaves old current; post-promotion materialized-view/retention failure returns non-zero but catalog truth is not falsely rolled back.
- R7: Add structured stdlib logging context including `run_id`, command, series/provider where applicable, `request_start`, `request_end`, build ID after creation, stage, status; sanitize secrets; stable exit codes for validation/provider/persistence/publication errors.
- R8: End-to-end offline regression covers empty bootstrap followed by normal next-day delta request, recent revision inside overlap, assertion that historical minimum is never requested by `run-daily`, full-file provider logical-window filtering, date-range exact bounds, explicit separate old-history reconcile, shortened source, affected Silver months, Gold causality/schema, publication/materialized views, strict/fallback resolution, retention, no-op rerun, inventory.
- R9: Document daily cron/systemd examples for `run-daily` as delta-only, persistent lake path, FRED secret injection, overlap configuration, and a **separate optional explicit `reconcile` schedule**; no scheduled GitHub Actions ingestion.

Acceptance:
- A1 (verifies R1): all commands parse and layer boundaries/Gold publication path are exact.
- A2 (verifies R2): Gold materialized-view recovery repairs or stops safely and cannot trigger source full-history fetching.
- A3 (verifies R3): canonical existing-history fixture `min=2000-01-03,max=2026-08-18,today=2026-08-19,overlap=7` makes `run-daily` request only `2026-08-11..2026-08-19`; spies prove no source reconcile/max-history/min-date path is invoked.
- A4 (verifies R4): only explicit `reconcile` command can request maximum history; scheduler docs keep it separate from daily update.
- A5 (verifies R5): default 13/filter/unknown/full-Gold behavior exact.
- A6 (verifies R6): failure injection proves exact pre/post commit durable truth.
- A7 (verifies R7): log/exit fixtures contain exact request window context and no secret.
- A8 (verifies R8): full bootstrap/delta/revision/window-filter/explicit-reconcile/publication/resolution/retention/no-op scenario passes offline.
- A9 (verifies R9): README documents delta-only daily execution and optional separate reconciliation; no scheduled CI ingestion exists.

## PR-24: Enforce Backlog Git Metadata And Design-Pattern Governance

Status: Merged

Updated: 2026-08-19

PR: #9

Git branch: `pr-24/backlog-governance-contracts`

Git status: `merged`

Agent lane: Governance; one agent only

Depends on: PR-01

Commit: `chore(pr-24): enforce backlog governance contracts`

Design patterns: Specification/Policy Object, Command validation, Dependency Injection where validation inputs are externalized.

Description:
- R1: Synchronize all existing PR entries with actual GitHub delivery state and preserve exact `Git branch`, `Git status`, and PR-scoped Conventional Commit metadata.
- R2: Require every PR entry to declare non-empty `Design patterns`; pattern use is mandatory when it materially improves coupling/testability/lifecycle clarity, but cargo-cult patterns are forbidden.
- R3: Extend Git status vocabulary with `pushed-ci-failing` so pushed branches with failing required checks are represented truthfully instead of mislabeled active or green.
- R4: Add an offline executable backlog contract test that parses every `PR-XX` section and rejects missing/duplicate PR IDs, missing Git metadata, branch/commit PR-ID mismatch, invalid Conventional Commit syntax/type, invalid Git status, or missing Design-pattern metadata.
- R5: Keep `AGENTS.md` synchronized with the executable contract and require exact backlog branch plus PR-scoped Conventional Commits before commit/push.

Acceptance:
- A1 (verifies R1): every PR-01..PR-24 entry records its verified merged GitHub PR number, exact head branch, and `Merged/merged` delivery state.
- A2 (verifies R2): every PR-01..PR-24 section contains one non-empty `Design patterns` field and the docs explicitly forbid pattern-for-pattern's-sake implementation.
- A3 (verifies R3): allowed status tests accept `pushed-ci-failing`, reject unknown states, and reserve `pushed-ci-green` for all-green required remote gates.
- A4 (verifies R4): parser proves exactly 24 PR sections and all metadata contracts; negative fixtures for missing branch/status/pattern and PR-ID/commit mismatch fail deterministically.
- A5 (verifies R5): AGENTS and BACKLOG state the same branch/commit/status/pattern rules without contradiction.

## PR-25: Add Saturday Daily Update Cron Template

Status: Merged

Updated: 2026-08-21

PR: #27

Git branch: `pr-25/saturday-daily-update-cron`

Git status: `merged`

Agent lane: Operations; one agent only

Depends on: PR-23, PR-24

Commit: `feat(pr-25): add saturday daily update cron template`

Design patterns: Command; Architectural baseline only for declarative host scheduling.

Description:
- R1: Provide a versioned, installable host-crontab template that invokes only the existing `run-daily` command on Saturday at 10:00 local deployment-host time; it must use a persistent lake path and append logs.
- R2: Document installation, the exact schedule, host-local-time assumption, and the fact that the scheduled command remains delta-only and does not schedule source reconciliation.
- R3: Add offline regression coverage that guards the cron expression, command, persistent lake path, and absence of a scheduled GitHub Actions ingestion workflow.

Acceptance:
- A1 (verifies R1): the template has exactly `0 10 * * 6`, invokes `run-daily`, uses `/srv/market-regime/lake`, and redirects stdout/stderr to the operational log.
- A2 (verifies R2): README gives the install command and confirms Saturday 10:00 host-local execution with no implicit source reconciliation.
- A3 (verifies R3): offline tests validate the template and retain the no-scheduled-CI policy.

## PR-26: Repair Live Provider Compatibility

Status: Merged

Updated: 2026-08-22

PR: #28

Git branch: `pr-26/repair-live-provider-compatibility`

Git status: `merged`

Agent lane: Provider maintenance; one agent only

Depends on: PR-07, PR-08, PR-24

Commit: `fix(pr-26): repair live provider compatibility`

Design patterns: Adapter, Strategy, Dependency Injection.

Description:
- R1: Adapt the STOXX VSTOXX parser to accept the provider's current `Indexvalue` value-column spelling without weakening schema, numeric, or duplicate validation.
- R2: Supply Yahoo's chart endpoint with a stable client User-Agent and ignore only all-null OHLC bars, so the registered MOVE adapter can access its live source without fabricating observations while preserving bounded-request and retry behavior.
- R3: Add offline regression coverage for both live-source compatibility cases and retain the backlog metadata contract.

Acceptance:
- A1 (verifies R1): a semicolon-delimited `Date;Symbol;Indexvalue` V2TX fixture parses to the expected scalar observations.
- A2 (verifies R2): the Yahoo request has the stable User-Agent, all-null OHLC bars are ignored, partially missing bars fail, and exact update bounds remain unchanged.
- A3 (verifies R3): provider tests and the complete offline quality gate pass.

## PR-27: Parallelize Silver And Gold Polars Work

Status: Merged

Updated: 2026-08-22

PR: #29

Git branch: `pr-27/parallelize-polars-silver-gold`

Git status: `merged`

Agent lane: Performance; one agent only

Depends on: PR-23, PR-24

Commit: `perf(pr-27): parallelize silver and gold polars work`

Design patterns: Strategy, Dependency Injection, Facade/Orchestrator.

Description:
- R1: Use an explicit injected execution policy sized from Polars' thread pool so independent Silver builds and reads run concurrently without exceeding Polars' available-core bound.
- R2: Execute independent volatility and macro Gold feature families concurrently before deterministic canonical assembly.
- R3: Test the all-core policy and retain deterministic result ordering and existing pipeline behavior.

Acceptance:
- A1 (verifies R1): default policy worker count equals `polars.thread_pool_size()` and its map preserves input order.
- A2 (verifies R2): Gold feature-family calls use the execution policy before assembly.
- A3 (verifies R3): offline quality gate passes.

## PR-28: Add Post-Publication Gold Mirror

Status: Merged

Updated: 2026-08-22

PR: #30

Git branch: `pr-28/gold-publication-mirror`

Git status: `merged`

Agent lane: Operations; one agent only

Depends on: PR-21, PR-24

Commit: `feat(pr-28): add post-publication gold mirror`

Design patterns: Adapter, Repository, State Machine, Dependency Injection.

Description:
- R1: After the authoritative catalog promotion and root-view refresh succeed, optionally mirror the complete Gold directory through an injected adapter.
- R2: Configure the rsync destination through `MARKET_MACRO_GOLD_MIRROR_ROOT`; mirroring failure must not roll back an already-complete catalog row.
- R3: Document the configured host mirror and test the exact rsync semantics.

Acceptance:
- A1 (verifies R1): mirror runs only after catalog promotion and materialized-view refresh.
- A2 (verifies R2): rsync mirrors directory contents with archive, partial-transfer, and delayed-delete semantics.
- A3 (verifies R3): configuration and offline tests are exact.

## PR-29: Add Protected Operational YAML Configuration

Status: Merged

Updated: 2026-08-22

PR: #31

Git branch: `pr-29/operational-yaml-config`

Git status: `merged`

Agent lane: Operations; one agent only

Depends on: PR-25, PR-28

Commit: `feat(pr-29): add protected operational yaml config`

Design patterns: Adapter, Command, Dependency Injection.

Description:
- R1: Export protected operational YAML settings as shell-safe cron environment assignments.
- R2: Keep local `config.yaml` out of version control while validating required runtime settings.

Acceptance:
- A1 (verifies R1): exporter emits shell-safe required values.
- A2 (verifies R2): missing settings fail safely and config is ignored by Git.

## PR-30: Add Diagnostic Gold Feature Profile

Status: Merged

Updated: 2026-08-22

PR: #32

Git branch: `pr-30/diagnostic-gold-feature-profile`

Git status: `merged`

Agent lane: Gold presentation; one agent only

Depends on: PR-20, PR-24

Commit: `feat(pr-30): add diagnostic gold feature profile`

Design patterns: Adapter, Materialized View.

Description:
- R1: Replace the coverage-only Gold PNG with a dark diagnostic sheet containing a time series, distribution, coverage, and summary statistics for every canonical feature.
- R2: Preserve immutable sidecar semantics and render missing features explicitly without fabricating values.
- R3: Test deterministic valid diagnostic PNG generation.

Acceptance:
- A1 (verifies R1): every feature has a time-series and histogram panel with summary context.
- A2 (verifies R2): null-only features render as no-data panels.
- A3 (verifies R3): PNG contract tests pass offline.

## Definition Of MVP Complete

MVP is complete only when PR-01 through PR-24 are merged and:

- `main` is actually protected with the five required checks, squash-only PR merging, auto-merge enabled, and no direct/force/delete path;
- implementation branches/commits follow the PR Git contract;
- every backlog PR has explicit Git branch, truthful Git status, PR-scoped Conventional Commit, and Design-pattern metadata;
- established patterns are applied where they reduce coupling/testability/lifecycle ambiguity, without unnecessary pattern layering;
- local/remote gates run four execution checks in parallel plus combined production coverage >=90%;
- application follows documented ports/adapters and no provider conditional ladder leaks into orchestration;
- first execution bootstraps maximum history only when Bronze is empty;
- every normal `update`/`run-daily` for existing history derives its request start from **max stored observation date minus overlap**, never from historical minimum, and ends at injected today;
- `date_range` providers perform exact bounded network requests for normal update; `full_file` providers may download full remote files only because of provider capability but must filter to the logical delta window before normal diff/persistence;
- source full-history `reconcile` is explicit only and never auto-triggered by `run-daily`;
- Bronze/Silver update only affected monthly partitions and normal source omission never deletes retained history;
- Gold uses only canonical UTC `timestamp_m1`, explicit causal feature math, no NaN/infinity, and explicit semantic versions;
- every build is immutable Parquet + artifact manifest + deterministic plot with reproducibility hashes;
- build JSON never conflicts with catalog publication state because publication lifecycle exists only in `manifest.parquet`;
- catalog resolution is pure/policy-driven, strict-current by default, filesystem-recency independent;
- atomic catalog replacement is the Gold publication commit point; root JSON/PNG are recoverable materialized views;
- retention tombstones catalog rows before deleting bundles and cannot create a selectable partial bundle;
- daily pipeline logs exact delta request bounds, handles pre/post commit failures correctly, and remains network-free in required integration tests;
- README, ARCHITECTURE, AGENTS, and BACKLOG remain synchronized.

## PostgreSQL Gold Serving Plane

This section consolidates the former `BACKLOG_POSTGRES.md`. Only the canonical
Gold dataset is replicated to PostgreSQL; Parquet Gold remains authoritative.
The target is `10.10.1.3:54321`, the application role is exactly
`macro-loader`, and credentials are runtime-only and never committed,
logged, or persisted. PostgreSQL temporal storage follows the shared
`pg-temporal-v1` contract used by `xetra-loader` and `crypto-loader`: every
persisted instant is exactly `TIMESTAMPTZ(6)`, every PostgreSQL session is UTC,
the persistence boundary accepts only aware UTC zero-offset microsecond values,
and true date-only values remain `DATE`. PostgreSQL stores typed instants rather
than a string format; sanitized acceptance artifacts serialize instants as
`YYYY-MM-DDTHH:MM:SS.ffffffZ` for diagnostics only. The first synchronization
loads the complete current Gold history; later synchronizations reconcile the
complete row-digest state, including missed runs and historical corrections.
Sync logs use the existing `${PROJECT_ROOT}/.logs/macro-loader.log` path.

Dependency graph:

```text
PR-31 -> PR-32 -> PR-33
   |       |\
   |       +-> PR-34 -> PR-37 -> PR-38 -> PR-39
   +-------> PR-35 -----^   \
   +-------> PR-36 -----------+

PR-39 -> PR-40 -> PR-41 -> PR-42
```

## PR-31: Backlog PostgreSQL Sync Plan

PR name: `backlog-postgres-sync-plan`
Status: Merged
Updated: 2026-08-22
PR: #33
Git branch: `pr-31/backlog-postgres-sync-plan`
Git status: `merged`
Agent lane: Governance; one agent only
Depends on: none
Commit: `docs(pr-31): backlog-postgres-sync-plan add postgres sync backlog`
Design patterns: Specification/Policy Object; Architectural baseline only.

Description:
- R1: Define PR-31 through PR-39 with exact dependencies, Git metadata, and one-to-one requirements and acceptance criteria.
- R2: Define Gold-only serving to PostgreSQL at `10.10.1.3:54321`; Parquet Gold remains authoritative.
- R3: Define the dedicated `macro-loader` runtime role and protected credential handling.
- R4: Define UTC `timestamp_m1` storage as `TIMESTAMPTZ(6)` and observation-day identity.
- R5: Define complete bootstrap and complete accumulated-delta reconciliation semantics.
- R6: Define the shared project log path and an executable offline governance contract.

Acceptance:
- A1 (verifies R1): PR-31 through PR-39 metadata is complete and unique.
- A2 (verifies R2): the contract names only canonical Gold as the PostgreSQL source.
- A3 (verifies R3): role and credential rules contain no operational secret.
- A4 (verifies R4): the timestamp and UTC contracts are explicit.
- A5 (verifies R5): bootstrap, missed-run, and historical-revision behavior is explicit.
- A6 (verifies R6): the single project log path is explicit and tested.

## PR-32: PostgreSQL Gold Sync Contracts

PR name: `postgres-gold-sync-contracts`
Status: Merged
Updated: 2026-08-22
PR: #34
Git branch: `pr-32/postgres-gold-sync-contracts`
Git status: `merged`
Agent lane: Foundation; one weak agent
Depends on: PR-31
Commit: `feat(pr-32): postgres-gold-sync-contracts define gold sync boundary`
Design patterns: Ports and Adapters, Repository, Value Object, Dependency Injection.

Description:
- R1: Define the Gold-to-PostgreSQL dataset and internal sync-table contracts.
- R2: Define immutable sync state, row digest, delta plan, and result value objects.
- R3: Define a narrow application `GoldSyncRepository` protocol with no `psycopg` import.
- R4: Require exact schema/feature compatibility, catalog-current complete-build selection, UTC sessions, and redaction.

Acceptance:
- A1 (verifies R1): only canonical Gold is publishable.
- A2 (verifies R2): all value objects and result counts are typed and immutable.
- A3 (verifies R3): application contracts remain adapter-independent.
- A4 (verifies R4): incompatible or non-current sources fail deterministically.

## PR-33: Deterministic Gold Row Delta Planner

PR name: `gold-row-delta-planner`
Status: Merged
Updated: 2026-08-22
PR: #37
Git branch: `pr-33/gold-row-delta-planner`
Git status: `merged`
Agent lane: Application planning; one agent only
Depends on: PR-32
Commit: `feat(pr-33): gold-row-delta-planner compute complete gold delta`
Design patterns: Strategy, Value Object, Dependency Injection.

Description:
- R1: Plan deterministic insert/update/delete/unchanged sets from complete source and target digests.
- R2: Support empty-target bootstrap, stale keys, historical revisions, missed runs, and no-op checkpoints.
- R3: Preserve stable ordering, exact counts, and credential-free contracts.

Acceptance:
- A1 (verifies R1): mixed deltas contain exactly the expected keys and counts.
- A2 (verifies R2): bootstrap and accumulated missed-run reconciliation converge.
- A3 (verifies R3): repeated planning is deterministic and credential-free.

## PR-34: PostgreSQL Gold Sync Adapter

PR name: `postgres-gold-sync-adapter`
Status: Merged
Updated: 2026-08-22
PR: #38
Git branch: `pr-34/postgres-gold-sync-adapter`
Git status: `merged`
Agent lane: Agent B; PostgreSQL persistence
Depends on: PR-32
Commit: `feat(pr-34): postgres-gold-sync-adapter implement transactional repository`
Design patterns: Adapter, Repository, Unit of Work, Dependency Injection.

Description:
- R1: Add `psycopg` as the only PostgreSQL client and configure the exact endpoint, role, database, protected password, and UTC timezone.
- R2: Implement idempotent consumer and internal sync-table DDL with exact Gold types.
- R3: Read only state/digests for comparison and apply scoped insert/update/delete deltas under an advisory transaction lock.
- R4: Commit sync state last, roll back all mutations together, and redact credentials.

Acceptance:
- A1 (verifies R1): dependency and connection identity are exact.
- A2 (verifies R2): DDL, keys, columns, and internal tables are exact and idempotent.
- A3 (verifies R3): reads and mutation ordering avoid full-target reloads.
- A4 (verifies R4): failures roll back and diagnostics contain no credentials.

## PR-35: Provision Dedicated PostgreSQL Service Role

PR name: `postgres-service-role-provisioning`
Status: Merged
Updated: 2026-08-22
PR: #35
Git branch: `pr-35/postgres-service-role-provisioning`
Git status: `merged`
Agent lane: PostgreSQL operations; one weak agent
Depends on: PR-31
Commit: `feat(pr-35): postgres-service-role-provisioning add least privilege role setup`
Design patterns: Command, Least Privilege, Idempotent Provisioning.

Description:
- R1: Provision or validate exactly the `macro-loader` LOGIN role at the dedicated endpoint.
- R2: Enforce least-privilege attributes and only the `macro_loader` and `macro_loader_sync` schema rights.
- R3: Keep administrator and runtime credentials separate, protected, redacted, and idempotent; incompatible state fails safely.

Acceptance:
- A1 (verifies R1): endpoint and role identity are exact.
- A2 (verifies R2): all required role attributes and schema grants are exact.
- A3 (verifies R3): credential separation, idempotency, and failure behavior pass offline.

## PR-36: PostgreSQL Sync Operational Config

PR name: `postgres-sync-operational-config`
Status: Merged
Updated: 2026-08-22
PR: #36
Git branch: `pr-36/postgres-sync-operational-config`
Git status: `merged`
Agent lane: Operations; one weak agent
Depends on: PR-31
Commit: `feat(pr-36): postgres-sync-operational-config add repository postgres settings`
Design patterns: Adapter, Dependency Injection.

Description:
- R1: Extend protected ignored YAML config with exact PostgreSQL host, port, role, database, and password settings.
- R2: Export shell-safe `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, and `PGPASSWORD`; validate and redact failures.
- R3: Define `${PROJECT_ROOT}/.logs/macro-loader.log` as the canonical log path.

Acceptance:
- A1 (verifies R1): valid settings resolve exactly.
- A2 (verifies R2): export, validation, quoting, and redaction pass offline.
- A3 (verifies R3): config/log paths are ignored and canonical.

## PR-37: Gold To PostgreSQL Complete Delta Sync

PR name: `gold-postgres-delta-sync`
Status: Merged
Updated: 2026-08-22
PR: #39
Git branch: `pr-37/gold-postgres-delta-sync`
Git status: `merged`
Agent lane: Integration; one agent only
Depends on: PR-33, PR-34
Commit: `feat(pr-37): gold-postgres-delta-sync synchronize complete gold state`
Design patterns: Facade/Orchestrator, Unit of Work, Repository, Dependency Injection.

Description:
- R1: Resolve only the catalog-current compatible Gold build and reconcile its complete row-digest state.
- R2: Bootstrap every row, apply accumulated insert/update/delete deltas, propagate historical corrections, and perform no unrelated pipeline work.
- R3: Verify post-write bounds/counts, preserve prior state on failure, and return typed credential-free counts.

Acceptance:
- A1 (verifies R1): only current Gold and sync repositories are called.
- A2 (verifies R2): bootstrap, no-op, mixed delta, missed-run, revision, and delete cases are exact.
- A3 (verifies R3): verification failure and retry preserve consistency and result fields are exact.

## PR-38: PostgreSQL Gold Sync CLI

PR name: `postgres-gold-sync-cli`
Status: Merged
Updated: 2026-08-22
PR: #40
Git branch: `pr-38/postgres-gold-sync-cli`
Git status: `merged`
Agent lane: CLI/composition; one weak agent
Depends on: PR-35, PR-36, PR-37
Commit: `feat(pr-38): postgres-gold-sync-cli expose repository postgres synchronization`
Design patterns: Command, Dependency Injection.

Description:
- R1: Add exactly one `gold-sync-postgres` command composed from protected runtime variables and existing event logging.
- R2: Keep the command read-only toward local source/Gold production and report structured success/failure counts without secrets.

Acceptance:
- A1 (verifies R1): parser and dispatch expose exactly the command and exact connection settings.
- A2 (verifies R2): no-side-effect, structured-output, redaction, and stable error tests pass.

## PR-39: Sunday PostgreSQL Gold Sync Cron

PR name: `sunday-postgres-gold-sync-cron`
Status: Merged
Updated: 2026-08-22
PR: #41
Git branch: `pr-39/sunday-postgres-gold-sync-cron`
Git status: `merged`
Agent lane: Operations; one weak agent
Depends on: PR-38
Commit: `feat(pr-39): sunday-postgres-gold-sync-cron chain gold sync after daily run`
Design patterns: Command; Architectural baseline only for declarative scheduling.

Description:
- R1: Schedule the host-local chain at exactly `0 10 * * 0`.
- R2: Load protected config, create `.logs`, run `run-daily`, and run `gold-sync-postgres` only after success, using one log.
- R3: Preserve local Gold on PostgreSQL failure, provide a sync-only retry, and keep source reconciliation separate.

Acceptance:
- A1 (verifies R1): Sunday expression and no Saturday main schedule are exact.
- A2 (verifies R2): order, `&&` gating, log path, and full/delta semantics are tested.
- A3 (verifies R3): failure/retry, no-reconcile, no-secret, and no-scheduled-GitHub-ingestion cases pass.

## Repository Audit Corrective Program

The audit performed on 2026-08-24 supersedes any interpretation that PR-31 through PR-39 alone establish production correctness. The following corrective work orders cover repository governance, CI realism, scheduler safety, provider parsing, Gold provenance/integrity, PostgreSQL transaction/schema/role correctness, and final production reconstruction. PR-61 is the only authorized destructive/reload gate in this program; the earlier temporal-only rewrite concept is superseded and must not be executed independently.

Shared PostgreSQL temporal contract `pg-temporal-v1`:

- every persisted instant is exactly `TIMESTAMPTZ(6)`;
- every PostgreSQL session is UTC;
- the PostgreSQL persistence boundary accepts only timezone-aware zero-offset UTC datetimes;
- true calendar dates remain `DATE`;
- `TIMESTAMP WITHOUT TIME ZONE` and timestamp precision other than six digits are forbidden;
- diagnostic serialization, where required outside PostgreSQL, is `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

Corrective dependency graph:

```text
PR-40 audit plan
  |-- PR-41 temporal boundary
  |-- PR-42 repository governance
  |-- PR-43 real PostgreSQL CI
  |-- PR-44 Sunday runner cwd/Git identity -> PR-45 runner lock -> PR-46 scheduler timezone
  |-- PR-47 provider secret-safe errors
  |-- PR-48 scalar provider numeric validation
  |-- PR-49 OHLC semantic validation
  |-- PR-50 Gold formula manifest -> PR-51 Gold input provenance -> PR-52 PG bundle integrity
  |-- PR-59 documentation accuracy

PR-43 -> PR-53 PostgreSQL lock-before-plan UoW
PR-41 + PR-43 -> PR-54 PostgreSQL schema contract
PR-54 -> PR-55 admin/runtime DDL separation -> PR-56 runtime role hardening
PR-43 + PR-52 + PR-53 + PR-54 + PR-56 -> PR-57 consumer integrity
PR-43 + PR-53 -> PR-58 PostgreSQL timeout policy
PR-41 + PR-43 + PR-46 + PR-52 + PR-53 + PR-54 + PR-56 + PR-57 + PR-58 -> PR-60 live conformance
PR-41..PR-60 -> PR-61 authoritative source/Gold/PostgreSQL reconstruction
```

Safe first parallel wave after PR-40: PR-41, PR-42, PR-43, PR-44, PR-47, PR-48, PR-49, PR-50, and PR-59. Do not start a PR until every explicit `Depends on` item is merged.

## PR-40: Repository Audit And PostgreSQL Temporal Conformance Plan

PR name: `postgres-temporal-conformance-plan`
Status: In Progress
Updated: 2026-08-28
PR: #42
Git branch: `pr-40/postgres-temporal-conformance-plan`
Git status: `active-clean`
Agent lane: Planning/governance; one agent only
Depends on: PR-39
Commit: `docs(pr-40): expand repository audit corrective backlog`
Design patterns: Specification/Policy Object; Architectural baseline only.

Description:
- R1: Freeze `pg-temporal-v1` and record the complete audit findings without claiming that PostgreSQL stores datetime strings or that the live target is already conformant.
- R2: Replace the early temporal-only rewrite sequence with atomic PR-41 through PR-61 and make PR-61 the sole destructive/reload authority after all correctness prerequisites are merged.
- R3: Record the verified repository defects: actual GitHub governance differs from documented policy; required CI has no real PostgreSQL service; the Sunday runner does not establish repository working directory/Git identity or a chain-wide lock; provider error and parser boundaries are weaker than their contracts; Gold provenance/integrity is incomplete; and PostgreSQL planning, schema, role, timeout, and consumer-verification boundaries are insufficiently fail-closed.
- R4: Make the general backlog validator accept only a contiguous `PR-01..PR-N` sequence instead of hard-coding PR-39, and scope the historical PostgreSQL backlog validator to its original PR-31..PR-39 contract so future unrelated corrective PRs do not create circular CI failures.
- R5: Repair the two pre-existing Ruff-format blockers observed in `ingestion/yahoo_provider.py` and `tests/unit/test_postgres_gold_repository.py` without changing runtime semantics.
- R6: Require final production completion to depend on PR-61 real-target reconstruction and independent `PASS` evidence rather than fixture/unit evidence or the legacy PR-31..PR-39 completion claim.

Acceptance:
- A1 (verifies R1): the shared type/precision/timezone/date rules are exact and live conformance remains explicitly unproven until PR-60/PR-61.
- A2 (verifies R2): PR-41 through PR-61 are contiguous, atomic, dependency-complete work orders and no earlier PR authorizes destructive production reconstruction.
- A3 (verifies R3): every listed finding is traceable to current repository code/settings and no speculative live-data corruption is asserted.
- A4 (verifies R4): adding a contiguous future PR no longer breaks backlog validation, while gaps, malformed metadata, and legacy PR-31..PR-39 violations still fail.
- A5 (verifies R5): `ruff format --check .` has no failure from the two audited pre-existing files and the diffs are formatting-only.
- A6 (verifies R6): completion text states that PR-61 real-target evidence is mandatory.

## PR-41: Harden PostgreSQL UTC Persistence And Read Boundaries

PR name: `postgres-temporal-boundary-hardening`
Status: Planned
Updated: 2026-08-24
PR: #67
Git branch: `pr-41/postgres-temporal-boundary-hardening`
Git status: `not-started (branch absent)`
Agent lane: PostgreSQL temporal contracts; one weak agent
Depends on: PR-40
Commit: `fix(pr-41): enforce postgres utc boundary`
Design patterns: Value Object, Specification/Policy Object, Adapter.

Description:
- R1: Change `application.postgres_sync._utc` so naive datetimes and every aware datetime with non-zero `utcoffset()` are rejected; upstream code may normalize before the persistence boundary, but the boundary itself must not silently reinterpret `+01:00`, `+02:00`, or another offset.
- R2: Harden PostgreSQL row decoding so every timestamp returned from the driver is timezone-aware and zero-offset UTC before constructing sync state, digests, or summaries.
- R3: Add focused unit tests for UTC pass-through, naive rejection, non-zero-offset rejection, and invalid database-returned timestamp values without adding live-database concerns to this PR.

Acceptance:
- A1 (verifies R1): UTC values pass unchanged; naive, `+01:00`, `+02:00`, and another non-zero offset fail deterministically.
- A2 (verifies R2): fake driver UTC values pass and fake naive/non-UTC values fail before authoritative state construction.
- A3 (verifies R3): the temporal unit suite covers every boundary and the existing sync behavior remains unchanged for valid UTC inputs.

## PR-42: Repair Actual GitHub Repository Governance

PR name: `repository-governance-repair`
Status: Planned
Updated: 2026-08-24
PR: TBD
Git branch: `pr-42/repository-governance-repair`
Git status: `not-started (branch absent)`
Agent lane: Repository administration; one agent only
Depends on: PR-40
Commit: `chore(pr-42): repair repository governance`
Design patterns: Specification/Policy Object, Idempotent Provisioning.

Description:
- R1: Apply the repository policy already documented by the project: protect `main`, require pull requests, block force-push/deletion/direct-push bypass for normal actors, require `lint|type|unit|integration|coverage`, and require the branch to be up to date or merge-queue compatible.
- R2: Configure squash-only merging, disable merge commits and rebase merges, enable repository auto-merge, and enable automatic deletion of merged head branches.
- R3: Add an executable read-back/drift verifier that queries actual GitHub settings and fails if repository state differs from the source-controlled contract; the setup command remains idempotent and fails visibly on insufficient admin permission.

Acceptance:
- A1 (verifies R1): GitHub read-back proves `main` is protected with the exact required checks and prohibited push/delete behavior.
- A2 (verifies R2): repository settings prove squash-only, auto-merge enabled, merge/rebase disabled, and merged head deletion enabled.
- A3 (verifies R3): a second setup run is a no-op and an injected/settings mismatch makes the verifier non-zero with an actionable sanitized message.

## PR-43: Run Real PostgreSQL Integration Tests In CI

PR name: `real-postgres-ci`
Status: Planned
Updated: 2026-08-24
PR: TBD
Git branch: `pr-43/real-postgres-ci`
Git status: `not-started (branch absent)`
Agent lane: CI/database integration; one weak agent
Depends on: PR-40
Commit: `ci(pr-43): add real postgres integration gate`
Design patterns: Testcontainer/Service Fixture, Dependency Injection.

Description:
- R1: Provision a disposable supported PostgreSQL service in the required `integration` CI job and expose only test credentials scoped to that job.
- R2: Add real-psycopg integration coverage for schema creation/migration, UTC session behavior, transactions/rollback, advisory locking, row round-trip, and sync-state/digest operations; do not silently skip these tests in CI.
- R3: Keep provider/network tests excluded and keep production target `10.10.1.3:54321` unreachable from required CI; tests must use only the disposable service.

Acceptance:
- A1 (verifies R1): CI logs prove a disposable PostgreSQL instance is created and the integration job fails if it is unavailable.
- A2 (verifies R2): the listed database behaviors execute against real PostgreSQL and deliberate SQL/type/transaction regressions fail.
- A3 (verifies R3): required CI performs no provider network calls and contains no route/credential to the production database.

## PR-44: Make Sunday Runner Working Directory And Git Identity Deterministic

PR name: `sunday-runner-working-directory`
Status: Planned
Updated: 2026-08-24
PR: TBD
Git branch: `pr-44/sunday-runner-working-directory`
Git status: `not-started (branch absent)`
Agent lane: Operations; one weak agent
Depends on: PR-40
Commit: `fix(pr-44): stabilize sunday runner cwd`
Design patterns: Command, Fail-Fast Preflight.

Description:
- R1: Make `ops/run-macro-loader-sunday.sh` `cd` to the resolved repository root before invoking any CLI command so fallback `git rev-parse HEAD` is deterministic when cron starts from an arbitrary home/root directory.
- R2: Resolve and export the exact repository commit identity once before `run-daily`; fail before data mutation when Git identity cannot be resolved instead of publishing an untraceable Gold build.
- R3: Add an offline runner test that launches the script from an unrelated working directory with faked commands and proves the expected repository root/Git SHA are propagated.

Acceptance:
- A1 (verifies R1): invocation from an unrelated cwd reaches the CLI from the repository root and does not depend on caller cwd.
- A2 (verifies R2): a valid SHA is exported once; Git-resolution failure prevents `run-daily` and PostgreSQL sync.
- A3 (verifies R3): the regression test fails against the pre-PR runner and passes after the fix.

## PR-45: Add One Single-Instance Lock Around The Sunday Chain

PR name: `sunday-runner-single-instance-lock`
Status: Planned
Updated: 2026-08-24
PR: TBD
Git branch: `pr-45/sunday-runner-single-instance-lock`
Git status: `not-started (branch absent)`
Agent lane: Operations concurrency; one weak agent
Depends on: PR-44
Commit: `fix(pr-45): lock sunday publication chain`
Design patterns: Command, Mutex/Lease, Fail-Fast Guard.

Description:
- R1: Acquire one non-blocking host lock before `run-daily` and hold it through the subsequent `gold-sync-postgres` command so duplicate cron/manual runner invocations cannot concurrently mutate the lake/catalog and serving state.
- R2: Use a deterministic lock path outside immutable Gold build directories, cleanly release it on every exit path, and return a distinct non-zero status when another chain owns the lock.
- R3: Add offline tests for first acquisition, second-process rejection, release after success/failure, and guarantee that PostgreSQL sync never starts when the chain lock is not held.

Acceptance:
- A1 (verifies R1): two concurrent runner fixtures execute at most one `run-daily -> gold-sync-postgres` chain.
- A2 (verifies R2): lock contention is fail-fast, sanitized, and leaves no false success.
- A3 (verifies R3): success/failure/contention cases prove exact lock lifetime and command gating.

## PR-46: Freeze An Explicit Europe/Vienna Scheduler Timezone

PR name: `scheduler-timezone-contract`
Status: In Progress
Updated: 2026-08-28
PR: none
Git branch: `pr-46/scheduler-timezone-contract`
Git status: `pushed-ci-green`
Agent lane: Scheduler semantics; one weak agent
Depends on: PR-45
Commit: `fix(pr-46): freeze scheduler timezone contract`
Design patterns: Configuration Contract; Architectural baseline only.

Description:
- R1: Replace the ambiguous host-local Sunday schedule with an explicit IANA timezone contract `Europe/Vienna` while preserving Sunday 10:00 wall-clock execution.
- R2: Make the installed cron representation and README/ARCHITECTURE wording agree on timezone and DST behavior; do not create a second scheduler.
- R3: Add offline schedule tests covering winter and summer offset expectations and the exact `0 10 * * 0` schedule under the explicit timezone.

Acceptance:
- A1 (verifies R1): the installed scheduler explicitly names `Europe/Vienna` and fires at Sunday 10:00 Vienna local time.
- A2 (verifies R2): docs and cron contain one non-contradictory scheduler contract and source reconciliation remains separately operator-invoked.
- A3 (verifies R3): DST regression fixtures prove the intended UTC offset changes without changing local 10:00 semantics.

## PR-47: Prevent Provider Secrets From Surviving Exception Causes

PR name: `provider-secret-safe-errors`
Status: Planned
Updated: 2026-08-24
PR: TBD
Git branch: `pr-47/provider-secret-safe-errors`
Git status: `not-started (branch absent)`
Agent lane: Provider security; one weak agent
Depends on: PR-40
Commit: `fix(pr-47): sanitize provider exception chains`
Design patterns: Adapter, Sanitized Error Boundary.

Description:
- R1: Prevent `httpx.TransportError` causes containing the original request URL/params from remaining attached to the public `ProviderHttpError` when a request may contain credentials such as the FRED `api_key`.
- R2: Preserve safe provider/series/source/category/status/path context while guaranteeing secrets are absent from `str`, `repr`, formatted traceback, exception context, and exception cause chains.
- R3: Add tests using a sentinel secret embedded in a real httpx request object for immediate and retry-exhausted transport failures.

Acceptance:
- A1 (verifies R1): the sentinel API key cannot be recovered from the raised error or its chained exceptions.
- A2 (verifies R2): safe operational context remains available and provider retry semantics are unchanged.
- A3 (verifies R3): traceback/cause/repr/string scans fail before the fix and pass after it.

## PR-48: Fail Closed On Invalid Scalar Provider Values

PR name: `scalar-provider-numeric-validation`
Status: Planned
Updated: 2026-08-24
PR: TBD
Git branch: `pr-48/scalar-provider-numeric-validation`
Git status: `not-started (branch absent)`
Agent lane: Provider data validation; one weak agent
Depends on: PR-40
Commit: `fix(pr-48): validate scalar provider numerics`
Design patterns: Adapter, Specification/Policy Object.

Description:
- R1: For FRED, treat only documented missing tokens (`null`, blank, `.`) as absent; an unexpected non-numeric or non-finite value must fail instead of being silently dropped.
- R2: For ECB scalar payloads, distinguish legitimate missing observations from malformed/non-numeric `OBS_VALUE`; malformed or non-finite values must fail rather than disappearing through `strict=False` casting/filtering.
- R3: Add fixtures for documented missing values, malformed text, NaN/infinity, duplicates, bounded-window behavior, and one valid value for every affected provider family.

Acceptance:
- A1 (verifies R1): documented FRED missing observations remain gaps and unexpected invalid numerics fail deterministically.
- A2 (verifies R2): ECB missing semantics remain supported while malformed/non-finite observations fail.
- A3 (verifies R3): all fixtures prove that data corruption cannot be converted silently into a market-data gap.

## PR-49: Enforce OHLC Market-Bar Invariants

PR name: `ohlc-semantic-validation`
Status: Planned
Updated: 2026-08-24
PR: TBD
Git branch: `pr-49/ohlc-semantic-validation`
Git status: `not-started (branch absent)`
Agent lane: Provider data validation; one weak agent
Depends on: PR-40
Commit: `fix(pr-49): enforce ohlc invariants`
Design patterns: Specification/Policy Object, Adapter.

Description:
- R1: Add one reusable OHLC specification for CBOE and Yahoo/MOVE requiring finite non-negative index levels, `high >= max(open, close)`, `low <= min(open, close)`, and `high >= low` for every accepted bar.
- R2: Apply the specification after parsing and before Bronze persistence without changing bounded/full-file request semantics or missing-row rules.
- R3: Add valid boundary fixtures and independent negative fixtures for negative levels, high/open, high/close, low/open, low/close, and inverted high/low.

Acceptance:
- A1 (verifies R1): every declared OHLC invariant is enforced identically by both affected provider adapters.
- A2 (verifies R2): valid historical fixtures remain unchanged and invalid bars never reach Bronze.
- A3 (verifies R3): each invalid relation has a focused failing regression test with deterministic provider-safe diagnostics.

## PR-50: Make Gold Feature-Formula Fingerprints Complete

PR name: `gold-formula-fingerprint-completeness`
Status: Planned
Updated: 2026-08-24
PR: TBD
Git branch: `pr-50/gold-formula-fingerprint-completeness`
Git status: `not-started (branch absent)`
Agent lane: Gold semantics; one weak agent
Depends on: PR-40
Commit: `fix(pr-50): complete gold formula fingerprints`
Design patterns: Specification/Policy Object, Value Object.

Description:
- R1: Derive the feature-set formula manifest from the actual volatility and macro policy objects and include every semantic parameter used by Gold, including macro 5/20-observation lags and explicit identifiers for term spreads/ratios and missing-value rules.
- R2: Remove duplicated hard-coded volatility shifts by using `VolatilityFeaturePolicy.delta_lags` consistently while preserving the currently frozen `(5,20)` outputs.
- R3: Add deterministic tests proving any schema/version/formula/policy-parameter change changes `feature_set_hash`, while identical semantics produce identical hashes.

Acceptance:
- A1 (verifies R1): the manifest contains all currently executed macro/volatility/term semantics and no undocumented semantic input is omitted.
- A2 (verifies R2): existing feature values and column names remain byte/numerically identical for the frozen policy.
- A3 (verifies R3): one-at-a-time semantic mutations change the hash and an unchanged rebuild does not.

## PR-51: Persist Exact Silver Inputs In Every Gold Build Manifest

PR name: `gold-input-provenance-manifest`
Status: In Progress
Updated: 2026-08-28
PR: none
Git branch: `pr-51/gold-input-provenance-manifest`
Git status: `pushed-ci-failing`
Agent lane: Gold provenance; one weak agent
Depends on: PR-50
Commit: `feat(pr-51): persist gold input provenance`
Design patterns: Value Object, Builder, Immutable Manifest.

Description:
- R1: Extend the immutable Gold build manifest with an explicit manifest version and the ordered `SilverInputSignature` set already computed by `GoldFrameBuild`: series ID, row count, min/max observation date, and SHA-256 for every canonical source series.
- R2: Thread `GoldFrameBuild.inputs` through publication instead of discarding it, and validate manifest/readback identity before catalog promotion.
- R3: Preserve backward readability of existing build manifests as legacy/non-certified artifacts, but require the new provenance fields for every newly published current build after this PR.

Acceptance:
- A1 (verifies R1): every new build manifest contains exactly 13 ordered input signatures matching independently recomputed Silver signatures.
- A2 (verifies R2): missing/reordered/wrong signature, count, bounds, or hash prevents promotion of the candidate build.
- A3 (verifies R3): legacy manifests can be identified/read for history but cannot be newly emitted or treated as provenance-certified current builds.

## PR-52: Verify Immutable Gold Bundle Integrity Before PostgreSQL Sync

PR name: `postgres-gold-bundle-integrity`
Status: In Progress
Updated: 2026-08-28
PR: none
Git branch: `pr-52/postgres-gold-bundle-integrity`
Git status: `active-clean`
Agent lane: Gold/PostgreSQL boundary; one weak agent
Depends on: PR-51
Commit: `fix(pr-52): verify gold bundle before postgres sync`
Design patterns: Specification/Policy Object, Adapter, Fail-Closed Verification.

Description:
- R1: Before constructing a PostgreSQL source snapshot, load the catalog-selected immutable build manifest and verify build/dataset identity, path containment, schema/feature versions, data SHA-256, row count, timestamp bounds, feature-set hash, Git identity, and PR-51 Silver-input provenance against the actual selected bundle.
- R2: Reject modified/replaced/truncated Parquet, mismatched manifest, path substitution, legacy non-certified current manifest, or missing sidecar before any PostgreSQL connection/mutation.
- R3: Add focused tamper fixtures for each checked field and prove a valid current bundle produces the same logical rows/digests as before.

Acceptance:
- A1 (verifies R1): independently recomputed bundle metadata matches every certified manifest field before sync planning starts.
- A2 (verifies R2): every tamper/missing/legacy-current case fails with zero PostgreSQL mutations.
- A3 (verifies R3): valid bundle synchronization input is unchanged except for the added integrity gate.

## PR-53: Acquire PostgreSQL Lock Before Reading And Planning

PR name: `postgres-lock-before-plan-uow`
Status: In Progress
Updated: 2026-08-28
PR: TBD
Git branch: `pr-53/postgres-lock-before-plan-uow`
Git status: `active-clean`
Agent lane: PostgreSQL concurrency; one agent only
Depends on: PR-43
Commit: `fix(pr-53): lock postgres before delta planning`
Design patterns: Unit of Work, Repository, Advisory Lock.

Description:
- R1: Move the advisory transaction lock to the start of one repository Unit of Work so target sync state and digests are read only after lock acquisition, the delta is planned under that same lock/transaction, mutations and verification run there, and state is committed last.
- R2: Use a stable namespaced advisory-lock key rather than relying solely on 32-bit `hashtext(dataset_id)` collision space, and expose a narrow application callback/transaction port without importing psycopg into application code.
- R3: Add a real PostgreSQL two-session integration test proving the second sync cannot plan from stale pre-lock state and converges idempotently after the first commits.

Acceptance:
- A1 (verifies R1): event trace is exactly lock -> read target -> plan -> mutate -> verify -> state -> commit for each sync.
- A2 (verifies R2): lock identity is deterministic/namespaced and application remains adapter-independent.
- A3 (verifies R3): concurrent identical and conflicting-source test cases produce no PK race/stale plan and end in the expected single authoritative state.

## PR-54: Enforce Complete PostgreSQL Schema Contracts And Versioned Migrations

PR name: `postgres-schema-contract-migrations`
Status: In Progress
Updated: 2026-08-24
PR: TBD
Git branch: `pr-54/postgres-schema-contract-migrations`
Git status: `active-clean`
Agent lane: PostgreSQL schema; one agent only
Depends on: PR-41, PR-43
Commit: `feat(pr-54): enforce postgres schema migrations`
Design patterns: Specification/Policy Object, Migration, Fail-Closed Verification.

Description:
- R1: Define the exact consumer/sync catalog contract for column names/order, PostgreSQL types, timestamp precision, nullability, primary/unique keys, hash widths, and allowed extra objects; `CREATE TABLE IF NOT EXISTS` alone is not schema validation.
- R2: Introduce explicit ordered/idempotent loader-owned schema migrations with a schema-version ledger; incompatible drift, missing migration, or unexpected timestamp/type/key shape fails before data mutation.
- R3: Add real PostgreSQL integration fixtures for compatible schema, missing column, extra forbidden column, wrong type, wrong timestamp precision, wrong nullability/key, and rerun idempotency.

Acceptance:
- A1 (verifies R1): exact introspection of every owned serving/sync table is source-controlled and checked.
- A2 (verifies R2): forward migrations apply exactly once, drift cannot be hidden by `IF NOT EXISTS`, and unrelated schemas are never modified.
- A3 (verifies R3): every deliberate schema drift fails closed and a clean schema/migration rerun is a no-op.

## PR-55: Remove Schema DDL From The Normal Runtime Sync Path

PR name: `postgres-admin-runtime-ddl-separation`
Status: In Progress
Updated: 2026-08-28
PR: #62
Git branch: `pr-55/postgres-admin-runtime-ddl-separation`
Git status: `active-clean`
Agent lane: PostgreSQL privilege boundary; one weak agent
Depends on: PR-54
Commit: `refactor(pr-55): separate postgres admin ddl`
Design patterns: Ports and Adapters, Command, Least Privilege.

Description:
- R1: Move schema creation/migration to an explicit admin/migration command using protected admin credentials; normal `gold-sync-postgres` must never create/alter/drop schemas or tables.
- R2: Replace runtime `ensure_schema()` behavior with read-only schema-contract preflight that fails if PR-54 migrations are missing/incompatible.
- R3: Keep admin and runtime configuration distinct in code, CLI, cron export, docs, errors, and tests; neither credential may be logged or committed.

Acceptance:
- A1 (verifies R1): normal runtime SQL contains no DDL and the explicit admin command is the only loader path that can apply migrations.
- A2 (verifies R2): compatible schema passes preflight; missing/drifted schema fails before row mutation.
- A3 (verifies R3): configuration/test scans prove credential separation and redaction across both paths.

## PR-56: Harden The Runtime PostgreSQL Role To DML-Only

PR name: `postgres-runtime-role-hardening`
Status: In Progress
Updated: 2026-08-28
PR: #63
Git branch: `pr-56/postgres-runtime-role-hardening`
Git status: `pushed-ci-failing`
Agent lane: PostgreSQL security; one agent only
Depends on: PR-55
Commit: `fix(pr-56): harden postgres runtime role`
Design patterns: Least Privilege, Idempotent Provisioning, Permission Probe.

Description:
- R1: Make `macro-loader` a non-owner LOGIN runtime principal with only the exact DML/USAGE permissions required for normal sync; remove schema ownership and `CREATE` privileges from the runtime role.
- R2: Own loader schemas/tables through a separate non-login/admin-managed owner and make provisioning/migration idempotently repair grants/default privileges without touching unrelated roles/schemas.
- R3: Add actual permission probes proving runtime SELECT/INSERT/UPDATE/DELETE as required while CREATE/ALTER/DROP/GRANT and unrelated-schema access fail.

Acceptance:
- A1 (verifies R1): catalog/privilege introspection proves the runtime role is non-owner, non-superuser, non-CREATEDB/CREATEROLE and lacks schema CREATE.
- A2 (verifies R2): owner/runtime grants are deterministic, least-privilege, and unrelated database objects remain unchanged.
- A3 (verifies R3): positive DML and negative DDL/escalation probes run against real PostgreSQL and fail the gate on any excess privilege.

## PR-57: Verify Actual Consumer Rows, Digest Index, And Sync State Agree

PR name: `postgres-consumer-integrity-verification`
Status: In Progress
Updated: 2026-08-28
PR: #64
Git branch: `pr-57/postgres-consumer-integrity-verification`
Git status: `pushed-ci-failing`
Agent lane: PostgreSQL correctness; one agent only
Depends on: PR-43, PR-52, PR-53, PR-54, PR-56
Commit: `fix(pr-57): verify postgres consumer integrity`
Design patterns: Reconciliation, Specification/Policy Object, Unit of Work.

Description:
- R1: Independently derive logical keys and row digests from the actual consumer table and require exact symmetric-key and digest equality with the certified Gold source and `gold_row_hashes`; count/min/max alone is insufficient.
- R2: Validate `gold_sync_state` source fingerprint/schema/version/count/bounds against both source and actual consumer on every sync, including the apparent no-op/same-data fast path; state absence is valid only when the serving and digest targets are empty or an explicit repair/rebuild command is used.
- R3: Require exact DML affected-row counts for update/delete operations and roll back if a planned existing key is missing or an unexpected row is affected.
- R4: Add real PostgreSQL tamper tests for changed feature value, missing/extra consumer row, changed/missing digest, stale state, false no-op, and zero-target bootstrap.

Acceptance:
- A1 (verifies R1): source, consumer, and digest index have zero symmetric key differences and equal per-row digests after every successful sync.
- A2 (verifies R2): stale/tampered state or non-empty target without state cannot be reported as success/no-op.
- A3 (verifies R3): wrong affected-row count aborts the transaction before state advancement.
- A4 (verifies R4): every tamper fixture fails and a clean bootstrap/delta/no-op path passes exactly.

## PR-58: Bound PostgreSQL Connection, Lock, And Statement Waits

PR name: `postgres-timeout-policy`
Status: In Progress
Updated: 2026-08-28
PR: TBD
Git branch: `pr-58/postgres-timeout-policy`
Git status: `active-clean`
Agent lane: PostgreSQL resilience; one weak agent
Depends on: PR-43, PR-53
Commit: `fix(pr-58): bound postgres operation waits`
Design patterns: Policy Object, Fail-Fast Adapter.

Description:
- R1: Define source-controlled positive bounds for connection establishment, advisory-lock waiting, SQL statement execution, and idle-in-transaction time; configure them on every runtime/admin session before data work.
- R2: Give the connection an explicit application name and convert timeout/lock-contention failures to sanitized typed operational errors without credentials/DSNs.
- R3: Add real PostgreSQL tests that deliberately hold the advisory lock and run a long statement to prove the configured bounds fail instead of hanging indefinitely.

Acceptance:
- A1 (verifies R1): all four wait classes have explicit tested bounds and invalid/non-positive configuration is rejected.
- A2 (verifies R2): server session metadata shows the expected application name/timeouts and diagnostics contain no secret.
- A3 (verifies R3): lock/statement timeout fixtures terminate within the contract and leave no committed partial state.

## PR-59: Remove Documentation Drift And Clarify Revision/Vintage Semantics

PR name: `documentation-contract-accuracy`
Status: In Progress
Updated: 2026-08-28
PR: #65
Git branch: `pr-59/documentation-contract-accuracy`
Git status: `pushed-ci-failing`
Agent lane: Documentation/governance; one weak agent
Depends on: PR-40
Commit: `docs(pr-59): correct repository contract documentation`
Design patterns: Specification/Policy Object; Architectural baseline only.

Description:
- R1: Remove obsolete references to standalone `BACKLOG_POSTGRES.md` and make `BACKLOG.md` the only PostgreSQL backlog source of truth across README/ARCHITECTURE/AGENTS.
- R2: Clarify that Bronze/Silver preserve historical observation dates and the latest accepted value for each natural key, while equal-key provider revisions replace prior values; the lake is not a provider-vintage/time-travel archive unless a future explicit vintage journal is added.
- R3: Synchronize docs with the corrected Sunday runner/timezone, PostgreSQL admin/runtime separation, least-privilege role, certified Gold bundle, and PR-61 completion/rebuild rules after their owning PRs merge; do not duplicate executable configuration in prose.

Acceptance:
- A1 (verifies R1): repository search finds no live instruction to edit/use `BACKLOG_POSTGRES.md` and identifies one backlog authority.
- A2 (verifies R2): revision semantics cannot be reasonably read as retaining every provider vintage and remain consistent with current equal-key upsert behavior.
- A3 (verifies R3): README/ARCHITECTURE/AGENTS/BACKLOG contain no contradiction with source-controlled operational/schema contracts.

## PR-60: Add Independent Live PostgreSQL Conformance Verifier

PR name: `postgres-live-conformance-verifier`
Status: Merged
Updated: 2026-08-28
PR: #66
Git branch: `pr-60/postgres-live-conformance-verifier`
Git status: `merged`
Agent lane: Production verification; one agent only
Depends on: PR-41, PR-43, PR-46, PR-52, PR-53, PR-54, PR-56, PR-57, PR-58
Commit: `test(pr-60): verify live postgres conformance`
Design patterns: Specification/Policy Object, Read-Only Verifier, Fail-Closed Verification.

Description:
- R1: Add a read-mostly command for exact target `10.10.1.3:54321` that independently introspects all loader-owned schema columns/types/precision/nullability/keys, role ownership/grants, UTC session and configured timeout/application identity.
- R2: Verify certified current Gold bundle versus actual consumer rows, actual consumer-derived digests, hash index, sync state, row count, timestamp bounds, and semantic/schema versions without trusting the mutation path's own success result.
- R3: Execute transaction-scoped microsecond round-trip probes at `2026-03-29T00:59:59.123456Z` and `2026-10-25T01:30:00.654321Z`, require exact instant/microsecond preservation, then roll back with zero durable probe objects/rows.
- R4: Emit a deterministic sanitized report with `temporal_contract_version=pg-temporal-v1`, checked schema/roles/data summaries and `PASS|FAIL`; no password, DSN, raw market rows, or provider secret may be emitted.

Acceptance:
- A1 (verifies R1): compatible live catalog/session/roles pass and any wrong type/precision/key/ownership/grant/timezone fails closed.
- A2 (verifies R2): any consumer/digest/state/source mismatch prevents `PASS`.
- A3 (verifies R3): both DST-adjacent UTC instants round-trip identically to six microseconds and leave no durable probe state.
- A4 (verifies R4): the report is sanitized/deterministic and cannot be marked `PASS` if any prior assertion fails.

## PR-61: Authoritative Source Reconcile, Gold Rebuild, And PostgreSQL Rewrite

PR name: `authoritative-production-reconstruction`
Status: Merged
Updated: 2026-09-19
PR: #67
Git branch: `pr-61/authoritative-production-reconstruction`
Git status: `merged`
Agent lane: Production cutover; one agent only
Depends on: PR-41, PR-42, PR-43, PR-44, PR-45, PR-46, PR-47, PR-48, PR-49, PR-50, PR-51, PR-52, PR-53, PR-54, PR-55, PR-56, PR-57, PR-58, PR-59, PR-60
Commit: `chore(pr-61): reconstruct production serving state`
Design patterns: Command, Unit of Work, Backup/Restore, Fail-Closed Verification.

Description:
- R1: Enter a documented maintenance window: disable the Sunday chain, acquire the PR-45 runner lock plus the PostgreSQL maintenance/advisory lock, and preflight exact target `10.10.1.3:54321`; abort on another target or concurrent writer.
- R2: Before any destructive/reconciling step, create and validate operator-controlled backups/snapshots of `macro_loader`, `macro_loader_sync`, current Gold catalog/build/manifest evidence, and lake state required to restore the pre-cutover serving lineage; record private checksums/restore instructions without committing credentials or raw market data.
- R3: Run explicit full source `reconcile` for all 13 registered series under PR-47/48/49 provider contracts, preserving local history where shorter sources do not imply deletion; rebuild all Silver and publish one new certified current Gold build carrying PR-50/51 formula and input provenance.
- R4: Through the PR-55 admin path, force one controlled recreation/migration of only `macro_loader` and `macro_loader_sync` from current canonical PR-54 DDL even if the old schema appears compatible; preserve unrelated schemas, roles, and other repositories exactly.
- R5: Republish the complete new current Gold into the empty serving target through the PR-56 least-privilege runtime path and require exact source/consumer/hash-index/state reconciliation under PR-57.
- R6: Run PR-60 independently and require exact schema/types/keys/roles/permissions, zero source-consumer key/digest differences, matching row counts/bounds/versions, UTC `TIMESTAMPTZ(6)` semantics, and successful microsecond probes.
- R7: Immediately rerun unchanged `gold-sync-postgres` and require exactly zero inserts, updates, deletes, digest changes, or state-semantic rewrites; run the guarded Sunday wrapper in non-destructive verification mode and prove it uses the corrected cwd/lock/timezone contract.
- R8: Commit only a sanitized `artifacts/acceptance/postgres-production-reconstruction-v2.json` report and mark `PASS` only when backup validation, source reconcile, Gold certification, schema recreation, full publication, independent verification, permission probes, and zero-mutation replay all succeed; re-enable scheduling only after `PASS`.

Acceptance:
- A1 (verifies R1): no normal writer/scheduler can overlap reconstruction and only the exact production endpoint can proceed.
- A2 (verifies R2): validated restore evidence exists before the first source reconcile/schema recreation and can restore the previous serving lineage.
- A3 (verifies R3): all 13 source series are explicitly reconciled and the new current Gold is provenance-certified from corrected Silver inputs/formula semantics.
- A4 (verifies R4): before/after catalog proves only loader-owned PostgreSQL schemas were recreated/migrated and unrelated objects are unchanged.
- A5 (verifies R5): complete certified Gold, actual consumer, digest index, and sync state are exactly equivalent after publication.
- A6 (verifies R6): PR-60 produces `PASS`, including role/schema/data/temporal checks and zero symmetric differences.
- A7 (verifies R7): immediate unchanged replay has exactly zero semantic mutations and corrected scheduled-wrapper behavior is proven without a second destructive/bootstrap path.
- A8 (verifies R8): the committed report contains no credentials/raw provider payloads, is `PASS`, and scheduling remains disabled on any failed assertion.

## PR-62: Add One-Observation Deltas To Every Source Feature

PR name: `gold-one-observation-deltas`
Status: Merged
Updated: 2026-08-27
PR: #43
Git branch: `pr-62/delta-one-observation-features`
Git status: `merged`
Agent lane: Gold/PostgreSQL feature delivery; one agent only
Depends on: PR-40
Commit: `feat(pr-62): add one-observation feature deltas`
Design patterns: Strategy, Adapter, Repository, Versioned Migration.

Description:
- R1: Add a causal `delta_1obs` feature for each of the 13 canonical source levels, calculated as the current valid observation minus the immediately preceding valid observation without calendar filling.
- R2: Add the 13 `delta_1obs` columns to canonical Gold in deterministic feature-family order, increment `schema_version` to 2, and include the one-observation horizon in build formula metadata and documentation.
- R3: Migrate the PostgreSQL consumer table idempotently with nullable `DOUBLE PRECISION` `delta_1obs` columns, permit only the explicit schema 1-to-2 delta-sync transition, and synchronize the new current Gold build.

Acceptance:
- A1 (verifies R1): focused volatility and macro feature tests prove the first value is null, the next valid observation has the exact one-observation difference, and all 13 source series expose the feature.
- A2 (verifies R2): Gold schema/order, sidecar formula hash, semantic-version, unit, and offline integration tests prove the new immutable schema contract.
- A3 (verifies R3): repository tests prove idempotent additive DDL and fail-closed incompatible versions; a live delta sync verifies all 13 columns exist and are populated from the schema-2 current build.

## PR-63: Parallelize Required Offline Test Execution

PR name: `parallelize-offline-tests`
Status: Merged
Updated: 2026-08-28
PR: #53
Git branch: `pr-63/parallelize-offline-tests`
Git status: `merged`
Agent lane: Test infrastructure; one agent only
Depends on: PR-40
Commit: `perf(pr-63): parallelize offline test execution`
Design patterns: Configuration Policy, Command.

Description:
- R1: Execute the required offline unit and integration suites with a bounded default worker count while preserving their current markers, test selection, and coverage collection.
- R2: Permit operators and CI to override the worker count through one documented Make variable without changing source code.
- R3: Keep the combined coverage threshold and all mandatory quality-gate stages unchanged.

Acceptance:
- A1 (verifies R1): both required suites complete successfully with isolated parallel workers and the integration suite remains provider-network-free.
- A2 (verifies R2): `TEST_WORKERS=1` provides deterministic serial fallback and a higher value is accepted by the Make targets.
- A3 (verifies R3): `make quality-gate` still runs lint, type, unit, integration, and the unchanged combined coverage threshold.

## PR-64: Deduplicate Pull Request Quality-Gate Triggers

PR name: `deduplicate-pr-ci-triggers`
Status: Merged
Updated: 2026-08-28
PR: #56
Git branch: `pr-64/deduplicate-pr-ci-triggers`
Git status: `merged`
Agent lane: CI infrastructure; one agent only
Depends on: PR-40
Commit: `ci(pr-64): deduplicate pull request test runs`
Design patterns: Configuration Policy, Command.

Description:
- R1: Run required quality gates once per pull request revision through `pull_request`, while preserving a `push` run for the protected `main` branch and the required merge-queue run.
- R2: Retain the exact required `lint`, `type`, `unit`, `integration`, and combined `coverage` jobs and their artifact handoff.

Acceptance:
- A1 (verifies R1): a non-main PR-branch push cannot trigger a duplicate `push` workflow alongside its `pull_request` workflow.
- A2 (verifies R2): main pushes and merge-queue revisions retain all required quality gates without rerunning either test suite inside `coverage`.

## PR-65: Accelerate Gold Integration Test Rendering

PR name: `fast-gold-test-renderer`
Status: Merged
Updated: 2026-08-28
PR: #58
Git branch: `pr-65/fast-gold-test-renderer`
Git status: `merged`
Agent lane: Test infrastructure; one agent only
Depends on: PR-40
Commit: `perf(pr-65): inject fast Gold test renderer`
Design patterns: Dependency Injection, Adapter.

Description:
- R1: Keep the production Gold profile renderer unchanged while allowing integration tests that do not inspect pixels to inject a deterministic valid PNG renderer.
- R2: Preserve genuine Matplotlib rendering coverage in the dedicated sidecar integration test.

Acceptance:
- A1 (verifies R1): publication, retention, and daily E2E tests produce valid immutable PNG sidecars without constructing large plot figures.
- A2 (verifies R2): the dedicated sidecar test continues to validate a genuine PNG render and malformed renderer output is rejected.

### Corrected Production Completion Gate

The repository's earlier MVP and PR-31..PR-39 completion statements describe historical implementation milestones only. Production serving correctness is not certified until PR-41 through PR-60 are merged and PR-61 has completed the authoritative source reconcile, certified Gold rebuild, loader-owned PostgreSQL reconstruction, independent real-target verification, and zero-mutation replay with a sanitized `PASS` report. Until then, no existing PostgreSQL data should be treated as independently verified merely because unit/fake-adapter tests or count/min/max checks pass.

## PR-66: Add Official Fed Policy Expectations And FOMC Clock Features

PR name: `fed-policy-expectations-features`
Status: Merged
Updated: 2026-09-19
PR: #76
Git branch: `pr-66/fed-policy-expectations-features`
Git status: merged
Agent lane: Gold/policy expectations; one agent only
Depends on: PR-70
Commit: `feat(pr-66): add Fed policy expectations and macro serving views`
Design patterns: Adapter, Repository, Strategy/Policy Object, Versioned Migration.

Description:
- R1: Ingest only official CME FedWatch exports from `https://www.cmegroup.cn/fed-watch/`, official Federal Reserve EFFR data, and the official FOMC calendar; do not call paid CME APIs or substitute unofficial probabilities.
- R2: Persist normalized FedWatch outcome snapshots with explicit `23:59:59.999999 UTC` end-of-day availability and preserve the maximum history exposed by the free source; unavailable dates remain null.
- R3: Add exactly `fed_next_expected_move_bp`, `fed_next_uncertainty_bp`, `fed_m3_expected_move_bp`, and `fed_repricing_5obs_bp`; no other Fed-family derived features.
- R4: Define the third-future-meeting selection rule, preserve causal daily keys, add immutable Gold/versioned PostgreSQL migration support, and test formulas, source limits, EOD boundaries, and no-look-ahead semantics offline.

Acceptance:
- A1: CME-methodology outcome probabilities produce the specified weighted mean and standard deviation exactly on hand-calculable fixtures.
- A2: The M3 feature sums expected moves through exactly the third scheduled decision date strictly after observation date; repricing uses only the prior five-observation value.
- A3: EOD availability is validated exactly and missing free-history snapshots are not filled or fabricated.
- A4: FOMC dates come from the official calendar adapter.
- A5: Gold/PostgreSQL schema and semantic versions migrate idempotently, with all required quality gates passing.

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
