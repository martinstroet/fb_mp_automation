"""CLI entry point.

Commands:
  cycle        one watch cycle (launchd runs this every 30 min; pacing decides
               whether the wake becomes a session). --once bypasses pacing,
               --dry-run scrapes for real but uses a separate DB and sends nothing.
  digest       send the daily digest email
  login        interactive FB login bootstrap (headed browser)
  test-email   send a fake hot alert + digest to verify Gmail setup
  targets-lint validate targets.yaml and print the effective query plan
  eval-replay  re-run stage-1/2 evaluation on a saved JSON fixture (no browser)
  value        market-value estimate for one listing URL (browser only if the
               listing isn't already in the DB or kv cache)
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import evaluate, health, notify, scraper
from .browser import Checkpoint, SessionExpired, goto, human_scroll, open_browser
from .config import DATA_DIR, RATING_ORDER, ConfigError, load_config, rating_meets
from .lock import AlreadyRunning, Lock
from .pacing import Pacer
from .store import Store

log = logging.getLogger("fbmp")

LOCK_PATH = DATA_DIR / "fbmp.lock"


def setup_logging(cfg):
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(cfg.logs_dir / "fbmp.log", maxBytes=2_000_000, backupCount=5)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


# ----------------------------------------------------------------- cycle

def prune_old_files(cfg):
    """Bounded housekeeping: debug dumps (~2.4MB each) and thumbnails accumulate
    forever otherwise. Purely local filesystem work — no FB traffic."""
    now = time.time()
    debug_days = cfg.get("retention", "debug_days", default=14)
    thumb_days = cfg.get("retention", "thumb_days", default=60)
    try:
        for d in cfg.debug_dir.iterdir():
            if d.name == "preview" or not d.is_dir():
                continue
            if now - d.stat().st_mtime > debug_days * 86400:
                shutil.rmtree(d, ignore_errors=True)
        for f in cfg.thumbs_dir.glob("*.jpg"):
            if now - f.stat().st_mtime > thumb_days * 86400:
                f.unlink(missing_ok=True)
    except OSError as e:
        log.warning("retention prune skipped: %s", e)


def build_query_plan(cfg, store) -> list[tuple]:
    """[(target, query), ...] across all active targets."""
    plan = []
    for t in cfg.active_targets():
        for q in evaluate.queries_for_target(cfg, store, t):
            plan.append((t, q))
    return plan


def pick_searches(store, plan: list[tuple], n: int) -> list[tuple]:
    """Round-robin cursor guarantees coverage over hours; order is shuffled
    within the cycle so the traffic pattern isn't sequential."""
    if not plan:
        return []
    cursor = store.kv_get("search_cursor", 0) % len(plan)
    chosen = [plan[(cursor + i) % len(plan)] for i in range(min(n, len(plan)))]
    store.kv_set("search_cursor", (cursor + len(chosen)) % len(plan))
    random.shuffle(chosen)
    return chosen


