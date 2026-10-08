"""Centralized text strings for the Admin Bot and system notifications.

All user-facing phrases are organized by screen/feature for easy editing.
Placeholders formatted as {name} are dynamically populated at runtime.
"""

# ═══════════════════════════ COMMON ═══════════════════════════
LINE = "━━━━━━━━━━"
ACCESS_DENIED = "⚠️ Access denied. Your ID: {user_id}"
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
HOME = "🏠 **Home**"
HOME_LANDING = "🏠 **Home** — select an option below:"
HELP = (
    "🤖 **Admin Bot**\n\n"
    "📊 Status — Today's report & activity\n"
    "⏳ Pending — Review pending listings\n"
    "📋 Sources — Manage supplier sources\n"
    "{destinations} — Forward destination groups\n"
    "✅ Published — Published posts & links\n"
    "🚫 Skipped — Dropped/filtered messages\n"
    "⏸ All Stop / ▶ All Start — Pause or resume AUTOMATIC publishing only. "
    "Manual Approve taps still publish immediately.\n\n"
    "/setchannel @channel — Change main destination channel\n"
    "/post 12 — Jump to post #12\n"
    "/headers — Manage custom emoji headers (tap ➕ to add)"
)
BTN_PAUSE = "⏸ All Stop"
BTN_RESUME = "▶ All Start"
PAUSE_ON = "⏸ **Paused** — auto-publishing stopped. Approve still publishes."
PAUSE_OFF = "▶ **Resumed** — auto-publishing active."
PAUSE_ANSWER_ON = "⏸ Automatic publishing paused"
PAUSE_ANSWER_OFF = "▶ Automatic publishing resumed"
BTN_ASLEEP = "💤 I'm Asleep"
BTN_AWAKE = "☀️ I'm Awake"
ASLEEP_ON = "💤 **Asleep** — new posts include footer: `Buyer away, back shortly`."
ASLEEP_OFF = "☀️ **Awake** — footer disabled."
ASLEEP_ANSWER_ON = "💤 Asleep — away footer enabled"
ASLEEP_ANSWER_OFF = "☀️ Awake — away footer disabled"

# ═══════════════════════════ STATUS & REPORTS ═══════════════════════════
STATUS_HEADER = "📊 **Today's Activity Report**\n\n"
STATUS_TOTALS = (
    "• Active Suppliers: `{active}`\n"
    "• Total Processed: `{processed}`\n"
    "• Published: `{published}`\n"
    "• Pending Approval: `{pending}`\n"
    "• Total Skipped: `{skipped}`\n\n"
)
STATUS_BY_SUPPLIER = "**By Supplier**\n{suppliers}\n\n"
STATUS_BY_REASON = "**Skip Breakdown**\n{reasons}"
STATUS_SUPPLIER_LINE = "• {name} — processed `{processed}` / published `{published}` / skipped `{skipped}`"
STATUS_REASON_LINE = "• {reason}: `{count}`"
STATUS_NONE = "• None"
STATUS_PAUSED_BANNER = "⏸ **PAUSED — automatic publishing is stopped**\n(manual Approve taps still publish)\n\n"

# ═══════════════════════════ REVIEW CARD (PENDING) ═══════════════════════════
REASONS = {
    "unknown_platform": "Platform Not Identified",
    "ai_unavailable": "AI Unavailable — Manual Review",
    "ai_blocked_review": "⚠️ AI Flagged Content — Verify",
}
REASON_DEFAULT = "Manual Review"
REVIEW_HEADER = "📬 {reason} — Listing #{id}"
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
PREVIEW_BUILDING = "📍 Building preview of Listing #{id}"
PREVIEW_FAIL = "❌ Could not build preview."
PREVIEW_SEND_FAIL = "❌ Could not send preview."
PREVIEW_TOO_LONG = "⚠️ Preview for listing **#{id}** is too long for inline display. Use `/pending` to view."
PREVIEW_USAGE = "Usage: `/preview 12`"
EDIT_PROMPT = (
    "✏️ **Edit Listing #{id}**\n"
    "**Current content** — edit and send back the FULL body:\n"
    ">{body}\n\n"
    "• Price & contact details are attached automatically.\n"
    "A revised preview will be shown before publishing."
)
EDIT_EMPTY = "_(no content yet — write the body lines)_"
EDIT_CANCELLED = "✏️ Edit cancelled."
EDIT_CANT = "Cannot edit — status is {status}"
EDIT_LOCKED = "⚠️ Cannot edit Listing #{id}: status is `{status}`. Draft discarded."
EDIT_PREVIEW_FAIL = "⚠️ Could not build preview from provided text. Please try again."
DRAFT = "✏️ **Draft for Listing #{id}** — review below, then Approve or Edit again.{price_note}\n" + LINE + "\n{text}"

