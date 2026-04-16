#!/usr/bin/env python3
"""
Poshmark Suit & Blazer Monitor Agent

Scraping strategy (in order per designer):
  1. POST to Poshmark's internal web API (vm-rest/posts/search/v2) —
     same endpoint the browser calls; returns clean JSON directly.
  2. If that is blocked (403/non-200), route the HTML search page through
     ScraperAPI (residential IPs — bypasses Cloudflare bot detection) and
     parse the __NEXT_DATA__ JSON blob embedded by Next.js.

Run:
    SENDGRID_API_KEY=<key> FROM_EMAIL=<addr> SCRAPER_API_KEY=<key> python scraper.py
"""

import html as html_lib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

import requests
from bs4 import BeautifulSoup

# ── Configuration ─────────────────────────────────────────────────────────────

DESIGNERS = [
    "Anderson & Sheppard",
    "Henry Poole",
    "Huntsman",
    "Cifonelli",
    "Rubinacci",
    "Caraceni",
    "A Caraceni",
    "Dege & Skinner",
    "Edward Sexton",
    "Stefano Ricci",
    "Kiton",
    "Brioni",
    "Isaia",
    "Oxxford",
    "Cesare Attolini",
    "Attolini",
    "Canali",
    "Corneliani",
    "Richard Anderson",
    "Kathryn Sargent",
    "Richard James",
    "Brunello Cucinelli",
    "Tom Ford",
    "Giorgio Armani",
    "Ralph Lauren Purple Label",
    "Saint Laurent",
    "Dior",
    "Berluti",
    "Hermès",
    "Gucci",
    "Prada",
    "Ermenegildo Zegna",
    "Zegna",
    "Boglioli",
    "Lardini",
    "Borelio",
    "Sartorio Napoli",
    "Belvest",
    "Caruso",
    "Samuelsohn",
    "Hickey Freeman",
    "Ravazzolo",
    "Coppley",
    "Lubiam",
    "L.B.M. 1911",
    "Pal Zileri",
    "Stile Latino",
    "Raffaele Caruso",
    "Southwick",
    "H. Freeman & Son",
    "Jack Victor",
    "Chester Barrie",
    "Gieves & Hawkes",
    "Ede & Ravenscroft",
    "Barneys New York",
    "Bergdorf Goodman",
    "Neiman Marcus",
    "Saks Fifth Avenue",
    "Paul Stuart",
    "Palm Beach",
    "Norman Hilton",
    "Aquascutum",
    "Dunhill",
]

# Sizes: 38/38R/38L/38S, 40/40R, Large, standalone L
TARGET_SIZES_RE = re.compile(r"\b(38[rls]?|40r?|large|l)\b", re.IGNORECASE)

STATE_FILE = Path("state/listings.json")
RECIPIENTS = ["travis.a.hees@gmail.com", "oliviapierce101@gmail.com"]

# Read all secrets at import time so they're visible in logs immediately
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "")

# ── Endpoints & headers ───────────────────────────────────────────────────────

# Primary: Poshmark's internal web API (what the browser calls)
VM_REST_URL = "https://poshmark.com/vm-rest/posts/search/v2"

VM_REST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": "https://poshmark.com/",
    "Origin": "https://poshmark.com",
    "X-Requested-With": "XMLHttpRequest",
}

# Fallback: HTML search page URL, routed through ScraperAPI
HTML_SEARCH_URL = (
    "https://poshmark.com/search"
    "?query={query}"
    "&department=Men"
    "&category_v2=Men%7CJackets_%26_Coats"
    "&sort_by=added_desc"
)

SCRAPER_API_ENDPOINT = "http://api.scraperapi.com/"

SLEEP_BETWEEN_DESIGNERS = 1.5   # seconds between designers
DIRECT_TIMEOUT = 20             # seconds for direct requests
SCRAPER_TIMEOUT = 60            # seconds for ScraperAPI (residential proxy)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _looks_like_listing(obj: dict) -> bool:
    """True if dict has an id-like key plus at least one content field."""
    has_id = bool(obj.get("id") or obj.get("listing_id") or obj.get("post_id"))
    has_content = bool(
        obj.get("title") or obj.get("price_amount") or obj.get("brand")
    )
    return has_id and has_content


