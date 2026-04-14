from __future__ import annotations

import asyncio
import random
import re
import tempfile
import time
from playwright.async_api import async_playwright

# ── Safety limits (matching PhantomBuster's recommended thresholds) ────────────
DAILY_PROFILE_LIMIT = 100        # warn + stop after this many profiles per session

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def _rand_ms(lo: int, hi: int, page=None):
    """Sleep for a random number of milliseconds between lo and hi."""
    ms = random.randint(lo, hi)
    if page:
        await page.wait_for_timeout(ms)
    else:
        await asyncio.sleep(ms / 1000)


def _is_sales_nav(url: str) -> bool:
    return "linkedin.com/sales/" in url


def _make_emit(progress_cb):
    """Return an async emit() function wrapping progress_cb."""
    async def emit(msg: str):
        if progress_cb:
            if asyncio.iscoroutinefunction(progress_cb):
                await progress_cb(msg)
            else:
                progress_cb(msg)
    return emit


# ── Public API ─────────────────────────────────────────────────────────────────

async def scrape_linkedin_profiles(url: str, max_pages: int,
                                   progress_cb=None, stop_after: int | None = None,
                                   li_at: str = ""):
    """
    Scrape a single LinkedIn / Sales Navigator search URL.

    stop_after: stop collecting once this many profiles are gathered (trims
    last page automatically).  None = no limit.
    li_at: LinkedIn session cookie value for authentication.
    """
    emit = _make_emit(progress_cb)
    sales_nav = _is_sales_nav(url)
    await emit(f"Mode: {'Sales Navigator' if sales_nav else 'LinkedIn Search'}")

    profiles = []
    session_start = time.time()

    async with async_playwright() as pw:
        context = await _launch_context(pw, li_at)
        page = context.pages[0] if context.pages else await context.new_page()

        ok = await _ensure_logged_in(page, url, emit)
        if not ok:
            await context.close()
            return profiles

        profiles = await _scrape_one_url(page, url, max_pages, stop_after, emit,
                                         session_start=session_start)

        # Tag every profile with its source URL
        for p in profiles:
            p.setdefault("search_url", url)

        elapsed = (time.time() - session_start) / 60
        await emit(f"Done. {len(profiles)} profile(s) collected in {elapsed:.1f} min.")
        await context.close()

    return profiles


async def scrape_linkedin_profiles_batch(urls: list[str], accounts_per_search: int,
                                         progress_cb=None, li_at: str = ""):
    """
    Scrape multiple Sales Navigator search URLs in a single browser session.

    Opens the browser once, loops through each URL, collects up to
    accounts_per_search profiles per URL, then closes the browser.
    Errors on individual URLs are logged and skipped — the batch continues.
    li_at: LinkedIn session cookie value for authentication.
    """
    import math

    emit = _make_emit(progress_cb)
    max_pages = math.ceil(accounts_per_search / 25)
    total = len(urls)
    all_profiles: list[dict] = []
    succeeded = 0

    await emit(f"Batch mode: {total} search URL(s), up to {accounts_per_search} accounts each "
               f"({max_pages} page(s) per search).")

    if total > 20:
        await emit(f"WARNING: {total} URLs — this batch may take a long time.")

    session_start = time.time()

    async with async_playwright() as pw:
        context = await _launch_context(pw, li_at)
        page = context.pages[0] if context.pages else await context.new_page()

        for i, url in enumerate(urls):
            prefix = f"[{i + 1}/{total}]"

            # Build a prefixed emit for this search
            async def _emit(msg, _p=prefix):
                await emit(f"{_p} {msg}")

            # Between-search cooldown (skip before the first one)
            if i > 0:
                delay_s = random.uniform(4, 9)
                await _emit(f"Cooling down {delay_s:.1f}s before next search …")
                await asyncio.sleep(delay_s)

            try:
                ok = await _ensure_logged_in(page, url, _emit)
                if not ok:
                    await _emit("Skipping — could not log in.")
                    continue

                profiles = await _scrape_one_url(page, url, max_pages,
                                                 stop_after=accounts_per_search,
                                                 emit=_emit,
                                                 session_start=session_start)

                # Trim to exactly accounts_per_search
                profiles = profiles[:accounts_per_search]

                # Tag each profile with its source URL and search index (1-based)
                for p in profiles:
                    p["search_url"] = url
                    p["search_index"] = i + 1

                all_profiles.extend(profiles)
                succeeded += 1
                await _emit(f"Got {len(profiles)} profile(s). Running total: {len(all_profiles)}.")

            except Exception as exc:
                await _emit(f"ERROR: {exc} — continuing with next URL.")

        await context.close()

    await emit(
        f"Completed {succeeded} of {total} searches. "
        f"Collected {len(all_profiles)} total profile(s)."
    )
    return all_profiles


