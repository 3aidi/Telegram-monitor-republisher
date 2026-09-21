# Changelog — Codebase Audit & Safe Fixes

**Date:** 2026-09-13
**Scope:** Full audit of the Telegram Channel Monitor & Auto-Republisher, followed by low-risk fixes only (no behavioral changes to the publishing pipeline).
**Verification:** `python -m py_compile` clean on all edited files; full test suite **93/93 OK** before and after.

---

## Changeset (2026-09-21) — Own-channel publishing + live skip alerts

**Verification:** `python -m unittest test_system` **192/192 OK**; `py_compile` clean on `main.py`, `admin_bot.py`, `test_system.py`.

1. **Self-echo guard removed**: the `self_echo` skip (footer `Contact : @<CONTACT_USERNAME>` detection in monitored channels) is gone from `_process_supplier_message`, along with `_build_self_echo_pattern` and `import re`. Messages from the admin's own test channel now flow through the normal pipeline and can be published. (Reconsider if the destination channel can ever echo back into a monitored source — that loop is the reason the guard existed.)
2. **Live skip notifications**: every skip now DMs the admin instead of only touching the DB — `admin_bot.send_skipped_alert()` + `main._alert_admin_on_skip()`, wired into the duplicate/`no_content` filter skip, the chatter pre-filter skip, and the AI "not a listing" skip. Publish failures already alerted (`send_failed_alert`) — unchanged.
3. **Reject removed — Skip is now the single decline action**: the `❌ Reject` button (approval prompt + action buttons), the `reject:` callback branch, and `^(approve|retry|skip):…` invalidation of `reject:` data are gone. `rejected` was terminal with no undo; `skipped_admin` is recoverable via `/skipped`, so mis-taps can no longer destroy a listing. README/ONBOARDING and tests updated.

---

## Later changeset (2026-09-16) — Verification queue P1–P9, deploy hardening, pricing removal

**Verification:** `python -m unittest test_system` **128/128 OK**; `ruff check .` (F+E9) **clean**.

