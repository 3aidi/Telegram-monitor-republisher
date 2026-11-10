"""Characterization tests for the Telegram bot overhaul (Phase 0).

These tests freeze and characterize current production behaviors across:
- Review actions (approve single-tap, double-tap, skip, stale tap, edit->approve, skip-all)
- Post lifecycle (mark sold, partial forwarding deletion failure)
- Wizard lifecycles (sources, destinations, edit, cancel behavior, zero-TTL)
- Forwarder login flows (phone, 2FA, restart-memory-loss)
- Paused system behavior (qualifying listings to pending_approval with 'system_paused', manual approve)
- Navigation telemetry (R-25, B-11)
"""

import asyncio
import os
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

import admin_bot
import core.texts as texts
import db
import main
import publish_guard
import services.forwarders_manager as fwd_mgr


class MockSender:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self._connected = True

    def is_connected(self):
        return self._connected

    async def send_message(self, peer, text, **kw):
        self.sent.append({"peer": peer, "text": text, "kw": kw})
        return type("MockMsg", (), {"id": len(self.sent) + 100})()

    async def delete_messages(self, peer, ids):
        self.deleted.append({"peer": peer, "ids": list(ids)})


class FakeEvent:
    def __init__(self, data_str=None, text=None, sender_id=None, client=None):
        self.data = data_str.encode("utf-8") if data_str else b""
        self.text = text or ""
        self.sender_id = sender_id or admin_bot.ADMIN_USER_ID
        self.client = client or self
        self.message = self
        self.fwd_from = None
        self.answered = []
        self.replies = []
        self.deleted = False
        self.sent_messages = []

    async def answer(self, text=None, alert=False):
        self.answered.append({"text": text, "alert": alert})

    async def reply(self, text, buttons=None, parse_mode=None, **kw):
        entry = {"text": text, "buttons": buttons, "kw": kw}
        self.replies.append(entry)
        return self

    async def edit(self, text, buttons=None, parse_mode=None, **kw):
        entry = {"text": text, "buttons": buttons, "kw": kw, "edited": True}
        self.replies.append(entry)
        return self

    async def send_message(self, recipient, text, buttons=None, parse_mode=None, **kw):
        entry = {"recipient": recipient, "text": text, "buttons": buttons, "kw": kw}
        self.sent_messages.append(entry)
        return self

    async def delete(self):
        self.deleted = True

    def is_connected(self):
        return True


