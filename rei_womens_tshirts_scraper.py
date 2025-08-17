import os, re, csv, json, asyncio, urllib.parse, random
from typing import Any, Dict, List, Optional, Tuple, Union
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Response

load_dotenv()

CATEGORY_URL = os.getenv("CATEGORY_URL", "https://www.rei.com/c/womens-t-shirts")
OUT_CSV = os.getenv("OUT_CSV", "rei_womens_tshirts.csv")

# Provider configuration
PROVIDER = os.getenv("PROVIDER", "scraperapi").lower().strip()  # "scraperapi" | "scrapingbee" | ""
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPINGBEE_KEY = os.getenv("SCRAPINGBEE_KEY", "").strip()
COUNTRY_CODE = os.getenv("COUNTRY_CODE", "us").lower().strip()
USE_PREMIUM = os.getenv("SCRAPERAPI_PREMIUM", "1").lower() in ("1", "true", "yes")
USE_ULTRA = os.getenv("SCRAPERAPI_ULTRA_PREMIUM", "0").lower() in ("1", "true", "yes")

# Choose where to apply provider wrapping
USE_PROVIDER_FOR_CATEGORY = os.getenv("USE_PROVIDER_FOR_CATEGORY", "0").lower() in ("1", "true", "yes")
USE_PROVIDER_FOR_PDP = os.getenv("USE_PROVIDER_FOR_PDP", "1").lower() in ("1", "true", "yes")

HEADLESS = os.getenv("HEADLESS", "0").lower() in ("1", "true", "yes")
LOAD_TIMEOUT_MS = int(os.getenv("LOAD_TIMEOUT_MS", "40000"))
STEP_SLEEP = (2, 5)   # ms range jitter between small actions
NAV_SLEEP  = (600, 1200)  # ms range jitter between navigations (increased for realism)
CONCURRENCY = int(os.getenv("CONCURRENCY", "2"))
MAX_PRODUCTS = int(os.getenv("MAX_PRODUCTS", "90"))

# Optional proxy: set PROXY_SERVER env var like "http://user:pass@host:port"
PROXY = os.getenv("PROXY_SERVER")  # None or "http://...."

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


def _rand_sleep(a_ms: int, b_ms: int) -> float:
    return random.uniform(a_ms/1000.0, b_ms/1000.0)


def _provider_is_configured() -> bool:
    if PROVIDER == "scraperapi" and SCRAPERAPI_KEY:
        return True
    if PROVIDER == "scrapingbee" and SCRAPINGBEE_KEY:
        return True
    return False


def build_provider_url(target_url: str, *, force_render: bool = True, session: Optional[int] = None) -> str:
    """
    Wraps a URL with the selected scraping provider endpoint so the request is fetched
    remotely and we receive the rendered HTML, avoiding Access Denied on direct visits.
    """
    if not _provider_is_configured():
        return target_url

    if PROVIDER == "scraperapi":
        base = "https://api.scraperapi.com/"
        params: Dict[str, Union[str, int]] = {
            "api_key": SCRAPERAPI_KEY,
            "url": target_url,
            "country_code": COUNTRY_CODE,
        }
        if force_render:
            params["render"] = "true"
        if USE_PREMIUM:
            params["premium"] = "true"
        if USE_ULTRA:
            params["ultra_premium"] = "true"
        if session is not None:
            params["session_number"] = str(session)
        return base + "?" + urllib.parse.urlencode(params, doseq=True)

    if PROVIDER == "scrapingbee":
        base = "https://app.scrapingbee.com/api/v1/"
        params: Dict[str, Union[str, int]] = {
            "api_key": SCRAPINGBEE_KEY,
            "url": target_url,
            "country_code": COUNTRY_CODE,
            "return_page_source": "true",
            "block_resources": "false",
        }
        if force_render:
            params["render_js"] = "true"
        if session is not None:
            params["session"] = str(session)
        return base + "?" + urllib.parse.urlencode(params, doseq=True)

    return target_url


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
    """
    Attempts to switch 'results per page' to 90.
    If no control is found, falls back to scrolling until we see >= 90 cards.
    """
    tried = False
    try:
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
                    options = await sel.evaluate("el => Array.from(el.options).map(o => ({value:o.value,text:o.text}))")
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
    """
    Scrolls the category page to load enough product cards.
    Note: Only effective when visiting the category directly (not via provider-returned static HTML).
    """
    last_count = 0
    attempts = 0
    while attempts < 20:
        count = await page.locator('a[href*="/product/"]').count()
        if count >= minimum:
            return
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=LOAD_TIMEOUT_MS)
        except Exception:
            pass
        await asyncio.sleep(_rand_sleep(*STEP_SLEEP))
        if count == last_count:
            attempts += 1
        else:
            attempts = 0
            last_count = count


