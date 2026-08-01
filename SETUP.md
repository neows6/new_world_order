# New World Order (NWO) — Setup Guide

Hey Chris 👋 — this is the trading system. It ingests fundamentals + live prices,
runs them through a multi-layer signal/decision pipeline, and shows everything on a
web dashboard. **It ships in dry-run mode — it places NO real orders** until that's
explicitly turned off. Get it running and explore safely first.

You're on Windows and you have Claude Code, so you've got two paths. The easy one first.

---

## Option A — let Claude Code do it (recommended)

1. Unzip this folder somewhere, e.g. `C:\Users\<you>\new_world_order`.
2. Open the folder in Claude Code.
3. Run the installer once so the setup skill is available:
   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```
4. Then just type **`/nwo-setup`** in Claude Code. It walks you through keys, Schwab
   login, the smoke test, and launching the dashboard — interactively.

---

## Option B — do it yourself

### 1. Install
From inside the project folder:
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```
This checks Python 3.10+, creates a `.venv`, installs dependencies, creates your `.env`,
initializes the database, and builds the antivirus CA bundle.

If Python is missing: install **Python 3.10+** from https://python.org (tick **"Add to
PATH"**), then re-run.

### 2. Add YOUR API keys
The installer creates `.env` from `.env.example`. Open `.env` and fill in **your own**
keys (none of mine carry over):

| Key | Required? | Where to get it |
|-----|-----------|-----------------|
| `SCHWAB_API_KEY`, `SCHWAB_API_SECRET` | Yes | https://developer.schwab.com (create an app) |
| `SCHWAB_ACCOUNT_HASH` | After OAuth | Comes from your Schwab account after step 3 |
| `EDGAR_USER_AGENT` | Yes | Your real name + email, e.g. `Chris S chris@x.com` |
| `ANTHROPIC_API_KEY` | For AI features | https://console.anthropic.com |
| `GOOGLE_API_KEY` | For morning brief | https://aistudio.google.com |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Optional | Leave blank to skip alerts |
| `TIPRANKS_EMAIL`, `TIPRANKS_PASSWORD` | Optional | Leave blank to skip |

### 3. Authorize Schwab (one time)
```powershell
.\.venv\Scripts\python.exe get_token.py
```
A browser opens — log in and approve. A token is saved to `tokens/`. If you ever get
`400 refresh_token` errors later, just run this again.

### 4. Launch
```powershell
# Dashboard  ->  http://localhost:8765
.\.venv\Scripts\python.exe -m monitor.start_monitor

# Trading pipeline + scheduler (separate terminal)
.\.venv\Scripts\python.exe main.py
```

### 5. Sanity check anytime
```powershell
.\.venv\Scripts\python.exe diagnose.py
```

---

## Notes
- **Dry run is ON by default.** Real orders require setting `config.risk.dry_run = False`
  on purpose. Don't touch that until you fully trust the setup.
- The full architecture (6-layer pipeline, dashboard tabs, wheel strategy, etc.) is
  documented in **`CLAUDE.md`** at the project root — Claude Code reads it automatically.
- Antivirus (Norton/ESET/etc.) that inspects HTTPS can break Python's SSL. `install.ps1`
  handles this by building `data/ca_bundle_with_norton.pem`. If you don't run such AV,
  it's a no-op.

Ping me if anything snags. — 🧠 NWO