def scrape_phase(cfg, store, pacer, searches: list[tuple], counters: dict) -> list[dict]:
    """Stage A (+ Stage B prep): search pages -> new cards; then detail fetches
    for anything already shortlisted. Returns cards seen this cycle."""
    all_cards = []
    with open_browser(cfg) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if pacer.use_organic_entry():
            goto(page, "https://www.facebook.com/marketplace", pacer)
            human_scroll(page, pacer.scroll_flicks())
            pacer.sleep(pacer.dwell())

        # Occasionally repurpose one slot as a "stale sweep": no freshness filter,
        # to discover older listings whose sellers may take a lower offer.
        neg = cfg.get("negotiation", default={}) or {}
        stale_slot = None
        if neg.get("enabled", True) and searches and (
            random.random() < neg.get("sweep_probability", 0.25)
        ):
            stale_slot = random.randrange(len(searches))

        any_results = False
        for i, (target, query) in enumerate(searches):
            stale = i == stale_slot
            url = scraper.build_search_url(cfg, query, target.max_price, stale=stale)
            log.info("search [%s]%s %r", target.id, " (stale sweep)" if stale else "", query)
            goto(page, url, pacer)
            human_scroll(page, pacer.scroll_flicks())
            cards = scraper.scrape_search_cards(page)
            counters["searches"] += 1
            counters["cards_seen"] += len(cards)
            if cards:
                any_results = True
            else:
                # zero results on a niche 1-day-fresh query is normal, not a
                # structural miss — keep one diagnostic dump per target per day
                today = time.strftime("%Y-%m-%d")
                if store.kv_get(f"zero_dump_day:{target.id}") != today:
                    scraper.dump_debug(cfg, page, f"zero-cards-{target.id}")
                    store.kv_set(f"zero_dump_day:{target.id}", today)
            for card in cards:
                card["source_target_id"] = target.id
                card["source_query"] = query
                card["sweep"] = "stale" if stale else "fresh"
                if store.get_listing(card["listing_id"]) is None:
                    card["thumb_path"] = scraper.download_thumb(
                        card.get("thumb_url"), cfg.thumbs_dir / f"{card['listing_id']}.jpg"
                    )
                if store.upsert_card(card):
                    counters["new_listings"] += 1
            all_cards.extend(cards)
            pacer.sleep(pacer.nav_delay())

        if searches:
            streak = health.note_search_results(store, all_zero=not any_results)
            health.maybe_alert_zero_streak(cfg, store, streak)

        # Stage 1 triage happens now, while the browser idles like an open tab —
        # so we can fetch details for fresh shortlists in this same session.
        triage_phase(cfg, store, counters)

        # Stage B: detail pages for shortlisted listings (incl. leftovers from
        # previous cycles), capped per cycle.
        cap = cfg.get("limits", "max_detail_fetches_per_cycle", default=5)
        shortlisted = store.shortlisted_for_detail(limit=cap)
        for row in shortlisted:
            pacer.sleep(pacer.nav_delay())
            log.info("detail fetch %s (%s)", row["listing_id"], row["title"])
            goto(page, row["url"], pacer)
            pacer.sleep(pacer.dwell())
            detail = scraper.scrape_detail(page)
            if not detail.get("title") and not detail.get("description"):
                scraper.dump_debug(cfg, page, f"detail-{row['listing_id']}")
                counters["errors"] += 1
            store.save_detail(row["listing_id"], detail)
            store.set_status(row["listing_id"], "detailed")
            counters["detail_fetches"] += 1

        if pacer.do_decoy() and all_cards:
            decoy = random.choice(all_cards)
            log.info("decoy visit %s", decoy["listing_id"])
            pacer.sleep(pacer.nav_delay())
            try:
                goto(page, decoy["url"], pacer)
                human_scroll(page, pacer.scroll_flicks())
                pacer.sleep(pacer.dwell())
            except (SessionExpired, Checkpoint):
                raise
            except Exception:
                pass
    return all_cards


def triage_phase(cfg, store, counters: dict):
    """Stage 1: claude triages card-level info for all status='new' listings."""
    batch_max = cfg.get("limits", "triage_batch_max", default=40)
    rows = store.listings_with_status("new", limit=batch_max)
    if not rows:
        return
    targets = {t.id: t for t in cfg.active_targets()}
    listings = [dict(r) for r in rows]
    try:
        results = evaluate.triage(cfg, list(targets.values()), listings)
    except Exception as e:
        log.error("stage-1 triage failed, will retry next cycle: %s", e)
        counters["errors"] += 1
        return

    for row in rows:
        lid = row["listing_id"]
        r = results.get(lid)
        if r is None:
            log.warning("triage omitted %s — leaving for next cycle", lid)
            continue
        tid = r.get("matched_target_id")
        target = targets.get(tid) if tid else None
        conf = float(r.get("match_confidence") or 0)
        rating = r.get("bargain_rating") or "unknown"
        store.record_evaluation(lid, 1, {
            "target_id": tid, "matched": target is not None,
            "est_value_aud": r.get("estimated_market_value_aud"),
            "bargain_rating": rating, "confidence": conf,
            "rationale": r.get("rationale"),
            "raw": r,  # claude's unfiltered result, for calibration analysis
        })
        if target is None:
            store.set_status(lid, "rejected")
            continue
        # code-enforced shortlist rule (claude's flag alone isn't trusted).
        # Stale-sweep finds also qualify at one rating step below the ask —
        # those are the negotiation candidates stage 2 may verdict as "offer".
        neg_enabled = (cfg.get("negotiation", default={}) or {}).get("enabled", True)
        near_miss = (
            neg_enabled
            and row["sweep"] == "stale"
            and RATING_ORDER.get(rating, -99) == RATING_ORDER[target.bargain_level] - 1
        )
        worth_detail = (
            rating_meets(rating, target.bargain_level)
            or (rating == "unknown" and conf >= 0.55)
            or near_miss
        )
        if worth_detail and r.get("shortlist"):
            # detail fetch is capped per cycle; the fetch queue is drained
            # best-stage-1-margin-first (store.shortlisted_for_detail)
            store.set_status(lid, "shortlisted")
        else:
            store.set_status(lid, "queued_digest")
            store.queue_digest(lid, tid)


