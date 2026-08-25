#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "requests>=2.31.0",
#     "python-dotenv>=1.0.0",
# ]
# ///
import os
import sys
import time
import json
import logging
import email.utils
import re
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import requests
from dotenv import load_dotenv

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

def validate_config():
    """Verify that all required environment variables are set."""
    missing = []
    if not DISCORD_WEBHOOK_URL:
        missing.append("DISCORD_WEBHOOK_URL")
    if not LLM_API_URL:
        missing.append("LLM_API_URL")
    if not LLM_API_KEY:
        missing.append("LLM_API_KEY")
    if not LLM_MODEL:
        missing.append("LLM_MODEL")
        
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        logger.error("Please create a .env file based on .env.example with the correct values.")
        sys.exit(1)

def load_state():
    """Load previously processed paper IDs/GUIDs from the state file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
                processed = state.get("processed_ids", [])
                
                # Support backwards compatibility and migration
                if isinstance(processed, list):
                    logger.info("Migrating state file from old list format to dictionary format...")
                    return {guid: "" for guid in processed}
                elif isinstance(processed, dict):
                    return processed
                else:
                    logger.warning(f"Unexpected processed_ids type: {type(processed)}. Starting fresh.")
        except Exception as e:
            logger.warning(f"Failed to load state file ({e}). Starting fresh.")
    return {}

def save_state(processed_dict):
    """Save processed paper IDs/GUIDs with their modified times to the state file."""
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({"processed_ids": processed_dict}, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(processed_dict)} processed IDs to {STATE_FILE}.")
    except Exception as e:
        logger.error(f"Failed to save state file: {e}")

def scrape_paper_metadata(url):
    """Scrape the ePrint page to extract modified_time and published_time.
    
    Returns a dict with:
        "modified_time": ISO 8601 string or None
        "published_time": ISO 8601 string or None
    """
    logger.info(f"Scraping metadata from {url}...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        html = response.text
        
        # Regex search for article:modified_time and article:published_time
        modified_match = re.search(r'<meta\s+property=["\']article:modified_time["\']\s+content=["\']([^"\']+)["\']', html)
        published_match = re.search(r'<meta\s+property=["\']article:published_time["\']\s+content=["\']([^"\']+)["\']', html)
        
        modified_time = modified_match.group(1) if modified_match else None
        published_time = published_match.group(1) if published_match else None
        
        return {
            "modified_time": modified_time,
            "published_time": published_time
        }
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
        response = requests.get(EPRINT_RSS_URL, timeout=30)
        response.raise_for_status()
        # Parse XML content natively
        root = ET.fromstring(response.content)
        items = root.findall('.//item')
        logger.info(f"Successfully fetched RSS feed containing {len(items)} items.")
        return items
    except Exception as e:
        logger.error(f"Error fetching or parsing RSS feed: {e}")
        sys.exit(1)

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
    if embed_title.startswith("🔄 [Update] "):
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

def post_batches_to_discord(embeds):
    """Post each paper as a plain message so push notifications are informative."""
    logger.info(f"Sending {len(embeds)} paper notification(s) to Discord...")

    for embed in embeds:
        notification_title, content = build_discord_notification(embed)
        payload = {
            "username": notification_title,
            "content": content,
        }
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
            response.raise_for_status()
            logger.info(f"Successfully posted to Discord: {notification_title}")
        except Exception as e:
            logger.error(f"Failed to post Discord notification: {e}")

def run_bot_logic():
    processed_ids = load_state()
    is_first_run = len(processed_ids) == 0
    
    raw_items = fetch_rss_feed()
    all_papers = [parse_item(item) for item in raw_items]
    
    new_papers = []
    has_changes = False
    
    if is_first_run:
        logger.info(f"First run: Initializing processed_ids with exact metadata for {len(all_papers)} papers in the feed...")
        parsed_papers = []
        for idx, paper in enumerate(all_papers):
            logger.info(f"[{idx+1}/{len(all_papers)}] Initializing paper metadata...")
            guid = paper["guid"]
            link = paper["link"]
            
            scraped_mod = None
            scraped_pub = None
            if link:
                metadata = scrape_paper_metadata(link)
                scraped_mod = metadata["modified_time"]
                scraped_pub = metadata["published_time"]
                time.sleep(1.0) # Polite sequential sleep
            
            if not scraped_pub:
                try:
                    pub_dt = email.utils.parsedate_to_datetime(paper["pub_date_str"])
                    scraped_pub = pub_dt.astimezone(timezone.utc).isoformat()
                except Exception:
                    pass
            if not scraped_mod:
                scraped_mod = scraped_pub
                
            paper["scraped_published_time"] = scraped_pub
            paper["scraped_modified_time"] = scraped_mod
            
            # Format date to match format "YYYY-MM-DD"
            paper_date_str = ""
            if scraped_pub:
                try:
                    paper_date_str = scraped_pub.split('T')[0]
                except Exception:
                    pass
            
            parsed_papers.append((paper, paper_date_str))
            
        # Determine the target date: try today first, otherwise fall back to the newest date in the feed
        now_utc = datetime.now(timezone.utc)
        current_date_str = now_utc.strftime('%Y-%m-%d')
        
        has_today_papers = any(date_str == current_date_str for _, date_str in parsed_papers if date_str)
        
        if has_today_papers:
            target_date_str = current_date_str
            logger.info(f"First run: Found papers published today: {target_date_str} (UTC).")
        else:
            valid_dates = [date_str for _, date_str in parsed_papers if date_str]
            newest_paper_date = max(valid_dates) if valid_dates else None
            
            if newest_paper_date:
                target_date_str = newest_paper_date
                logger.info(f"First run: No papers published today ({current_date_str}). Falling back to the most recent date in the feed: {target_date_str} (UTC).")
            else:
                target_date_str = current_date_str
                logger.warning("First run: Could not determine any publication dates in the feed. Defaulting to today's date.")
                
        # Filter papers based on the determined target date
        for paper, date_str in parsed_papers:
            guid = paper["guid"]
            scraped_mod = paper.get("scraped_modified_time")
            if date_str == target_date_str:
                paper["is_update"] = False
                new_papers.append(paper)
            
            # Mark all papers (including target and skipped) as processed with their actual scraped times
            processed_ids[guid] = scraped_mod if scraped_mod else ""
            has_changes = True
            
        logger.info(f"First run: Processing {len(new_papers)} paper(s) published on {target_date_str}. Marking {len(all_papers) - len(new_papers)} other papers as processed.")
    else:
        # Standard Run: Check if paper guid is not in processed list, handles updates & migration
        for idx, paper in enumerate(all_papers):
            guid = paper["guid"]
            link = paper["link"]
            if not link:
                continue
                
            in_state = guid in processed_ids
            stored_mod = processed_ids.get(guid, None)
            is_migrated = in_state and stored_mod == ""
            
            # Scrape page
            metadata = scrape_paper_metadata(link)
            scraped_mod = metadata["modified_time"]
            scraped_pub = metadata["published_time"]
            time.sleep(1.0) # Polite sequential sleep
            
            if not scraped_pub:
                try:
                    pub_dt = email.utils.parsedate_to_datetime(paper["pub_date_str"])
                    scraped_pub = pub_dt.astimezone(timezone.utc).isoformat()
                except Exception:
                    pass
            if not scraped_mod:
                scraped_mod = scraped_pub
                
            if not scraped_mod:
                logger.warning(f"Could not retrieve modification time for {guid}. Skipping in this run.")
                continue
                
            # If completely new to state
            if not in_state:
                paper["scraped_published_time"] = scraped_pub
                paper["scraped_modified_time"] = scraped_mod
                paper["is_update"] = False
                new_papers.append(paper)
                
                processed_ids[guid] = scraped_mod
                has_changes = True
                logger.info(f"Found new paper: {paper['title']}")
                
            # If in state but migrated (no date stored)
            elif is_migrated:
                logger.info(f"Populated migration date for already processed paper: {guid}")
                processed_ids[guid] = scraped_mod
                has_changes = True
                
            # If in state with stored date
            else:
                if scraped_mod > stored_mod:
                    paper["scraped_published_time"] = scraped_pub
                    paper["scraped_modified_time"] = scraped_mod
                    paper["is_update"] = True
                    new_papers.append(paper)
                    
                    processed_ids[guid] = scraped_mod
                    has_changes = True
                    logger.info(f"Found updated paper: {paper['title']} (New: {scraped_mod}, Old: {stored_mod})")
                else:
                    # Optimization Stop!
                    logger.info(f"Reached already processed paper ({guid}) with no new updates. Stopping feed scan.")
                    break
                    
    if not new_papers:
        logger.info("No new papers found to process.")
        if has_changes:
            save_state(processed_ids)
        return
        
    logger.info(f"Found {len(new_papers)} new paper(s) to process.")
    
    embeds_to_post = []
    
    for paper in new_papers:
        summary = summarize_abstract(paper["title"], paper["authors"], paper["abstract"])
        embed = build_discord_embed(paper, summary)
        embeds_to_post.append(embed)
        
    # Post consolidated notification to Discord
    post_batches_to_discord(embeds_to_post)
    
    # Save final processed state
    save_state(processed_ids)
    logger.info("Done!")

def main():
    validate_config()
    
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
    main()
