"""Standalone CLI tool to add and authenticate forwarder accounts.

Run this script in terminal:
    python add_forwarder.py
"""

import asyncio
import getpass
import os
import re
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

import db


def prompt_phone() -> str:
    print("\n" + "=" * 50)
    print("  Telegram Forwarder Account Setup")
    print("=" * 50)
    print("Please enter the account phone number in international format.")
    print("Example: +1234567890 or +447123456789")
    while True:
        raw = input("\nPhone number: ").strip()
        clean = re.sub(r"[^\d+]", "", raw)
        if clean.startswith("00"):
            clean = "+" + clean[2:]
        elif clean.startswith("0"):
            print("❌ Phone number cannot start with '0'. Please include the country code.")
            continue
        elif not clean.startswith("+"):
            clean = "+" + clean

        digits = re.sub(r"\D", "", clean)
        if len(digits) < 7:
            print("❌ Phone number is too short. Please include country code and digits.")
            continue
        return clean


async def main():
    load_dotenv()
    db.init_db()

    api_id_raw = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH", "").strip()

    if not api_id_raw or not api_hash:
        print("❌ Error: API_ID and API_HASH must be set in your .env file.")
        sys.exit(1)

    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("❌ Error: API_ID in .env must be an integer.")
        sys.exit(1)

    phone = prompt_phone()

    # Determine unique session name
    existing_accounts = db.list_forward_accounts()
    existing_sessions = {a["session_name"] for a in existing_accounts}

    counter = 1
    session_name = f"forward_session_{counter}"
    while (
        session_name in existing_sessions
        or os.path.exists(f"{session_name}.session")
    ):
        counter += 1
        session_name = f"forward_session_{counter}"

    print(f"\nCreating session: {session_name}...")
    client = TelegramClient(session_name, api_id, api_hash)
    await client.connect()

    try:
        print(f"Requesting verification code for {phone}...")
        sent_code = await client.send_code_request(phone)

        deliv_type = "Telegram App"
        if hasattr(sent_code, "type") and sent_code.type is not None:
            t_cls = type(sent_code.type).__name__
            if "Sms" in t_cls:
                deliv_type = "SMS"
            elif "Call" in t_cls:
                deliv_type = "Phone Call"

        print(f"\n📩 Code sent via: {deliv_type}")
        if deliv_type == "Telegram App":
            print("👉 Check the chat named 'Telegram' (ID 777000) inside your official Telegram app.")
        else:
            print("👉 Check your mobile SMS messages.")

        code_input = input("\nEnter verification code: ").strip()
        clean_code = re.sub(r"\D", "", code_input)

        try:
            await client.sign_in(phone=phone, code=clean_code, phone_code_hash=sent_code.phone_code_hash)
        except SessionPasswordNeededError:
            print("\n🔒 Two-Step Verification (2FA) is enabled for this account.")
            pwd = getpass.getpass("Enter your 2FA cloud password: ")
            await client.sign_in(password=pwd.strip())

        me = await client.get_me()
        user_id = getattr(me, "id", None)
        username = getattr(me, "username", None)
        first_name = getattr(me, "first_name", "") or ""
        last_name = getattr(me, "last_name", "") or ""
        display_name = f"{first_name} {last_name}".strip() or (f"@{username}" if username else phone)

        account_id = db.add_forward_account(
            session_name=session_name,
            phone=phone,
            username=username,
            display_name=display_name,
            active=True,
        )

        print("\n" + "=" * 50)
        print("  🎉 Forwarder Account Added Successfully!")
        print("=" * 50)
        print(f"• ID: {account_id}")
        print(f"• Display Name: {display_name}")
        print(f"• Username: @{username}" if username else f"• Phone: {phone}")
        print(f"• Session File: {session_name}.session")
        print("\nThis account is now registered in the forwarder pool.")
        print("When you run the bot or type /forwarders, it will be listed and used for cascading.")
        print("=" * 50 + "\n")

    except PhoneNumberInvalidError:
        print("❌ Telegram reported this phone number is invalid.")
    except PhoneNumberBannedError:
        print("❌ This phone number has been banned by Telegram.")
    except FloodWaitError as exc:
        print(f"❌ Telegram rate limit: please wait {exc.seconds} seconds.")
    except Exception as exc:
        print(f"❌ Login failed: {exc}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
