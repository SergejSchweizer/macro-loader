# Architecture

This document is the durable engineering contract for `macro-loader`.

`BACKLOG.md` defines delivery order and acceptance criteria. `README.md` is the operator/consumer contract. `AGENTS.md` defines coding-agent behavior. None may intentionally contradict this document.

## Current operational status

The repository includes causal feature transformations and native OHLC values in the
PostgreSQL serving-view contract. Retired Fed-policy runtime and serving code does not
establish production database or installed host-crontab state.
Production PostgreSQL migration 33 and canonical Gold synchronization completed on
2026-10-27. All 25 `macro_raw_<feature>` tables and both materialized views are populated;
the old wide `macro_raw` and orphaned Fed serving table are absent. Both public views reach
`timestamp_m1=2026-10-27`, with availability through 2026-10-28. The sync checkpoint is
schema 11 / feature 6 with 16,805 consumer and digest rows. The daily cron contract is
09:00 `Europe/Vienna` with a 60-calendar-day bounded source overlap. The installed daily
wrapper and its independent PostgreSQL replay/conformance acceptance passed on
2026-10-27; all 25 raw tables had 37–41 observations in the latest 60-calendar-day
window, and the explicit replay made zero row changes.
This document contains no Fed-specific operator commands or scheduling instructions.
Historical Fed acceptance evidence is retained, and the independent FRED provider remains
active for its ordinary registered macro series.

The target PostgreSQL transformation layout adds migration 34's
`macro_loader.macro_feature_values` (schema migration 34; population migration 35): a
single sparse long-form table for the primary and
extended transformation catalogs. Its key is `(timestamp_m1, feature_name)`; generated
`available_at_utc` uses the same UTC next-trading-day convention as the serving views,
and `value DOUBLE PRECISION` stores numeric outputs. Boolean Donchian outputs are mapped
to `1.0` (true) and `0.0` (false). NULL/warm-up values are omitted, and raw OHLC
passthroughs remain in their source/view contracts rather than being copied as
transformations. The table's catalog-generated refresh query calculates primary and
extended transformations directly over `macro_raw_<feature>` tables; neither public
feature materialized view is a source dependency. After Gold sync changes raw tables, the
explicit refresh transaction rebuilds it from a transaction-scoped staging result, which is
evaluated once for both validation and population. This is a full rebuild, not a delta
upsert; manual raw-table writes require an explicit refresh and do not fire a trigger. The
primary key prevents duplicate observation/feature pairs. Runtime consumer and sync roles
are read-only; the owner-controlled refresh is the sole writer. Production status is not
implied by this design statement and must be established by migration and independent
conformance QA.

The executable Gold contract is currently schema version **11** and feature version
**6** (`application.gold_frame`). PostgreSQL schema migration 31 creates one
`macro_loader.macro_raw_<feature>` table for each of the 25 registered source series,
backfills them from the previous wide `macro_loader.macro_raw`, then drops that wide
relation. It is a one-time migration source only: runtime sync and the materialized view
do not read, write, or recreate it. The `macro_loader.macro_features` materialized view
is rebuilt from a full-outer-join projection of the feature tables and then computes the
closed-world causal feature catalog. It preserves all available timestamps, including
pre-2010 rows, and exposes NULL where source data or causal warm-up is unavailable.
Migration 32 leaves `macro_loader.macro_features` and its public query contract
untouched. It adds a private materialized raw-key projection and private materialized
transformations for the 12 additional positive-level series, then adds
`macro_loader.macro_features_extended`, a separately versioned materialized view for
their 792 catalogued transformation outputs. This keeps the existing 954-column,
version-16 public contract stable and avoids PostgreSQL's 1,600-column per-relation limit;
the extended view has 794 columns including `timestamp_m1` and `available_at_utc`. The
private relations are not granted to consumer roles. Both public views expose the same
temporal/availability keys, are covered by catalog fingerprints and schema conformance,
and refresh in dependency order through the explicit PostgreSQL refresh routine.
Loader-controlled PostgreSQL sessions and that SECURITY DEFINER refresh routine disable
JIT because recursive CTE row estimates can otherwise cause compilation to dominate view
execution. During schema upgrades, migration versions 24–30 defer repeated view rebuilds;
the prior committed view remains available until the atomic feature-table cutover publishes
the replacement. Admin-only migration statements have a bounded 15-minute timeout for
full-history cutovers. Ordinary runtime sync retains a 5-minute statement timeout, with a
bounded 15-minute local override only for its dependency-ordered full-history feature-view
refresh. Connection, lock, and idle-transaction timeout bounds remain active.
Migration 33 also repairs historical ledger/object drift for retired Fed storage: it
replays the idempotent archive-and-drop prefix after feature-table publication, preserving
any orphaned `fed_policy_features_daily` rows in `macro_loader_retired` before removing
the stale active table and enforcing the owned-table contract.
PostgreSQL Gold synchronization may coalesce consecutive Gold schema versions 7 through
11 only while feature version 6 remains unchanged. It treats the jump as a schema upgrade
and reconciles canonical rows and digests in one transaction; feature-version changes,
backwards transitions, and versions outside that range remain fail-closed.

