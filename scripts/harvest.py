#!/usr/bin/env python3
"""Resumable, concurrent catalog harvester. Usage:
   python3 scripts/harvest.py <pool> [seconds]
Pools: loc-fsa, loc-matson, loc-detroit, commons, met, validate, finish
State per pool in catalog/state-<pool>.json; entries appended to catalog/parts/<pool>.jsonl
"""
import json, random, re, sys, time, urllib.parse, urllib.request, html, os
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "ephemera-folio-catalog/1.0 (catalog builder)"}
BASE = os.path.join(os.path.dirname(__file__), "..", "catalog")
PARTS = os.path.join(BASE, "parts")
os.makedirs(PARTS, exist_ok=True)

def get_json(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))

def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())

def strip_tags(s):
    return norm(html.unescape(re.sub(r"<[^>]+>", "", s or "")))

def load_state(pool):
    p = os.path.join(BASE, f"state-{pool}.json")
    if os.path.exists(p):
        return json.load(open(p))
    return {"done_pages": [], "order": None, "total_pages": None, "count": 0, "cursor": 0}

def save_state(pool, st):
    json.dump(st, open(os.path.join(BASE, f"state-{pool}.json"), "w"))

def append_entries(pool, entries):
    with open(os.path.join(PARTS, f"{pool}.jsonl"), "a") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

LOC_LIC = "No known restrictions (Library of Congress)"
LOC_LICU = "https://www.loc.gov/rr/print/res/rights.html"
LOC_POOLS = {
    "loc-fsa": ("partof:farm security administration/office of war information black-and-white negatives", 4000),
    "loc-matson": ("partof:matson (g. eric and edith) photograph collection", 2500),
    "loc-detroit": ("partof:detroit publishing company", 1500),
}

def loc_page(facet, sp):
    d = get_json("https://www.loc.gov/photos/?" + urllib.parse.urlencode(
        {"fo": "json", "c": 100, "sp": sp, "fa": facet}))
    out = []
    if not d:
        return sp, out, None
    for r in d.get("results", []):
        if r.get("unrestricted") is not True:
            continue
        urls = r.get("image_url") or []
        best = None
        for u in urls:
            u = u.split("#")[0]
            if u.endswith("v.jpg"):
                best = u
        if not best:
            for u in urls:
                u = u.split("#")[0]
                if u.endswith("r.jpg"):
                    best = u
        if not best:
            continue
        t = norm(r.get("title"))
        if not t or len(t) < 6:
            continue
        contrib = r.get("contributor") or []
        author = " & ".join(c.title() for c in contrib[:2]) if contrib else ""
        item_id = r.get("id", "").rstrip("/").rsplit("/", 1)[-1]
        out.append({"id": f"{item_id}", "url": best,
                    "page": (r.get("url") or r.get("id", "")).replace("http:", "https:"),
                    "t": t.rstrip("."), "a": author, "d": norm(r.get("date"))[:10],
                    "lic": LOC_LIC, "licu": LOC_LICU,
                    "_dk": re.sub(r"[^a-z0-9]", "", ((author or "loc") + " " + t).lower())[:60]})
    return sp, out, d.get("pagination", {}).get("total")

def run_loc(pool, budget_s):
    facet, target = LOC_POOLS[pool]
    st = load_state(pool)
    if st["count"] >= target:
        print(f"{pool}: already done ({st['count']})")
        return
    if st["order"] is None:
        sp, _, total = loc_page(facet, 1)
        if total is None:
            total = 2000
        st["total_pages"] = max(1, min(total, 2000))
        order = list(range(1, st["total_pages"] + 1))
        random.Random(pool).shuffle(order)
        st["order"] = order
        save_state(pool, st)
    t0 = time.time()
    done = set(st["done_pages"])
    pending = [p for p in st["order"] if p not in done]
    added = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = []
        it = iter(pending)
        active = {}
        def submit_more():
            while len(active) < 6:
                try:
                    p = next(it)
                except StopIteration:
                    return
                active[ex.submit(loc_page, facet, p)] = p
        submit_more()
        while active:
            for fut in list(active):
                if fut.done():
                    sp = active.pop(fut)
                    try:
                        _, entries, _ = fut.result()
                    except Exception:
                        entries = None
                    if entries is not None:
                        append_entries(pool, entries)
                        st["done_pages"].append(sp)
                        st["count"] += len(entries)
                        added += len(entries)
                    submit_more()
            save_state(pool, st)
            if st["count"] >= target or time.time() - t0 > budget_s:
                break
            time.sleep(0.3)
    print(f"{pool}: +{added} this run, total {st['count']}/{target}, pages done {len(st['done_pages'])}/{st['total_pages']}")

