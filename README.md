# Price Tracker

A Python tool that monitors product prices via Google Shopping (SerpAPI), verifies matches using Google Gemini (free tier), and sends you a Gmail alert when a price drops below your threshold. It runs automatically twice a day via GitHub Actions and logs every check to `price_history.csv`.

---

## How it works

1. **Fetch** — searches Google Shopping for each product in `products.json` using SerpAPI.
2. **Verify** — sends the results to Gemini, which filters out bundles, refurbished items, accessories, and unrelated products, and returns only genuine matches.
3. **Alert** — if the lowest verified price is below your defined threshold, an email is sent to your configured address.
4. **Log** — every check (pass or fail) is appended to `price_history.csv` with timestamp, price, retailer, link, and whether an alert was sent.

---

## One-time setup

### 1. SerpAPI account

1. Sign up at [serpapi.com](https://serpapi.com) (free tier: 100 searches/month).
2. Copy your **API key** from the dashboard.

### 2. Google Gemini API key (free)

1. Go to **aistudio.google.com** and sign in with your Google account.
2. Click **Get API key** → **Create API key**.
3. Copy the key.

### 3. Gmail App Password

Gmail requires an App Password when 2-Step Verification is enabled (which it should be):

1. Go to your Google Account → **Security** → **2-Step Verification** (enable if not already).
2. Then go to **Security** → **App Passwords**.
3. Create a new App Password (name it "Price Tracker" or similar).
4. Copy the 16-character password — you will not see it again.

> Your regular Gmail password will **not** work. You must use the App Password.

### 4. Fork or push this repo to GitHub

Push this project to a GitHub repository you own.

### 5. Add GitHub repository secrets

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Add all four secrets:

| Secret name         | Value                                  |
|---------------------|----------------------------------------|
| `SERPAPI_KEY`       | Your SerpAPI API key                   |
| `GEMINI_API_KEY`    | Your Google Gemini API key             |
| `GMAIL_ADDRESS`     | Your Gmail address (e.g. you@gmail.com)|
| `GMAIL_APP_PASSWORD`| The 16-character App Password          |

### 6. Enable GitHub Actions write permissions

Go to your repo → **Settings** → **Actions** → **General** → **Workflow permissions** → select **Read and write permissions**.

This allows the workflow to commit `price_history.csv` back to the repo after each run.

---

## Adding a product

Edit `products.json` and add a new object to the array:

```json
[
  {
    "name": "Baratza Encore coffee grinder",
    "description": "Entry-level burr grinder, new (not refurbished), standard Encore or Encore ESP model only. Exclude accessories, replacement parts, bundles, or any used/open-box listings.",
    "threshold": 120,
    "currency": "EUR",
    "alert_email": "you@gmail.com"
  },
  {
    "name": "Sony WH-1000XM5 headphones",
    "description": "Wireless over-ear noise-cancelling headphones, new condition only. Exclude XM4 or older models, third-party accessories, or bundle deals.",
    "threshold": 280,
    "currency": "EUR",
    "alert_email": "you@gmail.com"
  }
]
```

### Fields

| Field          | Required | Description |
|----------------|----------|-------------|
| `name`         | Yes      | Product name used as the search query. Be specific. |
| `description`  | Yes      | **Fill this in carefully.** Gemini uses this to decide what counts as a genuine match. Be explicit about what to exclude (refurbished, bundles, accessories, wrong model variants). The more detail here, the fewer false alerts you get. |
| `threshold`    | Yes      | Alert is sent when the price drops below this number. |
| `currency`     | Yes      | Informational — used in email alerts and CSV logs. |
| `alert_email`  | Yes      | Email address to notify when the threshold is crossed. |

---

## Adjusting the check frequency

Open [`.github/workflows/schedule.yml`](.github/workflows/schedule.yml) and edit the `cron` lines:

```yaml
on:
  schedule:
    - cron: "0 9 * * *"   # 9:00 UTC daily
    - cron: "0 18 * * *"  # 18:00 UTC daily
```

Cron syntax: `minute hour day-of-month month day-of-week` (all in UTC).

Examples:
- Once a day at noon UTC: `0 12 * * *`
- Every 6 hours: `0 */6 * * *`
- Weekdays only at 8:00 UTC: `0 8 * * 1-5`

---

## SerpAPI free tier limits

The SerpAPI free tier includes **100 searches per month**.

**Usage formula:**

```
searches/month = number_of_products × checks_per_day × ~30
```

| Products | Checks/day | Searches/month |
|----------|------------|----------------|
| 1        | 2          | ~60            |
| 1        | 3          | ~90            |
| 2        | 2          | ~120 ⚠️ over limit |
| 3        | 1          | ~90            |

If you track more than 1–2 products at the default frequency, consider:
- Reducing to one check per day.
- Upgrading to a paid SerpAPI plan.

---

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export SERPAPI_KEY="your_key"
export GEMINI_API_KEY="your_key"
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="your_app_password"

# Run
python checker.py
```

---

## Project structure

```
price-tracker/
├── .github/workflows/schedule.yml   # GitHub Actions cron workflow
├── products.json                    # Your tracked products (edit this)
├── price_history.csv                # Auto-updated log of every check
├── checker.py                       # Main script
├── requirements.txt                 # Python dependencies
└── README.md                        # This file
```

---

## price_history.csv columns

| Column                  | Description |
|-------------------------|-------------|
| `timestamp`             | ISO 8601 UTC timestamp of the check |
| `product_name`          | Name from products.json |
| `lowest_verified_price` | Lowest price Gemini confirmed as a genuine match |
| `currency`              | Currency of that price |
| `retailer`              | Store name |
| `link`                  | Direct link to the listing |
| `alert_sent`            | `True` if an email alert was sent, `False` otherwise |
