"""Centralized text strings for the Admin Bot and system notifications.

All user-facing phrases are organized by screen/feature for easy editing.
Placeholders formatted as {name} are dynamically populated at runtime.
"""

# ═══════════════════════════ COMMON ═══════════════════════════
LINE = "━━━━━━━━━━━━━━"
ACCESS_DENIED = "⚠️ Access denied. (ID: {user_id})"
UNAUTHORIZED = "⛔ Unauthorized"
UNKNOWN_ACTION = "Unknown action"
CANCELLED = "Cancelled"
BTN_HOME = "🏠 Home"
BTN_BACK = "⬅️ Back"
BTN_CANCEL = "🚫 Cancel"
BTN_PREV = "⬅️ Prev"
BTN_NEXT = "➡️ Next"
PAGE_FOOTER = "Page {page} of {pages}"
SOURCE_LINK_BTN = "🔗 Source"
POST_LINK_BTN = "🔗 Post"
NOT_FOUND_LISTING = "Listing not found."

# ═══════════════════════════ HOME & NAVIGATION ═══════════════════════════
HOME = "⚡ **Control Center**"
HOME_LANDING = "⚡ **Control Center**"
HELP = (
    "⚡ **Control Center**\n\n"
    "• /pending · Review queue\n"
    "• /sources · Monitored channels\n"
    "• {destinations} · Forward targets\n"
    "• /status · System telemetry\n"
    "• /published · Active posts\n"
    "• /skipped · Dropped logs\n"
    "• /headers · Custom emoji headers\n\n"
    "⚙️ Commands:\n"
    "• /desthealth · Check destination health & clean dead\n"
    "• /setchannel @target\n"
    "• /post 12\n"
    "• /headers (tap ➕ to add)\n\n"
    "⏸ All Stop / ▶ All Start — Pause or resume AUTOMATIC publishing only. "
    "Manual Approve taps still publish immediately."
)
BTN_PAUSE = "⏸ All Stop"
BTN_RESUME = "▶ All Start"
PAUSE_ON = "⏸ **System Paused** — Auto-publishing stopped."
PAUSE_OFF = "▶ **System Resumed** — Auto-publishing active."
PAUSE_ANSWER_ON = "⏸ System paused"
PAUSE_ANSWER_OFF = "▶ System resumed"
BTN_ASLEEP = "💤 I'm Asleep"
BTN_AWAKE = "☀️ I'm Awake"
ASLEEP_ON = "💤 **Asleep** — away footer enabled."
ASLEEP_OFF = "☀️ **Awake** — footer disabled."
ASLEEP_ANSWER_ON = "💤 Away mode on"
ASLEEP_ANSWER_OFF = "☀️ Away mode off"

# ═══════════════════════════ STATUS & REPORTS ═══════════════════════════
STATUS_HEADER = "📊 **System Telemetry**\n\n"
STATUS_TOTALS = (
    "• Suppliers Active: `{active}`\n"
    "• Processed: `{processed}`\n"
    "• Published: `{published}`\n"
    "• Pending: `{pending}`\n"
    "• Skipped: `{skipped}`\n\n"
)
STATUS_BY_SUPPLIER = "**By Supplier**\n{suppliers}\n\n"
STATUS_BY_REASON = "**Skip Breakdown**\n{reasons}"
STATUS_SUPPLIER_LINE = "• {name} — `{processed}` in / `{published}` pub / `{skipped}` skip"
STATUS_REASON_LINE = "• {reason}: `{count}`"
STATUS_NONE = "• None"
STATUS_PAUSED_BANNER = "⏸ **System Paused** — Auto-publishing stopped\n\n"

