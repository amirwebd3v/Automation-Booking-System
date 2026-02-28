# sim24 Auto Data Booker 🤖

Automatically monitors your sim24.de data volume and books a free 2 GB packet when remaining data drops below 0.5 GB. Controlled entirely via Telegram.

---

## Architecture

```
GitHub Actions (every 5 min)
    → Checks elapsed time against configured interval (stored in Gist)
    → Logs in fresh to sim24 (avoids 10-min session timeout)
    → Reads data usage via ARIA attributes
    → If remaining < 0.5 GB → clicks booking button
    → If captcha appears → sends image to Telegram → waits for your reply
    → Sends result notification via Telegram

Render.com (always-on free bot)
    → Listens for /interval and /status Telegram commands
    → Updates GitHub Gist config so next Actions run picks it up
```

---

## Project Structure

```
Automation-Booking-System/
├── .github/
│   └── workflows/
│       └── check_data.yml          GitHub Actions scheduler (every 5 min)
├── scheduler_bot/
│   ├── bot.py                      Always-on Telegram command listener (Render.com)
│   └── requirements_bot.txt        Dependencies for the bot only
├── booking.py                      Clicks the 2 GB booking button
├── captcha_handler.py              Sends captcha image to Telegram, waits for reply
├── config_manager.py               Reads env vars + manages Gist state
├── data_checker.py                 Scrapes usage data from sim24 page
├── decision_engine.py              0.5 GB threshold logic
├── login.py                        Playwright login flow
├── main.py                         Entry point (run by GitHub Actions)
├── telegram_notify.py              All Telegram API communication
├── test_local.py                   Local test runner (4 test modes)
├── requirements.txt                Dependencies for GitHub Actions
└── .env.example                    Template for local .env file
```

---

## Setup Guide

### Step 1 — Telegram Bot
1. Message `@BotFather` on Telegram
2. Send `/newbot` and follow the prompts
3. Save the **bot token** (format: `123456:ABC-DEF...`)
4. Message `@userinfobot` — save your **Chat ID** (a number like `987654321`)
5. Send `/start` to your bot to open the conversation

### Step 2 — GitHub Gist (Config Store)
1. Go to https://gist.github.com
2. Create a **secret** gist with filename: `sim24_bot_config.json`
3. Initial content:
   ```json
   {
     "interval_minutes": 30,
     "last_run_ts": 0
   }
   ```
4. Save the **Gist ID** from the URL: `gist.github.com/{username}/{GIST_ID}`

### Step 3 — GitHub Personal Access Token
1. Go to GitHub → Settings → Developer Settings → Personal Access Tokens → Fine-grained
2. Create a token with **Gist scope only** (read + write)
3. Save the token (shown only once)

### Step 4 — GitHub Repository Secrets
Push this project to a **private** GitHub repository, then go to **Settings → Secrets and variables → Actions** and add:

| Secret Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID from userinfobot |
| `SIM24_USERNAME` | Your sim24 username or phone number |
| `SIM24_PASSWORD` | Your sim24 online password |
| `GIST_TOKEN` | GitHub PAT from Step 3 |
| `GIST_ID` | Gist ID from Step 2 |

### Step 5 — Enable GitHub Actions
1. Go to the **Actions** tab in your repo
2. Enable workflows if prompted
3. The workflow runs automatically every 5 minutes via cron

### Step 6 — Deploy Scheduler Bot to Render.com
1. Go to https://render.com → Sign up (free tier)
2. **New → Web Service** → connect your GitHub repo
3. Configure the service:

| Setting | Value |
|---|---|
| **Root Directory** | `scheduler_bot` |
| **Build command** | `pip install -r requirements_bot.txt` |
| **Start command** | `python bot.py` |

4. Add Environment Variables:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Same as GitHub secret |
| `TELEGRAM_CHAT_ID` | Same as GitHub secret |
| `GITHUB_GIST_TOKEN` | Your GitHub PAT (the token value itself) |
| `GITHUB_GIST_ID` | Your Gist ID |

5. Click **Deploy**

---

## Local Testing

Copy `.env.example` to `.env` and fill in your credentials, then run:

```bash
pip install -r requirements.txt
```

Run tests in order:

```bash
# Test 1 — Telegram connectivity (fastest, no browser)
python test_local.py --test telegram

# Test 2 — Login (opens headless browser, watch Telegram for captcha)
python test_local.py --test login

# Test 3 — Data reader (login + read actual GB numbers)
python test_local.py --test data

# Test 4 — Full dry run (complete pipeline, no real booking)
python test_local.py --test full
```

---

## Telegram Commands

| Command | Description |
|---|---|
| `/interval 30` | Check every 30 minutes (default) |
| `/interval 60` | Check every 60 minutes |
| `/interval 5` | Check every 5 minutes (minimum) |
| `/status` | Show interval, last run time, and next run estimate |
| `/help` | Show all commands |

---

## Captcha Flow

When a captcha appears during login or booking:
1. Bot sends a photo of the captcha to Telegram
2. You have **3 minutes** to reply with the text you see
3. Bot enters your answer and continues
4. If no reply within 3 minutes → booking aborted → retried on next cycle

---

## How the 10-Minute Session Timeout is Handled

The sim24 portal logs out sessions after 10 minutes of inactivity. This system performs a **fresh login on every check cycle** — no session is persisted between runs. The booking threshold is checked immediately after login.
