# W1 — AMAZON cluster (the deepest integration)

Reverse-engineered data model for JIVO's Amazon e-commerce slice, via read-only
`jivo-ecom-pp-cli` (`https://ecom.jivo.in`). Snapshot date: **2026-06-26**; latest data month **June 2026**.

Amazon is the **only** platform that has `ads`, `coupon`, `price`, `marketplace`, `comparison`
AND `secondary-monthly` live, and it is the **only place with margin data**
(`amazon_sec_range_margins` → flows into `amazon_sec_range_master_view`).

## TL;DR for the intelligence layer
- **Ground-truth sell-out tables to trust:** the **`_master_view`** variants, NOT the raw `_sec_*`
  tables. The master views add litres, item/category mapping, margin %, ₹-after-margin, net realise,
  and split by selling entity. Raw tables are the un-enriched ingest.
- **Margins:** `amazon_sec_range_margins` is a tiny (114-row) **reference table**: one `margin_pct`
  per ASIN (∈ {18%, 20%, 25%}). It is **% only, no ₹** — the ₹ contribution is *computed downstream*
  in `amazon_sec_range_master_view` (`shipped_revenue_after_margin`, `selling_price_after_margin`,
  `realise`) and in the `secondary` dashboard (`margin_value`, `margin_tax_value`, `net_realise_shpd`).
- **Universal join key:** `asin` ↔ master `format_sku_code` (AMAZON) ↔ `sku_sap_code` (FGxxxx) ↔ `item`.
  327 AMAZON listings exist in `master products`.
- **OOS + live ad spend = burn flag** is fully constructable: `amazon_inventory.sellable_on_hand_units`
  (or `soh-doh` DOH) × `amazon_ads.total_cost` by ASIN/date.
- **Amazon inventory is siloed:** `all_platform_inventory` (W4) contains **NO Amazon rows** (only
  BIG BASKET/BLINKIT/JIO MART/SWIGGY/ZEPTO). Amazon SOH lives only in `amazon_inventory`, which uses a
  fundamentally different (Vendor-Central) inventory model.

---

# TABLES (9 in slice + 1 cross-link)

## 1. `amazon_inventory` — 11,446 rows
**Grain:** one **ASIN × inventory_date** snapshot of Amazon Vendor-Central availability
(149 distinct ASINs; 129 distinct dates **2025-12-31 → 2026-06-24**).
This is Amazon's *vendor* inventory feed (net received, open PO, sellable/unsellable on hand) — a
much richer model than the simple SOH in `all_platform_inventory`.

| column | type | meaning | example |
|---|---|---|---|
| `id` | int | PK | 5 |
| `inventory_date` | date | snapshot date | 2025-12-31 |
| `raw_viewing_range` | str | source report range label | "31/12/25 - 31/12/25" |
| `uploaded_at` | ts | ingest time | 2026-01-02T10:05Z |
| `business` | str | selling entity (4 spelling variants of JMPL / JWPL) | "JIVO MART PVT LTD" |
| `asin` | str | **join key** | B0CF9RQ9PL |
| `product_title` | str | Amazon listing title | "SANO Pomace Olive Oil…" |
| `brand` | str | brand — **DIRTY**: polluted with ASINs & literal "ASIN" (ingest column-shift), real values jivo/JIVO/sano | "sano" |
| `sourceable_product_oos_pct` | str% | % of sourceable catalog OOS | "0.00%" |
| `vendor_confirmation_pct` | str% | PO confirmation rate | "0.00%" |
| `net_received` | float ₹ | value of stock received | 135313.44 |
| `net_received_units` | int | units received | 444 |
| `open_purchase_order_quantity` | int | open PO qty (units) | 666 |
| `receive_fill_pct` | str% | receive fill rate | "0.00%" |
| `overall_vendor_lead_time_days` | int | vendor lead time | 0 |
| `unfilled_customer_ordered_units` | int | demand not yet filled | 76 |
| `aged_90_days_sellable_inventory` | float ₹ | value of 90-day-aged sellable stock | 1817.2 |
| `aged_90_days_sellable_units` | int | units aged 90d | 6 |
| `sellable_on_hand_inventory` | float ₹ | **SOH value (sellable)** | 172386.58 |
| `sellable_on_hand_units` | int | **SOH units (sellable)** — the availability number | 566 |
| `unsellable_on_hand_inventory` | float ₹ | SOH value (unsellable) | 293.34 |
| `unsellable_on_hand_units` | int | unsellable units | 1 |

