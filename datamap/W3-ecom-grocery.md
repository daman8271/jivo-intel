# W3 — E-COM / GROCERY cluster (BigBasket · Flipkart · Flipkart Grocery · JioMart) + empty shells (Citymall · Zomato)

The **marketplace / grocery** side of the warehouse. Unlike the q-comm cluster (W2), these
platforms are mostly **listing-marketplaces** (sell to end-customers via national listings) plus
two **PO-driven distributor channels** (Flipkart Grocery, and the empty-shell Citymall/Zomato).
This slice also owns the **Month-on-Month sale** view (unique to BigBasket + FK-Grocery) and the
two **empty-table platforms**.

Data as of recon **2026-06-26**. Latest data month **June 2026**. All reads are read-only via
`jivo-ecom-pp-cli <cmd> --data-source live --json 2>/dev/null | jq …`.

---

## TL;DR — headline answers to the 5 assigned questions

1. **`flipkartSec` vs `flipkart_secondary_all`** — same underlying Flipkart sell-out feed, **same
   `id` space** (a row with `id=77244` appears in both, identical base columns). The difference is
   **enrichment + history**:
   - `flipkartSec` (19,421) = **raw Flipkart-native export** only (the 17 columns Flipkart hands
     over: Product Id / SKU ID / Category / GMV / Final Sale Units…). Rolling **recent window**
     (current data ≈ **2026-02-27 → 2026-06-?**). **Not referenced by any dashboard.** Staging/mirror.
   - `flipkart_secondary_all` (21,430) = **canonical, JIVO-enriched, all-history** table. It is
     `flipkartSec` + **16 derived columns** (`item_head`, `mapped_category`, `sub_category`,
     `per_ltr`, `ltr_ordered`, `ltr_sold`, `cancellation_ltr`, `return_ltr`, `month`, `year`,
     `item`, `real_date`, `bank_settlement`, `total_settlement_value`, `packaging_cost_ltr`,
     `total_packaging_cost`), renames `Category`→`Platform Category`, and spans **2025 + 2026 (all
     12 months)**. **Both the `secondary` (sec-dashboard) and `secondary-monthly` dashboards read
     `source: "flipkart_secondary_all"`.** → **`flipkart_secondary_all` is canonical.**

2. **`flipkart_grocery_master`** (4,054) is a **sell-out (secondary) table mislabeled "master"** —
   it is NOT a SKU master. One row = one **SKU × day** sell-out line with `qty`, `ltr_sold`,
   `sale_amt_inclusive/exclusive`, already JIVO-enriched (`item`, `item_head`, `category`,
   `sub_category`, `per_ltr`). It is the `source` for both FK-Grocery `secondary` and
   `month-on-month-sale`. The **separate empty `fk_grocery` (0 rows)** is a stub/placeholder table
   that was never populated — FK-Grocery sell-out actually lands in `flipkart_grocery_master`.

3. **JioMart is sell-out + inventory only; every sell-in / target / DRR / SOH-DOH / sec dashboard
   is gated.** `jiomartSec` (9,434) and `jiomart_inventory` (2,223) hold data, and `stats` /
   `secondary-years` / `pos` / `inventory-match` respond — but `primary`, `primary-month-targets`,
   `month-targets`, `month-on-month-sale`, `soh-doh`, `drr`, **and even `secondary` (sec-dashboard)
   are all HTTP-400 gated**. JioMart has **no PO/sell-in feed** here (stats `openPOs: 0`,
   no rows in `master_po`), so all primary/target dashboards are "out of scope". The only way to
   read JioMart sell-out is the **raw `tables data jiomartSec`** — there is no shaped dashboard for it.

4. **`month-on-month-sale`** is **LIVE only for `bigbasket` + `flipkart_grocery`** (every other
   platform returns `HTTP 400: "Month On Month Sale is available only for Big Basket and Flipkart
   Grocery."`). It is a **liters-by-sub-category MoM comparison** (current month vs previous 4
   months) with month-end projection and target tracking. Sourced from `SecMaster` (BB) /
   `flipkart_grocery_master` (FKG).

5. **BigBasket geography is CITY-level, not pincode.** Both `bigbasketSec` (`source_city_name`) and
   `bigbasket_inventory` (`city`) carry a **city** dimension (~31–46 BB cities incl. "…Rural"
   splits) — **no pincode column**. (The only pincode-carrying table in this whole slice is
   `jiomartSec`, which has `BILLING_PINCODE` / `DELIVERY_PINCODE`.) So BB "national-price /
   pincode-serviced" is **not** observable from the warehouse at pincode grain — the stored grain
   is SKU × city × day.

---

## 0. Slice inventory (14 tables) + capability map

