"""ComedyPass scraper — Comedy Restobar and Distrito 04 venue pages.

ComedyPass is a WordPress/WooCommerce site.  Each show is a product card with
title (which embeds date + time), image, price, and a link to the product page.

Venue pages scraped:
  https://comedypass.online/comedy-restobar/
  https://comedypass.online/distrito-04/

Run standalone:
    python scrapers/comedypass_scraper.py           # fetch + print, no DB writes
    python scrapers/comedypass_scraper.py --debug   # print first card raw HTML
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import date, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# ── Venue configuration ───────────────────────────────────────────────────────

VENUE_PAGES: list[dict[str, Any]] = [
    {
        "url":               "https://comedypass.online/comedy-restobar/",
        "venue_name":        "Comedy Restobar",
        "venue_type":        "Bar",
        "coordinates":       [-33.4344, -70.6108],
        # This token appears in the product title between artist and date,
        # e.g. "PALOMA SALAS COMEDY RESTOBAR 27 DE ABRIL 19:30 HRS."
        "title_strip_token": "COMEDY RESTOBAR",
    },
    {
        "url":               "https://comedypass.online/distrito-04/",
        "venue_name":        "Distrito 04",
        "venue_type":        "Bar",
        "coordinates":       [-33.4489, -70.6456],
        # Distrito 04 appends the venue after "HRS.", not before the date,
        # e.g. "TRIBUTO BON JOVI 9 DE MAYO 21:00 HRS. DISTRITO 04 - SAN MIGUEL"
        "title_strip_token": None,
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_DELAY = 1.5  # seconds between requests

_MONTHS_ES: dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5,
    "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}

# Keywords (lowercase) that indicate a Música event — used as _category_hint
_MUSICA_KEYWORDS = {
    "tributo", "tributo a", "banda", "rock", "jazz", "blues",
    "metal", "punk", "reggae", "salsa", "cumbia", "música", "musica",
    "concierto", "toca", "en vivo", "tribute",
}


# ── Title parsing ─────────────────────────────────────────────────────────────

def _parse_title(
    raw: str,
    strip_token: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Extract (clean_name, date_str, time_str) from a ComedyPass product title.

    ComedyPass embeds date and time directly in the product title, e.g.:
        "PALOMA SALAS COMEDY RESTOBAR 27 DE ABRIL 19:30 HRS."
        "TRIBUTO BON JOVI 9 DE MAYO 21:00 HRS. DISTRITO 04 - SAN MIGUEL"

    Returns date as "YYYY-MM-DD" and time as "HH:MM", or None if not found.
    Year defaults to the current year; if the resulting date is in the past,
    year is advanced by 1.
    """
    # ── Date: DD DE MONTH [DE YYYY] ──────────────────────────────────────────
    date_m = re.search(
        r"(\d{1,2})\s+DE\s+([A-ZÁÉÍÓÚÜÑ]+)(?:\s+DE\s+(\d{4}))?",
        raw,
        re.IGNORECASE,
    )
    date_str: str | None = None
    time_str: str | None = None

    if date_m:
        day = int(date_m.group(1))
        month_name = date_m.group(2).lower()[:3]
        year_literal = date_m.group(3)
        month_num = _MONTHS_ES.get(month_name)

        if month_num:
            if year_literal:
                year = int(year_literal)
            else:
                year = datetime.now().year
                # If that date is already past, bump to next year
                try:
                    if date(year, month_num, day) < date.today():
                        year += 1
                except ValueError:
                    pass
            try:
                date(year, month_num, day)  # validate
                date_str = f"{year:04d}-{month_num:02d}-{day:02d}"
            except ValueError:
                pass

        # ── Time: HH:MM HRS ──────────────────────────────────────────────────
        time_m = re.search(r"(\d{1,2}):(\d{2})\s*HRS", raw, re.IGNORECASE)
        if time_m:
            time_str = f"{int(time_m.group(1)):02d}:{time_m.group(2)}"

        # ── Clean name: everything before the date ───────────────────────────
        name = raw[: date_m.start()].strip()
    else:
        name = raw.strip()

    # Strip known venue token that appears between artist and date
    if strip_token and strip_token:
        name = re.sub(re.escape(strip_token), "", name, flags=re.IGNORECASE).strip()

    # Remove leading/trailing punctuation and stray quote chars
    # (some ComedyPass titles wrap the show name in quotes, e.g.
    #  '"ARMANDO CHISTES" LUIS SLIMMING' → 'ARMANDO CHISTES LUIS SLIMMING')
    name = re.sub(r'["\u201C\u201D\u2018\u2019]', '', name)
    name = name.strip(" '–—-").strip()

    return name or raw.strip(), date_str, time_str


def _parse_price(raw: str) -> list[float] | None:
    """Extract [min, max] price from a price string like '$10.000'."""
    if not raw:
        return None
    lower = raw.lower()
    if "gratis" in lower or "gratuito" in lower or "libre" in lower:
        return [0.0, 0.0]
    numbers = re.findall(r"[\d.]+", raw.replace(",", "."))
    prices: list[float] = []
    for n in numbers:
        try:
            val = float(n.replace(".", ""))
            if val >= 100:
                prices.append(val)
        except ValueError:
            pass
    if prices:
        mn = min(prices)
        return [mn, max(prices)]
    return None


