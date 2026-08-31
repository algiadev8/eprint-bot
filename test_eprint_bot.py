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
        self.assertEqual(state, {})
        
        # Save state (dictionary)
        processed = {"id1": "2026-05-28T15:26:17+00:00", "id2": "2026-05-28T14:24:31+00:00"}
        eprint_bot.save_state(processed)
        
        # Load and verify
        loaded = eprint_bot.load_state()
        self.assertEqual(loaded, processed)
        
        # Test migration from old format (list of guids)
        with open(eprint_bot.STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({"processed_ids": ["id1", "id2"]}, f)
            
        migrated = eprint_bot.load_state()
        self.assertEqual(migrated, {"id1": "", "id2": ""})

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
        eprint_bot.DISCORD_NEW_WEBHOOK_URL = None
        eprint_bot.DISCORD_UPDATE_WEBHOOK_URL = None
        
        embeds = [
            {"title": f"Paper {i}", "description": f"Summary {i}"}
            for i in range(12) # 12 embeds exceeds the 10 limit, should create 2 batches
        ]
        
        eprint_bot.post_batches_to_discord(embeds)
        
        # Each paper gets its own message so its title and summary appear in
        # the push notification.
        self.assertEqual(mock_post.call_count, 12)

        first_call_json = mock_post.call_args_list[0][1]["json"]
        self.assertEqual(first_call_json["username"], "🆕 新着 | Paper 0")
        self.assertEqual(first_call_json["content"], "Summary 0")
        self.assertNotIn("embeds", first_call_json)

    @patch('requests.post')
    def test_post_batches_routes_new_and_updates_to_separate_webhooks(self, mock_post):
        mock_post.return_value = MagicMock(status_code=204)
        eprint_bot.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/fallback"
        eprint_bot.DISCORD_NEW_WEBHOOK_URL = "https://discord.com/api/webhooks/new"
        eprint_bot.DISCORD_UPDATE_WEBHOOK_URL = "https://discord.com/api/webhooks/update"

        eprint_bot.post_batches_to_discord([
            {"title": "A New Paper", "description": "New summary"},
            {"title": "🔄 [Update] A Revised Paper", "description": "Update summary"},
        ])

        self.assertEqual(mock_post.call_args_list[0].args[0], eprint_bot.DISCORD_NEW_WEBHOOK_URL)
        self.assertEqual(mock_post.call_args_list[1].args[0], eprint_bot.DISCORD_UPDATE_WEBHOOK_URL)

    def test_build_discord_notification_distinguishes_updates(self):
        embed = {
            "title": "🔄 [Update] A Revised Paper",
            "description": "The revised result.",
            "url": "https://eprint.iacr.org/2026/1",
        }

        username, content = eprint_bot.build_discord_notification(embed)

        self.assertEqual(username, "🔄 更新 | A Revised Paper")
        self.assertEqual(
            content,
            "The revised result.\n\nhttps://eprint.iacr.org/2026/1",
        )
        self.assertNotIn("[Update]", content)

    def test_build_discord_notification_labels_test_messages(self):
        username, content = eprint_bot.build_discord_notification({
            "title": "🧪 [TEST] ePrint Bot dummy notification",
            "description": "Dummy content",
        })

        self.assertEqual(username, "🧪 テスト | ePrint Bot dummy notification")
        self.assertEqual(content, "Dummy content")

    @patch('eprint_bot.post_batches_to_discord')
    def test_test_notification_only_requires_discord_and_sends_dummy(self, mock_post_discord):
        mock_post_discord.return_value = True
        eprint_bot.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"
        eprint_bot.LLM_API_URL = None
        eprint_bot.LLM_API_KEY = None
        eprint_bot.LLM_MODEL = None

        eprint_bot.main(["--test-notification"])

        mock_post_discord.assert_called_once()
        test_embed = mock_post_discord.call_args.args[0][0]
        self.assertIn("[TEST]", test_embed["title"])
        self.assertIn("ダミー通知", test_embed["description"])

    @patch('eprint_bot.run_bot_logic')
    @patch('eprint_bot.send_test_notification')
    def test_test_notification_skips_normal_bot_logic(self, mock_send_test, mock_run_bot):
        eprint_bot.main(["--test-notification"])

        mock_send_test.assert_called_once_with()
        mock_run_bot.assert_not_called()

    @patch('eprint_bot.post_batches_to_discord', return_value=False)
    def test_test_notification_exits_nonzero_on_delivery_failure(self, _mock_post_discord):
        eprint_bot.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"

        with self.assertRaises(SystemExit) as raised:
            eprint_bot.main(["--test-notification"])

        self.assertEqual(raised.exception.code, 1)

    @patch('eprint_bot.scrape_paper_metadata')
    @patch('eprint_bot.fetch_rss_feed')
    @patch('eprint_bot.summarize_abstract')
    @patch('eprint_bot.post_batches_to_discord')
    def test_main_first_run_date_filtering(self, mock_post_discord, mock_summarize, mock_fetch, mock_scrape):
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
        
        mock_scrape.side_effect = lambda url: {
            "https://eprint.iacr.org/2026/1111": {"modified_time": "2026-05-28T15:26:17+00:00", "published_time": "2026-05-28T15:26:17+00:00"},
            "https://eprint.iacr.org/2023/2222": {"modified_time": "2023-10-02T22:45:48+00:00", "published_time": "2023-10-02T22:45:48+00:00"}
        }[url]
        
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

    @patch('eprint_bot.scrape_paper_metadata')
    @patch('eprint_bot.fetch_rss_feed')
    @patch('eprint_bot.summarize_abstract')
    @patch('eprint_bot.post_batches_to_discord')
    def test_main_first_run_fallback_filtering(self, mock_post_discord, mock_summarize, mock_fetch, mock_scrape):
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
        
        mock_scrape.side_effect = lambda url: {
            "https://eprint.iacr.org/2026/1111": {"modified_time": "2026-05-25T09:04:12+00:00", "published_time": "2026-05-25T09:04:12+00:00"},
            "https://eprint.iacr.org/2026/1112": {"modified_time": "2026-05-25T09:04:12+00:00", "published_time": "2026-05-25T09:04:12+00:00"},
            "https://eprint.iacr.org/2023/2222": {"modified_time": "2023-10-02T22:45:48+00:00", "published_time": "2023-10-02T22:45:48+00:00"}
        }[url]
        
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

    @patch('eprint_bot.scrape_paper_metadata')
    @patch('eprint_bot.fetch_rss_feed')
    @patch('eprint_bot.summarize_abstract')
    @patch('eprint_bot.post_batches_to_discord')
    def test_main_standard_run_new_paper_and_update_detection(self, mock_post_discord, mock_summarize, mock_fetch, mock_scrape):
        # 1. Setup existing state in state.json
        initial_state = {
            "https://eprint.iacr.org/2026/1111": "2026-05-25T09:00:00+00:00",
            "https://eprint.iacr.org/2026/1112": "2026-05-25T09:04:12+00:00"
        }
        eprint_bot.save_state(initial_state)
        
        # XML feed:
        # Item 1: 2026/1113 (Brand new paper)
        # Item 2: 2026/1111 (Updated paper, new modified_time)
        # Item 3: 2026/1112 (Same paper, same modified_time -> triggers break!)
        xml_new = """
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Brand New Paper</title>
          <link>https://eprint.iacr.org/2026/1113</link>
          <guid>https://eprint.iacr.org/2026/1113</guid>
          <pubDate>Mon, 25 May 2026 09:05:00 +0000</pubDate>
        </item>
        """
        xml_updated = """
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Updated Paper</title>
          <link>https://eprint.iacr.org/2026/1111</link>
          <guid>https://eprint.iacr.org/2026/1111</guid>
          <pubDate>Mon, 25 May 2026 09:00:00 +0000</pubDate>
        </item>
        """
        xml_same = """
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Same Paper</title>
          <link>https://eprint.iacr.org/2026/1112</link>
          <guid>https://eprint.iacr.org/2026/1112</guid>
          <pubDate>Mon, 25 May 2026 09:04:12 +0000</pubDate>
        </item>
        """
        xml_older = """
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Older Unreached Paper</title>
          <link>https://eprint.iacr.org/2026/1114</link>
          <guid>https://eprint.iacr.org/2026/1114</guid>
          <pubDate>Mon, 25 May 2026 08:00:00 +0000</pubDate>
        </item>
        """
        
        mock_fetch.return_value = [
            ET.fromstring(xml_new),
            ET.fromstring(xml_updated),
            ET.fromstring(xml_same),
            ET.fromstring(xml_older)
        ]
        
        mock_scrape.side_effect = lambda url: {
            "https://eprint.iacr.org/2026/1113": {"modified_time": "2026-05-25T09:05:00+00:00", "published_time": "2026-05-25T09:05:00+00:00"},
            "https://eprint.iacr.org/2026/1111": {"modified_time": "2026-05-25T09:10:00+00:00", "published_time": "2026-05-25T09:00:00+00:00"},
            "https://eprint.iacr.org/2026/1112": {"modified_time": "2026-05-25T09:04:12+00:00", "published_time": "2026-05-25T09:04:12+00:00"},
            "https://eprint.iacr.org/2026/1114": {"modified_time": "2026-05-25T08:00:00+00:00", "published_time": "2026-05-25T08:00:00+00:00"}
        }[url]
        
        mock_summarize.return_value = "Summary Text"
        
        # Execute
        eprint_bot.main()
        
        # 1. Verify summarizes called for Brand New and Updated papers, but NOT for Same or Older
        self.assertEqual(mock_summarize.call_count, 2)
        mock_summarize.assert_any_call("Brand New Paper", [], "")
        mock_summarize.assert_any_call("Updated Paper", [], "")
        
        # 2. Verify scrape called for New, Updated, Same, but NOT for Older (due to break!)
        self.assertEqual(mock_scrape.call_count, 3)
        mock_scrape.assert_any_call("https://eprint.iacr.org/2026/1113")
        mock_scrape.assert_any_call("https://eprint.iacr.org/2026/1111")
        mock_scrape.assert_any_call("https://eprint.iacr.org/2026/1112")
        
        # 3. Verify Discord embeds were sent with correct embeds (New & Update)
        mock_post_discord.assert_called_once()
        posted_embeds = mock_post_discord.call_args[0][0]
        self.assertEqual(len(posted_embeds), 2)
        self.assertEqual(posted_embeds[0]["title"], "Brand New Paper")
        self.assertEqual(posted_embeds[1]["title"], "🔄 [Update] Updated Paper")
        
        # 4. Verify state was updated
        state = eprint_bot.load_state()
        self.assertEqual(state["https://eprint.iacr.org/2026/1113"], "2026-05-25T09:05:00+00:00")
        self.assertEqual(state["https://eprint.iacr.org/2026/1111"], "2026-05-25T09:10:00+00:00")

    @patch('eprint_bot.scrape_paper_metadata')
    @patch('eprint_bot.fetch_rss_feed')
    @patch('eprint_bot.summarize_abstract')
    @patch('eprint_bot.post_batches_to_discord')
    def test_main_standard_run_migration(self, mock_post_discord, mock_summarize, mock_fetch, mock_scrape):
        # 1. Setup old list state
        with open(eprint_bot.STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({"processed_ids": ["https://eprint.iacr.org/2026/1111"]}, f)
            
        # XML feed:
        # Item 1: 2026/1111 (Migrated paper, should be scraped but not notified!)
        xml_migrated = """
        <item xmlns:dc="http://purl.org/dc/elements/1.1/">
          <title>Migrated Paper</title>
          <link>https://eprint.iacr.org/2026/1111</link>
          <guid>https://eprint.iacr.org/2026/1111</guid>
          <pubDate>Mon, 25 May 2026 09:00:00 +0000</pubDate>
        </item>
        """
        mock_fetch.return_value = [ET.fromstring(xml_migrated)]
        
        mock_scrape.return_value = {
            "modified_time": "2026-05-25T09:15:00+00:00",
            "published_time": "2026-05-25T09:00:00+00:00"
        }
        
        # Execute
        eprint_bot.main()
        
        # Verify NO notifications were sent
        mock_summarize.assert_not_called()
        mock_post_discord.assert_not_called()
        
        # Verify scrape WAS called to populate the new date format
        mock_scrape.assert_called_once_with("https://eprint.iacr.org/2026/1111")
        
        # Verify state now has the dictionary format with the populated date
        state = eprint_bot.load_state()
        self.assertEqual(state, {"https://eprint.iacr.org/2026/1111": "2026-05-25T09:15:00+00:00"})

if __name__ == '__main__':
    unittest.main()