Gold keeps `timestamp_m1`, all registered `<series>_level` fields in registry order,
then registry-derived native OHLC fields in series-major order with `open`, `high`,
`low`, `close` per series. The OHLC series are `vix`, `vix9d`, `vix3m`, `vix6m`,
`vix1y`, and `move`, yielding 24 nullable Float64 fields (`vix_open` through
`move_close`). Each OHLC feature table stores its four nullable `DOUBLE PRECISION`
values; each scalar feature table stores nullable `indexvalue DOUBLE PRECISION`.
Every table orders columns as `timestamp_m1 TIMESTAMPTZ(6)` primary key, generated
non-null `available_at_utc TIMESTAMPTZ(6)`, then native values. `macro_features` exposes
the rejoined raw values through its closed-world feature catalog.
For each of the six OHLC series, the serving view also
exposes six nullable decimal-fraction transformations: `(high-low)/close` and
`(close-open)/open` for the current valid bar plus trailing-five-valid-metric means, and
`(open[t]-close[previous valid source observation])/close[previous valid source
observation]` plus its trailing-five-valid-gap mean. Five-observation outputs require a
complete window; missing/non-finite operands and nonpositive denominators yield NULL.
Overnight gaps pair each finite current open with the most recent preceding finite close in
that series' observation history (the current bar's close is not an operand), so sparse
calendar dates do not break the sequence. These derived values live only in
`macro_features`, not in raw or canonical Gold.
The same six series expose six nullable numeric stochastic outputs (`stoch_k_5`,
`stoch_d_5_3`, `stoch_k_14`, `stoch_d_14_3`, `stoch_kd_spread_5`, `stoch_k_slope_3`)
and two nullable BOOLEAN Donchian outputs (`donchian_breakout_up`,
`donchian_breakout_down`) in `macro_features` only. `%K` is
`100*(close-min(low,n))/(max(high,n)-min(low,n))` over the current and preceding `n-1`
complete finite OHLC source bars (`n` is 5 or 14); incomplete or zero-range windows yield
NULL. `%D` requires three consecutive valid `%K` values. Spread is `%K5-%D5`; slope is
`(%K5[t]-%K5[t-3])/3`. Donchian compares the current high/low strictly with the respective
maximum/minimum over the prior 20 valid source bars, excluding the current bar; both flags
remain NULL until 20 prior valid bars exist. These observation windows do not fill sparse
calendar dates and expose no intermediate columns.

## System Purpose

`macro-loader` is a reusable daily market-state data product. It acquires open/public market and macro series, preserves historical observations, performs bounded incremental source updates during normal operation, normalizes data into a canonical daily representation, derives causal reusable features, and publishes immutable Gold snapshots.

It does not own regime classification, HMM states, targets, portfolio optimization, position sizing, or trading execution.

## Architectural Style

The repository uses **hexagonal architecture (Ports and Adapters)**.

```text
                         api / scripts
                              |
                              v
                         application
                  use cases + policies + ports
                    /                    \
                   v                      v
        persistence abstractions      provider abstractions
                   ^                      ^
                   |                      |
          Parquet/JSON/PNG          HTTP/provider adapters
          filesystem adapters
```

Dependency rule:

```text
api/scripts -> application/contracts <- ingestion adapters
```

`application/` must not import concrete HTTP clients, provider modules, matplotlib, filesystem-specific persistence implementations, or CLI parsing.

## Design Pattern Policy

Patterns are used to make boundaries explicit, not to maximize abstraction count.

### Adapter

Concrete CBOE/STOXX/Yahoo/ECB/FRED implementations adapt provider protocols into canonical application contracts. Filesystem/Parquet/JSON/PNG implementations adapt persistence ports.

### Strategy

Use explicit strategy/policy objects for behavior that may vary independently:

- HTTP retry policy;
- strict normal update versus explicit full reconciliation planning;
- consumer resolution (`strict_current` versus `latest_compatible`).

Do not scatter equivalent behavior across provider-specific conditionals.

### Registry / Factory

Canonical series metadata and provider adapter lookup are registry-driven. Application orchestration receives a provider registry/factory and must not contain provider-name `if/elif` ladders.

### Repository

Persistence is exposed through narrow repository-style ports, for example:

```text
BronzeRepository
SilverRepository
IngestionStateRepository
RunManifestRepository
InventoryRepository
GoldBuildStore
GoldCatalogRepository
GoldMaterializedViewWriter
```

Physical partition/file logic stays in adapters.

### Unit of Work

Two boundaries need explicit commit semantics:

1. **One-series Bronze execution** — source fetch/diff/write, success-run persistence, state advancement.
2. **Gold publication** — candidate bundle validation and atomic catalog promotion.

A use case must make the durability boundary obvious and test failure before/after it.

### State Machine

Gold catalog publication state is:

```text
building -> complete
        \-> failed
```

Only the root Gold catalog owns this lifecycle. Immutable build `manifest.json` describes artifact identity and must not independently claim publication state.

### Materialized View

