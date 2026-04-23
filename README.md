# sim24 Auto Data Booker

This project automates sim24.de data checks and books a 2 GB packet when remaining data falls below a threshold. It is not a fully autonomous bot: if sim24 shows a captcha during login or booking, the system sends the captcha image to Telegram and waits for a human reply.

The repository currently contains two runtimes:

1. A scheduled GitHub Actions workflow that runs the booking pipeline.
2. A small always-on Telegram control bot intended to run on Render.

Both runtimes share state through a GitHub Gist.

## What The Project Does

On each eligible cycle, the pipeline:

1. Loads credentials and dynamic state.
2. Checks whether the configured interval has elapsed.
3. Launches a fresh browser session.
4. Logs in to sim24.
5. Navigates to the data-usage page.
6. Reads used and total data.
7. Computes remaining data.
8. If remaining data is below 0.5 GB, triggers the booking flow.
9. Sends status and error notifications to Telegram.
10. Stores the latest run timestamp back in the shared Gist state.

The control bot does not perform booking itself. It only:

1. Changes the configured interval in the Gist.
2. Reports the current interval and last-run timestamp.
3. Triggers the GitHub Actions workflow manually via `/book`.

## Current Architecture

```text
GitHub Actions workflow (.github/workflows/check_data.yml)
    -> runs on cron: every 30 minutes
    -> installs Python + Playwright Chromium
    -> injects secrets as environment variables
    -> executes main.py

main.py
    -> ConfigManager loads env + Gist state
    -> TelegramNotifier sends updates
    -> interval gate decides whether this run should do real work
    -> Sim24Login performs a fresh login
    -> DataChecker reads usage from the sim24 page
    -> DecisionEngine compares remaining data to threshold
    -> BookingModule attempts booking if threshold is crossed
    -> ConfigManager writes updated last_run_ts back to the Gist

Render / always-on process (scheduler_bot/bot.py)
    -> long-polls Telegram
    -> accepts /interval, /status, /help, /book
    -> reads/writes the same Gist state
    -> optionally dispatches the GitHub Actions workflow immediately
```

## Project Structure

```text
Automation-Booking-System/
├── .github/
│   └── workflows/
│       └── check_data.yml
├── scheduler_bot/
│   ├── bot.py
│   └── requirements_bot.txt
├── booking.py
├── captcha_handler.py
├── config_manager.py
├── data_checker.py
├── decision_engine.py
├── login.py
├── main.py
├── telegram_notify.py
├── test_local.py
├── tests/
│   └── test_main_workflow.py
├── requirements.txt
├── .env.example
└── README.md
```

## File-By-File Responsibilities

### Core runtime

#### `main.py`

The orchestration entry point for one pipeline run.

Responsibilities:

1. Create `ConfigManager`.
2. Create `TelegramNotifier`.
3. Stop early if the configured interval has not elapsed.
4. Run login.
5. Read data usage.
6. Compute used, total, and remaining GB.
7. Send a Telegram status report.
8. Trigger booking when `remaining_gb < 0.5`.
9. Update `last_run_ts`.
10. Close the browser in `finally`.

Important current behavior:

1. The code updates `last_run_ts` after successful work.
2. It also updates `last_run_ts` after several failure paths, including login failure, data-read failure, and unexpected exceptions.
3. In practice, the timestamp behaves like the last attempted run, not strictly the last successful run.

#### `config_manager.py`

Centralizes environment variables and shared state.

Reads these variables:

1. `TELEGRAM_BOT_TOKEN`
2. `TELEGRAM_CHAT_ID`
3. `SIM24_USERNAME`
4. `SIM24_PASSWORD`
5. `GIST_TOKEN`
6. `GIST_ID`

State model stored in the Gist file `sim24_bot_config.json`:

```json
{
  "interval_minutes": 30,
  "last_run_ts": 0
}
```

Responsibilities:

1. Fetch the current JSON state from GitHub Gist.
2. Expose `interval_minutes` and `last_run_ts` as properties.
3. Decide whether enough time has elapsed.
4. Persist `last_run_ts` back to the Gist.
5. Persist a changed interval back to the Gist.

Fallback behavior:

If Gist loading fails, it falls back to:

```json
{
  "interval_minutes": 10,
  "last_run_ts": 0
}
```

#### `login.py`

Owns the full Playwright login sequence.

Current implementation details:

1. Starts Playwright asynchronously.
2. Uses Microsoft Edge only when `USE_EDGE=true` is set locally.
3. Otherwise uses Playwright Chromium.
4. Navigates to `https://service.sim24.de/`.
5. Waits for the login form.
6. Checks for captcha before submit.
7. Fills username and password.
8. Clicks submit using multiple fallback selectors.
9. Checks for captcha again after submit.
10. Retries the login flow up to 2 times.
11. On success, navigates to `https://service.sim24.de/mytariff/invoice/showGprsDataUsage`.

Return contract:

1. Returns `(browser, page)` on success.
2. Returns `(None, None)` on failure.

