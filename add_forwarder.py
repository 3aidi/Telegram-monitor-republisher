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
import qrcode

import db


def create_client(session_name: str, api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(
        session_name,
        api_id,
        api_hash,
        device_model="Desktop",
        system_version="Windows 10",
        app_version="5.10.3 x64",
        lang_code="en",
        system_lang_code="en",
    )


def prompt_phone() -> str:
    print("\nEnter phone number (+1234567890):")
    while True:
        raw = input("Phone: ").strip()
        clean = re.sub(r"[^\d+]", "", raw)
        if clean.startswith("00"):
            clean = "+" + clean[2:]
        elif clean.startswith("0"):
            print("❌ Phone number cannot start with '0'. Include country code (e.g. +1...).")
            continue
        elif not clean.startswith("+"):
            clean = "+" + clean

        digits = re.sub(r"\D", "", clean)
        if len(digits) < 7:
            print("❌ Phone number too short.")
            continue
        return clean


async def login_via_qr(client: TelegramClient, session_name: str):
    print("\nGenerating QR code for login...")
    qr = await client.qr_login()
    img = qrcode.make(qr.url)
    qr_path = os.path.abspath("qr_login.png")
    img.save(qr_path)

    print(f"Opening QR code image on screen: {qr_path}")
    try:
        os.startfile(qr_path)
    except Exception:
        pass

    print("\n👉 Scan this QR code on your phone:")
    print("   Open Telegram -> Settings -> Devices -> Link Desktop Device")
    print("Waiting for scan...")

    try:
        await qr.wait(timeout=120.0)
    except SessionPasswordNeededError:
        print("\n🔒 2FA password required.")
        pwd = getpass.getpass("Enter 2FA password: ")
        await client.sign_in(password=pwd.strip())

    try:
        os.remove(qr_path)
    except Exception:
        pass


async def login_via_phone(client: TelegramClient, session_name: str):
    phone = prompt_phone()
    print(f"\nConnecting and requesting code for {phone}...")
    sent = await client.send_code_request(phone)
    print("Code sent to your Telegram app.")

    code_input = input("\nEnter verification code (e.g. 1 2 3 4 5): ").strip()
    clean_code = re.sub(r"\D", "", code_input)

    try:
        await client.sign_in(phone=phone, code=clean_code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        print("\n🔒 2FA password required.")
        pwd = getpass.getpass("Enter 2FA password: ")
        await client.sign_in(password=pwd.strip())


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

    print("\n" + "=" * 45)
    print("  Add Telegram Forwarder Account")
    print("=" * 45)
    print("1. Scan QR Code (Fastest - scan from phone)")
    print("2. Enter Phone Number")
    choice = input("\nChoose option [1/2] (default 1): ").strip() or "1"

    client = create_client(session_name, api_id, api_hash)
    await client.connect()

    try:
        if choice == "2":
            await login_via_phone(client, session_name)
        else:
            await login_via_qr(client, session_name)

        me = await client.get_me()
        user_id = getattr(me, "id", None)
        username = getattr(me, "username", None)
        first_name = getattr(me, "first_name", "") or ""
        last_name = getattr(me, "last_name", "") or ""
        phone = getattr(me, "phone", None)
        display_name = f"{first_name} {last_name}".strip() or (f"@{username}" if username else phone or session_name)

        account_id = db.add_forward_account(
            session_name=session_name,
            phone=phone,
            username=username,
            display_name=display_name,
            active=True,
        )

        print("\n" + "=" * 45)
        print("✅ Forwarder Account Added Successfully!")
        print("=" * 45)
        print(f"• Name: {display_name}")
        if username:
            print(f"• Username: @{username}")
        if phone:
            print(f"• Phone: +{phone}")
        print(f"• Session: {session_name}.session")
        print("\nAccount is ready and added to forwarders pool.")
        print("=" * 45 + "\n")

    except PhoneNumberInvalidError:
        print("❌ Invalid phone number.")
    except PhoneNumberBannedError:
        print("❌ Phone number banned by Telegram.")
    except FloodWaitError as exc:
        print(f"❌ Telegram rate limit: wait {exc.seconds}s.")
    except Exception as exc:
        print(f"❌ Login failed: {exc}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
