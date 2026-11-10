"""Contract & Unit Tests for Phase 3 Screens (Blueprint §4, R-06 through R-16)."""

import os
import tempfile
import unittest
from telethon import Button

import db
from services.tokens import parse_token, MAX_TOKEN_BYTES
from services.screens.home import render_home_screen, get_home_reply_keyboard
from services.screens.review import (
    render_review_card,
    render_review_more_overlay,
    render_review_empty_state,
    render_review_edit_prompt,
)
from services.screens.posts import (
    render_posts_list,
    render_post_card,
    render_post_fill_confirm,
)
from services.screens.delivery import (
    render_delivery_hub,
    render_group_card,
    render_main_channel_screen,
    render_headers_screen,
    render_forwarders_screen,
    render_forwarder_card,
)
from services.screens.sources import (
    render_sources_list,
    render_source_card,
    render_source_remove_confirm,
)
from services.screens.skipped import render_skipped_hub, get_filtered_skipped
from services.screens.system import render_system_screen


def _verify_all_buttons_valid(payload):
    """Verify that every button has valid token bytes <= 64 bytes and parses cleanly."""
    if not payload.buttons:
        return
    for row in payload.buttons:
        for btn in row:
            data = getattr(btn, "data", None)
            if data is not None:
                if isinstance(data, bytes):
                    token_str = data.decode("utf-8")
                else:
                    token_str = str(data)
                raw_len = len(token_str.encode("utf-8"))
                assert raw_len <= MAX_TOKEN_BYTES, f"Token {token_str!r} exceeded {MAX_TOKEN_BYTES} bytes ({raw_len})"
                parsed = parse_token(token_str)
                assert parsed is not None, f"Token {token_str!r} failed to parse"


