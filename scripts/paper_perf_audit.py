"""
scripts/paper_perf_audit.py — end-to-end performance audit of the 4 paper models.

Consolidates the one-off forensics written during the 2026-09-05 investigation
into a single re-runnable report. Run it at each evaluation checkpoint.

Sections:
  1. DATA HEALTH   duplicate price_history rows + ATR deflation check
  2. SCOREBOARD    per-model return, drawdown, deployment vs SPY/QQQ/RSP/IWM
  3. ROUND TRIPS   FIFO-matched closed trades, win rate, expectancy, exit reasons
  4. CHURN         repeat entries into names that just stopped out
  5. STOP FORENSICS depth histogram + did stopped names recover?
  6. COUNTERFACTUALS what the same entries would have made under other exit rules

Usage:
    python scripts/paper_perf_audit.py                    # since each model's reset
    python scripts/paper_perf_audit.py --start 2026-06-03 --end 2026-09-06
"""

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np

DBS = {
    "standard":     "data/paper_trading.db",
    "relaxed":      "data/paper_relaxed.db",
    "very_relaxed": "data/paper_very_relaxed.db",
    "claude":       "data/paper_claude.db",
}
MAIN_DB = "data/schwab_trader.db"
TRAIL_PCT = 0.08
BENCHMARKS = ["SPY", "QQQ", "RSP", "IWM"]


