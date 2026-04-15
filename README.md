# LinkedIn Profile Scraper

Scrape LinkedIn and Sales Navigator search results into a CSV file.

## Requirements

- Python 3.9 or later
- Mac or Linux (Windows: run commands in Git Bash)

## Setup (first time only)

Run 
```Working directory
cd [move scraper file here]
```
THEN

```bash
bash setup.sh
```

This creates a virtual environment and installs everything including Chromium.

## Running
An icon should appear on your desktop/desktop folder, you may double click that --however, if it fails then you can try to run the command: 

```bash
bash run.sh
```

Opens the app at **http://localhost:5050** automatically.

## How to use

1. **Paste a search URL** — go to LinkedIn or Sales Navigator, apply your filters, copy the URL from the address bar, paste it in. (or to use multiple urls follow the directions on the googlesheet/csv page and make sure formatting is correct)

2. **Click Start Scraping** — After clicking the start scraping button a popup window will appear for you to login to your linkedin account [*PLEASE DO SO and do NOT close the tab*] Then the scraper will start.
3.  **Results** Results appear in the preview table. You may want to rename your file in the [Rename files...] button. THEN Download as CSV when done

