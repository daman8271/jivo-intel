# ARCHITECTURE — how the JIVO-Intel data bank is laid out

How the data physically flows and where every byte lives. Read **[`README.md`](README.md)** first
for the mental model; read **[`VAULT-GUIDE.md`](VAULT-GUIDE.md)** next for how to read the output.

---

## The one principle

> **The live app keeps no history. Analytics upserts overwrite; only `notifications` persist.**
> So every un-snapshotted day is gone forever. This bank exists to make the app's data **durable,
> versioned, and connected.** It does that in three layers, each strictly downstream of the last,
> each rebuildable from the one before it.

---

## The pipeline

A single read-only run, orchestrated by `bin/run_daily.sh` (flock-guarded), driven entirely by the
declarative `registry/`:

```
registry/{tables,endpoints,taxonomy}.json          ← the contract: what to pull, how to key it
        │
        ▼
bin/auth.sh        refresh the bearer token (env creds → state/token.json, 0600)
        │
        ▼
bin/pull.py        call jivo-ecom-pp-cli for all 41 tables + dashboards + master + notifications
        │            → store/raw/<date>/…           (transient; asserts meta.source == "live")
        ▼
bin/ssot.py        diff today's raw vs current state → append only what changed
        │            → store/versioned/…            (APPEND-ONLY truth; fail-closed reconcile)
        ▼
bin/verify.py      re-derive sampled cells 3 ways (markdown vs state+changelog vs fresh live) — assert equal
        │
        ▼
bin/vault_build.py project the SSOT into linked Markdown  → vault/   (deterministic, stdlib-only)
        │            integrity gates: unique basenames + every wikilink resolves (nonzero exit on fail)
        ▼
   git add store vault archive registry → commit → push             # green only; else preserve last-good + alert
```

Weekly (`bin/archive_weekly.py`): freeze each table's present-state to
`archive/<YYYY-Www>/<table>.full.jsonl.gz` + a `MANIFEST.json` (rows + sha256), then prune old
`store/raw/`. `bin/doctor.sh` is the staleness / row-collapse health check.

Every step is **read-only against the app, deterministic, and fail-closed**: any assertion failure
stops the run and keeps the last good state rather than writing partial data.

---

## The three storage layers

| Layer | Path | Lifetime | Role |
|---|---|---|---|
| **L0 · Raw capture** | `store/raw/<date>/` | transient (gitignored, weekly-pruned) | exactly what the API returned, one JSON row per line per table |
| **L1 · Versioned SSOT** | `store/versioned/` | **permanent, committed** | the irreplaceable truth — every distinct row version, append-only |
| **L2 · Vault projection** | `vault/` | rebuildable from L1 | the linked Obsidian notes you actually read |
| **Cold archive** | `archive/<YYYY-Www>/` | permanent, committed | weekly gzipped full snapshots + manifests |

**Direction of trust:** L2 is a pure function of L1; L1 is the source of truth. Never edit the vault
by hand — change the builder and rebuild. The vault can always be regenerated; the SSOT cannot.

### L1 — the SSOT schema (the part that must never break)

Per table, two files under `store/versioned/tables/`:

- **`<table>.changelog.jsonl`** — append-only, one event per line
  (`insert` / `update` / `delete`), each carrying the full row, a `sha256` content hash, the
  previous hash, and `first_seen`. A given `(key, hash)` is written **at most once, ever**.
- **`<table>.state.jsonl`** — the derived "what's present right now" index (atomic rewrite), one
  line per live key with `first_seen` / `last_seen` / `version`.

Dashboards, `master` (products + fcs) and `notifications` are versioned the same way at the
**document** level (hash the whole doc, append only when it changes) under
`store/versioned/{dashboards,master}/` and `store/versioned/notifications.changelog.jsonl`.

Keys are derived per `registry/tables.json` (`id` / `natural` / `content-hash` strategy);
canonicalisation is `json.dumps(sort_keys, compact)` → `sha256`. Full contract: **`SPEC.md` §3**.

---

## Directory map (annotated)

