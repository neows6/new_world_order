---
name: nwo-setup
description: First-time setup for the New World Order (NWO) trading system on a new machine. Walks the user through installing dependencies, entering their own API keys, initializing the database, authorizing Schwab OAuth, and launching the dashboard. Use when a user has just unzipped/cloned NWO and needs to get it running, or asks "how do I set up NWO / this trading system".
---

# NWO first-time setup

You are helping someone set up the **New World Order (NWO)** trading system on their own
machine for the first time. They received it as a zip from the original author. Your job is
to get them from "unzipped folder" to "dashboard running" without leaking or assuming any
of the author's private credentials — every API key must be their own.

Work through these phases **in order**. After each phase, confirm success before moving on.
Do not modify project source files unless a step explicitly says so. `config.risk.dry_run`
defaults to `True` — never change it during setup.

## Phase 0 — Locate the project
1. Confirm the current directory is the NWO project root (it contains `main.py`,
   `requirements.txt`, `install.ps1`, and a `monitor/` folder). If not, ask the user for the
   path and `cd` there.

## Phase 1 — Run the installer
1. Run: `powershell -ExecutionPolicy Bypass -File install.ps1`
2. This creates `.venv`, installs dependencies, copies `.env.example` to `.env`, initializes
   the database, builds the AV CA bundle, and installs this skill. Watch its output.
3. If it fails on Python, tell them to install Python 3.10+ from python.org (with "Add to
   PATH" checked) and re-run.
4. From here on, the Python interpreter is `.\.venv\Scripts\python.exe`.

## Phase 2 — API keys (their own, required)
Open `.env` and confirm each key is filled in with the user's **own** value. Explain where to
get each one — do NOT invent values:
- `SCHWAB_API_KEY` / `SCHWAB_API_SECRET` — create an app at https://developer.schwab.com
- `SCHWAB_ACCOUNT_HASH` — obtained after the first OAuth (Phase 3); can be left for now
- `EDGAR_USER_AGENT` — their real name + email, e.g. `Chris Smith chris@example.com`
  (SEC rejects requests without this)
- `ANTHROPIC_API_KEY` — https://console.anthropic.com (Claude Haiku: sentinel, validator)
- `GOOGLE_API_KEY` — https://aistudio.google.com (Gemini Flash: morning brief)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional; leave blank to disable alerts
- `TIPRANKS_EMAIL` / `TIPRANKS_PASSWORD` — optional; leave blank to disable

Do not proceed to Schwab OAuth until at least the Schwab + EDGAR keys are present.

## Phase 3 — Schwab OAuth
1. Run: `.\.venv\Scripts\python.exe get_token.py`
2. This opens a browser for Schwab login/consent. The user completes it manually.
3. On success a token is written to `tokens/schwab_token.json`. If they now have their
   account hash, add it to `.env` as `SCHWAB_ACCOUNT_HASH`.
4. If they later see `400 refresh_token` errors, re-run `get_token.py`.

## Phase 4 — Smoke test
1. Run `.\.venv\Scripts\python.exe diagnose.py` to check DB health and data flow.
2. If a `test_connection.py` exists, run it to verify the Schwab token, a live quote, and
   account value.
3. Report any failures with the exact error; stop and fix before launching.

## Phase 5 — Launch
1. Dashboard: `.\.venv\Scripts\python.exe -m monitor.start_monitor` (background), then open
   http://localhost:8765
2. Trading pipeline: `.\.venv\Scripts\python.exe main.py` (background). Confirm it prints
   "Scheduler started".
3. Confirm `dry_run=True` is active before declaring setup complete — no real orders should
   be possible.

## Final report
Summarize for the user: which keys are configured, dry_run status, watchlist tickers, the
dashboard URL, and whether Telegram is connected. Point them at `SETUP.md` and the project
`CLAUDE.md` for deeper docs, and mention the `/nwo` command starts everything once set up.
