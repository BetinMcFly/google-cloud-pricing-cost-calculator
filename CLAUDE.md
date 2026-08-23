# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`gcosts` is an offline CLI that estimates monthly Google Cloud costs. Resources are described in
YAML "usage" files, prices come from a local generated `pricing.yml`, and the output is a
`costs.csv` breakdown. No network calls at calculation time.

## Deployment

This fork is deployed to GCP as a **Cloud Run Job**, with `pricing.yml` regenerated in the
fork's own CI (own Billing API key) and baked into the container image only after the 543
asserts and `t/diffcheck.sh` pass.

- [`deploy/PLAN.md`](deploy/PLAN.md) — architecture and rationale
- [`deploy/SETUP.md`](deploy/SETUP.md) — one-time setup, step by step

## `inventario/` — fork-local, not upstream

Turns a CSV inventory into a cost CSV. It exists because `gcosts` only prices
Compute Engine, Cloud Storage, Cloud Monitoring and network: the root `pricing.yml`
has no BigQuery, Dataplex, Dataflow or Logging keys at all. So `inventario/` runs
two engines and merges them — `gcosts` for what it covers, and unit prices read
from the Cloud Billing Catalog API (`inventario/servicios.csv` maps a resource type
to exactly one SKU) for everything else.

Client inventories all arrive in different shapes, so `inventario/perfiles/*.yml`
declares per client how to read theirs: column mapping, unit conversion, row
filters, value maps, and sizing from vCPU+RAM when there is no machine type. The
sizing picks the cheapest fitting type using the real prices in the root
`pricing.yml`, restricted to general-purpose/compute families.

Python, not Go: it orchestrates `gcloud` and the Billing API rather than extending
the calculator. `bash inventario/test.sh` — 41 checks, no network.

- [`inventario/README.md`](inventario/README.md) — CSV schema, how to add a service

## Repository layout: two independent Go modules

There is no `go.mod` at the repo root. Always `cd` into the right module first.

| Module | Binary | Purpose |
|---|---|---|
| `gcosts/` | `gcosts` | The user-facing calculator CLI |
| `build/` | `skus` | Exports SKUs from the Cloud Billing API into `skus.db` |

`build/` also contains Perl scripts; the pricing pipeline is Perl + Go + SQLite + Bash, while the
calculator itself is pure Go (cobra + pterm + yaml.v3).

## Commands

Build:
```bash
cd gcosts && make native          # ./gcosts for the host platform
cd gcosts && make build           # all: linux/macos/windows x amd64/arm64
cd build  && make native          # ./skus
```

Lint / format (CI runs `golangci-lint` on both modules separately):
```bash
cd gcosts && gofmt -s -w . && golangci-lint run
cd build  && gofmt -s -w . && golangci-lint run
```

Run the test suite. `t/test.sh` greps `costs.csv` in **its own working directory**, so generate the
CSV into `t/` first (this uses the committed root `pricing.yml`):
```bash
cd gcosts && make native && cd ..
cd t && ../gcosts/gcosts calc -d . -p ../pricing.yml --csv costs.csv && bash test.sh
```
`t/gcosts.sh` is the CI variant and expects `../build/pricing.yml`, which only exists after the
pricing pipeline has run — prefer the root `pricing.yml` for local work.

Run a single test case: fixtures in `t/*.yml` are self-contained (each sets its own `region` and
`project`), so point `calc` at one file's directory and grep the CSV directly:
```bash
mkdir -p /tmp/one && cp t/01_europe-west4-c4.yml /tmp/one/
./gcosts/gcosts calc -d /tmp/one -p pricing.yml --csv /tmp/one/costs.csv
grep 'c4-standard-8,' /tmp/one/costs.csv
```
`t/test.sh` itself is a flat bash array of grep patterns (~543 of them) — to add a case, append the
expected `costs.csv` substring to `MY_CHECKS`.

Other checks:
```bash
perl t/test_lssd_instances.pl   # validates build/gcp.yml (needs Test::More, YAML::XS)
cd t && bash diffcheck.sh       # CI guardrail: fails if regenerated pricing.yml deletes > adds
```

## Architecture

### Calculation path (`gcosts calc`)

`main.go` → `cmd.Execute()` → `cmd/usage.go` drives everything:

1. `usage.ReadDir` lists `*.yml` in the target dir, **sorted by filename**.
2. Each file is unmarshalled into `usage.StructUsage` (`gcosts/usage/usage.go`).
3. For each resource kind, a `pricing.Calc*` function looks up `pricing.StructPricing` and appends
   to the package-level slice `pricing.LineItems` (`gcosts/pricing/cost.go`).
4. `pricing.ExportCsv` writes the 10-column CSV: `Project,Region,Resource,Type/Class,Name,Cost,Data,CUD,Discount,File`.

Two things that are easy to get wrong:

- **Defaults are stateful across files.** `region`, `project` and `discount` set in an earlier file
  carry over into later files in the same directory (see the `defaultRegion`/`defaultProject`
  reassignment loop in `cmd/usage.go`). This is intentional and documented in `usage/README.md`.
- **`LineItems` is global mutable state**, not threaded through call sites. Any new resource type
  follows the same pattern: a `Calc*` func in `gcosts/pricing/` that appends a `LineItem`.

`gcosts/pricing/pricing.go` defines `StructPricing`, which mirrors the `pricing.yml` tree exactly
(`compute.instance.<type>.cost.<region>.{hour,hour_spot,month,month_1y,month_3y,month_spot}`).
Changing the YAML shape means changing this struct and `build/pricing.pl` together.

Missing prices are not silently zero: `Month`/`Hour` call `os.Exit(1)` when absent, while the CUD
and Spot variants fall back to the on-demand price with a warning.

### Pricing generation path (`build/`)

```
Cloud Billing API  +  build/mapping.csv
        ↓ build/skus.sh (runs ./skus, then mapping.sql)
   build/skus.db (SQLite)  +  build/gcp.yml
        ↓ perl build/pricing.pl
   build/pricing.yml  →  moved to repo root by CI
```

**Never hand-edit the root `pricing.yml`** — it is regenerated weekly by
`.github/workflows/build-pricing.yml` and committed by a bot. To correct a price or a unit, edit
`build/mapping.csv` / `build/mapping.sql` (SKU → internal ID mapping) or `build/gcp.yml` (machine
type metadata: vCPU, RAM, attached local SSD), then re-run the pipeline.

`tools/` is a separate daily job: it dumps the current GCE API inventory (regions, zones, machine
types, disk types, accelerators) to CSV and opens a GitHub issue when something changes upstream.
Those CSVs are inputs for humans updating `build/gcp.yml`, not for the calculator.

## Conventions

From `CONTRIBUTING.md` and `.editorconfig`:

- **Tabs** for `.go`, `.pl`, `.sh` (width 4); 2 spaces for `.yml`. LF endings, final newline.
- Go: `gofmt -s` and `golangci-lint` must pass.
- New Perl scripts and CSV files: `[\w\d_]+\.(pl|csv)` naming.
- Adding a Perl module means updating `build/cpanfile`, the docs and the GitHub Actions install step.
- Update the relevant `README.md` (`usage/`, `build/`, `t/`, `tools/` each have their own).

## Test expectations

Expected values in `t/test.sh` are cross-checked against the official Google Cloud Pricing
Calculator; `t/README.md` records the reference figures and the known deltas (e.g. large M2
instances calculate slightly high). When a price legitimately changes upstream, update the expected
substring and note the date and reason in a trailing comment, matching the existing style.
