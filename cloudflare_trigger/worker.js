/**
 * Cloudflare Worker — sim24 GitHub Actions Trigger
 *
 * Two entry points:
 *  1. Cron (scheduled) — fires workflow_dispatch every hour automatically.
 *  2. HTTP (fetch)     — Telegram webhook handler:
 *       /book        → triggers workflow_dispatch immediately
 *       /status      → reads and reports current Gist state (interval, last run, captcha)
 *       /activate    → force monitoring on (overrides auto-skip threshold)
 *       /pause       → force monitoring off (skips all hourly cron dispatches)
 *       plain text   → if captcha_pending in Gist, saves reply so GitHub Actions picks it up
 *
 * Secrets required (wrangler secret put <NAME>):
 *   GITHUB_PAT              — Fine-grained PAT, Actions: Read & Write on this repo
 *   GIST_TOKEN              — Classic PAT with the `gist` scope (same as GIST_TOKEN in GH Actions)
 *   GIST_ID                 — ID of the shared state Gist (same as GIST_ID in GH Actions)
 *   TELEGRAM_BOT_TOKEN      — Bot token (same token used by main.py / captcha_handler)
 *   TELEGRAM_CHAT_ID        — Your personal chat ID (authorized user)
 *   TELEGRAM_WEBHOOK_SECRET — Random string you choose; registered with setWebhook
 */

const OWNER         = "amirwebd3v";
const REPO          = "Automation-Booking-System";
const WORKFLOW      = "check_data.yml";
const BRANCH        = "main";
const GIST_FILENAME = "sim24_bot_config.json";
const BERLIN_TIMEZONE = "Europe/Berlin";
const MONITORING_SKIP_THRESHOLD_GB = 3.0;

// ── Entry points ──────────────────────────────────────────────────────────────

export default {
  /** Hourly cron: fires workflow_dispatch unless monitoring is paused or data is plentiful. */
  async scheduled(_event, env, _ctx) {
    const gist = await readGist(env);
    if (gist.ok) {
      const monitoringActive = gist.state.monitoring_active ?? null;
      const { last_used_kb, last_total_kb } = gist.state;

      if (monitoringActive === false) {
        console.log("[CRON] Skipping — monitoring is paused (/activate to resume).");
        return;
      }

      if (monitoringActive !== true && last_used_kb != null && last_total_kb != null) {
        const remainingGb = (last_total_kb - last_used_kb) / (1024 * 1024);
        if (remainingGb > MONITORING_SKIP_THRESHOLD_GB) {
          console.log(
            `[CRON] Skipping — ${remainingGb.toFixed(2)} GB remaining ` +
            `(auto mode, threshold: ${MONITORING_SKIP_THRESHOLD_GB} GB). ` +
            "Send /activate to override.",
          );
          return;
        }
      }
    } else {
      console.warn(`[CRON] Could not read Gist (${gist.error}); proceeding with dispatch.`);
    }

    const ok = await triggerGitHubWorkflow(env);
    if (ok) {
      console.log("[CRON] workflow_dispatch triggered successfully.");
    } else {
      console.error("[CRON] workflow_dispatch failed — see error above.");
    }
  },

  /** Telegram webhook: /book triggers, /status reports state, plain text submits captcha reply. */
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Only accept POST to /webhook — reject everything else
    if (request.method !== "POST" || url.pathname !== "/webhook") {
      return new Response("Not Found", { status: 404 });
    }

    // Verify every request is genuinely from Telegram via the shared secret
    const incomingSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!incomingSecret || incomingSecret !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad Request", { status: 400 });
    }

    const msg = update.message;
    if (!msg) return new Response("OK"); // ignore non-message updates (edits, etc.)

    // Only respond to the authorised chat — silently drop all others
    if (String(msg.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) {
      return new Response("OK");
    }

    const chatId = msg.chat.id;
    const text   = (msg.text || "").trim();

    // Extract command, stripping optional @botname suffix (e.g. /book@mybot → /book)
    const command = text.split(/[\s@]/)[0].toLowerCase();

    console.log(
      `[WEBHOOK] update=${update.update_id ?? "?"} chat=${chatId} ` +
      `command=${command || "(plain-text)"} text=${JSON.stringify(text)}`,
    );

    if (command === "/start") {
      await handleStart(env, chatId);
    } else if (command === "/book" || text === "📦 Book Now") {
      // Reply to Telegram immediately (5 s window), then trigger workflow asynchronously
      ctx.waitUntil(handleBook(env, chatId));
    } else if (command === "/status" || text === "📊 Status") {
      await handleStatus(env, chatId);
    } else if (command === "/activate" || text === "▶️ Activate") {
      ctx.waitUntil(handleActivate(env, chatId));
    } else if (command === "/pause" || text === "⏸️ Pause") {
      ctx.waitUntil(handlePause(env, chatId));
    } else if (text && !text.startsWith("/")) {
      // Plain text — could be a captcha reply; check Gist before acting
      ctx.waitUntil(handleCaptchaReply(env, chatId, text));
    } else if (command.startsWith("/")) {
      console.log(`[WEBHOOK] Ignoring unsupported command: ${command}`);
    }

    return new Response("OK");
  },
};

