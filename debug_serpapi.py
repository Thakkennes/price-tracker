"""
debug_serpapi.py — Inspect raw SerpAPI results and Gemini verification side-by-side.

Usage:
  python debug_serpapi.py

Prints two tables per product:
  1. Every raw result returned by SerpAPI (title, price, retailer, link)
  2. The subset that Gemini accepts as genuine matches

Use this to diagnose why certain cheap listings don't appear in alerts —
it narrows the cause to either SerpAPI not returning them, or Gemini rejecting them.

Requires the same environment variables as checker.py:
  SERPAPI_KEY, GEMINI_API_KEY
"""

import json
import os
from pathlib import Path

from google import genai

# Import the live functions directly from checker.py — no code duplication.
from checker import fetch_shopping_results, verify_with_gemini, GEMINI_MODEL

PRODUCTS_FILE = Path("products.json")

# Column widths for the printed tables
W_PRICE    = 12
W_RETAILER = 26
W_TITLE    = 52
W_NOTE     = 60


def _row(num: int, price: str, retailer: str, text: str) -> str:
    price    = price[:W_PRICE].ljust(W_PRICE)
    retailer = retailer[:W_RETAILER].ljust(W_RETAILER)
    text     = text[:W_TITLE]
    return f"  {num:>2}.  {price}  {retailer}  {text}"


def _header(price_col: str, right_col: str, right_width: int) -> str:
    price_col = price_col.ljust(W_PRICE)
    retailer  = "Retailer".ljust(W_RETAILER)
    right_col = right_col[:right_width]
    return f"  {'#':>2}.  {price_col}  {retailer}  {right_col}"


def main() -> None:
    for var in ("SERPAPI_KEY", "GEMINI_API_KEY"):
        if not os.environ.get(var):
            raise EnvironmentError(f"Missing environment variable: {var}")

    with PRODUCTS_FILE.open(encoding="utf-8") as f:
        products = json.load(f)

    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    for product in products:
        name = product["name"]
        print(f"\n{'=' * 80}")
        print(f"  {name}")
        print(f"{'=' * 80}")

        # --- Raw SerpAPI results ---
        results = fetch_shopping_results(name)
        print(f"\n  Raw SerpAPI results ({len(results)} returned)\n")
        print(_header("Price", "Title", W_TITLE))
        print("  " + "-" * 76)
        for i, r in enumerate(results, 1):
            print(_row(i, r.get("price", ""), r.get("retailer", ""), r.get("title", "")))

        if not results:
            print("  (no results)")
            continue

        # --- Gemini-verified matches ---
        verified = verify_with_gemini(product, results, gemini_client)

        # Apply excluded_retailers filter (mirrors checker.py logic)
        excluded = {r.lower() for r in product.get("excluded_retailers", [])}
        if excluded:
            before = len(verified)
            verified = [m for m in verified if m["retailer"].lower() not in excluded]
            if before != len(verified):
                print(f"\n  ({before - len(verified)} result(s) hidden by excluded_retailers filter)")

        print(f"\n  Gemini-verified matches ({len(verified)} accepted)\n")
        if verified:
            sorted_v = sorted(verified, key=lambda m: float(m["price"]))
            print(_header("Price", "Note", W_NOTE))
            print("  " + "-" * 76)
            for i, m in enumerate(sorted_v, 1):
                price_str = f"{m.get('currency', '')} {float(m['price']):.2f}"
                print(_row(i, price_str, m.get("retailer", ""), m.get("note", "")))
        else:
            print("  (none — Gemini rejected all results)")

        threshold = product.get("threshold")
        if threshold and verified:
            below = [m for m in verified if float(m["price"]) < float(threshold)]
            print(f"\n  Matches below threshold {threshold}: {len(below)}")

    print(f"\n{'=' * 80}\n")


if __name__ == "__main__":
    main()
