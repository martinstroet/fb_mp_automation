"""Invoke `claude -p` headlessly and parse its JSON answers."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess

from . import prompts
from .config import DATA_DIR

log = logging.getLogger(__name__)

FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class ClaudeError(Exception):
    pass


def _extract_json(text: str) -> dict:
    text = FENCE_RE.sub("", text.strip()).strip()
    # tolerate stray prose around the object
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ClaudeError(f"no JSON object in output: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def run_claude(prompt: str, timeout: int = 180, model: str | None = None) -> dict:
    """One headless claude call; returns the parsed JSON object it produced."""
    # Read is allowlisted so prompts can reference local thumbnail files and
    # have claude view them (the photo-interpretation layer)
    cmd = ["claude", "-p", "--output-format", "json", "--allowedTools", "Read"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(DATA_DIR),  # neutral cwd: never load this repo's CLAUDE.md as context
    )
    if proc.returncode != 0:
        raise ClaudeError(f"claude exited {proc.returncode}: {proc.stderr[:400]}")
    try:
        envelope = json.loads(proc.stdout)
        answer = envelope.get("result", proc.stdout) if isinstance(envelope, dict) else proc.stdout
    except json.JSONDecodeError:
        answer = proc.stdout
    if not isinstance(answer, str):
        answer = json.dumps(answer)
    return _extract_json(answer)


def run_claude_retry(prompt: str, timeout: int = 180, model: str | None = None) -> dict:
    try:
        return run_claude(prompt, timeout, model)
    except (ClaudeError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        log.warning("claude call failed (%s) — retrying once", e)
        nudge = (
            prompt
            + "\n\nIMPORTANT: your previous reply was not valid JSON. "
            "Reply with ONLY the JSON object this time."
        )
        return run_claude(nudge, timeout, model)


def _index_results(parsed: dict) -> dict[str, dict]:
    out = {}
    for r in parsed.get("results", []):
        lid = str(r.get("listing_id", "")).strip()
        if lid:
            out[lid] = r
    return out


def triage(cfg, targets, listings: list[dict]) -> dict[str, dict]:
    """Stage 1: card-level match/rate. Returns {listing_id: result}."""
    timeout = cfg.get("evaluation", "claude_timeout_seconds", default=180)
    model = cfg.get("evaluation", "model")
    factor = cfg.get("negotiation", "max_price_factor", default=1.4)
    prompt = prompts.stage1_triage(targets, listings, stale_price_factor=factor)
    return _index_results(run_claude_retry(prompt, timeout, model))


def verdict(cfg, target, listings: list[dict]) -> dict[str, dict]:
    """Stage 2: detail-level final verdict for one target's shortlist."""
    timeout = cfg.get("evaluation", "claude_timeout_seconds", default=180)
    model = cfg.get("evaluation", "model")
    neg = cfg.get("negotiation", default={}) or {}
    prompt = prompts.stage2_verdict(
        target, listings,
        offer_min_days=neg.get("min_days_listed", 4),
        offer_max_days=neg.get("max_days_listed", 45),
    )
    return _index_results(run_claude_retry(prompt, timeout, model))


def market_value(cfg, listing: dict) -> dict:
    """Standalone market-value estimate for one listing (the `value` command)."""
    timeout = cfg.get("evaluation", "claude_timeout_seconds", default=180)
    model = cfg.get("evaluation", "model")
    return run_claude_retry(prompts.market_value(listing), timeout, model)


def queries_for_target(cfg, store, target) -> list[str]:
    """Explicit queries from YAML, else Claude-generated (cached until the
    description changes)."""
    if target.queries:
        return target.queries
    desc_hash = hashlib.sha256((target.description + target.notes).encode()).hexdigest()[:16]
    cache_key = f"queries:{target.id}:{desc_hash}"
    cached = store.kv_get(cache_key)
    if cached:
        return cached
    timeout = cfg.get("evaluation", "claude_timeout_seconds", default=180)
    model = cfg.get("evaluation", "model")
    parsed = run_claude_retry(prompts.query_generation(target), timeout, model)
    queries = [q.strip() for q in parsed.get("queries", []) if isinstance(q, str) and q.strip()]
    if not queries:
        raise ClaudeError(f"query generation returned nothing for target {target.id}")
    store.kv_set(cache_key, queries[:4])
    log.info("generated queries for %s: %s", target.id, queries[:4])
    return queries[:4]
