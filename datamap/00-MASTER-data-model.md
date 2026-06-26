# JIVO E-Com — Master Data Model & Business Mental Model
**Built 2026-06-26** from a 270-call read-only recon + a 4-worker fleet deep-dive, all claims
adversarially verified live. Detailed per-domain references: `W1-amazon.md`, `W2-qcomm.md`,
`W3-ecom-grocery.md`, `W4-spine.md`. This file is the synthesis — read it first.

---

## 1. How the JIVO e-com business actually works (reverse-engineered from the data)

**What JIVO sells.** Edible oils (the core: OLIVE — Jivo's heritage extra-light/pomace/extra-virgin;
plus CANOLA, SUNFLOWER, MUSTARD, GROUNDNUT, RICE BRAN, SESAME, SOYABEAN, COTTON SEED, BLENDED),
**A2 desi ghee** (premium), and a **beverages** line (energy drinks, tonic/soda/mojito, many
sugar-free "SF" variants). Two house brands: **JIVO** (flagship) and **SANO** (value, esp. sunflower).

**Three product tiers** (`item_head`) drive every rollup: **PREMIUM** (olive + A2 ghee, 441 listings),
**COMMODITY** (canola/sunflower/mustard…, 196), **OTHER** (beverages/misc, 165). The **North-Star KPI
is premium-mix ≈ 52% by litres** — keeping the premium line pacing is "the one number."

**The house unit is LITRES** (`per_unit_value` = litres/unit). Units, ₹, and litres travel in parallel
everywhere; litres is what leadership reasons in.

**The two-sided flow** (per platform):
- **PRIMARY / sell-in:** platforms issue POs → JIVO's **distributors** (Knowtable, Chirag, Baba
  Lokenath, Antize…) fulfil → `master_po` tracks ordered vs delivered, fill/miss, expiry. **~82% of
  PO lines are quick-commerce** (the `zbs` trio).
- **SECONDARY / sell-out:** consumers buy on the apps → `*Sec` tables track units + GMV by SKU ×
  geography × day.

**JIVO sells through distributors, not direct.** `vendor_name` is the distributor. On Amazon, **"RK
World"** sells as two entities — **RK JMPL** (Jivo **M**art) and **RK JWPL** (Jivo **W**ellness) — and
every Amazon sell-out row is split by this `sales_type` (watch for double-counting). A **distributor
margin** is deducted to reach net realisation (~5.5% on q-comm/marketplace via `master_po`;
**18/20/25% flat per ASIN on Amazon**).

**Two disjoint geographies.** E-com (Amazon) uses **19 fulfilment centres** across 6 metros (NCR×6,
Mumbai/Pune×4, Bengaluru×3, Kolkata×3, Chennai×1, Hyderabad×1; HLK1 is a junk `#N/A` row). Quick-
commerce uses an entirely separate space — **dark-stores** (Swiggy: **1,282 stores**), cities, and
backend feeder-warehouses. **The two never share a key** (`fc_code` ≠ inventory `location`).

### The 3 platform archetypes (this is the key org insight)
| Archetype | Platforms | Character |
|---|---|---|
| **A. Vendor-Central e-com** | **Amazon** | The deepest integration & the *only* one with ads, coupons, price-audit, **margins**, 3P marketplace, and a comparison view. Vendor inventory (open-PO, aged-90d). **Siloed** from every shared table. |
| **B. Quick-commerce (`zbs`)** | **Blinkit, Swiggy, Zepto** | The **availability battleground**: daily sell-out at store/city grain, daily SOH, `soh-doh`/`drr`/`region-doh`, promo `brandfund` + `ads` (raw tables only). Highest PO volume, most time-sensitive. |
| **C. Marketplace / grocery long-tail** | **BigBasket, Flipkart-Grocery, JioMart, Flipkart-mktplace, + CityMall/Zomato/DealShare** | PO-driven via `master_po`; MoM-sale only for BB & FKG; mostly national price. Several are **empty or stale** (see §5). |

---

## 2. The data architecture — 3 layers (mental model for every table)

