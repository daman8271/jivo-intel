# W2 — QUICK-COMMERCE cluster (Blinkit · Swiggy Instamart · Zepto)

The availability heart of the warehouse. This slice holds the densest, most time-sensitive
signal (daily sell-out + daily inventory snapshots) and powers **Cockpit #1 (availability /
lost-sales)**: the `soh-doh`, `drr`, and `region-doh` dashboards.

Data as of recon **2026-06-26**. Latest data month June 2026. All findings read-only via
`jivo-ecom-pp-cli ... --data-source live --json`.

---

## TL;DR — the three headline answers

1. **Why is `swiggySec` 491k rows?** Because Swiggy sell-out is recorded at **dark-store grain**:
   one row = **SKU (`ITEM_CODE`) × dark-store (`STORE_ID`) × day (`ORDERED_DATE`)**. There are
   **1,282 distinct STORE_IDs** across 136 cities / 555 areas, over **320 days** (2025-08-10 →
   2026-06-25). `STORE_ID` is the cardinality bomb. By contrast `blinkitSec` (128k) and `zeptoSec`
   (43k) are aggregated to **city** grain (no store column), so they are ~10× smaller despite
   blinkit carrying 2.4 years of history.

2. **Brand fund vs ads** are two *different funding mechanisms*, in separate tables:
   - **`*_ads`** = paid **CPC/CPM advertising** performance (campaigns, impressions, clicks, spend,
     ROAS). The brand *buys visibility*.
   - **`*_brandfund`** = **trade/co-investment spend** — the brand funding *price discounts /
     promo claims* the platform runs (promo %, promo INR, claim amount, GST/cess/margin
     adjustments). It is money flowing into **price-off promotions**, not ad auctions.

3. **Q-comm geography**: `swiggySec` carries **STORE_ID + AREA_NAME + CITY** (store-level);
   `zeptoSec` carries **City** only; `blinkitSec` carries **city_id/city_name** only. Inventory:
   `swiggy_inventory` is **facility-level** (41 facilities), `blinkit_inventory` is
   **warehouse/facility-level** (68 backend facilities), `zepto_inventory` is **city-level**
   (68 cities). **`region-doh` is LIVE only for Swiggy + Zepto** because their *Sec tables carry a
   clean **city** dimension that joins to a city-keyed inventory feed; Blinkit `region-doh` is
   **HTTP 404 (not built)**.

---

## 0. Slice inventory (12 tables) + capability map

| Table | Rows | Grain | Geo dimension | Date col | Coverage |
|---|---|---|---|---|---|
| `swiggySec` | 491,250 | SKU × **dark-store** × day | STORE_ID/AREA/CITY | ORDERED_DATE | 2025-08-10 → 2026-06-25 (320 d) |
| `blinkitSec` | 128,749 | SKU × **city** × day | city_id/city_name | date | 2024-01-01 → 2026-06-25 (892 d) |
| `zeptoSec` | 43,471 | SKU × **city** × day | City | Date | 2025-10-01 → 2026-06-25 (268 d) |
| `swiggy_inventory` | 63,699 | SKU × **facility** × day | facility_name/city | inventory_date | 2026-01-01 → 2026-06-26 (120 d) |
| `zepto_inventory` | 31,955 | SKU × **city** × day | city | inventory_date | 2026-01-01 → 2026-06-26 (136 d) |
| `blinkit_inventory` | 27,069 | item × **backend facility** × day | backend_facility | inventory_date | 2026-01-01 → 2026-06-26 (134 d) |
| `blinkit_ads` | 1,073 | campaign × day | (none) | date | 2026-05-01 → 2026-06-25 |
| `zepto_ads` | 492 | product/campaign × day | (none) | date | 2026-05-19 → 2026-06-25 |
| `swiggy_ads` | 249 | campaign × day | (city_count agg) | date | 2026-05-19 → 2026-06-25 |
| `blinkit_brandfund` | 423 | SKU × city × day (promo) | city | date | 2026-05-01 → 2026-06-25 |
| `swiggy_brandfund` | 411 | SKU × city × day (promo) | city | date | 2026-05-01 → 2026-05-19 |
| `zepto_brandfund` | 44 | promo × SKU × city | city | date | 2026-05-10 → 2026-06-21 |

