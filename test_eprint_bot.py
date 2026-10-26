#!/usr/bin/env python3
"""Offline regressions: never load production state, contact LLM or Discord."""
import json
import os
import tempfile
from pathlib import Path
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import xml.etree.ElementTree as ET

import eprint_bot as bot

PUB = "2026-03-17T12:39:47+00:00"
MOD = "2026-10-25T13:59:50+00:00"
TARGET = "https://eprint.iacr.org/2026/537"


def item(url=TARGET, title="Paper"):
    return ET.fromstring(f"<item><title>{title}</title><link>{url}</link>"
                         f"<guid>{url}</guid><description>Abstract</description></item>")


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        self.patch("STATE_FILE", self.state)
        self.patch("DISCORD_WEBHOOK_URL", "https://example.invalid/fallback")
        self.patch("DISCORD_NEW_WEBHOOK_URL", "https://example.invalid/new")
        self.patch("DISCORD_UPDATE_WEBHOOK_URL", "https://example.invalid/update")
        self.fetch = self.patch("fetch_rss_feed", return_value=[item()])
        self.scrape = self.patch("scrape_paper_metadata", return_value={
            "published_time": PUB, "modified_time": MOD})
        self.summary = self.patch("summarize_abstract", return_value="Summary")
        self.post = self.patch("post_batches_to_discord", return_value=True)
        self.patch("time.sleep")

    def patch(self, name, *args, **kwargs):
        p = patch("eprint_bot." + name, *args, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def baseline(self, value=None):
        bot.save_state({"other": MOD} if value is None else value)

    def test_unseen_revision_2026_537_is_update(self):
        self.baseline()
        bot.run_bot_logic()
        self.assertEqual(self.post.call_args.args[0][0]["title"], "🔄 [Update] Paper")
        self.assertEqual(bot.load_state()[TARGET], MOD)

    def test_unseen_original_is_new(self):
        self.baseline()
        self.scrape.return_value["modified_time"] = PUB
        bot.run_bot_logic()
        self.assertEqual(self.post.call_args.args[0][0]["title"], "Paper")

    def test_scan_continues_after_known_item(self):
        self.baseline({TARGET: MOD})
        self.fetch.return_value = [item(), item(TARGET + "0", "Pending")]
        bot.run_bot_logic()
        self.assertEqual(self.scrape.call_count, 2)
        self.summary.assert_called_once_with("Pending", [], "Abstract")

    def test_failed_new_delivery_retried(self):
        self.baseline()
        self.post.return_value = False
        bot.run_bot_logic()
        self.assertNotIn(TARGET, bot.load_state())
        self.post.return_value = True
        bot.run_bot_logic()
        self.assertEqual(bot.load_state()[TARGET], MOD)

    def test_failed_update_preserves_previous_version(self):
        self.baseline({TARGET: PUB})
        self.post.return_value = False
        bot.run_bot_logic()
        self.assertEqual(bot.load_state()[TARGET], PUB)

    def test_partial_success_checkpoints_individually(self):
        self.baseline()
        other = TARGET + "0"
        self.fetch.return_value = [item(), item(other)]
        self.post.side_effect = [True, False]
        bot.run_bot_logic()
        self.assertIn(TARGET, bot.load_state())
        self.assertNotIn(other, bot.load_state())

    def test_unchanged_does_not_send(self):
        self.baseline({TARGET: MOD})
        bot.run_bot_logic()
        self.post.assert_not_called()

    def test_timezone_offsets_compared_as_instants(self):
        self.baseline({TARGET: "2026-10-25T15:59:50+02:00"})
        bot.run_bot_logic()
        self.post.assert_not_called()

    def test_older_metadata_does_not_send(self):
        self.baseline({TARGET: "2026-10-26T00:00:00Z"})
        bot.run_bot_logic()
        self.post.assert_not_called()

    def test_missing_metadata_is_not_rss_new_paper(self):
        self.baseline()
        self.scrape.return_value = {"published_time": None, "modified_time": None}
        bot.run_bot_logic()
        self.post.assert_not_called()
        self.assertNotIn(TARGET, bot.load_state())

    def test_rate_limit_stops_requests_but_keeps_pending_state(self):
        self.baseline()
        self.fetch.return_value = [item(), item(TARGET + "0")]
        self.scrape.side_effect = bot.FeedRateLimited("slow down")
        bot.run_bot_logic()
        self.scrape.assert_called_once()
        self.post.assert_not_called()
        self.assertNotIn(TARGET, bot.load_state())

    def test_invalid_dates_deferred(self):
        self.baseline()
        self.scrape.return_value["published_time"] = "bad"
        bot.run_bot_logic()
        self.post.assert_not_called()

    def test_reversed_dates_deferred(self):
        self.baseline()
        self.scrape.return_value = {"published_time": MOD, "modified_time": PUB}
        bot.run_bot_logic()
        self.post.assert_not_called()

    def test_duplicate_feed_entries_sent_once(self):
        self.baseline()
        self.fetch.return_value = [item(), item()]
        bot.run_bot_logic()
        self.post.assert_called_once()

    def test_legacy_migration_no_resend(self):
        with open(self.state, "w") as f:
            json.dump({"processed_ids": [TARGET]}, f)
        bot.run_bot_logic()
        self.post.assert_not_called()
        self.assertEqual(bot.load_state(), {TARGET: MOD})

    def test_corrupt_state_fails_closed(self):
        with open(self.state, "w") as f:
            f.write("{broken")
        with self.assertRaises(ValueError):
            bot.run_bot_logic()
        self.fetch.assert_not_called()

    def test_invalid_state_schema_fails_closed(self):
        with open(self.state, "w") as f:
            json.dump({"processed_ids": 1}, f)
        with self.assertRaises(ValueError):
            bot.load_state()

    def test_atomic_write_failure_preserves_state(self):
        self.baseline()
        with patch("eprint_bot.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                bot.save_state({TARGET: MOD})
        self.assertEqual(bot.load_state(), {"other": MOD})
        self.assertFalse(any(p.endswith(".tmp") for p in os.listdir(self.tmp.name)))

    def test_concurrent_run_rejected(self):
        with bot.state_lock():
            with self.assertRaises(RuntimeError):
                bot.run_bot_logic()
        self.fetch.assert_not_called()

    def test_first_run_bounds_notifications(self):
        other = TARGET + "0"
        self.fetch.return_value = [item(), item(other, "Newest")]
        self.scrape.side_effect = [
            {"published_time": PUB, "modified_time": MOD},
            {"published_time": MOD, "modified_time": MOD}]
        bot.run_bot_logic()
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0][0]["title"], "Newest")
        self.assertEqual(len(bot.load_state()), 2)

    def test_first_run_failed_delivery_remains_pending(self):
        self.post.return_value = False
        bot.run_bot_logic()
        self.assertEqual(bot.load_state(), {})
        self.post.return_value = True
        bot.run_bot_logic()
        self.assertEqual(bot.load_state()[TARGET], MOD)

    def test_metadata_attribute_order(self):
        parser = bot.ArticleMetadataParser()
        parser.feed(f'<meta content="{PUB}" property="article:published_time">')
        self.assertEqual(parser.metadata["article:published_time"], PUB)

    def test_notification_routing(self):
        embed = {"title": "🔄 [Update] Paper", "description": "Summary", "url": TARGET}
        self.assertEqual(bot.get_discord_webhook_url(embed), bot.DISCORD_UPDATE_WEBHOOK_URL)
        username, content = bot.build_discord_notification(embed)
        self.assertEqual(username, "🔄 更新 | Paper")
        self.assertIn(TARGET, content)
        self.assertEqual(bot.get_discord_webhook_url({"title": "Paper"}), bot.DISCORD_NEW_WEBHOOK_URL)

    def test_test_notification_does_not_run_feed(self):
        bot.main(["--test-notification"])
        self.fetch.assert_not_called()
        self.assertFalse(os.path.exists(self.state))

    def test_test_notification_failure_exits(self):
        self.post.return_value = False
        with self.assertRaises(SystemExit):
            bot.main(["--test-notification"])

    def test_dry_run_has_no_delivery_or_state_changes(self):
        self.baseline()
        before = Path(self.state).read_bytes()
        bot.main(["--dry-run"])
        self.assertEqual(Path(self.state).read_bytes(), before)
        self.post.assert_not_called()
        self.summary.assert_not_called()

    def test_parse_item(self):
        parsed = bot.parse_item(item())
        self.assertEqual(parsed["guid"], TARGET)
        self.assertEqual(parsed["abstract"], "Abstract")


eprint_bot = bot

class LegacyApiTests(unittest.TestCase):

    @patch('requests.get')
    def test_http_rate_limit_stops_scan(self, get):
        get.return_value.status_code = 429
        with self.assertRaises(bot.FeedRateLimited):
            bot.scrape_paper_metadata(TARGET)

    @patch('requests.get')
    def test_scrape_metadata_attribute_order(self, get):
        get.return_value.status_code = 200
        get.return_value.text = (f'<meta content="{PUB}" property="article:published_time">'
                                 f'<meta property="article:modified_time" content="{MOD}">')
        self.assertEqual(bot.scrape_paper_metadata(TARGET), {
            "published_time": PUB, "modified_time": MOD})

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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = patch.multiple(eprint_bot,
            STATE_FILE=os.path.join(tmp.name, "state.json"),
            DISCORD_WEBHOOK_URL=None, DISCORD_NEW_WEBHOOK_URL=None,
            DISCORD_UPDATE_WEBHOOK_URL=None, LLM_API_URL=None,
            LLM_API_KEY=None, LLM_MODEL=None)
        config.start()
        self.addCleanup(config.stop)

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


if __name__ == "__main__":
    unittest.main()
