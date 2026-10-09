"""Shared runtime metrics, health reporting, and worker state coordination.

Centralizes runtime state shared between ingestion, worker tasks, and the admin bot,
preventing circular imports between main.py and admin_bot.py.
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEST_HEALTH_REPORT_SECONDS: float = 24 * 3600.0
STUCK_WORKER_ALERT_SECONDS: float = 120.0

# In-memory operational metrics
_WORKER_HEARTBEATS: Dict[str, float] = {}
_WORKER_FAILURES: Dict[str, List[float]] = {}
_STUCK_ALERTED: Dict[str, float] = {}
_LAST_ACTIVITY_AT: Optional[float] = None
_LAST_PUBLISH_AT: Optional[float] = None
_LAST_FAILURE: Optional[Tuple[float, str]] = None
_LAST_DEST_HEALTH_REPORT: float = 0.0


def mark_activity() -> None:
    global _LAST_ACTIVITY_AT
    _LAST_ACTIVITY_AT = time.monotonic()


def mark_publish() -> None:
    global _LAST_PUBLISH_AT
    _LAST_PUBLISH_AT = time.monotonic()


def mark_worker_heartbeat(name: str) -> None:
    _WORKER_HEARTBEATS[name] = time.monotonic()


def note_worker_failure(name: str) -> None:
    entry = _WORKER_FAILURES.get(name)
    if entry is None:
        _WORKER_FAILURES[name] = [time.monotonic(), 1.0]
    else:
        entry[1] += 1.0


def clear_worker_failure(name: str) -> None:
    if _WORKER_FAILURES.pop(name, None) is not None:
        _STUCK_ALERTED.pop(name, None)


def record_failure(where: str, err: Any) -> None:
    global _LAST_FAILURE
    msg = str(err)
    _LAST_FAILURE = (time.monotonic(), f"{where}: {msg[:200]}")
    note_worker_failure(where)


def get_stuck_workers(threshold_seconds: float = STUCK_WORKER_ALERT_SECONDS) -> List[Tuple[str, int, float]]:
    """Workers failing continuously for longer than the threshold."""
    now = time.monotonic()
    stuck = []
    for name, (first_at, count) in _WORKER_FAILURES.items():
        elapsed = now - first_at
        if elapsed >= threshold_seconds:
            stuck.append((name, int(count), elapsed))
    return sorted(stuck, key=lambda item: -item[2])


def runtime_health_suffix() -> str:
    """Short summary of process-level liveness for the health log line."""
    upstream = max(
        [t for t in (_LAST_ACTIVITY_AT, _LAST_PUBLISH_AT) if t is not None], default=0
    )
    alive = (
        f"inbound-activity-{(time.monotonic() - upstream):.0f}s-ago"
        if upstream
        else "no-inbound-activity-yet"
    )
    last_pub = (
        f"last-publish-{(time.monotonic() - _LAST_PUBLISH_AT):.0f}s-ago"
        if _LAST_PUBLISH_AT
        else "no-publish-yet"
    )
    beats = ",".join(
        f"{name}-{(time.monotonic() - ts):.0f}s" for name, ts in sorted(_WORKER_HEARTBEATS.items())
    ) or "no-worker-heartbeats"
    failure = (
        f"last-failure={_LAST_FAILURE[1]}"
        if _LAST_FAILURE
        else "no-failures"
    )
    return f" | {alive} | {last_pub} | {beats} | {failure}"


def get_admin_chat_id(admin_user_id: int = 0) -> Optional[int]:
    """Resolve which chat to DM for operational alerts, or None."""
    raw = admin_user_id or os.environ.get("ADMIN_ID") or os.environ.get("ADMIN_CHAT_ID")
    try:
        value = int(raw)  # type: ignore
    except (TypeError, ValueError):
        return None
    return value or None


def destination_username(chat_ref: Any) -> str:
    """Public @handle of a destination peer, or '' when it has no public username."""
    ref = str(chat_ref or "").strip()
    if not ref.startswith("@"):
        return ""
    handle = ref[1:]
    if not handle or not all(c.isalnum() or c == "_" for c in handle):
        return ""
    return handle


def destination_open_url(chat_ref: Any) -> Optional[str]:
    """t.me URL that opens a destination chat, or None when it isn't public."""
    handle = destination_username(chat_ref)
    return f"https://t.me/{handle}" if handle else None


def destination_health_handle(chat_ref: Any) -> str:
    """HTML for one destination reference in the health DM."""
    ref = str(chat_ref or "").strip()
    if not ref:
        return "id ?"
    if ref.startswith("@"):
        url = destination_open_url(ref)
        if url:
            return f'<a href="{url}">{ref}</a>'
        return ref
    return f"id {ref}"


def format_destination_health_report(rows: list) -> str:
    """Render a DM listing every destination that is not fully healthy.

    Safely caps output under Telegram's 4096 character limit when large batches
    (e.g. 100+ channels) fail.
    """
    dead = [r for r in rows if r.get("is_dead")]
    flapping = [r for r in rows if r.get("is_flapping")]
    throttled = [r for r in rows if r.get("is_throttled")]

    if not dead and not flapping and not throttled:
        return ""

    lines = [
        "📊 <b>Destination health</b>",
        "",
    ]
    if dead:
        lines.append(f"<b>❌ Not delivering at all ({len(dead)})</b>")
        sorted_dead = sorted(dead, key=lambda x: -x["failures"])
        shown_dead = sorted_dead[:15]
        for r in shown_dead:
            handle = destination_health_handle(r["chat_id"])
            last = (r.get("last_error") or "unknown error").split(" (caused by")[0][:90]
            lines.append(
                f"• {handle}\n"
                f"   {r['failures']}/{r['attempts']} failed · {r['fail_pct']:.0f}%\n"
                f"   {last}"
            )
        if len(sorted_dead) > 15:
            lines.append(
                f"<i>...and {len(sorted_dead) - 15} more failing destination(s). "
                f"Use the delete button below to remove all {len(dead)} dead channels.</i>"
            )
        lines.append("")
    if flapping:
        lines.append(f"<b>⚠️ Intermittent ({len(flapping)})</b>")
        sorted_flapping = sorted(flapping, key=lambda x: -x["consecutive_failures"])
        shown_flapping = sorted_flapping[:15]
        for r in shown_flapping:
            handle = destination_health_handle(r["chat_id"])
            lines.append(
                f"• {handle} — {r['fail_pct']:.0f}% fail, "
                f"{r['consecutive_failures']} in a row (still succeeds sometimes)"
            )
        if len(sorted_flapping) > 15:
            lines.append(f"<i>...and {len(sorted_flapping) - 15} more intermittent destination(s).</i>")
        lines.append("")
    if throttled:
        lines.append(f"<b>⏳ Waiting on Telegram rate limit ({len(throttled)})</b>")
        for r in sorted(throttled, key=lambda x: -x.get("deferred", 0)):
            handle = destination_health_handle(r["chat_id"])
            lines.append(
                f"• {handle} — {r.get('deferred', 0)} queued, "
                f"waiting for the rate limit to clear"
            )
        lines.append("")

    if dead or flapping:
        lines.append(
            "Banned or private groups can never be delivered to. Disable or delete "
            "them in Destinations so they stop being retried."
        )
    if throttled:
        lines.append(
            "Rate-limit waits are the forwarding account's fault, not the "
            "group's — these will deliver on their own. Raise "
            "FORWARD_PACING_SECONDS to queue fewer at once."
        )
    return "\n".join(lines)
