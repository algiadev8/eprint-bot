#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31.0",
#     "python-dotenv>=1.0.0",
#     "scrapling[fetchers]==0.5",
# ]
# ///
import os
import sys
import time
import json
import logging
import argparse
import email.utils
import tempfile
import fcntl
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit
from html.parser import HTMLParser
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import requests
from dotenv import load_dotenv
from scrapling.fetchers import StealthySession

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("eprint_bot")

# Load environment variables
load_dotenv()

# Configuration settings
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_NEW_WEBHOOK_URL = os.getenv("DISCORD_NEW_WEBHOOK_URL")
DISCORD_UPDATE_WEBHOOK_URL = os.getenv("DISCORD_UPDATE_WEBHOOK_URL")
LLM_API_URL = os.getenv("LLM_API_URL")
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")
SUMMARY_LANGUAGE = os.getenv("SUMMARY_LANGUAGE", "Japanese").strip()
STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()
EPRINT_RSS_URL = "https://eprint.iacr.org/rss/rss.xml"

# XML Namespaces used by IACR ePrint RSS
NAMESPACES = {
    'dc': 'http://purl.org/dc/elements/1.1/',
    'atom': 'http://www.w3.org/2005/Atom',
    'content': 'http://purl.org/rss/1.0/modules/content/'
}

def validate_config(required=None):
    """Verify that all required environment variables are set."""
    if required is None:
        required = {
            "Discord webhook (DISCORD_NEW_WEBHOOK_URL or DISCORD_WEBHOOK_URL)": (
                DISCORD_NEW_WEBHOOK_URL or DISCORD_WEBHOOK_URL
            ),
            "Discord webhook (DISCORD_UPDATE_WEBHOOK_URL or DISCORD_WEBHOOK_URL)": (
                DISCORD_UPDATE_WEBHOOK_URL or DISCORD_WEBHOOK_URL
            ),
            "LLM_API_URL": LLM_API_URL,
            "LLM_API_KEY": LLM_API_KEY,
            "LLM_MODEL": LLM_MODEL,
        }

    missing = []
    for name, value in required.items():
        if not value:
            missing.append(name)
        
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        logger.error("Please create a .env file based on .env.example with the correct values.")
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

def load_state():
    """Load previously processed paper IDs/GUIDs from the state file."""
    if not os.path.exists(STATE_FILE):
        return {}
    # Fail closed: damaged/unreadable state must never trigger a mass replay.
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        state = json.load(f)
    processed = state["processed_ids"]
    if isinstance(processed, list) and all(isinstance(k, str) for k in processed):
        return {guid: "" for guid in processed}
    if isinstance(processed, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in processed.items()
    ):
        return processed
    raise ValueError("Invalid processed_ids in state file")

def save_state(processed_dict):
    """Save processed paper IDs/GUIDs with their modified times to the state file."""
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({"processed_ids": processed_dict}, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, STATE_FILE)
        logger.info(f"Saved {len(processed_dict)} processed IDs to {STATE_FILE}.")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class FeedRateLimited(RuntimeError):
    """Stop this scan when ePrint asks clients to back off."""


class FeedUnavailable(RuntimeError):
    """An unsuccessful/challenge response is not ePrint content."""


class EprintClient:
    """Lazily start one Chromium session per scan, sharing challenge cookies."""
    def __init__(self):
        self.session = None

    def fetch(self, url):
        if urlsplit(url).scheme != "https" or urlsplit(url).netloc != "eprint.iacr.org":
            raise FeedUnavailable("Unexpected ePrint URL")
        if self.session is None:
            session = StealthySession(
                headless=True, solve_cloudflare=True, timeout=60000,
                retries=1, max_pages=1, google_search=False,
            )
            session.__enter__()
            self.session = session
        response = self.session.fetch(url)
        if response.status in (429, 503):
            raise FeedRateLimited("ePrint rate limited the scan; defer remaining entries")
        if response.status != 200:
            raise FeedUnavailable(f"ePrint returned HTTP {response.status}")
        if urlsplit(response.url).netloc != "eprint.iacr.org":
            raise FeedUnavailable("Unexpected redirect away from ePrint")
        body = response.body
        # A challenge can return HTTP 200. Never parse it as a feed/article.
        lowered = body.lower()
        if any(marker in lowered for marker in (
            b"cf-chl-", b"challenges.cloudflare.com", b"<title>just a moment",
        )):
            raise FeedUnavailable("Cloudflare challenge remains unresolved")
        return body

    def close(self):
        if self.session is not None:
            try:
                self.session.__exit__(None, None, None)
            finally:
                self.session = None