def _best_image(img_tag: Any) -> str | None:
    """Return the highest-resolution URL from an <img> tag."""
    if img_tag is None:
        return None
    # Prefer 1080w from srcset
    srcset = img_tag.get("srcset", "") or ""
    m = re.search(r"(https://[^\s]+)\s+1080w", srcset)
    if m:
        return m.group(1)
    # Fallback: strip WordPress resize params from src
    src = (
        img_tag.get("data-src")
        or img_tag.get("src")
        or img_tag.get("data-lazy-src")
        or ""
    )
    if src.startswith("//"):
        src = "https:" + src
    if src.startswith("http"):
        # Strip ?fit=…&quality=… but keep ?w=1080&… if already high-res
        clean = re.sub(r"\?fit=[^&]+&.*", "", src)
        return clean if clean else src
    return None


def _category_hint(name: str, description: str) -> str:
    """Return 'Música' if music keywords fire, else 'Comedia'."""
    combined = (name + " " + description).lower()
    for kw in _MUSICA_KEYWORDS:
        if kw in combined:
            return "Música"
    return "Comedia"


# ── Scraper class ─────────────────────────────────────────────────────────────

class ComedyPassScraper(BaseScraper):
    """Scrapes Comedy Restobar and Distrito 04 show listings from ComedyPass."""

    name = "comedypass"

    def __init__(self, max_events: int = 0, debug: bool = False) -> None:
        super().__init__()
        self.max_events = max_events
        self.debug = debug
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _get_soup(self, url: str) -> BeautifulSoup | None:
        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as exc:
            logger.error("Failed to fetch %s: %s", url, exc)
            return None

    # ── Card parsing ──────────────────────────────────────────────────────────

    def _parse_card(
        self,
        card: Any,
        venue_cfg: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Extract an event dict from a single WooCommerce product card.

        Card structure (confirmed from live HTML):
            <div class="eael-product-wrap">
              <img srcset="...1080w..." src="...">
              <div class="eael-product-title">
                <a class="woocommerce-LoopProduct-link" href="...product-url...">
                  <h2 class="woocommerce-loop-product__title">TITLE WITH DATE TIME</h2>
                </a>
              </div>
              <div class="eael-product-price">
                <span class="woocommerce-Price-amount"><bdi>$10.000</bdi></span>
              </div>
            </div>
        """
        event: dict[str, Any] = {}

        # ── URL ───────────────────────────────────────────────────────────────
        link = card.find("a", class_="woocommerce-LoopProduct-link")
        if not link or not link.get("href"):
            return None
        product_url = link["href"]
        event["url"] = product_url
        event["source_url"] = product_url

        # ── Raw title ─────────────────────────────────────────────────────────
        h2 = card.find("h2", class_="woocommerce-loop-product__title")
        if not h2:
            h2 = card.find("h2")
        if not h2:
            return None
        raw_title = h2.get_text(strip=True)
        if not raw_title:
            return None

        # ── Parse name / date / time from title ───────────────────────────────
        strip_token = venue_cfg.get("title_strip_token")
        name, date_str, time_str = _parse_title(raw_title, strip_token)
        event["name"] = name
        if date_str:
            event["date"] = date_str
        if time_str:
            event["time_start"] = time_str

        # ── Image ─────────────────────────────────────────────────────────────
        img = card.find("img")
        img_url = _best_image(img)
        if img_url:
            event["image_url"] = img_url

        # ── Price ─────────────────────────────────────────────────────────────
        price_el = card.find(class_="eael-product-price")
        if price_el:
            amount_el = price_el.find(class_="woocommerce-Price-amount")
            if amount_el:
                price_range = _parse_price(amount_el.get_text(strip=True))
                if price_range:
                    event["price_range"] = price_range

        # ── Sold-out: AGOTADO badge on listing card ────────────────────────────
        outofstock = card.find(class_=re.compile(r"outofstock", re.I))
        if outofstock:
            badge_text = outofstock.get_text(strip=True).upper()
            if "AGOTADO" in badge_text or "SIN EXISTENCIAS" in badge_text:
                event["is_sold_out"] = True

        # ── Venue fields (fixed per venue page) ───────────────────────────────
        event["venue_name"]   = venue_cfg["venue_name"]
        event["coordinates"]  = venue_cfg["coordinates"]

        return event

    # ── Detail page fetcher ───────────────────────────────────────────────────

    def fetch_event_detail(self, url: str) -> dict[str, Any]:
        """Fetch the WooCommerce product page for description and price fallback.

        Uses REQUEST_DELAY before the fetch.  Returns {} on any error.
        """
        time.sleep(REQUEST_DELAY)
        soup = self._get_soup(url)
        if soup is None:
            return {}

        result: dict[str, Any] = {}

        # ── Description: short-description block (most reliable) ─────────────
        desc_el = soup.find(
            class_="woocommerce-product-details__short-description"
        )
        if desc_el:
            txt = desc_el.get_text(" ", strip=True)
            if len(txt) > 10:
                result["description"] = txt[:1500]

        # ── Price: scoped to .summary to avoid related-products prices ────────
        if "price_range" not in result:
            summary = soup.find("div", class_="summary")
            if summary:
                price_el = summary.find(class_="woocommerce-Price-amount")
                if price_el:
                    price_range = _parse_price(price_el.get_text(strip=True))
                    if price_range:
                        result["price_range"] = price_range

        # ── Sold-out: WooCommerce <p class="stock out-of-stock"> ─────────────────
        # This <p> element is generated only for the main product, not for
        # related-products widgets. Related products use <span class="out-of-stock
        # product-label">. Using the <p> tag avoids false positives.
        stock_p = soup.find(
            "p",
            class_=lambda c: c and "stock" in c and "out-of-stock" in c,
        )
        if stock_p:
            result["is_sold_out"] = True

        return result

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Scrape all products from both ComedyPass venue pages.

        For each card:
          1. Parses name / date / time from the embedded product title.
          2. Fetches the product detail page for description.
          3. Applies a _category_hint based on music keyword matching;
             the main pipeline classifier may upgrade this to Música when
             stronger rule signals fire (e.g. "tributo", "rock", "jazz").
        """
        all_events: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for venue_cfg in VENUE_PAGES:
            venue_url = venue_cfg["url"]
            venue_name = venue_cfg["venue_name"]
            logger.info("[comedypass] Scraping venue %r — %s", venue_name, venue_url)

            soup = self._get_soup(venue_url)
            if soup is None:
                logger.warning("[comedypass] Could not fetch %s", venue_url)
                continue

            if self.debug:
                self._print_debug(soup, venue_cfg)
                continue

            cards = soup.find_all(class_="eael-product-wrap")
            logger.info("[comedypass] %r — %d product cards found", venue_name, len(cards))

            for card in cards:
                ev = self._parse_card(card, venue_cfg)
                if ev is None:
                    continue
                if not ev.get("date"):
                    logger.debug(
                        "[comedypass] Skipping %r — could not parse date from title",
                        ev.get("name"),
                    )
                    continue

                if ev["url"] in seen_urls:
                    continue
                seen_urls.add(ev["url"])

                # Fetch detail page for description (always — listing has none)
                logger.debug("[comedypass] Fetching detail for %r", ev.get("name"))
                detail = self.fetch_event_detail(ev["url"])
                for key, val in detail.items():
                    # is_sold_out: OR the two signals — sold-out from either
                    # listing card OR detail page marks the event as sold out.
                    if key == "is_sold_out":
                        ev["is_sold_out"] = ev.get("is_sold_out", False) or val
                    else:
                        ev.setdefault(key, val)

                # Ensure is_sold_out is always present so the deduplicator can
                # reset stale True values in the DB when stock is restored.
                ev.setdefault("is_sold_out", False)

                # Category hint: let classifier decide with keyword rules;
                # fall back to Comedia (the venue's primary offering).
                hint = _category_hint(ev.get("name", ""), ev.get("description", ""))
                ev["_category_hint"] = hint

                all_events.append(ev)

                if self.max_events and len(all_events) >= self.max_events:
                    logger.info("[comedypass] Reached max_events=%d", self.max_events)
                    break

            if self.max_events and len(all_events) >= self.max_events:
                break

        logger.info("[comedypass] Total events collected: %d", len(all_events))
        return all_events

    # ── Debug helper ─────────────────────────────────────────────────────────

    def _print_debug(self, soup: BeautifulSoup, venue_cfg: dict) -> None:
        print("\n" + "=" * 70)
        print(f"DEBUG — ComedyPass: {venue_cfg['venue_name']}")
        print("=" * 70)
        title_el = soup.find("title")
        print(f"Page <title>: {title_el.get_text(strip=True) if title_el else '(none)'}")
        cards = soup.find_all(class_="eael-product-wrap")
        print(f"Cards (.eael-product-wrap): {len(cards)}")
        if cards:
            print("\n── First card raw HTML ─────────────────────────────────────────────")
            print(str(cards[0])[:2000])
            print("\n── Parsed fields ───────────────────────────────────────────────────")
            parsed = self._parse_card(cards[0], venue_cfg)
            if parsed:
                for k, v in parsed.items():
                    print(f"  {k}: {v!r}")
        print("=" * 70)


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="ComedyPass scraper")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print raw HTML structure of first card (no DB writes)",
    )
    parser.add_argument(
        "--max-events", type=int, default=0,
        help="Stop after this many events (0 = unlimited)",
    )
    args = parser.parse_args()

    scraper = ComedyPassScraper(max_events=args.max_events, debug=args.debug)
    events = scraper.fetch_events()
    if not args.debug:
        print(f"\nFetched {len(events)} events.")
        for ev in events[:10]:
            print(
                f"  • {ev.get('name')!r:<40s}  "
                f"date={ev.get('date')}  "
                f"time={ev.get('time_start')}  "
                f"venue={ev.get('venue_name')!r}  "
                f"hint={ev.get('_category_hint')}  "
                f"price={ev.get('price_range')}"
            )