# ═══════════════════════════ REVIEW CARD (PENDING) ═══════════════════════════
REASONS = {
    "unknown_platform": "Platform Unidentified",
    "ai_unavailable": "AI Offline — Review Required",
    "ai_blocked_review": "⚠️ AI Flagged — Verify",
}
REASON_DEFAULT = "Manual Review"
REVIEW_HEADER = "📬 {reason} · #{id}"
REVIEW_HEADER_POS = "📬 {pos} — {reason} — Listing #{id}"
REVIEW_HEADER_NEXT = "📬 Next Review — Listing #{id}"
REVIEW_HEADER_NEXT_POS = "📬 Next Review · {pos} — Listing #{id}"
REVIEW_PRICE = "Price: DM"
REVIEW_ACTIONS = "Approve → Publish  |  Skip → Later"
REVIEW_CARD = (
    "{header}\n"
    + LINE + "\n"
    "Supplier: {source}\n"
    "Platform: {platform}\n"
    "{price}\n"
    + LINE + "\n"
    "{content}\n"
    + LINE + "\n"
    "{actions}"
)
REVIEW_PLATFORM_NONE = "—"
REVIEW_POS_REMAINING = "{remaining} remaining"
REVIEW_NOTICE = "📬 Review needed: Listing #{id}\n{source}\nTap /pending to review."
REVIEW_NOTICE_SOURCE = "Source: {source}\n"
NO_PENDING = "✅ Nothing pending."
NO_PENDING_PAGE = "✅ No listings pending approval right now."
QUEUE_FINISHED = "✅ Review queue finished — No pending listings right now."
BTN_PREVIEW = "👁️ Preview"
BTN_EDIT = "✏️ Edit"
BTN_APPROVE = "✅ Approve"
BTN_SKIP = "⏭️ Skip"
BTN_VIEW_BUYER = "View in Buyer channel"
BTN_VIEW_DEST = "View in my channel"

# ═══════════════════════════ PREVIEW & EDITING ═══════════════════════════
PREVIEW = "📄 **Preview of Listing #{id}**\n" + LINE + "\n{text}"
PREVIEW_FULL = "📄 **Preview of Listing #{id}**\nSource: {source} · Status: `{status}`\n" + LINE + "\n{text}"
PREVIEW_BUILDING = "📍 Building preview of Listing #{id}..."
PREVIEW_FAIL = "❌ Could not build preview."
PREVIEW_SEND_FAIL = "❌ Could not send preview."
PREVIEW_TOO_LONG = "⚠️ Preview #{id} exceeds maximum display length. Use `/pending`."
PREVIEW_USAGE = "Usage: `/preview 12`"
EDIT_PROMPT = (
    "✏️ **Edit Listing #{id}**\n\n"
    "{body}\n\n"
    "• Price & contact details are attached automatically."
)
EDIT_EMPTY = "_(no content yet — write the body lines)_"
EDIT_CANCELLED = "✏️ Edit cancelled."
EDIT_CANT = "Cannot edit — status is {status}"
EDIT_LOCKED = "⚠️ Cannot edit Listing #{id}: status is `{status}`. Draft discarded."
EDIT_PREVIEW_FAIL = "⚠️ Could not build preview. Try again or /cancel."
DRAFT = "✏️ **Draft for Listing #{id}**{price_note}\n" + LINE + "\n{text}"

# ═══════════════════════════ APPROVAL & SKIP ACTIONS ═══════════════════════════
ALREADY_DONE = "Already processed (status: {status})"
SKIPPED_ANSWER = "⏭️ Skipped"
SKIPPED_MSG = "Listing #{id} skipped."
APPROVE_PROCESSING = "Processing..."
APPROVE_STALE_ALERT = "⚠️ Listing #{id} is `{status}`. Draft discarded."
APPROVE_STALE = "⚠️ **Listing #{id} not published.**\nStatus changed to `{status}`."
APPROVE_DUP = "♻️ **Listing #{id} Duplicate**\nIdentical to #{twin} (already published)."
APPROVE_RACE = "♻️ **Listing #{id} Duplicate**\nMatched copy #{twin} published first."
APPROVE_BUSY = "⚠️ Listing #{id} is currently publishing."
APPROVE_SEND_FAIL = "⚠️ Listing #{id} approved but publishing failed.\nQueued for retry: {error}"
APPROVE_DONE = "✅ Listing #{id} Published! · Post #{post}"
APPROVE_QUEUED = "✅ Listing #{id} queued for republishing."

