#!/usr/bin/env python3
"""Apply Move-Out Tracker changes (discounts, photo updates, Modify listing) to the public catalog.

The tracker artifact stores change requests; a Claude sync run reads them and calls this script.
No secrets here: Mercado Pago links are updated on Cristian's Mac with ~/MovingSale/mp_links.py --update.

Steps (run from the repo root):
  1. python3 tools/sync.py plan work/db_items.json work/db_requests.json
       db_items.json:    [{"id","version","data":{...}}]  posted items from db collection "items"
       db_requests.json: [{"id","version","data":{...}}]  docs from db collection "requests" with status pending/error
     -> work/plan.json        one entry per item to change
     -> work/mp_update.json   input for `mp_links.py --update` (only entries whose price changes)
     -> work/assets.txt       asset ids to download into work/photos/<assetId>.jpg
  2. (Mac) python3 ~/MovingSale/mp_links.py --update mp_update.json  > work/mp_result.json
     (Mercado Pago ids are the item codes, e.g. "MV-02"; titles are "MV-02 <title>", same as when the links were made.)
  3. python3 tools/sync.py apply work/plan.json work/mp_result.json work/photos
     -> edits items.json and images/, prints a summary
     -> work/db_ops.json      ops for the artifact db_write (item updates + request deletes)
  Sold items: python3 tools/sync.py sold MV-02 MV-14   -> status "vendido" in items.json (kept on the site as Vendido)
  Split payments: python3 tools/sync.py parts parts.json  -> marks paymentPlan parts paid in items.json
     parts.json is the output of (Mac) python3 ~/MovingSale/check_paid.py --parts 30: [{"code","part","date",...}].
     The list is the full truth: parts missing from it go back to unpaid (refunds).
  Share pages: python3 tools/sync.py pages             -> i/<CODE>/index.html for every item, en/index.html (English copy of
                                                          index.html) and en/i/<CODE>/index.html (apply and sold also run it)
     English item text lives in items.json as "en": {"title","description","size","condition"}; missing keys fall back to Spanish.
     Each page carries the item's title, price and first photo as link-preview tags (WhatsApp, iMessage, Facebook)
     and sends visitors on to index.html#<CODE>. Commit i/ and en/ together with items.json.
"""
import glob, hashlib, html, json, math, os, re, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ITEMS = os.path.join(ROOT, "items.json")
IMAGES = os.path.join(ROOT, "images")
WORK = os.path.join(ROOT, "work")
SHARE = os.path.join(ROOT, "i")
SITE = "https://caaronperez.github.io/venta-mudanza/"
MAX_PHOTOS = 4
SITE_FIELDS = ["title", "description", "category", "condition", "size", "brand", "originalPrice", "showOriginal", "held"]


def mp_link(cash):
    """Mercado Pago link amount so the seller nets the cash price (3.49% + $4, + IVA), rounded up to $10."""
    cash = float(cash or 0)
    if cash <= 0:
        return 0
    return int(math.ceil((cash + 4 * 1.16) / (1 - 0.0349 * 1.16) / 10) * 10)


def money(n):
    return "${:,.0f}".format(n)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def base_price(d):
    lp = float(d.get("listPrice") or 0)
    return lp if lp > 0 else float((d.get("proposal") or {}).get("price") or 0)