def revisit_phase(cfg, store):
    """Negotiation revisits: matched listings that were digested once and have
    since aged into the negotiation window are promoted back to 'shortlisted'
    (at most once per listing), so the offer pipeline isn't limited to
    stale-sweep discoveries. DB-only — the page load happens through the
    normal capped, margin-ordered detail fetch, and stage 2 re-checks the
    authoritative "Listed X ago" age."""
    neg = cfg.get("negotiation", default={}) or {}
    if not neg.get("enabled", True):
        return
    lo, hi = neg.get("revisit_per_cycle", [0, 2])
    n = random.randint(lo, hi)
    if n <= 0:
        return
    targets = {t.id: t for t in cfg.active_targets()}
    factor = neg.get("max_price_factor", 1.4)
    promoted = 0
    candidates = store.revisit_candidates(
        neg.get("min_days_listed", 4), neg.get("max_days_listed", 45)
    )
    for row in candidates:
        target = targets.get(row["eval_target_id"])
        if target is None:
            continue
        # wanted level, or one rating step below it (the negotiation near-miss)
        gap = RATING_ORDER[target.bargain_level] - RATING_ORDER.get(row["eval_rating"], -99)
        if gap > 1:
            continue
        if target.max_price and (row["price_aud"] or 0) > target.max_price * factor:
            continue
        store.mark_revisited(row["listing_id"])
        log.info("revisit: %s (%s) aged into negotiation window",
                 row["listing_id"], row["title"])
        promoted += 1
        if promoted >= n:
            break


def is_rejectable(r: dict) -> bool:
    """Stage-2 reject rule: claude's non-match verdict stands unless the listing
    is a dubious *match* (worth a flagged-digest warning). A dubious non-match
    is noise twice over — reject it, don't surface it."""
    return r.get("verdict") == "reject" and not (
        bool(r.get("dubious")) and bool(r.get("final_match"))
    )


def enforced_flags(cfg, listing: dict, r: dict) -> tuple[bool, bool]:
    """Code-enforced (hot, offer) decisions for one stage-2 result — claude's
    flags alone are never trusted. Shared by the live pipeline and eval-replay
    so prompt-tuning sessions see exactly what the pipeline would do.

    hot: verdict hot ∧ final_match ∧ meets_target_level ∧ ¬dubious ∧ conf ≥ min.
    offer: verdict offer ∧ final_match ∧ ¬dubious ∧ age verifiably inside the
    negotiation window (unknown age tolerated only for stale-sweep finds)."""
    conf = float(r.get("confidence") or 0)
    dubious = bool(r.get("dubious"))
    min_conf = cfg.get("evaluation", "hot_min_confidence", default=0.6)
    is_hot = (
        r.get("verdict") == "hot"
        and bool(r.get("final_match"))
        and bool(r.get("meets_target_level"))
        and not dubious
        and conf >= min_conf
    )
    neg = cfg.get("negotiation", default={}) or {}
    days = scraper.parse_listed_days(listing.get("listed_ago_text"))
    # unknown age is tolerated where age is already evidenced another way:
    # stale-sweep finds, and revisits (whose first_seen_at put them in-window)
    age_ok = (
        days is not None
        and neg.get("min_days_listed", 4) <= days <= neg.get("max_days_listed", 45)
    ) or (days is None and (listing.get("sweep") == "stale"
                            or listing.get("revisited_at") is not None))
    is_offer = (
        neg.get("enabled", True)
        and not is_hot
        and r.get("verdict") == "offer"
        and bool(r.get("final_match"))
        and not dubious
        and age_ok
    )
    return is_hot, is_offer