Root Gold `manifest.json` and `feature_profile.png` are derived views of authoritative `manifest.parquet` plus its current build. They are rebuildable after interruption and never participate in current-build selection.

### Mark-and-Sweep

Retention first makes a non-current build unselectable in the catalog, then deletes its physical bundle. This avoids a crash window in which a catalog-selectable build is partially deleted.

### Command

CLI subcommands are adapters: parse arguments/configuration, call one application use case, render output, set exit code. They do not contain provider, feature, or persistence business rules.

### Dependency Injection

Inject clocks, sleepers, HTTP clients, repositories, provider registries, source-control identity, and policies. Tests must not depend on real wall clock, sleeping, network, or repository discovery where an injected boundary is appropriate.

Prefer `typing.Protocol`, immutable dataclasses/Pydantic models, and composition. Avoid inheritance hierarchies unless a true substitutable abstraction exists.

## Layer Ownership

| Layer | Owns | Must not own |
|---|---|---|
| `api/` | CLI parsing, validation, output/exit codes | provider HTTP/parsing, lake persistence, feature formulas |
| `application/` | use cases, contracts, policies, ports, state machine, validation | `httpx`, filesystem details, matplotlib, CLI parsing |
| `ingestion/` | provider adapters, HTTP adapter, Polars/Parquet IO, JSON/PNG materialization | CLI policy, regime decisions, portfolio logic |
| `scripts/` | operational wrappers, GitHub/quality tooling | hidden domain behavior |
| `tests/` | unit/contract/regression/offline integration | production behavior |

## PostgreSQL Administration Boundary

`gold-sync-postgres` is a runtime DML command executed with the dedicated `macro-loader-sync`
writer role. Before acquiring its locked row-mutation transaction, it performs a read-only
schema-contract preflight against the administered feature-specific raw tables, sync
tables, columns, and keys. Missing or incompatible schema fails closed; runtime
sync never creates, alters, drops, grants, or migrates PostgreSQL objects.

`postgres-migrate` is the separate explicit admin command that applies idempotent schema
migrations. It is composed with protected `MARKET_MACRO_POSTGRES_ADMIN_*` credentials,
which are distinct from runtime `PG*` credentials. Normal cron export provides runtime
configuration only and never exports administrator credentials.

`postgres-verify` is a separate read-mostly command using the runtime endpoint. Its application
service compares the certified current Gold bundle with independently read consumer rows, digest
index, state, and summary. A dedicated ingestion adapter separately validates exact owned schema,
role ownership and grants, UTC session settings, timeout/application identity, and rollback-only
microsecond timestamp probes. It returns a deterministic, sanitized `PASS|FAIL` report and cannot
publish, synchronize, migrate, or create durable probe state.

`postgres-reconstruct --execute` is a separate, explicitly authorized production command. Its
application use case is fail-closed: maintenance marker, runner lock, PostgreSQL maintenance
lock, exact endpoint probe, and validated private backup evidence precede all source or schema
mutation. It invokes explicit reconciliation for all registered series, canonical Silver/Gold
publication, controlled recreation of only the two loader-owned schemas, runtime publication,
independent verification, and a zero-mutation replay. Only after every assertion passes may it
remove the maintenance marker. Private backup and restore material never crosses the adapter
boundary into a committed artifact; the sole source-controlled evidence is its deterministic,
sanitized PASS report.
## Canonical Series Registry

Exactly these canonical series are in MVP:

```text
vix
vix9d
vix3m
vix6m
vix1y
vstoxx
vstoxx1m
vstoxx2m
vstoxx3m
move
ciss
ciss_money
ciss_bonds
ciss_equity
ciss_financials
estr
euro_hy_oas
euro_hy_yield
euro_hy_ytw
euro_hy_total_return
us_2y
us_10y
usd_broad
us_3m
us_5y
```

Provider ownership:

```text
CBOE   -> vix, vix9d, vix3m, vix6m, vix1y
STOXX  -> vstoxx, vstoxx1m, vstoxx2m, vstoxx3m
Yahoo  -> move
ECB    -> ciss, ciss_money, ciss_bonds, ciss_equity, ciss_financials, estr
FRED   -> euro_hy_oas, euro_hy_yield, euro_hy_ytw, euro_hy_total_return,
          us_2y, us_10y, usd_broad, us_3m, us_5y
```

The four Euro high-yield series use daily ICE BofA Index observations from FRED.
OAS/effective yield/yield-to-worst are percent units; total return is an index level.
FRED currently exposes three years of history (2023-10-27 through 2026-10-23 when
verified on 2026-10-27). Existing durable observations are retained when source history
is shorter. ICE's FRED terms restrict this licensed index data to internal use and
prohibit third-party redistribution without prior written approval.

The daily Treasury yield series `us_3m` (`DGS3MO`) and `us_5y` (`DGS5`) are published
by the Federal Reserve Board in H.15, in percent and not seasonally adjusted. Their first
FRED observations are 1981-09-01 and 1962-01-02 respectively; on 2026-10-27 the exposed
last observations were 2026-10-22 and 2026-10-23. Missing source markers remain absent,
and shorter source responses do not delete retained history.

