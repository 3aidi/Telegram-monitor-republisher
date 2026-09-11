# Telegram Channel Monitor & Auto-Republisher (Financial, Crypto & Streaming Accounts)

An automated, robust Telegram channel monitor and republisher for digital accounts and subscriptions (Netflix, Spotify, Bybit, Crypto.com, Binance, Buddybank, Wise, Revolut, Hetzner, etc.). It monitors a network of supplier channels, cleans and filters listings, recalculates pricing with a configurable multiplier, flags stolen/cracked accounts, removes emojis, and republishes clean formatted listings to your destination channel with your contact handle for buyer direct messages.

---

## Key Features

1. **Multi-Supplier Monitoring**: Listen to multiple source channels simultaneously using Telethon MTProto. Source channels can be **usernames** (`@channel`) or **numeric channel IDs** (`-1001234567890`).
2. **Emoji Stripping & Cleaning**: Strips all emojis while preserving account specs, durations, and details.
3. **Smart Platform & Intent Extraction**:
   - **Streaming & Watching**: Netflix, Spotify, YouTube Premium, Disney+, Hulu, Prime Video, Apple TV, IPTV, etc.
   - **Crypto Platforms**: Bybit, Crypto.com, Binance, KuCoin, OKX, Coinbase, Kraken, Bitget, etc.
   - **Financial, Neobanks & Payment Gateways**: Buddybank, Indexo, Revolut, Wise, PayPal, Stripe, Payoneer, Skrill, Neteller, Shopify, etc.
   - **Cloud, VPS & KYC**: Hetzner, ChatGPT / OpenAI, AWS, DigitalOcean, OVH, etc.
   - **Customizable**: Easily add additional platforms via `ADDITIONAL_PLATFORMS` in `.env`.
4. **Robust Price Parsing**:
   - Handles thousands separators (`$1,200` → 1200), suffix currencies (`1,200$`), decimal-comma prices (`12,50€` → 12.50), and `price:`/`rate:` prefixes.
   - Falls back to the raw message text so prices placed next to emoji badges are not missed.
5. **Intent Detection**:
   - Detects buy/sell intent: `WTB`, `want to buy`, `wts`, `for sale`, `fs`, `selling`, `buying`, `need`, `available`, etc.
   - Listsings without a price but with a DM/contact signal are routed to manual review.
6. **Stolen / Hacked Account Keyword Blocklist**:
   - Rejects listings with keywords indicating illicit access (`hacked`, `cracked login`, `no email`, `stolen`, `dump`, `no recovery info changed`).
   - Logs matched keywords to SQLite for audit.
7. **Idempotent Storage & Deduplication**:
   - A unique index on `(supplier_id, source_message_id)` guarantees no source message is ever processed twice.
   - Fingerprint-based 48-hour duplicate detection using `(platform, rounded price, normalized text)`.
8. **Configurable Pricing Rule**:
   - Per-channel multiplier stored in SQLite, or default global multiplier from `.env` (e.g. `0.75` for a 25% discount or custom markup).
9. **Contact / DM Footer**:
   - Adds `📞 Order : @username` to the footer of every republished post so buyers can contact you directly.
10. **Admin Approval Bot**:
    - Inline `[Approve]` and `[Reject]` buttons for ambiguous listings.
    - Failed-publish queue (DLQ) with `/failed` and `/retry <id>`.
    - `/preview <id>` shows the exact formatted post before publishing.
11. **Publish Reliability**:
    - Retry with exponential backoff, automatic FloodWait sleep, and destination throttling.
    - After retries are exhausted a listing moves to the `failed` queue (visible to the admin bot) instead of being silently stuck.
    - Approved listings are never double-published (`published_message_id IS NULL` guard).
12. **Message Edit & Deletion Handling**:
    - Source message edits update the published post in real-time (with a no-op guard).
    - Source message deletions are safely logged while keeping published posts intact for manual review.
13. **Error Recovery & Logging**:
    - Auto-reconnect loop with capped backoff.
    - Console output + rotating log file (`monitor.log`, 5MB max, 5 backups).
    - Full `audit_log` table for actions (auto-publish, admin approve/reject/retry, edits, blocklist rejections).

---

## Project Structure

```
├── main.py            # Main entry point & orchestrator
├── parser.py          # Emoji stripping, platform/price/intent extraction & formatting
├── filters.py         # Keyword blocklist & 48h duplicate detection
├── db.py              # SQLite schema, migrations & helper queries
├── admin_bot.py       # Telegram bot for admin management & inline approvals
├── test_system.py     # Automated unit test suite
├── run.ps1            # PowerShell quick start script
├── .env.example       # Template for environment variables
├── requirements.txt   # Python dependencies
└── monitor.db         # SQLite database (auto-generated, gitignored)
```

---

## Setup & Installation

### 1. Virtual Environment

