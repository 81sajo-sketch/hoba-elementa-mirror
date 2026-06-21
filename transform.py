#!/usr/bin/env python3
"""
Elementa battery feed transform (mirror).

Why this exists
---------------
Elementa sells batteries PER PIECE; Hoba sells them PER PACKAGE.
So two corrections are needed for battery SKUs only:
  * package price  = CEILING(elementa_piece_price * 0.96 * pack_size)
  * package stock  = FLOOR(elementa_piece_stock / pack_size)

This script downloads the raw Elementa feed + hoba.rs/products.json, and writes:
  1) elementa-feed-mirror.xml  -> a drop-in copy of the Elementa feed where, for
     Hoba battery SKUs ONLY, <lagerVp> (stock) is replaced by the package count.
     Everything else is left untouched. Stock Sync imports this instead of the
     raw feed; its existing lagerVp->quantity mapping then yields correct package
     stock. ZERO mapping changes, zero effect on non-battery products.
  2) baterije-pregled.csv -> per-battery review/changelog (sku, variant id,
     current price, new package price, pack size, piece stock, package stock,
     flag). The weekly price job consumes this (it is plain text, so a headless
     web_fetch can read it — unlike the gzipped raw feed).
  3) last-run.txt -> timestamp + counts.

Battery price is intentionally NOT forced through Stock Sync here (that would risk
the ~8000 non-battery Elementa products if price-sync were enabled globally).
Price is applied by the separate, guard-railed weekly job from pregled.csv.

The Elementa feed URL carries a private token, so it is read from the
ELEMENTA_FEED_URL repo secret — never hard-code it in a public repo.
"""

import os, re, gzip, csv, json, math, sys, datetime, urllib.request
import xml.etree.ElementTree as ET

FEED_URL = os.environ.get("ELEMENTA_FEED_URL", "").strip()
HOBA_BASE = "https://www.hoba.rs"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---- guardrail / business rules (keep in sync with the dry-run logic) ----
DISCOUNT = 0.96          # we sit a touch below Elementa
BIG_SWING = 0.40         # |new-current|/current > 40% => don't auto-price, flag
CHARGER_RE = re.compile(
    r'(punja[čc]|charger|XTAR-VC|XTAR-SC|XTAR-MC|akumulator za bu[šs]ilic|USB.*punja)',
    re.I)


def fetch(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    data = urllib.request.urlopen(req, timeout=timeout).read()
    if data[:2] == b"\x1f\x8b":          # raw gzip body
        data = gzip.decompress(data)
    return data


def norm(s):
    s = (s or "").strip().upper()
    s = re.sub(r"\s*\(.*?\)\s*$", "", s)   # drop trailing "(lager)" etc.
    s = re.sub(r"\s+LAGER$", "", s)        # drop trailing " lager"
    return s.strip()


def load_hoba_batteries():
    """norm(sku) -> {price, charger, vid, title, sku_raw}. Elementa vendor + BATERIJE tag."""
    out = {}
    page = 1
    while True:
        raw = fetch(f"{HOBA_BASE}/products.json?limit=250&page={page}")
        prods = json.loads(raw.decode("utf-8", "replace")).get("products", [])
        if not prods:
            break
        for p in prods:
            if (p.get("vendor") or "").strip().upper() != "ELEMENTA":
                continue
            tags = p.get("tags") or []
            tl = [t.strip().upper() for t in (tags if isinstance(tags, list) else str(tags).split(","))]
            if "BATERIJE" not in tl:
                continue
            charger = bool(CHARGER_RE.search((p.get("title") or "") + " " + (p.get("handle") or "")))
            for v in (p.get("variants") or []):
                sku = (v.get("sku") or "").strip()
                if not sku:
                    continue
                k = norm(sku)
                rec = {"price": float(v.get("price") or 0), "charger": charger,
                       "vid": v.get("id"), "title": p.get("title"), "sku_raw": sku}
                if k in out:
                    rec["dupkey"] = True   # two hoba SKUs normalise the same (manual-review case)
                out[k] = rec
        page += 1
    return out


def main():
    if not FEED_URL:
        sys.exit("ELEMENTA_FEED_URL is not set (add it as a repo secret).")

    # self-test of the math (fast, offline) -------------------------------
    assert math.ceil(69 * DISCOUNT * 6) == 398, "price formula drift"
    assert 121 // 6 == 20, "qty formula drift"

    feed_bytes = fetch(FEED_URL)
    root = ET.fromstring(feed_bytes)                 # <xml><products><product>...
    products = root.find("products")
    if products is None:
        sys.exit("Unexpected feed shape: no <products> node.")

    bats = load_hoba_batteries()

    rows = []
    counts = {"feed": 0, "matched": 0, "safe": 0, "charger": 0, "big_swing": 0,
              "qty_fixed": 0, "no_feed_price": 0}

    for prod in products.findall("product"):
        counts["feed"] += 1
        tid_el = prod.find("textId")
        if tid_el is None or not (tid_el.text or "").strip():
            continue
        key = norm(tid_el.text)
        rec = bats.get(key)
        if not rec:
            continue                                  # not a Hoba battery -> leave untouched
        counts["matched"] += 1

        def num(tag, cast, default):
            el = prod.find(tag)
            try:
                return cast((el.text or "").strip())
            except Exception:
                return default

        cena = num("cena", float, 0.0)
        paket = num("paket", int, 1) or 1
        lager = num("lagerVp", int, 0)
        cur = rec["price"]

        if cena <= 0:
            counts["no_feed_price"] += 1
            new_price = ""                            # leave price alone
            flag = "no-feed-price"
        else:
            new_price = math.ceil(cena * DISCOUNT * paket)
            big = cur > 0 and abs(new_price - cur) / cur > BIG_SWING
            if rec["charger"]:
                flag = "charger"
            elif big:
                flag = "big-swing"
            else:
                flag = "safe"
        if rec.get("dupkey"):
            flag += "|dup-key"

        new_qty = lager // paket if paket > 1 else lager

        # --- write package stock into the mirror XML (battery rows only) ---
        lager_el = prod.find("lagerVp")
        if lager_el is not None:
            lager_el.text = str(new_qty)
            counts["qty_fixed"] += 1

        if flag.startswith("safe"):
            counts["safe"] += 1
        elif flag.startswith("charger"):
            counts["charger"] += 1
        elif flag.startswith("big-swing"):
            counts["big_swing"] += 1

        rows.append({
            "sku": tid_el.text.strip(), "vid": rec["vid"], "current_price": cur,
            "new_price": new_price, "paket": paket, "lager_pieces": lager,
            "qty_packages": new_qty, "flag": flag, "hoba_sku": rec["sku_raw"],
            "title": rec["title"],
        })

    # ---- outputs ----
    ET.ElementTree(root).write("elementa-feed-mirror.xml", encoding="utf-8", xml_declaration=True)

    rows.sort(key=lambda r: (r["flag"], r["sku"]))
    with open("baterije-pregled.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "vid", "current_price", "new_price",
                                          "paket", "lager_pieces", "qty_packages",
                                          "flag", "hoba_sku", "title"])
        w.writeheader()
        w.writerows(rows)

    stamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with open("last-run.txt", "w", encoding="utf-8") as f:
        f.write(f"Last successful run: {stamp}\n")
        for k, v in counts.items():
            f.write(f"{k}: {v}\n")

    print("OK", stamp, counts)


if __name__ == "__main__":
    main()