- **Join:** `asin` → master `format_sku_code`. **Business question:** "How many sellable units on hand
  per ASIN today, and what's at risk of aging out (aged_90_days)?"
  `platform stats amazon` headline `inventory` = 11446 = this table's row count.
- **Gotchas:** `*_pct` fields are **strings with "%"** (parse). `brand` column is corrupted — do not
  trust it; derive brand from master via ASIN. `*_inventory` = ₹ value, `*_units` = count (don't mix).
  Multiple `business` spellings need normalising.
- CLI: `jivo-ecom-pp-cli tables data amazon_inventory --page-size 5 --data-source live --json`

## 2. `amazon_ads` — 48,585 rows  ⭐ (biggest ads table in the system)
**Grain:** one **campaign × ad_group × advertised_product × day** row (986 distinct campaigns,
127 distinct advertised products/ASINs, 55 distinct dates **2026-05-01 → 2026-06-25**). Sponsored-Products style.

| column | type | meaning | example |
|---|---|---|---|
| `id` | int | PK | 1059 |
| `campaign_id` / `campaign_name` | str | campaign | "Mix Category \| MP Combo \| SP KT" |
| `ad_group_id` / `ad_group_name` | str | ad group | "Combo" |
| `advertised_product_id` | str | **= ASIN (join key)** | B0B3XKKM8K |
| `advertised_product_sku` | str | seller SKU label (free text, NOT the SAP code) | "JM-Canola 5L+Extra light 2L" |
| `portfolio_id` / `portfolio_name` | str | portfolio grouping | "Mix Campigns" |
| `budget_currency` | str | always INR | INR |
| `impressions`,`clicks`,`ctr` | float | funnel top | 429 / 1 / 0.002331 |
| `total_cost` | float ₹ | **ad spend** | 5.24 |
| `purchases`,`sales`,`units_sold` | float | conversions (₹ for sales) | 0/0/0 |
| `cost_per_purchase`,`purchase_rate`,`roas` | float | efficiency (roas = sales/cost) | null/0/0 |
| `*_promoted` (purchases/sales/units/cpp/rate/roas) | float | directly-promoted product attribution | 0 |
| `*_halo` (purchases/sales/units) | float | halo (other-SKU) attribution | 0 |
| `*_ntb` (purchases/sales/units/cpp/rate/roas) | float | **New-To-Brand** attribution | null |
| `detail_page_views`,`cost_per_detail_page_view`,`detail_page_view_rate` | float | DPV funnel | null |
| `format` | str | always "AMAZON" | AMAZON |
| `uploaded_at`,`date` | ts/date | ingest / **performance date** | 2026-05-01 |

- **Join:** `advertised_product_id` (ASIN) → master `format_sku_code`. `advertised_product_sku` is a
  seller-typed free-text label, do **not** join on it.
- **Business question / the burn flag:** "OOS + live ad spend = wasted burn." Join `amazon_ads` (by
  `advertised_product_id` + `date`) to `amazon_inventory`/`soh-doh` (by `asin` + date): any ASIN with
  `total_cost > 0` while `sellable_on_hand_units = 0` (or DOH ≈ 0) is burning ad budget on an OOS SKU.
- **Gotchas:** ROAS/ACOS not stored as columns in the *raw* table — the **dashboard derives** ACOS
  (=cost/sales) and CPC. Many NTB/DPV fields are null (sparse). `roas` raw column exists but dashboard
  recomputes. Numbers are floats even for counts.
- CLI raw: `tables data amazon_ads --page-size 5`. Shaped: `platform ads amazon`.

## 3. `amazon_coupon` — 765 rows
**Grain:** one **coupon × snapshot-date** (23 distinct dates **2026-05-22 → 2026-06-26**; ~30 coupons/day).
A daily redo of every live coupon's running budget burn.

| column | type | meaning | example |
|---|---|---|---|
| `id` | int | PK | 1 |
| `date` | date | snapshot date | 2026-05-22 |
| `coupon_name` | str | coupon (maps loosely to a product, e.g. "A2 1","CANOLA 5") | "A2 1" |
| `start_date`,`end_date` | date | coupon validity window | 2026-05-01 / 2026-05-31 |
| `clips` | int | times clipped | 7 |
| `redemptions` | int | times redeemed | 2 |
| `total_discount` | float ₹ | discount given | 226.2 |
| `budget_spent` | float ₹ | budget consumed | 226.2 |
| `budget_remaining` | float ₹ | budget left | 4773.8 |
| `budget_used` | float ratio | spent/total (0..1) | 0.0452 |
| `total_budget` | float ₹ | coupon budget | 5000.0 |
| `format` | str | "AMAZON" | AMAZON |
| `created_at`,`updated_at` | ts | ingest | — |

- **Join:** by `coupon_name` only — a fuzzy product label (no ASIN in the raw table). The **dashboard**
  enriches each coupon with `item_head` + `brand` (derived). Treat coupon→SKU as approximate.
- **Business question:** "Which coupons are about to exhaust budget / over-discount?"
  `budget_used` near 1.0 = nearly spent (dashboard KPI rolls these up).
- CLI: `platform coupon amazon`.

## 4. `amazon_price_data` — 192 rows
**Grain:** one **ASIN × upload_date** price snapshot. NOT a continuous history — only **2 upload dates**
(2026-05-12, 2026-05-14), **96 ASINs each** → 192. A competitive/landed-price audit, refreshed ad-hoc.

| column | type | meaning | example |
|---|---|---|---|
| `id` | int | PK | 1 |
| `upload_date` | date | snapshot | 2026-05-12 |
| `url` | str | Amazon short link | https://amz.cx/3ZBf |
| `asin` | str | **join key** | B0152TWWSQ |
| `product` | str | internal item label (= master `item`) | "CANOLA 1+1L" |
| `margin_basis` | str | basis for margin calc ("ASP") | ASP |
| `mrp` | float ₹ | MRP | 750.0 |
| `asp` | float ₹ | average selling price | 533.0 |
| `margin_pct` | float % | margin on basis | 25.0 |
| `tax_pct` | float % | GST % | 5.0 |
| `cost_without_tax` | float ₹ | derived cost ex-tax | 380.95 |
| `url_price` | float ₹ | live price scraped from listing (null if OOS) | 469.0 |
| `stock_status` | str | "In Stock" / "Out of Stock" | In Stock |
| `seller` | str | buy-box seller (null if OOS) | "RK World Infocom Pvt Ltd" |
| `rk_price`,`jm_price`,`svd_price`,`bau_price`,`art_price` | float ₹ | competitor/seller price columns (RK World, JioMart, SVD, BAU, ART) | 234 / 319 / 489 / 509 / 469 |
| `created_at`,`updated_at` | ts | ingest | — |

- **Join:** `asin` → master. Also has its own `margin_pct` (a *price-audit* margin, distinct from the
  authoritative `amazon_sec_range_margins`).
- **Business question:** "Is our listed/buy-box price right vs competitors, and is it In/Out of Stock?"
  Dashboard `price` summary: total_rows, in_stock vs out_of_stock count, missing_url_price, seller_count.
- **Gotchas:** only **96 ASINs**, 2 dates → snapshot not time-series; ~half rows OOS with null prices.
  Two different `margin_pct` sources exist (this table vs the margins table) — for contribution use the
  margins table, not this.
- CLI: `platform price amazon`.

## 5–6. Daily sell-out: `amazon_sec_daily` (2,749) + `amazon_sec_daily_master_view` (2,749)
**Grain (raw):** one **ASIN × report_date × business** daily sell-out row (71 ASINs, 55 dates
**2026-05-01 → 2026-06-24**, 2 business entities). Same row count as its master view → master view is a
**1:1 enrichment** of the raw daily.

**Raw `amazon_sec_daily`** columns: `id, business, asin, product_title, brand, ordered_revenue,
ordered_units, shipped_revenue, shipped_cogs, shipped_units, customer_returns, report_date, created_at`.

**`amazon_sec_daily_master_view`** ADDS (the enrichment layer — this is what dashboards read):
- litres everywhere: `ordered_litres, shipped_litres, return_litres, canceled_litres` (= units × `unit_size`)
- `unit_size`, `per_unit` ("5 LTR"), `item` (SAP item name), `item_head`, `category`, `sub_category`, `category_head` ("OIL")
- cancellations: `canceled_value/units/litres` (absent from raw)
- `return_value` (₹, raw only has `customer_returns` count)
- `sales_type` ∈ **{RK JMPL, RK JWPL}** (RK World selling on behalf of Jivo **M**art / Jivo **W**ellness)
- `distributor_margin` (e.g. 0.18) and `tax` (0.05) — per-row margin/tax
- `shipped_revenue_2` (a recomputed shipped revenue), `month`,`month_num` ("MAY-11"),`year`

- **Join:** `asin` → master. **Business question:** "Daily Amazon sell-out (ordered vs shipped vs
  returned vs cancelled), in ₹ and litres, by SKU/category." Feeds `platform drr amazon`
  (note: *DRR dashboard explicitly says it reads `amazon_sec_daily_master_view`*).
