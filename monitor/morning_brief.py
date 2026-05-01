"""
monitor/morning_brief.py — Morning Market Brief Generator.

Pulls data from free sources, synthesizes with Claude, caches to disk.
Served at /morning-brief in the dashboard.

Data sources (all free, no paid API keys required except Anthropic):
  - yfinance          : indices, futures, commodities, crypto
  - feedparser        : Reuters / AP / MarketWatch RSS headlines
  - ApeWisdom API     : WSB trending tickers (no auth)
  - QuiverQuant API   : Congressional trades (free tier)
  - Anthropic Claude  : Narrative synthesis (requires ANTHROPIC_API_KEY)

Schedule: generated once at ~7am ET on trading days, cached until stale.
Falls back to raw data display if Claude key is not set.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import feedparser
import requests
import yfinance as yf
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent


# ── Ticker symbols ────────────────────────────────────────────
INDICES = {
    "S&P 500":  "^GSPC",
    "Dow":      "^DJI",
    "Nasdaq":   "^IXIC",
    "VIX":      "^VIX",
    "Russell":  "^RUT",
}
FUTURES = {
    "ES (S&P)": "ES=F",
    "NQ (Nas)": "NQ=F",
    "YM (Dow)": "YM=F",
    "RTY":      "RTY=F",
}
GLOBAL = {
    "DAX":    "^GDAXI",
    "FTSE":   "^FTSE",
    "Nikkei": "^N225",
    "Hang Seng": "^HSI",
    "CAC 40": "^FCHI",
}
COMMODITIES = {
    "Gold":    "GC=F",
    "Silver":  "SI=F",
    "Oil WTI": "CL=F",
    "Oil Brent":"BZ=F",
    "Nat Gas": "NG=F",
    "Copper":  "HG=F",
    "Wheat":   "ZW=F",
}
CRYPTO = {
    "Bitcoin":  "BTC-USD",
    "Ethereum": "ETH-USD",
    "Solana":   "SOL-USD",
    "XRP":      "XRP-USD",
}
RATES = {
    "2yr Yield":  "^IRX",
    "10yr Yield": "^TNX",
    "30yr Yield": "^TYX",
    "USD Index":  "DX-Y.NYB",
}

NEWS_FEEDS = [
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters Markets",  "https://feeds.reuters.com/reuters/UKmarkets"),
    ("AP Business",      "https://feeds.apnews.com/apnews/business"),
    ("MarketWatch",      "https://feeds.marketwatch.com/marketwatch/topstories"),
    ("Seeking Alpha",    "https://seekingalpha.com/market_currents.xml"),
    ("Yahoo Finance",    "https://finance.yahoo.com/news/rssindex"),
]


# ── Helpers ───────────────────────────────────────────────────

def _safe_fetch(symbols: dict, period: str = "2d", interval: str = "1d") -> dict:
    """Download yfinance data for a group of symbols, return clean dict."""
    results = {}
    try:
        tickers = yf.download(
            list(symbols.values()),
            period=period,
            interval=interval,
            progress=False,
            threads=True,
        )
        closes = tickers["Close"] if "Close" in tickers else tickers.get("close", None)
        if closes is None:
            return results

        for name, sym in symbols.items():
            try:
                col = closes[sym] if sym in closes.columns else closes.get(sym)
                if col is None or col.dropna().empty:
                    continue
                vals = col.dropna()
                if len(vals) >= 2:
                    prev  = float(vals.iloc[-2])
                    last  = float(vals.iloc[-1])
                    chg   = last - prev
                    pct   = chg / prev * 100 if prev else 0.0
                    results[name] = {"price": last, "change": chg, "pct": pct, "symbol": sym}
                elif len(vals) == 1:
                    results[name] = {"price": float(vals.iloc[-1]), "change": 0, "pct": 0, "symbol": sym}
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"yfinance batch fetch failed: {e}")
    return results


def _fetch_news(max_per_feed: int = 3) -> list[dict]:
    """Pull top headlines from free RSS feeds."""
    headlines = []
    for source, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                headlines.append({
                    "source":    source,
                    "title":     entry.get("title", "").strip(),
                    "summary":   entry.get("summary", "")[:200].strip(),
                    "published": entry.get("published", ""),
                    "link":      entry.get("link", ""),
                })
        except Exception as e:
            logger.debug(f"RSS fetch failed for {source}: {e}")
    return headlines


def _fetch_wsb_sentiment(limit: int = 10) -> list[dict]:
    """ApeWisdom WSB trending tickers — free, no auth needed."""
    try:
        resp = requests.get(
            "https://apewisdom.io/api/v1.0/filter/wallstreetbets",
            timeout=10,
            headers={"User-Agent": "NWO-Trading-Bot/1.0"},
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        results = data.get("results", [])[:limit]
        return [
            {
                "ticker":   r.get("ticker", ""),
                "mentions": r.get("mentions", 0),
                "upvotes":  r.get("upvotes", 0),
                "rank":     r.get("rank", 0),
                "mentions_24h_ago": r.get("mentions_24h_ago", 0),
            }
            for r in results
        ]
    except Exception as e:
        logger.debug(f"ApeWisdom fetch failed: {e}")
        return []


def _fetch_congress_trades(limit: int = 10) -> list[dict]:
    """QuiverQuant congressional trades — free tier, no key needed for basic endpoint."""
    try:
        resp = requests.get(
            "https://api.quiverquant.com/beta/live/congresstrading",
            timeout=10,
            headers={
                "User-Agent":  "NWO-Trading-Bot/1.0",
                "Accept":      "application/json",
            },
        )
        if resp.status_code != 200:
            return []
        trades = resp.json()
        return [
            {
                "politician": t.get("Representative", t.get("Senator", "Unknown")),
                "party":      t.get("Party", ""),
                "ticker":     t.get("Ticker", ""),
                "action":     t.get("Transaction", ""),
                "amount":     t.get("Range", ""),
                "date":       t.get("TransactionDate", ""),
            }
            for t in trades[:limit]
        ]
    except Exception as e:
        logger.debug(f"QuiverQuant fetch failed: {e}")
        return []


# ── Main generator ────────────────────────────────────────────

class MorningBriefGenerator:

    def __init__(self):
        from config import config
        self.config = config
        self.cache_path = ROOT / config.brief.cache_file
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def is_stale(self) -> bool:
        """Returns True if cache is missing or older than stale_hours."""
        if not self.cache_path.exists():
            return True
        try:
            cached = json.loads(self.cache_path.read_text())
            generated_at = datetime.fromisoformat(cached.get("generated_at", "2000-01-01"))
            age_hours = (datetime.utcnow() - generated_at).total_seconds() / 3600
            return age_hours >= self.config.brief.stale_hours
        except Exception:
            return True

    def load_cached(self) -> Optional[dict]:
        try:
            if self.cache_path.exists():
                return json.loads(self.cache_path.read_text())
        except Exception:
            pass
        return None

    def _save(self, brief: dict):
        self.cache_path.write_text(json.dumps(brief, indent=2, default=str))

    def _build_context(self, raw: dict) -> str:
        """Build a text context block from raw market data for AI prompts."""
        parts = []
        if raw.get("futures"):
            parts.append("PRE-MARKET FUTURES:\n" + "\n".join(
                f"  {k}: {v['price']:.2f} ({v['pct']:+.2f}%)" for k, v in raw["futures"].items()
            ))
        if raw.get("indices"):
            parts.append("US INDICES:\n" + "\n".join(
                f"  {k}: {v['price']:.2f} ({v['pct']:+.2f}%)" for k, v in raw["indices"].items()
            ))
        if raw.get("global_markets"):
            parts.append("GLOBAL MARKETS:\n" + "\n".join(
                f"  {k}: {v['price']:.2f} ({v['pct']:+.2f}%)" for k, v in raw["global_markets"].items()
            ))
        if raw.get("commodities"):
            parts.append("COMMODITIES:\n" + "\n".join(
                f"  {k}: {v['price']:.2f} ({v['pct']:+.2f}%)" for k, v in raw["commodities"].items()
            ))
        if raw.get("crypto"):
            parts.append("CRYPTO:\n" + "\n".join(
                f"  {k}: ${v['price']:,.2f} ({v['pct']:+.2f}%)" for k, v in raw["crypto"].items()
            ))
        if raw.get("rates"):
            parts.append("RATES & FX:\n" + "\n".join(
                f"  {k}: {v['price']:.2f}" for k, v in raw["rates"].items()
            ))
        if raw.get("headlines"):
            parts.append("TOP NEWS HEADLINES:\n" + "\n".join(
                f"  [{h['source']}] {h['title']}" for h in raw["headlines"][:15]
            ))
        if raw.get("wsb"):
            parts.append("WSB TRENDING:\n" + ", ".join(
                f"{t['ticker']}({t['mentions']})" for t in raw["wsb"][:8]
            ))
        if raw.get("congress_trades"):
            parts.append("RECENT CONGRESS TRADES:\n" + "\n".join(
                f"  {t['politician']} ({t['party']}) {t['action']} {t['ticker']} {t['amount']} on {t['date']}"
                for t in raw["congress_trades"][:6]
            ))
        return "\n\n".join(parts)

    def _build_prompt(self, context: str) -> str:
        today = datetime.now().strftime("%A, %B %d, %Y")
        return f"""You are a concise institutional market analyst. Write a morning market brief for {today}.

