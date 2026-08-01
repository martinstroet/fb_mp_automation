"""Marketplace scraping. ALL selectors live in this file.

Selector strategy (FB class names are obfuscated and unstable — never use them):
- result cards are anchors matching  a[href*="/marketplace/item/"]
- listing id comes from the href:    /marketplace/item/<digits>/
- price/title/location are parsed positionally from the card's text lines,
  anchored on a price regex rather than DOM structure
- detail pages: h1 for title, text regexes for "Joined Facebook in YYYY" and
  "Listed <x> ago", href-anchored seller profile link
On a structural miss we dump HTML + screenshot to data/debug/ for diagnosis.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import Page

log = logging.getLogger(__name__)

ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")
PRICE_RE = re.compile(r"^(?:AU?\$\s?[\d,]+(?:\.\d{2})?|Free)$", re.IGNORECASE)
PRICE_NUM_RE = re.compile(r"[\d,]+")
JOINED_RE = re.compile(r"Joined Facebook in (\d{4})")
LISTED_RE = re.compile(r"Listed [^.\n]{2,60}? ago(?: in [^.\n]{2,40})?")


def build_search_url(cfg, query: str, max_price: int | None, stale: bool = False) -> str:
    """Fresh sweep: last-day listings, newest first. Stale sweep: no age filter,
    relevance sort, raised price ceiling — finds older listings whose sellers
    may be open to a lower offer."""
    slug = cfg.get("marketplace", "location_slug", default="brisbane")
    url = f"https://www.facebook.com/marketplace/{slug}/search?query={quote_plus(query)}"
    if stale:
        if max_price:
            factor = cfg.get("negotiation", "max_price_factor", default=1.4)
            url += f"&maxPrice={round(max_price * factor)}"
    else:
        days = cfg.get("marketplace", "days_since_listed", default=1)
        url += f"&daysSinceListed={days}&sortBy=creation_time_descend"
        if max_price:
            url += f"&maxPrice={max_price}"
    return url


AGO_RE = re.compile(r"(?:(a|an|\d+)\s+)?(minute|hour|day|week|month)s?\s+ago", re.IGNORECASE)


def parse_listed_days(text: str | None) -> int | None:
    """'Listed 3 weeks ago in …' -> 21. None if unparseable."""
    if not text:
        return None
    m = AGO_RE.search(text)
    if not m:
        return None
    n = m.group(1)
    n = 1 if n in (None, "a", "an") else int(n)
    per_unit = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30}
    return n * per_unit[m.group(2).lower()]


def parse_price_aud(price_text: str | None) -> int | None:
    if not price_text:
        return None
    if price_text.strip().lower() == "free":
        return 0
    m = PRICE_NUM_RE.search(price_text)
    return int(m.group(0).replace(",", "")) if m else None


def scrape_search_cards(page: Page) -> list[dict]:
    """Parse all result cards on the current search page."""
    cards = []
    anchors = page.locator('a[href*="/marketplace/item/"]')
    for i in range(anchors.count()):
        a = anchors.nth(i)
        try:
            href = a.get_attribute("href") or ""
            m = ITEM_ID_RE.search(href)
            if not m:
                continue
            listing_id = m.group(1)
            # FB renders price/title/location as inline spans, so inner_text()
            # gives no line breaks — collect individual text nodes instead.
            lines = a.evaluate(
                """el => {
                    const out = [];
                    const walk = n => {
                        if (n.nodeType === 3) {
                            const t = n.textContent.trim();
                            if (t) out.push(t);
                        } else if (n.nodeType === 1) {
                            for (const c of n.childNodes) walk(c);
                        }
                    };
                    walk(el);
                    return out;
                }"""
            ) or []
            price_text, title, location = None, None, None
            for j, ln in enumerate(lines):
                if PRICE_RE.match(ln):
                    price_text = ln
                    rest = [x for x in lines[j + 1:] if not PRICE_RE.match(x)]
                    if rest:
                        title = rest[0]
                    if len(rest) > 1:
                        location = rest[1]
                    break
            if title is None and lines:  # price-less card (rare): first line as title
                title = lines[0]
            img = a.locator("img").first
            thumb_url = img.get_attribute("src") if img.count() else None
            alt = img.get_attribute("alt") if img.count() else None
            cards.append(
                {
                    "listing_id": listing_id,
                    "url": f"https://www.facebook.com/marketplace/item/{listing_id}/",
                    "title": title or alt,
                    "price_text": price_text,
                    "price_aud": parse_price_aud(price_text),
                    "location": location,
                    "thumb_url": thumb_url,
                }
            )
        except Exception as e:
            log.debug("card %d parse error: %s", i, e)
    # de-dup within page (grid + carousel can repeat an item)
    seen, out = set(), []
    for c in cards:
        if c["listing_id"] not in seen:
            seen.add(c["listing_id"])
            out.append(c)
    return out


def scrape_detail(page: Page) -> dict:
    """Parse the currently-open listing detail page (best effort per field)."""
    detail: dict = {}
    try:  # title: h1 scoped to the main column — a page-global first h1 can be
        # a UI overlay heading ("Notifications"). Tab title as fallback.
        title = page.evaluate(
            """() => {
                const root = document.querySelector('div[role="main"]');
                const h1 = (root || document).querySelector("h1");
                if (h1 && h1.innerText.trim()) return h1.innerText.trim();
                return document.title
                    .replace(/\\s*\\|\\s*Facebook\\s*$/i, "")
                    .replace(/^\\s*Marketplace\\s*[-\\u2013\\u2014]\\s*/i, "")
                    .trim() || null;
            }"""
        )
        if title:
            detail["title"] = title
    except Exception:
        pass

    try:  # asking price: first standalone price text node in the main column —
        # on detail pages it sits right under the h1, before any "More like
        # this" rail, so document order picks the right one
        price_text = page.evaluate(
            """() => {
                const root = document.querySelector('div[role="main"]') || document.body;
                const re = /^(?:AU?\\$\\s?[\\d,]+(?:\\.\\d{2})?|Free)$/i;
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                let n;
                while ((n = walker.nextNode())) {
                    const t = n.textContent.trim();
                    if (re.test(t)) return t;
                }
                return null;
            }"""
        )
        if price_text:
            detail["price_text"] = price_text
            detail["price_aud"] = parse_price_aud(price_text)
    except Exception:
        pass

    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=10_000)
    except Exception:
        pass

    if m := JOINED_RE.search(body_text):
        detail["seller_joined_year"] = int(m.group(1))
    if m := LISTED_RE.search(body_text):
        detail["listed_ago_text"] = m.group(0)

    try:  # expand truncated description
        see_more = page.get_by_text("See more", exact=True).first
        if see_more.count():
            see_more.click(timeout=3000)
            page.wait_for_timeout(600)
    except Exception:
        pass

    try:  # longest coherent text block = description; skip recommendation rails
        detail["description"] = page.evaluate(
            """() => {
                const root = document.querySelector('div[role="main"]') || document.body;
                let best = "";
                for (const el of root.querySelectorAll("div, span")) {
                    if (el.children.length > 3) continue;
                    const t = (el.innerText || "").trim();
                    if ((t.match(/AU?\\$\\s?[\\d,]+/g) || []).length >= 2) continue;
                    if (/Today's picks|Just listed|More like this|Sponsored/.test(t)) continue;
                    if (t.length > best.length && t.length < 4000 &&
                        !t.includes("Joined Facebook") && !t.startsWith("Listed "))
                        best = t;
                }
                return best;
            }"""
        ) or None
    except Exception:
        pass

    GENERIC_SELLER = {"seller details", "seller information", "see profile",
                      "message", "follow", "message seller"}
    try:  # several profile links exist (avatar, section header, name) — pick the name
        links = page.locator('a[href*="/marketplace/profile/"]')
        for i in range(min(links.count(), 8)):
            for line in links.nth(i).inner_text(timeout=5000).strip().splitlines():
                line = line.strip()
                if line and line.lower() not in GENERIC_SELLER and len(line) <= 80:
                    detail["seller_name"] = line
                    break
            if "seller_name" in detail:
                break
    except Exception:
        pass

    try:  # photo count heuristic: large images only, so rail thumbnails don't count
        detail["image_count"] = page.evaluate(
            """() => {
                const root = document.querySelector('div[role="main"]') || document.body;
                return new Set(
                    [...root.querySelectorAll('img[src*="scontent"]')]
                      .filter(i => i.naturalWidth >= 350 && i.naturalHeight >= 250)
                      .map(i => i.src.split("?")[0])
                ).size;
            }"""
        )
    except Exception:
        pass
    return detail


def dump_debug(cfg, page: Page, tag: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = cfg.debug_dir / f"{stamp}-{tag}"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        (outdir / "page.html").write_text(page.content())
        page.screenshot(path=str(outdir / "page.png"), full_page=False)
    except Exception as e:
        log.warning("debug dump failed: %s", e)
    log.warning("structural miss (%s) — dumped to %s", tag, outdir)
    return outdir


def download_thumb(url: str | None, dest: Path) -> str | None:
    """FB CDN URLs are signed and expire — fetch at scrape time."""
    if not url or url.startswith("data:"):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.read())
        return str(dest)
    except Exception as e:
        log.debug("thumb download failed for %s: %s", url, e)
        return None
