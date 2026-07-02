# JIVO-INTEL — PINNED BUILD CONTRACT (all workers build against THIS)

Read this in full before writing any code. Every interface below is FIXED — do not change a
field name or path without posting to the bus first. The whole point is that components built in
parallel must fit together exactly. Reference data: `registry/{tables,endpoints,taxonomy}.json`
(already generated), `~/jivo-intel/datamap/00-MASTER-data-model.md` (the data model + landmines),
`~/jivo-intel/recon/2026-06-26/` (recon corpus). Reuse engine: `/opt/ecom-intel/tools/vault_build.py`,
`/opt/ecom-intel/run_all.sh`.

## 0. Hard rules (never violate)
- **READ-ONLY CLI.** Only `tables|dashboard|platform|master|notifications|account|version` reads.
  NEVER `import`, `sync` (writes), `auth logout/set-token`, `tail`, `--deliver webhook:`, or any
  mutation. Token refresh via `auth login` only (env creds) is allowed in `auth.sh`.
- **NEVER write secrets** (password/JWT) into any file tracked by git. `.env` is 0600 + gitignored;
  `state/token.json` is 0600 + gitignored. The password is only ever read from env at runtime.
- **Fail-closed.** The SSOT is irreplaceable. On any assertion failure: stop, preserve last-good,
  alert — never write partial/overwrite. Bias to "stop + alert".
- **Determinism.** `vault_build.py` is stdlib-only, no network, no LLM, fully rebuildable from the store.
- Python 3 stdlib only (no pip installs). Standard CLI form on every call:
  `jivo-ecom-pp-cli <cmd> --data-source live --json 2>/dev/null` (add `--rate-limit 8 --timeout 60s`).
  Assert `meta.source=="live"` on every response.

## 1. Directory layout (FIXED)
```
~/jivo-intel/
  bin/  auth.sh pull.py ssot.py vault_build.py verify.py archive_weekly.py run_daily.sh run_weekly.sh doctor.sh
  registry/  tables.json endpoints.json taxonomy.json        # DONE — read, don't regenerate
  store/
    raw/<YYYY-MM-DD>/<table>.jsonl                            # transient daily capture (gitignored)
    raw/<YYYY-MM-DD>/dashboards/<endpoint_key>.json          # transient dashboard docs
    versioned/
      tables/<table>.changelog.jsonl                          # APPEND-ONLY SSOT (committed)
      tables/<table>.state.jsonl                              # derived current index (atomic rewrite)
      dashboards/<endpoint_key>.changelog.jsonl               # doc-level Type-2 (committed)
      master/<products|fcs>.changelog.jsonl
      notifications.changelog.jsonl
  archive/<YYYY-Www>/<table>.full.jsonl.gz + dashboards.tar.gz + MANIFEST.json   # weekly cold
  vault/  skus/ platforms/ taxonomy/ locations/ locations/stores/ vendors/ months/ snapshots/ analysis/ attachments/  + *-index.md + index.md
  workflows/weekly/<YYYY-Www>/
  state/  token.json(0600) pull-ledger.jsonl
  logs/   cron.log pull-<date>.log *.lock
```
`endpoint_key` = `<endpoint>` or `<endpoint>__<slug>` or `<endpoint>__<slug>__<YYYY-MM>` (filesystem-safe; replace `/` with `-`).

## 2. store/raw format  (pull.py → ssot.py interface)  [FIXED]
- One file per table: `store/raw/<date>/<table>.jsonl` — **one JSON row object per line** (the raw row
  exactly as the API returned it under `.results.data[]`; for master use `.results.results[]`).
- Empty tables → file exists with 0 lines (emptiness is recorded).
- Dashboard/master/notifications docs → `store/raw/<date>/dashboards/<endpoint_key>.json` (the whole `.results`).
- A sidecar `store/raw/<date>/_pull-manifest.json`: `{table: {count_live, rows_written, ok:bool}, ...,
  "dashboards":{endpoint_key:{ok,bytes}}, "started","finished","all_ok":bool}`.

## 3. SSOT changelog + state schema  (ssot.py → vault_build.py interface)  [FIXED]
`store/versioned/tables/<table>.changelog.jsonl` — append-only, one event per line:
```json
{"seq":<int>,"pull_id":"<YYYY-MM-DD>","ts":"<ISO+05:30>","event":"insert|update|delete",
 "key":"<table>:<identity>","hash":"sha256:<hex>","prev_hash":"sha256:<hex>|null",
 "first_seen":"<YYYY-MM-DD>","row":{...complete row, or last-known for delete...}}
```
`store/versioned/tables/<table>.state.jsonl` — current index, atomic rewrite, one line per present key:
```json
{"key":"...","hash":"sha256:...","first_seen":"<date>","last_seen":"<date>","version":<int>}
```
**Key derivation** (per `registry/tables.json` → `key.strategy`):
- `id`: `key = "<table>:" + str(row["id"])`.
- `natural`: `key = "<table>:" + sha256(canon(subset of row over key.cols))`.
- `content-hash`: `key = "<table>:" + hash` (the content hash itself → every distinct content is its own key).
**Canonicalization:** `canon(obj)=json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=False)`;
`hash="sha256:"+sha256(canon(row).encode()).hexdigest()`. Null preserved; floats via `repr`.
**Upsert algorithm** (per table, per daily pull): see datamap design — insert if key unseen; update
(with prev_hash) if key seen but hash changed; unchanged → bump last_seen only; key in old-state but
absent today → append `delete` tombstone. A `(key,hash)` is written to the changelog **at most once
ever**. `last_seen`/`version` live ONLY in state.jsonl (keeps changelog append-only).
**Reconcile (fail-closed):** `inserts+updates+unchanged == count_live == len(state)`; else raise.
Dashboards/master/notifications: doc-level Type-2 — hash the whole doc; append only when hash changes.