- **Trust:** use the **master_view** (litres + margin + category mapping). Raw `shipped_revenue` is often
  0 in raw rows; master_view recomputes via `shipped_revenue_2`.

## 7–8. Ranged/period sell-out: `amazon_sec_range` (3,610) + `amazon_sec_range_master_view` (4,240)
**Grain (raw):** one **ASIN × [from_date,to_date] period × business** sell-out aggregate. Periods span
**2024-01-01 → 2026-06-24** (66 distinct to_dates) — this is the **historical/MTD range** table (unlike
daily which is May–Jun 2026 only). `master_view` has **MORE rows (4,240 > 3,610)** because it splits/
recomputes by selling entity (`sales_type` RK JMPL vs RK JWPL) and margin category, so it is **not** a
1:1 copy of the raw range.

**Raw `amazon_sec_range`** columns: `id, business, asin, product_title, brand, ordered_revenue,
ordered_units, shipped_revenue, shipped_cogs, shipped_units, customer_returns, from_date, to_date, created_at`.

**`amazon_sec_range_master_view`** is the **richest sell-out table in the system** — adds:
- litres (`ordered_litres, shipped_litres, return_litres, canceled_litres`), `unit_size`, `per_unit`
- item mapping: `item, category, sub_category, item_head, brand_2`, `month`, `month_day` ("12-MAY"), `year`
- entity: `sales_type` (RK JMPL/JWPL), `business`
- **MARGIN ECONOMICS** (joined from `amazon_sec_range_margins`):
  - `margin_pct` (e.g. 25.0), `margin_category` ("Canola 1+1")
  - `calculated_shipped_revenue` (recomputed shipped ₹)
  - `shipped_revenue_after_margin` = shipped − margin → **net distributor revenue**
  - `selling_price` and `selling_price_after_margin` (per-unit ₹)
  - `realise` (= net realisation per unit after margin)
  - `source_shipped_revenue` (raw), `shipped_cogs`