| Table | Rows | Grain (one row = ) | Geo dim | Date col | Coverage |
|---|---|---|---|---|---|
| `bigbasketSec` | 17,036 | SKU × **city** × day | `source_city_name` | `date_range` | 2024-12-31 → 2026-06-25 (219 d) |
| `bigbasket_inventory` | 34,816 | SKU × **city** × day | `city` | `inventory_date` | 2026-01-01 → 2026-06-26 (127 d) |
| `bigbasket_ads` | 40 | campaign × product × day | (none) | `date` | 2026-04-30 → 2026-06-10 (5 d, sparse) |
| `flipkartSec` | 19,421 | SKU × location × day (raw) | `Location Id` | `Order Date` | ≈2026-02-27 → 2026-06 (rolling) |
| `flipkart_secondary_all` | 21,430 | SKU × location × day (**enriched, all-hist**) | `Location Id` | `Order Date`/`real_date` | 2025 + 2026 (all 12 mo) |
| `flipkart_ads` | 7,965 | campaign × day | (none) | `date` | 2026-05-01 → 2026-06-21 (32 d) |
| `flipkart_grocery_master` | 4,054 | SKU × day (sell-out) | (none) | `real_date` | 2025-12-10 → 2026-06-24 (197 d) |
| `jiomartSec` | 9,434 | **order-item line** (txn) | `DELIVERY_PINCODE`/`STATE` | `ORDER_DATE` | 2025-07-31 → **2026-04-15** (stale) |
| `jiomart_inventory` | 2,223 | SKU × RFC × day | `rfc_name` | `inventory_date` | 2026-01-01 → 2026-06-22 |
| `citymallSec` | **0** | — (empty shell) | — | — | — |
| `citymall_inventory` | **0** | — (empty shell) | — | — | — |
| `fk_grocery` | **0** | — (empty stub) | — | — | — |
| `zomatoSec` | **0** | — (empty shell) | — | — | — |
| `zomato_inventory` | **0** | — (empty shell) | — | — | — |

**Platform-dashboard capability matrix** (this slice). LIVE = exit 0 with data; ⛔ = HTTP-400 gated
(business rule); 404 = endpoint not built; ∅ = responds but underlying table empty.

| dashboard | bigbasket | flipkart | flipkart_grocery | jiomart | citymall | zomato |
|---|---|---|---|---|---|---|
| `stats` | LIVE | LIVE | LIVE | LIVE | LIVE | LIVE |
| `secondary` (sec) | LIVE | LIVE | LIVE | ⛔ | ⛔ | ⛔ |
| `secondary-monthly` | (n/a) | LIVE | (n/a) | (n/a) | (n/a) | (n/a) |
| `secondary-years` | LIVE | LIVE | LIVE | LIVE(empty) | LIVE(empty) | LIVE(empty) |
| `primary` | LIVE | LIVE | LIVE | ⛔ | LIVE | LIVE |
| `primary-month-targets` | LIVE | LIVE | LIVE | ⛔ | LIVE | LIVE |
| `month-targets` | LIVE | LIVE | LIVE | ⛔ | LIVE | LIVE |
| `month-on-month-sale` | **LIVE** | ⛔ | **LIVE** | ⛔ | ⛔ | ⛔ |
| `soh-doh` | LIVE | ⛔ | ⛔ | ⛔ | ⛔ | ⛔ |
| `drr` | LIVE | LIVE | LIVE | ⛔ | LIVE | LIVE |
| `pos` | LIVE(0) | LIVE(0) | LIVE(1834) | LIVE(0) | LIVE(1433) | LIVE(1423) |
| `pendency` | LIVE | ⛔(not enabled) | LIVE | ⛔(not enabled) | LIVE | LIVE |
| `inventory-match` | LIVE(null) | LIVE(null) | LIVE(null) | LIVE(null) | LIVE(null) | LIVE(null) |
| `region-doh` | 404 | 404 | 404 | 404 | 404 | 404 |

Gating message strings (verbatim, useful as ground truth):
- sec/secondary gated → `"Sec Dashboard is available only for Amazon, Big Basket, Blinkit, Swiggy, Zepto, Flipkart and Flipkart Grocery."` (excludes **jiomart, citymall, zomato**).
- soh-doh gated → `"SOH/DOH Dashboard is available only for Blinkit, Zepto, Swiggy and BigBasket."`
- drr gated → `"DRR Dashboard is available only for Amazon, Blinkit, Zepto, Flipkart and Flipkart Grocery."`
- mom gated → `"Month On Month Sale is available only for Big Basket and Flipkart Grocery."`
- jiomart primary → `"Primary Dashboard is available only for primary sales platforms."`
- jiomart targets → `"Platform 'jiomart' is out of scope for Monthly Targets."` / `"…Primary Monthly Targets."`
- flipkart/jiomart pendency → `"Pendency dashboard is not yet enabled for platform 'X'."`