// ── Command handlers ───────────────────────────────────────────────────────────

async function handleStart(env, chatId) {
  console.log(`[START] Requested by chat ${chatId}`);
  await sendTelegram(
    env,
    chatId,
    "sim24 bot is online.\n\n" +
      "Available commands:\n" +
      "/status   - last run, next run, error, used data, monitoring mode\n" +
      "/book     - trigger the GitHub Actions workflow now\n" +
      "/activate - force monitoring on (overrides auto-skip threshold)\n" +
      "/pause    - suspend all hourly checks until /activate is sent",
  );
}

async function handleBook(env, chatId) {
  await sendTelegram(env, chatId, "⏳ Triggering GitHub Actions workflow...");
  const ok = await triggerGitHubWorkflow(env, true);
  await sendTelegram(
    env,
    chatId,
    ok
      ? "✅ Workflow triggered! Check GitHub Actions for progress."
      : "❌ Failed to trigger workflow. Check Cloudflare Worker logs.",
  );
}

async function handleStatus(env, chatId) {
  console.log(`[STATUS] Requested by chat ${chatId}`);
  const gist = await readGist(env);
  if (!gist.ok) {
    console.error(`[STATUS] ${gist.error}`);
    await sendTelegram(env, chatId, `❌ Status failed.\n${gist.error}`);
    return;
  }

  console.log(`[STATUS] Gist read succeeded for chat ${chatId}`);
  await sendTelegram(env, chatId, buildStatusMessage(gist.state));
}

function buildStatusMessage(state) {
  let lastRunText = "Never";
  if (state.last_run_ts && state.last_run_ts > 0) {
    lastRunText = formatBerlinTimestamp(state.last_run_ts);
  }

  const nextRunMinutes = minutesUntilNextRun();
  const lastError = formatLastError(state);
  const usedData = formatDataVolume(state.last_used_kb);
  const totalData = formatDataVolume(state.last_total_kb);
  const monitoring = formatMonitoringStatus(state);

  const message =
    `📊 Bot Status\n\n` +
    `🕑 Last Run: ${lastRunText}\n` +
    `⏱️ Next Run In: ${nextRunMinutes} min\n` +
    `❌ Error: ${lastError}\n` +
    `📈 Used Data: ${usedData}\n` +
    `📦 Total Data: ${totalData}\n` +
    `🔁 Monitoring: ${monitoring}`;

  return message;
}

function minutesUntilNextRun(now = new Date()) {
  const nextRun = new Date(now);
  nextRun.setUTCMinutes(0, 0, 0);
  nextRun.setUTCHours(nextRun.getUTCHours() + 1);
  const remainingMinutes = Math.ceil((nextRun.getTime() - now.getTime()) / 60000);
  return Math.max(1, remainingMinutes);
}

function formatDataVolume(kb) {
  const value = Number(kb);
  if (!Number.isFinite(value)) {
    return "Not yet recorded";
  }
  return `${(value / (1024 * 1024)).toFixed(2)} GB`;
}

