"""
Two-step Schwab re-auth. Run:

  Step 1:  python refresh_token.py
           → Opens the login URL. Visit it, log in, copy the redirect URL.

  Step 2:  python refresh_token.py "<paste full redirect URL here>"
           → Exchanges the code and saves a fresh token.
"""
import os, sys, json, site
sys.path = [p for p in sys.path if not p.endswith("new_world_order")] + [site.getusersitepackages()]

import schwab
from dotenv import load_dotenv

load_dotenv()
api_key    = os.getenv("SCHWAB_API_KEY")
app_secret = os.getenv("SCHWAB_API_SECRET")
STATE_FILE = "tokens/.auth_state"

if not api_key or not app_secret:
    print("ERROR: SCHWAB_API_KEY / SCHWAB_API_SECRET not set in .env")
    sys.exit(1)

if len(sys.argv) == 1:
    # Step 1 — generate URL and save state
    auth_ctx = schwab.auth.get_auth_context(api_key, "https://127.0.0.1")
    os.makedirs("tokens", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"state": auth_ctx.state}, f)
    print("\n=== STEP 1: Open this URL in your browser ===\n")
    print(auth_ctx.authorization_url)
    print("\n=== After logging in, copy the full redirect URL ===")
    print("=== Then run: python refresh_token.py \"<redirect URL>\" ===\n")

else:
    # Step 2 — exchange code
    # Join all args in case the URL was not quoted and & split it into separate argv entries
    received_url = " ".join(sys.argv[1:]).strip().strip('"').strip("'")
    # Remove any accidental prefix the user may have typed before the URL
    if "https://" in received_url:
        received_url = "https://" + received_url.split("https://", 1)[1]
    if not os.path.exists(STATE_FILE):
        print("ERROR: Run step 1 first (python refresh_token.py) to generate the login URL.")
        sys.exit(1)
    with open(STATE_FILE) as f:
        saved = json.load(f)
    state = saved["state"]
    auth_ctx = schwab.auth.get_auth_context(api_key, "https://127.0.0.1", state=state)

    def write_token(token):
        with open("tokens/schwab_token.json", "w") as f:
            json.dump(token, f, indent=2)

    try:
        client = schwab.auth.client_from_received_url(
            api_key=api_key,
            app_secret=app_secret,
            auth_context=auth_ctx,
            received_url=received_url,
            token_write_func=write_token,
        )
        os.remove(STATE_FILE)
        print("\nToken saved to tokens/schwab_token.json")
        resp = client.get_account_numbers()
        for acc in resp.json():
            print(f"  Account: {acc['accountNumber']}  Hash: {acc['hashValue']}")
        print("\nDone. Schwab API is authenticated.")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