```
L1  RAW WAREHOUSE (41 tables)        platform-native exports, dirty, un-normalised
      *Sec  *_inventory  *_ads  *_brandfund  total_po*  amazon_*
            │  (JIVO joins to the SKU master, adds litres/item_head/category/margin/entity)
            ▼
L2  ENRICHED / CANONICAL             ← TRUST THIS AS GROUND TRUTH
      master_po · *_sec_*_master_view · flipkart_secondary_all · flipkart_grocery_master ·
      SecMaster (q-comm aggregated) · all_platform_inventory (unioned SOH)
            │  (dashboards compute DOH/DRR/fill/projection on the fly; alert engine persists breaches)
            ▼
L3  SHAPED API  (what the CLI mostly serves)
      dashboard.* · platform <leaf> <slug> · notifications
```
- The CLI exposes **L1** (via `tables …`) and **L3** (via `dashboard`/`platform`/`notifications`).
  It does **not** expose L2 as such — but the `*_master_view` L2 tables *are* reachable through `tables`.
- **Raw vs master_view:** always trust the `_master_view` (litres, category map, margin, ₹-after-margin,
  entity split). Raw `shipped_revenue` is frequently **0**; the master view recomputes it.
- **The CLI keeps no history.** Analytics upserts overwrite; only `notifications` persist. → the
  intelligence layer MUST snapshot L1/L2 daily. **Every un-snapshotted day is gone forever.**

---

## 3. Metric dictionary (verified definitions — use these exactly)

