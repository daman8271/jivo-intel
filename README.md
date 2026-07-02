# JIVO-Intel — the JIVO e-com app data bank

A **lossless, offline, deterministic** capture of everything inside JIVO's internal
e-commerce control tower (`ecom.jivo.in`), turned into a linked **Obsidian Markdown
knowledge vault** you can read, query, and reason over without ever touching the live API.

> **As of the 2026-07-01 source vault:** `35,285` vault notes · `1,287,683` current-state table rows ·
> `41` source tables. The daily `today/` export is separate (`22,127` files generated
> 2026-07-01 20:51 IST). The live app drifts day to day; quote figures with the pull date and check
> `state/pull-ledger.jsonl` for the latest verification status.

---

## Start here (reading order for a new agent)

Read these four files **in order** before doing anything else. Together they take ~10 minutes
and give you the full foundation: what this is, where the data lives, how to read it, and what
it means.

1. **`README.md`** ← you are here — what this is, the mental model, the rules.
2. **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — how the data is physically laid out: the
   pipeline, the three storage layers, and the directory map (committed vs transient).
3. **[`VAULT-GUIDE.md`](VAULT-GUIDE.md)** — how to **read and navigate** the vault: the note
   types, the "one row stored once, everything else is a link" design, and navigation recipes.
4. **[`DATA-MODEL.md`](DATA-MODEL.md)** — what the data **means**: JIVO's business, the value
   chain, the platform archetypes, the join model, and the landmines that will bite you.

**Deep references** (cited throughout — read on demand, not upfront):

- `datamap/00-MASTER-data-model.md` — the full, adversarially-verified business + data model.
- `SPEC.md` — the pinned build contract (every interface the pipeline holds to).
- `docs/app-model/` — how the app itself works (value chain, 13 pages, targets) + an HTML brief.
- `docs/sku-bridge/` — the Rosetta Stone joining these internal SKUs to the competitor-price scraper.
- `vault/SESSION-MEMORY.md` — the build-session handoff (journey, errors found, historical next steps).

---

## The 30-second mental model

```
ecom.jivo.in  ──(read-only CLI)──►  raw daily capture  ──►  versioned SSOT  ──►  Obsidian vault
  the live app      jivo-ecom-pp-cli     store/raw/ (transient)   store/versioned/ (truth)   vault/ (what you read)
```

- **One read-only CLI** (`jivo-ecom-pp-cli`) pulls the app's 41 tables + dashboards + notifications.
- **A zero-loss SSOT** (`store/versioned/`) keeps every distinct row version, append-only, forever.
- **A deterministic vault** (`vault/`) projects that SSOT into linked notes. Every raw row is
  embedded **exactly once** under its single canonical home; every other dimension it relates to
  is a `[[wikilink]]`. **The connections are the product — not the raw extraction.**
- **The convergence node is the SKU.** Each `sku-<code>` note is where category, tier, brand,
  every platform it sells on, every vendor that ships it, every PO, and every month meet.

---

## Repo at a glance

```
jivo-intel/
├── README.md            ← this file
├── ARCHITECTURE.md      ← physical layout + pipeline
├── VAULT-GUIDE.md       ← how to read the vault
├── DATA-MODEL.md        ← what the data means
├── SPEC.md              the pinned build contract (fixed interfaces)
├── bin/                 the pipeline: auth · pull · ssot · verify · vault_build · archive · doctor
├── registry/            tables.json · endpoints.json · taxonomy.json  (drives the pull)
├── store/
│   ├── raw/             transient daily capture            (gitignored)
│   └── versioned/       APPEND-ONLY SSOT — the truth       (committed)
├── archive/             weekly cold gz snapshots + manifests
├── vault/               the 35,285-note Obsidian vault     ← what you read
├── datamap/             the master data model + 4 per-domain deep-dives
├── docs/                app-model/ (how the app works) · sku-bridge/ (join to the scraper)
├── recon/               the read-only recon corpus the model was built from
├── state/               token.json (0600, gitignored) · pull-ledger.jsonl
├── workflows/ · logs/   weekly rollups · run logs
└── *-fixture/           TINY test fixtures — NOT real data, ignore for analysis
```

Full annotated map: **[`ARCHITECTURE.md`](ARCHITECTURE.md)**.

---

## Hard rules (never violate)

- **Read-only.** This bank is built only from read endpoints (`tables · dashboard · platform ·
  master · notifications · account`). There are **no** write/mutation paths. Never add one.
- **Never write secrets** (password, JWT) into any git-tracked file. The token lives only in
  `state/token.json` (0600, gitignored); the password is read from env at runtime, never stored.
- **Fail-closed.** The SSOT is irreplaceable. On any integrity failure the pipeline stops and
  preserves last-good — it never overwrites with partial data.
- **A snapshot is not live.** Every figure you quote from this bank is "**as of `<pull date>`**".
  The live app drifts a few percent a day; date-stamp anything you report.
- **Push only through owner-sanctioned automation or owner action.** The installed 05:00 IST source
  feeder can commit/push after a green integrity gate; every push must be clean/formatted.

---

## Status

- **Done:** the app is extracted and versioned into SSOT, the vault is built, the SKU bridge to the
  competitor-price scraper is mapped, the app behaviour is documented (`docs/app-model/`), and this
  repo now feeds the combined **JIVO Data Bank**.
- **Daily feeder:** installed cron runs `/root/jivo-intel/bin/run_daily.sh` at **05:00 IST** before the
  same-day data-bank fusion.

### Where this fits — the data-bank programme

This is the **JIVO app source pillar** in the JIVO data-bank programme:

1. **ECOM-Intel** — the competitor-price scraper (`/opt/ecom-intel`, repo `daman8271/ecom-intel`).
2. **JIVO-Intel** — *this* repo: the app's own internal data (`daman8271/jivo-intel`).
3. **JIVO-Factory** — the Jivo Mart factory source pillar (`/root/jivo-factory-intel`).
4. **JIVO Data Bank** — the combined vault (`/root/jivo-data-bank`), where each product carries JIVO,
   competitor-price, and factory lenses. **`docs/sku-bridge/` is the key that joins this pillar to
   competitor-price data.**

---

*This bank is rebuildable from the committed SSOT alone — `bin/vault_build.py` is stdlib-only,
needs no network and no model, and produces byte-identical output from the same input.*
