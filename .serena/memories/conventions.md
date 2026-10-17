# Conventions & Design Patterns

## Repository Architecture
- Layered architecture: `core/`, `domain/`, `pipeline/`, `services/`, `storage/`, `docs/`, `scripts/`.
- Root backward-compatibility shims (`db.py`, `parser.py`, `texts.py`, `filters.py`, `models.py`, `countries.py`, `ai_rephraser.py`, `publish_guard.py`) are intentionally maintained so callers can `import db` or `import parser` without breaking.
- Repositories: All database access goes through `storage/repositories/` functions coordinated by `storage/db.py`.
- Threading & Async: Sync database operations run in async contexts via `await db.run_async(...)` or `asyncio.to_thread`.
- Telegram UI Text: All user-facing strings, button labels, and alerts must be defined in `core/texts.py`.