# ═══════════════════════════ LIVE ALERTS & NOTIFICATIONS ═══════════════════════════
PUBLISHED_ALERT = (
    "✅ **Published — Post {post}**\n"
    + LINE + "\n"
    "{platform}\n"
    "Supplier: {source}\n"
    + LINE + "\n"
)
PUBLISHED_POST = "#{n}"
PUBLISHED_LISTING = "Listing #{id}"
PLATFORM_UNKNOWN = "—"
FORMAT_LISTING = "{n}. **#{id}** | {platform} | DM | {source}\n   Status: `{status}` | {created}"
SKIP_NOTICE = "⏳ Skipped — {reason} — {source}{listing}"
SKIP_NOTICE_LISTING = " — Listing #{id}"
BTN_REREVIEW = "🔁 Re-review"
SKIP_ALERT = "⏭️ Skipped — {reason} — Listing #{id}{source}"
SKIP_ALERT_SOURCE = " from {source}"
DUP_BURST = (
    "♻️ **Duplicate burst collapsed**\n"
    "Kept Listing #{winner} from {source}.\n"
    "{count} identical dropped: {dropped}"
)

# ═══════════════════════════ SKIPPED LIST ═══════════════════════════
SKIPPED_TITLE = "🚫 **Skipped posts** — tap a button to open a post:\n"
SKIPPED_DROPPED_INFO = "\n_({dropped} more recent skip(s) not re-reviewable — no listing linked.)_"
SKIPPED_NONE_OPEN = "\n_None of the recent skips can be re-opened._"
SKIPPED_EMPTY = "✅ No skipped messages logged."
SKIP_NOT_FOUND = "Skip not found or already processed."
SKIP_INVALID = "Invalid skip ID."

# ═══════════════════════════ PUBLISHED LIST & POST LOOKUP ═══════════════════════════
PUBLISHED_TITLE = "✅ **Published Posts**\nTap to inspect:"
PUBLISHED_EMPTY = "📜 No published posts yet."
BTN_SEARCH_POST = "Search Post"
SEARCH_PROMPT = "🔍 Search post — send number (e.g. `12` or `/post 12`)."
POST_USAGE = "Usage: `/post 12`"
POST_NOT_FOUND = "❌ No published post found: `#{text}`."
POST_NOT_NUMBER = "⚠️ Invalid post number `#{text}`. Send a number like `12`."
POST_CARD = "**Post #{n}**\n" + LINE + "\nPlatform: {platform}\nSupplier: {source}"
BTN_SOLD = "🔴 Sold"
SOLD_REPLY = "STOP post #{post} Already Got"
SOLD_REPLY_PLATFORM = "STOP {platform} (post #{post}) Already Got"


def format_sold_reply(post: object, platform: Optional[str] = None) -> str:
    plat = str(platform or "").strip()
    if plat and plat != "?":
        return SOLD_REPLY_PLATFORM.format(platform=plat, post=post)
    return SOLD_REPLY.format(post=post)


SOLD_DONE = "🔴 Post #{post} marked as SOLD.\n• Replied in main channel\n• Deleted from {deleted} destination chat(s)."
SOLD_ALREADY = "⚠️ Post #{post} is already marked as sold."
SOLD_NOT_FOUND = "❌ Post #{post} not found."
SOLD_NOT_PUBLISHED = "⚠️ Post #{post} is not published yet."
SOLD_USAGE = "Usage: `/sold 12`"