- **Business question:** "Period (MTD / historical month) Amazon sell-out WITH contribution after
  distributor margin, by SKU and category." This is the join target for ₹-at-risk math.
- CLI raw: `tables data amazon_sec_range --page-size 5`. Shaped: `platform secondary amazon`,
  `platform secondary-monthly amazon`, `platform comparison amazon`.

## 9. `amazon_sec_range_margins` — 114 rows  ⭐ UNIQUE MARGIN SOURCE
**Grain:** **one row per ASIN** (114 rows = 114 distinct ASINs). A small **reference/lookup table** of
each ASIN's distributor margin — **not** time-series, **not** ₹.

| column | type | meaning | example |
|---|---|---|---|
| `id` | int | PK | 1 |
| `asin` | str | **join key (unique)** | B0B2RW9N9F |
| `margin_category` | str | human margin bucket label | "A2 Ghee 1kg" |
| `margin_pct` | float % | **distributor margin %** ∈ **{18.0, 20.0, 25.0}** | 20.0 |
| `created_at`,`updated_at` | ts | ingest (all 2026-05-13) | — |

- **What a margin row contains:** just `asin → margin_pct` (+ a label). **% only, no rupee fields, no
  gross/contribution columns, no grain by date.** Margins are flat per ASIN.
- **Can we compute ₹-at-risk & true contribution per SKU?** **YES**, but the ₹ math is *downstream*, not
  in this table:
  - `contribution_₹ = shipped_revenue × margin_pct` → already materialised as
    `amazon_sec_range_master_view.shipped_revenue_after_margin` is **revenue net of margin** and
    `margin_value` in the `secondary` dashboard is the **₹ margin** itself
    (`shipped_value × margin_pct`; e.g. PREMIUM margin_value 6.17M on 26.9M shipped @ 22.95%).
  - `net realise per unit = realise` (master_view) or `net_realise_shpd` (dashboard).
  - **₹-at-risk for an OOS SKU** = projected lost shipped ₹ × `margin_pct` → join margins.asin to
    soh-doh (DOH≈0) and drr (`drr_ops`/projection) by ASIN.
