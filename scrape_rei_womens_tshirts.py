#!/usr/bin/env python3

import argparse
import csv
import json
import random
import re
import sys
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup


DEFAULT_URL = "https://www.rei.com/c/womens-t-shirts?ir=category%3Awomens-t-shirts"
DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.8
DEFAULT_DELAY_SECONDS = 1.0

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "DNT": "1",
}


def create_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.cookies.set("akaas_BotMitigation", "0")
    return session


def fetch_html(
    session: requests.Session,
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_seconds = (backoff_base ** attempt) + random.uniform(0, 0.25)
            time.sleep(sleep_seconds)
    if last_error is None:
        last_error = RuntimeError("Unknown error fetching HTML")
    raise last_error


def parse_json_ld(soup: BeautifulSoup) -> List[Dict[str, Optional[str]]]:
    items: List[Dict[str, Optional[str]]] = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        text = script.string or script.get_text("", strip=True)
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            # Some pages embed multiple JSON objects without wrapping array; try to parse line-by-line
            # or fallback to a relaxed strategy
            continue
        for product in _iter_possible_products_from_json(data):
            items.append(product)
    return items


def _iter_possible_products_from_json(data) -> Iterable[Dict[str, Optional[str]]]:
    if isinstance(data, list):
        for element in data:
            yield from _iter_possible_products_from_json(element)
        return

    if isinstance(data, dict):
        # JSON-LD Product type
        if (data.get("@type") == "Product") or (
            isinstance(data.get("@type"), list) and "Product" in data.get("@type")
        ):
            name = _safe_str(data.get("name"))
            url = _safe_str(data.get("url"))
            image = _extract_image_from_json(data)
            brand = _extract_brand_from_json(data)
            price = _extract_price_from_json(data)
            rating, reviews = _extract_rating_from_json(data)
            yield {
                "name": name,
                "url": url,
                "brand": brand,
                "price": price,
                "rating": rating,
                "reviews": reviews,
                "image": image,
            }
        # Recurse
        for value in data.values():
            yield from _iter_possible_products_from_json(value)
        return

    # Non-dict/list values are ignored


def _safe_str(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(value).strip() or None
    except Exception:  # noqa: BLE001
        return None


def _extract_image_from_json(data: Dict) -> Optional[str]:
    image = data.get("image")
    if isinstance(image, list) and image:
        return _safe_str(image[0])
    return _safe_str(image)


def _extract_brand_from_json(data: Dict) -> Optional[str]:
    brand = data.get("brand")
    if isinstance(brand, dict):
        return _safe_str(brand.get("name"))
    return _safe_str(brand)


def _extract_price_from_json(data: Dict) -> Optional[str]:
    offers = data.get("offers")
    # Offers may be a dict or a list
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict):
        price = offers.get("price") or offers.get("priceSpecification", {}).get("price")
        currency = offers.get("priceCurrency")
        if price and currency:
            return f"{price} {currency}"
        if price:
            return str(price)
    return None


def _extract_rating_from_json(data: Dict) -> Tuple[Optional[str], Optional[str]]:
    aggregate_rating = data.get("aggregateRating")
    if isinstance(aggregate_rating, dict):
        rating = _safe_str(
            aggregate_rating.get("ratingValue") or aggregate_rating.get("rating")
        )
        reviews = _safe_str(
            aggregate_rating.get("reviewCount") or aggregate_rating.get("ratingCount")
        )
        return rating, reviews
    return None, None


def parse_dom_for_products(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Optional[str]]]:
    products_by_url: Dict[str, Dict[str, Optional[str]]] = {}

    # Strategy 1: anchors pointing to product pages
    for a in soup.select('a[href^="/product/"]'):
        href = a.get("href")
        if not href:
            continue
        full_url = urljoin(base_url, href)

        # Identify a probable container
        container = a
        for _ in range(6):
            if container is None:
                break
            if _is_probable_product_container(container):
                break
            container = container.parent
        if container is None:
            container = a.parent

        name = (
            a.get("aria-label")
            or (a.get_text(" ", strip=True) or None)
            or _extract_img_alt(a)
        )
        price = _find_price_near(container)
        brand = _find_brand_near(container)
        rating, reviews = _find_rating_near(container)
        image = _extract_img_src(a) or _extract_img_src(container)

        product = products_by_url.get(full_url)
        if product is None:
            products_by_url[full_url] = {
                "name": name,
                "url": full_url,
                "brand": brand,
                "price": price,
                "rating": rating,
                "reviews": reviews,
                "image": image,
            }
        else:
            # Prefer longer/more descriptive names
            if (not product.get("name")) or (
                name and len(name) > len(product.get("name") or "")
            ):
                product["name"] = name
            if not product.get("price") and price:
                product["price"] = price
            if not product.get("brand") and brand:
                product["brand"] = brand
            if not product.get("rating") and rating:
                product["rating"] = rating
            if not product.get("reviews") and reviews:
                product["reviews"] = reviews
            if not product.get("image") and image:
                product["image"] = image

    # Strategy 2: common data-qa hooks
    for card in soup.select('[data-qa*="product" i], [data-test*="product" i]'):
        name_el = card.select_one('[data-qa*="name" i], [data-test*="name" i]')
        price_el = card.select_one('[data-qa*="price" i], [data-test*="price" i]')
        link_el = card.select_one('a[href^="/product/"]')
        if not link_el:
            continue
        full_url = urljoin(base_url, link_el.get("href"))
        image = _extract_img_src(card)
        brand = _find_brand_near(card)
        rating, reviews = _find_rating_near(card)
        name = _safe_str(name_el.get_text(" ", strip=True)) if name_el else None
        price = (
            _safe_str(price_el.get_text(" ", strip=True)) if price_el else _find_price_near(card)
        )
        product = products_by_url.get(full_url)
        if product is None:
            products_by_url[full_url] = {
                "name": name,
                "url": full_url,
                "brand": brand,
                "price": price,
                "rating": rating,
                "reviews": reviews,
                "image": image,
            }
        else:
            if (not product.get("name")) or (
                name and len(name) > len(product.get("name") or "")
            ):
                product["name"] = name
            if not product.get("price") and price:
                product["price"] = price
            if not product.get("brand") and brand:
                product["brand"] = brand
            if not product.get("rating") and rating:
                product["rating"] = rating
            if not product.get("reviews") and reviews:
                product["reviews"] = reviews
            if not product.get("image") and image:
                product["image"] = image

    # Convert to list
    return list(products_by_url.values())