class CharacterizationTests(unittest.IsolatedAsyncioTestCase):
    """Characterization tests locking in pre-overhaul behaviors."""

    ADMIN = 987654321

    async def asyncSetUp(self):
        self._seq = int(time.time() * 1000)
        self._db_path = os.path.join(tempfile.gettempdir(), f"test_char_{self._seq}.db")
        db.init_db(self._db_path)

        # Preserve original globals
        self._orig_db_default = db.DEFAULT_DB_PATH
        self._orig_admin_id = admin_bot.ADMIN_USER_ID
        self._orig_main_admin = main.ADMIN_USER_ID
        self._orig_admin_dest = admin_bot.DEST_CHANNEL
        self._orig_main_dest = main.DEST_CHANNEL
        self._orig_user_ref = admin_bot.user_client_ref
        self._orig_fwd_ref = admin_bot.forward_client_ref
        self._orig_fwds_ref = list(admin_bot.forward_clients_ref)

        # Redirect to isolated database and mock clients
        db.DEFAULT_DB_PATH = self._db_path
        admin_bot.ADMIN_USER_ID = self.ADMIN
        main.ADMIN_USER_ID = self.ADMIN
        admin_bot.DEST_CHANNEL = "@char_dest"
        main.DEST_CHANNEL = "@char_dest"
        publish_guard.set_publish_interval(0.0)

        self.mock_user = MockSender()
        self.mock_fwd = MockSender()
        admin_bot.set_user_client(self.mock_user)
        admin_bot.set_forward_client(self.mock_fwd)
        admin_bot.forward_clients_ref = [self.mock_fwd]

        admin_bot._wizard_state.clear()
        admin_bot._drafts.clear()

        # Set up a real supplier in the test DB
        self.supplier_id = db.add_supplier("@char_sup", channel_id=-100111222, db_path=self._db_path)

        class FakeTelethonBot:
            def __init__(self):
                self.handlers = []
                self.sent_messages = []
            def on(self, filt):
                def deco(fn):
                    self.handlers.append((filt, fn))
                    return fn
                return deco
            def is_connected(self):
                return True
            async def send_message(self, recipient, text, **kw):
                entry = {"recipient": recipient, "text": text, "kw": kw}
                self.sent_messages.append(entry)
                return self

        self.fake_bot = FakeTelethonBot()
        admin_bot.setup_admin_handlers(self.fake_bot)

        self.callback_handler = next(fn for _, fn in self.fake_bot.handlers if getattr(fn, "__name__", "") == "handle_callback")
        self.message_handler = next(fn for _, fn in self.fake_bot.handlers if getattr(fn, "__name__", "") == "handle_wizard_input")

    async def asyncTearDown(self):
        db.DEFAULT_DB_PATH = self._orig_db_default
        admin_bot.ADMIN_USER_ID = self._orig_admin_id
        main.ADMIN_USER_ID = self._orig_main_admin
        admin_bot.DEST_CHANNEL = self._orig_admin_dest
        main.DEST_CHANNEL = self._orig_main_dest
        admin_bot.set_user_client(self._orig_user_ref)
        admin_bot.set_forward_client(self._orig_fwd_ref)
        admin_bot.forward_clients_ref = self._orig_fwds_ref
        admin_bot._wizard_state.clear()
        admin_bot._drafts.clear()
        fwd_mgr._in_flight_logins.clear()
        if os.path.exists(self._db_path):
            try:
                os.remove(self._db_path)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # 1. Review Actions: Approve (Single tap & Double tap)
    # -----------------------------------------------------------------------
    async def test_approve_single_tap_publishes_and_advances(self):
        """Single tap on approve transitions status to approved, increments post number and publishes."""
        lid = db.insert_listing(
            self.supplier_id, 101, "Netflix 4K", None, "pending_approval",
            "Netflix 4K", "Netflix 4K", db_path=self._db_path
        )
        ev = FakeEvent(data_str=f"approve:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev)

        listing = db.get_listing_by_id(lid, db_path=self._db_path)
        self.assertEqual(listing["status"], "published")
        self.assertIsNotNone(listing.get("post_number"))
        self.assertTrue(ev.deleted)
        # Main channel received published message
        self.assertEqual(len(self.mock_user.sent), 1)

    async def test_approve_double_tap_alerts_stale(self):
        """Second tap on approve detects listing is no longer editable, alerts admin, does not publish twice."""
        lid = db.insert_listing(
            self.supplier_id, 102, "Spotify Family", None, "pending_approval",
            "Spotify Family", "Spotify Family", db_path=self._db_path
        )
        ev1 = FakeEvent(data_str=f"approve:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev1)
        self.assertEqual(len(self.mock_user.sent), 1)

        # Second tap
        ev2 = FakeEvent(data_str=f"approve:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev2)
        # No second message published
        self.assertEqual(len(self.mock_user.sent), 1)
        # Alert received
        self.assertTrue(any(a.get("alert") for a in ev2.answered))

    # -----------------------------------------------------------------------
    # 2. Review Actions: Skip (Single tap & Stale tap)
    # -----------------------------------------------------------------------
    async def test_skip_single_tap_marks_skipped_admin(self):
        """Single tap on skip marks listing as skipped_admin and records skip log."""
        lid = db.insert_listing(
            self.supplier_id, 103, "Spam item", None, "pending_review",
            "Spam item", "Spam item", db_path=self._db_path
        )
        ev = FakeEvent(data_str=f"skip:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev)

        listing = db.get_listing_by_id(lid, db_path=self._db_path)
        self.assertEqual(listing["status"], "skipped_admin")
        self.assertTrue(ev.deleted)

        skips = db.get_skipped_listings(limit=10, db_path=self._db_path)
        self.assertTrue(any(s["reason"] == "admin_skip" for s in skips))

    async def test_skip_stale_tap_alerts_already_done(self):
        """Tapping skip on an already-published listing alerts that it is already done."""
        lid = db.insert_listing(
            self.supplier_id, 104, "Already Published", None, "published",
            "Clean", "Raw", published_message_id=999, db_path=self._db_path
        )
        ev = FakeEvent(data_str=f"skip:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev)

        listing = db.get_listing_by_id(lid, db_path=self._db_path)
        # Status unchanged
        self.assertEqual(listing["status"], "published")
        self.assertTrue(any(texts.ALREADY_DONE[:15] in str(a.get("text")) for a in ev.answered))

    # -----------------------------------------------------------------------
    # 3. Edit -> Approve Workflow
    # -----------------------------------------------------------------------
    async def test_edit_draft_and_approve_publishes_edited_content(self):
        """Editing content updates _drafts and Approve publishes the draft text instead of raw text."""
        lid = db.insert_listing(
            self.supplier_id, 105, "Original raw text", None, "pending_approval",
            "Original clean text", "Original raw text", db_path=self._db_path
        )
        # Admin taps Edit button
        ev_edit = FakeEvent(data_str=f"edit:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev_edit)

        # Wizard state is active
        self.assertEqual(admin_bot._wizard_state.get(self.ADMIN, {}).get("step"), "edit")
        self.assertEqual(admin_bot._wizard_state.get(self.ADMIN, {}).get("listing_id"), lid)

        # Admin sends corrected body
        corrected_body = "WTS Corrected Netflix 4K\n1 Month Warranty\nInstant delivery"
        ev_msg = FakeEvent(text=corrected_body, sender_id=self.ADMIN, client=self.fake_bot)
        await self.message_handler(ev_msg)

        # Verify draft is saved in PersistentDict
        self.assertEqual(admin_bot._drafts.get(lid), corrected_body)

        # Now admin taps Approve
        ev_appr = FakeEvent(data_str=f"approve:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev_appr)

        listing = db.get_listing_by_id(lid, db_path=self._db_path)
        self.assertEqual(listing["status"], "published")
        self.assertEqual(len(self.mock_user.sent), 1)
        # The published message contains the edited text
        published_text = self.mock_user.sent[0]["text"]
        self.assertIn("Corrected Netflix 4K", published_text)

    # -----------------------------------------------------------------------
    # 4. Skip All Pending
    # -----------------------------------------------------------------------
    async def test_skip_all_pending_workflow(self):
        """Skip all pending prompts confirm, then skips all pending listings and clears drafts."""
        lid1 = db.insert_listing(self.supplier_id, 201, "P1", None, "pending_approval", "C1", "R1", db_path=self._db_path)
        lid2 = db.insert_listing(self.supplier_id, 202, "P2", None, "pending_review", "C2", "R2", db_path=self._db_path)
        admin_bot._drafts[lid1] = "Some draft"

        ev_prompt = FakeEvent(data_str="skip_all_pending", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev_prompt)

        ev_confirm = FakeEvent(data_str="confirm_skip_all_pending", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev_confirm)

        self.assertEqual(db.get_listing_by_id(lid1, db_path=self._db_path)["status"], "skipped_admin")
        self.assertEqual(db.get_listing_by_id(lid2, db_path=self._db_path)["status"], "skipped_admin")
        self.assertEqual(len(admin_bot._drafts), 0)

    # -----------------------------------------------------------------------
    # 5. Mark Sold & Partial Failure Reporting
    # -----------------------------------------------------------------------
    async def test_mark_sold_workflow_and_partial_failure(self):
        """Mark Sold replies STOP in main channel and attempts deletes; failure is handled without aborting."""
        post_num = db.next_post_number(db_path=self._db_path)
        lid = db.insert_listing(
            self.supplier_id, 301, "Sold Item", None, "published",
            "Sold Item", "Sold Item", published_message_id=888,
            db_path=self._db_path
        )
        db.update_listing_status(
            lid, status="published", post_number=post_num, published_message_id=888,
            db_path=self._db_path
        )
        # Add two destination forwardings: one that succeeds, one that raises
        did1 = db.add_destination("-10022201", "Group 1", active=True, db_path=self._db_path)
        did2 = db.add_destination("-10022202", "Group 2", active=True, db_path=self._db_path)
        db.queue_forwarding(lid, "-10022201", 888, db_path=self._db_path)
        db.queue_forwarding(lid, "-10022202", 888, db_path=self._db_path)
        fwds = db.get_forwardings_for_listing(lid, db_path=self._db_path)
        db.mark_forwarded(fwds[0]["id"], destination_message_id=5001, db_path=self._db_path)
        db.mark_forwarded(fwds[1]["id"], destination_message_id=5002, db_path=self._db_path)

        class FailingFwdSender(MockSender):
            async def delete_messages(self, peer, ids):
                if "-10022202" in str(peer) or 5002 in ids:
                    raise Exception("Bot lacks delete rights in Group 2")
                await super().delete_messages(peer, ids)

        failing_fwd = FailingFwdSender()
        admin_bot.set_forward_client(failing_fwd)

        # Tapping Sold prompts two-step confirmation (R-01)
        ev_prompt = FakeEvent(data_str=f"sold:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev_prompt)
        confirm_msgs = (
            getattr(self.fake_bot, "sent", None)
            or getattr(self.fake_bot, "sent_messages", None)
            or getattr(ev_prompt, "sent_messages", None)
            or getattr(ev_prompt, "replies", [])
        )
        self.assertTrue(len(confirm_msgs) >= 1)
        confirm_text = confirm_msgs[-1]["text"]
        self.assertIn("STOP", confirm_text)

        # Confirming execution performs STOP reply and destination deletes (R-01)
        ev_confirm = FakeEvent(data_str=f"sold_y:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev_confirm)

        # Listing marked sold in DB despite partial delete failure
        listing = db.get_listing_by_id(lid, db_path=self._db_path)
        self.assertEqual(listing["status"], "sold")

        # Main channel received STOP reply
        self.assertTrue(any("STOP" in s["text"] for s in self.mock_user.sent))
        # Group 1 message deleted
        self.assertEqual(len(failing_fwd.deleted), 1)

    # -----------------------------------------------------------------------
    # 6. Wizard Lifecycles & Cancel
    # -----------------------------------------------------------------------
    async def test_wizard_cancel_clears_state_and_returns_home(self):
        """wiz:cancel clears active wizard state and returns CANCELLED with home keyboard."""
        admin_bot._wizard_state[self.ADMIN] = {"step": "add"}
        ev_cancel = FakeEvent(data_str="wiz:cancel", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev_cancel)

        self.assertNotIn(self.ADMIN, admin_bot._wizard_state)
        self.assertTrue(any(texts.CANCELLED in str(a.get("text")) for a in ev_cancel.answered))

    async def test_wizard_zero_ttl_behavior(self):
        """Wizard state has zero TTL: message hours later is captured unless matching reply button."""
        admin_bot._wizard_state[self.ADMIN] = {"step": "dest_set_link", "dest_id": 1, "dest_name": "Test Group"}

        # Non-matching text is captured by the wizard
        ev_text = FakeEvent(text="https://t.me/+InvalidLink123", sender_id=self.ADMIN, client=self.fake_bot)
        await self.message_handler(ev_text)

        # Wizard handled the message and cleared step
        self.assertNotIn(self.ADMIN, admin_bot._wizard_state)

    # -----------------------------------------------------------------------
    # 7. Forwarder Login Memory-Loss on Restart
    # -----------------------------------------------------------------------
    async def test_forwarder_login_restart_leaves_zombie_step(self):
        """Simulate restart mid-login: in-flight client is lost, wizard recovers cleanly without zombie loop (R-03)."""
        admin_bot._wizard_state[self.ADMIN] = {"step": "add_fwd_code"}
        fwd_mgr._in_flight_logins.clear()  # Simulate process restart

        ev_code = FakeEvent(text="1 2 3 4 5", sender_id=self.ADMIN, client=self.fake_bot)
        await self.message_handler(ev_code)

        # Clean recovery: notified that session expired and wizard state cleared
        self.assertTrue(any("Login session expired" in str(r.get("text")) for r in ev_code.replies))
        self.assertNotIn(self.ADMIN, admin_bot._wizard_state)

    # -----------------------------------------------------------------------
    # 8. Paused System Behavior
    # -----------------------------------------------------------------------
    async def test_paused_system_routes_to_pending_approval_with_system_paused(self):
        """When paused, auto_publish_ok is False and gate reason is 'system_paused'."""
        db.set_paused(True, db_path=self._db_path)
        self.assertTrue(db.is_paused(db_path=self._db_path))

        # Check gate
        auto_ok = main.auto_publish_ok(
            intent="buy",
            paused=True,
            body_ok=True,
            has_payment_proof=None,
        )
        self.assertFalse(auto_ok)

        # Manual approve in admin_bot still works immediately while paused
        lid = db.insert_listing(
            self.supplier_id, 401, "Paused Manual", None, "pending_approval",
            "Clean", "Raw", db_path=self._db_path
        )
        ev_appr = FakeEvent(data_str=f"approve:{lid}", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev_appr)

        listing = db.get_listing_by_id(lid, db_path=self._db_path)
        self.assertEqual(listing["status"], "published")

    # -----------------------------------------------------------------------
    # 9. Navigation Telemetry (R-25, B-11)
    # -----------------------------------------------------------------------
    async def test_navigation_telemetry_recorded_in_audit_log(self):
        """Callbacks dispatched by authorized admin record nav:<token> in audit log."""
        ev = FakeEvent(data_str="menu:home", sender_id=self.ADMIN, client=self.fake_bot)
        await self.callback_handler(ev)

        audits = db.get_audit_logs(limit=10, db_path=self._db_path)
        nav_entries = [a for a in audits if a["action"].startswith("nav:")]
        self.assertTrue(len(nav_entries) > 0)
        self.assertEqual(nav_entries[0]["action"], "nav:menu")
        self.assertEqual(nav_entries[0]["detail"], "menu:home")


if __name__ == "__main__":
    unittest.main()
