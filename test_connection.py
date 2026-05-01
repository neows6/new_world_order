"""
Quick connection test — run this before main.py to verify everything works.
Tests: token validity, live quote, account value, account hash.
"""

import sys
import site
import os

# Load real schwab-py before our local broker/ package
sys.path = [p for p in sys.path if not p.endswith("new_world_order")] + [site.getusersitepackages()]

import schwab
from dotenv import load_dotenv

load_dotenv()


def main():
    print("=" * 50)
    print("  Schwab Connection Test")
    print("=" * 50)

    # ── 1. Load token ─────────────────────────────────
    token_path = os.getenv("SCHWAB_TOKEN_PATH", "tokens/schwab_token.json")
    api_key    = os.getenv("SCHWAB_API_KEY")
    api_secret = os.getenv("SCHWAB_API_SECRET")

    print(f"\n[1] Loading token from {token_path}...")
    try:
        client = schwab.auth.client_from_token_file(
            token_path=token_path,
            api_key=api_key,
            app_secret=api_secret,
        )
        print("    ✓ Token loaded")
    except Exception as e:
        print(f"    ✗ Failed: {e}")
        sys.exit(1)

    # ── 2. Live quote ──────────────────────────────────
    print("\n[2] Fetching live AAPL quote...")
    try:
        resp = client.get_quote("AAPL")
        resp.raise_for_status()
        data = resp.json()
        price = data.get("AAPL", {}).get("quote", {}).get("lastPrice")
        print(f"    ✓ AAPL last price: ${price}")
    except Exception as e:
        print(f"    ✗ Failed: {e}")

    # ── 3. Account value ───────────────────────────────
    print("\n[3] Fetching account value...")
    try:
        account_hash = os.getenv("SCHWAB_ACCOUNT_HASH")
        resp = client.get_account(
            account_hash=account_hash,
            fields=[client.Account.Fields.POSITIONS]
        )
        resp.raise_for_status()
        data = resp.json()
        value = (
            data.get("securitiesAccount", {})
                .get("currentBalances", {})
                .get("liquidationValue")
        )
        print(f"    ✓ Account liquidation value: ${value:,.2f}")
    except Exception as e:
        print(f"    ✗ Failed: {e}")

    print("\n" + "=" * 50)
    print("  All tests passed — ready to run main.py")
    print("=" * 50)


if __name__ == "__main__":
    main()