---

## 1. BigBasket

### 1a. `bigbasketSec` (17,036) — sell-out, SKU × city × day
- **Grain:** one row = one **listing-SKU × BigBasket city × day**. (`brand_name` only ever "Jivo".)
- **Coverage:** `date_range` 2024-12-31 → 2026-06-25 (219 distinct days — *not* contiguous daily;
  BB sends periodic city pulls). Note the column is **named `date_range` but holds a single date.**
- **Geography:** `source_city_name` — BB operational cities incl. "…Rural" splits
  (Agra, Allahabad, Bangalore, Bangalore Rural, Chennai Rural, Gurgaon-Faridabad, Hyderabad Rural…).

| column | type | meaning | example |
|---|---|---|---|
| `id` | int | row id | 1 |
| `date_range` | date(str) | the sell-out **day** (misnamed; single date) | "2026-01-01" |
| `source_city_name` | str | BB city / serviceable region | "Gurgaon" |
| `brand_name` | str | brand — only "Jivo" | "Jivo" |
| `top_slug`/`mid_slug`/`leaf_slug` | str | BB category taxonomy (3 levels) | "foodgrains-oil-masala" / "edible-oils-ghee" / "olive-canola-oils" |
| `source_sku_id` | str | **BB listing id = master `format_sku_code`** | "40166398" |
| `sku_description` | str | BB listing title | "Jivo extra-light-olive-oil 1 L Bottle" |
| `sku_weight` | str | pack size | "1 L", "5 L" |
| `total_quantity` | int | units sold | 9 |
| `total_mrp` | float | gross MRP value | 12150.0 |
| `total_sales` | float | net sale ₹ (post-discount) | 7945.53 |

- **Join:** `source_sku_id` → master `format_sku_code` (format=`BIG BASKET`, 33 listings). NB many
  BB listings have `sku_sap_code = null` in master (un-mapped to SAP FG codes) → expect join gaps.
- **Gotcha:** slug columns are **double-encoded** — both slug-form *and* Title-Case appear in the
  same column (`"foodgrains-oil-masala"` **and** `"Foodgrains, Oil & Masala"`; `"cold-pressed-oil"`
  **and** `"Cold Pressed Oil"`). Two ingestion vintages were merged; normalise before grouping.

### 1b. `bigbasket_inventory` (34,816) — SOH snapshot, SKU × city × day
- **Grain:** one row = listing-SKU × BB city × **inventory_date**. Coverage 2026-01-01 → 2026-06-26
  (127 days). 31 cities (Agra…Visakhapatnam).

| column | type | meaning | example |
|---|---|---|---|
| `inventory_date` | date | snapshot day | "2026-01-01" |
| `uploaded_at` | ts | ingest timestamp | "2026-01-01T12:14:57Z" |
| `city` | str | BB city | "Kolkata" |
| `sku_id` | str | BB listing id = `format_sku_code` | "40335331" |
| `brand_name` | str | "Jivo" | "Jivo" |
| `sku_name` / `sku_description` | str | listing title | "Mojito Healthy Wheatgrass Juice…" |
| `sku_weight` | str | pack size | "200 ml" |
| `sku_pack_type` | str | pack form (**dirty**) | "Bottle"/"Can"/"Jar"/"Pouch"/"Tin"/"&"/""/"1225"/"1225.0" |
| `top/mid/leaf_category_name` | str | BB taxonomy (Title-Case here) | "Beverages" / "Fruit Juices & Drinks" / "Unsweetened, Cold Press" |
| `soh` | int | stock-on-hand **units** | 29 |
| `soh_value` | float | SOH value ₹ | 1450.0 |

- **Gotcha:** `sku_pack_type` is a junk column (empty strings, `&`, numeric `1225.0`, plus real
  values) — don't rely on it; use `sku_weight`. **No DOH column** in the raw table — DOH is computed
  in the `soh-doh`/`drr` dashboards (sell-out ÷ SOH). **No pincode** — city is the finest geo.

### 1c. `bigbasket_ads` (40) — campaign performance (tiny)
- **Grain:** product × campaign × day; only **5 dates** (2026-04-30 → 2026-06-10), `format:"BIGBASKET"`.
- Columns: `product_id`(=listing id), `campaign_id/name`, `ad_spend`, `ad_impressions`, `cpm`,
  `add_to_cart`, `orders_sku`, `ad_revenue`, `roas`, `other_sku_orders`, `same_category_orders`,
  `other_sku_ad_revenue`. Example: spend 8928.54, impressions 3143, roas 3.45.
