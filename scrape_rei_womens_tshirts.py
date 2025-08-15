#!/usr/bin/env python3

import asyncio
import csv
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

try:
	from playwright_stealth import stealth_async as stealth
except Exception:
	stealth = None


CATEGORY_URL = "https://www.rei.com/c/womens-t-shirts?ir=category%3Awomens-t-shirts"
OUTPUT_CSV = "rei_womens_tshirts_page1.csv"
MAX_PRODUCTS = 90


@dataclass
class Variant:
	variant_id: str
	sku: Optional[str]
	color: Optional[str]
	size: Optional[str]


@dataclass
class Product:
	parent_id: str
	title: Optional[str]
	brand: Optional[str]
	url: str
	variants: List[Variant]


def extract_json_ld(html: str) -> List[Dict[str, Any]]:
	"""Extract all JSON-LD script contents as structured JSON objects."""
	soup = BeautifulSoup(html, "lxml")
	json_items: List[Dict[str, Any]] = []
	for script in soup.find_all("script", attrs={"type": re.compile(r"application/(ld\+)?json") } ):
		text = script.string or script.get_text() or ""
		text = text.strip()
		if not text:
			continue
		try:
			data = json.loads(text)
			# Some pages wrap multiple items in a list or an object with @graph
			if isinstance(data, list):
				for item in data:
					if isinstance(item, dict):
						json_items.append(item)
			elif isinstance(data, dict):
				if "@graph" in data and isinstance(data["@graph"], list):
					for item in data["@graph"]:
						if isinstance(item, dict):
							json_items.append(item)
				else:
					json_items.append(data)
		except Exception:
			# JSON-LD can be malformed; ignore failures and continue
			continue
	return json_items


def parse_product_identifiers_from_json(json_items: List[Dict[str, Any]]) -> Tuple[Optional[str], List[Variant]]:
	"""Try to find parent and variant identifiers from structured data.

	Returns (parent_id, variants).
	"""
	parent_id: Optional[str] = None
	variants: List[Variant] = []

	# Common patterns: Product with sku, brand, offers; variants often under offers or additionalProperty
	for item in json_items:
		try:
			if item.get("@type") in ("Product", ["Product"], "IndividualProduct"):
				# Parent identifier candidates: productID, sku, gtin, mpn, or custom fields
				candidate_parent = item.get("productID") or item.get("sku") or item.get("mpn") or item.get("gtin13") or item.get("gtin")
				if candidate_parent and not parent_id:
					parent_id = str(candidate_parent)

				# Variants embedded in offers or additionalProperty
				offers = item.get("offers")
				if isinstance(offers, dict):
					offers = [offers]
				if isinstance(offers, list):
					for offer in offers:
						if not isinstance(offer, dict):
							continue
						# Each offer may correspond to a variant with a SKU
						variant_sku = offer.get("sku") or offer.get("itemOffered", {}).get("sku") if isinstance(offer.get("itemOffered"), dict) else None
						variant_id = str(variant_sku) if variant_sku else None
						color = None
						size = None
						# Look at itemOffered additionalProperty for color/size
						item_offered = offer.get("itemOffered")
						if isinstance(item_offered, dict):
							additional_props = item_offered.get("additionalProperty")
							if isinstance(additional_props, dict):
								additional_props = [additional_props]
							if isinstance(additional_props, list):
								for prop in additional_props:
									if not isinstance(prop, dict):
										continue
									name = (prop.get("name") or "").lower()
									value = prop.get("value")
									if name in ("color", "colour"):
										color = str(value)
									elif name in ("size"):
										size = str(value)
						if variant_id:
							variants.append(Variant(variant_id=variant_id, sku=str(variant_sku) if variant_sku else None, color=color, size=size))
		except Exception:
			continue

	return parent_id, variants