function formatBerlinTimestamp(tsSeconds) {
  const date = new Date(Number(tsSeconds) * 1000);
  const formatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: BERLIN_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZoneName: "short",
  });
  const parts = Object.fromEntries(
    formatter
      .formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );

  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} ${parts.timeZoneName} (${BERLIN_TIMEZONE})`;
}

function formatLastError(state) {
  if (state.last_run_ok === false) {
    return state.last_run_error || "Last run did not complete successfully.";
  }
  return "None";
}

function formatMonitoringStatus(state) {
  const ma = state.monitoring_active ?? null;
  if (ma === true)  return "Forced ON (/pause to stop)";
  if (ma === false) return "Paused (/activate to resume)";
  // Auto mode — derive live status from last recorded usage
  const { last_used_kb, last_total_kb } = state;
  if (last_used_kb != null && last_total_kb != null) {
    const remainingGb = (last_total_kb - last_used_kb) / (1024 * 1024);
    const status = remainingGb <= MONITORING_SKIP_THRESHOLD_GB ? "active" : "idle";
    return `Auto — ${remainingGb.toFixed(2)} GB remaining (${status})`;
  }
  return "Auto";
}

async function handleCaptchaReply(env, chatId, text) {
  const gist = await readGist(env);
  if (!gist.ok) {
    console.error(`[CAPTCHA] ${gist.error}`);
    return;
  }

  const state = gist.state;
  if (!state.captcha_pending) return; // nothing pending — ignore

  state.captcha_reply   = text;
  state.captcha_pending = false;
  const saved = await writeGist(env, state);

  if (saved) {
    await sendTelegram(
      env,
      chatId,
      `✅ *Captcha code submitted:* \`${text}\`\nThe booking workflow will pick it up now.`,
      "Markdown",
    );
  } else {
    await sendTelegram(
      env,
      chatId,
      "❌ Failed to save captcha reply to Gist. Check Worker logs.",
    );
  }
}

async function handleActivate(env, chatId) {
  const gist = await readGist(env);
  if (!gist.ok) {
    await sendTelegram(env, chatId, `❌ Could not read Gist: ${gist.error}`);
    return;
  }
  const state = gist.state;
  state.monitoring_active = true;
  const saved = await writeGist(env, state);
  await sendTelegram(
    env,
    chatId,
    saved
      ? "▶️ *Monitoring activated.*\nHourly checks will run regardless of remaining data.\nSend /pause to return to auto mode."
      : "❌ Failed to update Gist. Check Worker logs.",
    "Markdown",
  );
}

async function handlePause(env, chatId) {
  const gist = await readGist(env);
  if (!gist.ok) {
    await sendTelegram(env, chatId, `❌ Could not read Gist: ${gist.error}`);
    return;
  }
  const state = gist.state;
  state.monitoring_active = false;
  const saved = await writeGist(env, state);
  await sendTelegram(
    env,
    chatId,
    saved
      ? "⏸️ *Monitoring paused.*\nNo hourly checks will run until you send /activate."
      : "❌ Failed to update Gist. Check Worker logs.",
    "Markdown",
  );
}

// ── GitHub helpers ─────────────────────────────────────────────────────────────

function getGistToken(env) {
  return env.GITHUB_GIST_TOKEN || env.GIST_TOKEN || "";
}

function getGistId(env) {
  return env.GITHUB_GIST_ID || env.GIST_ID || "";
}

function getGitHubPat(env) {
  return env.GITHUB_PAT || getGistToken(env);
}

async function triggerGitHubWorkflow(env, forceReport = false) {
  const githubPat = getGitHubPat(env);
  if (!githubPat) {
    console.error("[ERROR] Missing GITHUB_PAT (or fallback GIST token with workflow scope).");
    return false;
  }

  const apiUrl = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const resp = await fetch(apiUrl, {
    method: "POST",
    headers: {
      "Authorization":        `Bearer ${githubPat}`,
      "Accept":               "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent":           `${OWNER}/${REPO}-cf-trigger`,
      "Content-Type":         "application/json",
    },
    body: JSON.stringify({ ref: BRANCH, inputs: forceReport ? { force_report: "true" } : {} }),
  });

  if (!resp.ok) {
    const body = await resp.text();
    console.error(`[ERROR] GitHub dispatch failed: HTTP ${resp.status} — ${body}`);
  }
  return resp.ok; // GitHub returns 204 No Content on success
}