_active_eprint_client = ContextVar("eprint_client", default=None)


@contextmanager
def eprint_client():
    client = EprintClient()
    token = _active_eprint_client.set(client)
    try:
        yield client
    finally:
        try:
            client.close()
        finally:
            _active_eprint_client.reset(token)


def fetch_eprint(url):
    client = _active_eprint_client.get()
    if client is not None:
        return client.fetch(url)
    with eprint_client() as client:
        return client.fetch(url)


class ArticleMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property") in (
            "article:modified_time", "article:published_time"
        ):
            self.metadata[attrs["property"]] = attrs.get("content")


def parse_timestamp(value):
    """Compare instants, not ISO strings with potentially different offsets."""
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


@contextmanager
def state_lock():
    with open(STATE_FILE + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another bot process is using this state file")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

def scrape_paper_metadata(url):
    """Scrape the ePrint page to extract modified_time and published_time.
    
    Returns a dict with:
        "modified_time": ISO 8601 string or None
        "published_time": ISO 8601 string or None
    """
    logger.info(f"Scraping metadata from {url}...")
    try:
        html = fetch_eprint(url).decode("utf-8")
        
        parser = ArticleMetadataParser()
        parser.feed(html)
        modified_time = parser.metadata.get("article:modified_time")
        published_time = parser.metadata.get("article:published_time")
        
        return {
            "modified_time": modified_time,
            "published_time": published_time
        }
    except FeedRateLimited:
        raise
    except Exception as e:
        logger.warning(f"Failed to scrape metadata from {url}: {e}")
        return {
            "modified_time": None,
            "published_time": None
        }

def fetch_rss_feed():
    """Fetch and parse the ePrint RSS feed."""
    logger.info(f"Fetching RSS feed from {EPRINT_RSS_URL}...")
    try:
        # Scrapling preserves the raw XML body, not Chromium's XML viewer DOM.
        root = ET.fromstring(fetch_eprint(EPRINT_RSS_URL))
        if root.tag != "rss":
            raise FeedUnavailable("Expected an RSS document from ePrint")
        items = root.findall('.//item')
        logger.info(f"Successfully fetched RSS feed containing {len(items)} items.")
        return items
    except Exception as e:
        logger.error(f"Error fetching or parsing RSS feed: {e}")
        raise FeedUnavailable("Unable to fetch a valid ePrint RSS feed") from e

def parse_item(item):
    """Extract clean metadata from an XML <item> element."""
    title_el = item.find('title')
    link_el = item.find('link')
    guid_el = item.find('guid')
    desc_el = item.find('description')
    pub_date_el = item.find('pubDate')
    
    title = title_el.text.strip() if title_el is not None and title_el.text else "Untitled"
    link = link_el.text.strip() if link_el is not None and link_el.text else ""
    guid = guid_el.text.strip() if guid_el is not None and guid_el.text else link
    abstract = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
    pub_date_str = pub_date_el.text.strip() if pub_date_el is not None and pub_date_el.text else ""
    
    # Extract creators (authors)
    authors = []
    for dc_creator in item.findall('dc:creator', NAMESPACES):
        if dc_creator.text:
            authors.append(dc_creator.text.strip())
            
    # Extract categories
    categories = []
    for cat in item.findall('category'):
        if cat.text:
            categories.append(cat.text.strip())
            
    return {
        "title": title,
        "link": link,
        "guid": guid,
        "abstract": abstract,
        "pub_date_str": pub_date_str,
        "authors": authors,
        "categories": categories
    }

def summarize_abstract(title, authors, abstract):
    """Send the abstract to the OpenAI-compatible LLM for summarization."""
    authors_str = ", ".join(authors) if authors else "Unknown"
    logger.info(f"Requesting summary for paper: {title}")
    
    system_prompt = (
        f"You are a professional cryptology researcher and an excellent scientific writer. "
        f"Summarize the following cryptology paper abstract in {SUMMARY_LANGUAGE}. "
        f"Provide a highly accurate, concise summary using exactly 3-4 bullet points. "
        f"Do not include the paper title or authors in your summary. "
        f"Focus on: 1) Background/Problem, 2) Key Contributions/Methodology, 3) Important Results/Impact."
    )
    
    user_prompt = f"Title: {title}\nAuthors: {authors_str}\nAbstract: {abstract}"
    
    # Prepare standard chat completions API endpoint
    endpoint = LLM_API_URL.rstrip('/')
    if not endpoint.endswith('/chat/completions'):
        endpoint += '/chat/completions'
        
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 1000
    }
    
    max_retries = 5
    base_delay = 2.0
    
    for attempt in range(max_retries):
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()
            summary = data['choices'][0]['message']['content'].strip()
            return summary
        except requests.exceptions.RequestException as e:
            # Differentiate between retriable and non-retriable HTTP errors
            status_code = e.response.status_code if e.response is not None else None
            
            # Non-retriable errors: client errors (4xx) except 429 Too Many Requests
            if status_code is not None and 400 <= status_code < 500 and status_code != 429:
                logger.error(f"LLM API non-retriable client error (status {status_code}): {e}")
                break
                
            if attempt < max_retries - 1:
                import random
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"LLM API request failed (status {status_code if status_code else 'network/timeout'}): {e}. Retrying in {delay:.2f} seconds (attempt {attempt + 1}/{max_retries})...")
                time.sleep(delay)
                continue
            else:
                logger.error(f"LLM API call failed after {max_retries} attempts: {e}")
                break
        except Exception as e:
            logger.error(f"Unexpected error during LLM API call: {e}")
            break
            
    # Fallback in case of failure or exhausted retries
    error_msg = "Retries exhausted or encountered non-retriable error"
    return f"*(LLM要約取得失敗: {error_msg})*\n\n" + (abstract[:300] + "..." if len(abstract) > 300 else abstract)

