# MACRO-LOADER

Reusable daily market-state loader for quantitative research and portfolio systems.

The repository acquires open/public market and macro series, preserves their historical observations in a deterministic Parquet lake, normalizes them into a canonical daily schema, derives causal market-state features, and publishes immutable Gold snapshots for downstream consumers. Canonical Gold can additionally be replicated into PostgreSQL as a rebuildable serving-plane copy; immutable Parquet Gold remains the source of truth.

The project is intentionally a **data product**, not a trading system. It does not own HMM states, `risk_on`/`risk_off` labels, prediction targets, portfolio weights, or execution decisions.

## Status

The reviewed medallion architecture is implemented through the atomic PR sequence in `BACKLOG.md`, including the PostgreSQL serving-plane sequence: only canonical Gold is replicated, while Bronze, Silver, immutable Gold bundles, and the authoritative Gold catalog remain local lake concerns.

The repository includes native OHLC inputs and OHLC-derived transformations in PostgreSQL.
Production migration 36 renamed the raw-derived long transformation materialized view to
`macro_loader.macro_features` and retired the superseded views on 2026-10-27. All 25
`macro_raw_<feature>` tables contain source rows; the wide `macro_raw` and orphaned retired
Fed table are absent. The long view has 11,794,988 rows across 1,720 features. PostgreSQL
sync state is schema 11 / feature 6, with 16,805 consumer rows and matching row digests.
The installed daily 09:00 `Europe/Vienna` cron wrapper passed its full acceptance on
2026-10-27, including a zero-change PostgreSQL replay and independent verification.
This operator guide does not provide Fed-specific command or scheduling instructions.
Historical Fed acceptance artifacts and backlog entries are retained for audit. The
independent FRED provider remains active for its registered macro series.

The current Gold semantic contract is schema version **11** and feature version **6**;
the authoritative definitions are `application.gold_frame.GOLD_SCHEMA_VERSION` and
`application.gold_frame.GOLD_FEATURE_VERSION`. PostgreSQL schema migration 31 moves the
serving store to one `macro_loader.macro_raw_<feature>` table per registered source.
The previous wide `macro_loader.macro_raw` is used only as the migration's historical
backfill input, then dropped; runtime synchronization never writes to or recreates it.
After migration 36, `macro_loader.macro_features` is the long-form materialized view
projected directly from the feature tables. Its columns are
`timestamp_m1`, `available_at_utc`, `feature_name`, and `value DOUBLE PRECISION`; the unique
key is `(timestamp_m1, feature_name)`. The former wide `macro_features` and
`macro_features_extended` materialized views and their private projection/transform views
are retired. Raw feature tables and canonical Gold synchronization state are unchanged.

Gold columns are ordered as `timestamp_m1`, every registered `<series>_level` in
registry order, then native OHLC fields in OHLC-series registry order and field order
`open`, `high`, `low`, `close`. The six OHLC series (`vix`, `vix9d`, `vix3m`, `vix6m`,
`vix1y`, and `move`) each have a table with nullable `open/high/low/close DOUBLE PRECISION`;
each of the 19 scalar series has nullable `indexvalue DOUBLE PRECISION`. Every table uses
`timestamp_m1 TIMESTAMPTZ(6)` as its primary key and has the same generated, non-null
`available_at_utc TIMESTAMPTZ(6)`. `macro_features` reconstructs the source projection
and exposes the same availability timestamp.

For the six OHLC series only, `macro_features` also provides `*_high_low_range_1obs` and
`*_high_low_range_5obs` as `(high-low)/close`, `*_intraday_return_1obs` and
`*_intraday_return_5obs` as `(close-open)/open`, and `*_overnight_gap_1obs` and
`*_overnight_gap_5obs` as `(open[t]-close[previous valid source observation]) /
close[previous valid source observation]`. These are decimal fractions (for example,
`0.05` means 5%), not percent-scaled values. The 1-observation variants are the current
valid metric; each 5-observation variant is the arithmetic mean of five trailing valid
metrics and remains NULL until that complete window exists. Each overnight gap pairs a
finite current open with the most recent preceding finite close in that series' observation
history; the current bar's close is not an operand. This follows source-observation order
across sparse calendar dates. Missing/non-finite operands or nonpositive denominators yield
NULL; no values are carried or fabricated.

The six OHLC series also have stochastic and Donchian outputs in `macro_features` only.
`*_stoch_k_5` and `*_stoch_k_14` use `100*(close-min(low,n))/(max(high,n)-min(low,n))`
over 5 or 14 consecutive complete finite OHLC source observations. A zero-range or
incomplete window is NULL; otherwise `%K` is on a 0–100 scale. `*_stoch_d_5_3` and
`*_stoch_d_14_3` are three-consecutive-source-observation means of their `%K` series;
`*_stoch_kd_spread_5` is `%K5-%D5`, and `*_stoch_k_slope_3` is
`(%K5[t]-%K5[t-3])/3`. Donchian columns are nullable BOOLEANs: `*_donchian_breakout_up`
is true when current high strictly exceeds the maximum high of the prior 20 valid source
bars, and `*_donchian_breakout_down` is true when current low is strictly below the prior
20-bar minimum low. The current bar is excluded from those channel bounds. Their warm-up
or invalid-input results are NULL. All windows follow source observations across sparse
calendar dates; no helper columns are exposed and none of these outputs is added to
the feature-specific raw tables or canonical Gold.

Before implementing a backlog PR, coding agents must read `AGENTS.md`, `BACKLOG.md`, and `ARCHITECTURE.md`.

## Architecture

The implementation uses **hexagonal architecture (Ports and Adapters)** with dependency injection and composition.

```text
                         api / scripts
                              |
                              v
                         application
                    use cases + policies
                    /                 \
                   v                   v
          persistence ports       provider ports
                   ^                   ^
                   |                   |
        Parquet/PostgreSQL       HTTP/provider
        repository adapters       adapters
                   \                   /
                    +--------+---------+
                             |
                             v
                 Bronze -> Silver -> Gold
                                      |
                                      v
                              PostgreSQL serving
```

Core design patterns are deliberately explicit:

- **Adapter** for provider and persistence implementations.
- **Strategy** for retry, update/reconciliation, and consumer-resolution policies.
- **Registry/Factory** for canonical provider/series routing; orchestration must not use provider `if/elif` ladders.
- **Repository** for Bronze, Silver, ingestion state, run manifests, Gold build storage, the Gold catalog, and PostgreSQL serving state.
- **Unit of Work** for one-series ingestion durability, Gold publication commit boundaries, and transactional PostgreSQL deltas.
- **State Machine** for Gold publication: `building -> complete|failed`.
- **Materialized View** for root Gold JSON/PNG, derived from authoritative `manifest.parquet`.
- **Mark-and-Sweep** for safe Gold retention: make a build unselectable before deleting physical files.
- **Command** for CLI adapters that call application use cases without embedding business logic.

Prefer `typing.Protocol`, immutable contracts, pure transformations, and constructor injection over inheritance-heavy frameworks.

## Initial Series Catalog

| Canonical ID | Primary source | Purpose |
|---|---|---|
| `vix` | CBOE | S&P 500 implied volatility |
| `vix9d` | CBOE | short-horizon implied volatility |
| `vix3m` | CBOE | 3-month implied volatility |
| `vix6m` | CBOE | 6-month implied volatility |
| `vix1y` | CBOE | 1-year implied volatility |
| `vstoxx` | STOXX | Euro-area equity implied volatility |
| `vstoxx1m` | STOXX (`V6I1`) | 1-month Euro-area equity implied volatility |
| `vstoxx2m` | STOXX (`V6I2`) | 2-month Euro-area equity implied volatility |
| `vstoxx3m` | STOXX (`V6I3`) | 3-month Euro-area equity implied volatility |
| `move` | Yahoo Finance | US Treasury implied volatility |
| `ciss` | ECB | euro-area systemic stress |
| `ciss_money` | ECB (`SS_MMN.CON`) | New-CISS money-market component contribution |
| `ciss_bonds` | ECB (`SS_BMN.CON`) | New-CISS bond-market component contribution |
| `ciss_equity` | ECB (`SS_EMN.CON`) | New-CISS equity-market component contribution |
| `ciss_financials` | ECB (`SS_FIN.CON`) | New-CISS financial-intermediaries component contribution |
| `estr` | ECB | euro short-term rate |
| `euro_hy_oas` | FRED | euro high-yield credit spread |
| `euro_hy_yield` | FRED (`BAMLHE00EHYIEY`) | Euro high-yield effective yield, percent |
| `euro_hy_ytw` | FRED (`BAMLHE00EHYISYTW`) | Euro high-yield semi-annual yield to worst, percent |
| `euro_hy_total_return` | FRED (`BAMLHE00EHYITRIV`) | Euro high-yield total-return index |
| `us_2y` | FRED | US 2-year Treasury yield |
| `us_10y` | FRED | US 10-year Treasury yield |
| `usd_broad` | FRED | broad US-dollar index |
| `us_3m` | FRED (`DGS3MO`) | U.S. 3-month constant-maturity Treasury yield, percent |
| `us_5y` | FRED (`DGS5`) | U.S. 5-year constant-maturity Treasury yield, percent |

No additional MVP series may be introduced without a separate backlog PR and matching contract updates.

### PostgreSQL raw-feature table catalogue

Migration 31 creates exactly one `macro_loader.macro_raw_<feature>` base table for each
series below. All tables are daily and share `timestamp_m1 TIMESTAMPTZ(6)` (primary key)
and generated `available_at_utc TIMESTAMPTZ(6)`. The six native-OHLC tables add nullable
`open`, `high`, `low`, and `close DOUBLE PRECISION`; each scalar table adds nullable
`indexvalue DOUBLE PRECISION`. “Local Silver range” is the persisted repository lake
inventory checked 2026-10-27, not a claim about a production PostgreSQL deployment.
Series newly registered in PRs 177–180 are not yet present in that local Silver snapshot;
their exposed source periods are stated instead. Sparse source gaps remain absent.