async def collect_pdp_urls_from_category(page: Page) -> List[str]:
    await _maybe_click_cookies(page)
    await _set_90_per_page(page)
    # Only meaningful when not using provider for category
    if not USE_PROVIDER_FOR_CATEGORY:
        await _scroll_until_n_products(page, MAX_PRODUCTS)
    els = await page.locator('a[href*="/product/"]').all()
    hrefs: List[str] = []
    for el in els:
        href = await el.get_attribute("href")
        if not href:
            continue
        if "/product/" not in href:
            continue
        if href.startswith("http"):
            url = href
        else:
            url = "https://www.rei.com" + href
        hrefs.append(url.split("?")[0])

    seen = set()
    unique_urls: List[str] = []
    for u in hrefs:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    return unique_urls[:MAX_PRODUCTS]


def _safe_json_loads(txt: str) -> Optional[Union[dict, list]]:
    try:
        return json.loads(txt)
    except Exception:
        return None


async def extract_json_ld(page: Page) -> Tuple[Optional[dict], Optional[list], Optional[list]]:
    product, offers, breadcrumbs = None, None, None
    nodes = page.locator("script[type='application/ld+json']")
    n = await nodes.count()
    for i in range(n):
        try:
            raw = await nodes.nth(i).inner_text()
        except Exception:
            continue
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
                        breadcrumbs = [it.get("name") for it in (g.get("itemListElement") or []) if isinstance(it, dict) and isinstance(it.get("name"), str)]
            if obj.get("@type") == "Product":
                product = obj
                off = obj.get("offers")
                if isinstance(off, dict):
                    offers = [off]
                elif isinstance(off, list):
                    offers = off

            if obj.get("@type") == "BreadcrumbList" and not breadcrumbs:
                breadcrumbs = [it.get("name") for it in (obj.get("itemListElement") or []) if isinstance(it, dict) and isinstance(it.get("name"), str)]

    return product, offers, breadcrumbs


async def extract_next_data(page: Page) -> Optional[dict]:
    node = page.locator("#__NEXT_DATA__")
    if not await node.count():
        return None
    try:
        raw = await node.inner_text()
    except Exception:
        return None
    return _safe_json_loads(raw)


def _collect_variants_from_nextdata(nd: dict) -> List[dict]:
    variants: List[dict] = []
    seen = set()

    def walk(obj: Any):
        if isinstance(obj, dict):
            keys = {k.lower() for k in obj.keys()}
            if (
                ("sku" in keys or "skuid" in keys or "partnumber" in keys)
                and ({"size", "color"} & keys or "attributes" in keys or "upc" in keys)
            ):
                sku = obj.get("sku") or obj.get("skuId") or obj.get("partNumber")
                if sku and sku not in seen:
                    seen.add(sku)
                    attrs = obj.get("attributes") if isinstance(obj.get("attributes"), dict) else {}
                    variants.append({
                        "variant_sku": str(sku),
                        "size": obj.get("size") or attrs.get("size"),
                        "color": obj.get("color") or attrs.get("color"),
                        "upc": obj.get("upc"),
                        "gtin": obj.get("gtin") or obj.get("gtin13") or obj.get("gtin14"),
                        "price": (obj.get("price") or (obj.get("priceInfo") or {}).get("price")) if isinstance(obj.get("priceInfo"), dict) else obj.get("price"),
                        "currency": (obj.get("currency") or (obj.get("priceInfo") or {}).get("currency")),
                        "availability": obj.get("availability") or (obj.get("inventory") or {}).get("status"),
                        "image_url": obj.get("image") or (obj.get("images") or [None])[0] if isinstance(obj.get("images"), list) else None
                    })
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(nd)
    return variants


def _first(*vals):
    for v in vals:
        if v:
            return v
    return None