class Phase3ScreensTests(unittest.TestCase):
    def setUp(self):
        self._db_path = os.path.join(tempfile.gettempdir(), f"test_p3_{os.getpid()}_{id(self)}.db")
        db.init_db(self._db_path)

    def tearDown(self):
        if os.path.exists(self._db_path):
            try:
                os.remove(self._db_path)
            except Exception:
                pass

    def test_home_screen(self):
        # Empty DB home
        p = render_home_screen(db_path=self._db_path)
        self.assertIn("Control Center", p.text)
        self.assertIn("Nothing needs you", p.text)
        _verify_all_buttons_valid(p)

        # Home with pending item & problem group
        sid = db.add_supplier("test_sup", db_path=self._db_path)
        db.insert_listing(sid, 101, "Test raw", None, "pending_review", "Clean", "Clean", db_path=self._db_path)
        did = db.add_destination("-100999", "Bad Group", active=True, db_path=self._db_path)
        db.set_destination_link_state(did, "needs_link", db_path=self._db_path)

        p2 = render_home_screen(db_path=self._db_path)
        self.assertIn("Needs you:", p2.text)
        self.assertIn("1 to review", p2.text)
        _verify_all_buttons_valid(p2)

        # 4-button persistent reply keyboard
        kb = get_home_reply_keyboard(db_path=self._db_path)
        self.assertEqual(len(kb), 2)
        self.assertEqual(len(kb[0]), 2)
        self.assertEqual(len(kb[1]), 2)

    def test_review_screen(self):
        sid = db.add_supplier("test_sup", db_path=self._db_path)
        lid = db.insert_listing(sid, 201, "Netflix", None, "pending_approval", "Raw text", "Formatted Netflix 4k", db_path=self._db_path)
        listing = db.get_listing_by_id(lid, db_path=self._db_path)

        # WYSIWYG review card
        p = render_review_card(listing, page_idx=0, total_count=3, toggle_mode="proposed", undo_skip_id=99, db_path=self._db_path)
        self.assertIn("Manual review", p.text)
        self.assertIn("Netflix", p.text)
        _verify_all_buttons_valid(p)

        # Toggle to original
        p_orig = render_review_card(listing, page_idx=0, total_count=3, toggle_mode="original", db_path=self._db_path)
        self.assertIn("Original Supplier Text", p_orig.text)
        _verify_all_buttons_valid(p_orig)

        # More overlay
        p_more = render_review_more_overlay(listing, total_pending=3)
        self.assertIn("Review Actions", p_more.text)
        _verify_all_buttons_valid(p_more)

        # Empty state
        p_empty = render_review_empty_state(db_path=self._db_path)
        self.assertIn("Queue is clear", p_empty.text)
        _verify_all_buttons_valid(p_empty)

        # Edit prompt
        p_edit = render_review_edit_prompt(listing)
        self.assertIn(f"Edit #{lid}", p_edit.text)
        _verify_all_buttons_valid(p_edit)

    def test_posts_screen(self):
        # Empty posts
        p_empty = render_posts_list(page=0, db_path=self._db_path)
        self.assertIn("No published posts yet", p_empty.text)
        _verify_all_buttons_valid(p_empty)

        # With published post
        sid = db.add_supplier("test_sup", db_path=self._db_path)
        pnum = db.next_post_number(db_path=self._db_path)
        lid = db.insert_listing(sid, 301, "Raw", None, "published", "Clean", "Clean", published_message_id=555, db_path=self._db_path)
        db.update_listing_status(lid, status="published", post_number=pnum, published_message_id=555, db_path=self._db_path)

        p_list = render_posts_list(page=0, db_path=self._db_path)
        self.assertIn("Posts", p_list.text)
        all_btn_text = [b.text for row in p_list.buttons for b in row]
        self.assertTrue(any(f"#{pnum}" in t for t in all_btn_text))
        _verify_all_buttons_valid(p_list)

        # Post detail card
        p_card = render_post_card(pnum, from_page=0, db_path=self._db_path)
        self.assertIn(f"Post #{pnum}", p_card.text)
        _verify_all_buttons_valid(p_card)

        # Fill confirm
        p_fill = render_post_fill_confirm(pnum, from_page=0, db_path=self._db_path)
        self.assertIn(f"Mark Post #{pnum} as Filled", p_fill.text)
        _verify_all_buttons_valid(p_fill)

    def test_delivery_screens(self):
        # Delivery hub
        did = db.add_destination("-100888", "Delivery Dest", active=True, db_path=self._db_path)
        p_dlv = render_delivery_hub(page=0, db_path=self._db_path)
        self.assertIn("Delivery Hub", p_dlv.text)
        all_dlv_btns = [b.text for row in p_dlv.buttons for b in row]
        self.assertTrue(any("Delivery Dest" in t for t in all_dlv_btns))
        _verify_all_buttons_valid(p_dlv)

        # Group card
        p_grp = render_group_card(did, from_page=0, db_path=self._db_path)
        self.assertIn("Delivery Dest", p_grp.text)
        _verify_all_buttons_valid(p_grp)

        # Main channel
        p_main = render_main_channel_screen(db_path=self._db_path)
        self.assertIn("Main Channel", p_main.text)
        _verify_all_buttons_valid(p_main)

        # Headers
        db.add_header([{"alt": "W", "doc_id": 1}], db_path=self._db_path)
        p_hdr = render_headers_screen(db_path=self._db_path)
        self.assertIn("Custom Emoji Headers", p_hdr.text)
        _verify_all_buttons_valid(p_hdr)

        # Forwarders
        aid = db.add_forward_account("TestAcct", phone="+1234567890", db_path=self._db_path)
        p_fwd = render_forwarders_screen(db_path=self._db_path)
        self.assertIn("Forwarders", p_fwd.text)
        _verify_all_buttons_valid(p_fwd)

        p_fwdc = render_forwarder_card(aid, db_path=self._db_path)
        self.assertIn("TestAcct", p_fwdc.text)
        _verify_all_buttons_valid(p_fwdc)

    def test_sources_screens(self):
        sid = db.add_supplier("src_awesome", db_path=self._db_path)
        p_list = render_sources_list(page=0, db_path=self._db_path)
        all_src_btns = [b.text for row in p_list.buttons for b in row]
        self.assertTrue(any("@src_awesome" in t for t in all_src_btns))
        _verify_all_buttons_valid(p_list)

        p_card = render_source_card(sid, from_page=0, db_path=self._db_path)
        self.assertIn("@src_awesome", p_card.text)
        _verify_all_buttons_valid(p_card)

        p_rm = render_source_remove_confirm(sid, from_page=0, db_path=self._db_path)
        self.assertIn("Remove @src_awesome", p_rm.text)
        _verify_all_buttons_valid(p_rm)

    def test_skipped_screen(self):
        sid = db.add_supplier("skip_src", db_path=self._db_path)
        db.log_skip(sid, 999, "filter", "dropped message", db_path=self._db_path)
        p_skp = render_skipped_hub(filter_code="n", page=0, db_path=self._db_path)
        self.assertIn("Skipped Messages", p_skp.text)
        _verify_all_buttons_valid(p_skp)

    def test_system_screen_tabs(self):
        # Overview tab
        p_ov = render_system_screen(tab="ov", db_path=self._db_path)
        self.assertIn("System — Overview", p_ov.text)
        _verify_all_buttons_valid(p_ov)

        # Workers tab
        p_wk = render_system_screen(tab="wk", db_path=self._db_path)
        self.assertIn("Workers & Connectivity", p_wk.text)
        _verify_all_buttons_valid(p_wk)

        # Stats tab
        p_st = render_system_screen(tab="st", db_path=self._db_path)
        self.assertIn("Today's Stats", p_st.text)
        _verify_all_buttons_valid(p_st)
