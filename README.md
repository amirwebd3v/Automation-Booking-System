# sim24 Auto Data Booker

Automates sim24.de data checks and books a 2 GB packet when remaining data falls below 0.5 GB.
Captcha handling is fully autonomous via Gemini AI; a human fallback via Telegram is available if Gemini fails.

## How It Works

```
Cloudflare Worker (cron: every hour)
    └─ POST workflow_dispatch → GitHub Actions check_data.yml
           └─ installs Python + Playwright Chromium
              └─ runs main.py
                     ├─ ConfigManager   — loads env vars + Gist state
                     ├─ interval gate   — skip if not enough time has elapsed
                     ├─ Sim24Login      — Playwright browser login (Edge locally, Chromium in CI)
                     ├─ DataChecker     — reads used/total KB from the data-usage page
                     ├─ DecisionEngine  — books if remaining < 0.5 GB
                     ├─ BookingModule   — clicks Buchen, activates packet, handles captcha
                     ├─ TelegramNotifier — sends run summary or error alerts
                     └─ ConfigManager   — writes last_run_ts back to Gist

scheduler_bot/bot.py  (always-on, runs on your machine / a server)
    └─ long-polls Telegram
       ├─ 📊 Status   — reads Gist, shows last-run time + inline [🔄 Refresh] [📦 Book Now]
       └─ 📦 Book Now — dispatches the GitHub Actions workflow immediately
```

Captcha flow (used in login and booking):

1. Gemini 1.5 Flash reads the captcha image automatically.
2. If Gemini fails after 3 attempts, a screenshot is sent to Telegram.
3. The user replies with the captcha text within 5 minutes.
4. The reply is entered into the page and the workflow resumes.

## Project Structure

```
Automation-Booking-System/
├── .github/
│   └── workflows/
│       ├── check_data.yml      — main booking pipeline (workflow_dispatch only)
│       └── tests.yml           — CI test runner (push / PR / manual)
├── cloudflare_trigger/
│   ├── worker.js               — hourly cron → workflow_dispatch + webhook captcha relay
│   └── wrangler.toml           — Cloudflare Worker config
├── scheduler_bot/
│   ├── __init__.py
│   └── bot.py                  — always-on Telegram control bot (button UX)
├── tests/
│   ├── test_booking.py
│   ├── test_captcha_handler.py
│   ├── test_config_manager.py
│   ├── test_data_checker.py
│   ├── test_decision_engine.py
│   ├── test_live_workflow.py   — live integration tests (require real credentials)
│   ├── test_login.py
│   ├── test_main_alerts.py
│   ├── test_main_workflow.py
│   ├── test_scheduler_bot.py
│   └── test_telegram_notify.py
├── booking.py
├── captcha_handler.py
├── config_manager.py
├── data_checker.py
├── decision_engine.py
├── login.py
├── main.py
├── telegram_notify.py
├── test_local.py               — manual local runner (telegram / login / data / full)
├── pyproject.toml              — project metadata, pytest config, ruff lint config
├── requirements.txt            — runtime dependencies
├── requirements-dev.txt        — test + lint dependencies
├── .env.example
└── README.md
```

## File Responsibilities

### Core pipeline

#### `main.py`

Entry point for one pipeline run.

1. Instantiates `ConfigManager` and `TelegramNotifier`.
2. Skips early if `is_time_to_run()` returns `False`.
3. Calls `Sim24Login.login()` → returns `(browser, page)`.
4. Calls `DataChecker.get_usage()` → returns `(used_kb, total_kb)`.
5. Calls `DecisionEngine.should_book(remaining_gb)`.
6. If booking is needed, calls `BookingModule.book_2gb_packet()`.
7. Sends a run summary via `_build_run_summary()` → always includes "Run complete" + action taken.
8. On `CaptchaSolveError` or unexpected exceptions: sends a photo alert with a screenshot.
9. Calls `config.update_last_run()` in `finally` regardless of outcome.
10. Closes the browser and stops Playwright in `finally`.

#### `config_manager.py`

Centralizes environment variables and shared Gist state.

Reads these environment variables:

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Authorized chat id |
| `SIM24_USERNAME` | sim24 login username |
| `SIM24_PASSWORD` | sim24 online password |
| `GIST_TOKEN` | Classic PAT with `gist` scope |
| `GIST_ID` | GitHub Gist id |

State stored in `sim24_bot_config.json` inside the Gist:

```json
{
  "interval_minutes": 10,
  "last_run_ts": 0,
  "captcha_pending": false,
  "captcha_reply": ""
}
```

`captcha_pending` / `captcha_reply` are written by the booking pipeline and read by the Cloudflare Worker captcha relay when Gemini fails and a human reply is needed.

If Gist loading fails, falls back to `interval_minutes=10, last_run_ts=0`.

#### `login.py`

Playwright-based login sequence.