def verdict_phase(cfg, store, counters: dict, preview_dir: Path | None = None):
    """Stage 2: final verdicts for everything with fetched detail, then alerts."""
    rows = store.listings_with_status("detailed", limit=50)
    if not rows:
        return
    targets = {t.id: t for t in cfg.active_targets()}

    by_target: dict[str, list] = {}
    for row in rows:
        ev1 = store.latest_evaluation(row["listing_id"])
        tid = ev1["target_id"] if ev1 else row["source_target_id"]
        if tid not in targets:
            store.set_status(row["listing_id"], "rejected")
            continue
        by_target.setdefault(tid, []).append(dict(row))

    for tid, listings in by_target.items():
        target = targets[tid]
        try:
            results = evaluate.verdict(cfg, target, listings)
        except Exception as e:
            log.error("stage-2 verdict failed for %s, will retry next cycle: %s", tid, e)
            counters["errors"] += 1
            continue
        for l in listings:
            lid = l["listing_id"]
            r = results.get(lid)
            if r is None:
                log.warning("verdict omitted %s — leaving for next cycle", lid)
                continue
            conf = float(r.get("confidence") or 0)
            dubious = bool(r.get("dubious"))
            is_hot, is_offer = enforced_flags(cfg, l, r)
            ev = {
                "target_id": tid, "matched": bool(r.get("final_match")),
                "est_value_aud": r.get("estimated_market_value_aud"),
                "bargain_rating": r.get("bargain_rating"),
                "meets_target": bool(r.get("meets_target_level")),
                "hot": is_hot, "dubious": dubious,
                "negotiation": is_offer,
                "suggested_offer_aud": r.get("suggested_offer_aud") if is_offer else None,
                "dubious_reasons": r.get("dubious_reasons") or [],
                "confidence": conf, "rationale": r.get("rationale"),
                "raw": r,  # claude's unfiltered result, for calibration analysis
            }
            store.record_evaluation(lid, 2, ev)
            if is_hot:
                send_hot_alert(cfg, store, l, ev, tid, counters, preview_dir)
            elif is_rejectable(r):
                store.set_status(lid, "rejected")
            else:  # digest, incl. dubious-but-matching (flags shown in digest)
                store.set_status(lid, "queued_digest")
                if l.get("revisited_at"):
                    # revisit outcome: re-arm the sent digest row; per-kind
                    # claims decide what may actually be reported again
                    store.requeue_digest(lid, tid)
                else:
                    store.queue_digest(lid, tid)


def send_hot_alert(cfg, store, listing: dict, ev: dict, target_id: str, counters, preview_dir):
    lid = listing["listing_id"]
    if preview_dir is not None:  # dry run: render, don't send
        html = notify.compose_hot(cfg.email, listing, ev, target_id) if cfg.email else None
        out = preview_dir / f"hot-{lid}.eml"
        preview_dir.mkdir(parents=True, exist_ok=True)
        out.write_bytes(html.as_bytes() if html else b"(no email config)")
        log.info("DRY RUN: hot alert for %s rendered to %s", lid, out)
        store.set_status(lid, "alerted_hot")
        return
    if not store.try_claim_alert(lid, "hot"):
        store.set_status(lid, "alerted_hot")
        return
    try:
        notify.send_hot(cfg.email, listing, ev, target_id, to=cfg.target_email(target_id))
        store.set_status(lid, "alerted_hot")
        counters["hot_alerts"] += 1
        log.info("HOT alert sent: %s (%s)", listing.get("title"), lid)
    except Exception as e:
        log.error("hot email failed for %s (%s) — falling back to digest", lid, e)
        counters["errors"] += 1
        store.set_status(lid, "queued_digest")
        store.queue_digest(lid, target_id)


