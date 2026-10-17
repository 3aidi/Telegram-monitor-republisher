# Tech Stack & Dependencies

- Runtime: Python 3.10+ (Active runtime: Python 3.14 on Windows, Python 3.12/3.14 on Ubuntu AWS EC2).
- Telegram MTProto Framework: `telethon` (~1.40+).
- Database: SQLite with WAL mode (`PRAGMA journal_mode=WAL`), foreign keys (`PRAGMA foreign_keys=ON`). Schema migrations managed sequentially in `storage/repositories/base.py`.
- AI LLM APIs: `groq` (primary Llama-3.3-70b-versatile), `httpx` (OpenRouter fallback), cached in `ai_cache` table.
- QR Codes & Imaging: `qrcode`, `pillow`.
- Testing: `pytest`, `unittest`.