# ═══════════════════════════ SOURCES (SUPPLIERS) ═══════════════════════════
SOURCES_EMPTY = "No sources configured yet."
SOURCES_MENU = "Tap a source below to manage it."
BTN_ADD_SOURCE = "➕ Add Source"
SOURCE_DETAIL = "{icon} **{name}**\n{status}ID: `{id}`\n\nSelect action:"
SOURCE_STATUS = "Status: `{state}`\n"
SOURCE_UNRESOLVED = "⚠️ **Unresolved — NOT monitored.**\n"
STATE_ACTIVE = "Active"
STATE_PAUSED = "Paused"
ID_UNRESOLVED = "unresolved"
BTN_PAUSE_ITEM = "⏸ Pause"
BTN_RESUME_ITEM = "▶ Resume"
BTN_DELETE_PERM = "🗑 Delete Permanently"
BTN_DELETE_YES = "✅ Delete"
BTN_CANCEL_X = "❌ Cancel"
SOURCE_NOT_FOUND = "Source not found."
SOURCE_DELETE_ASK = "🗑 **Delete {name}?**\nMonitoring stops. History preserved."
SOURCE_DELETED = "🗑 {name} deleted."
SOURCE_ADD_PROMPT = (
    "📡 **Add Source Channel**\n\n"
    "Send any of the following:\n"
    "• Username: `@channel`\n"
    "• Numeric ID: `-1001234567890`\n"
    "• Forwarded post from channel\n\n"
    "/cancel to return"
)
SOURCE_ADDED_BY_FWD_UNRESOLVED = (
    "✅ **Source Added (Unresolved)** — ID `{id}`.\n"
    "Add monitor account to the chat to begin listening."
)
SOURCE_ADDED_BY_FWD = "✅ **Source Added**: `{display}` (`{id}`)"
SOURCE_ADDED_RESOLVED = "✅ **Source Added**: `{display}` (`{id}`)"
SOURCE_BAD_FORWARD = (
    "Could not identify channel from forward.\n"
    "Send any of the following:\n"
    "• Username (@channel)\n"
    "• Forward made directly by channel\n"
    "• Channel ID"
)
SOURCE_BAD_REF = (
    "Could not read channel reference.\n"
    "Send any of the following:\n"
    "• Username: `@channel`\n"
    "• Numeric ID: `-100...`\n"
    "• Forwarded message"
)
SOURCE_UNRESOLVED_ASK = (
    "⚠️ **Could not resolve `{text}`.**\n\n"
    "Options:\n"
    "• Forward a message directly from chat\n"
    "• Tap **Add anyway** to retry in background\n\n"
    "Unresolved sources remain paused."
)
BTN_ADD_ANYWAY = "✅ Add anyway (retry later)"
SOURCE_STORED_UNRESOLVED = (
    "✅ **@{name} saved (unresolved)**\n"
    "Will retry in background.\n"
    "Tip: Forward any message directly from channel to resolve."
)
SOURCE_EXPIRED = "Request expired — tap ➕ Add Source again."
INVALID_REF = "Invalid reference."
SOURCE_REMOVE_USAGE = "Usage: `/removesupplier @channel_or_id`"
SOURCE_REMOVE_MISSING = "❌ Source **{name}** not found."
SOURCE_REMOVED = "🗑 @{name} deleted."
SOURCE_ADD_USAGE = "Usage: `/addsupplier @channel_or_id`"
SOURCE_ADD_PROMPT_PLAIN = "\nTap **➕ Add Source** to configure."
DEDUPE_NONE = "🧹 No duplicates found."
DEDUPE_TITLE = "🧹 **Merged {n} duplicate(s)**"
DEDUPE_LINE = "• `#{removed}` → `#{into}`"
RESEED_ASK = (
    "⚠️ **Re-import SOURCE_CHANNELS?**\n"
    ".env: {preview}\n"
    "Refreshes channels without removing history."
)
RESEED_NONE = "*(none)*"
BTN_RESEED_YES = "✅ Re-import"
RESEED_DONE = (
    "✅ Re-imported {n} channel(s) from .env.\n"
    "Seed marker set."
)
RESEED_FAIL = "Re-seed failed — check logs."

