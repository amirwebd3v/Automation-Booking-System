# sim24 Auto Data Booker 🤖

Automatically monitors your sim24.de data volume and books a free 2GB packet when remaining data drops below 1.5 GB. Controlled entirely via Telegram.

---

## Architecture

```
GitHub Actions (every 5min)
    → Checks elapsed time against configured interval
    → Logs in fresh to sim24 (avoids 10min session timeout)
    → Reads data usage via ARIA attributes
    → If remaining < 1.5 GB → clicks booking button
    → If captcha appears → sends image to Telegram → waits for your reply
    → Sends result notification via Telegram

Render.com (always-on free bot)
    → Listens for /interval and /status commands
    → Updates GitHub Gist config
```

---

## Setup Guide (Step by Step)

### Step 1 — Telegram Bot
1. Message `@BotFather` on Telegram
2. Send `/newbot` and follow the prompts
3. Save the **bot token** (format: `123456:ABC-DEF...`)
4. Message `@userinfobot` — save your **Chat ID** (a number like `987654321`)
5. Start a conversation with your bot (send it `/start`)

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
2. Create token with **only Gist scope** (read + write)
3. Save the token (shown only once)

### Step 4 — GitHub Repository
1. Create a new **private** GitHub repository
2. Push this entire project to it
3. Go to **Settings → Secrets and variables → Actions**
4. Add these secrets:

| Secret Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID from userinfobot |
| `SIM24_USERNAME` | Your sim24 username or phone number |
| `SIM24_PASSWORD` | Your sim24 online password |
| `GIST_TOKEN` | GitHub PAT from Step 3 |
| `GIST_ID` | Gist ID from Step 2 |

### Step 5 — Enable GitHub Actions
1. Go to **Actions** tab in your repo
2. Enable workflows if prompted
3. The workflow runs automatically every 5 minutes

### Step 6 — Deploy Scheduler Bot to Render.com
1. Go to https://render.com → Sign up (free)
2. New → **Web Service** → Connect your GitHub repo
3. Settings:
   - **Root Directory:** `scheduler_bot`
   - **Build command:** `pip install -r requirements_bot.txt`
   - **Start command:** `python bot.py`
4. Add Environment Variables (same as GitHub secrets, minus SIM24 credentials):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `GITHUB_GIST_TOKEN`
   - `GITHUB_GIST_ID`
5. Deploy

---

## Telegram Commands

| Command | Description |
|---|---|
| `/interval 30` | Check every 30 minutes (default) |
| `/interval 60` | Check every 60 minutes |
| `/interval 5` | Check every 5 minutes (minimum) |
| `/status` | Show interval, last run time, next run |
| `/help` | Show all commands |

---

## Captcha Flow

When a captcha appears during booking:
1. Bot sends you a photo of the captcha in Telegram
2. You have **3 minutes** to reply with the text you see
3. Bot enters your answer and completes the booking
4. If no reply in 3 minutes → booking aborted → retries next cycle

---

## How the 10-Minute Timeout is Handled

The sim24 portal logs users out after 10 minutes of inactivity. This system does a **fresh login on every check cycle** — no session is kept between runs. This is safe and reliable.

---

## Files

```
sim24-auto-booker/
├── .github/workflows/check_data.yml   GitHub Actions scheduler
├── src/
│   ├── main.py                        Entry point
│   ├── config_manager.py              Reads env vars + manages Gist state
│   ├── login.py                       Playwright login
│   ├── data_checker.py                Scrapes usage data
│   ├── decision_engine.py             1.5 GB threshold logic
│   ├── captcha_handler.py             Telegram captcha flow
│   ├── booking.py                     Clicks booking button
│   └── telegram_notify.py             All Telegram messaging
├── scheduler_bot/
│   ├── bot.py                         Always-on command listener
│   └── requirements_bot.txt
├── requirements.txt
├── .env.example
└── .gitignore
```