- **Gotcha:** only 40 rows — BB ads is barely populated; treat as illustrative, not analytical.

### 1d. BigBasket dashboards (shaped)
- **`secondary`** (`source: SecMaster`, max_date 2026-06-24): rolls `bigbasketSec` into
  `details[]` (per item_head × category × sub_category × per_ltr: shipped_value/units/ltr,
  per_liter_shpd) + `summary[]` per item_head with `target`, `drr`, `target_drr`, `estimated_ltr`.
  Note it converts units→**liters** (house unit) which the raw table does not carry.
- **`soh-doh`** (LIVE — one of only 5 platforms): `rows[]` per item with `quantity`, `ltr_sold`,
  `soh_units`, `soh_ltr`, `drr_ltr`, `drr_units`, **`doh`** (days-of-hand); `total` block. Sales
  from `SecMaster`. Inventory effective date carried separately from sales max date.
- **`drr`** (`source: {sales: SecMaster, inventory: all_platform_inventory}`): daily run-rate per
  item with `liters`, `value`, `drr_ltr/qty/value`, `cur_day_soh_ltr`, `doh`. `sales_of_options`
  = ["ALL","PREMIUM","COMMODITY","OTHER"] (filter by item_head). **Inventory joins via the shared
  `all_platform_inventory` table, not `bigbasket_inventory` directly.**
- **`primary`** (`kpi_source: master_po_order_minus_deliver`, mode "DEL MONTH"): sell-in/PO view
  fed from the shared `master_po` table (not from any BB sell-out table). `summary` per item_head:
  done/missed/pending/order ltrs+value, projection, `open_vendor_pending_*`.
- **`pos`** = open-PO line list → **count 0** for BB (no open POs; stats `openPOs:0`).
- **`month-on-month-sale`** → see §5.
- **`inventory-match`** returns `{match: null}` (a SKU-mapping reconciliation stub; null for all
  platforms in this slice).

---

## 2. Flipkart (marketplace)

### 2a. `flipkartSec` (19,421) — RAW Flipkart export (staging)
- **Grain:** SKU × `Location Id` × `Order Date`. Columns are **exactly Flipkart's native export**
  (note the **spaces in column names** — `"Product Id"`, `"SKU ID"`, `"Order Date"`,
  `"Gross Units"`, `"Final Sale Units"`…). Rolling recent window (paged tail = 2026-02-27/28).
- **No JIVO enrichment** (no item_head, no liters, no item). **Not used by any dashboard.**

### 2b. `flipkart_secondary_all` (21,430) — CANONICAL enriched sell-out (all history)
- **Grain:** same SKU × location × day, **same `id` space as `flipkartSec`** (id 77244 in both).
  Spans **year 2025 + 2026, all 12 months** (the all-history superset). `source` for the `secondary`
  and `secondary-monthly` dashboards.

| column | type | meaning | example |
|---|---|---|---|
| `Product Id` | str | **Flipkart FSN = master `format_sku_code`** | "EDOHGP27PXFNHZNZ" |
| `SKU ID` | str | seller SKU string | "Jivo_Extra Virgin_200ml_Pack_Of_3" |
| `Platform Category` | str | FK category (renamed from `Category`) | "Gourmet" |
| `Brand` / `Vertical` | str | "JIVO" / "OtherCookingOil" | |
| `Order Date` | date | order day | "2026-06-01" |
| `Fulfillment Type` | str | FBF vs non-FBF | "NON_FBF" |
| `Location Id` | str | FK fulfilment location | "LOCd11c4c…" |
| `Gross Units` / `GMV` | int/float | gross sold | 2 / 2316.0 |
| `Cancellation Units/Amount`, `Return Units/Amount` | | reversals | |
| `Final Sale Units/Amount` | int/float | net after cancel+return | 2 / 2316.0 |
| **`item_head`** | str | PREMIUM / COMMODITY / OTHER | "PREMIUM" |
| **`mapped_category`** | str | JIVO category dict (noisy — see gotcha) | "OLIVE" |
| **`sub_category`** | str | JIVO sub-cat | "EXTRA VIRGIN" |
| **`per_ltr`** | float | litres per unit | 0.2 |
| **`ltr_ordered`/`ltr_sold`/`cancellation_ltr`/`return_ltr`** | float | unit→litre conversions | 6.0 |
| **`item`** | str | master short SKU name | "EXTRA LIGHT 1L+POMACE 1+1L" |
| **`month`/`year`** | str/int | "JUNE" / 2026 | |
| **`real_date`** | str | dd-mm-yyyy form | "01-06-2026" |
| **`bank_settlement`/`total_settlement_value`** | float/null | FK settlement (mostly null) | null |
| **`packaging_cost_ltr`/`total_packaging_cost`** | float | packaging cost model | 9.875 / 19.75 |