def plan(db_items_path, db_requests_path):
    items = {r["id"]: r for r in load(db_items_path)}
    reqs = {r["id"]: r for r in load(db_requests_path)}
    # Modify-listing changes (pendingUpdate on the item) count as requests too.
    ids = set(reqs) | {i for i, r in items.items() if isinstance(r["data"].get("pendingUpdate"), dict)}
    out, mp, assets, skipped = [], [], [], []
    for iid in sorted(ids):
        it = items.get(iid)
        rq = (reqs.get(iid) or {}).get("data") or {}
        if not it or it["data"].get("status") != "posted" or not it["data"].get("code"):
            skipped.append({"id": iid, "reason": "item is not live on the catalog"})
            continue
        d = it["data"]
        p = d.get("proposal") or {}
        pu = d.get("pendingUpdate") if isinstance(d.get("pendingUpdate"), dict) else {}
        cash_now = float(p.get("price") or 0)
        cash_new = float(rq.get("price") or 0) or cash_now
        base = base_price(d)
        photos = [x for x in (rq.get("photos") or pu.get("photos") or []) if isinstance(x, str)][:MAX_PHOTOS]
        e = {
            "itemId": iid, "version": it["version"], "code": d["code"], "sid": d.get("sid") or iid,
            "requestId": iid if iid in reqs else None, "requestVersion": (reqs.get(iid) or {}).get("version"),
            "hadPendingUpdate": bool(pu), "reqPrice": float(rq.get("price") or 0),
            "title": pu.get("title") or p.get("title") or d.get("name") or d["code"],
            "cashOld": cash_now, "cash": cash_new, "base": base or cash_new,
            "link": mp_link(cash_new), "linkOld": int(((d.get("payLinks") or {}).get("pickup") or {}).get("amount") or mp_link(cash_now)),
            "delivery": float(d.get("delivery") or 0),
            "photos": photos,
            "site": {k: pu[k] for k in SITE_FIELDS if k in pu},
            "proposal": p, "siteObj": d.get("site") or {},
        }
        pl = d.get("payLinks") or {}
        e["prefIds"] = {k: v["prefId"] for k, v in pl.items() if isinstance(v, dict) and v.get("prefId")}
        e["urls"] = {k: v["url"] for k, v in pl.items() if isinstance(v, dict) and v.get("url")}
        e["priceChange"] = cash_new != cash_now
        if e["priceChange"]:
            if not e["prefIds"]:
                skipped.append({"id": iid, "reason": "no Mercado Pago link to update"})
                continue
            mp.append({"id": e["code"], "title": e["code"] + " " + e["title"], "price": e["link"], "delivery": e["delivery"],
                       "prefIds": e["prefIds"], "urls": e["urls"]})
        assets += photos
        out.append(e)
    dump(os.path.join(WORK, "plan.json"), out)
    dump(os.path.join(WORK, "mp_update.json"), mp)
    with open(os.path.join(WORK, "assets.txt"), "w") as f:
        f.write("\n".join(assets) + ("\n" if assets else ""))
    print(json.dumps({"changes": len(out), "mercadoPagoUpdates": len(mp), "photos": len(assets), "skipped": skipped}, ensure_ascii=False))


def save_photo(src, dst):
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
        im.thumbnail((1400, 1400))
        im.save(dst, "JPEG", quality=82, optimize=True, progressive=True)
    except ImportError:
        import shutil
        shutil.copyfile(src, dst)


