"""
EOD paper-model divergence snapshot -> Telegram.

Reads each of the four paper models (standard / relaxed / very_relaxed / claude)
directly from their per-model SQLite DBs and reports the standard-vs-looser
divergence. This is the live signature of the Gate 10 Three Green Arrows
tightening (commit 1a4a505, 2026-06-11): the standard model now BLOCKS
weak-composite (< 0.50) 0-arrow value buys that the relaxed / very_relaxed
models (lower scaled cutoffs) still take. When a looser model holds or buys a
name the standard model does not, that gap is the confirmation the gate works.

Run by a Windows Scheduled Task at ~16:05 ET on trading days. No dashboard
dependency. Safe to run any time; get_account_summary writes an idempotent
equity snapshot just like the dashboard does.

Manual run:   python scripts/paper_eod_divergence.py
Preview only: python scripts/paper_eod_divergence.py --dry-run
"""
import os
import sys

# Make the project root importable and the relative data/ DB paths resolve,
# regardless of where the scheduled task launches from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MODEL_ORDER = ["standard", "relaxed", "very_relaxed", "claude"]
LABELS = {
    "standard":     "Standard",
    "relaxed":      "Relaxed -25%",
    "very_relaxed": "Very Relaxed -50%",
    "claude":       "Claude",
}
LOOSER = ["relaxed", "very_relaxed", "claude"]


def _is_today_et(ts_iso, today):
    """True if an ISO timestamp falls on `today` in ET. Defensive about tz."""
    if not ts_iso:
        return False
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except Exception:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))  # system writes UTC
    return dt.astimezone(ET).date() == today


def _collect():
    from paper.executor import PaperExecutor, PAPER_MODEL_CONFIGS
    from models.database import init_db
    from config import config

    _, Session = init_db(config.database.url, echo=False)
    today = datetime.now(ET).date()

    data = {}
    for key in MODEL_ORDER:
        cfg = PAPER_MODEL_CONFIGS.get(key)
        if not cfg:
            continue
        try:
            ex = PaperExecutor(main_db_session_factory=Session,
                               db_path=cfg["db"], stagegate_file=cfg["stagegate"])
            acct = ex.get_account_summary()
            trades = ex.get_recent_trades(limit=300)
        except Exception as e:
            data[key] = {"error": str(e)}
            continue
        held = {p["ticker"] for p in acct.get("positions", [])}
        buys_today = sorted({
            t["ticker"] for t in trades
            if (t.get("action") or "").upper() == "BUY"
            and _is_today_et(t.get("timestamp"), today)
        })
        data[key] = {
            "equity":        acct.get("total_equity"),
            "daily_pnl":     acct.get("daily_pnl"),
            "daily_pnl_pct": acct.get("daily_pnl_pct"),
            "life_pnl_pct":  acct.get("lifetime_pnl_pct"),
            "npos":          len(held),
            "held":          held,
            "buys_today":    buys_today,
        }
    return data, today


def _format(data, today):
    lines = [
        f"\U0001F4CA <b>NWO Paper Divergence — {today} EOD</b>",
        "<i>Gate 10 TGA tightening watch (commit 1a4a505)</i>",
        "",
        "<b>Model · equity · day · lifetime · #pos</b>",
    ]
    for key in MODEL_ORDER:
        d = data.get(key)
        if not d:
            continue
        if "error" in d:
            lines.append(f"• {LABELS[key]}: ⚠ {d['error'][:60]}")
            continue
        eq  = d["equity"] or 0
        dp  = d["daily_pnl"] or 0
        dpp = d["daily_pnl_pct"] or 0
        lp  = d["life_pnl_pct"] or 0
        sgn = "+" if dp >= 0 else ""
        lines.append(
            f"• <b>{LABELS[key]}</b>: ${eq:,.0f} · {sgn}{dp:,.0f} ({sgn}{dpp:.1f}%) · "
            f"{lp:+.1f}% life · {d['npos']} pos"
        )

    std = data.get("standard", {})
    std_err = "error" in std
    std_held = set() if std_err else std.get("held", set())
    std_buys = set() if std_err else set(std.get("buys_today", []))

    div_found = False

    hold_div = []
    for key in LOOSER:
        d = data.get(key)
        if not d or "error" in d:
            continue
        extra = sorted(d["held"] - std_held)
        if extra:
            div_found = True
            hold_div.append(f"• {LABELS[key]}: {', '.join(extra)}")
    if hold_div:
        lines.append("")
        lines.append("\U0001F531 <b>Held by looser models, NOT standard:</b>")
        lines.extend(hold_div)
        lines.append("<i>↳ weak-composite buys Gate 10 now blocks in standard</i>")

    buy_div = []
    for key in LOOSER:
        d = data.get(key)
        if not d or "error" in d:
            continue
        extra = sorted(set(d["buys_today"]) - std_buys)
        if extra:
            div_found = True
            buy_div.append(f"• {LABELS[key]}: {', '.join(extra)}")
    if buy_div:
        lines.append("")
        lines.append("\U0001F195 <b>New BUYs today not taken by standard:</b>")
        lines.extend(buy_div)

    if not div_found and not std_err:
        lines.append("")
        lines.append("✓ <b>No standard-vs-looser divergence yet.</b>")
        lines.append("<i>No weak-composite buys taken by looser models that standard "
                     "blocked — may take a few sessions of these value setups to appear.</i>")

    return "\n".join(lines), div_found


def main():
    dry_run = "--dry-run" in sys.argv
    data, today = _collect()
    msg, div_found = _format(data, today)
    if dry_run:
        print(msg)
        print(f"\n[paper-eod-divergence] DRY RUN (not sent) div_found={div_found}")
        return 0
    from monitor.telegram_bot import send_alert
    ok = send_alert(msg)
    print(f"[paper-eod-divergence] sent={ok} div_found={div_found} "
          f"at {datetime.now(ET):%Y-%m-%d %H:%M ET}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        try:
            from monitor.telegram_bot import send_alert
            send_alert(f"⚠️ <b>NWO Paper Divergence job failed</b>\n"
                       f"<code>{type(e).__name__}: {e}</code>")
        except Exception:
            pass
        raise
