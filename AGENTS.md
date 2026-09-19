# AGENTS.md

This repository is implemented through small backlog PRs by parallel coding agents.

Current delivery status (2026-09-19): backlog PR-80 through PR-90 are merged to
`origin/main` as GitHub PRs #81 through #91. The guarded acceptance runners document
production-like verification but must only be executed with explicit deployment
authorization.

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
- Keep `Git status` in the backlog accurate while work is active. Use `pushed-ci-failing` after a pushed branch has a failing required remote gate, `pushed-ci-green` only when all required remote gates are green, and `merged` only after GitHub reports the PR merged.
- Stop after the PR is pushed, required CI is green, auto-merge is enabled, and the PR is waiting for or has completed merge. Do not start the next PR in the same task unless orchestration explicitly assigns another dependency-independent PR to a different agent lane.

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

## Quality gates

Required push/merge checks are:

```text
lint
type
unit
integration
coverage
```

`lint`, `type`, `unit`, and offline `integration` execute in parallel. `coverage` combines unit + integration coverage and requires production-code line coverage `>= 90.0%`. `network` tests are never part of required gates.

Do not weaken tests, exclude production files from coverage merely to reach 90%, add blanket `# type: ignore`, or suppress lint rules without a narrowly documented reason.

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
