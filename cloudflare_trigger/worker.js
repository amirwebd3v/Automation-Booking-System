/**
 * Cloudflare Worker — sim24 GitHub Actions Trigger
 *
 * Two entry points:
 *  1. Cron (scheduled) — fires workflow_dispatch every hour automatically.
 *  2. HTTP (fetch)     — Telegram webhook handler:
 *       /book        → triggers workflow_dispatch immediately
 *       /status      → reads and reports current Gist state (interval, last run, captcha)
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

// ── Entry points ──────────────────────────────────────────────────────────────

export default {
  /** Hourly cron: reliable replacement for GitHub's delayed schedule trigger. */
  async scheduled(_event, env, _ctx) {
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

    if (command === "/book") {
      // Reply to Telegram immediately (5 s window), then trigger workflow asynchronously
      ctx.waitUntil(handleBook(env, chatId));
    } else if (command === "/status") {
      ctx.waitUntil(handleStatus(env, chatId));
    } else if (text && !text.startsWith("/")) {
      // Plain text — could be a captcha reply; check Gist before acting
      ctx.waitUntil(handleCaptchaReply(env, chatId, text));
    }

    return new Response("OK");
  },
};

// ── Command handlers ───────────────────────────────────────────────────────────

async function handleBook(env, chatId) {
  await sendTelegram(env, chatId, "⏳ Triggering GitHub Actions workflow...");
  const ok = await triggerGitHubWorkflow(env);
  await sendTelegram(
    env,
    chatId,
    ok
      ? "✅ Workflow triggered! Check GitHub Actions for progress."
      : "❌ Failed to trigger workflow. Check Cloudflare Worker logs.",
  );
}

async function handleStatus(env, chatId) {
  const state = await readGist(env);
  if (!state) {
    await sendTelegram(env, chatId, "❌ Could not read status from Gist. Check Worker logs.");
    return;
  }

  const intervalMin = state.interval_minutes ?? "—";

  let lastRunText = "Never";
  if (state.last_run_ts && state.last_run_ts > 0) {
    const lastRunDate = new Date(state.last_run_ts * 1000);
    lastRunText = lastRunDate.toUTCString();
  }

  const captchaStatus = state.captcha_pending ? "⚠️ Pending" : "✅ None";

  const message =
    `📊 *Bot Status*\n\n` +
    `🕐 *Check Interval:* \`${intervalMin} min\`\n` +
    `🕑 *Last Run:* \`${lastRunText}\`\n` +
    `🔐 *Captcha:* ${captchaStatus}`;

  await sendTelegram(env, chatId, message);
}

async function handleCaptchaReply(env, chatId, text) {
  const state = await readGist(env);
  if (!state || !state.captcha_pending) return; // nothing pending — ignore

  state.captcha_reply   = text;
  state.captcha_pending = false;
  const saved = await writeGist(env, state);

  if (saved) {
    await sendTelegram(
      env,
      chatId,
      `✅ *Captcha code submitted:* \`${text}\`\nThe booking workflow will pick it up now.`,
    );
  } else {
    await sendTelegram(
      env,
      chatId,
      "❌ Failed to save captcha reply to Gist. Check Worker logs.",
    );
  }
}

// ── GitHub helpers ─────────────────────────────────────────────────────────────

async function triggerGitHubWorkflow(env) {
  const apiUrl = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const resp = await fetch(apiUrl, {
    method: "POST",
    headers: {
      "Authorization":        `Bearer ${env.GITHUB_PAT}`,
      "Accept":               "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent":           `${OWNER}/${REPO}-cf-trigger`,
      "Content-Type":         "application/json",
    },
    body: JSON.stringify({ ref: BRANCH }),
  });

  if (!resp.ok) {
    const body = await resp.text();
    console.error(`[ERROR] GitHub dispatch failed: HTTP ${resp.status} — ${body}`);
  }
  return resp.ok; // GitHub returns 204 No Content on success
}

// ── Gist helpers ───────────────────────────────────────────────────────────────

function gistHeaders(env) {
  return {
    "Authorization":        `Bearer ${env.GIST_TOKEN}`,
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent":           `${OWNER}/${REPO}-cf-trigger`,
  };
}

async function readGist(env) {
  try {
    const resp = await fetch(`https://api.github.com/gists/${env.GIST_ID}`, {
      headers: gistHeaders(env),
    });
    if (!resp.ok) {
      console.error(`[ERROR] Gist read failed: HTTP ${resp.status}`);
      return null;
    }
    const data    = await resp.json();
    const content = data.files?.[GIST_FILENAME]?.content;
    return content ? JSON.parse(content) : null;
  } catch (e) {
    console.error(`[ERROR] Gist read exception: ${e}`);
    return null;
  }
}

async function writeGist(env, state) {
  try {
    const resp = await fetch(`https://api.github.com/gists/${env.GIST_ID}`, {
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

async function sendTelegram(env, chatId, text) {
  try {
    const resp = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, text, parse_mode: "Markdown" }),
      },
    );
    if (!resp.ok) {
      const body = await resp.text();
      console.error(`[ERROR] Telegram sendMessage failed: HTTP ${resp.status} — ${body}`);
    }
  } catch (e) {
    console.error(`[ERROR] Telegram sendMessage exception: ${e}`);
  }
}
