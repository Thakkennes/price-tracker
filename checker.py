"""
Price Tracker — checker.py
==========================
Monitors products defined in products.json, verifies matches via Gemini,
and sends email alerts when prices fall below user-defined thresholds.

Environment variables required:
  SERPAPI_KEY         — SerpAPI key for Google Shopping searches
  GEMINI_API_KEY      — Google Gemini API key (free tier available)
  GMAIL_ADDRESS       — Gmail address used to send alert emails
  GMAIL_APP_PASSWORD  — Gmail App Password (not your account password)
"""

import csv
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup
from google import genai
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PRODUCTS_FILE = Path("products.json")
HISTORY_FILE = Path("price_history.csv")
HISTORY_COLUMNS = [
    "timestamp",
    "product_name",
    "lowest_verified_price",
    "currency",
    "retailer",
    "link",
    "alert_sent",
]

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# Gemini model to use for product verification.
# gemini-2.5-flash-lite is the recommended free-tier model as of early 2026.
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Number of SerpAPI results to fetch per product.
# One API credit per call regardless of num, so fetching more is free.
SERPAPI_NUM_RESULTS = 20

# How many verified matches below the threshold to include in the alert email.
# Set to 1 for a single best result, 3 for top 3, 5 for top 5, etc.
TOP_RESULTS = 5

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1 — Fetch prices via SerpAPI
# ---------------------------------------------------------------------------

