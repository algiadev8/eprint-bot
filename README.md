# Cryptology ePrint RSS Summarizer Bot 📢🤖

A robust, lightweight Python-based automation bot that monitors the [IACR Cryptology ePrint Archive](https://eprint.iacr.org/) RSS feed, summarizes newly published academic paper abstracts using an OpenAI-compatible LLM (e.g. OpenAI, DeepSeek, Gemini, local models), and posts beautiful, consolidated notifications to a Discord channel via a webhook.

## ✨ Features

- **🚀 Date-filtered First Run**: On its very first run (when no state file exists), the bot only processes papers published on the **current date (UTC)**. All older historical papers are immediately marked as processed, avoiding bulk LLM costs and feed channel spam.
- **📦 Consolidated Post (Anti-Spam)**: Instead of flooding your channel with separate messages for each paper, the bot aggregates all new papers found during an execution and posts them together. It automatically chunks posts using Discord's multiple embed feature (up to 10 embeds per message, keeping total character count within limits).
- **🧠 OpenAI-Compatible API Support**: Works natively with any provider supporting the standard Chat Completions endpoint (OpenAI, DeepSeek, Gemini via its OpenAI compatibility API, or local LLMs running via Ollama/vLLM).
- **🎨 Premium Visuals**: Renders paper titles, direct links, creators (authors), categories, and the generated bullet-point summaries in elegant dark slate Discord Embed cards.
- **💾 State Persistence**: Keeps track of processed papers in a lightweight, self-contained `state.json` file.

---

## 🛠️ Installation & Setup

### 1. Requirements
- **Python 3.9 or higher** (managed by `uv`).
- **uv** (Astral's ultra-fast package manager and Python runner).
- A Discord Webhook URL.
- An OpenAI-compatible LLM API Key.

### 2. Configuration
Copy the template environment file:
```bash
cp .env.example .env
```
Open `.env` in your favorite editor and configure the variables:

```env
# Discord Webhook configuration
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# LLM API configuration (OpenAI-compatible)
LLM_API_URL=https://api.openai.com/v1
LLM_API_KEY=your-actual-api-key
LLM_MODEL=gpt-4o-mini

# Optional configuration
SUMMARY_LANGUAGE=Japanese  # Output summary language
```

#### Provider Examples:
* **DeepSeek**:
  - `LLM_API_URL=https://api.deepseek.com/v1`
  - `LLM_MODEL=deepseek-chat`
* **Gemini (OpenAI Compatibility Mode)**:
  - `LLM_API_URL=https://generativelanguage.googleapis.com/v1beta/openai`
  - `LLM_MODEL=gemini-1.5-flash`
* **Ollama (Local)**:
  - `LLM_API_URL=http://localhost:11434/v1`
  - `LLM_MODEL=llama3`

---

## 🚀 Running the Bot (Zero Setup via `uv`)

Because the script defines its dependencies using standard **PEP 723 inline script metadata**, you do **NOT** need to create virtual environments or manually install packages! `uv` handles it all automatically.

Run the script in one shot:
```bash
uv run eprint_bot.py
```
Or run the unit tests:
```bash
uv run test_eprint_bot.py
```

- **First Launch**: It checks if there are any new papers published *today* (UTC calendar date), processes/summarizes them, posts them as a single/consolidated Discord post, and marks all older papers in the feed as read inside `state.json`.
- **Subsequent Runs**: It parses the RSS feed, compares it with `state.json`, and only processes new papers that arrived since the last execution.

*Note: If you do not have `uv` installed, you can still fall back to `pip install -r requirements.txt` and `python3 eprint_bot.py`.*

---

## ⏱️ Automation (Scheduled Execution)

To keep receiving updates, you should automate the execution of this script.

### Using `cron` with `uv` (macOS/Linux)
Open your crontab editor:
```bash
crontab -e
```
Add a rule to run the script regularly (for example, every 3 hours at minute 0). 

> [!NOTE]
> It is best to use absolute paths for both `uv`, the script, and the working directory so `state.json` and `.env` are loaded properly.

```cron
0 */3 * * * cd /Users/dmtst/ghq/github.com/daichi-629/eprint-bot && /usr/local/bin/uv run eprint_bot.py >> /Users/dmtst/ghq/github.com/daichi-629/eprint-bot/cron.log 2>&1
```
*(Make sure to replace `/usr/local/bin/uv` with the actual path to your `uv` binary, which you can find by running `which uv` in your terminal).*

### Using GitHub Actions (Optional)
If you want to run it completely serverless, you can push the project to GitHub and configure a GitHub Action with `cron` dispatch, utilizing repository secrets for the `.env` variables and `astral-sh/setup-uv` action.

---

## 🐳 Running with Docker (Recommended for continuous servers)

If you are running this bot on a continuous home server, NAS (e.g. Synology), or VPS, you can run the bot in persistent **Daemon Mode** inside a Docker container. The state database (`state.json`) will be persisted inside a local volume.

### 1. Set Up Environment
Configure `.env` as usual.

### 2. Launch with Docker Compose
Run the following command to build the image and start the container in the background:
```bash
docker compose up -d
```

- **Daemon Mode**: The container is configured by default to enter a continuous daemon loop (`DAEMON_INTERVAL=10800` - checks the feed every 3 hours). You can adjust this value in `docker-compose.yml`.
- **State Persistence**: The bot will write `state.json` inside the container's `/app/data` directory, which is mapped to `./data/state.json` on your host machine. This ensures no duplicate notifications are sent even if the container restarts.

### 3. Manage Container
- **Check Logs**:
  ```bash
  docker compose logs -f
  ```
- **Stop Bot**:
  ```bash
  docker compose down
  ```

---

## 📁 Files Included

- `eprint_bot.py`: Main executable Python script (supports one-shot & daemon loop execution).
- `test_eprint_bot.py`: Test suite using unittest.mock.
- `requirements.txt`: Project dependencies (`requests`, `python-dotenv`).
- `.env.example`: Configuration template.
- `.gitignore`: Prevents secrets and local DB files from leaking.
- `Dockerfile`: Highly optimized Docker image builder utilizing `uv`.
- `docker-compose.yml`: Quickcompose setup for continuous server run.
- `state.json` *(Generated on first run)*: Flat-file JSON containing processed paper IDs.