Use this raw data:
{context}

Format your response as clean HTML (no markdown) using these sections. Keep each section tight and data-driven:

<h3>Lead Story</h3>
2-3 sentences on the single most important macro theme today.

<h3>Pre-Market Snapshot</h3>
Table with futures, key indices direction, VIX level. Flag anything unusual.

<h3>Global Overnight</h3>
2-3 sentences on Asia/Europe. Any divergences from US direction.

<h3>Commodities and Crypto</h3>
Oil, gold, bitcoin — price levels and what they signal for risk appetite.

<h3>Rates and FX</h3>
10yr yield and USD direction. What it means for equities.

<h3>Top Headlines</h3>
Bullet list of 5-6 most market-moving headlines from the data, with source attribution.

<h3>WSB Pulse</h3>
Top 5 trending tickers with mention counts. Any unusual spikes worth noting.

<h3>Capitol Hill Trades</h3>
Recent congressional trades — note any interesting patterns or sector concentrations.

<h3>Key Risks Today</h3>
Bullet list of 3 things that could move markets today (earnings, data, geopolitical).

Be direct. No filler. Institutional tone. Use <span class="up"> for positive numbers and <span class="down"> for negative numbers."""

    def _synthesize(self, raw: dict) -> str:
        """
        Generate AI narrative. Tries Google Gemini first, falls back to Anthropic.
        Returns empty string if neither key is configured.
        """
        google_key    = self.config.brief.google_api_key
        anthropic_key = self.config.brief.anthropic_api_key

        if not google_key and not anthropic_key:
            return ""

        context = self._build_context(raw)
        prompt  = self._build_prompt(context)

        # ── Google Gemini Flash (primary) ─────────────────────
        if google_key:
            try:
                from google import genai
                client   = genai.Client(api_key=google_key)
                response = client.models.generate_content(
                    model="models/gemini-2.5-flash",
                    contents=prompt,
                )
                html = response.text
                # Strip markdown code fences if Gemini wraps in ```html
                if html.startswith("```"):
                    html = "\n".join(html.split("\n")[1:])
                if html.endswith("```"):
                    html = html.rsplit("```", 1)[0]
                logger.info("[BRIEF] Narrative generated via Google Gemini Flash")
                return html.strip()
            except Exception as e:
                logger.warning(f"[BRIEF] Gemini synthesis failed: {e} — trying Anthropic fallback")

        # ── Anthropic Claude Haiku (fallback) ─────────────────
        if anthropic_key:
            try:
                import anthropic
                client  = anthropic.Anthropic(api_key=anthropic_key)
                message = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=2000,
                    messages=[{"role": "user", "content": prompt}],
                )
                logger.info("[BRIEF] Narrative generated via Anthropic Claude Haiku")
                return message.content[0].text
            except Exception as e:
                logger.warning(f"[BRIEF] Anthropic synthesis failed: {e}")

        return ""

    def generate(self) -> dict:
        """Fetch all data sources and generate the brief. Caches result."""
        logger.info("[BRIEF] Generating morning brief...")
        t0 = time.time()

        # Fetch all data in sequence (yfinance handles internal threading)
        raw = {
            "generated_at":    datetime.utcnow().isoformat(),
            "indices":         _safe_fetch(INDICES),
            "futures":         _safe_fetch(FUTURES),
            "global_markets":  _safe_fetch(GLOBAL),
            "commodities":     _safe_fetch(COMMODITIES),
            "crypto":          _safe_fetch(CRYPTO),
            "rates":           _safe_fetch(RATES),
            "headlines":       _fetch_news(),
            "wsb":             _fetch_wsb_sentiment(),
            "congress_trades": _fetch_congress_trades(),
        }

        # AI narrative — Gemini primary, Anthropic fallback, empty if no key
        raw["narrative_html"] = self._synthesize(raw)

        self._save(raw)
        logger.info(f"[BRIEF] Generated in {time.time()-t0:.1f}s — "
                    f"{len(raw['headlines'])} headlines, {len(raw['wsb'])} WSB tickers")
        return raw


def get_brief(force_refresh: bool = False) -> dict:
    """Public entry point. Returns cached brief or generates fresh one."""
    gen = MorningBriefGenerator()
    if force_refresh or gen.is_stale():
        return gen.generate()
    return gen.load_cached() or gen.generate()