def _find_listing_arrays(obj, _depth: int = 0) -> list[list]:
    """
    Recursively walk any JSON structure and return all lists whose items
    look like Poshmark listings, sorted longest-first.
    No hardcoded key paths — works regardless of nesting.
    """
    if _depth > 12:
        return []

    results: list[list] = []

    if isinstance(obj, list):
        samples = [x for x in obj[:3] if isinstance(x, dict)]
        if samples and all(_looks_like_listing(s) for s in samples):
            results.append(obj)
        for item in obj:
            results.extend(_find_listing_arrays(item, _depth + 1))

    elif isinstance(obj, dict):
        # Visit likely keys first to surface the main listing array quickly
        priority = (
            "data", "posts", "listings", "results", "items",
            "search_results", "post_refs", "catalog",
        )
        visited: set[str] = set()
        for key in priority:
            if key in obj:
                visited.add(key)
                results.extend(_find_listing_arrays(obj[key], _depth + 1))
        for key, val in obj.items():
            if key not in visited:
                results.extend(_find_listing_arrays(val, _depth + 1))

    seen: set[int] = set()
    unique: list[list] = []
    for arr in results:
        if id(arr) not in seen:
            seen.add(id(arr))
            unique.append(arr)
    unique.sort(key=len, reverse=True)
    return unique


# ── Item parser ───────────────────────────────────────────────────────────────

def _parse_price(raw) -> float:
    try:
        if isinstance(raw, dict):
            val = raw.get("val")
            if val is not None:
                return round(float(val) / 100, 2)
            for key in ("amount", "amount_paid", "price"):
                v = raw.get(key)
                if v is not None:
                    return float(str(v).replace(",", ""))
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _parse_item(item: dict) -> dict | None:
    """Normalise a raw listing dict into our standard shape."""
    try:
        listing_id = (
            item.get("id") or item.get("listing_id") or item.get("post_id")
        )
        if not listing_id:
            return None

        pictures = item.get("pictures") or item.get("photos") or []
        img_url = ""
        if pictures and isinstance(pictures[0], dict):
            img_url = (
                pictures[0].get("url_small")
                or pictures[0].get("url_medium")
                or pictures[0].get("url_large")
                or pictures[0].get("url")
                or ""
            )

        seller_raw = item.get("seller")
        if isinstance(seller_raw, dict):
            seller = seller_raw.get("username", "")
        else:
            seller = item.get("creator_username") or ""

        return {
            "id": str(listing_id),
            "title": str(item.get("title") or "").strip(),
            "brand": str(item.get("brand") or "").strip(),
            "size": str(item.get("size") or "").strip(),
            "price": _parse_price(
                item.get("price_amount") or item.get("price") or 0
            ),
            "img_url": img_url,
            "seller": str(seller).strip(),
            "condition": str(item.get("condition") or "").strip(),
            "url": f"https://poshmark.com/listing/{listing_id}",
        }
    except Exception as exc:
        log.debug("Item parse error: %s", exc)
        return None


def _items_from_json(data: dict | list) -> list[dict]:
    """Find listing arrays in a JSON blob and return parsed items."""
    candidates = _find_listing_arrays(data)
    if not candidates:
        return []
    results = []
    for raw in candidates[0]:
        parsed = _parse_item(raw)
        if parsed:
            results.append(parsed)
    return results


# ── Strategy 1: direct POST to vm-rest internal API ──────────────────────────

def _fetch_direct(designer: str) -> list[dict]:
    """
    POST to Poshmark's internal web API — the same endpoint
    the browser calls on a search page load.
    """
    resp = requests.post(
        VM_REST_URL,
        headers=VM_REST_HEADERS,
        json={
            "filters": {
                "department": "Men",
                "category": "Jackets_&_Coats",
            },
            "query": designer,
            "sort_by": "added_desc",
            "count": 48,
            "page_type": "posts",
        },
        timeout=DIRECT_TIMEOUT,
    )
    resp.raise_for_status()
    return _items_from_json(resp.json())


# ── Strategy 2: ScraperAPI → HTML page → __NEXT_DATA__ ───────────────────────