# ── Core scraping loop ─────────────────────────────────────────────────────────

async def _scrape_one_url(page, url: str, max_pages: int,
                          stop_after: int | None, emit,
                          session_start: float | None = None) -> list[dict]:
    """
    Inner pagination loop — no browser management, no login handling.
    Returns a list of profile dicts (without search_url — callers add that).
    """
    profiles: list[dict] = []
    seen_urls: set[str] = set()
    sales_nav = _is_sales_nav(url)
    if session_start is None:
        session_start = time.time()

    current_page = 1
    while current_page <= max_pages:

        # ── Daily limit guard ────────────────────────────────────────────────
        if len(profiles) >= DAILY_PROFILE_LIMIT:
            await emit(f"WARNING: Reached {DAILY_PROFILE_LIMIT} profiles — stopping to "
                       f"protect your account. Resume tomorrow.")
            break

        await emit(f"Scraping page {current_page} …")
        await _scroll_to_load(page)

        if sales_nav:
            page_profiles = await _extract_salesnav(page, emit)
        else:
            page_profiles = await _extract_regular(page)

        new_count = 0
        for p in page_profiles:
            if p["url"] not in seen_urls:
                seen_urls.add(p["url"])
                profiles.append(p)
                new_count += 1

        await emit(f"  → {new_count} new profile(s) on page {current_page} "
                   f"(total: {len(profiles)})")

        if new_count == 0:
            await emit("No new results — stopping.")
            break

        # Early-stop if we already have enough
        if stop_after is not None and len(profiles) >= stop_after:
            await emit(f"Reached target of {stop_after} profiles — stopping early.")
            break

        # ── Pagination ──────────────────────────────────────────────────────
        went_next = False
        next_btn = await _find_next_button(page)

        if next_btn:
            is_disabled = await next_btn.get_attribute("disabled")
            aria_disabled = await next_btn.get_attribute("aria-disabled")
            if is_disabled is None and aria_disabled != "true":
                await next_btn.scroll_into_view_if_needed()
                await _rand_ms(800, 2_200, page)   # pause before clicking
                await next_btn.click()
                went_next = True
                await emit(f"  Navigating to page {current_page + 1} …")
                await _wait_for_new_results(page, sales_nav)
            else:
                await emit("Reached last page.")
                break
        elif sales_nav:
            next_url = _salesnav_next_url(page.url, current_page + 1)
            await emit(f"  No Next button — navigating by URL to page {current_page + 1} …")
            await page.goto(next_url, wait_until="domcontentloaded", timeout=60_000)
            await _wait_for_new_results(page, sales_nav)
            went_next = True
        else:
            await emit("No Next button found — reached last page.")
            break

        if not went_next:
            break

        current_page += 1

    return profiles


# ── Browser helpers ────────────────────────────────────────────────────────────

async def _launch_context(pw, li_at: str = ""):
    tmp_dir = tempfile.mkdtemp()
    # Randomise viewport slightly so every session looks different
    width  = random.randint(1260, 1420)
    height = random.randint(860, 960)
    context = await pw.chromium.launch_persistent_context(
        tmp_dir,
        headless=True,
        viewport={"width": width, "height": height},
        user_agent=_USER_AGENT,
        locale="en-US",
        timezone_id="America/New_York",
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
              "--disable-dev-shm-usage"],
        ignore_default_args=["--enable-automation"],
    )
    if li_at:
        await context.add_cookies([{
            "name": "li_at",
            "value": li_at,
            "domain": ".linkedin.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "None",
        }])
    return context