The STOXX term-structure scalar series use official daily full-history files:
`V6I1` (`vstoxx1m`), `V6I2` (`vstoxx2m`), and `V6I3` (`vstoxx3m`). Their source
rows have `Date;Symbol;Indexvalue`; the canonical value is the published index value in
index points. At verification on 2026-10-27, all three files exposed observations from
1999-01-04 through 2026-10-26. Bootstrap retains the full exposed file; normal updates
download the source file and restrict persistence to the exact logical delta window.
Rows with an empty `Indexvalue` are omitted as source-missing observations; malformed
non-empty values, invalid dates, duplicate dates, and non-finite values fail ingestion.

The New-CISS daily component series `ciss_money`, `ciss_bonds`, `ciss_equity`, and
`ciss_financials` use the ECB source keys `CISS.D.U2.Z0Z.4F.EC.SS_MMN.CON`,
`CISS.D.U2.Z0Z.4F.EC.SS_BMN.CON`, `CISS.D.U2.Z0Z.4F.EC.SS_EMN.CON`, and
`CISS.D.U2.Z0Z.4F.EC.SS_FIN.CON`. They are dimensionless component contributions, not
standalone subindex levels. Their canonical scalar values come from ECB's `OBS_VALUE`;
the source catalog reports daily observations beginning 1999-01-01, continuing through
the latest published observation. Missing observations are omitted, not filled.

Every registry entry declares canonical ID, provider, source ID/file, unit, native shape (`ohlc|scalar`), frequency, bootstrap policy, and fetch capability (`date_range|full_file`).

Provider adapters must not invent unregistered symbols or silent fallbacks.

## Medallion Data Flow

```text
Bronze
 provider-shaped observations
 source audit metadata
 monthly Parquet
       |
       v
Silver
 canonical daily long form
 monthly Parquet
       |
       v
Gold feature families
 causal deterministic transforms
       |
       v
Canonical Gold frame
       |
       v
Immutable build bundle
 data.parquet + manifest.json + feature_profile.png
       |
       v
Gold catalog publication
 manifest.parquet <- authority
       |
       v
Root materialized views
 manifest.json + feature_profile.png
```

## Physical Lake Layout

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

Runtime `lake/` is ignored by Git.

## Bronze Contract

Common fields:

```text
series_id: String
provider: String
observation_date: Date
fetched_at_utc: Datetime(time_zone="UTC")
source_id: String
source_url: String
```

Payload is exactly one shape:

```text
ohlc   -> open, high, low, close
scalar -> value
```

Natural key:

```text
(provider, series_id, observation_date)
```

Rules:

- no synthetic observations;
- duplicate incoming natural keys are rejected;
- Bronze and Silver retain historical observation dates and the latest accepted value for each natural key; an equal-key source revision replaces that retained value rather than creating a provider-vintage history;
- the lake is not a provider-vintage/time-travel archive unless a future explicit vintage journal is introduced;
- upstream omission or a shorter response does not imply deletion;
- only affected monthly partitions are rewritten;
- logical no-op is a physical no-op.

## Silver Contract

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

Natural key:

```text
(series_id, observation_date)
```

Rules:

- OHLC source: `value == close`;
- scalar source: scalar `value`, null OHLC;
- no fill/interpolation;
- finite non-null `value` only;
- deterministic selected-series rebuild and monthly diff/write.

## Strict Source Update Contract

The planner exposes three explicit operation modes:

```text
bootstrap
update
reconcile
```

### Bootstrap

When authoritative Bronze has no observation for a selected series, request maximum public history exposed by the configured source.

### Normal Update

When Bronze exists, calculate the logical request window from the **newest durable Bronze observation**:

```text
latest_stored_date = max(Bronze.observation_date)
request_start      = latest_stored_date - overlap_days
request_end        = injected_today
```

Default overlap is seven calendar days. The overlap exists only to capture recent source revisions.

The historical minimum is explicitly **not** part of normal delta planning:

```text
min(Bronze.observation_date) != request_start
```

Canonical example:

```text
min stored       = 2000-01-03
latest stored    = 2026-08-18
today            = 2026-08-19
overlap          = 7 days
normal request   = 2026-08-11 .. 2026-08-19
```

A normal update requesting `2000-01-03 .. 2026-08-19` is a contract violation.

Additional invariants:

- `latest_stored_date` comes from authoritative Bronze; state is a cache/audit record and cannot broaden the request.
- if `request_end < latest_stored_date`, execution fails.
- `date_range` providers receive the exact bounded start/end and may not silently broaden to maximum history.
- bounded-provider rows outside the requested interval are contract errors.
- `full_file` providers may have to download their entire compact public object because the upstream source exposes no bounded endpoint; during normal update they must filter parsed observations to the exact logical window before diff/persistence.
- full-file out-of-window rows are ignored during normal delta diff and must not trigger old-month rewrites.
- source omission/shortening never deletes older retained history.

This distinction is deliberate: the project guarantees **logical/persistence delta-only normal execution** for all providers and **network-level delta retrieval where the provider supports date bounds**. It does not falsely claim network-level delta for public full-file-only sources.

