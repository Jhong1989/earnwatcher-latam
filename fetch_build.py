import os, time, hmac, hashlib, pathlib, json
from urllib.parse import urlencode
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from collections import defaultdict

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
REF_CODE = os.getenv("BINANCE_REF_CODE", "")
SITE_BASE_URL = (os.getenv("SITE_BASE_URL", "") or "").rstrip("/")

# Hosts a probar (el Secret BINANCE_BASE va primero si existe)
BASES = []
_env = os.getenv("BINANCE_BASE")
if _env:
    BASES.append(_env.rstrip("/"))
BASES += [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://api-gcp.binance.com",
]

OUT_DIR = pathlib.Path("site")
TEMPLATES_DIR = pathlib.Path("templates")
HEADERS = {"X-MBX-APIKEY": API_KEY}
CACHE_PATH = OUT_DIR / "data.json"

def to_float(x):
    try: return float(x)
    except Exception: return None

def demo_items():
    return [
        {"exchange":"Binance","type":"Flexible","asset":"USDT","apr":0.045,"duration_days":None,"min_purchase":"10","sold_out":False,"can_purchase":True,"product_id":"demo-flex-usdt"},
        {"exchange":"Binance","type":"Locked","asset":"BNB","apr":0.12,"duration_days":30,"min_purchase":"0.1","sold_out":False,"can_purchase":True,"product_id":"demo-lock-bnb-30"},
        {"exchange":"Binance","type":"Locked","asset":"BTC","apr":0.06,"duration_days":60,"min_purchase":"0.001","sold_out":True,"can_purchase":False,"product_id":"demo-lock-btc-60"},
    ]

def signed_get(path: str, params: dict) -> dict:
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Faltan BINANCE_API_KEY/SECRET en Secrets.")
    last_err = None
    for base in BASES:
        try:
            try:
                t = requests.get(f"{base}/api/v3/time", timeout=10).json()["serverTime"]
            except Exception:
                t = int(time.time() * 1000)
            p = dict(params or {})
            p.setdefault("recvWindow", 5000)
            p["timestamp"] = t
            q = urlencode(p, doseq=True)
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f"{base}{path}?{q}&signature={sig}"
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                print("Binance error @", base, ":", r.status_code, r.text[:200])
                r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("No fue posible consultar Binance en ninguno de los hosts.")

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

def normalize_products(flex_rows, lock_rows):
    items = []
    for r in (flex_rows or []):
        items.append({
            "exchange": "Binance", "type": "Flexible", "asset": r.get("asset"),
            "apr": to_float(r.get("latestAnnualPercentageRate")),
            "duration_days": None, "min_purchase": r.get("minPurchaseAmount"),
            "sold_out": r.get("isSoldOut"), "can_purchase": r.get("canPurchase"),
            "product_id": r.get("productId"),
        })
    for r in (lock_rows or []):
        d = r.get("detail", {}) or {}; q = r.get("quota", {}) or {}
        items.append({
            "exchange": "Binance", "type": "Locked", "asset": d.get("asset"),
            "apr": to_float(d.get("apr")), "duration_days": d.get("duration"),
            "min_purchase": q.get("minimum"), "sold_out": d.get("isSoldOut"),
            "can_purchase": not d.get("isSoldOut"), "product_id": r.get("projectId"),
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
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)),
                      autoescape=select_autoescape(["html","xml"]))
    ctx = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "ref_link": f"https://accounts.binance.com/register?ref={REF_CODE}" if REF_CODE else None,
           "site_base": SITE_BASE_URL, "note": note}
    html = env.get_template("index.html").render(items=items, by_asset=by_asset, **ctx)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")
    tmpl = env.get_template("asset.html")
    for asset, lst in by_asset.items():
        html = tmpl.render(asset=asset, items=lst, **ctx)
        (OUT_DIR / f"{asset}.html").write_text(html, encoding="utf-8")
    if SITE_BASE_URL:
        urls = [f"{SITE_BASE_URL}/", *[f"{SITE_BASE_URL}/{a}.html" for a in by_asset.keys()]]
        sm = ["<?xml version='1.0' encoding='UTF-8'?>",
              "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"]
        for u in urls: sm.append(f"<url><loc>{u}</loc></url>")
        sm.append("</urlset>")
        (OUT_DIR / "sitemap.xml").write_text("\n".join(sm), encoding="utf-8")

def write_robots():
    txt = f"User-agent: *\nAllow: /\n\nSitemap: {SITE_BASE_URL}/sitemap.xml\n" if SITE_BASE_URL else "User-agent: *\nAllow: /\n"
    (OUT_DIR / "robots.txt").write_text(txt, encoding="utf-8")

def save_cache(items):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f)

def load_cache():
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    try:
        if SITE_BASE_URL:
            r = requests.get(f"{SITE_BASE_URL}/data.json", timeout=10)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None

def safe_main():
    try:
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
            print("WARN(fetch):", repr(e))
            cached = load_cache()
            if cached:
                items = cached
                note = "Mostrando datos en caché por un problema temporal con la API."
            else:
                items = demo_items()
                note = "Datos de demostración (se reemplazarán cuando la API responda)."
        by_asset = group_by_asset(items)
        render_site(items, by_asset, note=note)
        write_robots()
        save_cache(items)
        print(f"OK. Publicado con {len(items)} items. Nota: {note or '—'}")
    except Exception as e:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "index.html").write_text(
            "<h1>Estamos actualizando los datos…</h1><p>Intenta de nuevo en unos minutos.</p>",
            encoding="utf-8"
        )
        write_robots()
        print("FATAL:", repr(e))

if __name__ == "__main__":
    import time
    safe_main()
