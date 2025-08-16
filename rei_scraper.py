import os, re, csv, json, asyncio, urllib.parse, random
from typing import Any, Dict, List, Optional, Tuple, Union
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

# --------------------
# Config / Env
# --------------------
load_dotenv()

CATEGORY_URL = os.getenv("CATEGORY_URL", "https://www.rei.com/c/womens-t-shirts")
OUT_CSV = os.getenv("OUT_CSV", "rei_womens_tshirts.csv")
MAX_PRODUCTS = int(os.getenv("MAX_PRODUCTS", "90"))

HEADLESS = os.getenv("HEADLESS", "0").lower() in ("1", "true", "yes")
LOAD_TIMEOUT_MS = int(os.getenv("LOAD_TIMEOUT_MS", "90000"))
STEP_SLEEP = (float(os.getenv("STEP_SLEEP_MIN", "1.5")), float(os.getenv("STEP_SLEEP_MAX", "2.5")))
NAV_SLEEP  = (float(os.getenv("NAV_SLEEP_MIN", "4.5")), float(os.getenv("NAV_SLEEP_MAX", "9.0")))
CONCURRENCY = int(os.getenv("CONCURRENCY", "1"))

PROVIDER = os.getenv("PROVIDER", "scraperapi").lower()  # scraperapi|scrapingbee
PROVIDER_MODE = os.getenv("PROVIDER_MODE", "proxy").lower()  # proxy|api
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "")
SCRAPINGBEE_KEY = os.getenv("SCRAPINGBEE_KEY", "")
COUNTRY_CODE = os.getenv("COUNTRY_CODE", "us")
USE_PREMIUM = os.getenv("SCRAPERAPI_PREMIUM", "1").lower() in ("1", "true", "yes")
USE_ULTRA = os.getenv("SCRAPERAPI_ULTRA_PREMIUM", "0").lower() in ("1", "true", "yes")
SESSION_NUMBER = os.getenv("SCRAPER_SESSION") or str(random.randint(100_000, 999_999))

# Optional proxy for the Playwright context itself (NOT the scraping provider)
PLAYWRIGHT_PROXY = os.getenv("PLAYWRIGHT_PROXY")  # e.g. http://user:pass@host:port

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CSV_FIELDS = [
    "parent_product_id", "product_name", "brand", "category_path", "pdp_url",
    "description", "variant_sku", "upc", "gtin", "color", "size",
    "price", "currency", "availability", "image_url"
]

# --------------------
# Helpers
# --------------------

def _rand_sleep(a_s: float, b_s: float) -> float:
    return random.uniform(a_s, b_s)


