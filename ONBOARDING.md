# ONBOARDING — Telegram Channel Monitor & Auto-Republisher

> This document is a complete, self-contained map of how this codebase works so
> a developer (or LLM) can understand it and modify it safely WITHOUT reading
> every file. Treat it as the authoritative overview. Every heading maps to real
> code; inline comments in the source carry the finer detail.

---

## 1. What this project is

A **Telegram bot** that watches a set of **source channels** (reseller listing
channels), picks out real product listings, cleans / rephrases them with an **AI
model**, formats them into a fixed "listing post" shell, and **publishes them to
a destination channel** — while a **personal admin bot** lets the owner approve,
skip, edit, retry and manage everything by chatting with a Telegram bot.

Key characteristics that color every design decision:

- The **destination channel always speaks as the BUYER**. Posts are framed
  "WTB / DM", the price footer is always static `Price  DM`, and seller-ish
  wording must never leak into the header or body.
- The owner communicates through an **admin bot** (plain Telethon bot client,
  not a web app). Everything else (monitoring, AI, publishing) happens on a
  **user MTProto session**.
- **Publishing correctness is the top priority**: correct UTF-16 entity
  offsets, exact custom-emoji anchors, no duplicates, no leaked source prices,
  no accidental auto-publish of payment-proofs or hacked/stolen content.
- Storage is a single **SQLite** database (`monitor.db` by default). Everything
  is idempotent and re-runnable.

---

## 2. Tech stack & runtime

- **Python 3.x**, `asyncio`, **Telethon** (`TelegramClient`) for both the user
  session and the admin bot session.
- **SQLite** via stdlib `sqlite3` (wrapped by `db.py`).
- **Groq** for AI analysis/rephrasing (primary), **OpenRouter** as a free
  fallback provider. `prompt` in `ai_rephraser.py`.
- `python-dotenv` loads `.env`. Logging: rotating `monitor.log` + console.
- Windows is the primary dev OS; the repo ships `run.ps1`, `start_aws.ps1`,
  `stop_aws.ps1` and a GitHub Actions deploy workflow.

---

## 3. File map

| File | Role |
|---|---|
| `main.py` | **Orchestrator / entry point.** Config, logging, single-instance guard, Telethon user client, event handlers, the full supplier→published pipeline, backfill, workers, reconnect loop. `python main.py` starts the monitor; `--manual` starts the admin bot + user session **without** ingestion/publishing. |
| `admin_bot.py` | **Admin bot.** Inline keyboards, `/commands`, listing approval/skip/edit flows, preview, sources & supplier management, skip/failed digests, status screens. |
| `ai_rephraser.py` | **AI engine.** `analyze_message(raw_text) -> dict | None` — one call decides everything (is_listing, blocked, platform, intent, header, content lines). Groq + OpenRouter fallback, caching, retries, JSON cleanup. |
| `parser.py` | **Formatting module.** `build_ai_message(...) -> (text, entities)` builds the final post shell + all `MessageEntityCustomEmoji`/link entities; price extraction; body sanitizer (`sanitize_body_lines`); buyer-header validation. |
| `countries.py` | **Country→flag system.** `COUNTRY_EMOJI` (name→custom-emoji document id), `_FLAG_ALTS` (name→exact alt emoji), detection (`detect_countries`, `canonical_of`), flag attachment (`flag_body_lines`). Bootstrapped from a dumped paste of the actual custom emoji pack. |
| `filters.py` | **Pre-AI + fallback filters.** Duplicate detection (fingerprint+price), chatter pre-filter, payment-proof detection, blocked-keyword screen, `has_clear_listing_signal`. |
| `db.py` | **Everything SQLite.** Schema + migrations, listings, suppliers, skips, audit log, AI cache, settings/pause/asleep toggles, env-seed, fingerprints, post numbering, stats, dedupe/merge. |
| `publish_guard.py` | **Single shared publish lock + rate limit** (`PUBLISH_INTERVAL`) so the auto-worker and admin-approve never fire into the destination simultaneously. |
| `test_system.py` | The **spec**. ~130 unit tests covering the pipeline, parser formatting, entity offsets, sanitizer, filters, country flags, DB, FloodWait handling, injection defense. Run with `python -m unittest test_system`. |
| `requirements.txt` / `.env.example` / `run.ps1` / deploy workflow | Dependencies, config template, local start script, CI deploy. |