- **Join:** `Product Id` → master `format_sku_code` (format=`FLIPKART`, 281 listings); enriched
  rows also carry `item` directly.
- **Gotcha — `mapped_category` is a shared category dictionary** containing many non-JIVO/non-oil
  noise values (Crypto, Ferrero, TEA, Rice, COFFEE, HONEY, SEEDS…) alongside the real ones
  (OLIVE, MUSTARD, CANOLA, SUNFLOWER, SOYABEAN, BLENDED, GHEE). Filter to JIVO's real categories.
- **Gotcha — `real_date` is dd-mm-yyyy text**, so lexicographic min/max is meaningless; use
  `Order Date` (ISO) or `created_at` for sorting.

### 2c. `flipkart_ads` (7,965) — campaign performance
- **Grain:** campaign × day, 2026-05-01 → 2026-06-21 (`format:"FLIPKART"`).
- Categoricals: `campaign_type` ∈ {PLA, SELLER_PCA}; `campaign_status` ∈ {ABORTED, COMPLETED,
  DAILY_BUDGET_MET, DRAFT, LIVE, PAUSED, TOTAL_BUDGET_MET}; `budgeting_type` ∈ {DAILY_BUDGET,
  TOTAL_BUDGET}. Metrics: `ad_spend`, `views`, `clicks`, `total_converted_units`, `total_revenue`,
  `roi`, `click_through_rate`, `conversion_rate`. **No product_id** — campaign-level only (cannot
  join ads to SKU on Flipkart, unlike `bigbasket_ads`).

### 2d. Flipkart dashboards
- **`secondary`** (`source: flipkart_secondary_all`, max_date 2026-06-24): `details[]` per
  item_head×category×sub_category with order/shipped/return ltr & value, `drr_ltr`, `projection`,
  `return_units_percent`, `per_liter_shpd`; `summary[]` per item_head.
- **`secondary-monthly`** (Flipkart-only in this slice, `source: flipkart_secondary_all`): a
  month-grid — `category_liters` / `sales_liters` / `sales_values` per month (Jan…Dec) + `mom_growth`.
- **`primary`** (`master_po`): PO sell-in. **`soh-doh` ⛔ gated** (FK not in the SOH-DOH allowlist),
  **`pendency` ⛔ "not yet enabled"**, **`month-on-month-sale` ⛔ gated**. `pos` count 0.

---

## 3. Flipkart Grocery (PO-driven distributor channel)

### 3a. `flipkart_grocery_master` (4,054) — sell-out table (NOT a SKU master)
- **Grain:** one row = **SKU × day** sell-out line, already JIVO-enriched. Coverage
  `real_date` 2025-12-10 → 2026-06-24 (197 days; months Dec-2025…Jun-2026). `brand` only "JIVO".
  `source` for FKG `secondary` and `month-on-month-sale`.

| column | type | meaning | example |
|---|---|---|---|
| `date` | str(dd-mm-yyyy) | sell-out day (text form) | "24-06-2026" |
| `real_date` | date(ISO) | sell-out day (use this) | "2026-06-24" |
| `sku_id` | str | **FK FSN = master `format_sku_code`** | "AYDHFFYPB5ERAACU" |
| `brand` | str | "JIVO" | "JIVO" |
| `qty` | float | units sold | 3.0 |
| `per_ltr` / `per_ltr_unit` / `uom` | float/str/str | litres per unit / pack label / unit | 0.0 / "200 MLS" / "MLS" |
| `ltr_sold` | float | litres sold | 0.0 |
| `month` / `year` | int | 6 / 2026 | |
| `item` | str | master short SKU name | "WG APPLE JUICE 200 ML" |
| `landing_rate` / `basic_rate` | float | rate ₹ | 0.0 |
| `sale_amt_inclusive` / `sale_amt_exclusive` | float | sale ₹ incl/excl tax | 0.0 |
| `category` / `sub_category` / `item_head` | str | JIVO taxonomy | "DRINKS" / "APPLE SF" / "OTHER" |

- **Join:** `sku_id` → master `format_sku_code` (format=`FLIPKART GROCERY`, 22 listings); also carries `item`.
- Categoricals: `category` ∈ {BLENDED, CANOLA, DRINKS, MUSTARD, OLIVE, SOYABEAN, SUNFLOWER};
  `uom` ∈ {LTR, MLS}; `per_ltr_unit` ∈ {160 MLS, 1 LTR, 200 MLS, 2 LTR, 4 LTR, 500 MLS, 5 LTR}.
