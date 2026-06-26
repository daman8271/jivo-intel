# W4 — PO / Master / Cross-cutting spine

**Slice:** purchase orders (sell-in), unified inventory, SKU/FC master taxonomy, all
cross-platform `dashboard` endpoints, and the notifications/alert engine.
**Data source:** `jivo-ecom-pp-cli ... --data-source live --json` (API `https://ecom.jivo.in`).
All findings below verified live on **2026-06-26** (latest data month = June 2026).

---

## 0. TL;DR (the cross-platform spine in one screen)

| Thing | What it is | Grain | Authoritative for |
|---|---|---|---|
| `master_po` (44,081) | **Enriched union** of `total_po` + `total_po_zbs`, joined to the SKU/FC master | 1 PO line = PO × SKU × location | **THE sell-in / PO Early-Warning feed** |
| `total_po` (8,030) | Raw POs for **marketplace / scheduled** platforms (BigBasket, CityMall, DealShare, FK-Grocery, Zomato) | 1 PO line (has `id` PK) | raw upstream of `master_po` |
| `total_po_zbs` (35,839) | Raw POs for **quick-commerce trio** — **ZBS = Zepto / Blinkit / Swiggy** | 1 PO line (has `id` PK) | raw upstream of `master_po` |
| `prim_master_po` (0) | Empty "primary PO" table — **primary/sell-in is reconstructed from `master_po`**, not stored here | — | nothing (placeholder) |
| `test_master_po` (0) | Empty scratch table | — | nothing |
| `all_platform_inventory` (176,769) | Daily **SOH** across 5 q-comm/grocery platforms (NOT amazon/flipkart) | 1 = date × SKU × format × dark-store | SOH for the soh/inventory dashboards; **DOH/DRR are NOT stored here** |
| `master products` (803) | Platform **listing** master (one row per platform×listing) | 1 = platform listing | SKU taxonomy join hub |
| `master fcs` (19) | Amazon-style FC dimension (`fc_code`→city/state) | 1 = FC | FC join (Amazon side) |
| notifications (53) | Server-side `INVENTORY_DOH_LOW` alert stream | 1 = SKU×platform alert instance | **only persisted alert history** |

**Single biggest insight:** the `zbs` suffix = **Z**epto + **B**linkit + **S**wiggy (the
quick-commerce platforms). `total_po` holds the *other* platforms. `master_po` =
`total_po` ∪ `total_po_zbs`, enriched with category/brand/litres/margin columns from the
master. Row math confirms: 8,030 + 35,839 = 43,869 ≈ 44,081 (small drift = live ingest +
enrichment). **Diff `master_po` for PO Early-Warning** — it is the only table carrying both
the q-comm and marketplace POs *plus* the litres/fill-rate columns the cockpit needs.

---

## 1. The PO / sell-in tables

### 1.1 `master_po` — the authoritative enriched sell-in feed (44,081 rows)

- **Grain:** one **PO line item** = `po_number` × `sku_code` × `location`. A single
  `po_number` (e.g. `IRA27558106`) spans many SKU rows.
- **Date coverage:** `po_date` 2025-08-01 → 2026-06-26 (303 distinct dates); `delivery_date`
  blank → 2026-07-24 (future scheduled deliveries exist). `po_year` ∈ {2025, 2026}.
- **Formats (8):** BIG BASKET, BLINKIT, CITY MALL, DEAL SHARE, FLIPKART GROCERY, SWIGGY,
  ZEPTO, ZOMATO — i.e. **exactly `total_po` formats ∪ `total_po_zbs` formats** (no Amazon/
  plain-Flipkart — those sell-in feeds live elsewhere; Amazon PO volume appears only via the
  `fulfilment-health` / `primary-po-litres` dashboards).

**Column dictionary** (53 cols):

