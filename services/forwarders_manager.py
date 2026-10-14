"""Forwarders manager: multi-account pooling, in-bot authentication, and auto-joining."""

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from telethon import TelegramClient, events
from telethon.errors import (
    ApiIdInvalidError,
    ChannelsTooMuchError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
    UserKickedError,
)
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

import db
from core.runtime import check_can_write_to_destination, is_group_chat, leave_destination_chat

logger = logging.getLogger("forwarders_manager")

# In-flight login state for admin: admin_user_id -> dict
_in_flight_logins: Dict[int, dict] = {}


def normalize_phone_number(phone: str) -> Tuple[bool, str, Optional[str]]:
    """Sanitize and validate an international phone number string."""
    clean = re.sub(r"[^\d+]", "", (phone or "").strip())
    if clean.startswith("00"):
        clean = "+" + clean[2:]
    elif clean.startswith("0"):
        return False, "Phone number cannot start with '0'. Please include the international country code (e.g. +1 for USA, +44 for UK, +20 for Egypt, +961 for Lebanon).", None
    elif not clean.startswith("+"):
        clean = "+" + clean

    digits = re.sub(r"\D", "", clean)
    if len(digits) < 7:
        return False, "Invalid phone number format. Please include country code and at least 7 digits (e.g. +1234567890).", None
    return True, "OK", clean


def get_in_flight_login(admin_user_id: int) -> Optional[dict]:
    return _in_flight_logins.get(admin_user_id)


def clear_in_flight_login(admin_user_id: int) -> None:
    entry = _in_flight_logins.pop(admin_user_id, None)
    if entry and "client" in entry:
        try:
            cl = entry["client"]
            if hasattr(cl, "is_connected") and cl.is_connected():
                asyncio.create_task(cl.disconnect())
        except Exception:
            pass


async def start_login_flow(
    admin_user_id: int,
    phone: str,
    api_id: int,
    api_hash: str,
    session_prefix: str = "forward_session",
    base_dir: str = ".",
) -> Tuple[bool, str, Optional[str]]:
    """Initiate a Telethon client login for a new forwarder account."""
    valid, err_msg, clean_phone = normalize_phone_number(phone)
    if not valid or not clean_phone:
        return False, err_msg, None

    # Clear any previous in-flight login
    clear_in_flight_login(admin_user_id)

    # Determine unique session name
    existing_accounts = db.list_forward_accounts()
    existing_sessions = {a["session_name"] for a in existing_accounts}
    
    # Check sessions on disk
    counter = 1
    session_name = f"{session_prefix}_{counter}"
    while (
        session_name in existing_sessions
        or os.path.exists(os.path.join(base_dir, f"{session_name}.session"))
    ):
        counter += 1
        session_name = f"{session_prefix}_{counter}"

    session_path = os.path.join(base_dir, session_name)
    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.connect()
        code_request = await client.send_code_request(clean_phone)

        deliv_type = "app"
        deliv_label = "Telegram App"
        if hasattr(code_request, "type") and code_request.type is not None:
            t_cls = type(code_request.type).__name__
            if "Sms" in t_cls:
                deliv_type = "sms"
                deliv_label = "SMS"
            elif "Call" in t_cls:
                deliv_type = "call"
                deliv_label = "Phone Call"
            elif "Email" in t_cls:
                deliv_type = "email"
                deliv_label = "Email"
            elif "App" in t_cls:
                deliv_type = "app"
                deliv_label = "Telegram App"

        _in_flight_logins[admin_user_id] = {
            "client": client,
            "phone": clean_phone,
            "phone_code_hash": code_request.phone_code_hash,
            "session_name": session_name,
            "session_path": session_path,
            "delivery_type": deliv_type,
            "delivery_label": deliv_label,
            "timeout": getattr(code_request, "timeout", None) or 60,
            "request_time": time.time(),
        }
        logger.info("Sent login code to %s via %s (session: %s)", clean_phone, deliv_label, session_name)
        return True, f"Code sent to {deliv_label}", code_request.phone_code_hash
    except PhoneNumberInvalidError:
        return False, "Telegram reported this phone number is invalid. Please double check the country code and digits.", None
    except PhoneNumberBannedError:
        return False, "This phone number has been banned by Telegram.", None
    except PhoneNumberUnoccupiedError:
        return False, "No Telegram account exists with this phone number. Please create the account in Telegram first.", None
    except FloodWaitError as exc:
        return False, f"Telegram rate limit: please wait {exc.seconds} seconds before requesting a code again.", None
    except SendCodeUnavailableError:
        return False, "Telegram is unable to deliver SMS/codes to this number right now. Try logging in on the official Telegram app first.", None
    except ApiIdInvalidError:
        return False, "Invalid API_ID or API_HASH in your .env configuration.", None
    except Exception as exc:
        logger.exception("Failed to send login code to %s", clean_phone)
        try:
            await client.disconnect()
        except Exception:
            pass
        return False, f"Failed to send code: {exc}", None