def apply(plan_path, mp_result_path, photos_dir):
    plan_ = load(plan_path)
    mp = load(mp_result_path) if mp_result_path and os.path.exists(mp_result_path) else {}
    cat = load(ITEMS)
    by_code = {x["code"]: x for x in cat}
    ops, notes = [], []
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")
    for e in plan_:
        c = by_code.get(e["code"])
        if c is None:
            notes.append(e["code"] + ": not in items.json, skipped")
            continue
        did, patch = [], {}
        prop, site = dict(e["proposal"]), dict(e["siteObj"])
        if e["priceChange"]:
            res = mp.get(e["code"])
            if not res or not all(k in res for k in e["prefIds"]):
                notes.append(e["code"] + ": Mercado Pago update missing, price not changed")
                if e["requestId"]:
                    ops.append({"op": "update", "path": "requests/" + e["itemId"], "ifVersion": e["requestVersion"],
                                "data": {"status": "error", "error": "Mercado Pago didn't confirm the new price", "failedAt": now}})
                continue
            for k, v in res.items():
                if v.get("url") and e["urls"].get(k) and v["url"] != e["urls"][k]:
                    raise SystemExit(e["code"] + ": Mercado Pago returned a different URL; stop and check")
            c["price"] = e["link"]
            c["cashPrice"] = int(e["cash"])
            was = mp_link(e["base"])
            if e["cash"] < e["base"] and was > e["link"]:
                c["wasPrice"] = was
            else:
                c.pop("wasPrice", None)
            pl = {k: {"url": e["urls"][k], "prefId": e["prefIds"][k], "amount": res[k]["amount"]} for k in e["prefIds"]}
            patch["payLinks"] = pl
            patch["listPrice"] = int(e["base"])
            prop["price"] = int(e["cash"])
            prop["linkPrice"] = e["link"]
            did.append("price " + money(e["cash"]) + " cash / " + money(e["link"]) + " MP")
        if e["photos"]:
            slug = e["code"].lower()
            tag = hashlib.sha1("".join(e["photos"]).encode()).hexdigest()[:6]
            new = []
            for n, aid in enumerate(e["photos"], 1):
                src = os.path.join(photos_dir, aid + ".jpg")
                if not os.path.exists(src):
                    hits = glob.glob(os.path.join(photos_dir, aid + ".*"))
                    if not hits:
                        raise SystemExit(e["code"] + ": photo " + aid + " not downloaded")
                    src = hits[0]
                rel = "images/%s-%d-%s.jpg" % (slug, n, tag)
                save_photo(src, os.path.join(ROOT, rel))
                new.append(rel)
            for old in c.get("photos") or []:
                if old.startswith("images/") and old not in new and os.path.exists(os.path.join(ROOT, old)):
                    os.remove(os.path.join(ROOT, old))
            c["photos"] = new
            patch["listingPhotos"] = e["photos"]
            did.append("%d photo%s" % (len(new), "s" if len(new) > 1 else ""))
        s = e["site"]
        for k in ["title", "description", "category", "condition", "size", "brand"]:
            if k in s:
                v = str(s[k]).strip()
                if v != c.get(k) and isinstance(c.get("en"), dict):
                    c["en"].pop(k, None)   # the English text no longer matches; en/ shows the Spanish until it's translated again
                c[k] = v
        if "originalPrice" in s or "showOriginal" in s:
            show = s.get("showOriginal", True) is not False
            op = int(float(s.get("originalPrice") or 0)) if "originalPrice" in s else None
            if op is not None:
                patch["originalPrice"] = op
                prop["originalPrice"] = op
            if show and op is not None:
                c["originalPrice"] = op
            elif not show:
                c["originalPrice"] = 0
        if "held" in s and c.get("status") != "vendido":
            c["status"] = "apartado" if s["held"] else "disponible"
        if s:
            for k in ["title", "description"]:
                if k in s:
                    prop[k] = str(s[k]).strip()
            for k in ["category", "condition", "size", "brand", "held"]:
                if k in s:
                    site[k] = s[k]
            if "showOriginal" in s:
                patch["showOriginal"] = s["showOriginal"] is not False
            did.append("listing details")
        if e["hadPendingUpdate"]:
            patch["pendingUpdate"] = {"__delete__": True}
        if prop != e["proposal"]:
            patch["proposal"] = prop
        if site != e["siteObj"]:
            patch["site"] = site
        patch["lastSync"] = {"at": now, "note": ", ".join(did)}
        patch["updatedAt"] = now
        ops.append({"op": "update", "path": "items/" + e["itemId"], "ifVersion": e["version"], "data": patch})
        if e["requestId"]:
            if e["reqPrice"] and e["reqPrice"] != e["cashOld"] and not e["priceChange"]:
                # Price couldn't go to Mercado Pago this run (Mac offline): keep it pending, drop what was applied.
                ops.append({"op": "update", "path": "requests/" + e["itemId"], "ifVersion": e["requestVersion"],
                            "data": {"photos": {"__delete__": True}}})
            else:
                ops.append({"op": "delete", "path": "requests/" + e["itemId"], "ifVersion": e["requestVersion"]})
        notes.append(e["code"] + ": " + ", ".join(did))
    with open(ITEMS, "w", encoding="utf-8") as f:
        f.write(json.dumps(cat, ensure_ascii=False, indent=1) + "\n")
    dump(os.path.join(WORK, "db_ops.json"), ops)
    share_pages()
    print("\n".join(notes))


def _img_size(rel):
    try:
        from PIL import Image
        with Image.open(os.path.join(ROOT, rel)) as im:
            return im.size
    except Exception:
        return None