async def _ensure_logged_in(page, url: str, emit) -> bool:
    """
    Navigate to url and verify the li_at cookie authenticated the session.
    Returns True if we land on the target page, False if still on a login wall.
    """
    await emit("Opening LinkedIn …")
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await _rand_ms(3_000, 6_000, page)   # human reading time before checking

    if any(x in page.url for x in ("login", "authwall", "checkpoint", "uas/authenticate")):
        await emit("ERROR: Not logged in — your li_at cookie may be expired or invalid. "
                   "Please refresh it and try again.")
        return False

    return True


# ── Sales Navigator extractor ──────────────────────────────────────────────────

async def _extract_salesnav(page, emit):
    profiles = []

    # Wait for at least one result card
    try:
        await page.wait_for_selector(
            '[data-anonymize="person-name"], '
            '.result-lockup__name, '
            'li.artdeco-list__item',
            timeout=12_000,
        )
    except Exception:
        await emit("  WARNING: Result cards did not appear — page may not have loaded fully.")
        snippet = await page.evaluate("document.body.innerText.slice(0, 400)")
        await emit(f"  Page text preview: {snippet[:200]}")
        return profiles

    # Force all cards to render by scrolling each into view individually.
    # Sales Nav lazy-renders via IntersectionObserver — bulk scroll misses cards.
    await _force_render_all_cards(page)

    # Prefer selectors that only match actual profile cards, not generic list items
    card_selectors = [
        "[data-view-name='search-results-lead-result']",
        ".search-results__result-item",
        ".leads-search-result__person-container",
        "li.artdeco-list__item:has([data-anonymize='person-name'])",
        "li.artdeco-list__item:has(a[href*='/sales/lead/'])",
        "li.artdeco-list__item",
    ]

    cards = []
    for sel in card_selectors:
        try:
            found = await page.query_selector_all(sel)
        except Exception:
            continue
        if found:
            cards = found
            await emit(f"  Using card selector: {sel!r} → {len(found)} cards found")
            break

    if not cards:
        await emit("  WARNING: Could not find result cards with known selectors.")
        return profiles

    # Filter out non-profile cards (ad slots, etc.)
    real_cards = []
    for card in cards:
        link = (
            await card.query_selector('a[data-anonymize="person-name"]')
            or await card.query_selector('a[href*="/sales/lead/"]')
            or await card.query_selector('a[href*="/in/"]')
        )
        if link:
            real_cards.append(card)
    if real_cards:
        cards = real_cards
    await emit(f"  {len(cards)} profile card(s) ready for extraction")

    for card in cards:
        try:
            profile: dict[str, str] = {}

            # ── Profile URL ──────────────────────────────────────────────────
            link_el = (
                await card.query_selector('a[data-anonymize="person-name"]')
                or await card.query_selector('a[href*="/in/"]')
                or await card.query_selector('a[href*="/sales/lead/"]')
                or await card.query_selector('a[href*="/sales/people/"]')
            )
            if not link_el:
                continue

            href = await link_el.get_attribute("href") or ""
            href = re.split(r"[?#]", href)[0].rstrip("/")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.linkedin.com" + href
            profile["url"] = href

            # ── VMID ─────────────────────────────────────────────────────────
            # The VMID is the encoded slug in the Sales Nav URL — the string
            # itself IS the identifier.  Do NOT base64-decode it to a number.
            #
            # Priority:
            # 1. data-chameleon-result-urn / data-entity-urn on the card
            #    e.g. urn:li:fsd_salesProfile:(ACwAABx...,NAME_SEARCH,0)
            # 2. The /sales/lead/{VMID},... slug directly from the href
            #
            # Verification: https://www.linkedin.com/in/{vmid}/ in a logged-in
            # browser should redirect to the correct person's profile.
            vmid = ""
            for urn_attr in ("data-chameleon-result-urn", "data-entity-urn"):
                raw_urn = await card.get_attribute(urn_attr) or ""
                if not raw_urn:
                    el = await card.query_selector(f"[{urn_attr}]")
                    if el:
                        raw_urn = await el.get_attribute(urn_attr) or ""
                if raw_urn:
                    vmid = _vmid_from_urn(raw_urn)
                    if vmid:
                        break
            if not vmid:
                vmid = _vmid_from_salesnav_url(href)
            if not vmid:
                await emit(f"  WARNING: could not extract VMID (url={href[:60]})")
            profile["vmid"] = vmid

            # ── Name ─────────────────────────────────────────────────────────
            name_el = await card.query_selector(
                '[data-anonymize="person-name"], '
                '.result-lockup__name, '
                '.artdeco-entity-lockup__title'
            )
            profile["name"] = (await name_el.inner_text()).strip() if name_el else ""

            # ── Title ─────────────────────────────────────────────────────────
            title_el = await card.query_selector(
                '[data-anonymize="job-title"], '
                '[data-anonymize="title"], '
                '.result-lockup__highlight-keyword, '
                '.artdeco-entity-lockup__subtitle'
            )
            profile["title"] = (await title_el.inner_text()).strip() if title_el else ""

            # ── Company ───────────────────────────────────────────────────────
            company_el = await card.query_selector(
                '[data-anonymize="company-name"], '
                '.result-lockup__position-company a, '
                'a[data-anonymize="company-name"]'
            )
            profile["company"] = (await company_el.inner_text()).strip() if company_el else ""

            # ── Location ──────────────────────────────────────────────────────
            loc_el = await card.query_selector(
                '[data-anonymize="person-distance"], '
                '[data-anonymize="location"], '
                '.result-lockup__misc-item'
            )
            profile["location"] = (await loc_el.inner_text()).strip() if loc_el else ""

            # ── Time at Company ───────────────────────────────────────────────
            # Use inner_text() to get all visible lines in the card, then find
            # a line containing a duration (e.g. "2 yr 3 mo at current position").
            # Prefer lines that also contain "at current" — fall back to any
            # line with a duration pattern if no "at current" line is found.
            try:
                card_text = await card.inner_text()
                dur_pat = re.compile(
                    r'\d+\s*(?:yr|yrs|year|years|mo|mos|month|months)', re.IGNORECASE
                )
                tenure = ""
                for line in card_text.splitlines():
                    line = line.strip()
                    if dur_pat.search(line):
                        tenure = line
                        if re.search(r'at\s+current', line, re.IGNORECASE):
                            break
                profile["time_at_company"] = tenure
            except Exception:
                profile["time_at_company"] = ""

            # Debug: emit first card's raw text so we can verify the format
            if not profiles:
                await emit(f"  [debug] first card text: {card_text[:300]!r}")

            profiles.append(profile)
        except Exception:
            continue

    return profiles