### Explicit Reconcile

`reconcile` is an explicit operator command that may request maximum currently exposed source history to discover revisions older than the normal overlap window.

`run-daily` and normal `update` must **never automatically choose `reconcile`** based on elapsed time, state age, or any hidden policy. If periodic reconciliation is desired, it is scheduled as a separate explicit command.

The daily `run-daily -> gold-sync-postgres` chain is scheduled at `0 9 * * *` using the explicit `Europe/Vienna` IANA timezone. It retains 09:00 local wall-clock execution across daylight-saving changes and uses a 60-calendar-day update overlap to cover at least 30 normal weekday EOD observations. Source reconciliation remains a separate explicit operator command.

Reconciliation still obeys the rule that source omission is not deletion. Explicit deletion handling requires a separate source-mutation contract.

State tracks at least last success, last observation cache, requested bounds, operation mode, fetched/accepted/changed row counts, and optional last successful explicit reconciliation time.

## One-Series Ingestion Unit Of Work

Application orchestration follows:

```text
plan
 -> provider fetch/parse
 -> enforce logical request window
 -> logical diff
 -> durable Bronze write
 -> durable success run manifest
 -> ingestion state advance       <- commit complete
```

If failure occurs before the commit boundary, previous state remains authoritative. Independently completed series remain durable.

A no-op may update `last_success_utc`, but must not change `last_observed_date` or rewrite Bronze files.

## Gold Timestamp Contract

Gold key:

```text
timestamp_m1: Datetime(time_unit="us", time_zone="UTC")
```

It is first column, unique, strictly increasing, and UTC midnight for the represented source date.

`timestamp_m1` is **observation-day identity only**. It is not provider publication time, availability time, or proof that a value was tradable at midnight. Same-day intraday backtests need an explicit downstream availability/lag policy.

Gold contains no `observation_date`.

## Gold Feature Math

For a single series:

```text
delta_Nobs(t) = x(t) - x(previous Nth valid observation)
```

Every source level has a causal `delta_1obs` feature. Existing 5- and 20-observation
deltas remain series-specific as declared by the feature family.

A 60-observation z-score uses the last 60 valid observations including `t` and population standard deviation (`ddof=0`). It is null before 60 observations or when standard deviation is zero.

For every canonical source series, positive momentum autocorrelation uses the one-observation source-unit change:

```text
change_1obs(t) = x(t) - x(previous valid observation)
rho_h,W(t) = Corr(change_1obs(t-W+1:t), change_1obs(t-h-W+1:t-h))
momentum_autocorr_h_W(t) = max(rho_h,W(t), 0)
```

The fixed causal pairs are `(h, W) = (1, 60), (5, 60), (20, 120)`. Undefined values remain null; negative correlations are zero. The feature is a persistence signal, not a cumulative return.

Cross-series ratios/spreads require same `timestamp_m1` values.

The feature catalog uses an explicit domain policy: positive-level XETRA families
(log levels, returns, volatility, SMA ratios, RSI, ROC, and drawdown) are emitted only
for `PRICE_FEATURE_SERIES`. Signed/non-price levels such as `estr` remain raw inputs and
may receive source-unit delta/momentum features, but never silently enter a logarithmic
or price-domain formula.

The PostgreSQL `macro_features` view contract version 12 includes these observation
windows for every `PRICE_FEATURE_SERIES`: log returns `1/2/3/4/5/8/10/20`, geometric
returns `4/5/8/10/16/20`, mean log returns `5/10/20`, sample log-return volatility
`5/8/10/20/32/40/96`, SMA ratios `5/15`, `5/20`, `10/20`, and `10/40`, Wilder RSI
`3/5/7/14`, ROC `1/2/3/4/5/8/10/20`, and rolling drawdown `20/32/60/96`. Rolling
calculations use causal complete valid-observation windows; RSI preserves its established
Wilder recurrence and warm-up, with fully flat gain/loss windows as NULL and all-gain/all-loss
windows as 100/0. Geometric-return values are decimal
fractions despite their `_pct` suffix. This extension changes only the PostgreSQL
serving-view catalog and SQL, not canonical Gold or `macro_raw`; it adds no OHLC, volume,
market-breadth, or dispersion features.

#### PR-171 Composite and Statistical Features

The version-12 PostgreSQL `macro_features` view contract adds the following nullable
`DOUBLE PRECISION`, series-prefixed features to each `PRICE_FEATURE_SERIES`: positive-level
price series only; signed/non-price `estr` is excluded. Lag counts refer to non-NULL
source-level observation rows, not elapsed calendar days. This describes the repository's
view contract and does not assert that any production database has been migrated or
populated. Derived values belong only to `macro_features`, not raw feature tables or canonical
Gold.

