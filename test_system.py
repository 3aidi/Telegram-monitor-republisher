"""Comprehensive unit tests for db, parser, and filters modules for financial/streaming account reselling."""

import os
import sqlite3
import tempfile
import time
import unittest
import emoji

import ai_rephraser
import db
import filters
import parser

TEST_DB = "test_monitor.db"


class TestMonitorSystem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except OSError:
                pass
        db.init_db(TEST_DB)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except OSError:
                pass

    # -------------------------------------------------------------
    # PARSER TESTS
    # -------------------------------------------------------------
    def test_emoji_stripping(self):
        raw = "🔥 WTB Bybit via link ✅ European Verified ⚡ Rate $50 🔥"
        clean = parser.strip_all_emoji(raw)
        self.assertNotIn("🔥", clean)
        self.assertNotIn("✅", clean)
        self.assertNotIn("⚡", clean)
        self.assertIn("WTB Bybit via link", clean)

    def test_price_extraction(self):
        price, span = parser.extract_price("Netflix 1 Month $40")
        self.assertEqual(price, 40.0)
        self.assertEqual(span, "$40")

        price2, span2 = parser.extract_price("Wtb buddybank it Price 130$")
        self.assertEqual(price2, 130.0)

        price3, span3 = parser.extract_price("Hetzner Instant price 7 USD")
        self.assertEqual(price3, 7.0)

        price4, span4 = parser.extract_price("Bybit account Rate 50 USD")
        self.assertEqual(price4, 50.0)

        price5, span5 = parser.extract_price("Crypto.com 75 EUR")
        self.assertEqual(price5, 75.0)

        price6, span6 = parser.extract_price("No price mentioned here")
        self.assertIsNone(price6)

    def test_price_thousands_separator(self):
        # Regression: "$1,200" must parse as 1200, not 1
        price, span = parser.extract_price("Bybit premium $1,200 full kyc")
        self.assertEqual(price, 1200.0)
        self.assertEqual(span, "$1,200")

        # Regression: "1,200$" must parse as 1200, not 200
        price2, span2 = parser.extract_price("Buddybank it 1,200$")
        self.assertEqual(price2, 1200.0)
        self.assertEqual(span2, "1,200$")

        # Decimal-comma: 12,50 -> 12.5
        price3, _ = parser.extract_price("Netflix 12,50€")
        self.assertAlmostEqual(price3, 12.5)

        # Thousands + decimals
        price4, _ = parser.extract_price("Wise account $1,200.50")
        self.assertAlmostEqual(price4, 1200.5)

    def test_pricing_multiplier(self):
        self.assertEqual(parser.apply_pricing_rule(100.0, 0.75), 75)
        self.assertEqual(parser.apply_pricing_rule(130.0, 0.75), 98)
        self.assertEqual(parser.apply_pricing_rule(7.0, 0.75), 6)
        self.assertEqual(parser.apply_pricing_rule(50.0, 0.75), 38)

    def test_price_multiplier_applies_to_all_intents(self):
        """The multiplier is applied to buy, sell, and neutral prices alike —
        auto-publish is buy-only, so buy prices MUST be repriced from source
        (source $60 must become $45 at 0.75x)."""
        import main as main_mod
        self.assertEqual(main_mod._price_for_dispatch(60, 0.75, "buy"), 45)
        self.assertEqual(main_mod._price_for_dispatch(60.0, 0.75, "buy"), 45)
        self.assertEqual(main_mod._price_for_dispatch(13.5, 0.75, "buy"), 11)
        self.assertEqual(main_mod._price_for_dispatch(100.0, 0.75, "sell"), 75)
        self.assertEqual(main_mod._price_for_dispatch(130.0, 1.0, "neutral"), 130)
        self.assertEqual(main_mod._price_for_dispatch(None, 0.75, "buy"), None)

    def test_price_formatting(self):
        """Whole-number prices render without '.0'; decimals preserved."""
        self.assertEqual(parser._format_price(38.0), "38")
        self.assertEqual(parser._format_price(98), "98")
        self.assertEqual(parser._format_price(12.50), "12.5")
        self.assertEqual(parser._format_price(None), "")
        msg, _ = parser.build_ai_message(
            content_lines=["Bybit kyc", "da"],
            our_price=38.0,
            platform="bybit",
            contact_username="@x",
            intent="sell",
        )
        self.assertIn("Price  : $38", msg)
        self.assertNotIn("$38.0", msg)

    def test_custom_emoji_entity_offsets_valid(self):
        """Every entity must point inside the final text, ascending and non-overlapping."""
        msg, entities = parser.build_ai_message(
            content_lines=["Netflix 1 month", "4K ready"],
            our_price=30,
            platform="netflix",
            contact_username="@buy",
            intent="sell",
        )
        units = len(msg.encode("utf-16-le")) // 2
        prev = -1
        for e in entities:
            self.assertGreater(e.offset, prev, "entity offsets must be strictly ascending")
            self.assertGreaterEqual(e.offset, 0)
            self.assertLessEqual(e.offset + e.length, units)
            prev = e.offset
        # Header: second custom emoji sits right after "🔥 NETFLIX WTB ✦ DM FAST "
        header_prefix = parser.PH_FIRE + " NETFLIX WTB ✦ DM FAST "
        self.assertEqual(entities[1].offset, len(header_prefix.encode("utf-16-le")) // 2)

    # -------------------------------------------------------------
    # DATABASE TESTS
    # -------------------------------------------------------------
    def test_db_suppliers(self):
        db.add_supplier("@supplier_test1", channel_id=-100111, markup_multiplier=0.75, db_path=TEST_DB)
        db.add_supplier("@supplier_test2", channel_id=-100222, markup_multiplier=0.80, db_path=TEST_DB)

        suppliers = db.list_suppliers(active_only=True, db_path=TEST_DB)
        self.assertGreaterEqual(len(suppliers), 2)

        # Update rule
        db.set_supplier_rule("supplier_test1", 0.70, db_path=TEST_DB)
        sup1 = db.get_supplier_by_chat(chat_id=-100111, db_path=TEST_DB)
        self.assertIsNotNone(sup1)
        self.assertEqual(sup1["markup_multiplier"], 0.70)

        # Remove supplier
        db.remove_supplier("supplier_test2", db_path=TEST_DB)
        sup2 = db.get_supplier_by_chat(chat_id=-100222, db_path=TEST_DB)
        self.assertEqual(sup2["active"], 0)

        # Numeric channel id lookup
        sup_by_id = db.get_supplier_by_chat(chat_id=-100111, db_path=TEST_DB)
        self.assertIsNotNone(sup_by_id)

    def test_db_add_supplier_merge_prefers_real_username(self):
        """Re-adding a channel by numeric ID keeps the existing REAL username
        instead of overwriting it with the raw ID string (ID-based sources must
        never destroy the friendly label)."""
        db_path = TEST_DB
        db.add_supplier("kycgroupke", channel_id=-100123, markup_multiplier=0.75, db_path=db_path)
        # Same channel re-added by its numeric id -> single row, username preserved.
        db.add_supplier("-100123", channel_id=-100123, markup_multiplier=0.80, db_path=db_path)
        suppliers = db.list_suppliers(db_path=db_path)
        matches = [s for s in suppliers if s["channel_id"] == -100123]
        self.assertEqual(len(matches), 1, "channel must not be duplicated across rows")
        row = matches[0]
        self.assertEqual(row["channel_username"], "kycgroupke")
        self.assertEqual(row["markup_multiplier"], 0.75, "re-add must preserve the existing multiplier")
        self.assertEqual(row["active"], 1)

    def test_db_add_supplier_upgrades_numeric_placeholder_to_username(self):
        """A supplier seeded with only a numeric id is upgraded to the real
        username when the channel is later added by username."""
        db_path = TEST_DB
        db.add_supplier("-100456", channel_id=-100456, markup_multiplier=0.75, db_path=db_path)
        db.add_supplier("@renamedchan", channel_id=-100456, markup_multiplier=0.75, db_path=db_path)
        suppliers = db.list_suppliers(db_path=db_path)
        matches = [s for s in suppliers if s["channel_id"] == -100456]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["channel_username"], "renamedchan")

    def test_db_merge_supplier_rows_never_overwrites_keep_channel_id(self):
        """Merging two rows that BOTH hold a non-null channel_id must never crash
        UNIQUE(channel_id): the keep row's id is definitionally correct, so it is
        preserved and only the drop row is removed. (Regression: the old code
        wrote the drop row's id onto the keep row while the drop row still held
        it -> sqlite3.IntegrityError during resolve_supplier_entities.)"""
        db_path = TEST_DB
        keep = db.add_supplier("ownschan", channel_id=-100500, markup_multiplier=0.70, db_path=db_path)
        drop = db.add_supplier("stalechan", channel_id=-100501, markup_multiplier=0.90, db_path=db_path)
        ok = db.merge_supplier_rows(keep, drop, db_path=db_path)
        self.assertTrue(ok)
        kept = db.get_supplier_by_id(keep, db_path=db_path)
        self.assertIsNotNone(kept)
        self.assertEqual(kept["channel_id"], -100500, "keep row's channel_id must never be overwritten")
        self.assertEqual(kept["active"], 1)
        self.assertIsNone(db.get_supplier_by_id(drop, db_path=db_path), "drop row must be deleted")

    def test_db_merge_supplier_rows_backfills_null_keep_channel_id(self):
        """The ONLY case where channel_id is copied is when the keep row's is NULL:
        a previously-unresolved keep row adopts the drop row's resolved id after
        the drop row's unique slot is freed."""
        db_path = TEST_DB
        keep = db.add_supplier("noidchan", channel_id=None, markup_multiplier=0.75, db_path=db_path)
        drop = db.add_supplier("resolvedchan", channel_id=-100502, markup_multiplier=0.75, db_path=db_path)
        ok = db.merge_supplier_rows(keep, drop, db_path=db_path)
        self.assertTrue(ok)
        kept = db.get_supplier_by_id(keep, db_path=db_path)
        self.assertEqual(kept["channel_id"], -100502)
        self.assertIsNone(db.get_supplier_by_id(drop, db_path=db_path))

    @staticmethod
    def _fresh_db(name: str) -> str:
        path = os.path.join(tempfile.gettempdir(), name)
        try:
            os.remove(path)
        except OSError:
            pass
        db.init_db(path)
        return path

    def test_db_env_seed_bootstrap_once_and_never_again(self):
        """Fresh/empty DB + populated SOURCE_CHANNELS -> seeded once and the marker
        is set; a later restart must NOT re-seed, so a supplier deleted between the
        two startups does NOT reappear (env is one-time bootstrap, not live state)."""
        path = self._fresh_db("env_seed_bootstrap_test.db")
        channels = ["@chanone", "-100111"]
        result1 = db.ensure_env_seed(channels, 0.75, db_path=path)
        self.assertEqual(result1["state"], "seeded")
        self.assertEqual(result1["seeded"], 2)
        self.assertTrue(db.env_seed_completed(db_path=path))
        self.assertEqual(db.count_suppliers(db_path=path), 2)

        row = db.get_supplier_by_chat(username="chanone", db_path=path)
        db.delete_supplier(row["id"], db_path=path)

        result2 = db.ensure_env_seed(channels, 0.75, db_path=path)
        self.assertEqual(result2["state"], "already_seeded")
        self.assertEqual(result2["seeded"], 0)
        self.assertIsNone(
            db.get_supplier_by_chat(username="chanone", db_path=path),
            "deleted supplier must not reappear after restart",
        )
        self.assertEqual(db.count_suppliers(db_path=path), 1)
        db.clear_env_seed_completed(db_path=path)

    def test_db_env_seed_migrates_existing_db_without_changes(self):
        """Production migration: existing non-empty suppliers table + unset marker
        -> marker set WITHOUT calling the env sync, and no rows added/removed/
        duplicated as a side effect of the deploy."""
        path = self._fresh_db("env_seed_migrate_test.db")
        db.add_supplier("@exist1", channel_id=-100400, db_path=path)
        db.add_supplier("@exist2", channel_id=-100401, db_path=path)
        before = [dict(r) for r in db.list_suppliers(db_path=path)]
        self.assertFalse(db.env_seed_completed(db_path=path))

        result = db.ensure_env_seed(["@wouldhavebeen", "-100999"], 0.75, db_path=path)
        self.assertEqual(result["state"], "migrated")
        self.assertTrue(result["migrated"])
        self.assertEqual(result["seeded"], 0)
        after = [dict(r) for r in db.list_suppliers(db_path=path)]
        self.assertEqual(before, after, "migration must not modify any supplier row")
        self.assertTrue(db.env_seed_completed(db_path=path))

        again = db.ensure_env_seed(["@alsoignored"], 0.75, db_path=path)
        self.assertEqual(again["state"], "already_seeded")
        self.assertEqual(db.count_suppliers(db_path=path), 2)
        db.clear_env_seed_completed(db_path=path)

    def test_db_env_seed_reseed_escape_hatch(self):
        """/reseed_from_env path: the handler clears the marker, runs the seed sync
        DIRECTLY (bypassing the empty-table migration guard), and re-sets the marker.
        Existing suppliers are preserved, new ones are added, and restarts keep
        ignoring .env afterwards."""
        path = self._fresh_db("env_seed_reseed_test.db")
        db.ensure_env_seed(["@a", "@b"], 0.75, db_path=path)
        self.assertEqual(db.count_suppliers(db_path=path), 2)
        self.assertTrue(db.env_seed_completed(db_path=path))

        db.clear_env_seed_completed(db_path=path)
        n = db.seed_suppliers_from_env(["@a", "@b", "@c"], 0.75, db_path=path)
        db.mark_env_seed_completed(db_path=path)
        self.assertEqual(n, 3)
        self.assertEqual(db.count_suppliers(db_path=path), 3)
        self.assertIsNotNone(db.get_supplier_by_chat(username="b", db_path=path))
        self.assertIsNotNone(db.get_supplier_by_chat(username="c", db_path=path))
        self.assertTrue(db.env_seed_completed(db_path=path))

        again = db.ensure_env_seed(["@zzz"], 0.75, db_path=path)
        self.assertEqual(again["state"], "already_seeded")
        self.assertEqual(again["seeded"], 0)
        self.assertIsNone(db.get_supplier_by_chat(username="zzz", db_path=path))
        self.assertEqual(db.count_suppliers(db_path=path), 3)

    def test_validate_env_seed_empty_env_ok_with_existing_suppliers(self):
        """Empty/removed SOURCE_CHANNELS must NOT crash startup once suppliers
        exist in the DB (the classic post-bootstrap production state)."""
        path = self._fresh_db("validate_env_seed_existing.db")
        db.add_supplier("@existing", channel_id=-100600, db_path=path)
        db.mark_env_seed_completed(db_path=path)
        self.assertIsNone(db.validate_env_seed_config("", db_path=path))

    def test_validate_env_seed_empty_env_ok_with_preexisting_rows_unmarked(self):
        """Even with the marker unset, non-empty suppliers take precedence (the
        migration path) — empty .env stays fine."""
        path = self._fresh_db("validate_env_seed_migration.db")
        db.add_supplier("@keepme", channel_id=-100601, db_path=path)
        self.assertIsNone(db.validate_env_seed_config("", db_path=path))

    def test_validate_env_seed_empty_env_ok_when_all_deleted_after_seed(self):
        """Marker set + everything deliberately deleted = intentional empty state;
        must not crash."""
        path = self._fresh_db("validate_env_seed_deleted.db")
        db.mark_env_seed_completed(db_path=path)
        self.assertIsNone(db.validate_env_seed_config("", db_path=path))

    def test_validate_env_seed_empty_env_and_empty_db_returns_notice(self):
        """Empty .env + empty DB is a VALID fresh-install state: the bot must start
        with 0 suppliers and a warning, never crash. Assert the notice is returned
        and the seed/bootstrap path completes safely (0 seeded, marker set)."""
        path = self._fresh_db("validate_env_seed_fresh.db")
        notice = db.validate_env_seed_config("", db_path=path)
        self.assertIsNotNone(notice)
        self.assertIn("0 monitored sources", notice)
        self.assertIn("Sources menu", notice)
        result = db.ensure_env_seed([], 0.75, db_path=path)
        self.assertEqual(result["state"], "seeded")
        self.assertEqual(result["seeded"], 0)
        self.assertTrue(db.env_seed_completed(db_path=path))
        self.assertEqual(db.count_suppliers(db_path=path), 0)

    def test_zero_suppliers_resolved_alert_text(self):
        """The startup watchdog alert only fires when something IS configured but
        nothing resolved (0 configured suppliers is a calm, valid state)."""
        import main as main_mod

        self.assertIsNone(main_mod.zero_resolved_suppliers_alert_text(0, 0))
        self.assertIsNone(main_mod.zero_resolved_suppliers_alert_text(0, 5))
        self.assertIsNone(main_mod.zero_resolved_suppliers_alert_text(5, 5))
        self.assertIsNone(main_mod.zero_resolved_suppliers_alert_text(5, 3))
        text = main_mod.zero_resolved_suppliers_alert_text(5, 0)
        self.assertIsNotNone(text)
        self.assertIn("0 of 5", text)
        self.assertIn("MONITORING NOTHING", text)

    def test_warn_if_zero_suppliers_resolved_dms_admin(self):
        """Active suppliers but zero resolved -> log error AND admin DM."""
        import asyncio
        import main as main_mod

        class _FakeBot:
            def __init__(self):
                self.sent = []
                self.admin = 5883701139

            async def send_message(self, to, text):
                self.sent.append((to, text))
                return None

        bot = _FakeBot()
        sent_text = asyncio.run(
            main_mod._warn_if_zero_suppliers_resolved(bot, active_total=4, resolved_ok=0)
        )
        self.assertIsNotNone(sent_text)
        self.assertIn("0 of 4", sent_text)
        self.assertEqual([t for _, t in bot.sent if "MONITORING NOTHING" in t], [sent_text])
        self.assertEqual(len(bot.sent), 1)

        quiet_bot = _FakeBot()
        self.assertIsNone(asyncio.run(
            main_mod._warn_if_zero_suppliers_resolved(quiet_bot, active_total=4, resolved_ok=4)
        ))
        self.assertEqual(quiet_bot.sent, [])

    def test_listing_is_editable_helper(self):
        """The shared editable-status predicate gates edit/approve/reject flows."""
        import admin_bot

        self.assertTrue(admin_bot.listing_is_editable("pending_approval"))
        self.assertTrue(admin_bot.listing_is_editable("pending_review"))
        self.assertFalse(admin_bot.listing_is_editable("approved"))
        self.assertFalse(admin_bot.listing_is_editable("published"))
        self.assertFalse(admin_bot.listing_is_editable("rejected"))
        self.assertFalse(admin_bot.listing_is_editable("failed"))
        self.assertFalse(admin_bot.listing_is_editable(""))

    def test_edit_prompt_seeds_current_content(self):
        """The ✏️ Edit prompt is seeded with the current body as a blockquote
        (copy-tweak-resend) instead of a blank slate."""
        import admin_bot

        listing = {
            "id": 42,
            "clean_text": "KYC CURVE PAY\nANY EU",
            "our_price": 12.5,
        }
        prompt = admin_bot._edit_prompt(listing, None)
        self.assertIn("Current content", prompt)
        self.assertIn("> KYC CURVE PAY", prompt)
        self.assertIn("> ANY EU", prompt)
        self.assertIn("`$12.5`", prompt)
        self.assertIn("send back the FULL body", prompt)

    def test_edit_prompt_prior_draft_wins_and_no_price_note(self):
        """Re-entering Edit keeps the last draft as context, not the DB text."""
        import admin_bot

        listing = {
            "id": 7,
            "clean_text": "OLD LINE FROM AI",
            "our_price": None,
        }
        prompt = admin_bot._edit_prompt(listing, "MY EDITED LINE\nCHANGED")
        self.assertIn("> MY EDITED LINE", prompt)
        self.assertIn("> CHANGED", prompt)
        self.assertNotIn("OLD LINE FROM AI", prompt)
        self.assertIn("not set yet", prompt)

    def test_edit_prompt_falls_back_to_raw_text_and_empty(self):
        import admin_bot

        listing = {"id": 3, "clean_text": "", "raw_text": "RAW FALLBACK BODY"}
        prompt = admin_bot._edit_prompt(listing, None)
        self.assertIn("> RAW FALLBACK BODY", prompt)

        prompt2 = admin_bot._edit_prompt({"id": 4, "clean_text": "", "raw_text": "  "}, None)
        self.assertIn("no content yet", prompt2)
        self.assertNotIn("> ", prompt2)

    def test_normalize_channel_id_marks_bare_keeps_marked(self):
        """Every channel_id in the DB must be the marked form (-100... prefix)
        that matches what Telethon reports as event.chat_id."""
        TELEGRAM_CHANNEL_MARK = 1000000000000
        bare = 4331866910
        marked = -(TELEGRAM_CHANNEL_MARK + bare)
        self.assertEqual(marked, -1004331866910)
        self.assertEqual(db.normalize_channel_id(bare), marked)
        self.assertEqual(db.normalize_channel_id(marked), marked)
        self.assertEqual(db.normalize_channel_id("4331866910"), marked)
        self.assertEqual(db.normalize_channel_id("-1004331866910"), marked)
        self.assertIsNone(db.normalize_channel_id(None))
        self.assertIsNone(db.normalize_channel_id("not-a-number"))
        self.assertEqual(db.normalize_channel_id(-1223456789), -1223456789)

    def test_db_supplier_bare_id_and_marked_id_merge_no_duplicate(self):
        """Adding the same channel by bare id (from entity resolution) and then
        by marked id (numeric admin input) must NOT create duplicate rows."""
        path = self._fresh_db("bare_marked_dupe_test.db")
        TELEGRAM_CHANNEL_MARK = 1000000000000
        bare = 4331866910
        marked = -(TELEGRAM_CHANNEL_MARK + bare)
        add_chan = "@src_chan"
        id1 = db.add_supplier(add_chan, channel_id=bare, db_path=path)
        id2 = db.add_supplier(add_chan, channel_id=marked, db_path=path)
        self.assertEqual(id1, id2, "same channel added bare then marked must be one row")
        self.assertEqual(db.count_suppliers(db_path=path), 1)
        row = db.get_supplier_by_chat(chat_id=marked, db_path=path)
        self.assertIsNotNone(row)
        self.assertEqual(row["channel_id"], marked)
        self.assertEqual(row["channel_username"], add_chan.lstrip("@").lower())

    def test_db_normalize_supplier_channel_ids_migration(self):
        """Migration fixes bare (positive) channel_ids in place and merges into
        already-correct rows without duplicating."""
        path = self._fresh_db("normalize_migration_test.db")
        TELEGRAM_CHANNEL_MARK = 1000000000000
        bare_cha = 4331866910
        bare_chb = 4331866911
        marked_cha = -(TELEGRAM_CHANNEL_MARK + bare_cha)
        marked_chb = -(TELEGRAM_CHANNEL_MARK + bare_chb)
        # Create a legacy row with bare id via direct insert (simulates old build)
        db.add_supplier("ok_already_marked", channel_id=marked_chb, db_path=path)
        with db.db_session(path) as conn:
            conn.execute(
                "INSERT INTO suppliers (channel_username, channel_id, active, markup_multiplier, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("old_bare_chan", bare_cha, 1, 0.75, "2025-01-01T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO suppliers (channel_username, channel_id, active, markup_multiplier, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("dup_via_bare", bare_chb, 1, 0.75, "2025-01-01T00:00:00+00:00"),
            )
        self.assertEqual(db.count_suppliers(db_path=path), 3)
        report = db.normalize_supplier_channel_ids(db_path=path)
        self.assertGreaterEqual(report["scanned"], 3)
        self.assertEqual(report["normalized"], 1)
        self.assertEqual(report["merged"], 1)
        self.assertEqual(db.count_suppliers(db_path=path), 2, "dup_via_bare merged into ok_already_marked")
        row_cha = db.get_supplier_by_chat(chat_id=marked_cha, db_path=path)
        self.assertIsNotNone(row_cha)
        self.assertEqual(row_cha["channel_id"], marked_cha)
        row_chb = db.get_supplier_by_chat(chat_id=marked_chb, db_path=path)
        self.assertIsNotNone(row_chb)
        self.assertEqual(row_chb["channel_id"], marked_chb)

    def test_resolve_supplier_entities_backfills_marked_channel_id(self):
        """resolve_supplier_entities must store the marked -100... form, NOT
        the bare entity.id — matching what incoming events will report."""
        import asyncio
        import main as main_mod

        path = self._fresh_db("resolve_marked_test.db")
        TELEGRAM_CHANNEL_MARK = 1000000000000
        bare = 4331866910
        marked = -(TELEGRAM_CHANNEL_MARK + bare)

        db.add_supplier("@privchan", channel_id=None, db_path=path)

        class _FakeEntity:
            def __init__(self):
                self.id = bare
                self.username = "bbbbb"
                self.title = "b channel"

        class _FakeClient:
            def __init__(self, entity):
                self._entity = entity
                self.calls = []
            async def get_entity(self, ref):
                self.calls.append(ref)
                return self._entity

        fake_client = _FakeClient(_FakeEntity())
        old_default = db.DEFAULT_DB_PATH
        db.DEFAULT_DB_PATH = path
        try:
            result = asyncio.run(main_mod.resolve_supplier_entities(fake_client))
            self.assertGreaterEqual(len(result), 1)
            row = db.get_supplier_by_chat(chat_id=marked, db_path=path)
            self.assertIsNotNone(row)
            self.assertEqual(row["channel_id"], marked, "stored id must be the marked -100... form")
        finally:
            db.DEFAULT_DB_PATH = old_default

    def test_resolve_supplier_for_event_matches_entity_resolved_supplier(self):
        """An incoming event.chat_id (marked form) must match a supplier that was
        added via get_entity resolution, not just via directly-typed numeric ID."""
        import asyncio
        import main as main_mod

        path = self._fresh_db("event_match_test.db")
        TELEGRAM_CHANNEL_MARK = 1000000000000
        bare = 4331866910
        marked = -(TELEGRAM_CHANNEL_MARK + bare)

        db.add_supplier("@privchan", channel_id=bare, db_path=path)
        # Fix up the bare id that add_supplier auto-normalized if any, to be sure
        with db.db_session(path) as conn:
            conn.execute("UPDATE suppliers SET channel_id = ? WHERE channel_username = ?", (marked, "privchan"))

        class _FakeChat:
            username = "bbbbb"
        class _FakeEvent:
            chat_id = marked
            chat = _FakeChat()

        old_default = db.DEFAULT_DB_PATH
        db.DEFAULT_DB_PATH = path
        try:
            supplier = main_mod.resolve_supplier_for_event(_FakeEvent())
            self.assertIsNotNone(supplier, "resolve_supplier_for_event must match on marked chat_id")
            self.assertEqual(supplier["channel_id"], marked)
            self.assertTrue(supplier.get("active"))
        finally:
            db.DEFAULT_DB_PATH = old_default

    def test_db_display_name_roundtrip(self):
        """set_supplier_display_name stores a friendly label keyed by username OR id."""
        db_path = TEST_DB
        db.add_supplier("-100789", channel_id=-100789, markup_multiplier=0.75, db_path=db_path)
        ok = db.set_supplier_display_name("-100789", "KYC Group UK", db_path=db_path)
        self.assertTrue(ok)
        row = db.get_supplier_by_chat(chat_id=-100789, db_path=db_path)
        self.assertEqual(row["display_name"], "KYC Group UK")
        # Username-based suppliers work too.
        db.add_supplier("@disp_src", channel_id=-100790, db_path=db_path)
        db.set_supplier_display_name("@disp_src", "disp_handle", db_path=db_path)
        row2 = db.get_supplier_by_chat(username="disp_src", db_path=db_path)
        self.assertEqual(row2["display_name"], "disp_handle")

    def test_db_listings_and_dedup(self):
        db.add_supplier("@dedup_supplier", channel_id=-100333, markup_multiplier=0.75, db_path=TEST_DB)
        sup = db.get_supplier_by_chat(username="dedup_supplier", db_path=TEST_DB)
        self.assertIsNotNone(sup)
        sup_id = sup["id"]

        listing_id = db.insert_listing(
            supplier_id=sup_id,
            source_message_id=9991,
            game_name="bybit",
            rank_tier=None,
            original_price=100.0,
            our_price=75.0,
            status="published",
            raw_text="WTS Bybit verified account $100",
            clean_text="WTS Bybit verified account $100",
            published_message_id=5001,
            db_path=TEST_DB,
        )
        self.assertGreater(listing_id, 0)

        # Idempotent insert: same (supplier, source_message) must not create a row
        second_id = db.insert_listing(
            supplier_id=sup_id,
            source_message_id=9991,
            game_name="bybit",
            rank_tier=None,
            original_price=100.0,
            our_price=75.0,
            status="published",
            raw_text="WTS Bybit verified account $100",
            clean_text="WTS Bybit verified account $100",
            published_message_id=5001,
            db_path=TEST_DB,
        )
        self.assertEqual(second_id, listing_id)

        # Duplicate detection is content-based: fingerprint of (text + price),
        # so a re-post with a NEW message id is still caught...
        db.set_listing_fingerprint(
            listing_id, clean_text="WTS Bybit verified account $100",
            price=100.0, db_path=TEST_DB,
        )
        is_dup = filters.is_duplicate_listing(
            clean_text="WTS Bybit verified account $100",
            hours=48, price=100.0, db_path=TEST_DB,
        )
        self.assertTrue(is_dup)

        # ...while the same product at a DIFFERENT price is NOT a duplicate.
        is_not_dup_price = filters.is_duplicate_listing(
            clean_text="WTS Bybit verified account $100",
            hours=48, price=150.0, db_path=TEST_DB,
        )
        self.assertFalse(is_not_dup_price)

        # And a different product entirely is not a duplicate.
        is_not_dup = filters.is_duplicate_listing(
            clean_text="WTS Crypto.com account $150",
            hours=48, price=150.0, db_path=TEST_DB,
        )
        self.assertFalse(is_not_dup)

        # Named-rule pipeline: duplicate fires with its reason, clean passes.
        self.assertEqual(
            filters.check_filters(
                "WTS Bybit verified account $100", hours=48, price=100.0, db_path=TEST_DB,
            ),
            filters.REASON_DUPLICATE,
        )
        self.assertIsNone(
            filters.check_filters(
                "WTS Crypto.com account $150", hours=48, price=150.0, db_path=TEST_DB,
            )
        )
        self.assertEqual(
            filters.check_filters("   ", hours=48, db_path=TEST_DB),
            filters.REASON_NO_CONTENT,
        )

    def test_db_skips_log_and_breakdown(self):
        """log_skip feeds the per-reason and per-supplier daily breakdowns."""
        sid = db.add_supplier("@skip_src", channel_id=-100888, db_path=TEST_DB)
        db.log_skip(sid, 7001, "duplicate", "WTS Bybit $100", db_path=TEST_DB)
        db.log_skip(sid, 7002, "not_a_listing", "some chatter", db_path=TEST_DB)
        db.log_skip(sid, 7003, "self_echo", "our own output", db_path=TEST_DB)

        reasons = db.get_skip_reasons_today(db_path=TEST_DB)
        self.assertEqual(reasons.get("duplicate"), 1)
        self.assertEqual(reasons.get("not_a_listing"), 1)
        self.assertEqual(reasons.get("self_echo"), 1)

        # Per-supplier breakdown counts listings processed today + skips today.
        stats = db.get_today_stats(db_path=TEST_DB)
        self.assertEqual(stats["total_skipped"], 3)
        self.assertEqual(stats["skip_reasons"].get("duplicate"), 1)

        row = next(
            r for r in stats["supplier_breakdown"] if r["id"] == sid
        )
        self.assertEqual(row["channel_username"], "skip_src")
        self.assertEqual(row["skipped"], 3)

    def test_db_unpublished_listings(self):
        """get_unpublished_listings returns only non-published statuses."""
        db.add_supplier("@unpub_supplier", channel_id=-100557, markup_multiplier=0.75, db_path=TEST_DB)
        sup = db.get_supplier_by_chat(username="unpub_supplier", db_path=TEST_DB)
        pend_id = db.insert_listing(
            supplier_id=sup["id"], source_message_id=9951, game_name=None, rank_tier=None,
            original_price=100.0, our_price=75.0,
            status="pending_review", raw_text="WTS Revolut account $100",
            clean_text="WTS Revolut account $100", db_path=TEST_DB,
        )
        recv_id = db.insert_listing(
            supplier_id=sup["id"], source_message_id=9952, game_name=None, rank_tier=None,
            original_price=100.0, our_price=75.0,
            status="received", raw_text="WTS Revolut account $100",
            clean_text="WTS Revolut account $100", db_path=TEST_DB,
        )
        db.insert_listing(
            supplier_id=sup["id"], source_message_id=9953, game_name=None, rank_tier=None,
            original_price=100.0, our_price=75.0,
            status="published", raw_text="WTS Revolut account $100",
            clean_text="WTS Revolut account $100", published_message_id=5002, db_path=TEST_DB,
        )
        db.insert_listing(
            supplier_id=sup["id"], source_message_id=9954, game_name=None, rank_tier=None,
            original_price=100.0, our_price=75.0,
            status="failed", raw_text="WTS Revolut account $100",
            clean_text="WTS Revolut account $100", db_path=TEST_DB,
        )
        unpub = db.get_unpublished_listings(db_path=TEST_DB)
        unpub_ids = {l["id"] for l in unpub}
        self.assertIn(pend_id, unpub_ids)
        self.assertIn(recv_id, unpub_ids)
        self.assertTrue(all(l["status"] in ("received", "pending_approval", "pending_review") for l in unpub))

    def test_db_failed_queue_and_requeue(self):
        db.add_supplier("@fail_supplier", channel_id=-100444, markup_multiplier=0.75, db_path=TEST_DB)
        sup = db.get_supplier_by_chat(username="fail_supplier", db_path=TEST_DB)
        listing_id = db.insert_listing(
            supplier_id=sup["id"],
            source_message_id=9940,
            game_name=None,
            rank_tier=None,
            original_price=100.0,
            our_price=75.0,
            status="approved",
            raw_text="WTS Revolut account $100",
            clean_text="WTS Revolut account $100",
            db_path=TEST_DB,
        )

        # Approved listing with no published_message_id should be returned for publishing
        approved = db.get_approved_listings_to_publish(limit=10, db_path=TEST_DB)
        self.assertTrue(any(l["id"] == listing_id for l in approved))

        # Mark failed -> shows up in DLQ
        db.mark_listing_failed(listing_id, "FloodWaitError(30)", db_path=TEST_DB)
        failed = db.get_failed_listings(limit=10, db_path=TEST_DB)
        self.assertTrue(any(l["id"] == listing_id for l in failed))
        self.assertGreaterEqual(
            [l for l in failed if l["id"] == listing_id][0]["retry_count"], 1
        )

        # Requeue → back to approved, no longer in DLQ
        ok = db.requeue_listing(listing_id, db_path=TEST_DB)
        self.assertTrue(ok)
        failed_after = db.get_failed_listings(limit=10, db_path=TEST_DB)
        self.assertFalse(any(l["id"] == listing_id for l in failed_after))

    def test_db_audit_log(self):
        db.record_audit("published_auto", 42, detail="msg 5001", db_path=TEST_DB)
        with db.db_session(TEST_DB) as conn:
            entries = [dict(r) for r in conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT 10"
            ).fetchall()]
        self.assertTrue(any(e["action"] == "published_auto" for e in entries))

    def test_db_platform_fields_persisted(self):
        db.add_supplier("@fields_supplier", channel_id=-100555, markup_multiplier=0.75, db_path=TEST_DB)
        sup = db.get_supplier_by_chat(username="fields_supplier", db_path=TEST_DB)
        listing_id = db.insert_listing(
            supplier_id=sup["id"],
            source_message_id=9950,
            game_name=None,
            rank_tier=None,
            original_price=None,
            our_price=None,
            status="received",
            raw_text="WTS Wise personal $200",
            clean_text="WTS Wise personal $200",
            db_path=TEST_DB,
        )
        listing = db.get_listing_by_id(listing_id, db_path=TEST_DB)
        self.assertIsNone(listing["platform_name"])
        self.assertIsNone(listing["original_price"])

        db.update_listing_fields(
            listing_id, platform_name="wise", original_price=200.0, our_price=150.0,
            db_path=TEST_DB,
        )
        updated = db.get_listing_by_id(listing_id, db_path=TEST_DB)
        self.assertEqual(updated["platform_name"], "wise")
        self.assertEqual(updated["original_price"], 200.0)
        self.assertEqual(updated["our_price"], 150.0)

    def test_db_stats_and_pending(self):
        # A dedicated supplier so the FK insert is always valid (PRAGMA
        # foreign_keys is now ON).
        sid = db.add_supplier("@stats_src", channel_id=-100666, markup_multiplier=0.75, db_path=TEST_DB)
        # Insert a pending listing
        pending_id = db.insert_listing(
            supplier_id=sid,
            source_message_id=9992,
            game_name=None,
            rank_tier=None,
            original_price=40.0,
            our_price=30.0,
            status="pending_approval",
            raw_text="Netflix 1 Month $40",
            clean_text="Netflix 1 Month $40",
            db_path=TEST_DB,
        )

        pending_list = db.get_pending_listings(limit=10, db_path=TEST_DB)
        self.assertTrue(any(item["id"] == pending_id for item in pending_list))

        stats = db.get_today_stats(db_path=TEST_DB)
        self.assertGreaterEqual(stats["total_processed"], 1)
        self.assertGreaterEqual(stats["pending"], 1)

    # -------------------------------------------------------------
    # MIGRATION TESTS
    # -------------------------------------------------------------
    def test_migration_from_v0_schema(self):
        """A v0 database is upgraded: columns added, duplicates deduped, index created."""
        mig_db = "test_migration.db"
        if os.path.exists(mig_db):
            os.remove(mig_db)

        # Build the OLD schema exactly as it shipped pre-fix
        conn = sqlite3.connect(mig_db)
        conn.execute(
            """
            CREATE TABLE suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT UNIQUE,
                channel_id INTEGER UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                markup_multiplier REAL NOT NULL DEFAULT 0.75,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER,
                source_message_id INTEGER NOT NULL,
                game_name TEXT,
                rank_tier TEXT,
                original_price REAL,
                our_price REAL,
                status TEXT NOT NULL,
                raw_text TEXT,
                clean_text TEXT,
                published_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blocklist_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER,
                matched_keyword TEXT NOT NULL
            )
            """
        )
        # Seed a duplicate pair (exactly the live-DB bug: two rows for same source msg)
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO listings (supplier_id, source_message_id, status, raw_text, clean_text, created_at, updated_at) "
            "VALUES (1, 4, 'published', 'x', 'x', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO listings (supplier_id, source_message_id, status, raw_text, clean_text, created_at, updated_at) "
            "VALUES (1, 4, 'published', 'y', 'y', ?, ?)",
            (now, now),
        )
        conn.commit()
        conn.close()

        # Run migration
        db.init_db(mig_db)

        # New columns added
        conn = sqlite3.connect(mig_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
        for col in ("platform_name", "retry_count", "last_error", "reviewed_at",
                    "published_at", "fingerprint"):
            self.assertIn(col, cols)

        # Duplicates deduped (keep the MAX(id) row = id 2)
        rows = conn.execute(
            "SELECT id FROM listings WHERE supplier_id = 1 AND source_message_id = 4"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 2)

        # Unique index present so future duplicates are impossible
        idx = conn.execute(
            "PRAGMA index_list('listings')"
        ).fetchall()
        unique_idx = [r for r in idx if r[1] == "idx_listings_unique"]
        self.assertTrue(unique_idx, "expected unique index to exist")
        self.assertEqual(unique_idx[0][2], 1)

        # user_version migrated to latest
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertGreaterEqual(version, 7)

        # skips table added by the v6 migration; ai_cache by the v7 migration
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("skips", tables)
        self.assertIn("ai_cache", tables)
        conn.close()

        if os.path.exists(mig_db):
            os.remove(mig_db)

    def test_migration_v8_adds_display_name(self):
        """A pre-v8 DB gains suppliers.display_name; fresh DBs already have it."""
        mig_db = "test_migration_v8.db"
        if os.path.exists(mig_db):
            os.remove(mig_db)
        conn = sqlite3.connect(mig_db)
        conn.execute(
            """
            CREATE TABLE suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT UNIQUE,
                channel_id INTEGER UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                markup_multiplier REAL NOT NULL DEFAULT 0.75,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER,
                source_message_id INTEGER NOT NULL,
                game_name TEXT,
                rank_tier TEXT,
                original_price REAL,
                our_price REAL,
                status TEXT NOT NULL,
                raw_text TEXT,
                clean_text TEXT,
                published_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO listings (supplier_id, source_message_id, status, raw_text, clean_text, created_at, updated_at) "
            "VALUES (1, 1, 'published', 'x', 'x', ?, ?)",
            (now, now),
        )
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        conn.close()

        db.init_db(mig_db)
        conn = sqlite3.connect(mig_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
            self.assertIn("display_name", cols)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertGreaterEqual(version, 8)
        finally:
            conn.close()
        # display name is usable on the migrated table.
        db.add_supplier("-100001", channel_id=-100001, db_path=mig_db)
        db.set_supplier_display_name("-100001", "Migrated Channel", db_path=mig_db)
        row = db.get_supplier_by_chat(chat_id=-100001, db_path=mig_db)
        self.assertEqual(row["display_name"], "Migrated Channel")
        if os.path.exists(mig_db):
            os.remove(mig_db)
        db.set_setting("theme", "dark", db_path=TEST_DB)
        self.assertEqual(db.get_setting("theme", db_path=TEST_DB), "dark")
        db.set_setting("theme", "light", db_path=TEST_DB)
        self.assertEqual(db.get_setting("theme", db_path=TEST_DB), "light")
        self.assertEqual(db.get_setting("missing_key", "default", db_path=TEST_DB), "default")

    def test_pause_switch(self):
        db.set_paused(False, db_path=TEST_DB)
        self.assertFalse(db.is_paused(db_path=TEST_DB))
        db.set_paused(True, db_path=TEST_DB)
        self.assertTrue(db.is_paused(db_path=TEST_DB))
        db.set_paused(False, db_path=TEST_DB)
        self.assertFalse(db.is_paused(db_path=TEST_DB))

    def test_get_published_listings_joins_supplier(self):
        sid = db.add_supplier("trace_src", channel_id=-100777, db_path=TEST_DB)
        db.insert_listing(sid, 9001, None, None, 100.0, 80.0, "pending_approval",
                          "t", "t", db_path=TEST_DB)
        pid = db.insert_listing(sid, 9002, None, None, 100.0, 80.0, "published",
                                "t", "t", published_message_id=50, db_path=TEST_DB)
        pid2 = db.insert_listing(sid, 9003, None, None, 100.0, 80.0, "published",
                                 "t", "t", published_message_id=51, db_path=TEST_DB)
        rows = db.get_published_listings(db_path=TEST_DB)
        published_ids = {r["id"] for r in rows}
        self.assertIn(pid, published_ids)
        self.assertIn(pid2, published_ids)
        row = next(r for r in rows if r["id"] == pid)
        self.assertEqual(row["supplier_username"], "trace_src")
        self.assertEqual(row["supplier_channel_id"], -100777)
        self.assertEqual(row["source_message_id"], 9002)
        self.assertEqual(row["published_message_id"], 50)

    def test_post_number_lifecycle(self):
        n1 = db.next_post_number(db_path=TEST_DB)
        n2 = db.next_post_number(db_path=TEST_DB)
        self.assertEqual(n2, n1 + 1)

        sid = db.add_supplier("@pn_src", channel_id=-100889,
                              markup_multiplier=0.75, db_path=TEST_DB)
        listing_id = db.insert_listing(sid, 9100, None, None, 100.0, 80.0, "approved",
                                       "t", "t", db_path=TEST_DB)
        self.assertIsNone(db.get_listing_by_id(listing_id, db_path=TEST_DB)["post_number"])

        reserved = db.next_post_number(db_path=TEST_DB)
        db.update_listing_status(
            listing_id=listing_id, status="published",
            published_message_id=61, post_number=reserved, db_path=TEST_DB,
        )
        listing = db.get_listing_by_id(listing_id, db_path=TEST_DB)
        self.assertEqual(listing["status"], "published")
        self.assertEqual(listing["post_number"], reserved)

        fetched = db.get_post_by_number(reserved, db_path=TEST_DB)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["id"], listing_id)
        self.assertEqual(fetched["supplier_username"], "pn_src")
        self.assertEqual(fetched["post_number"], reserved)

        # Unallocated / unknown post numbers return None.
        self.assertIsNone(db.get_post_by_number(reserved + 999, db_path=TEST_DB))

    def test_migration_backfills_post_numbers(self):
        path = "test_backfill.db"
        for suffix in ("", "-wal", "-shm"):
            fp = path + suffix
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        try:
            db.init_db(path)
            sid = db.add_supplier("@bf_src", channel_id=-100900, db_path=path)
            db.insert_listing(sid, 1, None, None, None, None, "published",
                              "a", "a", published_message_id=10, db_path=path)
            db.insert_listing(sid, 2, None, None, None, None, "published",
                              "b", "b", published_message_id=11, db_path=path)
            # Simulate a pre-v4 database: drop the counter and downgrade.
            with db.db_session(path) as conn:
                conn.execute("DELETE FROM app_settings WHERE key = 'post_seq'")
                conn.execute("PRAGMA user_version = 3")
            with db.db_session(path) as conn:
                db._migrate(conn)
            with db.db_session(path) as conn:
                rows = conn.execute(
                    "SELECT post_number FROM listings ORDER BY id"
                ).fetchall()
                seq = conn.execute(
                    "SELECT value FROM app_settings WHERE key = 'post_seq'"
                ).fetchone()["value"]
            self.assertEqual([r["post_number"] for r in rows], [1, 2])
            self.assertEqual(seq, "2")
            # New post continues from where the backfill stopped.
            self.assertEqual(db.next_post_number(db_path=path), 3)
        finally:
            for suffix in ("", "-wal", "-shm"):
                fp = path + suffix
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass

    def test_build_ai_message_post_number_banner(self):
        out, entities = parser.build_ai_message(
            content_lines=["Netflix 1 month"],
            our_price=38.0,
            platform="netflix",
            post_number=42,
        )
        self.assertIn("Post  #42", out)
        msg2, _ = parser.build_ai_message(
            content_lines=["Netflix 1 month"],
            our_price=38.0,
            platform="netflix",
        )
        self.assertNotIn("Post  #", msg2)

    def test_tgram_chat_link_helper(self):
        import admin_bot
        self.assertEqual(
            admin_bot._tgram_chat_link("@aidikyc", 29),
            "https://t.me/aidikyc/29",
        )
        self.assertEqual(
            admin_bot._tgram_chat_link("-1004353573428", 7),
            "https://t.me/c/4353573428/7",
        )
        self.assertIsNone(admin_bot._tgram_chat_link("", 3))
        self.assertIsNone(admin_bot._tgram_chat_link("@x", None))
        # Source links: by username or supergroup id, using the source message id
        self.assertEqual(
            admin_bot._source_url({"supplier_username": "aro_onn", "source_message_id": 99}),
            "https://t.me/aro_onn/99",
        )
        self.assertEqual(
            admin_bot._source_url({"supplier_username": None,
                                   "supplier_channel_id": -1004353573428,
                                   "source_message_id": 77}),
            "https://t.me/c/4353573428/77",
        )
        self.assertIsNone(admin_bot._source_url({"supplier_username": "x", "source_message_id": None}))

    def test_pretty_source_helper(self):
        """Numeric-id suppliers never render as '@-100...'; display_name wins."""
        import admin_bot
        # No display name: numeric id shown bare, real usernames get '@'.
        self.assertEqual(admin_bot._pretty_source("-1003824132899"), "-1003824132899")
        self.assertEqual(admin_bot._pretty_source("kycgroupke"), "@kycgroupke")
        self.assertEqual(admin_bot._pretty_source("@kycgroupke"), "@kycgroupke")
        # display_name: username -> '@name', title -> as-is, empty -> fallback.
        self.assertEqual(
            admin_bot._pretty_source("-1003824132899", "KYC Group UK"), "KYC Group UK"
        )
        self.assertEqual(
            admin_bot._pretty_source("-1003824132899", "kycgroupke"), "@kycgroupke"
        )
        self.assertEqual(admin_bot._pretty_source(None), "?")

    def test_record_audit_accepts_int_detail(self):
        db.record_audit("published_auto", 42, detail=5001, db_path=TEST_DB)
        db.record_audit("published_auto", 43, detail=None, db_path=TEST_DB)
        with db.db_session(TEST_DB) as conn:
            entries = [dict(r) for r in conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT 10"
            ).fetchall()]
        newest42 = next(e for e in entries if e["listing_id"] == 42)
        newest43 = next(e for e in entries if e["listing_id"] == 43)
        self.assertEqual(newest42["detail"], "5001")
        self.assertEqual(newest43["detail"], "")

    def test_emoji_config_roundtrip(self):
        db.set_emoji_config("fire", "🔥", 1234567890, source="TestPack", db_path=TEST_DB)
        cfg = db.get_emoji_configs(db_path=TEST_DB)
        self.assertIn("fire", cfg)
        self.assertEqual(cfg["fire"]["document_id"], 1234567890)
        self.assertEqual(cfg["fire"]["source"], "TestPack")
        db.set_emoji_config("fire", "🔥", 987654321, source="TestPack2", db_path=TEST_DB)
        cfg = db.get_emoji_configs(db_path=TEST_DB)
        self.assertEqual(cfg["fire"]["document_id"], 987654321)

    def test_emoji_override_used_in_header(self):
        original_path = parser.EMOJI_DB_PATH
        parser.EMOJI_DB_PATH = TEST_DB
        try:
            db.set_emoji_config("fire", "🔥", 424242, source="PackX", db_path=TEST_DB)
            parser.reload_emoji_config()
            msg, entities = parser.build_ai_message(
                content_lines=["Netflix 1 month"],
                our_price=30,
                platform="netflix",
                contact_username="@buy",
                intent="sell",
            )
            self.assertEqual(entities[0].document_id, 424242,
                             "header emoji should use the DB override")
            self.assertIn(parser.PH_FIRE, msg)  # placeholder text unchanged
            # Falling back: remove the override -> default document id is used
            conn = sqlite3.connect(TEST_DB)
            conn.execute("DELETE FROM emoji_config WHERE role = 'fire'")
            conn.commit()
            conn.close()
            parser.reload_emoji_config()
            msg2, entities2 = parser.build_ai_message(
                content_lines=["Netflix 1 month"],
                our_price=30,
                platform="netflix",
                contact_username="@buy",
                intent="sell",
            )
            self.assertEqual(entities2[0].document_id, parser.CE_FIRE,
                             "default id should be used when no override exists")
        finally:
            parser.EMOJI_DB_PATH = original_path
            parser.reload_emoji_config()

    # -------------------------------------------------------------
    # AI REPHRASER TESTS
    # -------------------------------------------------------------
    def test_ai_rephraser_init_no_key_returns_false(self):
        """init_groq should fail gracefully without a key."""
        result = ai_rephraser.init_groq("")
        self.assertFalse(result)
        self.assertFalse(ai_rephraser.is_available())

    def test_ai_analyze_none_when_unavailable(self):
        """analyze_message should return None (triggering fallback) without a client."""
        ai_rephraser.init_groq("")
        import asyncio
        result = asyncio.run(ai_rephraser.analyze_message(
            "KYC Ikualo + Tuyo ID Card + Proof Address Serious Seller! Spain PRICE 50$"
        ))
        self.assertIsNone(result)

    def test_ai_analyze_none_on_empty(self):
        """analyze_message should return None for empty text."""
        ai_rephraser.init_groq("")
        import asyncio
        result = asyncio.run(ai_rephraser.analyze_message("   "))
        self.assertIsNone(result)

    def test_rephrase_unpublished_skips_when_ai_unavailable(self):
        """The stale-body rephrase sweep is a safe no-op without a Groq client."""
        import asyncio
        import main as main_mod
        # init_groq("") falls back to the .env key once main.py has loaded it,
        # so force the client off explicitly to keep this test hermetic.
        ai_rephraser._client = None
        ai_rephraser._api_key = None
        result = asyncio.run(main_mod.rephrase_unpublished())
        self.assertIsNone(result)

    def test_ai_analyze_prompt_is_strict_json_schema(self):
        """ANALYZE_PROMPT must demand the full JSON schema fields."""
        self.assertIn("is_listing", ai_rephraser.ANALYZE_PROMPT)
        self.assertIn("blocked", ai_rephraser.ANALYZE_PROMPT)
        self.assertIn("platform", ai_rephraser.ANALYZE_PROMPT)
        self.assertIn("price", ai_rephraser.ANALYZE_PROMPT)
        self.assertIn("intent", ai_rephraser.ANALYZE_PROMPT)
        self.assertIn("dm_request", ai_rephraser.ANALYZE_PROMPT)
        self.assertIn("content", ai_rephraser.ANALYZE_PROMPT)
        self.assertIn("JSON:", ai_rephraser.ANALYZE_PROMPT)

    def test_ai_parse_analysis_json(self):
        """Tolerant JSON parsing of analyzer output (fenced + trailing comma)."""
        data = ai_rephraser._parse_analysis_json(
            '```json\n'
            '{"is_listing": true, "blocked": false, "block_reason": "", '
            '"platform": "curve", "price": 50, "intent": "buy", '
            '"dm_request": true, "content": ["Line one", "Line two",],}\n'
            '```'
        )
        self.assertIsNotNone(data)
        self.assertTrue(data["is_listing"])
        self.assertFalse(data["blocked"])
        self.assertEqual(data["platform"], "curve")
        self.assertEqual(data["price"], 50.0)
        self.assertEqual(data["intent"], "buy")
        self.assertTrue(data["dm_request"])
        self.assertEqual(data["content"], ["Line one", "Line two"])

    def test_ai_parse_analysis_json_filters_junk(self):
        """Empty content, garbage platform/price must be normalized away."""
        data = ai_rephraser._parse_analysis_json(
            '{"is_listing": true, "platform": "  ", "price": "", '
            '"intent": "maybe", "content": ["", "  ", 123, null]}'
        )
        self.assertIsNotNone(data)
        self.assertIsNone(data["platform"])
        self.assertIsNone(data["price"])
        self.assertEqual(data["intent"], "neutral")
        self.assertEqual(data["content"], [])

    def test_ai_parse_analysis_json_rejects_invalid(self):
        self.assertIsNone(ai_rephraser._parse_analysis_json(""))
        self.assertIsNone(ai_rephraser._parse_analysis_json("no json here"))
        self.assertIsNone(ai_rephraser._parse_analysis_json("{not valid"))

    def test_build_ai_message_uses_clean_lines_directly(self):
        """build_ai_message wraps AI lines as-is (no regex cleanup, no body emoji)."""
        out, entities = parser.build_ai_message(
            content_lines=["Spain region", "Includes Tuyo account", "ID card + proof of address"],
            our_price=38,
            platform="ikualo",
            contact_username="@buyer",
            intent="sell",
        )
        self.assertIn("IKUALO WTB ✦ DM FAST", out)
        self.assertIn("\nSpain region\n", out)
        self.assertIn("\nIncludes Tuyo account\n", out)
        self.assertIn("\nID card + proof of address\n", out)
        self.assertIn("🤑 Price  : $38", out)
        self.assertIn("📞 Order  : @buyer", out)
        # Body must be emoji-free: no bullets, no fire/lightning/star inside.
        self.assertNotIn("⭐", out)

    def test_build_ai_message_header_rotates_by_seed(self):
        """Header emoji alternates fire/lightning deterministically per listing seed."""
        _, e0 = parser.build_ai_message(
            content_lines=["line"], our_price=30, platform="x", contact_username="@b", intent="sell",
            listing_seed=0,
        )
        _, e1 = parser.build_ai_message(
            content_lines=["line"], our_price=30, platform="x", contact_username="@b", intent="sell",
            listing_seed=1,
        )
        _, e0b = parser.build_ai_message(
            content_lines=["line"], our_price=30, platform="x", contact_username="@b", intent="sell",
            listing_seed=0,
        )
        self.assertEqual(e0[0].document_id, parser._ce("fire"))
        self.assertEqual(e1[0].document_id, parser._ce("lightning"))
        self.assertEqual(e0b[0].document_id, e0[0].document_id,
                         "same seed must pick the same emoji")

    def test_build_ai_message_body_is_emoji_free(self):
        """Emoji appear ONLY in the header/footer lines, never in the body."""
        out, _ = parser.build_ai_message(
            content_lines=["Plain body line one", "Plain body line two"],
            our_price=50,
            platform="revolut",
            contact_username="@b",
            intent="sell",
        )
        lines = out.split("\n")
        body_lines = [ln for ln in lines if ln.startswith("Plain body")]
        self.assertEqual(len(body_lines), 2)
        for ln in body_lines:
            self.assertEqual(
                parser.strip_all_emoji(ln), ln,
                "body line must not contain any emoji",
            )
        # The only emoji characters in the whole post are header + footer placeholders.
        allowed = {parser.PH_FIRE, parser.PH_LIGHT, parser.PH_PRICE, parser.PH_PHONE}
        present = {ch for ch in out if emoji.is_emoji(ch)}
        self.assertTrue(present.issubset(allowed), f"unexpected emoji in post: {present}")

    def test_build_ai_message_preserves_full_lists(self):
        """A long country/requirements list must pass through unchanged (no 5-line cap)."""
        countries = [f"Country {i}" for i in range(9)]
        out, _ = parser.build_ai_message(
            content_lines=countries,
            our_price=50,
            platform="kyc",
            contact_username="@b",
            intent="sell",
        )
        body_lines = [ln.strip() for ln in out.split("\n") if ln.strip().startswith("Country ")]
        self.assertEqual(len(body_lines), 9)

        # Tolerant JSON parsing must also keep > 5 lines.
        json_in = '{"is_listing": true, "blocked": false, "content": [' + \
            ", ".join(f'"{c}"' for c in countries) + "]}"
        parsed = ai_rephraser._parse_analysis_json(json_in)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed.get("content", [])), 9)

    def test_build_ai_message_defaults_to_buyer_header(self):
        """Every post carries a buyer-framed header, whatever the source intent."""
        out, _ = parser.build_ai_message(
            content_lines=["Need curve pay"], our_price=38,
            platform="curve", contact_username="@buyer", intent="buy",
        )
        self.assertIn("CURVE WTB ✦ DM FAST", out)
        self.assertNotIn("FOR SALE", out)

    def test_build_ai_message_uses_validated_ai_header(self):
        """A buyer-framed AI tagline is used; seller wording falls back to default."""
        out, _ = parser.build_ai_message(
            content_lines=["Netflix"], our_price=38,
            platform="netflix", contact_username="@b",
            header_word="WANTED ✦ DM FAST",
        )
        self.assertIn("NETFLIX WANTED ✦ DM FAST", out)
        out, _ = parser.build_ai_message(
            content_lines=["Netflix"], our_price=38,
            platform="netflix", contact_username="@b",
            header_word="FOR SALE",
        )
        self.assertIn("NETFLIX WTB ✦ DM FAST", out)

    def test_sanitize_buyer_header(self):
        """Buyer taglines pass; seller wording, emoji, and empty input are rejected."""
        self.assertEqual(parser.sanitize_buyer_header("WTB ✦ DM FAST"), "WTB ✦ DM FAST")
        self.assertEqual(parser.sanitize_buyer_header("  wanted  "), "wanted")
        self.assertEqual(parser.sanitize_buyer_header("🔥 DM FAST 🔥"), "DM FAST")
        self.assertIsNone(parser.sanitize_buyer_header("FOR SALE"))
        self.assertIsNone(parser.sanitize_buyer_header("SELLING FAST"))
        self.assertIsNone(parser.sanitize_buyer_header("AVAILABLE"))
        self.assertIsNone(parser.sanitize_buyer_header("hello world"))
        self.assertIsNone(parser.sanitize_buyer_header("   "))
        self.assertIsNone(parser.sanitize_buyer_header(None))

    def test_ai_parse_analysis_json_extracts_header(self):
        parsed = ai_rephraser._parse_analysis_json(
            '{"is_listing": true, "blocked": false, "header": "WANTED ✦ DM FAST", "content": ["a"]}'
        )
        self.assertEqual(parsed["header"], "WANTED ✦ DM FAST")
        # Seller-framed AI suggestion is rejected -> None, falls back to default.
        parsed_bad = ai_rephraser._parse_analysis_json(
            '{"is_listing": true, "blocked": false, "header": "FOR SALE", "content": ["a"]}'
        )
        self.assertIsNone(parsed_bad["header"])
        # Missing header -> None.
        parsed_missing = ai_rephraser._parse_analysis_json(
            '{"is_listing": true, "blocked": false, "content": ["a"]}'
        )
        self.assertIsNone(parsed_missing["header"])
        self.assertIn("header", ai_rephraser.ANALYZE_PROMPT)

    def test_build_ai_message_no_price_variant(self):
        out, _ = parser.build_ai_message(
            content_lines=["Tuyo full access"], our_price=None,
            platform="tuyo", contact_username="@buyer", intent="neutral",
        )
        self.assertIn("TUYO WANTED", out)
        self.assertNotIn("Price", out)

    def test_build_ai_message_knows_no_price_passed(self):
        """When our_price is 0/None the price line is skipped."""
        out, _ = parser.build_ai_message(
            content_lines=["Line"], our_price=0,
            platform="x", contact_username="@b", intent="sell",
        )
        self.assertNotIn("$0", out)

    # -------------------------------------------------------------
    # FREE-TIER RELIABILITY: chatter pre-filter, blocked-keyword screen,
    # AI result cache, OpenRouter adapter
    # -------------------------------------------------------------
    def test_filters_chatter_prefilter_fires_on_admin_chatter(self):
        """Rule posts / admin pins / greetings are classified as chatter."""
        self.assertEqual(
            filters.obvious_non_listing("📌 Group rules: no fv, no adverts. Read pinned."),
            filters.REASON_CHATTER,
        )
        self.assertEqual(
            filters.obvious_non_listing("Admin: please read the announcement above."),
            filters.REASON_CHATTER,
        )
        self.assertEqual(
            filters.obvious_non_listing("🔥 Welcome to the group!"),
            filters.REASON_CHATTER,
        )
        self.assertEqual(
            filters.obvious_non_listing("Hello everyone testing 1 2 3"),
            filters.REASON_CHATTER,
        )

    def test_filters_chatter_prefilter_vetoed_by_listing_signals(self):
        """A price, platform keyword, @contact or WTS/WTB wording vetoes chatter."""
        self.assertIsNone(filters.obvious_non_listing("New rules post WTS Bybit $100"))  # price+platform
        self.assertIsNone(filters.obvious_non_listing("Welcome sellers — WTB Netflix dm @trader"))
        self.assertIsNone(filters.obvious_non_listing("Admin note: kyc verified accounts available"))
        # Long messages always go to the AI for judgment.
        long_text = "Please read and follow " + ("everything " * 80)
        self.assertIsNone(filters.obvious_non_listing(long_text))

    def test_filters_blocked_keyword_screen(self):
        """Stolen/hacked signals are caught; clean listings pass."""
        self.assertEqual(filters.contains_blocked_keyword("WTS hacked netflix login"), "hacked")
        self.assertEqual(filters.contains_blocked_keyword("cracked account for sale"), "cracked")
        self.assertIsNotNone(filters.contains_blocked_keyword("combolist cc dumps fullz"))
        self.assertIsNone(filters.contains_blocked_keyword("Bybit verified kyc full access"))
        self.assertIsNone(filters.contains_blocked_keyword("   "))

    def test_db_ai_cache_roundtrip_and_ttl(self):
        """set/get/prune works, and stale entries (TTL negative) are not returned."""
        fp = db.make_listing_fingerprint("WTS Bybit $100", price=100.0)
        db.set_ai_cache(fp, '{"is_listing": true}', model_name="test-model", db_path=TEST_DB)
        cached = db.get_ai_cache(fp, max_age_hours=999, db_path=TEST_DB)
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0], '{"is_listing": true}')
        self.assertEqual(cached[1], "test-model")

        # Overwrite (unique fingerprint) -> single row, newest value wins.
        db.set_ai_cache(fp, '{"is_listing": false}', model_name="test-model2", db_path=TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        try:
            count = conn.execute("SELECT count(*) FROM ai_cache WHERE fingerprint = ?", (fp,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)
        cached2 = db.get_ai_cache(fp, max_age_hours=999, db_path=TEST_DB)
        self.assertEqual(cached2[0], '{"is_listing": false}')

        # Negative TTL == expired -> not returned.
        self.assertIsNone(db.get_ai_cache(fp, max_age_hours=-1, db_path=TEST_DB))
        # Unknown fingerprint -> miss.
        self.assertIsNone(db.get_ai_cache("nope", max_age_hours=999, db_path=TEST_DB))

        # prune removes rows older than the window.
        old_fp = db.make_listing_fingerprint("old ad $10", price=10.0)
        db.set_ai_cache(old_fp, "{}", db_path=TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute(
                "UPDATE ai_cache SET created_at = '2000-01-01T00:00:00+00:00' "
                "WHERE fingerprint = ?",
                (old_fp,),
            )
            conn.commit()
        finally:
            conn.close()
        pruned = db.prune_ai_cache(max_age_hours=48, db_path=TEST_DB)
        self.assertGreaterEqual(pruned, 1)
        self.assertIsNone(db.get_ai_cache(old_fp, max_age_hours=999, db_path=TEST_DB))

    def test_ai_rephraser_cache_fingerprint_matches_dedup_key(self):
        """The AI cache key is the SAME key the dedup fingerprint uses."""
        fp_ai = ai_rephraser._cache_fingerprint("Netflix 1 Month $40")
        fp_db = db.make_listing_fingerprint("Netflix 1 Month $40", price=40.0)
        self.assertEqual(fp_ai, fp_db)
        # A different price must map to a different cache entry.
        fp_other = db.make_listing_fingerprint("Netflix 1 Month $40", price=41.0)
        self.assertNotEqual(fp_ai, fp_other)
        # No price -> key built from text alone still matches the dedup key.
        no_price_ai = ai_rephraser._cache_fingerprint("KYC full access VPS")
        self.assertEqual(no_price_ai, db.make_listing_fingerprint("KYC full access VPS"))

    def test_ai_rephraser_adapts_openrouter_response(self):
        """OpenRouter's OpenAI-shaped dict is normalized; junk is rejected."""
        adapted = ai_rephraser._adapt_openrouter_response(
            {"choices": [{"message": {"content": '{"is_listing": true}'}}]}
        )
        self.assertIsNotNone(adapted)
        self.assertEqual(adapted.choices[0].message.content, '{"is_listing": true}')
        self.assertEqual(adapted.model, ai_rephraser._openrouter_model)
        self.assertIsNone(ai_rephraser._adapt_openrouter_response({"nope": True}))
        self.assertIsNone(ai_rephraser._adapt_openrouter_response(None))
        self.assertIsNone(
            ai_rephraser._adapt_openrouter_response(
                {"choices": [{"message": {"content": "   "}}]}
            )
        )

    def test_ai_openrouter_fallback_respects_cooldown(self):
        """The circuit breaker gates calls (early None) and engages on failure."""
        import asyncio

        class _ExplodingHttp:
            class AsyncClient:
                def __init__(self, timeout):
                    self.timeout = timeout

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def post(self, *a, **k):
                    raise RuntimeError("no network in tests")

        saved_key = ai_rephraser._openrouter_api_key
        saved_ts = ai_rephraser._openrouter_next_attempt_ts
        saved_httpx = ai_rephraser.httpx
        ai_rephraser._openrouter_api_key = "test-key"
        ai_rephraser.httpx = _ExplodingHttp
        try:
            # Cooldown active -> early None, nothing touches the API, cooldown unchanged.
            ai_rephraser._openrouter_next_attempt_ts = time.monotonic() + 3600
            self.assertIsNone(
                asyncio.run(ai_rephraser._create_openrouter_completion("hi", json_mode=False))
            )
            self.assertGreater(ai_rephraser._openrouter_next_attempt_ts, time.monotonic())

            # Gate cleared -> request attempted and fails -> cooldown ENGAGED.
            ai_rephraser._openrouter_next_attempt_ts = 0.0
            self.assertIsNone(
                asyncio.run(ai_rephraser._create_openrouter_completion("hi", json_mode=False))
            )
            self.assertGreater(ai_rephraser._openrouter_next_attempt_ts, time.monotonic())
        finally:
            ai_rephraser._openrouter_api_key = saved_key
            ai_rephraser._openrouter_next_attempt_ts = saved_ts
            ai_rephraser.httpx = saved_httpx

    # -------------------------------------------------------------
    # SANITIZER / BODY-GUARD TESTS
    # -------------------------------------------------------------
    def test_sanitize_body_drops_price_and_handle_lines(self):
        body = [
            "WTB NEED GOOD SELLERS",
            "KYC MEXC VIA LINK ONLY",
            "PRICE 25$",
            "GOOD SELLERS POLAND DM ME",
            "DM: @ARO_KYC1",
        ]
        lines, ok = parser.prepare_body(body, " ".join(body))
        self.assertTrue(ok)
        joined = " | ".join(lines)
        self.assertNotIn("25$", joined)
        self.assertNotIn("@ARO_KYC1", joined)
        self.assertIn("KYC MEXC", joined)

    def test_sanitize_body_styled_digit_lines(self):
        body = ["KYC CHATGPT", "\U0001D7ED\U0001D7EE US", "ANY EU"]
        lines, ok = parser.prepare_body(body, " ".join(body))
        self.assertTrue(ok)
        self.assertEqual(lines, ["KYC CHATGPT", "ANY EU"])

        body2 = ["\U0001D7ED\U0001D7F2 EUR"]
        lines2, ok2 = parser.prepare_body(body2, " ".join(body2))
        self.assertFalse(ok2)
        self.assertEqual(lines2, [])

        body3 = ["\U0001D7ED\U0001D7F2$", "Netflix 1 month", "EU supported"]
        lines3, ok3 = parser.prepare_body(body3, " ".join(body3))
        self.assertTrue(ok3)
        self.assertEqual(lines3, ["Netflix 1 month", "EU supported"])

    def test_sanitize_body_midline_price_blob_removed(self):
        body = ["Chat Gpt \u2014 Readymade 35$_USDT", "USA   50$"]
        lines, ok = parser.prepare_body(body, " ".join(body))
        self.assertFalse(ok)
        self.assertEqual(lines, ["Chat Gpt \u2014 Readymade"])

    def test_prepare_body_requires_substance(self):
        lines, ok = parser.prepare_body(["Any country"], "Any country")
        self.assertFalse(ok)
        self.assertEqual(lines, ["Any country"])

        lines, ok = parser.prepare_body([], "")
        self.assertFalse(ok)
        self.assertEqual(lines, [])

        lines, ok = parser.prepare_body(["1 VIVID KYC", "ANY EU"], "1 VIVID KYC ANY EU")
        self.assertTrue(ok)

    def test_build_ai_message_sanitizes_body_by_default(self):
        msg, _entities = parser.build_ai_message(
            content_lines=["KYC CURVE PAY", "PRICE: $30", "DM: @godf4therCO"],
            our_price=30,
            platform="curve",
            contact_username="@buy",
            intent="buy",
        )
        self.assertNotIn("PRICE: $30", msg)
        self.assertNotIn("@godf4therCO", msg)
        self.assertIn("KYC CURVE PAY", msg)

    def test_build_ai_message_preserves_body_when_sanitize_false(self):
        # /repair reconstructs the as-published text: verbatim body, no sanitizing.
        msg, _entities = parser.build_ai_message(
            content_lines=["KYC CURVE PAY", "PRICE: $30"],
            our_price=30,
            platform="curve",
            contact_username="@buy",
            intent="buy",
            sanitize_body=False,
        )
        self.assertIn("KYC CURVE PAY", msg)
        self.assertIn("PRICE: $30", msg)

    # -------------------------------------------------------------
    # FILTER GATE TESTS
    # -------------------------------------------------------------
    def test_detect_payment_proof(self):
        self.assertTrue(filters.detect_payment_proof("proof of payment attached here"))
        self.assertTrue(filters.detect_payment_proof("transaction received confirmed"))
        self.assertTrue(filters.detect_payment_proof("I paid 40$ sent receipt"))
        self.assertTrue(filters.detect_payment_proof("amount of $50 paid"))
        self.assertIsNone(filters.detect_payment_proof("KYC CURVE PAY ANY EU"))
        self.assertIsNone(filters.detect_payment_proof(""))
        self.assertIsNone(filters.detect_payment_proof("   "))

    def test_has_clear_listing_signal(self):
        self.assertTrue(filters.has_clear_listing_signal("WTB Chatgpt PRICE 30$", 30))
        self.assertTrue(filters.has_clear_listing_signal("KYC netflix 200$", 150))
        self.assertTrue(filters.has_clear_listing_signal("DM @buyer 40", 40))
        # Money present but no concrete platform / listing signal -> not clear.
        self.assertFalse(filters.has_clear_listing_signal("GOOD SELLER", 50))
        # No price -> never clear, even with a platform word.
        self.assertFalse(filters.has_clear_listing_signal("netflix kyc available", None))
        self.assertFalse(filters.has_clear_listing_signal("netflix kyc available", 0))

    # -------------------------------------------------------------
    # SKIP REVIEW / DIGEST TESTS
    # -------------------------------------------------------------
    def test_skip_review_and_reopen_flow(self):
        sup_id = db.add_supplier("skip_review_chan", db_path=TEST_DB)
        listing_id = db.insert_listing(
            sup_id,
            904477,
            "netflix",
            None,
            12.5,
            9.0,
            "skipped_filter",
            "some raw leaked body",
            "clean body",
            db_path=TEST_DB,
        )
        db.log_skip(sup_id, 904477, "filter", "some raw leaked body", db_path=TEST_DB)

        rows = db.get_skipped_listings(limit=10, db_path=TEST_DB)
        skip = next((r for r in rows if r["message_id"] == 904477), None)
        self.assertIsNotNone(skip)
        self.assertEqual(skip["listing_id"], listing_id)
        self.assertEqual(skip["listing_status"], "skipped_filter")
        self.assertEqual(skip["channel_username"], "skip_review_chan")

        reopened = db.reopen_skipped(skip["skip_id"], db_path=TEST_DB)
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened["status"], "pending_approval")

        # Second reopen is a no-op: the listing left the skipped state.
        self.assertIsNone(db.reopen_skipped(skip["skip_id"], db_path=TEST_DB))

        with sqlite3.connect(TEST_DB) as conn:
            conn.row_factory = sqlite3.Row
            audit = conn.execute(
                "SELECT * FROM audit_log WHERE listing_id = ? AND action = 'skipped_reopen'",
                (listing_id,),
            ).fetchone()
            self.assertIsNotNone(audit)
            self.assertIn(str(skip["skip_id"]), audit["detail"])

    def test_skip_digest_marker(self):
        self.assertEqual(db.get_skip_digest_marker(db_path=TEST_DB), 0)
        db.set_skip_digest_marker(7, db_path=TEST_DB)
        self.assertEqual(db.get_skip_digest_marker(db_path=TEST_DB), 7)
        db.set_skip_digest_marker(3, db_path=TEST_DB)
        self.assertEqual(db.get_skip_digest_marker(db_path=TEST_DB), 3)


if __name__ == "__main__":
    unittest.main()