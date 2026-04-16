#!/usr/bin/env python3
"""
Poshmark Suit & Blazer Monitor Agent

Scrapes Poshmark search pages, diffs against the previous run,
and emails an HTML digest via SendGrid.

Scraping approach:
  1. Fetch the Poshmark search page with requests + a browser UA
  2. Extract the __NEXT_DATA__ <script> JSON blob (Next.js SSR) with
     BeautifulSoup — this is the most reliable source of structured data
  3. Walk the entire JSON tree recursively to find listing arrays —
     no fragile hardcoded key paths
  4. Fall back to parsing visible card HTML elements if __NEXT_DATA__
     yields nothing

Run:
    SENDGRID_API_KEY=<key> FROM_EMAIL=<addr> python scraper.py
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
from urllib.parse import quote_plus

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

# Sizes to keep: 38/38R/38L/38S, 40/40R, Large, standalone L
# My measurements (for reference only):
#   shoulder 18.5–19", chest 40", jacket at button 34", sleeve 23", waist 33"
TARGET_SIZES_RE = re.compile(
    r"\b(38[rls]?|40r?|large|l)\b",
    re.IGNORECASE,
)

STATE_FILE = Path("state/listings.json")

RECIPIENTS = ["travis.a.hees@gmail.com", "oliviapierce101@gmail.com"]

# Confirmed-working Poshmark search URL format.
# category_v2 uses pipe-separated path: Men|Jackets_&_Coats
SEARCH_URL = (
    "https://poshmark.com/search"
    "?query={query}"
    "&department=Men"
    "&category_v2=Men%7CJackets_%26_Coats"
    "&sort_by=added_desc"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

SLEEP_BETWEEN_DESIGNERS = 1.5  # seconds
REQUEST_TIMEOUT = 25            # seconds

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── JSON tree walker ──────────────────────────────────────────────────────────

def _looks_like_listing(obj: dict) -> bool:
    """
    Heuristic: does this dict look like a Poshmark listing?
    Requires an id-like field plus at least one of title / price / brand.
    """
    has_id = bool(obj.get("id") or obj.get("listing_id") or obj.get("post_id"))
    has_content = bool(
        obj.get("title") or obj.get("price_amount") or obj.get("brand")
    )
    return has_id and has_content


def _find_listing_arrays(obj, _depth: int = 0) -> list[list]:
    """
    Recursively walk a decoded-JSON object and return every list that
    appears to contain Poshmark listing dicts.

    Returns a list of candidate arrays, longest first.
    """
    if _depth > 12:
        return []

    results: list[list] = []

    if isinstance(obj, list):
        # Sample up to 3 items to decide if this is a listing array
        samples = [x for x in obj[:3] if isinstance(x, dict)]
        if samples and all(_looks_like_listing(s) for s in samples):
            results.append(obj)
        # Also recurse into list elements
        for item in obj:
            results.extend(_find_listing_arrays(item, _depth + 1))

    elif isinstance(obj, dict):
        # Prioritise keys that are likely to hold posts/listings
        priority = ("posts", "listings", "data", "results", "items",
                    "search_results", "post_refs", "catalog")
        visited: set[str] = set()
        for key in priority:
            if key in obj:
                visited.add(key)
                results.extend(_find_listing_arrays(obj[key], _depth + 1))
        for key, val in obj.items():
            if key not in visited:
                results.extend(_find_listing_arrays(val, _depth + 1))

    # Deduplicate by id() of the list object and sort longest first
    seen: set[int] = set()
    unique: list[list] = []
    for arr in results:
        if id(arr) not in seen:
            seen.add(id(arr))
            unique.append(arr)
    unique.sort(key=len, reverse=True)
    return unique


# ── Listing parser ────────────────────────────────────────────────────────────

def _parse_price(raw) -> float:
    """
    Normalise Poshmark price fields into a float dollar amount.
      {"val": 12500}      → 125.00  (val is in cents)
      {"amount": "125"}   → 125.00
      125 / "125.00"      → 125.00
    """
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
    """Parse a raw listing dict (from __NEXT_DATA__ or card HTML) into a
    normalised listing dict."""
    try:
        listing_id = (
            item.get("id")
            or item.get("listing_id")
            or item.get("post_id")
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
            "price": _parse_price(item.get("price_amount") or item.get("price") or 0),
            "img_url": img_url,
            "seller": str(seller).strip(),
            "condition": str(item.get("condition") or "").strip(),
            "url": f"https://poshmark.com/listing/{listing_id}",
        }
    except Exception as exc:
        log.debug("Item parse error: %s", exc)
        return None


# ── HTML card fallback ────────────────────────────────────────────────────────

def _scrape_cards(soup: BeautifulSoup) -> list[dict]:
    """
    Last-resort fallback: parse visible listing card HTML elements.
    Tries several CSS selectors that Poshmark has used historically.
    """
    selectors = (
        "[data-et-name='listing']",
        ".card.card--small",
        ".tile__container",
        "[class*='item__details']",
        ".listing-card",
        "[class*='listing']",
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

                def _text(sel: str) -> str:
                    el = card.select_one(sel)
                    return el.get_text(strip=True) if el else ""

                price_text = _text("[class*='price']") or _text(".price")
                price_num = re.sub(r"[^\d.]", "", price_text)

                results.append({
                    "id": listing_id,
                    "title": _text("[class*='title']") or _text(".title"),
                    "brand": _text("[class*='brand']") or _text(".brand"),
                    "size": _text("[class*='size']") or _text(".size"),
                    "price": float(price_num) if price_num else 0.0,
                    "img_url": img_url,
                    "seller": (
                        _text("[class*='username']") or _text(".username")
                    ).lstrip("@"),
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


# ── Main per-designer fetch ───────────────────────────────────────────────────

def fetch_listings(designer: str) -> list[dict]:
    """
    Fetch search results for one designer from the Poshmark search page.

    Strategy (in order):
      1. Parse __NEXT_DATA__ JSON via BeautifulSoup script-tag lookup
      2. Recursively walk the JSON tree to find listing arrays
         (no hardcoded key paths — handles any Next.js data shape)
      3. Fall back to parsing visible card HTML elements
    """
    url = SEARCH_URL.format(query=quote_plus(designer))
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    # ── Strategy 1 & 2: __NEXT_DATA__ JSON + recursive walk ──────────────────
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if script_tag and script_tag.string:
        try:
            next_data = json.loads(script_tag.string)

            # Log top-level shape once (first designer) to aid debugging
            if designer == DESIGNERS[0]:
                top_keys = list(next_data.keys())
                props_keys = list((next_data.get("props") or {}).keys())
                pp_keys = list(
                    ((next_data.get("props") or {}).get("pageProps") or {}).keys()
                )
                log.info(
                    "__NEXT_DATA__ shape — top: %s | props: %s | pageProps: %s",
                    top_keys, props_keys, pp_keys,
                )

            candidates = _find_listing_arrays(next_data)
            if candidates:
                best = candidates[0]
                log.debug(
                    "__NEXT_DATA__ walker found %d candidate arrays; "
                    "using largest (%d items)",
                    len(candidates), len(best),
                )
                results = []
                for raw in best:
                    parsed = _parse_item(raw)
                    if parsed:
                        results.append(parsed)
                if results:
                    return results
            else:
                log.debug("__NEXT_DATA__ walker found no listing arrays")

        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("__NEXT_DATA__ JSON parse failed: %s", exc)
    else:
        log.debug("No <script id='__NEXT_DATA__'> found on page")

    # ── Strategy 3: card HTML fallback ───────────────────────────────────────
    log.debug("Falling back to card HTML parsing")
    return _scrape_cards(soup)


# ── Size filter ───────────────────────────────────────────────────────────────

def size_matches(listing: dict) -> bool:
    """Return True if the listing's size or title contains a target size."""
    text = f"{listing.get('size', '')} {listing.get('title', '')}"
    return bool(TARGET_SIZES_RE.search(text))