```powershell
# Windows (PowerShell)
python -m venv venv
.\venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
python3 -m venv venv
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure `.env`

Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

Edit `.env` with your credentials (see `.env.example` for the full list with comments):

```env
# Telegram MTProto User API (https://my.telegram.org)
API_ID=12345678
API_HASH=0123456789abcdef0123456789abcdef

# Destination Channel where listings will be posted
DEST_CHANNEL=@your_destination_channel

# Monitored Source Channels (comma-separated usernames OR numeric channel IDs)
# ⚠️ OPTIONAL — can be left empty! A fresh deployment starts fine with 0 sources
#    and the admin adds every channel via the admin bot's Sources menu (Add
#    Source, or forward a message from a private channel/group).
#    When set, it is READ ONLY ONCE — on the very first startup with an EMPTY
#    supplier database. After that the database is the single source of truth:
#    manage sources exclusively via the admin bot's Sources menu. Editing this
#    list later has NO effect (and a deleted source will never silently
#    reappear) unless /reseed_from_env is run manually.
SOURCE_CHANNELS=@channel_one, -1003340459479

# Admin Bot Token (from @BotFather)
BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ

# Your personal Telegram User ID (numeric, from @userinfobot)
ADMIN_USER_ID=123456789

# Contact handle for buyers to DM you
CONTACT_USERNAME=@your_username

# Default pricing multiplier (0.75 = 75% of original price)
PRICE_MULTIPLIER=0.75

# Optional extras:
ADDITIONAL_PLATFORMS=
# DB_PATH=monitor.db
# PUBLISH_INTERVAL=1.5
# PUBLISH_MAX_RETRIES=4
# BACKFILL_ON_START=0
```

> **Migrating an existing database**: `db.py` auto-migrates older databases on startup (`PRAGMA user_version`). It adds the new columns, removes pre-existing duplicate rows (keeping the newest), and creates the unique index.
>
> **`SOURCE_CHANNELS` one-time seeding**: entirely optional — a fresh deployment can start with it empty and have the admin add every source through the bot's Sources menu. When set, it is read once on the first startup against an empty supplier database, and on a DB that already has supplier rows (e.g. your existing production DB) the bot detects non-empty supplier data and marks `.env` as "already seeded" **without** re-adding anything — previously deleted sources stay deleted and nothing is duplicated. From then on, `.env` is never consulted for suppliers again. Only `/reseed_from_env` re-reads it, deliberately. Starting with 0 suppliers logs a warning and runs normally (bot monitors nothing until sources are added).

---

## Running the Application

### Option A: Unified Runner (Recommended)

Runs the Telegram listener and the Admin Bot together in a single process:

```powershell
.\run.ps1
# or:
python main.py
```

*Note on First Run:* Telethon will prompt you once in the terminal for your phone number and login code to create `monitor_session.session`. Subsequent runs will connect automatically.

### Option B: Separate Admin Bot

If you prefer to run the admin bot in a dedicated terminal window:

```powershell
python admin_bot.py
```

---

## Admin Bot Commands

Message your bot directly on Telegram (only authorized for `ADMIN_USER_ID`):

| Command | Description | Example |
| :--- | :--- | :--- |
| `/suppliers` | List all monitored suppliers and active status | `/suppliers` |
| `/addsupplier <channel>` | Add and activate a new channel (username or numeric ID) | `/addsupplier @kycgroupke` |
| `/removesupplier <channel>` | Permanently delete a channel (history kept) | `/removesupplier @kycgroupke` |
| `/dedupe_suppliers` | Merge duplicate supplier rows (same real channel) | `/dedupe_suppliers` |
| `/reseed_from_env` | Manually re-import `SOURCE_CHANNELS` from `.env` once (confirm required) | `/reseed_from_env` |
| `/rule <channel> <multiplier>` | Set channel markup multiplier | `/rule @kycgroupke 0.8` |
| `/status` | View today's stats (processed, published, skipped breakdown) | `/status` |
| `/pending` | Review and approve ambiguous listings | `/pending` |
| `/failed` | List failed publishes (DLQ) with error details | `/failed` |
| `/retry <id>` | Re-queue a failed listing for publishing | `/retry 15` |
| `/preview <id>` | Preview the formatted post before publishing | `/preview 15` |

### Ambiguous Listing Approval Flow

When a source message contains a valid platform and price but lacks an explicit WTB/FS keyword:

1. The listing is saved with status `pending_approval`.
2. The Admin Bot sends an alert to your Telegram with post details.
3. Tap **[✅ Approve]** to republish the listing immediately to `DEST_CHANNEL`.
4. Tap **[❌ Reject]** to mark it as skipped.

If the bot cannot publish (e.g. temporary error), the listing is marked `approved` and the background worker publishes it automatically. Failed publishes land in the DLQ and can be re-queued with `/retry`.

---

## Running Tests

Run the unit test suite to verify parsing, database, filters, and migrations:

```powershell
python test_system.py
```