# ── Regular LinkedIn search extractor ─────────────────────────────────────────

async def _extract_regular(page):
    profiles = []

    card_selectors = [
        ".entity-result__item",
        ".reusable-search__result-container",
        "li.reusable-search__result-container",
    ]
    cards = []
    for sel in card_selectors:
        cards = await page.query_selector_all(sel)
        if cards:
            break

    for card in cards:
        try:
            profile: dict[str, str] = {}

            link_el = await card.query_selector('a[href*="/in/"]')
            if not link_el:
                continue
            href = await link_el.get_attribute("href") or ""
            href = re.split(r"[?#]", href)[0].rstrip("/")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.linkedin.com" + href
            profile["url"] = href

            name_el = await card.query_selector(
                ".entity-result__title-text, "
                ".app-aware-link span[aria-hidden='true']"
            )
            profile["name"] = (await name_el.inner_text()).strip() if name_el else ""

            title_el = await card.query_selector(
                ".entity-result__primary-subtitle, .subline-level-1"
            )
            profile["title"] = (await title_el.inner_text()).strip() if title_el else ""

            profile["company"] = ""
            profile["vmid"] = ""

            loc_el = await card.query_selector(
                ".entity-result__secondary-subtitle, .subline-level-2"
            )
            profile["location"] = (await loc_el.inner_text()).strip() if loc_el else ""

            profile["time_at_company"] = ""

            profiles.append(profile)
        except Exception:
            continue

    return profiles


# ── Page helpers ───────────────────────────────────────────────────────────────

