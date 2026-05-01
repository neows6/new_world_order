"""
Run this once to authenticate with Schwab and get your account hash.

Steps:
  1. A URL will be printed — open it in your browser
  2. Log in to your Schwab account
  3. You'll be redirected to https://127.0.0.1 (shows an error — that's normal)
  4. Copy the FULL URL from the browser address bar and paste it here
  5. Your token is saved and your account hash is printed
  6. Copy the hashValue into your .env as SCHWAB_ACCOUNT_HASH=...
"""

import os
import sys
import site

# Ensure the real schwab-py library is found before our local broker/ package
sys.path = [p for p in sys.path if not p.endswith("new_world_order")] + [site.getusersitepackages()]

import schwab
from dotenv import load_dotenv


def main():
    load_dotenv()

    api_key    = os.getenv("SCHWAB_API_KEY")
    api_secret = os.getenv("SCHWAB_API_SECRET")

    if not api_key or not api_secret:
        print("ERROR: SCHWAB_API_KEY and SCHWAB_API_SECRET must be set in your .env file.")
        sys.exit(1)

    os.makedirs("tokens", exist_ok=True)

    client = schwab.auth.client_from_manual_flow(
        api_key=api_key,
        app_secret=api_secret,
        callback_url="https://127.0.0.1",
        token_path="tokens/schwab_token.json",
    )

    print("\nToken saved to tokens/schwab_token.json")
    print("\nFetching your account hash...\n")

    resp = client.get_account_numbers()
    accounts = resp.json()

    for acc in accounts:
        print(f"  Account Number : {acc['accountNumber']}")
        print(f"  Hash Value     : {acc['hashValue']}")
        print()

    print("Copy the hashValue above into your .env file as:")
    print("  SCHWAB_ACCOUNT_HASH=<hashValue>")


if __name__ == "__main__":
    main()
