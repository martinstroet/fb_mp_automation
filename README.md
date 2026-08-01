# FB Marketplace Watcher

Personal watcher that checks Facebook Marketplace on an irregular ~30-minute
rhythm for listings matching your `config/targets.yaml` watch list, uses Claude
(headless `claude -p`, your existing subscription) to fuzzy-match listings,
estimate market value, rate the bargain, and flag dubious sellers. HOT listings
email you immediately; everything else lands in a daily digest. Nothing is ever
reported twice.

> **Heads up:** automating your own logged-in FB account is against Facebook's
> ToS and carries some risk of account restriction. The pacing engine goes to
> considerable lengths to look like a bored human, but the risk is not zero.

## Setup

```sh
scripts/setup.sh                 # venv, deps, checks Chrome + claude CLI
vim .env                         # Gmail app password (Google Account → Security → App passwords)
vim config/settings.yaml         # set marketplace.location_slug to your city
vim config/targets.yaml          # your watch list
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

## How it decides

- **Stage 1 (triage)**: every new listing's card info (title/price/location) is
  batched to Claude with all targets. Non-matches are rejected; decent matches
  go to the digest; promising bargains are shortlisted.
- **Stage 2 (verdict)**: shortlisted listings get their detail page fetched
  (description, seller join year, photo count), then Claude gives a final
  verdict with scam checks. `hot` requires: matches the target, meets your
  requested bargain level, nothing dubious, confidence ≥ 0.6 — enforced in
  code, not just prompt.
- **Dubious flags** demote a listing to the digest's "⚠ flagged" section:
  new FB account + cheap item, price too good to be true, suspected stock
  photos, vague copy-paste description.
- **"Worth a lower offer"**: ~25% of cycles, one search runs without the
  freshness filter (and a raised price ceiling) to find *older* listings.
  A match priced one bargain-step above your ask that's been listed 4–45 days
  (seller motivated, listing not abandoned; window configurable under
  `negotiation:` in settings.yaml) lands in its own digest section with a
  suggested opening offer.

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
