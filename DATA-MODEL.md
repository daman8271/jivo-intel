# DATA-MODEL — what the data means

The semantic foundation: JIVO's business, how the numbers connect, and the traps. This is a
faithful **distillation** of the deep, adversarially-verified model in
**`datamap/00-MASTER-data-model.md`** — read that for full per-domain detail (and `W1`–`W4`
alongside it). Read **[`VAULT-GUIDE.md`](VAULT-GUIDE.md)** for how to find these things in the vault.

> Figures below are **as of the 2026-06-27 pull** unless noted. The live app drifts ~3–6 % a day —
> date-stamp anything you report.

---

## 1. The business in a nutshell

JIVO sells **edible oils** (heritage **OLIVE** — extra-light / pomace / extra-virgin — plus canola,
sunflower, mustard, groundnut, rice-bran, sesame, soyabean, cotton-seed, blended), **A2 desi ghee**
(premium), and a **beverages** line (energy drinks, tonic/soda/mojito, many sugar-free "SF"
variants). Two house brands: **JIVO** (flagship) and **SANO** (value, esp. sunflower).

- **The house unit is LITRES** (`per_unit_value` = litres per unit). Units, ₹ and litres travel in
  parallel everywhere; **leadership reasons in litres.**
- **Three product tiers** (`item_head`) drive every rollup — **PREMIUM** (olive + A2 ghee, 441
  listings), **COMMODITY** (canola/sunflower/mustard…, 196), **OTHER** (beverages/misc, 165).
- **The North-Star KPI is premium-mix ≈ 52 % by litres** — keeping the premium line pacing is "the
  one number".
- **JIVO sells through distributors, not direct.** `vendor_name` is the distributor (Knowtable,
  Chirag, Baba Lokenath, Antize…). A distributor margin is deducted to reach net realisation.

### The value chain (Jun-2026 snapshot, in litres)

```
Wellness   811,616 @ ₹180     →  JM Primary 672,739 @ ₹192.6  →  Primary 484,975 @ ₹210.3  →  Secondary 560,048 @ ₹218.2
(manufacture/wholesale)          (Jivo Mart primary)             (platform sell-in)            (consumer sell-out)
```

