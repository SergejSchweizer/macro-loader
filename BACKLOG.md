# Backlog

This backlog is the implementation source of truth for `macro-loader`.

The repository loads reusable daily market-state inputs from open/public sources, preserves source history, performs strict incremental updates during normal execution, and publishes deterministic immutable Gold feature snapshots through a Bronze -> Silver -> Gold architecture.

Last reviewed: 2026-09-23

## Current repository and production status

As of 2026-09-23, the Fed-policy reconstruction, `macro_raw` exposure, and
`macro_features` transformations are implemented. The unresolved issue is historical
source coverage, not formula availability.

The repository now has enough evidence to distinguish three different facts that were
previously conflated:

1. The Chinese CME FedWatch page really was used to download official
   `MeetingExport.aspx` CSVs.
2. Those raw CSV files were **not retained**: the browser adapter reads the temporary
   Playwright download path into memory and discards the file after parsing.
3. The then-active pipeline called the source only for
   `today - 30 calendar days .. today`, and the parser discarded export rows outside
   that requested interval before persisting normalized snapshots.

The historical operational status recorded on 2026-09-19 therefore proves only a recent
slice: the serving table had 16,775 total macro rows, while the four Fed columns had
20, 20, 20, and 15 non-NULL observations respectively. The 15-row repricing count is
consistent with the five-valid-observation warm-up. This is **not** evidence that only
20 observations existed in the downloaded CME CSVs; it proves that only the bounded
30-day slice was retained.

Official CME documentation for the same FedWatch tool states that the historical panel for
a selected meeting goes back **one full year** and that raw historical probability data
can be downloaded. Therefore the first corrective action is to redownload the official
Chinese CME exports without the historical 30-day truncation and measure their actual
coverage. The residual history gap, not the entire 2010-present span, is what may require
ZQ-settlement reconstruction.

The four canonical Fed-policy origins remain:

```text
fed_next_expected_move_bp
fed_path_slope_m3_bp
fed_next_uncertainty_bp
fed_repricing_5obs_bp
```

For dates covered by official CME MeetingExport probability distributions, those official
probabilities are the preferred historical source. For older dates not covered by the
official exports, the existing PR-101 reconstruction requires the simultaneous individual
monthly ZQ final-settlement curve plus point-in-time EFFR/target/FOMC references. A
continuous/front-month future cannot substitute for that curve.

No repository documentation, QA artifact, PostgreSQL row, or downstream model may claim
verified 2010-present Fed-policy coverage until the corrected source-coverage and hybrid
history acceptance below pass.

## Corrective Delivery Program — Measure CME Export History Then Fill Only The Residual Gap

The source hierarchy is:

```text
official CME China MeetingExport probabilities
        |
        | direct probability source where actually available
        v
canonical Fed probability snapshots
        ^
        |
        | PR-101 reconstruction only for dates before direct export coverage
        |
individual monthly ZQ final settlements
+ point-in-time EFFR / target range / FOMC schedule
```

The corrected dependency chain is:

```text
PR-120 correct source/acceptance contract
        |
        v
PR-121 retain + inventory unfiltered CME China exports
        |
        v
PR-122 prove actual direct-export date/meeting coverage
        |
        v
PR-123 acquire/verify ZQ curves only for residual history gap
        |
        v
PR-124 build and seam-test hybrid probability history
        |
        v
PR-125 rebuild four origins + macro_raw + macro_features
        |
        v
PR-126 true 2010-present end-to-end acceptance
```

PR-123 is conditional on the residual gap measured by PR-122. If the direct official CME
exports unexpectedly cover the complete requested reconstruction domain, PR-123 becomes a
documented no-op. Otherwise it must fill exactly the uncovered historical interval and no
more.

## PR-120: Correct Fed Historical Source And Acceptance Contract

PR name: `fed-history-source-gate`
Status: In Progress
Updated: 2026-09-23
PR: #122
Git branch: `pr-120/fed-history-source-gate`
Git status: `active-clean`
Agent lane: Architecture/data-provenance correction; one agent only
Depends on: none
Commit: `docs(pr-120): measure cme export history before zq fallback`
Design patterns: Specification/Policy Object, Fail-Closed Verification.

