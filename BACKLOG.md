# Backlog

This backlog is the implementation source of truth for `macro-loader`.

The repository loads reusable daily market-state inputs from open/public sources, preserves source history, performs strict incremental updates during normal execution, and publishes deterministic immutable Gold feature snapshots through a Bronze -> Silver -> Gold architecture.

Last reviewed: 2026-09-23

## Current repository and production status

As of 2026-09-23, the Fed-policy implementation path through ZQ reconstruction,
`macro_raw`, and `macro_features` is implemented and the associated code/QA harnesses
through PR-118 are merged. However, the repository does **not** currently contain or
prove possession of the historical individual-contract ZQ settlement curve required to
reconstruct the four Fed-policy origins continuously from 2010.

This distinction is mandatory:

```text
implemented reconstruction algorithm != available historical source dataset
test/acceptance harness             != historical data coverage proof
```

The four canonical Fed-policy origins remain:

```text
fed_next_expected_move_bp
fed_path_slope_m3_bp
fed_next_uncertainty_bp
fed_repricing_5obs_bp
```

They require, for each historical EOD observation, the individual monthly CME 30-Day
Federal Funds futures (`ZQ`) settlement curve needed by the PR-101 probability engine.
FRED DFF/EFFR, Federal Reserve FOMC calendars, target-range history, and CME FedWatch
exports are supporting/reference inputs; none substitutes for the historical ZQ curve.
A continuous/front-month futures series is explicitly insufficient.

The previous PR-108 history-acceptance implementation is therefore classified as an
**acceptance harness**, not evidence that 2010-present Fed-policy data exists or passed.
No repository documentation, QA artifact, PostgreSQL row, or downstream model may claim
verified 2010 Fed-policy history until PR-120 through PR-124 complete with a real
historical ZQ dataset.

## Corrective Delivery Program — Historical ZQ Source Gate

### External data prerequisite

A real historical dataset must be supplied to the authorized runtime outside Git. It must
contain final EOD settlements for individual standard monthly ZQ contracts with sufficient
curve breadth to reconstruct probabilities through at least the third known future FOMC
meeting for each observation date.

Acceptable examples, subject to actual coverage verification, include:

- CME DataMine historical ZQ settlements;
- a Bloomberg export of individual ZQ contract final settlements;
- a Refinitiv/LSEG export of individual ZQ contract final settlements;
- another licensed/raw source that independently proves equivalent individual-contract
  final-settlement coverage and provenance.

Known limitations that must be encoded in QA rather than ignored:

- CME FedWatch API history begins in 2015 and cannot satisfy a 2010 backfill alone;
- Databento ZQ coverage begins 2010-06-06 and therefore cannot satisfy the requested
  2010-01-01 boundary alone;
- FRED DFF/EFFR and Federal Reserve FOMC calendars are reference inputs only;
- continuous futures such as Nasdaq/Quandl CHRIS-style series are not sufficient because
  they do not preserve the simultaneous individual-contract term structure.

No paid/vendor dataset bytes, credentials, or license-restricted raw payloads are committed
to Git. Git may contain only schemas, import code, sanitized manifests/hashes, and QA
artifacts permitted by the source license.

The corrected dependency chain is:

```text
PR-120 source/acceptance contract correction
        |
        v
PR-121 licensed historical ZQ import adapter
        |
        v
PR-122 raw curve coverage + provenance + seam QA
        |
        v
PR-123 rebuild four Fed origins + macro_raw + macro_features
        |
        v
PR-124 true 2010-present end-to-end acceptance
```

PR-121 and everything downstream remain BLOCKED until an operator supplies a source that
passes the PR-122 coverage contract. This is intentional fail-closed behavior.

## PR-120: Correct Historical ZQ Source And Acceptance Contract

PR name: `fed-history-source-gate`
Status: In Progress
Updated: 2026-09-23
PR: #122
Git branch: `pr-120/fed-history-source-gate`
Git status: `active-clean`
Agent lane: Architecture/data-provenance correction; one agent only
Depends on: none
Commit: `docs(pr-120): gate fed history on real zq curve data`
Design patterns: Specification/Policy Object, Fail-Closed Verification.