**Dashboard capability (blinkit / swiggy / zepto):**

| Leaf | blinkit | swiggy | zepto | Notes |
|---|---|---|---|---|
| `stats` | ✅ | ✅ | ✅ | only `inventory` count populated; sells/openPOs/activeTrucks = 0 |
| `primary` | ✅ | ✅ | ✅ | from `master_po` (PO fulfilment) |
| `secondary` | ✅ | ✅ | ✅ | from SecMaster (sell-out summary + trend) |
| `secondary-years` | ✅ | ✅ | ✅ | tiny: list of years with data |
| `secondary-monthly` | ⛔ 400 | ⛔ 400 | ⛔ 400 | "Month..." param-gated |
| `month-targets` | ✅ | ✅ | ✅ | B2B target vs done |
| `primary-month-targets` | ✅ | ✅ | ✅ | |
| `month-on-month-sale` | ⛔ 400 | ⛔ 400 | ⛔ 400 | gated (live only BB/FKG per lead) |
| `soh-doh` | ✅ | ✅ | ✅ | **availability core** |
| `region-doh` | ⛔ **404** | ✅ | ✅ | **LIVE only SWG/ZEP** |
| `drr` | ✅ | ✅ | ✅ | **availability core** |
| `pendency` | ✅ | ✅ | ✅ | PO fulfilment aging |
| `pos` | ✅(empty) | ✅(empty) | ✅(empty) | returns `{data:[],count:0}` |
| `inventory-match` | ✅(`{match:null}`) | ✅ | ✅ | stub / no payload |
| `ads` | ⛔ 400 | ⛔ 400 | ⛔ 400 | **"Amazon Ads Dashboard…"** — Amazon-only shaped dashboard |
| `price` | ⛔ 400 | ⛔ 400 | ⛔ 400 | "Amazon Price…" — Amazon-only |
| `coupon` / `comparison` / `marketplace` | ⛔ 400 | ⛔ 400 | ⛔ 400 | Amazon-only |

> **Raw-table-present-but-no-dashboard distinction (important):** the `*_ads` and `*_brandfund`
> raw warehouse tables **exist and are populated** for all three q-comm platforms, but the shaped
> `ads-dashboard` / `price-dashboard` leaves are **gated to Amazon only** (the 400 literally says
> *"Amazon Ads Dashboard"*). So q-comm ad/promo data is queryable **only via `tables data
> <table>`**, never via a platform dashboard. Any q-comm ad cockpit must be built directly on the
> raw tables.

---

## 1. SELL-OUT (`*Sec`) — the daily consumer-offtake tables

These record **units sold to the end consumer** (secondary sell-out), the opposite end from
primary PO fulfilment. They are the **DRR / velocity numerator**.

### 1a. `swiggySec` — 491,250 rows — **the biggest table in the warehouse**

**Grain:** one **SKU (`ITEM_CODE`) × dark-store (`STORE_ID`) × day (`ORDERED_DATE`)**.
Cardinality drivers: **1,282 STORE_IDs · 136 CITY · 555 AREA_NAME · 31 ITEM_CODE · 320 days**.
(Theoretical max ≈ 1282×31×320 ≈ 12.7M; actual 491k ⇒ sparse — only store/SKU/days with a sale.)

| Column | Type | Meaning | Example |
|---|---|---|---|
| `id` | int | surrogate PK | 1791834 |
| `BRAND` | str | brand (single value: `jivo`) | jivo |
| `ORDERED_DATE` | date | sale day (the day grain) | 2026-06-07 |
| `CITY` | str | city (136 distinct) | Vizag |
| `AREA_NAME` | str | locality of the dark store (555) | dwarka nagar |
| `STORE_ID` | str | **dark-store id — the cardinality driver** (1,282) | 1382440 |
| `L1/L2/L3_CATEGORY` | str | taxonomy (L1=2 vals: edible oils & ghee / …) | rice bran oil |
| `PRODUCT_NAME` | str | platform product title (27) | jivo ricebran oil 1l |
| `VARIANT` | str | pack size text | 1 ltr |
| `ITEM_CODE` | str | **Swiggy listing id → master.format_sku_code** (31) | 958164 |
| `COMBO` | str | Yes/No — is this a combo line | No |
| `COMBO_ITEM_CODE` | str/null | combo's code when COMBO=Yes | null |
| `COMBO_UNITS_SOLD` | num/null | combo units | null |
| `BASE_MRP` | float | MRP at sale | 285.0 |
| `UNITS_SOLD` | int | units sold (store×SKU×day) | 3 |
| `GMV` | float | gross merchandise value ₹ | 855.0 |