---

## 4. Runtime lifecycle (main.py `main()`)

1. `_validate_config()` — fail fast if `API_ID`/`API_HASH`/`DEST_CHANNEL` missing.
2. `db.init_db()` (schema + migrations), then startup housekeeping: normalize legacy supplier ids, prune the AI cache, warn on env-seed config issues.
3. `db.ensure_env_seed(SOURCE_CHANNELS)` — seeds the suppliers table from `.env` **ONLY ONCE** (guarded by `env_seed_completed`). After that the DB is the single source of truth; `.env` `SOURCE_CHANNELS` is ignored unless `/reseed_from_env`.
4. `ai_rephraser.init_groq(GROQ_API_KEY)`.
5. Start Telethon **user client** + optional **admin bot client** (if `BOT_TOKEN`).
6. **Normal mode** (`python main.py`): register `NewMessage` / `MessageEdited` / `MessageDeleted` handlers → `resolve_supplier_entities` → warn if 0 suppliers resolved → dedupe supplier rows → background workers:
   - `approved_listings_worker` — drains `approved` listings into the destination;
   - `supplier_resolution_worker` — periodic re-resolve;
   - `health_check_worker` — hourly stats log;
   - `rephrase_unpublished()` — re-AI unpublished listings whose body is still the regex fallback;
   - optional `run_backfill` (`BACKFILL_ON_START=1`).
7. Reconnect loop with capped backoff on disconnect; graceful shutdown awaits/cancels all tasks (SHUT-1).

**Manual mode** (`--manual`): user client + admin bot only — no ingestion, no auto-publish; approvals publish immediately via the worker.

---

## 5. The end-to-end message pipeline

This is the heart of the app. Entry: `main.process_supplier_message` →
`_process_supplier_message` (serialized per `(supplier_id, source_msg_id)` by an
in-memory asyncio lock, CONC-2; plus a **FloodWait-aware** wrapper).

Order of operations for each source message:

1. **Idempotency**: if `db.get_listing_by_source` already has the message, ignore (no re-processing/duplicate publishing).
2. **Self-echo guard**: if the raw text contains our own `Contact  : @CONTACT` footer, skip as `self_echo` (stops the bot re-ingesting its own published posts).
3. `db.insert_listing(...)` status `received`.
4. **Step 1 — duplicate filter** (`filters.check_filters`): normalized-text + price fingerprint within `DEDUP_HOURS` → status `skipped_duplicate`, reason logged to skips table.
5. **Step 1.5 — chatter pre-filter** (`filters.obvious_non_listing`, only if `PRE_FILTER_CHATTER=1`): obvious rules/pins/greetings skip BEFORE the AI → `skipped_chatter` (quota saver; any listing signal vetoes).
6. **Step 1.75 — payment-proof screen** (`filters.detect_payment_proof`): a "I paid $X" message is NEVER auto-published → `pending_approval`, review prompt sent to admin.
7. **Step 2 — AI analysis** (`ai_rephraser.analyze_message`):
   - `None` → **deterministic fallback path** (see below) or `pending_review`.
   - `blocked=True` → `pending_review` (never auto-publish hacked/stolen/high-risk content).
   - **INJECT-1 (deterministic override)**: the `filters.contains_blocked_keyword(raw)`
     screen runs again AFTER the AI and forces `pending_review` even if the model
     returned a lenient `blocked:false` — a prompt-injected source can never
     talk its way into an auto-publish of stolen/hacked content. The AI prompt
     also wraps the untrusted supplier text in a `<supplier_message>` block and
     tells the model to treat it strictly as data.
   - `is_listing=False` → `skipped_filter` (`not_a_listing`).
   - else `platform`, `intent`, `header`, `content` are persisted; the AI body is sanitized (`prepare_body`) and gated:
   - **Buy-intent gate** (`buy_auto_publish_ok`): `intent == "buy"` + not paused + substantive sanitized body (`body_ok`) + no payment-proof → **rephrase + build_ai_message + auto-publish** → status `published` + admin alert.
   - Everything else (sell/neutral/weak body/paused/unsafe) → `pending_approval` + admin prompt with the exact gate reason.

