"""Prompt templates for the two claude -p evaluation stages + query generation."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def _thumb(l: dict) -> str | None:
    """Absolute path to the listing's local thumbnail, if it exists on disk."""
    p = l.get("thumb_path")
    return p if p and Path(p).exists() else None


def _extra_photos(l: dict) -> list[str] | None:
    """Saved gallery photos beyond the thumbnail: DB rows carry them as a JSON
    string, fixtures/detail dicts as a list. Only files still on disk count."""
    raw = l.get("image_paths")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, list):
        return None
    out = [p for p in raw if isinstance(p, str) and Path(p).exists()]
    return out or None


def _targets_block(targets) -> str:
    rows = []
    for t in targets:
        rows.append(
            {
                "target_id": t.id,
                "description": t.description,
                "notes": t.notes or None,
                "wanted_bargain_level": t.bargain_level,
                "max_price_aud": t.max_price,
            }
        )
    return json.dumps(rows, indent=2)


FRAMING = (
    "You are evaluating second-hand Facebook Marketplace listings in Australia. "
    "All prices are AUD. Today is {date}. Be conservative: listing titles are "
    "noisy and sellers exaggerate. When you cannot estimate a market value from "
    "the information given, say so rather than guessing wildly.\n"
)


def stage1_triage(targets, listings: list[dict], stale_price_factor: float = 1.4) -> str:
    items = [
        {
            "listing_id": l["listing_id"],
            "title": l.get("title"),
            "price_text": l.get("price_text"),
            "price_aud": l.get("price_aud"),
            "location": l.get("location"),
            "found_by_query": l.get("source_query"),
            "listing_age": (
                "older (found by an unfiltered sweep — likely listed days-to-weeks ago)"
                if l.get("sweep") == "stale" else "posted within the last day"
            ),
            "thumbnail_file": _thumb(l),
        }
        for l in listings
    ]
    return (
        FRAMING.format(date=dt.date.today().isoformat())
        + "\n## Buying targets\n" + _targets_block(targets)
        + "\n\n## New listings (card-level info only)\n" + json.dumps(items, indent=2)
        + """

For each listing decide whether it plausibly satisfies one of the buying targets,
estimate its second-hand market value in AUD, and rate the asking price.

bargain_rating scale: "well_below_market" (≲60% of market value),
"below_market" (~60-85%), "market" (~85-110%), "above_market" (>110%),
"unknown" (cannot estimate from the title alone).

Set "shortlist": true when the listing matches a target AND either (a) its
rating meets or beats that target's wanted_bargain_level, or (b) the rating is
"unknown" but the match is strong enough that the detail page is worth a look,
or (c) the listing_age is "older" and the rating is within ONE step below the
wanted level (e.g. rated "market" when the target wants "below_market") — an
aged listing at a near-miss price is a negotiation opportunity worth inspecting.

max_price_aud is the buyer's walk-away purchase budget, not a hard match
filter: a listing marked "older" can still match with an ask up to ~"""
        + f"{round((stale_price_factor - 1) * 100)}"
        + """% above it — the plan is to negotiate down. Rate bargain_rating
purely on asking price vs market value, independent of the budget.

Where a listing has a "thumbnail_file", it is a local photo of the item. Use
the Read tool to view it whenever a physical attribute the target cares about
(axle or wheel count, cage fitted, item type/size/condition) is not settled by
the text — titles are unreliable and photos are authoritative. e.g. a trailer
showing one wheel per side is single-axle no matter what the title says. Let
what you see raise or sink match_confidence accordingly.

Respond with ONLY a single JSON object, no prose, no markdown fences:
{"results": [{"listing_id": "...", "matched_target_id": "..." or null,
"match_confidence": 0.0-1.0, "estimated_market_value_aud": integer or null,
"bargain_rating": "...", "shortlist": true/false, "rationale": "one line"}]}
Include every listing exactly once."""
    )