// ── Gist helpers ───────────────────────────────────────────────────────────────

function gistHeaders(env) {
  const gistToken = getGistToken(env);
  return {
    "Authorization":        `Bearer ${gistToken}`,
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent":           `${OWNER}/${REPO}-cf-trigger`,
  };
}

async function readGist(env) {
  const gistId = getGistId(env);
  const gistToken = getGistToken(env);

  if (!gistToken) {
    return { ok: false, error: "Cloudflare secret GIST_TOKEN (or GITHUB_GIST_TOKEN) is missing." };
  }
  if (!gistId) {
    return { ok: false, error: "Cloudflare secret GIST_ID (or GITHUB_GIST_ID) is missing." };
  }

  try {
    const resp = await fetch(`https://api.github.com/gists/${gistId}`, {
      headers: gistHeaders(env),
    });
    if (!resp.ok) {
      const body = await resp.text();
      console.error(`[ERROR] Gist read failed: HTTP ${resp.status} — ${body}`);

      if (resp.status === 401 || resp.status === 403) {
        return { ok: false, error: "Gist token is invalid or missing the gist scope." };
      }
      if (resp.status === 404) {
        return { ok: false, error: "Gist not found. Check GIST_ID / GITHUB_GIST_ID and token access." };
      }
      return { ok: false, error: `GitHub Gist API returned HTTP ${resp.status}.` };
    }
    const data    = await resp.json();
    const content = data.files?.[GIST_FILENAME]?.content;
    if (!content) {
      return { ok: false, error: `Gist file ${GIST_FILENAME} was not found.` };
    }

    try {
      return { ok: true, state: JSON.parse(content) };
    } catch (e) {
      console.error(`[ERROR] Gist JSON parse failed: ${e}`);
      return { ok: false, error: `Gist file ${GIST_FILENAME} does not contain valid JSON.` };
    }
  } catch (e) {
    console.error(`[ERROR] Gist read exception: ${e}`);
    return { ok: false, error: "Network error while reading the Gist. Check Worker logs." };
  }
}

async function writeGist(env, state) {
  const gistId = getGistId(env);
  const gistToken = getGistToken(env);

  if (!gistToken || !gistId) {
    console.error("[ERROR] Cannot write Gist: missing GIST token or GIST id secret.");
    return false;
  }

  try {
    const resp = await fetch(`https://api.github.com/gists/${gistId}`, {
      method: "PATCH",
      headers: { ...gistHeaders(env), "Content-Type": "application/json" },
      body: JSON.stringify({
        files: { [GIST_FILENAME]: { content: JSON.stringify(state, null, 2) } },
      }),
    });
    if (!resp.ok) {
      const body = await resp.text();
      console.error(`[ERROR] Gist write failed: HTTP ${resp.status} — ${body}`);
    }
    return resp.ok;
  } catch (e) {
    console.error(`[ERROR] Gist write exception: ${e}`);
    return false;
  }
}

// ── Telegram helper ────────────────────────────────────────────────────────────

async function sendTelegram(env, chatId, text, parseMode = null) {
  try {
    const payload = { chat_id: chatId, text };
    if (parseMode) {
      payload.parse_mode = parseMode;
    }

    const resp = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    if (!resp.ok) {
      const body = await resp.text();
      console.error(`[ERROR] Telegram sendMessage failed: HTTP ${resp.status} — ${body}`);
      return false;
    }
    console.log(`[TG] sendMessage ok chat=${chatId}`);
    return true;
  } catch (e) {
    console.error(`[ERROR] Telegram sendMessage exception: ${e}`);
    return false;
  }
}