def cmd_cycle(args) -> int:
    cfg = load_config(dry_run=args.dry_run, require_email=not args.dry_run)
    setup_logging(cfg)
    manual = args.once or args.dry_run

    try:
        lock = Lock(LOCK_PATH)
        lock.acquire()
    except AlreadyRunning as e:
        log.info("skipping: %s", e)
        return 0

    store = Store(cfg.db_path)
    counters = dict(searches=0, cards_seen=0, new_listings=0,
                    detail_fetches=0, hot_alerts=0, errors=0)
    try:
        prune_old_files(cfg)
        abandoned = store.mark_abandoned_runs()
        if abandoned:
            log.info("tagged %d abandoned run(s) from earlier power loss/kill", abandoned)
        pacer = Pacer(cfg, store)
        if not manual:
            ok, reason = pacer.gate()
            if not ok:
                log.info("pacing gate: %s", reason)
                run_id = store.start_run("cycle", note=reason)
                store.finish_run(run_id)
                return 0
            delay = pacer.pre_sleep_seconds()
            log.info("pacing: pre-sleep %.0fs", delay)
            pacer.sleep(delay)

        run_id = store.start_run("cycle", note="dry_run" if args.dry_run else "")

        # session known-dead? probe cheaply before doing anything else
        if not store.kv_get("session_ok", True):
            from .browser import probe_logged_in
            with open_browser(cfg) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if probe_logged_in(page):
                    store.kv_set("session_ok", True)
                    log.info("session recovered")
                else:
                    log.warning("session still dead — standing down")
                    store.finish_run(run_id, note="session_dead", **counters)
                    return 1

        if args.query:
            targets = cfg.active_targets()
            if not targets:
                log.error("--query needs at least one active target")
                store.finish_run(run_id, note="no_active_targets")
                return 2
            searches = [(targets[0], args.query)]
        else:
            plan = build_query_plan(cfg, store)
            searches = pick_searches(store, plan, pacer.searches_this_cycle())

        revisit_phase(cfg, store)

        preview_dir = (cfg.debug_dir / "preview") if args.dry_run else None
        try:
            scrape_phase(cfg, store, pacer, searches, counters)
        except Checkpoint as e:
            log.error("CHECKPOINT hit: %s", e)
            health.mark_session_dead(cfg, store, f"security checkpoint at {e}")
            store.finish_run(run_id, note="checkpoint", **counters)
            return 1
        except SessionExpired as e:
            log.error("session expired: %s", e)
            health.mark_session_dead(cfg, store, f"logged-out page at {e}")
            store.finish_run(run_id, note="session_expired", **counters)
            return 1

        verdict_phase(cfg, store, counters, preview_dir)
        store.finish_run(run_id, **counters)
        log.info("cycle done: %s", counters)
        return 0
    finally:
        store.close()
        lock.release()


# ----------------------------------------------------------------- digest

def collect_digest_items(store) -> tuple[dict, list, list]:
    groups: dict[str, list[dict]] = {}
    offers: list[dict] = []
    flagged: list[dict] = []
    for row in store.pending_digest():
        item = dict(row)
        ev = store.latest_evaluation(item["listing_id"])
        item["dubious"], item["dubious_reasons"], negotiation = False, [], False
        if ev:
            item["est_value_aud"] = ev["est_value_aud"]
            item["bargain_rating"] = ev["bargain_rating"]
            item["rationale"] = ev["rationale"]
            item["dubious"] = bool(ev["dubious"])
            item["dubious_reasons"] = json.loads(ev["dubious_reasons"] or "[]")
            item["suggested_offer_aud"] = ev["suggested_offer_aud"]
            negotiation = bool(ev["negotiation"])
        if item["dubious"]:
            flagged.append(item)
        elif negotiation:
            offers.append(item)
        else:
            groups.setdefault(item["target_id"] or "unmatched", []).append(item)
    return groups, offers, flagged