Description:
- R1: Record the actual prior-download behavior: the Chinese CME browser path downloaded all exposed future-meeting `MeetingExport.aspx` files, but raw CSV bytes were temporary/unretained and normalized rows were filtered to the requested source interval before persistence.
- R2: Record the prior operational evidence exactly: on 2026-09-19 the serving copy had 16,775 total macro rows and the four Fed columns had 20/20/20/15 non-NULL observations; classify that as recent-slice evidence rather than source-history coverage.
- R3: Record the official direct-source contract: CME documents one full year of historical target-rate probabilities for a selected meeting and downloadable raw historical probability data; actual current export coverage must nevertheless be measured, not assumed.
- R4: Define source precedence: use official CME MeetingExport probability distributions directly where covered; use PR-101 ZQ reconstruction only for the older residual gap; supporting FRED/FOMC inputs and continuous futures never substitute for missing ZQ term structure.
- R5: Reclassify the earlier PR-108 work as an acceptance harness rather than proof of 2010-present historical population and make PR-121–126 prerequisites for any renewed verified-2010 claim.

Acceptance:
- A1 (verifies R1): BACKLOG and code references distinguish temporary raw downloads, filtered normalized persistence, and actual source coverage; no statement equates the retained 30-day slice with CME's full export contents.
- A2 (verifies R2): the 16,775 total-row and 20/20/20/15 Fed non-NULL evidence is documented with its correct scope and cannot satisfy a 2010-history assertion.
- A3 (verifies R3): the direct-source contract cites/measures one-year-style CME history and requires an emitted min/max/coverage manifest before any historical assumption is accepted.
- A4 (verifies R4): the backlog makes direct official probabilities primary where available, ZQ reconstruction residual-only, and explicitly rejects FRED/calendar/continuous-future substitution for a missing curve.
- A5 (verifies R5): no current status or acceptance item may claim verified 2010-present Fed history until PR-126 PASS exists.

## PR-121: Persist And Inventory Unfiltered Chinese CME Meeting Exports

PR name: `fed-history-cme-export-capture`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-121/fed-history-cme-export-capture`
Git status: `not-started (branch absent)`
Agent lane: Official CME direct-probability acquisition; one agent only
Depends on: PR-120
Commit: `feat(pr-121): retain cme fedwatch meeting export history`
Design patterns: Adapter, Repository, Ports and Adapters, Value Object.

Description:
- R1: Add an explicit historical-export command that navigates the official Chinese CME FedWatch/QuikStrike page and downloads every exposed future-meeting `MeetingExport.aspx` CSV without applying the normal 30-day observation filter.
- R2: Persist raw export bytes outside Git in a deterministic lake source directory keyed by fetch date/meeting date/content hash, rather than reading only the temporary Playwright path; never overwrite a different historical export silently.
- R3: Parse and normalize every CSV row to `observation_date`, `meeting_date`, target-rate bucket/move, probability, source URL/export identity, fetched-at time, content hash, and explicit availability/provenance metadata.
- R4: Preserve the raw official probability distributions exactly as published apart from deterministic normalization; reject malformed headers, invalid bucket syntax, duplicate outcomes, non-finite probabilities, or probability mass outside the declared tolerance.
- R5: Keep this historical capture separate from normal EOD cron execution: daily update remains bounded/delta-oriented, while complete export recapture is explicit operator-controlled reconcile/coverage work.
- R6: Add hermetic sanitized MeetingExport fixtures plus an opt-in real-browser smoke that records exposed meeting links, downloaded file count, hashes, and parsed date bounds without committing live/raw export payloads.

Acceptance:
- A1 (verifies R1): command trace proves every currently exposed MeetingExport link is attempted and no `start=today-30d` row truncation occurs in historical capture mode.
- A2 (verifies R2): successful capture leaves durable raw files/manifests with stable hashes; deleting the Playwright temp file after parsing does not remove the retained source.
- A3 (verifies R3): normalized rows round-trip exact observation/meeting/bucket/probability/provenance values and remain uniquely attributable to one raw export hash.
- A4 (verifies R4): invalid/duplicate/mass-error fixtures fail closed and valid official distributions retain exact probability mass within the source-controlled tolerance.
- A5 (verifies R5): repository call-path tests prove normal EOD cron cannot trigger full MeetingExport-history recapture implicitly.
- A6 (verifies R6): offline CI is fully hermetic; authorized browser smoke emits only sanitized coverage metadata and never commits live raw data.

## PR-122: Prove Actual Chinese CME FedWatch Export Coverage

PR name: `fed-history-cme-export-coverage-qa`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-122/fed-history-cme-export-coverage-qa`
Git status: `not-started (branch absent)`
Agent lane: Official direct-source coverage QA; one agent only
Depends on: PR-121
Commit: `test(pr-122): measure cme fedwatch export coverage`
Design patterns: End-to-End Test, Reconciliation, Differential Testing, Fail-Closed Verification.