1. Launches Playwright with Microsoft Edge (`USE_EDGE=true`) or Chromium (default / CI).
2. Reuses `storage_state.json` if present to skip the login form.
3. If the stored session is stale, falls back to credential login (up to 2 attempts).
4. Handles pre-submit and post-submit captcha via `CaptchaHandler`.
5. Saves `storage_state.json` after a successful fresh login.
6. Returns `(browser, page)` pointed at the data-usage page, or `(None, None)` on failure.

#### `data_checker.py`

Scrapes used/total data from the sim24 usage page.

- **Primary**: reads `aria-valuenow` / `aria-valuemax` from the progressbar element (KB values).
- **Fallback**: parses German-formatted text (`98,30 GB`) from visible spans.

Returns `(used_kb, total_kb)` or `(None, None)` on failure.

#### `decision_engine.py`

Single rule: `book if remaining_gb < threshold_gb` (default threshold: 0.5 GB).

#### `booking.py`

Executes the 2 GB packet booking flow.

1. Locates the Buchen button via multiple CSS selectors.
2. Aborts if the button is disabled (already active, payment issue, etc.).
3. Dismisses cookie consent if present.
4. Clicks the button while listening for the `getChangeServiceInfo` AJAX response.
5. Parses the response HTML for the activation URL and form id.
6. Calls `sendPostAndReplaceContent(url, formId)` via JavaScript.
7. Falls back to a direct `changeService` POST if HTML parsing fails.
8. Solves any post-activation captcha via `CaptchaHandler`.
9. Verifies success by inspecting page content, dialogs, and URL hints.
10. Sends a Telegram alert with an internal trace log on failure.

#### `captcha_handler.py`

Autonomous captcha resolution backed by Gemini 1.5 Flash with a human fallback.

1. Detects captcha images using multiple CSS selectors.
2. Encodes the image as base64 and calls the Gemini API.
3. Enters the Gemini response into the captcha input field.
4. If the code is wrong, reloads the captcha and retries (up to 3 attempts).
5. If Gemini is unavailable or fails all 3 attempts, sends the screenshot to Telegram and waits up to **5 minutes** for a human reply written into the Gist.
6. Raises `CaptchaSolveError` if the timeout expires.

Requires `GEMINI_API_KEY` in the environment for the autonomous path.

#### `telegram_notify.py`

Thin async Telegram Bot API client.

- `send(text)` — sends a Markdown-formatted message.
- `send_photo(bytes, caption)` — uploads an image (used for captcha and error screenshots).

#### `scheduler_bot/bot.py`

Always-on Telegram control bot with a button-based UX.

Reply keyboard (persistent bar shown to the authorized user):

```
[ 📊 Status ]  [ 📦 Book Now ]
```

- **📊 Status** — reads the Gist and sends last-run time + captcha state with inline buttons `[🔄 Refresh]` `[📦 Book Now]`.
- **🔄 Refresh** (inline) — edits the status message in place with fresh Gist data.
- **📦 Book Now** — dispatches the GitHub Actions `check_data.yml` workflow immediately.
- **Plain text while `captcha_pending`** — saves the captcha reply to the Gist so the pipeline can pick it up.

Authorization: only the `TELEGRAM_CHAT_ID` user is served; all other chats are silently ignored.

Requires these environment variables in addition to the standard ones:

| Variable | Purpose |
|---|---|
| `GITHUB_GIST_TOKEN` (or `GIST_TOKEN`) | Gist read/write |
| `GITHUB_GIST_ID` (or `GIST_ID`) | Gist id |
| `GITHUB_PAT` | Classic PAT with `gist` + `workflow` scopes (for dispatch); falls back to `GITHUB_GIST_TOKEN` |

### Scheduling layer

#### `cloudflare_trigger/worker.js`

A Cloudflare Worker with two entry points:

1. **Cron** (`0 * * * *` — every hour): calls `workflow_dispatch` on `check_data.yml`.  
   GitHub's built-in `schedule` trigger is unreliable under load; this Worker provides consistent hourly execution.
2. **Webhook** (`POST /webhook`): handles Telegram webhook updates:
   - `/book` → dispatches the workflow immediately.
   - `/status` → reads and reports Gist state.
   - Plain text → if `captcha_pending`, saves the reply to the Gist.

Cloudflare secrets required (`wrangler secret put <NAME>`):

| Secret | Purpose |
|---|---|
| `GITHUB_PAT` | Fine-grained PAT, Actions: Read & Write |
| `GIST_TOKEN` | Classic PAT with `gist` scope |
| `GIST_ID` | Gist id |
| `TELEGRAM_BOT_TOKEN` | Bot token |
| `TELEGRAM_CHAT_ID` | Authorized chat id |
| `TELEGRAM_WEBHOOK_SECRET` | Random string registered with `setWebhook` |

> **Note:** The Python bot (`scheduler_bot/bot.py`) and the Cloudflare Worker webhook are mutually exclusive. Only one can receive Telegram updates at a time. The Python bot calls `deleteWebhook` on startup to claim long-polling; the Cloudflare Worker uses the webhook. Run one or the other, not both.

## Setup

### 1. Telegram bot

1. Create a bot with `@BotFather`.
2. Save the bot token.
3. Get your personal chat id from `@userinfobot`.