```
jivo-intel/
├── bin/                      the pipeline (Python 3 stdlib only, no pip)
│   ├── auth.sh                 token refresh (env creds only — never stored in git)
│   ├── pull.py                 L0 capture — registry-driven CLI calls
│   ├── ssot.py                 L0 → L1 versioning (append-only, fail-closed reconcile)
│   ├── verify.py               3-way re-derivation checks (ingest / sample / replay stages)
│   ├── vault_build.py          L1 → L2 — THE "everything connects" layer
│   ├── archive_weekly.py       weekly cold gz + manifest
│   ├── run_daily.sh            the orchestrator (flock → auth → pull → ssot → verify → build → commit)
│   └── doctor.sh               staleness / row-collapse health check
│
├── registry/                 the declarative contract that drives the pull (DONE — read, don't regen)
│   ├── tables.json             41 tables + per-table key strategy
│   ├── endpoints.json          dashboard endpoints, years to probe, platform leaves
│   └── taxonomy.json           tier/brand/category basename prefixes
│
├── store/
│   ├── raw/<date>/             L0  transient daily capture            (gitignored)
│   │   └── dashboards/<key>.json
│   └── versioned/              L1  the SSOT — committed truth
│       ├── tables/<table>.changelog.jsonl  + .state.jsonl
│       ├── dashboards/<key>.changelog.jsonl
│       ├── master/<products|fcs>.changelog.jsonl
│       └── notifications.changelog.jsonl
│
├── archive/<YYYY-Www>/       weekly cold snapshots (.full.jsonl.gz, dashboards.tar.gz, MANIFEST.json)
│
├── vault/                    L2  the 35,285-note Obsidian vault  ← see VAULT-GUIDE.md
│   ├── index.md  +  *-index.md          home + per-type Maps of Content
│   ├── skus/ platforms/ taxonomy/ vendors/ pos/ locations/ months/ dashboards/ data/
│   └── SESSION-MEMORY.md                 build-session handoff (not a generated note)
│
├── datamap/                  the master data model + W1–W4 per-domain deep-dives
├── docs/
│   ├── app-model/              how the app works (4 MD + HTML + target history)
│   └── sku-bridge/             join key from these SKUs → the competitor-price scraper
├── recon/<date>/             the read-only recon corpus the model was built from
├── state/                    token.json (0600, gitignored) · pull-ledger.jsonl
├── workflows/weekly/ · logs/  weekly rollups · run + lock logs
├── SPEC.md                   the pinned build contract (every interface above, fixed)
└── *-fixture/                store/versioned-fixture/ + vault-fixture/ — TINY test fixtures only
```

> **Fixtures warning:** `vault-fixture/` and `store/versioned-fixture/` are miniature test inputs
> used while building the pipeline in parallel. They are **not real data** — never read them for
> analysis. The real data is `store/versioned/` and `vault/`.

---

## Determinism & integrity gates

`vault_build.py` is **stdlib-only, no network, no model** — the same SSOT always yields a
byte-identical vault. Two hard gates run on every build and **exit nonzero** (failing the run) if
violated:

- **`check_unique()`** — no two notes share a basename (basenames are globally unique, type-prefixed).
- **`check_links()`** — every `[[wikilink]]` in every note body resolves to a real note.

Plus the upstream `verify.py` invariants: per-table present-count equals the live count, and
sampled `(sku, platform, month)` cells re-derived three independent ways agree within float
tolerance. This is what "verified lossless" means.

---

## Rebuilding / refreshing (all read-only)

```bash
# rebuild the vault from the committed SSOT (no network, deterministic)
python3 bin/vault_build.py

# a fresh read-only daily pull → version → verify → rebuild (needs a valid token; see auth.sh)
bin/run_daily.sh

# health check only
bin/doctor.sh
```

The CLI itself is documented in `~/JIVO-ECOM-CLI-SETUP.md`; standard call form is
`jivo-ecom-pp-cli <cmd> --data-source live --json 2>/dev/null` with `meta.source == "live"` asserted.

Installed schedule: `/root/jivo-intel/bin/run_daily.sh` runs daily at **05:00 IST** from crontab. The
script commits and pushes only after the gated pull → SSOT → ingest-verify → vault-build chain is green;
sample verification runs afterward as a diagnostic and records status in `state/pull-ledger.jsonl`.