Description:
- R1: Inventory the real retained MeetingExport files and emit global/per-meeting `min(observation_date)`, `max(observation_date)`, distinct observation count, distinct meeting count, raw row count, normalized outcome count, and content hashes.
- R2: Verify actual direct-feature sufficiency by observation date: a date is directly usable only if official exported distributions exist for the first, second, and third future known FOMC meetings required by the four-feature contract; partial meeting coverage is reported, not silently promoted to complete coverage.
- R3: Quantify contiguous direct-coverage intervals, holes, weekday/business-day patterns, per-meeting one-year windows, and the exact earliest observation date that can produce all four canonical features.
- R4: Independently compare overlapping observations across multiple meeting exports for the same meeting/date/bucket and require equality within one explicit probability tolerance; conflicts fail closed and retain both source hashes for audit.
- R5: Compare the real observed export coverage with the documented one-year CME behavior and classify any shorter/longer coverage explicitly rather than forcing the documentation expectation.
- R6: Emit deterministic sanitized `artifacts/acceptance/fed-history-cme-export-coverage-v1.json` containing source hashes/meeting identities, global/per-meeting bounds, direct-feature coverage intervals/gaps, conflict statistics, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): the artifact states the exact actual earliest/latest observation dates and counts from the newly retained official exports; no inferred or documentation-only date may substitute for measured data.
- A2 (verifies R2): each date claimed as direct four-feature coverage has distributions through meeting three; missing third-meeting support is visible as a hard coverage gap.
- A3 (verifies R3): the exact earliest fully usable direct date and every internal gap are reported, making the residual pre-direct interval mechanically derivable.
- A4 (verifies R4): all duplicate-overlap probabilities agree within tolerance or the artifact is FAIL with conflicting source hashes listed.
- A5 (verifies R5): observed-vs-documented coverage comparison is informational only; PASS is based on measured files, not an assumed one-year cutoff.
- A6 (verifies R6): artifact is deterministic/sanitized and sufficient to define the exact PR-123 residual history interval.

## PR-123: Acquire And Verify ZQ Curves For The Residual Historical Gap

PR name: `fed-history-zq-residual-gap`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-123/fed-history-zq-residual-gap`
Git status: `not-started (branch absent)`
Agent lane: Residual historical market-data acquisition/QA; one agent only
Depends on: PR-122
Commit: `feat(pr-123): fill residual fed history with zq curves`
Design patterns: Adapter, Repository, Strategy, Reconciliation.

Description:
- R1: Derive the exact required ZQ interval as `2010-01-01 .. day-before-earliest-complete-direct-CME-date` from the PR-122 artifact; do not acquire or reconstruct dates already covered by complete official MeetingExport probabilities except for overlap QA.
- R2: Probe the existing public date-specific CME ZQ settlement adapter first and record its real earliest usable date/contract breadth; only unresolved residual dates may use an operator-supplied Databento/vendor/licensed export.
- R3: Normalize only individual standard monthly ZQ **final EOD settlements** into the existing canonical ZQ store with observation date, contract month/symbol, settlement, source identity, source hash/URL, fetched/imported time, and availability metadata; reject continuous/back-adjusted/front-month-only inputs.
- R4: For every residual observation date, prove enough simultaneous contract-month breadth for PR-101 to reconstruct through the third known future FOMC meeting; missing required curves are hard gaps and no synthetic term structure is allowed.
- R5: Where ZQ sources overlap each other or overlap the direct-CME period, compare same-date/same-contract final settlements and resulting PR-101 probabilities under explicit source-controlled tolerances to detect source seams.
- R6: Emit deterministic sanitized `artifacts/acceptance/fed-history-zq-residual-coverage-v1.json` with actual source identities/hashes, min/max coverage, hard gaps, curve breadth, overlap/seam statistics, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): requested/acquired ZQ history is exactly the PR-122 residual interval plus explicit small overlap QA windows; complete direct-CME dates are not needlessly reconstructed as primary history.
- A2 (verifies R2): artifact distinguishes what the public CME settlement endpoint actually supplied from what required another source; no provider capability is assumed from documentation alone.
- A3 (verifies R3): all normalized rows are individual final-settlement contracts with deterministic provenance and every continuous/front-only input fails.
- A4 (verifies R4): every date counted as reconstructable successfully supplies PR-101 through meeting three; all missing-curve dates are enumerated and cannot be filled.
- A5 (verifies R5): each source transition/overlap is within declared settlement/probability tolerances or the QA artifact is FAIL.
- A6 (verifies R6): artifact deterministically defines the actual reconstructable residual domain and is license-safe/sanitized.

## PR-124: Assemble And Validate Hybrid Fed Probability History

PR name: `fed-history-hybrid-probabilities`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-124/fed-history-hybrid-probabilities`
Git status: `not-started (branch absent)`
Agent lane: Historical probability-source composition; one agent only
Depends on: PR-122, PR-123
Commit: `feat(pr-124): assemble hybrid fed probability history`
Design patterns: Strategy, Specification/Policy Object, Reconciliation.

