# Cryptology ePrint RSS Summarizer Bot 📢🤖

A Python-based automation bot that monitors the [IACR Cryptology ePrint Archive](https://eprint.iacr.org/) RSS feed, summarizes academic paper abstracts using an LLM, and posts consolidated notifications to Discord.

## ✨ Features

- **Per-paper Notifications**: Sends each summary with a useful notification preview.
- **OpenAI-Compatible API**: Works natively with OpenAI, DeepSeek, Gemini, or local LLMs.
- **Date-filtered First Run**: Prevents bulk LLM API costs on first startup.
- **State Persistence**: Remembers processed papers in `state.json`.
- **Separate Discord Channels**: Routes original publications and revisions to different webhooks using the article's original/modified dates, not whether its ID is known.
- **Complete Feed Scan**: Checks every RSS entry, including pending deliveries after known entries.
- **Reliable State**: Checkpoints individual successful deliveries atomically; failed deliveries remain pending. Corrupt state aborts the run rather than replaying the feed. A file lock prevents concurrent runs sharing a state file.

### Notification semantics

An unseen article with a modification time later than its original publication is
an **update**, including old papers reappearing in RSS (e.g. `2026/537`). An
unseen original version is **new**. Known articles are notified only when the
modification instant advances; timezone offsets are normalized to UTC.
Missing/invalid article dates are deferred rather than guessed from RSS pubDate.
Page requests are spaced by at least five seconds. HTTP 429/503 stops further
page requests in that scan; remaining entries stay pending for the next run.
The first run baselines older publication days and notifies only the latest
publication day (today if present). Existing list-format state is migrated without
re-notifying known IDs.

Successful deliveries are saved one at a time. Delivery is **at least once**:
a crash after Discord accepts a message but before its checkpoint may cause a
retry. RSS only contains a limited recent window, so entries no longer in the
feed cannot be recovered automatically. Existing historical mislabels are not
resent or rewritten.

Preview a full scan without calling the LLM, posting to Discord, or changing state:

```sh
docker compose exec eprint-bot python eprint_bot.py --dry-run
```

The preview shares the state lock; run it between scheduled scans. Do not delete
state to troubleshoot: preserve `data/state.json` and inspect logs instead.
Unit tests use isolated temporary state and mocked network calls.

---

## 🚀 Setup & Execution

### 1. Configure Environment
Copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```

To separate notifications into two Discord channels, create a webhook in each
channel and configure both URLs:

```dotenv
DISCORD_NEW_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_UPDATE_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

The existing `DISCORD_WEBHOOK_URL` remains available as a fallback for either
notification type, so a single-channel configuration continues to work.

### 2. Run with `uv`
Ensure you have [uv](https://github.com/astral-sh/uv) installed, then run:
```bash
# Run the bot
uv run eprint_bot.py

# Run unit tests
uv run test_eprint_bot.py
```
*(Or fall back to: `pip install -r requirements.txt && python3 eprint_bot.py`)*

---

## 🐳 Running with Docker

You can run the bot in persistent Daemon Mode:

```bash
docker compose up -d
```
- Logs can be checked via `docker compose logs -f`
- The state is persisted in `./data/state.json`

### Test notifications in production

To verify the production Discord Webhook without fetching the RSS feed, calling
the LLM, or changing `state.json`, run:

```bash
docker compose exec eprint-bot python eprint_bot.py --test-notification
```

This sends one clearly labeled dummy notification and exits. A delivery failure
returns a non-zero exit status, so the command can also be used in deployment
checks.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
