# FB Marketplace Watcher

Personal watcher that checks Facebook Marketplace on an irregular ~30-minute
rhythm for listings matching your `config/targets.yaml` watch list, uses Claude
(headless `claude -p`, your existing subscription) to fuzzy-match listings —
reading the photos, not just the titles — estimate market value, rate the
bargain, and flag dubious sellers. HOT listings email immediately; everything
else lands in a daily digest, routable per target to different recipients,
with a full oversight copy + operations summary to the owner. Nothing is ever
reported twice in the same capacity (hot / digest / offer).

> **Heads up:** automating your own logged-in FB account is against Facebook's
> ToS and carries some risk of account restriction. The pacing engine goes to
> considerable lengths to look like a bored human, but the risk is not zero.

## Setup

```sh
scripts/setup.sh                 # venv, deps, checks Chrome + claude CLI
vim .env                         # Gmail app password (Google Account → Security → App passwords)
vim config/settings.yaml         # set marketplace.location_slug to your city
cp config/targets.example.yaml config/targets.yaml   # then edit — your watch
                                 # list stays local (gitignored, like .env)
```

Then, with `alias fbmp='PYTHONPATH=src .venv/bin/python -m fbmp.main'`:

```sh
fbmp login          # headed Chrome opens: log in, set Marketplace location/radius, press Enter
fbmp targets-lint   # validate targets, see the effective query plan
fbmp cycle --dry-run          # real scrape → separate DB, no emails, previews in data/debug/preview/
fbmp test-email     # fake hot alert + digest to your inbox (check spam folder!)
fbmp cycle --once   # first real run; run it twice — second run should find 0 new (dedup proof)
fbmp digest         # manually drain the digest queue once
scripts/install_launchd.sh      # enable the schedule (every 30 min + daily digest)
```

## Methodology

The core principle: **Claude proposes, code disposes.** Claude's judgments
(match, value, rating, scam flags) never trigger an email by themselves —
every disposition passes a code-enforced gate with thresholds from
`config/settings.yaml`, so a prompt regression can't spam or over-alert.

- **Discovery.** Each cycle runs a few target queries as *fresh sweeps*
  (last-day listings, newest first) via a round-robin cursor so every query
  gets coverage across the day. ~25% of cycles convert one slot into a
  *stale sweep* — no freshness filter, price ceiling ×1.4 — hunting older
  listings whose sellers may take a lower offer.
- **Stage 1 — triage.** New listings' card info (title/price/location +
  thumbnail) is batched to Claude with all targets: fuzzy match, market-value
  estimate, bargain rating, match confidence. Non-matches and weak matches
  (confidence < 0.4) are dropped; solid matches queue for the digest;
  bargains at/near your wanted level are shortlisted for a detail look.
- **Photo interpretation.** Every evaluation stage passes the listing's
  downloaded thumbnail; Claude views it and photos override titles for
  physical attributes (axle count, cage fitted, model variant, condition) and
  feed the scam checks. A "tandem trailer" showing one wheel per side is
  single-axle, whatever the seller typed.
- **Stage 2 — verdict.** Shortlisted listings get one detail-page fetch
  (description, seller join year, listing age, photo count; ≤5 per cycle,
  best-margin first), then a final verdict. **hot** = matches ∧ meets your
  bargain level ∧ nothing dubious ∧ confidence ≥ 0.6 → immediate email.
- **Scam screening.** Dubious *matches* land in the digest's "⚠ flagged"
  section, never hot. Signals must corroborate: a great price alone never
  flags (finding great prices is the tool's purpose) — it takes company like
  a months-old account, stock photos, or copy-paste text.
- **Negotiation ("worth a lower offer").** A match priced one bargain step
  above your ask and listed 4–45 days (configurable) gets a suggested opening
  offer in its own digest section. Candidates come from stale sweeps and from
  *revisits*: previously digested near-misses re-enter the pipeline exactly
  once when they age into the window.
- **Report-once, per kind.** Email claims are recorded per (listing, kind) —
  `hot` / `digest` / `offer` — transactionally before SMTP, with rollback on
  failure. Nothing ever repeats within a kind; a digested near-miss may
  return once as an offer (or hot) via the revisit path.
- **Routing & oversight.** A target may set `email:` to route its hot alerts
  and digest sections to someone else. The owner's daily email always carries
  the full picture: their own sections, labeled copies of everything routed
  elsewhere, and a 24-hour operations summary (cycles, match/verdict stats,
  alerts, pipeline backlog) — sent daily even when nothing matched.
- **Ad-hoc valuation.** `fbmp value <listing-url>` prints an estimated
  second-hand value range for any listing — zero FB traffic when the listing
  is already known; never touches pipeline state.

## Tuning

Prompts live in `src/fbmp/prompts.py`; iterate offline (zero FB traffic) with
`fbmp eval-replay tests/fixtures/cards_sample.json` (stage 1) or
`fbmp eval-replay --stage 2 --target <id> tests/fixtures/details_sample.json`
(stage 2 — also prints the code-enforced disposition each listing would get).
Fixtures captured from real listings, thumbnails included:
`cards_trailers.json`, `details_saragosa_hot.json`.

## Humanization

The launchd timer only *wakes* the script; `src/fbmp/pacing.py` decides whether
that wake becomes a browsing session: random pre-sleep (0–8 min), ~15% skipped
wakes, quiet hours (before 6:30am / after 11pm), daily "away" gaps, variable
searches per session, organic entries via the Marketplace homepage, decoy
listing visits, and long-tailed delays between navigations. All knobs live in
`config/settings.yaml` under `pacing:`. If FB ever throws a checkpoint: the
watcher stands down, emails you, and you should consider `browser.headless: false`
and/or a longer `StartInterval`.

## Operations

| Situation | What happens / what to do |
|---|---|
| Session expires | You get one email per 24h; run `fbmp login` |
| Security checkpoint | Watcher stands down + emails; complete the challenge via `fbmp login` |
| All searches empty 3 cycles | Health email; inspect `data/debug/` page dumps |
| FB layout change | Selectors are all in `src/fbmp/scraper.py`; debug dumps show the new DOM |
| Pause everything | `scripts/uninstall_launchd.sh` (state is kept; reinstall anytime) |
| Pause one target | `active: false` in targets.yaml |

Logs: `data/logs/fbmp.log`. State: `data/fbmp.db` (SQLite — `listings`,
`evaluations`, `alerts`, `digest_queue`, `runs`). Delete `data/fbmp.db` to
start fresh (you may get re-alerts for live listings).