ALLOWED_LIC = ("public domain", "cc0", "cc by", "cc by-sa", "fal")

def commons_api(params):
    return get_json("https://commons.wikimedia.org/w/api.php?" +
                    urllib.parse.urlencode(dict(params, action="query", format="json")))

def run_commons(budget_s):
    st = load_state("commons")
    target = 2500
    t0 = time.time()
    rng = random.Random("commons2")
    cats = ["Category:Featured pictures in the public domain"]
    for top in ["Category:Featured pictures by photographic technique", "Category:Pictures of the Year"]:
        cont, subs = {}, []
        while True:
            d = commons_api({"list": "categorymembers", "cmtitle": top, "cmtype": "subcat", "cmlimit": 100, **cont})
            if not d:
                break
            subs += [m["title"] for m in d["query"]["categorymembers"]]
            if "continue" in d:
                cont = d["continue"]
            else:
                break
        rng.shuffle(subs)
        cats += subs
    rng.shuffle(cats)
    done_cats = set(st.get("done_cats", []))
    for c in cats:
        if st["count"] >= target or time.time() - t0 > budget_s:
            break
        if c in done_cats:
            continue
        cont, batch_all = {}, []
        while st["count"] + len(batch_all) < target and time.time() - t0 < budget_s:
            d = commons_api({"generator": "categorymembers", "gcmtitle": c, "gcmtype": "file",
                             "gcmlimit": 50, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
                             "iiurlwidth": 1280,
                             "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist", **cont})
            if not d:
                break
            for p in (d.get("query", {}).get("pages") or {}).values():
                ii = (p.get("imageinfo") or [{}])[0]
                em = ii.get("extmetadata", {})
                lic = norm(em.get("LicenseShortName", {}).get("value"))
                if not lic or not any(lic.lower().startswith(a) for a in ALLOWED_LIC):
                    continue
                thumb = ii.get("thumburl")
                if not thumb or (ii.get("width") or 0) < 1000 or (ii.get("height") or 0) < 800:
                    continue
                author = strip_tags(em.get("Artist", {}).get("value"))[:80]
                t = re.sub(r"^File:", "", p.get("title", ""))
                t = norm(re.sub(r"\.[a-zA-Z0-9]+$", "", t))[:140]
                if len(t) < 4:
                    continue
                licu = norm(em.get("LicenseUrl", {}).get("value")) or None
                e = {"id": str(p.get("pageid")), "url": thumb.split("?")[0],
                     "page": ii.get("descriptionurl"), "t": t, "a": author, "d": "", "lic": lic,
                     "_dk": re.sub(r"[^a-z0-9]", "", ((author or "c") + " " + t).lower())[:60]}
                if licu:
                    e["licu"] = licu
                batch_all.append(e)
            if "continue" in d:
                cont = d["continue"]
            else:
                break
        append_entries("commons", batch_all)
        st["count"] += len(batch_all)
        done_cats.add(c)
        st["done_cats"] = sorted(done_cats)
        save_state("commons", st)
        print(f"commons: {st['count']}/{target} (cat {c[:60]})", flush=True)

