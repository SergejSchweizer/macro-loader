# AGENTS.md

This repository is implemented through small backlog PRs by parallel coding agents.

Current delivery status (2026-10-28): PRs #192–#219 are merged to `origin/main`.
Production migration 36, canonical Gold sync, and independent verification have completed;
25 raw feature tables and the long-form `macro_loader.macro_features` materialized view are
populated through 2026-10-27 (schema 11 / feature 6). PR-201,
`pr-201/sunday-gold-copy-cron`, adds the installed Sunday 22:00 Europe/Vienna copy from
`lake/gold/` to `/volume1/Temp/Quotes/macro/`; a live copy and checksum comparison passed.
The installed daily 09:00 Europe/Vienna schedule and 60-calendar-day delta window remain
unchanged. PR-202 centralizes debug logs and cron-log retention; PR-203 adds source-ingestion
telemetry; PR-204 covers core workflows; PR-205 and PR-206 add PostgreSQL sync telemetry;
PR-207 adds safe debug lifecycle logs to Gold storage adapters; PR-208 adds debug telemetry
to lake repositories; PR-209 adds telemetry to PostgreSQL conformance and Gold input
adapters; PR-210 adds elapsed-time telemetry to guarded reconstruction operations; PR-211
records daily cron total elapsed seconds and exit code; PR-212 adds application-level
conformance/reconstruction telemetry. PR-212 merged as
`923d63a67bce1ecfd8144745210059153c414d74`; PR-213 recorded that merge and merged as
`1972971f3640181f0f5cb385e83c7e608cf288d4`. PR-214 adds safe logs to canonical Gold-frame
assembly. PR-214 merged as `182b1c2a06dcfb5a9d93e91aa3a389caf796c6b7`; PR-215 added safe
validation telemetry to the macro feature catalog and merged as
`14fb82e8799fdfe1cbefc0b0a5fe80724eabdffe`. PR-216 added bounded parallel execution logs and
merged as `ee3db4e9167412317d7c0a759cd61e2240eafe54`. PR-217 added inventory rendering logs
and merged as `9be54b188ac66bcd6f81f70769ec0816181eb838`. PR-218 instruments PostgreSQL delta
planning.
PR-218 instruments PostgreSQL delta planning and merged as
`ad41794f8ce42eda9d67f66323aada219aeaaa7c`; PR-219 added safe debug logs to the
provider-neutral source planner and merged as
`1714c293ace2e84262e63d2edf8b8cc06da7be40`. PR-220 adds safe lifecycle telemetry to cron
log-retention and protected-config export helpers.
Per the user's instruction, GitHub Actions must
remain disabled and must not be triggered, re-enabled, rerun, or newly added; perform checks
locally only.
The guarded acceptance runners document production-like verification but must only be
executed with explicit deployment authorization.

## Source of truth

Before changing code, read in this order:

1. `BACKLOG.md` — delivery scope, dependencies, Git metadata, requirements and acceptance criteria.
2. `ARCHITECTURE.md` — durable engineering contract.
3. `README.md` — operator/consumer contract.
4. This file — implementation behavior for agents.

If documents conflict, stop and fix the documentation contract in the current PR instead of guessing.

## One agent task = one backlog PR

- Implement exactly one `PR-XX` from `BACKLOG.md`.
- Do not implement later PR scope “because it is easy”.
- Do not implement a PR until every `Depends on` PR is merged to `main`.
- Use exactly the `Git branch` defined by the backlog entry.
- Every non-generated commit must use Conventional Commit format `type(pr-XX): description` with the same PR identifier as the branch.
- The branch name and every non-generated commit message must contain the same PR identifier as the backlog entry.
- Before every commit, verify the active branch is exactly the backlog branch and the commit subject matches `^(feat|fix|docs|test|refactor|perf|build|ci|chore)\(pr-XX\): .+` for that PR.
- Every backlog PR entry must contain `Git branch`, `Git status`, `Commit`, and `Design patterns` metadata.
- Keep `Git status` in the backlog accurate while work is active. With local-only checks,
  use `pushed-local-checks-passed` only after pushing a branch whose local gate passed;
  use `merged` only after GitHub reports the PR merged.
- With GitHub Actions and remote quality gates retired, stop after the PR branch is pushed
  and the local `make quality-gate` passes, with the PR waiting for or having completed
  merge. Do not start the next PR in the same task unless orchestration explicitly assigns
  another dependency-independent PR to a different agent lane.

## Architecture rules

The repository uses hexagonal architecture (Ports and Adapters) with composition and dependency injection.

Mandatory patterns where applicable:

- **Adapter** — provider-specific HTTP/parsing and filesystem implementations sit behind application-facing ports.
- **Strategy** — provider selection, retry behavior, strict update/explicit reconciliation behavior, and consumer resolution policies are explicit strategies/policies rather than scattered conditionals.
- **Registry/Factory** — canonical provider/series resolution is registry-driven; orchestration must not contain provider `if/elif` ladders.
- **Repository** — Bronze/Silver/state/run-manifest/Gold-build/Gold-catalog persistence is accessed through narrow repositories/ports.
- **Unit of Work** — a Bronze series execution and Gold publication each have an explicit durability/commit boundary.
- **State Machine** — Gold publication state is `building -> complete|failed`; only the catalog owns publication status.
- **Materialized View** — root Gold `manifest.json` and `feature_profile.png` are rebuildable views of authoritative `manifest.parquet`; they never choose the current build.
- **Mark-and-Sweep** — retention first makes a non-current build unselectable in the catalog, then garbage-collects its physical bundle.
- **Command** — CLI subcommands translate arguments/config to application use cases and contain no provider or persistence business logic.
- **Dependency Injection** — clocks, sleepers, HTTP transports, repositories, provider registries, source-control identity, and policies are supplied from the composition root rather than created inside use cases.

