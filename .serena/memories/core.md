# Core Architecture & Invariants

Telegram channel monitor & auto-republisher engine with Telethon (MTProto).
Dual client architecture: User Client (main.py) for listening & forwarding, Admin Bot Client (admin_bot.py) for inline review cards and configuration.

## Key Subsystems
- Ingestion & pipeline: `pipeline/` (`parser.py`, `filters.py`, `rules.py`)
- Background workers: `main.py` (`held_listings_worker`, `approved_listings_worker`, `forwarding_worker`, `retention_worker`)
- Storage & SQLite repositories: `storage/` (`db.py`, `repositories/`)
- Multi-account cascading forwarders: `services/forwarders_manager.py`
- AI Rephraser: `services/ai_rephraser.py` (Groq primary, OpenRouter fallback, SQLite ai_cache)

## Strict Invariants
- Buyer-Only framing: Destination posts ALWAYS as buyer (WTB), seller terms never leak.
- Prices Never Render: Static `Price DM` footer; prices negotiated in direct messages.
- Payment Proofs never auto-publish (route to admin review).
- Custom Emoji UTF-16 offsets: MTProto offsets calculated using UTF-16 code units.
- Destinations must ONLY be groups, never broadcast channels. Must be writable by at least one forward account.

See:
- Tech Stack details: `mem:tech_stack`
- Project conventions & rules: `mem:conventions`
- Commands for dev/test: `mem:suggested_commands`
- Definition of Done / task completion: `mem:task_completion`