### 2. Gemini API key

1. Go to [aistudio.google.com](https://aistudio.google.com) and create an API key.
2. Add `GEMINI_API_KEY` to your `.env` and GitHub Secrets.

### 3. GitHub Gist state store

Create a **secret** Gist with filename `sim24_bot_config.json`:

```json
{
  "interval_minutes": 10,
  "last_run_ts": 0
}
```

Save the Gist ID from the URL.

### 4. GitHub tokens

| Token | Required scopes | Used by |
|---|---|---|
| `GIST_TOKEN` | `gist` | `config_manager.py`, Cloudflare Worker |
| `GITHUB_PAT` | `gist` + `workflow` | `scheduler_bot/bot.py`, Cloudflare Worker dispatch |

A single Classic PAT with both scopes works for all consumers.

### 5. GitHub Actions secrets

| Secret | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Authorized chat id |
| `SIM24_USERNAME` | sim24 username or phone number |
| `SIM24_PASSWORD` | sim24 online password |
| `GEMINI_API_KEY` | Gemini AI captcha solver |
| `GIST_TOKEN` | Gist PAT |
| `GIST_ID` | Gist id |

### 6. Local `.env`

Copy `.env.example` to `.env` and fill in all values:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
SIM24_USERNAME=your_username_or_phone_number
SIM24_PASSWORD=your_online_password
GEMINI_API_KEY=your_gemini_key_here
GIST_TOKEN=ghp_your_gist_token
GIST_ID=your_gist_id_here
GITHUB_PAT=ghp_your_pat_with_workflow_scope
USE_EDGE=true      # use local Edge instead of Playwright Chromium
```

### 7. Run the Telegram control bot

```bash
python -m scheduler_bot.bot
```

The bot calls `deleteWebhook` on start, clears stale queued messages, then begins long-polling.

### 8. Deploy the Cloudflare Worker (optional)

If you prefer the webhook approach or want the hourly cron without keeping a local process running:

```bash
cd cloudflare_trigger
wrangler deploy
wrangler secret put GITHUB_PAT
wrangler secret put GIST_TOKEN
# ... add all required secrets
```

## Captcha Handling

```
Captcha detected on page
    └─ Gemini 1.5 Flash reads image → enters solution
          ├─ Correct → continue
          └─ Wrong → reload + retry (up to 3 attempts)
                └─ All attempts fail
                       └─ Screenshot sent to Telegram
                              └─ Wait up to 5 minutes for human reply
                                     ├─ Reply received → enter code, continue
                                     └─ Timeout → raise CaptchaSolveError → pipeline aborts
```

## Testing

### Automated tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -m "not live and not destructive"
```

Test files and what they cover:

| File | Covers |
|---|---|
| `test_booking.py` | Button detection, modal activation, captcha handling in booking |
| `test_captcha_handler.py` | Gemini solve path, Aktivieren click |
| `test_config_manager.py` | Gist load, fallback defaults, state persistence |
| `test_data_checker.py` | ARIA method, text fallback, German decimal parsing |
| `test_decision_engine.py` | Threshold logic |
| `test_live_workflow.py` | Real Telegram, real login, real data read (requires `.env`) |
| `test_login.py` | Session reuse, fresh login fallback, captcha retry limit |
| `test_main_alerts.py` | `_build_run_summary`, error alert with screenshot |
| `test_main_workflow.py` | Full orchestration: booking path, skip path, login failure |
| `test_scheduler_bot.py` | Status format, all button handlers, auth checks, env fallbacks |
| `test_telegram_notify.py` | `send`, `send_photo`, transport failures |

### Live integration tests

Require real credentials in `.env`:

```bash
python -m pytest tests/test_live_workflow.py -m "live and not destructive" -v
```

### Manual local runner

Runs each module against the real sim24 site and your Telegram:

```bash
python test_local.py --test telegram   # verify Telegram connectivity
python test_local.py --test login      # real Playwright login
python test_local.py --test data       # login + read data usage
python test_local.py --test full       # full dry run (no booking button clicked)
```

## External Services

| Service | Role |
|---|---|
| sim24 portal (`service.sim24.de`) | Login, usage reading, booking |
| Telegram Bot API | Notifications, captcha relay, control bot |
| GitHub Gist | Shared persistent state (timestamps, captcha, interval) |
| GitHub Actions | Scheduled booking pipeline execution environment |
| Cloudflare Workers | Reliable hourly cron trigger + optional webhook entry point |
| Google Gemini API | Autonomous captcha image recognition |

## Dependencies

**Runtime** (`requirements.txt`):

```
playwright==1.44.0
playwright-stealth==1.0.6
google-generativeai==0.8.5
aiohttp==3.9.5
requests==2.32.3
python-dotenv==1.0.1
```

**Development** (`requirements-dev.txt`):

```
-r requirements.txt
pytest>=8.0.0
pytest-asyncio>=0.23.0
ruff>=0.6.0
```

The `scheduler_bot` uses only `aiohttp` and `python-dotenv`, both already in `requirements.txt`.
No separate bot requirements file is needed.