def _fetch_via_scraperapi(designer: str, api_key: str) -> list[dict]:
    """
    Fetch the Poshmark search page routed through ScraperAPI residential
    proxies, then extract listings from the __NEXT_DATA__ JSON blob.
    """
    target = HTML_SEARCH_URL.format(query=quote_plus(designer))
    resp = requests.get(
        SCRAPER_API_ENDPOINT,
        params={"api_key": api_key, "url": target},
        timeout=SCRAPER_TIMEOUT,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    # Primary parse: __NEXT_DATA__ JSON (reliable, structured)
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if script_tag and script_tag.string:
        try:
            next_data = json.loads(script_tag.string)

            # Log the data shape once so we can diagnose key-path issues
            if designer == DESIGNERS[0]:
                pp = (next_data.get("props") or {}).get("pageProps") or {}
                log.info(
                    "__NEXT_DATA__ pageProps keys: %s", list(pp.keys())[:20]
                )

            items = _items_from_json(next_data)
            if items:
                return items
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("__NEXT_DATA__ parse error: %s", exc)

    # Last resort: visible card elements
    return _scrape_cards(soup)


def _scrape_cards(soup: BeautifulSoup) -> list[dict]:
    """Parse visible card HTML elements as a last resort."""
    selectors = (
        "[data-et-name='listing']",
        ".card.card--small",
        ".tile__container",
        "[class*='item__details']",
        ".listing-card",
    )
    seen_ids: set[str] = set()
    results: list[dict] = []

    for selector in selectors:
        for card in soup.select(selector):
            try:
                a_tag = card.find("a", href=True)
                href = a_tag["href"] if a_tag else ""
                if "/listing/" not in href:
                    continue
                listing_id = href.rstrip("/").rsplit("-", 1)[-1]
                if not listing_id or listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)

                img_tag = card.find("img")
                img_url = ""
                if img_tag:
                    img_url = (
                        img_tag.get("src")
                        or img_tag.get("data-src")
                        or img_tag.get("data-lazy-src")
                        or ""
                    )

                def _t(sel: str) -> str:
                    el = card.select_one(sel)
                    return el.get_text(strip=True) if el else ""

                price_text = _t("[class*='price']") or _t(".price")
                price_num = re.sub(r"[^\d.]", "", price_text)

                results.append({
                    "id": listing_id,
                    "title": _t("[class*='title']") or _t(".title"),
                    "brand": _t("[class*='brand']") or _t(".brand"),
                    "size": _t("[class*='size']") or _t(".size"),
                    "price": float(price_num) if price_num else 0.0,
                    "img_url": img_url,
                    "seller": (_t("[class*='username']") or _t(".username")).lstrip("@"),
                    "condition": "",
                    "url": (
                        f"https://poshmark.com{href}"
                        if href.startswith("/") else href
                    ),
                })
            except Exception as exc:
                log.debug("Card parse error: %s", exc)
        if results:
            break
    return results


# ── Per-designer fetch orchestration ─────────────────────────────────────────

def fetch_listings(designer: str) -> list[dict]:
    """
    Fetch listings for one designer.
    Tries the direct vm-rest API first; falls back to ScraperAPI if blocked.
    """
    # ── Strategy 1: direct internal API ──────────────────────────────────────
    try:
        items = _fetch_direct(designer)
        if items:
            log.debug("  direct API → %d items", len(items))
            return items
        # 200 but empty — still try fallback in case category filter was off
        log.debug("  direct API → 200 but 0 items, trying fallback")
    except requests.HTTPError as exc:
        log.debug("  direct API blocked (%s), using ScraperAPI", exc)
    except Exception as exc:
        log.debug("  direct API error (%s), using ScraperAPI", exc)

    # ── Strategy 2: ScraperAPI ────────────────────────────────────────────────
    if not SCRAPER_API_KEY:
        log.warning(
            "  Direct request failed and SCRAPER_API_KEY is not set. "
            "Add it as a GitHub secret to enable the fallback."
        )
        return []

    try:
        items = _fetch_via_scraperapi(designer, SCRAPER_API_KEY)
        log.debug("  ScraperAPI → %d items", len(items))
        return items
    except Exception as exc:
        log.warning("  ScraperAPI also failed: %s", exc)
        return []


# ── Size filter ───────────────────────────────────────────────────────────────

def size_matches(listing: dict) -> bool:
    text = f"{listing.get('size', '')} {listing.get('title', '')}"
    return bool(TARGET_SIZES_RE.search(text))


# ── Main fetch loop ───────────────────────────────────────────────────────────