# ── Main fetch loop ───────────────────────────────────────────────────────────

def fetch_all_listings() -> dict[str, dict]:
    """
    Fetch all designers in sequence, apply size filter, deduplicate by ID.
    """
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
            log.info("  → 0 items returned")
            continue

        ok += 1
        kept = 0
        for item in listings:
            if size_matches(item) and item["id"] not in all_listings:
                all_listings[item["id"]] = item
                kept += 1

        log.info(
            "  → %d raw items, %d kept after size filter (total: %d)",
            len(listings), kept, len(all_listings),
        )

    log.info(
        "Fetch complete — %d designers returned data, %d failed, "
        "%d unique size-matched listings",
        ok, failures, len(all_listings),
    )
    return all_listings


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict[str, dict]:
    """Load previous listings. Returns empty dict on any error."""
    try:
        text = STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            log.info("Loaded %d listings from previous state", len(data))
            return data
        log.warning("State file has unexpected format; starting fresh")
        return {}
    except FileNotFoundError:
        log.info("No previous state file; starting fresh")
        return {}
    except Exception as exc:
        log.warning("Could not load state (%s); starting fresh", exc)
        return {}


def save_state(listings: dict[str, dict]) -> None:
    """Overwrite state file with current listings."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(listings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Saved %d listings to %s", len(listings), STATE_FILE)


# ── Email HTML builder ────────────────────────────────────────────────────────

def _card_html(listing: dict, is_new: bool) -> str:
    """Render a single listing as an inline-styled HTML table cell."""
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
        f"text-transform:uppercase;letter-spacing:.6px;"
        f'margin-bottom:3px;">{brand}</div>'
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
    """3-column table grid of listing cards."""
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
    all_listings: dict[str, dict],
    new_ids: set[str],
    run_date: str,
) -> str:
    """Assemble the full HTML email body with inline styles throughout."""
    all_list = list(all_listings.values())
    new_list = [lst for lst in all_list if lst["id"] in new_ids]
    n_total = len(all_list)
    n_new = len(new_list)

    new_section = ""
    if new_list:
        new_section = f"""
    <h2 style="font-size:15px;font-weight:700;color:#d32f2f;margin:30px 0 12px;
               letter-spacing:.5px;text-transform:uppercase;">
      &#10022; New Since Last Check ({n_new})
    </h2>
    <table cellpadding="0" cellspacing="0" border="0">
      {_card_grid(new_list, new_ids)}
    </table>
    <hr style="border:none;border-top:1px solid #e8e8e8;margin:30px 0;">"""

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
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f4f1ee;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr><td align="center" style="padding:28px 12px;">
  <table width="700" cellpadding="0" cellspacing="0" border="0"
         style="max-width:700px;width:100%;">
    <tr>
      <td style="background:#1a1a1a;border-radius:8px 8px 0 0;padding:28px 32px;">
        <h1 style="margin:0;color:#fff;font-size:22px;font-weight:700;
                   letter-spacing:-.3px;">Poshmark Suit &amp; Blazer Digest</h1>
        <p style="margin:7px 0 0;color:#bbb;font-size:13px;">
          {run_date}
          &nbsp;&middot;&nbsp; {n_total} listing{'' if n_total == 1 else 's'} found
          &nbsp;&middot;&nbsp;
          <span style="color:#ff6b6b;">{n_new} new</span>
        </p>
      </td>
    </tr>
    <tr>
      <td style="background:#fff;border-radius:0 0 8px 8px;
                 padding:24px 32px 36px;">
        {new_section}
        {all_section}
        <hr style="border:none;border-top:1px solid #eeeeee;margin:36px 0 20px;">
        <p style="margin:0;font-size:11px;color:#bbb;text-align:center;
                  line-height:1.6;">
          Tracking 63 designers
          &nbsp;&middot;&nbsp; sizes around 38&thinsp;/&thinsp;40 chest
          &nbsp;&middot;&nbsp; 18.5&ndash;19&Prime; shoulder
        </p>
      </td>
    </tr>
  </table>
  </td></tr>
</table>
</body>
</html>"""


# ── SendGrid sender ───────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> None:
    """Send via SendGrid REST API. Raises on non-202 response."""
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    from_email = os.environ.get("FROM_EMAIL", "")
    if not api_key:
        raise EnvironmentError("SENDGRID_API_KEY is not set")
    if not from_email:
        raise EnvironmentError("FROM_EMAIL is not set")

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": a} for a in RECIPIENTS]}],
            "from": {"email": from_email, "name": "Poshmark Suit Agent"},
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