def _text(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _safe_json_loads(txt: str) -> Optional[Union[dict, list]]:
    try:
        return json.loads(txt)
    except Exception:
        return None


def _is_denied(html: str) -> bool:
    deny_markers = (
        "Access Denied",
        "Request blocked",
        "Access to this resource has been denied",
        "Access Denied: You don't have permission",
        "/challenge-platform/",
    )
    h = html or ""
    return any(m in h for m in deny_markers)


# --------------------
# Provider URL wrapper (URL-mode) used by router
# --------------------

def wrap_url(target_url: str, *, is_document: bool, session_number: Optional[str]) -> str:
    enc = urllib.parse.quote(target_url, safe="")
    if PROVIDER == "scraperapi":
        if not SCRAPERAPI_KEY:
            raise RuntimeError("Set SCRAPERAPI_KEY in your .env")
        params = [
            f"api_key={SCRAPERAPI_KEY}",
            f"url={enc}",
            f"country_code={COUNTRY_CODE}",
            "keep_headers=true",
            "device_type=desktop",
            # Render only for main documents for tougher bypass; subresources stay fast
            f"render={'true' if is_document else 'false'}",
        ]
        if session_number:
            params.append(f"session_number={session_number}")
        if USE_PREMIUM:
            params.append("premium=true")
        if USE_ULTRA:
            params.append("ultra_premium=true")
        return "https://api.scraperapi.com/?" + "&".join(params)

    if PROVIDER == "scrapingbee":
        if not SCRAPINGBEE_KEY:
            raise RuntimeError("Set SCRAPINGBEE_KEY in your .env")
        params = [
            f"api_key={SCRAPINGBEE_KEY}",
            f"url={enc}",
            f"country_code={COUNTRY_CODE}",
            "forward_headers=true",
            # Render only for main documents
            f"render_js={'true' if is_document else 'false'}",
        ]
        return "https://app.scrapingbee.com/api/v1/?" + "&".join(params)

    return target_url


# --------------------
# Routing: send ALL rei.com requests through provider
# --------------------

REI_HOSTS = ("www.rei.com", "rei.com", "images.rei.com", "cdn.rei.com", "api.rei.com", "assets.rei.com", "static.rei.com")

async def route_through_provider(route, request):
    url = request.url
    resource_type = request.resource_type

    # Skip non-HTTP(S) or provider/self calls
    if not url.startswith("http"):
        return await route.continue_()
    host = urllib.parse.urlparse(url).hostname or ""
    if host.endswith("api.scraperapi.com") or host.endswith("scraperapi.com"):
        return await route.continue_()
    if host.endswith("scrapingbee.com") or host.endswith("app.scrapingbee.com"):
        return await route.continue_()

    # Only proxy REI domains; leave others (e.g., metrics, CDNs) unless included above
    should_proxy = any(host.endswith(h) for h in REI_HOSTS)
    if not should_proxy:
        return await route.continue_()

    # Rebuild headers to avoid Playwright auto-added hop-by-hop headers when overriding
    headers = request.headers.copy()
    # Remove headers that can conflict when passing through provider
    for k in ["host", "content-length"]:
        headers.pop(k, None)

    is_document = resource_type in ("document", "main")
    new_url = wrap_url(url, is_document=is_document, session_number=SESSION_NUMBER)
    try:
        await route.continue_(url=new_url, headers=headers)
    except Exception:
        await route.fallback()


# --------------------
# Page utilities
# --------------------

async def _maybe_click_cookies(page: Page) -> None:
    selectors = [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "button:has-text('I Accept')",
        "button:has-text('Agree')",
    ]
    for sel in selectors:
        btn = page.locator(sel)
        try:
            if await btn.count():
                await btn.first.click(timeout=2000)
                await asyncio.sleep(_rand_sleep(*STEP_SLEEP))
                break
        except Exception:
            pass


async def _set_90_per_page(page: Page) -> None:
    try:
        tried = False
        select_candidates = [
            'select[aria-label*="Results per page"]',
            'select[aria-label*="results"]',
            'select[name*="pageSize"]',
        ]
        for sc in select_candidates:
            sel = page.locator(sc)
            if await sel.count():
                try:
                    await sel.select_option("90", timeout=2000)
                    tried = True
                    break
                except Exception:
                    options = await sel.evaluate(
                        "el => Array.from(el.options).map(o => ({value:o.value,text:o.text}))"
                    )
                    value_90 = None
                    for opt in options:
                        if "90" in opt.get("text", ""):
                            value_90 = opt["value"]
                            break
                    if value_90:
                        await sel.select_option(value_90, timeout=2000)
                        tried = True
                        break
        if not tried:
            btn = page.locator("button:has-text('90')").first
            if await btn.count():
                await btn.click(timeout=2000)
                tried = True
        if tried:
            await page.wait_for_load_state("networkidle", timeout=LOAD_TIMEOUT_MS)
            await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
    except Exception:
        pass


async def _scroll_until_n_products(page: Page, minimum: int) -> None:
    last = 0
    attempts = 0
    while attempts < 20:
        count = await page.locator('a[href*="/product/"]').count()
        if count >= minimum:
            return
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
        await page.wait_for_load_state("domcontentloaded", timeout=LOAD_TIMEOUT_MS)
        await asyncio.sleep(_rand_sleep(*STEP_SLEEP))
        if count == last:
            attempts += 1
        else:
            attempts = 0
            last = count


async def safe_goto(page: Page, url: str, want_selector: str, label: str) -> None:
    global SESSION_NUMBER
    attempts = 0
    while attempts < 3:
        await page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
        await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
        try:
            await page.wait_for_selector(want_selector, timeout=20_000)
        except Exception:
            pass
        html = await page.content()
        if _is_denied(html):
            Path(f"debug_denied_{label}_{attempts}.html").write_text(html[:4000])
            SESSION_NUMBER = str(random.randint(100_000, 999_999))
            attempts += 1
            await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
            continue
        return
    Path(f"debug_denied_{label}_final.html").write_text((await page.content())[:4000])
    raise RuntimeError(f"Failed to load {label} without Access Denied after retries")


# --------------------
# Extraction helpers
# --------------------

async def collect_pdp_urls_from_category(page: Page) -> List[str]:
    await _maybe_click_cookies(page)
    await _set_90_per_page(page)
    await _scroll_until_n_products(page, MAX_PRODUCTS)
    els = await page.locator('a[href*="/product/"]').all()
    hrefs = []
    for el in els:
        href = await el.get_attribute("href")
        if not href or "/product/" not in href:
            continue
        url = href if href.startswith("http") else "https://www.rei.com" + href
        hrefs.append(url.split("?")[0])
    seen, uniq = set(), []
    for u in hrefs:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:MAX_PRODUCTS]


