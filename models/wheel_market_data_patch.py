# ─────────────────────────────────────────────────────────────────────────────
# ADD THESE METHODS to broker/market_data.py  (inside SchwabMarketData class)
# ─────────────────────────────────────────────────────────────────────────────

    def get_options_chain(self, symbol: str, days_to_expiry: int = 45) -> Optional[dict]:
        """
        Fetch options chain for a symbol from Schwab /marketdata/v1/chains.
        Returns a simplified dict with puts and calls keyed by expiry date.

        Args:
            symbol:          ticker e.g. "AAPL"
            days_to_expiry:  max DTE to include (default 45 for wheel strategy)

        Returns:
            {
              "symbol": "AAPL",
              "underlyingPrice": 192.34,
              "volatility": 28.4,          # IV of the chain (%)
              "ivRank": 62.1,              # estimated IV rank 0-100
              "puts": [
                {
                  "expiry": "2025-05-16",
                  "dte": 38,
                  "strike": 185.0,
                  "delta": -0.31,
                  "bid": 2.45,
                  "ask": 2.55,
                  "mid": 2.50,
                  "volume": 1234,
                  "openInterest": 5678,
                  "iv": 29.1
                },
                ...
              ],
              "calls": [ ... same shape ... ]
            }
        Returns None on any error.
        """
        try:
            client = self._get_client()
            from datetime import date, timedelta
            today = date.today()
            to_date = today + timedelta(days=days_to_expiry)

            resp = client.get_option_chain(
                symbol=symbol,
                contractType="ALL",
                toDate=to_date.strftime("%Y-%m-%d"),
                optionType="S",          # standard options only
            )
            resp.raise_for_status()
            raw = resp.json()

            underlying_price = raw.get("underlyingPrice", 0)
            volatility = raw.get("volatility", 0)

            # ── IV Rank estimate ──────────────────────────────────────────────
            # Schwab doesn't return IV rank directly; we approximate from
            # the chain's current IV vs the 52-week IV range if available,
            # otherwise we leave it as None and let the screener skip it.
            iv_rank = raw.get("ivRank", None)

            def _parse_leg(exp_date_str, strike_str, leg_list):
                results = []
                for leg in leg_list:
                    exp_clean = exp_date_str.split(":")[0]  # "2025-05-16:38"
                    dte_part  = exp_date_str.split(":")[-1] if ":" in exp_date_str else "0"
                    try:
                        dte = int(dte_part)
                    except ValueError:
                        dte = 0
                    results.append({
                        "expiry":        exp_clean,
                        "dte":           dte,
                        "strike":        float(strike_str),
                        "delta":         leg.get("delta", 0),
                        "bid":           leg.get("bid", 0),
                        "ask":           leg.get("ask", 0),
                        "mid":           round((leg.get("bid", 0) + leg.get("ask", 0)) / 2, 2),
                        "volume":        leg.get("totalVolume", 0),
                        "openInterest":  leg.get("openInterest", 0),
                        "iv":            round(leg.get("volatility", 0), 2),
                    })
                return results

            puts  = []
            calls = []

            for exp_date_str, strikes in raw.get("putExpDateMap", {}).items():
                for strike_str, leg_list in strikes.items():
                    puts.extend(_parse_leg(exp_date_str, strike_str, leg_list))

            for exp_date_str, strikes in raw.get("callExpDateMap", {}).items():
                for strike_str, leg_list in strikes.items():
                    calls.extend(_parse_leg(exp_date_str, strike_str, leg_list))

            return {
                "symbol":           symbol,
                "underlyingPrice":  underlying_price,
                "volatility":       round(volatility, 2),
                "ivRank":           iv_rank,
                "puts":             puts,
                "calls":            calls,
            }

        except Exception as e:
            print(f"[market_data] get_options_chain({symbol}) error: {e}")
            return None

    def get_iv_rank(self, symbol: str) -> Optional[float]:
        """
        Attempt to derive IV Rank for a symbol using 52-week high/low IV
        from the options chain volatility field vs historical quote data.
        Returns 0–100 float or None if unavailable.

        NOTE: Schwab does not expose a native ivRank field.  This is a
        best-effort approximation:  current IV vs the range seen across
        the nearest expiry strikes.  For a more accurate IV rank you'd
        integrate a dedicated data source (e.g. Market Chameleon, Barchart).
        """
        try:
            chain = self.get_options_chain(symbol, days_to_expiry=60)
            if not chain:
                return None
            current_iv = chain.get("volatility", 0)
            if not current_iv:
                return None
            all_ivs = [p["iv"] for p in chain["puts"] if p["iv"] > 0] + \
                      [c["iv"] for c in chain["calls"] if c["iv"] > 0]
            if not all_ivs:
                return None
            iv_min = min(all_ivs)
            iv_max = max(all_ivs)
            if iv_max == iv_min:
                return 50.0
            rank = round((current_iv - iv_min) / (iv_max - iv_min) * 100, 1)
            return max(0.0, min(100.0, rank))
        except Exception as e:
            print(f"[market_data] get_iv_rank({symbol}) error: {e}")
            return None