**Join keys:** `ITEM_CODE` → master `format_sku_code` (format=SWIGGY) → `sku_sap_code` / `item`.
No FC join (FCs are primary-side; q-comm geography is its own STORE_ID/CITY space).

### 1b. `blinkitSec` — 128,749 rows

**Grain:** **SKU (`item_id`) × city (`city_id`) × day (`date`)**. NO store column → city grain.
Drivers: **9 item_id · 187 city_id / 198 city_name · 892 days** (longest history: back to 2024-01-01).

| Column | Meaning | Example |
|---|---|---|
| `item_id` | Blinkit listing id → master.format_sku_code (only **9** SKUs tracked) | 10048295 |
| `item_name` | product title | Jivo Cold Pressed Canola Oil (5 l)(Pack) |
| `manufacturer_id` / `manufacturer_name` | 176 / Jivo Wellness Pvt. Ltd. | |
| `city_id` / `city_name` | geography (city grain) | 110 / Mohali |
| `category` | single value | Dry Fruits, Masala & Oil |
| `date` | sale day | 2026-02-01 |
| `qty_sold` | units sold | 1 |
| `mrp` | MRP | 1650 |
| `id` | PK | 121301 |
> No GMV column (compute qty_sold × mrp as proxy).

### 1c. `zeptoSec` — 43,471 rows

**Grain:** **SKU (`SKU Number`) × city (`City`) × day (`Date`)**. City grain. 67 cities, 268 days
(from 2025-10-01). Column names contain spaces.

| Column | Meaning | Example |
|---|---|---|
| `SKU Number` | Zepto SKU **UUID** → master.format_sku_code | 883a2716-…-13e49ce3b01f |
| `SKU Name` | product title | Jivo Jump Energy Drink - Sugar Free 200.0 MILLILITRE |
| `EAN` | barcode (stored in sci-notation text ⚠) | 8.91E+12 |
| `SKU Category` / `SKU Sub Category` | taxonomy | Cold Drinks & Juices / Energy Drink |
| `Brand Name` / `Manufacturer Name` / `Manufacturer ID` | Jivo / Jivo Wellness Pvt. Ltd. / UUID | |
| `City` | geography (city grain) | Kurukshetra |
| `Sales (Qty) - Units` | units sold | 1 |
| `MRP` | MRP | 60.0 |
| `Gross Merchandise Value` | GMV ₹ | 60.0 |
| `id` | PK | 2172 |

> **Gotcha:** `tables distinct zeptoSec "SKU Number"` (and other space-named text columns) returns
> empty `values` server-side even though data exists — only single-word columns (`City`, `Date`)
> distinct cleanly. EAN stored as `8.91E+12` text → **not** a reliable join key; use `SKU Number`.

---

## 2. INVENTORY — daily stock snapshots (SOH source)

These are the **SOH (stock-on-hand) numerator** for DOH. They are daily snapshots
(min 2026-01-01, max 2026-06-26 for all three).

### 2a. `swiggy_inventory` — 63,699 rows — the richest inventory feed

**Grain:** **SKU (`sku_code`) × facility (`facility_name`) × day (`inventory_date`)**.
32 SKUs · 41 facilities · 22 cities · 120 days. Carries platform-computed DOH + lost-sales.