Description:
- R1: Build one canonical probability-snapshot history with deterministic precedence: official CME MeetingExport distributions are authoritative where PR-122 marks complete direct coverage; PR-101 ZQ reconstruction supplies only older/residual dates approved by PR-123.
- R2: Preserve per-row source method (`cme_meeting_export` or `zq_reconstruction`), source-manifest hash, methodology version, and availability provenance so downstream features can be traced to the exact source path.
- R3: At the direct/reconstructed seam, maintain an overlap QA window in which both methods are computed independently and compare meeting/bucket distributions plus the four resulting canonical features under PR-106 tolerances; seam disagreement blocks publication.
- R4: Reject a date if the selected source lacks distributions through meeting three, contains conflicting probability mass, violates point-in-time meeting/reference availability, or is outside a PASS coverage artifact.
- R5: Keep source composition independent of `macro_raw`/`macro_features`; this PR publishes only the canonical probability history consumed by the existing feature builder.
- R6: Emit a sanitized hybrid-history manifest with source intervals, source hashes, seam errors, actual min/max reconstructable dates, hard gaps, and deterministic data hash.

Acceptance:
- A1 (verifies R1): every canonical probability row is sourced from exactly one declared primary method according to the PR-122/123 precedence contract with no duplicate-primary dates.
- A2 (verifies R2): lineage can trace any date/meeting/bucket back to an exact raw-export or ZQ-source manifest and methodology version.
- A3 (verifies R3): seam overlap compares both distributions and all four canonical features; any error above tolerance prevents publication.
- A4 (verifies R4): incomplete/causally unavailable dates are explicit gaps rather than fallback-filled rows.
- A5 (verifies R5): no PostgreSQL serving mutation occurs in this PR and the existing feature builder consumes the canonical snapshots unchanged.
- A6 (verifies R6): manifest is deterministic/sanitized and fully specifies source intervals/gaps required by PR-125.

## PR-125: Rebuild Canonical Fed Origins And PostgreSQL Serving History