Description:
- R1: Correct the historical source contract so 2010-present reconstruction requires individual monthly ZQ final settlements with enough simultaneous contract-month coverage for the PR-101 probability engine through at least the third known future FOMC meeting; continuous/front-month series are forbidden.
- R2: Explicitly classify DFF/EFFR, target-range history, and FOMC calendars as reference inputs; classify CME FedWatch exports/API as validation/oracle inputs where available, not as the missing 2010 raw curve.
- R3: Record source-boundary facts in the backlog: FedWatch API history alone starts too late for 2010, and Databento ZQ starts 2010-06-06, so neither alone proves the requested 2010-01-01 coverage.
- R4: Reclassify merged PR-108 as an acceptance-harness delivery only; absence of a real source manifest/coverage artifact means it must not be cited as evidence of successful 2010 historical population.
- R5: Make PR-121 through PR-124 hard prerequisites for any renewed claim that `macro_raw` or `macro_features` contains verified Fed-policy history from 2010 to current EOD.

Acceptance:
- A1 (verifies R1): BACKLOG states the exact individual-contract/final-settlement/curve-breadth prerequisite and rejects continuous futures as a substitute.
- A2 (verifies R2): each supporting reference/oracle source has an explicit non-substitute role and no text implies FRED/FOMC data can reconstruct missing futures prices.
- A3 (verifies R3): the documented provider date limits prevent either 2015 FedWatch history or 2010-06-06 Databento coverage from being represented as complete 2010-01-01 coverage.
- A4 (verifies R4): PR-108 is explicitly described as harness-only until a real historical source manifest and PASS artifact exist; repository status text contains no unsupported 2010 PASS claim.
- A5 (verifies R5): dependency text makes a verified 2010 serving claim impossible before PR-121–124 complete successfully.

## PR-121: Import Licensed Historical Individual-Contract ZQ Settlements

PR name: `fed-history-zq-import`
Status: Blocked
Updated: 2026-09-23
PR: not opened
Git branch: `pr-121/fed-history-zq-import`
Git status: `not-started (branch absent)`
Agent lane: Historical market-data import; one agent only
Depends on: PR-120; external operator-supplied historical ZQ dataset
Blocked by: no verified 2010-capable individual-contract ZQ dataset is currently present
Commit: `feat(pr-121): import historical zq settlement curves`
Design patterns: Adapter, Repository, Ports and Adapters, Value Object.

Description:
- R1: Add a provider-neutral offline import adapter for operator-supplied licensed ZQ history in CSV/Parquet or an explicitly supported vendor export; no vendor credential or licensed payload may be committed to Git.
- R2: Normalize each source row to at least `observation_date`, canonical contract month/instrument identity, final settlement price, settlement status/type, source/provider identity, source extraction timestamp, imported-at timestamp, and source-file/content hash.
- R3: Accept only individual standard monthly ZQ contracts and final EOD settlements; reject continuous symbols, synthetic back-adjusted series, OHLC close/last-trade substitutes, preliminary/non-final values when final status is required, ambiguous contract-month mapping, duplicates, and non-finite prices.
- R4: Support multi-file/multi-source history without erasing provenance: each normalized row retains its exact source identity, and equal-key conflicts across sources fail closed unless an explicit deterministic precedence rule backed by PR-122 overlap evidence is configured.
- R5: Store imported curves in the existing canonical ZQ settlement store used by PR-101 so the probability/feature code does not gain a second historical algorithm path.
- R6: Add hermetic import fixtures for CME-style, Bloomberg-style, Refinitiv-style, malformed, continuous-series, duplicate/conflict, and idempotent-reimport cases; vendor-specific fixtures must be synthetic/sanitized and license-safe.

Acceptance:
- A1 (verifies R1): a synthetic licensed-export-shaped fixture imports offline with zero network/credential dependency and repository search finds no committed vendor secrets/raw licensed dataset.
- A2 (verifies R2): round-trip rows preserve exact date/contract/final-settlement/provenance/hash metadata in deterministic schema/order.
- A3 (verifies R3): every forbidden input class fails closed and valid individual final settlements preserve all simultaneous contract months for a date.
- A4 (verifies R4): multi-source equal-key equality is auditable, conflicting values fail without a declared validated precedence policy, and no source silently overwrites another.
- A5 (verifies R5): imported data is consumable by the existing canonical ZQ store/PR-101 engine without an alternate probability implementation.
- A6 (verifies R6): all import/validation/idempotence fixtures pass required offline CI and contain no license-restricted raw data.

## PR-122: Prove Historical ZQ Curve Coverage Provenance And Source Seam