async def resend_login_code(
    admin_user_id: int,
) -> Tuple[bool, str, Optional[str]]:
    """Request Telegram to resend verification code (e.g. via SMS or call)."""
    entry = _in_flight_logins.get(admin_user_id)
    if not entry:
        return False, "No login in progress. Please start by adding an account.", None

    client: TelegramClient = entry["client"]
    phone = entry["phone"]
    phone_code_hash = entry["phone_code_hash"]

    try:
        sent_code = await client(ResendCodeRequest(phone_number=phone, phone_code_hash=phone_code_hash))
        entry["phone_code_hash"] = sent_code.phone_code_hash
        deliv_type = "sms"
        deliv_label = "SMS"
        if hasattr(sent_code, "type") and sent_code.type is not None:
            t_cls = type(sent_code.type).__name__
            if "Sms" in t_cls:
                deliv_type = "sms"
                deliv_label = "SMS"
            elif "Call" in t_cls:
                deliv_type = "call"
                deliv_label = "Phone Call"
            elif "App" in t_cls:
                deliv_type = "app"
                deliv_label = "Telegram App"
        entry["delivery_type"] = deliv_type
        entry["delivery_label"] = deliv_label
        entry["timeout"] = getattr(sent_code, "timeout", None) or 60
        entry["request_time"] = time.time()
        logger.info("Resent login code to %s via %s", phone, deliv_label)
        return True, f"Code resent via {deliv_label}", sent_code.phone_code_hash
    except FloodWaitError as exc:
        return False, f"Telegram rate limit: please wait {exc.seconds}s before requesting code again.", None
    except Exception as exc:
        logger.warning("Failed to resend code for %s: %s", phone, exc)
        return False, f"Could not resend code: {exc}", None


async def submit_login_code(
    admin_user_id: int,
    code: str,
) -> Tuple[bool, str, Optional[TelegramClient]]:
    """Submit verification code received by the user."""
    entry = _in_flight_logins.get(admin_user_id)
    if not entry:
        return False, "No login in progress. Please start by adding an account.", None

    clean_code = re.sub(r"\D", "", code.strip())
    if not clean_code or len(clean_code) < 4:
        return False, "Invalid code format. Please reply with the 5-digit verification code (e.g. 1 2 3 4 5).", None

    client: TelegramClient = entry["client"]
    phone = entry["phone"]
    phone_code_hash = entry["phone_code_hash"]

    try:
        await client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
        logger.info("Successfully signed in account %s", phone)
        return True, "SUCCESS", client
    except SessionPasswordNeededError:
        logger.info("2FA password required for account %s", phone)
        return False, "2FA_REQUIRED", client
    except PhoneCodeExpiredError:
        return False, "The code has expired or was invalidated by Telegram. Please tap 'Resend via SMS' or enter your phone again.", None
    except PhoneCodeInvalidError:
        return False, "Incorrect verification code. Please check and reply again (e.g. 1 2 3 4 5).", None
    except FloodWaitError as exc:
        return False, f"Too many attempts: please wait {exc.seconds}s.", None
    except Exception as exc:
        logger.warning("Code verification failed for %s: %s", phone, exc)
        return False, f"Verification failed: {exc}", None