def _d(s):
    return dt.datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def _wilder(h, l, c, p):
    tr = [h[0] - l[0]]
    for i in range(1, len(h)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    if len(tr) < p:
        return None
    atr = [0.0] * len(tr)
    atr[p - 1] = sum(tr[:p]) / p
    for i in range(p, len(tr)):
        atr[i] = (atr[i - 1] * (p - 1) + tr[i]) / p
    return atr[-1]


def round_trips(path):
    """FIFO-match BUYs against SELLs into closed round trips."""
    c = sqlite3.connect(path)
    tr = c.execute("SELECT ticker,action,qty,price,timestamp,notes FROM paper_trades "
                   "ORDER BY timestamp, id").fetchall()
    c.close()
    lots, out = defaultdict(list), []
    for tk, act, qty, px, ts, notes in tr:
        if act.upper() == "BUY":
            lots[tk].append([qty, px, ts])
            continue
        rem = qty
        while rem > 1e-9 and lots[tk]:
            lot = lots[tk][0]
            take = min(rem, lot[0])
            out.append({"ticker": tk, "qty": take, "entry": lot[1], "exit": px,
                        "pnl": (px - lot[1]) * take, "pct": 100 * (px - lot[1]) / lot[1],
                        "opened": lot[2], "closed": ts, "notes": (notes or "").upper()})
            lot[0] -= take
            rem -= take
            if lot[0] <= 1e-9:
                lots[tk].pop(0)
    return out


def section_data_health():
    print("#" * 80)
    print("# 1. DATA HEALTH")
    print("#" * 80)
    c = sqlite3.connect(MAIN_DB)
    dup = c.execute("""SELECT COUNT(*) FROM (
        SELECT co.ticker, DATE(ph.date) d, COUNT(*) n
        FROM price_history ph JOIN companies co ON co.id = ph.company_id
        GROUP BY co.ticker, DATE(ph.date) HAVING n > 1)""").fetchone()[0]
    c.close()
    print("  duplicated (ticker, date) pairs in price_history: %d" % dup)
    if dup:
        print("  NOTE: utils/price_data.dedupe_bars() collapses these at read time.")
        print("        A non-zero count here is expected — the rows are still on disk.")
    print("  Verifying ATR is not deflated (deduped ATR10 vs true ATR14):")
    try:
        import yfinance as yf
        from config import config
        from models.database import init_db, Company, PriceHistory
        from utils.price_data import ohlcv_arrays
        _, Session = init_db(config.database.url, echo=False)
        tks = ["WFC", "MSFT", "V", "NVDA", "BLK", "GOOGL", "COST"]
        yd = yf.download(tks, start="2026-01-01", progress=False, auto_adjust=True)
        YH, YL, YC = yd["High"], yd["Low"], yd["Close"]
        ratios = []
        for t in tks:
            with Session() as s:
                co = s.query(Company).filter_by(ticker=t).first()
                if not co:
                    continue
                recs = (s.query(PriceHistory).filter_by(company_id=co.id)
                        .order_by(PriceHistory.date).all())
            b = ohlcv_arrays(recs)
            new = _wilder(b["highs"], b["lows"], b["closes"], 10)
            true14 = _wilder(YH[t].dropna().tolist(), YL[t].dropna().tolist(),
                             YC[t].dropna().tolist(), 14)
            if new and true14:
                ratios.append(new / true14)
        if ratios:
            med = float(np.median(ratios))
            flag = "OK" if med > 0.80 else "*** DEFLATED — dedup may have regressed ***"
            print("    median deduped-ATR / true-ATR14 = %.2fx   %s" % (med, flag))
    except Exception as e:
        print("    (skipped: %s)" % e)


def section_scoreboard(start, end):
    print()
    print("#" * 80)
    print("# 2. SCOREBOARD")
    print("#" * 80)
    try:
        import yfinance as yf
        bm = yf.download(BENCHMARKS, start=start, end=end, progress=False, auto_adjust=True)
        cl = bm["Close"] if "Close" in bm else bm
        for t in BENCHMARKS:
            s = cl[t].dropna()
            if len(s) >= 2:
                print("  %-5s %+6.2f%%   (%s %.2f -> %s %.2f)" % (
                    t, 100 * (s.iloc[-1] / s.iloc[0] - 1),
                    s.index[0].date(), s.iloc[0], s.index[-1].date(), s.iloc[-1]))
    except Exception as e:
        print("  benchmark fetch failed: %s" % e)

    print()
    print("  %-14s %10s %9s %9s %10s %7s %7s" % (
        "model", "equity", "return", "maxDD", "avgDepl", "trades", "open"))
    for name, path in DBS.items():
        c = sqlite3.connect(path)
        snaps = c.execute("SELECT snap_date,total_equity,positions_value FROM "
                          "paper_equity_snapshots ORDER BY snap_date").fetchall()
        ntr = c.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        npos = c.execute("SELECT COUNT(*) FROM paper_positions WHERE qty>0").fetchone()[0]
        start_bal = c.execute("SELECT starting_balance FROM paper_account").fetchone()[0]
        c.close()
        if not snaps:
            continue
        eq = [s[1] for s in snaps]
        peak, mdd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            mdd = min(mdd, 100 * (v - peak) / peak)
        depl = [100 * s[2] / s[1] for s in snaps if s[1]]
        print("  %-14s %10.2f %+8.2f%% %8.2f%% %9.1f%% %7d %7d" % (
            name, eq[-1], 100 * (eq[-1] / start_bal - 1), mdd,
            sum(depl) / len(depl), ntr, npos))


def section_round_trips(trips_by_model):
    print()
    print("#" * 80)
    print("# 3. ROUND TRIPS")
    print("#" * 80)
    for name, trips in trips_by_model.items():
        if not trips:
            print("  %-14s no closed round trips" % name)
            continue
        wins = [x for x in trips if x["pnl"] > 0]
        losses = [x for x in trips if x["pnl"] <= 0]
        aw = sum(x["pnl"] for x in wins) / len(wins) if wins else 0
        al = sum(x["pnl"] for x in losses) / len(losses) if losses else 0
        exp = (len(wins) / len(trips)) * aw + (len(losses) / len(trips)) * al
        print("  %-14s n=%-3d win=%3.0f%%  realized=%+9.2f  avgW=%+8.2f avgL=%+8.2f  expectancy=%+7.2f"
              % (name, len(trips), 100 * len(wins) / len(trips),
                 sum(x["pnl"] for x in trips), aw, al, exp))
        reasons = defaultdict(lambda: [0, 0.0])
        for x in trips:
            k = ("STOP" if "STOP" in x["notes"] else
                 "TAKE_PROFIT" if "TAKE PROFIT" in x["notes"] else
                 "AI_EXIT" if "AI" in x["notes"] else "other")
            reasons[k][0] += 1
            reasons[k][1] += x["pnl"]
        for k, v in sorted(reasons.items(), key=lambda z: z[1][1]):
            print("        %-14s n=%-3d pnl=%+10.2f" % (k, v[0], v[1]))


def section_churn():
    print()
    print("#" * 80)
    print("# 4. CHURN (re-entry into names that just stopped out)")
    print("#" * 80)
    for name, path in DBS.items():
        c = sqlite3.connect(path)
        tr = c.execute("SELECT ticker,action,notes FROM paper_trades ORDER BY timestamp,id").fetchall()
        c.close()
        per = defaultdict(lambda: {"buys": 0, "stops": 0})
        for tk, act, notes in tr:
            if act.upper() == "BUY":
                per[tk]["buys"] += 1
            elif "STOP" in (notes or "").upper():
                per[tk]["stops"] += 1
        tot = sum(v["buys"] for v in per.values())
        rep = sum(v["buys"] - 1 for v in per.values() if v["buys"] > 1)
        print("  %-14s %d/%d buys are re-entries (%.0f%%)" % (
            name, rep, tot, 100 * rep / tot if tot else 0))
        for k, v in sorted(per.items(), key=lambda z: -z[1]["buys"])[:5]:
            if v["buys"] > 1:
                print("        %-6s buys=%d stops=%d" % (k, v["buys"], v["stops"]))


def section_stops_and_counterfactuals(trips_by_model, end):
    allt = [x for v in trips_by_model.values() for x in v]
    stops = [x for x in allt if "STOP" in x["notes"]]
    tps = [x for x in allt if "TAKE PROFIT" in x["notes"]]
    print()
    print("#" * 80)
    print("# 5. STOP FORENSICS")
    print("#" * 80)
    if not allt:
        print("  no round trips yet")
        return
    print("  %d round trips: %d stop-outs (%.0f%%), %d take-profits (%.0f%%)" % (
        len(allt), len(stops), 100 * len(stops) / len(allt),
        len(tps), 100 * len(tps) / len(allt)))
    if stops:
        sp = sorted(x["pct"] for x in stops)
        print("  stop depth: median %.2f%%  mean %.2f%%" % (sp[len(sp) // 2], sum(sp) / len(sp)))
        hist = defaultdict(int)
        for x in stops:
            b = int(abs(x["pct"]))
            hist["%d-%d%%" % (b, b + 1)] += 1
        for k in sorted(hist, key=lambda z: int(z.split("-")[0])):
            print("    %-8s %s %d" % (k, "#" * hist[k], hist[k]))

    try:
        import yfinance as yf
        tks = sorted({x["ticker"] for x in allt})
        px = yf.download(tks, start="2026-05-25", end=end, progress=False, auto_adjust=True)
        close = px["Close"] if "Close" in px else px

        rec = {5: [], 10: [], 20: []}
        back = []
        for x in stops:
            if x["ticker"] not in close.columns:
                continue
            s = close[x["ticker"]].dropna()
            fut = s[s.index.date > _d(x["closed"])]
            row = dict(x)
            for n in (5, 10, 20):
                if len(fut) >= n:
                    r = 100 * (fut.iloc[n - 1] / x["exit"] - 1)
                    rec[n].append(r)
                    row["d%d" % n] = r
            if "d20" in row:
                back.append(row)
        print()
        print("  Were the stops premature?")
        for n in (5, 10, 20):
            v = rec[n]
            if v:
                print("    +%2dd after stop: mean %+6.2f%%  %d/%d (%.0f%%) above the exit price" % (
                    n, sum(v) / len(v), sum(1 for z in v if z > 0), len(v),
                    100 * sum(1 for z in v if z > 0) / len(v)))
        if back:
            ae = [d for d in back if d["exit"] * (1 + d["d20"] / 100) > d["entry"]]
            print("    %d/%d (%.0f%%) were back ABOVE the original entry within 20 sessions" % (
                len(ae), len(back), 100 * len(ae) / len(back)))

        print()
        print("#" * 80)
        print("# 6. COUNTERFACTUALS")
        print("#" * 80)
        print("  A) hard TP1 sell replaced by an %.0f%% trailing stop:" % (100 * TRAIL_PCT))
        grand = 0.0
        for name, trips in trips_by_model.items():
            act = cf = 0.0
            for x in [t for t in trips if "TAKE PROFIT" in t["notes"]]:
                if x["ticker"] not in close.columns:
                    continue
                s = close[x["ticker"]].dropna()
                fut = s[s.index.date >= _d(x["closed"])]
                if len(fut) < 2:
                    continue
                peak, ex = x["exit"], None
                for _, p in fut.items():
                    peak = max(peak, float(p))
                    trail = max(peak * (1 - TRAIL_PCT), x["entry"])
                    if float(p) <= trail:
                        ex = trail
                        break
                ex = ex if ex is not None else float(fut.iloc[-1])
                act += x["pnl"]
                cf += (ex - x["entry"]) * x["qty"]
            grand += cf - act
            print("     %-14s actual %+9.2f -> trailing %+9.2f  (delta %+9.2f)" % (name, act, cf, cf - act))
        print("     TOTAL delta: %+.2f" % grand)

        print("  B) same entries simply held to the end of the window:")
        for name, trips in trips_by_model.items():
            act = sum(x["pnl"] for x in trips)
            bh = 0.0
            for x in trips:
                if x["ticker"] not in close.columns:
                    continue
                bh += (float(close[x["ticker"]].dropna().iloc[-1]) - x["entry"]) * x["qty"]
            print("     %-14s realized %+9.2f -> held %+9.2f  (delta %+9.2f)" % (name, act, bh, bh - act))
    except Exception as e:
        print("  (price-based sections skipped: %s)" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-06-03")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    args = ap.parse_args()

    trips_by_model = {n: round_trips(p) for n, p in DBS.items()}

    section_data_health()
    section_scoreboard(args.start, args.end)
    section_round_trips(trips_by_model)
    section_churn()
    section_stops_and_counterfactuals(trips_by_model, args.end)


if __name__ == "__main__":
    main()