- **Why "master" in the name?** Misnomer — it is the **single consolidated FK-Grocery sell-out
  feed** (a "master" copy across all dates), analogous to `flipkart_secondary_all`. It is NOT a
  product/SKU dimension table. The **empty `fk_grocery` (0 rows)** is a never-populated stub; the
  real data was routed to `flipkart_grocery_master`. So the "two tables" are an artifact of a
  renamed/abandoned table, not two live grains.

### 3b. FK-Grocery dashboards
- **`secondary`** (`source: flipkart_grocery_master`): item_head×category×sub_category shipped
  ltr/value + per_liter_shpd.
- **`primary`** (`kpi_source: master_po_order_minus_deliver`, mode "DEL MONTH"): the **main FKG
  story** — FKG is a **PO channel**. stats `openPOs: 1834`; `summary[]` per item_head with
  done/missed/pending/order/expired/cancelled ltrs+value, projection.
- **`pos`** = **1,834 open-PO line items** (the only platform in this slice with a live PO list
  alongside Citymall/Zomato). Each row is a rich PO line: `po_number`, `po_date`, `po_expiry_date`,
  `delivery_date`, `vendor_name`, `status` (CANCELLED/COMPLETED…), `sku_code`(FSN), `order_qty`,
  `delivered_qty`, `basic_rate`/`landing_rate`, `location`/`city`/`state`, `item`(master),
  `sap_sku_name`, `category`/`sub_category`/`item_head`, `distributor_margin`, `missed/filled_ltrs`,
  `po_month`/`delivery_month`. (Sourced from `master_po` filtered to FLIPKART GROCERY.)
- **`month-on-month-sale` LIVE** → §5. **`soh-doh` ⛔ gated** (FKG not in SOH allowlist). `drr` LIVE.

---

## 4. JioMart (transaction-grain sell-out + RFC inventory; almost all dashboards gated)

### 4a. `jiomartSec` (9,434) — **order-item transaction lines** (finest grain in slice)
- **Grain:** one row = one **order-item line** (`ORDER_ID` × `ORDER_ITEM_ID`) — full tax-invoice
  detail. This is **transaction-level**, unlike the city/SKU-aggregated Sec tables of BB/FK.
- **Coverage:** `ORDER_DATE` 2025-07-31 → **2026-04-15** (259 days). **⚠ STALE** — latest order is
  mid-April 2026, ~2.5 months behind the June-2026 warehouse latest. JioMart sell-out has stopped
  refreshing (consistent with all its sell-out dashboards being gated/abandoned).
- Categoricals: `ORDER_TYPE` ∈ {COD, Prepaid}; `FULFILLMENT_TYPE` only "RFC Shipment";
  `EVENT_TYPE` ∈ {sale, return}; `EVENT_SUB_TYPE` ∈ {sale, BeforeShippingReturn, DoorStepReturn,
  ReturnToOrigin}; `TYPE` ∈ {shipment, return}; `ORDER_STATUS` ∈ {complete, delivered, invoiced,
  pick_up_confirmed, shipment_returned}; `TAX_TYPE` only "GST".

| key columns | meaning | example |
|---|---|---|
| `ORDER_ID`/`ORDER_ITEM_ID` | composite grain | "17660288820195894M"/"59580764" |
| `ORDER_DATE`/`BUYER_INVOICE_DATE` | order / invoice day | "2025-12-18" |
| `EVENT_TYPE`/`EVENT_SUB_TYPE`/`TYPE` | sale vs return classification | "sale" |
| `FSN_PRODUCT_ID` | **JioMart FSN = master `format_sku_code`** | "RVO2FDFNG7" |
| `SKU` / `PRODUCT_TITLE` | human SKU label / title | "5 Ltr Canola" / "Jivo Canola Cold Press Oil, 5 Ltr…" |
| `HSN_CODE` | tax HSN | "15141920" |
| `ITEM_QUANTITY` | units | 2 |
| `FULFILLER_NAME` | RFC | "RRL HR JHJ Luhari 3P RFC" |
| `BILLING_PINCODE`/`DELIVERY_PINCODE` | **pincode geography** (unique to this table) | "431005" |
| `BILLING_STATE`/`DELIVERY_STATE`/`SHIPPED_FROM_STATE` | state geo | "Maharashtra" |
| `BUYER_INVOICE_AMOUNT`/`OFFER_PRICE`/`FINAL_INVOICE_AMOUNT`/`TAXABLE_VALUE` | money | 2410.0 |
| `IGST/CGST/SGST_RATE+AMOUNT`, `TCS_*`, `TDS_194O_*` | full GST/TCS/TDS tax breakdown | IGST 5% / 114.76 |
| `SELLER_COUPON_CODE`/`SELLER_COUPON_AMOUNT` | seller coupon | null / 0.0 |