EN_PAGE = [  # (Spanish, English) replacements that turn index.html into en/index.html; the page's JS translates the rest
    ('<html lang="es">', '<html lang="en">'),
    ('<title>Venta de mudanza · CDMX</title>', '<title>Moving sale · Mexico City</title>'),
    ('<meta name="description" content="Muebles y artículos en venta por mudanza en Ciudad de México. Paga con Mercado Pago hasta 3 meses sin intereses, o hazme una oferta por WhatsApp.">',
     '<meta name="description" content="Furniture and home goods for sale in Mexico City because I\'m moving. Pay with Mercado Pago in up to 3 interest-free installments, or make me an offer on WhatsApp.">'),
    ('<meta property="og:title" content="Venta de mudanza · CDMX">', '<meta property="og:title" content="Moving sale · Mexico City">'),
    ('<meta property="og:description" content="Recoge del 30 de noviembre al 13 de diciembre. Hasta 3 meses sin intereses con Mercado Pago.">',
     '<meta property="og:description" content="Pick up Nov 30 – Dec 13. Up to 3 interest-free installments with Mercado Pago.">'),
    ('<meta property="og:url" content="https://caaronperez.github.io/venta-mudanza/">', '<meta property="og:url" content="https://caaronperez.github.io/venta-mudanza/en/">'),
    ('src="images/', 'src="../images/'),
    ('src="brand/', 'src="../brand/'),
    ('href="brand/', 'href="../brand/'),
    ('<link rel="manifest" href="manifest.json">', '<link rel="manifest" href="../manifest.json">'),
]


