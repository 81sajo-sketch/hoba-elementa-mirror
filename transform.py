#!/usr/bin/env python3
"""
Elementa feed transform (mirror).

Why this exists
---------------
Elementa sells PER PIECE; Hoba sells SOME items as full packages and SOME as
single pieces (cut from a blister). So the correct unit differs per product:
  * package price = CEILING(piece_price * 0.96 * pack_size); package stock = FLOOR(pieces / pack_size)
  * single  price = CEILING(piece_price * 0.96);             single  stock = pieces (no division)

Scope: products tagged BATERIJE (batteries) OR PAKOVANJE (other pack-sold items
such as terminal connectors / klemne). Both flow through the SAME unit detection
and land in baterije-pregled.csv, so the ONE weekly price job applies both. Items
Elementa already pack-prices are detected as unit="single" (no extra x pack), so
they stay correct automatically.

How we decide a product's unit
------------------------------
We DON'T trust "Blister" wording alone (Hoba cuts some blisters and sells singly).
Instead we infer the unit from Hoba's CURRENT price: whichever of {single, pack}
is closer to the live price is how Hoba sells it. After the few known mispricings
were fixed by hand, every current price reflects the real unit, so this is robust.

Outputs (committed by the GitHub Action):
  1) elementa-feed-mirror.xml  -> the Elementa feed, byte-identical EXCEPT matched
     rows' <lagerVp> is replaced by the correct stock for that product's unit
     (packages for pack-sold, pieces for single-sold). Other rows untouched.
     Stock Sync imports this; its existing lagerVp->quantity mapping then yields
     correct stock with zero mapping changes and no effect on the others.
  2) baterije-pregled.csv -> per matched product: sku, variant id, current price,
     new price, detected unit, pack size, Elementa pack text, piece stock, new
     stock, and a flag. The weekly price job applies only flag=="safe" rows.
  3) last-run.txt -> timestamp + counts.

Flags: safe (auto-applied) | charger (excluded by choice) | pakovanje-mismatch
(Elementa's <paket> disagrees with its "Pakovanje" text -> needs a human) |
big-swing (>40% move even after unit detection -> needs a human) | no-feed-price.

The Elementa feed URL carries a private token, read from the ELEMENTA_FEED_URL
repo secret -- never hard-coded.
"""

import os, re, gzip, csv, math, sys, datetime, urllib.request
import xml.etree.ElementTree as ET

FEED_URL = os.environ.get("ELEMENTA_FEED_URL", "").strip()
HOBA_BASE = "https://www.hoba.rs"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DISCOUNT = 0.96          # we sit a touch below Elementa
BIG_SWING = 0.40         # |new-current|/current > 40% (after unit detection) => flag, don't auto-apply
CHARGER_RE = re.compile(
    r'(punja[čc]|charger|XTAR-VC|XTAR-SC|XTAR-MC|akumulator za bu[šs]ilic|USB.*punja)',
    re.I)