- **Coverage gotcha:** only **114 of 327** AMAZON listings have a margin row. SKUs without a margin row
  fall back to an `item_head` default (COMMODITY appears to default to 18%; see dashboard `margin_pct`
  values). Always LEFT-join and handle the missing-margin case.
- **Join chain:** `amazon_sec_range_margins.asin` → `amazon_sec_range_master_view.asin`
  (brings `margin_pct`,`margin_category`) → `secondary` dashboard rolls into `margin_value`.
  (Dashboard note, verbatim: *"Margins are sourced from amazon_sec_range_margins through
  amazon_sec_range_master_view."*)
- CLI: `tables data amazon_sec_range_margins --page-size 5 --data-source live --json`.

## Cross-link: `all_platform_inventory` (owned by W4)
**Amazon is ABSENT from it.** `distinct format` = {BIG BASKET, BLINKIT, JIO MART, SWIGGY, ZEPTO} — **no
AMAZON**. Columns: `inventory_date, sku_code, item, item_head, brand, soh_unit, soh_ltr, location, format`
(keyed by internal `sku_code`, not ASIN). So Amazon SOH is **siloed** in `amazon_inventory` with a totally
different (Vendor-Central) schema. To unify availability across platforms the intelligence layer must
read Amazon from `amazon_inventory`/`soh-doh amazon` separately and map ASIN→`sku_sap_code` to line it up
with the other platforms' `sku_code`. **(W4: note this gap.)**

---

# DASHBOARDS (`platform <leaf> amazon`) — Amazon has them ALL

| leaf | live? | source | what it returns (shape vs raw) |
|---|---|---|---|
| `stats` | ✅ | mixed | headline `{inventory:11446, sells, openPOs, activeTrucks}`. `inventory` = `amazon_inventory` row count. sells/openPOs/trucks = 0 for Amazon. |
| `primary` | ✅ | `reporting."Amazon PO"` (master_po) | Primary/sell-in (PO) dashboard: per item_head & per-SKU `done/pending/dp/cancelled/expired/order/projection` × value/ltrs/qty. (PO data is W3's slice; surfaced here per-platform.) |
| `secondary` | ✅ | `amazon_sec_range_master_view` (+margins) | Sell-out MTD: `summary` by item_head with `margin_pct, margin_value, margin_tax_value, per_liter_shpd, net_realise_shpd, projection_ltr`; `sku_details`, `category_summary`. `amazon_mp_available:false` (uses only master_view, excludes MP block). **This is where ₹ margin is materialised.** |
| `secondary-monthly` | ✅ (Amazon-exclusive) | sec_range_master_view | 12-month matrix per item_head & per category: `sales_values`/`sales_liters` (order vs shipped) + `mom_growth`. Note **JUNE=0** (range table not yet closed for current month). |
| `secondary-years` | ✅ | sec_range | which years available: **[2026, 2025, 2024]** (history depth). |
| `soh-doh` | ✅ (availability core) | amazon_inventory + sec_daily | **Stock-on-hand / Days-of-Health.** Returns 4 cut `dashboards` (by `asin`, `item_head`, `category`, `category_sub_category`), each row: `units_sold, ltr_sold, soh_unit, soh_ltr, drr_unit, drr_ltr, doh`. `doh = soh / drr`. `available_dates` list; `effective_inventory_date`. **DOH is the OOS/availability signal.** |
| `drr` | ✅ (availability core) | **`amazon_sec_daily_master_view`** | Daily run-rate. `rows`/`sku_rows`/`sub_category_rows` with `ops, units, ltr, drr_ops, drr_units, drr_ltr, projection_*`; `daily` time-series; `totals` incl `projection_*`. Toggle `sales_mode` ∈ {ORDERED, SHIPPED}, `sales_of`/`item_head` filters. Note (verbatim): *"Uses amazon_sec_daily_master_view to match DRR rows 1-20."* |
| `pos` | ✅ but empty | — | purchase orders; `count:0` for Amazon (no PO rows surfaced here). |
| `primary-month-targets` | ✅ | master_po | primary (sell-in) targets vs done: `targets, done_ltrs, achieved_pct, est_ltr, drr, require_drr, pending_ltr, dp_ltrs` per item_head. |
| `month-targets` | ✅ | amazon (sec) | secondary targets vs done: `targets, done_ltrs, done_value, achieved_pct, est_ltr/value, last_month, growth, growth_pct` per item_head (B2C). |
| `price` | ✅ (Amazon-exclusive) | amazon_price_data | shaped price audit: `rows` (per ASIN) + `summary{total_rows, in_stock, out_of_stock, missing_url_price, seller_count, avg_url_price}`; `upload_dates`. |
| `ads` | ✅ (Amazon-exclusive) | amazon_ads | configurable ads cube: `summary` (spend, sales, **roas, acos**, impressions, clicks, ctr, **cpc**, purchases, units, NTB, DPV — ACOS/CPC **derived by dashboard**, not stored), `breakdown_rows` by `dimension` (default `portfolio_name`), `trend_rows` (daily spend vs revenue), `available_metrics`, `filter_options` (years/months/dates). |
| `coupon` | ✅ (Amazon-exclusive) | amazon_coupon | `kpi{clips, redemptions, budget_spent, budget_remaining, total_budget}` + per-coupon `coupons[]` enriched with **`item_head`,`brand`** (not in raw table). `available_dates`. |
| `marketplace` | ✅ (Amazon-exclusive) | MP/sec | Amazon Marketplace (3P) sales: `kpi{inclusive, exclusive, ltrs, quantity}`, breakdowns by `brand`, `state`, `sub_category`, monthly `trend`, `unmapped_asins`. (Per-platform; only Amazon has it live.) |
| `comparison` | ✅ (Amazon-exclusive) | sec_range_master_view | current-month vs **highest historical month** per category: `rows[].{highest,current}` each `{shipped_ltr, shipped_rev, rev_after_margin, price_per_ltr, net_realise}`. Uses margin → `rev_after_margin`. |
| `inventory-match` | ✅ | reconciliation | inventory match/reconciliation; returns `{match:null}` (no mismatch payload at snapshot). |
| `month-on-month-sale` | ❌ gated | — | HTTP 400: *"available only for Big Basket and Flipkart Grocery."* |
| `pendency` | ❌ gated | — | HTTP 400: *"Pendency dashboard is not yet enabled for platform 'amazon'."* |
| `region-doh` | ❌ gated | — | HTTP 404 (only SWG/ZEP have it). |

CLI form for all: `jivo-ecom-pp-cli platform <leaf> amazon --data-source live --json 2>/dev/null | jq .`

---

# Answers to the task's specific questions
1. **`_sec_daily` vs `_sec_range` vs `_master_view`:** `_sec_daily` = one ASIN×day (May–Jun 2026 only);
   `_sec_range` = one ASIN×period[from,to] (history back to 2024, used for monthly/MTD). The `_master_view`
   variants are the **enriched/canonical** layer (litres, item/category mapping, margin %, ₹-after-margin,
   net realise, entity split). **Trust the master_views as ground truth** — raw tables are un-enriched
   ingest (raw `shipped_revenue` is frequently 0; master recomputes). `sec_daily_master_view` is 1:1 with
   raw (2749=2749); `sec_range_master_view` (4240) > raw (3610) because it splits by selling entity
   (RK JMPL/JWPL) and margin category.
2. **`amazon_sec_range_margins`:** one row per ASIN = `asin, margin_category, margin_pct(∈{18,20,25})`.
   **% only — no ₹, no gross/contribution columns, no date grain.** ₹ contribution & ₹-at-risk are
   computable but **downstream**: `shipped_revenue × margin_pct` is materialised as `margin_value`
   (secondary dashboard) and `shipped_revenue_after_margin`/`realise` (master_view). Join by `asin`.
   Covers only 114/327 ASINs → LEFT-join with item_head default fallback.
3. **`amazon_ads`:** spend=`total_cost`, `impressions/clicks/ctr`, conversions `purchases/sales/units_sold`,
   `roas` (raw) — ACOS/CPC derived by the ads dashboard. Plus promoted/halo/NTB attribution splits and DPV.
   **Grain = campaign×ad_group×advertised_product(ASIN)×day** (986 campaigns, 127 ASINs, 55 days). Join by
   `advertised_product_id`=ASIN+`date` → feeds the OOS+ad-spend burn flag.
4. **`amazon_price_data` (192):** a **snapshot price audit**, NOT a history — only 2 upload dates × 96 ASINs.
   Current MRP/ASP/url_price/stock_status/seller + 5 competitor price columns + its own margin_pct.
5. **`amazon_inventory` ↔ `all_platform_inventory`:** **no overlap** — Amazon is excluded from
   `all_platform_inventory`. Amazon uses a richer Vendor-Central schema in `amazon_inventory` keyed by
   ASIN; unify only via ASIN→`sku_sap_code`.