Every PR must state the intended `Design patterns` in `BACKLOG.md`. During implementation, apply those patterns when they materially reduce coupling, clarify lifecycle/ownership, improve substitution in tests, or protect transaction boundaries. Do **not** introduce a pattern only to satisfy a label: if the PR needs no pattern beyond the repository's architectural baseline, record `Architectural baseline only` and keep the implementation simple.

Prefer `typing.Protocol`, immutable dataclasses/Pydantic models, pure functions, and constructor injection over deep inheritance. Do not add an abstract base class merely to share a few lines of code. Prefer composition over inheritance and keep interfaces narrow according to Interface Segregation.

## Dependency direction

Allowed direction:

```text
api/scripts -> application/contracts <- ingestion adapters
```

`application/` must not import `httpx`, provider modules, filesystem-specific implementations, matplotlib, or CLI parsing.

Provider adapters may depend on shared HTTP and parsing ports. They must not mutate ingestion state, run manifests, or portfolio/model state themselves.

## Strict delta-update rules

For an existing series, a normal `update` or `run-daily` must derive the request from the **newest durable Bronze observation**, never the historical minimum:

```text
latest_stored_date = max(Bronze.observation_date)
request_start      = latest_stored_date - overlap_days
request_end        = injected_today
```

Default overlap is seven calendar days.

Mandatory implementation rules:

- Empty Bronze may bootstrap maximum history.
- Existing Bronze under normal `update`/`run-daily` must use the max-date-derived delta window.
- Never use `min(Bronze.observation_date)` as the normal request start.
- Never auto-switch normal update to source full-history `reconcile` because of elapsed time or state age.
- `reconcile` is an explicit separate operator command only.
- `date_range` providers must receive the exact bounded request.
- `full_file` providers may download a complete remote object only because the source lacks range capability; they must filter parsed observations to the exact logical delta window before normal diff/persistence.
- Out-of-window rows from a bounded provider are contract failures; out-of-window rows from a full-file provider are ignored during normal delta diff.
- A shorter source response never deletes retained history.

Every implementation touching planning, providers, Bronze orchestration, or `run-daily` must include the canonical regression case:

```text
min stored       = 2000-01-03
latest stored    = 2026-08-18
today            = 2026-08-19
overlap          = 7
expected request = 2026-08-11 .. 2026-08-19
```

A normal request from `2000-01-03` is a test failure.

## Data rules

- Production dataframe operations are Polars-first. Do not introduce pandas into production code.
- Bronze and Silver never fabricate missing market observations.
- An upstream response becoming shorter is not evidence that retained history should be deleted.
- Explicit source reconciliation may revise equal-key historical observations; deletion semantics require an explicit contract and must never be inferred from omission.
- Gold uses only `timestamp_m1: Datetime(time_unit="us", time_zone="UTC")` as temporal key.
- Gold UTC midnight is observation-day identity, not information-availability time.
- Gold features are causal. No future observations, centered windows, forward fill, backward fill, or implicit as-of carry.
- Final Gold features contain nulls, not NaN or infinity.

## Gold publication rules

- Version build directories are creation-only and immutable.
- Build `manifest.json` describes the immutable artifact bundle; it does not own catalog publication status.
- `lake/gold/dataset=macro_features_daily/manifest.parquet` is the sole publication authority.
- Consumer selection is catalog-driven, never filesystem-recency-driven.
- Root JSON/PNG are recoverable materialized views and must be reconciled from the catalog after interruption.
- Retention must never create a catalog-selectable row whose physical bundle has already been partly deleted.

## Local quality gates; no GitHub Actions

Run quality checks locally with `make quality-gate`; do not use GitHub Actions to run,
rerun, or report tests. The local quality-gate suite comprises:

```text
lint
type
unit
integration
coverage
```

`lint`, `type`, unit, and offline `integration` execute locally; `make test` runs unit and
integration tests. `coverage` combines their reports and reads the production-code line
coverage threshold (`85.0%`) from `pyproject.toml`. The pre-commit hook runs
`make quality-gate`.
`network` tests are never part of the local quality gate. Do not add workflow files or
dispatch, rerun, or re-enable any GitHub Actions workflow. Local results are not remote
GitHub status checks; do not add GitHub Actions or remote quality gates.

Do not weaken tests, exclude production files from coverage merely to reach the configured
85% threshold, add blanket `# type: ignore`, or suppress lint rules without a narrowly
documented reason.

## Testing style

- Prefer deterministic fixtures and injected clocks/sleepers/clients.
- Provider unit tests must not call the network.
- Test failures and recovery paths, not only happy paths.
- For source ingestion, assert exact request bounds as well as resulting rows/partitions.
- For persistence, assert logical state and physical invariants where relevant (hash/mtime/no orphan temp/current pointer).
- For Gold features, use hand-calculable fixtures and truncation/causality regression tests.
- For architecture-sensitive PRs, add substitution or dependency-direction tests that demonstrate the chosen pattern rather than merely naming it in documentation.

## Documentation sidecars

Update `README.md` and/or `ARCHITECTURE.md` in the same PR whenever the implemented contract they describe changes. Do not document functionality as available before its implementing PR is merged.
