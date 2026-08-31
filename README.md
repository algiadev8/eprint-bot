# Cryptology ePrint RSS Summarizer Bot 📢🤖

A Python-based automation bot that monitors the [IACR Cryptology ePrint Archive](https://eprint.iacr.org/) RSS feed, summarizes academic paper abstracts using an LLM, and posts consolidated notifications to Discord.

## ✨ Features

- **Consolidated Posts**: Aggregates new papers to avoid spamming the Discord channel.
- **OpenAI-Compatible API**: Works natively with OpenAI, DeepSeek, Gemini, or local LLMs.
- **Date-filtered First Run**: Prevents bulk LLM API costs on first startup.
- **State Persistence**: Remembers processed papers in `state.json`.
- **Separate Discord Channels**: Routes new papers and paper updates to different webhooks.

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