def cmd_digest(args) -> int:
    cfg = load_config(dry_run=args.dry_run, require_email=not args.dry_run)
    setup_logging(cfg)

    # The 18:00 slot must not be lost to a cycle holding the lock — wait for it.
    lock = Lock(LOCK_PATH)
    for attempt in range(20):
        try:
            lock.acquire()
            break
        except AlreadyRunning:
            log.info("digest waiting for lock (attempt %d)", attempt + 1)
            time.sleep(60)
    else:
        log.error("digest could not acquire lock after 20 min")
        return 1

    store = Store(cfg.db_path)
    try:
        run_id = store.start_run("digest")
        groups, offers, flagged = collect_digest_items(store)
        n = sum(len(v) for v in groups.values()) + len(offers) + len(flagged)
        hstats = health.summary(store)

        if n == 0:
            healthy = hstats["session_ok"] and hstats["errors"] == 0 and hstats["cycles"] > 0
            if not healthy and cfg.email and not args.dry_run:
                notify.send_plain(
                    cfg.email,
                    "FB MP watcher: no listings today (health warning)",
                    f"No matched listings today, and the day wasn't healthy: {hstats}",
                )
            elif cfg.get("alerts", "send_empty_digest", default=False) and cfg.email and not args.dry_run:
                notify.send_plain(cfg.email, "FB Marketplace digest — nothing today", f"Health: {hstats}")
            log.info("digest: nothing to send (healthy=%s)", hstats)
            store.finish_run(run_id, note="empty")
            return 0

        # One digest email per destination address: targets may override the
        # global recipient, so bundle sections by where they're going.
        default_to = cfg.email.to if cfg.email else "default"

        def recipient_for(target_id) -> str:
            return cfg.target_email(target_id) or default_to

        bundles: dict[str, dict] = {}

        def bundle(to: str) -> dict:
            return bundles.setdefault(to, {"groups": {}, "offers": [], "flagged": []})

        for tid, items in groups.items():
            bundle(recipient_for(tid))["groups"][tid] = items
        for it in offers:
            bundle(recipient_for(it.get("target_id")))["offers"].append(it)
        for it in flagged:
            bundle(recipient_for(it.get("target_id")))["flagged"].append(it)

        if args.dry_run:
            preview = cfg.debug_dir / "preview"
            preview.mkdir(parents=True, exist_ok=True)
            for to, b in bundles.items():
                msg = notify.compose_digest(cfg.email, b["groups"], b["offers"],
                                            b["flagged"], hstats, to=to) if cfg.email else None
                out = preview / f"digest-{to.replace('@', '_at_').replace('/', '_')}.eml"
                out.write_bytes(msg.as_bytes() if msg else b"(no email config)")
                log.info("DRY RUN: digest for %s rendered to %s", to, out)
            store.finish_run(run_id, note="dry_run")
            return 0

        # Claim transactionally before SMTP: at most one report per (listing,
        # kind) — plain/flagged rows claim 'digest', the offers section claims
        # 'offer', so a digested near-miss may return once as an offer but
        # nothing repeats within its kind. An item whose claim is already taken
        # was sent by an earlier attempt that died before marking the queue —
        # mark it sent now, keep it out of this email. Claims and rollback are
        # per recipient, so one failed send never blocks or duplicates another's.
        def claimed(it, kind):
            if store.try_claim_alert(it["listing_id"], kind):
                return True
            log.warning("digest item %s already claimed as %s — not resending",
                        it["listing_id"], kind)
            store.mark_digest_sent([it["listing_id"]])
            return False

        sent_total, failures = 0, 0
        for to, b in bundles.items():
            bgroups = {tid: kept for tid, items in b["groups"].items()
                       if (kept := [it for it in items if claimed(it, "digest")])}
            boffers = [it for it in b["offers"] if claimed(it, "offer")]
            bflagged = [it for it in b["flagged"] if claimed(it, "digest")]
            digest_ids = [it["listing_id"] for items in bgroups.values() for it in items]
            digest_ids += [it["listing_id"] for it in bflagged]
            offer_ids = [it["listing_id"] for it in boffers]
            ids = digest_ids + offer_ids
            if not ids:
                continue
            try:
                notify.send_digest(cfg.email, bgroups, boffers, bflagged, hstats, to=to)
            except Exception as e:
                log.error("digest send to %s failed (%s) — will retry tomorrow", to, e)
                store.release_alerts(digest_ids, "digest")
                store.release_alerts(offer_ids, "offer")
                failures += 1
                continue
            store.mark_digest_sent(ids)
            sent_total += len(ids)
            log.info("digest sent to %s: %d items", to, len(ids))

        if sent_total == 0 and failures == 0:
            log.info("digest: every pending item was already sent")
            store.finish_run(run_id, note="empty")
            return 0
        note = f"sent {sent_total}" + (f", {failures} send(s) failed" if failures else "")
        store.finish_run(run_id, note=note)
        return 1 if failures else 0
    finally:
        store.close()
        lock.release()


# ----------------------------------------------------------------- misc cmds

def cmd_login(args) -> int:
    cfg = load_config(require_email=False)
    setup_logging(cfg)
    ok = False
    try:
        from .login import run_login
        ok = run_login(cfg)
    finally:
        if ok:
            Store(cfg.db_path).kv_set("session_ok", True)
    return 0 if ok else 1


def cmd_test_email(args) -> int:
    cfg = load_config()
    setup_logging(cfg)
    fake = {
        "listing_id": "0", "title": "TEST — Wilson Live Fibre 10ft", "price_text": "$180",
        "price_aud": 180, "location": "Brisbane, QLD", "thumb_path": None,
        "url": "https://www.facebook.com/marketplace/",
        "seller_name": "Test Seller", "seller_joined_year": 2012,
    }
    ev = {"est_value_aud": 320, "bargain_rating": "well_below_market",
          "rationale": "This is a test alert — Gmail setup works.", "dubious_reasons": []}
    notify.send_hot(cfg.email, fake, ev, "test-target")
    groups = {"test-target": [dict(fake, est_value_aud=320, bargain_rating="below_market",
                                   rationale="Test digest row.", dubious_reasons=[])]}
    offers = [dict(fake, listing_id="2", title="TEST — Saragosa 10000 (3 weeks listed)",
                   price_text="$300", est_value_aud=310, bargain_rating="market",
                   suggested_offer_aud=240, listed_ago_text="Listed 3 weeks ago",
                   dubious_reasons=[], rationale="Test offer row — aged listing, try a lower offer.")]
    flagged = [dict(fake, listing_id="1", title="TEST — Suspicious GoPro", price_text="$50",
                    est_value_aud=400, bargain_rating="well_below_market",
                    dubious_reasons=["new_account_cheap_item", "price_too_good"],
                    rationale="Test flagged row.")]
    notify.send_digest(cfg.email, groups, offers, flagged,
                       {"cycles": 1, "new_listings": 2, "errors": 0, "session_ok": True})
    print(f"✓ Sent test hot alert + test digest to {cfg.email.to}. Check inbox (and spam).")
    return 0