def fetch_all_listings() -> dict[str, dict]:
    all_listings: dict[str, dict] = {}
    ok = 0
    failures = 0

    for idx, designer in enumerate(DESIGNERS):
        if idx > 0:
            time.sleep(SLEEP_BETWEEN_DESIGNERS)

        log.info("[%d/%d] %s", idx + 1, len(DESIGNERS), designer)

        try:
            listings = fetch_listings(designer)
        except Exception as exc:
            log.warning("  FAILED (%s)", exc)
            failures += 1
            continue

        if not listings:
            log.info("  → 0 items")
            continue

        ok += 1
        kept = 0
        for item in listings:
            if size_matches(item) and item["id"] not in all_listings:
                all_listings[item["id"]] = item
                kept += 1

        log.info(
            "  → %d raw, %d kept after size filter (running total: %d)",
            len(listings), kept, len(all_listings),
        )

    log.info(
        "Fetch complete — %d designers OK, %d failed, "
        "%d unique size-matched listings",
        ok, failures, len(all_listings),
    )
    return all_listings


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict[str, dict]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            log.info("Loaded %d listings from previous state", len(data))
            return data
        log.warning("State file unexpected format; starting fresh")
        return {}
    except FileNotFoundError:
        log.info("No previous state file; starting fresh")
        return {}
    except Exception as exc:
        log.warning("Could not load state (%s); starting fresh", exc)
        return {}