- **Join:** `FSN_PRODUCT_ID` → master `format_sku_code` (format=`JIO MART`, 22 listings). Verified:
  sample FSN `RVO2FDFNG7` → master `CANOLA 5L` / `FG0000004` (exact match). `SKU`/`PRODUCT_TITLE`
  are free-text, not join keys.
- **Gotchas:** (1) **stale to 2026-04-15**; (2) `DELIVERY_STATE` has **case-duplicate values**
  ("Andhra Pradesh" vs "ANDHRA PRADESH", "Bihar" vs "BIHAR" …) — uppercase before grouping;
  (3) returns are inline (`EVENT_TYPE=return`) — net sales = sale minus return rows;
  (4) it is the **only** sell-out table in the slice with **invoice-level tax + pincode** detail
  (good for tax/geo analysis, heavy for volume analysis).

### 4b. `jiomart_inventory` (2,223) — RFC inventory snapshot
- **Grain:** SKU × `rfc_name` × `inventory_date`. Coverage 2026-01-01 → 2026-06-22 (fresher than
  jiomartSec). Only **2 RFCs**: `RFC0VIHAX01`, `RRL HR JHJ Luhari 3P RFC`. 23 distinct sku_ids.

| column | meaning | example |
|---|---|---|
| `inventory_date`/`last_updated_at`/`uploaded_at` | snapshot timing | "2026-01-01" |
| `rfc_name` | regional FC | "RRL HR JHJ Luhari 3P RFC" |
| `sku_id` / `title` | **JioMart FSN = `format_sku_code`** / title | "RVMGQM8OIG" / "Jivo Extra Light Olive Oil 2 Ltr" |
| `category` / `product_status` | category / status (**dirty**) | "Edible Oils" / "Active" |
| `total_sellable_inv` / `total_unsellable_inv` | good vs bad stock | 272 / 5 |
| `fc_dmg_inv`/`lsp_dmg_inv`/`cust_dmg_inv`/`recvd_dmg`/`expired_inv`/`other_unsellable_inv` | damage breakdown | 0/5/0/0/0/0 |
| `mtd_fwd_intransit`/`mtd_delvd_cust`/`mtd_ret_intransit`/`mtd_order_count` | month-to-date flow | 0/44/3/49 |

- **Gotcha — dirty categoricals:** `category` includes a full SANO product title
  ("Sano Daily Cooking Pomace Olive Oil |1 Litre…") leaking in; `product_status` contains
  "Edible Oils" (a category) as a value; `sku_id` distinct set includes the RFC name
  "RRL HR JHJ Luhari 3P RFC" leaked into the SKU column. Clean before joining/grouping.

### 4c. JioMart dashboards
- **Only `stats` / `secondary-years`(empty) / `pos`(0) / `inventory-match`(null) respond.**
  Everything analytic is **⛔ gated**: `secondary`(sec), `drr`, `soh-doh`, `month-on-month-sale`,
  `primary`, `primary-month-targets`, `month-targets`, `pendency`. `region-doh` 404.
- **Capability fact:** JioMart is a **read-the-raw-table-only** platform — no shaped sell-out view
  exists. To analyse JioMart sell-out you must query `tables data jiomartSec` directly and do your
  own item_head/liters mapping via `master products` (format=JIO MART). `stats` confirms
  `inventory:2223, sells:0, openPOs:0` (no PO/sell-in feed → primary/target gating).

---

## 5. `month-on-month-sale` — the BB+FKG-only MoM view

LIVE only for **bigbasket** (`source: SecMaster`) and **flipkart_grocery** (`source:
flipkart_grocery_master`). Shape (identical for both):

- **Header:** `dashboard_title`, `format`, `month`/`year`, `max_date`, `elapsed_days`,
  `days_in_month`, `projection_days`, `defaulted_to_latest`, `estimation_note`.
- **`comparison_months[]`:** current + previous_1…previous_4 (5-month window), each `{key, month,
  year, label}` (e.g. JUNE / MAY / APRIL / MARCH / FEBRUARY).
- **`target_summary[]`:** per item_head target (PREMIUM/COMMODITY/TOTAL).
- **`groups[]`:** one per `sub_category`, each with `rows[]` and a `total`. Each row:
  `{sub_category, item, item_head, target, current_done_ltr, estimated_ltr, previous_1_ltr,
  previous_2_ltr, previous_3_ltr, previous_4_ltr}` — everything in **litres**.