async def extract_json_ld(page: Page) -> Tuple[Optional[dict], Optional[list], Optional[list]]:
    product, offers, breadcrumbs = None, None, None
    nodes = page.locator("script[type='application/ld+json']")
    n = await nodes.count()
    for i in range(n):
        raw = await nodes.nth(i).inner_text()
        data = _safe_json_loads(raw)
        if not data:
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type") or obj.get("@graph")
            if isinstance(t, list):
                for g in t:
                    if isinstance(g, dict) and g.get("@type") == "BreadcrumbList":
                        breadcrumbs = [
                            it.get("name")
                            for it in (g.get("itemListElement") or [])
                            if isinstance(it, dict) and isinstance(it.get("name"), str)
                        ]
            if obj.get("@type") == "Product":
                product = obj
                off = obj.get("offers")
                if isinstance(off, dict):
                    offers = [off]
                elif isinstance(off, list):
                    offers = off
            if obj.get("@type") == "BreadcrumbList" and not breadcrumbs:
                breadcrumbs = [
                    it.get("name")
                    for it in (obj.get("itemListElement") or [])
                    if isinstance(it, dict) and isinstance(it.get("name"), str)
                ]
    return product, offers, breadcrumbs


async def extract_next_data(page: Page) -> Optional[dict]:
    node = page.locator("#__NEXT_DATA__")
    if not await node.count():
        return None
    raw = await node.inner_text()
    return _safe_json_loads(raw)


def _collect_variants_from_nextdata(nd: dict) -> List[dict]:
    variants, seen = [], set()

    def walk(o: Any):
        if isinstance(o, dict):
            keys = {k.lower() for k in o.keys()}
            if (("sku" in keys or "skuid" in keys or "partnumber" in keys)
                and ({"size", "color"} & keys or "attributes" in keys or "upc" in keys)):
                sku = o.get("sku") or o.get("skuId") or o.get("partNumber")
                if sku and sku not in seen:
                    seen.add(sku)
                    variants.append({
                        "variant_sku": str(sku),
                        "size": o.get("size") or (o.get("attributes") or {}).get("size") if isinstance(o.get("attributes"), dict) else None,
                        "color": o.get("color") or (o.get("attributes") or {}).get("color") if isinstance(o.get("attributes"), dict) else None,
                        "upc": o.get("upc"),
                        "gtin": o.get("gtin") or o.get("gtin13") or o.get("gtin14"),
                        "price": (o.get("price") or (o.get("priceInfo") or {}).get("price")) if isinstance(o.get("priceInfo"), dict) else o.get("price"),
                        "currency": (o.get("currency") or (o.get("priceInfo") or {}).get("currency")),
                        "availability": o.get("availability") or (o.get("inventory") or {}).get("status"),
                        "image_url": o.get("image") or (o.get("images") or [None])[0] if isinstance(o.get("images"), list) else None
                    })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(nd)
    return variants


def _first(*vals):
    for v in vals:
        if v:
            return v
    return None