def save_state(listings: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(listings, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Saved %d listings to %s", len(listings), STATE_FILE)


# ── Email ─────────────────────────────────────────────────────────────────────

def _card_html(listing: dict, is_new: bool) -> str:
    title_raw = listing.get("title") or ""
    title = html_lib.escape(title_raw[:80] + ("…" if len(title_raw) > 80 else ""))
    brand = html_lib.escape(listing.get("brand") or "")
    size = html_lib.escape(listing.get("size") or "")
    seller = html_lib.escape(listing.get("seller") or "")
    condition = html_lib.escape(listing.get("condition") or "")
    price = listing.get("price") or 0.0
    url = html_lib.escape(listing.get("url") or "#")
    img_url = html_lib.escape(listing.get("img_url") or "")

    price_fmt = f"${price:,.0f}" if price == int(price) else f"${price:,.2f}"

    new_badge = (
        '<span style="display:inline-block;background:#d32f2f;color:#fff;'
        "font-size:9px;font-weight:700;letter-spacing:1.2px;"
        "text-transform:uppercase;padding:2px 6px;border-radius:2px;"
        'margin-bottom:5px;">NEW</span><br>'
        if is_new else ""
    )

    photo = (
        f'<img src="{img_url}" width="200" height="200" alt="" '
        'style="width:100%;height:200px;object-fit:cover;display:block;'
        'background:#e8e8e8;border-radius:6px 6px 0 0;" />'
        if img_url else
        '<div style="width:100%;height:200px;background:#e8e8e8;'
        'border-radius:6px 6px 0 0;"></div>'
    )

    cond_span = (
        f'<span style="color:#999;font-size:11px;"> · {condition}</span>'
        if condition else ""
    )

    return (
        f'<td style="vertical-align:top;padding:8px;">'
        f'<a href="{url}" style="text-decoration:none;color:inherit;">'
        f'<div style="background:#fff;border-radius:6px;overflow:hidden;'
        f"box-shadow:0 1px 4px rgba(0,0,0,.13);width:200px;"
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;">'
        f"{photo}"
        f'<div style="padding:10px 10px 13px;">'
        f"{new_badge}"
        f'<div style="color:#aaa;font-size:10px;font-weight:600;'
        f"text-transform:uppercase;letter-spacing:.6px;margin-bottom:3px;\">"
        f"{brand}</div>"
        f'<div style="font-size:13px;font-weight:500;color:#222;'
        f'line-height:1.35;margin-bottom:7px;">{title}</div>'
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="font-size:16px;font-weight:700;color:#111;">{price_fmt}</td>'
        f'<td align="right" style="font-size:12px;color:#999;">{size}</td>'
        f'</tr></table>'
        f'<div style="font-size:11px;color:#bbb;margin-top:5px;">'
        f"@{seller}{cond_span}</div>"
        f"</div></div></a></td>"
    )


def _card_grid(listings: list[dict], new_ids: set[str]) -> str:
    COLS = 3
    rows: list[str] = []
    for i in range(0, len(listings), COLS):
        chunk = listings[i : i + COLS]
        cells = "".join(_card_html(lst, lst["id"] in new_ids) for lst in chunk)
        for _ in range(COLS - len(chunk)):
            cells += '<td style="padding:8px;width:216px;"></td>'
        rows.append(f"<tr>{cells}</tr>")
    return "\n".join(rows)


def build_html_email(
    all_listings: dict[str, dict], new_ids: set[str], run_date: str
) -> str:
    all_list = list(all_listings.values())
    new_list = [l for l in all_list if l["id"] in new_ids]
    n_total, n_new = len(all_list), len(new_list)

    new_section = (
        f"""
    <h2 style="font-size:15px;font-weight:700;color:#d32f2f;margin:30px 0 12px;
               letter-spacing:.5px;text-transform:uppercase;">
      &#10022; New Since Last Check ({n_new})
    </h2>
    <table cellpadding="0" cellspacing="0" border="0">
      {_card_grid(new_list, new_ids)}
    </table>
    <hr style="border:none;border-top:1px solid #e8e8e8;margin:30px 0;">"""
        if new_list else ""
    )

    all_section = f"""
    <h2 style="font-size:15px;font-weight:700;color:#333;margin:30px 0 12px;
               letter-spacing:.5px;text-transform:uppercase;">
      All Active Listings ({n_total})
    </h2>
    <table cellpadding="0" cellspacing="0" border="0">
      {_card_grid(all_list, new_ids)}
    </table>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f1ee;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td align="center" style="padding:28px 12px;">
  <table width="700" cellpadding="0" cellspacing="0" border="0" style="max-width:700px;width:100%;">
    <tr>
      <td style="background:#1a1a1a;border-radius:8px 8px 0 0;padding:28px 32px;">
        <h1 style="margin:0;color:#fff;font-size:22px;font-weight:700;letter-spacing:-.3px;">
          Poshmark Suit &amp; Blazer Digest</h1>
        <p style="margin:7px 0 0;color:#bbb;font-size:13px;">
          {run_date} &nbsp;&middot;&nbsp;
          {n_total} listing{'' if n_total == 1 else 's'} found &nbsp;&middot;&nbsp;
          <span style="color:#ff6b6b;">{n_new} new</span>
        </p>
      </td>
    </tr>
    <tr>
      <td style="background:#fff;border-radius:0 0 8px 8px;padding:24px 32px 36px;">
        {new_section}
        {all_section}
        <hr style="border:none;border-top:1px solid #eeeeee;margin:36px 0 20px;">
        <p style="margin:0;font-size:11px;color:#bbb;text-align:center;line-height:1.6;">
          Tracking 63 designers &nbsp;&middot;&nbsp;
          sizes around 38&thinsp;/&thinsp;40 chest &nbsp;&middot;&nbsp;
          18.5&ndash;19&Prime; shoulder
        </p>
      </td>
    </tr>
  </table>
  </td></tr>
</table>
</body></html>"""


def send_email(subject: str, html_body: str) -> None:
    if not SENDGRID_API_KEY:
        raise EnvironmentError("SENDGRID_API_KEY is not set")
    if not FROM_EMAIL:
        raise EnvironmentError("FROM_EMAIL is not set")

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": a} for a in RECIPIENTS]}],
            "from": {"email": FROM_EMAIL, "name": "Poshmark Suit Agent"},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_body}],
        },
        timeout=30,
    )
    if resp.status_code != 202:
        raise RuntimeError(
            f"SendGrid returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    log.info("Email sent (SendGrid 202)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.utcnow()
    run_date = now.strftime("%a %b %e %Y").replace("  ", " ")

    log.info("=" * 60)
    log.info("Poshmark Monitor  —  %s UTC", now.strftime("%Y-%m-%d %H:%M"))
    log.info(
        "Scraper API key present: %s",
        "yes" if SCRAPER_API_KEY else "NO — fallback disabled",
    )
    log.info("=" * 60)

    current = fetch_all_listings()

    if not current:
        log.warning("Zero listings found — skipping email")
        return

    previous = load_state()
    new_ids = set(current.keys()) - set(previous.keys())
    log.info(
        "Delta: %d total, %d new, %d removed",
        len(current), len(new_ids),
        len(set(previous.keys()) - set(current.keys())),
    )

    n_new = len(new_ids)
    subject = (
        f"\U0001F195 {n_new} new listing{'s' if n_new != 1 else ''}"
        f" \u2013 Poshmark Suits ({run_date})"
        if n_new else
        f"Poshmark Suit Digest \u2013 {run_date}"
    )

    send_email(subject, build_html_email(current, new_ids, run_date))
    save_state(current)
    log.info("Run complete.")


if __name__ == "__main__":
    main()
