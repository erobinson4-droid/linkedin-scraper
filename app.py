import asyncio
import csv
import io
import json
import math
import os
import queue
import re
import threading
import uuid

import requests as req_lib
from flask import Flask, Response, render_template, request, send_file, stream_with_context

from scraper import scrape_linkedin_profiles, scrape_linkedin_profiles_batch

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1.1 * 1024 * 1024  # slightly above 1 MB so we can give a nice error

# In-memory job store  { job_id: {"profiles": [...], "done": bool, "error": str|None} }
_jobs: dict[str, dict] = {}
_queues: dict[str, queue.Queue] = {}


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── CSV parsing helper (shared by Google Sheet and file-upload paths) ──────────

def parse_search_urls_from_csv(csv_text: str, skip_header: bool) -> list[str]:
    """
    Parse Sales Navigator search URLs from column A of a CSV string.

    Args:
        csv_text:    Raw CSV content as a UTF-8 string.
        skip_header: If True, the first non-empty data row is treated as a
                     header and skipped.

    Returns:
        Ordered list of unique, whitespace-stripped Sales Navigator URLs
        (those containing 'linkedin.com/sales/').  Empty cells and rows whose
        column-A value does not look like a Sales Nav URL are silently skipped.

    Validation rules:
        - Each URL must contain 'linkedin.com/sales/' to be included.
        - Empty cells and blank rows are ignored.
        - Duplicates are removed while preserving order.
    """
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    if skip_header and rows:
        rows = rows[1:]

    seen: set[str] = set()
    urls: list[str] = []
    for row in rows:
        if not row:
            continue
        cell = row[0].strip()
        if not cell or "linkedin.com/sales/" not in cell:
            continue
        if cell not in seen:
            seen.add(cell)
            urls.append(cell)

    return urls