| Feature / table | Provider source ID | Columns after timestamp/availability | Range recorded for this repository | Meaning |
|---|---|---|---|---|
| `vix` / `macro_raw_vix` | CBOE `VIX_History.csv` | `open, high, low, close` | Local Silver 1990-01-02–2026-10-12 | S&P 500 30-day implied volatility |
| `vix9d` / `macro_raw_vix9d` | CBOE `VIX9D_History.csv` | OHLC | Local Silver 2011-01-04–2026-10-12 | S&P 500 9-day implied volatility |
| `vix3m` / `macro_raw_vix3m` | CBOE `VIX3M_History.csv` | OHLC | Local Silver 2009-09-18–2026-10-12 | S&P 500 3-month implied volatility |
| `vix6m` / `macro_raw_vix6m` | CBOE `VIX6M_History.csv` | OHLC | Local Silver 2008-01-02–2026-10-12 | S&P 500 6-month implied volatility |
| `vix1y` / `macro_raw_vix1y` | CBOE `VIX1Y_History.csv` | OHLC | Local Silver 2007-01-03–2026-10-12 | S&P 500 1-year implied volatility |
| `vstoxx` / `macro_raw_vstoxx` | STOXX `V2TX` | `indexvalue` | Local Silver 1999-01-04–2026-10-12 | Euro-area equity volatility index |
| `vstoxx1m` / `macro_raw_vstoxx1m` | STOXX `V6I1` | `indexvalue` | Source files 1999-01-04–2026-10-26; not yet in local Silver | 1-month VSTOXX term structure |
| `vstoxx2m` / `macro_raw_vstoxx2m` | STOXX `V6I2` | `indexvalue` | Source files 1999-01-04–2026-10-26; not yet in local Silver | 2-month VSTOXX term structure |
| `vstoxx3m` / `macro_raw_vstoxx3m` | STOXX `V6I3` | `indexvalue` | Source files 1999-01-04–2026-10-26; not yet in local Silver | 3-month VSTOXX term structure |
| `move` / `macro_raw_move` | Yahoo Finance `^MOVE` | OHLC | Local Silver 2002-11-12–2026-10-09 | U.S. Treasury-bond implied volatility |
| `ciss` / `macro_raw_ciss` | ECB `CISS.D.U2.Z0Z.4F.EC.SS_CIN.IDX` | `indexvalue` | Local Silver 1980-01-03–2026-09-02 | Euro-area composite systemic-stress indicator |
| `ciss_money` / `macro_raw_ciss_money` | ECB `CISS.D.U2.Z0Z.4F.EC.SS_MMN.CON` | `indexvalue` | Source catalog from 1999-01-01; not yet in local Silver | Money-market contribution to New-CISS |
| `ciss_bonds` / `macro_raw_ciss_bonds` | ECB `CISS.D.U2.Z0Z.4F.EC.SS_BMN.CON` | `indexvalue` | Source catalog from 1999-01-01; not yet in local Silver | Bond-market contribution to New-CISS |
| `ciss_equity` / `macro_raw_ciss_equity` | ECB `CISS.D.U2.Z0Z.4F.EC.SS_EMN.CON` | `indexvalue` | Source catalog from 1999-01-01; not yet in local Silver | Equity-market contribution to New-CISS |
| `ciss_financials` / `macro_raw_ciss_financials` | ECB `CISS.D.U2.Z0Z.4F.EC.SS_FIN.CON` | `indexvalue` | Source catalog from 1999-01-01; not yet in local Silver | Financial-intermediary contribution to New-CISS |
| `estr` / `macro_raw_estr` | ECB `EST.B.EU000A2X2A25.WT` | `indexvalue` | Local Silver 2019-10-01–2026-09-03 | Euro short-term rate |
| `euro_hy_oas` / `macro_raw_euro_hy_oas` | FRED `BAMLHE00EHYIOAS` | `indexvalue` | Local Silver 1997-12-31–2026-10-08; current FRED exposure is rolling | Euro high-yield option-adjusted spread (percent) |
| `euro_hy_yield` / `macro_raw_euro_hy_yield` | FRED `BAMLHE00EHYIEY` | `indexvalue` | FRED exposure 2023-10-27 onward; not yet in local Silver | Euro high-yield effective yield (percent) |
| `euro_hy_ytw` / `macro_raw_euro_hy_ytw` | FRED `BAMLHE00EHYISYTW` | `indexvalue` | FRED exposure 2023-10-27 onward; not yet in local Silver | Euro high-yield yield-to-worst (percent) |
| `euro_hy_total_return` / `macro_raw_euro_hy_total_return` | FRED `BAMLHE00EHYITRIV` | `indexvalue` | FRED exposure 2023-10-27 onward; not yet in local Silver | Euro high-yield total-return index level |
| `us_3m` / `macro_raw_us_3m` | FRED `DGS3MO` | `indexvalue` | Source history from 1981-09-01; not yet in local Silver | U.S. 3-month Treasury yield (percent) |
| `us_2y` / `macro_raw_us_2y` | FRED `DGS2` | `indexvalue` | Local Silver 1976-06-01–2026-10-08 | U.S. 2-year Treasury yield (percent) |
| `us_5y` / `macro_raw_us_5y` | FRED `DGS5` | `indexvalue` | Source history from 1962-01-02; not yet in local Silver | U.S. 5-year Treasury yield (percent) |
| `us_10y` / `macro_raw_us_10y` | FRED `DGS10` | `indexvalue` | Local Silver 1962-01-02–2026-10-08 | U.S. 10-year Treasury yield (percent) |
| `usd_broad` / `macro_raw_usd_broad` | FRED `DTWEXBGS` | `indexvalue` | Local Silver 2006-01-02–2026-10-02 | Trade-weighted broad U.S. dollar index |

The latest source observation and each lake's durable coverage can differ. A source that
publishes a shorter history does not remove previously retained rows. After cutover,
ordinary Gold synchronization writes these tables transactionally, including corrections
and deletions, and refreshes the long-form `macro_features` from those raw sources.

### STOXX term-structure raw features

