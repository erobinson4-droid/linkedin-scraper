# LinkedIn Profile Scraper

Scrape LinkedIn and Sales Navigator search results into a CSV file.

## Requirements

- Python 3.9 or later
- Mac or Linux (Windows: run commands in Git Bash)

## Setup (first time only)

```bash
bash setup.sh
```

This creates a virtual environment and installs everything including Chromium.

## Running

```bash
bash run.sh
```

Opens the app at **http://localhost:5050** automatically.

## How to use

1. **Get your `li_at` cookie** — this is your LinkedIn login token:
   - Open [linkedin.com](https://linkedin.com) in Chrome and log in
   - Press `F12` to open DevTools
   - Go to **Application** tab → **Cookies** → `https://www.linkedin.com`
   - Find the row named `li_at` and copy its **Value**
   - Paste it into the cookie field in the app

2. **Paste a search URL** — go to LinkedIn or Sales Navigator, apply your filters, copy the URL from the address bar, paste it in.

3. **Click Start Scraping** — results appear in the preview table. Download as CSV when done.

> Your `li_at` cookie is only used locally and never stored. It expires periodically — if you get a login error, just refresh it.