| Metric | Definition | Notes |
|---|---|---|
| **DRR** (daily run-rate) | `MTD units_sold ÷ elapsed_days` | qty / ltr / value variants. Velocity numerator. Source: SecMaster (q-comm), `amazon_sec_daily_master_view` (Amazon). |
| **DOH** (days of health) | `current SOH ÷ DRR` | The availability signal. **< 5 days fires `INVENTORY_DOH_LOW` (critical)**. `DOH 0` = stocked-out with live demand; **`DOH -2` = OOS sentinel**. SOH from `all_platform_inventory` (or `amazon_inventory`). |
| **fill-rate** | `filled_ltrs ÷ ordered_ltrs` | **PO/primary side, in litres. NOT on-time.** Trailing **30-day window lagged 7 days**. From `master_po`. June: 58.2% overall (worst Amazon 43%, Swiggy 52%; best BB 85%, CityMall 84%). |
| **premium-mix %** | `Σ PREMIUM litres ÷ Σ all litres` | litres = `qty × per_unit_value`. ≈ **52%** (May '26). The leadership number. |
| **margin (Amazon)** | flat `margin_pct ∈ {18,20,25}` per ASIN | `amazon_sec_range_margins`, **% only, no ₹**. ₹ contribution = `shipped_revenue × margin_pct`, materialised downstream (`shipped_revenue_after_margin`, `realise`, dashboard `margin_value`). Covers **114 of 327** ASINs → LEFT-join + item_head default. |
| **margin (others)** | `distributor_margin` fraction (~0.055) in `master_po` | No true COGS/contribution anywhere except Amazon. |

**The cheap, high-value flag (handoff #1):** **OOS + live ad spend = pure burn.** Constructable today:
`amazon_ads.total_cost > 0` (by ASIN+date) while `amazon_inventory.sellable_on_hand_units = 0`
(or `soh-doh` DOH≈0). Amazon-only for now (ads exist only there).

---

## 4. The join model (one SKU across the whole system)

```
PLATFORM LISTING                         INTERNAL SKU                  TAXONOMY (lives in master_po
master products.format_sku_code   ──►   sku_sap_code (FG0000083)  ──► + notifications, NOT in
   = Amazon ASIN  B0B2RW9N9F             (169 distinct SKUs)            master products):
   = Swiggy ITEM_CODE 958164             item  "A2 GHEE 1L"             item_head, brand, category,
   = Blinkit item_id 10048295            per_unit_value (litres)        sub_category, category_head
   = Zepto  "SKU Number" UUID            case_pack, uom, tax_rate
   = BB source_sku_id / FK "Product Id"
   = JioMart FSN_PRODUCT_ID
   (803 listings, ~4.75 per physical SKU)
```
- **Join landmines:** never join on **EAN** (stored as sci-notation text `8.91E+12`); `amazon_inventory.brand`
  is **dirty** (polluted with ASIN strings — derive brand from master); raw PO `status` is dirty (16
  mixed-case) → use normalised `po_status`/`item_status`; **`fc_code` ≠ inventory `location`**.
- `master products` has **only** `item_head` from the taxonomy — **brand/category/sub_category live in
  `master_po`** (and notifications). Use `master_po` to enumerate them.

---

## 5. Coverage matrix & gaps (the landmines that will bite the build)

| Feed | Covers | MISSING (the trap) |
|---|---|---|
| **PO / sell-in** `master_po` (8 formats) | BB, Blinkit, CityMall, DealShare, FK-Grocery, Swiggy, Zepto, Zomato | **No Amazon, no plain-Flipkart, no JioMart.** `fulfilment-health` shows Amazon 43% by pulling it from **Amazon vendor metrics**, not this table. ⇒ a fill-rate built by diffing `master_po` silently **drops Amazon + Flipkart**. |
| **Unified SOH** `all_platform_inventory` | BB, Blinkit, JioMart, Swiggy, Zepto | **No Amazon** (→ `amazon_inventory`, ASIN/vendor schema) **and no Flipkart** (nowhere). Stores **SOH only** — DOH/DRR/category NOT stored. |
| **Margins** | Amazon only | every other platform has no true contribution data. |
| **region-doh** (city lost-sales map) | **Swiggy + Zepto only** | Blinkit 404 (facility-keyed inventory ≠ city-keyed sales). Others n/a. |
| **Ads dashboards** | Amazon only | q-comm `*_ads`/`*_brandfund` exist as **raw tables only** (no shaped dashboard). |
| **Empty / stale** | — | **CityMall + Zomato**: secondary-empty but **primary-LIVE** (via `master_po`). **JioMart sell-out stale** (max 2026-04-15) + all its sell-in dashboards gated. **Flipkart-marketplace**: has sell-out (`flipkart_secondary_all`) + ads but **no PO and no inventory**. |
| **Two DOH numbers** | — | `swiggy_inventory.days_on_hand` (platform's) vs dashboard-computed (SOH/DRR). **Use the computed one.** |

**`zbs` = Zepto + Blinkit + Swiggy** (VERIFIED: `total_po_zbs` formats = exactly those 3). Row math:
`total_po` 8,030 + `total_po_zbs` 35,839 ≈ `master_po` 44,081.

---

## 6. The alert engine = the availability cockpit's spine (and its limits)

`notifications` returns server-side **`INVENTORY_DOH_LOW`** alerts — the **only persisted history** and
the only place **SOH + DRR + DOH sit together per SKU×platform** (with `category`/`brand` joined in, plus
`first_seen_at`/`last_seen_at`/`resolved_at` for **aging**). It already does the DOH-breach math for us.
**Limits to engineer around:** currently **Amazon-only**, **top-50 of 53** (no pagination), all
unread/active (fresh daily snapshot, no resolution lifecycle observed yet). ⇒ consume it as the spine,
but also compute DOH directly from each platform's `soh-doh` dashboard, and **snapshot inventory daily**.

---

## 7. Build implications (maps to the 5 handoff artifacts)

1. **Availability & Lost-Sales Cockpit (daily):** spine = `notifications` (DOH breaches) + per-platform
   `soh-doh`/`drr` (compute DOH where notifications don't reach) + `region-doh` for the Swiggy/Zepto
   **city stock-out map** + the **OOS×ad-spend burn** flag (Amazon). **Snapshot all inventory daily** —
   DOH is derived and never stored historically.
2. **PO Early-Warning (daily):** diff `master_po` on (`po_number`,`sku_code`); watch `item_status`→SHORT
   SUPPLIED/EXPIRED, rising `missed_ltrs`, `days_to_expiry`→0 while `open_close`=OPEN. **Handle Amazon +
   Flipkart separately** (not in `master_po`).
3. **Sell-in vs Sell-out divergence (weekly):** primary litres (`master_po`) vs secondary litres
   (SecMaster / `*Sec`), split premium/commodity.
4. **Expiry / aging burn (weekly):** `platform-expiry-alerts` + `master_po.days_to_expiry`/`open_close` +
   Amazon `aged_90_days_sellable`.
5. **Premium-mix & target pace (weekly):** `category-trend`/`category-litres` (premium_ltrs share) +
   `month-targets`.

**Out of scope here (needs external scraping):** competitor price/availability, share-of-search,
true-vs-constrained demand, review sentiment.
```
```
