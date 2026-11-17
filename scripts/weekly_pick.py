#!/usr/bin/env python3
"""Weekly rotation: sample 100 photos from the catalog, download and re-encode
them to webp, and regenerate index.html + manifest.json.

Runs on GitHub Actions (weekly cron) and locally. Visitors never touch source
URLs: sources are fetched server-side here, optimized copies are served from
the repo. Resize only - never crop the composition.

Safety: if fewer than MIN_OK photos validate, exit nonzero WITHOUT writing
output, so the previous week stays live.
"""
import io, json, math, os, random, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.join(os.path.dirname(__file__), "..")
UA = {"User-Agent": "ephemera-folio-weekly/1.0"}
TARGET = 100
MIN_OK = 90
MAX_SIDE = 1600
MIN_SIDE = 700
QUALITY = 74

def week_seed():
    import datetime
    iso = datetime.date.today().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"

def fetch_convert(entry):
    """Returns (bytes, w, h) or None. Validates identity of the payload as a
    real image, enforces resolution floor, re-encodes resize-only."""
    from PIL import Image
    try:
        req = urllib.request.Request(entry["url"], headers=UA)
        raw = urllib.request.urlopen(req, timeout=45).read()
        if len(raw) < 8000:
            return None
        im = Image.open(io.BytesIO(raw))
        im.load()
        w, h = im.size
        if max(w, h) < MIN_SIDE:
            return None
        im = im.convert("RGB")
        if max(w, h) > MAX_SIDE:
            s = MAX_SIDE / max(w, h)
            im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=QUALITY, method=4)
        b = buf.getvalue()
        return b, im.size[0], im.size[1]
    except Exception:
        return None

def main():
    catalog = json.load(open(os.path.join(BASE, "catalog", "catalog.json")))
    seed = week_seed()
    rng = random.Random("weekly-" + seed)
    order = list(range(len(catalog)))
    rng.shuffle(order)

    picked, results = [], {}
    def attempt(idx):
        e = catalog[idx]
        r = fetch_convert(e)
        return idx, e, r

    i = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        pending = []
        while len(picked) < TARGET and i < len(order):
            while len(pending) < 24 and i < len(order):
                pending.append(ex.submit(attempt, order[i]))
                i += 1
            for fut in list(pending):
                if fut.done():
                    pending.remove(fut)
                    idx, e, r = fut.result()
                    if r:
                        picked.append((e, r))
                    if len(picked) >= TARGET:
                        break
            time.sleep(0.05)
    print(f"week {seed}: {len(picked)} photos validated from {i} candidates")
    if len(picked) < MIN_OK:
        print("ABORT: too few validated photos; leaving previous week live")
        sys.exit(1)

    photos_dir = os.path.join(BASE, "photos")
    os.makedirs(photos_dir, exist_ok=True)
    manifest = []
    for n, (e, (b, w, h)) in enumerate(picked):
        fn = f"p{n:03d}.webp"
        with open(os.path.join(photos_dir, fn), "wb") as f:
            f.write(b)
        m = {"f": f"photos/{fn}", "t": e["t"], "a": e.get("a", ""), "d": e.get("d", ""),
             "lic": e["lic"], "page": e["page"], "w": w, "h": h}
        if e.get("licu"):
            m["licu"] = e["licu"]
        manifest.append(m)
    with open(os.path.join(BASE, "manifest.json"), "w") as f:
        json.dump(manifest, f, separators=(",", ":"), ensure_ascii=False)
    print("manifest +", len(manifest), "photos written")

if __name__ == "__main__":
    main()