For RSI-derived columns, Wilder RSI for period `n` uses gains
`max(level(t)-level(t-1), 0)` and losses `max(level(t-1)-level(t), 0)` on positive adjacent
source levels. Its initial average gain/loss is the arithmetic mean of the first `n` valid
one-observation changes; each subsequent average is
`((n-1)*previous_average + current_gain_or_loss)/n`. RSI is
`100 - 100/(1 + average_gain/average_loss)`. Thus RSI-5 requires five valid changes (six
positive levels), and RSI-15 requires fifteen valid changes (sixteen positive levels).
RSI is NULL before its seed is available and when both averages are zero. If loss is zero
and gain is positive, RSI is 100.

| Column suffix | Contract formula | Additional NULL/warm-up behavior |
| --- | --- | --- |
| `_rsi_ratio_5_15` | `RSI_5(t) / RSI_15(t)` | Both RSI values must exist and RSI-15 must be nonzero. RSI-15 is an internal intermediate, not a view column. |
| `_rsi5_slope_3` | `(RSI_5(t) - RSI_5(t-3 source rows)) / 3` | Requires defined RSI-5 at both endpoints. The lag counts source rows; intermediate NULL RSI values are not skipped. |
| `_rsi5_delta_1` | `RSI_5(t) - RSI_5(t-1 source row)` | Requires defined RSI-5 at both adjacent source rows; NULL endpoints propagate. |
| `_roc_acceleration_4_8` | `(level(t)/level(t-4)-1) - (level(t)/level(t-8)-1)` | Current, 4-lag, and 8-lag levels must be positive; otherwise NULL. |
| `_volatility_zscore_96` | `(volatility_8(t)-mean_96(volatility_8))/sample_stddev_96(volatility_8)` | `volatility_8` is sample standard deviation (`ddof=1`) of eight valid one-observation log returns in the trailing eight source rows. The z-score uses the latest 96 non-NULL `volatility_8` values including current; NULL values are skipped in that 96-value history. NULL until 96 exist or when sample standard deviation is zero/undefined. |
| `_level_zscore_96` | `(level(t)-mean_96(level))/sample_stddev_96(level)` | Uses the latest 96 positive, non-NULL levels including current; non-positive/missing levels are skipped in that 96-value history. NULL until 96 exist or when sample standard deviation is zero/undefined. |

The return-autocorrelation suffixes are `_momentum_autocorr_lag{h}_window{N}` and use
one-observation log return `r(t)=ln(level(t)/level(t-1 source row))`. Their exact
`(lag h, valid-pair window N)` pairs are `(1,96)`, `(4,96)`, and `(8,192)`, respectively.
Each output is the signed Pearson correlation of pairs `(r(t), r(t-h))` over the trailing
`N` valid pairs. Apply lag `h` to the return stream before filtering incomplete pairs; a
pair is valid only when both returns exist, and `N` counts valid pairs rather than source
rows or days. Return NULL until `N` pairs exist or if either side has zero/undefined sample
standard deviation. Do not clip negative correlation: this signed return-based measure is
distinct from positive-clipped autocorrelation of source-unit changes. All windows are
trailing and causal.

Market breadth and cross-instrument dispersion are excluded by the user's scope decision.
No cross-instrument panel is present; do not substitute statistics across heterogeneous
macro indicators.

#### PR-172 EMA-Level Trend Features

The version-12 PostgreSQL `macro_features` view adds six nullable
`DOUBLE PRECISION`, series-prefixed EMA outputs for each of the twelve
`PRICE_FEATURE_SERIES`; signed/non-price `estr` is excluded. EMA is private SQL
intermediate state and is not itself exposed. For period `n`, alpha is `2/(n+1)`;
the first EMA is the arithmetic mean of `n` consecutive valid positive levels, and
subsequent values use `EMA[t] = alpha*level[t] + (1-alpha)*EMA[t-1]`. A missing or
nonpositive source level resets the recurrence and its seed. Output remains NULL until
the seed is complete again. The EMA implementation follows the reference recurrence in
XETRA's `exponential_moving_average` helper.

| Column suffix | Formula | Warm-up and NULL rules |
| --- | --- | --- |
| `_ema5_distance` | `level/EMA5-1` | Requires positive current level and defined EMA5. |
| `_ema9_distance` | `level/EMA9-1` | Requires positive current level and defined EMA9. |
| `_ema21_distance` | `level/EMA21-1` | Requires positive current level and defined EMA21. |
| `_ema5_ema21_spread` | `(EMA5-EMA21)/level` | Requires positive current level and both EMA values. |
| `_ema9_slope_3` | `(EMA9[t]-EMA9[t-3 source rows])/(3*level)` | Requires positive current level and defined EMA9 at both endpoints. |
| `_ema5_ema9_cross` | `1` upward, `-1` downward, otherwise `0` | NULL until EMA5 and EMA9 are defined for both current and immediately prior source rows. |

