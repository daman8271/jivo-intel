# Inventory · JM Inventory · Marketing · Distributor (W3)

*Domain owner: W3. Source of truth: `/root/jivo-intel/store/versioned/` (tables `.changelog.jsonl` = full rows;
dashboards `.changelog.jsonl` `.doc` = the app's own computed page views). All figures below are computed from the
latest-version-per-key rows in the SSOT; inventory snapshots use the latest `inventory_date` per platform; the
marketing window is ~May–Jun 2026.*

This file covers four workspace pages and the supply-chain "stock + spend" layer of the app:

| Page | One-line | Primary data |
|------|----------|--------------|
| **Inventory** | What's sitting in the **platforms'** warehouses/dark-stores (stock at the edge) | `all_platform_inventory` + per-platform `*_inventory` tables; `inventory-charts`, `soh-doh`, `region-doh` |
| **JM Inventory** | What's in **OUR (Jivo Mart) own** warehouse — the buffer between supply and platforms | *(no dedicated extracted table — see §2.3; derived from the supply chain + open-PO signals)* |
| **Marketing** | Ad spend, ROAS/ACOS, coupons, brand-fund, and competitive price/margin | `*_ads`, `*_brandfund`, `amazon_coupon`, `amazon_price_data`; `ads/coupon/marketplace/comparison/price` (amazon) |
| **Distributor** | Vendor (distributor) performance — who fills our POs and how well | `master_po.vendor_new` / `vendor_name`; `fulfilment-health` |

---

## 0. The mental model (read this first)

The value chain (from Home KPI cards, Jun 2026, all in **litres**, price rising down the chain):

```
Jivo Wellness (parent makes oil)  →  JM Primary (Jivo Mart warehouse)  →  Primary (ship to platforms)  →  Secondary (sold to consumers)
   Billing 811,616 L @ ₹180/L          672,739 L @ ₹192.6/L                484,975 L @ ₹210.3/L              560,048 L @ ₹218.2/L
```

My four pages instrument the **stock and demand-generation** that sit *around* that flow:

- **JM Inventory** = the stock JM is holding in its own warehouse *between* "JM Primary received" and "Primary shipped to platforms."
- **Inventory** = the stock that has already moved out to the **platforms** and is sitting at their FCs / feeder warehouses / dark-stores, waiting to sell-through (Secondary).
- **Marketing** = the spend that *pulls* that platform stock through to consumers (spend → Secondary sell-out), plus the price/margin guardrails.
- **Distributor** = the vendors who actually *fulfil* the platforms' purchase orders on JM's behalf (Distributor → Primary fill-rate).

So: **Distributor fills Primary → stock lands as platform Inventory → Marketing depletes it as Secondary → DOH/SOH tells you when to re-order.** That loop is the spine of this whole domain.

---

## 1. INVENTORY (stock at the platforms)

### 1.1 Purpose
Show, per platform → city/facility → SKU, **how much Jivo stock is physically at the platform right now** and **how fast it is depleting**, so the team can spot stock-outs (lost sales) and overstock/expiry (locked cash). This is the "right-hand" inventory: stock that has already left JM and is at the edge.

### 1.2 Key metrics / KPIs (with real numbers)
- **SOH — Stock On Hand** (units and litres). e.g. Amazon SOH (eff. 2026-06-24) = **258,041 L / 111,880 units** sellable.
- **DRR — Daily Run Rate** (units/L sold per day, month-to-date). Amazon DRR = **8,137 L/day**.
- **DOH — Days On Hand = SOH ÷ DRR** (how many days the current stock lasts at current sell-rate). Amazon overall **DOH = 21.4 days**. Low DOH = imminent stock-out; high DOH = overstock/expiry risk.
- **Potential GMV loss** (Swiggy) — value of demand that can't be served from current stock. Swiggy latest snapshot (2026-06-26): **₹16,961,734** across 719 SKU-rows, with **198 SKU-rows at DOH < 7 days** (median DOH 26). *Verified it is a **per-snapshot** figure (₹13.7M on 06-24 → ₹16.96M on 06-25/26), and **43% of it is concentrated in the DOH<7 (imminent-stock-out) rows** — confirming the metric = unserved demand driven by low stock, i.e. the cost of stock-outs.*
- **Sellable vs Unsellable / Aged-90-day** (Amazon vendor view): sellable on-hand **₹54.9M**, unsellable **₹0.25M**, aged-90-day sellable **₹0.53M**, unfilled customer-ordered units **7,323**, open-PO qty **165,351**.
- **Damages / Expired** (JioMart): splits `fc_dmg / lsp_dmg / cust_dmg / expired / other_unsellable`.
- **Expiry exposure** — `platform-expiry-alerts` dashboard, JUNE 2026: City Mall **51,756 L / 5 POs**, Amazon **15,794 L / 4 POs**, Swiggy **12,326 L / 31 POs** flagged as expiry-risk.

### 1.3 Data behind it
- **`all_platform_inventory`** (174,155 rows, Jan–Jun 2026) — the **unified/normalized** platform inventory: `inventory_date, sku_code, item, item_head, brand, soh_unit, soh_ltr, location, format`. Latest snapshot (2026-06-26) = 1,742 rows across SWIGGY (104,000 L), BLINKIT (85,966 L), ZEPTO (76,373 L), BIG BASKET (27,674 L); by tier PREMIUM 188,651 L vs COMMODITY 105,362 L. *Content-hash keyed because grain is finer than the exposed columns (multiple dark-stores per city, no store column) — natural key would silently drop rows.*
- **Per-platform raw inventory tables** (each with platform-native columns):
  - `swiggy_inventory` (73,667 rows): `facility_name, city, days_on_hand, shelf_life_days, potential_gmv_loss, open_pos, open_po_quantity, warehouse_qty_available` — **the richest** (has DOH, GMV-loss, open-PO and shelf-life built in). 36 facilities / 20 cities.
  - `amazon_inventory` (12,899 rows): vendor-central view — `sellable_on_hand_inventory/units, unsellable_*, aged_90_days_*, net_received, open_purchase_order_quantity, receive_fill_pct, vendor_confirmation_pct, sourceable_product_oos_pct, unfilled_customer_ordered_units, overall_vendor_lead_time_days`. business = "JIVO MART PVT LTD". **Verified that this table feeds `soh-doh__amazon`:** its latest `sellable_on_hand_units` = **111,880**, *exactly* the dashboard's `soh_unit` 111,880. Note its `sellable_on_hand_inventory` (₹54.9M) is a **rupee value, not litres** — the dashboard's `soh_ltr` 258,041 is derived (units × pack size), as `amazon_inventory` has no litres column.
  - `blinkit_inventory` (29,161 rows): `backend_facility_name, backend_inv_qty, frontend_inv_qty, total_inv_qty` (59 facilities; latest backend 32,913 + frontend 20,155 = 53,068 units).
  - `zepto_inventory` (34,937 rows): `city, sku_*, units` (65 cities, 99,671 units latest).
  - `bigbasket_inventory` (36,895 rows): `city, soh, soh_value, top/mid/leaf_category_name` (latest 15,709 SOH = ₹6.9M, 29 cities).
  - `jiomart_inventory` (2,267 rows): `rfc_name, total_sellable_inv, total_unsellable_inv, expired_inv, fc/lsp/cust_dmg_inv, mtd_*` fulfilment counters.
  - `citymall_inventory`, `zomato_inventory` — **empty** (0 rows, `expected_empty: true`).
- **Dashboards (the actual page views):**
  - `inventory-charts` (plain, all-dates): `platform_totals` (cumulative qty + SKU count per platform: amazon 8.43M / swiggy 6.42M / zepto 5.95M / blinkit 4.53M / bigbasket 1.37M / jiomart 0.45M), `city_distribution` (top: Mumbai 1.64M, Hyderabad 1.34M, Gurugram 1.14M), `top_products` (Jivo Groundnut 1L on Zepto = 2.12M qty).
  - `soh-doh__<platform>` (amazon, bigbasket, blinkit, swiggy, zepto — these are the `live` ones; others gated): per-ASIN/SKU SOH/DRR/DOH with `totals` and breakdowns by `asin`, `category`, `category_sub_category`, `item_head`. Amazon item_head split: PREMIUM DOH 22.7 (115,033 L), COMMODITY DOH 20.3 (143,008 L).
  - `region-doh__<platform>` (swiggy, zepto only — rest gated): SOH/DOH by **city/region**. Swiggy: 131 cities, 19 with stock, 104,098 L SOH (top Bangalore 14,808 L @ DOH 14.8). Zepto: 65 cities, all stocked, 76,373 L SOH.
  - `expiry-alerts__<platform>` (per-platform, mostly empty) + `platform-expiry-alerts` (roll-up, populated).

### 1.4 Drill-down / structure
Platform → **city / facility** (or region) → **SKU/ASIN** → date snapshot. Grain options the app exposes: by ASIN, by category, by category×sub-category, by item_head (PREMIUM/COMMODITY/OTHER), and (swiggy/zepto) by city/region. The `available_dates` array in each `soh-doh` doc shows ~30 daily snapshots back to mid-May.

### 1.5 CONNECTIONS
- **Inventory ↔ JM Inventory** *(the headline connection)*: platform Inventory is downstream of JM's warehouse. The platforms' **open POs** (`swiggy_inventory.open_po_quantity` = 93,103 latest; `amazon_inventory.open_purchase_order_quantity` = 165,351) are exactly what JM must ship next — i.e. demand pulling stock out of JM Inventory. See §2.
- **Inventory ↔ Secondary** *(depletion engine)*: **DOH literally = SOH ÷ Secondary DRR.** The DRR/`ltr_sold` numbers in `soh-doh`/`region-doh` come from the secondary-sales tables that W1 owns (`source.sales = amazon_sec_range_master_view`). Sell-through (Secondary) is what drains this page; this page tells Secondary when it's about to run dry.
- **Inventory ↔ Primary/Distributor** *(replenishment)*: low DOH / high potential-GMV-loss is the trigger for the next Primary PO, which a **Distributor** (§4) fulfils. Expiry alerts here are the same PO objects scored on `days_to_expiry` from `master_po`.
- **Inventory ↔ Marketing**: pushing ad spend (§3) on a low-DOH SKU accelerates a stock-out (and wastes spend on OOS items); marketing should be steered by DOH.

### 1.6 Anomalies / caveats
- **`soh-doh` top-level `totals` are 0 for swiggy/blinkit/zepto/bigbasket** even though their per-SKU rows and `region-doh` have real stock (Amazon's totals *are* populated: 258,041 L). The roll-up aggregator for the non-Amazon platforms appears broken — use `region-doh` or the raw tables for those, not the soh-doh `totals` block.
- **`soh-doh` `totals` repeats the same grand total** across all four sub-keys (`asin`/`category`/`category_sub_category`/`item_head` are byte-identical) — they're not really per-grain totals.
- **`inventory-match__*` returns `{"match": null}` for ALL 10 platforms** — the "inventory-match" page (the screen that's supposed to reconcile JM-side stock vs platform-side stock) is effectively **empty/non-functional** in the live app. *This is the most important gap for the JM Inventory ↔ Inventory reconciliation.*
- **JioMart inventory looks near-dead**: latest snapshot = 22 rows, 1 RFC, total_sellable_inv = **0** (only 76 unsellable). Either feed stalled or JioMart wound down.
- **`citymall_inventory` & `zomato_inventory` empty** (consistent with those platforms being empty across the app).
- `inventory-charts.platform_totals` are **cumulative across all dates** (sku_count is really row-count, e.g. swiggy 73,667), *not* a current snapshot — don't read them as live stock.
- **`all_platform_inventory` and `inventory-charts` have different platform scope** (verified): `all_platform_inventory` carries only **5 formats — SWIGGY, BLINKIT, ZEPTO, BIG BASKET, JIO MART** (no Amazon, no Flipkart), whereas `inventory-charts.platform_totals` *includes Amazon (8.43M) and JioMart*. So `inventory-charts` is built from the **raw per-platform tables**, not from `all_platform_inventory` — Amazon/Flipkart stock lives only in their own tables. Also verified: **19% of latest `all_platform_inventory` SKU-rows have `soh_ltr = 0`** (SKU-location stock-outs), and live premium-mix by SOH-litres is **64% PREMIUM / 36% COMMODITY** — independently consistent with the context's "premium-mix is a headline KPI."

---

## 2. JM INVENTORY (Jivo Mart's own warehouse)

### 2.1 Purpose
Show the stock **JM physically holds in its own warehouse** — the buffer that sits between "what Wellness billed / JM received as Primary-in" and "what JM has shipped out to the platforms as Primary." It answers: *do we have enough in our own warehouse to fill the platforms' open POs?*

### 2.2 Key metrics / KPIs (conceptual — see caveat)
SOH in JM's warehouse, by SKU / item_head / brand, vs the **open-PO demand** from the platforms. The natural KPI is **cover = JM SOH ÷ outstanding platform open-PO qty**.

### 2.3 Data behind it — **important finding**
There is **no dedicated `jm_inventory` table in the extracted SSOT.** Every `*_inventory` table (and `all_platform_inventory`, whose 176 locations are all platform cities/feeder-warehouses/dark-stores/RFCs) describes stock *at the platforms*, not in JM's own warehouse. The JM-warehouse number is therefore **derived**, from data other workers own:
- **Supply-chain residual**: `JM Primary received − Primary shipped to platforms` ≈ what JM is holding (W2's `master_po` / `prim_master_po` / `total_po` lineage).
- **Open-PO demand signals** that live on the platform tables: `swiggy_inventory.open_po_quantity` (93,103) + `amazon_inventory.open_purchase_order_quantity` (165,351) + `jiomart_inventory.mtd_fwd_intransit` = the orders JM must fill *out of* JM Inventory.
- Amazon's vendor view (`net_received`, `vendor_confirmation_pct`, `receive_fill_pct`, `overall_vendor_lead_time_days`) is the closest thing to a "JM-as-supplier" scorecard the data exposes.

### 2.4 Drill-down / structure
Expected: warehouse → SKU → item_head/brand, matched against per-platform open POs. In practice the matching screen (`inventory-match`) is empty (§1.6), so the drill-down is currently aspirational.

### 2.5 CONNECTIONS
- **JM Inventory ↔ Inventory** *(the supply chain, the owner's explicit hint)*: JM Inventory is the **source**; platform Inventory is the **destination**. A platform's open PO debits JM Inventory and (once shipped & received) credits platform Inventory. The two pages are the two ends of one pipe.
- **JM Inventory ↔ JM Primary / Wellness Billing (W2)**: inflow side — Wellness bills JM (811,616 L), JM Primary records receipt (672,739 L); the un-shipped remainder is JM Inventory.
- **JM Inventory ↔ Distributor (§4)**: when JM uses third-party distributors (vendors) to fulfil platform POs, the "JM warehouse" for that lane is effectively the **vendor's** stock — `vendor_new = "JIVO MART PRIVATE LIMITED"` itself appears as one of the fulfilling vendors (888,054 ordered L), i.e. JM self-fills part of its own demand.

### 2.6 Anomalies / caveats
- **The dedicated reconciliation view (`inventory-match`) is empty for all platforms** — so the app cannot currently show JM-stock vs platform-stock side by side, even though both legs of data exist. This is the single biggest "built fast, incomplete" gap in my domain and the thing to flag to the owner.
- Because there's no `jm_inventory` table, any JM-warehouse figure on the page must be reconstructed from W2's PO tables + the open-PO columns above — treat it as **derived, not directly sourced**.

---

## 3. MARKETING (spend, ROAS, coupons, brand-fund, price)

### 3.1 Purpose
Track **demand generation and pricing**: how much is spent advertising on each platform, what it returns (ROAS/ACOS), coupon/brand-fund promotions, and the competitive price/margin landscape that bounds discounting. Goal: pull platform Inventory through to Secondary efficiently and protect margin.

### 3.2 Key metrics / KPIs (real numbers, ~May–Jun 2026)

**Ad performance per platform** (spend → attributed sales; ROAS = sales ÷ spend; ACOS = spend ÷ sales):

| Platform | Spend ₹ | Attributed sales ₹ | ROAS | Notes |
|----------|--------:|-------------------:|-----:|-------|
| Zepto | 7,453,358 | 139,365,497 | **18.70** | best ROAS; `revenue/spend/roas` per product-campaign-city |
| Swiggy | 9,139,333 | 123,284,657 | 13.49 | `total_budget_burnt` vs `total_gmv`; also 7/14-day direct-ROI |
| Amazon | 4,704,549 | 51,769,215 | 11.00 | richest schema: NTB (new-to-brand), halo, promoted, DPV; `roas`+derived `acos` |
| Blinkit | 2,806,456 | 29,450,800 | 10.49 | `ad_spent`, `direct_gmv` + `indirect_gmv` (halo) |
| Flipkart | 1,069,187 | 9,739,593 | 9.11 | `ad_spend`, `total_revenue`, `roi` |
| BigBasket | 137,940 | 554,520 | 4.02 | smallest (40 rows), weakest ROAS |
| **Total** | **₹25.3M** | **₹354.2M** | **~14** | |

> **Verified caveat — data depth is wildly uneven, and ROAS is computed as avg-of-ratios.** Ad coverage windows differ by an order of magnitude: amazon 55 days / blinkit 56 / flipkart 32, but **swiggy only 11 days, zepto only 10, bigbasket only 5** (all ending 2026-06-25). So the swiggy/zepto/bigbasket ROAS above rests on ~1–2 weeks of data, not comparable depth. Also, `ads__amazon` declares `roas`/`acos` with `agg: "avg"` — i.e. the app shows the **average of per-row ratios** (Amazon avg row-ROAS = **15.83**), which differs from the ratio-of-sums (11.00) I tabulated. Read the table as ratio-of-sums; the on-screen number is the avg.

- **ACOS** = inverse of ROAS (`acos = total_cost ÷ sales`), the headline efficiency metric on the `ads__amazon` dashboard (available_metrics include `roas`, `acos`, `ctr`, `cpc`, `ntb_orders_pct`, `ntb_sales_pct`). *Verified: `acos` is **not stored** on `amazon_ads` — it's dashboard-derived.* Amazon = 986 campaigns over 55 days.
- **Coupons** (`amazon_coupon`, 795 rows): budget spent **₹947,978**, **44,257 redemptions**; per-coupon budget-used %, e.g. "CANOLA 5" 80.1% used (₹4,005/₹5,000, 154 redemptions). The `coupon__amazon` dashboard tags each coupon with `item_head` (PREMIUM/COMMODITY).
- **Brand-fund** (co-funded trade discount): Blinkit `total_brand_fund` **₹1,693,154** (423 rows, but only **60% of rows are non-zero**), Zepto `promo_claim_amt` **₹410,879** (44 rows, **100% populated**), Swiggy `discount_spend` **₹1,178** (411 rows, only **9% non-zero — effectively empty/not yet wired**). Carries `multiplier`, `item_mrp`, `mrp_gmv`, `brandfund_percentage_value`. *Verified: Blinkit's `offer_type` is `"None"` on all 423 rows — that column is unpopulated.*
- **Price / margin** (`amazon_price_data` + `price__amazon`): per-ASIN `mrp, asp, margin_pct, cost_without_tax, stock_status` plus **competing-seller scraped prices** `url_price, rk_price (RK World Infocom), jm_price (Jivo Mart), svd_price, bau_price, art_price`. e.g. CANOLA 1+1L: MRP ₹750, ASP ₹533, margin 25%, url ₹469.
- **Comparison** (`comparison__amazon`): current-month vs best-month per category — `shipped_ltr, shipped_rev, net_realise, price_per_ltr, rev_after_margin`, split brand × item_head × sub-category.

### 3.3 Data behind it
- Ads: `amazon_ads` (50,503 rows, campaign→ad-group→ASIN grain, by date), `swiggy_ads` (campaign grain + city/keyword counts), `zepto_ads` (product-campaign grain, `same_skus`/`other_skus` halo), `blinkit_ads`, `flipkart_ads`, `bigbasket_ads`. **Only Amazon is `live` for the ads/coupon/marketplace/comparison/price leaves**; the other platforms' ad data exists as **tables** but their per-platform *dashboards* are server-gated.
- Brand-fund: `blinkit_brandfund` (423), `swiggy_brandfund` (411), `zepto_brandfund` (44).
- Coupons/price: `amazon_coupon` (795), `amazon_price_data` (192).
- Dashboards (all Amazon): `ads__amazon`, `coupon__amazon`, `marketplace__amazon`, `comparison__amazon`, `price__amazon`. `marketplace__amazon` = the amazon-live MP sell view (JUNE: 4,820 L / ₹2.38M, split by brand/item_head/state/sub-category).

### 3.4 Drill-down / structure
Platform → campaign (→ ad-group → ASIN/product, Amazon) → date. Cross-cut by item_head, brand, city (Swiggy/Zepto/BigBasket carry `city`/`city_count`). Coupons and brand-fund drill to the SKU/city + item_head. Price drills to ASIN with the competing-seller price columns side by side.

### 3.5 CONNECTIONS
- **Marketing ↔ Secondary (W1)** *(the core spend→sell-out loop)*: ad `sales/revenue/gmv`, `units_sold`, coupon `redemptions`, and brand-fund `qty_sold` are all **Secondary sell-out** that marketing claims to have driven. ROAS only makes sense against the Secondary numbers W1 owns. Zepto's 18.7 ROAS says ₹1 of spend pulled ₹18.7 of consumer sell-through.
- **Marketing ↔ Inventory** *(don't advertise what you can't ship)*: spend on a low-DOH or OOS SKU burns budget; `amazon_price_data.stock_status` ("Out of Stock") and `soh-doh` DOH should gate spend. Marketing accelerates the depletion this domain's §1 tracks.
- **Marketing price ↔ the price-match system**: `amazon_price_data`'s competing-seller columns (`rk_price`, `jm_price`, `svd_price`, `bau_price`, `art_price`) are a **price-monitoring/price-match** engine — JM watches other sellers on the same ASIN and sets ASP to stay competitive while holding `margin_pct`. This ties directly to the margin ladder (Primary ₹210.3/L → Secondary ₹218.2/L): coupons + brand-fund are the levers that move ASP within that band.
- **Marketing ↔ Distributor/Primary**: `distributor_margin` and `distributor_commission_per_unit` in `master_po` are the *trade-side* cost; ad/coupon/brand-fund are the *consumer-side* cost — together they're total go-to-market spend per litre.

### 3.6 Anomalies / caveats
- **Non-Amazon ad/price dashboards are gated** (the data tables exist; only the Amazon dashboards render live). So the Marketing *page* is Amazon-deep but thin on the dashboard layer for quick-commerce.
- **`amazon_price_data` is a one-shot snapshot, not a live series** — verified: only **2 upload dates (2026-05-12..14), 192 rows, 96 ASINs**. The competing-seller columns are sparse: **`jm_price` is null on 72% of rows** (138/192); among the 54 populated, median ₹422 vs ASP median ₹379, but ~5 are implausibly low (e.g. A2 GHEE 1L `jm_price` ₹129 vs ASP ₹1,579). So the price-match engine *exists structurally* but is under-fed and partly dirty — don't treat any seller column as a clean time-series.
- **`comparison__amazon` notes flag `excel_visible_match: false`** and `highest_block_calculated: true` — the "highest month" reconciliation doesn't tie out to the source Excel; treat the comparison's "highest" block as app-computed, not authoritative.
- **BigBasket ads = 40 rows / ROAS 4.0** — tiny sample, weak efficiency; likely early-stage or under-fed.
- Attributed-sales totals mix definitions across platforms (Amazon `sales` vs Swiggy `total_gmv` vs Blinkit `direct+indirect_gmv` vs Zepto `revenue`) — the blended ~14× ROAS is indicative, not strictly apples-to-apples.

---

## 4. DISTRIBUTOR (vendor / distributor performance)

### 4.1 Purpose
Show **who fulfils Jivo's purchase orders** and **how well** — the third-party distributors (and JM itself) that take a platform PO and deliver against it. The key question: which vendor reliably fills orders (high fill-rate, low miss) at what commission?

### 4.2 Key metrics / KPIs (real numbers, computed from current `master_po`)
Grain: each PO line carries `vendor_new` / `vendor_name`, `order_qty`/`total_order_liters`, `filled_qty`/`filled_ltrs`, `missed_qty`/`missed_ltrs`, `distributor_margin`, `distributor_commission_per_unit`, `total_distributor_commission`, `lead_time`, `days_to_expiry`, `item_status` (FULL SUPPLIED / …).

**Vendor scorecard (44,081 PO lines, all-time in SSOT):**

| Vendor | POs | Ordered L | **Fill %** | Commission ₹ | Top format |
|--------|----:|----------:|-----------:|-------------:|-----------|
| SUSTAINQUEST PRIVATE LIMITED | 1,239 | 1,185,292 | 56.8 | 6,403,541 | Swiggy |
| KNOWTABLE ONLINE SERVICES | 2,554 | 991,763 | 70.2 | 9,454,944 | Swiggy |
| JIVO MART PRIVATE LIMITED *(self-fill)* | 1,633 | 888,054 | 58.6 | 4,739,645 | Blinkit |
| CHIRAG ENTERPRISES MUMBAI | 2,111 | 836,788 | 57.6 | 6,146,565 | Swiggy |
| EVARA ENTERPRISES | 669 | 603,659 | **90.9** | 4,449,220 | Blinkit |
| ANTIZE FOODS PVT LTD | 1,133 | 486,282 | **86.7** | 6,488,297 | Blinkit |
| BABA LOKENATH TRADERS | 528 | 124,164 | 49.3 | 787,383 | Swiggy |
| SHIV SHAKTI | 38 | 33,519 | 61.7 | 175,114 | BigBasket |
| RAGHAV MARKETING | 3 | 998 | 44.0 | 6,368 | BigBasket |
| SURYAM ENTERPRISES-PUNE | 3 | 60 | 0.0 | 0 | Blinkit |
| Local Procurement-West | 1 | 2 | 0.0 | 0 | Swiggy |

- **Fill-rate** (filled ÷ ordered litres) is the headline. Best: **EVARA 90.9%**, **ANTIZE 86.7%**; the big-volume vendors are mediocre (Sustainquest 56.8%, Chirag 57.6%, KnowTable 70.2%). Low fill-rate = missed sales + stock-outs downstream.
- **Distributor commission/margin**: `distributor_margin` (~4.5% in sample), `distributor_commission_per_unit`, `total_distributor_commission` — total commission across vendors ≈ **₹39M**. This is the trade cost of using distributors.
- **Lead time** (`lead_time`, e.g. 10 days) and **days_to_expiry** per PO feed re-order timing and expiry alerts.
- **Why fill is low — verified `item_status` breakdown** (44,081 lines): FULL SUPPLIED **26,103**, SHORT SUPPLIED **7,347**, **EXPIRED 6,294**, **CANCELLED 3,061**, PENDING 1,259. Overall fill = **66.5% by litres / 63.3% by units** (the litre vs unit gap comes from multi-pack SKUs). The **EXPIRED + CANCELLED = ~21% of all PO lines** is a major drag on fill — i.e. a large share of misses are POs that lapsed/were cancelled before delivery, not pure short-supply. PO span = FY2025–2026.

### 4.3 Data behind it
- **`master_po`** (44,081 rows, natural key `po_number+sku_code+location+format`, 54 cols) — the single source for vendor/distributor performance. Vendor columns: `vendor_name`, `vendor_new` (cleaned). Also `fulfilment-health` dashboard rolls up fill/miss per **platform** (not per vendor): Amazon fill 42.1% (miss 11.6%, 108 POs, 429,242 L ordered) … Swiggy 51.5%, Zepto 71.6%, Blinkit 81.5%, BigBasket 82.2%, City Mall 84.2%, Flipkart Grocery 80.9%.
- Empty/placeholder PO tables: `prim_master_po`, `test_master_po` (0 rows). `total_po` (8,030) / `total_po_zbs` (35,839) are W2's primary-PO lineage.

### 4.4 Drill-down / structure
Vendor → format/platform → SKU → PO line (with ordered/filled/missed, commission, dates). Can also pivot by `item_head`, `city`, `po_month`, `po_status` (COMPLETED/…), `open_close`.

### 4.5 CONNECTIONS
- **Distributor ↔ Primary (W2)** *(the explicit link)*: `master_po` **is** the Primary-PO table — Primary (Jivo→platforms, 484,975 L) is the *sum* of these PO lines, and Distributor just re-views the same rows **grouped by vendor**. The platform-grouped view of the same data is `fulfilment-health` (which also appears on **Home**, W4). Vendor fill-rate is *why* a platform's Primary fill-rate is what it is.
- **Distributor ↔ Inventory** *(downstream effect)*: a vendor's **missed_ltrs** become the platform's **stock-out / potential GMV loss** (§1) and feed expiry alerts via `days_to_expiry`. Low vendor fill → low platform DOH → lost Secondary.
- **Distributor ↔ JM Inventory**: `vendor_new = "JIVO MART PRIVATE LIMITED"` shows JM **self-fulfilling** 888,054 L of its own POs — i.e. some lanes are served straight from JM Inventory, others outsourced to distributors.
- **Distributor ↔ Marketing**: distributor commission/margin is the trade-cost half of go-to-market; ad/coupon/brand-fund is the consumer-cost half (§3.5).

### 4.6 Anomalies / caveats
- **Owner flagged Distributor as partly broken/incomplete — confirmed in spirit.** There is **no dedicated distributor dashboard** in the SSOT (no `distributor__*` doc); the page is built ad-hoc over `master_po`'s vendor columns, and the only roll-up that exists is platform-grained (`fulfilment-health`), **not vendor-grained**. The per-vendor scorecard above had to be computed directly from rows.
- **`vendor_name` vs `vendor_new`** both exist (a cleanup column) — they agree in the sampled rows but the duplication signals an in-progress data-hygiene pass.
- **Tail vendors look like noise/test rows**: SURYAM (60 L, 0% fill), Local Procurement-West (2 L), RAGHAV (3 POs) — tiny/zero-fill entries that shouldn't be ranked next to real distributors.
- **Fill-rates here (all-time, vendor) differ from `fulfilment-health` (windowed, platform)** — e.g. Amazon platform fill 42.1% vs vendor blended ~60% — because the window and grouping differ. Don't mix the two without aligning the date window.
- Fill % is computed on **litres**; unit-based fill can differ for multi-pack SKUs (`case_pack`, `per_liter`).

---

## 5. Cross-domain summary (one paragraph for the lead)

**Distributor fills the Primary POs → stock lands as platform Inventory → Marketing spend pulls it through as Secondary → SOH/DOH signals the next re-order, which loops back to a Distributor PO.** JM Inventory is the buffer in the middle (JM's own warehouse, reconstructed from the supply chain because there's no `jm_inventory` table and the `inventory-match` reconciliation screen is empty for all platforms). The strongest, cleanest data is: per-platform inventory with DOH + potential-GMV-loss (Swiggy/Amazon), Amazon-deep marketing with ROAS/ACOS/coupons/price-match, and a computable vendor fill-rate scorecard from `master_po`. The biggest gaps to flag: empty `inventory-match` (no JM↔platform reconciliation), zeroed `soh-doh` totals for non-Amazon platforms, gated non-Amazon marketing dashboards, a buggy `jm_price` column, and a Distributor page with no native vendor-level dashboard.