def cmd_targets_lint(args) -> int:
    cfg = load_config(require_email=False)
    setup_logging(cfg)
    store = Store(cfg.db_path)
    print(f"{len(cfg.targets)} target(s), {len(cfg.active_targets())} active\n")
    for t in cfg.targets:
        state = "active" if t.active else "PAUSED"
        dest = f", alerts → {t.email}" if t.email else ""
        print(f"■ {t.id} [{state}] — wants {t.bargain_level}, max ${t.max_price}{dest}")
        print(f"  {t.description}")
        if t.notes:
            print(f"  notes: {t.notes}")
        if t.active:
            try:
                queries = evaluate.queries_for_target(cfg, store, t)
                src = "yaml" if t.queries else "claude-generated (cached)"
                print(f"  queries ({src}): {queries}")
            except Exception as e:
                print(f"  ⚠ query generation failed: {e}")
        print()
    print("targets.yaml OK")
    return 0


def cmd_value(args) -> int:
    """Market-value estimate for one listing URL/id. Reads pipeline data but
    never writes it: unknown listings are cached in kv, not in `listings`."""
    cfg = load_config(require_email=False)
    setup_logging(cfg)

    m = scraper.ITEM_ID_RE.search(args.url)
    lid = m.group(1) if m else (args.url.strip() if args.url.strip().isdigit() else None)
    if not lid:
        print(f"not a marketplace listing URL or id: {args.url!r}", file=sys.stderr)
        return 2

    store = Store(cfg.db_path)
    try:
        row = store.get_listing(lid)
        listing = {k: row[k] for k in row.keys()} if row else {}
        cache_key = f"value_detail:{lid}"
        source = None
        if not args.refresh:
            if listing.get("description"):
                source = "watcher db"
            else:
                cached = store.kv_get(cache_key)
                if cached:
                    listing = {**cached, **{k: v for k, v in listing.items() if v is not None}}
                    source = "cache"

        if source is None:  # one real page load, sharing the cycle lock/session
            try:
                lock = Lock(LOCK_PATH)
                lock.acquire()
            except AlreadyRunning as e:
                print(f"a watcher run holds the session ({e}) — try again in a few minutes",
                      file=sys.stderr)
                return 1
            try:
                pacer = Pacer(cfg, store)
                url = f"https://www.facebook.com/marketplace/item/{lid}/"
                with open_browser(cfg) as ctx:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    goto(page, url, pacer)
                    pacer.sleep(pacer.dwell())
                    detail = scraper.scrape_detail(page)
            except Checkpoint as e:
                health.mark_session_dead(cfg, store, f"security checkpoint at {e}")
                print("hit a Facebook checkpoint — watcher stood down", file=sys.stderr)
                return 1
            except SessionExpired as e:
                health.mark_session_dead(cfg, store, f"logged-out page at {e}")
                print("Facebook session expired — run: python -m fbmp.main login",
                      file=sys.stderr)
                return 1
            finally:
                lock.release()
            detail = {k: v for k, v in detail.items() if v is not None}
            if not detail.get("title") and not detail.get("description"):
                print("could not parse the listing page (removed/sold, or layout change)",
                      file=sys.stderr)
                return 1
            # card-scraped fields (esp. price) are more reliable than detail heuristics
            listing = {**detail, **{k: v for k, v in listing.items() if v is not None}}
            store.kv_set(cache_key, detail)
            source = "live fetch"

        result = evaluate.market_value(cfg, listing)
        if args.json:
            print(json.dumps(result, indent=2))
            return 0

        print(f"\n{result.get('product') or listing.get('title') or lid}")
        ask = listing.get("price_text") or (
            f"AU${listing['price_aud']}" if listing.get("price_aud") is not None else None)
        if ask:
            print(f"  asking:     {ask}")
        lo, mid, hi = (result.get("used_value_low_aud"), result.get("used_value_mid_aud"),
                       result.get("used_value_high_aud"))
        if lo is not None and hi is not None:
            mid_s = f" (mid ${mid})" if mid is not None else ""
            print(f"  used value: ${lo}-${hi} AUD{mid_s}")
        else:
            print("  used value: could not be estimated")
        if result.get("new_price_aud"):
            print(f"  new price:  ~${result['new_price_aud']}")
        rating = (result.get("bargain_rating") or "unknown").replace("_", " ")
        conf = result.get("confidence")
        print(f"  vs asking:  {rating}" + (f" (confidence {conf})" if conf is not None else ""))
        for d in result.get("value_drivers") or []:
            print(f"    - {d}")
        if result.get("rationale"):
            print(f"  {result['rationale']}")
        print(f"  [listing detail: {source}]")
        return 0
    finally:
        store.close()