All outputs are causal and use no calendar fill. Merged PR-173 adds relative MACD to the
version-13 view. The outputs are `(EMA12-EMA26)/level`,
`(MACD_line-EMA9_signal)/level`, and the
three-source-row relative histogram delta. Price EMAs use positive-level arithmetic
seeds; the EMA-9 signal seed accepts negative and zero MACD-line values and resets on NULL.
This follows XETRA's [MACD EMA CTEs](https://github.com/SergejSchweizer/xetra-loader/blob/main/sql/schema/004_xetra_features.sql#L2270-L2333)
and [relative output formulas](https://github.com/SergejSchweizer/xetra-loader/blob/main/sql/schema/004_xetra_features.sql#L2776-L2785).
Volume transformations, market breadth, and cross-instrument dispersion are excluded by
the current user scope. No applicable `volume_level` source or instrument panel exists in
the raw contract.

PR-168's merged runtime and serving contract removes Fed-policy acquisition,
calculations, stores, commands, and serving columns. This merge does not assert production
schema or host-crontab state. Historical source and acceptance artifacts are retained as
audit records. No Fed-specific operator commands or schedule are documented here. The
separate FRED provider and its registered macro series remain active.

### PostgreSQL Serving Availability Contract (introduced in PR-167)

PR-167 defines `available_at_utc` as PostgreSQL-serving metadata, not as a new
canonical Gold column. The current contract is that every `macro_loader.macro_raw_<feature>`
table contains a generated, non-null `available_at_utc` and that the
`macro_loader.macro_features` materialized view exposes that same value for each
`timestamp_m1` row. It is `00:00:00 UTC` on the first Monday-Friday date strictly
after the UTC observation date represented by `timestamp_m1`: Monday through
Thursday map to the following date, while Friday, Saturday, and Sunday map to
Monday. This generic weekday-only rule does not exclude public holidays because
the serving data spans sources and market calendars without one common holiday
calendar.

This generated serving timestamp is distinct from `timestamp_m1`, which remains
canonical Gold observation-day identity. It is the only availability timestamp in the
active PostgreSQL serving contract and does not alter the canonical Gold payload. The
migration changes schema only when the separately authorized PostgreSQL migration
command is run against its target database.

Forbidden:

- forward fill;
- backward fill;
- interpolation;
- centered windows;
- future data;
- implicit as-of carry.

Before final Gold validation, feature NaN is normalized to null. Infinity is rejected. Final feature columns are nullable `Float64` with no NaN/infinity.

## Gold Semantic Versions

Initial constants:

```text
schema_version  = 6
feature_version = 5
```

`schema_version` changes for column name/order/type changes. `feature_version` changes when formulas/parameters change without a schema change. Runtime never auto-increments either.

## Gold Build Store

Build ID format:

```text
YYYYMMDDTHHMMSSZ
```

A build directory is creation-only. Existing build path reuse fails before overwrite.

Build store writes `data.parquet` via same-directory temporary file, validates readback, then atomically replaces the final file. It returns row count, timestamp bounds, and SHA-256 of final bytes.

Explicit build reads require a concrete build ID. No filesystem `latest` discovery exists.

## Immutable Build Manifest

Build `manifest.json` describes artifact identity, not publication status.

Required concepts include:

```text
dataset_id
build_id
artifact_state = built
schema_version
feature_version
build_started_at_utc
build_completed_at_utc
rows_out
columns
min_timestamp
max_timestamp
data_path
data_sha256
feature_set_hash
git_commit_hash
plot_path
```

`feature_set_hash` is deterministic from schema/feature versions plus ordered names/dtypes/formula parameters. Build JSON/PNG are creation-only siblings of Parquet.

## Gold Catalog Repository

Authoritative file:

```text
lake/gold/dataset=macro_features_daily/manifest.parquet
```

Exact catalog fields:

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

Catalog invariants:

- unique `build_id`;
- only `complete` may be current;
- zero current before first successful publication is valid;
- after publication at most one current row;
- `building`/`failed` are never selectable;
- pruned rows have `pruned_at_utc != null` and null artifact paths;
- selectable rows are complete, non-pruned, and have all artifact paths populated.

The catalog repository validates logical/path-shape invariants but does **not** inspect physical filesystem existence. Physical bundle integrity belongs to `GoldBuildStore`/publication validation.

## Consumer Resolution Strategy

Resolution is a pure catalog operation with explicit policy.

### `strict_current` — default

Require the current row to be complete, non-pruned, selectable, and compatible with the caller's supported schema/feature versions. Otherwise fail.

### `latest_compatible`

Prefer a compatible current row; if current is incompatible, choose newest compatible complete/non-pruned/selectable row ordered by:

```text
completed_at_utc DESC, build_id DESC
```

No resolution strategy inspects filesystem mtime/glob order.

After logical resolution, opening the selected build performs physical integrity validation.

## Gold Publication State Machine And Unit Of Work

Publication stages:

```text
register attempt -> building,current=false
        |
        v
create + validate immutable bundle
        |
      success ---------------- failure
        |                         |
        v                         v
atomic catalog promotion      catalog failed
new complete/current          current unchanged
old current demoted
        |
        v
refresh root materialized views
```

The **atomic catalog replacement is the publication commit point**.

Before promotion, validate Parquet hash/row/timestamps/schema/version, build JSON identity/hash/path values, valid plot, and expected build-directory containment.

Filesystem presence never promotes an interrupted build. Stale non-current `building` rows are recovered to `failed` unless a future explicit resume protocol is introduced.

## Root Materialized Views

Root:

```text
manifest.json
feature_profile.png
```

are derived from authoritative catalog/current build.

They are refreshed after each successful catalog mutation and reconciled on startup/publication entry. If refresh fails after a catalog commit, return an operational error but do not pretend the catalog commit rolled back. A later reconciliation regenerates the views.

This Gold-view reconciliation is unrelated to market-source full-history `reconcile`.

## Retention: Mark And Sweep

Default:

```text
gold_retention_successful_builds = 5
```

Evaluated per `(schema_version, feature_version)` pair including current.

Safe prune flow:

```text
select eligible non-current build
 -> validate catalog identity
 -> atomic catalog tombstone:
      data_path = null
      build_manifest_path = null
      plot_path = null
      pruned_at_utc = now
 -> physical bundle delete
 -> retry orphan cleanup if deletion failed/interrupted
```

The tombstone happens **before** deletion, so a crash cannot leave a selectable row pointing at a partially deleted bundle.

Current/building/failed/other-version rows are never pruned. Repeated retention is idempotent.

## Operational Manifests

`dataset_inventory.parquet` is a snapshot of observed Bronze coverage, not a synthetic market calendar. It records canonical identity, provider, min/max observed date, row count, duplicate-key count, and physical file count.

For normal source update planning, **max observed date is relevant; min observed date is not the update start**.

`ingestion_runs.parquet` records run ID, series/provider, operation mode, requested bounds, fetched/accepted/inserted/revised rows, written partitions, status, timestamps, and sanitized error metadata.

No secrets may appear in registry files, persisted URLs, logs, error strings, or fixtures.

## Daily Pipeline

`run-daily` performs:

```text
1. recover stale Gold building attempts
2. reconcile root Gold materialized views if needed
3. for each selected series, using bounded parallel execution (`--cores`, default 6):
      no Bronze -> bootstrap
      Bronze exists -> strict update from latest-overlap through today
4. Bronze selected/all series
5. Silver selected/all series
6. build full canonical Gold from current Silver
7. create immutable Parquet + JSON + PNG bundle
8. validate candidate
9. atomically promote catalog
10. refresh root materialized views
11. mark-and-sweep retention
12. refresh inventory
```

`run-daily` must never invoke market-source full-history `reconcile` for an existing series. The source `reconcile` command is separate and explicit.

Gold always uses the full canonical schema from all available Silver inputs even when Bronze/Silver source execution was filtered.

The application shares one injected execution policy across independent Bronze series
work and Silver build/read operations. Each Bronze series retains its own unit-of-work
commit boundary; shared run-manifest and ingestion-state updates are serialized so
concurrent series cannot overwrite each other's durable audit/state records. Results are
reported in input order. A core count of one executes these work units sequentially.

## Local Quality And Git Contract

GitHub Actions are not used. The local pre-commit hook runs `make quality-gate`; local
checks can also be run directly. The suite comprises:

```text
lint
type
unit
integration
coverage
```

`lint`, `type`, unit, and offline `integration` run locally. Unit and integration tests
write separate coverage data; the `coverage` target combines it and applies the configured
production-code line coverage threshold of `85.0%` from `pyproject.toml`.

`network` tests are excluded from local checks. No GitHub Actions workflow or remote
quality gate is part of this repository's test contract.

## Testing Contract

Tests are deterministic and offline by default.

Required classes include:

- exact schema/type/order tests;
- provider parsing fixtures;
- retry/error/secret-redaction tests;
- canonical delta request tests proving max-date-derived bounds and rejecting historical-minimum requests;
- `date_range` exact-bound tests;
- `full_file` post-fetch logical-window filtering tests;
- assertions that `run-daily` never auto-invokes source reconcile;
- no-op hash/mtime tests;
- failure injection around durability boundaries;
- restart/recovery tests;
- hand-calculable Gold formula tests;
- truncation-based causality regression;
- catalog resolution policy tests;
- materialized-view reconciliation tests;
- retention tombstone/orphan-cleanup tests;
- end-to-end bootstrap/delta/explicit-reconcile/publication regression.

## Relationship To `crypto-history-loader`

`crypto-history-loader` remains the design reference for deterministic medallion ownership, Polars/Parquet persistence, restart safety, `timestamp_m1`, Gold JSON manifests, and feature-profile plots.

`macro-loader` intentionally uses daily source semantics, monthly Bronze/Silver partitions, strict delta-only normal execution, optional explicit source reconciliation, and a catalog-driven immutable Gold publication model.

Feature numerics are single-sourced in the versioned PostgreSQL `macro_features`
materialized view. Python retains only source-series policy metadata and canonical
column naming. The authorized full-history and Daily-wrapper acceptance runners
execute the documented commands in order and emit sanitized reports under
`artifacts/acceptance/`; neither runner is part of the normal cron path.

The view's raw-source projections are named `*_log_level` and contain the
natural logarithm of the corresponding positive raw feature level. Missing or
non-positive values are exposed as `NULL`; original levels remain in their
feature-specific raw tables. A delta sync explicitly refreshes the materialized view
after applying its row mutations.