#### `data_checker.py`

Scrapes usage data from the sim24 usage page.

Primary method:

1. Look for `.e-data_usage_meter-data_total[role='progressbar']`.
2. Read `aria-valuenow` as used KB.
3. Read `aria-valuemax` as total KB.

Fallback method:

1. Read visible text such as `98,30 GB`.
2. Parse German decimal formatting.
3. Convert GB into KB.

Return contract:

1. Returns `(used_kb, total_kb)`.
2. Returns `(None, None)` when both methods fail.

#### `decision_engine.py`

Encapsulates the booking rule.

Current effective threshold in the application is `0.5 GB`.

Rule:

```text
book if remaining_gb < threshold_gb
```

#### `booking.py`

Owns the booking sequence once the decision engine says booking is needed.

Current flow:

1. Find the booking button using multiple selectors.
2. Remove disabled state with JavaScript if needed.
3. Click the booking button while listening for the `getChangeServiceInfo` response.
4. Parse the returned HTML for the activation URL and form id.
5. Dismiss cookie consent if it appears.
6. Solve captcha if a pre-activation captcha appears.
7. Trigger activation using parsed HTML.
8. If that fails, fall back to a direct `changeService` call.
9. Check whether the server returned a post-activation captcha.
10. Verify booking outcome by inspecting page content, dialogs, and URL hints.

Other implementation details:

1. Keeps an internal trace log for debugging.
2. Sends Telegram alerts with the trace when booking fails or the outcome is unclear.
3. Uses screenshots for debug situations.

#### `captcha_handler.py`

Provides human-assisted captcha resolution.

Current behavior:

1. Detect captcha images using several selectors.
2. Capture a focused screenshot when possible.
3. Send the image to Telegram.
4. Wait up to 180 seconds for a reply.
5. Enter the provided text into the captcha field.
6. Click the activation button.
7. Detect wrong-code messages.
8. Optionally reload the captcha and retry up to 3 times.

This module is used by both login and booking flows.

#### `telegram_notify.py`

Thin async Telegram Bot API client.

Responsibilities:

1. Send Markdown messages.
2. Send photo uploads.
3. Long-poll `getUpdates` for replies.
4. Ignore messages from chats other than the configured chat id.
5. Skip old updates before starting a reply wait.

### Control runtime

#### `scheduler_bot/bot.py`

The control bot is a separate process from the booking pipeline.

Supported commands:

1. `/interval <minutes>`
2. `/status`
3. `/help`
4. `/book`

Behavior:

1. Only the configured Telegram chat is authorized.
2. `/interval` writes `interval_minutes` into the Gist.
3. `/status` reads interval and `last_run_ts` from the Gist.
4. `/book` triggers the GitHub Actions workflow through the GitHub API.
5. Unknown messages are ignored.

Important current requirement:

`/book` does not only need Gist access. The token used by the bot must also be able to dispatch the repository workflow.

### Local and test surfaces

#### `test_local.py`

Manual local runner with four modes:

1. `telegram`
2. `login`
3. `data`
4. `full`

What each mode does:

1. `telegram`: verifies Telegram messaging only.
2. `login`: runs a real login through Playwright.
3. `data`: logs in and reads actual usage data.
4. `full`: performs a full dry run without clicking the booking action.

#### `tests/test_main_workflow.py`

Automated unit tests for the main orchestration path.

Currently covered scenarios:

1. Booking occurs when remaining data is below threshold.
2. Booking does not occur when remaining data is above threshold.
3. Login failure is reported and state is updated.

This is currently the only automated test file in the repository.

## External Services And Dependencies

### External services

1. sim24 portal for login, usage reading, and booking.
2. Telegram Bot API for messaging and captcha replies.
3. GitHub Gist as the persistent state store.
4. GitHub Actions as the scheduled execution environment.
5. Render or any equivalent always-on host for the control bot.

### Python dependencies

Main runtime dependencies from `requirements.txt`:

```text
playwright==1.44.0
aiohttp==3.9.5
requests==2.32.3
python-dotenv==1.0.1
```

Control bot dependencies from `scheduler_bot/requirements_bot.txt`:

```text
aiohttp==3.9.5
requests==2.32.3
```

### Browser automation stack

1. Playwright async API.
2. Chromium in CI.
3. Optional Edge locally via `USE_EDGE=true`.
4. Selector-based DOM automation with JavaScript fallbacks.

## Setup

### 1. Telegram bot

1. Create a bot with `@BotFather`.
2. Save the bot token.
3. Get your Telegram chat id.
4. Start a conversation with the bot.

### 2. GitHub Gist state store

Create a secret gist with filename `sim24_bot_config.json` and content like:

```json
{
  "interval_minutes": 30,
  "last_run_ts": 0
}
```

### 3. GitHub token

The token must match how you use the project:

1. For Gist read/write only: gist permissions are enough.
2. If you also use `/book` from the control bot: the token must additionally be allowed to dispatch the repository workflow.

