"""Contract Tests for Phase 4: Workflow Depth (R-17, R-20, R-21, R-23)."""

import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

import admin_bot
import db


class FakeBotClient:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, peer, text, buttons=None, **kw):
        msg_id = len(self.sent) + 100
        msg = type("Msg", (), {"id": msg_id, "text": text, "buttons": buttons})()
        self.sent.append(msg)
        return msg

    async def edit_message(self, peer, msg_id, text, buttons=None, **kw):
        self.edited.append({"peer": peer, "id": msg_id, "text": text, "buttons": buttons})
        return True


class Phase4WorkflowsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._db_path = os.path.join(tempfile.gettempdir(), f"test_p4_{os.getpid()}_{id(self)}.db")
        db.init_db(self._db_path)
        self._orig_db_default = db.DEFAULT_DB_PATH
        db.DEFAULT_DB_PATH = self._db_path
        self._orig_admin_id = admin_bot.ADMIN_USER_ID
        admin_bot.ADMIN_USER_ID = 888777

    async def asyncTearDown(self):
        db.DEFAULT_DB_PATH = self._orig_db_default
        admin_bot.ADMIN_USER_ID = self._orig_admin_id
        if os.path.exists(self._db_path):
            try:
                os.remove(self._db_path)
            except Exception:
                pass

    # 1. R-21 / B-6 Card Collapsing
    async def test_card_collapsing_on_resolution(self):
        fake_bot = FakeBotClient()
        sid = db.add_supplier("test_sup", db_path=self._db_path)
        lid = db.insert_listing(sid, 101, "Raw", None, "pending_approval", "Clean", "Clean", db_path=self._db_path)
        listing = db.get_listing_by_id(lid, db_path=self._db_path)

        # Send approval prompt -> records prompt_msg:<id>
        await admin_bot.send_approval_prompt(fake_bot, admin_bot.ADMIN_USER_ID, listing)
        self.assertEqual(len(fake_bot.sent), 1)
        prompt_msg_id = fake_bot.sent[0].id
        stored_id = db.get_setting(f"prompt_msg:{lid}", db_path=self._db_path)
        self.assertEqual(stored_id, str(prompt_msg_id))

        # Collapse card on resolution
        await admin_bot.collapse_listing_card(fake_bot, admin_bot.ADMIN_USER_ID, lid, "✅ Approved")
        self.assertEqual(len(fake_bot.edited), 1)
        self.assertEqual(fake_bot.edited[0]["id"], prompt_msg_id)
        self.assertIn(f"Approved #{lid}", fake_bot.edited[0]["text"])
        self.assertIsNone(fake_bot.edited[0]["buttons"])
        # Setting cleaned up
        self.assertIsNone(db.get_setting(f"prompt_msg:{lid}", db_path=self._db_path))

    # 2. R-23 / B-13 Approve Hold Window
    def test_approve_hold_window_in_listings(self):
        sid = db.add_supplier("test_sup", db_path=self._db_path)
        
        # Listing 1: Approved with future hold_until -> NOT ready to publish
        future_iso = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        lid1 = db.insert_listing(
            sid, 201, "Raw 1", None, "approved", "Clean 1", "Clean 1",
            hold_until=future_iso, db_path=self._db_path
        )

        # Listing 2: Approved with past hold_until -> ready to publish
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        lid2 = db.insert_listing(
            sid, 202, "Raw 2", None, "approved", "Clean 2", "Clean 2",
            hold_until=past_iso, db_path=self._db_path
        )

        # Listing 3: Approved with NULL hold_until -> ready to publish immediately
        lid3 = db.insert_listing(
            sid, 203, "Raw 3", None, "approved", "Clean 3", "Clean 3",
            hold_until=None, db_path=self._db_path
        )

        to_publish = db.get_approved_listings_to_publish(limit=10, db_path=self._db_path)
        published_ids = [l["id"] for l in to_publish]

        self.assertNotIn(lid1, published_ids, "Future hold_until listing must be held")
        self.assertIn(lid2, published_ids, "Past hold_until listing must be ready")
        self.assertIn(lid3, published_ids, "NULL hold_until listing must be ready")

    # 3. R-20 / B-10 Main Channel Precedence
    async def test_main_channel_precedence(self):
        # Setting in DB takes precedence over .env
        db.set_dest_channel("@db_channel", db_path=self._db_path)
        current = db.get_dest_channel(db_path=self._db_path)
        self.assertEqual(current, "@db_channel")