**Deterministic AI-down fallback** (`deterministic_fallback_publish_ok`, gated HARD):
only when `DETERMINISTIC_FALLBACK=1` AND not paused AND no blocked keyword AND no
payment-proof AND a concrete listing signal (platform/WTS-WTB/@contact/URL) AND
`body_ok`. Body = emoji-stripped source, published via the same `build_ai_message`
shell. Everything weaker routes to manual review — never silently dropped, never
blindly published.

**Edited messages**: re-analyze; update the published post (edit in destination)
unless AI says blocked / not a listing / unavailable (then leave the published
post intact).

---

## 6. The AI contract (`ai_rephraser.analyze_message`)

Returns a dict or `None`:
```json
{
  "is_listing": true,          // false => skip or review
  "blocked": false,             // true => pending_review
  "block_reason": "",
  "platform": "netflix",        // informational only; never gates publishing
  "intent": "buy"|"sell"|"neutral",
  "header": "WTB ✦ DM FAST",    // must pass sanitize_buyer_header or default used
  "content": ["line1", "line2"],// the AI-rewritten body (emoji-free, must NOT contain prices/handles/DM-only lines)
  "price": null
}
```
`price` is informational; **pricing never influences publishing decisions** —
there is no price on any published post (`Price  DM`), and no price-gated
logic anywhere. Groq primary, OpenRouter fallback w/ cooldown, JSON repair,
per-fingerprint caching (`AI_CACHE_TTL_HOURS`).

---

## 7. The formatting contract (`parser.build_ai_message`)

The ONE function every published post goes through (auto, worker, admin approve,
preview). Hard rules:

- **`formatting_entities` is always passed WITHOUT `parse_mode`** when sending, so
  Telethon never interprets `**`/`*` as markdown. The body sanitizer strips them
  as literal text instead.
- Body is **emoji-free**; emoji appear ONLY in the header decoration, country
  lines, and footer lines.
- Layout:
  ```
  #N                               (optional, only when post_number given)
  🔥 PLATFORM MODE 🔥 …              (fire/lightning rotate by listing_seed)
  <sanitized AI body lines, each with its country flag appended if matched>
  [generated country lines for countries in source_text not in the body]
  ────────
  🤑 Price  DM
  📞 Contact  : @CONTACT
  [buyer-asleep footer if toggle ON]
  ```
- `post_number` comes from `db.next_post_number()`; header emoji rotates by
  `listing_seed` (`listing_id`) so preview == published.
- **All entity offsets/lengths are UTF-16 code units** — computed with
  `_utf16_len()`, never Python `len` (multi-codepoint: 🔥=2, 🇵🇱=4, 🏴󠁧󠁢󠁳󠁣󠁴󠁿=14).
- `_make_custom_emoji_entity(...)` is the ONLY place entities are built.

### The country-flag subsystem (`countries.py` + parser)

- `COUNTRY_EMOJI`: canonical country name → Telegram custom-emoji **document id**
  (the real set the owner's channel uses, bootstrapped from a pasted dump =
  `_FLAG_DUMP_LINE_RE` format `N)flag [id]`).
- `_FLAG_ALTS`: country → the **exact emoji** that is that document's
  `documentAttributeCustomEmoji.alt`. **Telegram renders the custom emoji ONLY
  when the `MessageEntityCustomEmoji` wraps exactly that glyph** — so anchors are
  never placeholders like `·` / `_` / `*` / space.
- Detection: `detect_countries(text)` scans for canonical names + aliases (USA,
  UK, EU, etc.); England resolves to the GB flag by design (Scotland/Wales keep
  their own subdivision-flag documents). URLs/@handles and compound territories
  are masked before matching.