async def submit_login_2fa(
    admin_user_id: int,
    password: str,
) -> Tuple[bool, str, Optional[TelegramClient]]:
    """Submit 2FA cloud password if enabled on the account."""
    entry = _in_flight_logins.get(admin_user_id)
    if not entry:
        return False, "No login in progress.", None

    client: TelegramClient = entry["client"]
    try:
        await client.sign_in(password=password.strip())
        logger.info("Successfully authenticated 2FA for %s", entry["phone"])
        return True, "SUCCESS", client
    except Exception as exc:
        logger.warning("2FA authentication failed for %s: %s", entry["phone"], exc)
        return False, f"Password incorrect: {exc}", None


async def save_and_activate_account(
    admin_user_id: int,
) -> Optional[Dict[str, Any]]:
    """Save authenticated account to DB and clear in-flight state."""
    entry = _in_flight_logins.pop(admin_user_id, None)
    if not entry:
        return None

    client: TelegramClient = entry["client"]
    session_name = entry["session_name"]
    phone = entry["phone"]

    username = None
    display_name = phone
    try:
        me = await client.get_me()
        if me:
            username = getattr(me, "username", None)
            first_name = getattr(me, "first_name", "") or ""
            last_name = getattr(me, "last_name", "") or ""
            full_name = f"{first_name} {last_name}".strip()
            display_name = full_name or (f"@{username}" if username else phone)
            if getattr(me, "phone", None):
                phone = me.phone
    except Exception:
        pass

    account_id = db.add_forward_account(
        session_name=session_name,
        phone=phone,
        username=username,
        display_name=display_name,
        active=True,
    )
    return {
        "id": account_id,
        "session_name": session_name,
        "phone": phone,
        "username": username,
        "display_name": display_name,
        "client": client,
    }


def parse_invite_hash(chat_ref: str) -> Optional[str]:
    """Extract invite hash from private group invite link if present."""
    m = re.search(r"(?:t\.me/\+|joinchat/)([\w-]+)", str(chat_ref or ""))
    return m.group(1) if m else None