def _is_probable_product_container(tag) -> bool:
    if not hasattr(tag, "attrs"):
        return False
    classes = " ".join(tag.get("class", []))
    tag_id = tag.get("id") or ""
    data_qa = tag.get("data-qa") or tag.get("data-test") or ""
    name = getattr(tag, "name", "")
    return (
        ("product" in classes.lower())
        or ("product" in (tag_id.lower() if tag_id else ""))
        or ("product" in data_qa.lower())
        or (name in ("article", "li"))
    )


def _extract_img_alt(tag) -> Optional[str]:
    img = tag.find("img") if hasattr(tag, "find") else None
    if img:
        return _safe_str(img.get("alt"))
    return None


def _extract_img_src(tag) -> Optional[str]:
    if not hasattr(tag, "find"):
        return None
    img = tag.find("img")
    if not img:
        return None
    for key in ("src", "data-src", "data-original", "data-lazy-src"):
        src = img.get(key)
        if src:
            return str(src)
    srcset = img.get("srcset")
    if srcset:
        # Choose the first URL in srcset
        first = srcset.split(",")[0].strip().split(" ")[0]
        return first or None
    return None


def _find_price_near(tag) -> Optional[str]:
    if not hasattr(tag, "find_all"):
        return None
    price_like = tag.find_all(string=re.compile(r"\$\s*\d"))
    if price_like:
        # Prefer the shortest price-like string (less noise like variants)
        candidate = sorted((s.strip() for s in price_like if s and s.strip()), key=len)[0]
        return candidate
    # Look for attributes labeled price
    price_el = tag.find(attrs={"data-qa": re.compile("price", re.I)})
    if price_el:
        return _safe_str(price_el.get_text(" ", strip=True))
    return None


def _find_brand_near(tag) -> Optional[str]:
    if not hasattr(tag, "find_all"):
        return None
    # Common brand placements
    brand_el = tag.find(attrs={"data-qa": re.compile("brand", re.I)})
    if brand_el:
        return _safe_str(brand_el.get_text(" ", strip=True))
    # Fallback: text preceding product title often shows brand as first token
    text_bits = [t.strip() for t in tag.stripped_strings]
    for bit in text_bits[:4]:
        if 1 <= len(bit) <= 40 and not re.search(r"\$\d", bit):
            # Heuristic: likely brand-like
            return bit
    return None


def _find_rating_near(tag) -> Tuple[Optional[str], Optional[str]]:
    if not hasattr(tag, "find_all"):
        return None, None
    # aria-label like: "4.5 out of 5 stars (123)"
    label_el = tag.find(attrs={"aria-label": re.compile("out of 5", re.I)})
    if label_el:
        text = label_el.get("aria-label")
        if text:
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*out of\s*5", text)
            rating = match.group(1) if match else None
            rev_match = re.search(r"\((\d+)\)", text)
            reviews = rev_match.group(1) if rev_match else None
            return rating, reviews
    # Rating text nodes
    text_nodes = tag.find_all(string=re.compile(r"out of 5", re.I))
    for node in text_nodes:
        text = str(node)
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*out of\s*5", text)
        if match:
            rating = match.group(1)
            rev_match = re.search(r"\((\d+)\)", text)
            reviews = rev_match.group(1) if rev_match else None
            return rating, reviews
    return None, None


