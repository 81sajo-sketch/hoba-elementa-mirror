# Elementa battery feed mirror

Fixes the per-piece vs per-package mismatch for Elementa batteries on hoba.rs,
**durably and automatically**, by transforming the Elementa feed before Stock Sync
sees it.

Elementa lists batteries **per piece**; Hoba sells them **per package**. So:

- **Stock**: package stock = `FLOOR(piece_stock / pack_size)` — e.g. 121 pieces of a 6-pack = **20 packages**, not 121.
- **Price**: package price = `CEILING(piece_price × 0.96 × pack_size)` — e.g. `6 × 69 × 0.96 = 397.44 → 398`.

A GitHub Action runs `transform.py` on a schedule and commits two files:

| File | Who reads it | What it is |
|---|---|---|
| `elementa-feed-mirror.xml` | **Stock Sync** | A byte-for-byte copy of the Elementa feed where, **for Hoba battery SKUs only**, `<lagerVp>` is replaced by the package count. Everything else (all ~8000 non-battery products) is untouched. |
| `baterije-pregled.csv` | **the weekly price job** + you | Per-battery: SKU, variant id, current price, new package price, pack size, piece stock, package stock, and a `flag`. Plain text, so it can be fetched headlessly (the raw feed is gzipped and can't). |

Stock is fixed by Stock Sync importing the XML. Price is applied by a separate,
guard-railed weekly job from the CSV — deliberately **not** forced through Stock
Sync, so enabling it can never touch the ~8000 non-battery prices.

---

## One-time GitHub setup (~10 min — same pattern as the aura-light mirror)

1. **Create a new repo**, e.g. `hoba-elementa-mirror`. It must be **Public** so Stock Sync and the price job can read the raw files without a token. (The private feed token is *not* in these files — see step 3.)
2. **Upload these files**, preserving paths:
   - `transform.py`
   - `.github/workflows/mirror.yml`
   - `README.md` (optional)
3. **Add the feed URL as a secret** (this keeps the token out of the public repo):
   Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `ELEMENTA_FEED_URL`
   - Value: the full Elementa feed URL (the long `…/feeds/products/MTY0…` link).
4. **Run it once**: **Actions** tab → enable workflows → **Elementa battery feed mirror** → **Run workflow**. After ~30s the three output files appear.
5. **Copy the two raw URLs** (open each file → **Raw**):
   - `https://raw.githubusercontent.com/<you>/hoba-elementa-mirror/main/elementa-feed-mirror.xml`
   - `https://raw.githubusercontent.com/<you>/hoba-elementa-mirror/main/baterije-pregled.csv`

It then refreshes itself **daily at 05:00 UTC** (edit the `cron` in `mirror.yml` for weekly).

---

## Point Stock Sync at the mirror (this is the whole inventory fix)

In Stock Sync, open the existing **Elementa** feed source and **change only the
download URL** to the mirror's `elementa-feed-mirror.xml` raw URL.

- **Keep the existing `lagerVp → quantity` mapping unchanged.** Battery rows now
  carry package counts, so Stock Sync writes the correct package stock to
  *Magacin dobavljača*. Non-battery products are identical to before → no change.
- **Leave price update OFF**, exactly as it is now. (Battery price is handled by
  the weekly job below.)

That's it for stock. Run a Stock Sync **dry-run/preview** first to confirm battery
quantities now show packages (VAR-P10 ≈ 20, not 121) and nothing else moved.

---

## Price (the weekly job)

Claude sets up a weekly scheduled task that:
1. fetches `baterije-pregled.csv` (the raw URL),
2. takes rows with `flag = safe` where `new_price ≠ current_price`,
3. writes the new price to Shopify via the hoba server,
4. emails you a summary and lists anything skipped.

Rows are **skipped** (price left alone, listed for you) when `flag` is:
- `charger` — chargers/punjači caught by the BATERIJE tag (you chose to exclude these),
- `big-swing` — change > ±40% (usually a data error or an odd pack, worth a human look),
- `no-feed-price` — Elementa has no price,
- `dup-key` — two Hoba SKUs collapse to the same Elementa code (a manual-merge case).

This keeps the dry-run-style guardrails and never auto-applies a suspicious jump.

---

## Notes

- **Token safety**: the feed URL/token lives only in the `ELEMENTA_FEED_URL`
  secret; the committed files never contain it.
- **Runs entirely on GitHub** — no computer or browser needs to be on.
- **Scope**: only `vendor = Elementa` + tag `BATERIJE`. New batteries are picked
  up automatically (the script re-reads hoba.rs each run).
- If you ever DO want Stock Sync to also manage battery price, that's possible but
  needs price scoped to battery SKUs only — ask Claude before enabling, so the
  ~8000 non-battery prices stay safe.
