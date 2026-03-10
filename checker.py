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
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

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

# Number of SerpAPI results to fetch per product
SERPAPI_NUM_RESULTS = 10

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

def fetch_shopping_results(product_name: str) -> list[dict]:
    """
    Query SerpAPI's Google Shopping endpoint for a product name.
    Returns a list of result dicts with title, price, retailer, and link.
    Raises an exception on HTTP errors; returns [] if no results found.
    """
    log.info(f"[SerpAPI] Searching for: {product_name!r}")

    params = {
        "engine": "google_shopping",
        "q": product_name,
        "location": "Amsterdam,North Holland,Netherlands",  # localises results to Amsterdam
        "gl": "nl",   # country: Netherlands (affects pricing and retailer selection)
        "hl": "en",   # response language: English (keeps parsing predictable)
        "num": SERPAPI_NUM_RESULTS,
        "api_key": SERPAPI_KEY,
    }

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
                "link": item.get("link", item.get("product_link", "")),
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

    client = genai.Client(api_key=GEMINI_API_KEY)

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
# Step 3 — Price check and email alert
# ---------------------------------------------------------------------------

def send_email_alert(
    product: dict,
    match: dict,
) -> None:
    """
    Send a Gmail alert for a product whose price dropped below the threshold.
    Raises smtplib.SMTPException (or similar) on failure so the caller can
    let GitHub Actions mark the run as failed.
    """
    subject = (
        f"Price Alert: {product['name']} now {match['currency']} {match['price']:.2f}"
    )

    body_lines = [
        f"Good news! A price alert has been triggered for one of your tracked products.",
        f"",
        f"Product : {product['name']}",
        f"Found price : {match['currency']} {match['price']:.2f} at {match['retailer']}",
        f"Your threshold : {match['currency']} {product['threshold']:.2f}",
        f"Link : {match['link']}",
        f"",
        f"Why this is a match:",
        f"  {match.get('note', 'Verified by Gemini.')}",
        f"",
        f"-- Price Tracker",
    ]
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

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for product in products:
        product_name = product["name"]
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
            results = fetch_shopping_results(product_name)
            if not results:
                log.warning(f"Skipping {product_name!r} — no SerpAPI results.")
                append_history(history_row)
                continue

            # Step 2: Verify
            verified = verify_with_gemini(product, results)
            if not verified:
                log.warning(f"Skipping {product_name!r} — no verified matches from Gemini.")
                append_history(history_row)
                continue

            # Step 3: Find cheapest match
            best = min(verified, key=lambda m: float(m["price"]))
            log.info(
                f"Lowest verified price: {best['currency']} {best['price']:.2f}"
                f" at {best['retailer']!r}"
            )

            history_row.update(
                {
                    "lowest_verified_price": best["price"],
                    "currency": best["currency"],
                    "retailer": best["retailer"],
                    "link": best["link"],
                }
            )

            # Step 3: Alert if below threshold
            alert_sent = False
            if float(best["price"]) < float(product["threshold"]):
                log.info(
                    f"Price {best['price']} < threshold {product['threshold']} — sending alert."
                )
                send_email_alert(product, best)
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