def cmd_eval_replay(args) -> int:
    cfg = load_config(require_email=False)
    setup_logging(cfg)
    listings = json.loads(Path(args.fixture).read_text())
    if isinstance(listings, dict):
        listings = listings.get("listings", [])

    if args.stage == 1:
        results = evaluate.triage(cfg, cfg.active_targets(), listings)
        print(json.dumps(results, indent=2))
        return 0

    # stage 2: verdict replay for one target (fixtures need detail-level fields:
    # description, listed_ago_text, seller_*, image_count, sweep)
    targets = cfg.active_targets()
    if not targets:
        print("no active targets", file=sys.stderr)
        return 2
    target = targets[0]
    if args.target:
        by_id = {t.id: t for t in targets}
        if args.target not in by_id:
            print(f"unknown target {args.target!r}; active: {sorted(by_id)}", file=sys.stderr)
            return 2
        target = by_id[args.target]
    results = evaluate.verdict(cfg, target, listings)
    print(json.dumps(results, indent=2))

    print(f"\ncode-enforced disposition (target {target.id}):")
    for l in listings:
        r = results.get(str(l["listing_id"]))
        if r is None:
            print(f"  {l['listing_id']}: OMITTED by claude — would retry next cycle")
            continue
        is_hot, is_offer = enforced_flags(cfg, l, r)
        if is_hot:
            disp = "HOT alert"
        elif is_rejectable(r):
            disp = "rejected"
        elif r.get("dubious"):
            disp = "digest (flagged dubious)"
        elif is_offer:
            disp = "digest (offer section)"
        else:
            disp = "digest"
        note = ""
        if r.get("verdict") == "hot" and not is_hot:
            note = "  <- claude said hot, code gate blocked it"
        elif r.get("verdict") == "offer" and not is_offer:
            note = "  <- claude said offer, code gate blocked it"
        print(f"  {l['listing_id']}: {disp}{note}")
    return 0


# ----------------------------------------------------------------- entry

def main(argv=None):
    p = argparse.ArgumentParser(prog="fbmp", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cycle", help="run one watch cycle")
    c.add_argument("--once", action="store_true", help="bypass pacing gate/pre-sleep (manual run)")
    c.add_argument("--dry-run", action="store_true", help="separate DB, no emails, render previews")
    c.add_argument("--query", help="ad-hoc single search query (selector testing)")
    c.set_defaults(fn=cmd_cycle)

    d = sub.add_parser("digest", help="send the daily digest")
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(fn=cmd_digest)

    sub.add_parser("login", help="interactive FB login bootstrap").set_defaults(fn=cmd_login)
    sub.add_parser("test-email", help="send test emails").set_defaults(fn=cmd_test_email)
    sub.add_parser("targets-lint", help="validate targets.yaml").set_defaults(fn=cmd_targets_lint)

    v = sub.add_parser("value", help="market-value estimate for a listing URL")
    v.add_argument("url", help="marketplace listing URL (or bare listing id)")
    v.add_argument("--refresh", action="store_true",
                   help="refetch the page even if the listing is known/cached")
    v.add_argument("--json", action="store_true", help="print the raw JSON result")
    v.set_defaults(fn=cmd_value)

    r = sub.add_parser("eval-replay", help="stage-1/2 evaluation on a JSON fixture")
    r.add_argument("fixture")
    r.add_argument("--stage", type=int, choices=[1, 2], default=1,
                   help="1 = card triage (default), 2 = detail verdict")
    r.add_argument("--target", help="target id for --stage 2 (default: first active)")
    r.set_defaults(fn=cmd_eval_replay)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
