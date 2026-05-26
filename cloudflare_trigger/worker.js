/**
 * Cloudflare Worker — sim24 GitHub Actions Trigger
 *
 * Fires the GitHub Actions workflow_dispatch event on a reliable hourly schedule,
 * replacing GitHub's built-in cron which can be delayed 3–5 hours during peak times.
 *
 * Required secret (set in Cloudflare dashboard → Worker → Settings → Variables):
 *   GITHUB_PAT  — Fine-grained PAT with Actions: Read & Write on this repo
 */

const OWNER    = "amirwebd3v";
const REPO     = "Automation-Booking-System";
const WORKFLOW = "check_data.yml";
const BRANCH   = "main";

export default {
  async scheduled(event, env, ctx) {
    const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;

    const resp = await fetch(url, {
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

    if (resp.ok) {
      // 204 No Content is the expected success response from GitHub
      console.log(`[OK] workflow_dispatch triggered (HTTP ${resp.status})`);
    } else {
      const body = await resp.text();
      console.error(`[ERROR] GitHub dispatch failed: HTTP ${resp.status} — ${body}`);
    }
  },
};