- `flag_body_lines(lines) -> (flagged_lines, covered)` appends `"  <flag>"` (exactly
  two spaces) to each body line mentioning a country, and returns `covered` — the
  set of canonicals already in the body. **The extras path skips any country in
  `covered`**; that single check is what kills the "NO POLAND / POLAND 🇵🇱"
  duplicate. Countries detected in `source_text` but absent from the body become
  generated lines. A detected-but-unmapped country stays plain (never a guessed
  flag).
- Flags are appended AFTER sanitization, so the body emoji-strip can never touch
  them.

---

## 8. Filters & publish guard (`filters.py`, `publish_guard.py`)

- **Duplicates**: fingerprint = NFKC-folded text + price, within `DEDUP_HOURS`
  (`find_recent_similar_listing`). Same text at a different price is NOT a dup.
- **Chatter**: conservative pattern list; any listing signal vetoes.
- **Payment proof**: narrow vocabulary (`proof of payment`, `screenshot of...`,
  `paid ... proof`) — never auto-publish, always manual review.
- **Blocked keywords**: mirror of the AI's `blocked` signal for the AI-down path.
- **Publish guard**: `publish_guard.throttle()` = one lock + `PUBLISH_INTERVAL`
  (default 1.5s) shared by auto-publish and admin-approve so publishes are
  serialized (CONC-1/CONC-3). `publish_guard` interval is owned by main.
- `publish_to_destination`: throttle → retry w/ exponential backoff →
  honors `FloodWaitError.seconds` → `PublishError` after `PUBLISH_MAX_RETRIES`.

---

## 9. Database model (`db.py`)

Single SQLite file (`DB_PATH`, default `monitor.db`). Key concepts:

- **suppliers** (monitored channels): username/display_name/channel_id (numeric,
  marked `-100...`), `active`, membership/entity metadata. Manage EXCLUSIVELY
  via the admin bot Sources menu after the one-time env seed.
- **listings**: one row per source message — `raw_text`, `clean_text`
  (AI-rewritten body or emoji-stripped fallback), `platform_name`, `intent`,
  `header_word`, `status`, `published_message_id`,
  `post_number`, fingerprint, `created_at`.
- **Statuses** that matter (the admin flow keys off these):
  `received` → `skipped_*` / `pending_review` / `pending_approval` / `approved` /
  `published` / `failed`. `listing_is_editable()` whitelist stops
  stale in-flight edits after a listing leaves an editable state.
- **skips**: every skip reason with message linkage (drives the skip digest).
- **audit_log**: one row per decision (`published_auto`, `published_approved`,
  `ai_blocked_review`, `payment_proof_review`, `duplicate`, ...).
- **ai_cache**: fingerprint → analysis dict, pruned after `AI_CACHE_TTL_HOURS`.
- **settings**: `paused` ("All Stop" — gates ONLY automatic paths, never a manual
  Approve), `buyer_asleep` (adds a footer to every post), env-seed flags,
  skip-digest marker.
- Operations are synchronous and wrapped in `db_session`/`_commit_with_retry`;
  async callers use `asyncio.to_thread`.

---

## 10. Admin bot (`admin_bot.py`)

Personal bot (filtered to `ADMIN_USER_ID`). Command surface (also reachable via
the Home keyboard buttons):

`/start` `/help` · `/suppliers` `/sources` · `/addsupplier [text|forwarded msg]`
`/removesupplier` · `/dedupe_suppliers` · `/reseed_from_env` · `/status`
`/pending` · `/failed` · `/skipped` · `/published`
`/post [N]` · `/retry [N]` · `/preview [N]` · pause/resume ("All Stop/All Start")
· "I'm Asleep"/"I'm Awake" toggle.

Inline flows:
- **Approve/Skip** (`approve:{id}` / `skip:{id}`): Approve sets `approved`
  and the worker publishes immediately (never gated by All Stop). Skip sets
  `skipped_admin` (re-reviewable from `/skipped`). Buttons refuse in-flight
  actions on non-editable statuses.
- **Edit wizard** (`_edit_prompt` + `_edit_listing`): edit the body draft, then
  preview the exact publishing view before Approve.