| Column | Meaning | Example |
|---|---|---|
| `inventory_date` | snapshot day | 2026-01-01 |
| `uploaded_at` | ingest timestamp | 2026-01-01T12:12:09Z |
| `storage_type` | cold-chain dim: `Ambient` / `` / `StorageType` (dirty header rows ⚠) | Ambient |
| `facility_name` | warehouse (41) | AHM DELHIVERY |
| `city` | facility city (22) | AHMEDABAD |
| `sku_code` | Swiggy listing id → format_sku_code | 15685 |
| `sku_description` | title | Jivo Canola Cold Press Edible Oil 1.0 ltr |
| `l1` / `l2` / `business_category` | taxonomy | Edible Oils and Ghee / Edible Oils / Cooking Essentials |
| `shelf_life_days` | shelf life | 730 |
| `days_on_hand` | **platform-provided DOH** (distinct from dashboard-computed DOH) | 4 |
| `potential_gmv_loss` | ₹ lost-sales estimate when OOS | 7350.0 |
| `open_pos` | open PO ref(s) | "174751" |
| `open_po_quantity` | inbound units on open PO | 20 |
| `warehouse_qty_available` | **SOH units** | 12 |

> **DOH note:** this table has its *own* `days_on_hand` field (platform's number), **but the
> `soh-doh`/`drr`/`region-doh` dashboards recompute DOH themselves** as
> `current SOH ÷ DRR` (see §3). The dashboards source SOH from `all_platform_inventory`
> (the unioned inventory feed) and DRR from `SecMaster` — they do **not** read this column.

### 2b. `zepto_inventory` — 31,955 rows

**Grain:** **SKU (`sku_code`) × city × day**. 51 SKUs · 68 cities · 136 days. **City-level SOH**
→ this is what enables Zepto `region-doh`.

| Column | Meaning | Example |
|---|---|---|
| `inventory_date` / `uploaded_at` | snapshot day / ingest | 2026-01-01 |
| `city` | geography (city grain) | Faridabad |
| `sku_code` | Zepto SKU UUID → format_sku_code | 06c8f55b-… |
| `sku_name` / `ean` | title / barcode (sci-notation ⚠) | …1.0 LITER / 8.908E+12 |
| `sku_category` / `sku_sub_category` | taxonomy | Atta, Rice, Oil & Dals / Oil |
| `brand_name` / `manufacturer_name` / `manufacturer_id` | Jivo / … / UUID | |
| `units` | **SOH units** (only stock field) | 25 |

### 2c. `blinkit_inventory` — 27,069 rows

**Grain:** **item (`item_id`) × backend facility × day**. 10 items · 68 backend facilities ·
134 days. **Facility/warehouse-level**, split backend vs frontend.

| Column | Meaning | Example |
|---|---|---|
| `inventory_date` | snapshot day | 2026-01-01 |
| `raw_created_at` | source date (DD-MM-YYYY string ⚠) | 31-12-2025 |
| `uploaded_at` | ingest ts | 2026-01-01T11:58:15Z |
| `backend_facility_id` / `backend_facility_name` | warehouse (68) | 1320 / Ludhiana - Feeder Warehouse |
| `item_id` / `item_name` | Blinkit listing id → format_sku_code | 10150509 |
| `backend_inv_qty` | stock in backend warehouse | 0 |
| `frontend_inv_qty` | stock in front (dark store) | 0 |
| `total_inv_qty` | **total SOH = backend + frontend** | 0 |

> **Why no Blinkit `region-doh`:** Blinkit inventory is keyed by **backend facility**, not city,
> and `blinkitSec` is keyed by **city** — the two don't share a clean geographic key, so the
> city-grain region cross hasn't been built (dashboard = HTTP 404). Swiggy/Zepto both have a
> city dimension on the sales side and a city-joinable inventory feed, so region-doh works.

---

## 3. AVAILABILITY CORE — `soh-doh`, `drr`, `region-doh`

All three live on **all q-comm platforms** *except* `region-doh` (Blinkit 404). All compute
**DOH the same way** (the dashboards say so explicitly):

```
DRR (qty)  = month-to-date units sold  ÷  elapsed_days
DOH        = current-day SOH units     ÷  DRR (qty)
```
> `doh_note`: *"DOH follows the DRR sheet: current SOH units divided by DRR qty."*
> `value_source_note`: *"VALUE and OPS use SecMaster.sales_amt_exc to match the DRR workbook."*
> Source object on every one: `{"sales":"SecMaster","inventory":"all_platform_inventory"}`
> — **except `region-doh`, whose sales source is the RAW `swiggySec`/`zeptoSec` table** (see §3c).

Worked example (Swiggy CANOLA 1L, elapsed_days=25): qty 2406 → drr_qty 2406/25 = **96.24**;
soh 1515 → doh 1515/96.24 = **15.74**. ✔

### 3a. `soh-doh` (item-level availability)

`platform soh-doh blinkit|swiggy|zepto`. Top keys: `requested_date`, `effective_date`,
`max_sales_date`, `month_start`, `elapsed_day`, `available_dates[]` (date→row-count history),
`rows[]`, `total{}`.

`rows[]` grain = **one item (item_head SKU bucket)**:
`{item, quantity, ltr_sold, inventory_item, soh_units, soh_ltr, drr_ltr, drr_units, doh}`.
`total` = same fields summed (Swiggy: soh 79,640u / drr 4,898u/day / **DOH 16.26**).
`available_dates` lets you pick any snapshot day (Swiggy ~719 rows/day at item×?? grain internally).

### 3b. `drr` (velocity dashboard — the DRR workbook)

`platform drr ...`. Filterable by `sales_of` ∈ {ALL, PREMIUM, COMMODITY, OTHER}.
Keys: `daily[]` (per-day ops ₹ + ltr trend), `daily_groups`, `daily_total`, `rows[]`/`items[]`
(per-SKU), `total`/`totals`, `days_in_month`, `elapsed_days`, flags `show_blinkit_drr`,
`show_inventory_drr`, `show_value_column`.

`rows[]`/`items[]` grain = **one item**:
`{item_head, product, item, inventory_item, qty, ltr, value, landing_amt,
drr_qty, drr_ltr, drr_value, cur_day_soh_units, cur_day_soh_ltr, doh}`.
`daily[]` = `{date, display_date, day, ops(₹), ltr}` — the month-to-date sell-out curve.

### 3c. `region-doh` (city breakdown of DOH) — **LIVE ONLY swiggy + zepto**

`platform region-doh swiggy|zepto` (Blinkit → HTTP 404 "not built" — record as capability fact).

**Critical structural difference:** its source is
`{"sales":"<platform>Sec","inventory":"all_platform_inventory"}` — i.e. it reads the **raw
`swiggySec` / `zeptoSec` table directly, NOT SecMaster**, precisely because the raw *Sec table
carries the **city** column that SecMaster (item_head-aggregated) lacks. This is *the* reason
region-doh exists only for swiggy+zepto.

`rows[]` grain = **one city**:
`{city, soh_units, soh_ltr, units_sold, ltr_sold, drr_units, drr_ltr, doh}`.
`total{}` = all-city roll-up (Swiggy DOH 16.26 — matches soh-doh total ✔).
Example Swiggy/Agra: soh 0, units_sold 187, drr 7.48/day, **doh 0.0** (stocked-out city — a
lost-sales hotspot). Zepto/Agra: soh 133, drr 10.64/day, doh 12.5.

> **Cockpit #1 implication:** `region-doh` is the only place DOH is broken down by city, so the
> "which cities are about to stock out" map is a swiggy+zepto-only capability today. For Blinkit,
> the equivalent must be hand-built from `blinkitSec` (city) ⋈ `blinkit_inventory` (facility) — and
> the facility↔city mismatch is the open modelling problem.

---

## 4. PENDENCY — PO fulfilment aging (`platform pendency ...`)

Live all three. **Primary/PO-side**, not sell-out (drawn from `master_po`, joined to platform
listing codes). Answers "how much ordered stock is still undelivered, and where is it stuck."

Top: `totals{pending_units, pending_ltrs, open_units, open_ltrs, open_pos, rows}`,
`max_po_date`/`min_po_date`, then five breakdown arrays — each row carries
`pending_units, pending_ltrs, open_units, open_ltrs, order_value, open_pos`:

- `by_city` — `{city, …}` (Swiggy top: Hyderabad 16,974u / 26 POs)
- `by_warehouse` — `{warehouse, …}`
- `by_sku` — `{sku_code, sku_name, item, …}` — **`sku_code` here = `swiggySec.ITEM_CODE`** (e.g.
  390730 = GROUNDNUT 1L) → joins sell-out to open POs. Useful join.
- `by_distributor` — `{distributor, …}`
- `by_po` — `{po_number, distributor, po_date, po_expiry_date, …}` — line-level aging (po_expiry
  gives the "days to expire" signal).

> Pendency is gated on AMZ/FK (per lead) but **live on all q-comm** — it's the fulfilment side of
> the availability story (stock that's ordered but not yet on shelf).

---

## 5. ADS vs BRAND FUND (the §2 question, in detail)

Two separate spend mechanisms, two separate raw tables each. **No shaped dashboard exists for
either on q-comm** — ads-dashboard/price-dashboard are Amazon-only (§0). Query via `tables data`.

### 5a. `*_ads` = paid advertising performance (CPC/CPM auctions)

Brand **buys visibility**; tables record campaign delivery + efficiency.

- **`blinkit_ads`** (1,073): `campaign_id, campaign_name, direct_qty_sold, indirect_qty_sold,
  ad_spent, direct_gmv, indirect_gmv, impression`. (direct = attributed to the ad's own SKU;
  indirect = halo on other SKUs.)
- **`zepto_ads`** (492): richest — `product_id, campaign_id, brand_id, atc, clicks, cpc, cpm, ctr,
  impressions, orders, revenue, roas, robas, same_skus, other_skus, spend`. (robas = return on
  brand ad spend; same/other_skus = self vs halo orders.)
- **`swiggy_ads`** (249): campaign-level w/ status & bidding — `campaign_status, bidding_type
  (CPM/…), budget_type, ecpm, ecpc, total_impressions, total_budget, total_budget_burnt,
  total_clicks, total_ctr, total_a2c, a2c_rate, total_gmv, total_conversions, total_roi,
  total_direct_gmv_7/14_days, total_direct_roi_7/14_days`. Carries `city_count`/`product_count`
  (campaign spans N cities — not a per-city row).

### 5b. `*_brandfund` = trade co-investment on price/promotions

Brand **funds discounts/claims** the platform runs — *not* an ad auction. This is promo/markdown
money + the claim the brand reimburses the platform.

- **`zepto_brandfund`** (44): the clearest promo schema — `promo_unique_id, product_type
  (SINGLE/bundle), campaign_id/name, event_type (BAU), type (Off), promo_percentage, promo_inr,
  promo_claim_amt, claim, qty, mrp, gst, cess, margin, is_gst_adjusted/is_cess_adjusted/
  is_margin_adjusted, is_return, vendor_code, active_from/active_till`. → a **promo claim ledger**.
- **`blinkit_brandfund`** (423): `item_id, p_type, offer_type, multiplier, item_mrp,
  brandfund_absolute_value, brandfund_absolute_input_value, brandfund_percentage_value, qty_sold,
  total_brand_fund, mrp_gmv, l0/l1/l2_category, system_sheet_id, upload_source, user_email`.
  `total_brand_fund` = the funded amount; tied to a sheet upload (system_sheet_id/user_email).
- **`swiggy_brandfund`** (411): mirrors `swiggySec` shape **+ `discount_spend`** —
  `item_code, combo_item_code, brand, city, l1/l2/l3_category, product_name, variant, combo,
  combo_units_sold, base_mrp, units_sold, gmv, discount_spend`. `discount_spend` = the brand-funded
  discount ₹ on those sold units. (City grain; no store.)

**Distinction in one line:** `*_ads` = *spend to be seen* (impressions→clicks→ROAS);
`*_brandfund` = *spend to be cheaper* (promo %/INR, discount_spend, claim amounts). Different KPI
families, never mix them in a single "marketing spend" number without labelling.

---

## 6. PRIMARY / SECONDARY / supporting leaves (brief)

- **`primary`** (`source: master_po`): PO fulfilment funnel per `item_head` —
  `done/missed/pending/dp/order/projection × value/ltrs/qty`, plus `fill_rate_summary`,
  `lead_time_days`, `open_vendor_pending*`, `top_items`, `trends`, `details`. The "did we ship
  what was ordered" view. (Primary-side; complements pendency.)
- **`secondary`** (`source: SecMaster`): sell-out summary per `item_head`
  (`shipped_units, shipped_ltr, shipped_value, per_liter_shpd`) + `sec_trend` (daily order/deliver/
  return curve, in ₹ + ltrs + qty) + `top_items`. The aggregated sibling of the raw `*Sec` tables.
- **`secondary-years`**: `{years:[…]}` — data-availability years (blinkit 2024-26, swiggy/zepto
  2025-26). Useful to bound queries.
- **`month-targets` / `primary-month-targets`**: B2B target vs done per item_head
  (`targets, done_ltrs, done_value, achieved_pct, est_ltr, growth_pct, last_month`).
- **`stats`**: header tiles — only `inventory` (row count of the platform inventory feed)
  is populated; `sells`/`openPOs`/`activeTrucks` = 0 (not wired for q-comm).
- **`pos`**: returns empty (`{data:[],count:0}`) — purchase-order list not populated for q-comm.
- **`inventory-match`**: stub (`{match:null}` / 79b) — no payload.
- **Gated (HTTP 400/404):** `secondary-monthly`, `month-on-month-sale`, `region-doh` (blinkit only),
  `ads`, `price`, `coupon`, `comparison`, `marketplace` — record as capability facts, not bugs.

---

## 7. Join keys & gotchas

**Join map (sell-out / inventory → master products):**
| Platform | *Sec SKU key | inventory SKU key | → master |
|---|---|---|---|
| Swiggy | `ITEM_CODE` (e.g. 958164) | `sku_code` (e.g. 15685) | `format_sku_code` (format=SWIGGY) |
| Blinkit | `item_id` (e.g. 10048295) | `item_id` | `format_sku_code` (format=BLINKIT) |
| Zepto | `SKU Number` (UUID) | `sku_code` (UUID) | `format_sku_code` (format=ZEPTO) |
Then `format_sku_code → sku_sap_code (FG…) → item / item_head / per_unit_value (litres)`.
Pendency `by_sku.sku_code` = the same platform listing code → ties open POs to sell-out velocity.

**Gotchas:**
- **Litres vs units everywhere.** Dashboards carry parallel `*_units`/`*_ltr` (and `*_value` ₹).
  `per_unit_value` in master = litres/unit (house unit). DRR/DOH have both qty and ltr variants.
- **DOH = 0 means stocked-out (not missing).** A city/SKU with sales but soh 0 → doh 0 = active
  lost-sales (e.g. Swiggy/Agra). `swiggy_inventory.potential_gmv_loss` quantifies the ₹ at risk.
- **swiggy_inventory has dirty `storage_type` values** (`""`, `"StorageType"` header bleed).
- **EAN stored in sci-notation text** (`8.91E+12`) in zeptoSec/zepto_inventory — do not join on it.
- **Zepto `tables distinct` returns empty for space-named text columns** (server-side quirk) — works
  for `City`/`Date`; use `tables data --page-size N` if you need those value sets.
- **blinkit_inventory.raw_created_at is DD-MM-YYYY string**; use `inventory_date` for joins.
- **Two DOH numbers exist**: platform-provided (`swiggy_inventory.days_on_hand`) vs
  dashboard-computed (SOH/DRR). The cockpit uses the **computed** one; they will differ.
- **Date coverage differs wildly**: blinkitSec back to 2024-01; zeptoSec only from 2025-10;
  inventory feeds all start 2026-01-01 — so DOH history is bounded by inventory, not sales.
- **region-doh & soh-doh totals agree** (good cross-check; both = 16.26 DOH for Swiggy).

---

## 8. Exact commands that surface each business question

```bash
P=~/go/bin/jivo-ecom-pp-cli
# Why swiggySec is huge — the store dimension:
$P tables distinct swiggySec STORE_ID --json 2>/dev/null | jq '.results.values|length'   # 1282
# Availability core (item-level DOH) per platform:
$P platform soh-doh swiggy  --data-source live --json 2>/dev/null | jq '.results.total'
$P platform drr     swiggy  --data-source live --json 2>/dev/null | jq '.results.rows[]|{item,doh}'
# City-level DOH (swiggy+zepto ONLY) — the lost-sales map:
$P platform region-doh zepto --data-source live --json 2>/dev/null | jq '.results.rows[]|select(.doh<3)'
# Fulfilment aging:
$P platform pendency swiggy --data-source live --json 2>/dev/null | jq '.results.by_sku[0]'
# Ads vs brandfund (no dashboard — raw tables only):
$P tables data zepto_ads        --page-size 3 --json 2>/dev/null | jq '.results'
$P tables data swiggy_brandfund --page-size 3 --json 2>/dev/null | jq '.results'
```