### 4. GitHub Actions secrets

Set these repository secrets:

| Secret Name | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Authorized Telegram chat id |
| `SIM24_USERNAME` | sim24 username or phone number |
| `SIM24_PASSWORD` | sim24 online password |
| `GIST_TOKEN` | Token injected into `GIST_TOKEN` |
| `GIST_ID` | Gist id injected into `GIST_ID` |

### 5. Local `.env`

Copy `.env.example` to `.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
SIM24_USERNAME=your_username_or_phone_number
SIM24_PASSWORD=your_online_password
GIST_TOKEN=ghp_your_gist_token
GIST_ID=your_gist_id_here
USE_EDGE=true
```

### 6. Control bot environment

For `scheduler_bot/bot.py`, configure:

1. `TELEGRAM_BOT_TOKEN`
2. `TELEGRAM_CHAT_ID`
3. `GIST_TOKEN`
4. `GIST_ID`

Optional overrides used by `/book`:

1. `GITHUB_REPO_OWNER` defaults to `amirwebd3v`
2. `GITHUB_REPO_NAME` defaults to `Automation-Booking-System`
3. `GITHUB_WORKFLOW_FILE` defaults to `check_data.yml`
4. `GITHUB_WORKFLOW_REF` defaults to `main`

## How Scheduling Works Right Now

There are two scheduling layers:

1. GitHub Actions cron in `.github/workflows/check_data.yml`.
2. The interval gate stored in the Gist and enforced in `main.py`.

Current actual cron schedule:

```text
*/30 * * * *
```

That means the workflow wakes up every 30 minutes, then `main.py` decides whether enough time has elapsed based on `interval_minutes`.

Important consequence:

If you set `/interval 5`, the code accepts it, but GitHub Actions still only wakes up every 30 minutes. So the effective minimum execution cadence is currently 30 minutes unless the workflow is triggered manually.

## Captcha Flow

When sim24 shows a captcha during login or booking:

1. The captcha image is detected in the browser.
2. A screenshot is sent to Telegram.
3. The user replies with the captcha text.
4. The bot enters that text into the page.
5. The workflow continues.
6. If the captcha is wrong, the handler can reload and retry.
7. If no reply arrives within 3 minutes, that booking attempt is aborted.

## Execution Flow In Detail

### Normal booking pipeline

1. GitHub Actions starts `main.py`.
2. `ConfigManager` loads environment variables and Gist state.
3. `is_time_to_run()` checks elapsed time.
4. `TelegramNotifier` is created.
5. `Sim24Login.login()` launches the browser and logs in.
6. The data-usage page is opened.
7. `DataChecker.get_usage()` returns used and total KB.
8. `main.py` converts those values to GB.
9. `DecisionEngine.should_book()` evaluates the threshold.
10. A Telegram status message is sent.
11. If booking is needed, `BookingModule.book_2gb_packet()` runs.
12. The Gist timestamp is updated.
13. The browser is closed.

### Failure behavior

The project generally handles failures by:

1. Printing diagnostic output.
2. Sending a Telegram message when possible.
3. Updating `last_run_ts` in many failure cases.
4. Letting the next scheduled run retry later.

## Testing And Validation Surfaces

### Manual local checks

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the manual helper:

```bash
python test_local.py --test telegram
python test_local.py --test login
python test_local.py --test data
python test_local.py --test full
```

### Automated tests

Automated orchestration tests live in `tests/test_main_workflow.py`.

At the time this README was updated, editor diagnostics were clean, but the local virtual environment used for inspection had broken pytest or pip metadata, so that environment could not execute pytest normally. That was an environment issue observed during inspection, not a confirmed repository code failure.

## Known Current Mismatches And Caveats

These points reflect the repository as it currently exists:

1. The GitHub Actions workflow runs every 30 minutes, not every 5 minutes.
2. The control bot accepts intervals smaller than 30 minutes, but cron still limits actual unattended runs to every 30 minutes.
3. The control bot's `/book` command requires workflow-dispatch permissions in addition to Gist access.
4. `config_manager.py` comments describe `last_run_ts` as the last successful run, but `main.py` updates it on several failure paths too.
5. `decision_engine.py` comments mention a default threshold of 1.5 GB, but the actual constructor default and main runtime behavior use 0.5 GB.
6. `test_local.py` still contains older comments referring to a `src` directory, while the repository is currently flat at the root.
7. Most sim24 automation is selector-driven and therefore sensitive to portal markup changes.
8. The repository currently has one automated test file, focused on orchestration rather than full browser behavior.

## Onboarding Summary

If you are new to this project, the most important mental model is:

1. `main.py` is the real production pipeline.
2. `scheduler_bot/bot.py` only controls timing and manual triggering.
3. The GitHub Gist is the shared persistence layer.
4. Playwright selectors are the fragile integration point.
5. Telegram is both the alerting channel and the human fallback for captcha.
6. The system always performs a fresh login instead of reusing sessions.