> **Correction (from the owner's independent review):** the Primary tier split is
> **PREMIUM + COMMODITY = 484,975 exactly; OTHER = 0** at this stage. An earlier draft mislabelled a
> ~26,271 L gap between *two different Primary metrics* (stale `primary-po-litres` 458,703 vs the
> Primary card 484,975) as an "OTHER residual" — that was wrong. Don't reintroduce it.

---

## 2. The two-sided flow (per platform)

- **PRIMARY / sell-in:** platforms issue **POs** → JIVO's **distributors** fulfil → **`master_po`**
  tracks ordered-vs-delivered, fill / miss, expiry. ~82 % of PO lines are quick-commerce.
- **SECONDARY / sell-out:** consumers buy on the apps → the **`*Sec`** tables track units + GMV by
  SKU × geography × day.

**Two disjoint geographies that never share a key:** e-com (Amazon) uses **19 fulfilment centres**
(`fc-<code>`) across 6 metros; quick-commerce uses an entirely separate space of **dark-stores**,
cities and feeder-warehouses. `fc_code` ≠ inventory `location`.

---

## 3. The three platform archetypes (the key org insight)

| Archetype | Platforms | Character |
|---|---|---|
| **A · Vendor-Central e-com** | **Amazon** | Deepest integration; the *only* one with ads, coupons, price-audit, **true margins**, 3P marketplace and a comparison view. Siloed from every shared table. |
| **B · Quick-commerce (`zbs`)** | **Blinkit, Swiggy, Zepto** | The **availability battleground** — daily sell-out at store/city grain, daily SOH, `soh-doh`/`drr`/`region-doh`, promo `brandfund`+`ads`. Highest PO volume, most time-sensitive. |
| **C · Marketplace / grocery long-tail** | **BigBasket, Flipkart-Grocery, JioMart, Flipkart-mktplace, + CityMall / Zomato / DealShare** | PO-driven via `master_po`; mostly national price. Several are **empty or stale** (see §6). |

`zbs` = **Z**epto + **B**linkit + **S**wiggy (verified: `total_po_zbs` formats = exactly those three).

---

## 4. The three data layers (which to trust)

```
L1  RAW WAREHOUSE (41 tables)   platform-native exports — dirty, un-normalised  (*Sec, *_inventory, *_ads, total_po*, amazon_*)
        │   JIVO joins to the SKU master, adds litres / item_head / category / margin / entity
        ▼
L2  ENRICHED / CANONICAL        ← TRUST THIS AS GROUND TRUTH
        master_po · *_sec_*_master_view · flipkart_secondary_all · all_platform_inventory (unioned SOH)
        │   dashboards compute DOH / DRR / fill / projection on the fly
        ▼
L3  SHAPED API                  what the CLI's dashboard/platform/notifications endpoints serve
```

**Raw vs master_view:** always trust the `_master_view` (litres, category map, margin, ₹-after-margin,
entity split). Raw `shipped_revenue` is frequently **0** — the master view recomputes it. In the
vault, `data/` notes = L1 raw; `dash-` notes = L3 app aggregates.

---

## 5. The join model — one SKU across the whole system

```
PLATFORM LISTING                          INTERNAL SKU                 TAXONOMY  (lives in master_po
master_products.format_sku_code   ──►   sku_sap_code (FG0000083) ──►   + notifications, NOT in
  = Amazon ASIN  B0B2RW9N9F               (~169 distinct SKUs)          master_products):
  = Swiggy ITEM_CODE / Blinkit item_id    item  "A2 GHEE 1L"            item_head, brand, category,
  = Zepto UUID / BB source_sku_id …       per_unit_value (litres)       sub_category, category_head
  (803 listings, ~4.75 per physical SKU)
```

The vault encodes exactly this: the **`sku-<code>`** hub is where listing, tier, brand, category,
platform, vendor, PO and month converge (the vault has **168** such hubs).

**Join landmines (these will bite you):**

- **Taxonomy is NOT in `master_products`** — only `item_head` is. `brand` / `category` /
  `sub_category` live in **`master_po` (+ notifications)**. Enumerate them from there.
- **Never join on EAN** — stored as sci-notation text (`8.91E+12`).
- **`amazon_inventory.brand` is dirty** (polluted with ASIN strings) — derive brand from the master.
- **Raw PO `status` is dirty** (16 mixed-case values) — use normalised `po_status` / `item_status`.
- **`fc_code` ≠ inventory `location`** — the two geographies never share a key.
- **Amazon double-count:** "RK World" sells as two entities — **RK JMPL** (Jivo **M**art) and
  **RK JWPL** (Jivo **W**ellness); every Amazon sell-out row is split by `sales_type`.

---

## 6. Coverage map & gaps

| Feed | Covers | The trap |
|---|---|---|
| **PO / sell-in** `master_po` | BB, Blinkit, CityMall, DealShare, FK-Grocery, Swiggy, Zepto, Zomato | **No Amazon, no plain-Flipkart, no JioMart** — a fill-rate built by diffing `master_po` silently drops them. |
| **Unified SOH** `all_platform_inventory` | BB, Blinkit, JioMart, Swiggy, Zepto | **No Amazon** (→ `amazon_inventory`) and **no Flipkart**. Stores SOH only — DOH/DRR are derived, never stored historically. |
| **Margins** | Amazon only (flat 18/20/25 % per ASIN) | No true contribution data anywhere else. |
| **Empty / stale** | — | **CityMall + Zomato:** sell-out empty **but POs are LIVE** (empty on secondary + inventory only). **JioMart sell-out stale** (max 2026-04-15). **Flipkart-mktplace:** sell-out + ads but no PO, no inventory. |

---

## 7. Metric dictionary (use these definitions exactly)

| Metric | Definition |
|---|---|
| **DRR** (daily run-rate) | `MTD units_sold ÷ elapsed_days` — the velocity numerator |
| **DOH** (days of health) | `current SOH ÷ DRR` — the availability signal; `< 5` fires `INVENTORY_DOH_LOW`, `DOH -2` = OOS sentinel |
| **fill-rate** | `filled_ltrs ÷ ordered_ltrs` (PO side, in **litres**, 30-day window lagged 7 days). **Not** on-time. ~58 % overall in June |
| **premium-mix %** | `Σ PREMIUM litres ÷ Σ all litres` ≈ **52 %** — the leadership number |
| **margin (Amazon)** | flat `margin_pct ∈ {18,20,25}` per ASIN; covers 114 of 327 ASINs (LEFT-join + default) |

The high-value flag this enables: **OOS + live ad spend = pure burn** (`amazon_ads.total_cost > 0`
while `amazon_inventory` SOH = 0) — Amazon-only, since ads exist only there.

---

## 8. Trust & caveats

- **Verified lossless.** The capture passed 4 integrity gates + an adversarial pass; an independent
  reviewer confirmed ~95 % of the architecture, 13/16 anomalies, the 41 tables and the join model —
  **the core is sound, not hallucinated.**
- **The numbers were right; some *causal stories* wrapped around them were not.** Before relaying any
  causal claim ("this gap = OTHER"), check it against the raw data; reconcile **scope** before
  aggregating (all-platform vs single-platform); and **date-stamp every figure**.
- **Known correction to carry:** the marketing "₹25.3M → ₹354M ≈ 14× ROAS" headline likely
  **conflates platform scopes** (Amazon-only is ~11×). Treat as unreliable until re-derived per-scope.

---

*Full model + provenance: `datamap/00-MASTER-data-model.md` (+ `W1-amazon`, `W2-qcomm`,
`W3-ecom-grocery`, `W4-spine`). App behaviour: `docs/app-model/`. Build-session record &
the running corrections list: `vault/SESSION-MEMORY.md`.*