def build_en():
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        page = f.read()
    for a, b in EN_PAGE:
        if a not in page and a.startswith("<"):
            raise SystemExit("en/index.html: index.html no longer contains " + a[:60])
        page = page.replace(a, b)
    os.makedirs(os.path.join(ROOT, "en"), exist_ok=True)
    with open(os.path.join(ROOT, "en", "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


COND_EN = {"Nuevo": "New", "Como nuevo": "Like new", "Buen estado": "Good condition", "Con detalles de uso": "Shows some wear"}


def share_pages():
    """i/<CODE>/ and en/i/<CODE>/ for every catalog item: link-preview tags + redirect to the item; also rebuilds en/index.html."""
    build_en()
    cat = load(ITEMS)
    keep = set()
    for lang in ("es", "en"):
        base_dir = SHARE if lang == "es" else os.path.join(ROOT, "en", "i")
        os.makedirs(base_dir, exist_ok=True)
        L = {"es": {"sold": "Vendido · ", "msi": " · hasta 3 MSI", "cash": "Efectivo ", "site": "Venta de mudanza · CDMX",
                    "locale": "es_MX", "suffix": " · Venta de mudanza CDMX", "see": "Ver el artículo en el catálogo", "hl": "es"},
             "en": {"sold": "Sold · ", "msi": " · up to 3 interest-free payments", "cash": "Cash ", "site": "Moving sale · Mexico City",
                    "locale": "en_US", "suffix": " · Moving sale Mexico City", "see": "See the item in the catalog", "hl": "en"}}[lang]
        prefix = SITE + ("" if lang == "es" else "en/")
        up = "../../" if lang == "es" else "../../../"
        for c in cat:
            code = str(c.get("code") or "")
            if not re.match(r"^MV-\d+$", code) or not c.get("title"):
                continue
            keep.add(code)
            en = c.get("en") if isinstance(c.get("en"), dict) else {}
            name = str((en.get("title") if lang == "en" else None) or c["title"]).strip()
            text = str((en.get("description") if lang == "en" else None) or c.get("description") or "")
            sold_ = c.get("status") == "vendido"
            price = float(c.get("price") or 0)
            cash = float(c.get("cashPrice") or 0)
            title = (L["sold"] if sold_ else "") + name
            # Cards top out at $300,000 per payment on Mercado Pago MX, so only promise MSI below that.
            bits = [money(price) + " MXN" + (L["msi"] if not sold_ and 0 < price <= 300000 else "")]
            if cash > 0 and not sold_ and cash < price:
                bits.append(L["cash"] + money(cash))
            desc = " · ".join(bits) + ". " + re.sub(r"\s+", " ", text).strip()
            if len(desc) > 200:
                desc = desc[:197].rsplit(" ", 1)[0] + "…"
            photo = next((p for p in (c.get("photos") or []) if isinstance(p, str) and re.match(r"^images/[\w.\-/]+$", p)), "images/hero/IMG_4221.jpg")
            size = _img_size(photo)
            url = prefix + "i/" + code + "/"
            target = "../../#" + code
            e = lambda v: html.escape(str(v), quote=True)
            dims = (f'<meta property="og:image:width" content="{size[0]}">\n<meta property="og:image:height" content="{size[1]}">\n') if size else ""
            page = f"""<!doctype html>
<html lang="{L['hl']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}{e(L['suffix'])}</title>
<meta name="description" content="{e(desc)}">
<meta property="og:type" content="product">
<meta property="og:site_name" content="{e(L['site'])}">
<meta property="og:locale" content="{L['locale']}">
<meta property="og:url" content="{e(url)}">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:image" content="{e(SITE + photo)}">
<meta property="og:image:secure_url" content="{e(SITE + photo)}">
<meta property="og:image:type" content="image/jpeg">
{dims}<meta property="og:image:alt" content="{e(name)}">
<meta property="product:price:amount" content="{int(round(price))}">
<meta property="product:price:currency" content="MXN">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(desc)}">
<meta name="twitter:image" content="{e(SITE + photo)}">
<script>location.replace("{target}");</script>
<style>body{{margin:0;font:16px/1.5 system-ui,sans-serif;background:#F6F1E6;color:#14251B;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box;text-align:center}}img{{max-width:min(420px,100%);border-radius:16px}}a{{color:#1E3A2B;font-weight:600}}</style>
</head>
<body>
<main>
<img src="{up}{e(photo)}" alt="{e(name)}">
<h1>{e(title)}</h1>
<p>{e(money(price))} MXN</p>
<p><a href="{target}">{e(L['see'])}</a></p>
</main>
</body>
</html>
"""
            d = os.path.join(base_dir, code)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
                f.write(page)
        for d in glob.glob(os.path.join(base_dir, "MV-*")):
            if os.path.basename(d) not in keep:
                shutil.rmtree(d, ignore_errors=True)
    return sorted(keep)


def parts(parts_path):
    """Mark the paid parts of split payments (items.json paymentPlan[].paid / paidAt) from check_paid.py --parts."""
    paid = {}
    for r in load(parts_path):
        try:
            paid[(str(r["code"]), int(r["part"]))] = r.get("date")
        except (KeyError, TypeError, ValueError):
            continue
    cat = load(ITEMS)
    changed = []
    for c in cat:
        plan_ = c.get("paymentPlan")
        if not isinstance(plan_, list):
            continue
        before = json.dumps(plan_, sort_keys=True)
        for k, pt in enumerate(plan_, 1):
            n = int(pt.get("n") or k)
            if (c["code"], n) in paid:
                pt["paid"] = True
                pt["paidAt"] = paid[(c["code"], n)] or pt.get("paidAt")
            else:
                pt.pop("paid", None)
                pt.pop("paidAt", None)
        if json.dumps(plan_, sort_keys=True) != before:
            done = sum(1 for pt in plan_ if pt.get("paid"))
            changed.append("%s %d/%d" % (c["code"], done, len(plan_)))
    if changed:
        with open(ITEMS, "w", encoding="utf-8") as f:
            f.write(json.dumps(cat, ensure_ascii=False, indent=1) + "\n")
        share_pages()
    print(json.dumps({"partsChanged": changed}))


def sold(codes):
    """Mark items as sold on the catalog (they stay visible with the "Vendido" badge)."""
    cat = load(ITEMS)
    changed = []
    for c in cat:
        if c.get("code") in codes and c.get("status") != "vendido":
            c["status"] = "vendido"
            changed.append(c["code"])
    with open(ITEMS, "w", encoding="utf-8") as f:
        f.write(json.dumps(cat, ensure_ascii=False, indent=1) + "\n")
    share_pages()
    print(json.dumps({"markedSold": changed}))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "plan":
        plan(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2 and sys.argv[1] == "sold":
        sold(set(sys.argv[2:]))
    elif len(sys.argv) == 5 and sys.argv[1] == "apply":
        apply(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) == 3 and sys.argv[1] == "parts":
        parts(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "pages":
        print(json.dumps({"sharePages": len(share_pages())}))
    else:
        sys.exit(__doc__)
