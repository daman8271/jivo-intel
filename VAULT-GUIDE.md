# VAULT-GUIDE — how to read & navigate the vault

The vault (`vault/`) is the part you actually read to gather data and make sense of it. This is the
manual. Read **[`ARCHITECTURE.md`](ARCHITECTURE.md)** first for where it comes from, and
**[`DATA-MODEL.md`](DATA-MODEL.md)** alongside this for what the numbers mean.

---

## The design in one sentence

> **Every raw row is embedded exactly once, as CSV, under its single canonical entity note; every
> other dimension that row relates to is a `[[wikilink]]` in the body — and the SKU note is the
> convergence point where all dimensions meet.**

That means you never look for the same fact in two places, and you navigate the data the way you'd
navigate Wikipedia: open the entity you care about, then follow its links. The graph edges are
**body wikilinks only** (frontmatter holds attributes, not the graph).

**Start at `vault/index.md`** — the home Map of Content. It links the per-type indexes
(`skus-index`, `platforms-index`, `taxonomy-index`, `vendors-index`, `pos-index`, `locations-index`,
`months-index`, `dashboards-index`), each of which lists every note of that type.

---

## The note types

`35,285` notes (current `vault/`), all with a globally-unique, type-prefixed basename:

| Prefix | Folder | Count | What it is |
|---|---|---:|---|
| `sku-<sap_code>` | `vault/skus/` | 168 | **The convergence node** — one physical SKU; links every dimension it touches |
| `pf-<slug>` | `vault/platforms/` | 11 | Platform hub (Amazon, the q-comm trio, the grocery long-tail) + its archetype |
| `tier-` `brand-` `cat-` `subcat-` | `vault/taxonomy/` | 66 | The product taxonomy (tiers · brands · categories · sub-categories) |
| `vendor-<NAME>` | `vault/vendors/` | 19 | The distributors that fulfil POs (JIVO sells through distributors, not direct) |
| `po-<number>` | `vault/pos/` | 10,064 | One purchase order; links its vendor, SKUs, platform, month |
| `fc-<code>` | `vault/locations/` | 19 | Amazon fulfilment centres (e-com geography; q-comm uses dark-stores, kept separate) |
| `<YYYY-MM>` | `vault/months/` | 31 | A connective month rollup; links what was active that month |
| `dash-<endpoint_key>` | `vault/dashboards/` | 3,012 | **The app's OWN computed aggregate**, captured verbatim (NOT raw rows, NOT our summary) |
| `<entity>.<table>.<chunk>` | `vault/data/` | 21,885 | **Raw rows** for one entity×table, embedded once as CSV (large tables split into chunks) |
| `index` / `*-index` | `vault/` | 10 | Home MOC + the per-type Maps of Content |

> **The most important distinction:** a **`data/` note holds raw source rows (L1)**; a **`dash-`
> note holds the app's own pre-computed number (L3)**. When the two disagree, the
> [DATA-MODEL](DATA-MODEL.md) tells you which to trust (usually the `_master_view`, never raw
> `shipped_revenue`).

---

## Anatomy of the key notes

### `sku-<code>` — the hub (start here for almost any question)

Frontmatter carries the SKU's attributes; the body carries the graph + small inline current state.

```yaml
type: sku-hub
sku_sap_code: FG0000004
item: CANOLA 5L
item_head: PREMIUM          # the tier — drives every rollup
per_unit_value: 5.0         # LITRES per unit — the house unit
brand: JIVO                 # JIVO or SANO
category: CANOLA
sub_category: CANOLA
platforms: [amazon, bigbasket, blinkit, flipkart, jiomart, zepto, zomato]
vendors:  [ANTIZE FOODS PVT LTD, … ]
pos:      [1177810211422, … ]
```

Body: `[[tier-…]] [[brand-…]] [[cat-…]] [[subcat-…]]`, every `[[pf-<slug>]]` it sells on, every
`[[vendor-…]]` shipping it, every `[[po-…]]`, the `[[<month>]]`s it was active, and links to its
raw `data/` chunks. From one SKU note you can reach its entire footprint in the business.

### `<entity>.<table>.<chunk>` — the raw data (in `vault/data/`)

