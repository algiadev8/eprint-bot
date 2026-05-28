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
                return set(state.get("processed_ids", []))
        except Exception as e:
            logger.warning(f"Failed to load state file ({e}). Starting fresh.")
    return set()

def save_state(processed_ids):
    """Save processed paper IDs/GUIDs to the state file."""
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({"processed_ids": sorted(list(processed_ids))}, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(processed_ids)} processed IDs to {STATE_FILE}.")
    except Exception as e:
        logger.error(f"Failed to save state file: {e}")

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

def build_discord_embed(paper, summary):
    """Build a beautifully formatted Discord embed dictionary for a paper."""
    authors_str = ", ".join(paper["authors"]) if paper["authors"] else "Unknown"
    categories_str = ", ".join(paper["categories"]) if paper["categories"] else "General"
    
    # Parse and format publish date
    formatted_date = paper["pub_date_str"]
    try:
        dt = email.utils.parsedate_to_datetime(paper["pub_date_str"])
        formatted_date = dt.strftime("%Y-%m-%d %H:%M (UTC)")
    except Exception:
        pass
        
    # Sophisticated scientific dark slate color (decimal: 1717853 = hex: 1A365D)
    embed = {
        "title": paper["title"],
        "url": paper["link"],
        "description": summary,
        "color": 1717853,
        "fields": [
            {
                "name": "👤 Authors",
                "value": authors_str,
                "inline": True
            },
            {
                "name": "🏷️ Category",
                "value": categories_str,
                "inline": True
            },
            {
                "name": "📅 Published (UTC)",
                "value": formatted_date,
                "inline": True
            }
        ],
        "footer": {
            "text": "IACR Cryptology ePrint Archive Feed Notification",
            "icon_url": "https://iacr.org/img/logo/iacrlogo_small.png"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    return embed

def post_batches_to_discord(embeds):
    """Post multiple embeds to the Discord webhook, batching to respect API limits."""
    # Discord limits:
    # - Max 10 embeds per message payload.
    # - Max 6000 characters total across the entire message object.
    
    batches = []
    current_batch = []
    current_char_count = 0
    
    for embed in embeds:
        # Calculate approximate character size of the embed
        embed_size = len(embed.get('title', '')) + len(embed.get('description', ''))
        for field in embed.get('fields', []):
            embed_size += len(field.get('name', '')) + len(field.get('value', ''))
        if embed.get('footer', {}).get('text'):
            embed_size += len(embed['footer']['text'])
            
        # If adding this embed exceeds limits, start a new batch
        if len(current_batch) >= 10 or (current_char_count + embed_size) > 5500:
            batches.append(current_batch)
            current_batch = [embed]
            current_char_count = embed_size
        else:
            current_batch.append(embed)
            current_char_count += embed_size
            
    if current_batch:
        batches.append(current_batch)
        
    logger.info(f"Consolidating embeds. Sending {len(embeds)} embeds in {len(batches)} post(s)...")
    
    for idx, batch in enumerate(batches, 1):
        payload = {
            "content": f"📢 **IACR ePrint New Submissions Notification** (Part {idx}/{len(batches)})" if len(batches) > 1 else "📢 **IACR ePrint New Submissions Notification**",
            "embeds": batch
        }
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
            response.raise_for_status()
            logger.info(f"Batch {idx} successfully posted to Discord.")
        except Exception as e:
            logger.error(f"Failed to post batch {idx} to Discord Webhook: {e}")

def run_bot_logic():
    processed_ids = load_state()
    is_first_run = len(processed_ids) == 0
    
    raw_items = fetch_rss_feed()
    all_papers = [parse_item(item) for item in raw_items]
    
    new_papers = []
    
    if is_first_run:
        # First Run: Get current date in UTC
        now_utc = datetime.now(timezone.utc)
        current_date_str = now_utc.strftime('%Y-%m-%d')
        
        # Pre-parse publication dates for all papers to assist matching
        parsed_papers = []
        for paper in all_papers:
            paper_date_str = ""
            try:
                pub_dt = email.utils.parsedate_to_datetime(paper["pub_date_str"])
                pub_dt_utc = pub_dt.astimezone(timezone.utc)
                paper_date_str = pub_dt_utc.strftime('%Y-%m-%d')
            except Exception:
                pass
            parsed_papers.append((paper, paper_date_str))
            
        # Determine the target date: try today first, otherwise fall back to the newest date in the feed
        has_today_papers = any(date_str == current_date_str for _, date_str in parsed_papers if date_str)
        
        if has_today_papers:
            target_date_str = current_date_str
            logger.info(f"First run: Found papers published today: {target_date_str} (UTC).")
        else:
            # Find the date of the newest paper in the feed
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
            if date_str == target_date_str:
                new_papers.append(paper)
            else:
                # Mark other older or different date papers as processed
                processed_ids.add(paper["guid"])
                
        logger.info(f"First run: Processing {len(new_papers)} paper(s) published on {target_date_str}. Marking {len(all_papers) - len(new_papers)} other papers as processed.")
    else:
        # Standard Run: Check if paper guid is not in processed list
        for paper in all_papers:
            if paper["guid"] not in processed_ids:
                new_papers.append(paper)
                
    if not new_papers:
        logger.info("No new papers found to process.")
        # If it was the first run, we still want to save the state of historical papers
        if is_first_run:
            save_state(processed_ids)
        return
        
    logger.info(f"Found {len(new_papers)} new paper(s) to process.")
    
    embeds_to_post = []
    
    for paper in new_papers:
        summary = summarize_abstract(paper["title"], paper["authors"], paper["abstract"])
        embed = build_discord_embed(paper, summary)
        embeds_to_post.append(embed)
        
        # Add to processed IDs
        processed_ids.add(paper["guid"])
        
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
