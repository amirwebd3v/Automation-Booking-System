History rewrite runbook
======================

This document contains safe, repeatable commands to permanently remove sensitive
files (for example `.env` and `venv/`) from the repository history using
`git-filter-repo`. Read this fully before running.

Important preconditions
- Make a complete mirror backup of the repository.
- Rotate any exposed credentials (Telegram bot token, GitHub PAT) BEFORE or
  immediately AFTER performing the rewrite so leaked secrets cannot be abused.
- Coordinate with collaborators: history rewrite requires everyone to re-clone.

1) Create a mirror backup

```bash
git clone --mirror git@github.com:OWNER/REPO.git repo-mirror.git
cd repo-mirror.git
```

2) Remove paths from history (preferred: git-filter-repo)

Example: remove `.env` and `venv/` and `__pycache__/` from every commit:

```bash
git filter-repo --path .env --path venv --path __pycache__ --invert-paths
```

Notes:
- `--invert-paths` keeps everything except the listed paths.
- To replace secret strings (token values) instead of removing files, prepare
  a `replacements.txt` file and use `--replace-text replacements.txt`.

3) Cleanup and push

```bash
git reflog expire --expire=now --all
git gc --prune=now --aggressive
git push --force --all
git push --force --tags
```

4) Verification

Locally verify that `.env` does not appear in any commit:

```bash
git rev-list --all -- .env || echo ".env not found"
git grep -n "GITHUB_GIST_TOKEN" $(git rev-list --all) || echo "no matches"
```

5) Post-rewrite steps

- Notify collaborators to re-clone: `git clone <repo>`
- Revoke and reissue any rotated credentials if not already done.

Alternative: BFG

If `git-filter-repo` is unavailable, the BFG Repo-Cleaner can be used. See
https://rtyley.github.io/bfg-repo-cleaner/ for details.

Warnings
- Rewriting history is disruptive. Do not do this without team coordination.
- Back up before proceeding; keep the mirror until you are satisfied.