def _sheet_url_to_csv_export(sheet_url: str) -> str:
    """
    Convert a Google Sheets browser URL to its public CSV export URL.
    Extracts the spreadsheet ID and optional gid (sheet tab) from the URL.
    """
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
    if not m:
        raise ValueError(
            "Invalid Google Sheets URL — could not find a spreadsheet ID. "
            "Expected format: https://docs.google.com/spreadsheets/d/{ID}/..."
        )
    sheet_id = m.group(1)
    gid_m = re.search(r"[#&?]gid=(\d+)", sheet_url)
    gid = gid_m.group(1) if gid_m else "0"
    return (f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/export?format=csv&gid={gid}")


def _decode_csv_bytes(raw: bytes) -> str:
    """Try common encodings; raise ValueError if all fail."""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    raise ValueError("Could not read file — please save it as UTF-8 CSV.")


# ── Start a scrape job  (POST /scrape) ─────────────────────────────────────────

@app.route("/scrape", methods=["POST"])
def start_scrape():
    is_multipart = bool(request.content_type and "multipart" in request.content_type)

    # ── Parse request ──────────────────────────────────────────────────────────
    if is_multipart:
        # ── File upload batch ──────────────────────────────────────────────────
        mode = "batch"
        accounts_per_search = int(request.form.get("accounts_per_search") or 25)
        skip_header = request.form.get("skip_header", "true").lower() in ("true", "1", "on")
        li_at = (request.form.get("li_at") or "").strip()

        csv_file = request.files.get("csv_file")
        if not csv_file:
            return {"error": "No CSV file provided."}, 400
        if not (csv_file.filename or "").lower().endswith(".csv"):
            return {"error": "File must have a .csv extension."}, 400

        raw = csv_file.read()
        if not raw:
            return {"error": "CSV file is empty."}, 400
        if len(raw) > 1024 * 1024:
            return {"error": "File exceeds 1 MB limit."}, 400

        try:
            csv_text = _decode_csv_bytes(raw)
        except ValueError as exc:
            return {"error": str(exc)}, 400

        urls = parse_search_urls_from_csv(csv_text, skip_header)
        if not urls:
            return {
                "error": (
                    "No valid Sales Navigator URLs found in column A. "
                    "Make sure URLs start with 'https://www.linkedin.com/sales/'."
                )
            }, 400

    else:
        # ── JSON request (single URL or Google Sheet batch) ────────────────────
        data = request.get_json(force=True) or {}
        mode = data.get("mode", "single")

        li_at = (data.get("li_at") or "").strip()

        if mode == "single":
            url = (data.get("url") or "").strip()
            max_pages = int(data.get("max_pages") or 10)
            if not url:
                return {"error": "No URL provided."}, 400
            if "linkedin.com" not in url:
                return {"error": "URL must be a linkedin.com link."}, 400

        else:
            # Batch via Google Sheet
            sheet_url = (data.get("sheet_url") or "").strip()
            accounts_per_search = int(data.get("accounts_per_search") or 25)
            skip_header = bool(data.get("skip_header", True))

            if not sheet_url:
                return {"error": "No Google Sheet URL provided."}, 400
            if "docs.google.com/spreadsheets" not in sheet_url:
                return {
                    "error": (
                        "Invalid Google Sheets URL. "
                        "Expected: https://docs.google.com/spreadsheets/d/{ID}/..."
                    )
                }, 400

            try:
                csv_export_url = _sheet_url_to_csv_export(sheet_url)
            except ValueError as exc:
                return {"error": str(exc)}, 400

            try:
                resp = req_lib.get(csv_export_url, timeout=15)
            except Exception as exc:
                return {"error": f"Could not fetch Google Sheet: {exc}"}, 400

            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                return {
                    "error": (
                        "Sheet is not publicly accessible — set sharing to "
                        "'Anyone with the link — Viewer' in Google Sheets."
                    )
                }, 400

            urls = parse_search_urls_from_csv(resp.text, skip_header)
            if not urls:
                return {
                    "error": (
                        "No valid Sales Navigator URLs found in column A of the sheet. "
                        "Make sure URLs start with 'https://www.linkedin.com/sales/'."
                    )
                }, 400

    # ── Launch job ─────────────────────────────────────────────────────────────
    job_id = uuid.uuid4().hex
    q: queue.Queue = queue.Queue()
    _queues[job_id] = q
    _jobs[job_id] = {"profiles": [], "done": False, "error": None}

    if mode == "single":
        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            def progress(msg: str):
                q.put(("progress", msg))

            try:
                profiles = loop.run_until_complete(
                    scrape_linkedin_profiles(url, max_pages, progress_cb=progress, li_at=li_at)
                )
                _jobs[job_id]["profiles"] = profiles
            except Exception as exc:
                _jobs[job_id]["error"] = str(exc)
                q.put(("error", str(exc)))
            finally:
                _jobs[job_id]["done"] = True
                q.put(("done", ""))
                loop.close()

    else:
        # Batch (Google Sheet or file upload — same downstream path)
        if len(urls) > 20:
            q.put(("progress",
                   f"WARNING: {len(urls)} URLs found — this batch may take a long time."))

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            def progress(msg: str):
                q.put(("progress", msg))

            try:
                profiles = loop.run_until_complete(
                    scrape_linkedin_profiles_batch(
                        urls, accounts_per_search, progress_cb=progress, li_at=li_at
                    )
                )
                _jobs[job_id]["profiles"] = profiles
            except Exception as exc:
                _jobs[job_id]["error"] = str(exc)
                q.put(("error", str(exc)))
            finally:
                _jobs[job_id]["done"] = True
                q.put(("done", ""))
                loop.close()

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


# ── Stream progress via Server-Sent Events ─────────────────────────────────────

@app.route("/progress/<job_id>")
def progress_stream(job_id: str):
    q = _queues.get(job_id)
    if q is None:
        return {"error": "Unknown job"}, 404

    def generate():
        while True:
            try:
                kind, msg = q.get(timeout=60)
            except queue.Empty:
                yield "data: {}\n\n"   # keep-alive
                continue

            payload = json.dumps({"kind": kind, "msg": msg})
            yield f"data: {payload}\n\n"

            if kind == "done":
                profiles = _jobs.get(job_id, {}).get("profiles", [])
                yield f"data: {json.dumps({'kind': 'profiles', 'profiles': profiles})}\n\n"
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Download CSV ───────────────────────────────────────────────────────────────

@app.route("/download", methods=["POST"])
def download_csv():
    data = request.get_json(force=True) or {}
    profiles = data.get("profiles", [])

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["search_url", "name", "title", "company", "time_at_company", "location", "vmid", "url"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(profiles)

    bytes_out = io.BytesIO(output.getvalue().encode("utf-8"))
    bytes_out.seek(0)
    return send_file(
        bytes_out,
        mimetype="text/csv",
        as_attachment=True,
        download_name="linkedin_profiles.csv",
    )


# ── Error handlers ─────────────────────────────────────────────────────────────

@app.errorhandler(413)
def too_large(e):
    return {"error": "File exceeds 1 MB limit."}, 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