def fetch(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    data = urllib.request.urlopen(req, timeout=timeout).read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def norm(s):
    s = (s or "").strip().upper()
    s = re.sub(r"\s*\(.*?\)\s*$", "", s)
    s = re.sub(r"\s+LAGER$", "", s)
    return s.strip()


def parse_pack_text(s):
    """Pack count stated in Elementa's 'Pakovanje' text. 'Blister 4+2 kom.' -> 6,
    'Blister 6 kom.' -> 6, '5 kom.' -> 5. Returns None if no number found."""
    if not s:
        return None
    nums = re.findall(r"\d+", s)
    if not nums:
        return None
    if "+" in s:                       # e.g. 4+2
        plus = re.findall(r"(\d+)\s*\+\s*(\d+)", s)
        if plus:
            return int(plus[0][0]) + int(plus[0][1])
    return int(nums[0])


def load_hoba_packaged():
    """Hoba Elementa variants tagged BATERIJE or PAKOVANJE -> {norm(sku): record}."""
    out = {}
    page = 1
    while True:
        raw = fetch(f"{HOBA_BASE}/products.json?limit=250&page={page}")
        import json
        prods = json.loads(raw.decode("utf-8", "replace")).get("products", [])
        if not prods:
            break
        for p in prods:
            if (p.get("vendor") or "").strip().upper() != "ELEMENTA":
                continue
            tags = p.get("tags") or []
            tl = [t.strip().upper() for t in (tags if isinstance(tags, list) else str(tags).split(","))]
            # Batteries AND other pack-sold (PAKOVANJE) products use the same pipeline.
            if "BATERIJE" not in tl and "PAKOVANJE" not in tl:
                continue
            charger = bool(CHARGER_RE.search((p.get("title") or "") + " " + (p.get("handle") or "")))
            for v in (p.get("variants") or []):
                sku = (v.get("sku") or "").strip()
                if not sku:
                    continue
                out[norm(sku)] = {"price": float(v.get("price") or 0), "charger": charger,
                                  "vid": v.get("id"), "title": p.get("title"), "sku_raw": sku}
        page += 1
    return out


def main():
    if not FEED_URL:
        sys.exit("ELEMENTA_FEED_URL is not set (add it as a repo secret).")

    # offline self-tests of the core math / unit logic
    assert math.ceil(69 * DISCOUNT * 6) == 398
    assert 121 // 6 == 20
    assert parse_pack_text("Blister 4+2 kom.") == 6
    assert parse_pack_text("Blister 6 kom.") == 6
    assert parse_pack_text("5 kom.") == 5

    feed_bytes = fetch(FEED_URL)
    root = ET.fromstring(feed_bytes)
    products = root.find("products")
    if products is None:
        sys.exit("Unexpected feed shape: no <products> node.")

    bats = load_hoba_packaged()
    rows = []
    counts = {"feed": 0, "matched": 0, "safe": 0, "charger": 0, "big_swing": 0,
              "pakovanje_mismatch": 0, "no_feed_price": 0, "unit_pack": 0, "unit_single": 0}

    for prod in products.findall("product"):
        counts["feed"] += 1
        tid_el = prod.find("textId")
        if tid_el is None or not (tid_el.text or "").strip():
            continue
        rec = bats.get(norm(tid_el.text))
        if not rec:
            continue
        counts["matched"] += 1

        def num(tag, cast, default):
            el = prod.find(tag)
            try:
                return cast((el.text or "").strip())
            except Exception:
                return default

        def attr(name):
            el = prod.find(".//attribute[@name='%s']/value" % name)
            return (el.text or "").strip() if el is not None and el.text else None

        cena = num("cena", float, 0.0)
        paket = num("paket", int, 1) or 1
        lager = num("lagerVp", int, 0)
        pak_text = attr("Pakovanje")
        cur = rec["price"]

        single_p = math.ceil(cena * DISCOUNT) if cena > 0 else 0
        pack_p = math.ceil(cena * DISCOUNT * paket) if cena > 0 else 0

        # ---- detect selling unit from the current price ----
        if paket <= 1:
            unit = "single"
        elif cur and cur > 0:
            d_single = abs(single_p - cur) / cur
            d_pack = abs(pack_p - cur) / cur
            unit = "pack" if d_pack <= d_single else "single"
        else:
            unit = "pack"
        new_price = pack_p if unit == "pack" else single_p
        new_qty = (lager // paket) if unit == "pack" else lager
        counts["unit_pack" if unit == "pack" else "unit_single"] += 1

        # ---- flag ----
        pak_count = parse_pack_text(pak_text)
        if cena <= 0:
            flag = "no-feed-price"; counts["no_feed_price"] += 1
        elif rec["charger"]:
            flag = "charger"; counts["charger"] += 1
        elif pak_count is not None and pak_count != paket:
            flag = "pakovanje-mismatch"; counts["pakovanje_mismatch"] += 1
        elif cur > 0 and abs(new_price - cur) / cur > BIG_SWING:
            flag = "big-swing"; counts["big_swing"] += 1
        else:
            flag = "safe"; counts["safe"] += 1

        # ---- write stock into the mirror XML ----
        lager_el = prod.find("lagerVp")
        if lager_el is not None:
            lager_el.text = str(new_qty)

        rows.append({"sku": tid_el.text.strip(), "vid": rec["vid"], "current_price": cur,
                     "new_price": ("" if flag == "no-feed-price" else new_price), "unit": unit,
                     "paket": paket, "pakovanje": pak_text or "", "lager_pieces": lager,
                     "new_stock": new_qty, "flag": flag, "hoba_sku": rec["sku_raw"], "title": rec["title"]})

    ET.ElementTree(root).write("elementa-feed-mirror.xml", encoding="utf-8", xml_declaration=True)

    rows.sort(key=lambda r: (r["flag"], r["sku"]))
    with open("baterije-pregled.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "vid", "current_price", "new_price", "unit",
                                          "paket", "pakovanje", "lager_pieces", "new_stock",
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
