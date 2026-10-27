# Backlog

This backlog records the active GOALS.md implementation sequence, durable repository
contracts, and compact historical context for `macro-loader`.

The repository loads reusable daily market-state inputs from open/public sources, preserves source history, performs strict incremental updates during normal execution, and publishes deterministic immutable Gold feature snapshots through a Bronze -> Silver -> Gold architecture.

Last reviewed: 2026-10-27

## GOALS.md Implementation Wave

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
Status: Ready
Updated: 2026-10-27
PR: [#179](https://github.com/SergejSchweizer/macro-loader/pull/179) (open)
Git branch: `pr-179/add-euro-high-yield-series`
Git status: pushed-local-checks-passed
Agent lane: FRED credit-series registration and source-history QA
Depends on: PR-178
Commit: `0b0e0ff feat(pr-179): add euro high-yield series`
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
Status: Planned
Updated: 2026-10-27
PR: not created
Git branch: `pr-180/add-us-treasury-3m-5y-series`
Git status: not-started (branch absent)
Agent lane: FRED Treasury registration and provider QA
Depends on: PR-179
Commit: pending
Design patterns: Registry/Factory, Pure Parser.

Description:
- R1: Add daily scalar FRED series `us_3m` (`DGS3MO`) and `us_5y` (`DGS5`) in percent
  units, keeping the existing `us_2y` only once and retaining `us_10y` unchanged.
- R2: Bootstrap maximum exposed history: `DGS3MO` from 1981-09-01 and `DGS5` from
  1962-01-02 as checked on 2026-10-27. Existing FRED bounded-update and durable-history
  retention rules continue to apply.
- R3: Update source metadata and offline registry/request-bound tests.

Acceptance:
- A1 verifies R1: exact IDs, daily frequency, percent units, and no duplicate `us_2y`
  registration.
- A2 verifies R2: documented and tested earliest available dates; no source omission
  deletes durable history.
- A3 verifies R3: local quality-gate passes with no network tests in required checks.

### PR-181: Cut Over To Per-Feature PostgreSQL Raw Tables

PR name: `publish-feature-specific-raw-tables`
Status: Planned
Updated: 2026-10-27
PR: not created
Git branch: `pr-181/publish-feature-specific-raw-tables`
Git status: not-started (branch absent)
Agent lane: PostgreSQL raw-table projection, migration, backfill, and schema QA
Depends on: PR-180
Commit: pending
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

PR-176 through PR-178 are merged; PR-179 is in progress and PR-180 through PR-181 are planned. No production migration or
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