def parse_brand_and_title_from_json(json_items: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
	brand = None
	title = None
	for item in json_items:
		try:
			if item.get("@type") in ("Product", ["Product"], "IndividualProduct"):
				if isinstance(item.get("brand"), dict):
					brand = item.get("brand", {}).get("name") or brand
				else:
					brand = item.get("brand") or brand
				title = item.get("name") or title
		except Exception:
			continue
	return brand, title


async def try_set_90_per_page(page) -> None:
	"""Attempt to change the category listing to 90 items per page.
	Tries multiple selector strategies to adapt to different DOM structures.
	"""
	try:
		# Sometimes there is a dropdown/select for results per page
		# Try selecting via any select element whose options contain 90
		select_count = await page.locator("select").count()
		for i in range(select_count):
			selector = f"select >> nth={i}"
			try:
				await page.select_option(selector, label="90")
				await page.wait_for_timeout(800)
				return
			except Exception:
				try:
					await page.select_option(selector, value="90")
					await page.wait_for_timeout(800)
					return
				except Exception:
					pass

		# Try common clickable elements that might represent the 90-per-page control
		candidates = [
			"button:has-text('90 per page')",
			"button:has-text('View 90')",
			"button:has-text('90')",
			"a:has-text('90 per page')",
			"a:has-text('View 90')",
			"a:has-text('90')",
			"[role='menuitem']:has-text('90')",
			"li:has-text('90')",
		]
		for sel in candidates:
			loc = page.locator(sel).first
			if await loc.count() > 0:
				try:
					await loc.click(timeout=2000)
					await page.wait_for_timeout(1000)
					return
				except Exception:
					pass
	except Exception:
		return


async def get_product_urls_from_category(page) -> List[str]:
	await page.route("**/*", lambda route: route.continue_())
	await page.goto(CATEGORY_URL, wait_until="load")
	# Give extra time for client-side loading
	await page.wait_for_timeout(1500)
	# Try to switch to 90 items per page
	await try_set_90_per_page(page)
	await page.wait_for_timeout(1000)
	loaded_urls: List[str] = []
	for _ in range(40):
		await page.wait_for_timeout(250)
		anchors = await page.locator("a[href*='/product/']").all()
		urls = []
		for a in anchors:
			href = await a.get_attribute("href")
			if href and "/product/" in href:
				if href.startswith("/"):
					urls.append(f"https://www.rei.com{href}")
				else:
					urls.append(href)
		seen = set(loaded_urls)
		for u in urls:
			if u not in seen:
				seen.add(u)
				loaded_urls.append(u)
		if len(loaded_urls) >= MAX_PRODUCTS:
			break
		await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
	return loaded_urls[:MAX_PRODUCTS]


async def extract_product(page, url: str) -> Product:
	await page.goto(url, wait_until="domcontentloaded")
	# Some sites block until network idle; try a short wait and ensure scripts executed
	await page.wait_for_timeout(800)
	html = await page.content()
	json_items = extract_json_ld(html)
	parent_id, variants = parse_product_identifiers_from_json(json_items)
	brand, title = parse_brand_and_title_from_json(json_items)

	# Fallbacks if JSON-LD missing: try meta tags or data attributes
	if not parent_id:
		# Look for meta tags or data attributes that include product id or style id
		soup = BeautifulSoup(html, "lxml")
		meta_pid = soup.find("meta", attrs={"name": re.compile("(product|item).*id", re.I)})
		if meta_pid and meta_pid.get("content"):
			parent_id = meta_pid.get("content")
		else:
			# Common REI attribute names may include data-sku, data-style, data-product-id on containers
			container = soup.find(attrs={"data-product-id": True}) or soup.find(attrs={"data-style": True})
			if container:
				parent_id = container.get("data-product-id") or container.get("data-style")

	# Ensure at least one variant entry even if unknown
	if not variants:
		# Try to derive a single SKU from meta tags
		soup = BeautifulSoup(html, "lxml")
		meta_sku = soup.find("meta", attrs={"name": re.compile("sku", re.I)})
		if meta_sku and meta_sku.get("content"):
			variants = [Variant(variant_id=meta_sku.get("content"), sku=meta_sku.get("content"), color=None, size=None)]

	return Product(
		parent_id=parent_id or "",
		title=title,
		brand=brand,
		url=url,
		variants=variants,
	)


async def run() -> int:
	products: List[Product] = []
	async with async_playwright() as p:
		browsers = [
			("chromium", p.chromium, ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox", "--disable-features=IsolateOrigins,site-per-process", "--disable-gpu", "--disable-extensions", "--no-first-run", "--no-default-browser-check", "--disable-web-security"]),
			("firefox", p.firefox, []),
			("webkit", p.webkit, []),
		]
		page = None
		context = None
		browser_obj = None
		for name, browser_type, args in browsers:
			try:
				browser_obj = await browser_type.launch(headless=True, args=args)
				context = await browser_obj.new_context(
					ignore_https_errors=True,
					locale="en-US",
					user_agent=(
						"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
						"AppleWebKit/537.36 (KHTML, like Gecko) "
						"Chrome/125.0.0.0 Safari/537.36"
					),
					extra_http_headers={
						"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
						"Accept-Language": "en-US,en;q=0.9",
						"Upgrade-Insecure-Requests": "1",
						"Referer": "https://www.rei.com/",
						"Sec-Fetch-Site": "same-origin",
						"Sec-Fetch-Mode": "navigate",
						"Sec-Fetch-Dest": "document",
					}
				)
				page = await context.new_page()
				if stealth is not None and name == "chromium":
					try:
						await stealth(page)
					except Exception:
						pass
				print(f"Using browser: {name}", file=sys.stderr)
				break
			except Exception as e:
				print(f"Failed launching {name}: {e}", file=sys.stderr)
				if browser_obj:
					await browser_obj.close()
				browser_obj = None
				context = None
				page = None

		if page is None:
			print("Could not launch any browser.", file=sys.stderr)
			return 2

		print("Loading category and collecting product URLs...", file=sys.stderr)
		try:
			urls = await get_product_urls_from_category(page)
		except Exception as e:
			print(f"Failed to load category: {e}", file=sys.stderr)
			urls = []
		print(f"Found {len(urls)} product URLs", file=sys.stderr)

		for idx, url in enumerate(urls, start=1):
			print(f"[{idx}/{len(urls)}] Extracting {url}", file=sys.stderr)
			try:
				prod = await extract_product(page, url)
				products.append(prod)
			except Exception as e:
				print(f"Failed to extract {url}: {e}", file=sys.stderr)

		await browser_obj.close()

	# Write CSV
	output_path = Path(OUTPUT_CSV)
	with output_path.open("w", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)
		writer.writerow([
			"parent_id",
			"title",
			"brand",
			"product_url",
			"variant_id",
			"variant_sku",
			"variant_color",
			"variant_size",
		])
		for product in products:
			if product.variants:
				for v in product.variants:
					writer.writerow([
						product.parent_id,
						product.title or "",
						product.brand or "",
						product.url,
						v.variant_id,
						v.sku or "",
						v.color or "",
						v.size or "",
					])
			else:
				writer.writerow([
					product.parent_id,
					product.title or "",
					product.brand or "",
					product.url,
					"",
					"",
					"",
					"",
				])

	print(f"Wrote {output_path}")
	return 0


if __name__ == "__main__":
	try:
		ret = asyncio.run(run())
		sys.exit(ret)
	except KeyboardInterrupt:
		sys.exit(130)