| column | type | meaning | example |
|---|---|---|---|
| `po_number` | str | Purchase order id (platform-issued). `IRA…`=marketplace, `P…`/`PO…`=q-comm | `IRA27558106` |
| `po_date` | date | PO raised | `2025-09-15` |
| `po_expiry_date` | date | PO expires (must be filled by) | `2025-10-11` |
| `delivery_date` | date | actual/scheduled delivery | `2025-10-07` |
| `vendor_name` / `vendor_new` | str | distributor (raw / normalised). 17 raw → ~7 canonical (Knowtable, Chirag, Baba Lokenath, Antize, Sustainquest, Suryam, Evara…) | `KNOWTABLE` / `KNOWTABLE ONLINE SERVICES PRIVATE LIMITED` |
| `status` | str | platform raw status (16 mixed-case values, see below) | `COMPLETED` |
| `po_status` | str | normalised PO header status ∈ {"", APPOINTMENT DONE, CANCELLED, COMPLETED, EXPIRED, PENDING} | `COMPLETED` |
| `item_status` | str | line fill status ∈ {"", APPOINTMENT DONE, CANCELLED, EXPIRED, **FULL SUPPLIED**, PENDING, **SHORT SUPPLIED**} | `FULL SUPPLIED` |
| `sku_code` | str | **platform listing id** (ASIN-style number or GUID) — joins to `master products.format_sku_code` | `40166398` |
| `sku_name` | str | platform listing title | `Jivo Extra Light Olive Oil 1 L Bottle` |
| `item` | str | short internal SKU name → joins `master products.item` | `EXTRA LIGHT 1L` |
| `sap_sku_name` | str | SAP FG description | `EXTRA LIGHT OLIVE 1 LTR 16 PCS` |
| `order_qty` / `delivered_qty` | float | units ordered / delivered | 64 / 64 |
| `missed_qty` / `filled_qty` | float | shortfall / fulfilled units (= order−delivered / delivered) | 0 / 64 |
| `basic_rate` / `landing_rate` | float | per-unit price excl / incl | 542.86 / 570.0 |
| `case_pack` | int | units per case | 16 |
| `per_liter` | float | **litres per unit** (house-unit factor) | 1.0 / 5.0 |
| `total_order_liters` / `total_delivered_liters` | float | **litres** = qty × per_liter | 64 / 100 |
| `filled_ltrs` / `missed_ltrs` | float | litres delivered / shortfall | 64 / 0 |
| `total_order_amt_inclusive` / `_exclusive` / `_without_margin` | float | order ₹ value (incl tax / excl tax / excl distributor margin) | 36480 / 34743 / 32931 |
| `total_deliver_amt_inclusive` / `total_delivered_amt_exclusive` / `_without_margin` | float | delivered ₹ value variants | … |
| `distributor_margin` | float | margin fraction | 0.055 |
| `distributor_commission_per_unit` / `total_distributor_commission` | float | distributor payout | 29.86 / 1910.87 |
| `realise` | float | net realisation per unit | 514.56 |
| `location` / `city` / `state` | str | delivery node + geo (free-text, NOT `fc_code`) | Bangalore / BENGALURU / KARNATAKA |
| `format` | str | platform (8 values) | BIG BASKET |
| `category` / `sub_category` / `category_head` | str | taxonomy (see §3) | OLIVE / EXTRA LIGHT / OIL |
| `item_head` | str | PREMIUM \| COMMODITY \| OTHER \| "" | PREMIUM |
| `brand` | str | JIVO \| SANO \| "" | JIVO |
| `unit_of_measure` | str | pack size label (12 values, e.g. `1 LTR`,`5+1 LTR`,`160 MLS`) | `1 LTR` |
| `lead_time` / `po_window` | int | days raise→deliver / raise→expiry | 22 / 26 |
| `days_to_expiry` | int | days left to expiry | 0 |
| `open_close` | str | OPEN \| CLOSED (whether window still open) | CLOSED |
| `po_month` / `delivery_month` | str | month name (APRIL…DECEMBER) | SEPTEMBER / OCTOBER |
| `po_year` / `delivered_year` | int | 2025 / 2026 | 2025 |
| `remark` | str | free-text (e.g. `STOCK ISSUE`) | "" |