# ═══════════════════════════ DESTINATIONS (CHANNELS/GROUPS) ═══════════════════════════
DESTS_EMPTY = "No destinations configured.\n\nTap **➕ Add Destination** to forward posts."
DESTS_MENU = "\nTap a destination below to manage it.\n\n"
BTN_ADD_DEST = "➕ Add Destination"
DEST_NOTE_OFF = " (disabled)"
DEST_NOTE_DEAD = " — not delivering"
DEST_NOTE_FLAPPING = " — {pct:.0f}% failed"
DEST_NOTE_THROTTLED = " — {n} queued"
DEST_DETAIL_CARD = "{header}{health}\n\nSelect action:"
DEST_HEADER = "{icon} **{name}**\nStatus: {state}"
DEST_STATE_ON = "Active"
DEST_STATE_OFF = "Disabled"
DEST_HEALTH_THROTTLED = "\nDelivery: ⏳ rate-limited by Telegram\nQueued: {deferred}"
DEST_HEALTH_ATTEMPTS = "\nDelivery: {verdict}\nRecent: {successes}/{attempts} forwarded"
DEST_HEALTH_NO_ATTEMPTS = "\nDelivery: no forwards attempted yet"
DEST_HEALTH_LAST_OK = "\nLast success: `{when}`"
DEST_HEALTH_LAST_ERR = "\nLast error: `{reason}`"
DEST_V_DEAD = "❌ Inactive (banned or private)"
DEST_V_FLAPPING = "⚠️ Flapping — {pct:.0f}% failed"
DEST_V_FAILING = "⚠️ Failing — {pct:.0f}% failed"
DEST_V_OK = "✅ Healthy"
BTN_OPEN_CHAT = "🔗 Open Chat"
BTN_PAUSE_TRAILING = "⏸ Pause "
DEST_NOT_FOUND = "Destination not found."
DEST_DELETE_ASK = (
    "🗑 **Delete destination {name}?**\n\n"
    "Forwarding stops immediately. History preserved."
)
DEST_DELETE_PERM = "🗑 **Destination deleted.**\n{name} removed."
DEST_DELETED = "🗑 {name} deleted."
DEST_ADD_PROMPT = (
    "🎯 **Add Destination Group**\n\n"
    "Send any of the following:\n"
    "• Username: `@group`\n"
    "• Numeric ID: `-1001234567890`\n"
    "• Forwarded post from group\n\n"
    "/cancel to return"
)
DEST_ADD_BY_FWD = "✅ **Destination added**: {display} (`{id}`)"
DEST_ADDED = "✅ **Destination added**: {display}"
DEST_BAD_FORWARD = (
    "Could not identify group from forward.\n"
    "Send username or forward directly from group."
)
DEST_BAD_REF = (
    "Could not read chat reference.\n"
    "Send any of the following:\n"
    "• Username: `@group`\n"
    "• Numeric ID: `-100...`\n"
    "• Forwarded post"
)
DEST_UNVERIFIED_PROMPT = (
    "Could not verify access to **{name}**.\n\n"
    "Options:\n"
    "• Forward a message from group to verify ID\n"
    "• Tap **Add anyway** to retry on next post"
)
DEST_ADDED_UNVERIFIED = (
    "⚠️ **Destination added (unverified): {name}**\n"
    "Ensure the bot account is added to the chat."
)
DEST_ADD_ERROR = "⚠️ **Could not add destination**: {error}"
DEST_NOTHING = "Nothing to add."
DEST_ADD_USAGE = (
    "🎯 **Add Destination**\n\n"
    "Provide target:\n"
    "• Username: `@group`\n"
    "• Numeric ID: `-1001234567890`\n"
    "• Forwarded post from group\n\n"
    "Example: `/adddestination @group`"
)
DEST_AUTO_DETECTED = "➕ **New destination auto-detected**: {title} (`{ref}`)"
DEST_CANNOT_WRITE = "🚪 Left unwritable group: **{title}** (`{ref}`)"
SYNC_NO_CLIENT = "⚠️ Cannot sync: Forwarding client disconnected."
SYNC_SCANNING = "🔄 Scanning dialogs for channels/groups..."
SYNC_ADDED = "✅ Synced {n} new destination(s)."
SYNC_NONE = "✅ Destinations up to date."
SYNC_FAIL = "❌ Sync failed: {error}"
SYNC_AUTO = "🔄 Auto-synced **{n}** destination(s)."