PR name: `fed-history-zq-coverage-qa`
Status: Blocked
Updated: 2026-09-23
PR: not opened
Git branch: `pr-122/fed-history-zq-coverage-qa`
Git status: `not-started (branch absent)`
Agent lane: Historical source QA; one agent only
Depends on: PR-121
Blocked by: PR-121 external dataset prerequisite
Commit: `test(pr-122): prove historical zq curve coverage`
Design patterns: End-to-End Test, Reconciliation, Differential Testing, Fail-Closed Verification.

Description:
- R1: Inventory the real imported ZQ history from requested boundary 2010-01-01 through latest completed EOD and emit source manifests containing provider/file hashes, min/max observation dates, row counts, unique contract counts, and source transitions without exposing licensed prices.
- R2: For every expected trading observation used for Fed reconstruction, verify the simultaneous curve contains all contract months required by PR-101 to resolve probability distributions through the third known future FOMC meeting; missing required contract months/dates are hard gaps.
- R3: Independently validate final-settlement identity, contract-month mapping, duplicate/conflict absence, finite/range sanity, monotonic date ordering, and exact preservation of vendor precision/tick semantics.
- R4: Where two sources overlap (for example licensed history vs current public CME or Databento), compare same-date/same-contract final settlements under one explicit tick-level tolerance; any systematic seam or unresolved disagreement blocks downstream reconstruction.
- R5: Run PR-101 as a sufficiency probe across the imported history and report every date on which source/reference inputs cannot produce valid probability distributions through meeting three; classify each failure by raw-curve gap, reference-calendar availability, or methodology condition.
- R6: Emit deterministic sanitized `artifacts/acceptance/fed-history-zq-coverage-v1.json` with source hashes/identities, coverage metrics, required-curve gaps, overlap error statistics, PR-101 sufficiency results, and PASS|FAIL.

Acceptance:
- A1 (verifies R1): the artifact identifies the actual source dataset(s) and proves their real temporal extent without embedding licensed price payloads.
- A2 (verifies R2): PASS requires zero unexplained missing required curves/contract months over the claimed 2010-current reconstruction domain; a date lacking enough curve breadth cannot be counted as covered.
- A3 (verifies R3): all raw identity/precision/duplicate/order checks pass and deliberate contract-month or settlement-type corruption fails QA.
- A4 (verifies R4): every source handoff has an overlap/parity result within the declared tolerance or the overall artifact is FAIL.
- A5 (verifies R5): each claimed reconstructable date actually runs through PR-101 successfully to at least meeting three; all failures are enumerated and prevent unsupported continuity claims.
- A6 (verifies R6): the sanitized artifact is deterministic, license-safe, and PASS is impossible unless A1-A5 all pass.

## PR-123: Rebuild Fed Policy History And Repopulate PostgreSQL

PR name: `fed-history-rebuild-serving`
Status: Blocked
Updated: 2026-09-23
PR: not opened
Git branch: `pr-123/fed-history-rebuild-serving`
Git status: `not-started (branch absent)`
Agent lane: Historical reconstruction/backfill; one agent only
Depends on: PR-122 PASS
Blocked by: verified historical ZQ coverage artifact not yet present
Commit: `feat(pr-123): rebuild fed policy history from verified zq curves`
Design patterns: Command, Unit of Work, Repository, Reconciliation.

Description:
- R1: Rebuild the canonical probability snapshots and four Fed-policy origin features from the PR-122-approved historical ZQ curves plus point-in-time EFFR/target/FOMC reference inputs using exactly the existing versioned PR-101/PR-102 algorithms.
- R2: Process the requested 2010-01-01 boundary through latest completed EOD, but publish values only for dates proven reconstructable by PR-122; no probability/value may be synthesized for a missing curve.
- R3: Persist reconstruction lineage including source-manifest hash, probability-methodology version, reference-input versions, min/max dates, row counts, and deterministic data hash so the serving history can be traced back to the approved raw dataset.
- R4: Reconcile the rebuilt four origins into `macro_raw` and refresh/rebuild all 28 approved Fed-derived columns in `macro_features` through the existing serving path; do not write the materialized derived values independently.
- R5: Verify transaction/rebuild atomicity and idempotence: failure leaves prior serving state selectable, successful immediate replay produces identical hashes/rows and zero semantic changes.
- R6: Produce a sanitized rebuild artifact linking the PR-122 source-coverage artifact to canonical feature hashes and PostgreSQL sync/view fingerprints.

