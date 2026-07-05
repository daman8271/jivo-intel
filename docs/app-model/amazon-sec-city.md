# amazon_sec_city — Amazon secondary sell-out, city-cut (NEW 2026-07-03)

> **Scope:** the warehouse table `amazon_sec_city`, first seen live on **2026-07-03** (it broke the
> fail-closed pull gate and was adjudicated into `registry/tables.json` on 2026-07-05). All numbers
> below are profiled from the CLI (`jivo-ecom-pp-cli tables ...`) and today's raw capture
> `store/raw/2026-07-05/amazon_sec_city.jsonl`, anchored to **June 2026** (the only window loaded).
> Registry identity: `layer L1, key.strategy id, 12 cols, rows_recon 17,671`.

---

## 1. Purpose — what this table IS

**amazon_sec_city = Amazon SECONDARY sales (sell-out from Amazon's FCs to end consumers), re-cut
by ship-to CITY at ASIN grain.** It is the geographic drill-down that the existing Amazon
secondary family lacked: `amazon_sec_daily` slices the same feed by *day*, `amazon_sec_range` by
*date window* — this table slices it by *city*, over one month-long window per load.

In the app's value-chain model (see `secondary.md`) this sits at the very last link:

```
Wellness Billing → JM Primary → Primary (ship TO Amazon) → Secondary (Amazon sells TO consumers)
                                                              └─ amazon_sec_city = WHERE those
                                                                 consumers are (city × ASIN)
```

It is the Amazon analogue of what `swiggySec`/`blinkitSec`/`zeptoSec` already provide natively
(date × city × SKU), and the table-level counterpart of the pooled `state-sales` dashboard —
except at city grain, ASIN grain, and (so far) monthly windows.

## 2. Schema (12 columns) vs the sibling tables

| Column | Meaning |
|---|---|
| `id` | PK (June load = contiguous block 22,600 → 40,270) |
| `business` | selling entity: `JIVO WELLNESS PVT. LTD` or `Jivo Mart Private Limited` |
| `city` | **raw ship-to city free-text** (see caveats — pincodes/addresses leak in) |
| `asin` | Amazon ASIN (58 distinct in the June load) |
| `shipped_revenue`, `shipped_cogs`, `shipped_units` | shipped (sell-out) metrics for the window |
| `from_date`, `to_date` | the aggregation window (June load: `2026-06-01` → `2026-06-30`) |
| `created_at` | load timestamp (whole table loaded 2026-07-03 12:04:46–51, one ~5 s bulk insert) |
| `month_day`, `year` | window labels (`june-30`, `2026`) — same convention as target sheets |

Column overlap with the family:

| Column | sec_city | sec_daily | sec_range | range_margins |
|---|---|---|---|---|
| business | ✅ | ✅ | ✅ | — |
| asin | ✅ | ✅ | ✅ | ✅ |
| **city** | **✅ (unique to this table)** | — | — | — |
| product_title / brand | — | ✅ | ✅ | — |
| ordered_revenue / ordered_units | — | ✅ | ✅ | — |
| shipped_revenue / cogs / units | ✅ | ✅ | ✅ | — |
| customer_returns | — | ✅ | ✅ | — |
| report_date | — | ✅ | — | — |
| from_date / to_date | ✅ | — | ✅ | — |
| month_day / year | ✅ | — | — | — |
| margin_category / margin_pct | — | — | — | ✅ |

So it is a **strict city-cut of the same shipped-metrics feed** — it drops the ordered-side
columns (`ordered_revenue`, `ordered_units`, `customer_returns`, `product_title`, `brand`) and
adds `city`. There is (as of 2026-07-05) **no `amazon_sec_city_master_view`** — no litres,
`item_head`, or margin enrichment layer yet, and no dashboard in the capture consumes it
(only `table-columns__amazon_sec_city` exists under `dashboards/`).

## 3. Profile (June 2026 load, verified 2026-07-05)

- **17,671 rows**, all in a single window `2026-06-01 → 2026-06-30` (`month_day=june-30`,
  `year=2026`). One month per load so far; expect a new block per month (or a re-load — watch
  whether July replaces or appends).
- **2 businesses:** JIVO WELLNESS PVT. LTD (9,503 rows / 71,547 units / ₹133,349 shipped_revenue)
  and Jivo Mart Private Limited (8,168 rows / 75,135 units / ₹80,501).
- **5,608 distinct `city` strings** (⚠️ the CLI `tables distinct` endpoint caps at 5,000 values —
  the true count comes from the raw capture), **58 distinct ASINs**.
- Totals: **146,682 shipped_units, ₹213,850.26 shipped_revenue, ₹184,153.35 shipped_cogs**.
- **Top cities by shipped_units** (the trustworthy metric — see caveat 1):
  1. BENGALURU 13,542 · 2. NEW DELHI 13,195 · 3. HYDERABAD 8,470 · 4. MUMBAI 8,158 ·
  5. GURUGRAM 4,844 · 6. PUNE 4,824 · 7. NOIDA 3,492 · 8. GHAZIABAD 3,339 · 9. KOLKATA 2,868 ·
  10. CHENNAI 2,303.
- **Top cities by shipped_revenue** (misleading — see caveat 1): NEW DELHI ₹106,349 ·
  MUMBAI ₹97,426 · DELHI ₹4,701 · everything else ≤ ₹1k.

## 4. Reconciliation — it IS the same feed (exact match)

June-2026 totals across the family, computed from the 2026-07-05 raw capture:

| Table | June selector | shipped_revenue | shipped_units |
|---|---|---:|---:|
| `amazon_sec_city` | all rows (only window) | **₹213,850.26** | **146,682** |
| `amazon_sec_daily` | `report_date` 06-01→06-30 (1,398 rows) | **₹213,850.26** | **146,682** |
| `amazon_sec_range` | window `06-01→06-30` exactly (64 rows) | **₹213,850.26** | **146,682** |

**Exact three-way match to the paisa.** `amazon_sec_city` is a pure city-dimension re-cut of the
identical June secondary feed — not a new data source. (For `amazon_sec_range`, only the single
full-month window row-set reconciles; summing all `from_date=2026-06-01` windows gives
₹3,483,802.58 / 1,866,362 units because of the overlapping-MTD-snapshot anomaly documented in
`secondary.md` §6.10.)

## 5. Anomalies / caveats

1. **`shipped_revenue` is ~99.75 % zeros — do NOT rank cities by revenue.** Only **44 / 17,671**
   rows have revenue > 0, and they are confined to NEW DELHI (17 rows, ₹106,349), MUMBAI (14,
   ₹97,426), DELHI (4, ₹4,701) and a handful of Delhi/Mumbai sub-locality strings. BENGALURU
   (13,542 units, the #1 city by volume) shows ₹0. This mirrors the parent feed:
   `amazon_sec_daily` June rows are 180 rev>0 / 975 rev=0 / 243 rev=null, while the money lives in
   `ordered_revenue` (June daily ordered = ₹57.15 M vs shipped ₹0.21 M) — a known quirk of the
   Amazon vendor feed family. **Use `shipped_units` for geography; use `ordered_revenue` (absent
   here) or the range master view for money.**
2. **`city` is raw free-text**, not a normalized dimension: 5,608 distinct strings for what is
   really a few hundred cities — pincodes (`122006`), full addresses (`BANDRA (W),MUMBAI-400050`),
   locality names (`LAJPAT NAGAR 2`), and `DELHI` vs `NEW DELHI` vs `SOUTH DELHI` all coexist.
   Any serious city roll-up needs a normalization pass first.
3. **One window only (June 2026)** as of 2026-07-05; unknown whether monthly loads will append
   (new id block) or replace. The June block starts at id 22,600 — ids 1–22,599 are absent,
   suggesting earlier loads existed and were purged (replace-per-load behaviour likely).
4. **No enrichment layer, no dashboard.** Unlike its siblings there is no `*_master_view`
   (no litres / item_head / margin), and no captured dashboard reads it yet. It currently looks
   like a just-landed backend table awaiting a UI (the app's usual pattern: table first,
   master-view + page later). Watch for an `amazon_sec_city_master_view` or a city drill-down
   appearing on the Amazon Secondary page.
5. **Side observation (2026-07-05):** the raw capture of `amazon_sec_daily` holds 3,113
   distinct-id rows (05-01→07-02) while the live `tables count` returns 2,749 — the daily table
   appears to rotate/purge rows intra-day. Does not affect the June reconciliation above.

## 6. UNDERSTANDING COVERAGE

| Aspect | Status | Evidence |
|---|---|---|
| Grain (business × city × ASIN × month-window) | **FULLY** | 17,671 rows profiled; single June window; 58 ASINs × 5,608 city strings |
| Identity / feed lineage | **FULLY** | exact 3-way June match (₹213,850.26 / 146,682 u) vs sec_daily and sec_range |
| `shipped_units` semantics | **FULLY** | reconciles exactly to the sibling tables |
| `shipped_revenue` semantics | **PARTIAL** | 99.75 % zeros, Delhi/Mumbai-only nonzero — populated-when-available quirk understood, upstream cause (vendor report gaps) inferred |
| `city` normalization | **PARTIAL** | raw free-text confirmed; no mapping table found in the 42-table extract |
| Load cadence / replace-vs-append | **UNCLEAR** | only one load observed (2026-07-03 12:04); id gap 1–22,599 suggests purged history |
| Consuming dashboard | **FULLY (as absent)** | no dashboard in the 2026-07-05 capture reads it |
