"""
monitor/telegram_bot.py — Telegram push alerts for trade events.

Setup:
  1. Create bot via @BotFather → copy BOT_TOKEN
  2. Send bot a message, then run: python -m monitor.telegram_bot --get-chat-id
  3. Add to .env: TELEGRAM_BOT_TOKEN=... and TELEGRAM_CHAT_ID=...

Usage in code:
  from monitor.telegram_bot import send_alert
  send_alert("BUY AAPL — 10 shares @ $192.40 (confidence: 0.78)")
"""

import os
import sys
import argparse

from dotenv import load_dotenv
from loguru import logger

from utils.ssl_context import make_httpx_client as _mhc

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Supports comma-separated list: "-1003975931056,8322227501"
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
_CHAT_IDS = [c.strip() for c in CHAT_ID.split(",") if c.strip()]

_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send_alert(message: str) -> bool:
    """
    Send a Telegram message to all configured chat IDs.
    Returns True if at least one succeeds.
    Silent no-op if credentials are not configured.
    """
    if not BOT_TOKEN or not _CHAT_IDS:
        return False

    ok = False
    with _mhc(timeout=10.0) as client:
        for chat_id in _CHAT_IDS:
            try:
                resp = client.post(
                    f"{_BASE}/sendMessage",
                    json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                )
                resp.raise_for_status()
                ok = True
            except Exception as e:
                logger.warning(f"Telegram alert failed for {chat_id}: {e}")
    return ok


def get_chat_id() -> None:
    """
    Poll Telegram for recent updates and print the chat ID.
    Run once after sending your bot a message.
    """
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    print("Fetching recent messages sent to your bot...")
    try:
        with _mhc(timeout=15.0) as client:
            resp = client.get(f"{_BASE}/getUpdates")
            resp.raise_for_status()
            updates = resp.json().get("result", [])
    except Exception as e:
        print(f"Failed to fetch updates: {e}")
        sys.exit(1)

    if not updates:
        print("No messages found. Send your bot a message in Telegram first, then rerun.")
        return

    seen = set()
    for update in updates:
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        name    = chat.get("first_name") or chat.get("title") or "unknown"
        if chat_id and chat_id not in seen:
            print(f"  Chat ID : {chat_id}  ({name})")
            seen.add(chat_id)

    print("\nAdd the correct ID to your .env:")
    print("  TELEGRAM_CHAT_ID=<chat_id>")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--get-chat-id", action="store_true", help="Print chat IDs from recent messages")
    parser.add_argument("--test", action="store_true", help="Send a test alert")
    args = parser.parse_args()

    if args.get_chat_id:
        get_chat_id()
    elif args.test:
        ok = send_alert("NWO Monitor — test alert. System is connected.")
        print("Sent." if ok else "Failed — check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    else:
        parser.print_help()