Acceptance:
- A1 (verifies R1): every canonical Fed row is reproducible from the approved raw/source/reference manifests using the existing methodology versions; no alternate history-only formula exists.
- A2 (verifies R2): published coverage exactly equals the dates approved reconstructable by PR-122 and no gap is filled or silently dropped from reporting.
- A3 (verifies R3): lineage/digest metadata uniquely identifies source and methodology inputs for the rebuilt history.
- A4 (verifies R4): `macro_raw` contains exact four-origin parity and `macro_features` contains those four plus the 28 derived columns computed from `macro_raw`, not from a side channel.
- A5 (verifies R5): injected failure preserves prior committed serving state; immediate successful replay is idempotent with identical data/view fingerprints.
- A6 (verifies R6): rebuild evidence is deterministic/sanitized and references the exact PASS PR-122 source artifact hash.

## PR-124: Execute True 2010-Present Fed History Acceptance

PR name: `fed-history-true-2010-acceptance`
Status: Blocked
Updated: 2026-09-23
PR: not opened
Git branch: `pr-124/fed-history-true-2010-acceptance`
Git status: `not-started (branch absent)`
Agent lane: Production-like historical acceptance; one agent only
Depends on: PR-123, PASS source/rebuild artifacts
Blocked by: no verified 2010-capable historical ZQ dataset/rebuild currently exists
Commit: `test(pr-124): accept verified fed history from 2010`
Design patterns: End-to-End Acceptance, Differential Testing, Reconciliation, Fail-Closed Verification.

Description:
- R1: Execute the complete real-data path in an authorized environment: import/verify source manifest -> point-in-time references -> PR-101 probability reconstruction -> four canonical origins -> `macro_raw` -> `macro_features` -> independent PostgreSQL verification.
- R2: Prove the exact real reconstruction domain from the requested 2010-01-01 boundary to latest completed EOD, reporting first reconstructable date, every non-reconstructable expected date, reason, and source manifest; no date may be counted solely because a PostgreSQL timestamp row exists.
- R3: Validate reconstructed probabilities/features against official CME FedWatch exports on all available overlapping QA dates and report per-meeting/per-feature errors under the source-controlled PR-106 tolerances; 2015+ oracle availability must not be extrapolated backward as evidence.
- R4: Verify real `macro_raw` four-origin coverage/parity and all 32 Fed columns in `macro_features`, including independent recalculation of 28 derived columns, warm-up NULL attribution, finite values, chronology, and no look-ahead.
- R5: Verify the historical-to-current source seam and normal daily EOD continuation: current CME delta data must append/revise through the same canonical ZQ store and feature pipeline without a methodology/source discontinuity outside the PR-122 tolerance.
- R6: Commit only deterministic sanitized `artifacts/acceptance/fed-history-true-2010-v1.json` containing source/rebuild artifact hashes, actual domain/gaps, CME overlap statistics, PostgreSQL coverage, seam results, and PASS|FAIL; PASS requires a real PR-122 source-coverage PASS.

Acceptance:
- A1 (verifies R1): evidence proves the actual licensed/public source data, existing reconstruction algorithms, serving path, and independent verifier all executed in order; no fixture/synthetic data can satisfy this acceptance.
- A2 (verifies R2): the claimed historical domain is derived from reconstructable raw curves rather than table timestamps, and every missing expected date is explicit.
- A3 (verifies R3): available official CME overlap distributions/features meet the declared tolerances with no use of future/oracle information outside QA.
- A4 (verifies R4): real PostgreSQL contains exact canonical origins and independently correct derived features with all NULL/gap/warm-up causes accounted for.
- A5 (verifies R5): overlap/seam evidence proves historical backfill and ongoing EOD updates form one consistent series or acceptance fails.
- A6 (verifies R6): the artifact is deterministic/sanitized and cannot report PASS without the exact PR-122 and PR-123 PASS artifact hashes.

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

- PR-106–118: CME differential-test machinery, PostgreSQL QA/acceptance harnesses, legacy cleanup, Fed origin/materialized transformations, `macro_raw` integration, and associated QA were delivered. PR-108 is retained as a historical acceptance harness only; because no real 2010-capable ZQ source manifest/coverage artifact was committed or verified, it is not evidence of successful 2010-present data population.

The active corrective backlog above begins with PR-120. Completed implementation/harness work is historical context only; verified 2010-present data coverage may be claimed only after PR-122 and PR-124 produce PASS artifacts against a real historical ZQ source.