async def auto_join_destinations_for_client(
    client: TelegramClient,
    bot_client: Optional[TelegramClient] = None,
    admin_user_id: Optional[int] = None,
    delay_seconds: float = 10.0,
    status_message: Optional[Any] = None,
) -> Dict[str, Any]:
    """Auto-join a forwarder client to all active destination groups in the database.
    
    Safe anti-ban pacing: sleeps between joins and handles FloodWait gracefully.
    """
    results = {
        "joined": 0,
        "already_member": 0,
        "failed": 0,
        "details": [],
    }
    if not client or not hasattr(client, "is_connected") or not client.is_connected():
        return results

    try:
        me = await client.get_me()
        acct_label = f"@{me.username}" if getattr(me, "username", None) else (getattr(me, "phone", "") or "forwarder")
    except Exception:
        acct_label = "forwarder"

    dest_rows = await db.run_async(db.list_destinations, active_only=True)
    if not dest_rows:
        logger.info("No active destinations found to auto-join for %s", acct_label)
        return results

    # Pre-warm dialogs to identify groups already joined
    joined_refs = set()
    try:
        dialogs = await client.get_dialogs(limit=250)
        for d in dialogs:
            ent = getattr(d, "entity", None)
            if ent:
                norm_id = db.normalize_channel_id(ent)
                if norm_id:
                    joined_refs.add(str(norm_id))
                uname = getattr(ent, "username", None)
                if uname:
                    joined_refs.add(f"@{uname.lower()}")
    except Exception as exc:
        logger.debug("Could not pre-warm dialogs for auto-join: %s", exc)

    total = len(dest_rows)
    logger.info("Starting auto-join to %s destinations for %s", total, acct_label)

    for idx, d in enumerate(dest_rows, start=1):
        chat_ref = str(d.get("chat_id") or "").strip()
        title = d.get("title") or chat_ref
        norm_ref = chat_ref.lower() if chat_ref.startswith("@") else chat_ref

        # Check if already in dialogs
        if norm_ref in joined_refs or chat_ref in joined_refs:
            results["already_member"] += 1
            results["details"].append((title, "already_member"))
            continue

        success = False
        try:
            # 1. Public group by @username
            if chat_ref.startswith("@"):
                await client(JoinChannelRequest(chat_ref))
                success = True
            # 2. Private group invite link
            elif "t.me/" in chat_ref:
                inv_hash = parse_invite_hash(chat_ref)
                if inv_hash:
                    await client(ImportChatInviteRequest(inv_hash))
                    success = True
                else:
                    results["failed"] += 1
                    results["details"].append((title, "invalid_invite_link"))
                    continue
            else:
                # Numeric ID without public handle: try resolving entity if visible
                try:
                    peer = int(chat_ref)
                    ent = await client.get_entity(peer)
                    if ent:
                        await client(JoinChannelRequest(ent))
                        success = True
                except Exception as ent_err:
                    results["failed"] += 1
                    results["details"].append((title, f"unresolvable_id: {ent_err}"))
                    continue

            if success:
                results["joined"] += 1
                results["details"].append((title, "joined"))
                logger.info("Auto-joined group %s (%s) for %s", title, chat_ref, acct_label)
        except UserAlreadyParticipantError:
            results["already_member"] += 1
            results["details"].append((title, "already_member"))
        except ChannelsTooMuchError:
            logger.warning("Account %s has reached maximum channel/group limit!", acct_label)
            results["failed"] += 1
            results["details"].append((title, "channels_too_much"))
            if bot_client and admin_user_id:
                try:
                    await bot_client.send_message(
                        admin_user_id,
                        f"⚠️ **Limit reached**: {acct_label} is in too many groups (Telegram limit).",
                        parse_mode="markdown",
                    )
                except Exception:
                    pass
            break
        except FloodWaitError as fwe:
            wait_s = int(getattr(fwe, "seconds", 60) or 60)
            logger.warning("FloodWait of %ss while joining %s for %s", wait_s, title, acct_label)
            results["failed"] += 1
            results["details"].append((title, f"floodwait_{wait_s}s"))
            if wait_s > 180:
                # Long flood wait: stop auto-joining to prevent bans
                if bot_client and admin_user_id:
                    try:
                        await bot_client.send_message(
                            admin_user_id,
                            f"⏸️ **Auto-join paused**: Telegram rate-limited {acct_label} for {wait_s}s.",
                            parse_mode="markdown",
                        )
                    except Exception:
                        pass
                break
            await asyncio.sleep(wait_s)
        except (InviteHashExpiredError, InviteHashInvalidError) as inv_err:
            results["failed"] += 1
            results["details"].append((title, f"invite_expired: {inv_err}"))
        except Exception as exc:
            results["failed"] += 1
            results["details"].append((title, f"error: {exc}"))
            logger.warning("Failed to auto-join %s for %s: %s", title, acct_label, exc)

        # Anti-ban delay between joins
        if delay_seconds > 0 and idx < total:
            await asyncio.sleep(delay_seconds)

    # Progress / completion summary to admin
    if bot_client and admin_user_id:
        summary_text = (
            f"✅ **Auto-Join Complete for {acct_label}**\n\n"
            f"• 🟢 Joined: **{results['joined']}**\n"
            f"• ℹ️ Already Member: **{results['already_member']}**\n"
            f"• 🔴 Failed / Private: **{results['failed']}**"
        )
        try:
            if status_message:
                await status_message.edit(summary_text, parse_mode="markdown")
            else:
                await bot_client.send_message(admin_user_id, summary_text, parse_mode="markdown")
        except Exception:
            pass

    return results