def stage2_verdict(target, listings: list[dict], current_year: int | None = None,
                   offer_min_days: int = 4, offer_max_days: int = 45) -> str:
    current_year = current_year or dt.date.today().year
    items = [
        {
            "listing_id": l["listing_id"],
            "title": l.get("title"),
            "price_aud": l.get("price_aud"),
            "price_text": l.get("price_text"),
            "location": l.get("location"),
            "description": (l.get("description") or "")[:2500],
            "seller_name": l.get("seller_name"),
            "seller_joined_facebook_year": l.get("seller_joined_year"),
            "photo_count_approx": l.get("image_count"),
            "listed": l.get("listed_ago_text"),
            "thumbnail_file": _thumb(l),
            "more_photo_files": _extra_photos(l),
        }
        for l in listings
    ]
    return (
        FRAMING.format(date=dt.date.today().isoformat())
        + "\n## The buying target these listings were shortlisted for\n"
        + _targets_block([target])
        + "\n\n## Shortlisted listings (full detail)\n" + json.dumps(items, indent=2)
        + f"""

Give a final verdict on each listing. Where a listing has a "thumbnail_file",
view it with the Read tool BEFORE deciding — photos are primary evidence for
physical attributes (axle/wheel count, cage, size, variant, condition) and for
the scam checks below; what you see overrides what the title claims.

"more_photo_files" lists the rest of the listing's photo gallery at higher
resolution. Whenever the thumbnail leaves anything decision-relevant
unsettled — a physical attribute the target cares about, the item's exact
identity/variant, its condition, or one of the scam checks — keep Reading
additional photos until it is settled or the photos run out. Stop as soon as
you have a conclusive view: an already-clear listing needs no extra photos.
Rarely, a trailing gallery photo is actually an unrelated listing picked up
from FB's recommendation rail — ignore any photo that plainly shows a
different item, and never let one change the verdict.

Scam/dubiousness checks — set "dubious": true and list every reason that applies:
- "new_account_cheap_item": the seller's account is only a few months old
  (joined_facebook_year {current_year} — an earlier join is an established
  account, not a scam signal) AND the item is priced well below market
- "price_too_good": asking ≲50% of your market value estimate AND at least one
  other suspicion signal (brand-new account, stock photos, vague copy-paste
  text, urgency pressure, off-platform contact). Finding genuinely cheap
  listings from legitimate sellers is this system's entire purpose — a great
  price from an established account with specific, personal details is a
  bargain to alert on, never dubious on price alone.
- "stock_photos_suspected": single pristine catalog-style photo, or the
  thumbnail looks like a retailer image rather than a private seller's (note:
  photo_count_approx overcounts — treat only very low values (0-1) as a
  signal, never high ones as reassurance)
- "vague_description": no condition/pickup details, generic copy-paste text
- "other": anything else that smells off (explain in rationale)
A listing is dubious when signals corroborate each other, not on one soft
signal from an otherwise-legitimate listing.

verdict rules:
- "hot": truly matches the target, price meets the wanted_bargain_level, nothing dubious.
- "offer": matches the target and is NOT dubious, but the asking price sits
  above the wanted level by roughly one rating step, AND the "listed" field
  shows it has been up for {offer_min_days}-{offer_max_days} days (younger =
  seller not yet motivated; older = likely abandoned/stale) — a seller who has
  waited that long may accept a lower offer. Set
  "suggested_offer_aud" to a realistic opening offer (typically the price that
  would bring it to the wanted bargain level, rounded to something a person
  would say).
- "digest": decent-but-not-hot matches, or dubious-but-interesting ones.
- "reject": non-matches.

bargain_rating scale: "well_below_market" (≲60% of market value),
"below_market" (~60-85%), "market" (~85-110%), "above_market" (>110%), "unknown".

Respond with ONLY a single JSON object, no prose, no markdown fences:
{{"results": [{{"listing_id": "...", "final_match": true/false,
"estimated_market_value_aud": integer or null, "bargain_rating": "...",
"meets_target_level": true/false, "dubious": true/false,
"dubious_reasons": ["..."], "confidence": 0.0-1.0,
"verdict": "hot"|"offer"|"digest"|"reject",
"suggested_offer_aud": integer or null, "rationale": "one line"}}]}}
Include every listing exactly once."""
    )


def market_value(listing: dict) -> str:
    item = {
        "title": listing.get("title"),
        "asking_price": listing.get("price_text") or listing.get("price_aud"),
        "location": listing.get("location"),
        "description": (listing.get("description") or "")[:2500] or None,
        "listed": listing.get("listed_ago_text"),
        "photo_count_approx": listing.get("image_count"),
        "thumbnail_file": _thumb(listing),
        "more_photo_files": _extra_photos(listing),
    }
    return (
        FRAMING.format(date=dt.date.today().isoformat())
        + "\n## Listing\n" + json.dumps(item, indent=2)
        + """

If "thumbnail_file" is present, view it with the Read tool first — the photo
often pins down model/variant and condition better than the text. If it leaves
the model/variant or condition uncertain, keep Reading from "more_photo_files"
(the rest of the gallery, higher resolution) until settled; stop as soon as
the photos are conclusive.

Identify the product (brand, model, variant where determinable) and estimate
what it would realistically sell for second-hand on the Australian private
market. Give a range wide enough to cover the condition/variant uncertainty in
the information above, but no wider.

bargain_rating scale (asking price vs your mid estimate): "well_below_market"
(≲60%), "below_market" (~60-85%), "market" (~85-110%), "above_market" (>110%),
"unknown" (cannot identify the product well enough to price it).

Respond with ONLY a single JSON object, no prose, no markdown fences:
{"product": "best identification of what is being sold",
"new_price_aud": integer or null,
"used_value_low_aud": integer or null, "used_value_mid_aud": integer or null,
"used_value_high_aud": integer or null,
"bargain_rating": "...", "confidence": 0.0-1.0,
"value_drivers": ["factors or unknowns that most move the price"],
"rationale": "one or two lines"}"""
    )


def query_generation(target) -> str:
    return (
        "You generate Facebook Marketplace search queries for an Australian buyer.\n"
        f"Buying target: {target.description}\n"
        + (f"Notes: {target.notes}\n" if target.notes else "")
        + """
Produce 2-4 short, concrete Marketplace search strings a person would actually
type (2-4 words each; think brand names, item types — not full sentences).
Respond with ONLY a JSON object, no prose: {"queries": ["...", "..."]}"""
    )