- **`grand_total`:** `{target, current_done_ltr, estimated_ltr, previous_1..4_ltr}`.
- **Projection logic:** `estimated_ltr ≈ current_done_ltr × days_in_month / elapsed_days`
  (BB June: 9294 done × 30/25 = 11152.8; matches). Lets ops project month-end vs target and the
  trailing-4-month trend per sub-category. This is the unique "are we pacing to target?" view those
  two platforms expose (both have a clean per-day liters sell-out feed + an item_head target table).

---

## 6. Empty platforms — Citymall & Zomato (capability facts)

**All four/five tables are confirmed 0 rows** (`tables count`): `citymallSec`=0,
`citymall_inventory`=0, `zomatoSec`=0, `zomato_inventory`=0, plus `fk_grocery`=0.

But "empty platform" needs nuance — **the sell-out/inventory tables are empty, yet the PO/sell-in
side is live** (because primary/pos read the shared `master_po`, not these tables):

| | Citymall | Zomato |
|---|---|---|
| Sec table | citymallSec **0** | zomatoSec **0** |
| Inventory table | citymall_inventory **0** | zomato_inventory **0** |
| `stats` | inventory 0, openPOs **1433** | inventory 0, openPOs **1423** |
| `pos` (open-PO list) | **1433 rows** (e.g. PO-1358660, MUSTARD 5L, Dadri) | **1423 rows** (e.g. ZHPDL26-PO-…, JIVO POMACE 5L) |
| `primary` | LIVE, `kpi_source: master_po_order_minus_deliver` | LIVE, `kpi_source: master_po` |
| `primary-month-targets`/`month-targets` | LIVE | LIVE |
| `pendency` | LIVE | LIVE |
| `secondary`(sec)/`drr` shaped | drr LIVE (master_po based); **sec ⛔ gated** | same |
| `secondary-years` | LIVE → `{years: [], errors: []}` (empty) | LIVE → empty |
| `inventory-match` | LIVE → `{match: null}` | LIVE → `{match: null}` |
| `soh-doh`/`month-on-month-sale` | ⛔ gated | ⛔ gated |
| master `products` listings | **16** (CITY MALL) | **0** (no ZOMATO listings at all) |

**Capability summary:** Citymall and Zomato are **distributor/PO channels with no sell-out or
inventory feed wired in** — they are "secondary-empty, primary-live". `stats`, `secondary-years`,
`pos`, `inventory-match` respond **structurally** but the sell-out/SOH content is empty (years=[],
match=null, sells=0). Zomato is the **most genuinely empty** (0 master listings + 0 table rows; it
exists only as PO rows in `master_po`); Citymall at least has 16 SKU listings in the master.

---

## 7. Cross-cutting notes for the lead

- **Raw vs enriched / canonical pairs** in this slice:
  `flipkartSec`(raw, recent) → **`flipkart_secondary_all`**(enriched, all-history, **canonical**);
  FK-Grocery sell-out → **`flipkart_grocery_master`** (the empty `fk_grocery` is a dead stub).
  Dashboards never read the raw `flipkartSec`.
- **Dashboard data sources** (who feeds what): BB sec/soh-doh/mom = `SecMaster`; BB drr inventory =
  `all_platform_inventory`; FK sec/monthly = `flipkart_secondary_all`; FKG sec/mom =
  `flipkart_grocery_master`; **all `primary`/`pos` across BB/FK/FKG/Citymall/Zomato = `master_po`**
  (shared PO table — W-owner of `master_po` should note these 5 platforms read it).
- **Liters is the house unit** everywhere (`ltr_sold`, `*_ltr`, `per_ltr`, targets). Raw BB/FK/Jio
  Sec tables store **units only**; the dashboards & enriched tables derive liters via master `per_ltr`.
- **Join keys confirmed:** `format_sku_code` = BB `source_sku_id`/`sku_id`, FK `Product Id`,
  FKG `sku_id`, JioMart `FSN_PRODUCT_ID`/inventory `sku_id`. Enriched tables also carry `item`.
- **Data-quality landmines:** BB slugs double-encoded (slug + Title-Case); BB `sku_pack_type` junk;
  FK `mapped_category` polluted with non-JIVO dictionary entries; JioMart `DELIVERY_STATE`
  case-duplicated; JioMart inventory `category`/`product_status`/`sku_id` cross-contaminated.
- **Staleness:** `jiomartSec` stops at **2026-04-15** (vs June-2026 elsewhere) — flag any JioMart
  sell-out number as ~2.5 months old.
- **`inventory-match` returns `null` for every platform** in this slice — appears to be an
  unbuilt/placeholder reconciliation endpoint, not a per-platform capability difference.