# ═══════════════════════════ APPROVAL & SKIP ACTIONS ═══════════════════════════
ALREADY_DONE = "Already processed (status: {status})"
SKIPPED_ANSWER = "⏭️ Skipped"
SKIPPED_MSG = "Listing #{id} skipped."
APPROVE_PROCESSING = "Processing..."
APPROVE_STALE_ALERT = "⚠️ Listing #{id} is no longer editable (status: {status}). Draft discarded."
APPROVE_STALE = "⚠️ **Listing #{id} not published.**\nStatus is `{status}` — draft was discarded.\nCheck Pending list for current state."
APPROVE_DUP = "♻️ **Listing #{id} not published** — identical to #{twin}, which is already published.\nMarked as duplicate."
APPROVE_RACE = "♻️ **Listing #{id} not published** — identical copy (#{twin}) reached the channel first.\nMarked as duplicate."
APPROVE_BUSY = "⚠️ Listing #{id} is already being published elsewhere."
APPROVE_SEND_FAIL = "⚠️ Listing #{id} approved but publishing failed.\nQueued for retry: {error}"
APPROVE_DONE = "✅ Listing #{id} Published! · Post #{post}"
APPROVE_QUEUED = "✅ Listing #{id} queued for republishing."

# ═══════════════════════════ LIVE ALERTS & NOTIFICATIONS ═══════════════════════════
PUBLISHED_ALERT = (
    "✅ **Auto-Published — Post {post}**\n"
    + LINE + "\n"
    "{platform}\n"
    "Supplier: {source}\n"
    + LINE + "\n"
)
PUBLISHED_POST = "#{n}"
PUBLISHED_LISTING = "Listing #{id}"
PLATFORM_UNKNOWN = "Platform ?"
FORMAT_LISTING = "{n}. **#{id}** | {platform} | DM | {source}\n   Status: `{status}` | {created}"
SKIP_NOTICE = "⏳ Skipped — {reason} — {source}{listing}"
SKIP_NOTICE_LISTING = " — Listing #{id}"
BTN_REREVIEW = "🔁 Re-review"
SKIP_ALERT = "⏭️ Skipped — {reason} — Listing #{id}{source}"
SKIP_ALERT_SOURCE = " from {source}"
DUP_BURST = (
    "♻️ **Duplicate burst collapsed**\n"
    "Kept Listing #{winner} from {source}.\n"
    "{count} identical cop{suffix} dropped: {dropped}"
)

# ═══════════════════════════ SKIPPED LIST ═══════════════════════════
SKIPPED_TITLE = "🚫 **Skipped posts** — tap a button to open a post:\n"
SKIPPED_DROPPED_INFO = "\n_({dropped} more recent skip(s) not re-reviewable — no listing linked.)_"
SKIPPED_NONE_OPEN = "\n_None of the recent skips can be re-opened._"
SKIPPED_EMPTY = "✅ No skipped messages logged."
SKIP_NOT_FOUND = "Skip not found or already processed."
SKIP_INVALID = "Invalid skip ID."

# ═══════════════════════════ PUBLISHED LIST & POST LOOKUP ═══════════════════════════
PUBLISHED_TITLE = " ✅ **Published posts** — tap a button to open a post:\n"
PUBLISHED_EMPTY = "📜 No published posts yet."
BTN_SEARCH_POST = "Search Post"
SEARCH_PROMPT = "Search published posts — send the post number (e.g. `12` or `/post 12`)."
POST_USAGE = "Usage: `/post 12`"
POST_NOT_FOUND = "❌ No published post found with number `#{text}`."
POST_NOT_NUMBER = "⚠️ `{text}` is not a valid post number. Send a number like `12` (or `/post 12`)."
POST_CARD = "**Post #{n}**\n━━━━━━━━━\nPlatform: {platform}\nSupplier: {source}"