The canonical home of raw rows. Header tells you the shape; the body is verbatim CSV:

```yaml
type: data
entity: pf-amazon        # whose canonical data this is
table: amazon_ads
rows: 1072
chunk: 1/46              # large tables are split — follow all chunks for the full set
source: raw-table
```

The CSV is the **full column set**, plus three audit columns the pipeline adds:
`__key` (the SSOT identity), `__first_seen`, `__last_seen` (snapshot dates). Very large tables are
written as gzipped attachments referenced by path + row count + sha256 rather than inline CSV — so a
note staying small does **not** mean the data is small; check `rows`.

### `dash-<endpoint_key>` — the app's own aggregate (in `vault/dashboards/`)

```yaml
type: app-dashboard
endpoint_key: ads__amazon       # <endpoint>__<slug>__<YYYY-MM> when scoped
platform: amazon
```

Body is a `json` block: the app's computed dashboard, captured verbatim. Use these to see **what
JIVO's own cockpit shows** (DOH/DRR/fill/projection/targets) rather than recomputing from raw rows.
`endpoint_key` encodes scope: plain (`ads__amazon`), or month-scoped (`category-breakdown__2023-05`).

### The connective notes

- **`pf-<slug>`** — lists the SKUs sold there and links every raw `data/` table that platform owns.
  Frontmatter notes its **archetype** (A / B / C — see [DATA-MODEL](DATA-MODEL.md)) and `skus_listed`.
- **`po-<number>`** — "Connects to" its vendor, SKUs, platform(s), month(s); then its own rows.
- **`<YYYY-MM>`** — purely connective: links the platforms / SKUs / POs active that month
  (`skus_active`, `pos` in frontmatter). Raw rows live under the SKU/PO, not here.
- **`vendor-`, `fc-`, `tier-/brand-/cat-/subcat-`** — entity pages that back-link everything tagged
  to them. Note vendor names are dirty in the source (`ANTIZE FOODS PVT LTD` vs `…PRIVATE LIMITED`
  vs `…DELHI` appear as distinct notes) — see the join landmines in [DATA-MODEL](DATA-MODEL.md).

---

## Navigation recipes

| You want to know… | Path through the vault |
|---|---|
| Everything about one product | `sku-<code>` → follow its links (platforms, vendors, POs, taxonomy, months) |
| What sells on a platform + its raw feeds | `pf-<slug>` → "SKUs listed" + "Complete data" table links |
| What JIVO's cockpit reported for a period | `dashboards-index` → `dash-<metric>__<YYYY-MM>` (the app's own number) |
| The raw rows behind a number | the entity's `data/` chunk(s) — follow **every** `chunk N/M` |
| Who supplies a SKU / a PO's fulfilment | `sku-…`/`po-…` → `[[vendor-…]]` |
| All POs / SKUs active in a month | `<YYYY-MM>` month note |
| The premium vs commodity split | `tier-PREMIUM` / `tier-COMMODITY` / `tier-OTHER` |

---

## How to open it

- **Obsidian** — open `vault/` as a vault; the graph view and backlinks make the connections visible.
- **Plain reading / grep** — it's all Markdown + CSV; `rg`/`grep` over `vault/` works directly, e.g.
  `rg -l "item_head: PREMIUM" vault/skus` or read any `.md` straight.
- **Re-query the SSOT** — for full raw data, `store/versioned/tables/<table>.state.jsonl` is the
  current-state index; the vault `data/` notes are the same rows made human-navigable.

---

## Caveats when reading

- **Date-stamp everything.** These notes are a checked-in snapshot of the live app. The latest full
  vault is from **2026-07-01**; use git history and `state/pull-ledger.jsonl` for exact run status.
- **Chunks:** a `data/` note may be `chunk 1/46` — don't read one chunk and assume it's the whole table.
- **Raw ≠ enriched:** prefer the app's `_master_view` tables / `dash-` aggregates over raw rows for
  revenue, margin and category — raw fields are frequently zeroed or dirty. See [DATA-MODEL](DATA-MODEL.md).
- **Empty is real:** some platforms (CityMall, Zomato, DealShare) are intentionally sparse on
  sell-out while live on POs — emptiness is recorded, not missing.
