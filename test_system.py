"""Comprehensive unit tests for db, parser, and filters modules for financial/streaming account reselling."""

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import emoji

import ai_rephraser
import countries
import db
import filters
import parser
import publish_guard
from telethon.errors import ChatWriteForbiddenError, FloodWaitError

TEST_DB = "test_monitor.db"


def _utf16_to_char(text: str, utf16_offset: int) -> int:
    """Python char index whose UTF-16 code-unit offset == utf16_offset.

    A single source of truth for verifying that every MessageEntityCustomEmoji
    offset/length (which Telegram measures in UTF-16 code units) points at the
    exact anchor emoji inside the final message string.
    """
    for i in range(len(text) + 1):
        if len(text[:i].encode("utf-16-le")) // 2 >= utf16_offset:
            return i
    return len(text)


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

    def test_build_ai_message_never_emits_numeric_price(self):
        """Prices must NEVER reach rendered output: every post carries the
        static '🤑 Price  DM' footer and the frozen 'WTB ✦ DM FAST' header no
        matter what the source text contained."""
        import re
        for source in ("Tuyo full access", "Tuyo full access 50$", "Tuyo 1000 EUR", "Tuyo 12,50€"):
            msg, _ = parser.build_ai_message(
                content_lines=["Tuyo full access"],
                platform="tuyo",
                contact_username="@buyer",
                intent="sell",
                source_text=source,
            )
            self.assertIn("TUYO WTB ✦ DM FAST", msg, f"header default broken for {source}")
            self.assertIn("🤑 Price  DM", msg, f"static price footer missing for {source}")
            leaked = re.findall(r"\$\s?\d|€|\b(?:USD|USDT|EUR)\b", msg)
            self.assertEqual(leaked, [], f"numeric price leaked in output for {source}: {msg!r}")

    def test_price_footer_static(self):
        """The footer price line is always the static '🤑 Price  DM'."""
        msg, _ = parser.build_ai_message(
            content_lines=["Bybit kyc", "da"],
            platform="bybit",
            contact_username="@x",
            intent="sell",
        )
        self.assertIn("🤑 Price  DM", msg)
        self.assertIn("WTB ✦ DM FAST", msg)
        self.assertNotIn("$38", msg)
        self.assertNotIn("€", msg)

    def test_custom_emoji_entity_offsets_valid(self):
        """Every entity must point inside the final text, ascending and non-overlapping."""
        msg, entities = parser.build_ai_message(
            content_lines=["Netflix 1 month", "4K ready"],
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
    # COUNTRY -> CUSTOM EMOJI TESTS
    # -------------------------------------------------------------
    def test_country_emoji_map_has_fixed_ids(self):
        """The 4 ground-truth mappings supplied by the user are exact."""
        self.assertEqual(countries.emoji_for("Egypt"), 5293992082212409502)
        self.assertEqual(countries.emoji_for("United States"), 5294244076533600593)
        self.assertEqual(countries.emoji_for("United Kingdom"), 5293993521026453119)
        self.assertEqual(countries.emoji_for("Saudi Arabia"), 5294163983983463099)

    def test_detect_countries_aliases_dedupe_and_order(self):
        """Names are returned as written, deduped per country, first spelling wins."""
        text = "USA then Egypt, and again Egypt, finally KSA and the UK"
        self.assertEqual(countries.detect_countries(text),
                         ["USA", "Egypt", "KSA", "UK"])
        self.assertEqual(countries.detect_countries("America / UK / Britain"),
                         ["America", "UK"])
        self.assertEqual(countries.detect_countries("south korea vs north korean"),
                         ["south korea"])
        # Repeated countries collapse to the first spelling.
        self.assertEqual(countries.detect_countries("USA and US"), ["USA"])
        self.assertEqual(countries.detect_countries("UK and Britain"), ["UK"])
        # Leading article trimmed for display.
        self.assertEqual(countries.detect_countries("ship from THE USA and the uk"),
                         ["USA", "uk"])
        self.assertEqual(countries.detect_countries(""), [])
        self.assertEqual(countries.detect_countries(None), [])

    def test_detect_countries_whole_token_only(self):
        """Two-letter aliases must not fire inside other words."""
        self.assertEqual(countries.detect_countries("just some better used token"), [])
        self.assertEqual(countries.detect_countries("private request"), [])

    def test_detect_countries_skips_urls_and_handles(self):
        self.assertEqual(
            countries.detect_countries("https://ru.example.com/login @us t.me/uk"), []
        )
        self.assertEqual(
            countries.detect_countries("Orders from Russia (https://x.ru/account @us)"),
            ["Russia"],
        )

    def test_detect_countries_partial_name_compounds(self):
        """Different territories that contain a mapped country word must not match."""
        self.assertEqual(countries.detect_countries("cover South Sudan only"), [])
        self.assertEqual(countries.detect_countries("DR Congo seller"), [])
        self.assertEqual(countries.detect_countries("Northern Cyprus HQs"), [])

    def test_detect_countries_own_flags_for_home_nations(self):
        self.assertEqual(countries.detect_countries("England, Scotland, Wales"),
                         ["England", "Scotland", "Wales"])
        # Per requirement, England resolves to the GB flag; Scotland/Wales keep
        # their own pack entries.
        self.assertEqual(countries.emoji_for("England"), countries.emoji_for("United Kingdom"))
        self.assertEqual(countries.emoji_for("Scotland"), 5294434665707368018)
        self.assertEqual(countries.emoji_for("Wales"), 5294139949346476093)

    def test_detect_countries_guinea_bissau_mapped_grenada_not(self):
        """Row 74 is Guinea-Bissau (its own line); Grenada is not in the list."""
        self.assertEqual(countries.detect_countries("Guinea-Bissau here"),
                         ["Guinea-Bissau"])
        self.assertEqual(countries.emoji_for("Guinea-Bissau"), 5294409819321550432)
        self.assertEqual(countries.detect_countries("ship to Grenada"), [])
        with self.assertRaises(KeyError):
            countries.emoji_for("Grenada")

    def test_build_ai_message_emits_country_lines(self):
        """Each country gets its own line, showing the name as written and
        carrying the real flag emoji that its custom document's alt matches."""
        msg, entities = parser.build_ai_message(
            content_lines=["Available"],
            platform="netflix",
            contact_username="@buy",
            intent="sell",
            source_text="USA\nEgypt\nSaudi Arabia",
        )
        self.assertIn("USA  🇺🇸", msg)
        self.assertIn("Egypt  🇪🇬", msg)
        self.assertIn("Saudi Arabia  🇸🇦", msg)
        self.assertNotIn("United States", msg)
        self.assertNotIn("United Kingdom", msg)
        # Three country lines, each ending with its real flag anchor.
        flag_lines = [ln for ln in msg.split("\n")
                      if ln.strip().endswith(tuple(countries.alt_for(c) for c in
                                                   ("United States", "Egypt", "Saudi Arabia")))]
        self.assertEqual(len(flag_lines), 3)
        for line in flag_lines:
            self.assertGreater(msg.index(line), msg.index("Available"),
                               "country lines must come after the body")

        celeb_ids = {e.document_id for e in entities}
        for name in ("USA", "Egypt", "Saudi Arabia"):
            self.assertIn(countries.emoji_for(name), celeb_ids,
                          f"{name} custom emoji must be attached")
        # Still a valid, monotonic entity layout (offsets ascend, inside text).
        units = len(msg.encode("utf-16-le")) // 2
        prev = -1
        for e in entities:
            self.assertGreater(e.offset, prev)
            self.assertLessEqual(e.offset + e.length, units)
            prev = e.offset

    def test_build_ai_message_no_countries_when_no_source_text(self):
        msg, entities = parser.build_ai_message(
            content_lines=["Plain line"],
            platform="netflix",
            intent="sell",
        )
        self.assertNotIn("United States", msg)
        self.assertTrue(all(not hasattr(e, "document_id")
                            or e.document_id not in countries.COUNTRY_EMOJI.values()
                            for e in entities))

    def test_build_ai_message_skips_unmapped_countries(self):
        msg, entities = parser.build_ai_message(
            content_lines=["Available"],
            platform="netflix",
            intent="sell",
            source_text="Applicable in DR Congo and South Sudan only",
        )
        self.assertNotIn("DR Congo", msg)
        self.assertNotIn("South Sudan", msg)
        self.assertEqual(entities, [e for e in entities
                                    if e.document_id not in countries.COUNTRY_EMOJI.values()])

    # -------------------------------------------------------------
    # PART 1 REGRESSION: no duplicate/garbled country lines
    # -------------------------------------------------------------
    def test_country_flags_on_body_lines_no_duplicate(self):
        """PRESERVE COMPLETE LISTS body + separate flag lines must not double
        a country ('NO POLAND' must not also emit a generated 'POLAND  🇵🇱' line)."""
        msg, entities = parser.build_ai_message(
            content_lines=["KYC BY LINK", "ANY EUROPE", "NO POLAND"],
            platform="bybit",
            contact_username="@b",
            intent="sell",
            source_text="KYC BY LINK ANY EUROPE NO POLAND",
        )
        lines = [ln.strip() for ln in msg.split("\n")]
        # The flag lands on the body line that already says the country…
        self.assertIn("NO POLAND  🇵🇱", lines)
        self.assertIn("ANY EUROPE  🇪🇺", lines)
        # …a non-country line stays plain (no flag, no leftover placeholder)…
        self.assertIn("KYC BY LINK", lines)
        # …and NO standalone generated 'POLAND  🇵🇱' line exists (single mention).
        self.assertEqual(len([ln for ln in lines if ln == "POLAND  🇵🇱"]), 0)
        self.assertEqual(len([ln for ln in lines if ln == "NO POLAND  🇵🇱"]), 1)
        # Flag entities are REALLY attached (EU + Poland custom emoji).
        doc_ids = {e.document_id for e in entities}
        self.assertIn(countries.emoji_for("European Union"), doc_ids)
        self.assertIn(countries.emoji_for("Poland"), doc_ids)

    # -------------------------------------------------------------
    # PART 2: custom flag render on country lines
    # -------------------------------------------------------------
    def test_country_flags_multi_country_body_line_split(self):
        """\"UK, USA and Germany\" splits into one flag-carrying line per country."""
        msg, entities = parser.build_ai_message(
            content_lines=["UK, USA and Germany"],
            platform="netflix",
            contact_username="@b",
            intent="sell",
        )
        lines = [ln.strip() for ln in msg.split("\n")]
        self.assertIn("UK  🇬🇧", lines)
        self.assertIn("USA  🇺🇸", lines)
        self.assertIn("Germany  🇩🇪", lines)
        self.assertNotIn("UK, USA and Germany", lines)
        doc_ids = {e.document_id for e in entities}
        for name in ("United Kingdom", "United States", "Germany"):
            self.assertIn(countries.emoji_for(name), doc_ids)

    def test_country_flags_unmapped_countries_bare(self):
        """A country with no flag mapping stays plain text — never a wrong flag."""
        flagged, covered = countries.flag_body_lines(["ship to Grenada"])
        line, ids = flagged[0]
        self.assertEqual(line, "ship to Grenada")
        self.assertEqual(ids, [])
        self.assertNotIn("Grenada", covered)
        # End-to-end: build never appends anything for an unmapped mention.
        msg, _entities = parser.build_ai_message(
            content_lines=["ship to Grenada"],
            platform="netflix",
            contact_username="@b",
            intent="sell",
            source_text="ship to Grenada",
        )
        self.assertIn("ship to Grenada", msg)
        self.assertNotIn("🇬🇩", msg)

    def test_country_aliases_resolve_to_correct_flags(self):
        """Requirement 2 aliases resolve to the right flag entries."""
        self.assertEqual(countries.emoji_for("EU"), countries.emoji_for("European Union"))
        self.assertEqual(countries.emoji_for("Europe"), countries.emoji_for("European Union"))
        self.assertEqual(countries.emoji_for("europe"), countries.emoji_for("European Union"))
        self.assertEqual(countries.emoji_for("USA"), countries.emoji_for("United States"))
        self.assertEqual(countries.emoji_for("US"), countries.emoji_for("United States"))
        self.assertEqual(countries.emoji_for("UK"), countries.emoji_for("United Kingdom"))
        self.assertEqual(countries.emoji_for("Great Britain"), countries.emoji_for("United Kingdom"))
        self.assertEqual(countries.emoji_for("England"), countries.emoji_for("United Kingdom"))
        # detect/flag a 'Europe' alias so the EU flag lands on the line.
        flagged, covered = countries.flag_body_lines(["ANY EUROPE"])
        self.assertEqual(covered, {"European Union"})
        self.assertEqual(
            flagged[0][1],
            [(countries.alt_for("European Union"), countries.emoji_for("European Union"))],
        )

    def test_flags2024_decodes_unicode_to_country(self):
        """Raw dump rows are turned into a name -> id map by decoding each
        flag's regional-indicator code points — never hand-typed names."""
        self.assertEqual(countries.iso2_from_flag_emoji("🇪🇺"), "EU")
        self.assertEqual(countries.iso2_from_flag_emoji("🇺🇸"), "US")
        self.assertEqual(countries.iso2_from_flag_emoji("🇬🇧"), "GB")
        self.assertEqual(countries.name_from_flag_emoji("🇪🇺"), "European Union")
        self.assertEqual(countries.name_from_flag_emoji("🇩🇪"), "Germany")
        self.assertEqual(countries.name_from_flag_emoji("🇫🇷"), "France")
        # Subdivision flags (England) and unknown glyphs.
        self.assertEqual(countries.name_from_flag_emoji("🏴󠁧󠁢󠁥󠁮󠁧󠁿"), "England")
        self.assertIsNone(countries.name_from_flag_emoji("🚩"))

    def test_flags2024_dump_parser_skips_unknown_rows(self):
        """Malformed/unknown rows are reported and dropped — never guessed."""
        dump = (
            "1)🇪🇺 [1234567890123456789]\n"
            "2)🇫🇷 [2234567890123456789]\n"
            "garbage line without brackets\n"
        )
        mapping, errors = countries.build_flags2024_mapping(dump)
        self.assertEqual(mapping["European Union"], 1234567890123456789)
        self.assertEqual(mapping["France"], 2234567890123456789)
        self.assertNotIn("🚩", mapping)
        self.assertEqual(len(errors), 2)  # unknown flag + unparsable row
        # A parsed dump can REPLACE the active map (pack switch) on request.
        swapped = {k: countries.COUNTRY_EMOJI[k] for k in ("European Union", "France")}
        for name, doc in mapping.items():
            swapped[name] = doc
        self.assertNotEqual(swapped["European Union"], countries.emoji_for("EU"))
        # The dump's own glyph also becomes the alt anchor for each row.
        alts = countries.build_flags2024_alts(dump)
        self.assertEqual(alts["European Union"], "🇪🇺")
        self.assertEqual(alts["France"], "🇫🇷")

    def test_flag_anchor_is_real_matching_emoji(self):
        """Every flag anchors on the country's EXACT alt emoji (never a generic
        placeholder), and each entity's length equals that emoji's UTF-16 size."""
        for alias, canonical in [("EU", "European Union"), ("USA", "United States"),
                                 ("Germany", "Germany"), ("England", "United Kingdom"),
                                 ("Scotland", "Scotland"), ("Wales", "Wales")]:
            alt = countries.alt_for(alias)
            self.assertTrue(emoji.is_emoji(alt), f"{alias} anchor must be an emoji")
            self.assertEqual(parser._utf16_len(alt), len(alt.encode("utf-16-le")) // 2)
            self.assertEqual(countries.name_from_flag_emoji(alt), canonical)
        # Multi-code-unit anchors: 🇵🇱 = 4 UTF-16 units, Scotland's tag flag = 14.
        self.assertEqual(parser._utf16_len(countries.alt_for("Poland")), 4)
        self.assertEqual(parser._utf16_len(countries.alt_for("Scotland")), 14)

    def test_flag_offset_math_survives_multibyte_emoji_prefix(self):
        """UTF-16 offsets must survive non-BMP emoji BEFORE the flag — the exact
        ordering that broke in production (🔥🔥 header, country flags, 💀 order
        line). A Python-len() computation would be off by 2 for every flag here;
        the shared helper uses real UTF-16 length."""
        text = "🔥🔥 NETFLIX\nNO POLAND 🇵🇱\nANY EUROPE 🇪🇺\n💀 Price  : DM\n"
        poland = countries.alt_for("Poland")
        eu = countries.alt_for("European Union")
        entities = [
            parser._make_custom_emoji_entity(0, parser.CE_FIRE, parser.PH_FIRE),
            parser._make_custom_emoji_entity(
                parser._utf16_len(text[:text.index(poland)]),
                countries.emoji_for("Poland"), poland),
            parser._make_custom_emoji_entity(
                parser._utf16_len(text[:text.index(eu)]),
                countries.emoji_for("European Union"), eu),
        ]
        for e, anchor in zip(entities, (parser.PH_FIRE, poland, eu)):
            start = _utf16_to_char(text, e.offset)
            end = _utf16_to_char(text, e.offset + e.length)
            self.assertEqual(text[start:end], anchor,
                             "entity must wrap its exact alt emoji")
            self.assertEqual(e.offset, len(text[:start].encode("utf-16-le")) // 2)
            self.assertEqual(e.length, len(anchor.encode("utf-16-le")) // 2)
        # PROOF the bug class: Python len() before 🇵🇱 is NOT its UTF-16 offset.
        py_prefix = len(text[:text.index(poland)])
        u16_prefix = len(text[:text.index(poland)].encode("utf-16-le")) // 2
        self.assertNotEqual(py_prefix, u16_prefix)  # two non-BMP flames skew it
        self.assertEqual(entities[1].offset, u16_prefix)

    def test_custom_emoji_entities_utf16_aligned(self):
        """INTEGRATION: with a non-BMP post-number banner BEFORE the header and
        country flags BETWEEN header+filters, every entity must still be UTF-16
        aligned against the real message text (offset+lengte units as Telegram
        counts them)."""
        msg, entities = parser.build_ai_message(
            content_lines=["KYC BY LINK", "ANY EUROPE", "NO POLAND", "UK and USA"],
            platform="bybit",
            contact_username="@b",
            intent="sell",
            post_number=17,
            source_text="KYC BY LINK ANY EUROPE NO POLAND UK and USA",
        )
        units = len(msg.encode("utf-16-le")) // 2
        prev_end = 0
        for e in entities:
            start = _utf16_to_char(msg, e.offset)
            end = _utf16_to_char(msg, e.offset + e.length)
            anchored = msg[start:end]
            # UTF-16 units: offset == encoded length of everything before it…
            self.assertEqual(e.offset, len(msg[:start].encode("utf-16-le")) // 2,
                             f"entity {e} offset is not UTF-16 aligned")
            # …each entity wraps exactly one emoji of matching length…
            self.assertGreaterEqual(len(anchored), 1)
            self.assertEqual(e.length, len(anchored.encode("utf-16-le")) // 2)
            # …all in-bounds and non-overlapping.
            self.assertGreaterEqual(e.offset, prev_end)
            self.assertLessEqual(e.offset + e.length, units)
            prev_end = e.offset + e.length
        # The ‼ header flames must be rebased past the #N banner prefix.
        header_prefix = msg[:msg.index(parser.PH_FIRE)]
        self.assertEqual(entities[0].offset, parser._utf16_len(header_prefix))
        self.assertIn("NO POLAND  🇵🇱", msg)
        self.assertIn("ANY EUROPE  🇪🇺", msg)

    def test_sanitizer_never_strips_inserted_flag_anchors(self):
        """Flag glyphs are real emoji; they reach the post only because they
        are attached AFTER sanitization, and survive the leak-lint untouched."""
        self.assertTrue(emoji.is_emoji(countries.alt_for("Poland")))
        # The emoji-free-body invariant (strip_all_emoji) WOULD remove a flag
        # that leaked into AI content — hence flags are appended post-sanitize.
        self.assertEqual(parser.strip_all_emoji("NO POLAND 🇵🇱"), "NO POLAND")
        # sanitize_body_lines keeps the original line text verbatim…
        kept = parser.sanitize_body_lines(["NO POLAND 🇵🇱", "ANY EUROPE 🇪🇺 ..."])
        self.assertEqual(kept, ["NO POLAND 🇵🇱", "ANY EUROPE 🇪🇺 ..."])
        # …and flag_body_lines / build_ai_message attach the anchors post-sanitize
        # in the final render, so every published post carries the real glyphs.
        flagged, covered = countries.flag_body_lines(["NO POLAND"])
        line, flags = flagged[0]
        self.assertEqual(line, "NO POLAND  🇵🇱")
        self.assertEqual(flags, [("🇵🇱", countries.emoji_for("Poland"))])
        self.assertEqual(covered, {"Poland"})

    def test_country_flag_line_clean_format_two_spaces(self):
        """A flag line is exactly '<Country>  <flag>' — no visible anchor /
        placeholder leftovers (_, ·, zero-width) ever appear in the text."""
        banned = {"_", "·", "—", "\u200b", "\u200e", "\u200f"}
        # In-body flag attachment (single + bare-list split + prose + extras).
        lines, covered = countries.flag_body_lines(
            ["NO POLAND", "UK, USA and Germany", "ship to Spain and Italy"])
        for text, _flags in lines:
            self.assertFalse(any(ch in banned for ch in text),
                             f"leftover placeholder char in {text!r}")
        self.assertIn("NO POLAND  🇵🇱", [t for t, _ in lines])
        self.assertIn("UK  🇬🇧", [t for t, _ in lines])
        self.assertIn("USA  🇺🇸", [t for t, _ in lines])
        self.assertIn("Germany  🇩🇪", [t for t, _ in lines])
        # Extras path (from raw source text) uses the same two-space format.
        msg, entities = parser.build_ai_message(
            content_lines=["Available"],
            platform="netflix",
            contact_username="@b",
            intent="sell",
            source_text="Lithuania\nAustralia",
        )
        self.assertIn("Lithuania  🇱🇹", msg)
        self.assertIn("Australia  🇦🇺", msg)
        self.assertFalse(any(ch in banned for ch in msg))
        # Every flag entity must sit EXACTLY on its flag emoji, right after the
        # two separating spaces — no offset drift that would leave stray chars.
        for e in [e for e in entities if e.document_id in countries.COUNTRY_EMOJI.values()]:
            start = _utf16_to_char(msg, e.offset)
            self.assertEqual(msg[start - 2:start], "  ",
                             "flag entity must start right after exactly two spaces")

    def test_markdown_bold_stripped_from_body(self):
        """Literal '**'/'*' markdown leaks from sources must be stripped to plain
        text by the SHARED sanitizer — every render path runs this one pass; the
        published-post send uses formatting_entities WITHOUT parse_mode so
        Telethon never parses '**' as bold."""
        # The sanitizer
        self.assertEqual(
            parser.sanitize_body_lines(["**ESTY KYC**", "*single*", "***bold***",
                                        "mixed **bold** here", "this stays"]),
            ["ESTY KYC", "single", "bold", "mixed bold here", "this stays"],
        )
        # Through the full render (auto-publish / approve / preview all use this).
        out, _ = parser.build_ai_message(
            content_lines=["**ESTY KYC**", "Full access now"],
            platform="bybit",
            contact_username="@b",
            intent="sell",
        )
        self.assertIn("\nESTY KYC\n", out)
        self.assertNotIn("*", out)
        self.assertIn("\nFull access now\n", out)

    # -------------------------------------------------------------
    # DATABASE TESTS
    # -------------------------------------------------------------
    def test_db_suppliers(self):
        db.add_supplier("@supplier_test1", channel_id=-100111, db_path=TEST_DB)
        db.add_supplier("@supplier_test2", channel_id=-100222, db_path=TEST_DB)

        suppliers = db.list_suppliers(active_only=True, db_path=TEST_DB)
        self.assertGreaterEqual(len(suppliers), 2)

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
        db.add_supplier("kycgroupke", channel_id=-100123, db_path=db_path)
        # Same channel re-added by its numeric id -> single row, username preserved.
        db.add_supplier("-100123", channel_id=-100123, db_path=db_path)
        suppliers = db.list_suppliers(db_path=db_path)
        matches = [s for s in suppliers if s["channel_id"] == -100123]
        self.assertEqual(len(matches), 1, "channel must not be duplicated across rows")
        row = matches[0]
        self.assertEqual(row["channel_username"], "kycgroupke")
        self.assertNotIn("markup_multiplier", row, "pricing columns must not exist in v9")
        self.assertEqual(row["active"], 1)

    def test_db_add_supplier_upgrades_numeric_placeholder_to_username(self):
        """A supplier seeded with only a numeric id is upgraded to the real
        username when the channel is later added by username."""
        db_path = TEST_DB
        db.add_supplier("-100456", channel_id=-100456, db_path=db_path)
        db.add_supplier("@renamedchan", channel_id=-100456, db_path=db_path)
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
        keep = db.add_supplier("ownschan", channel_id=-100500, db_path=db_path)
        drop = db.add_supplier("stalechan", channel_id=-100501, db_path=db_path)
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
        keep = db.add_supplier("noidchan", channel_id=None, db_path=db_path)
        drop = db.add_supplier("resolvedchan", channel_id=-100502, db_path=db_path)
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
        result1 = db.ensure_env_seed(channels, db_path=path)
        self.assertEqual(result1["state"], "seeded")
        self.assertEqual(result1["seeded"], 2)
        self.assertTrue(db.env_seed_completed(db_path=path))
        self.assertEqual(db.count_suppliers(db_path=path), 2)

        row = db.get_supplier_by_chat(username="chanone", db_path=path)
        db.delete_supplier(row["id"], db_path=path)

        result2 = db.ensure_env_seed(channels, db_path=path)
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

        result = db.ensure_env_seed(["@wouldhavebeen", "-100999"], db_path=path)
        self.assertEqual(result["state"], "migrated")
        self.assertTrue(result["migrated"])
        self.assertEqual(result["seeded"], 0)
        after = [dict(r) for r in db.list_suppliers(db_path=path)]
        self.assertEqual(before, after, "migration must not modify any supplier row")
        self.assertTrue(db.env_seed_completed(db_path=path))

        again = db.ensure_env_seed(["@alsoignored"], db_path=path)
        self.assertEqual(again["state"], "already_seeded")
        self.assertEqual(db.count_suppliers(db_path=path), 2)
        db.clear_env_seed_completed(db_path=path)

    def test_db_env_seed_reseed_escape_hatch(self):
        """/reseed_from_env path: the handler clears the marker, runs the seed sync
        DIRECTLY (bypassing the empty-table migration guard), and re-sets the marker.
        Existing suppliers are preserved, new ones are added, and restarts keep
        ignoring .env afterwards."""
        path = self._fresh_db("env_seed_reseed_test.db")
        db.ensure_env_seed(["@a", "@b"], db_path=path)
        self.assertEqual(db.count_suppliers(db_path=path), 2)
        self.assertTrue(db.env_seed_completed(db_path=path))

        db.clear_env_seed_completed(db_path=path)
        n = db.seed_suppliers_from_env(["@a", "@b", "@c"], db_path=path)
        db.mark_env_seed_completed(db_path=path)
        self.assertEqual(n, 3)
        self.assertEqual(db.count_suppliers(db_path=path), 3)
        self.assertIsNotNone(db.get_supplier_by_chat(username="b", db_path=path))
        self.assertIsNotNone(db.get_supplier_by_chat(username="c", db_path=path))
        self.assertTrue(db.env_seed_completed(db_path=path))

        again = db.ensure_env_seed(["@zzz"], db_path=path)
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
        result = db.ensure_env_seed([], db_path=path)
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

        old_admin_id = main_mod.ADMIN_USER_ID
        main_mod.ADMIN_USER_ID = 5883701139
        try:
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
        finally:
            main_mod.ADMIN_USER_ID = old_admin_id

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
        }
        prompt = admin_bot._edit_prompt(listing, None)
        self.assertIn("Current content", prompt)
        self.assertIn("> KYC CURVE PAY", prompt)
        self.assertIn("> ANY EU", prompt)
        self.assertNotIn("$", prompt)
        self.assertNotIn("12.5", prompt)
        self.assertIn("send back the FULL body", prompt)

    def test_edit_prompt_prior_draft_wins_and_no_price_note(self):
        """Re-entering Edit keeps the last draft as context, not the DB text."""
        import admin_bot

        listing = {
            "id": 7,
            "clean_text": "OLD LINE FROM AI",
        }
        prompt = admin_bot._edit_prompt(listing, "MY EDITED LINE\nCHANGED")
        self.assertIn("> MY EDITED LINE", prompt)
        self.assertIn("> CHANGED", prompt)
        self.assertNotIn("OLD LINE FROM AI", prompt)
        self.assertNotIn("$", prompt)
        self.assertNotIn("not set yet", prompt)

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
                "INSERT INTO suppliers (channel_username, channel_id, active, added_at) "
                "VALUES (?, ?, ?, ?)",
                ("old_bare_chan", bare_cha, 1, "2025-01-01T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO suppliers (channel_username, channel_id, active, added_at) "
                "VALUES (?, ?, ?, ?)",
                ("dup_via_bare", bare_chb, 1, "2025-01-01T00:00:00+00:00"),
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
            supplier = asyncio.run(main_mod.resolve_supplier_for_event(_FakeEvent()))
            self.assertIsNotNone(supplier, "resolve_supplier_for_event must match on marked chat_id")
            self.assertEqual(supplier["channel_id"], marked)
            self.assertTrue(supplier.get("active"))
        finally:
            db.DEFAULT_DB_PATH = old_default

    def test_db_display_name_roundtrip(self):
        """set_supplier_display_name stores a friendly label keyed by username OR id."""
        db_path = TEST_DB
        db.add_supplier("-100789", channel_id=-100789, db_path=db_path)
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
        db.add_supplier("@dedup_supplier", channel_id=-100333, db_path=TEST_DB)
        sup = db.get_supplier_by_chat(username="dedup_supplier", db_path=TEST_DB)
        self.assertIsNotNone(sup)
        sup_id = sup["id"]

        listing_id = db.insert_listing(
            supplier_id=sup_id,
            source_message_id=9991,
            game_name="bybit",
            rank_tier=None,
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
        db.add_supplier("@unpub_supplier", channel_id=-100557, db_path=TEST_DB)
        sup = db.get_supplier_by_chat(username="unpub_supplier", db_path=TEST_DB)
        pend_id = db.insert_listing(
            supplier_id=sup["id"], source_message_id=9951, game_name=None, rank_tier=None,
            status="pending_review", raw_text="WTS Revolut account $100",
            clean_text="WTS Revolut account $100", db_path=TEST_DB,
        )
        recv_id = db.insert_listing(
            supplier_id=sup["id"], source_message_id=9952, game_name=None, rank_tier=None,
            status="received", raw_text="WTS Revolut account $100",
            clean_text="WTS Revolut account $100", db_path=TEST_DB,
        )
        db.insert_listing(
            supplier_id=sup["id"], source_message_id=9953, game_name=None, rank_tier=None,
            status="published", raw_text="WTS Revolut account $100",
            clean_text="WTS Revolut account $100", published_message_id=5002, db_path=TEST_DB,
        )
        db.insert_listing(
            supplier_id=sup["id"], source_message_id=9954, game_name=None, rank_tier=None,
            status="failed", raw_text="WTS Revolut account $100",
            clean_text="WTS Revolut account $100", db_path=TEST_DB,
        )
        unpub = db.get_unpublished_listings(db_path=TEST_DB)
        unpub_ids = {l["id"] for l in unpub}
        self.assertIn(pend_id, unpub_ids)
        self.assertIn(recv_id, unpub_ids)
        self.assertTrue(all(l["status"] in ("received", "pending_approval", "pending_review") for l in unpub))

    def test_db_failed_queue_and_requeue(self):
        db.add_supplier("@fail_supplier", channel_id=-100444, db_path=TEST_DB)
        sup = db.get_supplier_by_chat(username="fail_supplier", db_path=TEST_DB)
        listing_id = db.insert_listing(
            supplier_id=sup["id"],
            source_message_id=9940,
            game_name=None,
            rank_tier=None,
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

    def test_concurrent_duplicate_twin_cannot_both_publish(self):
        db.add_supplier("@twin_src", channel_id=-100401, db_path=TEST_DB)
        sup_id = db.get_supplier_by_chat(username="twin_src", db_path=TEST_DB)["id"]
        text = "WTS Bybit verified account $100"
        fp = db.make_listing_fingerprint(text, price=100.0)
        first_id = db.insert_listing(
            supplier_id=sup_id, source_message_id=60001,
            game_name="bybit", rank_tier=None,
            status="received",
            raw_text=text, clean_text=text, fingerprint=fp, db_path=TEST_DB,
        )
        twin_id = db.insert_listing(
            supplier_id=sup_id, source_message_id=60002,
            game_name="bybit", rank_tier=None,
            status="received",
            raw_text=text, clean_text=text, fingerprint=fp, db_path=TEST_DB,
        )
        block = db.find_recent_similar_listing(
            text, price=100.0, exclude_listing_id=twin_id, db_path=TEST_DB
        )
        self.assertIsNotNone(
            block,
            "concurrent twin must be blocked by the in-flight row (CONC-2)",
        )
        self.assertEqual(block["id"], first_id)

    def test_duplicate_check_never_matches_self(self):
        db.add_supplier("@self_src", channel_id=-100402, db_path=TEST_DB)
        sup_id = db.get_supplier_by_chat(username="self_src", db_path=TEST_DB)["id"]
        text = "WTS Wise personal $200"
        fp = db.make_listing_fingerprint(text, price=200.0)
        listing_id = db.insert_listing(
            supplier_id=sup_id, source_message_id=60003,
            game_name="wise", rank_tier=None,
            status="pending_approval",
            raw_text=text, clean_text=text, fingerprint=fp, db_path=TEST_DB,
        )
        match = db.find_recent_similar_listing(
            text, price=200.0, exclude_listing_id=listing_id, db_path=TEST_DB
        )
        self.assertIsNone(match, "a listing must never count as its own duplicate")

    def test_stale_received_row_does_not_blind_dedup(self):
        db.add_supplier("@stale_src", channel_id=-100403, db_path=TEST_DB)
        sup_id = db.get_supplier_by_chat(username="stale_src", db_path=TEST_DB)["id"]
        text = "WTS Revolut premium $150"
        fp = db.make_listing_fingerprint(text, price=150.0)
        stale_id = db.insert_listing(
            supplier_id=sup_id, source_message_id=60004,
            game_name=None, rank_tier=None,
            status="received",
            raw_text=text, clean_text=text, fingerprint=fp, db_path=TEST_DB,
        )
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with sqlite3.connect(TEST_DB) as conn:
            conn.execute(
                "UPDATE listings SET created_at = ? WHERE id = ?", (old_ts, stale_id)
            )
        match = db.find_recent_similar_listing(text, price=150.0, db_path=TEST_DB)
        self.assertIsNone(
            match, "stale received row must not blind the dedup window"
        )
        fresh_id = db.insert_listing(
            supplier_id=sup_id, source_message_id=60005,
            game_name=None, rank_tier=None,
            status="received",
            raw_text=text, clean_text=text, fingerprint=fp, db_path=TEST_DB,
        )
        twin_id = db.insert_listing(
            supplier_id=sup_id, source_message_id=60006,
            game_name=None, rank_tier=None,
            status="received",
            raw_text=text, clean_text=text, fingerprint=fp, db_path=TEST_DB,
        )
        block = db.find_recent_similar_listing(
            text, price=150.0, exclude_listing_id=twin_id, db_path=TEST_DB
        )
        self.assertIsNotNone(block)
        self.assertEqual(block["id"], fresh_id)

    def test_processing_lock_released_after_processing(self):
        """REG (CONC-3): per-message in-flight locks serialize identical
        concurrent messages and are removed from the lock dict once processing
        finishes — no dead entries leak."""
        import asyncio
        import main as main_mod

        main_mod._processing_locks.clear()
        runs = []
        gate = asyncio.Event()

        async def worker(name, wait_for_twin):
            if wait_for_twin:
                await gate.wait()
            entry = main_mod._acquire_processing_lock(42, 800001)
            try:
                async with entry.lock:
                    runs.append(name)
                    if not wait_for_twin:
                        gate.set()
                        await asyncio.sleep(0.02)
            finally:
                main_mod._release_processing_lock(42, 800001, entry)

        async def scenario():
            await asyncio.gather(
                worker("A", wait_for_twin=False),
                worker("B", wait_for_twin=True),
            )

        asyncio.run(scenario())
        self.assertEqual(runs, ["A", "B"], "twin messages must be serialized")
        self.assertEqual(
            main_mod._processing_locks, {}, "lock entries must not leak"
        )

    def test_db_platform_fields_persisted(self):
        db.add_supplier("@fields_supplier", channel_id=-100555, db_path=TEST_DB)
        sup = db.get_supplier_by_chat(username="fields_supplier", db_path=TEST_DB)
        listing_id = db.insert_listing(
            supplier_id=sup["id"],
            source_message_id=9950,
            game_name=None,
            rank_tier=None,
            status="received",
            raw_text="WTS Wise personal $200",
            clean_text="WTS Wise personal $200",
            db_path=TEST_DB,
        )
        listing = db.get_listing_by_id(listing_id, db_path=TEST_DB)
        self.assertIsNone(listing["platform_name"])
        self.assertNotIn("original_price", listing, "pricing columns must not exist in v9")

        db.update_listing_fields(
            listing_id, platform_name="wise",
            db_path=TEST_DB,
        )
        updated = db.get_listing_by_id(listing_id, db_path=TEST_DB)
        self.assertEqual(updated["platform_name"], "wise")
        self.assertNotIn("our_price", updated, "pricing columns must not exist in v9")

    def test_db_stats_and_pending(self):
        # A dedicated supplier so the FK insert is always valid (PRAGMA
        # foreign_keys is now ON).
        sid = db.add_supplier("@stats_src", channel_id=-100666, db_path=TEST_DB)
        # Insert a pending listing
        pending_id = db.insert_listing(
            supplier_id=sid,
            source_message_id=9992,
            game_name=None,
            rank_tier=None,
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
        self.assertGreaterEqual(version, 9)

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
            self.assertGreaterEqual(version, 9)
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

    def test_buy_auto_publish_gate_respects_pause(self):
        """'All Stop' must gate the buy-intent auto-publish; manual Approve is separate."""
        import main as main_mod
        # Unpaused + full conditions -> auto-publish.
        self.assertTrue(main_mod.buy_auto_publish_ok(
            intent="buy", paused=False, body_ok=True, has_payment_proof=None,
        ))
        # Paused -> NEVER auto-publishes; routes to manual approval instead.
        self.assertFalse(main_mod.buy_auto_publish_ok(
            intent="buy", paused=True, body_ok=True, has_payment_proof=None,
        ))
        # Unpaused, the other gates still route to manual review.
        self.assertFalse(main_mod.buy_auto_publish_ok(
            intent="buy", paused=False, body_ok=False, has_payment_proof=None,
        ))
        self.assertFalse(main_mod.buy_auto_publish_ok(
            intent="buy", paused=False, body_ok=True, has_payment_proof="paid $40 receipt",
        ))
        self.assertFalse(main_mod.buy_auto_publish_ok(
            intent="sell", paused=False, body_ok=True, has_payment_proof=None,
        ))

    def test_deterministic_fallback_gate_respects_pause(self):
        """'All Stop' must gate the deterministic AI-down fallback auto-publish."""
        import main as main_mod
        old = main_mod.DETERMINISTIC_FALLBACK
        main_mod.DETERMINISTIC_FALLBACK = True
        try:
            self.assertTrue(main_mod.deterministic_fallback_publish_ok(
                paused=False, risky_keyword=None, has_payment_proof=None,
                has_clear_signal=True, body_ok=True,
            ))
            # Paused -> fallback listing routes to manual review, never auto-publishes.
            self.assertFalse(main_mod.deterministic_fallback_publish_ok(
                paused=True, risky_keyword=None, has_payment_proof=None,
                has_clear_signal=True, body_ok=True,
            ))
            # Unpaused, the safety valves still route to manual review.
            self.assertFalse(main_mod.deterministic_fallback_publish_ok(
                paused=False, risky_keyword="hacked", has_payment_proof=None,
                has_clear_signal=True, body_ok=True,
            ))
            self.assertFalse(main_mod.deterministic_fallback_publish_ok(
                paused=False, risky_keyword=None, has_payment_proof="receipt shown",
                has_clear_signal=True, body_ok=True,
            ))
            self.assertFalse(main_mod.deterministic_fallback_publish_ok(
                paused=False, risky_keyword=None, has_payment_proof=None,
                has_clear_signal=False, body_ok=True,
            ))
        finally:
            main_mod.DETERMINISTIC_FALLBACK = old

    def test_approved_listings_drain_ignores_pause(self):
        """Approved-but-unpublished listings drain even while 'All Stop' is on."""
        db.set_paused(True, db_path=TEST_DB)
        try:
            sup_id = db.add_supplier("@pause_drain", channel_id=-100777, db_path=TEST_DB)
            listing_id = db.insert_listing(
                supplier_id=sup_id, source_message_id=9960, game_name=None, rank_tier=None,
                status="approved",
                raw_text="WTS Netflix $50", clean_text="WTS Netflix $50", db_path=TEST_DB,
            )
            # The drain query feeds the worker: paused or not, approved rows surface.
            approved = db.get_approved_listings_to_publish(limit=10, db_path=TEST_DB)
            self.assertTrue(any(l["id"] == listing_id for l in approved))
            # The queue-drain worker itself must not skip on the pause switch.
            import inspect
            import main as main_mod
            worker_src = inspect.getsource(main_mod.approved_listings_worker)
            self.assertNotIn("is_paused", worker_src)
        finally:
            db.set_paused(False, db_path=TEST_DB)

    def test_approve_callback_never_blocked_by_pause(self):
        """Manual Approve must never be refused by the pause switch."""
        import inspect
        import admin_bot
        handlers = inspect.getsource(admin_bot.setup_admin_handlers)
        # The old paused-refusal ("resume first, then approve again") is gone.
        self.assertNotIn("to resume, then approve again", handlers)
        # And the approve flow still publishes immediately when the status allows.
        self.assertIn("Processing...", handlers)

    def test_pause_help_text_clarifies_manual_approve_unaffected(self):
        """Help and pause messages must say pausing only affects automatic publishing."""
        import admin_bot
        help_text = admin_bot._help_text()
        self.assertIn("AUTOMATIC publishing", help_text)
        self.assertIn("Manual Approve taps still publish immediately", help_text)

    # -------------------------------------------------------------
    # "I'm ASLEEP" TOGGLE + FOOTER (UX item 4)
    # -------------------------------------------------------------
    def test_asleep_toggle(self):
        """The asleep switch round-trips through app_settings like the pause switch."""
        db.set_buyer_asleep(False, db_path=TEST_DB)
        self.assertFalse(db.is_buyer_asleep(db_path=TEST_DB))
        db.set_buyer_asleep(True, db_path=TEST_DB)
        self.assertTrue(db.is_buyer_asleep(db_path=TEST_DB))
        db.set_buyer_asleep(False, db_path=TEST_DB)
        self.assertFalse(db.is_buyer_asleep(db_path=TEST_DB))

    def test_buyer_asleep_footer_resolution(self):
        """Footer resolution: app_settings > BUYER_ASLEEP_FOOTER env > default."""
        old_env = os.environ.get("BUYER_ASLEEP_FOOTER")
        try:
            self.assertEqual(
                db.get_buyer_asleep_footer(db_path=TEST_DB),
                "Buyer away, back shortly",
            )
            db.set_setting("buyer_asleep_footer", "Back now", db_path=TEST_DB)
            self.assertEqual(db.get_buyer_asleep_footer(db_path=TEST_DB), "Back now")
            db.set_setting("buyer_asleep_footer", "", db_path=TEST_DB)

            os.environ["BUYER_ASLEEP_FOOTER"] = "Will reply later"
            self.assertEqual(db.get_buyer_asleep_footer(db_path=TEST_DB), "Will reply later")
            os.environ["BUYER_ASLEEP_FOOTER"] = "   "
            self.assertEqual(
                db.get_buyer_asleep_footer(db_path=TEST_DB),
                "Buyer away, back shortly",
            )
        finally:
            if old_env is None:
                os.environ.pop("BUYER_ASLEEP_FOOTER", None)
            else:
                os.environ["BUYER_ASLEEP_FOOTER"] = old_env
            db.set_setting("buyer_asleep_footer", "", db_path=TEST_DB)

    def test_build_ai_message_appends_asleep_footer(self):
        """While the toggle is ON the footer is appended to EVERY render path;
        toggled OFF it never appears. Uses a fresh temp DB so the default
        monitor.db is never touched by the render."""
        path = self._fresh_db("asleep_footer_test.db")
        old_default = db.DEFAULT_DB_PATH
        try:
            db.DEFAULT_DB_PATH = path
            db.set_buyer_asleep(False, db_path=path)
            msg_off, _ = parser.build_ai_message(
                content_lines=["Bybit full kyc"],
                platform="bybit",
                contact_username="@buyer",
                intent="buy",
            )
            self.assertNotIn("Buyer away, back shortly", msg_off)

            db.set_buyer_asleep(True, db_path=path)
            db.set_setting("buyer_asleep_footer", "Back soon", db_path=path)
            msg_on, entities = parser.build_ai_message(
                content_lines=["Bybit full kyc"],
                platform="bybit",
                contact_username="@buyer",
                intent="buy",
            )
            self.assertIn("Back soon", msg_on)
            self.assertTrue(msg_on.rstrip().endswith("Back soon"), msg_on)
            for e in entities:  # footer is plain text; entities still land in-bounds
                self.assertLess(e.offset, parser._utf16_len(msg_on))
        finally:
            db.DEFAULT_DB_PATH = old_default

    def test_asleep_label_helper(self):
        """The home button label mirrors the current awake/asleep state."""
        import admin_bot  # loads .env (load_dotenv) — imported lazily so the
        # no-key AI tests above run against a clean environment first.

        old_default = db.DEFAULT_DB_PATH
        try:
            db.DEFAULT_DB_PATH = TEST_DB
            db.set_buyer_asleep(False, db_path=TEST_DB)
            self.assertEqual(admin_bot._asleep_label(), "💤 I'm Asleep")
            db.set_buyer_asleep(True, db_path=TEST_DB)
            self.assertEqual(admin_bot._asleep_label(), "☀️ I'm Awake")
            db.set_buyer_asleep(False, db_path=TEST_DB)
        finally:
            db.DEFAULT_DB_PATH = old_default

    # -------------------------------------------------------------
    # UX ITEM 2/5/1 — destination + published digest + skip cards
    # -------------------------------------------------------------
    def test_destination_url_helper(self):
        """_destination_url links to the republished post, and None until a
        published_message_id exists / DEST_CHANNEL is set."""
        import admin_bot

        old = admin_bot.DEST_CHANNEL
        try:
            admin_bot.DEST_CHANNEL = "@mychannel"
            self.assertEqual(
                admin_bot._destination_url({"published_message_id": 77}),
                "https://t.me/mychannel/77",
            )
            self.assertIsNone(admin_bot._destination_url({"post_number": 3}))
            admin_bot.DEST_CHANNEL = "-1001234567890"
            self.assertEqual(
                admin_bot._destination_url({"published_message_id": 77}),
                "https://t.me/c/1234567890/77",
            )
            admin_bot.DEST_CHANNEL = ""
            self.assertIsNone(admin_bot._destination_url({"published_message_id": 77}))
        finally:
            admin_bot.DEST_CHANNEL = old

    def test_skip_notification_render(self):
        """One skip renders as a send_published_alert-style card with a
        Re-review button and (when resolvable) a source-channel link."""
        import admin_bot

        text, buttons = admin_bot._skip_notification({
            "skip_id": 42,
            "channel_username": "kycgroupke",
            "message_id": 9001,
            "reason": "duplicate",
            "raw_text": "WTS Bybit account $100",
        })
        self.assertIn("⏳ **Skipped — duplicate**", text)
        self.assertIn("━━━━━━━━━━━━━━━━━━━━", text)
        self.assertIn("Supplier : @kycgroupke", text)
        self.assertEqual(len(buttons), 1, "one row of buttons")
        row = buttons[0]
        self.assertTrue(any(getattr(b, "text", "") == "🔁 Re-review" for b in row))
        urls = [getattr(b, "url", None) for b in row]
        self.assertIn("https://t.me/kycgroupke/9001", urls)

    def test_published_digest_buttons_only(self):
        """/published is buttons-only: no per-post text lines; each post is one
        row of two URL buttons (#N -> our channel, source name -> source) with
        no emoji in the labels."""
        import admin_bot

        rows = [
            {
                "id": 1, "source_message_id": 100, "published_message_id": 200,
                "post_number": 7, "platform_name": "bybit",
                "supplier_username": "kycgroupke", "supplier_display_name": None,
            },
            {
                "id": 2, "source_message_id": 101, "published_message_id": None,
                "post_number": None, "platform_name": None,
                "supplier_username": "anon_src", "supplier_display_name": None,
            },
        ]
        old = admin_bot.DEST_CHANNEL
        try:
            admin_bot.DEST_CHANNEL = "@mychannel"
            text, buttons = admin_bot._published_digest(rows)
        finally:
            admin_bot.DEST_CHANNEL = old
        self.assertNotIn("·", text)
        self.assertEqual(text.count("\n"), 1, "header line only")
        self.assertEqual(len(buttons), 4, "two post rows + search button + home row")

        row1 = buttons[0]
        self.assertTrue(any(getattr(b, "text", None) == "#7" for b in row1))
        urls1 = [getattr(b, "url", None) for b in row1]
        self.assertIn("https://t.me/mychannel/200", urls1)
        self.assertIn("https://t.me/kycgroupke/100", urls1)

        row2 = buttons[1]
        self.assertEqual(len(row2), 1, "no dest link without published_message_id")
        self.assertEqual(
            getattr(row2[0], "url", None), "https://t.me/anon_src/101"
        )

        search_row = buttons[2]
        self.assertTrue(
            any(getattr(b, "data", b"").decode() == "published:search" for b in search_row),
            "published list offers a post-number search button",
        )

        for row_btn in buttons:
            for b in row_btn:
                label = getattr(b, "text", "")
                if label.startswith("🏠"):
                    continue  # home row is exempt
                first = label[0]
                self.assertEqual(first, first.strip() and first, f"emoji/space-led label {label!r}")

    def test_relative_time_helper(self):
        """_relative_time formats an ISO timestamp as a compact age."""
        import admin_bot
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        self.assertEqual(admin_bot._relative_time(""), "")
        self.assertEqual(admin_bot._relative_time("not-a-date"), "")
        self.assertEqual(admin_bot._relative_time((now).isoformat()), "just now")
        self.assertEqual(admin_bot._relative_time((now).replace(tzinfo=None).isoformat()), "just now")
        self.assertEqual(admin_bot._relative_time((now - timedelta(minutes=2)).isoformat()), "2m ago")
        self.assertEqual(admin_bot._relative_time((now - timedelta(hours=5)).isoformat()), "5h ago")
        self.assertEqual(admin_bot._relative_time((now - timedelta(days=3)).isoformat()), "3d ago")
        self.assertEqual(admin_bot._relative_time((now - timedelta(days=10)).isoformat()), "1w ago")
        # Future/clock-skew timestamps clamp to "just now" instead of negative ages.
        self.assertEqual(admin_bot._relative_time((now + timedelta(hours=1)).isoformat()), "just now")

    def test_sources_menu_text_minimal(self):
        """The Sources screen is buttons-first: no emoji header, no bold
        per-supplier lines, no numeric IDs in the body."""
        import admin_bot

        text = admin_bot._sources_menu_text([
            {"channel_username": "kycgroupke", "display_name": None, "channel_id": -100123, "active": True},
            {"channel_username": "no_id_ref", "display_name": "Unresolved Chan", "channel_id": None, "active": True},
        ])
        self.assertNotIn("📋", text)
        self.assertNotIn("Monitored Sources", text)
        self.assertNotIn("**", text)  # no bold per-supplier lines
        self.assertNotIn("ID:", text)
        self.assertNotIn("kycgroupke", text)
        self.assertNotIn("-100123", text)
        self.assertIn("Tap a source", text)

        empty = admin_bot._sources_menu_text([])
        self.assertIn("No sources configured yet", empty)
        self.assertNotIn("📋", empty)

    def test_sources_buttons_labels(self):
        """Source buttons carry icon + name (the info that used to be text);
        Add Source / Back rows are preserved."""
        import admin_bot

        suppliers = [
            {"id": 1, "channel_username": "kycgroupke", "display_name": None, "channel_id": -100123, "active": True},
            {"id": 2, "channel_username": "no_id_ref", "display_name": "Unresolved Chan", "channel_id": None, "active": True},
            {"id": 3, "channel_username": "-1004567890", "display_name": None, "channel_id": -1004567890, "active": False},
        ]
        buttons = admin_bot._sources_buttons(suppliers)
        self.assertEqual(len(buttons), len(suppliers) + 1, "one row per supplier + Add/Back row")
        self.assertIn("kycgroupke", buttons[0][0].text)
        self.assertIn("@kycgroupke", buttons[0][0].text)
        self.assertIn("Unresolved Chan", buttons[1][0].text)
        self.assertIn("-1004567890", buttons[2][0].text)  # numeric-id supplier label IS the id
        self.assertIn("➕ Add Source", buttons[3][0].text)
        self.assertIn("⬅️ Back", buttons[3][1].text)

    def test_skipped_digest_buttons_only(self):
        """/skipped is buttons-first: short caption header, no numbered/snippet
        wall; each Re-review button label carries reason -- supplier -- age;
        non-reopenable skips drop to a one-line count."""
        import admin_bot
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        skips = [
            {"skip_id": 42, "listing_id": 7, "reason": "duplicate",
             "channel_username": "kycgroupke", "display_name": None,
             "timestamp": (now - timedelta(hours=2)).isoformat(),
             "raw_text": "WTS Bybit account $100"},
            {"skip_id": 41, "listing_id": 6, "reason": "no_content",
             "channel_username": "src_b", "display_name": None,
             "timestamp": (now - timedelta(minutes=5)).isoformat(),
             "raw_text": "hello group"},
            {"skip_id": 40, "listing_id": None, "reason": "chatter",  # not reopenable
             "channel_username": "src_c", "display_name": None,
             "timestamp": (now - timedelta(hours=1)).isoformat(),
             "raw_text": "wassup"},
        ]
        text, buttons = admin_bot._skipped_digest(skips)

        # Short caption header: no redundant "tap one to re-review", no
        # numbering, no snippet, no "Recently skipped".
        self.assertTrue(
            text.strip().startswith("🚫 **Skipped posts** — tap a button to open a post:"),
            text,
        )
        self.assertNotIn("tap one to re-review", text)
        self.assertNotIn("1.", text)
        self.assertNotIn("WTS Bybit", text)
        self.assertNotIn("hello group", text)
        self.assertNotIn("Recently skipped", text)

        # Buttons carry reason -- supplier -- age (no reason emojis), one
        # button per row, and only for reopenable skips.
        self.assertEqual(len(buttons), 3, "two skip rows + home row")
        labels = [b[0].text for b in buttons[:2]]
        self.assertIn("duplicate -- @kycgroupke -- 2h ago", labels)
        self.assertIn("no content -- @src_b -- 5m ago", labels)
        self.assertFalse(any(c in "".join(labels) for c in "🔁⬜💬🗨️🔄📄"),
                         "no reason emojis in skipped-list labels")
        self.assertFalse(any("chatter" in l for l in labels), "non-reopenable skip has no button")
        self.assertEqual(len(buttons[:2][0]), 1, "one button per skip row")
        self.assertEqual(len(buttons[:2][1]), 1)

        # Dropped skip is surfaced as a one-line count, not a dead screen entry.
        self.assertIn("1 more recent skip(s) not re-reviewable", text)

        home = buttons[-1][0].text
        self.assertTrue(home.startswith("🏠"))

    def test_skipped_digest_buttons_only_none_reopenable(self):
        """When no recent skip can be reopened, say so instead of showing an
        empty tap-to-review hint."""
        import admin_bot

        text, buttons = admin_bot._skipped_digest([
            {"skip_id": 9, "listing_id": None, "reason": "chatter",
             "channel_username": "src_c", "display_name": None,
             "timestamp": None, "raw_text": "x"},
        ])
        self.assertIn("🚫 **Skipped posts**", text)
        self.assertIn("None of the recent skips can be re-opened", text)
        self.assertEqual(len(buttons), 1, "just the home row")
        self.assertTrue(buttons[0][0].text.startswith("🏠"))

    def test_get_published_listings_joins_supplier(self):
        sid = db.add_supplier("trace_src", channel_id=-100777, db_path=TEST_DB)
        db.insert_listing(sid, 9001, None, None, "pending_approval",
                          "t", "t", db_path=TEST_DB)
        pid = db.insert_listing(sid, 9002, None, None, "published",
                                "t", "t", published_message_id=50, db_path=TEST_DB)
        pid2 = db.insert_listing(sid, 9003, None, None, "published",
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

        sid = db.add_supplier("@pn_src", channel_id=-100889, db_path=TEST_DB)
        listing_id = db.insert_listing(sid, 9100, None, None, "approved",
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
            db.insert_listing(sid, 1, None, None, "published",
                              "a", "a", published_message_id=10, db_path=path)
            db.insert_listing(sid, 2, None, None, "published",
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
            platform="netflix",
            post_number=42,
        )
        self.assertIn("#42\n", out)
        msg2, _ = parser.build_ai_message(
            content_lines=["Netflix 1 month"],
            platform="netflix",
        )
        self.assertNotIn("#42", msg2)

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

    # -------------------------------------------------------------
    # AI REPHRASER TESTS
    # -------------------------------------------------------------
    def test_ai_rephraser_init_no_key_returns_false(self):
        """init_groq should fail gracefully without a key."""
        env_key = os.environ.pop("GROQ_API_KEY", None)
        old_client, old_key = ai_rephraser._client, ai_rephraser._api_key
        ai_rephraser._client = None
        ai_rephraser._api_key = None
        try:
            result = ai_rephraser.init_groq("")
            self.assertFalse(result)
            self.assertFalse(ai_rephraser.is_available())
        finally:
            ai_rephraser._client, ai_rephraser._api_key = old_client, old_key
            if env_key is not None:
                os.environ["GROQ_API_KEY"] = env_key

    def test_ai_analyze_none_when_unavailable(self):
        """analyze_message should return None (triggering fallback) without a client."""
        env_key = os.environ.pop("GROQ_API_KEY", None)
        old_client, old_key = ai_rephraser._client, ai_rephraser._api_key
        ai_rephraser._client = None
        ai_rephraser._api_key = None
        try:
            ai_rephraser.init_groq("")
            import asyncio
            result = asyncio.run(ai_rephraser.analyze_message(
                "KYC Ikualo + Tuyo ID Card + Proof Address Serious Seller! Spain PRICE 50$"
            ))
            self.assertIsNone(result)
        finally:
            ai_rephraser._client, ai_rephraser._api_key = old_client, old_key
            if env_key is not None:
                os.environ["GROQ_API_KEY"] = env_key

    def test_ai_analyze_none_on_empty(self):
        """analyze_message should return None for empty text."""
        env_key = os.environ.pop("GROQ_API_KEY", None)
        old_client, old_key = ai_rephraser._client, ai_rephraser._api_key
        ai_rephraser._client = None
        ai_rephraser._api_key = None
        try:
            ai_rephraser.init_groq("")
            import asyncio
            result = asyncio.run(ai_rephraser.analyze_message("   "))
            self.assertIsNone(result)
        finally:
            ai_rephraser._client, ai_rephraser._api_key = old_client, old_key
            if env_key is not None:
                os.environ["GROQ_API_KEY"] = env_key

    def test_rephrase_unpublished_skips_when_ai_unavailable(self):
        """The stale-body rephrase sweep is a safe no-op without a Groq client."""
        import asyncio
        import main as main_mod
        # init_groq("") falls back to the .env key once main.py has loaded it,
        # so force the client off explicitly to keep this test hermetic.
        old_client, old_key = ai_rephraser._client, ai_rephraser._api_key
        ai_rephraser._client = None
        ai_rephraser._api_key = None
        try:
            result = asyncio.run(main_mod.rephrase_unpublished())
            self.assertIsNone(result)
        finally:
            ai_rephraser._client, ai_rephraser._api_key = old_client, old_key

    def test_unpublished_sweep_bounded_by_limit_and_age(self):
        """REG (REPHR-1): the startup rephrase sweep must be bounded (per-run
        limit) and skip listings that are too fresh to race the live pipeline."""
        db.add_supplier("@rephr_src", channel_id=-100995, db_path=TEST_DB)
        sup_id = db.get_supplier_by_chat(username="rephr_src", db_path=TEST_DB)["id"]
        fresh_id = db.insert_listing(
            supplier_id=sup_id, source_message_id=50001, game_name=None,
            rank_tier=None,
            status="pending_review", raw_text="WTS Chime account",
            clean_text="WTS Chime account", db_path=TEST_DB,
        )
        old_id = db.insert_listing(
            supplier_id=sup_id, source_message_id=50002, game_name=None,
            rank_tier=None,
            status="pending_review", raw_text="WTS Wise account",
            clean_text="WTS Wise account", db_path=TEST_DB,
        )
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        with sqlite3.connect(TEST_DB) as conn:
            conn.execute(
                "UPDATE listings SET created_at = ? WHERE id = ?", (old_ts, old_id)
            )
        aged = [l["id"] for l in db.get_unpublished_listings(
            min_age_seconds=300, db_path=TEST_DB
        ) if l["supplier_id"] == sup_id]
        self.assertIn(old_id, aged)
        self.assertNotIn(fresh_id, aged)
        limited = db.get_unpublished_listings(
            min_age_seconds=300, limit=1, db_path=TEST_DB
        )
        self.assertLessEqual(len(limited), 1)

    def test_floodwait_retry_shared_helper(self):
        """REG (TEL-1): the single FloodWait retry helper honors floods, retries
        until the budget is spent, and bubbles the last FloodWaitError at the
        cap so interactive admin actions never hang the bot loop forever."""

        def _flood(seconds):
            err = FloodWaitError.__new__(FloodWaitError)
            err.seconds = seconds
            return err

        calls = {"n": 0}

        async def flaky_then_success():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise _flood(3)
            return 42

        with mock.patch.object(publish_guard.asyncio, "sleep", new_callable=lambda: mock.AsyncMock()):
            result = publish_guard.asyncio.run(
                publish_guard.run_with_floodwait_retry(flaky_then_success, "test")
            )
        self.assertEqual(result, 42)
        self.assertEqual(calls["n"], 3)

        calls["n"] = 0

        async def always_floods():
            calls["n"] += 1
            raise _flood(3)

        with mock.patch.object(publish_guard.asyncio, "sleep", new_callable=lambda: mock.AsyncMock()):
            with self.assertRaises(FloodWaitError):
                publish_guard.asyncio.run(
                    publish_guard.run_with_floodwait_retry(
                        always_floods, "test", max_total_sleep=5.0
                    )
                )
        self.assertEqual(calls["n"], 2)

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

    def test_ai_prompt_delimiters_untrusted_message(self):
        """INJECT-1: the supplier text is wrapped in a <supplier_message> block
        and the model is told it is untrusted data, never instructions."""
        self.assertIn("<supplier_message>", ai_rephraser.ANALYZE_PROMPT)
        self.assertIn("</supplier_message>", ai_rephraser.ANALYZE_PROMPT)
        self.assertIn("UNTRUSTED USER DATA", ai_rephraser.ANALYZE_PROMPT)

    def test_ai_blocklist_has_deterministic_override(self):
        """INJECT-1: even a lenient AI verdict ('blocked':false) must route a
        listing to manual review when the raw source trips a blocked keyword —
        the deterministic keyword screen re-runs AFTER the AI, untrusting it."""
        import inspect
        import main as main_mod
        handler_src = inspect.getsource(main_mod._process_supplier_message)
        self.assertIn(
            "filters.contains_blocked_keyword(raw_text)",
            handler_src,
            "post-AI keyword screen must exist",
        )
        self.assertIn(
            'analysis.get("blocked") or risky_keyword',
            handler_src,
            "a blocked ALWAYS routes to review even when the model says lenient",
        )

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
            platform="ikualo",
            contact_username="@buyer",
            intent="sell",
        )
        self.assertIn("IKUALO WTB ✦ DM FAST", out)
        # A body line mentioning a country carries that country's flag on the
        # SAME line — the real alt emoji, anchored post-sanitization.
        self.assertIn("\nSpain region  🇪🇸\n", out)
        self.assertIn("\nIncludes Tuyo account\n", out)
        self.assertIn("\nID card + proof of address\n", out)
        self.assertIn("🤑 Price  DM", out)
        self.assertIn("📞 Contact  : @buyer", out)
        # Body must be emoji-free: no bullets, no fire/lightning/star inside
        # (the appended flag anchor is the only exception, and it is here).
        self.assertNotIn("⭐", out)
        # …and Spain's custom flag entity is really attached at the right spot.
        doc_ids = [e.document_id for e in entities]
        self.assertIn(countries.emoji_for("Spain"), doc_ids)

    def test_build_ai_message_header_rotates_by_seed(self):
        """Header emoji alternates fire/lightning deterministically per listing seed."""
        _, e0 = parser.build_ai_message(
            content_lines=["line"], platform="x", contact_username="@b", intent="sell",
            listing_seed=0,
        )
        _, e1 = parser.build_ai_message(
            content_lines=["line"], platform="x", contact_username="@b", intent="sell",
            listing_seed=1,
        )
        _, e0b = parser.build_ai_message(
            content_lines=["line"], platform="x", contact_username="@b", intent="sell",
            listing_seed=0,
        )
        self.assertEqual(e0[0].document_id, parser.CE_FIRE)
        self.assertEqual(e1[0].document_id, parser.CE_LIGHTNING)
        self.assertEqual(e0b[0].document_id, e0[0].document_id,
                         "same seed must pick the same emoji")

    def test_build_ai_message_body_is_emoji_free(self):
        """Emoji appear ONLY in the header/footer lines, never in the body."""
        out, _ = parser.build_ai_message(
            content_lines=["Plain body line one", "Plain body line two"],
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
            content_lines=["Need curve pay"], platform="curve", contact_username="@buyer", intent="buy",
        )
        self.assertIn("CURVE WTB ✦ DM FAST", out)
        self.assertNotIn("FOR SALE", out)

    def test_build_ai_message_uses_validated_ai_header(self):
        """A buyer-framed AI tagline is used; seller wording falls back to default."""
        out, _ = parser.build_ai_message(
            content_lines=["Netflix"], platform="netflix", contact_username="@b",
            header_word="WANTED ✦ DM FAST",
        )
        self.assertIn("NETFLIX WANTED ✦ DM FAST", out)
        out, _ = parser.build_ai_message(
            content_lines=["Netflix"], platform="netflix", contact_username="@b",
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
        """Even with no price the static 'Price DM' footer and the frozen buyer
        header are present — the variant used to show 'WANTED' and skip Price."""
        out, _ = parser.build_ai_message(
            content_lines=["Tuyo full access"], platform="tuyo", contact_username="@buyer", intent="neutral",
        )
        self.assertIn("TUYO WTB ✦ DM FAST", out)
        self.assertIn("🤑 Price  DM", out)

    def test_build_ai_message_price_zero_source_text(self):
        """A '$0'-looking source never renders 'Price: $0' — the footer is
        always the static 'Price DM' line."""
        out, _ = parser.build_ai_message(
            content_lines=["Line"], source_text="WTS netflix $0",
            platform="x", contact_username="@b", intent="sell",
        )
        self.assertIn("🤑 Price  DM", out)
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
        self.assertTrue(filters.has_clear_listing_signal("WTB Chatgpt PRICE 30$"))
        self.assertTrue(filters.has_clear_listing_signal("KYC netflix 200$"))
        self.assertTrue(filters.has_clear_listing_signal("DM @buyer 40"))
        # A platform / listing word alone is a clear signal — price not required.
        self.assertTrue(filters.has_clear_listing_signal("netflix kyc available"))
        # Price-only text carries no listing signal -> not clear.
        self.assertFalse(filters.has_clear_listing_signal("500$ USD 40 EUR pay whatever"))
        # Money present but no concrete platform / listing signal -> not clear.
        self.assertFalse(filters.has_clear_listing_signal("GOOD SELLER"))

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


class TestDestinationsForwarding(unittest.TestCase):
    """DEST-1: destination forwards are queued ONLY on successful publication and
    drained independently of the main-channel publish path."""

    def setUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"test_destinations_{os.getpid()}.db"
        )
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        db.init_db(self.db_path)

    def tearDown(self):
        for _ in range(3):
            try:
                os.remove(self.db_path)
                return
            except OSError:
                time.sleep(0.05)

    def _fresh_db(self, name: str) -> str:
        path = os.path.join(tempfile.gettempdir(), name)
        if os.path.exists(path):
            os.remove(path)
        db.init_db(path)
        return path

    def _sql(self, forwarding_id: int, db_path=None):
        conn = sqlite3.connect(db_path or self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM forwardings WHERE id = ?", (forwarding_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def test_add_and_list_destinations(self):
        a = db.add_destination(-100111, "Team Buyers", db_path=self.db_path)
        b = db.add_destination("@mygroup", "My Group", db_path=self.db_path)
        c = db.add_destination(-100333, active=False, db_path=self.db_path)

        rows = db.list_destinations(active_only=False, db_path=self.db_path)
        self.assertEqual([r["id"] for r in rows], [a, b, c])
        self.assertEqual(rows[0]["chat_id"], "-100111")
        self.assertEqual(rows[1]["chat_id"], "@mygroup")
        self.assertEqual([r["active"] for r in rows], [1, 1, 0])

        active = db.list_destinations(active_only=True, db_path=self.db_path)
        self.assertEqual([r["id"] for r in active], [a, b])

        got = db.get_destination_by_id(c, db_path=self.db_path)
        self.assertIsNotNone(got)
        self.assertEqual(got["chat_id"], "-100333")

    def test_add_destination_rejects_invalid_reference(self):
        for bad in ("", "   ", "not a peer", "a#b", "-abc", "bad username!"):
            with self.assertRaises(ValueError):
                db.add_destination(bad, db_path=self.db_path)
        self.assertEqual(db.list_destinations(active_only=False, db_path=self.db_path), [])

    def test_upsert_reactivates_and_renames(self):
        a = db.add_destination(-100121, "Old Title", active=False, db_path=self.db_path)
        a2 = db.add_destination("-100121", "New Title", active=True, db_path=self.db_path)
        self.assertEqual(a2, a, "re-adding the same chat must upsert, not duplicate")
        rows = db.list_destinations(active_only=False, db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "New Title")
        self.assertEqual(rows[0]["active"], 1)

    def test_disabled_destination_skipped_until_reenabled(self):
        off = db.add_destination(-100999, active=False, db_path=self.db_path)
        created = db.queue_forwarding(10, "@chan", 500, db_path=self.db_path)
        self.assertEqual(created, 0, "disabled destinations must not receive forwards")

        ok = db.set_destination_active(off, True, db_path=self.db_path)
        self.assertTrue(ok)
        created = db.queue_forwarding(10, "@chan", 500, db_path=self.db_path)
        self.assertEqual(created, 1)
        pending = db.get_pending_forwardings(db_path=self.db_path)
        self.assertEqual([p["destination_id"] for p in pending], [off])

    def test_enqueue_helper_creates_one_forward_per_active_destination(self):
        """The single choke point used by every publish site."""
        import asyncio
        import main as main_mod

        db.add_destination(-100211, "G1", db_path=self.db_path)
        db.add_destination(-100212, "G2", db_path=self.db_path)
        db.add_destination(-100213, "G3", active=False, db_path=self.db_path)

        old_default = db.DEFAULT_DB_PATH
        old_dest = main_mod.DEST_CHANNEL
        db.DEFAULT_DB_PATH = self.db_path
        main_mod.DEST_CHANNEL = "@mainchan"
        try:
            n = asyncio.run(main_mod.enqueue_destination_forwardings(7, 222))
        finally:
            db.DEFAULT_DB_PATH = old_default
            main_mod.DEST_CHANNEL = old_dest
        self.assertEqual(n, 2)

        pending = db.get_pending_forwardings(db_path=self.db_path)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["published_chat_id"], "@mainchan")
        self.assertEqual(
            [p["destination_chat_id"] for p in pending], ["-100211", "-100212"]
        )
        self.assertEqual(pending[0]["listing_id"], 7)
        self.assertEqual(pending[0]["published_message_id"], 222)

    def test_queue_forwarding_idempotent(self):
        db.add_destination(-100221, "G", db_path=self.db_path)
        first = db.queue_forwarding(7, "@chan", 111, db_path=self.db_path)
        dup = db.queue_forwarding(7, "@chan", 111, db_path=self.db_path)
        self.assertEqual(first, 1)
        self.assertEqual(dup, 0, "re-queuing the same published message is a no-op")
        self.assertEqual(len(db.get_pending_forwardings(db_path=self.db_path)), 1)

    def test_get_pending_orders_by_id_and_hides_not_due(self):
        a = db.add_destination(-100311, "A", db_path=self.db_path)
        b = db.add_destination(-100312, "B", db_path=self.db_path)
        db.queue_forwarding(1, "@c", 1, db_path=self.db_path)

        pending = db.get_pending_forwardings(limit=1, db_path=self.db_path)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["destination_id"], a, "oldest row first")
        self.assertEqual(pending[0]["listing_id"], 1)

        fid = pending[0]["id"]
        db.mark_forward_failed(fid, "transient", db_path=self.db_path)
        pending2 = db.get_pending_forwardings(limit=10, db_path=self.db_path)
        self.assertEqual(
            [p["destination_id"] for p in pending2],
            [b],
            "a backoff row is not due until retry_at",
        )

        row = self._sql(fid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["retry_count"], 1)
        self.assertIsNotNone(row["retry_at"])

    def test_transient_failures_eventually_failed(self):
        db.add_destination(-100411, "T", db_path=self.db_path)
        db.queue_forwarding(1, "@c", 1, db_path=self.db_path)
        pending = db.get_pending_forwardings(db_path=self.db_path)
        self.assertEqual(len(pending), 1)
        fid = pending[0]["id"]

        for _ in range(db.FORWARD_MAX_RETRIES + 2):
            row = self._sql(fid)
            if row and row["status"] == "failed":
                break
            db.mark_forward_failed(fid, "boom", db_path=self.db_path)
        row = self._sql(fid)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["retry_count"], db.FORWARD_MAX_RETRIES - 1)
        self.assertEqual(db.get_pending_forwardings(db_path=self.db_path), [])

    def test_permanent_failure_is_terminal(self):
        db.add_destination(-100511, "P", db_path=self.db_path)
        db.queue_forwarding(1, "@c", 1, db_path=self.db_path)
        pending = db.get_pending_forwardings(db_path=self.db_path)
        fid = pending[0]["id"]
        db.mark_forward_failed(fid, "ChatWriteForbiddenError", permanent=True, db_path=self.db_path)
        row = self._sql(fid)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["retry_count"], 0, "permanent errors are never retried")
        self.assertEqual(db.get_pending_forwardings(db_path=self.db_path), [])

    def test_delete_destination_keeps_forwarding_history(self):
        d = db.add_destination(-100611, "H", db_path=self.db_path)
        db.queue_forwarding(1, "@c", 1, db_path=self.db_path)

        ok = db.delete_destination(d, db_path=self.db_path)
        self.assertTrue(ok)
        self.assertIsNone(db.get_destination_by_id(d, db_path=self.db_path))
        self.assertEqual(db.list_destinations(active_only=False, db_path=self.db_path), [])
        self.assertEqual(db.get_pending_forwardings(db_path=self.db_path), [])

        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM forwardings WHERE destination_id = ?", (d,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "history rows outlive the destination")

    def test_worker_drain_delivers_and_defers_a_flooded_destination(self):
        db_path = self._fresh_db("dest_worker_flood.db")
        db.add_destination("-100710", "A", db_path=db_path)
        db.add_destination("-100712", "B", db_path=db_path)
        db.queue_forwarding(1, "@chan", 700, db_path=db_path)

        delivered = []
        calls = {"n": 0}

        def _flood_err():
            calls["n"] += 1
            err = FloodWaitError.__new__(FloodWaitError)
            err.seconds = 5
            return err

        class _FakeClient:
            async def forward_messages(self, to_entity, messages, from_peer):
                if to_entity == "-100712":
                    raise _flood_err()
                delivered.append(to_entity)
                return object()

        main_mod = None
        import asyncio
        import main as _main
        main_mod = _main
        old_default = db.DEFAULT_DB_PATH
        db.DEFAULT_DB_PATH = db_path
        try:
            with mock.patch("main.FORWARD_FLOODWAIT_BUDGET_SECONDS", 2.0), mock.patch.object(
                publish_guard.asyncio, "sleep", new_callable=lambda: mock.AsyncMock()
            ):
                asyncio.run(main_mod._drain_forward_queue(_FakeClient()))
        finally:
            db.DEFAULT_DB_PATH = old_default

        self.assertEqual(delivered, ["-100710"], "non-flooded destination still delivered")
        self.assertGreaterEqual(calls["n"], 1, "the flooded destination was attempted")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT f.*, d.chat_id AS destination_chat_id FROM forwardings f "
            "JOIN destinations d ON d.id = f.destination_id ORDER BY f.id"
        ).fetchall()
        conn.close()
        by_dest = {r["destination_chat_id"]: r for r in rows}
        self.assertEqual(by_dest["-100710"]["status"], "forwarded")
        self.assertEqual(by_dest["-100712"]["status"], "pending", "flooded dest is deferred, not failed")
        self.assertEqual(by_dest["-100712"]["retry_count"], 1)
        os.remove(db_path)

    def test_worker_drain_marks_permanent_failure_terminal(self):
        db_path = self._fresh_db("dest_worker_perm.db")
        db.add_destination("-100810", "A", db_path=db_path)
        db.add_destination("-100812", "B", db_path=db_path)
        db.queue_forwarding(1, "@chan", 800, db_path=db_path)

        delivered = []

        class _FakeClient:
            async def forward_messages(self, to_entity, messages, from_peer):
                if to_entity == "-100812":
                    raise ChatWriteForbiddenError(request=None)
                delivered.append(to_entity)
                return object()

        import asyncio
        import main as _main
        main_mod = _main
        old_default = db.DEFAULT_DB_PATH
        db.DEFAULT_DB_PATH = db_path
        try:
            asyncio.run(main_mod._drain_forward_queue(_FakeClient()))
        finally:
            db.DEFAULT_DB_PATH = old_default

        self.assertEqual(delivered, ["-100810"])
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT f.*, d.chat_id AS destination_chat_id FROM forwardings f "
            "JOIN destinations d ON d.id = f.destination_id ORDER BY f.id"
        ).fetchall()
        conn.close()
        by_dest = {r["destination_chat_id"]: r for r in rows}
        self.assertEqual(by_dest["-100810"]["status"], "forwarded")
        self.assertEqual(by_dest["-100812"]["status"], "failed", "permanent error is terminal")
        self.assertEqual(by_dest["-100812"]["retry_count"], 0, "no retries for permanent errors")
        os.remove(db_path)

    def test_queue_forwarding_only_called_from_publish_path(self):
        """DEST-1 guard: manual messages typed in the main channel can NEVER be
        forwarded — the only callers of queue_forwarding in the whole codebase
        are the successful-publication choke point (main.py) and the admin
        approve-publish hook (admin_bot.py)."""
        import admin_bot as admin_mod
        import main as main_mod

        src_main = open(main_mod.__file__, encoding="utf-8").read()
        src_admin = open(admin_mod.__file__, encoding="utf-8").read()

        self.assertEqual(
            src_main.count("queue_forwarding"),
            1,
            "main.py must enqueue ONLY inside enqueue_destination_forwardings "
            "(monitoring / pipeline paths may never call it)",
        )
        self.assertIn("async def enqueue_destination_forwardings", src_main)
        self.assertIn("db.queue_forwarding", src_main)

        approve_blocks = src_admin.count("db.queue_forwarding")
        self.assertGreaterEqual(
            approve_blocks, 1, "admin approve-publish must enqueue forwarding"
        )
        for needle in ("destadd", "desttoggle", "destdel", "destdelyes",
                       "menu:destinations", "adddestination"):
            self.assertIn(needle, src_admin)

    def test_destinations_reply_keyboard_button_exact_text(self):
        """The reply-keyboard button the admin sees must be exactly
        '📥 Destinations' (Telegram sends the button text back as a plain
        message, so the router must match this text character-for-character)."""
        import admin_bot as admin_mod

        with mock.patch("db.is_paused", return_value=False), \
             mock.patch("db.is_buyer_asleep", return_value=False):
            labels = [b.button.text for row in admin_mod._home_keyboard() for b in row]
        self.assertIn("📥 Destinations", labels)

    def test_destinations_button_text_routes_to_menu(self):
        """Logical routing check mirroring Telethon's own matching: the
        destinations text-tap router is wired to the SAME constant as the reply
        keyboard button, and Telethon runs `re.compile(pattern).match(raw_text)`;
        the exact text sent by the '📥 Destinations' button must therefore reach
        the handler — just like '📋 Sources' does for Sources."""
        import re

        import admin_bot as admin_mod

        with mock.patch("db.is_paused", return_value=False), \
             mock.patch("db.is_buyer_asleep", return_value=False):
            labels = [b.button.text for row in admin_mod._home_keyboard() for b in row]

        for text in (
            "📥 Destinations",            # the label the admin actually sees
            admin_mod.DESTINATIONS_BTN,   # the shared constant
            "/destinations",              # command entry point still routes
            "📤 Destinations",            # legacy label keyboards keep working
        ):
            self.assertIsNotNone(
                re.compile(admin_mod.DESTINATIONS_ROUTE_RE).match(text),
                f"'{text}' must route to the Destinations menu",
            )

        self.assertTrue(
            any(
                re.compile(admin_mod.DESTINATIONS_ROUTE_RE).match(t) for t in labels
            ),
            "the reply-keyboard Destinations button text must match the router",
        )

        self.assertIsNone(
            re.compile(admin_mod.DESTINATIONS_ROUTE_RE).match("📋 Sources"),
            "Sources text must not be swallowed by the Destinations router",
        )


if __name__ == "__main__":
    unittest.main()