# ═══════════════════════════ SOURCES (SUPPLIERS) ═══════════════════════════
SOURCES_EMPTY = "No sources configured yet."
SOURCES_MENU = "Tap a source below to manage it."
BTN_ADD_SOURCE = "➕ Add Source"
SOURCE_DETAIL = "{icon} **{name}**\n{status}ID: `{id}`\n\nWhat would you like to do?"
SOURCE_STATUS = "Status: `{state}`\n"
SOURCE_UNRESOLVED = "⚠️ **Unresolved — NOT monitored.** Re-add with username or forward a message from the channel.\n"
STATE_ACTIVE = "Active"
STATE_PAUSED = "Paused"
ID_UNRESOLVED = "unresolved"
BTN_PAUSE_ITEM = "⏸ Pause"
BTN_RESUME_ITEM = "▶ Resume"
BTN_DELETE_PERM = "🗑 Delete Permanently"
BTN_DELETE_YES = "✅ Delete"
BTN_CANCEL_X = "❌ Cancel"
SOURCE_NOT_FOUND = "Source not found."
SOURCE_DELETE_ASK = "🗑 **Delete {name}?**\nStops monitoring. History is kept."
SOURCE_DELETED = "🗑 {name} deleted."
SOURCE_ADD_PROMPT = (
    "➕ **Add Source Channel**\n\n"
    "Send one of the following:\n"
    "• Username: `@channel`\n"
    "• Numeric ID: `-1001234567890`\n"
    "• **Forward a message from the channel** (recommended for private chats)"
)
SOURCE_ADDED_BY_FWD_UNRESOLVED = (
    "✅ **Source added by forward** — ID `{id}`.\n"
    "⚠️ Monitor account cannot see this chat yet. Add the monitor account "
    "as a member to begin listening automatically."
)
SOURCE_ADDED_BY_FWD = "✅ **Source added by forward**: `{display}` (ID `{id}`)"
SOURCE_ADDED_RESOLVED = "✅ **Source added**: `{display}` (ID `{id}`)"
SOURCE_BAD_FORWARD = (
    "Could not identify channel from forwarded message. "
    "Forward a post made directly by the channel, or send a username/ID."
)
SOURCE_BAD_REF = (
    "Could not read channel reference. "
    "Send a username (`@channel`), numeric ID (`-100...`), or forward a post."
)
SOURCE_UNRESOLVED_ASK = (
    "⚠️ **Could not resolve reference (`{text}`).**\n\n"
    "• If private, forward any message directly **from the channel/group**.\n"
    "• Or select **Add anyway** to queue background resolution.\n\n"
    "Unresolved sources remain paused until resolved."
)
BTN_ADD_ANYWAY = "✅ Add anyway (retry later)"
SOURCE_STORED_UNRESOLVED = (
    "✅ Source **@{name}** saved as **unresolved**.\n"
    "Resolution will retry automatically in the background.\n\n"
    "Tip: Forward any message directly from the channel to resolve immediately."
)
SOURCE_EXPIRED = "Request expired — tap ➕ Add Source to start again."
INVALID_REF = "Invalid reference."
SOURCE_REMOVE_USAGE = "Usage: `/removesupplier @channel_or_id`"
SOURCE_REMOVE_MISSING = "❌ Source **{name}** not found."
SOURCE_REMOVED = "🗑 @{name} deleted."
SOURCE_ADD_USAGE = "Usage: `/addsupplier @channel_or_id`"
SOURCE_ADD_PROMPT_PLAIN = "\nTap **➕ Add Source** to configure your first one."
DEDUPE_NONE = "🧹 No duplicates found."
DEDUPE_TITLE = "🧹 **Merged {n} duplicate(s)**"
DEDUPE_LINE = "• `#{removed}` → `#{into}`"
RESEED_ASK = (
    "⚠️ **Re-import SOURCE_CHANNELS?**\n"
    ".env: {preview}\n"
    "Adds/refreshes channels without removing existing history."
)
RESEED_NONE = "*(none)*"
BTN_RESEED_YES = "✅ Re-import"
RESEED_DONE = (
    "✅ Re-imported {n} channel(s) from SOURCE_CHANNELS.\n"
    "Seed marker set — .env will not overwrite future changes."
)
RESEED_FAIL = "Re-seed failed — check logs for details."

