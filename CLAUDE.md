# CLAUDE.md — FB Marketplace Watcher

Personal FB Marketplace watcher: scrapes on a humanized ~30-min rhythm via the
user's logged-in session (Playwright + real Chrome), evaluates listings with
headless `claude -p` (text + thumbnail photos), emails HOT bargains
immediately and the rest in a daily 18:00 digest — per-target routable, with
the owner always receiving the full oversight copy + 24h ops summary.
**This system is LIVE on launchd** — sessions in this repo are usually about
optimizing settings/prompts/methodology, not rebuilding. README.md holds the
user-facing methodology writeup; keep both in sync when behavior changes.

## Live-system rules (read first)

- `com.fbmp.cycle` (every 30 min) and `com.fbmp.digest` (18:00) are installed in
  `~/Library/LaunchAgents/`. A cycle may be running right now; runs share the
  lockfile `data/fbmp.lock` — never delete it unless the owning PID is dead.
- **Minimize real FB traffic.** Every page load counts against ban risk. Test
  with `--dry-run` (separate DB `data/fbmp.dryrun.db`, no emails, previews to
  `data/debug/preview/`) and prefer `eval-replay` (no browser at all) for
  prompt work. Don't run repeated live cycles back-to-back for testing.
- `data/fbmp.db` is the real state. Additive schema changes only, via
  `Store._migrate()` (ALTER TABLE pattern) — the live DB must migrate in place.
- The FB session lives in `data/browser_profile/`. Don't wipe it; re-login is
  `python -m fbmp.main login` (interactive, user-driven).
- The alert guarantee: `alerts` table `UNIQUE(listing_id, kind)`, claimed
  transactionally before SMTP send (kinds: `hot`, `digest`, `offer`). Don't
  weaken this — "never report twice **per kind**" is a core requirement. A
  digested near-miss may legitimately reappear once, as an offer or a hot
  alert, via the revisit path — never again within the same kind.

## Commands

Alias used throughout: `PYTHONPATH=src .venv/bin/python -m fbmp.main <cmd>`

| Command | Use |
|---|---|
| `cycle --dry-run [--query Q]` | Real scrape → dryrun DB, no emails, renders previews. The safe live test. |
| `cycle --once` | Real cycle, bypasses pacing gate. Use sparingly. |
| `eval-replay tests/fixtures/cards_sample.json` | Stage-1 triage on fixtures, zero FB traffic. **The prompt-tuning loop.** |
| `eval-replay --stage 2 [--target ID] tests/fixtures/details_sample.json` | Stage-2 verdict on detail-level fixtures + the code-enforced disposition each listing would get. |
| `targets-lint` | Validate targets.yaml, show effective queries. |
| `value <listing-url> [--json] [--refresh]` | Market-value range for any listing. Zero FB traffic when the listing is in the DB/kv cache; otherwise one page load (shares the cycle lock). Never writes pipeline state. |
| `digest [--dry-run]` | Drain/preview digest queue. |
| `test-email` | Fake hot + digest to the real inbox. |

Inspect behavior: `sqlite3 data/fbmp.db "SELECT datetime(started_at,'unixepoch','localtime'), note, searches, new_listings, hot_alerts, errors FROM runs ORDER BY id DESC LIMIT 20"` — gated wakes log their reason in `note` (`skipped_humanization`, `long_gap`, `outside_active_hours`). Logs: `data/logs/fbmp.log`.

## Where the knobs are

- `config/settings.yaml` — all tunables: pacing distributions/probabilities,
  active hours, per-cycle caps, negotiation window (4–45 days), digest hour,
  `alerts.daily_summary` (owner's daily ops summary + routed-section copies,
  sent even on empty days), `headless` flag. Picked up next cycle, no
  reinstall.
- `config/targets.yaml` — user-owned watch list. Queries must be short
  title-style search strings ("Saragosa 8000"), never descriptive phrases;
  omit `queries:` to have Claude generate them (cached in `kv` until the
  description changes). Optional per-target `email:` routes that target's
  hot alerts + digest sections to a different address (digest becomes one
  email per distinct recipient; claims/rollback are per recipient).