def synthesize_rows(
    pdp_url: str,
    jsonld_product: Optional[dict],
    jsonld_offers: Optional[list],
    breadcrumbs: Optional[list],
    nextdata: Optional[dict]
) -> List[Dict[str, Any]]:
    name = _first(jsonld_product.get("name") if isinstance(jsonld_product, dict) else None, None)
    brand = None
    if isinstance(jsonld_product, dict):
        b = jsonld_product.get("brand")
        if isinstance(b, dict):
            brand = b.get("name") or b.get("brand")
        elif isinstance(b, str):
            brand = b
    description = _first((jsonld_product or {}).get("description") if isinstance(jsonld_product, dict) else None, None)
    parent_product_id = _first(
        (jsonld_product or {}).get("mpn") if isinstance(jsonld_product, dict) else None,
        (jsonld_product or {}).get("sku") if isinstance(jsonld_product, dict) else None,
        None
    )
    cat_path = " > ".join([c for c in (breadcrumbs or []) if isinstance(c, str)]) if breadcrumbs else ""
    if nextdata:
        text = json.dumps(nextdata)
        m = re.search(r'"productId"\s*:\s*"([^"]+)"', text)
        if not parent_product_id and m:
            parent_product_id = m.group(1)
        if not name:
            m2 = re.search(r'"productName"\s*:\s*"([^"]+)"', text)
            if m2:
                name = m2.group(1)
        if not brand:
            m3 = re.search(r'"brand"\s*:\s*"([^"]+)"', text)
            if m3:
                brand = m3.group(1)
    nd_variants = _collect_variants_from_nextdata(nextdata) if nextdata else []
    rows: List[Dict[str, Any]] = []
    if jsonld_offers and any(isinstance(o, dict) for o in jsonld_offers):
        for off in jsonld_offers:
            if not isinstance(off, dict):
                continue
            sku = off.get("sku") or (jsonld_product or {}).get("sku")
            availability = off.get("availability")
            price = off.get("price")
            currency = off.get("priceCurrency") or (off.get("priceSpecification") or {}).get("priceCurrency")
            image_url = _first((jsonld_product or {}).get("image"), off.get("image"))
            upc = (jsonld_product or {}).get("gtin") or off.get("gtin")
            gtin = _first(
                (jsonld_product or {}).get("gtin14"),
                (jsonld_product or {}).get("gtin13"),
                off.get("gtin14") or off.get("gtin13") or off.get("gtin")
            )
            rows.append({
                "parent_product_id": _text(parent_product_id),
                "product_name": _text(name),
                "brand": _text(brand),
                "category_path": _text(cat_path),
                "pdp_url": pdp_url,
                "description": _text(description),
                "variant_sku": _text(sku),
                "upc": _text(upc),
                "gtin": _text(gtin),
                "color": "", "size": "",
                "price": _text(str(price) if price is not None else ""),
                "currency": _text(currency),
                "availability": _text(availability),
                "image_url": _text(image_url if isinstance(image_url, str) else "")
            })
    if nd_variants:
        existing_skus = {r["variant_sku"] for r in rows if r["variant_sku"]}
        for v in nd_variants:
            if v.get("variant_sku") in existing_skus:
                continue
            rows.append({
                "parent_product_id": _text(parent_product_id),
                "product_name": _text(name),
                "brand": _text(brand),
                "category_path": _text(cat_path),
                "pdp_url": pdp_url,
                "description": _text(description),
                "variant_sku": _text(v.get("variant_sku")),
                "upc": _text(v.get("upc")),
                "gtin": _text(v.get("gtin")),
                "color": _text(v.get("color")),
                "size": _text(v.get("size")),
                "price": _text(str(v.get("price")) if v.get("price") is not None else ""),
                "currency": _text(v.get("currency")),
                "availability": _text(v.get("availability")),
                "image_url": _text(v.get("image_url")),
            })
    if not rows:
        rows.append({
            "parent_product_id": _text(parent_product_id),
            "product_name": _text(name),
            "brand": _text(brand),
            "category_path": _text(cat_path),
            "pdp_url": pdp_url,
            "description": _text(description),
            "variant_sku": _text((jsonld_product or {}).get("sku") if isinstance(jsonld_product, dict) else ""),
            "upc": _text((jsonld_product or {}).get("gtin") if isinstance(jsonld_product, dict) else ""),
            "gtin": _text(_first(
                (jsonld_product or {}).get("gtin14") if isinstance(jsonld_product, dict) else None,
                (jsonld_product or {}).get("gtin13") if isinstance(jsonld_product, dict) else None
            )),
            "color": "", "size": "",
            "price": _text(""),
            "currency": _text(""),
            "availability": _text(""),
            "image_url": _text(_first(
                (jsonld_product or {}).get("image") if isinstance(jsonld_product, dict) else None,
                ""
            )),
        })
    return rows