def _text(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def synthesize_rows(
    pdp_url: str,
    jsonld_product: Optional[dict],
    jsonld_offers: Optional[list],
    breadcrumbs: Optional[list],
    nextdata: Optional[dict]
) -> List[Dict[str, Any]]:
    name = _first(
        jsonld_product.get("name") if isinstance(jsonld_product, dict) else None,
        None
    )
    brand = None
    if isinstance(jsonld_product, dict):
        b = jsonld_product.get("brand")
        if isinstance(b, dict):
            brand = b.get("name") or b.get("brand")
        elif isinstance(b, str):
            brand = b

    description = _first(
        (jsonld_product or {}).get("description") if isinstance(jsonld_product, dict) else None,
        None
    )
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
            image_url = _first(
                (jsonld_product or {}).get("image"),
                off.get("image")
            )
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


async def _is_access_denied(page: Page) -> bool:
    try:
        html = await page.content()
        lower = html.lower()
        return (
            "access denied" in lower or
            "request blocked" in lower or
            "forbidden" in lower or
            "bot detected" in lower
        )
    except Exception:
        return False


async def goto_with_retries(
    page: Page,
    url: str,
    *,
    use_provider: bool,
    referer: Optional[str] = None,
    max_attempts: int = 3,
) -> None:
    """Navigate with retries. If provider is enabled and configured, rotate session on retry."""
    for attempt in range(1, max_attempts + 1):
        try:
            if use_provider and _provider_is_configured():
                session = None if attempt == 1 else random.randint(10_000, 9_999_999)
                target = build_provider_url(url, session=session)
            else:
                target = url
            resp = await page.goto(
                target,
                wait_until="domcontentloaded",
                timeout=LOAD_TIMEOUT_MS,
                referer=referer,
            )
            await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
            if await _is_access_denied(page) or (resp and resp.status and resp.status in (401, 403, 429)):
                if attempt == max_attempts:
                    return
                await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
                continue
            return
        except Exception:
            if attempt == max_attempts:
                raise
            await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
            continue


async def scrape_pdp(page: Page, url: str) -> List[Dict[str, Any]]:
    target_url = build_provider_url(url) if USE_PROVIDER_FOR_PDP else url
    response: Optional[Response] = None
    try:
        response = await page.goto(target_url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS, referer="https://www.rei.com/")
    except Exception:
        # Retry once with a new provider session if configured
        if _provider_is_configured() and USE_PROVIDER_FOR_PDP:
            alt = build_provider_url(url, session=random.randint(10_000, 9_999_999))
            response = await page.goto(alt, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS, referer="https://www.rei.com/")
        else:
            raise

    await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
    await _maybe_click_cookies(page)

    # If provider returned a block page or site blocked us, try one more provider session
    if await _is_access_denied(page) or (response and response.status and response.status in (401, 403)):
        if _provider_is_configured() and USE_PROVIDER_FOR_PDP:
            alt = build_provider_url(url, session=random.randint(10_000, 9_999_999))
            await page.goto(alt, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS, referer="https://www.rei.com/")
            await asyncio.sleep(_rand_sleep(*NAV_SLEEP))
        else:
            # Nothing else to do; we'll parse whatever we can
            pass

    try:
        await page.wait_for_load_state("networkidle", timeout=LOAD_TIMEOUT_MS)
    except Exception:
        pass

    jsonld_product, jsonld_offers, breadcrumbs = await extract_json_ld(page)
    nextdata = await extract_next_data(page)
    return synthesize_rows(url, jsonld_product, jsonld_offers, breadcrumbs, nextdata)


async def main():
    out = Path(OUT_CSV)
    out.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-http2",
            "--disable-gpu",
            "--disable-dev-shm-usage",
        ])
        context_kwargs = {
            "user_agent": UA,
            "viewport": {"width": 1320, "height": 900},
            "java_script_enabled": True,
            "locale": "en-US",
            "timezone_id": "America/Los_Angeles",
            "ignore_https_errors": True,
            # Extra headers can sometimes help avoid bot screens
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        }
        if PROXY:
            context_kwargs["proxy"] = {"server": PROXY}

        context = await browser.new_context(**context_kwargs)

        # Light stealth tweaks
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
        )

        page = await context.new_page()

        # CATEGORY: collect PDP URLs
        await goto_with_retries(
            page,
            CATEGORY_URL,
            use_provider=USE_PROVIDER_FOR_CATEGORY,
            referer="https://www.rei.com/",
            max_attempts=3,
        )
        await _maybe_click_cookies(page)
        try:
            await page.wait_for_selector('a[href*="/product/"]', timeout=LOAD_TIMEOUT_MS)
        except Exception:
            pass
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