`status` raw value set (mixed case = un-normalised platform feed; use `po_status`/`item_status` instead):
`ASN_CREATED, Cancelled, CANCELLED, Cancelled post Creation, COMPLETED, CONFIRMED, Expired,
EXPIRED, Fulfilled, INVALID, Pending, PENDING, PENDING_ACKNOWLEDGEMENT, PENDING_ASN_CREATION,
Scheduled, Unscheduled`.

**Business questions:** PO fill-rate / miss-rate, sell-in litres & ₹, expiry exposure,
distributor-wise commission. **PO Early-Warning (#2): diff `master_po` on
(`po_number`,`sku_code`) day-over-day** and watch `item_status` flipping to SHORT
SUPPLIED/EXPIRED, rising `missed_ltrs`, and `days_to_expiry`→0 with `open_close`=OPEN.
`jivo-ecom-pp-cli tables data master_po --page-size 5 --data-source live --json`

### 1.2 `total_po` (8,030) & `total_po_zbs` (35,839) — raw upstream feeds

Identical 16-column schema; **`master_po` is their enriched superset**. The split is by
**platform family**:

| | `total_po` | `total_po_zbs` |
|---|---|---|
| **formats** | BIG BASKET, CITY MALL, DEAL SHARE, FLIPKART GROCERY, ZOMATO | **BLINKIT, SWIGGY, ZEPTO** (= **ZBS**) |
| **rows** | 8,030 | 35,839 |
| **po_date** | 2025-09-15 → 2026-06-26 | 2025-08-01 → 2026-06-26 |
| **po_number style** | `IRA…` (marketplace) | `P…` / GUID sku_codes (q-comm) |
| **status set** | {CANCELLED, COMPLETED, EXPIRED, Fulfilled, Pending, PENDING} | full 14-value q-comm set (ASN_CREATED, CONFIRMED, Scheduled, …) |

**`zbs` = Zepto / Blinkit / Swiggy** — the quick-commerce trio. (Not a billing system or
vendor portal — it is the q-comm platform grouping; q-comm = ~82% of all PO lines.)

Columns: `id` (PK), `po_number, po_date, po_expiry_date, grn_date, vendor_name, status,
sku_code, sku_name, order_qty, delivered_qty, basic_rate, landing_rate, location, format,
remark`. Note `grn_date` (goods-receipt date, often null) exists here but NOT in `master_po`;
`master_po` instead carries the enriched `delivery_date` + litres/margin columns.

> **Authoritative feed for the intelligence layer = `master_po`.** It is the only table with
> both platform families *and* the litres/fill/expiry enrichment. Use `total_po*` only to
> spot raw values dropped during enrichment (e.g. `grn_date`, raw `status`).

### 1.3 `prim_master_po` (0) & `test_master_po` (0) — empty (confirmed)

`tables columns` returns `columns: []`, `sample: null`; `tables count` = 0 for both.
`prim_master_po` is the intended "primary PO" store but is **empty** — **primary/sell-in is
reconstructed at query time from `master_po`/`total_po`** (the `primary-po-litres`,
`category-litres`, `top-skus`, `category-breakdown` dashboards all carry `"source":"primary"`
yet read from `master_po`). `test_master_po` = dev scratch. Neither should be relied on.

---

## 2. `all_platform_inventory` — the unified SOH table (176,769 rows)

- **Grain:** one **`inventory_date` × `sku_code` × `format` × `location`** (dark-store/feeder
  warehouse). Confirmed: same date+sku+format splits into multiple rows by `location`
  (e.g. Rajpura vs "Rajpura R2 - Feeder Warehouse").
- **Date coverage:** 2026-01-01 → 2026-06-26, **147 distinct dates** (~daily, with gaps —
  no inventory snapshot every calendar day).
- **Coverage caveat — NOT truly "all platforms":** `format` ∈ **{BIG BASKET, BLINKIT, JIO
  MART, SWIGGY, ZEPTO}** only. **Amazon and Flipkart inventory are NOT here** (they live in
  `amazon_inventory` / per-platform tables). So this is the unified SOH for q-comm + BB +
  JioMart, *not* a universal SOH source.

**Column dictionary** (9 cols — deliberately thin):

| column | type | meaning | example |
|---|---|---|---|
| `inventory_date` | date | snapshot day | 2026-01-01 |
| `sku_code` | str | platform listing id → `master products.format_sku_code` | 10048295 |
| `item` | str | short SKU name → `master products.item` | CANOLA 5L |
| `item_head` | str | PREMIUM \| COMMODITY \| OTHER | PREMIUM |
| `brand` | str | JIVO \| SANO | JIVO |
| `soh_unit` | int | **stock-on-hand, units** | 123 |
| `soh_ltr` | float | **stock-on-hand, litres** (= soh_unit × per_unit_value) | 615.0 |
| `location` | str | dark-store / feeder-warehouse name (free-text, ~180 values, platform-specific — does NOT map to `fc_code`) | `Rajpura R2 - Feeder Warehouse` |
| `format` | str | platform (5 values) | BLINKIT |

**Critical gotcha — only SOH is stored. There is NO `doh`, `drr`, `category`, or `state`
column.** Days-of-Health (DOH) and Daily-Run-Rate (DRR) are **derived at query/alert time**
(DRR from secondary-sales tables, DOH = soh_ltr ÷ drr_ltr) — they appear only in the
notifications stream (§4) and the per-platform soh-doh dashboards, never in this table. To
get category/state for an inventory row you must join `sku_code`→`master products` /
`master_po`. **Negative/zero DOH and OOS-but-listed are inferred downstream, not here.**

**Business question:** "current stock cover per SKU/platform/dark-store."
`jivo-ecom-pp-cli tables data all_platform_inventory --page-size 5 --data-source live --json`
`jivo-ecom-pp-cli tables distinct all_platform_inventory format --data-source live --json`

---

## 3. SKU master taxonomy

### 3.1 `master products` (803 rows) — the listing master / join hub

- **Grain:** one **platform listing** (one row per `format` × `format_sku_code`). 803 rows =
  803 distinct `format_sku_code` (every listing unique).
- **Cardinality:** **169 distinct internal SAP SKUs** (`sku_sap_code`, e.g. `FG0000083`) vs
  **803 platform listings** (`format_sku_code`) — i.e. each physical product is listed ~4.75×
  across platforms. 500 distinct `item` short-names.
- **Columns:** `format, format_sku_code, product_name, item, sku_sap_code, sku_sap_name,
  case_pack, per_unit, per_unit_value, tax_rate, uom, item_head`.

> **GOTCHA:** `master products` does **NOT** contain `brand`, `category`, or `sub_category`
> columns. Those live in **`master_po`** (and notifications). To enumerate the full
> category/brand value sets, use `master_po`, not products.json.

**Listings per platform (`format`):** AMAZON 327, FLIPKART 281, ZEPTO 38, SWIGGY 34, BIG
BASKET 33, FLIPKART GROCERY 22, JIO MART 22, CITY MALL 16, ZOMATO 13, BLINKIT 10, DEAL SHARE
7. (Amazon+Flipkart dominate the *catalog* even though q-comm dominates *POs*.)

**`item_head` (PREMIUM mix driver):** PREMIUM 441, COMMODITY 196, OTHER 165, null 1.
**`uom`:** LTR 633, MLS 86, GMS 71, KGS/KG 7, null 6.

### 3.2 Full categorical value sets (from `master_po` — the only place with them)

- **`item_head`** (4): `"" , COMMODITY, OTHER, PREMIUM`
- **`brand`** (3): `"" , JIVO, SANO` — **two house brands: JIVO (flagship) + SANO (value)**.
- **`category_head`** (4): `"" , BEVERAGE, OIL, OTHER`
- **`category`** (13): BLENDED, CANOLA, COTTON SEED, DRINKS, GHEE, GROUNDNUT, MUSTARD, OLIVE,
  RICE BRAN, SESAME OIL, SLICED OLIVE, SOYABEAN, SUNFLOWER (+"").
- **`sub_category`** (31): APPLE, APPLE SF, BLACK OLIVE, BLUEBERRY, CANOLA, COTTON SEED, DESI
  GHEE, ENERGY DRINK SF, EXTRA LIGHT, EXTRA VIRGIN, GINGER ALE SF, GOLD, GROUNDNUT, JEERA,
  JEERA SF, JIVO POMACE, MANGO, MINERAL WATER, MOJITO, MOJITO SF, MUSTARD KACCHI GHANI, RICE
  BRAN, ROSE, SANO POMACE, SESAME OIL, SO OLIVE, SODA, SOYABEAN, SUNFLOWER, TONIC WATER,
  YELLOW MUSTARD (+""). (`SF` suffix ≈ sugar-free/special beverage variants.)

### 3.3 Premium-mix % (the house KPI)

**Premium-mix % = Σ litres where `item_head`=PREMIUM ÷ Σ all litres.** Litres = `qty ×
per_unit_value` (= `per_liter` in master_po, = `per_unit_value` in products). The
`category-litres` / `category-breakdown` / `category-trend` dashboards already split
**premium_ltrs vs commodity_ltrs** so the mix is read directly: e.g. May '26 = 488,662 prem /
938,378 total ≈ **52% premium by litres**.

---

## 4. FCs (`master fcs`, 19 rows)

- **Grain:** one Amazon-style fulfilment centre. Columns: `fc_id, fc_code, fc_name(null),
  city, state, region(null)`. `fc_name` & `region` are all-null (not populated).
- **Join:** `fc_code` ↔ Amazon-side inventory/secondary tables. **Does NOT join to
  `all_platform_inventory.location`** (that uses free-text dark-store names).

**Regional clusters (by state/metro):**

| Region | FCs |
|---|---|
| **North / Delhi-NCR (Haryana)** — 6 | DED3 (Gurgaon), DED5 (Gurugram), HDL2 (Sonepat), HHR7 (Sonipat), HHS1 (Gurgaon), HNR4 (Dadri Toe) |
| **West / Mumbai-Pune (Maharashtra)** — 4 | HBA4 (Bhiwandi), HMU5 (Bhiwandi), HMH4 (Kalyan), HPN6 (Pune) |
| **South / Bengaluru (Karnataka)** — 3 | HBL4 (Kolar), HKA2 (Bengaluru), HKR2 (Bengaluru) |
| **East / Kolkata (West Bengal)** — 3 | HCC2 (Hooghly), HCC5 (Kolkata), HCC6 (Kolkata) |
| **South / Chennai (Tamil Nadu)** — 1 | HCI2 (Chennai) |
| **South / Hyderabad (Telangana)** — 1 | HHY7 (Hyderabad) |
| **BAD ROW** — 1 | **HLK1 — city `#N/A`, state `#N/A`** (un-geocoded; exclude from regional rollups) |

---

## 5. Cross-platform dashboards (`dashboard …`)

All read June-2026 (latest) by default. `"source":"primary"` ⇒ sell-in from `master_po`;
`"source":"secondary"` ⇒ sell-out from the per-platform `*Sec`/`*_sec_*` tables (other
workers). Many accept `--platform <slug>` and `--head premium|commodity` filters.

| Dashboard | Returns | Business question / notes |
|---|---|---|
| **latest-month** | `{month, year, month_label, source_date, defaulted, source}` | "what is the current data month?" → June 2026. Drives all defaults. |
| **fulfilment-health** | `window{start,end,window_days:30,lag_days:7}`, `total{ordered_ltrs, filled_ltrs, missed_ltrs, fill_rate, miss_rate, po_count}`, `by_platform[…]` | **PO fill-rate, NOT on-time.** `fill_rate = filled_ltrs/ordered_ltrs` over a trailing 30-day window lagged 7 days, all in **litres** from `master_po`. June: 58.2% overall fill, 17.7% miss, 1,060 POs. Best: FK-Grocery 82%, Zepto 77%; worst: Amazon 43%, Swiggy 52%. |
| **primary-po-litres** | `platforms[{format, delivered_ltrs}]`, month/year | **delivered** sell-in litres by platform (incl. Amazon here). The "primary PO" headline. |
| **category-litres** | `{head, total_ltrs, categories[{category, ltrs}]}` | premium (default) sell-in **litres by category** for the month — the house-unit volume view. |
| **category-breakdown** | `premium{categories[],sub_categories[]}` + commodity, all in `ltrs` | category & sub-category litre split, premium vs commodity, for a month. |
| **category-platform-breakdown** | platform split for one category/sub_category | **requires** `category`/`sub_category` param — empty ("Uncategorized") otherwise. |
| **category-trend** | `series[{month,year,label, premium_ltrs, commodity_ltrs, total_ltrs}]` | 6-month premium-vs-commodity litre trend (e.g. May '26 938k ltr). The premium-mix time series. |
| **category-sku-breakdown `--platform`** | `skus[]`, months[] | **requires** `--platform` + category param; SKU-level litres within a category per platform. Empty without params. |
| **top-skus** (overall + `--platform`) | `skus[{name, head, code, brand, ltrs, prev_ltrs, delta_pct, is_new}]`, prev_month | top sell-in SKUs by litres MoM with delta%. Overall: `code`/`brand` null (aggregated by name); per-platform variant same shape. June top: MUSTARD 1L 84,108 ltr (−24.6% MoM). |
| **inventory-charts** | `platform_totals[{platform, total_qty, sku_count, color}]`, `city_distribution[{city, qty}]`, `top_products[]` | SOH visualization across **6 platforms incl. amazon** (so it aggregates per-platform inventory tables, NOT just `all_platform_inventory`). amazon 8.43M units / swiggy 6.42M / zepto 5.95M. Top city: Mumbai 1.64M. **No DOH bucket** here. |
| **state-sales** | `states[{state, units, value, by_platform{}}]`, `brands/categories/sub_categories` filter_options, `mapped_units/value`, `pct_mapped`, metric | **secondary** sales geography: units/₹ by state, split by platform. `pct_mapped` = share of sales mappable to a state. |
| **state-sales-detail** | `rows[]`, paginated (`limit/offset`, `total_rows`) | drill into one state's SKU sales — **requires `state` param** (empty otherwise). |
| **secondary-yoy-growth** | `rows[{slug,name,values{2024,2025,2026:{actual,value,units,growth_pct,projection,elapsed_day,days_in_month,max_date,source}}}]` | **secondary** YoY litre growth per platform 2024→2026 with month-to-date **projection**. e.g. Amazon 2025 +68.5%, 2026 +0.15% MTD (proj 244,118 ltr). `source` names the underlying table (e.g. `amazon_sec_range_master_view`). |
| **platform-expiry-alerts** | `platforms[{format, slug, po_count, total_litrs, total_units, total_order_units}]` | **PO expiry exposure** per platform (POs expired/expiring with unfilled qty). June: CityMall 5 POs / 51,756 ltr most exposed. |
| **expiry-alerts `<platform>`** | `{alerts:[…]}` | per-platform expiry detail. **All 8 returned empty `{"alerts":[]}`** on 2026-06-26 (use the aggregate `platform-expiry-alerts` instead). |

---

## 6. Notifications / alert engine (the availability cockpit spine)

Command: `jivo-ecom-pp-cli notifications list --json` (alias `notifications`). **No pagination
flags exist**; the endpoint returns the **top 50** of `count=53` (the other 3 are unreachable
via CLI). Live pull 2026-06-26: 53 total, 53 unread, **all 50 returned are identical type**.

- **Distinct `type`:** **only `INVENTORY_DOH_LOW`** (no other alert type exists in the stream).
- **Distinct `severity`:** only `critical`. `threshold` only `5.0`. `platform_slug`/`format`
  only `amazon`. `inventory_date` all `2026-06-24`. `doh` ∈ {−2.0 (×3), 0.0 (×47)}.
- **State:** every row `active:true`, `read:false`, `resolved_at:null` — **nothing resolved
  yet**; this is a fresh snapshot generated 2026-06-26 05:34 UTC (all `first_seen_at` within
  the same second).

> The engine fires `INVENTORY_DOH_LOW` when a SKU's **DOH < threshold (5 days)**. `doh = soh_ltr
> ÷ drr_ltr`; **`doh = -2.0` is the OOS sentinel** (soh=0 with live demand), `doh = 0.0` =
> zero cover. Currently only Amazon SKUs have breached — but the schema is platform-generic.

**Full notification schema** (top-level + nested `payload` duplicates the metrics):

| field | type | meaning |
|---|---|---|
| `id` | int | alert id (e.g. 1391) |
| `type` | str | alert type — `INVENTORY_DOH_LOW` (only value seen) |
| `title` | str | `AMAZON SANO SUNFLOWER 1L DOH -2.00` |
| `message` | str | human text incl. threshold |
| `format` / `platform_slug` | str | AMAZON / amazon |
| `sku_code` | str | platform listing id (ASIN) → `master products.format_sku_code` |
| `sku_name` / `item` | str | SKU name |
| `item_head` | str | PREMIUM \| COMMODITY \| OTHER |
| `category` / `sub_category` / `brand` | str | taxonomy (joined in — NOT present in `all_platform_inventory`) |
| `inventory_date` | date | SOH snapshot date used |
| `sales_max_date` | date | latest secondary-sales date used for DRR |
| `month_start` | date | month anchor (2026-06-01) |
| `units_sold` / `ltr_sold` | float | sales in the DRR window |
| `soh_units` / `soh_ltr` | float | stock on hand |
| `drr_units` / `drr_ltr` | float | **daily run-rate** (= sales ÷ days) |
| `doh` | float | **days-of-health** = soh ÷ drr; `-2.0` = OOS sentinel |
| `threshold` | float | alert trigger (5.0 days) |
| `severity` | str | `critical` |
| `read` / `is_read` / `active` | bool | read + active flags |
| `resolved_at` | ts/null | resolution time (always null in snapshot) |
| `first_seen_at` / `last_seen_at` / `created_at` | ts | alert lifecycle timestamps |
| `link` | str | UI deep-link `/notifications/inventory-doh/<id>` |
| `payload` | obj | nested copy of all metric fields (`alert_type` mirrors `type`) |

**Why it matters:** this is the **only persisted alert history** and the single place where
**SOH + DRR + DOH are co-located per SKU×platform** (`all_platform_inventory` has SOH only).
It is the spine of the availability cockpit — but note its current limits: snapshot-only (no
historical resolution states observed), top-50 cap, and currently Amazon-only coverage.

---

## 7. Gotchas (consolidated)

1. **`zbs` = Zepto/Blinkit/Swiggy**, not a billing system. `total_po` = the other platforms.
2. **`master_po` ≈ `total_po` ⊕ `total_po_zbs`** enriched; it (not `prim_master_po`, which is
   empty) is the authoritative sell-in feed. Primary PO is *reconstructed*, not stored.
3. **`all_platform_inventory` excludes Amazon & Flipkart** and stores **SOH only** — no DOH/
   DRR/category. DOH/DRR exist only in notifications + per-platform dashboards.
4. **Litres are the house unit** everywhere: litres = `qty × per_unit_value`(=`per_liter`).
   Premium-mix % = PREMIUM litres ÷ total litres.
5. **brand/category/sub_category are in `master_po` (and notifications), NOT in
   `master products`** — use `master_po` to enumerate them.
6. **FC `fc_code` ≠ inventory `location`.** FCs are Amazon-side codes; inventory locations are
   free-text dark-store names. Row **HLK1 has `#N/A` city/state** — drop from geo rollups.
7. **`status` (raw, mixed-case, 16 values) is dirty** — use normalised `po_status`/
   `item_status` for logic. `delivery_date` can be future (up to 2026-07-24).
8. **Notifications: no pagination (top-50 of 53), Amazon-only, all unread/active right now** —
   treat as a live snapshot, not a complete historical log.
9. **inventory-charts spans 6 platforms (incl. amazon)** so it aggregates per-platform
   inventory tables, not just `all_platform_inventory`. Numbers won't tie to that one table.