# ═══════════════════════════ MAIN CHANNEL CONFIG ═══════════════════════════
MAIN_CHANNEL_CARD = (
    "📢 **Main Destination Channel**\n\n"
    "• Current: `{current}`\n\n"
    "Primary target for all approved posts.\n\n"
    "To change, send username (`@channel`), numeric ID (`-100...`), "
    "or forward any post from the new channel.\n\n"
    "Shortcut: `/setchannel @new_channel`"
)
MAIN_CHANNEL_PROMPT = (
    "📢 **Change Main Destination Channel**\n\n"
    "Send any of the following:\n"
    "• Username: `@channel`\n"
    "• Numeric ID: `-1001234567890`\n"
    "• Forwarded post from new channel\n\n"
    "/cancel to return"
)
MAIN_CHANNEL_UPDATED = (
    "✅ **Main Channel Updated**\n\n"
    "• New: {display}\n"
    "• Previous: `{previous}`"
)
MAIN_CHANNEL_UPDATED_UNVERIFIED = (
    "⚠️ **Main Channel Updated (Unverified)**\n\n"
    "• New: `{channel}`\n"
    "• Previous: `{previous}`\n\n"
    "Ensure bot is Admin with Post Messages permission."
)
MAIN_CHANNEL_BAD_REF = (
    "❌ **Invalid channel reference.**\n\n"
    "Send `@channel`, numeric ID, or forward a post from the channel."
)
BTN_MAIN_CHANNEL = "📢 Main Channel"

# ═══════════════════════════ CUSTOM EMOJI HEADERS ═══════════════════════════
HEADERS_EMPTY = "No headers saved yet (posts have no header line).\nTap ➕ Add Header to add {n} custom emoji."
HEADERS_MENU = "**{count} header{s} saved** — picked at random.\nTap 🗑 to delete."
BTN_ADD_HEADER = "➕ Add Header"
HEADER_ADD_PROMPT = (
    "🏷️ **Add Custom Header**\n\n"
    "Send exactly {n} custom emoji in one message:\n"
    "• Spelling WTB\n"
    "• No extra text or symbols\n\n"
    "/cancel to abort"
)
HEADER_DELETED = "🗑 Deleted **header #{id}**.\n\n"
HEADER_DELETED_ANSWER = "🗑 Deleted header #{id}"
HEADER_GONE = "Header already removed."
HEADER_NO_EMOJI = (
    "❌ **No Custom Emoji Detected**\n\n"
    "Requirements:\n"
    "• Send Telegram custom emoji directly\n"
    "• Plain text and standard emoji not accepted"
)
HEADER_WRONG_COUNT = (
    "❌ **Invalid Emoji Count**\n\n"
    "Received: **{got}** emoji\n"
    "Required: exactly **{need}** custom emoji (spelling WTB)"
)
HEADER_STRAY = (
    "❌ **Extra Text Detected** — other text (`{stray}`).\n\n"
    "Send **only** the {need} custom emoji."
)
HEADER_SAVE_FAIL = "❌ Could not save header. Try again."
HEADER_SAVED = "✅ Saved as **header #{n}**.\nPress the header to delete"
HEADER_DELETE_HINT = "Press the header to delete"

# ═══════════════════════════ SYSTEM & STARTUP ALERTS ═══════════════════════════
MANUAL_MODE_ONLINE = "🛠 **Manual mode online.** Ingestion and auto-publish paused."
STARTUP_DEDUPE = "🧹 Merged {merges} duplicate supplier row(s) ({removed} removed)."
BOT_ONLINE = "✅ Bot online · Monitoring active."
ZERO_RESOLVED_ALERT = (
    "🚨 **0 of {active_total} suppliers resolved — MONITORING NOTHING.**\n"
    "Check that the account is still in the source channels."
)
HEALTH_STATUS = (
    "🩺 **System Health & Heartbeat**\n"
    + LINE + "\n"
    "• Ingest Listener: {user_status}\n"
    "• Forward Sender: {fwd_status}\n"
    "• Admin Bot: {bot_status}\n"
    "• Active Sources: {sources}\n"
    "• Active Destinations: {dests}\n"
    "• Inbound Activity: {last_msg}"
)
HEALTH_ALERT = "⚠️ **System Health Alert**\n" + LINE + "\n{alert}"

# ═══════════════════════════ TELEGRAM BOT MENU ═══════════════════════════
BOT_MENU = [
    ("status", "System telemetry"),
    ("health", "System health check"),
    ("pending", "Review queue"),
    ("skipped", "Skipped logs"),
    ("sources", "Monitored sources"),
    ("published", "Published posts"),
    ("sold", "Mark sold: /sold 12"),
    ("headers", "Emoji headers"),
    ("post", "Lookup: /post 12"),
    ("help", "Commands & shortcuts"),
]