# ═══════════════════════════ DESTINATIONS (CHANNELS/GROUPS) ═══════════════════════════
DESTS_EMPTY = "No destinations configured — \n\nTap **➕ Add Destination** to forward posts."
DESTS_MENU = "\nTap a destination below to manage it.\n\n"
BTN_ADD_DEST = "➕ Add Destination"
DEST_NOTE_OFF = " (disabled)"
DEST_NOTE_DEAD = " — not delivering"
DEST_NOTE_FLAPPING = " — {pct:.0f}% failed"
DEST_NOTE_THROTTLED = " — {n} queued"
DEST_DETAIL_CARD = "{header}{health}\n\nWhat would you like to do?"
DEST_HEADER = "{icon} **{name}**\nStatus: {state}"
DEST_STATE_ON = "Active"
DEST_STATE_OFF = "Disabled"
DEST_HEALTH_THROTTLED = "\nDelivery: ⏳ waiting on Telegram rate limit\nQueued and retrying automatically: {deferred}\nThis is not a problem with this destination."
DEST_HEALTH_ATTEMPTS = "\nDelivery: {verdict}\nRecent: {successes}/{attempts} forwarded"
DEST_HEALTH_NO_ATTEMPTS = "\nDelivery: no forwards attempted yet"
DEST_HEALTH_LAST_OK = "\nLast success: `{when}`"
DEST_HEALTH_LAST_ERR = "\nLast error: `{reason}`"
DEST_V_DEAD = "❌ Not delivering at all — likely banned or private"
DEST_V_FLAPPING = "⚠️ Intermittent — {pct:.0f}% of recent forwards failed"
DEST_V_FAILING = "⚠️ {pct:.0f}% of recent forwards failed"
DEST_V_OK = "✅ Delivering normally"
BTN_OPEN_CHAT = "🔗 Open Chat"
BTN_PAUSE_TRAILING = "⏸ Pause "
DEST_NOT_FOUND = "Destination not found."
DEST_DELETE_ASK = (
    "🗑 **Delete destination {name}?**\n\n"
    "Forwarding to this group will stop immediately. Existing forward history stays."
)
DEST_DELETE_PERM = "🗑 **Destination permanently deleted.**\n{name} removed. History kept."
DEST_DELETED = "🗑 {name} deleted."
DEST_ADD_PROMPT = (
    "➕ **Add Destination Group**\n\n"
    "Send one of the following:\n"
    "• Username: `@group`\n"
    "• Numeric ID: `-1001234567890`\n"
    "• **Forward a message from the group** (recommended for private groups)"
)
DEST_ADD_BY_FWD = "✅ **Destination added by forward**: {display} (ID {id})\nFuture posts will be forwarded here."
DEST_ADDED = "✅ **Destination added**: {display}\nFuture posts will be forwarded here."
DEST_BAD_FORWARD = (
    "Could not identify group from forwarded message. "
    "Forward a post made directly by the group, or send a username/ID."
)
DEST_BAD_REF = (
    "Could not read chat reference. "
    "Send a group username (`@group`), numeric ID (`-100...`), or forward a post."
)
DEST_UNVERIFIED_PROMPT = (
    "Could not verify access to **{name}**.\n\n"
    "• Forward a message **from the group** to detect ID, or\n"
    "• Add anyway to retry delivery on future posts (requires bot membership)."
)
DEST_ADDED_UNVERIFIED = (
    "⚠️ **Destination added (unverified): {name}**\n"
    "Delivery will be retried on future posts. Ensure the bot account is a member."
)
DEST_ADD_ERROR = "⚠️ **Could not add destination**: {error}\nProvide a username (`@group`) or numeric ID (`-100...`)."
DEST_NOTHING = "Nothing to add."
DEST_ADD_USAGE = (
    "**Add a destination group** — where published posts will be forwarded:\n\n"
    "• Username: `@group`\n"
    "• Numeric ID: `-1001234567890`\n"
    "• **Forward a message from the group**\n\n"
    "Example: `/adddestination @group`"
)
DEST_AUTO_DETECTED = (
    "➕ **New destination auto-detected**: {title} (`{ref}`)\n"
    "Joined via forward account. Future posts will be forwarded here."
)
SYNC_NO_CLIENT = "⚠️ Cannot sync: Forwarding client is not connected."
SYNC_SCANNING = "🔄 Scanning dialogs for supergroups/channels..."
SYNC_ADDED = "✅ Synced {n} new destination group(s)."
SYNC_NONE = "✅ Destination groups already up to date."
SYNC_FAIL = "❌ Sync failed: {error}"
SYNC_AUTO = "🔄 Auto-synced **{n}** destination group(s) from forwarding account."