def fetch_shopping_results(product_name: str, min_price: int | None = None) -> list[dict]:
    """
    Query SerpAPI's Google Shopping endpoint for a product name.
    Returns a list of result dicts with title, price, retailer, and link.
    Raises an exception on HTTP errors; returns [] if no results found.

    min_price: optional integer price floor to filter out accessories.
    Note: sort_by and min_price conflict in SerpAPI and together return empty
    results, so sort_by is omitted when min_price is used. Verified results
    are already sorted by price in run() after Gemini verification.
    """
    log.info(f"[SerpAPI] Searching for: {product_name!r}" + (f" (min_price: {min_price})" if min_price else ""))

    params = {
        "engine": "google_shopping",
        "q": product_name,
        "location": "Amsterdam,North Holland,Netherlands",  # localises results to Amsterdam
        "gl": "nl",   # country: Netherlands (affects pricing and retailer selection)
        "hl": "en",   # response language: English (keeps parsing predictable)
        "num": SERPAPI_NUM_RESULTS,
        "api_key": SERPAPI_KEY,
    }

    if min_price is not None:
        # min_price filters accessories; sort_by conflicts with it so is omitted.
        # Verified results are sorted cheapest-first by run() after Gemini filtering.
        params["min_price"] = int(min_price)
    else:
        # No price floor set — sort by price to surface cheap results first.
        params["sort_by"] = "1"

    response = requests.get(
        "https://serpapi.com/search",
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    raw_results = data.get("shopping_results", [])
    if not raw_results:
        log.warning(f"[SerpAPI] No shopping results returned for {product_name!r}")
        return []

    # Normalise to a flat structure for the LLM
    results = []
    for item in raw_results[:SERPAPI_NUM_RESULTS]:
        results.append(
            {
                "title": item.get("title", ""),
                "price": item.get("price", ""),          # raw string, e.g. "€ 129.00"
                "retailer": item.get("source", ""),
                "link": quote(item.get("link", item.get("product_link", "")), safe=":/?=&#+%@!$'()*,;~"),
            }
        )

    log.info(f"[SerpAPI] Received {len(results)} results")
    return results


# ---------------------------------------------------------------------------
# Step 2 — Verify matches using Gemini
# ---------------------------------------------------------------------------

VERIFICATION_PROMPT = """\
You are a strict product matching assistant. The buyer is located in Amsterdam, Netherlands.

I am looking for a specific product:
  Name: {name}
  Description: {description}

Below are shopping results returned by a search engine (JSON array):
{results_json}

Your task:
1. Identify which results are a genuine match for the product based on the name and description.
2. Exclude:
   - Bundle deals (e.g. "grinder + accessories kit")
   - Refurbished, used, or open-box items unless the description explicitly allows them
   - Accessories or replacement parts for the product
   - Completely unrelated products
   - Retailers that do not ship to the Netherlands or are clearly US/UK-only with no EU presence
3. For each confirmed match, extract the numeric price as a float (strip currency symbols).
4. Return a JSON array of verified matches. Each element must have exactly these fields:
     title     (string)
     price     (float)
     currency  (string, e.g. "EUR", "USD" — infer from the price string or context)
     retailer  (string)
     link      (string)
     note      (string — one sentence explaining why this is a valid match and ships to NL)
5. If no results match, return an empty array: []

Respond with ONLY the JSON array. No explanation, no markdown fences.
"""


def verify_with_gemini(
    product: dict,
    serpapi_results: list[dict],
    client: genai.Client,
) -> list[dict]:
    """
    Send SerpAPI results to Gemini and return a list of verified product matches.
    Retries once on malformed JSON before giving up.
    """
    prompt = VERIFICATION_PROMPT.format(
        name=product["name"],
        description=product.get("description", "No additional description provided."),
        results_json=json.dumps(serpapi_results, indent=2),
    )

    for attempt in range(1, 3):  # up to 2 attempts
        log.info(f"[Gemini] Verifying matches (attempt {attempt})")
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            raw = response.text.strip()
            # Strip markdown code fences if Gemini wraps the JSON anyway
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            verified = json.loads(raw)
            if not isinstance(verified, list):
                raise ValueError("Expected a JSON array, got something else.")
            log.info(f"[Gemini] {len(verified)} verified match(es)")
            return verified
        except (json.JSONDecodeError, ValueError) as exc:
            log.error(f"[Gemini] Malformed JSON on attempt {attempt}: {exc}")
            if attempt == 2:
                log.error("[Gemini] Giving up after 2 failed attempts.")
                return []

    return []  # unreachable, but satisfies type checkers


# ---------------------------------------------------------------------------
# Step 2b — Scrape live price from the retailer's product page
# ---------------------------------------------------------------------------

SCRAPE_PRICE_PROMPT = """\
Below is the visible text content of a product page. Extract the current listed price.
Return ONLY valid JSON in this exact format:
  {{"price": <number>}}
or, if you cannot determine the price:
  {{"price": null}}
No explanation. No markdown. Just the JSON object.

Page text:
{text}
"""

# Maximum characters of cleaned page text sent to Gemini
SCRAPE_TEXT_LIMIT = 8_000

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def scrape_price_from_page(url: str, client: genai.Client) -> float | None:
    """
    Fetch a retailer product page and extract the live price using a layered approach:
      1. Parse JSON-LD structured data (schema.org Product) — no AI, very reliable
      2. Fall back to BeautifulSoup-cleaned text sent to Gemini
    Returns a float, or None if the price cannot be determined.
    All errors are soft — callers should treat None as "unknown".
    """
    log.info(f"[Scrape] Fetching: {url}")
    try:
        resp = requests.get(url, timeout=10, headers=_BROWSER_HEADERS)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        log.warning(f"[Scrape] Could not fetch page: {exc}")
        return None

    # Layer 1: JSON-LD structured data (schema.org Product)
    price = _price_from_json_ld(html)
    if price is not None:
        log.info(f"[Scrape] Price from JSON-LD: {price}")
        return price

    # Layer 2: Strip HTML to clean text, then ask Gemini
    log.info("[Scrape] No JSON-LD price found, falling back to Gemini on cleaned text.")
    return _price_from_gemini(html, client)


def _price_from_json_ld(html: str) -> float | None:
    """
    Parse all <script type="application/ld+json"> blocks and look for a
    schema.org Product node with an Offer price. Returns a float or None.
    """
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    for raw in scripts:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue

        # Unwrap @graph arrays
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        if not isinstance(nodes, list):
            nodes = [nodes]

        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("@type") not in ("Product", "IndividualProduct"):
                continue
            offers = node.get("offers") or node.get("Offers")
            if not offers:
                continue
            if isinstance(offers, list):
                offers = offers[0]
            price = offers.get("price") or offers.get("lowPrice")
            if price is not None:
                try:
                    return float(price)
                except (ValueError, TypeError):
                    continue
    return None


def _price_from_gemini(html: str, client: genai.Client) -> float | None:
    """
    Strip a page's HTML to readable text with BeautifulSoup, then ask Gemini
    to extract the listed price from that text.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))[:SCRAPE_TEXT_LIMIT]

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=SCRAPE_PRICE_PROMPT.format(text=text),
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        price = data.get("price")
        if price is None:
            log.info("[Scrape] Gemini could not determine price from page text.")
            return None
        live_price = float(price)
        log.info(f"[Scrape] Price from Gemini (cleaned text): {live_price}")
        return live_price
    except Exception as exc:
        log.warning(f"[Scrape] Gemini extraction failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Step 3 — Price check and email alert
# ---------------------------------------------------------------------------

def send_email_alert(
    product: dict,
    matches: list[dict],
    price_warnings: list[str | None],
) -> None:
    """
    Send a Gmail alert listing the top verified matches below the threshold.
    `matches` and `price_warnings` are parallel lists, sorted cheapest-first.
    Raises smtplib.SMTPException (or similar) on failure so the caller can
    let GitHub Actions mark the run as failed.
    """
    best = matches[0]
    count = len(matches)
    deals_str = f"{count} deal{'s' if count > 1 else ''} found"

    subject = (
        f"Price Alert: {product['name']} — "
        f"best price {best['currency']} {float(best['price']):.2f} ({deals_str})"
    )

    body_lines = [
        f"A price alert has been triggered for one of your tracked products.",
        f"",
        f"Product        : {product['name']}",
        f"Your threshold : {best['currency']} {float(product['threshold']):.2f}",
        f"",
        f"{deals_str.capitalize()} below your threshold:",
        f"",
    ]

    for i, (match, warning) in enumerate(zip(matches, price_warnings), start=1):
        body_lines += [
            f"{i}. {match['currency']} {float(match['price']):.2f} at {match['retailer']}",
            f"   Link  : {match['link']}",
            f"   Match : {match.get('note', 'Verified by Gemini.')}",
        ]
        if warning:
            body_lines += [
                f"   ⚠ Price mismatch: {warning}",
                f"   Please verify the price on the website before purchasing.",
            ]
        body_lines.append("")

    body_lines.append("-- Price Tracker")
    body = "\n".join(body_lines)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = product["alert_email"]
    msg.attach(MIMEText(body, "plain"))

    log.info(f"[Email] Sending alert to {product['alert_email']!r}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, product["alert_email"], msg.as_string())
    log.info("[Email] Alert sent successfully.")


# ---------------------------------------------------------------------------
# Step 4 — Logging to CSV
# ---------------------------------------------------------------------------

def append_history(row: dict) -> None:
    """
    Append one result row to price_history.csv.
    Creates the file with headers if it does not exist yet.
    """
    file_exists = HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
    log.info(f"[CSV] Row appended to {HISTORY_FILE}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run() -> None:
    """
    Main runner: iterates over all products in products.json and performs
    the full fetch → verify → alert → log pipeline for each one.
    """
    # Validate required environment variables
    missing = [
        var
        for var in ("SERPAPI_KEY", "GEMINI_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
        if not os.environ.get(var)
    ]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variable(s): {', '.join(missing)}"
        )

    # Load product list
    if not PRODUCTS_FILE.exists():
        raise FileNotFoundError(f"{PRODUCTS_FILE} not found. Please create it.")
    with PRODUCTS_FILE.open(encoding="utf-8") as f:
        products = json.load(f)
    log.info(f"Loaded {len(products)} product(s) from {PRODUCTS_FILE}")

    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for product in products:
        product_name = product["name"]
        min_price = product.get("min_price")  # optional — omit from products.json to disable
        log.info(f"--- Processing: {product_name!r} ---")

        # Default history row (used even on early failure)
        history_row = {
            "timestamp": timestamp,
            "product_name": product_name,
            "lowest_verified_price": "",
            "currency": product.get("currency", ""),
            "retailer": "",
            "link": "",
            "alert_sent": False,
        }

        try:
            # Step 1: Fetch
            results = fetch_shopping_results(product_name, min_price)
            if not results:
                log.warning(f"Skipping {product_name!r} — no SerpAPI results.")
                append_history(history_row)
                continue

            # Step 2: Verify
            verified = verify_with_gemini(product, results, gemini_client)
            if not verified:
                log.warning(f"Skipping {product_name!r} — no verified matches from Gemini.")
                append_history(history_row)
                continue

            # Step 2c: Filter out manually excluded retailers
            excluded = {r.lower() for r in product.get("excluded_retailers", [])}
            if excluded:
                before = len(verified)
                verified = [m for m in verified if m["retailer"].lower() not in excluded]
                log.info(f"[Filter] Excluded {before - len(verified)} result(s) by retailer.")
            if not verified:
                log.warning(f"Skipping {product_name!r} — all matches excluded by retailer filter.")
                append_history(history_row)
                continue

            # Step 3: Sort by price, filter below threshold, take top N
            sorted_verified = sorted(verified, key=lambda m: float(m["price"]))
            top_matches = [
                m for m in sorted_verified
                if float(m["price"]) < float(product["threshold"])
            ][:TOP_RESULTS]

            best = sorted_verified[0]  # cheapest overall, for CSV logging
            log.info(
                f"Lowest verified price: {best['currency']} {float(best['price']):.2f}"
                f" at {best['retailer']!r}"
            )
            log.info(
                f"{len(top_matches)} of {len(sorted_verified)} verified match(es) "
                f"below threshold {product['threshold']}"
            )

            history_row.update(
                {
                    "lowest_verified_price": best["price"],
                    "currency": best["currency"],
                    "retailer": best["retailer"],
                    "link": best["link"],
                }
            )

            # Step 2b: Scrape live price for each match below threshold
            price_warnings: list[str | None] = []
            for match in top_matches:
                live_price = scrape_price_from_page(match["link"], gemini_client)
                warning = None
                if live_price is not None:
                    shopping_price = float(match["price"])
                    diff_pct = abs(live_price - shopping_price) / shopping_price
                    if diff_pct > 0.10:
                        warning = (
                            f"Google Shopping shows {match['currency']} {shopping_price:.2f}, "
                            f"but the website shows {match['currency']} {live_price:.2f} "
                            f"({diff_pct * 100:.0f}% difference)."
                        )
                        log.warning(f"[Scrape] Price mismatch for {match['retailer']!r}: {warning}")
                price_warnings.append(warning)

            # Step 3: Alert if any matches are below threshold
            alert_sent = False
            if top_matches:
                log.info(
                    f"Sending alert with {len(top_matches)} result(s) below threshold."
                )
                send_email_alert(product, top_matches, price_warnings)
                alert_sent = True
            else:
                log.info(
                    f"Price {best['price']} >= threshold {product['threshold']} — no alert."
                )

            history_row["alert_sent"] = alert_sent

        except Exception as exc:
            log.error(f"Unexpected error while processing {product_name!r}: {exc}", exc_info=True)
            # Re-raise email failures so GitHub Actions marks the run as failed
            if "smtp" in type(exc).__name__.lower() or "email" in str(exc).lower():
                append_history(history_row)
                raise

        finally:
            # Step 4: Always write to log
            append_history(history_row)

    log.info("All products processed.")


if __name__ == "__main__":
    run()