`vstoxx1m`, `vstoxx2m`, and `vstoxx3m` are daily STOXX implied-volatility index
observations sourced respectively from `V6I1`, `V6I2`, and `V6I3`. The official files
are [`h_v6i1.txt`](https://www.stoxx.com/document/Indices/Current/HistoricalData/h_v6i1.txt),
[`h_v6i2.txt`](https://www.stoxx.com/document/Indices/Current/HistoricalData/h_v6i2.txt),
and [`h_v6i3.txt`](https://www.stoxx.com/document/Indices/Current/HistoricalData/h_v6i3.txt).
Each provider row contains `Date`, `Symbol`, and `Indexvalue`; the canonical scalar is
stored in `value` with the exact source symbol, source URL, observation date, and UTC
fetch time in Bronze. Values are in index points, with no synthetic dates or carry-forward.
Official rows with an empty `Indexvalue` are omitted as missing source observations;
malformed non-empty values still fail ingestion.
The files exposed history from 1999-01-04 through 2026-10-26 when checked on 2026-10-27;
the newest exposed date changes as STOXX publishes data. These are full-file sources:
bootstrap keeps all exposed observations, while an update persists only its requested
logical date range.

### ECB New-CISS component contributions

`ciss_money`, `ciss_bonds`, `ciss_equity`, and `ciss_financials` are daily Euro-area
contributions from the ECB New-CISS money, bond, equity, and financial-intermediary
subindices. Their exact ECB source keys are `CISS.D.U2.Z0Z.4F.EC.SS_MMN.CON`,
`CISS.D.U2.Z0Z.4F.EC.SS_BMN.CON`, `CISS.D.U2.Z0Z.4F.EC.SS_EMN.CON`, and
`CISS.D.U2.Z0Z.4F.EC.SS_FIN.CON`. The provider's `TIME_PERIOD` and `OBS_VALUE` fields
become canonical `observation_date` and Float64 `value`; the value unit is a pure-number
dimensionless contribution, not an independent component index. The ECB catalog exposes
history from 1999-01-01 through its latest published observation. Sparse dates and source
missing-value markers remain absent from the stored observations; nothing is carried or
synthesized. Normal updates send exact `startPeriod`/`endPeriod` bounds to the ECB API.

### Euro high-yield source series

`euro_hy_oas`, `euro_hy_yield`, `euro_hy_ytw`, and `euro_hy_total_return` are daily
ICE BofA Euro High Yield Index series exposed through FRED. Their FRED identifiers are
`BAMLHE00EHYIOAS`, `BAMLHE00EHYIEY`, `BAMLHE00EHYISYTW`, and `BAMLHE00EHYITRIV`,
respectively. OAS, effective yield, and semi-annual yield-to-worst are percentages;
total return is an index level and is not a percentage return. Each source reports daily
close observations. On 2026-10-27, FRED exposed values from 2023-10-27 through
2026-10-23 for all four series. FRED notes that since April 2026 these ICE BofA series
expose only a rolling three years; this is a source limitation, not a reason to remove
older observations already durable in the lake. Existing `euro_hy_oas` history is reused
for migration rather than downloaded a second time. New observations enter the normal
source/Gold sync path. ICE's FRED terms state that index data is for internal use and may
not be redistributed to third parties without ICE's prior written approval; users must
observe those restrictions.

### U.S. Treasury yields

`us_3m` (`DGS3MO`) and `us_5y` (`DGS5`) are daily, not-seasonally-adjusted U.S.
constant-maturity Treasury yields from the Federal Reserve Board's H.15 release, quoted
in percent. FRED's [DGS3MO table](https://fred.stlouisfed.org/data/DGS3MO) exposed
`us_3m` from 1981-09-01 through 2026-10-22, and its
[DGS5 table](https://fred.stlouisfed.org/data/DGS5) exposed `us_5y` from 1962-01-02
through 2026-10-23 when checked on 2026-10-27. These are the first exposed observations,
not promises of future availability. Source observations with
the FRED `.` missing marker are omitted; no calendar-day values are synthesized.
Normal updates request the exact bounded delta, and older durable observations remain
stored if a source response is shorter.

## Source Update Policy

The loader distinguishes three explicit operation modes:

```text
bootstrap   -> first complete public history when no Bronze exists
update      -> normal delta-only execution for an existing series
reconcile   -> explicit operator-requested full-history reconciliation
```

### Normal update is delta-only

For an existing series, the authoritative Bronze data determines:

```text
latest_stored_date = max(Bronze.observation_date)
request_start      = latest_stored_date - overlap_days
request_end        = injected_today
```

Default `overlap_days = 7` calendar days so recent source corrections can still replace equal-key observations. The normal `update` command and `run-daily` **never automatically switch to full-history reconciliation**.

For `date_range` providers, the network request must use those exact bounds. For `full_file` providers such as catalogued CBOE/STOXX sources, the complete remote object may have to be downloaded, but the adapter restricts the logical update/diff to the requested delta window before persistence. Normal execution therefore rewrites only inserted/revised delta rows and affected monthly partitions.

A shorter upstream response is never interpreted as permission to delete older retained history. Equal-key observations may be revised. Explicit deletion semantics require an explicit provider contract and are not inferred from omission.

### Explicit reconciliation

`reconcile` is a separate explicit command. It may request maximum currently exposed history to detect older revisions outside the overlap window. It is **not invoked automatically by `run-daily`**. If operators want periodic reconciliation, they schedule the explicit `reconcile` command separately.

## Medallion Lake

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

Bronze and Silver use deterministic monthly partitions. Gold is small enough to publish complete immutable snapshots.

## Bronze

Bronze preserves provider-shaped observations plus safe ingestion metadata. Natural key:

```text
(provider, series_id, observation_date)
```

## Silver

Canonical daily long-form contract:

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

OHLC sources use `value == close`; scalar sources use `value` and null OHLC fields. Silver never fills missing dates.

## Gold

Gold uses the temporal key:

```text
timestamp_m1: Datetime(time_unit="us", time_zone="UTC")
```

Daily source dates map to UTC midnight. This is **observation-day identity**, not provider release time or tradability time. Gold contains no `observation_date` column.

Feature semantics are fixed and causal:

- `delta_Nobs(t) = x(t) - x(previous Nth valid observation)`;
- every source level includes a causal `delta_1obs`; existing 5- and 20-observation deltas remain available according to the feature family;
- 60-observation z-scores use the last 60 valid observations including `t` and population standard deviation (`ddof=0`);
- each of the 13 eligible volatility and macro series includes positive momentum autocorrelation at `(lag, window)` pairs `(1, 60)`, `(5, 60)`, and `(20, 120)`;
- momentum autocorrelation is computed on one-observation source-unit changes, clips negative correlations to zero, and remains null until its full causal window is available;
- each eligible source level includes XETRA-compatible log returns over 1, 2, 3, 4, 5, 8, 10, and 20 observations; cumulative geometric returns over 4, 5, 8, 10, 16, and 20 observations; average one-observation log returns over 5, 10, and 20 observations; and sample log-return volatility over 5, 8, 10, 20, 32, 40, and 96 observations. Geometric-return columns retain the `_pct` suffix but store decimal fractions (`0.05` means 5%);
- eligible source levels also include SMA ratios `(5,15)`, `(5,20)`, `(10,20)`, `(10,40)`; Wilder RSI over 3, 5, 7, and 14 observations; ROC over 1, 2, 3, 4, 5, 8, 10, and 20 observations; and rolling drawdown over 20, 32, 60, and 96 observations. These are complete-window, observation-based calculations; RSI uses Wilder recurrence, with fully flat gain/loss windows as `NULL` and all-gain/all-loss windows as 100/0;
- price-domain XETRA families are emitted only for positive-level series; signed/non-price levels such as `estr` remain available as raw levels and source-unit delta/momentum features but do not receive log, return, volatility, SMA-ratio, RSI, ROC, or drawdown columns;
- no forward fill, backward fill, interpolation, centered windows, or implicit as-of carry;
- same-series rolling operations count valid observations, not calendar days;
- cross-series ratios/spreads require the same `timestamp_m1`;
- final Gold contains nulls but no NaN or infinity.

Initial semantic versions:

```text
schema_version  = 6
feature_version = 5
```

Schema version changes for column name/order/type changes. Feature version changes for formula/parameter changes that preserve schema.

## Immutable Gold Build Bundle

Each successful build directory is creation-only:

```text
versions/build_id=<YYYYMMDDTHHMMSSZ>/
  data.parquet
  manifest.json
  feature_profile.png
```

`data.parquet` is the canonical full-history Gold frame. Build `manifest.json` records dataset/build identity, semantic versions, ordered columns, row/timestamp bounds, `data_sha256`, `feature_set_hash`, source Git commit, and plot path.

## Authoritative Gold Catalog

The publication authority is:

```text
lake/gold/dataset=macro_features_daily/manifest.parquet
```

Only `manifest.parquet` chooses the current build. Consumers and PostgreSQL synchronization must never use directory order, mtime, or `max(build_id)`.

Consumer resolution is policy-driven:

- `strict_current` is the safe default: current must be compatible/selectable or resolution fails.
- `latest_compatible` is an explicit resilience policy for ordinary local consumers.

PostgreSQL synchronization deliberately uses the strict current compatible build and its explicit immutable `data_path`.

## Root JSON And Plot Are Materialized Views

The dataset root also exposes rebuildable `manifest.json` and `feature_profile.png`. These materialized views do not participate in consumer selection. The atomic catalog replacement is the Gold publication commit.

## Publication State Machine

```text
new attempt
    |
    v
building,current=false
    |
    +--> build/validate immutable bundle
    |          |
    |          +-- failure --> failed,current=false
    |
    v
atomic catalog promotion
new=complete,current=true
old=current=false
    |
    v
refresh root materialized views
```

A previous current build remains authoritative until the atomic catalog promotion. Filesystem presence never auto-promotes an interrupted build.

## Retention

Default retention keeps five physical successful builds per `(schema_version, feature_version)` pair, including current. Retention first marks a build unselectable in the catalog and only then deletes its immutable physical bundle.

## PostgreSQL Gold Serving Replica

PostgreSQL is a serving/research replica, not the canonical data store. The only synchronized dataset is `macro_features_daily`.

```text
canonical source: lake/gold/dataset=macro_features_daily/...
raw tables:      macro_loader.macro_raw_<feature> (25 tables)
consumer view:   macro_loader.macro_features
sync state:      macro_loader_sync.gold_sync_state
row digests:     macro_loader_sync.gold_row_hashes
```

Migration 31 transactionally backfills each feature table from the former wide
`macro_loader.macro_raw` and drops that wide relation. It is a one-time migration source;
no runtime sync path writes to or recreates it. Every feature table has the common ordered
columns `timestamp_m1 TIMESTAMPTZ(6) NOT NULL PRIMARY KEY`, generated non-null
`available_at_utc TIMESTAMPTZ(6)`, then either nullable OHLC fields (`open`, `high`,
`low`, `close`) or nullable scalar `indexvalue DOUBLE PRECISION`. All timestamps are
microsecond-precision UTC. Exact provider identifiers, persisted/source-history periods,
shape, and feature meaning are catalogued in the [raw-feature table catalogue](#postgresql-raw-feature-table-catalogue).
Sync state and row digests stay in the separate `macro_loader_sync` schema.

Migration 36 publishes the populated long-form view as the sole public transformation
relation, `macro_loader.macro_features`, and drops the superseded wide
`macro_features`/`macro_features_extended` views and their private projection/transform
materialized views. The rename preserves the already populated long view and avoids another
full-history recomputation. The four columns are `timestamp_m1`, projected
`available_at_utc`, `feature_name`, and `value DOUBLE PRECISION`. Its unique index is
`(timestamp_m1, feature_name)` and a feature-first index supports time-series lookup.
Boolean Donchian outputs are encoded as `1.0` (true) and `0.0` (false); SQL NULLs,
including causal warm-up values, have no row. Raw OHLC passthrough fields are excluded.
The materialized-view definition calculates all catalogued transformations directly from
the 25 `macro_raw_<feature>` tables. Consumer and sync roles may SELECT but cannot write.
After Gold sync updates raw tables, the explicit refresh routine fully recomputes this
materialized view. Manual raw-table writes require the explicit refresh routine and do not
fire a trigger. The catalog fingerprint and independent PostgreSQL conformance check
detect schema, type, source, index, obsolete-view, and population drift. Full-history
refresh and independent source comparison use the bounded 15-minute feature-query timeout.

In the transformation definitions below, each named output is stored as a `feature_name`
row in this long-form view, not as a wide SQL column.

#### PR-171: Composite and Statistical Feature Semantics

The version-12 transformation catalog defines the following series-prefixed, nullable
`DOUBLE PRECISION` feature values for every `PRICE_FEATURE_SERIES` (all positive-level
price series; signed/non-price `estr` is excluded). These are serving-view transformations
only: they do not add columns to the raw feature tables or canonical Gold. Every lag/window is measured
on the source series' non-null observation rows, not calendar days.

| Column suffix | Formula | Warm-up and NULL rules |
| --- | --- | --- |
| `_rsi_ratio_5_15` | `RSI_5(t) / RSI_15(t)` | RSI values use Wilder-smoothed gains/losses from positive adjacent source levels. The seed is the arithmetic mean of the first 5 or 15 valid one-observation gains/losses; later averages use `((n-1) * previous_average + current_value) / n`. RSI is `100 - 100 / (1 + average_gain / average_loss)`. It is NULL until the seed is available; fully flat gain/loss state is NULL, while zero average loss with positive average gain yields 100. The ratio is NULL unless both RSI values exist and `RSI_15 != 0`. RSI-15 is an internal intermediate and is not exposed as a column. |
| `_rsi5_slope_3` | `(RSI_5(t) - RSI_5(t-3 source observations)) / 3` | Requires both endpoint RSI-5 values. The three-row lag is over source-observation rows; intermediate NULL RSI values are not skipped, so they remain NULL at either endpoint. |
| `_rsi5_delta_1` | `RSI_5(t) - RSI_5(t-1 source observation)` | Requires both adjacent source-row RSI-5 values; a NULL at either endpoint propagates to NULL. |
| `_roc_acceleration_4_8` | `(level(t) / level(t-4) - 1) - (level(t) / level(t-8) - 1)` | Requires the current, 4-lag, and 8-lag source observations to be positive. Missing/non-positive endpoints yield NULL; no future observation is used. |
| `_volatility_zscore_96` | `(volatility_8(t) - mean_96(volatility_8)) / sample_stddev_96(volatility_8)` | `volatility_8` is the sample standard deviation (`ddof=1`) of eight valid one-observation log returns in the trailing eight source rows. The z-score uses the current and previous 95 non-NULL `volatility_8` values (96 valid volatility observations; NULL values are skipped when forming this history). It is NULL until all 96 exist or if their sample standard deviation is zero/undefined. |
| `_level_zscore_96` | `(level(t) - mean_96(level)) / sample_stddev_96(level)` | Uses the current and previous 95 positive, non-NULL source levels (96 valid levels; non-positive/missing rows are skipped when forming this history). It is NULL until all 96 exist or if sample standard deviation is zero/undefined. |

The signed return-autocorrelation columns are named
`_momentum_autocorr_lag{h}_window{N}` and use one-observation log returns
`r(t) = ln(level(t) / level(t-1 source observation))`:

| Column suffix | Pair definition and window |
| --- | --- |
| `_momentum_autocorr_lag1_window96` | Pearson correlation of `(r(t), r(t-1))` over the trailing 96 valid pairs. |
| `_momentum_autocorr_lag4_window96` | Pearson correlation of `(r(t), r(t-4))` over the trailing 96 valid pairs. |
| `_momentum_autocorr_lag8_window192` | Pearson correlation of `(r(t), r(t-8))` over the trailing 192 valid pairs. |

For these features the lag is applied to the return stream before incomplete pairs are
filtered; the trailing window then counts valid pairs, not rows or days. Both returns in a
pair must be non-NULL. Output is NULL until the full pair window exists, or if either side
of the window has zero/undefined sample standard deviation (so Pearson correlation is
undefined). Correlations retain their sign and are not clipped at zero. These return-based
features are distinct from the existing positive-clipped autocorrelation of source-unit
changes. All calculations are causal and use no future values. The PR-172 contract adds
private-recursive EMA intermediates and six series-prefixed nullable outputs for each
`PRICE_FEATURE_SERIES` (excluding `estr`): EMA distance to periods 5, 9, and 21;
`(EMA5-EMA21)/level`; normalized three-source-row EMA9 slope; and an EMA5/EMA9 crossover
value (`1`, `-1`, or `0`). Each EMA seeds from the arithmetic mean of its first `n`
consecutive positive levels, then recurs with alpha `2/(n+1)`; missing or nonpositive
levels reset the seed. EMA intermediates are not view columns, and outputs remain NULL until
the seed/endpoints exist. This follows XETRA's `exponential_moving_average` recurrence.
PR-173 adds three relative MACD outputs to the version-13 merged view. Their formulas are
`(EMA12-EMA26)/level`,
`(MACD_line-EMA9_signal)/level`, and the three-source-row difference of the relative
histogram. The signal EMA accepts signed and zero MACD-line values and resets only on a
NULL MACD line, matching XETRA's `allow_nonpositive` EMA mode. See the
[XETRA MACD CTEs](https://github.com/SergejSchweizer/xetra-loader/blob/main/sql/schema/004_xetra_features.sql#L2270-L2333)
and [output formulas](https://github.com/SergejSchweizer/xetra-loader/blob/main/sql/schema/004_xetra_features.sql#L2776-L2785).
Volume transformations, market breadth, and cross-instrument dispersion are excluded by
the current user scope. The raw contract has no applicable `volume_level` field or
cross-instrument panel.

### Availability-time contract (introduced in PR-167; carried by migration 31)

The PR-167 implementation contract adds a generated, non-null `available_at_utc`
to every PostgreSQL raw table; the `macro_features` materialized view exposes the
same value. For a UTC observation date represented
by `timestamp_m1`, availability is `00:00:00 UTC` on the first Monday-Friday date
strictly after that observation date. Weekends therefore roll forward to Monday.
Public holidays are not excluded: these data span market calendars and this
serving contract uses a generic weekday-only calendar.

This is PostgreSQL-serving metadata, not a canonical Gold timestamp. Its generated
value does not alter the canonical Gold payload or row digests.

The first successful `gold-sync-postgres` run is necessarily a complete bootstrap because PostgreSQL has no synchronized state. Every later run compares the complete current Gold state against the complete stored row-digest state. This is an **accumulated delta**: if one or more daily runs were missed, the next run inserts all missing rows, updates historical revisions, deletes stale serving keys, leaves unchanged rows untouched, and advances the synchronized checkpoint atomically.

A `feature_version` mismatch, backwards schema transition, or schema transition outside
the supported contract fails closed. Consecutive Gold schema versions 7 through 11 may be
coalesced only while feature version 6 remains unchanged; the accumulated delta then
reconciles canonical rows and digests atomically without resetting PostgreSQL sync state.
PostgreSQL delete semantics affect only the rebuildable serving replica and do not alter
Bronze, Silver, immutable Gold, or source-history retention rules.

## Operational CLI

Install/sync the project and use the console entry point:

```bash
uv sync
uv run macro-loader --help
```

The exact command surface is:

```text
bootstrap
update
reconcile
silver-build
gold-build
gold-sync-postgres
postgres-migrate
postgres-verify
postgres-reconstruct
inventory
run-daily
```

Global options such as `--lake-root`, `--today`, `--overlap-days`, and `--cores` precede
the subcommand. `--cores` must be positive and defaults to `6`; it bounds concurrent
independent source-series and Silver work. `--series` follows commands that accept a
series restriction.

Examples:

```bash
# Normal bounded source update only.
uv run macro-loader \
  --lake-root /srv/market-regime/lake \
  update --series us_10y

# Explicit operator reconciliation; never invoked by run-daily.
uv run macro-loader \
  --lake-root /srv/market-regime/lake \
  reconcile --series us_10y

# Full local Gold publication path.
uv run macro-loader \
  --lake-root /srv/market-regime/lake \
  --cores 6 \
  run-daily

# Synchronize the currently catalog-selected Gold build only.
uv run macro-loader \
  --lake-root /srv/market-regime/lake \
  gold-sync-postgres

# Explicitly apply PostgreSQL schema migrations with protected admin credentials.
uv run macro-loader postgres-migrate

# Independently verify the current Gold bundle and PostgreSQL serving replica.
uv run macro-loader \
  --lake-root /srv/market-regime/lake \
  postgres-verify

# Rebuild and print the local inventory.
uv run macro-loader \
  --lake-root /srv/market-regime/lake \
  inventory --json
```

`gold-sync-postgres` is intentionally independent of `run-daily`: it does not construct provider clients and does not execute Bronze, Silver, Gold build/publication, mirror, retention, or source reconciliation. This makes a failed database synchronization safely retryable without rebuilding canonical Gold.

`postgres-verify` is a read-mostly, independent serving-plane check. It verifies the current
certified Gold bundle against actual consumer rows, consumer-derived digests, the hash index,
and sync state, then separately inspects loader schema, ownership, runtime grants, UTC session,
timeouts, application identity, and two UTC microsecond round trips. It rolls its transaction
back, emits deterministic sanitized JSON only, and returns non-zero unless every check is `PASS`.

`postgres-reconstruct --execute` is the one explicit production reconstruction command. It
disables the Daily runner, acquires its filesystem lock and a PostgreSQL maintenance lock,
requires the exact production endpoint, validates private restore evidence before source
reconciliation or schema recreation, then reconciles all canonical series, rebuilds Gold,
recreates only the loader-owned PostgreSQL schemas, publishes, independently verifies, and
requires a zero-mutation replay. Scheduling remains disabled on every failure. A successful run
writes only the sanitized `artifacts/acceptance/postgres-production-reconstruction-v2.json`
evidence file; database dumps, lake copies, checksums, and restore instructions remain under
ignored `artifacts/private/`.
### Daily pipeline contract

`run-daily` is deliberately **delta-only** for sources:

```text
recover interrupted Gold publication/root views
        -> Bronze update (or bootstrap only when that series has no Bronze)
        -> selected Silver rebuild
        -> full canonical Gold from all available Silver series
        -> immutable bundle + physical validation
        -> authoritative catalog promotion
        -> root materialized-view refresh
        -> Gold retention
        -> inventory refresh
```

For existing Bronze the request window is always:

```text
max(Bronze.observation_date) - overlap_days .. injected today
```

With the default overlap this is seven calendar days. `run-daily` has no hidden call path to source `reconcile`, maximum-history loading, or the historical minimum.

### Runtime configuration

Use a **persistent** lake path. A container-local ephemeral path would lose incremental state and defeat delta planning.

FRED-backed source commands require `FRED_API_KEY`. Gold-capable commands (`gold-build`, `run-daily`) record the source Git commit and packaged/deployed environments should set `MARKET_MACRO_GIT_COMMIT` explicitly.

`gold-sync-postgres` uses only the dedicated runtime role and exact endpoint:

```text
PGHOST=10.10.1.3
PGPORT=54321
PGUSER=macro-loader
PGDATABASE=macro_loader
PGPASSWORD=<repository-specific secret>
```

All PostgreSQL macro data is stored in the dedicated `macro_loader` database. The
`macro_loader` and `macro_loader_sync` schemas are created inside that database;
the CI-only `macro_loader_test` database remains separate.

The 25 raw feature tables are in schema `macro_loader` and are named
`macro_raw_<feature>` (for example `macro_loader.macro_raw_vix`). In the deployed
environment, SQL-client inspection was verified with the `macro-loader-sync` login
using its configured `PGSYNCUSER`/`PGSYNCPASSWORD` credentials. The `macro-loader`
login did not authenticate with the configured password during that check. In a SQL
client, select database `macro_loader`, schema `macro_loader`, and use the working
deployment login; list the raw tables with:

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema = 'macro_loader'
  AND table_name LIKE 'macro_raw_%'
ORDER BY table_name;
```

Runtime sync first performs a read-only schema-contract preflight. Missing or incompatible
feature-specific raw tables, support tables, columns, or keys fail before it acquires the
row-mutation transaction. It never creates, alters, drops, grants, or migrates PostgreSQL
objects.

`postgres-verify` uses the same strictly validated runtime `PG*` endpoint configuration. It does
not run the synchronization mutation path and does not create durable probe rows or objects.

Schema migration is an explicit, separately authorized `postgres-migrate` operation. It requires the protected admin-only environment variables `MARKET_MACRO_POSTGRES_ADMIN_HOST`, `MARKET_MACRO_POSTGRES_ADMIN_PORT`, `MARKET_MACRO_POSTGRES_ADMIN_USER`, `MARKET_MACRO_POSTGRES_ADMIN_DATABASE`, and `MARKET_MACRO_POSTGRES_ADMIN_PASSWORD`. The admin user and password are distinct from the runtime role/credential and are never exported by the normal cron configuration.

The `macro-loader` runtime role is a non-owning LOGIN principal with schema `USAGE` and
read-only access to the 25 loader-owned feature tables and sync tables; it cannot create
schemas or objects, change grants, or access unrelated schemas. The dedicated
`macro-loader-sync` LOGIN role performs Gold synchronization with DML on those feature
tables and the sync tables, while the admin-managed `macro-loader-owner` role owns loader
schemas and tables without LOGIN capability.

Do not commit either credential set as a connection string. Deployment configuration lives in ignored `config.yaml`. `scripts/export_cron_config.py config.yaml` validates the exact runtime host, port, and role and exports shell-safe runtime `PG*`, lake, project, mirror, FRED, and logging variables only. The installed cron wrapper also rejects a configuration whose `runtime.project_root` does not match its resolved checkout, preventing stale paths from an older project rename.

The canonical main log is enforced as:

```text
${PROJECT_ROOT}/.logs/macro-loader.log
```

The optional Gold mirror still runs only as part of local publication; a PostgreSQL sync failure does not roll back the authoritative Gold catalog.

### Scheduling

The data lake is intended to run on the deployment host/NAS, not as scheduled GitHub Actions ingestion. The checked-in crontab template runs ingestion **daily at 09:00 Europe/Vienna** and copies Gold to the NAS **Sundays at 22:00 Europe/Vienna**. The daily job loads protected configuration, creates the project log directory, publishes local Gold, and only after a successful `run-daily` synchronizes PostgreSQL:

```cron
CRON_TZ=Europe/Vienna
0 9 * * * /home/dev_macro/macro-loader/ops/run-macro-loader-daily.sh
0 22 * * 0 /home/dev_macro/macro-loader/ops/copy-macro-loader-gold.sh
```

The daily job runs at 09:00 `Europe/Vienna` wall-clock time; daylight saving changes its UTC offset from UTC+1 in winter to UTC+2 in summer. The runner resolves its project root, exports protected `config.yaml`, creates `.logs`, and appends both command streams to `macro-loader.log`. It uses `--overlap-days 60`, a bounded calendar window spanning at least 30 normal weekday EOD observations for daily series, then synchronizes the complete canonical Gold snapshot to PostgreSQL. PostgreSQL sync runs only after `run-daily` succeeds.

At exit, the daily wrapper records `[daily-cron] finished exit_code=<code> elapsed_seconds=<seconds>` in `macro-loader.log`. This duration covers the real wrapper execution, including `run-daily` and PostgreSQL sync, and is emitted on success or failure; failures before log redirection emit the same sanitized record to stderr. Verification-only runs do not emit a real-job duration. Use a later scheduled/manual run to measure actual runtime; acceptance fixtures do not predict production duration.

A second cron entry runs Sundays at 22:00 `Europe/Vienna` and copies the contents of
`/home/dev_macro/macro-loader/lake/gold/` to `/volume1/Temp/Quotes/macro/` using `rsync -a`.
It requires `/volume1` to be mounted, uses a separate lock and log, and does not delete
files already present at the destination. Its log is
`${PROJECT_ROOT}/.logs/macro-loader-gold-copy.log`.

The daily runner passes `--cores` from `MACRO_LOADER_CORES`, defaulting to `6`; set
that variable in the service environment to use a different positive concurrency limit.

Python module debug records and sanitized structured operational events are written to
`${PROJECT_ROOT}/.logs/macro-loader-debug.log`. That file rotates at UTC midnight and
keeps up to 90 daily archives. The daily and Sunday wrappers keep their operator-facing
stdout/stderr in `macro-loader.log` and `macro-loader-gold-copy.log`; before appending,
they rotate prior-day files to date-suffixed archives and prune only those two log families
older than 90 days. Other files in `.logs` are not subject to this cleanup.

Debug records cover provider request windows and row counts, HTTP attempts/retries using a
query-free safe path, Bronze diff/commit/rollback counts, daily pipeline and Silver stage
counts, and Gold publication/retention state transitions. Failure records contain only the
exception class. PostgreSQL Gold sync logs record the selected build, schema preflight,
locked delta counts, and completion state; the adapter records transaction, materialized-
view refresh, commit, and rollback outcomes. Raw observations, digests, SQL parameters,
connection configuration, credentials, and exception messages are intentionally excluded.
Gold artifact adapters also log Parquet/catalog/sidecar creation and reads, bundle validation,
root-view refresh, retention sweeps, and mirror completion with row or artifact counts where
available. These records use canonical build IDs and exception classes only; they exclude
absolute paths, feature values, artifact digests, and exception text.
Shared Parquet, Silver, ingestion-state, inventory, and run-audit repositories log operation
outcomes and row/partition counts using canonical provider/series IDs. They do not log row
payloads, stored error messages, absolute paths, or secrets.
The live PostgreSQL conformance inspector logs verification stages, safe schema/population
counts, temporal probe counts, and exception classes. The catalog-selected Gold input reader
logs bundle validation and frame/digest read outcomes, row counts, canonical build IDs, and
exception classes without filesystem paths, feature values, digests, or exception text.
The guarded production reconstruction adapter logs fixed operation names, elapsed milliseconds,
safe counts/status, and exception classes. It never logs credentials, endpoints, paths,
subprocess output, or exception text. Guarded reconstruction remains separately and explicitly
authorized and is not part of the daily cron path.
The application-level PostgreSQL conformance and reconstruction services also log safe
start/result records, canonical build IDs, stage names/counts, statuses, and exception classes;
they omit hashes, paths, report payloads, secrets, and exception text.
Canonical Gold frame assembly logs input-series count, output row/column counts, semantic
versions, and exception classes; it never logs source values, observation dates, provenance
hashes, or exception text.

Install it for the service account after reviewing the absolute project path:

```bash
crontab ops/macro-loader.cron
```

Operational semantics are explicit:

- `run-daily` failure prevents PostgreSQL synchronization;
- `gold-sync-postgres` failure makes the cron job non-zero but does **not** roll back or invalidate the already published local Gold build;
- after a database-only failure, retry only `uv run macro-loader --lake-root "$LAKE_ROOT" gold-sync-postgres` rather than rerunning source ingestion;
- the first successful database synchronization is complete; subsequent synchronizations are accumulated deltas and catch up any missed daily runs;
- source maximum-history reconciliation remains a separate explicit command and is never part of the daily main chain;
- `run-daily` and `gold-sync-postgres` append stdout/stderr to `${PROJECT_ROOT}/.logs/macro-loader.log`; the Sunday copy uses its separate `macro-loader-gold-copy.log`.

If periodic maximum-history source reconciliation is desired, schedule `reconcile` separately and less frequently. Keeping source reconciliation separate makes the normal bounded source-update contract observable and testable.

### Feature-library acceptance runs

Use the acceptance runners only on an explicitly authorized host with the normal
runtime configuration already exported. They are dry-run by default:

```bash
uv run python scripts/macro_feature_acceptance.py --lake-root /srv/market-regime/lake
uv run python scripts/macro_feature_acceptance.py \
  --lake-root /srv/market-regime/lake --execute
uv run python scripts/macro_feature_cron_acceptance.py --project-root /home/dev_macro/macro-loader
uv run python scripts/macro_feature_cron_acceptance.py \
  --project-root /home/dev_macro/macro-loader --execute
```

The first runner plans maximum-history reconciliation for every registered series,
then Silver/Gold publication, PostgreSQL migration/sync/verification, and an
unchanged replay. The second executes the installed daily wrapper twice, explicitly
checks a zero-change PostgreSQL replay, and runs independent `postgres-verify`. It writes
only a sanitized JSON report under `artifacts/acceptance/`; no credentials or raw
provider payloads are recorded. A report is `PASS` only when every stage passes.

## Local Quality Checks

GitHub Actions are not used. Install the local pre-commit hook with `make install-hooks`;
each commit runs the local quality suite. You can also run it directly with `make quality-gate`.
The test targets are available individually, or together with `make test`:

```text
lint
type
unit
integration
coverage
```

The local quality suite runs `lint`, `type`, unit tests, and offline integration tests;
unit and integration suites produce independent coverage data. The `coverage` target
combines them and reads its minimum production-code line coverage from `pyproject.toml`:

```text
>= 85.0%
```

The offline test targets use four isolated workers by default. Override the count for constrained hosts or deterministic troubleshooting, for example `make integration TEST_WORKERS=1`.

Live provider tests are marked `network` and excluded from the local quality suite.

## Repository Structure

```text
api/                 CLI adapters only
application/         use cases, contracts, policies, ports
application/ports/   provider/persistence/clock/sleeper interfaces
ingestion/           provider + filesystem/PostgreSQL adapters
scripts/             operational wrappers and repo tooling
tests/unit/          deterministic unit tests
tests/integration/   offline component/E2E tests
tests/fixtures/      committed small fixtures
lake/                ignored runtime data
AGENTS.md             coding-agent rules
BACKLOG.md            implementation backlog, including PostgreSQL work
ARCHITECTURE.md       durable engineering contract
README.md             operator/consumer contract
```

## Documentation Contract

`BACKLOG.md`, `ARCHITECTURE.md`, `README.md`, and `AGENTS.md` must not intentionally contradict one another. `BACKLOG.md` is the sole backlog authority. A PR that changes a documented contract updates the relevant sidecars in the same PR.