def run_met(budget_s):
    st = load_state("met")
    target = 1200
    t0 = time.time()
    if not st.get("ids"):
        ids, off = [], 0
        while len(ids) < 10000:
            d = get_json("https://collectionapi.metmuseum.org/public/collection/v1.1/search?" +
                         urllib.parse.urlencode({"isPublicDomain": "true", "departmentId": 19,
                                                 "q": "photograph", "limit": 100, "offset": off}))
            if not d or not d.get("objectIDs"):
                break
            ids += d["objectIDs"]
            off += 100
            time.sleep(0.05)
        random.Random("met").shuffle(ids)
        st["ids"] = ids
        save_state("met", st)
    ids = st["ids"]
    cur = st.get("cursor", 0)
    def fetch(oid):
        return get_json(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}")
    with ThreadPoolExecutor(max_workers=8) as ex:
        while st["count"] < target and cur < len(ids) and time.time() - t0 < budget_s:
            chunk = ids[cur:cur + 64]
            cur += len(chunk)
            batch = []
            for d in ex.map(fetch, chunk):
                if not d or not d.get("isPublicDomain") or not d.get("primaryImage"):
                    continue
                med = (d.get("medium") or "").lower()
                if not any(w in med for w in ["photograph", "gelatin", "salted", "daguerreotype",
                                              "albumen", "negative", "platinum", "collotype"]):
                    continue
                t = norm(d.get("title"))[:140]
                if not t:
                    continue
                author = norm(d.get("artistDisplayName"))[:80]
                batch.append({"id": str(d.get("objectID")),
                              "url": d.get("primaryImageSmall") or d["primaryImage"],
                              "page": d.get("objectURL"), "t": t, "a": author,
                              "d": norm(d.get("objectDate"))[:40],
                              "lic": "CC0 1.0 (Met Open Access)",
                              "licu": "https://creativecommons.org/publicdomain/zero/1.0/",
                              "_dk": re.sub(r"[^a-z0-9]", "", ((author or "met") + " " + t).lower())[:60]})
            append_entries("met", batch)
            st["count"] += len(batch)
            st["cursor"] = cur
            save_state("met", st)
            print(f"met: {st['count']}/{target} cursor {cur}", flush=True)

def run_validate(n=120):
    import collections
    entries = []
    for fn in os.listdir(PARTS):
        if fn.endswith(".jsonl"):
            for line in open(os.path.join(PARTS, fn)):
                entries.append(json.loads(line))
    rng = random.Random("validate")
    sample = rng.sample(entries, min(n, len(entries)))
    def head(e):
        try:
            req = urllib.request.Request(e["url"], method="HEAD", headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status == 200
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=10) as ex:
        ok = list(ex.map(head, sample))
    by_src = collections.Counter()
    ok_src = collections.Counter()
    for e, o in zip(sample, ok):
        s = e["id"]
        by_src[s] += 0  # placeholder
    print(f"URL validation: {sum(ok)}/{len(ok)} OK")

def run_finish():
    entries, seen = [], set()
    for fn in sorted(os.listdir(PARTS)):
        if not fn.endswith(".jsonl"):
            continue
        src = fn[:-6]
        for line in open(os.path.join(PARTS, fn)):
            e = json.loads(line)
            dk = e.pop("_dk", None)
            if dk in seen:
                continue
            seen.add(dk)
            e["src"] = src
            entries.append(e)
    random.Random("final").shuffle(entries)
    with open(os.path.join(BASE, "catalog.json"), "w") as f:
        json.dump(entries, f, separators=(",", ":"), ensure_ascii=False)
    import collections
    print("TOTAL:", len(entries))
    print(collections.Counter(e["src"] for e in entries))
    print("bytes:", os.path.getsize(os.path.join(BASE, "catalog.json")))

if __name__ == "__main__":
    pool = sys.argv[1]
    budget = float(sys.argv[2]) if len(sys.argv) > 2 else 95.0
    if pool in LOC_POOLS:
        run_loc(pool, budget)
    elif pool == "commons":
        run_commons(budget)
    elif pool == "met":
        run_met(budget)
    elif pool == "validate":
        run_validate()
    elif pool == "finish":
        run_finish()
