#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "requests>=2.31.0",
#     "python-dotenv>=1.0.0",
# ]
# ///
import unittest
from unittest.mock import patch, MagicMock
import os
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import email.utils

# Import functions from eprint_bot
import eprint_bot

class TestEprintBot(unittest.TestCase):

    def setUp(self):
        # Sample RSS Item XML string
        self.sample_item_xml = """
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Test Cryptology Paper Title</title>
          <link>https://eprint.iacr.org/2026/9999</link>
          <description>This is the abstract of the test cryptology paper. It explains the core concepts and results.</description>
          <guid isPermaLink="true">https://eprint.iacr.org/2026/9999</guid>
          <category>Cryptographic protocols</category>
          <category>Foundations</category>
          <pubDate>Mon, 25 May 2026 09:04:12 +0000</pubDate>
          <dc:creator>Alice Researcher</dc:creator>
          <dc:creator>Bob Cryptographer</dc:creator>
        </item>
        """
        # Ensure state file doesn't exist before each test
        if os.path.exists(eprint_bot.STATE_FILE):
            os.remove(eprint_bot.STATE_FILE)

    def tearDown(self):
        # Clean up state file
        if os.path.exists(eprint_bot.STATE_FILE):
            os.remove(eprint_bot.STATE_FILE)

    def test_parse_item(self):
        root = ET.fromstring(self.sample_item_xml)
        paper = eprint_bot.parse_item(root)
        
        self.assertEqual(paper["title"], "Test Cryptology Paper Title")
        self.assertEqual(paper["link"], "https://eprint.iacr.org/2026/9999")
        self.assertEqual(paper["guid"], "https://eprint.iacr.org/2026/9999")
        self.assertEqual(paper["abstract"], "This is the abstract of the test cryptology paper. It explains the core concepts and results.")
        self.assertEqual(paper["pub_date_str"], "Mon, 25 May 2026 09:04:12 +0000")
        self.assertEqual(paper["authors"], ["Alice Researcher", "Bob Cryptographer"])
        self.assertEqual(paper["categories"], ["Cryptographic protocols", "Foundations"])

    def test_load_save_state(self):
        # Test empty state
        state = eprint_bot.load_state()
        self.assertEqual(state, set())
        
        # Save state
        processed = {"id1", "id2", "id3"}
        eprint_bot.save_state(processed)
        
        # Load and verify
        loaded = eprint_bot.load_state()
        self.assertEqual(loaded, processed)

    @patch('requests.post')
    def test_summarize_abstract_success(self, mock_post):
        # Mock successful LLM API response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "・テスト要約バレット1\n・テスト要約バレット2"
                    }
                }
            ]
        }
        mock_post.return_value = mock_response
        
        # Temp mock configurations
        eprint_bot.LLM_API_URL = "https://api.example.com/v1"
        eprint_bot.LLM_API_KEY = "test_key"
        eprint_bot.LLM_MODEL = "test-model"
        
        summary = eprint_bot.summarize_abstract(
            "Test Paper", 
            ["Author A"], 
            "This is a long abstract..."
        )
        
        self.assertEqual(summary, "・テスト要約バレット1\n・テスト要約バレット2")
        mock_post.assert_called_once()
        # Verify it automatically appends chat/completions to URL
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.example.com/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test_key")

    @patch('time.sleep') # Mock sleep to speed up the test
    @patch('requests.post')
    def test_summarize_abstract_retry_then_success(self, mock_post, mock_sleep):
        # Create a mock 429 response
        mock_429_response = MagicMock()
        mock_429_response.status_code = 429
        
        from requests.exceptions import HTTPError
        err_429 = HTTPError("Too Many Requests", response=mock_429_response)
        
        # Create a mock success response
        mock_success_response = MagicMock()
        mock_success_response.status_code = 200
        mock_success_response.json.return_value = {
            "choices": [{"message": {"content": "Retry success"}}]
        }
        
        # Side effect: first call raises 429 error, second call succeeds
        mock_post.side_effect = [err_429, mock_success_response]
        
        eprint_bot.LLM_API_URL = "https://api.example.com/v1"
        eprint_bot.LLM_API_KEY = "test_key"
        eprint_bot.LLM_MODEL = "test-model"
        
        summary = eprint_bot.summarize_abstract("Test", ["Author"], "Abstract")
        
        self.assertEqual(summary, "Retry success")
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once() # Verify time.sleep was called exactly once

    @patch('time.sleep')
    @patch('requests.post')
    def test_summarize_abstract_non_retriable_client_error(self, mock_post, mock_sleep):
        # Create a mock 401 response
        mock_401_response = MagicMock()
        mock_401_response.status_code = 401
        
        from requests.exceptions import HTTPError
        err_401 = HTTPError("Unauthorized", response=mock_401_response)
        
        mock_post.side_effect = err_401
        
        eprint_bot.LLM_API_URL = "https://api.example.com/v1"
        eprint_bot.LLM_API_KEY = "test_key"
        eprint_bot.LLM_MODEL = "test-model"
        
        summary = eprint_bot.summarize_abstract("Test", ["Author"], "Abstract")
        
        # Should fail immediately without retrying
        self.assertIn("LLM要約取得失敗", summary)
        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @patch('requests.post')
    def test_post_batches_to_discord(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_post.return_value = mock_response
        
        eprint_bot.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"
        
        embeds = [
            {"title": f"Paper {i}", "description": f"Summary {i}"}
            for i in range(12) # 12 embeds exceeds the 10 limit, should create 2 batches
        ]
        
        eprint_bot.post_batches_to_discord(embeds)
        
        # Verify requests.post was called twice
        self.assertEqual(mock_post.call_count, 2)
        
        # Verify first call had 10 embeds, second had 2 embeds
        first_call_json = mock_post.call_args_list[0][1]["json"]
        second_call_json = mock_post.call_args_list[1][1]["json"]
        
        self.assertEqual(len(first_call_json["embeds"]), 10)
        self.assertEqual(len(second_call_json["embeds"]), 2)

    @patch('eprint_bot.fetch_rss_feed')
    @patch('eprint_bot.summarize_abstract')
    @patch('eprint_bot.post_batches_to_discord')
    def test_main_first_run_date_filtering(self, mock_post_discord, mock_summarize, mock_fetch):
        # Mock RSS Feed Items
        now_utc = datetime.now(timezone.utc)
        current_date_str = now_utc.strftime('%Y-%m-%d')
        
        # Format today's date into the RFC 822 format used by the RSS feed
        # We'll use email.utils.format_datetime
        today_rss_date = email.utils.format_datetime(now_utc)
        older_rss_date = "Mon, 02 Oct 2023 22:45:48 +0000"
        
        xml_today = f"""
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Today's Paper</title>
          <link>https://eprint.iacr.org/2026/1111</link>
          <guid>https://eprint.iacr.org/2026/1111</guid>
          <pubDate>{today_rss_date}</pubDate>
        </item>
        """
        xml_older = f"""
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Older Paper</title>
          <link>https://eprint.iacr.org/2023/2222</link>
          <guid>https://eprint.iacr.org/2023/2222</guid>
          <pubDate>{older_rss_date}</pubDate>
        </item>
        """
        
        mock_fetch.return_value = [
            ET.fromstring(xml_today),
            ET.fromstring(xml_older)
        ]
        
        mock_summarize.return_value = "Summary text"
        
        # Setup configs
        eprint_bot.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"
        eprint_bot.LLM_API_URL = "https://api.example.com/v1"
        eprint_bot.LLM_API_KEY = "test"
        eprint_bot.LLM_MODEL = "test"
        
        # Execute main (First Run: no state file)
        eprint_bot.main()
        
        # Verify we only summarized today's paper
        mock_summarize.assert_called_once_with("Today's Paper", [], "")
        
        # Verify we posted one embed
        mock_post_discord.assert_called_once()
        posted_embeds = mock_post_discord.call_args[0][0]
        self.assertEqual(len(posted_embeds), 1)
        self.assertEqual(posted_embeds[0]["title"], "Today's Paper")
        
        # Verify both are now in the processed state so they won't run again
        state = eprint_bot.load_state()
        self.assertIn("https://eprint.iacr.org/2026/1111", state)
        self.assertIn("https://eprint.iacr.org/2023/2222", state)

    @patch('eprint_bot.fetch_rss_feed')
    @patch('eprint_bot.summarize_abstract')
    @patch('eprint_bot.post_batches_to_discord')
    def test_main_first_run_fallback_filtering(self, mock_post_discord, mock_summarize, mock_fetch):
        # Mock RSS Feed Items containing NO papers today.
        # The newest paper in the feed is from "yesterday".
        yesterday_rss_date = "Mon, 25 May 2026 09:04:12 +0000"
        older_rss_date = "Mon, 02 Oct 2023 22:45:48 +0000"
        
        xml_yesterday1 = f"""
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Yesterday's Paper 1</title>
          <link>https://eprint.iacr.org/2026/1111</link>
          <guid>https://eprint.iacr.org/2026/1111</guid>
          <pubDate>{yesterday_rss_date}</pubDate>
        </item>
        """
        xml_yesterday2 = f"""
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Yesterday's Paper 2</title>
          <link>https://eprint.iacr.org/2026/1112</link>
          <guid>https://eprint.iacr.org/2026/1112</guid>
          <pubDate>{yesterday_rss_date}</pubDate>
        </item>
        """
        xml_older = f"""
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Older Paper</title>
          <link>https://eprint.iacr.org/2023/2222</link>
          <guid>https://eprint.iacr.org/2023/2222</guid>
          <pubDate>{older_rss_date}</pubDate>
        </item>
        """
        
        mock_fetch.return_value = [
            ET.fromstring(xml_older),
            ET.fromstring(xml_yesterday1),
            ET.fromstring(xml_yesterday2)
        ]
        
        mock_summarize.return_value = "Summary text"
        
        # Setup configs
        eprint_bot.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"
        eprint_bot.LLM_API_URL = "https://api.example.com/v1"
        eprint_bot.LLM_API_KEY = "test"
        eprint_bot.LLM_MODEL = "test"
        
        # Execute main (First Run: no state file)
        eprint_bot.main()
        
        # Verify we summarized both of yesterday's papers
        self.assertEqual(mock_summarize.call_count, 2)
        mock_summarize.assert_any_call("Yesterday's Paper 1", [], "")
        mock_summarize.assert_any_call("Yesterday's Paper 2", [], "")
        
        # Verify we posted both embeds consolidated in one Discord post
        mock_post_discord.assert_called_once()
        posted_embeds = mock_post_discord.call_args[0][0]
        self.assertEqual(len(posted_embeds), 2)
        self.assertEqual(posted_embeds[0]["title"], "Yesterday's Paper 1")
        self.assertEqual(posted_embeds[1]["title"], "Yesterday's Paper 2")
        
        # Verify all are now in the processed state
        state = eprint_bot.load_state()
        self.assertIn("https://eprint.iacr.org/2026/1111", state)
        self.assertIn("https://eprint.iacr.org/2026/1112", state)
        self.assertIn("https://eprint.iacr.org/2023/2222", state)

if __name__ == '__main__':
    unittest.main()