# ═══════════════════════════ MAIN CHANNEL CONFIG ═══════════════════════════
MAIN_CHANNEL_CARD = (
    "📢 **Main Destination Channel**\n\n"
    "• Current: `{current}`\n\n"
    "This is the primary channel where listings are published before forwarding.\n\n"
    "To change it, send a channel username (`@channel`), numeric ID (`-100...`), "
    "or forward any message from your new channel here.\n\n"
    "Or use: `/setchannel @new_channel`"
)
MAIN_CHANNEL_PROMPT = (
    "📢 **Change Main Destination Channel**\n\n"
    "Send the new channel:\n"
    "• Username: `@channel`\n"
    "• Numeric ID: `-1001234567890`\n"
    "• **Or forward any post from the new channel**\n\n"
    "Example: `/setchannel @mychannel`"
)
MAIN_CHANNEL_UPDATED = (
    "✅ **Main Channel Updated!**\n\n"
    "• New channel: {display}\n"
    "• Previous: `{previous}`\n\n"
    "All new approved and auto-published listings will be posted here."
)
MAIN_CHANNEL_UPDATED_UNVERIFIED = (
    "⚠️ **Main Channel Updated (Unverified)**\n\n"
    "• New channel: `{channel}`\n"
    "• Previous: `{previous}`\n\n"
    "Could not verify channel access right now. Ensure your user client or bot account is an administrator with **Post Messages** permission in that channel."
)
MAIN_CHANNEL_BAD_REF = (
    "❌ **Invalid channel reference.**\n\n"
    "Provide a channel username (`@channel`), numeric ID (`-100...`), "
    "or forward a message directly from the channel."
)
BTN_MAIN_CHANNEL = "📢 Main Channel"

# ═══════════════════════════ CUSTOM EMOJI HEADERS ═══════════════════════════
HEADERS_EMPTY = "No headers saved yet (posts have no header line).\nTap ➕ Add Header to add {n} custom emoji."
HEADERS_MENU = "**{count} header{s} saved** — picked at random.\nTap 🗑 to delete."
BTN_ADD_HEADER = "➕ Add Header"
HEADER_ADD_PROMPT = "Send the {n} custom emoji in one message.\n"
HEADER_DELETED = "🗑 Deleted **header #{id}**.\n\n"
HEADER_DELETED_ANSWER = "🗑 Deleted header #{id}"
HEADER_GONE = "That header is already gone."
HEADER_NO_EMOJI = "❌ No **custom emoji** found in message.\n\nSend Telegram custom emoji directly, not text or forwards."
HEADER_WRONG_COUNT = "❌ Message has **{got}** custom emoji — exactly **{need}** required.\nSend {need} custom emoji spelling **WTB**."
HEADER_STRAY = "❌ Your message also contained other text (`{stray}`).\n\nSend **only** the {need} custom emoji."
HEADER_SAVE_FAIL = "❌ Could not save header. Nothing was changed — try again."
HEADER_SAVED = "✅ Saved as **header #{n}**.\nPress the header to delete"
HEADER_DELETE_HINT = "Press the header to delete"

# ═══════════════════════════ SYSTEM & STARTUP ALERTS ═══════════════════════════
MANUAL_MODE_ONLINE = (
    "🛠 **Manual mode online.** Ingestion and auto-publish are OFF. "
    "Approvals publish immediately; /skipped is available."
)
STARTUP_DEDUPE = (
    "🧹 Merged {merges} duplicate supplier row(s) ({removed} removed). "
    "No action needed — listings were preserved on the surviving row."
)
BOT_ONLINE = "✅ Bot is online and monitoring suppliers."
ZERO_RESOLVED_ALERT = (
    "🚨 **0 of {active_total} suppliers resolved — the bot is running but "
    "MONITORING NOTHING.** Check that the account is still in the source "
    "channels (kicked? deleted? logged out?) or fix the sources in the "
    "admin bot's Sources menu."
)

# ═══════════════════════════ TELEGRAM BOT MENU ═══════════════════════════
BOT_MENU = [
    ("status", "Today's report"),
    ("pending", "Listings to review"),
    ("skipped", "Skipped messages"),
    ("sources", "Manage sources"),
    ("published", "Published posts"),
    ("headers", "Emoji headers"),
    ("post", "Find a post: /post 12"),
    ("help", "Buttons & shortcuts"),
]