PR name: `fed-history-rebuild-serving`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-125/fed-history-rebuild-serving`
Git status: `not-started (branch absent)`
Agent lane: Historical feature rebuild/serving reconciliation; one agent only
Depends on: PR-124
Commit: `feat(pr-125): rebuild fed serving history from hybrid probabilities`
Design patterns: Command, Unit of Work, Repository, Reconciliation.

Description:
- R1: Rebuild the four canonical Fed-policy origin features from the PR-124 canonical probability history using exactly the existing feature formulas and five-valid-observation repricing semantics; no history-only alternate formula exists.
- R2: Publish only dates approved by the hybrid source manifest, preserving explicit NULL/gap behavior and source/availability lineage; process the requested 2010-01-01 boundary through latest completed EOD without fabricating uncovered dates.
- R3: Reconcile the rebuilt four origins into `macro_raw` and rebuild/refresh the 28 approved Fed-derived columns in `macro_features` exclusively from `macro_raw`.
- R4: Persist source-manifest hash, probability-methodology/source-composition version, feature contract version, min/max dates, row counts, canonical data hash, PostgreSQL sync state, and materialized-view fingerprint.
- R5: Verify transactional atomicity, historical revision propagation, and idempotence: failed rebuild leaves prior serving state committed; immediate successful replay yields identical hashes and zero semantic mutations/refreshes.
- R6: Emit deterministic sanitized rebuild evidence linking PR-122/123/124 artifact hashes to canonical feature and PostgreSQL fingerprints.

Acceptance:
- A1 (verifies R1): every canonical origin value is independently reproducible from the approved PR-124 probability rows and existing formulas.
- A2 (verifies R2): serving coverage equals the approved hybrid domain exactly; every uncovered date remains explicit and attributable.
- A3 (verifies R3): `macro_raw` contains the four origins and `macro_features` contains those four plus all 28 approved derived columns computed from `macro_raw`, never a side table.
- A4 (verifies R4): all source/methodology/feature/PostgreSQL lineage hashes and date/count bounds reconcile deterministically.
- A5 (verifies R5): failure/revision/no-op scenarios have exact expected mutation and refresh behavior with no partial serving publication.
- A6 (verifies R6): rebuild artifact is deterministic/sanitized and references exact PASS upstream artifact hashes.

## PR-126: Execute True 2010-Present Fed History Acceptance

PR name: `fed-history-true-2010-acceptance`
Status: Planned
Updated: 2026-09-23
PR: not opened
Git branch: `pr-126/fed-history-true-2010-acceptance`
Git status: `not-started (branch absent)`
Agent lane: Production-like real-data historical acceptance; one agent only
Depends on: PR-122 PASS, PR-123 PASS, PR-124 PASS, PR-125 PASS
Commit: `test(pr-126): accept measured fed history from 2010`
Design patterns: End-to-End Acceptance, Differential Testing, Reconciliation, Fail-Closed Verification.

Description:
- R1: Execute the real-data chain in an authorized environment: retained direct CME exports -> residual ZQ coverage -> hybrid probability assembly -> four canonical origins -> `macro_raw` -> `macro_features` -> independent PostgreSQL verification.
- R2: Prove the exact actual reconstruction domain from requested 2010-01-01 through latest completed EOD using measured source coverage, reporting first reconstructable date, every expected non-reconstructable date/reason, direct-vs-reconstructed source interval, and all source manifest hashes.
- R3: Verify official direct-CME probabilities against reconstructed overlap probabilities and all four canonical features under PR-106 tolerances without treating the official one-year direct history as evidence for older dates.
- R4: Verify real PostgreSQL `macro_raw` four-origin parity and all 32 Fed columns in `macro_features`, including independent recomputation of 28 derived columns, warm-up NULL attribution, finite values, chronology, and no look-ahead.
- R5: Verify ongoing normal EOD continuation after the historical rebuild: bounded current source update must append/revise through the same canonical probability/feature/serving contracts, and immediate unchanged replay must produce zero semantic mutations/refreshes.
- R6: Commit only deterministic sanitized `artifacts/acceptance/fed-history-true-2010-v1.json` containing exact upstream artifact hashes, actual source/domain/gaps, overlap errors, PostgreSQL coverage, EOD replay evidence, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): evidence proves every real source and pipeline stage executed in dependency order; fixture/synthetic data cannot satisfy this acceptance.
- A2 (verifies R2): historical coverage claims derive from measured direct-export/ZQ source manifests rather than timestamps or planned boundaries; every gap is explicit.
- A3 (verifies R3): all direct-vs-reconstructed overlap distributions/features meet declared tolerances or acceptance is FAIL.
- A4 (verifies R4): real PostgreSQL contains exact canonical origins and independently correct derived features with all NULL/gap/warm-up causes accounted for.
- A5 (verifies R5): normal current EOD mutation and immediate no-op replay obey bounded-delta and one-refresh/zero-refresh contracts.
- A6 (verifies R6): artifact is deterministic/sanitized and PASS is impossible without the exact PR-122–125 PASS artifact hashes.

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
- PR-97–105: Fed-policy EOD contract, ZQ persistence/acquisition adapters, point-in-time references, probability reconstruction, four canonical features, PostgreSQL synchronization, delta orchestration, and daily cron machinery were delivered; these implementations do not by themselves prove possession of a 2010-capable historical ZQ curve.

- PR-106–118: CME differential-test machinery, PostgreSQL acceptance harnesses, Fed origin/materialized transformations, `macro_raw` integration, and QA were delivered. The historical 2026-09-19 evidence contained only 20/20/20/15 non-NULL Fed feature observations because the then-active source call retained only a 30-day slice; raw MeetingExport CSVs themselves were temporary and their full date bounds were not retained.

The active corrective backlog above begins with PR-120. Verified 2010-present Fed history may be claimed only after PR-122 measures the retained official CME export coverage, PR-123 proves the residual ZQ interval, and PR-126 completes the real-data acceptance.
