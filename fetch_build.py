import os, time, hmac, hashlib, pathlib, json
from urllib.parse import urlencode
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from collections import defaultdict

# === Config desde Secrets ===
API_KEY       = os.getenv("BINANCE_API_KEY", "")
API_SECRET    = os.getenv("BINANCE_API_SECRET", "")
REF_CODE      = os.getenv("BINANCE_REF_CODE", "")
SITE_BASE_URL = (os.getenv("SITE_BASE_URL", "") or "").rstrip("/")
BINANCE_BASE  = (os.getenv("BINANCE_BASE", "https://api.binance.com") or "").rstrip("/")

# === Paths ===
OUT_DIR       = pathlib.Path("site")
TEMPLATES_DIR = pathlib.Path("templates")
CACHE_PATH    = OUT_DIR / "data.json"
HEADERS       = {"X-MBX-APIKEY": API_KEY}

# ---------- Util ----------
def to_float(x):
    try:
        return float(x)
    except Exception:
        return None

# ---------- Binance (UN SOLO HOST) ----------
def signed_get(path: str, params: dict) -> dict:
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Faltan BINANCE_API_KEY/SECRET en Secrets.")
    # sincroniza tiempo (evita -1021)
    try:
        t = requests.get(f"{BINANCE_BASE}/api/v3/time", timeout=10).json()["serverTime"]
    except Exception:
        t = int(time.time() * 1000)

    p = dict(params or {})
    p.setdefault("recvWindow", 5000)
    p["timestamp"] = t

    q = urlencode(p, doseq=True)
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{BINANCE_BASE}{path}?{q}&signature={sig}"

    r = requests.get(url, headers=HEADERS, timeout=25)
    if r.status_code != 200:
        # deja rastro en logs para diagnosticar
        print("Binance error:", r.status_code, r.text[:200])
        r.raise_for_status()
    return r.json()

def fetch_all_rows(path: str, page_size=100, max_pages=50):
    rows = []
    for current in range(1, max_pages + 1):
        data = signed_get(path, {"size": page_size, "current": current})
        part = data.get("rows", []) or []
        rows.extend(part)
        total = data.get("total", 0)
        if len(rows) >= total or not part:
            break
        time.sleep(0.2)
    return rows

# ---------- Normalización y render ----------
def normalize_products(flex_rows, lock_rows):
    items = []
    for r in (flex_rows or []):
        items.append({
            "exchange": "Binance",
            "type": "Flexible",
            "asset": r.get("asset"),
            "apr": to_float(r.get("latestAnnualPercentageRate")),
            "duration_days": None,
            "min_purchase": r.get("minPurchaseAmount"),
            "sold_out": r.get("isSoldOut"),
            "can_purchase": r.get("canPurchase"),
            "product_id": r.get("productId"),
        })
    for r in (lock_rows or []):
        d = r.get("detail", {}) or {}
        q = r.get("quota", {}) or {}
        items.append({
            "exchange": "Binance",
            "type": "Locked",
            "asset": d.get("asset"),
            "apr": to_float(d.get("apr")),
            "duration_days": d.get("duration"),
            "min_purchase": q.get("minimum"),
            "sold_out": d.get("isSoldOut"),
            "can_purchase": not d.get("isSoldOut"),
            "product_id": r.get("projectId"),
        })

    items = [it for it in items if it.get("asset") and it.get("apr") is not None]
    items.sort(key=lambda x: (x["apr"] or 0.0), reverse=True)
    return items

def group_by_asset(items):
    g = defaultdict(list)
    for it in items:
        g[it["asset"]].append(it)
    for k in g:
        g[k].sort(key=lambda x: (x["type"] != "Locked", -(x["apr"] or 0), x.get("duration_days") or 0))
    return g

def render_site(items, by_asset, note=None):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"])
    )
    ctx = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ref_link": f"https://accounts.binance.com/register?ref={REF_CODE}" if REF_CODE else None,
        "site_base": SITE_BASE_URL,
        "note": note,
    }

    # index
    html = env.get_template("index.html").render(items=items, by_asset=by_asset, **ctx)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")

    # páginas por activo
    tmpl = env.get_template("asset.html")
    for asset, lst in by_asset.items():
        html = tmpl.render(asset=asset, items=lst, **ctx)
        (OUT_DIR / f"{asset}.html").write_text(html, encoding="utf-8")

    # sitemap
    if SITE_BASE_URL:
        urls = [f"{SITE_BASE_URL}/", *[f"{SITE_BASE_URL}/{a}.html" for a in by_asset.keys()]]
        sm = [
            "<?xml version='1.0' encoding='UTF-8'?>",
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>",
        ]
        for u in urls:
            sm.append(f"<url><loc>{u}</loc></url>")
        sm.append("</urlset>")
        (OUT_DIR / "sitemap.xml").write_text("\n".join(sm), encoding="utf-8")

def write_robots():
    txt = (
        f"User-agent: *\nAllow: /\n\nSitemap: {SITE_BASE_URL}/sitemap.xml\n"
        if SITE_BASE_URL else
        "User-agent: *\nAllow: /\n"
    )
    (OUT_DIR / "robots.txt").write_text(txt, encoding="utf-8")

def save_cache(items):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f)

def load_cache():
    # 0) lee directo del branch gh-pages (raw) → fuente "raw"
    slug = os.getenv("GITHUB_REPOSITORY", "")  # p.ej. "jhong1989/earnwatcher-latam" en Actions
    if slug:
        try:
            owner, repo = slug.split("/", 1)
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/gh-pages/data.json"
            r = requests.get(raw_url, timeout=12)
            if r.status_code == 200 and r.text.strip():
                return r.json(), "raw"
        except Exception:
            pass

    # 1) caché local del build anterior → fuente "local"
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f), "local"
        except Exception:
            pass

    # 2) como respaldo, desde el sitio público → fuente "site"
    try:
        if SITE_BASE_URL:
            r = requests.get(f"{SITE_BASE_URL}/data.json", timeout=10)
            if r.status_code == 200:
                return r.json(), "site"
    except Exception:
        pass

    return None, None

def main():
    note = None
    try:
        flex = fetch_all_rows("/sapi/v1/simple-earn/flexible/list")
        lock = fetch_all_rows("/sapi/v1/simple-earn/locked/list")
        items = normalize_products(flex, lock)
        if items:
            save_cache(items)
        else:
            note = "Sin ítems devueltos por la API en este ciclo."
    except Exception as e:
        print("WARN:", repr(e))
        cached, src = load_cache()
        if cached:
            items = cached
            # si viene del branch gh-pages (raw), NO mostrar aviso
            note = None if src == "raw" else "Mostrando datos en caché por un problema temporal con la API."
        else:
            # sin datos ni caché: página mínima
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / "index.html").write_text(
                "<h1>Estamos actualizando los datos…</h1><p>Intenta de nuevo en unos minutos.</p>",
                encoding="utf-8"
            )
            write_robots()
            return

    by_asset = group_by_asset(items)
    render_site(items, by_asset, note=note)
    write_robots()
    # guarda lo que mostramos
    save_cache(items)
    print(f"OK. Publicado con {len(items)} items. Nota: {note or '—'}")

if __name__ == "__main__":
    main()