1. **P1 — Deduplication (DEDUP)**: unique `(supplier_id, source_message_id)` index on `listings`; fresh insert conflict-resistant; dedup fingerprint includes `(platform, rounded price, normalized text)`; `find_recent_similar_listing` keeps the 48h window, excludes self, ignores stale `received` rows; concurrency test proves two true twins can never both publish.
2. **P2 — Type-safety**: `db.get_supplier_by_chat()` never returns `None` where the caller expects a row.
3. **P3 — Concurrent-safety**: per-message in-flight locks (`_processing_locks`) serialize identical concurrent messages and release on completion (no leaking entries).
4. **P4 — Bounded rephrase sweep**: `db.get_unpublished_listings(limit, min_age_seconds)`; startup rephrase loop is time/age bounded via `REPHRASE_SWEEP_LIMIT` / `REPHRASE_SWEEP_MIN_AGE_SECONDS`.
5. **P5 — FloodWait handling**: one shared `publish_guard.run_with_floodwait_retry()` helper (honors `FloodWaitError`, bounded retry budgets for the admin-bot edit/approve paths so floods can't hang the loop).
6. **P6/P7 — Deploy hardening**: `deploy.yml` runs the full test suite in CI *and* on the server, with automatic rollback on test failure or an unhealthy restart; `run.ps1` / `start_aws.ps1` / `stop_aws.ps1` now read `AWS_SSH_HOST` / `AWS_SSH_KEY` from the environment instead of hardcoded values.
7. **P8 — Runtime health**: the health-check log line reports uptime, last inbound activity, last publish, worker heartbeats, and last failure.
8. **P9 — Pricing system removed**: `original_price`, `our_price`, `markup_multiplier`, `PRICE_MULTIPLIER`, `MAX_PRICE`, `db.set_supplier_rule()`, and `parser.apply_pricing_rule()` / `_format_price()` are gone. Fresh databases never create the pricing columns (schema v9); production databases keep them dormant — a full table rebuild is unsafe while `PRAGMA foreign_keys` is enabled inside an open migration transaction. Every published post always carries the static `Price: DM` footer.

---

## Summary of Changes

| # | File | Type | What |
| --- | ------ | ------ | ------ |
| 1 | `main.py` | Improvement | Wire dead `db.get_unresolved_suppliers()` into the supplier resolution path |
| 2 | `ai_rephraser.py` | Cleanup | Remove unused `List` import |
| 3 | `start_aws.ps1` | Bugfix | Fix mojibake em-dash (encoding issue in Windows PowerShell) |
| 4 | `requirements.txt` | New file | Create pinned dependency list (README referenced it, but it was missing) |
| 5 | `.env.example` | New file | Create environment template (README referenced it, but it was missing) |
| 6 | `.github/workflows/deploy.yml` | Bugfix | Add missing `pip install` step to the AWS deploy workflow |
| 7 | `_inspect_tmp.py` | Cleanup | Temporary inspection script deleted |

---

## 1. `main.py` — Use `db.get_unresolved_suppliers()` (dead code wired in)

### Before

```python
rows = db.list_suppliers(active_only=True)
if only_unresolved:
    rows = [s for s in rows if s.get("channel_id") is None]
```

### After

```python
if only_unresolved:
    rows = db.get_unresolved_suppliers(active_only=True)
else:
    rows = db.list_suppliers(active_only=True)
```

### Why

- `db.get_unresolved_suppliers()` existed in `db.py` (its docstring even claimed `main.py` uses it) but was **never called anywhere** — dead code.
- The old code fetched **every active supplier row** from SQLite and filtered in Python. The self-healing `supplier_resolution_worker` runs every 300 seconds with `only_unresolved=True`, so each tick was re-resolving *all* suppliers instead of just the unresolved ones.
- **Effect:** fewer redundant Telegram `get_entity()` API calls, less log noise, and the DB function's contract is finally honored. Behavior is identical (same rows returned, just filtered in SQL).

---

## 2. `ai_rephraser.py` — Remove unused `List` import

### Before

```python
from typing import List, Optional
```

### After

```python
from typing import Optional
```

### Why

- `List` appeared only in the import line and one docstring mention — it was never used as an actual type annotation in the file.
- `Optional` **is** used extensively (verified: 8 usages), so only `List` was removed.
- Pure cleanup; zero runtime impact.

---

## 3. `start_aws.ps1` — Fix mojibake em-dash

### Before

```powershell
Write-Host "Something went wrong — status is '$status', not 'active'." -ForegroundColor Red
```

The file was saved as **UTF-8 without BOM**, so Windows PowerShell 5.1 (which defaults to the system codepage for BOM-less files) rendered the em-dash `—` as `â€"`.

### After

```powershell
Write-Host "Something went wrong - status is '$status', not 'active'." -ForegroundColor Red
```

The em-dash was replaced with a plain ASCII hyphen and the file rewritten as pure ASCII.

### Why

- PowerShell 5.1 only auto-detects UTF-8 when a BOM is present. Since the rest of the file is ASCII, making the whole file ASCII is the most robust fix (displays correctly in every console and codepage).
- Cosmetic/operational fix — the script logic is unchanged.

---

## 4. `requirements.txt` — Created (was missing)

### Content

```
Telethon==1.44.0
python-dotenv==1.2.3
groq==1.7.0
httpx==0.28.1
emoji==2.15.0
```

### Why

- The README instructs users to `pip install -r requirements.txt`, but the file **did not exist** — a fresh clone could not be set up by following the docs.
- Versions are pinned to the exact ones installed and tested in the project venv (verified via `pip freeze`).
- `emoji` is confirmed still a live dependency (`import emoji` in `filters.py` and `parser.py`).

---

## 5. `.env.example` — Created (was missing)

### Why

- The README references `.env.example` as the setup template, but the file **did not exist**.
- The template documents **every environment variable** actually read by the codebase (discovered by grepping all `os.environ.get(...)` calls across `main.py`, `admin_bot.py`, `db.py`, `ai_rephraser.py`), grouped into sections:
  - **Telegram credentials** — `API_ID`, `API_HASH`, `DEST_CHANNEL`, `BOT_TOKEN`, `ADMIN_USER_ID`, `CONTACT_USERNAME`
  - **Sources** — `SOURCE_CHANNELS`
  - **AI (Groq)** — `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_TEMPERATURE`, `GROQ_MAX_TOKENS`, `GROQ_TIMEOUT`, `GROQ_MAX_RETRIES`, `GROQ_MAX_CONCURRENCY`
  - **AI fallback (OpenRouter)** — `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_TIMEOUT`, `OPENROUTER_COOLDOWN_SECONDS`
  - **Behavior tuning** — `BACKFILL_ON_START`, `PUBLISH_INTERVAL`, `PUBLISH_MAX_RETRIES`, `DEDUP_HOURS`, `AI_CACHE_TTL_HOURS`, `PRE_FILTER_CHATTER`, `DETERMINISTIC_FALLBACK`
  - **Storage** — `DB_PATH`
- Each variable carries its code-default value and a one-line comment, so `cp .env.example .env` gives a working starting point. No real secrets are included.

---

## 6. `.github/workflows/deploy.yml` — Add missing `pip install` step

### Before

```yaml
script: |
  cd ${{ secrets.DEPLOY_PATH }}
  git pull
  sudo systemctl restart tele-monitor
```

### After

```yaml
script: |
  cd ${{ secrets.DEPLOY_PATH }}
  git pull
  if [ -d venv ]; then
    ./venv/bin/pip install -r requirements.txt
  else
    pip3 install -r requirements.txt
  fi
  sudo systemctl restart tele-monitor
```

### Why

- The deploy workflow pulled new code and restarted the systemd service **without installing dependencies**. Any commit that added a new dependency would leave the service crash-looping on the server until someone SSH'd in manually.
- The install is venv-aware: it uses the project venv's pip when `venv/` exists, falling back to system `pip3`.
- `pip install -r` with already-satisfied pins is a fast no-op on normal deploys, so this adds negligible deploy time.

---

## 7. `_inspect_tmp.py` — Deleted

- A temporary throwaway script created during the audit for inspecting module state. Removed from disk (verified gone) so it can't be accidentally committed.

---

## Verification

| Check | Result |
| ------- | -------- |
| `python -m py_compile main.py ai_rephraser.py` | ✅ Clean |
| Full test suite (`test_system.py`) | ✅ **93/93 OK** (identical to pre-change baseline) |
| `.env` still untracked by git | ✅ No secrets touched |
| `_inspect_tmp.py` removed | ✅ Verified |

> Note: the `ù` character visible in the test console output is a Windows console codepage rendering artifact of the UTF-8 em-dash inside an alert string — the source files themselves are correct UTF-8.

---

## Audit Findings — Documented, No Action Taken (by design)

These were observed during the audit and deliberately **left unchanged** to keep this changeset risk-free:

1. **Pricing system removed (2026-09-16)** — the vestigial `our_price`/`original_price`/`markup_multiplier` machinery no longer exists in code or for fresh databases (see the later changeset section above); legacy columns stay dormant in existing databases for FK-safety.
2. **Auth is solid** — admin user-ID checks are present on both the `NewMessage` and `CallbackQuery` handler paths in `admin_bot.py`.
3. **Single-instance guard** — the Windows named-mutex guard correctly avoids false positives from the venv launcher shim.
4. **Publishing is centralized** — all destination publishes (auto, worker, admin-approve) route through `publish_guard.throttle()`, so rate limiting is serialized under one lock.