async def _force_render_all_cards(page):
    """
    Sales Nav renders cards lazily via IntersectionObserver.
    Scroll every list item into view individually so all cards on the page
    actually receive their render trigger before extraction.
    """
    items = await page.query_selector_all("li.artdeco-list__item")
    for item in items:
        try:
            await item.scroll_into_view_if_needed()
            await _rand_ms(80, 260, page)
        except Exception:
            pass
    try:
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    await page.evaluate("window.scrollTo(0, 0)")
    await _rand_ms(300, 700, page)


async def _scroll_to_load(page):
    """Scroll top-to-bottom in small random steps to mimic human reading."""
    total_height = await page.evaluate("document.body.scrollHeight")
    position = 0
    while position < total_height:
        step = random.randint(120, 320)   # humans scroll in uneven bursts
        position = min(position + step, total_height)
        await page.evaluate(f"window.scrollTo(0, {position})")
        await _rand_ms(120, 500, page)
        # Occasionally pause longer as if reading something
        if random.random() < 0.12:
            await _rand_ms(800, 2_000, page)
        try:
            await page.wait_for_load_state("networkidle", timeout=3_000)
        except Exception:
            pass
        total_height = await page.evaluate("document.body.scrollHeight")
    await page.evaluate("window.scrollTo(0, 0)")
    await _rand_ms(400, 900, page)


async def _find_next_button(page):
    """Return the Next pagination button, or None."""
    for sel in [
        'button[aria-label="Next"]',
        'button[data-test-pagination-page-btn="next"]',
        'li.artdeco-pagination__button--next button',
        'button.artdeco-pagination__button--next',
        'button[data-test-next-btn]',
        'li.search-results__pagination--next button',
    ]:
        btn = await page.query_selector(sel)
        if btn:
            return btn
    return None


async def _wait_for_new_results(page, sales_nav: bool):
    """Wait for the result list to populate after a page transition."""
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    card_sel = (
        '[data-anonymize="person-name"], li.artdeco-list__item'
        if sales_nav else
        '.entity-result__item, li.reusable-search__result-container'
    )
    try:
        await page.wait_for_selector(card_sel, timeout=15_000)
    except Exception:
        pass
    await _rand_ms(1_000, 3_000, page)   # human "reading" pause after page load


# ── VMID helpers ───────────────────────────────────────────────────────────────
# The VMID is the encoded slug LinkedIn uses as a member's persistent ID.
# It is NOT a number derived from base64-decoding — the encoded string IS the ID.
# Tools like PhantomBuster, Evaboot, and LinkedIn URNs all use the encoded form,
# e.g. "ACwAABc1d2E...".  DO NOT decode it to bytes or integers.

_VMID_RE = re.compile(r"^[A-Za-z0-9_-]{10,60}$")


def _validate_vmid(candidate: str) -> str:
    """Return candidate if it matches the VMID pattern, else empty string."""
    return candidate if (candidate and _VMID_RE.match(candidate)) else ""


def _vmid_from_urn(urn: str) -> str:
    """
    Extract VMID from a LinkedIn URN.
      urn:li:fsd_salesProfile:(ACwAABx...,NAME_SEARCH,0)  → ACwAABx...
      urn:li:fsd_profile:ACwAABx...                       → ACwAABx...
    """
    m = re.search(r"\(([A-Za-z0-9_-]+)[,)]", urn)
    if m:
        return _validate_vmid(m.group(1))
    m = re.search(r":([A-Za-z0-9_-]{10,60})$", urn)
    if m:
        return _validate_vmid(m.group(1))
    return ""


def _vmid_from_salesnav_url(url: str) -> str:
    """
    Extract VMID from /sales/lead/{VMID},{context},... — no decoding needed.
    """
    m = re.search(r"/sales/(?:lead|people)/([A-Za-z0-9_-]+?)(?:[,?#/]|$)", url)
    return _validate_vmid(m.group(1)) if m else ""


def _salesnav_next_url(current_url: str, next_page: int) -> str:
    """Increment or insert the page= parameter in a Sales Nav URL."""
    if re.search(r"[?&]page=\d+", current_url):
        return re.sub(r"(page=)\d+", f"\\g<1>{next_page}", current_url)
    sep = "&" if "?" in current_url else "?"
    return f"{current_url}{sep}page={next_page}"