def normalize_url(url: Optional[str], base_url: str) -> Optional[str]:
    if not url:
        return None
    try:
        absolute = urljoin(base_url, url)
        parsed = urlparse(absolute)
        # Remove tracking params
        query_pairs = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in {"ir"}]
        new_query = urlencode(query_pairs)
        normalized = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_query, ""))
        return normalized
    except Exception:  # noqa: BLE001
        return url


def merge_and_dedupe_products(
    products_lists: List[List[Dict[str, Optional[str]]]],
    base_url: str,
) -> List[Dict[str, Optional[str]]]:
    by_url: Dict[str, Dict[str, Optional[str]]] = {}
    by_name: Dict[str, Dict[str, Optional[str]]] = {}

    for products in products_lists:
        for p in products:
            url_key = normalize_url(p.get("url"), base_url)
            name_key = (p.get("name") or "").strip().lower()
            if url_key:
                existing = by_url.get(url_key)
                if existing is None:
                    by_url[url_key] = dict(p, url=url_key)
                else:
                    _prefer_enriched(existing, p)
            elif name_key:
                existing = by_name.get(name_key)
                if existing is None:
                    by_name[name_key] = dict(p)
                else:
                    _prefer_enriched(existing, p)

    # Merge leftovers by name into URL dict using a synthetic URL key
    for name_key, product in by_name.items():
        by_url.setdefault(f"name://{name_key}", product)

    return list(by_url.values())


def _prefer_enriched(target: Dict[str, Optional[str]], source: Dict[str, Optional[str]]) -> None:
    for key in ("name", "brand", "price", "rating", "reviews", "image", "url"):
        if (not target.get(key)) and source.get(key):
            target[key] = source[key]
        elif key == "name" and source.get(key) and (
            len(source[key] or "") > len(target.get(key) or "")
        ):
            target[key] = source[key]


def resolve_page_url(base_url: str, page_number: int) -> str:
    if page_number <= 1:
        return base_url
    parsed = urlparse(base_url)
    params = dict(parse_qsl(parsed.query))
    params["page"] = str(page_number)
    new_query = urlencode(params)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_query, ""))


def scrape_page(session: requests.Session, url: str) -> Tuple[List[Dict[str, Optional[str]]], str]:
    html = fetch_html(session, url)
    soup = BeautifulSoup(html, "lxml")

    json_ld_products = parse_json_ld(soup)
    dom_products = parse_dom_for_products(soup, base_url=url)

    products = merge_and_dedupe_products([json_ld_products, dom_products], base_url=url)
    return products, html


def write_csv(path: str, products: List[Dict[str, Optional[str]]]) -> None:
    fieldnames = ["name", "brand", "price", "rating", "reviews", "url", "image"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in products:
            writer.writerow({k: p.get(k) or "" for k in fieldnames})


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape item list from an REI category page")
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="Category URL to scrape (default: women's t-shirts)",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="Number of pages to scrape via ?page=N (default: 1)",
    )
    parser.add_argument(
        "--out",
        default="rei_items.csv",
        help="Output CSV file path (default: rei_items.csv)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help="Delay in seconds between requests (default: 1.0)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="HTTP retries (default: 3)",
    )
    parser.add_argument(
        "--dump-html",
        action="store_true",
        help="Also write each page HTML snapshot next to the CSV (for debugging)",
    )
    args = parser.parse_args()

    session = create_http_session()

    all_products: List[Dict[str, Optional[str]]] = []

    for page_number in range(1, max(1, args.pages) + 1):
        page_url = resolve_page_url(args.url, page_number)
        try:
            products, html = scrape_page(session, page_url)
        except Exception as exc:  # noqa: BLE001
            print(f"Error scraping {page_url}: {exc}", file=sys.stderr)
            continue

        if args.dump_html:
            safe_page = str(page_number)
            html_path = re.sub(r"[^a-zA-Z0-9._-]", "_", f"rei_page_{safe_page}.html")
            try:
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:  # noqa: BLE001
                pass

        all_products.extend(products)
        if page_number < args.pages:
            time.sleep(max(0.0, args.delay))

    # Final dedupe pass
    all_products = merge_and_dedupe_products([all_products], base_url=args.url)

    write_csv(args.out, all_products)

    print(f"Wrote {len(all_products)} items to {args.out}")


if __name__ == "__main__":
    main()