def format_iso_date(iso_str):
    if not iso_str:
        return "Unknown"
    try:
        clean_str = iso_str.replace('Z', '+00:00')
        dt = datetime.fromisoformat(clean_str)
        return dt.strftime("%Y-%m-%d %H:%M (UTC)")
    except Exception:
        return iso_str

def build_discord_embed(paper, summary):
    """Build a beautifully formatted Discord embed dictionary for a paper."""
    authors_str = ", ".join(paper["authors"]) if paper["authors"] else "Unknown"
    categories_str = ", ".join(paper["categories"]) if paper["categories"] else "General"
    
    # Format dates
    scraped_pub = paper.get("scraped_published_time")
    scraped_mod = paper.get("scraped_modified_time")
    
    if scraped_pub:
        formatted_pub_date = format_iso_date(scraped_pub)
    else:
        formatted_pub_date = paper["pub_date_str"]
        try:
            dt = email.utils.parsedate_to_datetime(paper["pub_date_str"])
            formatted_pub_date = dt.strftime("%Y-%m-%d %H:%M (UTC)")
        except Exception:
            pass
            
    is_update = paper.get("is_update", False)
    
    # Custom styling depending on whether it is new or updated
    if is_update:
        title = f"🔄 [Update] {paper['title']}"
        color = 1752220  # Emerald/Teal (hex: 1AB394)
    else:
        title = paper["title"]
        color = 1717853  # Sophisticated scientific dark slate color (hex: 1A365D)
        
    fields = [
        {
            "name": "👤 Authors",
            "value": authors_str,
            "inline": True
        },
        {
            "name": "🏷️ Category",
            "value": categories_str,
            "inline": True
        }
    ]
    
    if is_update and scraped_mod:
        formatted_mod_date = format_iso_date(scraped_mod)
        fields.append({
            "name": "📅 Last Updated (UTC)",
            "value": formatted_mod_date,
            "inline": True
        })
        fields.append({
            "name": "📅 Originally Published (UTC)",
            "value": formatted_pub_date,
            "inline": True
        })
    else:
        fields.append({
            "name": "📅 Published (UTC)",
            "value": formatted_pub_date,
            "inline": True
        })
        
    embed = {
        "title": title,
        "url": paper["link"],
        "description": summary,
        "color": color,
        "fields": fields,
        "footer": {
            "text": "IACR Cryptology ePrint Archive Feed Notification",
            "icon_url": "https://iacr.org/img/logo/iacrlogo_small.png"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    return embed

def build_discord_notification(embed):
    """Build a plain Discord message that is useful in notification previews."""
    embed_title = embed.get("title", "Untitled")
    if embed_title.startswith("🧪 [TEST] "):
        status = "🧪 テスト"
        paper_title = embed_title.removeprefix("🧪 [TEST] ")
    elif embed_title.startswith("🔄 [Update] "):
        status = "🔄 更新"
        paper_title = embed_title.removeprefix("🔄 [Update] ")
    else:
        status = "🆕 新着"
        paper_title = embed_title

    # Discord displays the webhook name as the notification title. Its limit
    # is 80 characters, so retain the status even when a paper title is long.
    notification_title = f"{status} | {paper_title}"[:80]

    summary = embed.get("description", "")
    link = embed.get("url", "")
    content = summary
    if link:
        content = f"{content}\n\n{link}" if content else link
    if len(content) > 2000:
        content = content[:1999] + "…"

    return notification_title, content

def get_discord_webhook_url(embed):
    """Select a Discord webhook based on the notification type."""
    title = embed.get("title", "")
    if title.startswith("🔄 [Update] "):
        return DISCORD_UPDATE_WEBHOOK_URL or DISCORD_WEBHOOK_URL
    return DISCORD_NEW_WEBHOOK_URL or DISCORD_WEBHOOK_URL

def post_batches_to_discord(embeds):
    """Post each paper as a plain message so push notifications are informative."""
    logger.info(f"Sending {len(embeds)} paper notification(s) to Discord...")
    all_succeeded = True

    for embed in embeds:
        notification_title, content = build_discord_notification(embed)
        webhook_url = get_discord_webhook_url(embed)
        if not webhook_url:
            logger.error(
                f"No Discord webhook configured for notification: {notification_title}"
            )
            all_succeeded = False
            continue
        payload = {
            "username": notification_title,
            "content": content,
        }
        try:
            response = requests.post(webhook_url, json=payload, timeout=20)
            response.raise_for_status()
            logger.info(f"Successfully posted to Discord: {notification_title}")
        except Exception as e:
            logger.error("Failed to post Discord notification (%s); retry next run", type(e).__name__)
            all_succeeded = False

    return all_succeeded

def send_test_notification():
    """Send one synthetic notification without touching RSS, LLM, or state."""
    validate_config({
        "DISCORD_NEW_WEBHOOK_URL or DISCORD_WEBHOOK_URL": (
            DISCORD_NEW_WEBHOOK_URL or DISCORD_WEBHOOK_URL
        )
    })
    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    test_embed = {
        "title": "🧪 [TEST] ePrint Bot dummy notification",
        "description": (
            "これは本番環境から送信したダミー通知です。"
            "RSS取得・LLM要約・state更新は実行していません。"
            f"\n\n送信時刻: {sent_at}"
        ),
    }
    if not post_batches_to_discord([test_embed]):
        raise RuntimeError("Test notification delivery failed")
    logger.info("Test notification delivered successfully.")

def run_bot_logic(dry_run=False):
    with state_lock(), eprint_client():
        _run_bot_logic(dry_run=dry_run)


def _run_bot_logic(dry_run=False):
    is_first_run = not os.path.exists(STATE_FILE)
    processed_ids = load_state()
    papers = []
    seen = set()
    for item in fetch_rss_feed():
        paper = parse_item(item)
        guid = paper["guid"]
        if not guid or not paper["link"] or guid in seen:
            continue
        seen.add(guid)
        try:
            metadata = scrape_paper_metadata(paper["link"])
        except FeedRateLimited as exc:
            logger.warning("%s", exc)
            break
        time.sleep(5.0)  # Bound request rate now that the entire feed is scanned.
        published = parse_timestamp(metadata.get("published_time"))
        modified = parse_timestamp(metadata.get("modified_time"))
        # RSS pubDate can describe an update. Never use it as original publication.
        if published is None or modified is None or modified < published:
            logger.warning("Missing/invalid article dates for %s; retry next run", guid)
            continue
        paper["scraped_published_time"] = published.isoformat()
        paper["scraped_modified_time"] = modified.isoformat()
        paper["is_update"] = modified > published
        papers.append(paper)

    # Keep first-start notifications bounded to the newest publication day.
    # Persist the baseline before delivery so failures remain pending, not reinitialized.
    target_date = None
    if is_first_run and papers:
        today = datetime.now(timezone.utc).date().isoformat()
        dates = [p["scraped_published_time"][:10] for p in papers]
        target_date = today if today in dates else max(dates)
        for paper in papers:
            if paper["scraped_published_time"][:10] != target_date:
                processed_ids[paper["guid"]] = paper["scraped_modified_time"]
        if not dry_run:
            save_state(processed_ids)

    sent = 0
    for paper in papers:
        guid = paper["guid"]
        modified = paper["scraped_modified_time"]
        if guid in processed_ids:
            stored = processed_ids[guid]
            if stored == "":
                # Legacy list migration: baseline without resending.
                processed_ids[guid] = modified
                if not dry_run:
                    save_state(processed_ids)
                continue
            stored_dt = parse_timestamp(stored)
            if stored_dt is None:
                raise ValueError("Invalid stored modification time for " + guid)
            if parse_timestamp(modified) <= stored_dt:
                continue  # Do not stop: later RSS entries may still be pending.
            paper["is_update"] = True

        logger.info("Found %s paper: %s (%s)",
                    "updated" if paper["is_update"] else "new", paper["title"], guid)
        if dry_run:
            continue
        summary = summarize_abstract(paper["title"], paper["authors"], paper["abstract"])
        embed = build_discord_embed(paper, summary)
        # Checkpoint each individual success. A failed paper stays pending while
        # other deliveries can proceed; it will be retried even behind known items.
        if post_batches_to_discord([embed]):
            processed_ids[guid] = modified
            save_state(processed_ids)
            sent += 1
        else:
            logger.warning("Notification pending for %s", guid)
    logger.info("Feed scan complete: %d paper(s) checked, %d delivered", len(papers), sent)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="IACR ePrint notification bot")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="scan and log classifications without LLM, Discord, or state changes",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="send one dummy Discord notification and exit",
    )
    return parser.parse_args([] if argv is None else argv)

def main(argv=None):
    args = parse_args(argv)

    try:
        if args.dry_run:
            run_bot_logic(dry_run=True)
            return
        if args.test_notification:
            send_test_notification()
            return

        validate_config()
    except (ValueError, RuntimeError) as e:
        logger.error(str(e))
        raise SystemExit(1) from e
    
    daemon_interval_str = os.getenv("DAEMON_INTERVAL")
    if daemon_interval_str:
        try:
            interval = int(daemon_interval_str)
            if interval <= 0:
                raise ValueError("DAEMON_INTERVAL must be a positive integer.")
            
            logger.info(f"Running in daemon mode. Check interval: {interval} seconds.")
            while True:
                try:
                    run_bot_logic()
                except Exception as e:
                    logger.error(f"Error during bot execution: {e}", exc_info=True)
                logger.info(f"Sleeping for {interval} seconds...")
                time.sleep(interval)
        except ValueError as e:
            logger.error(f"Invalid DAEMON_INTERVAL: {e}")
            sys.exit(1)
        except KeyboardInterrupt:
            logger.info("Daemon execution stopped by user.")
    else:
        run_bot_logic()

if __name__ == "__main__":
    main(sys.argv[1:])