- `src/fbmp/prompts.py` — stage-1 triage, stage-2 verdict, query generation,
  market value. All evaluation stages include a **photo layer**: listing
  thumbnails (`data/thumbs/`) are passed as `thumbnail_file` paths and claude
  views them via Read (`--allowedTools Read`); photos are authoritative over
  titles for physical attributes (axle counts, cage, variant) and feed the
  scam checks. Tune here + verify with `eval-replay`; capture new fixtures
  from dry-run DBs (real fixtures with live thumbs: `cards_trailers.json`,
  `details_saragosa_hot.json`).
- `src/fbmp/pacing.py` — humanization engine. **Hard rule: no fixed constants
  in the FB interaction path** — every delay/count/probability is a draw from
  a settings range. The launchd 1800s tick is only a wake-up; `Pacer.gate()`
  decides if it becomes a session.
- `src/fbmp/scraper.py` — ALL selectors live here, anchored on hrefs/regex/
  text nodes, never obfuscated class names. On layout breaks, check
  `data/debug/<timestamp>-*/page.html` dumps.

## Decision pipeline (code-enforced, not just prompted)

new card → stage-1 triage (batched, card-level) → `rejected` | `queued_digest`
| `shortlisted` → detail fetch (≤5/cycle) → stage-2 verdict →
`alerted_hot` | `queued_digest` (incl. offers + dubious) | `rejected`.

Code enforcement in `main.py` (Claude's flags alone are never trusted):
- **hot** = verdict `hot` ∧ final_match ∧ meets_target_level ∧ ¬dubious ∧ confidence ≥ 0.6
- **shortlist** = rating meets target level ∨ (unknown ∧ conf ≥ 0.55) ∨ near-miss (stale sweep, one rating step below ask)
- **offer** = verdict `offer` ∧ final_match ∧ ¬dubious ∧ listed 4–45 days (parsed from "Listed X ago"; unknown age tolerated only for stale-sweep finds)
- **digest** (stage 1) = matched ∧ match_confidence ≥ 0.4 (`digest_min_confidence`); weaker matches are dropped, not digested — broad categories soft-match half their search results
- Crash-resume: `status` column is the state machine; stuck listings are picked up next cycle. Claude call failures defer, never drop.

Discovery: fresh sweeps (`daysSinceListed=1`, newest first) via round-robin
cursor over all target queries; ~25% of cycles convert one slot to a **stale
sweep** (no age filter, maxPrice ×1.4) to find negotiation candidates.
**Revisits** feed the offer pipeline from the other side: each cycle promotes
0–2 (drawn) previously digested near-miss matches back to `shortlisted` once
their `first_seen_at` ages into the negotiation window (at most once per
listing, `revisited_at`); stage 2 then re-checks the authoritative listing
age and may verdict `offer` (re-reported via `requeue_digest` + the `offer`
alert kind) or even `hot`.

## Hard-won scraping facts (don't rediscover these)

- Card text renders as **inline spans** — `inner_text()` has no line breaks.
  Cards are parsed by walking text nodes; price/title/location split is
  anchored on the price regex. Prices use **`AU$`** prefix.
- Detail pages: description = longest text block scoped to `div[role="main"]`,
  excluding blocks with ≥2 prices or "Today's picks"/"Just listed" (the
  recommendation rail). Seller-profile links: several exist; skip generic
  labels ("Seller details") to get the actual name.
- `image_count` **overcounts** (rail images) — prompts treat it as
  approximate; only very low values are a scam signal, high values are never
  reassurance.
- FB CDN image URLs expire — thumbnails are downloaded at scrape time to
  `data/thumbs/`; emails attach them as CID parts, never hot-link.
- Search URL `radius` param is unreliable; location comes from the account's
  Marketplace setting (set during `login`) + the `{location_slug}` path.

## Optimization workflow for new sessions

1. Understand current behavior from data first: `runs` table, `evaluations`
   (compare stage-1 vs stage-2 ratings, confidence calibration, rejection
   rationales), digest composition.
2. Prompt/threshold changes → iterate with `eval-replay` on fixtures; add new
   fixtures from real dry-run scrapes if coverage is thin.
3. Pacing/discovery changes → edit settings.yaml, verify with one
   `cycle --dry-run`, then let scheduled cycles run; judge over a day of
   `runs` data, not a single cycle.
4. If FB throws a checkpoint: watcher stands down and emails the user. First
   responses: `headless: false` in settings, longer intervals, lower caps —
   not code surgery.
5. Anything user-visible (email layout, targets semantics) — confirm with the
   user before changing; the emails are the product.
