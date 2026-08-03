"""Health signals: zero-result streaks, session-expiry alerts (rate-limited)."""

from __future__ import annotations

import logging
import time

from . import notify

log = logging.getLogger(__name__)


def note_search_results(store, all_zero: bool) -> int:
    """Track consecutive cycles where every search returned zero cards."""
    streak = store.kv_get("zero_streak", 0)
    streak = streak + 1 if all_zero else 0
    store.kv_set("zero_streak", streak)
    return streak


def maybe_alert_zero_streak(cfg, store, streak: int):
    threshold = cfg.get("alerts", "zero_result_streak_alert", default=3)
    if streak != threshold or not cfg.email or cfg.dry_run:
        return
    try:
        notify.send_plain(
            cfg.email,
            "⚠ FB MP watcher: searches returning nothing",
            f"All searches have returned 0 results for {streak} consecutive cycles.\n"
            "Likely causes: Facebook layout change, a soft block, or a dead session\n"
            "that still passes the login check.\n\n"
            "Check data/debug/ for page dumps, or run:\n"
            "  .venv/bin/python -m fbmp.main cycle --once --dry-run\n",
        )
    except Exception as e:
        log.error("failed to send zero-streak alert: %s", e)


def mark_session_dead(cfg, store, reason: str):
    """Record a dead/checkpointed session and email the user (max 1 per N hours)."""
    store.kv_set("session_ok", False)
    min_hours = cfg.get("alerts", "session_alert_min_hours", default=24)
    last = store.kv_get("last_session_alert_at", 0)
    if time.time() - last < min_hours * 3600 or not cfg.email or cfg.dry_run:
        return
    try:
        notify.send_plain(
            cfg.email,
            "⚠ FB MP watcher: Facebook session needs attention",
            f"The watcher hit: {reason}\n\n"
            "Cycles are paused until the session works again. To fix, run:\n"
            "  .venv/bin/python -m fbmp.main login\n\n"
            "If this was a security checkpoint, log in manually in the opened\n"
            "browser and complete the challenge before pressing Enter.\n",
        )
        store.kv_set("last_session_alert_at", time.time())
    except Exception as e:
        log.error("failed to send session alert: %s", e)


def ops_summary(store) -> list[tuple[str, str]]:
    """Last-24h operational stats for the owner's daily oversight email —
    the review/debugging/optimisation view across all targets."""
    day = int(time.time()) - 86400
    db = store.db
    c = db.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(searches),0) s, COALESCE(SUM(cards_seen),0) cs,
                  COALESCE(SUM(new_listings),0) nl, COALESCE(SUM(detail_fetches),0) df,
                  COALESCE(SUM(errors),0) e
           FROM runs WHERE kind='cycle' AND started_at>=? AND searches>0""", (day,)).fetchone()
    skips = db.execute(
        "SELECT COALESCE(NULLIF(note,''),'?'), COUNT(*) FROM runs "
        "WHERE kind='cycle' AND started_at>=? AND searches=0 GROUP BY 1", (day,)).fetchall()
    s1 = db.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(matched),0) m,
                  ROUND(AVG(CASE WHEN matched=1 THEN confidence END),2) mc
           FROM evaluations WHERE stage=1 AND created_at>=?""", (day,)).fetchone()
    s2 = db.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(hot),0) h, COALESCE(SUM(negotiation),0) o,
                  COALESCE(SUM(dubious),0) d
           FROM evaluations WHERE stage=2 AND created_at>=?""", (day,)).fetchone()
    sent = db.execute(
        "SELECT kind, COUNT(*) FROM alerts WHERE sent_at>=? GROUP BY kind", (day,)).fetchall()
    backlog = db.execute(
        "SELECT status, COUNT(*) FROM listings WHERE status IN "
        "('new','shortlisted','detailed') GROUP BY status").fetchall()
    rows = [
        ("Cycles ran (24h)", str(c["n"])),
        ("Wakes skipped", ", ".join(f"{k}×{v}" for k, v in skips) or "none"),
        ("Searches / cards seen / new", f"{c['s']} / {c['cs']} / {c['nl']}"),
        ("Detail fetches / errors", f"{c['df']} / {c['e']}"),
        ("Stage-1 evals (matched)", f"{s1['n']} ({s1['m']} matched"
         + (f", avg conf {s1['mc']}" if s1["mc"] is not None else "") + ")"),
        ("Stage-2 verdicts", f"{s2['n']} (hot {s2['h']}, offer {s2['o']}, dubious {s2['d']})"),
        ("Alerts sent", ", ".join(f"{k}×{v}" for k, v in sent) or "none"),
        ("Pipeline backlog", ", ".join(f"{k}×{v}" for k, v in backlog) or "empty"),
        ("Zero-result streak", str(store.kv_get("zero_streak", 0))),
        ("Session", "OK" if store.kv_get("session_ok", True) else "NEEDS RE-LOGIN"),
    ]
    return rows


def summary(store) -> dict:
    runs = [r for r in store.runs_today() if r["kind"] == "cycle"]
    return {
        "cycles": len(runs),
        "new_listings": sum(r["new_listings"] or 0 for r in runs),
        "errors": sum(r["errors"] or 0 for r in runs),
        "session_ok": bool(store.kv_get("session_ok", True)),
    }