## 4. pull.py contract  [Phase F1 — owner: W1]
Driven by `registry/`. Steps: (0) call `bin/auth.sh`. (1) `tables counts` → assert returned set ==
41 registry tables (new table → fail loud). (2) For each table: `count`=tables count; pages=ceil(count/200);
loop `--page N --page-size 200` (also stop on short page) writing rows to raw jsonl; assert
`rows_written==count` (re-read count at end to catch growth; re-pull tail if grown). Whales first
(swiggySec, all_platform_inventory). (3) master products(803)+fcs(19) full (`.results.results[]`).
(4) notifications full. (5) dashboards: plain ones once; year_month ones over years
`registry.endpoints.years_to_probe` (2023-2026) × months 1-12 (skip > latest-month) via `--year --month`;
per-platform dashboards over live slugs. (6) platform leaves: only `live` combos from
`endpoints.platform_leaves`; gated recorded as n/a. Write `_pull-manifest.json`. `--mode full|daily`
(daily = current+2 trailing months for year_month dashboards; full = all years). Atomic temp+rename;
5× exponential backoff; assert meta.source==live everywhere. Exit nonzero if any table not OK.

## 5. vault_build.py contract  [Phase F3 — owner: W3]  — THE "EVERYTHING CONNECTS" LAYER
Port `/opt/ecom-intel/tools/vault_build.py` (reuse `frontmatter,safe_name,link,csv_block,write_file,
prune_orphans,write_obsidian,check_unique,check_links` + nonzero-exit-on-violation contract). Reads
`store/versioned/*` only. Builds these notes (basenames are **type-prefixed + globally unique**, per
`taxonomy.basename_prefixes`; graph edges are **body `[[wikilinks]]` ONLY**):
- **SKU hub `sku-<sku_sap_code>`** = THE convergence node. Frontmatter: type, sku_sap_code, item,
  item_head, per_unit_value, brand, category, sub_category, listings (platform→format_sku_code), platforms,
  first_seen,last_seen. Body links: `[[tier-...]] [[brand-...]] [[cat-...]] [[subcat-...]]`, every
  `[[pf-<slug>]]` it sells on, every `[[vendor-...]]` shipping it, latest `[[<month>]]`. Inline: current
  price/SOH/DOH per platform (small). Attachment (gz, >2000 rows): full sell-out + PO history.
- **pf-<slug>** platform hub (archetype A/B/C from taxonomy, capabilities). **cat-/subcat-** category.
  **brand-JIVO/SANO**. **tier-PREMIUM/COMMODITY/OTHER** (premium-mix anchor). **fc-<code>** (19).
  **city-<CITY>** + **store-<STORE_ID>** (store notes threshold-gated; bulk → city gz attachment).
  **vendor-<VENDOR>** (from master_po vendor_new; fill-rate signal). **<YYYY-MM>** month rollup
  (sell_in_ltrs, sell_out_ltrs, premium_mix_pct, fill_rate_pct, inventory_ltrs; prev/next linked).
  **snapshots/<date>/{availability,po-fill-rate,premium-mix-pace,expiry}.md** daily. MOC indexes + `index.md`.
- Taxonomy join: item_head/per_unit_value from master products; brand/category/sub_category from
  **master_po + notifications** (NOT master products — landmine). Big tables → fenced ```csv``` or gz
  attachment referenced by path+rows+sha256 (NEVER giant rendered markdown tables).
- **Integrity gates (hard, nonzero exit):** `check_unique()` (no duplicate basenames) + `check_links()`
  (every body wikilink resolves to a real note). `vault/analysis/` + `vault/snapshots/` pruning-exempt
  but still link-checked.

## 6. verify.py contract  [Phase F4 — owner: W4]
`--stage ingest`: re-fetch tables counts; assert SSOT present-count (len state.jsonl) == live count per
table; registry==catalog. `--stage sample --n N`: random (sku,platform,month) cells re-derived 3 ways
(markdown/CSV vs state+changelog vs fresh live CLI call) asserted equal within float tol. `--stage replay`:
present-state from changelog == state.jsonl == latest weekly archive (3-way). Fail-closed; log to
pull-ledger; Telegram alert on fail. Also projection invariants (month sell_in_ltrs == Σ master_po
total_order_liters; premium-mix ≈ 52%).

## 7. cron + archive  [Phase F4 — owner: W4]
Installed at **05:00 IST**. `run_daily.sh` (mirror run_all.sh): flock `.daily.lock` → auth.sh → pull.py --mode daily → ssot.py →
verify.py --stage ingest → vault_build.py → if green: git add store vault archive registry; commit;
pull --rebase --autostash; push (1 retry) → else preserve last-good + Telegram 🛑 → verify.py --stage
sample. Sundays: archive_weekly.py then prune store/raw older than archived week. IST cron line.
`archive_weekly.py`: per table emit present-state → `archive/<week>/<table>.full.jsonl.gz`; dashboards.tar.gz;
MANIFEST.json (rows+sha256). `doctor.sh`: staleness/row-collapse health check + alert (mirror healthcheck.sh).

## 8. Coordination protocol (fleet)
- Post to `bus.md` under your `## W<n>` header when you (a) finish a component, (b) need an interface
  clarified, (c) discover something others must know. Read the bus before starting + when blocked.
- One owner per file. Do NOT edit another worker's files; request via bus. Commit nothing (lead pushes).
- Test your component in isolation against a TINY fixture before declaring done (don't run the full
  1.8GB pull — the lead orchestrates the real backfill). `touch state-done/w<n>.done` when done + verified.
