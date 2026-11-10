"""Contract Tests for Phase 2A: Tokens, Registry, Renderer, and Feature Flags."""

import os
import tempfile
import unittest
import db
from services.tokens import Area, build_token, parse_token, resolve_callback, LEGACY_CALLBACK_MAP, MAX_TOKEN_BYTES
from services.feature_flags import is_flag_enabled, set_flag, UI_V2_RENDERER, UI_V2_REVIEW
from services.renderer import show, get_console_msg_id, set_console_msg_id


class FakeEditEvent:
    def __init__(self, data_str=None, message_id=100, sender_id=999):
        self.data = data_str.encode("utf-8") if data_str else b"nav:home"
        self.message_id = message_id
        self.sender_id = sender_id
        self.client = self
        self.edited = []
        self.deleted = False
        self.sent = []
        self.should_raise_edit = False

    async def edit(self, text, buttons=None, **kw):
        if self.should_raise_edit:
            raise Exception("MessageCantBeEdited")
        self.edited.append({"text": text, "buttons": buttons, "kw": kw})
        return self

    async def delete(self):
        self.deleted = True

    async def send_message(self, peer, text, buttons=None, **kw):
        self.sent.append({"peer": peer, "text": text, "buttons": buttons, "kw": kw})
        return type("SentMsg", (), {"id": len(self.sent) + 500})()

    async def delete_messages(self, peer, ids):
        pass


class Phase2AContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._db_path = os.path.join(tempfile.gettempdir(), f"test_p2a_{os.getpid()}_{id(self)}.db")
        db.init_db(self._db_path)

    def tearDown(self):
        if os.path.exists(self._db_path):
            try:
                os.remove(self._db_path)
            except Exception:
                pass

    # 1. Tokens Building and Parsing
    def test_token_build_and_parse(self):
        tok = build_token(Area.REV, "ok", 123)
        self.assertEqual(tok, "rev:ok:123")
        self.assertLessEqual(len(tok.encode("utf-8")), MAX_TOKEN_BYTES)

        parsed = parse_token(tok)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.area, "rev")
        self.assertEqual(parsed.action, "ok")
        self.assertEqual(parsed.args, ("123",))

    def test_token_overflow_rejected(self):
        long_arg = "x" * 70
        with self.assertRaises(ValueError):
            build_token(Area.NAV, "test", long_arg)

    # 2. Legacy Token Map Resolution
    def test_legacy_token_map_resolution(self):
        # Exact mappings
        self.assertEqual(resolve_callback("menu:home"), "nav:home")
        self.assertEqual(resolve_callback("skip_all_pending"), "rev:skall")
        self.assertEqual(resolve_callback("forwarders:view"), "fwd:list")
        self.assertEqual(resolve_callback("wiz:cancel"), "wiz:x")

        # Parameterized regex mappings
        self.assertEqual(resolve_callback("approve:42"), "rev:ok:42")
        self.assertEqual(resolve_callback("skip:99"), "rev:skip:99")
        self.assertEqual(resolve_callback("sold:101"), "post:fill:101")
        self.assertEqual(resolve_callback("sold_y:101"), "post:filly:101")
        self.assertEqual(resolve_callback("dest:5"), "grp:v:5:0")
        self.assertEqual(resolve_callback("sup:7"), "src:v:7:0")

        # Modern tokens pass through untouched
        self.assertEqual(resolve_callback("rev:ok:42"), "rev:ok:42")
        self.assertEqual(resolve_callback("nav:home"), "nav:home")

    # 3. Feature Flags
    def test_feature_flags(self):
        # Default
        self.assertFalse(is_flag_enabled(UI_V2_RENDERER, db_path=self._db_path))
        set_flag(UI_V2_RENDERER, True, db_path=self._db_path)
        self.assertTrue(is_flag_enabled(UI_V2_RENDERER, db_path=self._db_path))
        # Persist change
        set_flag(UI_V2_REVIEW, False, db_path=self._db_path)
        self.assertFalse(is_flag_enabled(UI_V2_REVIEW, db_path=self._db_path))
        set_flag(UI_V2_REVIEW, True, db_path=self._db_path)
        self.assertTrue(is_flag_enabled(UI_V2_REVIEW, db_path=self._db_path))

    # 4. Renderer show()
    async def test_renderer_edit_in_place(self):
        ev = FakeEditEvent()
        await show(ev, "Updated Card Text", buttons=[[]], db_path=self._db_path)
        self.assertEqual(len(ev.edited), 1)
        self.assertEqual(ev.edited[0]["text"], "Updated Card Text")
        self.assertEqual(get_console_msg_id(db_path=self._db_path), 100)

    async def test_renderer_reanchor_on_command_or_failure(self):
        ev = FakeEditEvent()
        # With reanchor=True, it should send a new message instead of editing in place
        await show(ev, "Fresh Menu", buttons=[[]], reanchor=True, db_path=self._db_path)
        self.assertEqual(len(ev.edited), 0)
        self.assertEqual(len(ev.sent), 1)
        self.assertEqual(ev.sent[0]["text"], "Fresh Menu")
        self.assertEqual(get_console_msg_id(db_path=self._db_path), 501)


if __name__ == "__main__":
    unittest.main()