async def scrape_pdp(page: Page, url: str) -> List[Dict[str, Any]]:
    await safe_goto(page, url, want_selector="script[type='application/ld+json']", label="pdp")
    await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
    await _maybe_click_cookies(page)
    try:
        await page.wait_for_load_state("networkidle", timeout=LOAD_TIMEOUT_MS)
    except Exception:
        pass
    jsonld_product, jsonld_offers, breadcrumbs = await extract_json_ld(page)
    nextdata = await extract_next_data(page)
    return synthesize_rows(url, jsonld_product, jsonld_offers, breadcrumbs, nextdata)


# --------------------
# Main
# --------------------

async def main():
    out = Path(OUT_CSV)
    out.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ])
        context_kwargs: Dict[str, Any] = {
            "user_agent": UA,
            "viewport": {"width": 1320, "height": 900},
            "java_script_enabled": True,
            "locale": "en-US",
            "timezone_id": "America/Los_Angeles",
            "extra_http_headers": {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
                "cache-control": "no-cache",
                "pragma": "no-cache",
                "dnt": "1",
                "sec-ch-ua": '"Google Chrome";v="139", "Chromium";v="139", "Not=A?Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            },
        }
        # Provider proxy-mode (preferred): proxies ALL requests including follow-ups
        if PROVIDER_MODE == "proxy" and PROVIDER == "scraperapi":
            if not SCRAPERAPI_KEY:
                raise RuntimeError("Set SCRAPERAPI_KEY in your .env for proxy mode")
            params = [
                f"country_code={COUNTRY_CODE}",
                "keep_headers=true",
                "device_type=desktop",
            ]
            if USE_PREMIUM:
                params.append("premium=true")
            if USE_ULTRA:
                params.append("ultra_premium=true")
            if SESSION_NUMBER:
                params.append(f"session_number={SESSION_NUMBER}")
            context_kwargs["proxy"] = {
                "server": "http://proxy-server.scraperapi.com:8001",
                "username": SCRAPERAPI_KEY,
                "password": "&".join(params),
            }
        elif PLAYWRIGHT_PROXY:
            context_kwargs["proxy"] = {"server": PLAYWRIGHT_PROXY}

        context = await browser.new_context(**context_kwargs)
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # In API mode only, route REI requests via provider URL wrapper
        if PROVIDER_MODE == "api":
            await context.route("**/*", route_through_provider)

        page = await context.new_page()

        # CATEGORY: collect PDP URLs
        await safe_goto(page, CATEGORY_URL, want_selector='a[href*="/product/"]', label="category")
        await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
        await _maybe_click_cookies(page)
        pdp_urls = await collect_pdp_urls_from_category(page)
        print(f"Collected {len(pdp_urls)} PDP URLs")
        await page.close()

        # PDP scraping with limited concurrency
        sem = asyncio.Semaphore(CONCURRENCY)
        rows: List[Dict[str, Any]] = []

        async def worker(url: str):
            async with sem:
                p = await context.new_page()
                try:
                    r = await scrape_pdp(p, url)
                    rows.extend(r)
                    print(f"\u2713 {url} -> {len(r)} row(s)")
                except Exception as e:
                    print(f"ERROR: {url} :: {e}")
                finally:
                    await p.close()
                await asyncio.sleep(_rand_sleep(*STEP_SLEEP))

        await asyncio.gather(*[worker(u) for u in pdp_urls])

        # Write CSV
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in CSV_FIELDS})

        print(f"Wrote {len(rows)} rows to {out.resolve()}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted.")