- Home menu, **Sources** editor (add via text or by forwarding a message from a
  private channel/group, toggle active, remove), **Skipped** with
  per-card Re-review (`reskip:{skip_id}`), **Failed/DLQ** with Retry,
  **Published** listing with #post + source/destination links, Status report with
  skip-reason breakdown.

---

## 11. Invariants — "rules that must never break"

These are the project's hard-won safety properties. Preserve them in ANY change:

1. **Never auto-publish without a hard gate.** Auto-publish happens only for
   (a) buy-intent with a substantive sanitized body, no payment-proof, not
   paused; or (b) the deterministic fallback under its full gate. Everything else
   goes to manual review — nothing is ever silently dropped.
2. **Payment-proof messages are never auto-published**, ever. This is the #1
   dangerous misclassification by design.
3. **Prices never render and never gate publishing.** The footer is always
   `Price  DM` — the whole pricing system (`our_price`/`original_price`/
   `markup_multiplier`/`apply_pricing_rule`) was removed (schema v9); fresh DBs
   don't even have the columns.
4. **The channel is always the buyer.** Seller wording must never appear in the
   header (`sanitize_buyer_header`); leaked source prices/@handles/DM-only lines
   are stripped from bodies by the shared sanitizer.
5. **Entities must use UTF-16 offsets** (`_utf16_len`), and every custom emoji
   must wrap exactly the real alt emoji. Never guess an anchor, never use a
   placeholder. `_make_custom_emoji_entity` is the only place they're built.
6. **No duplicate flags**: a country already in the body (`covered`) is never
   re-emitted as an extras line.
7. **No double-processing / double-publishing**: idempotency by
   `(supplier_id, source_msg_id)`; processing locks per key; publish serialized by
   `publish_guard`.
8. **The `.env` seeds suppliers only once**; after that the DB is authoritative —
   edit sources only via the admin bot; `/reseed_from_env` is the explicit escape.
9. **Never change the published message after the fact except via the edit
   path** (`process_edited_message`), and never delete/retract a post
   silently.
10. **All entity/alts data stays data**: `COUNTRY_EMOJI`/`_FLAG_ALTS` are loaded
    from the dump, never fetched at runtime, never guessed.

---

## 12. Configuration (`.env`)

See `.env.example`. Essentials: `API_ID`, `API_HASH`, `DEST_CHANNEL`, `BOT_TOKEN`,
`ADMIN_USER_ID`, `CONTACT_USERNAME`, `SOURCE_CHANNELS` (one-time seed),
`GROQ_API_KEY` (+ `GROQ_MODEL`/`GROQ_TEMPERATURE`/`GROQ_MAX_RETRIES`/
`GROQ_MAX_CONCURRENCY`), optional `OPENROUTER_API_KEY` (+ model/timeout/cooldown),
`PUBLISH_INTERVAL`, `PUBLISH_MAX_RETRIES`, `BACKFILL_ON_START`,
`DEDUP_HOURS`, `AI_CACHE_TTL_HOURS`, `PRE_FILTER_CHATTER`,
`DETERMINISTIC_FALLBACK`, `DB_PATH`.

---

## 13. Running & testing

- Unit tests: `python -m unittest test_system` (no network needed; the suite
  mocks/stubs AI and Telegram).
- Run the bot: `python main.py` (normal) · `python main.py --manual` (admin-only).
- Local launcher: `run.ps1` (Windows). Server: `start_aws.ps1`; CI/`deploy.yml`
  installs deps and rebuilds.
- Never commit real `.env`, `*.session`, `*.pem` — `.gitignore` covers them.

---

## 14. History / provenance

- `countries.py`'s `COUNTRY_EMOJI` came from pasting the actual custom-emoji
  pack a Telegram account owns and decoding each flag's Unicode; the dump format
  is parseable via `countries.build_flags2024_mapping`/`build_flags2024_alts`
  (kept as the bootstrap tooling).
- The admin bot is being actively iterated on (latest work: new buttons + renewed
  design and logic flow in `admin_bot.py`). `CHANGES.md` documents older
  audit-style change history.
- Everything in the codebase is designed so the owner can run it as a private
  self-service system with no web UI.